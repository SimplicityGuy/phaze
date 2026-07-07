"""Integration tests for proposal approval workflow UI endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.analysis import AnalysisResult, AnalysisWindow
from phaze.models.file import FileRecord, FileState
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
        id=file_id,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/music/{uuid.uuid4().hex}/{original_filename}",
        original_filename=original_filename,
        current_path=f"/music/{original_filename}",
        file_type="music",
        file_size=1_000_000,
        state=FileState.PROPOSAL_GENERATED,
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
    in-page HX filter request returns the proposals content partial (filter tabs + table).
    """
    await create_test_proposal(session)
    response = await client.get("/proposals/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'aria-label="Status filter tabs"' in response.text


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
