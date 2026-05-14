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
    # Clear the lru_cache so get_settings re-dispatches to AgentSettings.
    from phaze.config import get_settings

    get_settings.cache_clear()

    import saq

    from phaze.tasks.agent_worker import settings as agent_settings

    worker = saq.Worker(**agent_settings)
    assert worker is not None
