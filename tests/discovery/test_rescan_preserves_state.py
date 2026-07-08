"""Rescan-wipe regression (Phase 77, MIG-03 / CONTEXT D-08) — bulk_upsert_files site.

Re-scanning an already-advanced file MUST NOT reset its pipeline progress. Today the
``ON CONFLICT DO UPDATE`` ``set_`` dict in ``services/ingestion.py`` carries
``"state": stmt.excluded.state``, so a rescan of an ``ANALYZED`` file re-stamps
``state = DISCOVERED`` (discovery always builds records at ``DISCOVERED``), silently
wiping progress. This test proves the two-part D-08 invariant at the ingestion-service
upsert site: (1) the ``state`` survives the rescan, and (2) the downstream ``analysis``
output row survives.

RED against current (pre-fix) code — the state assertion fails because the ``set_`` clause
overwrites ``analyzed`` with the incoming ``discovered``. GREEN once the ``state`` key is
removed from the ``set_`` dict (new-file INSERT still stamps ``DISCOVERED`` via the VALUES
dict, so newly discovered files are unaffected).

Lives in the ``discovery`` bucket (one bucket per file — the ingestion service is the
module under test). Needs the ephemeral :5433 DB via ``just test-bucket discovery``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import select

from phaze.models.agent import LEGACY_AGENT_ID
from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord, FileState
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.services.ingestion import bulk_upsert_files


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_RESCAN_PATH = "/test/music/rescan/advanced-set.mp3"


def _discovery_record(batch_id: uuid.UUID) -> dict[str, Any]:
    """Build a fresh discovery record for ``_RESCAN_PATH`` (always state=DISCOVERED).

    Mirrors ``discover_and_hash_files``: every discovery pass stamps a NEW ``id`` and
    ``state = DISCOVERED``. ON CONFLICT preserves the existing row's ``id``.
    """
    return {
        "id": uuid.uuid4(),
        "agent_id": LEGACY_AGENT_ID,
        "sha256_hash": "a" * 64,
        "original_path": _RESCAN_PATH,
        "original_filename": "advanced-set.mp3",
        "current_path": _RESCAN_PATH,
        "file_type": "mp3",
        "file_size": 2048,
        "state": FileState.DISCOVERED,
        "batch_id": batch_id,
    }


@pytest.mark.asyncio
async def test_bulk_upsert_rescan_preserves_analyzed_state_and_analysis_row(session: AsyncSession) -> None:
    """Re-upserting an ANALYZED file keeps state=ANALYZED and its analysis row (D-08 / MIG-03)."""
    batch_id = uuid.uuid4()
    session.add(
        ScanBatch(
            id=batch_id,
            agent_id=LEGACY_AGENT_ID,
            scan_path="/test/music/rescan",
            status=ScanStatus.RUNNING,
            total_files=0,
            processed_files=0,
        )
    )
    await session.commit()

    # 1. Initial discovery: the file lands at DISCOVERED.
    await bulk_upsert_files(session, [_discovery_record(batch_id)], batch_size=10)
    file = (await session.execute(select(FileRecord).where(FileRecord.original_path == _RESCAN_PATH))).scalar_one()
    assert file.state == FileState.DISCOVERED  # sanity: a fresh discovery lands at DISCOVERED

    # 2. Advance it all the way to ANALYZED and create its 1:1 analysis output row.
    file.state = FileState.ANALYZED
    session.add(AnalysisResult(id=uuid.uuid4(), file_id=file.id, bpm=128.0, musical_key="Am"))
    await session.commit()

    # 3. Rescan: a fresh discovery pass re-upserts the SAME (agent_id, original_path)
    #    with state=DISCOVERED. The state-wipe bug would reset it here.
    await bulk_upsert_files(session, [_discovery_record(batch_id)], batch_size=10)

    # 4. Invariant (D-08): state survives AND the analysis output row survives.
    session.expire_all()
    reloaded = (await session.execute(select(FileRecord).where(FileRecord.original_path == _RESCAN_PATH))).scalar_one()
    assert reloaded.state == FileState.ANALYZED, "rescan wiped the file's state back to DISCOVERED (MIG-03 regression)"

    analysis = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == reloaded.id))).scalar_one_or_none()
    assert analysis is not None, "the file's analysis output row must survive a rescan"
