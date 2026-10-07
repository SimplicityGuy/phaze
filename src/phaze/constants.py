"""Constants for file discovery and ingestion."""

import enum
from pathlib import PurePath
import re
import unicodedata


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
    ".mp2": FileCategory.MUSIC,
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

INGESTIBLE_COMPANION_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".cue",
        ".m3u",
        ".m3u8",
        ".nfo",
        ".pls",
        ".txt",
    }
)
"""Companion extensions admitted by the scan and watcher ingestion producers.

This is the operator-approved D6 subset of the COMPANION entries in
``EXTENSION_MAP``. Artwork and checksum companions remain intentionally excluded.
"""

QUARANTINE_DIRNAME: str = ".phaze-quarantine"
"""The hidden per-scan-root directory approved junk companions are moved into.

Operator decision 9, 2026-10-07, "Hidden dir per scan root (Recommended)"; epic phaze-4x319: the
quarantine task places it at ``<root>/.phaze-quarantine/``, mirroring each file's original relative
path. Neither producer ingests anything beneath it: ``tasks/scan.py`` prunes it from the walk and the
watcher drops its events, so a quarantined file is never re-ingested (bead phaze-gafl9).
"""


def is_quarantined(path: str) -> bool:
    """True when any component of ``path`` is :data:`QUARANTINE_DIRNAME` -- the scan and watcher share this test."""
    return QUARANTINE_DIRNAME in PurePath(path).parts


_SCENE_INDEX_PREFIX = re.compile(r"^\d{1,3}[\s_.\-]+")
_DUPLICATE_MARKER_SUFFIX = re.compile(r"\s*\(\d{1,2}\)$")


def companion_match_key(name: str) -> str:
    """Normalize a file stem for the linking chain's own-folder stem step (phaze-rmhfr; key from phaze-ehryj).

    ``services/companion_linking.py``'s step 3 links a companion to the media in its OWN folder whose
    stem has the same key as the companion's own stem (phaze-9aker method (s): it narrows a
    multi-media folder to the file the companion is named after). This is the only caller.

    Until phaze-rmhfr this key also paired a companion with no media of its own with media in its
    PARENT folder whose stem matched the companion's sub-folder name, its own stem, or the parent
    folder's own name (phaze-ehryj, operator decisions 2026-10-06 "Match by name" and "Add
    parent-folder name"). That fallback is retired, scan side (phaze-gafl9) and association side
    (phaze-rmhfr): operator decision 8, 2026-10-07, "Yes, file it (Recommended)"; epic phaze-4x319.
    phaze-9aker measured it at link precision 0.125-0.345 with 7 of 8 links into the archive's flat
    dump folder wrong (``docs/spikes/phaze-9aker-companion-matching-accuracy.md`` §4.4-§4.5).

    A trailing duplicate-copy marker (`` (1)``, `` (12)``) is dropped first, so a download saved as
    ``<release> (1).mkv`` still matches its ``<release>`` folder (operator decision 2026-10-06,
    "Strip \" (N)\" markers (Recommended)"; bead phaze-ehryj). ``X (1)`` and ``X (2)`` therefore share
    a key, and a companion named ``X`` links to both. Then the name is case-folded and NFC-normalized
    (after folding, so an NFD name off disk and its NFC database row agree), a leading scene index
    (``00-``, ``01 ``, ``002_``) dropped, then every non-alphanumeric
    character removed -- so ``_``, space, ``-``, ``.``, commas and brackets never decide a match.
    Unicode letters are kept: ``isalnum`` is Unicode-aware, unlike an ASCII character class. A key of
    ``""`` never matches anything.
    """
    folded = unicodedata.normalize("NFC", _DUPLICATE_MARKER_SUFFIX.sub("", name).casefold())
    return "".join(char for char in _SCENE_INDEX_PREFIX.sub("", folded) if char.isalnum())


BULK_INSERT_BATCH_SIZE: int = 1000
"""Number of records per bulk INSERT batch for database ingestion."""

AGENT_HEARTBEAT_INTERVAL_SECONDS: int = 30
"""Phase 46: cadence (seconds) of the agent liveness heartbeat loop.

Single source of truth for the heartbeat cadence. The heartbeat runs as an
asyncio background task launched in the agent worker startup hook (NOT a SAQ
CronJob), so it cannot be starved by a saturated ``worker_max_jobs`` dispatch
pool — the Phase 46 incident where a busy-but-healthy agent was wrongly marked
DEAD. Kept at 30s (matches the prior cron cadence). ``AGENT_LIVENESS_ALIVE_SECONDS``
(90) is intentionally 3x this value so a single missed beat never flips a healthy
agent to 'stale'.
"""

AGENT_LIVENESS_ALIVE_SECONDS: int = 90
"""Phase 29 D-12: seconds since `last_seen_at` below which an agent is 'alive'.

The threshold is 3x ``AGENT_HEARTBEAT_INTERVAL_SECONDS`` (the heartbeat cadence)
so a single missed beat does not flip an otherwise-healthy agent to 'stale'.
Shared by the classifier (``phaze.services.agent_liveness.classify``), the UI
render, and the classify-matrix tests so every consumer reads the same source of
truth.
"""

AGENT_LIVENESS_STALE_SECONDS: int = 300
"""Phase 29 D-12: seconds since `last_seen_at` below which an agent is 'stale';
deltas ``>= AGENT_LIVENESS_STALE_SECONDS`` classify as 'dead'.

5 minutes of missed heartbeats (~10 beats) is the LOCKED threshold for treating
a worker as ineffective. Shared by the classifier and the matrix tests.
"""

AGENT_BROKER_UNHEALTHY_BEATS: int = 3
"""phaze-xuec1: consecutive failed/timed-out broker probes (``queue.info()``) before a
heartbeat beat withholds its POST instead of reporting the agent alive.

The 2026-08-08 nox incident: the analyze-lane worker beat every 30s for 1h41m while unable
to dequeue a single job -- the heartbeat POST is independent HTTP traffic to the control
plane and proves nothing about the worker's Postgres broker connection. ``queue.info()``
exercises the SAME psycopg3 pool the SAQ dispatch loop's ``_dequeue()`` needs, so repeated
failures there are real evidence the worker cannot currently consume its queue.

3 beats at the 30s cadence (``AGENT_HEARTBEAT_INTERVAL_SECONDS``) is ~90s -- deliberately
equal to ``AGENT_LIVENESS_ALIVE_SECONDS`` so a single bad tick (or two) never flips an
otherwise-healthy agent to non-alive, mirroring the existing "a single missed beat must not
flip 'alive'" reasoning for the interval/threshold ratio above.
"""

AGENT_BROKER_EXIT_BEATS: int = 10
"""phaze-xuec1: consecutive failed broker probes before the worker exits loudly (SIGTERM)
rather than sitting wedged indefinitely.

Matches ``AGENT_LIVENESS_STALE_SECONDS`` (5 minutes at the 30s cadence): by the time an
operator's UI would show this agent 'dead' anyway, the worker stops waiting for the psycopg3
pool to self-heal and asks the container's restart policy for a fresh process -- the same
recovery a manual ``docker restart`` provided in the 2026-08-08 nox incident, now automatic.
"""
