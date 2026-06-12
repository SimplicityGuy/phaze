"""Integration tests for store_proposals idempotency (Phase 35, D-04).

Exercises the real partial-index upsert against a live Postgres ``session``
fixture (which builds the schema from model metadata, so the
``uq_proposals_file_id_pending`` partial unique index added to
``RenameProposal.__table_args__`` is present here too).

Proves the D-04 contract:
  1. Calling store_proposals twice for the same file yields EXACTLY ONE pending
     row, holding the SECOND call's content (overwrite-in-place, not append).
  2. An APPROVED proposal for the same file is NEVER touched by a re-run, and no
     second pending row appears alongside it (human approvals structurally
     protected by the partial index's ``WHERE status = 'pending'`` predicate).
  3. The explicit PK stamp makes a fresh INSERT succeed (no NotNullViolation),
     since pg_insert bypasses RenameProposal.id's Python-side default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import func, select

from phaze.models.file import FileRecord, FileState
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.services.proposal import (
    BatchProposalResponse,
    FileProposalResponse,
    store_proposals,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_file(session: AsyncSession) -> uuid.UUID:
    """Insert a minimal FileRecord and return its id."""
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            id=file_id,
            sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            original_path=f"/music/{uuid.uuid4().hex}/orig.mp3",
            original_filename="orig.mp3",
            current_path="/music/orig.mp3",
            file_type="music",
            file_size=1_000_000,
            state=FileState.ANALYZED,
        )
    )
    await session.flush()
    return file_id


def _batch(filename: str, *, confidence: float = 0.9, reasoning: str = "r", path: str | None = None) -> BatchProposalResponse:
    """Build a single-file BatchProposalResponse."""
    return BatchProposalResponse(
        proposals=[
            FileProposalResponse(
                file_index=0,
                proposed_filename=filename,
                proposed_path=path,
                confidence=confidence,
                reasoning=reasoning,
            )
        ]
    )


async def _count(session: AsyncSession, file_id: uuid.UUID, status: str) -> int:
    result = await session.execute(select(func.count(RenameProposal.id)).where(RenameProposal.file_id == file_id, RenameProposal.status == status))
    return int(result.scalar_one())


@pytest.mark.asyncio
async def test_double_run_overwrites_single_pending_row(session: AsyncSession) -> None:
    """Two store_proposals calls for one file leave exactly one pending row holding the second call's content."""
    file_id = await _seed_file(session)

    count1 = await store_proposals(session, [str(file_id)], _batch("first.mp3", confidence=0.5, reasoning="first"), [{"ctx": 1}])
    await session.commit()
    assert count1 == 1

    count2 = await store_proposals(session, [str(file_id)], _batch("second.mp3", confidence=0.8, reasoning="second"), [{"ctx": 2}])
    await session.commit()
    assert count2 == 1

    # Exactly one pending row remains for the file.
    assert await _count(session, file_id, ProposalStatus.PENDING) == 1

    # It holds the SECOND call's content (overwrite-in-place).
    result = await session.execute(select(RenameProposal).where(RenameProposal.file_id == file_id, RenameProposal.status == ProposalStatus.PENDING))
    row = result.scalar_one()
    assert row.proposed_filename == "second.mp3"
    assert row.confidence == pytest.approx(0.8)
    assert row.reason == "second"
    assert row.context_used["input_context"] == {"ctx": 2}


@pytest.mark.asyncio
async def test_rerun_never_touches_approved_row(session: AsyncSession) -> None:
    """An APPROVED proposal survives a re-run untouched; the re-run adds exactly one pending row beside it."""
    file_id = await _seed_file(session)

    approved_id = uuid.uuid4()
    session.add(
        RenameProposal(
            id=approved_id,
            file_id=file_id,
            proposed_filename="human-approved.mp3",
            proposed_path="approved/path",
            confidence=1.0,
            status=ProposalStatus.APPROVED,
            context_used={"human": True},
            reason="approved by human",
        )
    )
    await session.commit()

    # Re-run proposal generation for the same file.
    await store_proposals(session, [str(file_id)], _batch("regenerated.mp3", reasoning="regen"), [{"ctx": "regen"}])
    await session.commit()

    # The APPROVED row is byte-for-byte untouched.
    approved = (await session.execute(select(RenameProposal).where(RenameProposal.id == approved_id))).scalar_one()
    assert approved.status == ProposalStatus.APPROVED
    assert approved.proposed_filename == "human-approved.mp3"
    assert approved.reason == "approved by human"

    # Exactly one approved + one new pending row coexist for the file.
    assert await _count(session, file_id, ProposalStatus.APPROVED) == 1
    assert await _count(session, file_id, ProposalStatus.PENDING) == 1
    pending = (
        await session.execute(select(RenameProposal).where(RenameProposal.file_id == file_id, RenameProposal.status == ProposalStatus.PENDING))
    ).scalar_one()
    assert pending.proposed_filename == "regenerated.mp3"


@pytest.mark.asyncio
async def test_fresh_insert_stamps_pk(session: AsyncSession) -> None:
    """A first-time store_proposals INSERT succeeds with a stamped PK (no NotNullViolation)."""
    file_id = await _seed_file(session)

    count = await store_proposals(session, [str(file_id)], _batch("fresh.mp3", path="//perf//artist//"), [{"ctx": "fresh"}])
    await session.commit()
    assert count == 1

    row = (await session.execute(select(RenameProposal).where(RenameProposal.file_id == file_id))).scalar_one()
    assert row.id is not None
    assert row.status == ProposalStatus.PENDING
    # Path normalization (strip + collapse) is preserved through the upsert path.
    assert row.proposed_path == "perf/artist"

    # The file's state is advanced to PROPOSAL_GENERATED.
    file_record = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one()
    assert file_record.state == FileState.PROPOSAL_GENERATED
