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


_SEAT_KEYS = frozenset({"TEST_DATABASE_URL", "MIGRATIONS_TEST_DATABASE_URL", "PHAZE_REDIS_URL"})
_EXPORT_RE = re.compile(r'^export ([A-Z_]+)="([^"]+)"$')
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
ReleaseSeat = Callable[[str], int]
SpawnLane = Callable[[LaneLaunch], ProcessHandle]
RunCommand = Callable[[Sequence[str], Mapping[str, str]], int]
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
    signal_group: SignalGroup
    group_alive: GroupAlive
    sleep: Sleep = time.sleep


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
            raise RuntimeError(f"seat provisioning failed for {seat_name!r}: {(result.stderr or result.stdout).strip()}")
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
            )
            launches.append(LaneLaunch(name=lane.name, command=command, environment=environment))
        return tuple(launches)

    def _postprocess(self, run_root: Path) -> int:
        environment = dict(os.environ)
        environment["COVERAGE_FILE"] = str(self._repo_root / ".coverage")
        coverage_files = tuple(str(run_root / lane / ".coverage") for lane in ("lane-a", "lane-b"))
        commands = (
            ("uv", "run", "coverage", "combine", *coverage_files),
            ("uv", "run", "coverage", "json", "--fail-under=0"),
            ("uv", "run", "coverage", "xml", "--fail-under=0"),
            ("uv", "run", "coverage", "report", "--fail-under=95"),
            ("uv", "run", "python", "scripts/coverage_floor.py"),
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
        old_handlers = {signum: signal.signal(signum, self._forward_signal) for signum in (signal.SIGINT, signal.SIGTERM)}
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
            except (OSError, RuntimeError, subprocess.SubprocessError):
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
        return self._postprocess(run_root)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--shards", type=Path, default=DEFAULT_SHARDS_PATH)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
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
        seat_prefix = derive_parallel_seat_prefix(repo_root)
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


if __name__ == "__main__":
    raise SystemExit(main())
