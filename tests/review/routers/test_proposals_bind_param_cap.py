"""phaze-sco9: the asyncpg 32767-bind-parameter cap sweep for proposals.py's OOB row refresh.

``bulk_approve_high_confidence`` (REVIEW-02) hydrates the v7 OOB row fragments for every id
``approve_pending_above_confidence`` RETURNs as just-approved. D-04 keeps one PENDING proposal per
file and the product is designed for a ~200K-file archive, so on a large batch that id list can
organically exceed asyncpg's 32767-bind-parameter cap. A bare ``RenameProposal.id.in_(ids)`` would
expand to one bind parameter PER id and raise at the driver before the row-hydration SELECT ever
ran -- the approving UPDATE has already committed by then (phaze-r7j9's sibling defer, phaze-sco9),
so the blast radius is a 500 on the OOB refresh response alone; a re-click is a clean no-op.

The fix reuses the repo's established idiom (``tasks/reenqueue.py::_fids_scope``,
``routers/pipeline.py::_analysis_file_ids_scope`` / ``_ledger_keys_scope``, phaze-r7j9): bind the
WHOLE id list as ONE ``uuid[]`` Postgres array parameter via ``routers.proposals._proposal_ids_scope``,
which sidesteps the per-element cap regardless of list size. This test exercises the real asyncpg
driver with a list comfortably past the 32767-parameter break, proving the statement can be bound
and executed -- the exact wire-level step the original bug crashed at, before any row match happens.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import select

from phaze.models.proposal import RenameProposal
from phaze.routers.proposals import _proposal_ids_scope


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.integration

# Comfortably past asyncpg's 32767-parameter int16 wire cap -- an unchunked `.in_(ids)` SELECT over
# this many uuid binds raises `InterfaceError: the number of query arguments cannot exceed 32767`
# before the statement is even sent. This mirrors the size the phaze-r7j9 sweep settled on.
_BIND_LIMIT_BREAK_IDS = 40_000


@pytest.mark.asyncio
async def test_proposal_ids_scope_survives_past_bind_param_cap(session: AsyncSession) -> None:
    """An id list past the bind-parameter cap must not raise (phaze-sco9 regression).

    No matching rows exist in the empty test schema for these random ids -- the point of the test
    is that the statement is BOUND and EXECUTED at all (the driver-level step the pre-fix `.in_()`
    would crash at), not that it matches anything.
    """
    proposal_ids = [uuid.uuid4() for _ in range(_BIND_LIMIT_BREAK_IDS)]

    result = await session.execute(select(RenameProposal.id).where(_proposal_ids_scope(proposal_ids, "ids")))

    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_proposal_ids_scope_matches_only_the_bound_ids(session: AsyncSession) -> None:
    """Functional sanity alongside the scale test: the array-bind ``= ANY`` selects exactly the
    rows whose ids were passed, same as a (small-scale) ``.in_(...)`` would -- the fix changes the
    wire encoding, not the query semantics."""
    from tests.review.routers.test_proposals import create_test_proposal

    matched = await create_test_proposal(session)
    await create_test_proposal(session)

    result = await session.execute(select(RenameProposal.id).where(_proposal_ids_scope([matched.id], "ids")))
    assert result.scalars().all() == [matched.id]
