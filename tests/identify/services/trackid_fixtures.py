"""Synthetic TrackId response shapes and a scripted stand-in for the core transport effect.

Shared by the TrackId provider tests. No slug, title, artist or id here is real; the shapes follow the documented
list and detail responses (docs/design/tracklist-provider-trackid.md).
"""

from collections.abc import Iterable
from datetime import UTC, datetime
import json
from typing import Any

from phaze.tracklist_providers.domain import DiscoveryBudget, OutcomeStatus, SourceRead, SourceReference


NOW = datetime(2026, 1, 1, tzinfo=UTC)


def list_page(indices: Iterable[int], row_count: int) -> dict[str, Any]:
    items = [{"id": 1000 + i, "slug": f"set-slug-{i:02d}", "title": f"Example Title {i}", "trackCount": 10 + i, "status": 3} for i in indices]
    return {"result": {"audiostreams": items, "rowCount": row_count}}


def row(start: object = "00:02:43", artist: object = "Artist A", title: object = "Title A", **extra: object) -> dict[str, object]:
    return {"artist": artist, "title": title, "label": "Label A", "startTime": start, "endTime": "00:05:00", **extra}


def process(process_id: int, rows: list[object], end_date: object = "2026-01-01T00:00:00Z") -> dict[str, object]:
    return {"id": process_id, "endDate": end_date, "detectionProcessMusicTracks": rows}


def detail_document(processes: list[dict[str, object]], *, status: int = 3) -> dict[str, object]:
    return {"status": status, "trackCount": 0, "detectionProcesses": processes}


def found(body: object, *, scope: str = "search:x", size: int | None = None) -> SourceRead:
    text = body if isinstance(body, str) else json.dumps(body)
    return SourceRead(status=OutcomeStatus.FOUND, code="http_200", scope=scope, text=text, bytes_read=size or len(text), retrieved_at=NOW)


class ScriptedEffects:
    """Hands back the scripted reads in order and records every reference it was asked to read."""

    def __init__(self, *script: SourceRead) -> None:
        self.script = list(script)
        self.reads: list[SourceReference] = []

    async def read(self, source: SourceReference, budget: DiscoveryBudget) -> SourceRead:
        del budget
        self.reads.append(source)
        if not self.script:
            raise AssertionError("an unscripted read: the provider asked for more than the test allowed")
        return self.script.pop(0)
