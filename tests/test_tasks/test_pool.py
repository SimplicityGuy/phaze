"""Tests for process pool lifecycle and helpers.

The process pool is owned by ``phaze.tasks.agent_worker`` (Phase 26 D-03 +
D-04): only the agent role runs CPU-bound essentia analysis, so the controller
role does not need a process pool. The agent worker's startup/shutdown
behaviour is exercised by tests/test_tasks/test_agent_startup_banner.py;
this file covers the pool helpers themselves.
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from typing import TYPE_CHECKING

from phaze.config import settings
from phaze.tasks.pool import create_process_pool, run_in_process_pool


if TYPE_CHECKING:
    import pytest


def test_create_process_pool_returns_executor() -> None:
    """create_process_pool returns a ProcessPoolExecutor with correct max_workers."""
    pool = create_process_pool()
    try:
        assert isinstance(pool, ProcessPoolExecutor)
        assert pool._max_workers == settings.worker_process_pool_size
    finally:
        pool.shutdown(wait=False)


async def test_run_in_process_pool_executes_function() -> None:
    """run_in_process_pool calls run_in_executor and returns result."""
    pool = ProcessPoolExecutor(max_workers=1)
    ctx: dict[str, object] = {"process_pool": pool}
    try:
        result = await run_in_process_pool(ctx, _double, 21)
        assert result == 42
    finally:
        pool.shutdown(wait=False)


def _double(x: int) -> int:
    """Simple test function for process pool."""
    return x * 2


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
