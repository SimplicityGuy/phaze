"""Constants for file discovery and ingestion."""

import enum


class FileCategory(enum.StrEnum):
    """Categories for classifying discovered files."""

    MUSIC = "music"
    VIDEO = "video"
    COMPANION = "companion"
    UNKNOWN = "unknown"


EXTENSION_MAP: dict[str, FileCategory] = {
    # Music formats
    ".mp3": FileCategory.MUSIC,
    ".m4a": FileCategory.MUSIC,
    ".ogg": FileCategory.MUSIC,
    ".flac": FileCategory.MUSIC,
    ".wav": FileCategory.MUSIC,
    ".aiff": FileCategory.MUSIC,
    ".wma": FileCategory.MUSIC,
    ".aac": FileCategory.MUSIC,
    ".opus": FileCategory.MUSIC,
    # Video formats
    ".mp4": FileCategory.VIDEO,
    ".mkv": FileCategory.VIDEO,
    ".avi": FileCategory.VIDEO,
    ".webm": FileCategory.VIDEO,
    ".mov": FileCategory.VIDEO,
    ".wmv": FileCategory.VIDEO,
    ".flv": FileCategory.VIDEO,
    # Companion formats
    ".cue": FileCategory.COMPANION,
    ".nfo": FileCategory.COMPANION,
    ".txt": FileCategory.COMPANION,
    ".jpg": FileCategory.COMPANION,
    ".jpeg": FileCategory.COMPANION,
    ".png": FileCategory.COMPANION,
    ".gif": FileCategory.COMPANION,
    ".m3u": FileCategory.COMPANION,
    ".m3u8": FileCategory.COMPANION,
    ".pls": FileCategory.COMPANION,
    ".sfv": FileCategory.COMPANION,
    ".md5": FileCategory.COMPANION,
}

HASH_CHUNK_SIZE: int = 65_536
"""Size in bytes for reading file chunks during SHA-256 hashing (64KB)."""

BULK_INSERT_BATCH_SIZE: int = 1000
"""Number of records per bulk INSERT batch for database ingestion."""
