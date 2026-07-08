"""quick-260707-dh1: lane-parametrized agent_worker settings.

Each lane worker (driven by PHAZE_AGENT_LANE) must build its own queue name, register
ONLY its lane's functions (from LANE_TASKS), and use its lane concurrency knob. All-mode
(PHAZE_AGENT_LANE unset) preserves today's exact behavior (base queue, all 8 functions,
worker_max_jobs). An invalid lane fails loud at import.

The module reads PHAZE_AGENT_LANE / PHAZE_AGENT_QUEUE at import time and caches `settings`
as a top-level attribute, so each case reloads the module under the desired env. An autouse
finalizer evicts it from sys.modules so a later importer re-imports fresh under its own env
(the in-process heartbeat tests rely on a clean re-import).

Kept Postgres/DB-free: build_pipeline_queue builds the PostgresQueue open=False (no socket),
so importing under the agent env never connects. Mirrors the import-boundary invariant guarded
by tests/shared/core/test_task_split.py.
"""

from __future__ import annotations

import importlib
import sys
from typing import TYPE_CHECKING

import pytest

from phaze.services.enqueue_router import AGENT_TASKS, LANE_TASKS


if TYPE_CHECKING:
    from types import ModuleType


def _set_base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHAZE_ROLE", "agent")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", "http://test")
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-1234567890abcdef")
    monkeypatch.setenv("PHAZE_AGENT_QUEUE", "phaze-agent-nox")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", "/var/empty")
    monkeypatch.setenv("PHAZE_QUEUE_URL", "postgresql://phaze:phaze@localhost:5432/phaze")
    monkeypatch.setenv("PHAZE_REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture(autouse=True)
def _evict_agent_worker() -> None:
    """After each test, drop agent_worker from sys.modules so the next import is fresh."""
    yield
    sys.modules.pop("phaze.tasks.agent_worker", None)


def _reload_worker(monkeypatch: pytest.MonkeyPatch, *, lane: str | None) -> ModuleType:
    """(Re)import agent_worker under the base agent env + the given PHAZE_AGENT_LANE."""
    _set_base_env(monkeypatch)
    if lane is None:
        monkeypatch.delenv("PHAZE_AGENT_LANE", raising=False)
    else:
        monkeypatch.setenv("PHAZE_AGENT_LANE", lane)
    sys.modules.pop("phaze.tasks.agent_worker", None)
    return importlib.import_module("phaze.tasks.agent_worker")


def _registered_names(mod: ModuleType) -> set[str]:
    """SAQ task names of the registered functions (plain fn __name__ or (name, fn) tuple[0])."""
    names: set[str] = set()
    for entry in mod.settings["functions"]:
        if isinstance(entry, tuple):
            names.add(entry[0])
        else:
            names.add(entry.__name__)
    return names


@pytest.mark.parametrize(
    ("lane", "expected_concurrency_attr"),
    [
        ("analyze", "lane_analyze_concurrency"),
        ("fingerprint", "lane_fingerprint_concurrency"),
        ("meta", "lane_meta_concurrency"),
        ("io", "lane_io_concurrency"),
    ],
)
def test_lane_selects_queue_functions_and_concurrency(monkeypatch: pytest.MonkeyPatch, lane: str, expected_concurrency_attr: str) -> None:
    """Each lane -> phaze-agent-nox-<lane>, ONLY LANE_TASKS[lane] functions, its concurrency knob."""
    mod = _reload_worker(monkeypatch, lane=lane)
    from phaze.config import get_settings

    assert mod.settings["queue"].name == f"phaze-agent-nox-{lane}"
    assert _registered_names(mod) == set(LANE_TASKS[lane])
    assert mod.settings["concurrency"] == getattr(get_settings(), expected_concurrency_attr)


def test_io_lane_keeps_s3_upload_tuple(monkeypatch: pytest.MonkeyPatch) -> None:
    """The io lane registers s3_upload as the (name, func) tuple + push_file (plain)."""
    mod = _reload_worker(monkeypatch, lane="io")
    entries = mod.settings["functions"]
    s3_entries = [e for e in entries if isinstance(e, tuple) and e[0] == "s3_upload"]
    assert len(s3_entries) == 1, "s3_upload must stay the (name, func) tuple"
    assert _registered_names(mod) == {"s3_upload", "push_file"}


def test_all_mode_preserves_todays_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    """PHAZE_AGENT_LANE unset -> base queue, all 8 functions, worker_max_jobs concurrency."""
    mod = _reload_worker(monkeypatch, lane=None)
    from phaze.config import get_settings

    assert mod.settings["queue"].name == "phaze-agent-nox"
    assert _registered_names(mod) == set(AGENT_TASKS)
    assert len(mod.settings["functions"]) == 8
    assert mod.settings["concurrency"] == get_settings().worker_max_jobs


def test_union_of_lane_functions_equals_agent_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    """The union of the four lanes' registered SAQ names == AGENT_TASKS (mirror contract)."""
    union: set[str] = set()
    for lane in ("analyze", "fingerprint", "meta", "io"):
        union |= _registered_names(_reload_worker(monkeypatch, lane=lane))
        sys.modules.pop("phaze.tasks.agent_worker", None)
    assert union == set(AGENT_TASKS)


def test_invalid_lane_raises_at_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo'd PHAZE_AGENT_LANE fails loud at module import (container boots non-zero)."""
    with pytest.raises(RuntimeError, match="invalid PHAZE_AGENT_LANE"):
        _reload_worker(monkeypatch, lane="analyzeXX")


def test_lane_concurrency_clamped_by_worker_max_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """quick-260707-g84: an explicit WORKER_MAX_JOBS=1 ceiling clamps the analyze lane to 1.

    The OCI Ampere A1 (12 GB) compute agent runs the analyze lane, where a single
    `process_file` job peaks ~8 GB. Before the fix the lane knob (default 4) governed
    concurrency and WORKER_MAX_JOBS was inert in lane mode -> 4 concurrent jobs OOM-killed
    the host. The resolution is now `min(lane knob, worker_max_jobs)`, so the explicit
    WORKER_MAX_JOBS=1 cap is authoritative.
    """
    monkeypatch.setenv("WORKER_MAX_JOBS", "1")
    mod = _reload_worker(monkeypatch, lane="analyze")

    assert mod.settings["concurrency"] == 1


def test_lane_concurrency_default_unclamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """quick-260707-g84: with no WORKER_MAX_JOBS override the analyze lane keeps its knob (4).

    The file-server default case (lane 4, worker_max_jobs default 8) is unchanged:
    min(4, 8) == 4. Only an explicit, lower WORKER_MAX_JOBS ceiling clamps a lane.
    """
    mod = _reload_worker(monkeypatch, lane="analyze")

    assert mod.settings["concurrency"] == 4


def test_agent_worker_queue_pool_stays_one_four(monkeypatch: pytest.MonkeyPatch) -> None:
    """quick-260707-ryn REGRESSION: the agent's OWN lane queue pool stays min_size=1/max_size=4.

    The ryn dispatch-footprint reduction trimmed the CONTROL-side per-(agent,lane) dispatch
    queues to 0/2, but the agent worker's own consumer queue (agent_worker.py:354) is a
    long-lived DRAIN pool that must keep a warm connection -- it MUST NOT be swept into that
    reduction. Read off the SAQ ``queue.min_size`` / ``queue.max_size`` attributes: SAQ carries
    the configured sizing there at construction and only resizes the underlying psycopg pool at
    ``connect()``, so the unopened queue's ``pool.min_size`` would still report psycopg's default.
    """
    mod = _reload_worker(monkeypatch, lane="analyze")

    assert mod.queue.min_size == 1
    assert mod.queue.max_size == 4
