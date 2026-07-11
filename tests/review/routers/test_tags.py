"""Integration tests for tag review UI endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch
import uuid

import pytest

from phaze.models.file import FileRecord, FileState
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


async def _create_executed_file(
    session: AsyncSession,
    *,
    filename: str = "Artist - Test Track.mp3",
    file_type: str = "mp3",
    state: str = FileState.MOVED,
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
        state=state,
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
    file_record, _ = await _create_executed_file(session, state=FileState.DISCOVERED, applied=False)
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
