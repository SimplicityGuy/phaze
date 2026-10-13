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


@pytest.mark.parametrize("threshold", [89, 90, 99, 100])
async def test_bulk_preview_and_write_threshold_bounds(client: AsyncClient, threshold: int) -> None:
    preview = await client.post("/proposals/bulk/preview", data={"threshold": threshold})
    write = await client.patch("/proposals/bulk", data={"action": "approve_eligible", "threshold": threshold})
    expected = 200 if 90 <= threshold <= 99 else 422
    assert preview.status_code == write.status_code == expected
    if expected == 200:
        assert preview.json() == {
            "threshold": threshold,
            "scope": "selected",
            "capped": False,
            "eligible_count": 0,
            "skipped_count": 0,
            "eligible_ids": [],
            "review_tokens": [],
            "preview_rows": [],
            "view_query": f"status=needs_review&q=&page=1&page_size=25&sort=confidence&order=asc&selected=&threshold={threshold}",
        }


async def test_active_threshold_controls_visible_eligibility_and_wire_limit(
    client: AsyncClient, seed_pending_proposal: Callable[..., Awaitable[RenameProposal]]
) -> None:
    await seed_pending_proposal(0.95, original_filename="threshold.mp3")
    body = (await client.get("/s/rename?threshold=99")).text
    assert "Below the 99% bulk-approval threshold" in body
    assert "Excluded: confidence is below 99%." in body
    assert 'name="threshold" value="99"' in body
    token = html.unescape(re.findall(r'<input(?=[^>]*name="review_tokens")(?=[^>]*value="([^"]+)")[^>]*>', body)[0])
    for url, method in [("/proposals/bulk/preview", client.post), ("/proposals/bulk", client.patch)]:
        accepted = await method(url, data={"action": "approve_eligible", "review_tokens": [token] * 500, "threshold": 99})
        assert accepted.status_code == 200
        refused = await method(url, data={"action": "approve_eligible", "review_tokens": [token] * 501, "threshold": 99})
        assert refused.status_code == 422


@pytest.mark.parametrize("headers", [{}, {"HX-Request": "true"}, {"HX-Request": "true", "HX-History-Restore-Request": "true"}])
async def test_preview_shape_and_confirmation_bind_original_eligible_selection(
    client: AsyncClient, seed_pending_proposal: Callable[..., Awaitable[RenameProposal]], headers: dict[str, str]
) -> None:
    proposal = await seed_pending_proposal(0.99, original_filename="preview.mp3")
    rendered = (await client.get("/s/rename")).text
    token = html.unescape(re.findall(r'<input(?=[^>]*name="review_tokens")(?=[^>]*value="([^"]+)")[^>]*>', rendered)[0])
    response = await client.post("/proposals/bulk/preview", data={"review_tokens": token, "threshold": 99}, headers=headers)
    assert response.status_code == 200
    assert "HX-Request" in response.headers["vary"] and "no-store" in response.headers["cache-control"]
    if headers == {"HX-Request": "true"}:
        assert "Nothing has been approved" in response.text
        assert "Approve these 1 previewed proposals" in response.text
        assert html.escape(token, quote=True) in response.text
    else:
        assert response.json()["eligible_ids"] == [str(proposal.id)]
        assert response.json()["review_tokens"] == [token]


async def test_bulk_undo_response_counts_actual_versions_and_replay(
    client: AsyncClient, session: AsyncSession, seed_pending_proposal: Callable[..., Awaitable[RenameProposal]]
) -> None:
    from datetime import timedelta

    from bs4 import BeautifulSoup
    from sqlalchemy import update

    from phaze.models.proposal import RenameProposal

    rows = [await seed_pending_proposal(0.99, original_filename=f"undo-{i}.mp3") for i in range(3)]
    body = (await client.get("/s/rename")).text
    tokens = [html.unescape(token) for token in re.findall(r'<input(?=[^>]*name="review_tokens")(?=[^>]*value="([^"]+)")[^>]*>', body)]
    response = await client.patch("/proposals/bulk", data={"action": "approve_eligible", "review_tokens": tokens})
    form = BeautifulSoup(response.text, "html.parser").select_one('form[hx-patch^="/proposals/bulk/undo?"]')
    assert form is not None
    undo_tokens = [field["value"] for field in form.select('input[name="review_tokens"]')]
    assert len(undo_tokens) == 3
    await session.execute(
        update(RenameProposal)
        .where(RenameProposal.id == rows[1].id)
        .values(proposed_filename="Edited.mp3", updated_at=rows[1].updated_at + timedelta(seconds=5))
    )
    await session.execute(update(RenameProposal).where(RenameProposal.id == rows[2].id).values(status=ProposalStatus.EXECUTED.value))
    await session.commit()
    response = await client.patch("/proposals/bulk/undo", data={"review_tokens": [*undo_tokens, undo_tokens[0]]})
    assert response.status_code == 200
    assert "1 reverted, 2 skipped" in response.text
    assert "0 reverted, 3 skipped" in (await client.patch("/proposals/bulk/undo", data={"review_tokens": undo_tokens})).text
    assert (await client.patch("/proposals/bulk/undo", data={"review_tokens": [undo_tokens[0]] * 501})).status_code == 422


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
    assert "1 approved" in response.text
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
    assert "Excluded: confidence unavailable; individual review required." in body
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
    assert "0 approved" in response.text


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


async def test_individual_edit_and_undo_keep_active_bulk_threshold(
    client: AsyncClient, session: AsyncSession, seed_pending_proposal: Callable[..., Awaitable[RenameProposal]]
) -> None:
    proposal = await seed_pending_proposal(0.95, original_filename="threshold-row.mp3")
    body = (await client.get("/s/rename?threshold=99")).text
    assert f"/proposals/{proposal.id}/edit?threshold=99" in body
    edited = await client.patch(f"/proposals/{proposal.id}/edit?threshold=99", data={"proposed": "Reviewed.mp3"})
    assert edited.status_code == 200
    assert "Below the 99% bulk-approval threshold" in edited.text
    assert "Eligible for selected bulk approval." not in edited.text
    assert f"/proposals/{proposal.id}/approve?threshold=99" in edited.text
    refused = await client.patch(f"/proposals/{proposal.id}/approve?threshold=100")
    assert refused.status_code == 422
    await session.refresh(proposal)
    approved = await client.patch(f"/proposals/{proposal.id}/approve?threshold=99", data={"expected_updated_at": proposal.updated_at.isoformat()})
    assert approved.status_code == 200
    assert f"/proposals/{proposal.id}/undo?threshold=99" in approved.text
    reverted = await client.patch(f"/proposals/{proposal.id}/undo?threshold=99")
    assert reverted.status_code == 200
    assert "Below the 99% bulk-approval threshold" in reverted.text
    assert f"/proposals/{proposal.id}/edit?threshold=99" in reverted.text


async def test_population_preview_lists_exact_bounded_operations_and_preserves_view(
    client: AsyncClient, session: AsyncSession, seed_pending_proposal: Callable[..., Awaitable[RenameProposal]]
) -> None:
    from urllib.parse import parse_qs, urlsplit

    from bs4 import BeautifulSoup
    from sqlalchemy import func, select

    from phaze.models.proposal import RenameProposal

    proposals = [
        await seed_pending_proposal(
            0.99, original_filename=f"cap-{index}.mp3", proposed_filename=f"Target-{index}.mp3", proposed_path="Artists/Event"
        )
        for index in range(501)
    ]
    query = "status=all&q=retained&sort=filename&order=desc&page=2&page_size=50&selected=" + str(proposals[0].id)
    preview = await client.post("/proposals/bulk/preview?" + query, data={"scope": "filtered", "threshold": 99}, headers={"HX-Request": "true"})
    assert preview.status_code == 200
    document = BeautifulSoup(preview.text, "html.parser")
    assert "More than 500 eligible proposals match" in document.get_text()
    displayed = document.select("[data-preview-proposal]")
    assert len(displayed) == 500
    assert all("Filename:" in row.get_text() and "Destination:" in row.get_text() and "Artists/Event/Target-" in row.get_text() for row in displayed)
    form = document.select_one("form[hx-patch]")
    assert form is not None
    tokens = [field["value"] for field in form.select('input[name="review_tokens"]')]
    assert len(tokens) == 500
    assert [token.split("|")[0] for token in tokens] == [row["data-preview-proposal"] for row in displayed]
    preserved = parse_qs(urlsplit(form["hx-patch"]).query, keep_blank_values=True)
    assert preserved == {
        "status": ["all"],
        "q": ["retained"],
        "sort": ["filename"],
        "order": ["desc"],
        "page": ["2"],
        "page_size": ["50"],
        "selected": [str(proposals[0].id)],
        "threshold": ["99"],
    }
    assert await session.scalar(select(func.count()).select_from(RenameProposal).where(RenameProposal.status == ProposalStatus.APPROVED.value)) == 0
    approved = await client.patch(form["hx-patch"], data={"action": "approve_eligible", "threshold": 99, "review_tokens": tokens})
    assert approved.status_code == 200 and "500 approved, 0 skipped" in approved.text
    assert await session.scalar(select(func.count()).select_from(RenameProposal).where(RenameProposal.status == ProposalStatus.APPROVED.value)) == 500
    undo = BeautifulSoup(approved.text, "html.parser").select_one('form[hx-patch^="/proposals/bulk/undo?"]')
    assert undo is not None
    assert parse_qs(urlsplit(undo["hx-patch"]).query, keep_blank_values=True) == preserved
    assert len(undo.select('input[name="review_tokens"]')) == 500


async def test_paired_preview_refuses_rewritten_timestamp_and_same_timestamp_digest(
    client: AsyncClient, session: AsyncSession, seed_pending_proposal: Callable[..., Awaitable[RenameProposal]]
) -> None:
    from datetime import timedelta

    from sqlalchemy import update

    from phaze.models.proposal import RenameProposal

    rows = [
        await seed_pending_proposal(
            0.99, original_filename=f"paired-{index}.mp3", proposed_filename=f"Renamed-{index}.mp3", proposed_path="Before/Event"
        )
        for index in range(3)
    ]
    response = await client.post("/proposals/bulk/preview", data={"scope": "filtered", "threshold": 99})
    payload = response.json()
    assert payload["eligible_count"] == 3 and payload["scope"] == "filtered" and payload["skipped_count"] is None
    assert all(
        row["after_filename"].startswith("Renamed-") and row["after_destination"].startswith("Before/Event/") for row in payload["preview_rows"]
    )
    rows[1].proposed_path = "Unreviewed/Destination"
    rows[1].updated_at += timedelta(seconds=5)
    await session.commit()
    original_timestamp = rows[2].updated_at
    await session.execute(
        update(RenameProposal).where(RenameProposal.id == rows[2].id).values(proposed_filename="Unreviewed Name.mp3", updated_at=original_timestamp)
    )
    await session.commit()
    await session.refresh(rows[2])
    assert rows[2].updated_at == original_timestamp
    result = await client.patch("/proposals/bulk", data={"action": "approve_eligible", "review_tokens": payload["review_tokens"], "threshold": 99})
    assert result.status_code == 200 and "1 approved, 2 skipped" in result.text
    for row in rows:
        await session.refresh(row)
    assert [row.status for row in rows] == [ProposalStatus.APPROVED.value, ProposalStatus.PENDING.value, ProposalStatus.PENDING.value]
