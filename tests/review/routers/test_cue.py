"""Integration tests for CUE management UI endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch
import uuid

import pytest
from sqlalchemy import select

from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion


if TYPE_CHECKING:
    from pathlib import Path

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


async def _create_approved_tracklist_with_file(
    session: AsyncSession,
    *,
    artist: str = "DJ Shadow",
    event: str = "Coachella 2024",
    applied: bool = True,
    with_timestamps: bool = True,
    track_count: int = 3,
    source: str = "1001tracklists",
) -> tuple[Tracklist, FileRecord]:
    """Create an approved tracklist with an applied file and timestamped tracks.

    READ-05/D-01: a file is "applied" iff an ``executed`` :class:`RenameProposal` exists for it,
    NOT ``file.state == 'executed'`` (no ``src/`` writer produces that state -- it was the whole
    reason the CUE gate was dead). The file is seeded with ``state='moved'`` (a real post-apply
    state) so these fixtures prove the CUE gate reads ``applied()`` (``proposals.status``), not
    ``files.state``. Pass ``applied=False`` to seed an ``approved`` (non-executed) proposal so the
    file is NOT applied (excluded from the eligible set / rejected by ``generate_cue``).
    """
    file_id = uuid.uuid4()
    file_record = FileRecord(
        agent_id="test-fileserver",
        id=file_id,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/music/{uuid.uuid4().hex}/{artist}.mp3",
        original_filename=f"{artist} - Live @ {event}.mp3",
        current_path=f"/dest/{artist} - Live @ {event}.mp3",
        file_type="mp3",
        file_size=50_000_000,
    )
    session.add(file_record)
    await session.flush()

    session.add(
        RenameProposal(
            id=uuid.uuid4(),
            file_id=file_id,
            proposed_filename=f"{artist} - Live @ {event}.mp3",
            status=ProposalStatus.EXECUTED if applied else ProposalStatus.APPROVED,
        )
    )

    tracklist_id = uuid.uuid4()
    version_id = uuid.uuid4()

    tracklist = Tracklist(
        id=tracklist_id,
        external_id=f"ext-{uuid.uuid4().hex[:8]}",
        source_url=f"https://www.1001tracklists.com/tracklist/{uuid.uuid4().hex[:6]}",
        file_id=file_id,
        match_confidence=95,
        artist=artist,
        event=event,
        latest_version_id=version_id,
        source=source,
        status="approved",
    )
    session.add(tracklist)

    version = TracklistVersion(
        id=version_id,
        tracklist_id=tracklist_id,
        version_number=1,
    )
    session.add(version)
    await session.flush()

    for i in range(1, track_count + 1):
        track = TracklistTrack(
            id=uuid.uuid4(),
            version_id=version_id,
            position=i,
            artist=f"Track Artist {i}",
            title=f"Track Title {i}",
            timestamp=f"0:{i * 10}:00" if with_timestamps else None,
        )
        session.add(track)

    await session.commit()
    return tracklist, file_record


@pytest.mark.asyncio
async def test_cue_list_full_page(client: AsyncClient, session: AsyncSession) -> None:
    """Phase 57 (SHELL-05): a plain GET /cue/ 302-redirects into the shell.

    The "CUE Sheets" heading + stats header + empty-state are full-page chrome on the cue
    workspace node (a Phase-57 placeholder; real content lands in 58-61). The in-page HX
    list partial stays usable (test_cue_list_htmx_partial covers it).
    """
    await _create_approved_tracklist_with_file(session)
    response = await client.get("/cue/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/cue"


@pytest.mark.asyncio
async def test_cue_list_htmx_partial(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-y4s6: GET /cue/ redirects unconditionally now, even with an HX-Request header.

    The in-page HX list/pagination fragment this used to preserve (``cue/partials/cue_list.html``)
    had no live caller left post-v7-cutover and was deleted outright -- unlike the sibling
    ``/proposals/`` redirect, there is no HX-filter branch here to keep working.
    """
    await _create_approved_tracklist_with_file(session)
    response = await client.get("/cue/", headers={"HX-Request": "true"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/cue"


@pytest.mark.asyncio
async def test_cue_list_empty_state(client: AsyncClient, session: AsyncSession) -> None:
    """Phase 57 (SHELL-05): the cue empty-state moved to the shell workspace node.

    The "No tracklists eligible for CUE generation" message is full-page chrome (the cue
    node is a Phase-57 placeholder), so a plain GET /cue/ now 302-redirects into the shell.
    """
    response = await client.get("/cue/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/cue"


@pytest.mark.asyncio
async def test_cue_list_stats(client: AsyncClient, session: AsyncSession) -> None:
    """Phase 57 (SHELL-05): the cue stats header moved to the shell workspace node.

    The "Eligible"/"Generated"/"Missing Timestamps" stats header is full-page chrome on the
    cue workspace node (a Phase-57 placeholder), so a plain GET /cue/ now 302-redirects into
    the shell.
    """
    await _create_approved_tracklist_with_file(session)
    response = await client.get("/cue/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/cue"


@pytest.mark.asyncio
async def test_generate_cue_success(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """POST /cue/{id}/generate with valid tracklist generates CUE file."""
    tracklist, file_record = await _create_approved_tracklist_with_file(session)

    # Use tmp_path for file paths
    audio_path = tmp_path / f"{file_record.original_filename}"
    audio_path.write_text("fake audio")
    file_record.current_path = str(audio_path)
    await session.commit()

    response = await client.post(f"/cue/{tracklist.id}/generate")
    assert response.status_code == 200
    assert "CUE file generated" in response.text or "toast-container" in response.text

    # Verify CUE file was written
    cue_path = audio_path.with_suffix(".cue")
    assert cue_path.exists()


@pytest.mark.asyncio
async def test_generate_cue_admits_applied_file_not_executed_state(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """SC#2 (READ-05/D-01): generate_cue ADMITS a file whose applied-ness comes from an executed proposal.

    The file's ``state`` is ``'moved'`` (NEVER ``'executed'`` -- no prod writer sets that), yet the CUE
    is written because ``is_applied`` reads ``proposals.status == 'executed'``. This is the behavior that
    was dead before READ-05 (the old ``state == EXECUTED`` guard rejected every real applied file).
    Mutation guard: revert ``generate_cue`` to a scalar executed-state check and this test goes RED
    (the fixture is applied via an executed proposal, not a scalar state).
    """
    tracklist, file_record = await _create_approved_tracklist_with_file(session, applied=True)

    audio_path = tmp_path / f"{file_record.original_filename}"
    audio_path.write_text("fake audio")
    file_record.current_path = str(audio_path)
    await session.commit()

    response = await client.post(f"/cue/{tracklist.id}/generate")
    assert response.status_code == 200
    assert "CUE file generated" in response.text or "toast-container" in response.text
    assert audio_path.with_suffix(".cue").exists()


@pytest.mark.asyncio
async def test_generate_cue_not_found(client: AsyncClient, session: AsyncSession) -> None:
    """POST /cue/{id}/generate with non-existent tracklist returns 404."""
    fake_id = uuid.uuid4()
    response = await client.post(f"/cue/{fake_id}/generate")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_generate_cue_file_not_applied(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """SC#2 (READ-05/D-01): a non-applied file (only an approved, non-executed proposal) is rejected.

    ``generate_cue`` now gates on ``is_applied`` (an executed proposal), not ``file.state`` -- a file
    that never got an executed proposal returns the "must be executed" toast even though it carries an
    approved proposal.
    """
    tracklist, file_record = await _create_approved_tracklist_with_file(session, applied=False)

    audio_path = tmp_path / f"{file_record.original_filename}"
    audio_path.write_text("fake audio")
    file_record.current_path = str(audio_path)
    await session.commit()

    response = await client.post(f"/cue/{tracklist.id}/generate")
    assert response.status_code == 200
    assert "executed" in response.text.lower() or "must be executed" in response.text.lower()


@pytest.mark.asyncio
async def test_generate_cue_no_timestamps(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """POST /cue/{id}/generate with no timestamps returns error toast."""
    tracklist, file_record = await _create_approved_tracklist_with_file(session, with_timestamps=False)

    audio_path = tmp_path / f"{file_record.original_filename}"
    audio_path.write_text("fake audio")
    file_record.current_path = str(audio_path)
    await session.commit()

    response = await client.post(f"/cue/{tracklist.id}/generate")
    assert response.status_code == 200
    assert "timestamps" in response.text.lower()


@pytest.mark.asyncio
async def test_generate_cue_unparseable_timestamp_returns_friendly_toast_not_500(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """phaze-97u7: a track whose timestamp is non-NULL but unparseable (e.g. scraped "") must
    route to the "No tracks have timestamps" toast, not an unhandled 500 from ValueError.
    """
    tracklist, file_record = await _create_approved_tracklist_with_file(session, track_count=1)

    # Force the single track's timestamp to an unparseable value the eligibility subquery still
    # admits (non-NULL), mirroring an empty scraped cue-time cell.
    track_result = await session.execute(select(TracklistTrack).where(TracklistTrack.version_id == tracklist.latest_version_id))
    track = track_result.scalars().one()
    track.timestamp = ""
    await session.commit()

    audio_path = tmp_path / file_record.original_filename
    audio_path.write_text("fake audio")
    file_record.current_path = str(audio_path)
    await session.commit()

    response = await client.post(f"/cue/{tracklist.id}/generate")
    assert response.status_code == 200
    assert "timestamps" in response.text.lower()


@pytest.mark.asyncio
async def test_generate_cue_regenerate_increments_version(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """POST /cue/{id}/generate twice creates versioned CUE files."""
    tracklist, file_record = await _create_approved_tracklist_with_file(session)

    audio_path = tmp_path / file_record.original_filename
    audio_path.write_text("fake audio")
    file_record.current_path = str(audio_path)
    await session.commit()

    # First generation
    response1 = await client.post(f"/cue/{tracklist.id}/generate")
    assert response1.status_code == 200
    assert audio_path.with_suffix(".cue").exists()

    # Second generation (regenerate)
    response2 = await client.post(f"/cue/{tracklist.id}/generate")
    assert response2.status_code == 200
    # Should have v2 file
    v2_path = audio_path.parent / f"{audio_path.stem}.v2.cue"
    assert v2_path.exists()


@pytest.mark.asyncio
async def test_generate_cue_not_approved(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """POST /cue/{id}/generate with non-approved tracklist returns error toast."""
    file_id = uuid.uuid4()
    file_record = FileRecord(
        agent_id="test-fileserver",
        id=file_id,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path="/music/test.mp3",
        original_filename="test.mp3",
        current_path=str(tmp_path / "test.mp3"),
        file_type="mp3",
        file_size=50_000_000,
    )
    session.add(file_record)
    session.add(RenameProposal(id=uuid.uuid4(), file_id=file_id, proposed_filename="test.mp3", status=ProposalStatus.EXECUTED))
    (tmp_path / "test.mp3").write_text("fake")

    tracklist = Tracklist(
        id=uuid.uuid4(),
        external_id=f"ext-{uuid.uuid4().hex[:8]}",
        source_url="https://example.com",
        file_id=file_id,
        artist="Test",
        latest_version_id=uuid.uuid4(),
        source="1001tracklists",
        status="proposed",  # Not approved
    )
    session.add(tracklist)
    await session.commit()

    response = await client.post(f"/cue/{tracklist.id}/generate")
    assert response.status_code == 200
    assert "approved" in response.text.lower()


@pytest.mark.asyncio
async def test_generate_cue_write_failure(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """POST /cue/{id}/generate with write failure returns error toast."""
    tracklist, file_record = await _create_approved_tracklist_with_file(session)

    audio_path = tmp_path / file_record.original_filename
    audio_path.write_text("fake audio")
    file_record.current_path = str(audio_path)
    await session.commit()

    with patch("phaze.routers.cue.write_cue_file", side_effect=OSError("Permission denied")):
        response = await client.post(f"/cue/{tracklist.id}/generate")
    assert response.status_code == 200
    assert "Failed to write CUE file" in response.text


@pytest.mark.asyncio
async def test_generate_cue_error_preserves_row_on_default_target(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """phaze-2w49 (row surface): an error response must NOT delete #cue-row-{id}.

    ``_render_error_toast`` used to return an OOB-only toast fragment at HTTP 200; htmx strips the
    OOB element before the primary ``outerHTML`` swap runs, so the (now-empty) fragment deletes the
    row via ``target.remove()``. The fixed response must re-render the row alongside the toast.
    """
    tracklist, file_record = await _create_approved_tracklist_with_file(session, applied=False)

    audio_path = tmp_path / f"{file_record.original_filename}"
    audio_path.write_text("fake audio")
    file_record.current_path = str(audio_path)
    await session.commit()

    response = await client.post(f"/cue/{tracklist.id}/generate")
    assert response.status_code == 200
    assert f'id="cue-row-{tracklist.id}"' in response.text, "the row must survive the error, not be deleted"
    assert "executed" in response.text.lower()


@pytest.mark.asyncio
async def test_generate_cue_error_preserves_preview_card_on_cue_card_target(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """phaze-2w49 (pipeline preview-card surface): HX-Target: cue-card-{id} must keep #cue-card-{id}.

    The write-failure branch is the live path for this surface (its own message expects a retry) --
    the OOB-only response used to delete the eligible card entirely, leaving no way to retry without
    a full refresh.
    """
    tracklist, file_record = await _create_approved_tracklist_with_file(session)

    audio_path = tmp_path / file_record.original_filename
    audio_path.write_text("fake audio")
    file_record.current_path = str(audio_path)
    await session.commit()

    with patch("phaze.routers.cue.write_cue_file", side_effect=OSError("Permission denied")):
        response = await client.post(
            f"/cue/{tracklist.id}/generate",
            headers={"HX-Target": f"cue-card-{tracklist.id}"},
        )
    assert response.status_code == 200
    assert f'id="cue-card-{tracklist.id}"' in response.text, "the preview card must survive the error, not be deleted"
    assert "Failed to write CUE file" in response.text


@pytest.mark.asyncio
async def test_generate_cue_buttons_guard_against_double_submit(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """phaze-efu0: every CUE generate/regenerate/APPROVE surface must carry ``hx-disabled-elt="this"``
    so a second click while the first POST is in flight is dropped client-side instead of writing a
    phantom ``.vN.cue`` version file.

    Covers both remaining CUE-generate surfaces: the legacy cue-page row (``cue_row.html``, default
    target) and the v7 cue workspace preview card (``_cue_preview.html``, ``cue-card-`` target).
    phaze-y4s6 removed the third, the tracklist card (``tracklist-`` target), along with the rest
    of the dead legacy tracklists UI.
    """
    tracklist, file_record = await _create_approved_tracklist_with_file(session)

    audio_path = tmp_path / file_record.original_filename
    audio_path.write_text("fake audio")
    file_record.current_path = str(audio_path)
    await session.commit()

    # cue_row.html (default / legacy cue-page surface).
    row_response = await client.post(f"/cue/{tracklist.id}/generate")
    assert row_response.status_code == 200
    assert 'hx-disabled-elt="this"' in row_response.text

    # _cue_preview.html (v7 cue-workspace APPROVE surface).
    preview_response = await client.post(
        f"/cue/{tracklist.id}/generate",
        headers={"HX-Target": f"cue-card-{tracklist.id}"},
    )
    assert preview_response.status_code == 200
    assert 'hx-disabled-elt="this"' in preview_response.text


@pytest.mark.asyncio
async def test_generate_cue_success_on_cue_card_target_returns_preview_card(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """phaze-js16: a SUCCESSFUL approve from the v7 cue workspace must re-render #cue-card-{id}
    with pipeline/partials/_cue_preview.html, not fall through to the legacy cue_row.html.

    Before the fix, the success path forked only on ``HX-Target`` starting with ``tracklist-`` and
    fell through to ``cue/partials/cue_row.html`` (root id ``cue-row-{id}``) for every other target
    -- including the v7 workspace card's ``cue-card-{id}``, swapping legacy markup into the v7 grid
    and losing the in-memory ``.cue`` preview.
    """
    tracklist, file_record = await _create_approved_tracklist_with_file(session)

    audio_path = tmp_path / file_record.original_filename
    audio_path.write_text("fake audio")
    file_record.current_path = str(audio_path)
    await session.commit()

    response = await client.post(
        f"/cue/{tracklist.id}/generate",
        headers={"HX-Target": f"cue-card-{tracklist.id}"},
    )
    assert response.status_code == 200
    assert f'id="cue-card-{tracklist.id}"' in response.text, "the v7 preview card id must survive a successful approve"
    assert f'id="cue-row-{tracklist.id}"' not in response.text, "the legacy cue_row.html markup must NOT be swapped in"
    assert "APPROVE" in response.text, "the eligible card's APPROVE control must still be present (still eligible post-write)"


@pytest.mark.asyncio
async def test_generate_cue_no_latest_version(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """POST /cue/{id}/generate with tracklist lacking latest_version_id returns error."""
    file_id = uuid.uuid4()
    file_record = FileRecord(
        agent_id="test-fileserver",
        id=file_id,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path="/music/test.mp3",
        original_filename="test.mp3",
        current_path=str(tmp_path / "test.mp3"),
        file_type="mp3",
        file_size=50_000_000,
    )
    session.add(file_record)
    session.add(RenameProposal(id=uuid.uuid4(), file_id=file_id, proposed_filename="test.mp3", status=ProposalStatus.EXECUTED))
    (tmp_path / "test.mp3").write_text("fake")

    tracklist = Tracklist(
        id=uuid.uuid4(),
        external_id=f"ext-{uuid.uuid4().hex[:8]}",
        source_url="https://example.com",
        file_id=file_id,
        artist="Test",
        latest_version_id=None,  # No version
        source="1001tracklists",
        status="approved",
    )
    session.add(tracklist)
    await session.commit()

    response = await client.post(f"/cue/{tracklist.id}/generate")
    assert response.status_code == 200
    assert "timestamps" in response.text.lower()


@pytest.mark.asyncio
async def test_get_eligible_tracklist_query_respects_sql_limit(session: AsyncSession) -> None:
    """WR-03: ``limit=`` bounds the eligible set at the SQL level; no ``limit`` returns the full set.

    ``get_cue_review_cards`` passes ``limit=_MAX_REVIEW_ROWS`` so the DB never materializes more than
    the render cap (the D-03 memory bound). The count/pagination callers pass no ``limit`` and still
    see every eligible pair.
    """
    from phaze.routers.cue import _get_eligible_tracklist_query

    for i in range(4):  # four eligible (approved + applied + timestamped) tracklists
        await _create_approved_tracklist_with_file(session, artist=f"Artist {i}", event=f"Event {i}")

    bounded = await _get_eligible_tracklist_query(session, limit=2)
    assert len(bounded) == 2, "the SQL .limit(2) bounds the eligible half (WR-03)"

    unbounded = await _get_eligible_tracklist_query(session)
    assert len(unbounded) == 4, "no limit -> the full eligible set (count/pagination callers)"


@pytest.mark.asyncio
async def test_eligibility_scoped_to_latest_version_not_any_version(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """phaze-dboy: a tracklist whose ONLY timestamped track lives on an OLDER version (a
    re-scrape/re-fingerprint created a newer ``latest_version_id`` with no timestamps) must be
    excluded from the eligible set -- generation only ever reads ``latest_version_id``, so
    counting "any version" produced an always-failing "eligible" row.
    """
    from phaze.routers.cue import _get_eligible_tracklist_query

    tracklist, file_record = await _create_approved_tracklist_with_file(session, artist="Stale Version", track_count=1)

    # v1 (the current latest) already has a timestamped track from the fixture -- capture it,
    # then create v2 (untimestamped) and repoint latest_version_id at it, simulating a re-scrape
    # that lost timing data.
    v2_id = uuid.uuid4()
    session.add(TracklistVersion(id=v2_id, tracklist_id=tracklist.id, version_number=2))
    await session.flush()
    session.add(
        TracklistTrack(
            id=uuid.uuid4(),
            version_id=v2_id,
            position=1,
            artist="Track Artist 1",
            title="Track Title 1",
            timestamp=None,
        )
    )
    tracklist.latest_version_id = v2_id
    await session.commit()

    audio_path = tmp_path / file_record.original_filename
    audio_path.write_text("fake audio")
    file_record.current_path = str(audio_path)
    await session.commit()

    eligible_pairs = await _get_eligible_tracklist_query(session)
    assert tracklist.id not in {tl.id for tl, _fr in eligible_pairs}, "stale-version timestamps must not count as eligible"

    # Generation must fail cleanly (not silently "succeed" against the stale v1 data).
    response = await client.post(f"/cue/{tracklist.id}/generate")
    assert response.status_code == 200
    assert "timestamps" in response.text.lower()


# ---------------------------------------------------------------------------
# ``GET /cue/`` -- legacy bookmark redirect only (phaze-y4s6).
#
# The in-page HX list/pagination fragment this handler used to serve (``cue/partials/cue_list.html``)
# had no live caller left post-v7-cutover and was deleted outright, along with the paging/history-
# restore branches that used to distinguish a live htmx swap from a bookmark/restore. Every shape
# now redirects unconditionally -- these tests pin that down across the header combinations the
# old branching handler used to care about.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cue_history_restore_redirects_to_the_shell(client: AsyncClient) -> None:
    """A history-restore GET ``/cue/`` redirects, same as any other shape."""
    response = await client.get("/cue/", headers={"HX-Request": "true", "HX-History-Restore-Request": "true"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/cue"


@pytest.mark.asyncio
async def test_cue_history_restore_resolves_to_a_full_document(client: AsyncClient) -> None:
    """Following that redirect yields a FULL document with chrome intact."""
    response = await client.get(
        "/cue/",
        headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    body = response.text
    assert "<html" in body.lower(), "a history restore must resolve to a full document"
    assert 'aria-label="Pipeline navigation"' in body, "the page chrome must be present after a restore"


@pytest.mark.asyncio
async def test_cue_restore_header_alone_redirects_to_the_shell(client: AsyncClient) -> None:
    """The restore header alone (no HX-Request) redirects too."""
    response = await client.get("/cue/", headers={"HX-History-Restore-Request": "true"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/cue"
