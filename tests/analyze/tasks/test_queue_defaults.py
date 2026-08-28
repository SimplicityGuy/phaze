"""Unit tests for phaze.tasks._shared.queue_defaults (Phase 27 UAT Gap 1).

Background
----------
Phase 27 UAT discovered that SAQ 0.26.3's ``Worker.__init__`` does NOT accept
``timeout``, ``retries``, or ``keep_result``. Those keys are per-Job settings
(Job defaults: 10s / 1 / 600s). Both ``phaze.tasks.controller`` and
``phaze.tasks.agent_worker`` previously passed those keys through their
``settings`` dict, which broke ``saq <module>.settings`` invocation with
``TypeError`` on a fresh docker compose stack.

The fix routes the project's policy defaults
(``worker_job_timeout=600`` / ``worker_max_retries=4`` /
``worker_keep_result=3600``) through a Queue-level ``before_enqueue`` hook
defined in :mod:`phaze.tasks._shared.queue_defaults`.

These tests would have caught the original bug at unit-test level had they
existed.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from saq import Job

from phaze.tasks._shared.queue_defaults import (
    _SAQ_DEFAULT_RETRIES,
    _SAQ_DEFAULT_TIMEOUT,
    _SAQ_DEFAULT_TTL,
    apply_project_job_defaults,
)


# ----------------------------------------------------------------------
# Behaviour 1: hook applies project defaults to a Job at SAQ defaults
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_before_enqueue_applies_project_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Job constructed with SAQ defaults must inherit Phaze's policy values."""
    fake_cfg = MagicMock(
        worker_job_timeout=600,
        worker_max_retries=4,
        worker_keep_result=3600,
    )
    monkeypatch.setattr("phaze.config.get_settings", lambda: fake_cfg)

    job = Job(function="x")
    # Sanity: the Job ships with SAQ defaults out of the box.
    assert job.timeout == _SAQ_DEFAULT_TIMEOUT
    assert job.retries == _SAQ_DEFAULT_RETRIES
    assert job.ttl == _SAQ_DEFAULT_TTL

    await apply_project_job_defaults(job)

    assert job.timeout == 600, f"timeout not applied: got {job.timeout}"
    assert job.retries == 4, f"retries not applied: got {job.retries}"
    assert job.ttl == 3600, f"ttl not applied: got {job.ttl}"


# ----------------------------------------------------------------------
# Behaviour 2: hook preserves caller-supplied overrides
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_before_enqueue_preserves_explicit_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Job with explicit non-default values must be left alone.

    Enqueue call sites that deliberately tune per-job timeout/retries/ttl
    (e.g., batch execution tasks) MUST keep those overrides intact.
    """
    fake_cfg = MagicMock(
        worker_job_timeout=600,
        worker_max_retries=4,
        worker_keep_result=3600,
    )
    monkeypatch.setattr("phaze.config.get_settings", lambda: fake_cfg)

    job = Job(function="x", timeout=42, retries=9, ttl=7200)

    await apply_project_job_defaults(job)

    assert job.timeout == 42, "explicit timeout was clobbered"
    assert job.retries == 9, "explicit retries was clobbered"
    assert job.ttl == 7200, "explicit ttl was clobbered"


# ----------------------------------------------------------------------
# Behaviour 2b (phaze-plpnf, re-shaped by phaze-w55w1): per-function job policy -- process_file
# can never land on the generic role default (600s/4 retries), regardless of producer or replay
# path. Its policy is now `timeout=0` (SAQ's "disabled") plus a progress heartbeat, so the
# timeout half is enforced as a PIN rather than a floor: 0 is below every value a floor could
# raise from, and a floor would silently leave a replayed job on the 600s role default -- the
# very defect this registry exists to prevent.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_file_policy_overrides_role_default_even_when_role_default_is_weaker(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``process_file`` Job with NO explicit policy must land on timeout=0/retries=2, never the
    role default -- even when the role default (as configured in production, 2026-08-11) is
    600s/4 retries, i.e. BOTH wrong (a wall clock at all) and more retry-happy than the pinned
    policy. This is exactly the replay-with-NULL-bounds shape (``reenqueue._replay_row`` omits
    ``timeout``/``retries`` entirely for a legacy ledger row with NULL captured bounds), reproduced
    at the hook level: a bare ``Job(function="process_file")`` starts at the SAQ defaults
    (10s/1 retry), which is what a ``queue.enqueue("process_file", key=..., **payload)`` call with
    no policy kwargs constructs.
    """
    fake_cfg = MagicMock(
        worker_job_timeout=600,
        worker_max_retries=4,
        worker_keep_result=3600,
    )
    monkeypatch.setattr("phaze.config.get_settings", lambda: fake_cfg)

    job = Job(function="process_file")

    await apply_project_job_defaults(job)

    assert job.timeout == 0, f"process_file must never carry a wall-clock timeout: got {job.timeout}"
    assert job.retries == 2, f"process_file must never run over its retries=2 policy: got {job.retries}"


@pytest.mark.asyncio
async def test_process_file_policy_is_a_noop_for_the_pinned_producer_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``process_file`` Job already at its pinned policy (timeout=0/retries=2 -- what
    ``services.analysis_enqueue.enqueue_process_file``, the sole current producer, always sets)
    must be left byte-identical by the hook, and in particular the hook's generic "fill a job
    still at a default" step must not treat 0 as a value in need of helping (phaze-w55w1).
    """
    fake_cfg = MagicMock(
        worker_job_timeout=600,
        worker_max_retries=4,
        worker_keep_result=3600,
    )
    monkeypatch.setattr("phaze.config.get_settings", lambda: fake_cfg)

    job = Job(function="process_file", timeout=0, retries=2)

    await apply_project_job_defaults(job)

    assert job.timeout == 0
    assert job.retries == 2


@pytest.mark.asyncio
async def test_process_file_policy_pins_over_any_explicit_wall_clock_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``process_file`` Job explicitly enqueued with ANY wall-clock timeout -- a stray call
    site, or a replay of a ledger row that captured the pre-phaze-w55w1 7200s bound -- is pinned
    back to 0. This is the load-bearing direction now: those 7200s rows really are on disk, and
    replaying one at its captured bound would kill a progressing multi-hour analysis at 2h, the
    exact outcome ADR-0007 §7 removed.
    """
    fake_cfg = MagicMock(
        worker_job_timeout=600,
        worker_max_retries=4,
        worker_keep_result=3600,
    )
    monkeypatch.setattr("phaze.config.get_settings", lambda: fake_cfg)

    job = Job(function="process_file", timeout=7200, retries=3)

    await apply_project_job_defaults(job)

    assert job.timeout == 0
    assert job.retries == 2


@pytest.mark.asyncio
async def test_policy_pin_does_not_apply_to_other_functions(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-function policy must not leak onto functions absent from the registry -- the
    generic "still at SAQ default" contract for every OTHER function is unchanged."""
    fake_cfg = MagicMock(
        worker_job_timeout=600,
        worker_max_retries=4,
        worker_keep_result=3600,
    )
    monkeypatch.setattr("phaze.config.get_settings", lambda: fake_cfg)

    job = Job(function="extract_file_metadata")

    await apply_project_job_defaults(job)

    assert job.timeout == 600
    assert job.retries == 4


# ----------------------------------------------------------------------
# Behaviour 3: SAQ Worker can be constructed from the controller settings dict.
# ----------------------------------------------------------------------


def test_controller_settings_construct_real_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """``saq.Worker(**phaze.tasks.controller.settings)`` must NOT raise TypeError.

    This is the canonical regression test for Phase 27 UAT Gap 1. The original
    bug surfaced as ``TypeError: __init__() got an unexpected keyword argument
    'timeout'`` when ``saq phaze.tasks.controller.settings`` was launched. The
    fix removed ``timeout`` / ``retries`` / ``keep_result`` from the dict.
    """
    # Force PHAZE_ROLE=control before any import so module-level Queue.from_url
    # picks up a control-mode redis URL (defaults are fine here).
    monkeypatch.setenv("PHAZE_ROLE", "control")
    # Clear the lru_cache so get_settings re-dispatches.
    from phaze.config import get_settings

    get_settings.cache_clear()

    import saq

    from phaze.tasks.controller import settings as controller_settings

    # ``settings`` is consumed by saq's CLI exactly like so:
    #   worker = Worker(**settings)
    # If any key is unknown to Worker.__init__ it raises TypeError.
    worker = saq.Worker(**controller_settings)
    assert worker is not None


# ----------------------------------------------------------------------
# Behaviour 4: SAQ Worker can be constructed from the agent_worker settings dict.
# ----------------------------------------------------------------------


def _record(sink: list[str], name: str) -> Any:
    """An async no-op that records that it ran -- for asserting hook ORDER, not hook identity."""

    async def _hook(_ctx: dict[str, Any]) -> None:
        sink.append(name)

    return _hook


def test_agent_worker_settings_construct_real_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """``saq.Worker(**phaze.tasks.agent_worker.settings)`` must NOT raise TypeError.

    Parallel to Gap 1's controller-side fix: the agent_worker entry point is
    invoked via ``saq phaze.tasks.agent_worker.settings`` on file-server hosts.
    The same three rejected keys (``timeout`` / ``retries`` / ``keep_result``)
    were present in this dict; Worker.__init__ would have rejected them.
    """
    # AgentSettings requires these env vars; set them before any import so the
    # module-level Queue.from_url and AgentSettings() validator both pass.
    monkeypatch.setenv("PHAZE_ROLE", "agent")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", "http://app.test:8000")
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test-TOKEN-1234567890ab")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", "/data/music")
    monkeypatch.setenv("PHAZE_AGENT_QUEUE", "phaze-agent-test")
    # phaze-27myl: fileserver-kind (the default) AgentSettings now fail-fasts on the default
    # (docker-service-name) queue_url.
    monkeypatch.setenv("PHAZE_QUEUE_URL", "postgresql://phaze:phaze@app-server.example:5432/phaze")
    # Clear the lru_cache so get_settings re-dispatches to AgentSettings.
    from phaze.config import get_settings

    get_settings.cache_clear()

    import saq

    from phaze.tasks import agent_worker as agent_worker_module
    from phaze.tasks._shared.stage_control import repark_if_stage_paused
    from phaze.tasks.agent_worker import settings as agent_settings
    from phaze.telemetry.saq import after_process as telemetry_after_process

    worker = saq.Worker(**agent_settings)
    assert worker is not None

    # phaze-geuq: the agent worker MUST run enforce_stage_pause_on_process before every job's
    # function so a job retried via SAQ's `_retry` (which bypasses before_enqueue) is bounced
    # rather than run fresh on a paused stage; repark_if_stage_paused must run BEFORE
    # increment_completed so a bounce is never mistaken for a genuine COMPLETE/FAILED outcome.
    #
    # phaze-m1drf.1: SAQ takes ONE before_process callable (only after_process accepts a list),
    # so the pause check is now composed with the telemetry hook inside
    # `agent_worker._before_process`. The invariant this test protects is unchanged and is
    # asserted more directly than before: the pause check still runs, and it still runs FIRST.
    assert worker.before_process is not None
    assert len(worker.before_process) == 1
    composed = worker.before_process[0]
    assert composed is agent_worker_module._before_process
    called: list[str] = []
    monkeypatch.setattr(agent_worker_module, "enforce_stage_pause_on_process", _record(called, "pause"))
    monkeypatch.setattr(agent_worker_module, "telemetry_before_process", _record(called, "telemetry"))
    asyncio.run(composed({}))
    assert called == ["pause", "telemetry"], "the pause check must run, and must run before telemetry"

    assert worker.after_process is not None
    assert worker.after_process[0] is repark_if_stage_paused
    assert worker.after_process[-1] is telemetry_after_process, "telemetry runs LAST so it measures the other hooks' work too"


@pytest.mark.asyncio
async def test_process_file_heartbeat_is_pinned_even_when_the_job_carries_a_stale_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The heartbeat is PINNED, not filled-if-absent (phaze-w55w1).

    A replayed row can carry a stale heartbeat from an older deployment's configuration just as
    easily as none at all, and both must land on the CURRENT policy -- otherwise an operator who
    raises the stall threshold leaves old rows supervised on the old, now-too-tight deadline.
    """
    fake_cfg = MagicMock(
        worker_job_timeout=600,
        worker_max_retries=4,
        worker_keep_result=3600,
        analysis_job_heartbeat_sec=3600,
    )
    monkeypatch.setattr("phaze.config.get_settings", lambda: fake_cfg)

    job = Job(function="process_file", timeout=0, retries=2, heartbeat=120)

    await apply_project_job_defaults(job)

    assert job.heartbeat == 3600


@pytest.mark.asyncio
async def test_heartbeat_pin_does_not_leak_onto_other_functions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only functions in the heartbeat registry are pinned; everything else keeps SAQ's default 0.

    Other jobs carry a real wall-clock `timeout`, so `Job.stuck` already works for them and an
    unasked-for heartbeat would make them sweepable on a signal nothing is emitting.
    """
    fake_cfg = MagicMock(
        worker_job_timeout=600,
        worker_max_retries=4,
        worker_keep_result=3600,
        analysis_job_heartbeat_sec=3600,
    )
    monkeypatch.setattr("phaze.config.get_settings", lambda: fake_cfg)

    job = Job(function="extract_metadata")

    await apply_project_job_defaults(job)

    assert job.heartbeat == 0
    assert job.timeout == 600
