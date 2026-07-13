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

from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.services.proposal import (
    BatchProposalResponse,
    FileProposalResponse,
    store_proposals,
)
from phaze.services.stage_status import is_applied


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_file(session: AsyncSession) -> uuid.UUID:
    """Insert a minimal FileRecord and return its id."""
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            agent_id="test-fileserver",
            id=file_id,
            sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            original_path=f"/music/{uuid.uuid4().hex}/orig.mp3",
            original_filename="orig.mp3",
            current_path="/music/orig.mp3",
            file_type="music",
            file_size=1_000_000,
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


@pytest.mark.asyncio
async def test_out_of_range_file_index_is_skipped(session: AsyncSession) -> None:
    """WR-01: an out-of-range or negative LLM file_index is skipped — no crash, no wrong-file write.

    ``file_index`` is an unbounded int from the LLM. ``5`` (>= batch size) would crash the batch
    with IndexError; ``-1`` would silently wrap and write the proposal against the wrong file.
    Both must be skipped, leaving zero rows for the only real file.
    """
    file_id = await _seed_file(session)
    batch = BatchProposalResponse(
        proposals=[
            FileProposalResponse(file_index=5, proposed_filename="oob.mp3", proposed_path=None, confidence=0.9, reasoning="oob"),
            FileProposalResponse(file_index=-1, proposed_filename="neg.mp3", proposed_path=None, confidence=0.9, reasoning="neg"),
        ]
    )

    count = await store_proposals(session, [str(file_id)], batch, [{"ctx": 0}])
    await session.commit()

    assert count == 0
    assert await _count(session, file_id, ProposalStatus.PENDING) == 0


@pytest.mark.asyncio
async def test_stale_batch_does_not_disturb_executed_file(session: AsyncSession) -> None:
    """D-03: a stale store_proposals batch on an already-applied file leaves the executed proposal
    row untouched and ``is_applied()`` True (the MOVED-regression bug is gone).

    Authority for "this file has been applied" now lives entirely in ``proposals.status``; the
    ``file.state`` mirror that the ``_TERMINAL_FILE_STATES`` frozenset guarded (and whose omission
    of MOVED/UNCHANGED was the bug) is deleted. A stale/duplicated batch re-running
    ``store_proposals`` must not disturb the executed proposal — proven by an independent read after
    commit (the conftest override reads uncommitted rows — ``project_get_session_never_commits``).
    """
    file_id = await _seed_file(session)
    executed_id = uuid.uuid4()
    session.add(
        RenameProposal(
            id=executed_id,
            file_id=file_id,
            proposed_filename="done.mp3",
            proposed_path="done/path",
            confidence=1.0,
            status=ProposalStatus.EXECUTED,
            context_used={},
            reason="applied",
        )
    )
    await session.commit()

    await store_proposals(session, [str(file_id)], _batch("regen.mp3", reasoning="regen"), [{"ctx": "regen"}])
    await session.commit()

    # The executed proposal row is untouched, and is_applied() (reads proposals, never file.state) stays True.
    executed = (await session.execute(select(RenameProposal).where(RenameProposal.id == executed_id))).scalar_one()
    assert executed.status == ProposalStatus.EXECUTED
    assert await is_applied(session, file_id) is True
