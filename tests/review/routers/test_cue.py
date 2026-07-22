"""Integration tests for CUE management UI endpoints."""

from __future__ import annotations

import re
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
    """GET /cue/ with HX-Request header returns partial without full page wrapper."""
    await _create_approved_tracklist_with_file(session)
    response = await client.get("/cue/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "<!DOCTYPE html>" not in response.text


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
async def test_cue_list_shows_generated_count_after_generation(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """GET /cue/ stats show generated count > 0 after generating a CUE file."""
    tracklist, file_record = await _create_approved_tracklist_with_file(session)

    audio_path = tmp_path / file_record.original_filename
    audio_path.write_text("fake audio")
    file_record.current_path = str(audio_path)
    await session.commit()

    # Generate a CUE first
    await client.post(f"/cue/{tracklist.id}/generate")
    assert audio_path.with_suffix(".cue").exists()

    # Now list page should show generated count and CUE version
    response = await client.get("/cue/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "CUE v1" in response.text or "Regenerate" in response.text


@pytest.mark.asyncio
async def test_cue_list_shows_version_after_regeneration(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """GET /cue/ shows correct version number after regenerating CUE."""
    tracklist, file_record = await _create_approved_tracklist_with_file(session)

    audio_path = tmp_path / file_record.original_filename
    audio_path.write_text("fake audio")
    file_record.current_path = str(audio_path)
    await session.commit()

    # Generate twice to create v2
    await client.post(f"/cue/{tracklist.id}/generate")
    await client.post(f"/cue/{tracklist.id}/generate")
    v2_path = audio_path.parent / f"{audio_path.stem}.v2.cue"
    assert v2_path.exists()

    # List page should show version 2
    response = await client.get("/cue/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "CUE v2" in response.text


@pytest.mark.asyncio
async def test_batch_generate_with_write_failure_continues(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """POST /cue/generate-batch continues past write failures and logs the error."""
    _tl1, file1 = await _create_approved_tracklist_with_file(session, artist="Good")
    _tl2, file2 = await _create_approved_tracklist_with_file(session, artist="Bad")

    for fr in [file1, file2]:
        audio_path = tmp_path / fr.original_filename
        audio_path.write_text("fake audio")
        fr.current_path = str(audio_path)
    await session.commit()

    call_count = 0

    def _write_side_effect(content: str, audio_path_arg: Path) -> Path:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise OSError("Permission denied")
        from phaze.services.cue_generator import write_cue_file as real_write

        return real_write(content, audio_path_arg)

    with patch("phaze.routers.cue.write_cue_file", side_effect=_write_side_effect):
        response = await client.post("/cue/generate-batch")
    assert response.status_code == 200
    assert "Generated 1 CUE files" in response.text


@pytest.mark.asyncio
async def test_tracklist_list_shows_cue_version(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """GET /tracklists/ computes CUE version for approved tracklists with executed files."""
    tracklist, file_record = await _create_approved_tracklist_with_file(session)

    audio_path = tmp_path / file_record.original_filename
    audio_path.write_text("fake audio")
    file_record.current_path = str(audio_path)
    await session.commit()

    # Generate a CUE file
    await client.post(f"/cue/{tracklist.id}/generate")
    assert audio_path.with_suffix(".cue").exists()

    # Tracklist list should return 200 with CUE version computed
    response = await client.get("/tracklists/", headers={"HX-Request": "true"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_generate_batch(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """POST /cue/generate-batch generates CUEs for all eligible tracklists."""
    _tracklist1, file1 = await _create_approved_tracklist_with_file(session, artist="Artist A")
    _tracklist2, file2 = await _create_approved_tracklist_with_file(session, artist="Artist B")

    # Set up temp paths
    for fr in [file1, file2]:
        audio_path = tmp_path / fr.original_filename
        audio_path.write_text("fake audio")
        fr.current_path = str(audio_path)
    await session.commit()

    response = await client.post("/cue/generate-batch")
    assert response.status_code == 200
    assert "Generated 2 CUE files" in response.text or "toast-container" in response.text


@pytest.mark.asyncio
async def test_generate_batch_offloads_write_to_thread(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """phaze-8lpg: each per-file write_cue_file call must run off the event loop via asyncio.to_thread."""
    from phaze.routers import cue as cue_router

    _tracklist, file_record = await _create_approved_tracklist_with_file(session)
    audio_path = tmp_path / file_record.original_filename
    audio_path.write_text("fake audio")
    file_record.current_path = str(audio_path)
    await session.commit()

    with patch.object(cue_router.asyncio, "to_thread", wraps=cue_router.asyncio.to_thread) as mock_to_thread:
        response = await client.post("/cue/generate-batch")

    assert response.status_code == 200
    assert "Generated 1 CUE files" in response.text
    write_calls = [c for c in mock_to_thread.call_args_list if c.args and c.args[0] is cue_router.write_cue_file]
    assert len(write_calls) == 1, "write_cue_file must be offloaded via asyncio.to_thread exactly once per generated file"


@pytest.mark.asyncio
async def test_generate_batch_materializes_eligible_set_exactly_once(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """phaze-8lpg: the eligible set is queried once (the generation loop), not re-materialized for the response."""
    from phaze.routers import cue as cue_router

    _tracklist, file_record = await _create_approved_tracklist_with_file(session)
    audio_path = tmp_path / file_record.original_filename
    audio_path.write_text("fake audio")
    file_record.current_path = str(audio_path)
    await session.commit()

    with patch.object(cue_router, "_get_eligible_tracklist_query", wraps=cue_router._get_eligible_tracklist_query) as mock_query:
        response = await client.post("/cue/generate-batch")

    assert response.status_code == 200
    assert mock_query.call_count == 1, "the eligible set must be materialized exactly once, not twice"


@pytest.mark.asyncio
async def test_generate_batch_renders_a_bounded_page_not_the_whole_corpus(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """phaze-8lpg: the post-batch response renders ONE bounded page (paged_stmt), not the full eligible set."""
    from phaze.routers import cue as cue_router
    from phaze.services.pagination import MIN_PAGE_SIZE

    # paged_stmt/split_sentinel clamp page_size to >= MIN_PAGE_SIZE internally, so the corpus must
    # exceed MIN_PAGE_SIZE for a bound to be observable at all.
    total = MIN_PAGE_SIZE + 1
    for i in range(total):
        _tracklist, fr = await _create_approved_tracklist_with_file(session, artist=f"Artist {i}", event=f"Event {i}")
        audio_path = tmp_path / fr.original_filename
        audio_path.write_text("fake audio")
        fr.current_path = str(audio_path)
    await session.commit()

    with patch.object(cue_router, "DEFAULT_PAGE_SIZE", MIN_PAGE_SIZE):
        response = await client.post("/cue/generate-batch")

    assert response.status_code == 200
    # All of them were still generated on disk...
    assert f"Generated {total} CUE files" in response.text
    # ...but only the bounded page (MIN_PAGE_SIZE rows) is rendered in the response fragment (each
    # row's top-level `id="cue-row-<id>"` marker is a stable per-row count -- cue_row.html also
    # emits the same id twice more as hx-target attributes on action buttons).
    assert response.text.count('id="cue-row-') == MIN_PAGE_SIZE


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
async def test_cue_list_shows_source_badge(client: AsyncClient, session: AsyncSession) -> None:
    """GET /cue/ shows source badge for each tracklist."""
    await _create_approved_tracklist_with_file(session, source="1001tracklists")
    response = await client.get("/cue/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "1001tracklists" in response.text


@pytest.mark.asyncio
async def test_cue_list_fingerprint_first(client: AsyncClient, session: AsyncSession) -> None:
    """GET /cue/ sorts fingerprint-sourced tracklists before 1001tracklists."""
    await _create_approved_tracklist_with_file(session, artist="ZZZ Last", source="1001tracklists")
    await _create_approved_tracklist_with_file(session, artist="AAA First", source="fingerprint")
    response = await client.get("/cue/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    text = response.text
    # Fingerprint artist should appear before 1001tracklists artist
    fp_pos = text.index("AAA First")
    tt_pos = text.index("ZZZ Last")
    assert fp_pos < tt_pos, "Fingerprint-sourced tracklist should appear before 1001tracklists-sourced"


@pytest.mark.asyncio
async def test_generate_cue_returns_tracklist_card_when_target_is_tracklist(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """POST /cue/{id}/generate with HX-Target: tracklist-{id} returns tracklist card with Regenerate CUE."""
    tracklist, file_record = await _create_approved_tracklist_with_file(session)

    audio_path = tmp_path / file_record.original_filename
    audio_path.write_text("fake audio")
    file_record.current_path = str(audio_path)
    await session.commit()

    response = await client.post(
        f"/cue/{tracklist.id}/generate",
        headers={"HX-Target": f"tracklist-{tracklist.id}"},
    )
    assert response.status_code == 200
    assert "Regenerate CUE" in response.text
    assert "CUE v1" in response.text


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
async def test_generate_cue_error_preserves_tracklist_card_on_tracklist_target(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """phaze-2w49 (tracklist-card surface): HX-Target: tracklist-{id} must keep #tracklist-{id}.

    tracklist_card.html's "Regenerate CUE" button gates only on ``cue_version``, so a routine
    "must be approved" error is reachable here without the tracklist being re-eligible -- the OOB-
    only response used to wipe the row silently on this surface.
    """
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

    response = await client.post(
        f"/cue/{tracklist.id}/generate",
        headers={"HX-Target": f"tracklist-{tracklist.id}"},
    )
    assert response.status_code == 200
    assert f'id="tracklist-{tracklist.id}"' in response.text, "the tracklist card must survive the error, not be deleted"
    assert "approved" in response.text.lower()


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
async def test_generate_batch_skips_no_timestamps(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """POST /cue/generate-batch skips tracklists without timestamps."""
    _tl_with, file_with = await _create_approved_tracklist_with_file(session, artist="With Timestamps")
    _tl_without, file_without = await _create_approved_tracklist_with_file(session, artist="No Timestamps", with_timestamps=False)

    for fr in [file_with, file_without]:
        audio_path = tmp_path / fr.original_filename
        audio_path.write_text("fake audio")
        fr.current_path = str(audio_path)
    await session.commit()

    response = await client.post("/cue/generate-batch")
    assert response.status_code == 200
    assert "Generated 1 CUE files" in response.text


@pytest.mark.asyncio
async def test_generate_batch_unparseable_timestamp_does_not_abort_whole_batch(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """phaze-97u7: _build_cue_tracks runs BEFORE generate_batch's per-row try/except (cue.py:404
    vs :409) -- an unhandled ValueError there would 500 the WHOLE batch, not just the bad row.
    """
    _tl_good, file_good = await _create_approved_tracklist_with_file(session, artist="Good")
    tl_bad, file_bad = await _create_approved_tracklist_with_file(session, artist="Bad", track_count=1)

    bad_track_result = await session.execute(select(TracklistTrack).where(TracklistTrack.version_id == tl_bad.latest_version_id))
    bad_track = bad_track_result.scalars().one()
    bad_track.timestamp = ""
    await session.commit()

    for fr in [file_good, file_bad]:
        audio_path = tmp_path / fr.original_filename
        audio_path.write_text("fake audio")
        fr.current_path = str(audio_path)
    await session.commit()

    response = await client.post("/cue/generate-batch")
    assert response.status_code == 200
    assert "Generated 1 CUE files" in response.text  # the good row still generated


@pytest.mark.asyncio
async def test_cue_list_pagination(client: AsyncClient, session: AsyncSession) -> None:
    """GET /cue/?page=2 returns second page of results."""
    # Create enough tracklists to paginate (default page_size=25)
    for i in range(3):
        await _create_approved_tracklist_with_file(session, artist=f"Artist {i}")

    response = await client.get("/cue/?page=1&page_size=10", headers={"HX-Request": "true"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_cue_list_pagination_tie_group_no_skip_or_duplicate(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-hdho: paging a large tie group must never skip or duplicate a row across pages.

    Every fixture here shares the SAME source/artist/event, so all rows tie completely on the CUE
    list's display ORDER BY (``(source == 'fingerprint').desc(), artist, event``). Before the fix,
    ``list_cue`` re-ran that non-unique ORDER BY on EVERY page request and sliced the fully
    materialized list in Python -- Postgres gives no stability guarantee for a tie group across two
    SEPARATE query executions, so a boundary row could land on both pages (duplicate) or on neither
    (silently skipped). A test that only inspects page 1 would not catch this; this one pages through
    the WHOLE set and asserts the union of every page is exact -- every row exactly once.
    """
    total_rows = 25
    page_size = 10  # MIN_PAGE_SIZE -- forces 3 pages (10, 10, 5) over one fully-tied group.
    expected_ids: set[str] = set()
    for _ in range(total_rows):
        tracklist, _file_record = await _create_approved_tracklist_with_file(
            session, artist="Tied Artist", event="Tied Event", source="1001tracklists"
        )
        expected_ids.add(str(tracklist.id))

    row_id_re = re.compile(r'id="cue-row-([0-9a-fA-F-]{36})"')
    seen_per_page: list[list[str]] = []
    page = 1
    while True:
        response = await client.get(f"/cue/?page={page}&page_size={page_size}", headers={"HX-Request": "true"})
        assert response.status_code == 200
        row_ids = row_id_re.findall(response.text)
        if not row_ids:
            break
        seen_per_page.append(row_ids)
        page += 1
        assert page <= total_rows + 1, "pagination did not terminate -- possible duplicate/skip loop"

    all_seen = [row_id for page_rows in seen_per_page for row_id in page_rows]
    assert len(all_seen) == len(set(all_seen)), "a row appeared on more than one page (duplicate)"
    assert set(all_seen) == expected_ids, "the union of all pages must contain every eligible row exactly once"
    assert [len(page_rows) for page_rows in seen_per_page] == [10, 10, 5], "25 rows at page_size=10 must split into pages of 10/10/5"


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
async def test_get_cue_stats_offloads_generated_scan_to_thread(session: AsyncSession, tmp_path: Path) -> None:
    """phaze-rkvb: the per-file '.cue exists' filesystem probe backing the 'generated' stat must
    run OFF the event loop via ``asyncio.to_thread``, not inline in the async handler -- an
    unbounded, synchronous ``exists()``/``iterdir()`` scan over the whole eligible set on an
    NFS/SMB media mount previously froze the API event loop for the scan's duration on every
    ``/cue/`` render. Correctness of the resulting count is covered by
    ``test_cue_list_shows_generated_count_after_generation``; this test guards the offload
    mechanism itself so a future edit cannot silently move the scan back onto the loop.
    """
    from phaze.routers import cue as cue_router

    _tracklist, file_record = await _create_approved_tracklist_with_file(session)
    audio_path = tmp_path / file_record.original_filename
    audio_path.write_text("fake audio")
    file_record.current_path = str(audio_path)
    await session.commit()

    with patch.object(cue_router.asyncio, "to_thread", wraps=cue_router.asyncio.to_thread) as mock_to_thread:
        stats = await cue_router._get_cue_stats(session)

    mock_to_thread.assert_called_once()
    assert mock_to_thread.call_args.args[0] is cue_router._count_generated_sync
    assert stats["eligible"] == 1
    assert stats["generated"] == 0  # no .cue written yet


def test_count_generated_sync_counts_only_files_with_a_cue_on_disk(tmp_path: Path) -> None:
    """Unit test for the bundled sync scan (phaze-rkvb): mixed generated/ungenerated pairs."""
    from phaze.routers.cue import _count_generated_sync

    with_cue = tmp_path / "with_cue.mp3"
    with_cue.write_text("audio")
    (tmp_path / "with_cue.cue").write_text("cue content")

    without_cue = tmp_path / "without_cue.mp3"
    without_cue.write_text("audio")

    def _record(path: Path) -> FileRecord:
        return FileRecord(
            agent_id="test-fileserver",
            id=uuid.uuid4(),
            sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            original_path=str(path),
            original_filename=path.name,
            current_path=str(path),
            file_type="mp3",
            file_size=1,
        )

    pairs = [
        (None, _record(with_cue)),
        (None, _record(without_cue)),
    ]

    assert _count_generated_sync(pairs) == 1


@pytest.mark.asyncio
async def test_eligibility_scoped_to_latest_version_not_any_version(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """phaze-dboy: a tracklist whose ONLY timestamped track lives on an OLDER version (a
    re-scrape/re-fingerprint created a newer ``latest_version_id`` with no timestamps) must be
    excluded from ``eligible`` and counted in ``missing_timestamps`` -- generation only ever
    reads ``latest_version_id``, so counting "any version" produced an always-failing
    "eligible" row that permanently inflated eligible past generated.
    """
    from phaze.routers.cue import _get_cue_stats, _get_eligible_tracklist_query

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

    stats = await _get_cue_stats(session)
    assert stats["eligible"] == 0
    assert stats["missing_timestamps"] == 1

    # Generation must fail cleanly (not silently "succeed" against the stale v1 data) and batch
    # generation must skip it rather than counting it.
    response = await client.post(f"/cue/{tracklist.id}/generate")
    assert response.status_code == 200
    assert "timestamps" in response.text.lower()

    batch_response = await client.post("/cue/generate-batch")
    assert "Generated 0 CUE files" in batch_response.text


@pytest.mark.asyncio
async def test_cue_list_paged_stmt_has_unique_tiebreaker(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-hdho: guard against silently dropping the paging contract's mandatory tiebreaker.

    ``paged_stmt`` raises ``ValueError`` if ``tiebreaker`` is empty (services.pagination rule 4), but
    that guard only fires if a future edit still CALLS ``paged_stmt`` WITHOUT a tiebreaker -- it would
    not catch a regression that passes a non-unique column (e.g. reverting to ``Tracklist.artist``) or
    reverts ``list_cue`` to raw offset/limit Python slicing (the original bug shape) entirely. This
    wraps ``phaze.routers.cue.paged_stmt`` around the ACTUAL statement ``GET /cue/`` executes and
    asserts the final compiled ``ORDER BY`` key is the unique ``Tracklist.id`` column.

    Deliberately NOT relying on Postgres reproducing tie-order instability across two live queries to
    prove the bug: the bead's own adversarial review notes the pure-tie-nondeterminism mechanism
    "rarely fires on a static table with a stable plan between two close reads" -- an assertion tied to
    that would be a flaky, unreliable regression guard. Asserting the compiled ORDER BY shape of the
    router's OWN call is exact and deterministic regardless of the Postgres instance running the suite.
    """
    from phaze.services.pagination import paged_stmt as real_paged_stmt

    await _create_approved_tracklist_with_file(session)

    captured: list[str] = []

    def _capturing_paged_stmt(*args: object, **kwargs: object) -> object:
        stmt = real_paged_stmt(*args, **kwargs)  # type: ignore[arg-type]
        captured.append(str(stmt))
        return stmt

    with patch("phaze.routers.cue.paged_stmt", side_effect=_capturing_paged_stmt):
        response = await client.get("/cue/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert captured, "list_cue must route its eligible-tracklist read through paged_stmt"

    order_by_clause = captured[0].split("ORDER BY", 1)[1].split("LIMIT", 1)[0]
    sort_keys = [key.strip() for key in order_by_clause.split(",")]
    assert sort_keys[-1] == "tracklists.id", (
        f"the LAST ORDER BY key of the router's OWN paged_stmt call must be the unique Tracklist.id "
        f"tiebreaker so OFFSET paging can never skip or duplicate a tied row across pages -- got: {sort_keys}"
    )


# ---------------------------------------------------------------------------
# ``/cue/`` history-restore response shape (phaze-64uy) -- HYGIENE, not a live defect.
#
# This handler branched on the raw ``HX-Request`` header, which routers/response_shape.py rule 1
# bans outright. But NOTHING in the template corpus pushes a ``/cue/`` URL into history
# (no template carries hx-push-url on a /cue/ control), so no history restore can currently REACH this handler and the raw check was not
# reachable-broken the way shell.py / proposals.py / duplicates.py / admin_agents.py were.
#
# It is converted, and pinned here, so that adding ``hx-push-url`` to these controls later cannot
# silently re-introduce the defect: the shape would already be correct on the day the URL starts
# entering history.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cue_history_restore_does_not_return_a_fragment(client: AsyncClient) -> None:
    """A history-restore GET ``/cue/`` falls through to the shell redirect, not the fragment.

    Asserts the SHAPE, not merely a 200 -- before the fix this returned a 200 fragment, which htmx
    would have swapped into ``<body>``, replacing the whole page.
    """
    response = await client.get("/cue/", headers={"HX-Request": "true", "HX-History-Restore-Request": "true"})
    assert response.status_code == 302, "a restore must not be answered with a chrome-less 200 fragment"
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
async def test_cue_live_htmx_swap_still_returns_the_fragment(client: AsyncClient) -> None:
    """The other direction: an ordinary htmx swap must still get the chrome-less fragment."""
    response = await client.get("/cue/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    body = response.text
    assert "<html" not in body.lower(), "a live htmx swap must get a fragment, not a full document"


@pytest.mark.asyncio
async def test_cue_restore_header_alone_does_not_return_a_fragment(client: AsyncClient) -> None:
    """The restore header dominates even without ``HX-Request`` (response_shape rule 2)."""
    response = await client.get("/cue/", headers={"HX-History-Restore-Request": "true"})
    assert response.status_code == 302
    assert response.headers["location"] == "/s/cue"
