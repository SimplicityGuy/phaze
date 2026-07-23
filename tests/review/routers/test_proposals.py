"""Integration tests for proposal approval workflow UI endpoints."""

from __future__ import annotations

from pathlib import Path
import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse
import uuid

import pytest

import phaze
from phaze.models.analysis import AnalysisResult, AnalysisWindow
from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.routers.proposals import BpmSpark, _bpm_spark


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


async def add_analysis_windows(
    session: AsyncSession,
    file_id: uuid.UUID,
    *,
    mood: str = "happy",
) -> None:
    """Seed a couple of fine + coarse analysis windows for a file."""
    session.add_all(
        [
            AnalysisWindow(file_id=file_id, tier="fine", window_index=0, start_sec=0.0, end_sec=30.0, bpm=120.0, musical_key="Am"),
            AnalysisWindow(file_id=file_id, tier="fine", window_index=1, start_sec=30.0, end_sec=60.0, bpm=128.0, musical_key="C"),
            AnalysisWindow(file_id=file_id, tier="coarse", window_index=0, start_sec=0.0, end_sec=60.0, mood=mood, style="techno", danceability=0.8),
        ]
    )
    await session.commit()


async def create_test_proposal(
    session: AsyncSession,
    *,
    original_filename: str = "test_file.mp3",
    proposed_filename: str = "Artist - Track.mp3",
    confidence: float = 0.85,
    status: str = ProposalStatus.PENDING,
    reason: str = "Test reasoning",
    proposed_path: str | None = None,
) -> RenameProposal:
    """Create a FileRecord + RenameProposal pair for testing."""
    file_id = uuid.uuid4()
    file_record = FileRecord(
        agent_id="test-fileserver",
        id=file_id,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/music/{uuid.uuid4().hex}/{original_filename}",
        original_filename=original_filename,
        current_path=f"/music/{original_filename}",
        file_type="music",
        file_size=1_000_000,
    )
    session.add(file_record)
    await session.flush()

    proposal = RenameProposal(
        id=uuid.uuid4(),
        file_id=file_id,
        proposed_filename=proposed_filename,
        proposed_path=proposed_path,
        confidence=confidence,
        status=status,
        context_used={"artist": "Test Artist", "event_name": "Test Event"},
        reason=reason,
    )
    session.add(proposal)
    await session.commit()
    return proposal


@pytest.mark.asyncio
async def test_proposals_list_returns_html(client: AsyncClient, session: AsyncSession) -> None:
    """GET /proposals/ returns 200 with text/html content type.

    Phase 57 (SHELL-05): a plain GET /proposals/ now 302-redirects into the shell; the
    in-page HX request returns a proposals fragment.

    phaze-7j50: that fragment is the #proposal-list-container INNER content (table + bulk bar +
    pager), no longer the whole proposal_content.html -- every caller of this GET swaps it into the
    container with innerHTML, so returning the chrome nested it. The chrome assertion that used to
    live here encoded the buggy contract; see
    test_list_fragment_is_container_inner_content_only for the replacement.
    """
    await create_test_proposal(session)
    response = await client.get("/proposals/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'id="proposals-table"' in response.text


@pytest.mark.asyncio
async def test_proposals_list_empty_state(client: AsyncClient) -> None:
    """GET /proposals/ with no proposals returns 200 with empty state message."""
    response = await client.get("/proposals/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "No proposals yet" in response.text


@pytest.mark.asyncio
async def test_proposals_list_shows_proposals(client: AsyncClient, session: AsyncSession) -> None:
    """GET /proposals/ with seeded proposals returns rows containing proposed_filename text."""
    await create_test_proposal(session, proposed_filename="DJ Shadow - Live @ Coachella 2025.mp3")
    response = await client.get("/proposals/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "DJ Shadow - Live @ Coachella 2025.mp3" in response.text


_PROPOSAL_ROW_TEMPLATE = Path(phaze.__file__).parent / "templates" / "proposals" / "partials" / "proposal_row.html"


@pytest.mark.asyncio
async def test_executed_badge_derives_from_proposal_status(client: AsyncClient, session: AsyncSession) -> None:
    """D-04: the 'Executed' badge renders from ``proposal.status == 'executed'``, not ``file.state``.

    ``create_test_proposal`` leaves ``file.state`` at ``PROPOSAL_GENERATED`` (NOT ``'executed'``), so a
    rendered 'Executed' badge can ONLY come from the proposal's own status -- the whole point of the
    cutover. With the pre-fix template (``proposal.file.state == 'executed'``) this badge would never
    render for this fixture; this test flips RED if the reader regresses to ``file.state``.
    """
    await create_test_proposal(session, proposed_filename="Applied Set.mp3", status=ProposalStatus.EXECUTED)
    response = await client.get("/proposals/?status=all", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "Applied Set.mp3" in response.text
    assert "Executed</span>" in response.text


def test_proposal_row_badge_reads_status_not_file_state() -> None:
    """Source-scan (D-04 / Pitfall 4): the badge branch reads ``proposal.status``; no ``file.state`` survives.

    Phase 90 drops ``files.state``; the last stray ``proposal.file.state`` reader must be gone so it
    does not trip over the removed column.
    """
    src = _PROPOSAL_ROW_TEMPLATE.read_text(encoding="utf-8")
    assert 'proposal.status == "executed"' in src
    assert "proposal.file.state" not in src


@pytest.mark.asyncio
async def test_proposals_htmx_returns_fragment(client: AsyncClient, session: AsyncSession) -> None:
    """GET /proposals/ with HX-Request header returns fragment without full HTML page."""
    await create_test_proposal(session)
    response = await client.get("/proposals/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "<html" not in response.text.lower()
    assert "<table" in response.text.lower() or "<tbody" in response.text.lower()


@pytest.mark.asyncio
async def test_proposals_pagination(client: AsyncClient, session: AsyncSession) -> None:
    """Seed 60 proposals, GET /proposals/?page=2&page_size=25 returns correct range text."""
    for i in range(60):
        await create_test_proposal(
            session,
            original_filename=f"file_{i:03d}.mp3",
            proposed_filename=f"Artist - Track {i:03d}.mp3",
        )
    response = await client.get("/proposals/?status=all&page=2&page_size=25", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "Showing 26-50" in response.text


@pytest.mark.asyncio
async def test_filter_by_status(client: AsyncClient, session: AsyncSession) -> None:
    """GET /proposals/?status=approved returns only approved proposals."""
    await create_test_proposal(session, proposed_filename="Pending One.mp3", status=ProposalStatus.PENDING, original_filename="p1.mp3")
    await create_test_proposal(session, proposed_filename="Approved One.mp3", status=ProposalStatus.APPROVED, original_filename="a1.mp3")
    await create_test_proposal(session, proposed_filename="Rejected One.mp3", status=ProposalStatus.REJECTED, original_filename="r1.mp3")

    response = await client.get("/proposals/?status=approved", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "Approved One.mp3" in response.text
    assert "Pending One.mp3" not in response.text
    assert "Rejected One.mp3" not in response.text


@pytest.mark.asyncio
async def test_search_proposals(client: AsyncClient, session: AsyncSession) -> None:
    """GET /proposals/?q=searchterm returns matching proposals."""
    await create_test_proposal(
        session,
        original_filename="coachella_set.mp3",
        proposed_filename="DJ Shadow - Live @ Coachella.mp3",
    )
    await create_test_proposal(
        session,
        original_filename="random_track.mp3",
        proposed_filename="Random Artist - Song.mp3",
    )
    response = await client.get("/proposals/?status=all&q=coachella", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "Coachella" in response.text


@pytest.mark.asyncio
async def test_pagination_and_sort_urls_urlencode_search_query(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-ro47: `&`/`=`/`#` in the search text must not corrupt pager/sort hx-get URLs.

    Un-encoded, ``q=Drum & Bass`` splices a bogus `` Bass`` parameter after the real ``q``
    (Starlette resolves duplicate params from the FIRST occurrence, so injected params can win
    over the template's own ``sort``/``order``/``page_size``), and a bare ``#`` truncates the rest
    of the URL client-side. This parses every ``hx-get="/proposals/?..."`` URL emitted by
    pagination.html and proposal_table.html and asserts the search text round-trips intact and
    sort/order/page_size all survive.
    """
    query = "Drum & Bass #1 100% mix=on"
    await create_test_proposal(session, proposed_filename=f"{query} track.mp3", original_filename="rt.mp3")
    response = await client.get(
        "/proposals/",
        params={"status": "all", "q": query, "sort": "confidence", "order": "desc"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200

    urls = re.findall(r'hx-get="([^"]+)"', response.text)
    proposal_urls = [u for u in urls if u.startswith("/proposals/?")]
    assert proposal_urls, "expected at least one pagination/sort hx-get URL to be rendered"

    for url in proposal_urls:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        assert params.get("q") == [query], f"search query corrupted in {url!r}: got {params.get('q')!r}"
        assert "sort" in params, f"sort param lost (likely truncated by an un-encoded '#') in {url!r}"
        assert "order" in params, f"order param lost (likely truncated by an un-encoded '#') in {url!r}"
        # Un-encoded, the raw '#' would have been parsed as the URL FRAGMENT delimiter by
        # urlparse itself -- proving the client (browser/htmx) would truncate there too.
        assert parsed.fragment == ""


@pytest.mark.asyncio
async def test_approve_proposal(client: AsyncClient, session: AsyncSession) -> None:
    """PATCH /proposals/{id}/approve returns 200, updates DB status, includes stats-bar."""
    proposal = await create_test_proposal(session)
    response = await client.patch(f"/proposals/{proposal.id}/approve")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "stats-bar" in response.text

    # Verify DB state
    updated = await session.get(RenameProposal, proposal.id)
    assert updated is not None
    assert updated.status == ProposalStatus.APPROVED


@pytest.mark.asyncio
async def test_reject_proposal(client: AsyncClient, session: AsyncSession) -> None:
    """PATCH /proposals/{id}/reject returns 200, updates DB status to rejected."""
    proposal = await create_test_proposal(session)
    response = await client.patch(f"/proposals/{proposal.id}/reject")
    assert response.status_code == 200

    updated = await session.get(RenameProposal, proposal.id)
    assert updated is not None
    assert updated.status == ProposalStatus.REJECTED


@pytest.mark.asyncio
async def test_undo_proposal(client: AsyncClient, session: AsyncSession) -> None:
    """Approve then undo -- proposal should revert to pending."""
    proposal = await create_test_proposal(session, status=ProposalStatus.APPROVED)
    response = await client.patch(f"/proposals/{proposal.id}/undo")
    assert response.status_code == 200

    updated = await session.get(RenameProposal, proposal.id)
    assert updated is not None
    assert updated.status == ProposalStatus.PENDING


@pytest.mark.asyncio
async def test_approve_not_found(client: AsyncClient) -> None:
    """PATCH /proposals/{random_uuid}/approve returns 404."""
    random_id = uuid.uuid4()
    response = await client.patch(f"/proposals/{random_id}/approve")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_row_detail(client: AsyncClient, session: AsyncSession) -> None:
    """GET /proposals/{id}/detail returns 200 with AI Reasoning text."""
    proposal = await create_test_proposal(session, reason="Because the artist name matches Coachella lineup")
    response = await client.get(f"/proposals/{proposal.id}/detail")
    assert response.status_code == 200
    assert "AI Reasoning" in response.text
    assert "Because the artist name matches Coachella lineup" in response.text


@pytest.mark.asyncio
async def test_bulk_approve(client: AsyncClient, session: AsyncSession) -> None:
    """PATCH /proposals/bulk with action=approve updates all listed proposals."""
    p1 = await create_test_proposal(session, original_filename="bulk1.mp3", proposed_filename="Bulk One.mp3")
    p2 = await create_test_proposal(session, original_filename="bulk2.mp3", proposed_filename="Bulk Two.mp3")

    response = await client.patch(
        "/proposals/bulk",
        data={"action": "approve", "proposal_ids": [str(p1.id), str(p2.id)]},
    )
    assert response.status_code == 200

    updated1 = await session.get(RenameProposal, p1.id)
    updated2 = await session.get(RenameProposal, p2.id)
    assert updated1 is not None
    assert updated1.status == ProposalStatus.APPROVED
    assert updated2 is not None
    assert updated2.status == ProposalStatus.APPROVED


@pytest.mark.asyncio
async def test_bulk_response_rerenders_list_container(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-gc5d: the bulk PATCH body must be the re-rendered list, not an OOB-only empty payload.

    The forms swap this response into #proposal-list-container with innerHTML. Before the fix the
    handler returned approve_response.html with proposal=None, whose non-OOB body is gated on
    `{% if proposal %}` -- so htmx applied the OOB stats/toast and then blanked the container,
    destroying the table AND the selection toolbar until a full page reload.
    """
    keep = await create_test_proposal(session, original_filename="gc5d_keep.mp3", proposed_filename="GC5D Keep.mp3")
    acted = await create_test_proposal(session, original_filename="gc5d_acted.mp3", proposed_filename="GC5D Acted.mp3")

    response = await client.patch(
        "/proposals/bulk",
        data={"action": "approve", "proposal_ids": [str(acted.id)], "status": "pending"},
    )
    assert response.status_code == 200

    # The primary (non-OOB) body carries the table and the bulk-actions toolbar back.
    assert 'id="proposals-table"' in response.text
    assert f'id="proposal-{keep.id}"' in response.text
    assert 'hx-patch="/proposals/bulk"' in response.text
    # ...and the OOB stats/toast still fire.
    assert 'id="stats-bar"' in response.text
    assert "1 proposals approved." in response.text


@pytest.mark.asyncio
async def test_bulk_response_preserves_page_and_filter(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-gc5d: a bulk action issued from page 2 with a search filter comes back on page 2 with it.

    Re-rendering the container is only correct if it re-renders the SAME view; otherwise the wipe is
    merely traded for a silent reset to page 1 of the default filter.
    """
    created = [
        await create_test_proposal(session, original_filename=f"gc5dpage_{i:02d}.mp3", proposed_filename=f"GC5D Page {i:02d}.mp3") for i in range(12)
    ]
    # Filenames are zero-padded and created in ascending order, so page 2 of a 10-per-page
    # original_filename-ascending listing holds the last two.
    page_two = created[10:]

    response = await client.patch(
        "/proposals/bulk",
        data={
            "action": "approve",
            "proposal_ids": [str(page_two[0].id)],
            "status": "pending",
            "q": "gc5dpage",
            "page": "2",
            "page_size": "10",
            "sort": "original_filename",
            "order": "asc",
        },
    )
    assert response.status_code == 200

    # The re-rendered bulk toolbar echoes the state back, so the NEXT bulk action stays put too.
    assert 'name="page" value="2"' in response.text
    assert 'name="q" value="gc5dpage"' in response.text
    assert 'name="sort" value="original_filename"' in response.text
    assert 'name="status" value="pending"' in response.text
    # Still showing page 2 of the filtered pending set (11 pending remain -> 1 row on page 2).
    assert f'id="proposal-{page_two[1].id}"' in response.text
    assert f'id="proposal-{page_two[0].id}"' not in response.text


@pytest.mark.asyncio
async def test_list_fragment_is_container_inner_content_only(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-7j50 (symptom 1): the pagination/sort/search GET must not nest chrome in the container.

    Every pagination button, page-size button, sort header and the search box issue
    ``hx-get="/proposals/?..."`` with ``hx-target="#proposal-list-container"`` and
    ``hx-swap="innerHTML"``. The handler used to answer with the whole proposal_content.html --
    filter tabs, search box, the container div itself and the pager -- so ONE page change or column
    sort swapped a duplicate #proposal-list-container, a duplicate filter-tab bar, a duplicate
    search box and a duplicate pager INSIDE the live container. Subsequent swaps then resolved to
    the outer element while the stale inner copy stayed on screen.

    The response is asserted to be safely innerHTML-swappable: zero occurrences of the container id
    (not merely "at most one"), and none of the chrome that lives outside it.
    """
    for i in range(12):
        await create_test_proposal(session, original_filename=f"7j50_{i:02d}.mp3", proposed_filename=f"7J50 {i:02d}.mp3")

    response = await client.get("/proposals/?status=pending&page=1&page_size=10", headers={"HX-Request": "true"})
    assert response.status_code == 200

    # (a) swapping this in cannot produce a duplicate id.
    assert response.text.count('id="proposal-list-container"') == 0
    # ...nor a duplicate filter-tab bar or search box.
    assert 'aria-label="Status filter tabs"' not in response.text
    assert 'placeholder="Search by filename..."' not in response.text
    # It IS the container's content: table + selection toolbar + exactly one pager.
    assert 'id="proposals-table"' in response.text
    assert 'hx-patch="/proposals/bulk"' in response.text
    assert response.text.count("Per page:") == 1
    assert "Showing 1-10 of 12 proposals" in response.text


@pytest.mark.asyncio
async def test_bulk_response_refreshes_pager(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-7j50 (symptom 2): after a bulk action the pager reports POST-action totals.

    The pager used to render outside #proposal-list-container, so the bulk response -- which
    re-renders the container -- could not update it and the operator was left reading
    "Showing 1-N of <pre-action total> proposals" plus page buttons for rows that had just left the
    filtered set. Moving the pager inside the container makes the bulk re-render carry it.
    """
    created = [
        await create_test_proposal(session, original_filename=f"7j50pager_{i:02d}.mp3", proposed_filename=f"7J50 Pager {i:02d}.mp3") for i in range(6)
    ]

    before = await client.get("/proposals/?status=pending&q=7j50pager&page=1&page_size=10", headers={"HX-Request": "true"})
    assert "Showing 1-6 of 6 proposals" in before.text

    response = await client.patch(
        "/proposals/bulk",
        data={
            "action": "approve",
            "proposal_ids": [str(created[0].id), str(created[1].id)],
            "status": "pending",
            "q": "7j50pager",
            "page": "1",
            "page_size": "10",
        },
    )
    assert response.status_code == 200
    # Two rows left the pending set: the pager in the swapped body reflects that, and only once.
    assert "Showing 1-4 of 4 proposals" in response.text
    assert "of 6 proposals" not in response.text
    assert response.text.count("Per page:") == 1


@pytest.mark.asyncio
async def test_bulk_approve_skips_malformed_id(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-3st0: a malformed proposal_ids entry is SKIPPED (never a 500); valid ids still act."""
    p1 = await create_test_proposal(session, original_filename="bulkmal1.mp3")

    response = await client.patch(
        "/proposals/bulk",
        data={"action": "approve", "proposal_ids": [str(p1.id), "not-a-uuid"]},
    )
    assert response.status_code == 200
    assert "1 proposals approved." in response.text

    updated1 = await session.get(RenameProposal, p1.id)
    assert updated1 is not None
    assert updated1.status == ProposalStatus.APPROVED


@pytest.mark.asyncio
async def test_bulk_approve_skips_empty_id(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-3st0: an empty-string proposal_ids entry is SKIPPED (never a 500)."""
    p1 = await create_test_proposal(session, original_filename="bulkmal2.mp3")

    response = await client.patch(
        "/proposals/bulk",
        data={"action": "approve", "proposal_ids": [str(p1.id), ""]},
    )
    assert response.status_code == 200

    updated1 = await session.get(RenameProposal, p1.id)
    assert updated1 is not None
    assert updated1.status == ProposalStatus.APPROVED


@pytest.mark.asyncio
async def test_bulk_approve_all_ids_malformed(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-3st0: every id malformed -> 200 with a zero count, never a 500."""
    response = await client.patch(
        "/proposals/bulk",
        data={"action": "approve", "proposal_ids": ["not-a-uuid", ""]},
    )
    assert response.status_code == 200
    assert "0 proposals approved." in response.text


@pytest.mark.asyncio
async def test_sort_by_confidence(client: AsyncClient, session: AsyncSession) -> None:
    """GET /proposals/?sort=confidence&order=asc returns proposals sorted by confidence ascending."""
    await create_test_proposal(session, original_filename="high.mp3", proposed_filename="High Conf.mp3", confidence=0.95)
    await create_test_proposal(session, original_filename="low.mp3", proposed_filename="Low Conf.mp3", confidence=0.30)
    await create_test_proposal(session, original_filename="mid.mp3", proposed_filename="Mid Conf.mp3", confidence=0.60)

    response = await client.get("/proposals/?status=all&sort=confidence&order=asc", headers={"HX-Request": "true"})
    assert response.status_code == 200
    text = response.text
    # Low confidence should appear before high confidence in ascending order
    low_pos = text.find("Low Conf.mp3")
    mid_pos = text.find("Mid Conf.mp3")
    high_pos = text.find("High Conf.mp3")
    assert low_pos < mid_pos < high_pos


@pytest.mark.asyncio
async def test_reject_not_found(client: AsyncClient) -> None:
    """PATCH /proposals/{random_uuid}/reject returns 404."""
    response = await client.patch(f"/proposals/{uuid.uuid4()}/reject")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_undo_not_found(client: AsyncClient) -> None:
    """PATCH /proposals/{random_uuid}/undo returns 404."""
    response = await client.patch(f"/proposals/{uuid.uuid4()}/undo")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_row_detail_not_found(client: AsyncClient) -> None:
    """GET /proposals/{random_uuid}/detail returns 404."""
    response = await client.get(f"/proposals/{uuid.uuid4()}/detail")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_bulk_invalid_action(client: AsyncClient, session: AsyncSession) -> None:
    """PATCH /proposals/bulk with invalid action returns 400."""
    p = await create_test_proposal(session)
    response = await client.patch(
        "/proposals/bulk",
        data={"action": "invalid", "proposal_ids": [str(p.id)]},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_bulk_reject(client: AsyncClient, session: AsyncSession) -> None:
    """PATCH /proposals/bulk with action=reject updates proposals to rejected."""
    p1 = await create_test_proposal(session, original_filename="br1.mp3")
    p2 = await create_test_proposal(session, original_filename="br2.mp3")

    response = await client.patch(
        "/proposals/bulk",
        data={"action": "reject", "proposal_ids": [str(p1.id), str(p2.id)]},
    )
    assert response.status_code == 200

    updated1 = await session.get(RenameProposal, p1.id)
    updated2 = await session.get(RenameProposal, p2.id)
    assert updated1 is not None
    assert updated1.status == ProposalStatus.REJECTED
    assert updated2 is not None
    assert updated2.status == ProposalStatus.REJECTED


@pytest.mark.asyncio
async def test_sort_by_original_filename(client: AsyncClient, session: AsyncSession) -> None:
    """GET /proposals/?sort=original_filename sorts by file's original name."""
    await create_test_proposal(session, original_filename="zzz.mp3", proposed_filename="Z.mp3")
    await create_test_proposal(session, original_filename="aaa.mp3", proposed_filename="A.mp3")

    response = await client.get("/proposals/?status=all&sort=original_filename&order=asc", headers={"HX-Request": "true"})
    assert response.status_code == 200
    text = response.text
    assert text.find("aaa.mp3") < text.find("zzz.mp3")


@pytest.mark.asyncio
async def test_destination_column_header(client: AsyncClient, session: AsyncSession) -> None:
    """GET /proposals/ renders a Destination column header in the table."""
    await create_test_proposal(session)
    response = await client.get("/proposals/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "Destination" in response.text


@pytest.mark.asyncio
async def test_destination_path_displayed(client: AsyncClient, session: AsyncSession) -> None:
    """Proposal with proposed_path renders the path text in the table row."""
    await create_test_proposal(
        session,
        proposed_path="performances/artists/Disclosure",
        proposed_filename="Disclosure - Live @ Coachella 2025.mp3",
    )
    response = await client.get("/proposals/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "performances/artists/Disclosure" in response.text


@pytest.mark.asyncio
async def test_destination_null_path_badge(client: AsyncClient, session: AsyncSession) -> None:
    """Proposal with null proposed_path renders 'No path' badge."""
    await create_test_proposal(session, proposed_path=None)
    response = await client.get("/proposals/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "No path" in response.text


@pytest.mark.asyncio
async def test_row_renders_sparkline_and_timeline_control(client: AsyncClient, session: AsyncSession) -> None:
    """The review row shows a BPM sparkline SVG and an HTMX timeline expand control."""
    proposal = await create_test_proposal(session)
    await add_analysis_windows(session, proposal.file_id)
    response = await client.get("/proposals/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "<svg" in response.text
    assert f'hx-get="/proposals/{proposal.id}/timeline"' in response.text
    assert f'id="timeline-{proposal.id}"' in response.text
    # Fine-window BPMs flow into a rendered polyline sparkline.
    assert "<polyline" in response.text


@pytest.mark.asyncio
async def test_row_sparkline_without_windows(client: AsyncClient, session: AsyncSession) -> None:
    """A file with no analysis windows still renders a (flat) sparkline + timeline control."""
    proposal = await create_test_proposal(session)
    response = await client.get("/proposals/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "<svg" in response.text
    assert f'hx-get="/proposals/{proposal.id}/timeline"' in response.text


@pytest.mark.asyncio
async def test_timeline_with_windows(client: AsyncClient, session: AsyncSession) -> None:
    """GET /proposals/{id}/timeline returns a 200 SVG fragment with BPM polyline + ribbons."""
    proposal = await create_test_proposal(session)
    await add_analysis_windows(session, proposal.file_id)
    response = await client.get(f"/proposals/{proposal.id}/timeline")
    assert response.status_code == 200
    assert "<polyline" in response.text
    # quick 260707-c9o: max (top) + min (bottom) BPM labels render as HTML gutter text.
    # Seeded fine windows carry bpm 120.0 / 128.0 (add_analysis_windows).
    assert "128" in response.text
    assert "120" in response.text
    assert 'aria-label="BPM range 120 to 128"' in response.text
    # Escaped ribbon labels rendered via Jinja2 autoescaping.
    assert "techno" in response.text
    assert "happy" in response.text


@pytest.mark.asyncio
async def test_timeline_empty_state(client: AsyncClient, session: AsyncSession) -> None:
    """GET /proposals/{id}/timeline for a file with no windows renders the empty state."""
    proposal = await create_test_proposal(session)
    response = await client.get(f"/proposals/{proposal.id}/timeline")
    assert response.status_code == 200
    assert "No analysis windows" in response.text


@pytest.mark.asyncio
async def test_timeline_not_found(client: AsyncClient) -> None:
    """GET /proposals/{random_uuid}/timeline returns 404."""
    response = await client.get(f"/proposals/{uuid.uuid4()}/timeline")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_timeline_scoped_by_file_id(client: AsyncClient, session: AsyncSession) -> None:
    """Timeline only renders windows belonging to the proposal's own file_id."""
    p1 = await create_test_proposal(session, original_filename="one.mp3")
    p2 = await create_test_proposal(session, original_filename="two.mp3")
    await add_analysis_windows(session, p1.file_id, mood="euphoric")
    await add_analysis_windows(session, p2.file_id, mood="melancholy")
    response = await client.get(f"/proposals/{p1.id}/timeline")
    assert response.status_code == 200
    assert "euphoric" in response.text
    assert "melancholy" not in response.text


@pytest.mark.asyncio
async def test_timeline_escapes_label_xss(client: AsyncClient, session: AsyncSession) -> None:
    """A malicious essentia-derived label is HTML-escaped, never rendered as raw markup."""
    proposal = await create_test_proposal(session)
    session.add(
        AnalysisWindow(
            file_id=proposal.file_id,
            tier="coarse",
            window_index=0,
            start_sec=0.0,
            end_sec=60.0,
            mood="<script>alert(1)</script>",
            style="techno",
        )
    )
    await session.commit()
    response = await client.get(f"/proposals/{proposal.id}/timeline")
    assert response.status_code == 200
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "<script>alert(1)</script>" not in response.text


# ---------------------------------------------------------------------------
# Phase 44 Plan 04 Task 2: sampled badge + Deepen-analysis button on the timeline
#
# The timeline route also fetches the 1:1 AnalysisResult and passes `analysis`
# + `file_id` into the context. The badge renders ONLY when analysis.sampled is
# truthy (NULL/false -> nothing, never an error); the Deepen button is gated on
# the same condition and POSTs to the Plan-03 /pipeline/files/{file_id}/deepen
# endpoint.
# ---------------------------------------------------------------------------


async def add_analysis_result(
    session: AsyncSession,
    file_id: uuid.UUID,
    *,
    sampled: bool | None,
    fine_analyzed: int | None = 20,
    fine_total: int | None = 100,
    coarse_analyzed: int | None = 5,
    coarse_total: int | None = 30,
) -> None:
    """Seed the 1:1 AnalysisResult row driving the sampled badge."""
    session.add(
        AnalysisResult(
            file_id=file_id,
            bpm=128.0,
            musical_key="Am",
            sampled=sampled,
            fine_windows_analyzed=fine_analyzed,
            fine_windows_total=fine_total,
            coarse_windows_analyzed=coarse_analyzed,
            coarse_windows_total=coarse_total,
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_timeline_renders_sampled_badge_and_deepen_button(client: AsyncClient, session: AsyncSession) -> None:
    """A file whose AnalysisResult.sampled is True shows the badge (with coverage tooltip) + Deepen button."""
    proposal = await create_test_proposal(session)
    await add_analysis_windows(session, proposal.file_id)
    await add_analysis_result(session, proposal.file_id, sampled=True)

    response = await client.get(f"/proposals/{proposal.id}/timeline")
    assert response.status_code == 200
    # Badge present.
    assert "Sampled — more data available" in response.text
    # The four coverage counts ride the tooltip.
    assert "fine 20/100, coarse 5/30 windows — sampled" in response.text
    # Deepen button POSTs to the Plan-03 endpoint for THIS file_id.
    assert f'hx-post="/pipeline/files/{proposal.file_id}/deepen"' in response.text
    assert "Deepen analysis" in response.text


@pytest.mark.asyncio
async def test_timeline_no_badge_when_sampled_false(client: AsyncClient, session: AsyncSession) -> None:
    """A full-budget analysis (sampled=False) renders NEITHER the badge NOR the Deepen button."""
    proposal = await create_test_proposal(session)
    await add_analysis_windows(session, proposal.file_id)
    await add_analysis_result(session, proposal.file_id, sampled=False)

    response = await client.get(f"/proposals/{proposal.id}/timeline")
    assert response.status_code == 200
    assert "Sampled — more data available" not in response.text
    assert "Deepen analysis" not in response.text
    assert "/deepen" not in response.text


@pytest.mark.asyncio
async def test_timeline_no_badge_when_sampled_null(client: AsyncClient, session: AsyncSession) -> None:
    """A pre-Phase-43 row (sampled=NULL coverage) renders NOTHING -- never an error (D-03 / T-44-12)."""
    proposal = await create_test_proposal(session)
    await add_analysis_windows(session, proposal.file_id)
    await add_analysis_result(
        session,
        proposal.file_id,
        sampled=None,
        fine_analyzed=None,
        fine_total=None,
        coarse_analyzed=None,
        coarse_total=None,
    )

    response = await client.get(f"/proposals/{proposal.id}/timeline")
    assert response.status_code == 200
    assert "Sampled — more data available" not in response.text
    assert "Deepen analysis" not in response.text


@pytest.mark.asyncio
async def test_timeline_no_badge_when_no_analysis_row(client: AsyncClient, session: AsyncSession) -> None:
    """A file with NO AnalysisResult row at all renders the timeline without error and no badge."""
    proposal = await create_test_proposal(session)
    await add_analysis_windows(session, proposal.file_id)

    response = await client.get(f"/proposals/{proposal.id}/timeline")
    assert response.status_code == 200
    assert "Sampled — more data available" not in response.text
    assert "Deepen analysis" not in response.text


# ---------------------------------------------------------------------------
# _bpm_spark helper (quick 260707-c9o): surface min/max BPM alongside the points.
# Pure sync function -- no DB needed. Windows are constructed in-memory.
# ---------------------------------------------------------------------------


def _fine_window(index: int, start: float, end: float, bpm: float | None) -> AnalysisWindow:
    """Build a detached fine-tier AnalysisWindow carrying just the fields the helper reads."""
    return AnalysisWindow(file_id=uuid.uuid4(), tier="fine", window_index=index, start_sec=start, end_sec=end, bpm=bpm)


def test_bpm_spark_normal_range() -> None:
    """A range of distinct BPMs yields a non-empty points string plus lo/hi/count."""
    windows = [_fine_window(0, 0.0, 30.0, 120.0), _fine_window(1, 30.0, 60.0, 128.0)]
    result = _bpm_spark(windows, 60.0, 1000.0, 120.0)
    assert isinstance(result, BpmSpark)
    assert result.points
    assert "," in result.points
    assert result.lo == 120.0
    assert result.hi == 128.0
    assert result.window_count == 2


def test_bpm_spark_flat_line() -> None:
    """All-equal BPMs surface the single value (lo == hi) and land the line at height/2."""
    windows = [_fine_window(0, 0.0, 30.0, 120.0), _fine_window(1, 30.0, 60.0, 120.0)]
    height = 120.0
    result = _bpm_spark(windows, 60.0, 1000.0, height)
    assert result.lo == result.hi == 120.0
    assert result.points
    # span <= 0 => every y coordinate is height/2 (unchanged flat-line behavior).
    y_values = {pair.split(",")[1] for pair in result.points.split(" ")}
    assert y_values == {f"{height / 2.0:.2f}"}
    assert result.window_count == 2


def test_bpm_spark_empty() -> None:
    """No bpm-bearing windows (or total_sec <= 0) yields empty points and None lo/hi."""
    result = _bpm_spark([_fine_window(0, 0.0, 30.0, None)], 60.0, 1000.0, 120.0)
    assert result.points == ""
    assert result.lo is None
    assert result.hi is None
    assert result.window_count == 0


# ---------------------------------------------------------------------------
# State-machine guard on the review-UI status routes (phaze-uu17)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_terminal_executed_returns_409(client: AsyncClient, session: AsyncSession) -> None:
    """An EXECUTED proposal cannot be flipped back to APPROVED via the review UI (phaze-uu17)."""
    proposal = await create_test_proposal(session, status=ProposalStatus.EXECUTED)
    response = await client.patch(f"/proposals/{proposal.id}/approve")
    assert response.status_code == 409

    updated = await session.get(RenameProposal, proposal.id)
    assert updated is not None
    assert updated.status == ProposalStatus.EXECUTED  # authoritative applied fact preserved


@pytest.mark.asyncio
async def test_reject_terminal_failed_returns_409(client: AsyncClient, session: AsyncSession) -> None:
    """A FAILED proposal cannot be flipped to REJECTED via the review UI (phaze-uu17)."""
    proposal = await create_test_proposal(session, status=ProposalStatus.FAILED)
    response = await client.patch(f"/proposals/{proposal.id}/reject")
    assert response.status_code == 409

    updated = await session.get(RenameProposal, proposal.id)
    assert updated is not None
    assert updated.status == ProposalStatus.FAILED


@pytest.mark.asyncio
async def test_undo_terminal_executed_returns_409(client: AsyncClient, session: AsyncSession) -> None:
    """UNDO refuses to resurrect a terminal EXECUTED proposal to PENDING (phaze-uu17)."""
    proposal = await create_test_proposal(session, status=ProposalStatus.EXECUTED)
    response = await client.patch(f"/proposals/{proposal.id}/undo")
    assert response.status_code == 409

    updated = await session.get(RenameProposal, proposal.id)
    assert updated is not None
    assert updated.status == ProposalStatus.EXECUTED


@pytest.mark.asyncio
async def test_bulk_approve_skips_terminal_rows(client: AsyncClient, session: AsyncSession) -> None:
    """A bulk approve over a mix of PENDING + EXECUTED rows only transitions the PENDING one (phaze-uu17)."""
    pending = await create_test_proposal(session, original_filename="p.mp3")
    executed = await create_test_proposal(session, original_filename="x.mp3", status=ProposalStatus.EXECUTED)

    response = await client.patch(
        "/proposals/bulk",
        data={"action": "approve", "proposal_ids": [str(pending.id), str(executed.id)]},
    )
    assert response.status_code == 200
    assert "1 proposals approved" in response.text

    assert (await session.get(RenameProposal, pending.id)).status == ProposalStatus.APPROVED
    assert (await session.get(RenameProposal, executed.id)).status == ProposalStatus.EXECUTED


@pytest.mark.asyncio
async def test_undo_pending_conflict_returns_409(client: AsyncClient, session: AsyncSession) -> None:
    """UNDO of an APPROVED proposal whose file already has a PENDING proposal returns 409 (phaze-uu17)."""
    # One file, two proposals: an existing PENDING and an APPROVED one we try to revert.
    file_id = uuid.uuid4()
    file_record = FileRecord(
        agent_id="test-fileserver",
        id=file_id,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/music/{uuid.uuid4().hex}/dup.mp3",
        original_filename="dup.mp3",
        current_path="/music/dup.mp3",
        file_type="music",
        file_size=1_000_000,
    )
    session.add(file_record)
    await session.flush()
    session.add(RenameProposal(id=uuid.uuid4(), file_id=file_id, proposed_filename="Pending.mp3", status=ProposalStatus.PENDING))
    approved = RenameProposal(id=uuid.uuid4(), file_id=file_id, proposed_filename="Approved.mp3", status=ProposalStatus.APPROVED)
    session.add(approved)
    await session.commit()

    response = await client.patch(f"/proposals/{approved.id}/undo")
    # The pending-unique IntegrityError is translated to a 409 rather than a 500 (phaze-uu17). The
    # in-request rollback leaves the approved row unchanged; we assert only the status code here
    # because the router shares this test's session and re-querying it post-rollback is unreliable.
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# v7 diff-row workspace negotiation on the mutation routes (phaze-3a2j)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_from_v7_workspace_returns_diff_row(client: AsyncClient, session: AsyncSession) -> None:
    """Approving from a v7 workspace returns the styled _diff_row.html, not the legacy <tr> (phaze-3a2j)."""
    proposal = await create_test_proposal(session)
    response = await client.patch(
        f"/proposals/{proposal.id}/approve",
        headers={"HX-Request": "true", "HX-Target": f"rename-row-{proposal.id}"},
    )
    assert response.status_code == 200
    body = response.text
    assert f'id="rename-row-{proposal.id}"' in body
    assert "approved" in body
    assert "UNDO" in body
    # The legacy proposals <tr> markup (checkbox/bulk row) must NOT be present.
    assert 'id="proposal-' not in body


@pytest.mark.asyncio
async def test_reject_from_v7_move_workspace_returns_skipped_diff_row(client: AsyncClient, session: AsyncSession) -> None:
    """Rejecting from the Move workspace returns a skipped diff-row keyed to move-row (phaze-3a2j)."""
    proposal = await create_test_proposal(session, proposed_path="performances/A")
    response = await client.patch(
        f"/proposals/{proposal.id}/reject",
        headers={"HX-Request": "true", "HX-Target": f"move-row-{proposal.id}"},
    )
    assert response.status_code == 200
    body = response.text
    assert f'id="move-row-{proposal.id}"' in body
    assert "skipped" in body


@pytest.mark.asyncio
async def test_edit_from_v7_workspace_returns_diff_row(client: AsyncClient, session: AsyncSession) -> None:
    """Editing from a v7 workspace returns the pending diff-row with the new value (phaze-3a2j)."""
    proposal = await create_test_proposal(session)
    response = await client.patch(
        f"/proposals/{proposal.id}/edit",
        data={"proposed": "Edited Name.mp3", "facet": "filename"},
        headers={"HX-Request": "true", "HX-Target": f"rename-row-{proposal.id}"},
    )
    assert response.status_code == 200
    body = response.text
    assert f'id="rename-row-{proposal.id}"' in body
    assert "Edited Name.mp3" in body
    assert "APPROVE" in body  # still pending -> action cluster present


@pytest.mark.asyncio
async def test_edit_on_approved_proposal_returns_409(client: AsyncClient, session: AsyncSession) -> None:
    """An edit that lands after approval is refused with 409 and does NOT redirect the approved move (phaze-3tj4).

    Before the guard, edit_proposal wrote proposed_path unconditionally, so a stale edit tab could
    rewrite the destination an APPROVED row feeds into execution_dispatch — moving the file somewhere
    that was never reviewed. The edit is now gated to PENDING rows.
    """
    proposal = await create_test_proposal(session, status=ProposalStatus.APPROVED, proposed_path="performances/Reviewed")
    response = await client.patch(
        f"/proposals/{proposal.id}/edit",
        data={"proposed": "performances/Unreviewed", "facet": "path"},
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_edit_on_pending_proposal_succeeds(client: AsyncClient, session: AsyncSession) -> None:
    """The edit path still works for a PENDING proposal (the legal, pre-approve case)."""
    proposal = await create_test_proposal(session, status=ProposalStatus.PENDING)
    response = await client.patch(
        f"/proposals/{proposal.id}/edit",
        data={"proposed": "Renamed Track.mp3", "facet": "filename"},
    )
    assert response.status_code == 200
    assert "Renamed Track.mp3" in response.text


@pytest.mark.asyncio
async def test_approve_without_hx_target_returns_legacy_response(client: AsyncClient, session: AsyncSession) -> None:
    """The legacy proposals list (no v7 HX-Target) still gets approve_response.html with the stats-bar (phaze-3a2j)."""
    proposal = await create_test_proposal(session)
    response = await client.patch(f"/proposals/{proposal.id}/approve")
    assert response.status_code == 200
    assert "stats-bar" in response.text


# ---------------------------------------------------------------------------
# bulk-approve-high-confidence row sync for the v7 Rename/Move workspaces (phaze-71hi)
#
# rename_workspace.html / move_workspace.html hx-target this endpoint at their own tiny
# #rename-trigger-response / #move-trigger-response status div, and the workspaces run no row poll
# (R-2) to pick a change up on their own. Before this fix, every row the predicate approved stayed
# rendered PENDING with live APPROVE/EDIT/SKIP controls; a subsequent click 409'd silently.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_approve_high_confidence_from_rename_workspace_syncs_approved_row(client: AsyncClient, session: AsyncSession) -> None:
    """From the Rename workspace, the response OOB-swaps the transitioned row to row_state=approved."""
    high = await create_test_proposal(session, confidence=0.95, original_filename="high.mp3")
    low = await create_test_proposal(session, confidence=0.5, original_filename="low.mp3")

    response = await client.patch(
        "/proposals/bulk-approve-high-confidence",
        headers={"HX-Request": "true", "HX-Target": "rename-trigger-response"},
    )
    assert response.status_code == 200
    body = response.text

    high_row = await session.get(RenameProposal, high.id)
    assert high_row is not None
    assert high_row.status == ProposalStatus.APPROVED.value

    # The transitioned row is OOB-swapped in place, keyed to the rename-row prefix, showing the
    # approved lifecycle affordances -- not left with live APPROVE/EDIT/SKIP controls (phaze-71hi).
    assert f'id="rename-row-{high.id}"' in body
    assert 'hx-swap-oob="true"' in body
    assert "approved" in body
    assert "UNDO" in body

    # The still-pending row (confidence below threshold) must NOT appear in the response at all --
    # it was never transitioned, so no OOB fragment is needed for it.
    assert f"rename-row-{low.id}" not in body


@pytest.mark.asyncio
async def test_bulk_approve_high_confidence_from_move_workspace_syncs_approved_row(client: AsyncClient, session: AsyncSession) -> None:
    """From the Move workspace, the OOB row fragment uses the move-row prefix and path facet."""
    high = await create_test_proposal(session, confidence=0.95, proposed_path="performances/A", original_filename="high.mp3")

    response = await client.patch(
        "/proposals/bulk-approve-high-confidence",
        headers={"HX-Request": "true", "HX-Target": "move-trigger-response"},
    )
    assert response.status_code == 200
    body = response.text

    assert f'id="move-row-{high.id}"' in body
    assert 'hx-swap-oob="true"' in body
    assert "approved" in body
    assert (await session.get(RenameProposal, high.id)).status == ProposalStatus.APPROVED.value


@pytest.mark.asyncio
async def test_bulk_approve_high_confidence_v7_response_omits_nonexistent_stats_bar(client: AsyncClient, session: AsyncSession) -> None:
    """The v7 response must not OOB-target #stats-bar -- it does not exist in the v7 shell (phaze-71hi)."""
    await create_test_proposal(session, confidence=0.95)
    response = await client.patch(
        "/proposals/bulk-approve-high-confidence",
        headers={"HX-Request": "true", "HX-Target": "rename-trigger-response"},
    )
    assert response.status_code == 200
    assert "stats-bar" not in response.text


@pytest.mark.asyncio
async def test_bulk_approve_high_confidence_zero_matches_returns_toast_with_no_oob_rows(client: AsyncClient, session: AsyncSession) -> None:
    """No pending row meets the predicate: a toast is returned but no OOB row fragment is emitted."""
    await create_test_proposal(session, confidence=0.5)
    response = await client.patch(
        "/proposals/bulk-approve-high-confidence",
        headers={"HX-Request": "true", "HX-Target": "rename-trigger-response"},
    )
    assert response.status_code == 200
    assert "Nothing matched" in response.text
    assert 'hx-swap-oob="true"' not in response.text


@pytest.mark.asyncio
async def test_bulk_approve_high_confidence_without_v7_target_returns_legacy_response(client: AsyncClient, session: AsyncSession) -> None:
    """The legacy proposals list (no v7 HX-Target) keeps the original approve_response.html shape."""
    await create_test_proposal(session, confidence=0.95)
    response = await client.patch("/proposals/bulk-approve-high-confidence")
    assert response.status_code == 200
    assert "stats-bar" in response.text


# ---------------------------------------------------------------------------
# GET /proposals/ history-restore response shape (phaze-64uy)
#
# proposals/partials/filter_tabs.html and proposals/partials/pagination.html BOTH set
# hx-push-url="true" on their /proposals/?... controls, so those URLs enter browser history. A Back
# with the snapshot evicted from htmx's 10-entry cache re-fetches the URL carrying BOTH
# HX-Request: true and HX-History-Restore-Request: true -- and on a restore htmx IGNORES hx-target
# and swaps into <body>. The handler branched on the raw HX-Request header, so it answered with the
# chrome-less proposal_list.html fragment and REPLACED THE WHOLE DOCUMENT with it (the dispatcher
# measured /proposals/?status=pending at 3707 bytes, full_document=False).
#
# response_shape.py rule 1 bans that raw check; wants_fragment is the predicate.
# ---------------------------------------------------------------------------


_RESTORE_HEADERS = {"HX-Request": "true", "HX-History-Restore-Request": "true"}


@pytest.mark.asyncio
async def test_proposals_history_restore_does_not_return_a_fragment(client: AsyncClient) -> None:
    """A history-restore GET /proposals/ falls through to the shell redirect, not the fragment.

    Asserts the SHAPE, not merely a 200 -- the buggy handler answered this with a 200 fragment,
    so a status-only assertion would pass against the bug.
    """
    response = await client.get("/proposals/?status=pending", headers=_RESTORE_HEADERS)
    assert response.status_code == 302, "a restore must not be answered with a chrome-less 200 fragment"
    assert response.headers["location"] == "/s/propose"


@pytest.mark.asyncio
async def test_proposals_history_restore_resolves_to_the_full_shell(client: AsyncClient) -> None:
    """Following that redirect yields a FULL document with the shell chrome intact.

    The end-to-end guarantee the operator actually experiences: press Back, land on a real page
    with navigation, rather than on a bare proposal table with no way out but a manual reload.
    Note the redirect is followed WITH the restore headers still set, so this also pins the
    dependency on shell.py answering a restore with the full shell (phaze-64uy fixes both).
    """
    response = await client.get("/proposals/?status=pending", headers=_RESTORE_HEADERS, follow_redirects=True)
    assert response.status_code == 200
    body = response.text
    assert "<html" in body.lower(), "a history restore must resolve to a full document"
    assert 'aria-label="Pipeline navigation"' in body, "the rail must be present after a restore"
    assert 'id="stage-workspace"' in body, "the shell's swap target must be present after a restore"


@pytest.mark.asyncio
async def test_proposals_live_htmx_swap_still_returns_the_fragment(client: AsyncClient) -> None:
    """The other direction: an ordinary htmx swap must still get the chrome-less fragment.

    Every control that issues this GET targets #proposal-list-container with hx-swap="innerHTML"
    (phaze-7j50), so a full document here would nest an entire page inside the container.
    """
    response = await client.get("/proposals/?status=pending", headers={"HX-Request": "true"})
    assert response.status_code == 200
    body = response.text
    assert "<html" not in body.lower(), "a live htmx swap must get a fragment, not a full document"
    assert 'aria-label="Pipeline navigation"' not in body, "the fragment must not carry the shell rail"


@pytest.mark.asyncio
async def test_proposals_restore_header_alone_does_not_return_a_fragment(client: AsyncClient) -> None:
    """The restore header dominates even without ``HX-Request`` (response_shape rule 2)."""
    response = await client.get("/proposals/", headers={"HX-History-Restore-Request": "true"})
    assert response.status_code == 302
    assert response.headers["location"] == "/s/propose"
