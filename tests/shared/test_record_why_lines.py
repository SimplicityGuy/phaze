"""phaze-cavai: the record slide-in's per-stage "why" lines — failure reasons and orphan diagnostics.

Before this bead the stored failure reasons (``FileMetadata.error_message`` /
``AnalysisResult.error_message``) were written on failure and surfaced NOWHERE in the UI, and an
orphaned file was visible only as an aggregate count (amber badge / agents matrix). The record
slide-in (``GET /record/{id}`` -> ``record_body.html``) now renders, under the corresponding
Stage-Eligibility row:

* FAILED (metadata / analyze): the stored ``error_message``, or the explicit
  "no error detail recorded" fallback — never a bare red pill with no explanation;
* ORPHANED (the two enrich stages): the scheduling-ledger facts recovery will replay —
  scheduled-when, the timeout/retries replay budget, the re-drive count — plus
  started-vs-never-started evidence for analyze.

Mirrors ``test_record_stage_pills.py``'s composition-level idiom (real endpoint, seeded markers).
``saq_jobs`` is SAQ-owned and absent from ``Base.metadata``, so the orphan tests pin the same
module-controlled minimal table ``tests/analyze/recovery_cloud/orphan_replay/test_orphan_count.py`` documents.

Must pass in the ``shared`` bucket in isolation (consumes the DB fixtures -> auto-marked integration).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import text

from phaze.enums.stage import Stage
from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.pipeline.files import get_file_orphan_details


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_file(session: AsyncSession) -> uuid.UUID:
    """Seed a committed FileRecord (FK anchor for the per-stage marker rows)."""
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            agent_id="test-fileserver",
            id=file_id,
            sha256_hash=f"{uuid.uuid4().hex}{uuid.uuid4().hex}",
            original_path=f"/test/music/{file_id}.mp3",
            original_filename=f"{file_id}.mp3",
            current_path=f"/test/music/{file_id}.mp3",
            file_type="mp3",
            file_size=1024,
        )
    )
    await session.commit()
    return file_id


async def _pin_empty_saq_jobs(session: AsyncSession) -> None:
    """Pin a module-controlled, empty ``saq_jobs`` table so the live-keys probe has a real relation."""
    await session.execute(text("DROP TABLE IF EXISTS saq_jobs"))
    await session.execute(text("CREATE TABLE saq_jobs (key TEXT PRIMARY KEY, status TEXT NOT NULL)"))
    await session.commit()


@pytest.mark.asyncio
async def test_failed_analyze_shows_the_stored_error_message(client: AsyncClient, session: AsyncSession) -> None:
    """A failed analyze row's stored error_message renders under the Analyze stage row."""
    file_id = await _seed_file(session)
    session.add(AnalysisResult(file_id=file_id, failed_at=datetime.now(UTC), error_message="essentia exploded: tensor shape mismatch"))
    await session.commit()

    body = (await client.get(f"/record/{file_id}")).text
    assert "essentia exploded: tensor shape mismatch" in body


@pytest.mark.asyncio
async def test_failed_analyze_without_message_shows_the_explicit_fallback(client: AsyncClient, session: AsyncSession) -> None:
    """A failed analyze with NO stored message says so explicitly — never a bare unexplained red pill."""
    file_id = await _seed_file(session)
    session.add(AnalysisResult(file_id=file_id, failed_at=datetime.now(UTC)))
    await session.commit()

    body = (await client.get(f"/record/{file_id}")).text
    assert "no error detail recorded" in body


@pytest.mark.asyncio
async def test_failed_metadata_shows_the_stored_error_message(client: AsyncClient, session: AsyncSession) -> None:
    """A failed metadata row's stored error_message renders under the Meta stage row."""
    file_id = await _seed_file(session)
    session.add(FileMetadata(file_id=file_id, failed_at=datetime.now(UTC), error_message="mutagen could not parse the header"))
    await session.commit()

    body = (await client.get(f"/record/{file_id}")).text
    assert "mutagen could not parse the header" in body


@pytest.mark.asyncio
async def test_orphaned_analyze_shows_ledger_diagnostics(client: AsyncClient, session: AsyncSession) -> None:
    """An orphaned analyze (ledger row, no live key, no analysis) renders the amber diagnostics line."""
    await _pin_empty_saq_jobs(session)
    file_id = await _seed_file(session)
    session.add(
        SchedulingLedger(
            key=f"process_file:{file_id}",
            function="process_file",
            routing="agent",
            payload={"file_id": str(file_id)},
            timeout=7200,
            retries=2,
        )
    )
    await session.commit()

    body = (await client.get(f"/record/{file_id}")).text
    assert "Orphaned — scheduled" in body
    assert "The analysis never started." in body
    assert "timeout 7200s" in body
    assert "retries 2" in body


@pytest.mark.asyncio
async def test_both_enrich_stages_orphaned_populate_both_diagnostics(session: AsyncSession) -> None:
    """phaze-cmy1e: both enrich stages orphaned on the SAME file -- BOTH loop iterations in
    ``get_file_orphan_details`` run their per-stage ``SchedulingLedger`` SELECT (files.py:223-232),
    a branch neither existing guarding test reached (phaze-o8sie.6, closed REFUTED -- the finding
    was escalated rather than covered by that bead's own tests).

    Calls the service function directly (rather than through the HTML slide-in) so the assertions
    can pin the exact per-stage dict shape and values, not just substring presence in rendered text.
    """
    await _pin_empty_saq_jobs(session)
    file_id = await _seed_file(session)
    session.add_all(
        [
            SchedulingLedger(
                key=f"extract_file_metadata:{file_id}",
                function="extract_file_metadata",
                routing="agent",
                payload={"file_id": str(file_id)},
                timeout=3600,
                retries=1,
            ),
            SchedulingLedger(
                key=f"process_file:{file_id}",
                function="process_file",
                routing="agent",
                payload={"file_id": str(file_id)},
                timeout=7200,
                retries=2,
            ),
        ]
    )
    await session.commit()

    details = await get_file_orphan_details(session, file_id)

    # Every stage in _ORPHAN_DETAIL_STAGES is always a key -- the mapping never collapses one out.
    assert set(details) == {Stage.METADATA.value, Stage.ANALYZE.value}

    metadata_details = details[Stage.METADATA.value]
    assert metadata_details is not None
    assert metadata_details["timeout"] == 3600
    assert metadata_details["retries"] == 1
    assert metadata_details["enqueued_at"] is not None
    assert metadata_details["redrive_attempt"] is None  # never set on this row -- NULL == 0 (never re-driven)

    analyze_details = details[Stage.ANALYZE.value]
    assert analyze_details is not None
    assert analyze_details["timeout"] == 7200
    assert analyze_details["retries"] == 2
    assert analyze_details["enqueued_at"] is not None
    assert analyze_details["redrive_attempt"] is None  # never set on this row -- NULL == 0 (never re-driven)


@pytest.mark.asyncio
async def test_non_orphaned_stage_maps_to_none_alongside_an_orphaned_one(session: AsyncSession) -> None:
    """A stage with no ``SchedulingLedger`` row is not orphaned and maps to ``None`` -- even when the
    OTHER stage on the same file IS orphaned and populated, so the ``None`` default from
    ``get_file_orphan_details``'s initial dict (files.py:213) survives untouched rather than being
    overwritten or dropped. Every stage in ``_ORPHAN_DETAIL_STAGES`` stays a key either way (phaze-cmy1e).
    """
    await _pin_empty_saq_jobs(session)
    file_id = await _seed_file(session)
    session.add(
        SchedulingLedger(
            key=f"process_file:{file_id}",
            function="process_file",
            routing="agent",
            payload={"file_id": str(file_id)},
            timeout=7200,
            retries=2,
        )
    )
    await session.commit()

    details = await get_file_orphan_details(session, file_id)

    assert set(details) == {Stage.METADATA.value, Stage.ANALYZE.value}
    assert details[Stage.METADATA.value] is None
    assert details[Stage.ANALYZE.value] is not None


@pytest.mark.asyncio
async def test_non_orphaned_file_renders_no_orphan_line(client: AsyncClient, session: AsyncSession) -> None:
    """A file with no ledger row renders no orphan diagnostics at all."""
    await _pin_empty_saq_jobs(session)
    file_id = await _seed_file(session)

    body = (await client.get(f"/record/{file_id}")).text
    assert "Orphaned — scheduled" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["nested-entry", "inside-savepoint"])
async def test_orphan_details_fault_degrades_the_record_and_keeps_the_outer_session_usable(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, fault: str
) -> None:
    """A fault in ``get_file_orphan_details`` degrades only its own diagnostics (G0, phaze-47gba).

    The fault is scoped to the ONE orphan-details invocation the real record route makes -- the
    route calls other readers on the same session first, so faulting every ``begin_nested`` would
    mis-target the signal:

    * ``nested-entry``: ``session.begin_nested`` raises at entry. No SAVEPOINT exists yet, so there
      is nothing to roll back; the reader must still return the all-``None`` mapping.
    * ``inside-savepoint``: the first statement that reads ``scheduling_ledger`` is replaced by one Postgres REALLY
      rejects (division by zero) inside the established SAVEPOINT. Every reader the route runs
      AFTER orphan details uses the same session, so a 200 proves the SAVEPOINT rollback left the
      outer transaction usable -- an unrolled-back error would fail them with
      InFailedSqlTransaction.

    Both arms then remove the fault and prove the real diagnostics come back.
    """
    from sqlalchemy import select

    import phaze.routers.record as record_module

    await _pin_empty_saq_jobs(session)
    file_id = await _seed_file(session)
    session.add(
        SchedulingLedger(
            key=f"process_file:{file_id}", function="process_file", routing="agent", payload={"file_id": str(file_id)}, timeout=7200, retries=2
        )
    )
    await session.commit()

    real_details = record_module.get_file_orphan_details
    real_begin_nested = session.begin_nested
    real_execute = session.execute
    results: list[dict[str, dict[str, object] | None]] = []

    def failing_begin_nested(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("forced orphan-details nested-entry failure")

    async def execute_rejecting_ledger_read(statement: object, *args: object, **kwargs: object) -> object:
        if "scheduling_ledger" in str(statement):
            return await real_execute(text("SELECT 1 / 0"))
        return await real_execute(statement, *args, **kwargs)  # type: ignore[arg-type]

    async def faulted_details(target_session: AsyncSession, target_file_id: uuid.UUID) -> dict[str, dict[str, object] | None]:
        assert target_session is session
        if fault == "nested-entry":
            monkeypatch.setattr(session, "begin_nested", failing_begin_nested)
        else:
            monkeypatch.setattr(session, "execute", execute_rejecting_ledger_read)
        try:
            out = await real_details(target_session, target_file_id)
        finally:
            monkeypatch.setattr(session, "begin_nested", real_begin_nested)
            monkeypatch.setattr(session, "execute", real_execute)
        results.append(out)
        return out

    monkeypatch.setattr(record_module, "get_file_orphan_details", faulted_details)
    with caplog.at_level("WARNING", logger="phaze.services.pipeline.files"):
        response = await client.get(f"/record/{file_id}")

    assert results == [{Stage.METADATA.value: None, Stage.ANALYZE.value: None}]
    assert response.status_code == 200, response.text
    assert "Orphaned — scheduled" not in response.text
    degraded = [r for r in caplog.records if "file_orphan_details_degraded" in r.getMessage()]
    assert len(degraded) == 1 and degraded[0].levelname == "WARNING"
    expected = "forced orphan-details nested-entry failure" if fault == "nested-entry" else "division by zero"
    # structlog renders the traceback into the record itself: the injected cause must be in it.
    assert expected in degraded[0].getMessage()

    # Outer state: the committed ledger row is still readable on the SAME session.
    ledger = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == f"process_file:{file_id}"))).scalar_one()
    assert (ledger.timeout, ledger.retries) == (7200, 2)

    # Recovery: with the fault removed, the real reader and record render the diagnostics again.
    monkeypatch.setattr(record_module, "get_file_orphan_details", real_details)
    recovered = await client.get(f"/record/{file_id}")
    assert recovered.status_code == 200
    assert "Orphaned — scheduled" in recovered.text
    assert "timeout 7200s" in recovered.text
    assert "retries 2" in recovered.text
