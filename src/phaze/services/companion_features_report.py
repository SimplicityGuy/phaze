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

from phaze.constants import INGESTIBLE_COMPANION_EXTENSIONS
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


def read_companion_records(paths: Iterable[str]) -> tuple[list[CompanionFeaturesRecord], int]:
    """Read every companion in ``paths`` (NFC row keys); returns ``(records, unreadable)``. Blocking I/O."""
    records: list[CompanionFeaturesRecord] = []
    unreadable = 0
    for path in paths:
        if not is_companion_path(path):
            continue
        try:
            # phaze-9pg11: the row key is NFC; the on-disk entry may be NFD.
            reading = read_companion(resolve_media_path(path))
        except OSError as exc:
            unreadable += 1
            logger.warning("companion features: unreadable companion skipped", path=path, error=str(exc))
            continue
        records.append(CompanionFeaturesRecord.from_reading(path, reading))
    return records, unreadable


async def report_companion_features(api: PhazeAgentClient, paths: Iterable[str]) -> int:
    """Read the companions among ``paths`` and POST their features; returns how many the control plane stored."""
    wanted = [path for path in paths if is_companion_path(path)]
    if not wanted:
        return 0
    records, _unreadable = await asyncio.to_thread(read_companion_records, wanted)
    if not records:
        return 0
    try:
        response = await api.post_companion_features(CompanionFeaturesChunk(features=records))
    except AgentApiError as exc:
        logger.warning("companion features not reported; the backfill will cover them", count=len(records), error=str(exc))
        return 0
    return response.stored
