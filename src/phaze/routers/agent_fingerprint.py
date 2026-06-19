"""PUT /api/internal/agent/fingerprints/{file_id}/{engine} -- idempotent fingerprint write (phase-25)."""

from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.fingerprint import FingerprintResult
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_fingerprint import FingerprintWriteRequest, FingerprintWriteResponse
from phaze.services.scheduling_ledger import clear_ledger_entry


router = APIRouter(prefix="/api/internal/agent/fingerprints", tags=["agent-internal"])


@router.put("/{file_id}/{engine}", status_code=status.HTTP_200_OK, response_model=FingerprintWriteResponse)
async def put_fingerprint(
    file_id: uuid.UUID,
    engine: str,
    body: FingerprintWriteRequest,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FingerprintWriteResponse:
    """Idempotently replace fingerprint result. Natural key: (file_id, engine) from models/fingerprint.py:25 (ix_fprint_file_engine).

    Last-write-wins per D-14. `agent_id` comes from auth dep, NEVER from body (AUTH-01).

    PK NOTE: `FingerprintResult.id` declares Python-only `default=uuid.uuid4`
    (no server_default). `pg_insert(...).values()` bypasses ORM defaults, so
    we stamp `payload["id"] = uuid.uuid4()` explicitly. ON CONFLICT DO UPDATE
    preserves the existing row's id.
    """
    # Stamp PK explicitly because FingerprintResult.id has only a Python-side default.
    payload = {**body.model_dump(), "file_id": file_id, "engine": engine, "id": uuid.uuid4()}
    stmt = pg_insert(FingerprintResult).values([payload])
    stmt = stmt.on_conflict_do_update(
        index_elements=["file_id", "engine"],  # composite UQ per models/fingerprint.py:25
        set_={
            "status": stmt.excluded.status,
            "error_message": stmt.excluded.error_message,
        },
    )
    await session.execute(stmt)
    # Phase 45 (L-02): clear the fingerprint_file:<file_id> ledger row in the SAME transaction
    # as the fingerprint upsert. The ledger key is a SINGLE key per file (NOT per engine -- the
    # fingerprint_file task enqueues ONE job per file, keyed by file_id), so clearing on any
    # engine PUT is correct; a second engine PUT is a clean no-op. Key from the PATH file_id
    # ONLY (engine is NOT part of the ledger key; AUTH-01 / T-45-05).
    await clear_ledger_entry(session, f"fingerprint_file:{file_id}")
    await session.commit()
    return FingerprintWriteResponse(agent_id=agent.id, file_id=file_id, engine=engine)
