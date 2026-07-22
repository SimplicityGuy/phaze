"""Integration tests for tag review UI endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta
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
async def test_list_tags_default_order_is_filename_ascending(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-a6hm.7: with no ``sort``/``order``, rows come back filename-ascending (TAGS_SORT default)."""
    await _create_executed_file(session, filename="Zulu - Track.mp3")
    await _create_executed_file(session, filename="Alpha - Track.mp3")

    response = await client.get("/tags/", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert response.text.index("Alpha - Track.mp3") < response.text.index("Zulu - Track.mp3")


@pytest.mark.asyncio
async def test_list_tags_sort_by_filename_desc_reverses_rows(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-a6hm.7: ``sort=filename&order=desc`` is server-side -- the SQL ORDER BY reverses, not the DOM."""
    await _create_executed_file(session, filename="Alpha - Track.mp3")
    await _create_executed_file(session, filename="Zulu - Track.mp3")

    response = await client.get("/tags/?sort=filename&order=desc", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert response.text.index("Zulu - Track.mp3") < response.text.index("Alpha - Track.mp3")


@pytest.mark.asyncio
async def test_list_tags_sort_by_format(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-a6hm.7: "Format" is whitelisted onto ``FileRecord.file_type``."""
    await _create_executed_file(session, filename="A.flac", file_type="flac")
    await _create_executed_file(session, filename="B.mp3", file_type="mp3")

    response = await client.get("/tags/?sort=file_type&order=desc", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert response.text.index("B.mp3") < response.text.index("A.flac")


@pytest.mark.asyncio
async def test_list_tags_unwhitelisted_sort_degrades_to_default_not_422(client: AsyncClient, session: AsyncSession) -> None:
    """column_sort contract rule 3: an unrecognised ``sort`` degrades to the default; it never 422s.

    ``sort`` and ``order`` resolve independently (SortContract.resolve): an unwhitelisted ``sort``
    (a real ``FileRecord`` column that is deliberately NOT offered) falls back to the default KEY
    (``filename``) but a valid ``order`` still applies to it -- so this asserts on the DEFAULT KEY's
    direction, not that ``order`` is ignored too.
    """
    await _create_executed_file(session, filename="Alpha - Track.mp3")
    await _create_executed_file(session, filename="Zulu - Track.mp3")

    response = await client.get("/tags/?sort=original_path&order=desc", headers={"HX-Request": "true"})

    assert response.status_code == 200
    # "original_path" never reached a column (rule 2): the response 200s with the default key
    # (filename) under the requested "desc" direction, rather than 422-ing or raising.
    assert response.text.index("Zulu - Track.mp3") < response.text.index("Alpha - Track.mp3")


@pytest.mark.asyncio
async def test_list_tags_sort_headers_carry_aria_sort_and_preserve_page_size(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-a6hm.7: the active header announces its direction; header links preserve page_size (rule 4)."""
    await _create_executed_file(session)

    response = await client.get("/tags/?sort=file_type&order=desc&page_size=50", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert 'aria-sort="descending"' in response.text
    assert 'aria-sort="none"' in response.text
    assert "page_size=50" in response.text
    assert "sort=filename" in response.text  # the Filename header's own (inactive) link
    assert "sort=file_type" in response.text


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
async def test_compare_tags_renders_id_stamped_hidden_write_inputs(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-8wzi: the Write Tags form's hidden inputs carry the stable id save_tag_field OOB-syncs.

    Without a stable, predictable id, the PUT response has nothing to target and the operator's
    inline edit can never reach the value the form actually submits.
    """
    # artist/title left None so the filename-parse fallback proposes a change (current None -> a
    # non-None proposed value), which is what makes ``has_changes`` true and the form render at all.
    file_record, _ = await _create_executed_file(session, filename="New Artist - New Title.mp3", artist=None, title=None)
    response = await client.get(f"/tags/{file_record.id}/compare")
    assert response.status_code == 200
    assert f'id="tag-write-hidden-artist-{file_record.id}"' in response.text


@pytest.mark.asyncio
async def test_inline_edit_save_oob_syncs_write_tags_hidden_input(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-8wzi: saving an inline edit OOB-swaps the Write Tags form's hidden input to the typed
    value, so the form -- which lives OUTSIDE the table the edited <td> is inside of and is otherwise
    never touched by the edit's own "closest td" swap -- submits what the operator actually typed
    instead of the stale pre-edit snapshot frozen at compare-render time.
    """
    file_record, _ = await _create_executed_file(session, artist="Sven Vath")
    response = await client.put(
        f"/tags/{file_record.id}/edit/artist",
        data={"artist": "Sven Väth"},
    )
    assert response.status_code == 200
    body = response.text
    assert 'hx-swap-oob="true"' in body
    assert f'id="tag-write-hidden-artist-{file_record.id}"' in body
    assert 'name="artist"' in body
    assert 'value="Sven Väth"' in body


@pytest.mark.asyncio
async def test_inline_edit_save_then_write_uses_edited_value(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-8wzi end-to-end: the value the OOB sync carries is what write_file_tags receives and
    writes, and the write is correctly classified as a manual edit (not silently reported as
    source="proposal" when the operator's correction was in fact used).
    """
    file_record, _ = await _create_executed_file(session, artist="Sven Vath", title="Old Title")

    edit_response = await client.put(
        f"/tags/{file_record.id}/edit/artist",
        data={"artist": "Sven Väth"},
    )
    assert "Sven Väth" in edit_response.text

    # The browser's htmx runtime would have OOB-swapped the hidden input to "Sven Väth" by now; the
    # test client has no DOM, so submit what that swap would have produced (the exact value asserted
    # in the OOB fragment above) to prove the write path honors it end-to-end.
    with (
        patch("phaze.services.tag_writer._extract_before_tags", return_value={"artist": "Sven Vath", "title": "Old Title"}),
        patch("phaze.services.tag_writer.write_tags") as mock_write_tags,
        patch("phaze.services.tag_writer.verify_write", return_value={}),
    ):
        write_response = await client.post(
            f"/tags/{file_record.id}/write",
            data={"artist": "Sven Väth", "title": "Old Title"},
        )

    assert write_response.status_code == 200
    written_tags = mock_write_tags.call_args.args[1]
    assert written_tags["artist"] == "Sven Väth", "the operator's correction must be what mutagen writes"


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
async def test_write_tags_v7_approved_row_undo_uses_post_not_patch(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-ldaq: the approved row's UNDO button must issue the SAME verb the router accepts.

    /tags/{id}/undo is POST-only (@router.post). The shared _diff_row.html's lifecycle branch used
    to hard-code hx-patch on the UNDO button regardless of the caller's undo_method, so every
    post-approve row emitted a PATCH against a route that only serves POST -- 405, silently dropped
    by htmx. Assert the rendered button uses hx-post (templated from undo_method="post") and never
    hx-patch.
    """
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
    assert f'hx-post="/tags/{file_record.id}/undo"' in body
    assert "hx-patch=" not in body


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


# ---------------------------------------------------------------------------
# ``/tags/`` history-restore response shape (phaze-64uy) -- HYGIENE, not a live defect.
#
# This handler branched on the raw ``HX-Request`` header, which routers/response_shape.py rule 1
# bans outright. But NOTHING in the template corpus pushes a ``/tags/`` URL into history
# (no template carries hx-push-url on a /tags/ control), so no history restore can currently REACH this handler and the raw check was not
# reachable-broken the way shell.py / proposals.py / duplicates.py / admin_agents.py were.
#
# It is converted, and pinned here, so that adding ``hx-push-url`` to these controls later cannot
# silently re-introduce the defect: the shape would already be correct on the day the URL starts
# entering history.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tags_history_restore_does_not_return_a_fragment(client: AsyncClient) -> None:
    """A history-restore GET ``/tags/`` falls through to the shell redirect, not the fragment.

    Asserts the SHAPE, not merely a 200 -- before the fix this returned a 200 fragment, which htmx
    would have swapped into ``<body>``, replacing the whole page.
    """
    response = await client.get("/tags/", headers={"HX-Request": "true", "HX-History-Restore-Request": "true"})
    assert response.status_code == 302, "a restore must not be answered with a chrome-less 200 fragment"
    assert response.headers["location"] == "/s/tagwrite"


@pytest.mark.asyncio
async def test_tags_history_restore_resolves_to_a_full_document(client: AsyncClient) -> None:
    """Following that redirect yields a FULL document with chrome intact."""
    response = await client.get(
        "/tags/",
        headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    body = response.text
    assert "<html" in body.lower(), "a history restore must resolve to a full document"
    assert 'aria-label="Pipeline navigation"' in body, "the page chrome must be present after a restore"


@pytest.mark.asyncio
async def test_tags_live_htmx_swap_still_returns_the_fragment(client: AsyncClient) -> None:
    """The other direction: an ordinary htmx swap must still get the chrome-less fragment."""
    response = await client.get("/tags/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    body = response.text
    assert "<html" not in body.lower(), "a live htmx swap must get a fragment, not a full document"


@pytest.mark.asyncio
async def test_tags_restore_header_alone_does_not_return_a_fragment(client: AsyncClient) -> None:
    """The restore header dominates even without ``HX-Request`` (response_shape rule 2)."""
    response = await client.get("/tags/", headers={"HX-History-Restore-Request": "true"})
    assert response.status_code == 302
    assert response.headers["location"] == "/s/tagwrite"


# ---------------------------------------------------------------------------
# UNDO snapshot selection + idempotency + honest outcome (phaze-soph / 04bz / 26t7)
# ---------------------------------------------------------------------------


async def _add_write_log(
    session: AsyncSession,
    file_id: uuid.UUID,
    *,
    status: TagWriteStatus,
    source: str,
    before_tags: dict[str, str | int | None],
    written_at: datetime,
    error_message: str | None = None,
) -> TagWriteLog:
    """Insert one fully-specified ``TagWriteLog`` (explicit ``written_at`` to pin ordering)."""
    log = TagWriteLog(
        id=uuid.uuid4(),
        file_id=file_id,
        before_tags=before_tags,
        after_tags={"artist": "Written Artist"},
        source=source,
        status=status.value,
        error_message=error_message,
        written_at=written_at,
    )
    session.add(log)
    await session.commit()
    return log


@pytest.mark.asyncio
async def test_undo_skips_failed_shadow_and_restores_real_write_snapshot(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-soph: a FAILED retry must not shadow the real write's before_tags.

    L1 is the real COMPLETED write (before_tags = original). L2 is a later FAILED retry whose
    before_tags are the post-L1 disk state. Undo must re-apply L1's snapshot, not L2's.
    """
    file_record, _ = await _create_executed_file(session)
    base = datetime(2026, 7, 20, 12, 0, 0)
    await _add_write_log(
        session,
        file_record.id,
        status=TagWriteStatus.COMPLETED,
        source="proposal",
        before_tags={"artist": "Original Artist"},
        written_at=base,
    )
    await _add_write_log(
        session,
        file_record.id,
        status=TagWriteStatus.FAILED,
        source="proposal",
        before_tags={"artist": "Written Artist"},
        written_at=base + timedelta(seconds=30),
        error_message="boom",
    )

    with (
        patch("phaze.services.tag_writer._extract_before_tags", return_value={}),
        patch("phaze.services.tag_writer.write_tags") as mock_write,
        patch("phaze.services.tag_writer.verify_write", return_value={}),
    ):
        response = await client.post(f"/tags/{file_record.id}/undo")

    assert response.status_code == 200
    mock_write.assert_called_once()
    # The reversal re-applies L1's before_tags (the original), never L2's post-write shadow.
    assert mock_write.call_args.args[1] == {"artist": "Original Artist"}


@pytest.mark.asyncio
async def test_undo_skips_no_op_marker_shadow(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-soph: a bulk NO_OP marker (before_tags={}) must not shadow the real DISCREPANCY write."""
    file_record, _ = await _create_executed_file(session)
    base = datetime(2026, 7, 20, 12, 0, 0)
    await _add_write_log(
        session,
        file_record.id,
        status=TagWriteStatus.DISCREPANCY,
        source="proposal",
        before_tags={"artist": "Original Artist"},
        written_at=base,
    )
    await _add_write_log(
        session,
        file_record.id,
        status=TagWriteStatus.NO_OP,
        source="bulk_noop",
        before_tags={},
        written_at=base + timedelta(seconds=30),
    )

    with (
        patch("phaze.services.tag_writer._extract_before_tags", return_value={}),
        patch("phaze.services.tag_writer.write_tags") as mock_write,
        patch("phaze.services.tag_writer.verify_write", return_value={}),
    ):
        response = await client.post(f"/tags/{file_record.id}/undo")

    assert response.status_code == 200
    mock_write.assert_called_once()
    assert mock_write.call_args.args[1] == {"artist": "Original Artist"}


@pytest.mark.asyncio
async def test_undo_with_only_failed_write_reports_nothing_to_undo(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-soph: a file whose only log is FAILED never wrote to disk -> nothing to undo (404)."""
    file_record, _ = await _create_executed_file(session)
    await _add_write_log(
        session,
        file_record.id,
        status=TagWriteStatus.FAILED,
        source="proposal",
        before_tags={"artist": "Written Artist"},
        written_at=datetime(2026, 7, 20, 12, 0, 0),
        error_message="boom",
    )

    response = await client.post(f"/tags/{file_record.id}/undo")
    assert response.status_code == 404
    assert "no prior tag write" in response.text.lower()


async def _count_write_logs(session: AsyncSession, file_id: uuid.UUID, *, source: str) -> int:
    """Count TagWriteLog rows for ``file_id`` with the given ``source``."""
    from sqlalchemy import func, select

    stmt = select(func.count(TagWriteLog.id)).where(TagWriteLog.file_id == file_id, TagWriteLog.source == source)
    return int((await session.execute(stmt)).scalar() or 0)


@pytest.mark.asyncio
async def test_second_undo_is_idempotent_no_op(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-04bz: a second UNDO must NOT re-apply the written tags.

    Seed a real COMPLETED write, then run UNDO twice. The first reverts; the second sees the
    newest log is already a COMPLETED reversal and no-ops -- it must NOT call write_tags again
    and must NOT append another undo log.
    """
    file_record, _ = await _create_executed_file(session)
    await _add_write_log(
        session,
        file_record.id,
        status=TagWriteStatus.COMPLETED,
        source="proposal",
        before_tags={"artist": "Original Artist"},
        written_at=datetime(2026, 7, 20, 12, 0, 0),
    )

    with (
        patch("phaze.services.tag_writer._extract_before_tags", return_value={}),
        patch("phaze.services.tag_writer.write_tags"),
        patch("phaze.services.tag_writer.verify_write", return_value={}),
    ):
        first = await client.post(f"/tags/{file_record.id}/undo")
    assert first.status_code == 200
    assert await _count_write_logs(session, file_record.id, source="undo") == 1

    with (
        patch("phaze.services.tag_writer._extract_before_tags", return_value={}),
        patch("phaze.services.tag_writer.write_tags") as second_write,
        patch("phaze.services.tag_writer.verify_write", return_value={}),
    ):
        second = await client.post(f"/tags/{file_record.id}/undo")

    assert second.status_code == 200
    assert "already reverted" in second.text.lower()
    second_write.assert_not_called()
    # No second reversal was appended to the audit trail.
    assert await _count_write_logs(session, file_record.id, source="undo") == 1


@pytest.mark.asyncio
async def test_second_undo_v7_surfaces_already_reverted_toast(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-04bz: the v7 workspace second-undo no-op redraws a pending row with an honest toast."""
    file_record, _ = await _create_executed_file(session)
    base = datetime(2026, 7, 20, 12, 0, 0)
    await _add_write_log(
        session,
        file_record.id,
        status=TagWriteStatus.COMPLETED,
        source="proposal",
        before_tags={"artist": "Original Artist"},
        written_at=base,
    )
    await _add_write_log(
        session,
        file_record.id,
        status=TagWriteStatus.COMPLETED,
        source="undo",
        before_tags={"artist": "Written Artist"},
        written_at=base + timedelta(seconds=30),
    )

    with patch("phaze.services.tag_writer.write_tags") as mock_write:
        response = await client.post(
            f"/tags/{file_record.id}/undo",
            headers={"HX-Request": "true", "HX-Target": f"tagwrite-row-{file_record.id}"},
        )

    assert response.status_code == 200
    body = response.text
    assert "already reverted" in body.lower()
    assert f'id="tagwrite-row-{file_record.id}"' in body
    assert "APPROVE" in body  # pending row
    mock_write.assert_not_called()


@pytest.mark.asyncio
async def test_undo_retries_after_failed_reversal(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-04bz/26t7: a FAILED reversal does NOT count as already-reverted -- undo may retry."""
    file_record, _ = await _create_executed_file(session)
    base = datetime(2026, 7, 20, 12, 0, 0)
    await _add_write_log(
        session,
        file_record.id,
        status=TagWriteStatus.COMPLETED,
        source="proposal",
        before_tags={"artist": "Original Artist"},
        written_at=base,
    )
    await _add_write_log(
        session,
        file_record.id,
        status=TagWriteStatus.FAILED,
        source="undo",
        before_tags={"artist": "Original Artist"},
        written_at=base + timedelta(seconds=30),
        error_message="read-only",
    )

    with (
        patch("phaze.services.tag_writer._extract_before_tags", return_value={}),
        patch("phaze.services.tag_writer.write_tags") as mock_write,
        patch("phaze.services.tag_writer.verify_write", return_value={}),
    ):
        response = await client.post(f"/tags/{file_record.id}/undo")

    assert response.status_code == 200
    assert "already reverted" not in response.text.lower()
    # The retry re-applies the real write's before_tags.
    mock_write.assert_called_once()
    assert mock_write.call_args.args[1] == {"artist": "Original Artist"}


@pytest.mark.asyncio
async def test_undo_failed_reversal_toasts_failure_not_success(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-26t7: when the reversal write FAILS on disk, the toast must say so, not 'Reverted tags'."""
    file_record, _ = await _create_executed_file(session)
    await _add_write_log(
        session,
        file_record.id,
        status=TagWriteStatus.COMPLETED,
        source="proposal",
        before_tags={"artist": "Original Artist"},
        written_at=datetime(2026, 7, 20, 12, 0, 0),
    )

    # A real mutagen error is swallowed into a FAILED log by execute_tag_write.
    with (
        patch("phaze.services.tag_writer._extract_before_tags", return_value={}),
        patch("phaze.services.tag_writer.write_tags", side_effect=OSError("read-only file system")),
    ):
        response = await client.post(f"/tags/{file_record.id}/undo")

    assert response.status_code == 200
    body = response.text.lower()
    assert "undo failed" in body
    assert "read-only file system" in body
    assert "reverted tags for" not in body


@pytest.mark.asyncio
async def test_undo_failed_reversal_v7_keeps_approved_row_with_failure_toast(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-26t7: the v7 row stays 'approved' (UNDO available to retry) with a failure toast."""
    file_record, _ = await _create_executed_file(session)
    await _add_write_log(
        session,
        file_record.id,
        status=TagWriteStatus.COMPLETED,
        source="proposal",
        before_tags={"artist": "Original Artist"},
        written_at=datetime(2026, 7, 20, 12, 0, 0),
    )

    with (
        patch("phaze.services.tag_writer._extract_before_tags", return_value={}),
        patch("phaze.services.tag_writer.write_tags", side_effect=OSError("boom")),
    ):
        response = await client.post(
            f"/tags/{file_record.id}/undo",
            headers={"HX-Request": "true", "HX-Target": f"tagwrite-row-{file_record.id}"},
        )

    assert response.status_code == 200
    body = response.text
    assert "Undo failed" in body
    assert f'id="tagwrite-row-{file_record.id}"' in body
    assert "UNDO" in body  # approved row keeps the retry control
    assert "Reverted tags for" not in body


@pytest.mark.asyncio
async def test_undo_discrepancy_reversal_toasts_distinct_message(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-26t7: a DISCREPANCY reversal reports the drift, not a clean 'Reverted tags'."""
    file_record, _ = await _create_executed_file(session)
    await _add_write_log(
        session,
        file_record.id,
        status=TagWriteStatus.COMPLETED,
        source="proposal",
        before_tags={"artist": "Original Artist"},
        written_at=datetime(2026, 7, 20, 12, 0, 0),
    )

    with (
        patch("phaze.services.tag_writer._extract_before_tags", return_value={}),
        patch("phaze.services.tag_writer.write_tags"),
        patch("phaze.services.tag_writer.verify_write", return_value={"artist": {"expected": "A", "actual": "B"}}),
    ):
        response = await client.post(f"/tags/{file_record.id}/undo")

    assert response.status_code == 200
    body = response.text.lower()
    assert "discrepancy" in body
