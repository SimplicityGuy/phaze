"""Run the verified two-lane pytest plan with isolated resources."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess  # nosec B404 -- every production command is a fixed argv sequence.
import sys
import tempfile
import time
from typing import TYPE_CHECKING, Protocol

from scripts.parallel_test_manifests import (
    DEFAULT_SHARDS_PATH,
    ManifestError,
    ManifestPlan,
    build_manifest_plan,
    collect_canonical_node_ids,
    load_shard_definitions,
    write_manifest_arguments,
)


if TYPE_CHECKING:
    from types import FrameType

    from psycopg import Connection


_SEAT_KEYS = frozenset({"TEST_DATABASE_URL", "MIGRATIONS_TEST_DATABASE_URL", "PHAZE_REDIS_URL"})
_EXPORT_RE = re.compile(r'^export ([A-Z_]+)="([^"]+)"$')
# phaze-qov36: the exact shapes `scripts/provision-test-seat.sh` prints for a `just test-db-for`
# seat. Only a triplet of exactly this shape can be a local harness seat worth splitting into
# lanes; anything else -- CI's 5432 service container, the shared `phaze_test`, a hand-built DSN --
# is an explicit environment contract and stays verbatim.
_LOCAL_SEAT_DSN_RE = re.compile(r"postgresql\+asyncpg://phaze:phaze@localhost:(?P<port>[0-9]+)/phaze_(?P<seat>[a-z][a-z0-9_]*)_test")
_LOCAL_SEAT_REDIS_RE = re.compile(r"redis://localhost:(?P<port>[0-9]+)/(?P<index>[1-9][0-9]*)")
_REDIS_EXHAUSTED_MARKER = "allocatable Redis logical DBs"
CALLER_SEAT_DECLINED = 3
CALLER_SEAT_FATAL = 5
CALLER_SEAT_BUSY = 4
_REGISTRY_NOT_HELD = 5
_RELEASE_ATTEMPTS = 5
_RELEASE_DELAY_SECONDS = 1.0
_QUIESCE_SECONDS = 30.0
_TERMINATE_SECONDS = 10.0


class ProcessHandle(Protocol):
    """The subprocess operations used by the supervisor."""

    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...


@dataclass(frozen=True)
class Seat:
    """A runner-owned database and Redis seat."""

    name: str
    environment: Mapping[str, str]


@dataclass(frozen=True)
class LaneLaunch:
    """One isolated pytest process invocation."""

    name: str
    command: tuple[str, ...]
    environment: Mapping[str, str]


ProvisionSeat = Callable[[str], Seat]
SeatIndexLookup = Callable[[str], str | None]
ReleaseSeat = Callable[[str], int]
SpawnLane = Callable[[LaneLaunch], ProcessHandle]
RunCommand = Callable[[Sequence[str], Mapping[str, str]], int]
# phaze-vad6y: a SEPARATE dependency from `RunCommand`, not another argv tuple in the same
# fault-injection sequence, because this one call needs its STDOUT (the record-start token) and
# the others only ever need a status code.
RecordCoverageScopeStart = Callable[[], str | None]
SignalGroup = Callable[[int, int], None]
GroupAlive = Callable[[int], bool]
Sleep = Callable[[float], None]


@dataclass(frozen=True)
class SupervisorDependencies:
    """Injectable operating-system boundaries used by fault tests."""

    provision: ProvisionSeat
    release: ReleaseSeat
    spawn: SpawnLane
    run_command: RunCommand
    record_coverage_scope_start: RecordCoverageScopeStart
    signal_group: SignalGroup
    group_alive: GroupAlive
    sleep: Sleep = time.sleep


@dataclass(frozen=True)
class CallerSeatVerdict:
    """Whether a caller-exported triplet is a `just test-db-for` seat the runner may derive lanes from.

    Three outcomes: ``seat_id`` set (split it into lanes); declined (``seat_id`` None, ``fatal``
    False -- run `test-cov` verbatim); or ``fatal`` -- the triplet claims to be a harness seat and
    the registry contradicts it, so running ANYTHING on it, serial included, could land on an index
    that now belongs to another live seat.
    """

    seat_id: str | None
    reason: str
    fatal: bool = False


class RegistryUnavailableError(RuntimeError):
    """The seat registry could not be read, which is not the same answer as "not registered"."""


def classify_caller_seat(environ: Mapping[str, str], *, pg_port: str, redis_port: str, seat_index: SeatIndexLookup) -> CallerSeatVerdict:
    """Accept only a triplet minted by `just test-db-for` on this harness; decline everything else.

    phaze-qov36. CI and every shape check come before the registry lookup, so an environment that
    is plainly not a local seat (CI above all) never reaches Docker. A shape-perfect triplet is then
    held to what the registry says BEFORE the opt-out is honoured: a released or reclaimed seat, a
    stale Redis index, or an unreadable registry is fatal on every path, serial included, because
    the exported index may already belong to another live seat. Every verdict names its reason,
    because the caller's gate log is the only place a reader can learn why a run went the way it did.
    """

    if environ.get("CI"):
        return CallerSeatVerdict(None, "CI is set, and a CI environment is always honoured verbatim")
    missing = sorted(key for key in _SEAT_KEYS if not environ.get(key))
    if missing:
        return CallerSeatVerdict(None, f"the caller's triplet is incomplete ({', '.join(missing)} unset)")
    main = _LOCAL_SEAT_DSN_RE.fullmatch(environ["TEST_DATABASE_URL"])
    if main is None or main["port"] != pg_port:
        return CallerSeatVerdict(None, f"TEST_DATABASE_URL is not a `just test-db-for` seat database on localhost:{pg_port}")
    seat_id = main["seat"]
    migrations = f"postgresql+asyncpg://phaze:phaze@localhost:{pg_port}/phaze_{seat_id}_migrations_test"
    if environ["MIGRATIONS_TEST_DATABASE_URL"] != migrations:
        return CallerSeatVerdict(None, f"MIGRATIONS_TEST_DATABASE_URL is not the migrations database of seat {seat_id!r}")
    redis = _LOCAL_SEAT_REDIS_RE.fullmatch(environ["PHAZE_REDIS_URL"])
    if redis is None or redis["port"] != redis_port:
        return CallerSeatVerdict(None, f"PHAZE_REDIS_URL is not a seat's logical DB on localhost:{redis_port}")
    remedy = "Re-run `just test-db-for <your seat name>` and re-export the three lines it prints."
    try:
        registered = seat_index(seat_id)
    except RegistryUnavailableError as exc:
        return CallerSeatVerdict(None, f"the harness seat registry could not be read ({exc}). Start the harness with `just test-db`", fatal=True)
    if registered is None:
        return CallerSeatVerdict(
            None,
            f"{seat_id!r} has the exact shape of a `just test-db-for` seat but is not registered -- released or reclaimed, "
            f"so Redis DB {redis['index']} may now belong to another live seat. {remedy}",
            fatal=True,
        )
    if registered != redis["index"]:
        return CallerSeatVerdict(
            None,
            f"PHAZE_REDIS_URL names Redis DB {redis['index']}, but the registry holds DB {registered} for {seat_id!r}: "
            f"the export is stale and DB {redis['index']} may belong to another live seat. {remedy}",
            fatal=True,
        )
    parallel = environ.get("PHAZE_TEST_PARALLEL", "1")
    if parallel != "1":
        return CallerSeatVerdict(None, f"PHAZE_TEST_PARALLEL={parallel} (anything but 1) forces the serial path")
    return CallerSeatVerdict(seat_id, f"{seat_id!r} is a registered `just test-db-for` seat on Redis DB {registered}")


def registry_seat_index(repo_root: Path, redis_container: str) -> SeatIndexLookup:
    """The real, read-only `redis-seat-registry.sh seat-index` lookup."""

    bash_binary = shutil.which("bash")
    if bash_binary is None:
        raise RuntimeError("the required 'bash' command is not installed")

    def lookup(seat_id: str) -> str | None:
        result = subprocess.run(  # noqa: S603  # nosec B603 -- fixed in-repo script; seat id matched a strict pattern.
            (bash_binary, str(repo_root / "scripts/redis-seat-registry.sh"), "seat-index", "--redis-container", redis_container, "--seat", seat_id),
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == _REGISTRY_NOT_HELD:
            return None
        index = result.stdout.strip()
        if result.returncode != 0 or not index.isdigit():
            raise RegistryUnavailableError(f"exit {result.returncode}: {(result.stderr or result.stdout).strip()}")
        return index

    return lookup


def hold_caller_seat(test_database_url: str) -> Connection[tuple[object, ...]] | None:
    """Take the caller seat's own session lock for the whole two-lane run, or refuse at once.

    phaze-qov36. The lanes never touch the caller's database, so nothing ELSE would stop a second
    gate on the same seat (`bh work check` in one terminal and `just check` in another, or a re-run
    after a tool-call timeout) from provisioning the SAME `<seat>-lane-a/-b`, splitting their locks
    and releasing each other's seats. This is the very lock pytest takes on that database
    (`tests/db_guard.py`), so the second gate -- or any pytest on the caller's seat -- is refused
    before record-start, collection or provisioning, exactly as a serial run on the seat always was.
    It reads nothing and writes nothing; it only holds a connection.
    """

    from tests.db_guard import acquire_exclusive_session_lock  # noqa: PLC0415 -- test-harness module, needed only on this path

    return acquire_exclusive_session_lock(test_database_url, explicit=True)


def release_caller_seat(connection: Connection[tuple[object, ...]] | None) -> None:
    from tests.db_guard import release_exclusive_session_lock  # noqa: PLC0415 -- see hold_caller_seat

    release_exclusive_session_lock(connection)


def _parse_seat_exports(output: str, seat_name: str) -> Seat:
    exports: dict[str, str] = {}
    for line in output.splitlines():
        match = _EXPORT_RE.fullmatch(line.strip())
        if match is not None:
            exports[match.group(1)] = match.group(2)
    if set(exports) != _SEAT_KEYS:
        raise RuntimeError(f"seat {seat_name!r} did not return exactly the required exports: {sorted(exports)}")
    return Seat(name=seat_name, environment=exports)


def production_dependencies(repo_root: Path) -> SupervisorDependencies:
    """Create the real command boundaries for a repository checkout."""

    just_binary = shutil.which("just")
    if just_binary is None:
        raise RuntimeError("the required 'just' command is not installed")

    def provision(seat_name: str) -> Seat:
        result = subprocess.run(  # noqa: S603  # nosec B603 -- fixed command and runner-owned seat name.
            (just_binary, "test-db-for", seat_name),
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            if _REDIS_EXHAUSTED_MARKER in detail:
                # phaze-qov36: a caller-seat gate holds three indices (its own + two lanes), so
                # exhaustion is likelier than it was. Fail loudly and name the cause -- never
                # fall back to serial behind the caller's back.
                raise RuntimeError(
                    f"lane seat {seat_name!r} could not get a Redis logical DB: the harness index pool is exhausted. "
                    "A two-lane gate holds two lane indices on top of any seat the caller exported. Free stale seats with "
                    "`just test-db-reclaim --apply`, or run serial on your own seat with PHAZE_TEST_PARALLEL=0.\n" + detail
                )
            raise RuntimeError(f"seat provisioning failed for {seat_name!r}: {detail}")
        return _parse_seat_exports(result.stdout, seat_name)

    def release(seat_name: str) -> int:
        return subprocess.run(  # noqa: S603  # nosec B603 -- fixed command and runner-owned seat name.
            (just_binary, "test-db-release", seat_name), cwd=repo_root, check=False
        ).returncode

    def spawn(launch: LaneLaunch) -> ProcessHandle:
        return subprocess.Popen(  # noqa: S603  # nosec B603 -- launch is constructed from fixed pytest arguments.
            launch.command, cwd=repo_root, env=dict(launch.environment), start_new_session=True
        )

    def run_command(command: Sequence[str], environment: Mapping[str, str]) -> int:
        return subprocess.run(  # noqa: S603  # nosec B603 -- callers supply fixed coverage commands.
            tuple(command), cwd=repo_root, env=dict(environment), check=False
        ).returncode

    def record_coverage_scope_start() -> str | None:
        """A SEPARATE call from `run_command` (phaze-vad6y): this one needs the token printed to
        STDOUT, not just a status code. Best-effort -- any failure (a nonzero exit, `uv` missing,
        or the script itself not runnable) yields `None`, which `_postprocess` reads as "no stamp
        is possible this run", never as a reason to abort the two-lane run.
        """
        uv_binary = shutil.which("uv")
        if uv_binary is None:
            return None
        try:
            result = subprocess.run(  # noqa: S603  # nosec B603 -- resolved executable, fixed argv, no shell.
                (uv_binary, "run", "python", "scripts/stamp_coverage_scope.py", "--record-start"),
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return None
        if result.returncode != 0:
            return None
        token = result.stdout.strip()
        return token or None

    def signal_group(process_group: int, signum: int) -> None:
        os.killpg(process_group, signum)

    def group_alive(process_group: int) -> bool:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    return SupervisorDependencies(
        provision=provision,
        release=release,
        spawn=spawn,
        run_command=run_command,
        record_coverage_scope_start=record_coverage_scope_start,
        signal_group=signal_group,
        group_alive=group_alive,
    )


def derive_parallel_seat_prefix(repo_root: Path) -> str:
    """Derive one stable, worktree-specific prefix for both parallel lanes."""

    bash_binary = shutil.which("bash")
    if bash_binary is None:
        raise RuntimeError("the required 'bash' command is not installed")
    result = subprocess.run(  # noqa: S603  # nosec B603 -- fixed in-repo derivation script and checkout path.
        (bash_binary, str(repo_root / "scripts/derive-validate-seat-name.sh"), str(repo_root)),
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"parallel seat-name derivation failed: {(result.stderr or result.stdout).strip()}")
    return f"{result.stdout.strip()}-parallel"


class ParallelTestSupervisor:
    """Own two pytest process groups and their temporary test seats."""

    def __init__(self, repo_root: Path, dependencies: SupervisorDependencies) -> None:
        self._repo_root = repo_root
        self._dependencies = dependencies
        self._processes: list[ProcessHandle] = []
        self._received_signal: int | None = None

    def _forward_signal(self, signum: int, _frame: FrameType | None) -> None:
        self._received_signal = signum
        self._signal_active_groups(signum)

    def _signal_active_groups(self, signum: int) -> None:
        for process in self._processes:
            if process.poll() is not None and not self._dependencies.group_alive(process.pid):
                continue
            try:
                self._dependencies.signal_group(process.pid, signum)
            except ProcessLookupError:
                continue

    def _wait_for_group(self, process_group: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self._dependencies.group_alive(process_group):
            if time.monotonic() >= deadline:
                return False
            self._dependencies.sleep(0.05)
        return True

    def _wait_processes(self) -> list[int]:
        statuses: list[int] = []
        for process in self._processes:
            statuses.append(process.wait())
        for process in self._processes:
            if not self._wait_for_group(process.pid, _QUIESCE_SECONDS):
                self._dependencies.signal_group(process.pid, signal.SIGTERM)
                if not self._wait_for_group(process.pid, _TERMINATE_SECONDS):
                    self._dependencies.signal_group(process.pid, signal.SIGKILL)
                    self._wait_for_group(process.pid, _TERMINATE_SECONDS)
        return statuses

    def _stop_processes(self) -> None:
        self._signal_active_groups(signal.SIGTERM)
        for process in self._processes:
            try:
                process.wait(timeout=_TERMINATE_SECONDS)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    self._dependencies.signal_group(process.pid, signal.SIGKILL)
                process.wait()
        for process in self._processes:
            self._wait_for_group(process.pid, _TERMINATE_SECONDS)

    def _release_seats(self, seats: Sequence[Seat]) -> bool:
        all_released = True
        for seat in seats:
            released = False
            for attempt in range(_RELEASE_ATTEMPTS):
                if self._dependencies.release(seat.name) == 0:
                    released = True
                    break
                if attempt + 1 < _RELEASE_ATTEMPTS:
                    self._dependencies.sleep(_RELEASE_DELAY_SECONDS)
            all_released = released and all_released
        return all_released

    def _lane_launches(self, plan: ManifestPlan, run_root: Path, seats: Sequence[Seat]) -> tuple[LaneLaunch, ...]:
        manifest_paths = write_manifest_arguments(plan, run_root / "manifests")
        launches: list[LaneLaunch] = []
        for lane, manifest_path, seat in zip(plan.lanes, manifest_paths, seats, strict=True):
            lane_root = run_root / lane.name
            cache_dir = lane_root / "pytest-cache"
            temp_dir = lane_root / "tmp"
            bytecode_dir = lane_root / "pycache"
            report_dir = lane_root / "bh-reports"
            for path in (cache_dir, temp_dir, bytecode_dir, report_dir):
                path.mkdir(parents=True, exist_ok=True)

            environment = dict(os.environ)
            environment.pop("PHAZE_TEST_DB_ALLOW_SHARED", None)
            environment.pop("PYTEST_ADDOPTS", None)
            environment.update(seat.environment)
            environment.update(
                {
                    "BH_TEST_REPORT_DIR": str(report_dir),
                    "COVERAGE_FILE": str(lane_root / ".coverage"),
                    # phaze-bein3: --cov-context=test below needs CTracer+greenlet; the default
                    # sys.monitoring core records only the first test per line.
                    "PHAZE_COVERAGE_GREENLET": "greenlet",
                    "PYTHONPYCACHEPREFIX": str(bytecode_dir),
                    "TMPDIR": str(temp_dir),
                }
            )
            # The outer recipe already prepared this worktree's environment. Letting both lanes
            # enter uv's sync path together can uninstall the same packages concurrently, so each
            # worker must treat the shared environment as read-only.
            command = (
                "uv",
                "run",
                "--no-sync",
                "pytest",
                f"@{manifest_path}",
                "--cov",
                "--cov-context=test",
                "--cov-fail-under=0",
                "--cov-report=",
                f"--junitxml={lane_root / 'junit.xml'}",
                "-o",
                "junit_family=legacy",
                "-o",
                f"cache_dir={cache_dir}",
                # phaze-7w18f: argv-visible, per-worktree marker so a path-scoped `pkill -f`
                # cannot match a sibling seat's lane. `--rootdir` is a genuine pytest option and
                # a no-op for collection here (pyproject.toml already sits at `self._repo_root`,
                # so this is where pytest would already land), same reasoning as the justfile
                # recipes' `--rootdir={{justfile_directory()}}`.
                f"--rootdir={self._repo_root}",
            )
            launches.append(LaneLaunch(name=lane.name, command=command, environment=environment))
        return tuple(launches)

    def _postprocess(self, run_root: Path, scope_token: str | None) -> int:
        environment = dict(os.environ)
        environment["COVERAGE_FILE"] = str(self._repo_root / ".coverage")
        coverage_files = tuple(str(run_root / lane / ".coverage") for lane in ("lane-a", "lane-b"))
        commands = [
            ("uv", "run", "coverage", "combine", *coverage_files),
            ("uv", "run", "coverage", "json", "--fail-under=0"),
        ]
        if scope_token is not None:
            # phaze-vad6y: stamps the `_phaze_scope` marker `find_reusable_report`
            # (scripts/branch_coverage_check.py) needs, matching test-cov. This is the path
            # `just check` runs by DEFAULT (no caller-owned seat exported) and it writes its own
            # coverage.json here rather than going through `just test-cov` -- skipping it left the
            # default path permanently unstamped, a gap a reviewing gate run with seat exports set
            # (the SERIAL FALLBACK, already stamped) could not see.
            # `--expect-token` closes a race a bare `--record-start`/stamp pair does not: a SECOND
            # concurrent run (or a stale `--record-start` never consumed) can overwrite the ONE
            # marker file between this run's own record and its own stamp. The token is minted by
            # `record_coverage_scope_start` and never touches the shared marker file except as
            # the value written into it, so a mismatch there means "someone else's run clobbered
            # my marker" and the stamp correctly refuses rather than trusting the wrong tree state.
            commands.append(("uv", "run", "python", "scripts/stamp_coverage_scope.py", "--expect-token", scope_token))
        commands.extend(
            [
                ("uv", "run", "coverage", "xml", "--fail-under=0"),
                ("uv", "run", "coverage", "report", "--fail-under=95"),
                ("uv", "run", "python", "scripts/coverage_floor.py"),
            ]
        )
        for command in commands:
            status = self._dependencies.run_command(command, environment)
            if status != 0:
                return status
        return 0

    def run(self, plan: ManifestPlan, run_root: Path, *, seat_prefix: str) -> int:
        """Run both lanes, clean them up, then combine and enforce coverage."""

        seats: list[Seat] = []
        statuses: list[int] = []
        runtime_error = False
        released = False
        # phaze-vad6y: capture HEAD + working-tree cleanliness NOW, before either lane starts
        # reading files -- it has to happen before seat provisioning / lane spawning, not folded
        # into `_postprocess`, so the marker describes the tree before pytest can possibly change
        # what HEAD or dirtiness mean. A `None` token (HEAD unresolvable, or the subprocess itself
        # failed) means `_postprocess` skips the stamp attempt entirely rather than calling a
        # stamp step that could only ever refuse -- a failure here must never abort a two-lane
        # test run over a marker file, and never leaves a stale marker for a LATER, unrelated run
        # to find (`record_start` in stamp_coverage_scope.py clears any existing marker first).
        scope_token = self._dependencies.record_coverage_scope_start()
        old_handlers = {signum: signal.signal(signum, self._forward_signal) for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)}
        try:
            try:
                for lane in plan.lanes:
                    seat = self._dependencies.provision(f"{seat_prefix}-{lane.name}")
                    seats.append(seat)
                for launch in self._lane_launches(plan, run_root, seats):
                    if self._received_signal is not None:
                        break
                    self._processes.append(self._dependencies.spawn(launch))
                if len(self._processes) == len(plan.lanes):
                    statuses = self._wait_processes()
                else:
                    runtime_error = self._received_signal is None
                    self._stop_processes()
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                sys.stderr.write(f"parallel test runner error: {exc}\n")
                runtime_error = True
                self._stop_processes()
            finally:
                released = self._release_seats(seats)
        finally:
            for signum, old_handler in old_handlers.items():
                signal.signal(signum, old_handler)

        if self._received_signal is not None:
            return 128 + self._received_signal
        if runtime_error or not released:
            return 1
        child_failure = next((status for status in statuses if status != 0), 0)
        if child_failure:
            return child_failure if child_failure > 0 else 128 - child_failure
        return self._postprocess(run_root, scope_token)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--shards", type=Path, default=DEFAULT_SHARDS_PATH)
    # phaze-qov36: `just test-validate` classifies a caller-exported triplet first, then hands an
    # accepted seat's id back as the lane prefix, so lanes are `<seat>-lane-a` / `<seat>-lane-b`.
    parser.add_argument("--classify-caller-seat", action="store_true")
    parser.add_argument("--pg-port", default="5433")
    parser.add_argument("--redis-port", default="6380")
    parser.add_argument("--redis-container", default="phaze-test-redis")
    parser.add_argument("--seat-prefix")
    return parser


def _classify_main(args: argparse.Namespace, repo_root: Path) -> int:
    try:
        seat_index = registry_seat_index(repo_root, args.redis_container)
    except RuntimeError as exc:
        sys.stderr.write(f"parallel test runner error: {exc}\n")
        return 2
    verdict = classify_caller_seat(os.environ, pg_port=args.pg_port, redis_port=args.redis_port, seat_index=seat_index)
    if verdict.fatal:
        sys.stderr.write(f"❌ caller seat refused, and nothing was run on it: {verdict.reason}\n")
        return CALLER_SEAT_FATAL
    if verdict.seat_id is None:
        sys.stderr.write(f"↩️  caller seat not split into lanes: {verdict.reason}.\n")
        return CALLER_SEAT_DECLINED
    sys.stdout.write(f"{verdict.seat_id}\n")
    return 0


def _gate_main(args: argparse.Namespace, repo_root: Path) -> int:
    shards_path = args.shards if args.shards.is_absolute() else repo_root / args.shards
    try:
        shards = load_shard_definitions(shards_path)
        canonical = collect_canonical_node_ids(repo_root)
        plan = build_manifest_plan(canonical, shards)
    except ManifestError as exc:
        sys.stderr.write(f"parallel test runner error: {exc}\n")
        return 2

    counts = ", ".join(f"{lane.name}={len(lane.node_ids)}" for lane in plan.lanes)
    sys.stdout.write(f"Verified parallel test partition: {counts}; union={len(plan.canonical_node_ids)} nodes in canonical order.\n")
    try:
        seat_prefix = args.seat_prefix or derive_parallel_seat_prefix(repo_root)
        dependencies = production_dependencies(repo_root)
    except RuntimeError as exc:
        sys.stderr.write(f"parallel test runner error: {exc}\n")
        return 2
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="phaze-parallel-tests-") as temporary:
        supervisor = ParallelTestSupervisor(repo_root, dependencies)
        status = supervisor.run(plan, Path(temporary), seat_prefix=seat_prefix)
    elapsed = time.monotonic() - started
    sys.stdout.write(f"Parallel coverage gate finished with status {status} in {elapsed:.2f} s.\n")
    return status


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    if args.classify_caller_seat:
        return _classify_main(args, repo_root)
    if not args.seat_prefix:
        return _gate_main(args, repo_root)

    # phaze-qov36: lanes derived from a caller's seat. Hold that seat FIRST -- before record-start,
    # collection or provisioning -- so a second gate on the same seat is refused before it can
    # touch anything the first one owns.
    caller_dsn = os.environ.get("TEST_DATABASE_URL", "")
    if not caller_dsn:
        sys.stderr.write("parallel test runner error: --seat-prefix derives lanes from a caller seat, but TEST_DATABASE_URL is not exported\n")
        return 2
    try:
        held = hold_caller_seat(caller_dsn)
    except RuntimeError as exc:
        sys.stderr.write(f"❌ caller seat busy -- refusing before collection; nothing was provisioned or run:\n{exc}\n")
        return CALLER_SEAT_BUSY
    if held is None:
        # Only PHAZE_TEST_DB_ALLOW_SHARED=1 gets here. Lanes strip it for themselves, but without the
        # caller lock two gates on this seat would share lanes, so the bypass is refused, not honoured.
        sys.stderr.write("❌ PHAZE_TEST_DB_ALLOW_SHARED=1 would skip the caller-seat lock that keeps two gates off the same lanes; unset it.\n")
        return CALLER_SEAT_BUSY
    try:
        return _gate_main(args, repo_root)
    finally:
        release_caller_seat(held)


if __name__ == "__main__":
    raise SystemExit(main())
