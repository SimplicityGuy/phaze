"""Tests for phaze.constants module."""

from phaze.constants import BULK_INSERT_BATCH_SIZE, EXTENSION_MAP, HASH_CHUNK_SIZE, FileCategory


def test_file_category_values():
    """FileCategory enum has exactly 4 members with correct string values."""
    members = list(FileCategory)
    assert len(members) == 4
    assert FileCategory.MUSIC == "music"
    assert FileCategory.VIDEO == "video"
    assert FileCategory.COMPANION == "companion"
    assert FileCategory.UNKNOWN == "unknown"


def test_extension_map_completeness():
    """EXTENSION_MAP contains exactly 28 entries (9 music + 7 video + 12 companion)."""
    assert len(EXTENSION_MAP) == 28


def test_opus_extension_classified():
    """The .opus extension maps to FileCategory.MUSIC (ING-05)."""
    assert ".opus" in EXTENSION_MAP
    assert EXTENSION_MAP[".opus"] == FileCategory.MUSIC


def test_music_extensions_classified():
    """All music file extensions map to FileCategory.MUSIC."""
    music_exts = [".mp3", ".m4a", ".ogg", ".flac", ".wav", ".aiff", ".wma", ".aac", ".opus"]
    for ext in music_exts:
        assert EXTENSION_MAP[ext] == FileCategory.MUSIC, f"{ext} should be MUSIC"


def test_video_extensions_classified():
    """All video file extensions map to FileCategory.VIDEO."""
    video_exts = [".mp4", ".mkv", ".avi", ".webm", ".mov", ".wmv", ".flv"]
    for ext in video_exts:
        assert EXTENSION_MAP[ext] == FileCategory.VIDEO, f"{ext} should be VIDEO"


def test_companion_extensions_classified():
    """All companion file extensions map to FileCategory.COMPANION."""
    companion_exts = [".cue", ".nfo", ".txt", ".jpg", ".jpeg", ".png", ".gif", ".m3u", ".m3u8", ".pls", ".sfv", ".md5"]
    for ext in companion_exts:
        assert EXTENSION_MAP[ext] == FileCategory.COMPANION, f"{ext} should be COMPANION"


def test_unknown_extension_not_in_map():
    """Extensions not in the map should not be present (used for UNKNOWN classification)."""
    assert ".exe" not in EXTENSION_MAP
    assert ".dll" not in EXTENSION_MAP
    assert ".zip" not in EXTENSION_MAP


def test_extensions_are_lowercase_with_dot():
    """Every key in EXTENSION_MAP starts with '.' and is lowercase."""
    for ext in EXTENSION_MAP:
        assert ext.startswith("."), f"{ext} does not start with '.'"
        assert ext == ext.lower(), f"{ext} is not lowercase"


def test_hash_chunk_size():
    """HASH_CHUNK_SIZE is 64KB (65536 bytes)."""
    assert HASH_CHUNK_SIZE == 65536


def test_bulk_insert_batch_size():
    """BULK_INSERT_BATCH_SIZE is 1000 records per batch."""
    assert BULK_INSERT_BATCH_SIZE == 1000
