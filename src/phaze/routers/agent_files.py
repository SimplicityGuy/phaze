"""POST /api/internal/agent/files -- chunked file upsert + auto-enqueue (phase-25 D-20..D-22).

Idempotent on the composite natural key `(agent_id, original_path)` via
`INSERT ... ON CONFLICT DO UPDATE`. For each row that was actually INSERTed
(RETURNING (xmax = 0) AS inserted) AND has a music/video file_type per
`EXTENSION_MAP`, enqueues `extract_file_metadata` onto the per-agent SAQ
queue `phaze-agent-<agent.id>` (D-22).

Per AUTH-01: `agent_id` comes from `Depends(get_authenticated_agent)` -- the
request schema has no agent_id field, so accidental body forgery returns
422 `extra_forbidden`.
"""

import logging
from typing import Annotated, Any
import unicodedata
import uuid

from fastapi import APIRouter, Depends, status
from saq import Queue
from sqlalchemy import Executable, literal_column
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.config import settings
from phaze.constants import EXTENSION_MAP, FileCategory
from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.file import FileRecord, FileState
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_files import FileUpsertChunk, FileUpsertResponse


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/internal/agent/files", tags=["agent-internal"])

_EXTRACTABLE: frozenset[FileCategory] = frozenset({FileCategory.MUSIC, FileCategory.VIDEO})


@router.post("", status_code=status.HTTP_200_OK, response_model=FileUpsertResponse)
async def upsert_files(
    body: FileUpsertChunk,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FileUpsertResponse:
    """Idempotently upsert a chunk of FileRecord rows for the calling agent.

    - Stamps `agent_id` from auth dep (NEVER from body -- AUTH-01).
    - NFC-normalizes `original_path` on receive (RESEARCH Pitfall 7).
    - Server-side dedups same-chunk records on `original_path` (RESEARCH Pitfall 4)
      to avoid Postgres "cannot affect row a second time" errors on duplicate
      natural keys within one statement.
    - Returns `(upserted, inserted, enqueued)` counts.
    """
    # 1. Build raw record dicts with agent_id stamped from auth dep (NEVER from body)
    raw_records: list[dict[str, Any]] = []
    for r in body.files:
        data = r.model_dump()
        # RESEARCH Pitfall 7: NFC-normalize defensively
        data["original_path"] = unicodedata.normalize("NFC", data["original_path"])
        data["agent_id"] = agent.id  # AUTH-01 -- stamped from auth, NEVER from body
        data["state"] = FileState.DISCOVERED  # server stamps initial state
        data["id"] = uuid.uuid4()  # server-generates new id; ON CONFLICT preserves existing id
        raw_records.append(data)

    # 2. RESEARCH Pitfall 4: same-chunk dedup on (original_path) -- last write wins.
    # Postgres rejects multiple rows targeting the same conflict-target within one stmt.
    deduped: dict[str, dict[str, Any]] = {}
    for rec in raw_records:
        deduped[rec["original_path"]] = rec
    records = list(deduped.values())

    # 3. UPSERT with insert-detection (RESEARCH Pattern 2; D-12 + D-21).
    # Mirrors services/ingestion.py:103-117 with `.returning(...)` extended.
    base_stmt = pg_insert(FileRecord).values(records)
    upsert_stmt: Executable = base_stmt.on_conflict_do_update(
        index_elements=["agent_id", "original_path"],  # composite UQ from models/file.py:61
        set_={
            "sha256_hash": base_stmt.excluded.sha256_hash,
            "file_size": base_stmt.excluded.file_size,
            "state": base_stmt.excluded.state,
            "batch_id": base_stmt.excluded.batch_id,
            "file_type": base_stmt.excluded.file_type,
        },
    ).returning(
        FileRecord.id,
        FileRecord.file_type,
        literal_column("(xmax = 0)").label("inserted"),
    )
    result = await session.execute(upsert_stmt)
    rows = result.all()
    await session.commit()

    # 4. Auto-enqueue extract_file_metadata for INSERTed music/video files (D-20, D-22).
    # Per-agent queue per CONTEXT.md D-22; per-call construction per RESEARCH Pattern 3.
    # Discretion: AFTER commit; on enqueue failure, log + continue (do NOT raise).
    enqueued = 0
    queue_name = f"phaze-agent-{agent.id}"
    queue = Queue.from_url(settings.redis_url, name=queue_name)
    try:
        for row in rows:
            if not row.inserted:
                continue
            ext = "." + row.file_type.lower()
            if EXTENSION_MAP.get(ext, FileCategory.UNKNOWN) not in _EXTRACTABLE:
                continue
            try:
                await queue.enqueue("extract_file_metadata", file_id=str(row.id))
                enqueued += 1
            except Exception:
                # Enqueue is best-effort post-commit -- DB is the source of truth; the
                # operator can re-enqueue manually via Phase 27's UI on retryable failure.
                logger.exception("Failed to enqueue extract_file_metadata for file_id=%s agent_id=%s", row.id, agent.id)
    finally:
        await queue.disconnect()  # RESEARCH Pitfall 6

    return FileUpsertResponse(
        agent_id=agent.id,
        upserted=len(rows),
        inserted=sum(1 for r in rows if r.inserted),
        enqueued=enqueued,
    )
