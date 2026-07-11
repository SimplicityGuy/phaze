"""Phase 87 (87-06, UI-03 / D-06/D-07): the per-file eligibility trace endpoint.

``GET /pipeline/files/{file_id}/trace/{stage}`` is the diagnostic whose absence hid the deadlock: for
ONE file it evaluates ``resolve_status`` + the REAL ``eligible()`` conjuncts in Python (a single-row
read, NEVER a corpus scan, T-87-23) and names the single unmet blocker keeping a stage out of the
pending set.

Behavior 10 coverage:
- A downstream stage blocked on an unfinished upstream names that upstream ("metadata not done").
- The trace is a single-row evaluation: every emitted SELECT is file_id-scoped, and no whole-corpus
  COUNT/scan is issued.
- An enrich stage renders ``upstream met?`` vacuously satisfied (no upstream).
- The trace verdict is the REAL ``eligible()`` — the scheduler's source of truth. Under the OQ-1
  SCOPE-MINIMAL resolution a force-skipped enrich upstream does NOT unblock its downstream (deferred to
  Phase 90), so a SKIPPED upstream is rendered as still-gating (an HONEST blocker) rather than
  "satisfied" — a lenient display would make the trace claim a downstream is eligible when the
  scheduler permanently gates it (the exact deadlock UI-03 exists to expose).
- An unknown stage degrades to "Trace unavailable this tick." (a poll never 500s).

Must pass in the ``shared`` bucket in isolation (consumes the DB fixtures -> auto-marked integration).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import event

from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord, FileState
from phaze.models.stage_skip import StageSkip


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


async def _seed_file(session: AsyncSession) -> uuid.UUID:
    """Seed a committed FileRecord (FK anchor for the per-stage marker rows)."""
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            id=file_id,
            sha256_hash=f"{uuid.uuid4().hex}{uuid.uuid4().hex}",
            original_path=f"/test/music/{file_id}.mp3",
            original_filename=f"{file_id}.mp3",
            current_path=f"/test/music/{file_id}.mp3",
            file_type="mp3",
            file_size=1024,
            state=FileState.DISCOVERED,
        )
    )
    await session.commit()
    return file_id


async def _mark_analyze_done(session: AsyncSession, file_id: uuid.UUID) -> None:
    """analyze DONE requires analysis_completed_at NOT NULL (DERIV-03)."""
    session.add(AnalysisResult(file_id=file_id, analysis_completed_at=datetime.now(UTC)))
    await session.commit()


@pytest.mark.asyncio
async def test_downstream_trace_names_unfinished_upstream_blocker(client: AsyncClient, session: AsyncSession) -> None:
    """propose is blocked on metadata (not done) even with analyze done -> the trace names it (behavior 10)."""
    file_id = await _seed_file(session)
    await _mark_analyze_done(session, file_id)  # analyze done, metadata untouched (not_started)

    response = await client.get(f"/pipeline/files/{file_id}/trace/propose")

    assert response.status_code == 200
    body = response.text
    assert "Prop — NOT eligible" in body
    assert "metadata not done" in body
    assert "← blocker" in body


@pytest.mark.asyncio
async def test_trace_is_single_row_no_corpus_scan(client: AsyncClient, session: AsyncSession, async_engine: AsyncEngine) -> None:
    """Every emitted SELECT is file_id-scoped (WHERE) and no COUNT/whole-corpus scan is issued (T-87-23)."""
    file_id = await _seed_file(session)
    await _mark_analyze_done(session, file_id)

    captured: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany) -> None:  # type: ignore[no-untyped-def]
        captured.append(statement)

    event.listen(async_engine.sync_engine, "before_cursor_execute", _capture)
    try:
        response = await client.get(f"/pipeline/files/{file_id}/trace/propose")
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", _capture)

    assert response.status_code == 200
    selects = [s for s in captured if s.lstrip().lower().startswith("select")]
    assert selects, f"no SELECT captured; saw: {captured}"
    # Single-row discipline: every read is file_id-scoped (a WHERE clause) and none aggregates the corpus.
    for stmt in selects:
        low = stmt.lower()
        assert "where" in low, f"unscoped SELECT (no WHERE -> potential corpus scan): {stmt}"
        assert "count(" not in low, f"corpus COUNT issued by the trace: {stmt}"


@pytest.mark.asyncio
async def test_enrich_stage_upstream_is_vacuously_met(client: AsyncClient, session: AsyncSession) -> None:
    """An enrich stage (no upstream) renders eligible with ``upstream met?`` vacuously satisfied."""
    file_id = await _seed_file(session)  # fresh: metadata not_started -> eligible

    response = await client.get(f"/pipeline/files/{file_id}/trace/metadata")

    assert response.status_code == 200
    body = response.text
    assert "Meta — eligible (in the pending set)" in body
    assert "no upstream (enrich stage)" in body


@pytest.mark.asyncio
async def test_skipped_upstream_still_gates_downstream(client: AsyncClient, session: AsyncSession) -> None:
    """A force-skipped metadata upstream does NOT unblock propose (OQ-1 scope-minimal) -- rendered honestly.

    This is the DEVIATION from the plan's "a skipped upstream renders as satisfied" note: under the
    RESOLVED scope-minimal semantics ``eligible()`` strictly requires the upstream DONE, so a SKIPPED
    metadata keeps propose gated. The trace reflects the scheduler's real behavior rather than claiming
    an eligibility the scheduler will never grant.
    """
    file_id = await _seed_file(session)
    await _mark_analyze_done(session, file_id)
    session.add(StageSkip(file_id=file_id, stage="metadata", reason="corrupt tags on this file"))
    await session.commit()

    response = await client.get(f"/pipeline/files/{file_id}/trace/propose")

    assert response.status_code == 200
    body = response.text
    assert "Prop — NOT eligible" in body
    assert "metadata skipped — downstream stays gated" in body
    assert "← blocker" in body


@pytest.mark.asyncio
async def test_unknown_stage_degrades_safely(client: AsyncClient, session: AsyncSession) -> None:
    """An unknown stage renders the degrade line, never a 500 (a poll must never crash)."""
    file_id = await _seed_file(session)

    response = await client.get(f"/pipeline/files/{file_id}/trace/bogus")

    assert response.status_code == 200
    assert "Trace unavailable this tick." in response.text
