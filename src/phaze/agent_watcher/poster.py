"""Single-record POST adapter for the always-on watcher.

``Poster.post_one`` is the asyncio-side terminal step: given a single settled
path, it computes file size + SHA-256 off-loop (``asyncio.to_thread``), builds
a one-record :class:`FileUpsertChunk`, and POSTs it via :class:`PhazeAgentClient`.

Invariants:

- **D-18 (LIVE-sentinel resolution):** the chunk omits ``batch_id`` entirely.
  The controller's ``upsert_files`` handler resolves the calling agent's LIVE
  sentinel from the bearer token (see ``phaze.routers.agent_files``). Setting
  ``batch_id`` here would attribute watcher events to a stale scan batch.
- **Vanished path:** rsync's atomic rename and transient unmounts
  can race the debouncer's settle window. If ``stat`` or ``compute_sha256``
  raises :class:`OSError`, the entry is dropped with a WARNING (raised from
  DEBUG -- a DEBUG-level drop is invisible at the watcher's default INFO
  level, and on a Unicode-normalization-sensitive filesystem an ENOENT here
  is NOT always a transient race: it is also what a permanently-mismatched
  path (e.g. an NFD-named file whose handle diverged from disk) looks like,
  and that case never self-heals). A single OSError on one path MUST NOT
  crash the sweep loop -- the user's next manual scan will pick up any
  genuinely-missed files.
- **Pitfall 3 (NFC drift):** ``path`` itself is used VERBATIM as the
  filesystem handle for ``stat``/hashing -- it is NEVER normalized before
  that (mirrors ``phaze.tasks.scan.scan_directory``, which stats the raw
  ``os.walk`` path). Only the three outgoing record fields
  (``original_path``, ``original_filename``, ``current_path``) are
  NFC-normalized before being serialized into the FileUpsertRecord, so the
  DB-facing keys stay canonical while the on-disk lookup stays byte-exact.
- **Moves (phaze-oxn2m):** a path reached by an in-tree move is posted with its lineage
  (``previous_paths``) to ``/api/internal/agent/files/move``, so the controller re-points the row
  the file was already posted under rather than adding a second one. A control plane that
  predates that route answers 404 (any 4xx is treated the same way), and the record is then
  posted through the plain upsert -- what every move did before. Agent and control plane deploy
  separately, so this fallback is the backward-compatibility contract, not an error path.
- **T-27-04 (no bearer leakage):** the only client surfaces exposed here are
  ``self._client.upsert_files(chunk)`` and ``self._client.move_file(request)``. Exception logs go through
  ``logger.exception`` which captures the traceback for the AgentApiError
  (already redacted to ``METHOD path -> status``), never
  the client or chunk repr.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING
import unicodedata

import structlog

from phaze.schemas.agent_files import MOVE_LINEAGE_MAX, FileMoveRequest, FileUpsertChunk, FileUpsertRecord
from phaze.services.agent_client import (
    AgentApiClientError,
    AgentApiError,
    AgentApiServerError,
    PhazeAgentClient,
)
from phaze.services.hashing import compute_sha256


if TYPE_CHECKING:
    from collections.abc import Sequence


logger = structlog.get_logger(__name__)


class Poster:
    """Single-record POST adapter; resilient to vanished-path races (Pitfall 1)."""

    def __init__(self, client: PhazeAgentClient, agent_id: str) -> None:
        self._client = client
        # The controller binds identity from the bearer; this value is diagnostic context only.
        self._agent_id = agent_id

    async def post_one(self, path: str, previous_paths: Sequence[str] = ()) -> None:
        """POST one settled path as a chunk-of-1 to /api/internal/agent/files.

        With ``previous_paths`` (the names it was moved away from, oldest first) the record goes
        to /api/internal/agent/files/move instead, falling back to the plain upsert on a 4xx.

        Failure modes:
            OSError on stat/SHA-256 -> WARNING log, return (Pitfall 1).
            AgentApiClientError (4xx, non-auth) -> ERROR log, return (drop).
            AgentApiServerError (5xx after retries) -> ERROR log, return (drop;
                user's next manual scan recovers).
            AgentApiError (catch-all) -> ERROR log, return (drop).

        No exception escapes this method: the caller's sweep loop must keep
        running across transient failures of any single record.
        """
        p = Path(path)
        try:
            file_size = await asyncio.to_thread(lambda: p.stat().st_size)
            sha256 = await asyncio.to_thread(compute_sha256, p)
        except OSError as exc:
            # Atomic rename and transient unmount -- but also a
            # permanently-mismatched handle (e.g. an NFD-named file on a
            # normalization-sensitive filesystem) looks identical from here.
            # Keep this at WARNING so non-transient drops remain operator-visible.
            logger.warning("watcher: path vanished before post; dropping path=%s err=%s", path, exc)
            return

        record = FileUpsertRecord(
            sha256_hash=sha256,
            # NFC-normalize every path field independently so a future
            # refactor that splits original_path from current_path stays correct.
            original_path=unicodedata.normalize("NFC", path),
            original_filename=unicodedata.normalize("NFC", p.name),
            current_path=unicodedata.normalize("NFC", path),
            file_type=p.suffix.lower().lstrip("."),
            file_size=file_size,
        )
        try:
            if previous_paths and await self._moved(record, previous_paths):
                return
            await self._client.upsert_files(FileUpsertChunk(files=[record]))  # D-18: batch_id omitted; controller resolves LIVE.
        except AgentApiClientError:
            logger.exception("watcher: 4xx posting path=%s; dropping", path)
        except AgentApiServerError:
            logger.exception("watcher: 5xx posting path=%s; dropping (will recover via manual scan)", path)
        except AgentApiError:
            logger.exception("watcher: unknown error posting path=%s; dropping", path)

    async def _moved(self, record: FileUpsertRecord, previous_paths: Sequence[str]) -> bool:
        """POST ``record`` as a move; ``False`` means the control plane refused it and the caller should upsert.

        Only a 4xx falls back: it is what a control plane without the move route answers (404).
        5xx and transport errors propagate to ``post_one``'s handlers exactly like an upsert's.
        """
        lineage = [unicodedata.normalize("NFC", p) for p in previous_paths][-MOVE_LINEAGE_MAX:]
        try:
            await self._client.move_file(FileMoveRequest(previous_paths=lineage, file=record))
        except AgentApiClientError as exc:
            logger.warning("watcher: move post refused (%s); falling back to a plain upsert path=%s", exc, record.original_path)
            return False
        return True
