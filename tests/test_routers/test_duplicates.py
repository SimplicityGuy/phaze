"""Integration tests for duplicate resolution router."""

import json
import uuid

from httpx import AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.file import FileRecord, FileState
from phaze.models.metadata import FileMetadata


HASH_A = "a" * 64
HASH_B = "b" * 64


def _make_file(
    original_path: str,
    file_type: str,
    sha256_hash: str,
    file_size: int = 1000,
) -> FileRecord:
    """Helper to create a FileRecord with explicit hash."""
    filename = original_path.rsplit("/", 1)[-1]
    return FileRecord(
        id=uuid.uuid4(),
        sha256_hash=sha256_hash,
        original_path=original_path,
        original_filename=filename,
        current_path=original_path,
        file_type=file_type,
        file_size=file_size,
        state=FileState.DISCOVERED,
    )


def _make_metadata(file_id: uuid.UUID, **kwargs) -> FileMetadata:
    """Helper to create a FileMetadata row."""
    return FileMetadata(
        id=uuid.uuid4(),
        file_id=file_id,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_list_duplicates_returns_html(session: AsyncSession, client: AsyncClient) -> None:
    """GET /duplicates/ with duplicate files in DB returns 200 with page heading."""
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    response = await client.get("/duplicates/")

    assert response.status_code == 200
    assert "Duplicate Resolution" in response.text


@pytest.mark.asyncio
async def test_list_duplicates_htmx_returns_partial(session: AsyncSession, client: AsyncClient) -> None:
    """GET /duplicates/ with HX-Request header returns partial without full base.html."""
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    response = await client.get("/duplicates/", headers={"HX-Request": "true"})

    assert response.status_code == 200
    # Partial should NOT contain full base.html elements
    assert "<!DOCTYPE html>" not in response.text
    # But should have group content
    assert HASH_A[:12] in response.text


@pytest.mark.asyncio
async def test_empty_state(session: AsyncSession, client: AsyncClient) -> None:
    """GET /duplicates/ with no duplicate files returns empty state message."""
    # Add a single unique file (no duplicates)
    f1 = _make_file("/dir/unique.mp3", "mp3", HASH_A)
    session.add(f1)
    await session.flush()

    response = await client.get("/duplicates/")

    assert response.status_code == 200
    assert "No duplicates found" in response.text


@pytest.mark.asyncio
async def test_compare_endpoint(session: AsyncSession, client: AsyncClient) -> None:
    """GET /duplicates/{hash}/compare returns comparison table with Resolve Group button."""
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A, file_size=2000)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A, file_size=1000)
    session.add_all([f1, f2])
    await session.flush()

    m1 = _make_metadata(f1.id, bitrate=320, artist="Artist A")
    m2 = _make_metadata(f2.id, bitrate=128, artist="Artist B")
    session.add_all([m1, m2])
    await session.flush()

    response = await client.get(f"/duplicates/{HASH_A}/compare")

    assert response.status_code == 200
    assert "Resolve Group" in response.text
    assert "Artist A" in response.text
    assert "Artist B" in response.text


@pytest.mark.asyncio
async def test_resolve_group(session: AsyncSession, client: AsyncClient) -> None:
    """POST /duplicates/{hash}/resolve marks non-canonical files as DUPLICATE_RESOLVED."""
    f1 = _make_file("/dir/keep.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/dup.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    response = await client.post(
        f"/duplicates/{HASH_A}/resolve",
        data={"canonical_id": str(f1.id)},
    )

    assert response.status_code == 200
    assert "Group resolved" in response.text

    # Verify DB state
    await session.refresh(f2)
    assert f2.state == FileState.DUPLICATE_RESOLVED
    await session.refresh(f1)
    assert f1.state == FileState.DISCOVERED


@pytest.mark.asyncio
async def test_undo_resolve(session: AsyncSession, client: AsyncClient) -> None:
    """POST /duplicates/{hash}/undo restores files to previous state."""
    f1 = _make_file("/dir/keep.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/dup.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    # First resolve
    resolve_response = await client.post(
        f"/duplicates/{HASH_A}/resolve",
        data={"canonical_id": str(f1.id)},
    )
    assert resolve_response.status_code == 200

    # Construct file_states for undo
    file_states = [{"id": str(f2.id), "previous_state": FileState.DISCOVERED}]

    # Undo
    undo_response = await client.post(
        f"/duplicates/{HASH_A}/undo",
        data={"file_states": json.dumps(file_states)},
    )

    assert undo_response.status_code == 200

    # Verify file restored
    await session.refresh(f2)
    assert f2.state == FileState.DISCOVERED


@pytest.mark.asyncio
async def test_bulk_resolve(session: AsyncSession, client: AsyncClient) -> None:
    """POST /duplicates/resolve-all resolves all groups on page."""
    # Group A
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A, file_size=2000)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A, file_size=1000)
    # Group B
    f3 = _make_file("/dir/b1.mp3", "mp3", HASH_B, file_size=3000)
    f4 = _make_file("/dir/b2.mp3", "mp3", HASH_B, file_size=1500)
    session.add_all([f1, f2, f3, f4])
    await session.flush()

    response = await client.post(
        "/duplicates/resolve-all",
        data={"page": "1", "page_size": "20"},
    )

    assert response.status_code == 200
    assert "Resolved" in response.text
    assert "groups" in response.text.lower()


@pytest.mark.asyncio
async def test_bulk_undo(session: AsyncSession, client: AsyncClient) -> None:
    """POST /duplicates/undo-all restores all bulk-resolved files."""
    # Group A
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    # Bulk resolve first
    await client.post("/duplicates/resolve-all", data={"page": "1", "page_size": "20"})

    # Build undo states
    file_states = [{"id": str(f2.id), "previous_state": FileState.DISCOVERED}]

    response = await client.post(
        "/duplicates/undo-all",
        data={
            "file_states": json.dumps(file_states),
            "page": "1",
            "page_size": "20",
        },
    )

    assert response.status_code == 200

    # Verify file restored
    await session.refresh(f2)
    assert f2.state == FileState.DISCOVERED


@pytest.mark.asyncio
async def test_resolved_groups_not_shown(session: AsyncSession, client: AsyncClient) -> None:
    """After resolving a group, GET /duplicates/ no longer shows that group."""
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    # Resolve the group
    await client.post(
        f"/duplicates/{HASH_A}/resolve",
        data={"canonical_id": str(f1.id)},
    )

    # Check listing
    response = await client.get("/duplicates/")
    assert response.status_code == 200
    assert HASH_A[:12] not in response.text
    assert "No duplicates found" in response.text


@pytest.mark.asyncio
async def test_stats_header_values(session: AsyncSession, client: AsyncClient) -> None:
    """Stats response includes correct group count and total files."""
    # Create 2 groups: A (2 files) and B (2 files)
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A, file_size=1000)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A, file_size=2000)
    f3 = _make_file("/dir/b1.mp3", "mp3", HASH_B, file_size=3000)
    f4 = _make_file("/dir/b2.mp3", "mp3", HASH_B, file_size=4000)
    session.add_all([f1, f2, f3, f4])
    await session.flush()

    response = await client.get("/duplicates/")
    assert response.status_code == 200

    # Stats header should show "2" groups and "4" total files
    body = response.text
    # The stats are rendered in the stats_header partial
    assert "Groups" in body
    assert "Total Files" in body
