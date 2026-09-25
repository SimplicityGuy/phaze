"""Fault-injection contracts for the isolated two-lane test supervisor."""

from __future__ import annotations

from pathlib import Path
import signal
import subprocess
from typing import TYPE_CHECKING

import pytest

from scripts.parallel_test_manifests import LaneManifest, ManifestPlan
from scripts.parallel_test_runner import (
    LaneLaunch,
    ParallelTestSupervisor,
    Seat,
    SupervisorDependencies,
    _parse_seat_exports,
    derive_parallel_seat_prefix,
    production_dependencies,
)


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from pytest import MonkeyPatch


_NODES = ("tests/a.py::test_a", "tests/b.py::test_b")
_PLAN = ManifestPlan(
    canonical_node_ids=_NODES,
    lanes=(
        LaneManifest("lane-a", (_NODES[0],), (f"--deselect={_NODES[1]}",)),
        LaneManifest("lane-b", (_NODES[1],), (f"--deselect={_NODES[0]}",)),
    ),
)


class FakeProcess:
    def __init__(self, pid: int, returncode: int, on_wait: Callable[[], None] | None = None) -> None:
        self.pid = pid
        self.returncode = returncode
        self.on_wait = on_wait
        self.waited = False

    def poll(self) -> int | None:
        return self.returncode if self.waited else None

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if not self.waited and self.on_wait is not None:
            self.on_wait()
        self.waited = True
        return self.returncode


class Harness:
    def __init__(
        self,
        *,
        child_statuses: Sequence[int] = (0, 0),
        # 6 successes: _postprocess's six commands (combine, json, stamp, xml, report, floor).
        # `record_coverage_scope_start` is a SEPARATE dependency (phaze-vad6y) -- it needs to
        # return a TOKEN, not just a status, so it does not share this queue.
        command_statuses: Sequence[int] = (0, 0, 0, 0, 0, 0),
        spawn_failure_at: int | None = None,
        record_start_token: str | None = "faketoken",  # noqa: S107 -- a fake test fixture value, not a credential
    ) -> None:
        self.child_statuses = list(child_statuses)
        self.command_statuses = list(command_statuses)
        self.spawn_failure_at = spawn_failure_at
        self.record_start_token = record_start_token
        self.events: list[str] = []
        self.launches: list[LaneLaunch] = []
        self.releases: dict[str, list[int]] = {}
        self.processes: list[FakeProcess] = []
        self.on_first_wait: Callable[[], None] | None = None
        self.group_alive_outcomes: dict[int, list[bool]] = {}

    def provision(self, name: str) -> Seat:
        self.events.append(f"provision:{name}")
        suffix = name.rsplit("-", 1)[-1]
        return Seat(
            name=name,
            environment={
                "TEST_DATABASE_URL": f"postgresql+asyncpg://test/{suffix}",
                "MIGRATIONS_TEST_DATABASE_URL": f"postgresql+asyncpg://migrations/{suffix}",
                "PHAZE_REDIS_URL": f"redis://test/{suffix}",
            },
        )

    def release(self, name: str) -> int:
        self.events.append(f"release:{name}")
        outcomes = self.releases.get(name)
        return outcomes.pop(0) if outcomes else 0

    def spawn(self, launch: LaneLaunch) -> FakeProcess:
        index = len(self.launches)
        self.events.append(f"spawn:{launch.name}")
        self.launches.append(launch)
        if self.spawn_failure_at == index:
            raise OSError("spawn failed")
        callback = self.on_first_wait if index == 0 else None
        process = FakeProcess(1000 + index, self.child_statuses[index], callback)
        self.processes.append(process)
        return process

    def run_command(self, command: Sequence[str], environment: Mapping[str, str]) -> int:
        del environment
        self.events.append(f"command:{' '.join(command)}")
        return self.command_statuses.pop(0)

    def record_coverage_scope_start(self) -> str | None:
        self.events.append("record-start")
        return self.record_start_token

    def signal_group(self, process_group: int, signum: int) -> None:
        self.events.append(f"signal:{process_group}:{signum}")

    def group_alive(self, process_group: int) -> bool:
        self.events.append(f"group-alive:{process_group}")
        outcomes = self.group_alive_outcomes.get(process_group)
        return outcomes.pop(0) if outcomes else False

    def dependencies(self) -> SupervisorDependencies:
        return SupervisorDependencies(
            provision=self.provision,
            release=self.release,
            spawn=self.spawn,
            run_command=self.run_command,
            record_coverage_scope_start=self.record_coverage_scope_start,
            signal_group=self.signal_group,
            group_alive=self.group_alive,
            sleep=lambda _seconds: None,
        )


def _run(tmp_path: Path, harness: Harness) -> tuple[int, ParallelTestSupervisor]:
    supervisor = ParallelTestSupervisor(tmp_path, harness.dependencies())
    status = supervisor.run(_PLAN, tmp_path / "run", seat_prefix="runner-owned")
    return status, supervisor


def test_success_isolates_every_surface_releases_before_combine_and_ignores_caller_seat(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+asyncpg://caller/keep")
    monkeypatch.setenv("MIGRATIONS_TEST_DATABASE_URL", "postgresql+asyncpg://caller/keep-migrations")
    monkeypatch.setenv("PHAZE_REDIS_URL", "redis://caller/63")
    monkeypatch.setenv("PHAZE_TEST_DB_ALLOW_SHARED", "1")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-n auto -m browser")
    harness = Harness()

    status, _ = _run(tmp_path, harness)

    assert status == 0
    assert [launch.name for launch in harness.launches] == ["lane-a", "lane-b"]
    first, second = harness.launches
    for key in ("TEST_DATABASE_URL", "MIGRATIONS_TEST_DATABASE_URL", "PHAZE_REDIS_URL"):
        assert first.environment[key] != second.environment[key]
        assert "caller" not in first.environment[key]
        assert "caller" not in second.environment[key]
    assert "PHAZE_TEST_DB_ALLOW_SHARED" not in first.environment
    assert "PHAZE_TEST_DB_ALLOW_SHARED" not in second.environment
    assert "PYTEST_ADDOPTS" not in first.environment
    assert "PYTEST_ADDOPTS" not in second.environment

    for key in ("COVERAGE_FILE", "TMPDIR", "PYTHONPYCACHEPREFIX", "BH_TEST_REPORT_DIR"):
        assert first.environment[key] != second.environment[key]
    assert next(arg for arg in first.command if arg.startswith("--junitxml=")) != next(arg for arg in second.command if arg.startswith("--junitxml="))
    assert next(arg for arg in first.command if arg.startswith("cache_dir=")) != next(arg for arg in second.command if arg.startswith("cache_dir="))
    assert all(launch.command[:4] == ("uv", "run", "--no-sync", "pytest") for launch in harness.launches)
    assert first.command[4].startswith("@")
    assert "--cov-fail-under=0" in first.command
    assert "--cov-report=" in first.command

    # phaze-vad6y: `record_coverage_scope_start` fires before ANY lane is provisioned or spawned
    # -- it has to, so the marker describes the tree before pytest can possibly change what HEAD
    # or dirtiness mean -- so it is legitimately the very first event of the whole run, ahead of
    # both seats' provisioning. It is a SEPARATE dependency from `run_command` (it needs a token
    # back, not just a status), so it never shows up as a "command:" event; the release-before-
    # combine invariant this test guards is about the actual coverage POST-PROCESS commands only.
    assert harness.events[0] == "record-start"
    release_events = [event for event in harness.events if event.startswith("release:")]
    assert release_events == ["release:runner-owned-lane-a", "release:runner-owned-lane-b"]
    first_command = next(index for index, event in enumerate(harness.events) if event.startswith("command:"))
    assert all(harness.events.index(event) < first_command for event in release_events)
    assert len([event for event in harness.events if event.startswith("command:")]) == 6

    commands = [event.removeprefix("command:") for event in harness.events if event.startswith("command:")]
    assert commands[0].startswith("uv run coverage combine ")
    assert commands[1:] == [
        "uv run coverage json --fail-under=0",
        "uv run python scripts/stamp_coverage_scope.py --expect-token faketoken",
        "uv run coverage xml --fail-under=0",
        "uv run coverage report --fail-under=95",
        "uv run python scripts/coverage_floor.py",
    ]


def test_each_lane_carries_an_argv_visible_worktree_marker(tmp_path: Path) -> None:
    """phaze-7w18f: the two-worker path is what `just check` runs by DEFAULT when no caller-owned
    seat is exported, so its lanes need the same `--rootdir` marker `test-cov`'s single-process
    invocation carries -- otherwise a path-scoped `pkill -f` still can't tell this worktree's
    parallel lanes from a sibling worktree's.
    """
    harness = Harness()

    status, _ = _run(tmp_path, harness)

    assert status == 0
    first, second = harness.launches
    # Both lanes belong to the SAME worktree, so they share one marker -- distinguishing lanes
    # from each other is not the ask; distinguishing this worktree from a sibling's is.
    assert f"--rootdir={tmp_path}" in first.command
    assert f"--rootdir={tmp_path}" in second.command


def test_the_argv_marker_is_unique_per_worktree(tmp_path: Path) -> None:
    """Two different `repo_root`s (i.e. two different worktrees) must render two different markers."""
    first_root = tmp_path / "worktree-a"
    second_root = tmp_path / "worktree-b"
    first_root.mkdir()
    second_root.mkdir()
    first_harness = Harness()
    second_harness = Harness()

    _run(first_root, first_harness)
    _run(second_root, second_harness)

    first_marker = next(arg for arg in first_harness.launches[0].command if arg.startswith("--rootdir="))
    second_marker = next(arg for arg in second_harness.launches[0].command if arg.startswith("--rootdir="))
    assert first_marker != second_marker


def test_lane_failure_skips_coverage_commands_and_propagates_status(tmp_path: Path) -> None:
    harness = Harness(child_statuses=(0, 7))

    status, _ = _run(tmp_path, harness)

    assert status == 7
    # phaze-vad6y: `record_coverage_scope_start` fires unconditionally before either lane is even
    # provisioned (a SEPARATE dependency, not a "command:" event), so it still ran exactly once
    # even though the run never reaches `_postprocess` -- the post-process COMMANDS never ran.
    assert harness.events.count("record-start") == 1
    assert not any(event.startswith("command:") for event in harness.events)
    assert sum(event.startswith("release:") for event in harness.events) == 2


def test_descendant_group_quiesces_before_seat_release(tmp_path: Path) -> None:
    harness = Harness()
    harness.group_alive_outcomes[1000] = [True, False]

    status, _ = _run(tmp_path, harness)

    assert status == 0
    final_group_check = max(index for index, event in enumerate(harness.events) if event == "group-alive:1000")
    first_release = next(index for index, event in enumerate(harness.events) if event.startswith("release:"))
    assert final_group_check < first_release


def test_spawn_failure_terminates_started_group_and_releases_both_registered_seats(tmp_path: Path) -> None:
    harness = Harness(spawn_failure_at=1)

    status, _ = _run(tmp_path, harness)

    assert status == 1
    assert f"signal:1000:{signal.SIGTERM}" in harness.events
    assert harness.processes[0].waited
    assert sum(event.startswith("release:") for event in harness.events) == 2
    assert harness.events.count("record-start") == 1
    assert not any(event.startswith("command:") for event in harness.events)


@pytest.mark.parametrize(
    ("command_statuses", "expected", "commands_run"),
    [
        ((9,), 9, 1),  # coverage combine fails
        ((0, 8), 8, 2),  # coverage json fails
        ((0, 0, 7), 7, 3),  # scripts/stamp_coverage_scope.py fails (phaze-vad6y)
        ((0, 0, 0, 6), 6, 4),  # coverage xml fails
        ((0, 0, 0, 0, 5), 5, 5),  # coverage report fails
        ((0, 0, 0, 0, 0, 4), 4, 6),  # scripts/coverage_floor.py fails
    ],
)
def test_combine_report_and_floor_failures_propagate(tmp_path: Path, command_statuses: tuple[int, ...], expected: int, commands_run: int) -> None:
    harness = Harness(command_statuses=command_statuses)

    status, _ = _run(tmp_path, harness)

    assert status == expected
    assert sum(event.startswith("command:") for event in harness.events) == commands_run
    assert sum(event.startswith("release:") for event in harness.events) == 2


def test_a_failing_record_start_does_not_abort_the_run_and_the_stamp_step_is_skipped(tmp_path: Path) -> None:
    """`record_coverage_scope_start`'s return value is `None` when it fails (phaze-vad6y): a
    failure recording the pre-run marker must not cost a two-lane test run its result, AND
    `_postprocess` must not even ATTEMPT the stamp call it could only ever refuse -- there is no
    token to pass it.
    """
    harness = Harness(record_start_token=None)

    status, _ = _run(tmp_path, harness)

    assert status == 0
    assert harness.events.count("record-start") == 1
    commands = [event.removeprefix("command:") for event in harness.events if event.startswith("command:")]
    assert commands == [
        "uv run coverage combine {}".format(" ".join(str(tmp_path / "run" / lane / ".coverage") for lane in ("lane-a", "lane-b"))),
        "uv run coverage json --fail-under=0",
        "uv run coverage xml --fail-under=0",
        "uv run coverage report --fail-under=95",
        "uv run python scripts/coverage_floor.py",
    ]
    assert not any("stamp_coverage_scope.py" in command for command in commands)


@pytest.mark.parametrize(("signum", "expected"), [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
def test_interrupt_reaches_both_process_groups_waits_and_releases(tmp_path: Path, signum: int, expected: int) -> None:
    harness = Harness(child_statuses=(-signum, -signum))
    supervisor = ParallelTestSupervisor(tmp_path, harness.dependencies())
    harness.on_first_wait = lambda: supervisor._forward_signal(signum, None)

    status = supervisor.run(_PLAN, tmp_path / "run", seat_prefix="runner-owned")

    assert status == expected
    assert f"signal:1000:{signum}" in harness.events
    assert f"signal:1001:{signum}" in harness.events
    assert all(process.waited for process in harness.processes)
    assert sum(event.startswith("release:") for event in harness.events) == 2
    assert harness.events.count("record-start") == 1
    assert not any(event.startswith("command:") for event in harness.events)


def test_each_release_retries_even_when_first_seat_never_releases(tmp_path: Path) -> None:
    harness = Harness()
    harness.releases = {
        "runner-owned-lane-a": [1, 1, 1, 1, 1],
        "runner-owned-lane-b": [1, 0],
    }

    status, _ = _run(tmp_path, harness)

    assert status == 1
    assert harness.events.count("release:runner-owned-lane-a") == 5
    assert harness.events.count("release:runner-owned-lane-b") == 2
    assert harness.events.count("record-start") == 1
    assert not any(event.startswith("command:") for event in harness.events)


def test_production_spawn_starts_a_new_process_session(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_popen(command: Sequence[str], **kwargs: object) -> FakeProcess:
        captured["command"] = command
        captured.update(kwargs)
        return FakeProcess(42, 0)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    launch = LaneLaunch("lane-a", ("uv", "run", "pytest"), {})

    production_dependencies(tmp_path).spawn(launch)

    assert captured["command"] == launch.command
    assert captured["start_new_session"] is True


def test_production_provision_names_redis_index_exhaustion_and_both_remedies(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    """phaze-qov36: a caller-seat gate holds three indices, so exhaustion must be loud and named.

    The stderr fed in is the allocator's REAL refusal line, lifted from redis-seat-registry.sh and
    rendered, so rewording that message breaks this test rather than silently un-naming the cause.
    """

    script = Path("scripts/redis-seat-registry.sh").read_text(encoding="utf-8")
    template = next(line for line in script.splitlines() if "allocatable Redis logical DBs" in line)
    exhausted = (
        template.strip()
        .removeprefix('echo "')
        .removesuffix('" >&2')
        .replace("$((cap - 1))", "63")
        .replace("${redis_container}", "phaze-test-redis")
        .replace("${seat}", "x")
    )
    assert "$" not in exhausted, exhausted

    def fake_run(command: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=exhausted)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as raised:
        production_dependencies(tmp_path).provision("seat-lane-b")

    message = str(raised.value)
    assert "index pool is exhausted" in message
    assert "just test-db-reclaim --apply" in message
    assert "PHAZE_TEST_PARALLEL=0" in message
    assert exhausted in message


def test_a_provisioning_failure_is_printed_fails_the_run_and_releases_the_seat_already_taken(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exhaustion on lane-b must fail the gate, never degrade it to a serial run, and give lane-a back."""

    harness = Harness()
    provision = harness.provision

    def exhaust_on_lane_b(name: str) -> Seat:
        if name.endswith("lane-b"):
            raise RuntimeError("lane seat 'x-lane-b' could not get a Redis logical DB: the harness index pool is exhausted.")
        return provision(name)

    harness.provision = exhaust_on_lane_b

    status, _ = _run(tmp_path, harness)

    assert status == 1
    assert "index pool is exhausted" in capsys.readouterr().err
    assert harness.launches == []
    assert "release:runner-owned-lane-a" in harness.events
    assert not any(event.startswith("command:") for event in harness.events)


def test_canonical_seat_output_requires_exactly_three_exports() -> None:
    valid = (
        'export TEST_DATABASE_URL="postgresql+asyncpg://test/a"\n'
        'export MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://test/b"\n'
        'export PHAZE_REDIS_URL="redis://test/1"\n'
    )

    seat = _parse_seat_exports(valid, "seat-a")

    assert set(seat.environment) == {"TEST_DATABASE_URL", "MIGRATIONS_TEST_DATABASE_URL", "PHAZE_REDIS_URL"}
    with pytest.raises(RuntimeError, match="exactly the required exports"):
        _parse_seat_exports('export TEST_DATABASE_URL="postgresql+asyncpg://test/a"\n', "seat-a")


def test_parallel_seat_prefix_is_stable_and_worktree_derived() -> None:
    first = derive_parallel_seat_prefix(Path.cwd())

    assert first == derive_parallel_seat_prefix(Path.cwd())
    assert first.startswith("auto-")
    assert first.endswith("-parallel")


def test_source_never_uses_shared_or_forced_cleanup_bypasses() -> None:
    source = Path("scripts/parallel_test_runner.py").read_text(encoding="utf-8")

    assert 'environment.pop("PHAZE_TEST_DB_ALLOW_SHARED", None)' in source
    assert 'environment.pop("PYTEST_ADDOPTS", None)' in source
    assert 'environment["PHAZE_TEST_DB_ALLOW_SHARED"]' not in source
    assert "PHAZE_TEST_DB_FORCE_DOWN" not in source
    assert "test-db-down" not in source
    assert "--force" not in source
