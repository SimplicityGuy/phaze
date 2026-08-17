from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING

import pytest

from phaze.models.proposal import ProposalStatus


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.proposal import RenameProposal


@pytest.mark.asyncio
async def test_changes_review_is_the_only_navigation_approval_surface(
    client: AsyncClient,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    proposal = await seed_pending_proposal(
        0.88,
        original_filename="before.mp3",
        proposed_filename="After.mp3",
        proposed_path="Artist/Event",
    )

    body = (await client.get("/s/rename")).text
    assert body.count('data-rail-stage="rename"') == 1
    assert 'data-rail-stage="move"' not in body
    assert 'data-rail-stage="tagwrite"' not in body
    assert "Changes Review" in body
    assert "Filename" in body and "before.mp3" in body and "After.mp3" in body
    assert "Destination" in body and "Artist/Event/After.mp3" in body
    assert "Confidence:</span> 88%" in body
    assert "Below the 90% bulk-approval threshold" in body
    assert f'hx-patch="/proposals/{proposal.id}/approve"' in body

    for alias in ("move", "tagwrite"):
        alias_body = (await client.get(f"/s/{alias}", headers={"HX-Request": "true"})).text
        assert "Changes Review" in alias_body
        assert f'id="rename-row-{proposal.id}"' in alias_body


@pytest.mark.asyncio
async def test_changes_review_filter_and_selection_survive_htmx_and_history_urls(
    client: AsyncClient,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    first = await seed_pending_proposal(0.95, original_filename="first.mp3")
    second = await seed_pending_proposal(0.96, original_filename="second.mp3")
    selected = f"{first.id},{second.id}"

    response = await client.get(
        f"/s/rename?status=needs_review&page=1&page_size=25&sort=confidence&order=asc&selected={selected}",
        headers={"HX-Request": "true", "HX-Target": "changes-workspace-list"},
    )
    body = response.text

    assert body.lstrip().startswith('<div id="changes-workspace-list" class="px-4 py-4 sm:px-6"')
    assert str(first.id) in body and str(second.id) in body
    assert "history.replaceState(history.state" in body
    assert "data-changes-nav" in body
    assert 'hx-push-url="true"' in body
    assert "selected=" in body
    for label in ("All", "Needs Review", "Approved", "Blocked", "Rejected"):
        assert label in body

    second_swap = await client.get(
        "/s/rename?status=approved&page=1&page_size=25",
        headers={"HX-Request": "true", "HX-Target": "changes-workspace-list"},
    )
    assert second_swap.text.lstrip().startswith('<div id="changes-workspace-list"'), "outerHTML responses must retain the next HTMX target"


@pytest.mark.asyncio
async def test_bulk_approval_revalidates_stale_state_and_confidence(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    eligible = await seed_pending_proposal(0.95, original_filename="eligible.mp3")
    low = await seed_pending_proposal(0.89, original_filename="low.mp3")
    null = await seed_pending_proposal(None, original_filename="null.mp3")
    stale = await seed_pending_proposal(0.99, original_filename="stale.mp3")

    rendered = (await client.get("/s/rename?status=needs_review", headers={"HX-Request": "true"})).text
    review_tokens = [html.unescape(token) for token in re.findall(r'<input(?=[^>]*name="review_tokens")(?=[^>]*value="([^"]+)")[^>]*>', rendered)]
    assert len(review_tokens) == 4
    stale.status = ProposalStatus.REJECTED.value
    await session.commit()

    response = await client.patch(
        "/proposals/bulk?status=needs_review&page=1&page_size=25&sort=confidence&order=asc",
        data={
            "action": "approve_eligible",
            "review_tokens": review_tokens,
        },
        headers={"HX-Request": "true", "HX-Target": "changes-workspace-list"},
    )
    assert response.status_code == 200
    assert "1 proposal approved" in response.text
    assert "3 skipped" in response.text

    for proposal in (eligible, low, null, stale):
        await session.refresh(proposal)
    assert eligible.status == ProposalStatus.APPROVED.value
    assert low.status == ProposalStatus.PENDING.value
    assert null.status == ProposalStatus.PENDING.value
    assert stale.status == ProposalStatus.REJECTED.value


@pytest.mark.asyncio
async def test_changes_review_has_narrow_screen_access_and_visible_disabled_explanations(
    client: AsyncClient,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    await seed_pending_proposal(None, original_filename="phone.mp3")
    body = (await client.get("/s/rename", headers={"HX-Request": "true"})).text

    assert "grid-cols-1" in body and "sm:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)]" in body
    assert "overflow-x-auto" in body
    assert "disabled:cursor-not-allowed" in body
    assert "Individual approval only: confidence is below 90% or unavailable." in body
    assert 'title="Already actioned' not in body, "eligibility must not rely on hover-only copy"


@pytest.mark.asyncio
async def test_bulk_approval_rejects_content_regenerated_after_review(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    proposal = await seed_pending_proposal(0.97, original_filename="regenerated.mp3", proposed_filename="Reviewed.mp3")
    rendered = (await client.get("/s/rename?status=needs_review", headers={"HX-Request": "true"})).text
    token = html.unescape(re.findall(r'<input(?=[^>]*name="review_tokens")(?=[^>]*value="([^"]+)")[^>]*>', rendered)[0])
    proposal.proposed_filename = "Regenerated Unseen.mp3"
    await session.commit()

    response = await client.patch(
        "/proposals/bulk?status=needs_review",
        data={"action": "approve_eligible", "review_tokens": [token]},
        headers={"HX-Request": "true", "HX-Target": "changes-workspace-list"},
    )

    await session.refresh(proposal)
    assert proposal.status == ProposalStatus.PENDING.value
    assert "Nothing approved" in response.text


@pytest.mark.asyncio
async def test_pending_destination_collision_is_visible_and_cannot_be_approved(
    client: AsyncClient,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    first = await seed_pending_proposal(0.98, original_filename="collision-a.mp3", proposed_filename="Same.mp3", proposed_path="Artist/Event")
    second = await seed_pending_proposal(0.99, original_filename="collision-b.mp3", proposed_filename="Same.mp3", proposed_path="Artist/Event")

    body = (await client.get("/s/rename?status=needs_review", headers={"HX-Request": "true"})).text

    assert body.count("Destination collides with another pending or approved operation") == 2
    assert f'hx-patch="/proposals/{first.id}/approve"' not in body
    assert f'hx-patch="/proposals/{second.id}/approve"' not in body
    response = await client.patch(
        f"/proposals/{first.id}/approve",
        data={"expected_updated_at": first.updated_at.isoformat()},
    )
    assert response.status_code == 409
