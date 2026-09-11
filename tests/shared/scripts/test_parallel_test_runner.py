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
        command_statuses: Sequence[int] = (0, 0, 0, 0, 0),
        spawn_failure_at: int | None = None,
    ) -> None:
        self.child_statuses = list(child_statuses)
        self.command_statuses = list(command_statuses)
        self.spawn_failure_at = spawn_failure_at
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
    assert first.command[:3] == ("uv", "run", "pytest")
    assert first.command[3].startswith("@")
    assert "--cov-fail-under=0" in first.command
    assert "--cov-report=" in first.command

    release_events = [event for event in harness.events if event.startswith("release:")]
    assert release_events == ["release:runner-owned-lane-a", "release:runner-owned-lane-b"]
    first_command = next(index for index, event in enumerate(harness.events) if event.startswith("command:"))
    assert all(harness.events.index(event) < first_command for event in release_events)
    assert len([event for event in harness.events if event.startswith("command:")]) == 5

    commands = [event.removeprefix("command:") for event in harness.events if event.startswith("command:")]
    assert commands[0].startswith("uv run coverage combine ")
    assert commands[1:] == [
        "uv run coverage json --fail-under=0",
        "uv run coverage xml --fail-under=0",
        "uv run coverage report --fail-under=95",
        "uv run python scripts/coverage_floor.py",
    ]
    assert commands[0].startswith("uv run coverage combine ")
    assert commands[1:] == [
        "uv run coverage json --fail-under=0",
        "uv run coverage xml --fail-under=0",
        "uv run coverage report --fail-under=95",
        "uv run python scripts/coverage_floor.py",
    ]


def test_lane_failure_skips_coverage_commands_and_propagates_status(tmp_path: Path) -> None:
    harness = Harness(child_statuses=(0, 7))

    status, _ = _run(tmp_path, harness)

    assert status == 7
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
    assert not any(event.startswith("command:") for event in harness.events)


@pytest.mark.parametrize(
    ("command_statuses", "expected", "commands_run"),
    [
        ((9,), 9, 1),
        ((0, 8), 8, 2),
        ((0, 0, 7), 7, 3),
        ((0, 0, 0, 6), 6, 4),
        ((0, 0, 0, 0, 5), 5, 5),
    ],
)
def test_combine_report_and_floor_failures_propagate(tmp_path: Path, command_statuses: tuple[int, ...], expected: int, commands_run: int) -> None:
    harness = Harness(command_statuses=command_statuses)

    status, _ = _run(tmp_path, harness)

    assert status == expected
    assert sum(event.startswith("command:") for event in harness.events) == commands_run
    assert sum(event.startswith("release:") for event in harness.events) == 2


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
