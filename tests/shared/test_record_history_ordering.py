"""phaze-rgxg: the ``GET /record/{file_id}`` history / pending-approval orderings carry a unique tiebreaker.

Same shape as phaze-lrwz / phaze-c6j5: a bare ``ORDER BY <non-unique column>`` leaves ties broken by
Postgres heap order (which shifts with page layout, vacuum, and plan choice) instead of a total,
deterministic order. Two orderings in ``routers/record.py`` are fixed here:

- :func:`phaze.routers.record.file_record`'s pending-proposals query (``:95``) -- ``RenameProposal.
  created_at`` -> ``RenameProposal.created_at, RenameProposal.id``.
- the same function's execution-history query (``:116``) -- ``ExecutionLog.executed_at.desc()`` ->
  ``ExecutionLog.executed_at.desc(), ExecutionLog.id.desc()``.

The pending-proposals query is genuinely UNREACHABLE with more than one tied row today:
``uq_proposals_file_id_pending`` (migration 039) is a partial unique index on
``proposals (file_id) WHERE status = 'pending'``, so a single ``file_id`` can never carry two PENDING
rows to tie against each other. The tiebreaker there is still added -- for the same total-order
discipline as every other multi-row query in this router, so a future relaxation of that constraint
doesn't silently reintroduce non-determinism -- but it can only be regression-guarded at the
statement-shape level (see ``test_pending_proposals_query_carries_id_tiebreaker`` below), not with
seeded tied rows.

The execution-history query carries NO such constraint (a file accumulates one ``ExecutionLog`` row
per operation across however many proposals it has had over time), so that one gets the full
seed-tied-rows-and-assert-order regression test the pagination/display-ordering precedent expects.

Must pass in the ``shared`` bucket in isolation (consumes the DB fixtures -> auto-marked integration).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.execution import ExecutionLog, ExecutionStatus
from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


_SRC = Path(__file__).resolve().parents[2] / "src" / "phaze"


def test_pending_proposals_query_carries_id_tiebreaker() -> None:
    """``file_record``'s pending-proposals query orders by ``created_at, RenameProposal.id``.

    ``uq_proposals_file_id_pending`` (see module docstring) makes a real tied-row regression test
    for this specific query impossible: at most one PENDING proposal can ever exist per file, so
    there is never more than one row to order. This is a statement-shape guard, not a behavioral
    one -- it fails if the ``, RenameProposal.id`` tiebreaker is removed, same as it would for any
    other query in this file, without depending on data this schema cannot produce.
    """
    source = (_SRC / "routers" / "record.py").read_text()
    assert re.search(r"\.order_by\(RenameProposal\.created_at,\s*RenameProposal\.id\)", source), (
        "the pending-proposals query must order by created_at, RenameProposal.id (defensive "
        "unique tiebreaker; see uq_proposals_file_id_pending in the module docstring)"
    )


async def _seed_file(session: AsyncSession) -> uuid.UUID:
    """Seed a committed FileRecord (FK anchor for the proposal/execution-log rows)."""
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


async def _seed_executed_proposal(session: AsyncSession, file_id: uuid.UUID, *, proposed_filename: str) -> uuid.UUID:
    """Seed one EXECUTED RenameProposal for ``file_id`` (never PENDING -- see module docstring)."""
    proposal_id = uuid.uuid4()
    session.add(
        RenameProposal(
            id=proposal_id,
            file_id=file_id,
            proposed_filename=proposed_filename,
            status=ProposalStatus.EXECUTED,
        )
    )
    await session.commit()
    return proposal_id


@pytest.mark.asyncio
async def test_execution_history_tiebreaker_orders_tied_executed_at_by_id_desc(session: AsyncSession, client: AsyncClient) -> None:
    """Rows with an IDENTICAL ``executed_at`` come back ordered by ``ExecutionLog.id`` DESC, not heap order.

    Seeds 8 ExecutionLog rows for the SAME file (via 8 distinct EXECUTED proposals -- one
    ExecutionLog per proposal, since ``uq_proposals_file_id_pending`` only restricts PENDING rows,
    not EXECUTED ones), all sharing ONE explicit ``executed_at``, with ids assigned in a SCRAMBLED
    order relative to insertion -- the same no-clock-race, no-insertion-order-luck discipline as
    the lrwz/c6j5 precedent tests. Only the ``ExecutionLog.id`` tiebreaker on
    ``routers.record.file_record`` makes the render order total and deterministic.

    Each row's ``destination_path`` encodes its OWN id so the rendered ``History`` section (which
    has no id attribute, only ``h.detail``) can still recover per-row identity from the HTML.

    Regression guard for phaze-rgxg: reverting the ``, ExecutionLog.id.desc()`` suffix in
    ``routers/record.py`` makes this order depend on Postgres heap layout (verified: this
    assertion fails without the tiebreaker for the scrambled ids below).
    """
    file_id = await _seed_file(session)
    tied_at = datetime(2026, 7, 20, 12, 0, 0)  # naive on purpose (executed_at is TIMESTAMP WITHOUT TZ)

    ids = [uuid.UUID(f"00000000-0000-0000-0000-0000000000{i:02d}") for i in range(8)]
    scrambled = ids[::2] + ids[1::2]  # e.g. [0,2,4,6,1,3,5,7] -- deliberately not insertion==id order

    log_id_to_path = {log_id: f"dest-{log_id}.mp3" for log_id in ids}
    for log_id in scrambled:
        proposal_id = await _seed_executed_proposal(session, file_id, proposed_filename=log_id_to_path[log_id])
        # Force the specific, pre-scrambled log id so the id-DESC expectation below is checkable.
        session.add(
            ExecutionLog(
                id=log_id,
                proposal_id=proposal_id,
                operation="rename",
                source_path="/test/music/before.mp3",
                destination_path=log_id_to_path[log_id],
                sha256_verified=True,
                status=ExecutionStatus.COMPLETED.value,
                executed_at=tied_at,
            )
        )
        await session.commit()

    body = (await client.get(f"/record/{file_id}")).text

    # The History section renders each row's destination_path in a title attribute -- extract the
    # render order of the per-row markers.
    rendered_order = re.findall(r'title="(dest-[0-9a-fA-F-]{36}\.mp3)"', body)
    expected_order = [log_id_to_path[log_id] for log_id in sorted(ids, reverse=True)]

    assert rendered_order == expected_order, (
        f"execution history must render in executed_at DESC, id DESC order on a tie -- got {rendered_order!r}, expected {expected_order!r}"
    )
