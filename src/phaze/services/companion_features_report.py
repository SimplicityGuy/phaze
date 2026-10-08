"""Read companions on the agent and report their content features to the control plane (phaze-osy6j).

The one reporting path every agent-side producer shares: the scan (``tasks/scan.py``, after each
upsert chunk), the watcher (``agent_watcher/poster.py``, after each settled post) and the backfill
task (``tasks/companion_features.py``). Each hands over the NFC ``original_path`` values it just
posted -- non-companions are ignored here, so a producer never has to filter.

Best-effort by design. Features are derived data that ``phaze backfill companion-features`` can
always re-create, so a failure here must never cost the ingest that triggered it: an unreadable file
is skipped, and any ``AgentApiError`` -- including the 404 of a control plane that predates this
route -- is logged and swallowed rather than aborting a scan or dropping a watcher event.

Agent-safe: no ``phaze.database`` / ``sqlalchemy`` (``tests/shared/core/test_task_split.py``).
"""

from __future__ import annotations

import asyncio
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import structlog

from phaze.constants import INGESTIBLE_COMPANION_EXTENSIONS, is_quarantined
from phaze.schemas.agent_companion_features import CompanionFeaturesChunk, CompanionFeaturesRecord
from phaze.services.agent_client import AgentApiError
from phaze.services.companion_features import read_companion
from phaze.services.media_path_resolve import resolve_media_path


if TYPE_CHECKING:
    from collections.abc import Iterable

    from phaze.services.agent_client import PhazeAgentClient


logger = structlog.get_logger(__name__)


def is_companion_path(path: str) -> bool:
    """True when ``path`` has one of the ingestible companion extensions."""
    return PurePosixPath(path).suffix.lower() in INGESTIBLE_COMPANION_EXTENSIONS


def read_companion_records(targets: Iterable[tuple[str, str]], *, follow_symlinks: bool = True) -> tuple[list[CompanionFeaturesRecord], int]:
    """Read each ``(row_key, read_path)`` companion; returns ``(records, unreadable)``. Blocking I/O.

    ``row_key`` is the NFC ``original_path`` the features are reported under -- what matches them to
    their ``files`` row. ``read_path`` is the handle actually opened; the backfill task passes the
    containment-RESOLVED path there (with ``follow_symlinks=False``), never the control-plane string.
    A non-companion or a quarantined path is skipped without being opened.
    """
    records: list[CompanionFeaturesRecord] = []
    unreadable = 0
    for row_key, read_path in targets:
        if not is_companion_path(row_key) or is_quarantined(row_key):
            continue
        try:
            reading = read_companion(read_path, follow_symlinks=follow_symlinks)
        except OSError as exc:
            unreadable += 1
            logger.warning("companion features: unreadable companion skipped", path=row_key, error=str(exc))
            continue
        records.append(CompanionFeaturesRecord.from_reading(row_key, reading))
    return records, unreadable


def _ingest_targets(paths: Iterable[str]) -> list[tuple[str, str]]:
    """Ingest's ``(row_key, read_path)`` pairs. Blocking (``resolve_media_path`` may list a directory).

    The scan and the watcher hand over paths from their OWN walk or event -- the same handle they just
    hashed -- so no control-plane string is involved. phaze-9pg11: the row key is NFC, the on-disk entry
    may be NFD.
    """
    return [(path, resolve_media_path(path)) for path in paths]


async def report_companion_features(api: PhazeAgentClient, paths: Iterable[str]) -> int:
    """Read the companions among ``paths`` and POST their features; returns how many the control plane stored."""
    wanted = [path for path in paths if is_companion_path(path)]
    if not wanted:
        return 0
    records, _unreadable = await asyncio.to_thread(lambda: read_companion_records(_ingest_targets(wanted)))
    if not records:
        return 0
    try:
        response = await api.post_companion_features(CompanionFeaturesChunk(features=records))
    except AgentApiError as exc:
        # Never silent: the rows keep no features row, so `phaze backfill companion-features` selects
        # them as missing (tests/discovery/test_companion_features_producers.py pins the recovery).
        logger.warning("companion features not reported; the backfill will cover them", count=len(records), error=str(exc))
        return 0
    return response.stored
