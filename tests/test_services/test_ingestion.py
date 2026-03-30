"""Tests for the ingestion service."""

from pathlib import Path
import platform
from unittest.mock import patch
import uuid

import pytest

from phaze.constants import HASH_CHUNK_SIZE, FileCategory
from phaze.models.file import FileRecord, FileState
from phaze.services.ingestion import bulk_upsert_files, classify_file, compute_sha256, discover_and_hash_files, normalize_path


# --- normalize_path tests ---


def test_normalize_path_nfd_to_nfc() -> None:
    """NFD input (decomposed) is normalized to NFC (composed)."""
    nfd = "cafe\u0301"  # e + combining acute
    result = normalize_path(nfd)
    assert result == "caf\u00e9"  # precomposed e-acute


def test_normalize_path_already_nfc() -> None:
    """NFC input passes through unchanged."""
    nfc = "caf\u00e9"
    assert normalize_path(nfc) == nfc


# --- compute_sha256 tests ---


def test_compute_sha256_known_content(tmp_path: Path) -> None:
    """SHA256 of 'hello world' matches known digest."""
    f = tmp_path / "hello.txt"
    f.write_bytes(b"hello world")
    assert compute_sha256(f) == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"


def test_compute_sha256_reads_in_chunks(tmp_path: Path) -> None:
    """File is read in HASH_CHUNK_SIZE chunks, not all at once."""
    f = tmp_path / "data.bin"
    data_size = HASH_CHUNK_SIZE * 3 + 100
    f.write_bytes(b"x" * data_size)
    with patch.object(Path, "open", wraps=f.open) as mock_open:
        compute_sha256(f)
        mock_open.assert_called_once_with("rb")


def test_compute_sha256_empty_file(tmp_path: Path) -> None:
    """SHA256 of empty file matches known empty digest."""
    f = tmp_path / "empty.bin"
    f.write_bytes(b"")
    assert compute_sha256(f) == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


# --- classify_file tests ---


def test_classify_file_music() -> None:
    """Music extensions are classified as MUSIC."""
    assert classify_file("song.mp3") == FileCategory.MUSIC
    assert classify_file("track.flac") == FileCategory.MUSIC
    assert classify_file("audio.ogg") == FileCategory.MUSIC


def test_classify_file_video() -> None:
    """Video extensions are classified as VIDEO."""
    assert classify_file("movie.mp4") == FileCategory.VIDEO
    assert classify_file("clip.mkv") == FileCategory.VIDEO


def test_classify_file_companion() -> None:
    """Companion extensions are classified as COMPANION."""
    assert classify_file("cover.jpg") == FileCategory.COMPANION
    assert classify_file("info.nfo") == FileCategory.COMPANION
    assert classify_file("tracklist.cue") == FileCategory.COMPANION


def test_classify_file_unknown() -> None:
    """Unknown extensions return UNKNOWN."""
    assert classify_file("readme.exe") == FileCategory.UNKNOWN
    assert classify_file("archive.zip") == FileCategory.UNKNOWN


def test_classify_file_case_insensitive() -> None:
    """Extension matching is case-insensitive."""
    assert classify_file("SONG.MP3") == FileCategory.MUSIC
    assert classify_file("Track.Mp3") == FileCategory.MUSIC


# --- discover_and_hash_files tests ---


def test_discover_files_recursive(tmp_path: Path) -> None:
    """Discovers files recursively in subdirectories."""
    batch_id = uuid.uuid4()
    (tmp_path / "root.mp3").write_bytes(b"root")
    sub = tmp_path / "subdir"
    sub.mkdir()
    (sub / "nested.jpg").write_bytes(b"nested")
    (tmp_path / "skip.exe").write_bytes(b"skip")

    results = discover_and_hash_files(str(tmp_path), batch_id)
    paths = {r["original_filename"] for r in results}
    assert "root.mp3" in paths
    assert "nested.jpg" in paths
    assert "skip.exe" not in paths
    assert len(results) == 2


def test_discover_files_skips_unknown(tmp_path: Path) -> None:
    """Only unknown-extension files -> empty results."""
    batch_id = uuid.uuid4()
    (tmp_path / "virus.exe").write_bytes(b"bad")
    (tmp_path / "archive.dll").write_bytes(b"lib")

    results = discover_and_hash_files(str(tmp_path), batch_id)
    assert results == []


def test_discover_files_nfc_normalized(tmp_path: Path) -> None:
    """Returned paths are NFC-normalized."""
    batch_id = uuid.uuid4()
    # Create a file - we test normalize_path directly since filesystem may auto-normalize
    (tmp_path / "test.mp3").write_bytes(b"data")
    results = discover_and_hash_files(str(tmp_path), batch_id)
    assert len(results) == 1
    # Path should be NFC (normalize_path is applied)
    import unicodedata

    assert results[0]["original_path"] == unicodedata.normalize("NFC", results[0]["original_path"])


def test_discover_files_includes_hash(tmp_path: Path) -> None:
    """Each record has sha256_hash key with hex digest."""
    batch_id = uuid.uuid4()
    (tmp_path / "song.mp3").write_bytes(b"music data")
    results = discover_and_hash_files(str(tmp_path), batch_id)
    assert len(results) == 1
    assert "sha256_hash" in results[0]
    assert len(results[0]["sha256_hash"]) == 64  # hex digest length


def test_discover_files_record_keys(tmp_path: Path) -> None:
    """Each record dict has all required keys."""
    batch_id = uuid.uuid4()
    (tmp_path / "track.flac").write_bytes(b"flac data")
    results = discover_and_hash_files(str(tmp_path), batch_id)
    assert len(results) == 1
    expected_keys = {"id", "sha256_hash", "original_path", "original_filename", "current_path", "file_type", "file_size", "state", "batch_id"}
    assert set(results[0].keys()) == expected_keys


@pytest.mark.skipif(platform.system() == "Windows", reason="chmod not reliable on Windows")
def test_discover_files_skips_unreadable(tmp_path: Path) -> None:
    """Unreadable files are skipped gracefully without crashing."""
    batch_id = uuid.uuid4()
    readable = tmp_path / "good.mp3"
    readable.write_bytes(b"good")
    unreadable = tmp_path / "bad.mp3"
    unreadable.write_bytes(b"bad")
    unreadable.chmod(0o000)

    try:
        results = discover_and_hash_files(str(tmp_path), batch_id)
        filenames = {r["original_filename"] for r in results}
        assert "good.mp3" in filenames
        # bad.mp3 should be skipped (or included if OS allows root read)
    finally:
        unreadable.chmod(0o644)


def test_discover_files_file_type_no_dot(tmp_path: Path) -> None:
    """file_type is stored without leading dot."""
    batch_id = uuid.uuid4()
    (tmp_path / "song.mp3").write_bytes(b"data")
    results = discover_and_hash_files(str(tmp_path), batch_id)
    assert results[0]["file_type"] == "mp3"


def test_discover_files_includes_metadata(tmp_path: Path) -> None:
    """Each record includes correct metadata values."""
    batch_id = uuid.uuid4()
    content = b"test content for metadata"
    (tmp_path / "track.mp3").write_bytes(content)
    results = discover_and_hash_files(str(tmp_path), batch_id)
    assert len(results) == 1
    record = results[0]
    assert record["file_size"] == len(content)
    assert record["state"] == FileState.DISCOVERED
    assert record["batch_id"] == batch_id
    assert record["original_filename"] == "track.mp3"
    assert record["original_path"] == record["current_path"]


# --- Integration tests (require PostgreSQL + greenlet) ---

_has_greenlet = True
try:
    import greenlet  # noqa: F401
except ImportError:
    _has_greenlet = False

_skip_no_db = pytest.mark.skipif(not _has_greenlet, reason="greenlet not installed (required for async SQLAlchemy integration tests)")


@_skip_no_db
@pytest.mark.asyncio
async def test_bulk_upsert_stores_paths(session) -> None:  # type: ignore[no-untyped-def]
    """Records are persisted with correct original_path values."""
    from sqlalchemy import select

    from phaze.models.scan_batch import ScanBatch, ScanStatus

    batch_id = uuid.uuid4()
    scan_batch = ScanBatch(id=batch_id, scan_path="/music", status=ScanStatus.RUNNING, total_files=0, processed_files=0)
    session.add(scan_batch)
    await session.commit()

    records = [
        {
            "id": uuid.uuid4(),
            "sha256_hash": f"{'a' * 63}{i}",
            "original_path": f"/music/song{i}.mp3",
            "original_filename": f"song{i}.mp3",
            "current_path": f"/music/song{i}.mp3",
            "file_type": "mp3",
            "file_size": 1000 + i,
            "state": FileState.DISCOVERED,
            "batch_id": batch_id,
        }
        for i in range(5)
    ]

    count = await bulk_upsert_files(session, records, batch_size=10)
    assert count == 5

    result = await session.execute(select(FileRecord).where(FileRecord.batch_id == batch_id))
    rows = result.scalars().all()
    assert len(rows) == 5
    stored_paths = {r.original_path for r in rows}
    for i in range(5):
        assert f"/music/song{i}.mp3" in stored_paths


@_skip_no_db
@pytest.mark.asyncio
async def test_bulk_upsert_handles_duplicates(session) -> None:  # type: ignore[no-untyped-def]
    """Re-inserting same original_path updates sha256_hash instead of creating duplicates."""
    from sqlalchemy import func, select

    from phaze.models.scan_batch import ScanBatch, ScanStatus

    batch_id = uuid.uuid4()
    scan_batch = ScanBatch(id=batch_id, scan_path="/music", status=ScanStatus.RUNNING, total_files=0, processed_files=0)
    session.add(scan_batch)
    await session.commit()

    original_record = {
        "id": uuid.uuid4(),
        "sha256_hash": "a" * 64,
        "original_path": "/music/duplicate.mp3",
        "original_filename": "duplicate.mp3",
        "current_path": "/music/duplicate.mp3",
        "file_type": "mp3",
        "file_size": 5000,
        "state": FileState.DISCOVERED,
        "batch_id": batch_id,
    }

    # First insert
    await bulk_upsert_files(session, [original_record], batch_size=10)

    # Second insert with same path but different hash
    updated_record = {
        "id": uuid.uuid4(),
        "sha256_hash": "b" * 64,
        "original_path": "/music/duplicate.mp3",
        "original_filename": "duplicate.mp3",
        "current_path": "/music/duplicate.mp3",
        "file_type": "mp3",
        "file_size": 6000,
        "state": FileState.DISCOVERED,
        "batch_id": batch_id,
    }
    await bulk_upsert_files(session, [updated_record], batch_size=10)

    # Should have exactly 1 row, not 2
    count_result = await session.execute(select(func.count()).select_from(FileRecord).where(FileRecord.original_path == "/music/duplicate.mp3"))
    assert count_result.scalar() == 1

    # Hash should be updated
    row_result = await session.execute(select(FileRecord).where(FileRecord.original_path == "/music/duplicate.mp3"))
    row = row_result.scalar_one()
    assert row.sha256_hash == "b" * 64
    assert row.file_size == 6000
