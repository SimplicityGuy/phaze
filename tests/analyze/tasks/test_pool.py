"""Tests for process pool lifecycle and helpers.

The process pool is owned by ``phaze.tasks.agent_worker`` (Phase 26 D-03 +
D-04): only the agent role runs CPU-bound essentia analysis, so the controller
role does not need a process pool. The agent worker's startup/shutdown
behaviour is exercised by tests/test_tasks/test_agent_startup_banner.py;
this file covers the pool helpers themselves.

Phase 43 replaces the un-killable ``concurrent.futures.ProcessPoolExecutor``
with a ``pebble.ProcessPool`` so a runaway child exceeding the inner timeout is
SIGKILLed and its slot reclaimed.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from pebble import ProcessPool
import pytest

from phaze.tasks.pool import create_process_pool, run_in_process_pool


if TYPE_CHECKING:
    from pathlib import Path


# --- module-level (picklable) helpers for the spawn-based pebble workers ---


def _double(x: int) -> int:
    """Simple picklable test function for the process pool."""
    return x * 2


def _echo_kwarg(*, marker: str = "") -> str:
    """Return a keyword argument so kwargs passthrough can be asserted."""
    return marker


def _write_pid_then_sleep(pid_path: str, seconds: float) -> None:
    """Record this worker's PID, then sleep — a stand-in for a runaway essentia child."""
    from pathlib import Path

    Path(pid_path).write_text(str(os.getpid()))
    time.sleep(seconds)


# --- pool construction -----------------------------------------------------


def test_create_process_pool_returns_pebble_pool() -> None:
    """create_process_pool returns a killable pebble ProcessPool."""
    pool = create_process_pool()
    try:
        assert isinstance(pool, ProcessPool)
    finally:
        pool.stop()
        pool.join()


async def test_run_in_process_pool_executes_function() -> None:
    """run_in_process_pool schedules the function and returns its result."""
    pool = create_process_pool()
    ctx: dict[str, object] = {"process_pool": pool}
    try:
        result = await run_in_process_pool(ctx, _double, 21)
        assert result == 42
    finally:
        pool.stop()
        pool.join()


async def test_run_in_process_pool_passes_kwargs() -> None:
    """run_in_process_pool forwards keyword arguments to the scheduled function."""
    pool = create_process_pool()
    ctx: dict[str, object] = {"process_pool": pool}
    try:
        result = await run_in_process_pool(ctx, _echo_kwarg, marker="hello")
        assert result == "hello"
    finally:
        pool.stop()
        pool.join()


async def test_run_in_process_pool_kills_runaway_child_on_timeout(tmp_path: Path) -> None:
    """A task exceeding its per-task timeout raises TimeoutError and the child is killed.

    Proves the gating Phase 43 fix: the runaway child PID is gone after the
    timeout (slot reclaimed) and the recycled pool still runs a fresh task.
    No real essentia — a module-level sleeper stands in for the runaway child.
    """
    pid_file = tmp_path / "child.pid"
    pool = create_process_pool()
    ctx: dict[str, object] = {"process_pool": pool}
    try:
        with pytest.raises(TimeoutError):
            # 60s sleep with a 5s inner timeout: the child is SIGKILLed, never completes.
            await run_in_process_pool(ctx, _write_pid_then_sleep, str(pid_file), 60.0, timeout=5.0)

        assert pid_file.exists(), "runaway child never started before the kill"
        child_pid = int(pid_file.read_text())

        # The killed child must be reaped (gone) — poll until os.kill raises.
        deadline = time.monotonic() + 10.0
        killed = False
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except (ProcessLookupError, PermissionError):
                killed = True
                break
            time.sleep(0.05)
        assert killed, f"runaway child {child_pid} still alive after the inner timeout"

        # Slot reclaimed: a fresh task runs on the recycled pool.
        assert await run_in_process_pool(ctx, _double, 21) == 42
    finally:
        pool.stop()
        pool.join()


# --- Phase 43 config knobs -------------------------------------------------


def test_pebble_importable() -> None:
    """pebble (killable ProcessPool backend) imports under the project venv."""
    import pebble

    assert pebble.__name__ == "pebble"
    assert hasattr(pebble, "ProcessPool")


def _agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the minimum env vars needed for AgentSettings() to construct (PHAZE_ROLE=agent)."""
    monkeypatch.setenv("PHAZE_AGENT_API_URL", "http://app.test:8000")
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-abc123")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", "/data/music,/data/concerts")


def test_analysis_timeout_and_cap_knobs_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The three Phase 43 AgentSettings knobs resolve to their documented defaults."""
    from phaze.config import AgentSettings

    _agent_env(monkeypatch)
    # Shadow any .env overrides so the assertions hold against documented defaults.
    monkeypatch.delenv("PHAZE_ANALYSIS_INNER_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("PHAZE_ANALYSIS_FINE_CAP", raising=False)
    monkeypatch.delenv("PHAZE_ANALYSIS_COARSE_CAP", raising=False)
    agent_settings = AgentSettings()
    assert agent_settings.analysis_inner_timeout_sec == 6600
    assert agent_settings.analysis_fine_cap == 60
    assert agent_settings.analysis_coarse_cap == 30


def test_analysis_knobs_honor_env_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each knob picks up its PHAZE_* env alias override."""
    from phaze.config import AgentSettings

    _agent_env(monkeypatch)
    monkeypatch.setenv("PHAZE_ANALYSIS_INNER_TIMEOUT_SEC", "1234")
    monkeypatch.setenv("PHAZE_ANALYSIS_FINE_CAP", "11")
    monkeypatch.setenv("PHAZE_ANALYSIS_COARSE_CAP", "22")
    agent_settings = AgentSettings()
    assert agent_settings.analysis_inner_timeout_sec == 1234
    assert agent_settings.analysis_fine_cap == 11
    assert agent_settings.analysis_coarse_cap == 22


@pytest.mark.parametrize("env_var", ["PHAZE_ANALYSIS_FINE_CAP", "PHAZE_ANALYSIS_COARSE_CAP"])
def test_analysis_caps_reject_below_two(monkeypatch: pytest.MonkeyPatch, env_var: str) -> None:
    """Caps are ge=2: a cap of 1 (or 0) is rejected at load so it can't divide-by-zero in _stride_to_cap."""
    from pydantic import ValidationError

    from phaze.config import AgentSettings

    _agent_env(monkeypatch)
    monkeypatch.setenv(env_var, "1")
    with pytest.raises(ValidationError):
        AgentSettings()


def test_inner_timeout_rejects_at_or_above_outer_net(monkeypatch: pytest.MonkeyPatch) -> None:
    """inner_timeout is lt=7200: a value >= the SAQ outer net would disable the deterministic kill."""
    from pydantic import ValidationError

    from phaze.config import AgentSettings

    _agent_env(monkeypatch)
    monkeypatch.setenv("PHAZE_ANALYSIS_INNER_TIMEOUT_SEC", "7200")
    with pytest.raises(ValidationError):
        AgentSettings()
