"""FAIL-01 (Phase 81, D-05/D-06/D-07/D-13): the analyze failure marker is a durable dual-write.

`report_analysis_failed` now stamps a per-stage marker on the file's 1:1 ``analysis`` row --
``failed_at`` + ``error_message = "<reason>: <error>"`` -- while KEEPING the ``state = ANALYSIS_FAILED``
write (D-05). Because a pure analyze failure never wrote an ``analysis`` row, the write is
``INSERT .. ON CONFLICT (file_id) DO UPDATE`` (a bare UPDATE would silently no-op, D-06); it clears
``analysis_completed_at`` in the same row so the migration-033 XOR CHECK never sees a mixed row.
``put_analysis`` conversely clears ``failed_at``/``error_message`` on any real success (D-13), which is
also what lets its completion branch stamp ``analysis_completed_at`` without violating the CHECK.

This suite covers the three behaviors: (a) failure with NO prior analysis row inserts the marker
(RESEARCH OQ2); (b) failure on a previously-COMPLETED file is a guarded no-op (phaze-ts1d WR-01
equivalent) -- ``analysis_completed_at`` and the payload columns are left untouched, only the
ledger/staged-object/awaiting-reap side effects fire; (c) a success after a failure clears
the marker and stamps ``analysis_completed_at``. Every assertion re-checks the XOR invariant.

Uses the inline smoke-app + authed-agent client pattern from tests/agents/routers/test_agent_analysis.py;
parallel-safe and independent of main.py wiring. Must pass in the ``analyze`` bucket in isolation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import func as sa_func, select, update

from phaze.database import get_session
from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.routers.agent_analysis import router as agent_analysis_router
from phaze.services.scheduling_ledger import upsert_ledger_entry


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


def _make_smoke_app(session: AsyncSession) -> FastAPI:
    """Build a small FastAPI app that wires the agent_analysis router (mirrors test_agent_analysis.py)."""
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_analysis_router)
    app.dependency_overrides[get_session] = lambda: session
    return app


async def _seed_file(session: AsyncSession, agent_id: str) -> uuid.UUID:
    """Seed a FileRecord so AnalysisResult.file_id FK (files.id) is satisfied."""
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            id=file_id,
            agent_id=agent_id,
            sha256_hash="0" * 64,
            original_path=f"/test/music/{file_id}.mp3",
            original_filename=f"{file_id}.mp3",
            current_path=f"/test/music/{file_id}.mp3",
            file_type="mp3",
            file_size=1024,
        )
    )
    await session.commit()
    return file_id


async def _seed_ledger(session: AsyncSession, file_id: uuid.UUID) -> None:
    """Seed the process_file:<file_id> ledger row so the failure callback's clear has something to remove."""
    await upsert_ledger_entry(session, key=f"process_file:{file_id}", function="process_file", kwargs={"file_id": str(file_id)})
    await session.commit()


async def _ledger_present(session: AsyncSession, file_id: uuid.UUID) -> bool:
    session.expire_all()
    row = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == f"process_file:{file_id}"))).scalar_one_or_none()
    return row is not None


async def _analysis_row(session: AsyncSession, file_id: uuid.UUID) -> AnalysisResult | None:
    session.expire_all()
    return (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one_or_none()


async def _no_mixed_row_exists(session: AsyncSession) -> bool:
    """The D-06 invariant: no ``analysis`` row ever has BOTH analysis_completed_at and failed_at set."""
    session.expire_all()
    mixed = (
        await session.execute(
            select(sa_func.count())
            .select_from(AnalysisResult)
            .where(AnalysisResult.analysis_completed_at.is_not(None), AnalysisResult.failed_at.is_not(None))
        )
    ).scalar_one()
    return mixed == 0


# ---------------------------------------------------------------------------
# (a) failure with NO prior analysis row inserts the marker (RESEARCH OQ2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_failed_inserts_marker_with_no_prior_row(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """FAIL-01/D-06: a pure analyze failure (no prior analysis row) INSERTs a marker via ON CONFLICT DO UPDATE.

    A bare UPDATE would silently no-op here (no row to update), so the durable marker would be lost --
    the exact reason the writer uses INSERT .. ON CONFLICT. Asserts the marker fields, the kept state
    write (D-05 dual-write), the cleared ledger, and the XOR invariant.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    await _seed_ledger(session, file_id)
    assert await _analysis_row(session, file_id) is None, "precondition: a pure analyze failure has NO prior analysis row"
    assert await _ledger_present(session, file_id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.post(f"/api/internal/agent/analysis/{file_id}/failed", json={"reason": "timeout", "error": "boom"})

    assert r.status_code == 200, r.text
    row = await _analysis_row(session, file_id)
    assert row is not None, "ON CONFLICT DO UPDATE must INSERT a fresh analysis row when none exists (D-06)"
    assert row.failed_at is not None, "failed_at must be stamped"
    assert row.error_message == "timeout: boom", "error_message is the composed '<reason>: <error>' (D-07)"
    assert row.analysis_completed_at is None, "a failed row keeps analysis_completed_at NULL (XOR CHECK)"
    # Phase 90 (D-09): the files.state = ANALYSIS_FAILED dual-write was removed; failed_clause derives from failed_at.
    assert not await _ledger_present(session, file_id), "the process_file ledger row is cleared in the same transaction"
    assert await _no_mixed_row_exists(session)


@pytest.mark.asyncio
async def test_report_failed_error_message_bodyless_error(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """An omitted ``error`` still composes a bounded marker string (error defaults to None on the wire)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.post(f"/api/internal/agent/analysis/{file_id}/failed", json={"reason": "crashed"})

    assert r.status_code == 200, r.text
    row = await _analysis_row(session, file_id)
    assert row is not None
    assert row.error_message == "crashed: None", "error defaults to None -> composed marker records the reason"
    assert row.failed_at is not None
    assert row.analysis_completed_at is None


# ---------------------------------------------------------------------------
# (b) failure on a previously-COMPLETED file is a guarded no-op (phaze-ts1d)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_failed_after_success_is_guarded_and_leaves_completed_row_untouched(
    seed_test_agent: tuple[Agent, str], session: AsyncSession
) -> None:
    """phaze-ts1d: a failure callback on an already-COMPLETED row must NOT regress it to FAILED.

    ``deepen_analysis`` deliberately re-enqueues ``process_file`` for an already-analyzed file at an
    unbounded budget; a timeout on that re-run reaches this endpoint for a row that already reads DONE.
    Before the fix, the unconditional ``ON CONFLICT DO UPDATE`` nulled ``analysis_completed_at`` and
    stamped ``failed_at`` here -- silently and PERMANENTLY regressing the file (analyze failures are
    terminal / non-auto-retryable), even though the good bpm/key/mood data was never touched. The
    conflict predicate (``WHERE analysis_completed_at IS NULL``) now makes this a benign no-op: the
    completed row -- including its payload columns -- is left byte-for-byte untouched, exactly like
    ``report_metadata_failed``'s WR-01 guard.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    await _seed_ledger(session, file_id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        # First a real analysis success -> analysis_completed_at set, state ANALYZED.
        w = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={"bpm": 128.0})
        assert w.status_code == 200, w.text

    pre = await _analysis_row(session, file_id)
    assert pre is not None and pre.analysis_completed_at is not None and pre.failed_at is None, "precondition: a done row"
    completed_at_before = pre.analysis_completed_at

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.post(f"/api/internal/agent/analysis/{file_id}/failed", json={"reason": "timeout", "error": "unbounded deepen timeout"})

    assert r.status_code == 200, r.text
    row = await _analysis_row(session, file_id)
    assert row is not None
    assert row.analysis_completed_at == completed_at_before, "a completed row's analysis_completed_at must survive a deepen-timeout callback"
    assert row.failed_at is None, "the guard must skip the DO UPDATE entirely -- no failed_at stamp on a done row"
    assert row.error_message is None, "the guard must not touch error_message on a done row either"
    assert row.bpm == 128.0, "the good analysis payload must be untouched"
    # The run still terminated, so the ledger clear (and staged-object delete / awaiting reap) must
    # remain UNCONDITIONAL even though the marker write was skipped (fix hint's requirement).
    assert not await _ledger_present(session, file_id), "the ledger clear must still fire even when the CAS guard skips the marker"
    assert await _no_mixed_row_exists(session)


@pytest.mark.asyncio
async def test_report_failed_on_not_yet_completed_row_still_stamps_marker(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """The guard only protects a COMPLETED row -- a second failure on an already-failed row still refreshes.

    Regression guard against an overly-broad predicate: an already-failed row (analysis_completed_at
    IS NULL) must still take the new failed_at/error_message on a repeat failure.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        first = await ac.post(f"/api/internal/agent/analysis/{file_id}/failed", json={"reason": "timeout", "error": "first crash"})
        assert first.status_code == 200, first.text

    pre = await _analysis_row(session, file_id)
    assert pre is not None and pre.analysis_completed_at is None and pre.error_message == "timeout: first crash"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        second = await ac.post(f"/api/internal/agent/analysis/{file_id}/failed", json={"reason": "crashed", "error": "second crash"})
        assert second.status_code == 200, second.text

    row = await _analysis_row(session, file_id)
    assert row is not None
    assert row.failed_at is not None
    assert row.error_message == "crashed: second crash", "a not-yet-completed row must still refresh the marker on repeat failure"
    assert row.analysis_completed_at is None
    assert await _no_mixed_row_exists(session)


@pytest.mark.asyncio
async def test_report_failed_repeat_bumps_updated_at_not_created_at(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """phaze-7634: a repeat failure marker refresh bumps updated_at; created_at stays pinned.

    Same defect class as phaze-c8nz on the CAS-guarded ``report_analysis_failed`` upsert: the
    `set_` clause used to omit `updated_at`, and `TimestampMixin.updated_at`'s ORM `onupdate`
    hook never fires for a Core upsert. This exercises the not-yet-completed refresh branch
    (the `where=analysis_completed_at IS NULL` predicate lets the DO UPDATE fire) -- the CAS
    predicate itself is untouched by the fix, only which columns the guarded UPDATE writes.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        first = await ac.post(f"/api/internal/agent/analysis/{file_id}/failed", json={"reason": "timeout", "error": "first crash"})
        assert first.status_code == 200, first.text

    # Backdate created_at/updated_at directly (bypassing the ORM/onupdate hook) to a fixed point
    # well in the past. analysis.created_at/updated_at are TIMESTAMP WITHOUT TIME ZONE columns --
    # use a naive UTC value so asyncpg doesn't reject the aware/naive mismatch.
    outage_time = datetime.now(UTC).replace(microsecond=0, tzinfo=None) - timedelta(hours=12)
    await session.execute(update(AnalysisResult).where(AnalysisResult.file_id == file_id).values(created_at=outage_time, updated_at=outage_time))
    await session.commit()

    before_repeat = datetime.now(UTC).replace(tzinfo=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        second = await ac.post(f"/api/internal/agent/analysis/{file_id}/failed", json={"reason": "crashed", "error": "second crash"})
        assert second.status_code == 200, second.text

    row = await _analysis_row(session, file_id)
    assert row is not None
    assert row.error_message == "crashed: second crash"
    assert row.created_at == outage_time, "created_at must stay pinned to the first-write value"
    assert row.updated_at > outage_time, "updated_at must move forward off the stale outage-window value"
    assert row.updated_at >= before_repeat - timedelta(seconds=5), (
        "updated_at must reflect the server clock at conflict-resolution time, not the stale backdated value"
    )


# ---------------------------------------------------------------------------
# (c) a success after a failure clears the marker and stamps completed_at (D-13)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_after_failure_clears_marker(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """put_analysis after a failure clears failed_at/error_message and stamps analysis_completed_at (D-13).

    Without the unconditional clear, a successful (re)analysis would leave the stale failed_at set --
    the row would read FAILED forever AND the completion branch's analysis_completed_at stamp would
    violate the XOR CHECK. This proves the marker is wiped and the row converges to done.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        # Fail first -> failed_at set, analysis_completed_at NULL.
        f = await ac.post(f"/api/internal/agent/analysis/{file_id}/failed", json={"reason": "timeout", "error": "boom"})
        assert f.status_code == 200, f.text

    failed = await _analysis_row(session, file_id)
    assert failed is not None and failed.failed_at is not None and failed.analysis_completed_at is None, "precondition: a failed row"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        # A real success clears the marker and stamps completion.
        w = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={"bpm": 130.0})
        assert w.status_code == 200, w.text

    row = await _analysis_row(session, file_id)
    assert row is not None
    assert row.failed_at is None, "a real success clears failed_at (D-13)"
    assert row.error_message is None, "a real success clears error_message (D-13)"
    assert row.analysis_completed_at is not None, "the completion branch stamps analysis_completed_at"
    assert row.bpm == 130.0
    # Phase 90 (D-09): 'analyzed' derives from analysis_completed_at, not the removed files.state write.
    assert await _no_mixed_row_exists(session)


@pytest.mark.asyncio
async def test_report_failed_nul_in_error_persists_and_clears_ledger(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """T-81-05-03 (PG-invalid limb): a NUL-bearing ``error`` must NOT abort the transaction.

    NUL passes the wire (``max_length`` bounds length only; lone surrogates are separately rejected by
    pydantic-core as ``string_unicode``), then Postgres rejects the write with
    ``CharacterNotInRepertoireError``. Because the marker upsert, the ``files.state`` write and the
    ledger clear all share one transaction, the rollback strands the ledger row and the file
    re-analyzes into the identical failure forever. ``sanitize_pg_text`` strips NUL before persist.
    """
    nul_error = "bad" + chr(0) + "frame"
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    await _seed_ledger(session, file_id)
    assert await _ledger_present(session, file_id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.post(f"/api/internal/agent/analysis/{file_id}/failed", json={"reason": "crashed", "error": nul_error})

    assert r.status_code == 200, r.text
    row = await _analysis_row(session, file_id)
    assert row is not None
    assert row.failed_at is not None
    assert row.error_message is not None
    assert chr(0) not in row.error_message, "NUL must be stripped before persist"
    assert row.error_message == "crashed: badframe"
    # Phase 90 (D-09): failed status derives from analysis.failed_at, not the removed files.state write.
    # The whole point: the transaction survived, so the ledger clear committed.
    assert not await _ledger_present(session, file_id), "a NUL-bearing error must not strand the ledger row"
    assert await _no_mixed_row_exists(session)


@pytest.mark.asyncio
async def test_report_failed_oversized_error_rejected_and_no_row_persisted(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """T-81-05-03 (oversized limb): a 2001-char ``error`` -> 422 at the wire; NO analysis row is persisted.

    ``AnalysisFailurePayload.error`` bounds free text with ``max_length=2000`` (T-81-05-03's oversized
    limb, the DoS-via-huge-string threat -- same remediation as T-81-03-04). One char over the bound
    must never reach the handler, so the rejected request must leave no trace: no ``analysis`` row for
    this file (the precondition here has none), and no ``files.state`` flip to ``ANALYSIS_FAILED``.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    assert await _analysis_row(session, file_id) is None, "precondition: no prior analysis row"

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.post(f"/api/internal/agent/analysis/{file_id}/failed", json={"reason": "error", "error": "x" * 2001})

    assert r.status_code == 422, r.text
    errors = r.json()["detail"]
    assert any(e.get("type") == "string_too_long" and list(e.get("loc")) == ["body", "error"] for e in errors), errors

    row = await _analysis_row(session, file_id)
    assert row is None, "a rejected (422) failure POST must not persist an analysis failure row"


@pytest.mark.asyncio
async def test_report_failed_error_at_max_length_boundary_is_accepted(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """T-81-05-03 boundary: a 2000-char ``error`` (exactly at ``max_length``) IS accepted -> 200, row persisted.

    Regression guard against someone "fixing" the bound by lowering it below 2000: this asserts the
    boundary is exact, not merely that 2001 is rejected.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.post(f"/api/internal/agent/analysis/{file_id}/failed", json={"reason": "error", "error": "x" * 2000})

    assert r.status_code == 200, r.text
    row = await _analysis_row(session, file_id)
    assert row is not None, "an accepted (2000-char) failure POST must persist the marker"
    assert row.failed_at is not None
    assert row.error_message is not None
    assert row.error_message.startswith("error: "), f"error_message must be composed as '<reason>: <error>', got {row.error_message!r}"
    # Phase 90 (D-09): failed status derives from analysis.failed_at, not the removed files.state write.
