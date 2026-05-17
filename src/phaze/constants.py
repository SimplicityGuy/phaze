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

BULK_INSERT_BATCH_SIZE: int = 1000
"""Number of records per bulk INSERT batch for database ingestion."""

AGENT_LIVENESS_ALIVE_SECONDS: int = 90
"""Phase 29 D-12: seconds since `last_seen_at` below which an agent is 'alive'.

The threshold is 3x the heartbeat cadence (30s) so a single missed beat does
not flip an otherwise-healthy agent to 'stale'. Shared by the classifier
(``phaze.services.agent_liveness.classify``), the UI render, and the
classify-matrix tests so every consumer reads the same source of truth.
"""

AGENT_LIVENESS_STALE_SECONDS: int = 300
"""Phase 29 D-12: seconds since `last_seen_at` below which an agent is 'stale';
deltas ``>= AGENT_LIVENESS_STALE_SECONDS`` classify as 'dead'.

5 minutes of missed heartbeats (~10 beats) is the LOCKED threshold for treating
a worker as ineffective. Shared by the classifier and the matrix tests.
"""
