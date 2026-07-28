"""Tests for the HTTP-rewritten fingerprint_file SAQ task (Phase 26 Plan 11)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
import uuid

import httpx
from pydantic import ValidationError
import pytest


def _make_ctx(api_client: AsyncMock | None = None, orchestrator: AsyncMock | None = None) -> dict[str, Any]:
    """Create a minimal SAQ context dict with api_client + orchestrator mocks."""
    if api_client is None:
        api_client = AsyncMock()
        api_client.put_fingerprint = AsyncMock(return_value=MagicMock())
    if orchestrator is None:
        orchestrator = AsyncMock()
    return {"api_client": api_client, "fingerprint_orchestrator": orchestrator}


def _make_payload_kwargs(file_id: uuid.UUID | None = None) -> dict[str, Any]:
    return {
        "file_id": str(file_id or uuid.uuid4()),
        "original_path": "/music/track.mp3",
        "agent_id": "test-agent",
    }


def _make_ingest_result(status: str = "success", error: str | None = None, *, engine_error: bool = False) -> MagicMock:
    """Create a mock IngestResult.

    ``engine_error`` mirrors the real dataclass field (phaze-ds1z): True means the SIDECAR
    failed (5xx/unreachable), False means a healthy sidecar rejected this specific file.
    It must be set explicitly here -- a bare MagicMock attribute is truthy, which would
    silently turn every file-level failure in these tests into a fake outage.
    """
    result = MagicMock()
    result.status = status
    result.error = error
    result.engine_error = engine_error
    return result


async def test_both_engines_success_returns_fingerprinted() -> None:
    """fingerprint_file with both engines succeeding returns status=fingerprinted."""
    from phaze.tasks.fingerprint import fingerprint_file

    api = AsyncMock()
    api.put_fingerprint = AsyncMock(return_value=MagicMock())
    orchestrator = AsyncMock()
    orchestrator.ingest_all = AsyncMock(
        return_value={
            "audfprint": _make_ingest_result("success"),
            "panako": _make_ingest_result("success"),
        },
    )
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)
    file_id = uuid.uuid4()

    result = await fingerprint_file(ctx, **_make_payload_kwargs(file_id=file_id))

    assert result["status"] == "fingerprinted"
    assert result["file_id"] == str(file_id)
    orchestrator.ingest_all.assert_awaited_once_with("/music/track.mp3")
    # One PUT per engine
    assert api.put_fingerprint.await_count == 2
    engines_called = [call.args[1] for call in api.put_fingerprint.await_args_list]
    assert sorted(engines_called) == ["audfprint", "panako"]


async def test_one_engine_fails_returns_partial() -> None:
    """fingerprint_file with one engine failing returns status=partial."""
    from phaze.tasks.fingerprint import fingerprint_file

    api = AsyncMock()
    api.put_fingerprint = AsyncMock(return_value=MagicMock())
    orchestrator = AsyncMock()
    orchestrator.ingest_all = AsyncMock(
        return_value={
            "audfprint": _make_ingest_result("success"),
            "panako": _make_ingest_result("failed", error="HTTP 500"),
        },
    )
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)

    result = await fingerprint_file(ctx, **_make_payload_kwargs())

    assert result["status"] == "partial"
    # Still PUT both engines (so server records the failure too)
    assert api.put_fingerprint.await_count == 2


async def test_http_error_propagates() -> None:
    """put_fingerprint failures propagate (SAQ will retry the job)."""
    from phaze.tasks.fingerprint import fingerprint_file

    api = AsyncMock()
    api.put_fingerprint = AsyncMock(side_effect=RuntimeError("server unreachable"))
    orchestrator = AsyncMock()
    orchestrator.ingest_all = AsyncMock(
        return_value={"audfprint": _make_ingest_result("success")},
    )
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)

    with pytest.raises(RuntimeError, match="server unreachable"):
        await fingerprint_file(ctx, **_make_payload_kwargs())


async def test_orchestrator_error_propagates() -> None:
    """Orchestrator failures propagate (SAQ retries)."""
    from phaze.tasks.fingerprint import fingerprint_file

    api = AsyncMock()
    api.put_fingerprint = AsyncMock()
    orchestrator = AsyncMock()
    orchestrator.ingest_all = AsyncMock(side_effect=RuntimeError("audfprint sidecar down"))
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)

    with pytest.raises(RuntimeError, match="audfprint sidecar down"):
        await fingerprint_file(ctx, **_make_payload_kwargs())
    api.put_fingerprint.assert_not_awaited()


async def test_rejects_extra_kwargs() -> None:
    """FingerprintFilePayload.extra='forbid' rejects unknown fields."""
    from phaze.tasks.fingerprint import fingerprint_file

    api = AsyncMock()
    api.put_fingerprint = AsyncMock()
    orchestrator = AsyncMock()
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)

    bad_kwargs = _make_payload_kwargs()
    bad_kwargs["bogus_field"] = "x"
    with pytest.raises(ValidationError):
        await fingerprint_file(ctx, **bad_kwargs)
    orchestrator.ingest_all.assert_not_awaited()
    api.put_fingerprint.assert_not_awaited()


# ---------------------------------------------------------------------------
# Phase 45 (L-02 / CR-02): terminal-failure ack discipline (mirrors process_file)
# ---------------------------------------------------------------------------


def _job_stub(*, retryable: bool) -> MagicMock:
    """A minimal SAQ Job stub exposing only the ``.retryable`` attribute the guard reads."""
    job = MagicMock()
    job.retryable = retryable
    return job


async def test_terminal_attempt_acks_then_raises() -> None:
    """Terminal attempt (job not retryable): report_fingerprint_failed called once, then re-raise."""
    from phaze.tasks.fingerprint import fingerprint_file

    api = AsyncMock()
    api.put_fingerprint = AsyncMock(return_value=MagicMock())
    api.report_fingerprint_failed = AsyncMock()
    orchestrator = AsyncMock()
    orchestrator.ingest_all = AsyncMock(side_effect=RuntimeError("sidecar down"))
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)
    ctx["job"] = _job_stub(retryable=False)
    file_id = uuid.uuid4()

    with pytest.raises(RuntimeError, match="sidecar down"):
        await fingerprint_file(ctx, **_make_payload_kwargs(file_id=file_id))

    api.report_fingerprint_failed.assert_awaited_once_with(file_id)


async def test_terminal_attempt_acks_on_put_loop_failure() -> None:
    """A failure mid per-engine PUT loop on the terminal attempt also acks once then re-raises."""
    from phaze.tasks.fingerprint import fingerprint_file

    api = AsyncMock()
    api.put_fingerprint = AsyncMock(side_effect=RuntimeError("PUT 500"))
    api.report_fingerprint_failed = AsyncMock()
    orchestrator = AsyncMock()
    orchestrator.ingest_all = AsyncMock(return_value={"audfprint": _make_ingest_result("success")})
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)
    ctx["job"] = _job_stub(retryable=False)
    file_id = uuid.uuid4()

    with pytest.raises(RuntimeError, match="PUT 500"):
        await fingerprint_file(ctx, **_make_payload_kwargs(file_id=file_id))

    api.report_fingerprint_failed.assert_awaited_once_with(file_id)


async def test_terminal_ack_failure_reraises_original_error() -> None:
    """WR-01: on the TERMINAL attempt, if report_fingerprint_failed ALSO raises (E2), the ORIGINAL
    task error (E1) must propagate -- not the ack error. The ack is awaited once, failure swallowed."""
    from phaze.services.agent_client import AgentApiServerError
    from phaze.tasks.fingerprint import fingerprint_file

    api = AsyncMock()
    api.put_fingerprint = AsyncMock(return_value=MagicMock())
    api.report_fingerprint_failed = AsyncMock(side_effect=AgentApiServerError("ack boom"))
    orchestrator = AsyncMock()
    orchestrator.ingest_all = AsyncMock(side_effect=RuntimeError("controller 5xx"))
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)
    ctx["job"] = _job_stub(retryable=False)
    file_id = uuid.uuid4()

    # E1 (the ingest_all RuntimeError) propagates -- NOT E2 (the AgentApiServerError ack).
    with pytest.raises(RuntimeError, match="controller 5xx"):
        await fingerprint_file(ctx, **_make_payload_kwargs(file_id=file_id))

    api.report_fingerprint_failed.assert_awaited_once_with(file_id)


async def test_retryable_attempt_does_not_ack() -> None:
    """Retryable attempt: NO ack (row survives for the real retry), still re-raises."""
    from phaze.tasks.fingerprint import fingerprint_file

    api = AsyncMock()
    api.put_fingerprint = AsyncMock()
    api.report_fingerprint_failed = AsyncMock()
    orchestrator = AsyncMock()
    orchestrator.ingest_all = AsyncMock(side_effect=RuntimeError("transient"))
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)
    ctx["job"] = _job_stub(retryable=True)

    with pytest.raises(RuntimeError, match="transient"):
        await fingerprint_file(ctx, **_make_payload_kwargs())

    api.report_fingerprint_failed.assert_not_awaited()


async def test_job_absent_does_not_ack() -> None:
    """No job in ctx (pure unit context): NO ack, still re-raises (mirrors `job is not None`)."""
    from phaze.tasks.fingerprint import fingerprint_file

    api = AsyncMock()
    api.put_fingerprint = AsyncMock()
    api.report_fingerprint_failed = AsyncMock()
    orchestrator = AsyncMock()
    orchestrator.ingest_all = AsyncMock(side_effect=RuntimeError("boom"))
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)  # no "job" key

    with pytest.raises(RuntimeError, match="boom"):
        await fingerprint_file(ctx, **_make_payload_kwargs())

    api.report_fingerprint_failed.assert_not_awaited()


async def test_success_path_does_not_ack() -> None:
    """Success path: report_fingerprint_failed is NOT called even on the terminal attempt."""
    from phaze.tasks.fingerprint import fingerprint_file

    api = AsyncMock()
    api.put_fingerprint = AsyncMock(return_value=MagicMock())
    api.report_fingerprint_failed = AsyncMock()
    orchestrator = AsyncMock()
    orchestrator.ingest_all = AsyncMock(
        return_value={
            "audfprint": _make_ingest_result("success"),
            "panako": _make_ingest_result("success"),
        },
    )
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)
    ctx["job"] = _job_stub(retryable=False)

    result = await fingerprint_file(ctx, **_make_payload_kwargs())

    assert result["status"] == "fingerprinted"
    api.report_fingerprint_failed.assert_not_awaited()


# ---------------------------------------------------------------------------
# phaze-ds1z: a total ENGINE outage must not be completed as success/partial
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_outage_counter() -> Any:
    """Isolate the module-level consecutive-outage counter between tests."""
    import phaze.tasks.fingerprint as fp_task

    fp_task._reset_engine_outages()
    yield
    fp_task._reset_engine_outages()


async def test_all_engines_engine_level_failure_raises_and_writes_nothing() -> None:
    """Both sidecars 500ing: raise FingerprintEnginesUnavailable, write NO per-engine rows.

    This is the core regression. Pre-fix, this exact input completed the SAQ job with
    status="partial" and PUT a `failed` row per engine -- which is how an 11k-file backlog
    was drained into 22,856 FAILED rows with zero successes while the dashboard looked green.
    """
    from phaze.tasks.fingerprint import FingerprintEnginesUnavailable, fingerprint_file

    api = AsyncMock()
    api.put_fingerprint = AsyncMock(return_value=MagicMock())
    orchestrator = AsyncMock()
    orchestrator.ingest_all = AsyncMock(
        return_value={
            "audfprint": _make_ingest_result("failed", error="HTTP 500: boom", engine_error=True),
            "panako": _make_ingest_result("failed", error="HTTP 500: boom", engine_error=True),
        },
    )
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)

    with pytest.raises(FingerprintEnginesUnavailable, match="all fingerprint engines failed"):
        await fingerprint_file(ctx, **_make_payload_kwargs())

    # No verdict was fabricated for this file -- it stays merely pending, not FAILED.
    api.put_fingerprint.assert_not_awaited()


async def test_engine_outage_is_a_retryable_saq_failure() -> None:
    """The outage raise flows through the normal terminal-ack discipline (retryable => no ack)."""
    from phaze.tasks.fingerprint import FingerprintEnginesUnavailable, fingerprint_file

    api = AsyncMock()
    api.put_fingerprint = AsyncMock(return_value=MagicMock())
    api.report_fingerprint_failed = AsyncMock()
    orchestrator = AsyncMock()
    orchestrator.ingest_all = AsyncMock(
        return_value={"audfprint": _make_ingest_result("failed", error="connect refused", engine_error=True)},
    )
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)
    ctx["job"] = _job_stub(retryable=True)

    with pytest.raises(FingerprintEnginesUnavailable):
        await fingerprint_file(ctx, **_make_payload_kwargs())

    api.report_fingerprint_failed.assert_not_awaited()


async def test_one_engine_up_still_completes_unchanged() -> None:
    """D-18 NON-REGRESSION: one sidecar down at the ENGINE level, one succeeding => unchanged.

    Any-engine success still completes the stage; the dead sibling must not stall the lane.
    """
    from phaze.tasks.fingerprint import fingerprint_file

    api = AsyncMock()
    api.put_fingerprint = AsyncMock(return_value=MagicMock())
    orchestrator = AsyncMock()
    orchestrator.ingest_all = AsyncMock(
        return_value={
            "audfprint": _make_ingest_result("success"),
            "panako": _make_ingest_result("failed", error="HTTP 503", engine_error=True),
        },
    )
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)

    result = await fingerprint_file(ctx, **_make_payload_kwargs())

    assert result["status"] == "partial"
    assert api.put_fingerprint.await_count == 2


async def test_file_level_failure_on_every_engine_still_completes_as_failed() -> None:
    """A corrupt file rejected 4xx by HEALTHY sidecars fails its OWN job, it does not stall the lane.

    The engines are up, so the verdict is real and worth recording: rows ARE written and the
    job completes -- but the reported status is "failed", never "partial" (zero successes).
    """
    from phaze.tasks.fingerprint import fingerprint_file

    api = AsyncMock()
    api.put_fingerprint = AsyncMock(return_value=MagicMock())
    orchestrator = AsyncMock()
    orchestrator.ingest_all = AsyncMock(
        return_value={
            "audfprint": _make_ingest_result("failed", error="HTTP 422: undecodable", engine_error=False),
            "panako": _make_ingest_result("failed", error="HTTP 422: undecodable", engine_error=False),
        },
    )
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)

    result = await fingerprint_file(ctx, **_make_payload_kwargs())

    assert result["status"] == "failed"
    assert api.put_fingerprint.await_count == 2


async def test_mixed_engine_and_file_failure_completes_as_failed() -> None:
    """Zero successes but NOT a pure outage (one file-level failure) => record, don't raise."""
    from phaze.tasks.fingerprint import fingerprint_file

    api = AsyncMock()
    api.put_fingerprint = AsyncMock(return_value=MagicMock())
    orchestrator = AsyncMock()
    orchestrator.ingest_all = AsyncMock(
        return_value={
            "audfprint": _make_ingest_result("failed", error="HTTP 500", engine_error=True),
            "panako": _make_ingest_result("failed", error="HTTP 422", engine_error=False),
        },
    )
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)

    result = await fingerprint_file(ctx, **_make_payload_kwargs())

    assert result["status"] == "failed"


async def test_zero_success_is_never_reported_as_partial() -> None:
    """Acceptance: 'partial' must be reachable ONLY when at least one engine succeeded."""
    from phaze.tasks.fingerprint import fingerprint_file

    api = AsyncMock()
    api.put_fingerprint = AsyncMock(return_value=MagicMock())
    orchestrator = AsyncMock()
    orchestrator.ingest_all = AsyncMock(
        return_value={"audfprint": _make_ingest_result("failed", error="HTTP 400", engine_error=False)},
    )
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)

    result = await fingerprint_file(ctx, **_make_payload_kwargs())

    assert result["status"] != "partial"
    assert result["status"] == "failed"


async def test_outage_alert_escalates_after_threshold(caplog: pytest.LogCaptureFixture) -> None:
    """N consecutive all-engine outages emit ONE loud operator-visible alert per file thereafter."""
    import phaze.tasks.fingerprint as fp_task

    errors = {"audfprint": "HTTP 500", "panako": "HTTP 500"}

    for _ in range(fp_task.ENGINE_OUTAGE_ALERT_THRESHOLD - 1):
        fp_task._note_engine_outage("file-1", errors)
    assert not [r for r in caplog.records if "FINGERPRINT ENGINES DOWN" in r.getMessage()]

    fp_task._note_engine_outage("file-1", errors)
    assert [r for r in caplog.records if "FINGERPRINT ENGINES DOWN" in r.getMessage()]


async def test_engine_success_resets_the_outage_counter() -> None:
    """Self-healing: a successful ingest clears the counter so the alert cannot latch on."""
    import phaze.tasks.fingerprint as fp_task
    from phaze.tasks.fingerprint import fingerprint_file

    fp_task._note_engine_outage("file-1", {"audfprint": "HTTP 500"})
    assert fp_task._consecutive_engine_outages == 1

    api = AsyncMock()
    api.put_fingerprint = AsyncMock(return_value=MagicMock())
    orchestrator = AsyncMock()
    orchestrator.ingest_all = AsyncMock(return_value={"audfprint": _make_ingest_result("success")})
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)

    await fingerprint_file(ctx, **_make_payload_kwargs())

    assert fp_task._consecutive_engine_outages == 0


# ---------------------------------------------------------------------------
# phaze-p3hj.3: a SYSTEMATIC single-engine outage (real orchestrator, not mocked away)
# must not be silently absorbed -- end to end through fingerprint_file.
# ---------------------------------------------------------------------------


async def test_systematic_single_engine_outage_completes_but_is_logged_every_time(caplog: pytest.LogCaptureFixture) -> None:
    """phaze-p3hj.1's exact shape, run through the real SAQ task: audfprint 500s on every one
    of several files while panako keeps succeeding.

    Every prior test in this module stubs ``orchestrator.ingest_all`` with an ``AsyncMock``,
    which proves ``fingerprint_file``'s own branching but never exercises the real HTTP
    classification in ``services/fingerprint.py`` that phaze-p3hj.1 traced the outage through.
    This one wires a REAL ``FingerprintOrchestrator`` with real adapters over
    ``httpx.MockTransport`` so a regression in that layer (e.g. a widened except clause that
    stops logging once the sibling engine "covers" the failure) fails HERE, at the layer SAQ
    actually calls.

    D-18 is unchanged throughout: the job completes ``status="partial"`` on every file, never
    stalling the lane behind the dead engine. What must NOT happen is that repeated success
    erasing every trace of the failure -- so this asserts BOTH old and new signals survive: the
    per-row ``error_message`` PUT (what an operator had to go looking for, phaze-p3hj.1 SS4)
    AND a WARNING-level log naming the dead engine on every single file (the signal that makes
    a sustained pattern visible without opening that table).
    """
    from phaze.services.fingerprint import AudfprintAdapter, FingerprintOrchestrator, PanakoAdapter
    from phaze.tasks.fingerprint import fingerprint_file

    def dead_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="EOFError: Ran out of input")

    def healthy_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    audfprint = AudfprintAdapter(base_url="http://dead:9999")
    audfprint._client = httpx.AsyncClient(transport=httpx.MockTransport(dead_handler), base_url="http://dead:9999")
    panako = PanakoAdapter(base_url="http://healthy:9999")
    panako._client = httpx.AsyncClient(transport=httpx.MockTransport(healthy_handler), base_url="http://healthy:9999")
    orchestrator = FingerprintOrchestrator(engines=[audfprint, panako])

    api = AsyncMock()
    api.put_fingerprint = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)

    n_files = 4
    for _ in range(n_files):
        result = await fingerprint_file(ctx, **_make_payload_kwargs())
        # D-18 unchanged: the dead sibling never stalls the lane.
        assert result["status"] == "partial"

    await audfprint.close()
    await panako.close()

    # Signal 1 (pre-existing, the one an operator had to go looking for): the per-row PUT still
    # carries the engine's own failure for every file.
    audfprint_calls = [call for call in api.put_fingerprint.await_args_list if call.args[1] == "audfprint"]
    assert len(audfprint_calls) == n_files
    assert all(call.args[2].status == "failed" for call in audfprint_calls)

    # Signal 2 (the one this bead pins): a WARNING-level log naming the dead engine on EVERY
    # file -- not just the first -- so the pattern is visible without opening that table.
    warning_records = [r for r in caplog.records if r.levelname == "WARNING" and "audfprint" in r.getMessage()]
    assert len(warning_records) == n_files
