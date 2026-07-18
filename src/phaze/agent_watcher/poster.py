"""Chunk-of-1 POST adapter for the always-on watcher (Phase 27 D-18, Pitfall 1).

``Poster.post_one`` is the asyncio-side terminal step: given a single settled
path, it computes file size + SHA-256 off-loop (``asyncio.to_thread``), builds
a one-record :class:`FileUpsertChunk`, and POSTs it via :class:`PhazeAgentClient`.

Critical invariants:

- **D-18 (LIVE-sentinel resolution):** the chunk omits ``batch_id`` entirely.
  The controller's ``upsert_files`` handler resolves the calling agent's LIVE
  sentinel from the bearer token (see ``phaze.routers.agent_files``). Setting
  ``batch_id`` here would attribute watcher events to a stale scan batch.
- **Pitfall 1 (vanished path):** rsync's atomic rename and transient unmounts
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
- **T-27-04 (no bearer leakage):** the only client surface exposed here is
  ``self._client.upsert_files(chunk)``. Exception logs go through
  ``logger.exception`` which captures the traceback for the AgentApiError
  (already redacted to ``METHOD path -> status`` per Phase 26 D-13), never
  the client or chunk repr.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import unicodedata

import structlog

from phaze.schemas.agent_files import FileUpsertChunk, FileUpsertRecord
from phaze.services.agent_client import (
    AgentApiClientError,
    AgentApiError,
    AgentApiServerError,
    PhazeAgentClient,
)
from phaze.services.hashing import compute_sha256


logger = structlog.get_logger(__name__)


class Poster:
    """Single-record POST adapter; resilient to vanished-path races (Pitfall 1)."""

    def __init__(self, client: PhazeAgentClient, agent_id: str) -> None:
        self._client = client
        # ``agent_id`` is the resolved /whoami identity; kept for diagnostic
        # log context (the controller binds agent_id from the bearer, never
        # from request body, per AUTH-01).
        self._agent_id = agent_id

    async def post_one(self, path: str) -> None:
        """POST one settled path as a chunk-of-1 to /api/internal/agent/files.

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
            # Pitfall 1: rsync atomic-rename, transient unmount -- but ALSO a
            # permanently-mismatched handle (e.g. an NFD-named file on a
            # normalization-sensitive filesystem) looks identical from here.
            # WARNING (raised from DEBUG): a DEBUG-level drop was invisible at
            # the watcher's default INFO level, silently and permanently
            # hiding non-transient cases with no operator-visible signal.
            logger.warning("watcher: path vanished before post; dropping path=%s err=%s", path, exc)
            return

        record = FileUpsertRecord(
            sha256_hash=sha256,
            # Pitfall 3: NFC-normalize every path field independently so a future
            # refactor that splits original_path from current_path stays correct.
            original_path=unicodedata.normalize("NFC", path),
            original_filename=unicodedata.normalize("NFC", p.name),
            current_path=unicodedata.normalize("NFC", path),
            file_type=p.suffix.lower().lstrip("."),
            file_size=file_size,
        )
        chunk = FileUpsertChunk(files=[record])  # D-18: batch_id omitted; controller resolves LIVE.
        try:
            await self._client.upsert_files(chunk)
        except AgentApiClientError:
            logger.exception("watcher: 4xx posting path=%s; dropping", path)
        except AgentApiServerError:
            logger.exception("watcher: 5xx posting path=%s; dropping (will recover via manual scan)", path)
        except AgentApiError:
            logger.exception("watcher: unknown error posting path=%s; dropping", path)
