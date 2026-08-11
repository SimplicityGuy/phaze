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
# Behaviour 2b (phaze-plpnf): per-function policy floor -- process_file can never
# land on the generic role default (600s/4 retries), regardless of producer or
# replay path.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_file_floor_overrides_role_default_even_when_role_default_is_weaker(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``process_file`` Job with NO explicit policy must land on 7200s/retries=2, never the
    role default -- even when the role default (as configured in production, 2026-08-11) is
    600s/4 retries, i.e. BOTH weaker (shorter timeout) and more retry-happy than the pinned
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

    assert job.timeout == 7200, f"process_file must never run under its 7200s floor: got {job.timeout}"
    assert job.retries == 2, f"process_file must never run over its retries=2 policy: got {job.retries}"


@pytest.mark.asyncio
async def test_process_file_floor_is_a_noop_for_the_pinned_producer_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``process_file`` Job already at its pinned policy (7200s/retries=2 -- what
    ``services.analysis_enqueue.enqueue_process_file``, the sole current producer, always sets)
    must be left byte-identical by the floor. The floor is a MINIMUM/MAXIMUM guard, not a
    reset-to-policy: it must never re-clobber a value that is already at least as protective.
    """
    fake_cfg = MagicMock(
        worker_job_timeout=600,
        worker_max_retries=4,
        worker_keep_result=3600,
    )
    monkeypatch.setattr("phaze.config.get_settings", lambda: fake_cfg)

    job = Job(function="process_file", timeout=7200, retries=2)

    await apply_project_job_defaults(job)

    assert job.timeout == 7200
    assert job.retries == 2


@pytest.mark.asyncio
async def test_process_file_floor_raises_a_shorter_explicit_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``process_file`` Job explicitly enqueued with a SHORTER-than-policy timeout (e.g. a
    stray call site, or a replay that captured a stale pre-policy bound) must still be raised to
    the 7200s floor -- the floor is unconditional, not gated on "still at a default".
    """
    fake_cfg = MagicMock(
        worker_job_timeout=600,
        worker_max_retries=4,
        worker_keep_result=3600,
    )
    monkeypatch.setattr("phaze.config.get_settings", lambda: fake_cfg)

    job = Job(function="process_file", timeout=120, retries=3)

    await apply_project_job_defaults(job)

    assert job.timeout == 7200
    assert job.retries == 2


@pytest.mark.asyncio
async def test_policy_floor_does_not_apply_to_other_functions(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-function floor must not leak onto functions absent from the registry -- the
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

    from phaze.tasks._shared.stage_control import enforce_stage_pause_on_process, repark_if_stage_paused
    from phaze.tasks.agent_worker import settings as agent_settings

    worker = saq.Worker(**agent_settings)
    assert worker is not None

    # phaze-geuq: the agent worker MUST run enforce_stage_pause_on_process before every job's
    # function so a job retried via SAQ's `_retry` (which bypasses before_enqueue) is bounced
    # rather than run fresh on a paused stage; repark_if_stage_paused must run BEFORE
    # increment_completed so a bounce is never mistaken for a genuine COMPLETE/FAILED outcome.
    assert worker.before_process == [enforce_stage_pause_on_process]
    assert worker.after_process is not None
    assert worker.after_process[0] is repark_if_stage_paused
