"""Integration tests for tag review UI endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest

from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from phaze.models.tracklist import Tracklist
from phaze.routers.tags import _determine_file_status, _get_accepted_discogs_link, _get_tag_stats, _get_tracklist_for_file


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


async def _create_executed_file(
    session: AsyncSession,
    *,
    filename: str = "Artist - Test Track.mp3",
    file_type: str = "mp3",
    applied: bool = True,
    artist: str | None = "Old Artist",
    title: str | None = "Old Title",
    album: str | None = None,
    year: int | None = None,
    genre: str | None = None,
    track_number: int | None = None,
) -> tuple[FileRecord, FileMetadata]:
    """Create an applied FileRecord with FileMetadata for testing.

    READ-05 / D-01: the tag routes now gate on ``applied()`` -- an ``executed`` RenameProposal exists
    -- NOT on ``file.state == 'executed'`` (no ``src/`` writer produces that value). By default this
    seeds an ``executed`` proposal and sets the file's own ``state`` to ``'moved'`` (the real
    apply-path outcome), so the list/count/guard routes see it as a tag-write target. Pass
    ``applied=False`` to seed a file with NO executed proposal (a non-applied file the guards reject).
    """
    file_id = uuid.uuid4()
    file_record = FileRecord(
        agent_id="test-fileserver",
        id=file_id,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/dest/{uuid.uuid4().hex}/{filename}",
        original_filename=filename,
        current_path=f"/dest/{filename}",
        file_type=file_type,
        file_size=5_000_000,
    )
    session.add(file_record)
    await session.flush()

    metadata = FileMetadata(
        id=uuid.uuid4(),
        file_id=file_id,
        artist=artist,
        title=title,
        album=album,
        year=year,
        genre=genre,
        track_number=track_number,
    )
    session.add(metadata)
    if applied:
        session.add(
            RenameProposal(
                id=uuid.uuid4(),
                file_id=file_id,
                proposed_filename=filename,
                proposed_path=None,
                confidence=0.95,
                status=ProposalStatus.EXECUTED.value,
            )
        )
    await session.commit()
    return file_record, metadata


@pytest.mark.asyncio
async def test_list_tags_full_page(client: AsyncClient, session: AsyncSession) -> None:
    """Phase 57 (SHELL-05): a plain GET /tags/ 302-redirects into the shell.

    The "Tag Review" heading + stats header + empty-state are full-page chrome on the
    tagwrite workspace node (a Phase-57 placeholder; real content lands in 58-61). The
    in-page HX list partial stays usable (test_list_tags_htmx_partial covers it).
    """
    await _create_executed_file(session)
    response = await client.get("/tags/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/tagwrite"


@pytest.mark.asyncio
async def test_list_tags_htmx_partial(client: AsyncClient, session: AsyncSession) -> None:
    """GET /tags/ with HX-Request header returns partial (no full HTML)."""
    await _create_executed_file(session)
    response = await client.get("/tags/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "<!DOCTYPE html>" not in response.text


@pytest.mark.asyncio
async def test_list_tags_empty_state(client: AsyncClient, session: AsyncSession) -> None:
    """Phase 57 (SHELL-05): the tags empty-state moved to the shell workspace node.

    The "No files ready for tag writing" message is full-page chrome (the tagwrite node is
    a Phase-57 placeholder), so a plain GET /tags/ now 302-redirects into the shell.
    """
    response = await client.get("/tags/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/tagwrite"


@pytest.mark.asyncio
async def test_compare_tags(client: AsyncClient, session: AsyncSession) -> None:
    """GET /tags/{file_id}/compare returns comparison with all 6 fields."""
    file_record, _ = await _create_executed_file(
        session,
        filename="DJ Shadow - Live @ Coachella 2024.mp3",
        artist="DJ Shadow",
        title="Live Set",
    )
    response = await client.get(f"/tags/{file_record.id}/compare")
    assert response.status_code == 200
    assert "Tag Comparison" in response.text
    assert "Artist" in response.text
    assert "Title" in response.text
    assert "Album" in response.text
    assert "Year" in response.text
    assert "Genre" in response.text


@pytest.mark.asyncio
async def test_inline_edit_returns_input(client: AsyncClient, session: AsyncSession) -> None:
    """GET /tags/{file_id}/edit/artist returns HTML input with hx-put."""
    file_record, _ = await _create_executed_file(session)
    response = await client.get(f"/tags/{file_record.id}/edit/artist")
    assert response.status_code == 200
    assert "hx-put" in response.text
    assert "input" in response.text.lower()


@pytest.mark.asyncio
async def test_inline_edit_invalid_field(client: AsyncClient, session: AsyncSession) -> None:
    """GET /tags/{file_id}/edit/invalid returns 400."""
    file_record, _ = await _create_executed_file(session)
    response = await client.get(f"/tags/{file_record.id}/edit/invalid_field")
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_inline_edit_save(client: AsyncClient, session: AsyncSession) -> None:
    """PUT /tags/{file_id}/edit/artist with form data returns display span."""
    file_record, _ = await _create_executed_file(session)
    response = await client.put(
        f"/tags/{file_record.id}/edit/artist",
        data={"artist": "New Artist"},
    )
    assert response.status_code == 200
    assert "New Artist" in response.text
    assert "hx-get" in response.text


@pytest.mark.asyncio
async def test_write_tags_success(client: AsyncClient, session: AsyncSession) -> None:
    """POST /tags/{file_id}/write with valid data returns success status."""
    file_record, _ = await _create_executed_file(session, artist="Original Artist")

    with (
        patch("phaze.services.tag_writer._extract_before_tags", return_value={"artist": "Original Artist"}),
        patch("phaze.services.tag_writer.write_tags"),
        patch("phaze.services.tag_writer.verify_write", return_value={}),
    ):
        response = await client.post(
            f"/tags/{file_record.id}/write",
            data={"artist": "New Artist", "title": "New Title"},
        )
    assert response.status_code == 200
    assert "completed" in response.text.lower() or "Done" in response.text


@pytest.mark.asyncio
async def test_write_tags_non_integer_year_and_track_number_kept_as_string(client: AsyncClient, session: AsyncSession) -> None:
    """A non-integer year/track_number falls through the int() ValueError branch and is kept as the raw string."""
    file_record, _ = await _create_executed_file(session, artist="Original Artist")

    with (
        patch("phaze.services.tag_writer._extract_before_tags", return_value={"artist": "Original Artist"}),
        patch("phaze.services.tag_writer.write_tags"),
        patch("phaze.services.tag_writer.verify_write", return_value={}),
    ):
        response = await client.post(
            f"/tags/{file_record.id}/write",
            data={"artist": "New Artist", "year": "not-a-year", "track_number": "A1"},
        )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_write_tags_non_executed_rejected(client: AsyncClient, session: AsyncSession) -> None:
    """POST /tags/{file_id}/write for a non-applied file (no executed proposal) returns error."""
    file_record, _ = await _create_executed_file(session, applied=False)
    response = await client.post(
        f"/tags/{file_record.id}/write",
        data={"artist": "Test"},
    )
    assert response.status_code == 400
    assert "executed" in response.text.lower() or "Only" in response.text


@pytest.mark.asyncio
async def test_stats_counts(client: AsyncClient, session: AsyncSession) -> None:
    """Phase 57 (SHELL-05): the tags stats header moved to the shell workspace node.

    The pending/completed/discrepancy stats header ("Written" etc.) is full-page chrome on
    the tagwrite workspace node (a Phase-57 placeholder), so a plain GET /tags/ now
    302-redirects into the shell. The stats computation itself is covered by
    ``_get_tag_stats`` service tests.
    """
    response = await client.get("/tags/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/tagwrite"


@pytest.mark.asyncio
async def test_write_tags_empty_body_uses_fallback(client: AsyncClient, session: AsyncSession) -> None:
    """POST /tags/{file_id}/write with empty form body computes proposed tags server-side."""
    file_record, _ = await _create_executed_file(
        session,
        filename="DJ Shadow - Live @ Coachella 2024.mp3",
        artist="DJ Shadow",
        title="Live Set",
    )

    with (
        patch("phaze.services.tag_writer._extract_before_tags", return_value={"artist": "DJ Shadow"}),
        patch("phaze.services.tag_writer.write_tags") as mock_write,
        patch("phaze.services.tag_writer.verify_write", return_value={}),
    ):
        response = await client.post(f"/tags/{file_record.id}/write")

    assert response.status_code == 200
    assert "completed" in response.text.lower() or "Done" in response.text

    # Verify write_tags was called with non-empty tags (the computed proposed tags)
    mock_write.assert_called_once()
    written_tags = mock_write.call_args[0][1]  # second positional arg is tags dict
    assert len(written_tags) > 0, "Fallback should compute non-empty proposed tags"
    assert "artist" in written_tags


@pytest.mark.asyncio
async def test_write_tags_response_has_row_id(client: AsyncClient, session: AsyncSession) -> None:
    """POST /tags/{file_id}/write response HTML contains id='row-{file_id}' for HTMX targeting."""
    file_record, _ = await _create_executed_file(session, artist="Test Artist")

    with (
        patch("phaze.services.tag_writer._extract_before_tags", return_value={"artist": "Test Artist"}),
        patch("phaze.services.tag_writer.write_tags"),
        patch("phaze.services.tag_writer.verify_write", return_value={}),
    ):
        response = await client.post(
            f"/tags/{file_record.id}/write",
            data={"artist": "New Artist"},
        )

    assert response.status_code == 200
    assert f'id="row-{file_record.id}"' in response.text


# --- helper unit tests --------------------------------------------------------


def test_determine_file_status_pending_when_no_write_log() -> None:
    assert _determine_file_status(None) == "pending"


def test_determine_file_status_returns_write_log_status() -> None:
    """A present write log surfaces its own status verbatim."""
    log = MagicMock()
    log.status = "completed"
    assert _determine_file_status(log) == "completed"


@pytest.mark.asyncio
async def test_get_accepted_discogs_link_returns_highest_confidence_accepted() -> None:
    """With a resolved tracklist version, the accepted DiscogsLink is returned."""
    version_result = MagicMock()
    # phaze-1am9: the version lookup is now multiplicity-tolerant (scalars().first(), not scalar_one_or_none).
    version_result.scalars.return_value.first.return_value = uuid.uuid4()  # a real version_id
    sentinel_link = MagicMock(name="accepted-link")
    link_result = MagicMock()
    link_result.scalar_one_or_none.return_value = sentinel_link

    session = AsyncMock()
    session.execute.side_effect = [version_result, link_result]

    got = await _get_accepted_discogs_link(session, uuid.uuid4())
    assert got is sentinel_link


@pytest.mark.asyncio
async def test_get_accepted_discogs_link_none_when_no_tracklist_version() -> None:
    """No resolved tracklist version short-circuits to None without a link query."""
    version_result = MagicMock()
    version_result.scalars.return_value.first.return_value = None
    session = AsyncMock()
    session.execute.return_value = version_result

    got = await _get_accepted_discogs_link(session, uuid.uuid4())
    assert got is None
    session.execute.assert_awaited_once()  # only the version lookup ran


@pytest.mark.asyncio
async def test_get_tracklist_for_file_tolerates_multiple_links(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-1am9: a file with TWO linked tracklists must not raise MultipleResultsFound.

    ``tracklists.file_id`` has only a non-unique index and mainline paths (>=90 auto-link, fingerprint
    re-scan) create multiple tracklists per file. The helper must pick the highest-confidence link
    deterministically instead of crashing (which 500s /tags/ and silently empties the tagwrite queue).
    """
    file_record, _ = await _create_executed_file(session)

    low = Tracklist(
        id=uuid.uuid4(),
        file_id=file_record.id,
        external_id=f"tl-low-{uuid.uuid4().hex[:8]}",
        source_url="https://www.1001tracklists.com/tracklist/low/test.html",
        source="1001tracklists",
        status="matched",
        match_confidence=90,
    )
    high = Tracklist(
        id=uuid.uuid4(),
        file_id=file_record.id,
        external_id=f"fp-{uuid.uuid4().hex[:8]}",
        source_url="https://www.1001tracklists.com/tracklist/high/test.html",
        source="fingerprint",
        status="matched",
        match_confidence=97,
    )
    session.add_all([low, high])
    await session.commit()

    got = await _get_tracklist_for_file(session, file_record.id)
    assert got is not None
    assert got.id == high.id, "the highest-confidence tracklist wins deterministically"

    # And the accepted-link helper (same multiplicity trap on the version lookup) does not raise either.
    link = await _get_accepted_discogs_link(session, file_record.id)
    assert link is None  # no accepted DiscogsLink seeded; the point is it returns cleanly

    # The list page renders (previously a MultipleResultsFound 500 for every file once one bad file existed).
    resp = await client.get("/tags/", headers={"HX-Request": "true"})
    assert resp.status_code == 200


# --- route not-found / invalid-field guards -----------------------------------


@pytest.mark.asyncio
async def test_compare_tags_missing_file_404(client: AsyncClient) -> None:
    response = await client.get(f"/tags/{uuid.uuid4()}/compare")
    assert response.status_code == 404
    assert "not found" in response.text.lower()


@pytest.mark.asyncio
async def test_edit_tag_field_missing_file_404(client: AsyncClient) -> None:
    response = await client.get(f"/tags/{uuid.uuid4()}/edit/artist")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_save_tag_field_invalid_field_400(client: AsyncClient) -> None:
    """The field allow-list is checked before the file lookup -> 400, not 404."""
    response = await client.put(f"/tags/{uuid.uuid4()}/edit/not_a_field", data={"not_a_field": "x"})
    assert response.status_code == 400
    assert "invalid field" in response.text.lower()


@pytest.mark.asyncio
async def test_save_tag_field_missing_file_404(client: AsyncClient) -> None:
    """A valid field but absent file falls through to the 404 branch."""
    response = await client.put(f"/tags/{uuid.uuid4()}/edit/artist", data={"artist": "x"})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_write_file_tags_missing_file_404(client: AsyncClient) -> None:
    response = await client.post(f"/tags/{uuid.uuid4()}/write", data={"artist": "x"})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_undo_tag_write_missing_file_404(client: AsyncClient) -> None:
    response = await client.post(f"/tags/{uuid.uuid4()}/undo")
    assert response.status_code == 404


# --- write_file_tags status/toast branches ------------------------------------


@pytest.mark.asyncio
async def test_write_tags_discrepancy_branch(client: AsyncClient, session: AsyncSession) -> None:
    """A non-empty verify_write result yields a DISCREPANCY status + discrepancy toast."""
    file_record, _ = await _create_executed_file(session, artist="Original Artist")

    with (
        patch("phaze.services.tag_writer._extract_before_tags", return_value={"artist": "Original Artist"}),
        patch("phaze.services.tag_writer.write_tags"),
        patch("phaze.services.tag_writer.verify_write", return_value={"artist": {"sent": "New Artist", "got": "New  Artist"}}),
    ):
        response = await client.post(f"/tags/{file_record.id}/write", data={"artist": "New Artist"})

    assert response.status_code == 200
    assert "discrepancy" in response.text.lower()


@pytest.mark.asyncio
async def test_write_tags_failed_branch(client: AsyncClient, session: AsyncSession) -> None:
    """A write_tags exception yields a FAILED status + failure toast, not a 500."""
    file_record, _ = await _create_executed_file(session, artist="Original Artist")

    with (
        patch("phaze.services.tag_writer._extract_before_tags", return_value={"artist": "Original Artist"}),
        patch("phaze.services.tag_writer.write_tags", side_effect=OSError("read-only file")),
    ):
        response = await client.post(f"/tags/{file_record.id}/write", data={"artist": "New Artist"})

    assert response.status_code == 200
    assert "failed" in response.text.lower()


@pytest.mark.asyncio
async def test_write_tags_valueerror_branch(client: AsyncClient, session: AsyncSession) -> None:
    """A ValueError raised by execute_tag_write is caught and surfaced as a failed toast."""
    file_record, _ = await _create_executed_file(session, artist="Original Artist")

    with patch("phaze.routers.tags.execute_tag_write", new=AsyncMock(side_effect=ValueError("boom"))):
        response = await client.post(f"/tags/{file_record.id}/write", data={"artist": "New Artist"})

    assert response.status_code == 200
    assert "failed" in response.text.lower()
    assert "boom" in response.text


# ---------------------------------------------------------------------------
# v7 diff-row workspace negotiation on the write/undo mutation routes (phaze-nvll)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_tags_from_v7_workspace_returns_diff_row_with_undo(client: AsyncClient, session: AsyncSession) -> None:
    """Approving from the v7 tagwrite workspace returns the styled _diff_row.html WITH a working UNDO,
    not the legacy <tr>-based tag_row.html (phaze-nvll defects 1+2)."""
    file_record, _ = await _create_executed_file(session, artist="Original Artist")

    with (
        patch("phaze.services.tag_writer._extract_before_tags", return_value={"artist": "Original Artist"}),
        patch("phaze.services.tag_writer.write_tags"),
        patch("phaze.services.tag_writer.verify_write", return_value={}),
    ):
        response = await client.post(
            f"/tags/{file_record.id}/write",
            data={"artist": "New Artist"},
            headers={"HX-Request": "true", "HX-Target": f"tagwrite-row-{file_record.id}"},
        )

    assert response.status_code == 200
    body = response.text
    assert f'id="tagwrite-row-{file_record.id}"' in body
    assert "approved" in body
    assert "UNDO" in body
    # The legacy <tr id="row-..."> markup (and its stray OOB detail-<tr>) must NOT be present.
    assert f'id="row-{file_record.id}"' not in body
    assert "<tr" not in body
    assert f'id="detail-{file_record.id}"' not in body


@pytest.mark.asyncio
async def test_undo_tag_write_from_v7_workspace_restores_pending_row(client: AsyncClient, session: AsyncSession) -> None:
    """Undoing from the v7 workspace swaps the row back to the pending diff-row (phaze-nvll)."""
    file_record, _ = await _create_executed_file(session, artist="Original Artist")

    with (
        patch("phaze.services.tag_writer._extract_before_tags", return_value={"artist": "Original Artist"}),
        patch("phaze.services.tag_writer.write_tags"),
        patch("phaze.services.tag_writer.verify_write", return_value={}),
    ):
        await client.post(f"/tags/{file_record.id}/write", data={"artist": "New Artist"})

        response = await client.post(
            f"/tags/{file_record.id}/undo",
            headers={"HX-Request": "true", "HX-Target": f"tagwrite-row-{file_record.id}"},
        )

    assert response.status_code == 200
    body = response.text
    assert f'id="tagwrite-row-{file_record.id}"' in body
    assert "APPROVE" in body  # reverted to pending -> the approve action cluster is back
    assert f'id="row-{file_record.id}"' not in body


@pytest.mark.asyncio
async def test_write_tags_v7_missing_file_surfaces_toast_not_bare_404(client: AsyncClient) -> None:
    """A stale row (file gone) posting APPROVE from the v7 workspace gets a 200 + OOB toast, not a
    bare 404 string htmx silently drops (phaze-nvll defect 3)."""
    missing_id = uuid.uuid4()
    response = await client.post(
        f"/tags/{missing_id}/write",
        data={"artist": "x"},
        headers={"HX-Request": "true", "HX-Target": f"tagwrite-row-{missing_id}"},
    )
    assert response.status_code == 200
    assert "hx-swap-oob" in response.text
    assert "not found" in response.text.lower()


@pytest.mark.asyncio
async def test_write_tags_v7_non_executed_surfaces_toast_and_keeps_pending_row(client: AsyncClient, session: AsyncSession) -> None:
    """A stale row (file no longer executed) posting APPROVE gets its toast surfaced AND the row
    redrawn as still-pending, not silently dropped (phaze-nvll defect 3)."""
    file_record, _ = await _create_executed_file(session, applied=False)
    response = await client.post(
        f"/tags/{file_record.id}/write",
        data={"artist": "Test"},
        headers={"HX-Request": "true", "HX-Target": f"tagwrite-row-{file_record.id}"},
    )
    assert response.status_code == 200
    body = response.text
    assert "hx-swap-oob" in body
    assert "only executed files" in body.lower()
    assert f'id="tagwrite-row-{file_record.id}"' in body
    assert "APPROVE" in body


@pytest.mark.asyncio
async def test_undo_tag_write_v7_missing_file_surfaces_toast_not_bare_404(client: AsyncClient) -> None:
    """A stale row (file gone) posting UNDO from the v7 workspace gets a 200 + OOB toast (phaze-nvll defect 3)."""
    missing_id = uuid.uuid4()
    response = await client.post(
        f"/tags/{missing_id}/undo",
        headers={"HX-Request": "true", "HX-Target": f"tagwrite-row-{missing_id}"},
    )
    assert response.status_code == 200
    assert "hx-swap-oob" in response.text
    assert "not found" in response.text.lower()


@pytest.mark.asyncio
async def test_undo_tag_write_v7_no_prior_write_surfaces_toast_and_pending_row(client: AsyncClient, session: AsyncSession) -> None:
    """UNDO with no prior TagWriteLog (a race/stale row) surfaces its toast AND redraws the row as
    pending instead of silently doing nothing (phaze-nvll defect 3)."""
    file_record, _ = await _create_executed_file(session)
    response = await client.post(
        f"/tags/{file_record.id}/undo",
        headers={"HX-Request": "true", "HX-Target": f"tagwrite-row-{file_record.id}"},
    )
    assert response.status_code == 200
    body = response.text
    assert "hx-swap-oob" in body
    assert "no prior tag write" in body.lower()
    assert f'id="tagwrite-row-{file_record.id}"' in body
    assert "APPROVE" in body


@pytest.mark.asyncio
async def test_write_tags_without_v7_target_keeps_legacy_response(client: AsyncClient, session: AsyncSession) -> None:
    """The legacy tag list/comparison pages (no v7 HX-Target) still get tag_row.html back (phaze-nvll)."""
    file_record, _ = await _create_executed_file(session, artist="Original Artist")

    with (
        patch("phaze.services.tag_writer._extract_before_tags", return_value={"artist": "Original Artist"}),
        patch("phaze.services.tag_writer.write_tags"),
        patch("phaze.services.tag_writer.verify_write", return_value={}),
    ):
        response = await client.post(
            f"/tags/{file_record.id}/write",
            data={"artist": "New Artist"},
            headers={"HX-Request": "true", "HX-Target": f"row-{file_record.id}"},
        )

    assert response.status_code == 200
    assert f'id="row-{file_record.id}"' in response.text


def _add_tag_write_log(session: AsyncSession, file_id: uuid.UUID, status: TagWriteStatus) -> None:
    """Attach one ``TagWriteLog`` of ``status`` to ``file_id`` (append-only audit row)."""
    session.add(
        TagWriteLog(
            id=uuid.uuid4(),
            file_id=file_id,
            before_tags={},
            after_tags={},
            source="review",
            status=status.value,
        )
    )


@pytest.mark.asyncio
async def test_tag_stats_file_with_both_completed_and_discrepancy_counted_once(client: AsyncClient, session: AsyncSession) -> None:
    """WR-02: a file with BOTH a COMPLETED and a DISCREPANCY log is subtracted from ``pending`` only once.

    Two applied files: file A carries both a COMPLETED and a DISCREPANCY ``TagWriteLog`` (a normal
    re-write sequence), file B is untouched (genuinely pending). The old ``pending = total_executed -
    completed - discrepancies`` double-subtracted A (it is in both the ``completed`` and the
    ``discrepancies`` DISTINCT tally), reporting ``pending == 0`` and eating B's real pending count.
    The correct answer is ``pending == 1`` (file B). The separate ``completed`` / ``discrepancies``
    display cells must still each report 1 (their own DISTINCT-file tallies are unchanged).
    """
    file_a, _ = await _create_executed_file(session, filename="A - handled.mp3")
    await _create_executed_file(session, filename="B - pending.mp3")

    _add_tag_write_log(session, file_a.id, TagWriteStatus.COMPLETED)
    _add_tag_write_log(session, file_a.id, TagWriteStatus.DISCREPANCY)
    await session.commit()

    stats = await _get_tag_stats(session)

    assert stats["pending"] == 1, "file B is still pending; file A must not be subtracted twice"
    assert stats["completed"] == 1, "one distinct file has a COMPLETED write"
    assert stats["discrepancies"] == 1, "one distinct file has a DISCREPANCY write"
