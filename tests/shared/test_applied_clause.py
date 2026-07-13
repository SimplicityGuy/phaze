"""SC#1 unit contract for the ``applied()`` predicate pair (Phase 85, READ-05 / D-01).

Bucket: ``shared`` (path segment immediately under ``tests/``) -- ``stage_status`` is a shared
predicate module. These are DB-backed contract tests (the ``session`` / ``make_file`` fixtures are
auto-marked ``integration`` by ``conftest``): ``applied_clause()`` and ``is_applied()`` are exercised
against a real Postgres round-trip because their whole point is what they read (``proposals.status``)
and, load-bearingly, what they NEVER read (``files.state``).

D-01: a file is ``applied`` iff an ``executed`` proposal exists for it. This must hold:
  * executed proposal        -> applied (True)
  * failed / approved / pending / no-proposal -> NOT applied (False)
  * BOTH a failed AND an executed proposal for the same file -> applied (True) (multi-proposal)
  * CRITICAL: a file whose ``state`` is deliberately ``'moved'`` (NOT ``'executed'``) but which has an
    executed proposal is STILL applied -- proving the predicate never reads ``files.state`` (the whole
    reason READ-05's ``state == EXECUTED`` gates were dead; no writer in ``src/`` produces that value).

Both forms are exercised: the SQL-fragment ``applied_clause()`` via a
``select(FileRecord.id).where(applied_clause())`` query, and the per-record ``is_applied()`` twin.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from sqlalchemy import select

from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.services.stage_status import applied_clause, is_applied


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession


async def _add_proposal(session: AsyncSession, file_id: uuid.UUID, status: str) -> None:
    """Insert one ``RenameProposal`` with ``status`` for ``file_id`` and commit."""
    session.add(
        RenameProposal(
            id=uuid.uuid4(),
            file_id=file_id,
            proposed_filename="Renamed Set.mp3",
            proposed_path=None,
            confidence=0.9,
            status=status,
        )
    )
    await session.commit()


async def _clause_selects(session: AsyncSession, file_id: uuid.UUID) -> bool:
    """True iff ``applied_clause()`` selects ``file_id`` in a correlated ``FileRecord`` query."""
    stmt = select(FileRecord.id).where(FileRecord.id == file_id, applied_clause())
    return await session.scalar(stmt) is not None


# ---------------------------------------------------------------------------------------------------
# executed -> applied (both forms agree)
# ---------------------------------------------------------------------------------------------------
async def test_executed_proposal_is_applied(session: AsyncSession, make_file: Callable[..., Awaitable[FileRecord]]) -> None:
    file = await make_file()
    await _add_proposal(session, file.id, ProposalStatus.EXECUTED.value)

    assert await _clause_selects(session, file.id) is True
    assert await is_applied(session, file.id) is True


# ---------------------------------------------------------------------------------------------------
# non-executed statuses -> NOT applied
# ---------------------------------------------------------------------------------------------------
async def test_failed_proposal_is_not_applied(session: AsyncSession, make_file: Callable[..., Awaitable[FileRecord]]) -> None:
    file = await make_file()
    await _add_proposal(session, file.id, ProposalStatus.FAILED.value)

    assert await _clause_selects(session, file.id) is False
    assert await is_applied(session, file.id) is False


async def test_approved_proposal_is_not_applied(session: AsyncSession, make_file: Callable[..., Awaitable[FileRecord]]) -> None:
    file = await make_file()
    await _add_proposal(session, file.id, ProposalStatus.APPROVED.value)

    assert await _clause_selects(session, file.id) is False
    assert await is_applied(session, file.id) is False


async def test_pending_proposal_is_not_applied(session: AsyncSession, make_file: Callable[..., Awaitable[FileRecord]]) -> None:
    file = await make_file()
    await _add_proposal(session, file.id, ProposalStatus.PENDING.value)

    assert await _clause_selects(session, file.id) is False
    assert await is_applied(session, file.id) is False


async def test_no_proposal_is_not_applied(session: AsyncSession, make_file: Callable[..., Awaitable[FileRecord]]) -> None:
    file = await make_file()

    assert await _clause_selects(session, file.id) is False
    assert await is_applied(session, file.id) is False


# ---------------------------------------------------------------------------------------------------
# multi-proposal: a file with BOTH a failed and an executed proposal is applied (D-01)
# ---------------------------------------------------------------------------------------------------
async def test_failed_and_executed_proposals_is_applied(session: AsyncSession, make_file: Callable[..., Awaitable[FileRecord]]) -> None:
    file = await make_file()
    await _add_proposal(session, file.id, ProposalStatus.FAILED.value)
    await _add_proposal(session, file.id, ProposalStatus.EXECUTED.value)

    assert await _clause_selects(session, file.id) is True
    assert await is_applied(session, file.id) is True


# ---------------------------------------------------------------------------------------------------
# LOAD-BEARING (SC#1): the predicate is independent of files.state.
# An executed proposal on a file whose state is NOT 'executed' is still applied -- this is the whole
# reason the phase exists (no src/ writer produced the EXECUTED scalar state; the apply path uses proposals.status).
# ---------------------------------------------------------------------------------------------------
async def test_applied_never_reads_file_state(session: AsyncSession, make_file: Callable[..., Awaitable[FileRecord]]) -> None:
    file = await make_file()  # deliberately NOT 'executed'
    await _add_proposal(session, file.id, ProposalStatus.EXECUTED.value)

    # Applied purely because an executed proposal exists -- file.state is irrelevant.
    assert await _clause_selects(session, file.id) is True
    assert await is_applied(session, file.id) is True
