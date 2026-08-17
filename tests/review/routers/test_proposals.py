"""Integration tests for proposal approval workflow UI endpoints."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING
import uuid

from bs4 import BeautifulSoup
import pytest
from sqlalchemy import update

import phaze
from phaze.models.analysis import AnalysisWindow
from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.routers.proposals import BpmSpark, _bpm_spark
from phaze.services.proposal_queries import proposal_review_digest


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
    context_used: dict[str, object] | None = None,
) -> RenameProposal:
    """Create a FileRecord + RenameProposal pair for testing.

    ``context_used`` defaults to the plain artist/event_name shape every pre-phaze-5fta.6 test
    relies on; pass an override to exercise the ``date_convention`` provenance key (phaze-5fta.6).
    """
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
        context_used=context_used if context_used is not None else {"artist": "Test Artist", "event_name": "Test Event"},
        reason=reason,
    )
    session.add(proposal)
    await session.commit()
    proposal.file = file_record
    return proposal


def _review_token(proposal: RenameProposal) -> str:
    return f"{proposal.id}|{proposal.updated_at.isoformat()}|{proposal_review_digest(proposal)}"


_TEMPLATES_DIR = Path(phaze.__file__).parent / "templates"


def test_no_template_reads_the_removed_file_state_column() -> None:
    """Source-scan (D-04 / Pitfall 4): no template reads ``file.state``; Phase 90 dropped that column.

    phaze-vvmh generalised this. It used to scan exactly one file, ``proposals/partials/proposal_row.html``,
    asserting that its status badge read ``proposal.status`` rather than ``proposal.file.state``. That
    template is now deleted -- it was rendered only by ``edit_proposal``'s legacy fork, itself
    unreachable because every live edit control is a ``_diff_row.html`` naming a v7 row target -- so the
    single-file scan would have to go with it. Scanning the whole tree keeps the guarantee the test
    existed for (nothing renders the removed column) and widens it, instead of deleting coverage.
    """
    offenders = [
        path.relative_to(_TEMPLATES_DIR).as_posix()
        for path in sorted(_TEMPLATES_DIR.rglob("*.html"))
        if "file.state" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"template(s) still read the removed files.state column: {offenders}"


@pytest.mark.asyncio
async def test_approve_proposal(client: AsyncClient, session: AsyncSession) -> None:
    """PATCH /proposals/{id}/approve returns 200, updates DB status, answers with the approved row.

    phaze-vvmh: the response used to be ``approve_response.html``, whose payload was an OOB fragment
    aimed at ``#stats-bar`` -- an id the v7 cutover deleted, so the browser discarded it. Every
    mutation route now has ONE response shape, the shared ``_diff_row.html``.
    """
    proposal = await create_test_proposal(session)
    response = await client.patch(f"/proposals/{proposal.id}/approve", data={"expected_updated_at": proposal.updated_at.isoformat()})
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "stats-bar" not in response.text
    assert f'id="rename-row-{proposal.id}"' in response.text

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
    response = await client.patch(f"/proposals/{random_id}/approve", data={"expected_updated_at": "2026-01-01T00:00:00+00:00"})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# approve optimistic-concurrency token (phaze-exivg)
#
# update_proposal_status's allowed_from guard folds only the from-STATUS into its conditional
# UPDATE's WHERE clause. store_proposals is an idempotent upsert that overwrites a still-PENDING
# row's content in place (same id, same status, new proposed_filename/proposed_path/confidence) on
# a replayed generate_proposals (a SAQ retry, or recover_orphaned_work's verbatim ledger replay) --
# a row swapped between the operator's page render and their Approve click still satisfies that
# status-only WHERE. These exercise the fix end to end: the APPROVE route now takes the row's
# render-time updated_at as expected_updated_at and refuses when it no longer matches.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_proposal_stale_token_refused(client: AsyncClient, session: AsyncSession) -> None:
    """An approve carrying a stale updated_at token is refused, and the concurrently swapped-in
    (unreviewed) content is never approved.
    """
    proposal = await create_test_proposal(session, proposed_filename="Artist - Track A.mp3")
    stale_token = proposal.updated_at.isoformat()

    # Simulate the concurrent store_proposals upsert: same id, same PENDING status, new content and
    # a bumped updated_at -- exactly the TOCTOU window the render-time token is meant to catch.
    # Bumped explicitly (rather than relying on onupdate=func.now()) because Postgres' now() is
    # constant for the whole surrounding transaction, and this test's session fixture wraps
    # everything in one.
    await session.execute(
        update(RenameProposal)
        .where(RenameProposal.id == proposal.id)
        .values(proposed_filename="Artist - Track B (unreviewed).mp3", updated_at=proposal.updated_at + timedelta(seconds=5))
    )
    await session.commit()
    await session.refresh(proposal)
    assert proposal.updated_at.isoformat() != stale_token

    response = await client.patch(f"/proposals/{proposal.id}/approve", data={"expected_updated_at": stale_token})
    assert response.status_code == 409

    refetched = await session.get(RenameProposal, proposal.id)
    assert refetched is not None
    assert refetched.status == ProposalStatus.PENDING
    assert refetched.proposed_filename == "Artist - Track B (unreviewed).mp3"


@pytest.mark.asyncio
async def test_approve_proposal_current_token_succeeds(client: AsyncClient, session: AsyncSession) -> None:
    """A token matching the row's live updated_at (the non-race path) approves normally."""
    proposal = await create_test_proposal(session)
    token = proposal.updated_at.isoformat()

    response = await client.patch(f"/proposals/{proposal.id}/approve", data={"expected_updated_at": token})
    assert response.status_code == 200

    updated = await session.get(RenameProposal, proposal.id)
    assert updated is not None
    assert updated.status == ProposalStatus.APPROVED


@pytest.mark.asyncio
async def test_approve_proposal_without_review_token_is_rejected(client: AsyncClient, session: AsyncSession) -> None:
    """A bare API PATCH cannot bypass the version reviewed by the operator."""
    proposal = await create_test_proposal(session)
    response = await client.patch(f"/proposals/{proposal.id}/approve")
    assert response.status_code == 400

    updated = await session.get(RenameProposal, proposal.id)
    assert updated is not None
    assert updated.status == ProposalStatus.PENDING


@pytest.mark.asyncio
async def test_rename_workspace_approve_button_carries_updated_at_token(client: AsyncClient, session: AsyncSession) -> None:
    """The Rename workspace's rendered APPROVE button round-trips the row's updated_at via hx-vals
    (phaze-exivg), so a real browser click actually supplies the guard's token.
    """
    proposal = await create_test_proposal(session)
    response = await client.get("/s/rename")
    assert response.status_code == 200
    assert f'"expected_updated_at": "{proposal.updated_at.isoformat()}"' in response.text


@pytest.mark.asyncio
async def test_row_detail(client: AsyncClient, session: AsyncSession) -> None:
    """GET /proposals/{id}/detail returns 200 with AI Reasoning text."""
    proposal = await create_test_proposal(session, reason="Because the artist name matches Coachella lineup")
    response = await client.get(f"/proposals/{proposal.id}/detail")
    assert response.status_code == 200
    assert "AI Reasoning" in response.text
    assert "Because the artist name matches Coachella lineup" in response.text


def _date_dd_text(html: str) -> str:
    """Extract the normalized text of the "Date:" row's ``<dd>`` from a row_detail response.

    Normalizing (collapsing all whitespace runs to single spaces, stripping the ends) makes the
    assertion robust to the template's own indentation -- HTML whitespace inside a block element is
    not visually significant, so this is the right notion of "unchanged" for a browser-rendered
    comparison, as opposed to a literal byte diff of the response body.
    """
    soup = BeautifulSoup(html, "html.parser")
    for dt in soup.find_all("dt"):
        if dt.get_text(strip=True) == "Date:":
            dd = dt.find_next_sibling("dd")
            assert dd is not None
            return " ".join(dd.get_text(separator=" ").split())
    raise AssertionError("no 'Date:' row found in row_detail response")


@pytest.mark.asyncio
async def test_row_detail_convention_derived_date_shows_provenance(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-5fta.6: a convention-derived date renders the convention, direction and evidence counts.

    ``date_convention`` here is exactly the persisted ``DateProvenance.as_dict()`` shape written by
    ``phaze.services.date_convention.resolve_date`` (phaze-5fta.4) for ``source=SOURCE_CONVENTION`` --
    this test constructs it explicitly rather than exercising the flag end-to-end because the flag
    defaults off (see module docstring on ``date_convention.py``), so no live proposal carries this
    key today.
    """
    proposal = await create_test_proposal(
        session,
        context_used={
            "artist": "Test Artist",
            "event_name": "Test Event",
            "date": "2014-05-04",
            "date_convention": {
                "date": "2014-05-04",
                "raw": "04-05-2014",
                "date_order": "MM-DD",
                "source": "release_group_convention",
                "scope": "release_group",
                "scope_value": "talion",
                "convention_kind": "date_order",
                "convention_value": "MM-DD",
                "convention_id": str(uuid.uuid4()),
                "supporting_count": 1373,
                "contradicting_count": 0,
                "ambiguous_count": 753,
                "confidence": 1.0,
                "computed_at": "2026-07-18T00:00:00+00:00",
            },
        },
    )
    response = await client.get(f"/proposals/{proposal.id}/detail")
    assert response.status_code == 200
    assert _date_dd_text(response.text) == "2014-05-04 date inferred from release-group convention (MM-DD — 1,373 supporting, 0 contradicting)"


@pytest.mark.asyncio
async def test_row_detail_self_resolved_date_is_unchanged(client: AsyncClient, session: AsyncSession) -> None:
    """A self-resolving date (``source=filename``) never shows convention provenance.

    Exercises the case introduced by enabling ``convention_date_fallback_enabled``:
    ``annotate_date_conventions`` attaches a ``date_convention`` key to EVERY resolved date, self-
    resolving ones included, tagged ``source="filename"``. The UI must key off ``source``, not mere
    presence of the ``date_convention`` dict, or a self-resolving proposal would grow a spurious
    "inferred" note the moment the flag is enabled.
    """
    proposal = await create_test_proposal(
        session,
        context_used={
            "artist": "Test Artist",
            "date": "2014-04-25",
            "date_convention": {
                "date": "2014-04-25",
                "raw": "04-25-2014",
                "date_order": "MM-DD",
                "source": "filename",
                "scope": None,
                "scope_value": None,
                "convention_kind": None,
                "convention_value": None,
                "convention_id": None,
                "supporting_count": None,
                "contradicting_count": None,
                "ambiguous_count": None,
                "confidence": None,
                "computed_at": None,
            },
        },
    )
    response = await client.get(f"/proposals/{proposal.id}/detail")
    assert response.status_code == 200
    assert _date_dd_text(response.text) == "2014-04-25"
    assert "date inferred from release-group convention" not in response.text
    assert "date-convention-provenance" not in response.text


@pytest.mark.asyncio
async def test_row_detail_no_date_convention_key_is_unchanged(client: AsyncClient, session: AsyncSession) -> None:
    """The unresolved shape -- no ``date_convention`` key at all -- renders a bare date.

    This is the byte-for-byte-equivalent case the epic's gated rollout guarantees. It is reached
    whenever the fallback did not resolve the date: ``convention_date_fallback_enabled`` set false,
    or on (the default since 2026-08-04) but with no group clearing the evidence and purity bars.
    ``context_used`` then carries no ``date_convention`` and this row_detail view must be
    indistinguishable from before phaze-5fta.6.
    """
    proposal = await create_test_proposal(
        session,
        context_used={"artist": "Test Artist", "date": "2014-04-25"},
    )
    response = await client.get(f"/proposals/{proposal.id}/detail")
    assert response.status_code == 200
    assert _date_dd_text(response.text) == "2014-04-25"
    assert "date inferred from release-group convention" not in response.text
    assert "date-convention-provenance" not in response.text


@pytest.mark.asyncio
async def test_row_detail_no_date_key_shows_no_metadata_row_for_date(client: AsyncClient, session: AsyncSession) -> None:
    """A proposal whose ``context_used`` has no ``date`` at all renders no Date: row (unchanged)."""
    proposal = await create_test_proposal(session)  # default context_used has no "date" key
    response = await client.get(f"/proposals/{proposal.id}/detail")
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    labels = {dt.get_text(strip=True) for dt in soup.find_all("dt")}
    assert "Date:" not in labels


@pytest.mark.asyncio
async def test_bulk_approve(client: AsyncClient, session: AsyncSession) -> None:
    """PATCH /proposals/bulk with action=approve updates all listed proposals."""
    p1 = await create_test_proposal(session, original_filename="bulk1.mp3", proposed_filename="Bulk One.mp3", confidence=0.95)
    p2 = await create_test_proposal(session, original_filename="bulk2.mp3", proposed_filename="Bulk Two.mp3", confidence=0.95)

    response = await client.patch(
        "/proposals/bulk",
        data={"action": "approve_eligible", "review_tokens": [_review_token(p1), _review_token(p2)]},
    )
    assert response.status_code == 200

    updated1 = await session.get(RenameProposal, p1.id)
    updated2 = await session.get(RenameProposal, p2.id)
    assert updated1 is not None
    assert updated1.status == ProposalStatus.APPROVED
    assert updated2 is not None
    assert updated2.status == ProposalStatus.APPROVED


@pytest.mark.asyncio
async def test_bulk_approve_skips_malformed_id(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-3st0: a malformed proposal_ids entry is SKIPPED (never a 500); valid ids still act."""
    p1 = await create_test_proposal(session, original_filename="bulkmal1.mp3", confidence=0.95)

    response = await client.patch(
        "/proposals/bulk",
        data={"action": "approve_eligible", "review_tokens": [_review_token(p1), "not-a-token"]},
    )
    assert response.status_code == 200
    assert "1 proposal approved." in response.text

    updated1 = await session.get(RenameProposal, p1.id)
    assert updated1 is not None
    assert updated1.status == ProposalStatus.APPROVED


@pytest.mark.asyncio
async def test_bulk_approve_skips_empty_id(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-3st0: an empty-string proposal_ids entry is SKIPPED (never a 500)."""
    p1 = await create_test_proposal(session, original_filename="bulkmal2.mp3", confidence=0.95)

    response = await client.patch(
        "/proposals/bulk",
        data={"action": "approve_eligible", "review_tokens": [_review_token(p1), ""]},
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
        data={"action": "approve_eligible", "review_tokens": ["not-a-token", ""]},
    )
    assert response.status_code == 200
    assert "0 proposals approved." in response.text


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
    # phaze-3yop: _bulk_toast's old `f"{action}d"` concatenation misspelled the reject toast as
    # "2 proposals rejectd." -- this response-text assertion is what test_bulk_reject was missing;
    # without it, a regression to the concatenation shape would pass every status-column check here
    # and still ship a misspelled verb on the operator's only confirmation of a bulk reject.
    assert "2 proposals rejected." in response.text
    assert "rejectd" not in response.text

    updated1 = await session.get(RenameProposal, p1.id)
    updated2 = await session.get(RenameProposal, p2.id)
    assert updated1 is not None
    assert updated1.status == ProposalStatus.REJECTED
    assert updated2 is not None
    assert updated2.status == ProposalStatus.REJECTED


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
# phaze-w55w1: the Phase 44 "Sampled" badge and "Deepen analysis" button are GONE
#
# The three tests here previously pinned the badge's render-if-sampled behaviour and its
# NULL/false no-op. They are replaced by ONE test asserting the whole surface is absent,
# because there is no longer a sampled state to render: every file is analyzed exhaustively
# (ADR-0007 §7), the `analysis.sampled` column is dropped (migration 060), and the timeline
# route no longer fetches AnalysisResult at all.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeline_has_no_sampled_badge_or_deepen_action(client: AsyncClient, session: AsyncSession) -> None:
    """The timeline renders windows only -- no sampled pill, no Deepen button, no /deepen link.

    A dead `hx-post` to a removed route is worse than a missing button: it renders fine and
    fails on click, so the absence is asserted rather than assumed.
    """
    proposal = await create_test_proposal(session)
    await add_analysis_windows(session, proposal.file_id)

    response = await client.get(f"/proposals/{proposal.id}/timeline")

    assert response.status_code == 200
    assert "Sampled" not in response.text
    assert "Deepen" not in response.text
    assert "/deepen" not in response.text
    # The windows themselves still render -- this is a removal, not a regression.
    assert "BPM (fine windows)" in response.text

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
    response = await client.patch(f"/proposals/{proposal.id}/approve", data={"expected_updated_at": proposal.updated_at.isoformat()})
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
    pending = await create_test_proposal(session, original_filename="p.mp3", confidence=0.95)
    executed = await create_test_proposal(session, original_filename="x.mp3", status=ProposalStatus.EXECUTED)

    response = await client.patch(
        "/proposals/bulk",
        data={"action": "approve_eligible", "review_tokens": [_review_token(pending), _review_token(executed)]},
    )
    assert response.status_code == 200
    assert "1 proposal approved · 1 skipped (already actioned)." in response.text

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
        data={"expected_updated_at": proposal.updated_at.isoformat()},
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
    # phaze-3mru: the detail names the refusal, not a self-contradictory "approved -> approved".
    assert response.json()["detail"] == "edit refused: proposal is approved, only PENDING rows are editable"


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
async def test_approve_without_hx_target_returns_the_shared_diff_row(client: AsyncClient, session: AsyncSession) -> None:
    """A request naming no v7 row gets the SAME _diff_row.html shape, not a second legacy shape.

    phaze-vvmh deleted the fallback. It rendered ``approve_response.html`` -> ``proposal_row.html``
    (a ``<tr>``, which the v7 workspaces mount into a ``<div>`` list) plus an OOB fragment addressed
    to the deleted ``#stats-bar`` -- a response that was both unreachable and wrong. There is no
    legacy list left for it to serve, and keeping it was what held the dead Execute Approved button's
    host chain reachable.
    """
    proposal = await create_test_proposal(session)
    response = await client.patch(f"/proposals/{proposal.id}/approve", data={"expected_updated_at": proposal.updated_at.isoformat()})
    assert response.status_code == 200
    assert "stats-bar" not in response.text
    assert f'id="rename-row-{proposal.id}"' in response.text
    assert "<tr" not in response.text, "the legacy table-row shape is back"


# ---------------------------------------------------------------------------
# bulk-approve-high-confidence row sync for the v7 Rename/Move workspaces (phaze-71hi)
#
# rename_workspace.html / move_workspace.html hx-targeted this endpoint at their own tiny
# #rename-trigger-response / #move-trigger-response status div, and the workspaces ran no row poll
# (R-2) to pick a change up on their own. Before this fix, every row the predicate approved stayed
# rendered PENDING with live APPROVE/EDIT/SKIP controls; a subsequent click 409'd silently.
#
# phaze-tzy6s.11 / ADR-0008: both of those workspaces were deleted when rename, destination, and tag
# decisions were consolidated into Changes Review, which bulk-approves through the selection-driven
# PATCH /proposals/bulk instead. NO template calls /proposals/bulk-approve-high-confidence any more,
# so the tests below are currently the route's only callers -- they hold the server contract, not a
# live UI path. Bead phaze-tzy6s.12 (Execute preflight) owns the keep-or-retire decision; see
# routers/proposals.py::_BULK_HIGH_CONFIDENCE_TARGETS.
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
async def test_bulk_approve_high_confidence_pluralizes_a_single_v7_approval(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-zbgi9: exactly one pending proposal clears the predicate -- the toast must read
    '1 proposal approved.', not '1 proposals approved.' (_bulk_toast's own pluralization, reused
    here rather than a second hand-rolled string). Covers the v7-target response path; the
    no-target path is covered by ``test_bulk_approve_high_confidence_without_v7_target_returns_the_toast_alone``.
    """
    await create_test_proposal(session, confidence=0.95)
    response = await client.patch(
        "/proposals/bulk-approve-high-confidence",
        headers={"HX-Request": "true", "HX-Target": "rename-trigger-response"},
    )
    assert response.status_code == 200
    assert "1 proposal approved." in response.text
    assert "1 proposals approved." not in response.text


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
async def test_bulk_approve_high_confidence_without_v7_target_returns_the_toast_alone(client: AsyncClient, session: AsyncSession) -> None:
    """A caller naming neither workspace status div gets the toast and NO OOB rows -- never a stats-bar.

    phaze-vvmh: the deleted fallback rendered ``approve_response.html`` with ``proposal=None``, i.e.
    a response whose entire payload was an OOB fragment aimed at an id no served document contains.
    There is no row prefix to key OOB rows to here, so the honest answer is the toast by itself.
    """
    await create_test_proposal(session, confidence=0.95)
    response = await client.patch("/proposals/bulk-approve-high-confidence")
    assert response.status_code == 200
    assert "stats-bar" not in response.text
    # phaze-zbgi9: singular, matching the module's own _bulk_toast pluralization -- was
    # "1 proposals approved." before the fix, which this test used to pin rather than catch.
    assert "1 proposal approved." in response.text
    assert 'hx-swap-oob="true"' not in response.text


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
async def test_proposals_redirects_with_query_params_and_hx_request(client: AsyncClient) -> None:
    """phaze-y4s6: an ordinary htmx GET (with query params) also redirects unconditionally now.

    The legacy in-page HX filter/sort/search surface this control used to swap into
    ``#proposal-list-container`` (``proposal_table.html``/``pagination.html``/``bulk_actions.html``/
    ``proposal_list.html``) had no live caller left post-v7-cutover and was deleted outright.
    """
    response = await client.get("/proposals/?status=pending", headers={"HX-Request": "true"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/propose"


@pytest.mark.asyncio
async def test_proposals_restore_header_alone_does_not_return_a_fragment(client: AsyncClient) -> None:
    """The restore header dominates even without ``HX-Request`` (response_shape rule 2)."""
    response = await client.get("/proposals/", headers={"HX-History-Restore-Request": "true"})
    assert response.status_code == 302
    assert response.headers["location"] == "/s/propose"
