"""POST /api/internal/agent/files -- chunked, idempotent file upsert (phase-25 D-20..D-22).

Idempotent on the composite natural key `(agent_id, original_path)` via
`INSERT ... ON CONFLICT DO UPDATE`.

Phase 35 (D-06): this handler NO LONGER auto-enqueues the metadata-extraction task.
Metadata extraction is operator-triggered ONLY (MANUAL-META) -- discovery just persists
rows. The `enqueued` field of the response is retained for schema stability and is always 0.

Per AUTH-01: `agent_id` comes from `Depends(get_authenticated_agent)` -- the
request schema has no agent_id field, so accidental body forgery returns
422 `extra_forbidden`.
"""

from typing import Annotated, Any
import unicodedata
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import Executable, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.file import FileRecord, FileState
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_files import FileUpsertChunk, FileUpsertResponse


router = APIRouter(prefix="/api/internal/agent/files", tags=["agent-internal"])


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
    - Phase 35 (D-06): does NOT auto-enqueue the metadata-extraction task. The
      `enqueued` count is always 0 (metadata extraction is operator-triggered only).
    - Returns `(upserted, inserted, enqueued)` counts.
    """
    # Phase 27 D-09 + D-18 + D-21: resolve batch_id BEFORE the records loop.
    # Cross-tenant guard returns 403 BEFORE any FileRecord insert -- mirrors the
    # Phase 26 D-08 placement in agent_proposals.py:62-76 (and the new
    # agent_scan_batches.py PATCH handler). T-27-02 mitigation: a leaked
    # batch_id cannot be probed by attempting an upsert, because the 403
    # rejection precedes the records loop.
    if body.batch_id is not None:
        batch = await session.get(ScanBatch, body.batch_id)
        if batch is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="scan batch not found")
        if batch.agent_id != agent.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="scan batch does not belong to authenticated agent",
            )
        resolved_batch_id = batch.id
    else:
        # D-18: batch_id omitted -> resolve the calling agent's LIVE sentinel
        # batch from the bearer-token-derived agent_id. The partial unique index
        # `uq_scan_batches_agent_id_live` guarantees exactly one row exists for
        # any registered agent (Phase 24 D-11 + D-12), so `.scalar_one()` is
        # safe -- a missing sentinel is an operator-actionable invariant
        # violation (500-level), not a 4xx contract failure.
        stmt = select(ScanBatch.id).where(
            ScanBatch.agent_id == agent.id,
            ScanBatch.status == ScanStatus.LIVE.value,
        )
        resolved_batch_id = (await session.execute(stmt)).scalar_one()

    # 1. Build raw record dicts with agent_id stamped from auth dep (NEVER from body)
    raw_records: list[dict[str, Any]] = []
    for r in body.files:
        data = r.model_dump()
        # RESEARCH Pitfall 7: NFC-normalize defensively
        data["original_path"] = unicodedata.normalize("NFC", data["original_path"])
        data["agent_id"] = agent.id  # AUTH-01 -- stamped from auth, NEVER from body
        data["state"] = FileState.DISCOVERED  # server stamps initial state
        data["id"] = uuid.uuid4()  # server-generates new id; ON CONFLICT preserves existing id
        data["batch_id"] = resolved_batch_id  # Phase 27 D-09/D-18 -- server resolves; never from body
        raw_records.append(data)

    # 2. RESEARCH Pitfall 4: same-chunk dedup on (original_path) -- last write wins.
    # Postgres rejects multiple rows targeting the same conflict-target within one stmt.
    deduped: dict[str, dict[str, Any]] = {}
    for rec in raw_records:
        deduped[rec["original_path"]] = rec
    records = list(deduped.values())

    # 3. UPSERT with insert-detection (RESEARCH Pattern 2; D-12 + D-21).
    # Mirrors services/ingestion.py:103-117. `inserted` (xmax = 0) is retained so the
    # response can report how many rows were newly INSERTed vs updated.
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
        FileRecord.original_path,
        literal_column("(xmax = 0)").label("inserted"),
    )
    result = await session.execute(upsert_stmt)
    rows = result.all()
    await session.commit()

    # Phase 35 (D-06): NO auto-enqueue of the metadata-extraction task. Discovery persists
    # rows; metadata extraction is operator-triggered only (MANUAL-META). `enqueued` is
    # always 0, kept on the response for schema stability.
    return FileUpsertResponse(
        agent_id=agent.id,
        upserted=len(rows),
        inserted=sum(1 for r in rows if r.inserted),
        enqueued=0,
    )
