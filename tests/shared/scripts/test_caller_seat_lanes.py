"""A caller-exported `just test-db-for` seat runs two derived lanes; everything else stays verbatim (phaze-qov36).

Until phaze-qov36, `just test-validate` sent ANY exported Postgres/Redis triplet down the serial
path -- which is exactly what CLAUDE.md tells every dispatched seat to export, so the seats that
followed the isolation rules paid 864 s where a bare worktree paid 622 s (phaze-7kc20). The runner
now classifies the triplet: one minted by `test-db-for` on this harness is split into
`<seat>-lane-a` / `<seat>-lane-b`; anything else -- CI above all -- is honoured verbatim.

Three kinds of evidence, because each answers a different question:

* the classifier's contract, condition by condition, against injected registry probes;
* CI safety, against the REAL workflow files: CI's own environment is declined without a single
  registry probe, and no recipe CI runs can reach the classifier, the lane runner or a provisioner;
* isolation, against the REAL harness: the real supervisor provisions both lanes through the real
  `just test-db-for`, each lane process reports what it actually connected to, and the lanes must
  be distinct from each other and from the caller, with both lane seats released afterwards.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest
import yaml

from scripts.parallel_test_manifests import LaneManifest, ManifestPlan
from scripts.parallel_test_runner import (
    CALLER_SEAT_BUSY,
    CALLER_SEAT_DECLINED,
    CALLER_SEAT_FATAL,
    CallerSeatVerdict,
    LaneLaunch,
    ParallelTestSupervisor,
    RegistryUnavailableError,
    SupervisorDependencies,
    classify_caller_seat,
    production_dependencies,
    registry_seat_index,
)


if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from pytest import MonkeyPatch

    from scripts.parallel_test_runner import ProcessHandle


_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOWS = _ROOT / ".github" / "workflows"
_JUST = shutil.which("just") or "just"
_BASH = shutil.which("bash") or "bash"
_DOCKER = shutil.which("docker") or "docker"
_PG_PORT = "5433"
_REDIS_PORT = "6380"
_SEAT = "phaze_qov36_48f37603"
_MINTED = {
    "TEST_DATABASE_URL": f"postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_{_SEAT}_test",
    "MIGRATIONS_TEST_DATABASE_URL": f"postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_{_SEAT}_migrations_test",
    # Parsed by the classifier, never connected to -- and built from the port so
    # tests/shared/test_redis_worktree_isolation.py's literal-DSN guard is not tripped by a fixture.
    "PHAZE_REDIS_URL": f"redis://localhost:{_REDIS_PORT}/7",
}


class RecordingRegistry:
    """An injected `seat-index` lookup: returns ``index`` (None = not registered) or raises."""

    def __init__(self, *, index: str | None = "7", unavailable: bool = False) -> None:
        self.index = index
        self.unavailable = unavailable
        self.probes: list[str] = []

    def __call__(self, seat_id: str) -> str | None:
        self.probes.append(seat_id)
        if self.unavailable:
            raise RegistryUnavailableError("exit 1: Redis container 'phaze-test-redis' is not reachable")
        return self.index


def _verdict(environ: Mapping[str, str], registry: RecordingRegistry) -> CallerSeatVerdict:
    return classify_caller_seat(environ, pg_port=_PG_PORT, redis_port=_REDIS_PORT, seat_index=registry)


def test_a_minted_registered_seat_is_accepted_under_its_registry_identifier() -> None:
    registry = RecordingRegistry()

    verdict = _verdict(_MINTED, registry)

    assert (verdict.seat_id, verdict.fatal) == (_SEAT, False)
    assert registry.probes == [_SEAT]


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"CI": "true"}, "CI is set"),
        ({"PHAZE_REDIS_URL": ""}, "incomplete (PHAZE_REDIS_URL unset)"),
        ({"TEST_DATABASE_URL": "postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test"}, "TEST_DATABASE_URL is not"),
        ({"TEST_DATABASE_URL": _MINTED["TEST_DATABASE_URL"].replace("5433", "5432")}, "TEST_DATABASE_URL is not"),
        ({"TEST_DATABASE_URL": _MINTED["TEST_DATABASE_URL"].replace("localhost", "db.example")}, "TEST_DATABASE_URL is not"),
        ({"MIGRATIONS_TEST_DATABASE_URL": "postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_other_migrations_test"}, "MIGRATIONS_TEST"),
        ({"PHAZE_REDIS_URL": f"redis://localhost:{_REDIS_PORT}/0"}, "PHAZE_REDIS_URL is not"),
        ({"PHAZE_REDIS_URL": "redis://localhost:6379/7"}, "PHAZE_REDIS_URL is not"),
    ],
)
def test_every_non_seat_shape_is_declined_by_name_without_probing_the_registry(override: dict[str, str], reason: str) -> None:
    registry = RecordingRegistry()

    verdict = _verdict({**_MINTED, **override}, registry)

    assert (verdict.seat_id, verdict.fatal) == (None, False)
    assert reason in verdict.reason
    assert registry.probes == []


@pytest.mark.parametrize("value", ["0", "2", "false", ""])
def test_any_opt_out_value_but_1_declines_a_valid_seat_to_serial(value: str) -> None:
    verdict = _verdict({**_MINTED, "PHAZE_TEST_PARALLEL": value}, RecordingRegistry())

    assert (verdict.seat_id, verdict.fatal) == (None, False)
    assert f"PHAZE_TEST_PARALLEL={value} (anything but 1)" in verdict.reason


@pytest.mark.parametrize(
    ("registry", "reason"),
    [
        (RecordingRegistry(index=None), "is not registered -- released or reclaimed"),
        (RecordingRegistry(index="12"), "names Redis DB 7, but the registry holds DB 12"),
        (RecordingRegistry(unavailable=True), "registry could not be read"),
    ],
    ids=["released-or-reclaimed", "stale-redis-index", "registry-unreachable"],
)
@pytest.mark.parametrize("opt_out", [{}, {"PHAZE_TEST_PARALLEL": "0"}], ids=["default", "opted-out"])
def test_a_shape_perfect_seat_the_registry_contradicts_is_fatal_even_when_opted_out(
    registry: RecordingRegistry, reason: str, opt_out: dict[str, str]
) -> None:
    """Never serial on a stale seat: its exported Redis DB may already belong to another live seat."""

    verdict = _verdict({**_MINTED, **opt_out}, registry)

    assert (verdict.seat_id, verdict.fatal) == (None, True)
    assert reason in verdict.reason
    if not registry.unavailable:
        assert "Re-run `just test-db-for <your seat name>` and re-export" in verdict.reason


def _recipe(name: str) -> str:
    result = subprocess.run([_JUST, "--show", name], cwd=_ROOT, capture_output=True, text=True, check=True)  # noqa: S603 - resolved executable, fixed test argv
    return result.stdout


def test_test_validate_maps_each_classifier_outcome_to_one_path() -> None:
    body = _recipe("test-validate")

    assert "--classify-caller-seat" in body
    assert 'uv run python -m scripts.parallel_test_runner --seat-prefix "$seat"' in body
    assert "🛣️  CALLER SEAT" in body
    # exit 3 -- and only exit 3 -- is "declined", which keeps the verbatim serial primitive.
    declined = body.split("3)", 1)[1].split(";;", 1)[0]
    assert "SERIAL FALLBACK" in declined
    assert "just test-cov" in declined
    # Any other failure is loud; a classifier crash must never silently become a serial run.
    other = body.split("*)", 1)[1].split(";;", 1)[0]
    assert 'exit "$rc"' in other
    assert "just test-cov" not in other


# ---------------------------------------------------------------------------------------------
# CI safety: pinned against the real workflow files.
# ---------------------------------------------------------------------------------------------


def _workflow_documents() -> list[dict[str, object]]:
    return [yaml.safe_load(path.read_text(encoding="utf-8")) for path in sorted(_WORKFLOWS.glob("*.yml"))]


def _job_environments() -> list[tuple[str, dict[str, str]]]:
    environments: list[tuple[str, dict[str, str]]] = []
    for document in _workflow_documents():
        jobs = document.get("jobs") or {}
        assert isinstance(jobs, dict)
        for job_name, job in jobs.items():
            env = job.get("env") if isinstance(job, dict) else None
            if isinstance(env, dict) and any(key in env for key in ("TEST_DATABASE_URL", "PHAZE_REDIS_URL")):
                environments.append((job_name, {key: str(value) for key, value in env.items()}))
    return environments


def _ci_invoked_recipes() -> set[str]:
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "run" and isinstance(value, str):
                    for line in value.splitlines():
                        found.update(re.findall(r"(?:^|[\s;&|(])just\s+([a-z][a-z0-9-]*)", line.split("#", 1)[0]))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for document in _workflow_documents():
        walk(document)
    return found


def test_ci_job_environments_are_declined_without_a_single_registry_probe() -> None:
    """CI's DSNs are honoured verbatim: declined by CI=true, and still declined by shape without it."""

    environments = _job_environments()
    assert {name for name, _ in environments} >= {"test", "browser"}, environments
    for name, environment in environments:
        for ci in ({"CI": "true"}, {}):
            registry = RecordingRegistry()
            verdict = _verdict({**environment, **ci}, registry)
            assert (verdict.seat_id, verdict.fatal) == (None, False), name
            assert registry.probes == [], f"job {name!r} reached the registry probe with {ci}"


_LOCAL_ONLY = ("test-validate", "check", "check-fast", "check-all", "test-cov-parallel", "test-fast")
_PROVISIONING = ("parallel_test_runner", "classify-caller-seat", "test-db-for", "provision-test-seat", "ensure-test-seat", "test-validate")


def _reachable(recipe: str, seen: set[str]) -> None:
    if recipe in seen:
        return
    seen.add(recipe)
    body = _recipe(recipe)
    header = next(line for line in body.splitlines() if re.match(rf"^{re.escape(recipe)}\b.*:", line))
    dependencies = re.findall(r"[a-z][a-z0-9-]*", header.split(":", 1)[1])
    for called in dependencies + re.findall(r"(?:^|[\s;&|(])just\s+([a-z][a-z0-9-]*)", body):
        _reachable(called, seen)


def test_no_recipe_ci_runs_can_reach_the_classifier_the_lane_runner_or_a_provisioner() -> None:
    invoked = _ci_invoked_recipes()
    assert {"test-bucket", "coverage-combine", "test-browser"} <= invoked, invoked
    assert not invoked & set(_LOCAL_ONLY), invoked & set(_LOCAL_ONLY)

    reachable: set[str] = set()
    for recipe in invoked:
        _reachable(recipe, reachable)
    offenders = {recipe: [token for token in _PROVISIONING if token in _recipe(recipe)] for recipe in sorted(reachable)}
    assert not {recipe: tokens for recipe, tokens in offenders.items() if tokens}, offenders


# ---------------------------------------------------------------------------------------------
# Isolation, against the real harness.
# ---------------------------------------------------------------------------------------------

_PROBE = textwrap.dedent(
    """
    import asyncio, json, os, sys
    import asyncpg, redis

    async def main():
        dsn = os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
        migrations = os.environ["MIGRATIONS_TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
        out = {"env": {k: os.environ[k] for k in ("TEST_DATABASE_URL", "MIGRATIONS_TEST_DATABASE_URL", "PHAZE_REDIS_URL")}}
        for key, target in (("database", dsn), ("migrations_database", migrations)):
            connection = await asyncpg.connect(target)
            try:
                out[key] = await connection.fetchval("select current_database()")
            finally:
                await connection.close()
        client = redis.Redis.from_url(os.environ["PHAZE_REDIS_URL"])
        client.set("phaze-qov36-lane-probe", "1")
        out["redis_db"] = int(client.client_info()["db"])
        client.close()
        with open(sys.argv[1], "w") as handle:
            json.dump(out, handle)

    asyncio.run(main())
    """
)


def _harness_is_up() -> bool:
    docker = shutil.which("docker")
    if docker is None or shutil.which("just") is None:
        return False
    probes = (
        [docker, "exec", "phaze-test-redis", "redis-cli", "PING"],
        [docker, "exec", "phaze-test-db", "pg_isready", "-U", "phaze"],
    )
    return all(subprocess.run(probe, capture_output=True, check=False).returncode == 0 for probe in probes)  # noqa: S603 - resolved executable, fixed test argv


def _just(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([_JUST, *args], cwd=_ROOT, capture_output=True, text=True, check=False)  # noqa: S603 - resolved executable, fixed test argv


def _derived(raw: str) -> str:
    return subprocess.run([_BASH, str(_ROOT / "scripts/derive-seat-name.sh"), raw], capture_output=True, text=True, check=True).stdout.strip()  # noqa: S603 - resolved executable, fixed test argv


def _registered(seat_id: str) -> bool:
    return registry_seat_index(_ROOT, "phaze-test-redis")(seat_id) is not None


def _redis_dbsize(index: str) -> int:
    result = subprocess.run([_DOCKER, "exec", "phaze-test-redis", "redis-cli", "-n", index, "DBSIZE"], capture_output=True, text=True, check=True)  # noqa: S603 - resolved executable, fixed test argv
    return int(result.stdout.strip())


_needs_harness = pytest.mark.skipif(
    not _harness_is_up(), reason="needs the running local test harness (phaze-test-db + phaze-test-redis); `just test-db`"
)


@pytest.fixture
def probe_caller() -> Iterator[tuple[dict[str, str], str]]:
    """A real `just test-db-for` caller seat, released afterwards.

    Named from the RUNNING seat's own database, so it is stable per seat (re-runs reuse the same
    Postgres databases rather than leaving new ones behind) and unique across concurrent seats. One
    seat never runs this module twice at once: the session lock refuses it.
    """

    own_database = os.environ.get("TEST_DATABASE_URL", "phaze_test").rsplit("/", 1)[-1]
    raw_caller = f"qprobe-{hashlib.sha256(own_database.encode()).hexdigest()[:10]}"
    provisioned = _just("test-db-for", raw_caller)
    assert provisioned.returncode == 0, provisioned.stderr
    caller_id = _derived(raw_caller)
    try:
        yield dict(re.findall(r'export ([A-Z_]+)="([^"]+)"', provisioned.stdout)), caller_id
    finally:
        _just("test-db-release", caller_id)


def _runner_cli(*args: str, environ: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, **environ}
    environment.pop("CI", None)
    environment.pop("PHAZE_TEST_DB_ALLOW_SHARED", None)
    return subprocess.run(  # noqa: S603 - resolved interpreter, fixed test argv
        [sys.executable, "-m", "scripts.parallel_test_runner", *args],
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


@_needs_harness
def test_the_real_cli_accepts_a_live_seat_and_honours_the_opt_out(probe_caller: tuple[dict[str, str], str]) -> None:
    caller, caller_id = probe_caller

    accepted = _runner_cli("--classify-caller-seat", environ={**caller, "PHAZE_TEST_PARALLEL": "1"})
    opted_out = _runner_cli("--classify-caller-seat", environ={**caller, "PHAZE_TEST_PARALLEL": "0"})

    assert (accepted.returncode, accepted.stdout.strip()) == (0, caller_id), accepted.stderr
    assert opted_out.returncode == CALLER_SEAT_DECLINED, opted_out.stderr
    assert opted_out.stdout == ""
    assert "PHAZE_TEST_PARALLEL=0 (anything but 1) forces the serial path" in opted_out.stderr


@_needs_harness
def test_the_real_cli_fails_loudly_on_a_stale_or_unreadable_seat(probe_caller: tuple[dict[str, str], str]) -> None:
    """Released, index-mismatched and registry-unreachable seats exit 5 -- which the recipe never maps to `test-cov`."""

    caller, caller_id = probe_caller
    index = caller["PHAZE_REDIS_URL"].rsplit("/", 1)[1]
    other = str(int(index) + 1)
    never_registered = {
        "TEST_DATABASE_URL": f"postgresql+asyncpg://phaze:phaze@localhost:{_PG_PORT}/phaze_qprobe_never_0000_test",
        "MIGRATIONS_TEST_DATABASE_URL": f"postgresql+asyncpg://phaze:phaze@localhost:{_PG_PORT}/phaze_qprobe_never_0000_migrations_test",
        "PHAZE_REDIS_URL": caller["PHAZE_REDIS_URL"],
    }
    cases = {
        "not registered -- released or reclaimed": _runner_cli("--classify-caller-seat", environ=never_registered),
        f"names Redis DB {other}, but the registry holds DB {index} for {caller_id!r}": _runner_cli(
            "--classify-caller-seat", environ={**caller, "PHAZE_REDIS_URL": caller["PHAZE_REDIS_URL"].rsplit("/", 1)[0] + f"/{other}"}
        ),
        "registry could not be read": _runner_cli("--classify-caller-seat", "--redis-container", "phaze-no-such-redis", environ=caller),
    }
    for reason, result in cases.items():
        assert result.returncode == CALLER_SEAT_FATAL, (reason, result.stderr)
        assert result.stdout == ""
        assert "❌ caller seat refused, and nothing was run on it" in result.stderr
        assert reason in result.stderr


_HOLDER = textwrap.dedent(
    """
    import sys, time
    from scripts.parallel_test_runner import hold_caller_seat
    held = hold_caller_seat(sys.argv[1])
    print("held", flush=True)
    time.sleep(120)
    """
)


@_needs_harness
def test_a_second_gate_on_the_same_caller_seat_is_refused_before_collection(probe_caller: tuple[dict[str, str], str], tmp_path: Path) -> None:
    """Two gates on one caller seat would provision the SAME `<seat>-lane-a/-b` and wreck each other.

    The first gate's hold is taken by the runner's own `hold_caller_seat` in a separate process;
    the second is the real runner CLI. `--shards` names a file that does not exist, so a second gate
    that got PAST the hold would fail on the shard file instead -- proving the refusal comes first,
    before shards, record-start, collection or provisioning, and keeping a broken hold from ever
    reaching a real suite run.

    Mutation evidence (phaze-qov36): making `hold_caller_seat` a no-op fails this test (exit 2 on
    the missing shard file, no refusal).
    """

    caller, _caller_id = probe_caller
    missing_shards = tmp_path / "no-such-shards.json"
    holder = subprocess.Popen(  # noqa: S603 - resolved interpreter, fixed test argv
        [sys.executable, "-c", _HOLDER, caller["TEST_DATABASE_URL"]], cwd=_ROOT, stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"

        refused = _runner_cli("--seat-prefix", "qprobe-busy", "--shards", str(missing_shards), environ=caller)

        assert refused.returncode == CALLER_SEAT_BUSY, refused.stderr
        assert "caller seat busy -- refusing before collection" in refused.stderr
        assert f"pid={holder.pid}" in refused.stderr, "the refusal must name the gate holding the seat"
        assert "Verified parallel test partition" not in refused.stdout
        assert "no-such-shards" not in refused.stderr
    finally:
        holder.kill()
        holder.wait()

    # Once the first gate is gone, the same start gets past the hold -- and stops at the shard file.
    released = _runner_cli("--seat-prefix", "qprobe-busy", "--shards", str(missing_shards), environ=caller)
    assert released.returncode == 2, released.stderr
    assert "caller seat busy" not in released.stderr


@_needs_harness
def test_real_lanes_derived_from_a_caller_seat_are_isolated_and_released(
    probe_caller: tuple[dict[str, str], str], tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """The acceptance test: real provisioning, real connections, real release.

    Mutation evidence (phaze-qov36): making `ParallelTestSupervisor.run` name both lanes' seats
    identically, or dropping the per-lane seat override so lanes inherit the caller's, fails this
    test on the distinctness assertions.
    """

    caller, caller_id = probe_caller
    caller_redis_index = caller["PHAZE_REDIS_URL"].rsplit("/", 1)[1]
    lane_ids = [_derived(f"{caller_id}-lane-a"), _derived(f"{caller_id}-lane-b")]
    try:
        environ = {**caller, "PHAZE_TEST_PARALLEL": "1"}
        verdict = classify_caller_seat(environ, pg_port=_PG_PORT, redis_port=_REDIS_PORT, seat_index=registry_seat_index(_ROOT, "phaze-test-redis"))
        assert verdict.seat_id == caller_id, verdict.reason

        real = production_dependencies(_ROOT)

        def spawn(launch: LaneLaunch) -> ProcessHandle:
            report = tmp_path / f"{launch.name}.json"
            return subprocess.Popen([sys.executable, "-c", _PROBE, str(report)], cwd=_ROOT, env=dict(launch.environment), start_new_session=True)  # noqa: S603 - resolved executable, fixed test argv

        dependencies = SupervisorDependencies(
            provision=real.provision,
            release=real.release,
            spawn=spawn,
            run_command=lambda _command, _environment: 0,
            record_coverage_scope_start=lambda: None,
            signal_group=real.signal_group,
            group_alive=real.group_alive,
        )
        plan = ManifestPlan(
            canonical_node_ids=("tests/a.py::t", "tests/b.py::t"),
            lanes=(LaneManifest("lane-a", ("tests/a.py::t",), ()), LaneManifest("lane-b", ("tests/b.py::t",), ())),
        )
        # The caller's seat is exported into THIS process's environment, exactly as a gate sees it.
        for key, value in caller.items():
            monkeypatch.setenv(key, value)
        status = ParallelTestSupervisor(_ROOT, dependencies).run(plan, tmp_path / "run", seat_prefix=caller_id)
        # What each lane process ACTUALLY connected to -- read before the exit status, so a
        # shared-seat mutation fails here, on the property under test, and not on a side effect.
        lanes = [json.loads((tmp_path / f"{lane}.json").read_text(encoding="utf-8")) for lane in ("lane-a", "lane-b")]
        databases = [lane["database"] for lane in lanes] + [caller["TEST_DATABASE_URL"].rsplit("/", 1)[1]]
        migrations = [lane["migrations_database"] for lane in lanes] + [caller["MIGRATIONS_TEST_DATABASE_URL"].rsplit("/", 1)[1]]
        redis_dbs = [lane["redis_db"] for lane in lanes] + [int(caller_redis_index)]
        assert len(set(databases)) == 3, f"lane-a, lane-b and the caller must be three distinct databases: {databases}"
        assert len(set(migrations)) == 3, f"...and three distinct migrations databases: {migrations}"
        assert len(set(redis_dbs)) == 3, f"...and three distinct Redis logical DBs: {redis_dbs}"
        assert 0 not in redis_dbs
        for lane, lane_id in zip(lanes, lane_ids, strict=True):
            assert lane["database"] == f"phaze_{lane_id}_test"
            assert lane["migrations_database"] == f"phaze_{lane_id}_migrations_test"
        assert status == 0

        # The caller's own seat was not written: the lanes wrote a key into THEIR logical DBs only.
        assert _redis_dbsize(caller_redis_index) == 0
        # And both lane seats were handed back.
        assert [_registered(lane_id) for lane_id in lane_ids] == [False, False]
    finally:
        for lane_id in lane_ids:
            if _registered(lane_id):
                _just("test-db-release", lane_id)
