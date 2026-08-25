"""Unit tests for ``phaze.tasks.controller._run_boot_reconcile_with_retry`` (phaze-sekvl, phaze-k309t).

The retry-with-backoff helper closes the boot race where ``worker`` and ``api`` share an
IDENTICAL ``depends_on: postgres: service_healthy`` with no ordering edge, so a one-shot boot
reconcile (``backfill_ledger_from_saq_jobs`` / ``recover_orphaned_work``) can observe a
pre-``alembic upgrade head`` schema and fail the FIRST attempt of the process lifetime -- with
no cron to retry it, that used to strand recovery permanently until an operator restarted the
worker. These tests exercise the helper directly (not through the whole ``startup()`` boot
sequence, which needs a working async session for the backfill half) because it is the boot-race
mitigation itself, and until this bead it had zero direct coverage: every existing
``test_controller_startup_*`` module drives ``startup()`` with collaborators stubbed enough to
reach the retry loop, but none of them exercise the actual RETRY (schema-not-ready) branch --
their stubbed ``ctx["async_session"]()`` raises a plain (non-``DBAPIError``) failure, which only
ever walks the ``except Exception: ... break`` non-retryable branch.

Each test asserts an OBSERVABLE outcome (D-07): the returned value, how many times ``attempt_fn``
actually ran, how many times the backoff slept, and the emitted log line.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import DBAPIError

from phaze.tasks.controller import _run_boot_reconcile_with_retry
from tests.shared.tasks._shared import make_stub_session_factory


def _schema_not_ready_error() -> DBAPIError:
    """A DBAPIError shaped like the boot race's actual failure (relation does not exist yet)."""
    return DBAPIError("SELECT 1", {}, Exception('relation "scheduling_ledger" does not exist'))


@pytest.mark.asyncio
async def test_succeeds_on_the_first_attempt_without_retrying_or_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    """The common case: no schema race, no retry budget spent."""
    fake_sleep = AsyncMock()
    monkeypatch.setattr("phaze.tasks.controller.asyncio.sleep", fake_sleep)

    calls = 0

    async def attempt_fn() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"inserted": 3, "skipped": 0}

    result = await _run_boot_reconcile_with_retry("ledger backfill on startup", attempt_fn, attempts=5, delay_seconds=1.0)

    assert result == {"inserted": 3, "skipped": 0}
    assert calls == 1
    fake_sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_retries_a_transient_dbapi_error_then_succeeds(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """phaze-sekvl core case: the schema isn't ready for the first two attempts, then it is."""
    fake_sleep = AsyncMock()
    monkeypatch.setattr("phaze.tasks.controller.asyncio.sleep", fake_sleep)

    calls = 0

    async def attempt_fn() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _schema_not_ready_error()
        return {"detected_loss": False, "stages": []}

    with caplog.at_level(logging.WARNING):
        result = await _run_boot_reconcile_with_retry("recover_orphaned_work on startup", attempt_fn, attempts=5, delay_seconds=2.0)

    assert result == {"detected_loss": False, "stages": []}
    assert calls == 3, "must retry exactly until the attempt that succeeds, no more"
    # One retry-warning per failed attempt (two failures -> two warnings), and the backoff slept
    # between each -- never after the final, successful attempt.
    assert fake_sleep.await_count == 2
    fake_sleep.assert_awaited_with(2.0)
    warnings = [r for r in caplog.records if "retrying" in r.getMessage()]
    assert len(warnings) == 2
    assert all("recover_orphaned_work on startup" in r.getMessage() for r in warnings)


@pytest.mark.asyncio
async def test_exhausts_every_attempt_and_returns_none(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """The schema race never clears within the retry budget -- boot resilience still applies (D-05):
    the helper returns ``None`` rather than raising, so a persistent failure never aborts controller
    boot, but it DOES log a distinct final-attempt failure (not just another "retrying" warning).
    """
    fake_sleep = AsyncMock()
    monkeypatch.setattr("phaze.tasks.controller.asyncio.sleep", fake_sleep)

    calls = 0

    async def attempt_fn() -> None:
        nonlocal calls
        calls += 1
        raise _schema_not_ready_error()

    with caplog.at_level(logging.WARNING):
        result = await _run_boot_reconcile_with_retry("ledger backfill on startup", attempt_fn, attempts=3, delay_seconds=0.01)

    assert result is None
    assert calls == 3, "every attempt in the budget must run"
    # Two retries (attempts 1 and 2) + one final-failure log (attempt 3) = 3 sleeps minus the last.
    assert fake_sleep.await_count == 2, "the backoff must not sleep after the FINAL attempt"
    final_failures = [r for r in caplog.records if "failed on final attempt" in r.getMessage()]
    assert len(final_failures) == 1
    assert "ledger backfill on startup" in final_failures[0].getMessage()


@pytest.mark.asyncio
async def test_a_non_retryable_exception_stops_immediately_without_spending_the_retry_budget(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Only a DBAPIError (schema-not-ready) is worth retrying. Any OTHER exception -- a genuine bug
    in the reconcile logic, not a boot race -- must surface (via the log) on the FIRST attempt
    instead of silently consuming the whole retry budget on a failure retrying can never fix.
    """
    fake_sleep = AsyncMock()
    monkeypatch.setattr("phaze.tasks.controller.asyncio.sleep", fake_sleep)

    calls = 0

    async def attempt_fn() -> None:
        nonlocal calls
        calls += 1
        raise ValueError("a genuine bug, not a schema race")

    with caplog.at_level(logging.WARNING):
        result = await _run_boot_reconcile_with_retry("recover_orphaned_work on startup", attempt_fn, attempts=5, delay_seconds=1.0)

    assert result is None
    assert calls == 1, "a non-retryable exception must not spend the retry budget"
    fake_sleep.assert_not_awaited()
    failures = [r for r in caplog.records if "recover_orphaned_work on startup" in r.getMessage()]
    assert len(failures) == 1
    assert "failed on final attempt" not in failures[0].getMessage(), "distinct log shape from the retry-exhausted case"


# ---------------------------------------------------------------------------
# startup()'s consumption of the retry helper's RESULT -- the recovery success log (line 254 of
# controller.py), which none of the other test_controller_startup_*.py modules exercise: their
# stubbed ``ctx["async_session"]()`` cannot support ``async with``, so ``_do_backfill`` always
# fails non-retryably there and ``recover_orphaned_work`` is never mocked to actually succeed.
# ---------------------------------------------------------------------------


def _stub_controller_for_boot_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub every startup() collaborator that does not matter here (mirrors the sibling
    ``test_controller_startup_*.py`` modules' ``_stub_controller`` recipe): heavyweight
    constructors, an all-local/no-bucket config (skips the LocalQueue probe and the lifecycle-TTL
    loop), and a Redis client stub.
    """
    monkeypatch.setattr("phaze.database.create_async_engine", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.async_sessionmaker", lambda *_a, **_kw: make_stub_session_factory())
    monkeypatch.setattr("phaze.tasks.controller.DiscogsographyClient", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.load_prompt_template", lambda: "stub")
    monkeypatch.setattr("phaze.tasks.controller.ProposalService", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.AgentTaskRouter", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.redis_async.Redis.from_url", lambda *_a, **_kw: AsyncMock())

    fake_cfg = MagicMock()
    fake_cfg.redis_url = "redis://localhost:6379/0"
    fake_cfg.database_url = "postgresql+asyncpg://test"
    fake_cfg.queue_url = "postgresql+asyncpg://test"
    fake_cfg.debug = False
    fake_cfg.discogsography_url = "http://test"
    fake_cfg.llm_model = "stub-model"
    fake_cfg.llm_max_rpm = 60
    fake_cfg.log_level = "INFO"
    fake_cfg.log_json = False
    fake_cfg.anthropic_api_key = None
    fake_cfg.openai_api_key = None
    fake_cfg.backends = []  # no kueue backend -> the LocalQueue probe is skipped, out of scope here
    fake_cfg.buckets = []  # no bucket -> the lifecycle-TTL loop is a no-op, out of scope here
    monkeypatch.setattr("phaze.tasks.controller.get_settings", lambda: fake_cfg)


@pytest.mark.asyncio
async def test_startup_logs_the_recovery_result_when_recover_orphaned_work_succeeds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the gated boot recovery actually runs and returns a result, startup() logs its
    ``detected_loss``/``stages`` summary (controller.py line 254) -- distinct from the ``tally is
    None`` (retry-exhausted or non-retryable) case, which logs nothing.

    ``recover_orphaned_work`` is patched directly (it takes ``ctx``, not a session, so this needs
    none of the ``ctx["async_session"]`` plumbing the ledger-backfill half does) -- the retry
    helper itself is proven correct by the tests above; this only proves startup() reports a
    non-``None`` result.
    """
    _stub_controller_for_boot_log(monkeypatch)
    monkeypatch.setattr(
        "phaze.tasks.controller.recover_orphaned_work",
        AsyncMock(return_value={"detected_loss": True, "stages": ["analysis", "tracklist"]}),
    )

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    await controller.startup(ctx)

    out = capsys.readouterr().out
    assert "phaze.controller startup recovery" in out
    assert "detected_loss" in out
    assert "analysis" in out and "tracklist" in out
