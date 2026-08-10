"""phaze-mlrwl: bulk_approve_high_confidence bounds its OOB row hydration/render at the 200-row
workspace render cap, regardless of how many proposals the confidence-threshold UPDATE actually
approves.

``approve_pending_above_confidence`` is a server-evaluated predicate with no client id-list
(REVIEW-02) -- D-04 keeps one PENDING proposal per file and the product is designed for a ~200K-file
archive, so on a large batch its ``RETURNING id`` list can organically run into the tens of
thousands (see ``tests/review/routers/test_proposals_bind_param_cap.py``, which exercises the same
scale for the bind-parameter cap this route's row-hydration SELECT sits behind). The rename/move
workspaces that trigger this route render at most 200 rows (``get_pending_proposal_rows``'s
``page_size=200``, phaze-rw14), so hydrating + rendering an OOB fragment for every approved id -- as
opposed to only the first ``_BULK_APPROVE_OOB_ROW_CAP`` -- did unbounded work whose result the
browser mostly discarded (``htmx:oobErrorNoTarget`` for every id not currently on screen).

Seeding tens of thousands of real rows would make this test itself slow in the same way the bug is
slow, so ``approve_pending_above_confidence`` is mocked to RETURN a large synthetic id list (mirroring
the scale a real archive produces) with exactly one id backed by a real, seeded proposal -- placed
either past or within the cap boundary -- so the assertion is on the SAME observable behaviour an
operator would see: whether that row's OOB fragment made it into the response.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch
import uuid

import pytest

from phaze.routers.proposals import _BULK_APPROVE_OOB_ROW_CAP


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.integration

_TARGET = "phaze.routers.proposals.approve_pending_above_confidence"


@pytest.mark.asyncio
async def test_bulk_approve_does_not_render_an_oob_row_past_the_cap(client: AsyncClient, session: AsyncSession) -> None:
    """A real proposal's id placed past the cap boundary must not be hydrated or rendered.

    If the route hydrated the WHOLE ``approved_ids`` list (the pre-fix behaviour), this row's OOB
    fragment would appear in the response no matter where its id sits in the list.
    """
    from tests.review.routers.test_proposals import create_test_proposal

    real = await create_test_proposal(session, confidence=0.95, original_filename="past_cap.mp3")
    filler_ids = [uuid.uuid4() for _ in range(_BULK_APPROVE_OOB_ROW_CAP + 50)]
    synthetic_ids = [*filler_ids, real.id]

    with patch(_TARGET, new=AsyncMock(return_value=synthetic_ids)):
        response = await client.patch(
            "/proposals/bulk-approve-high-confidence",
            headers={"HX-Request": "true", "HX-Target": "rename-trigger-response"},
        )

    assert response.status_code == 200
    # The toast reports the TRUE total -- capping the OOB render must never shrink what the operator
    # is told actually got approved.
    assert f"{len(synthetic_ids)} proposals approved." in response.text
    assert "past_cap.mp3" not in response.text


@pytest.mark.asyncio
async def test_bulk_approve_still_renders_an_oob_row_within_the_cap(client: AsyncClient, session: AsyncSession) -> None:
    """A real proposal's id placed WITHIN the cap boundary must still be hydrated and rendered.

    Proves the cap trims the tail rather than degrading into "never render anything" -- the fix must
    still deliver the OOB rows an operator with a normal-sized (<=200) approved batch relies on.
    """
    from tests.review.routers.test_proposals import create_test_proposal

    real = await create_test_proposal(session, confidence=0.95, original_filename="within_cap.mp3")
    filler_ids = [uuid.uuid4() for _ in range(_BULK_APPROVE_OOB_ROW_CAP + 50)]
    synthetic_ids = [real.id, *filler_ids]

    with patch(_TARGET, new=AsyncMock(return_value=synthetic_ids)):
        response = await client.patch(
            "/proposals/bulk-approve-high-confidence",
            headers={"HX-Request": "true", "HX-Target": "rename-trigger-response"},
        )

    assert response.status_code == 200
    assert "within_cap.mp3" in response.text
    assert 'hx-swap-oob="true"' in response.text
