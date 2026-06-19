"""PUT /api/internal/agent/metadata/{file_id} -- idempotent tag-metadata write (phase-25)."""

from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.metadata import FileMetadata
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_metadata import MetadataWriteRequest, MetadataWriteResponse
from phaze.services.scheduling_ledger import clear_ledger_entry


router = APIRouter(prefix="/api/internal/agent/metadata", tags=["agent-internal"])


@router.put("/{file_id}", status_code=status.HTTP_200_OK, response_model=MetadataWriteResponse)
async def put_metadata(
    file_id: uuid.UUID,
    body: MetadataWriteRequest,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MetadataWriteResponse:
    """Idempotently replace tag-metadata for a file. Natural key: metadata.file_id (UQ from models/metadata.py:18).

    Field-level last-write-wins per D-14: only fields the client *explicitly
    set* (via Pydantic's exclude-unset dump semantics) land in the UPDATE
    SET clause. Unset Optional fields preserve whatever was already on the
    row, matching the natural read of "last write wins per field, not per
    row". `agent_id` comes from the auth dep, NEVER from body (AUTH-01).

    Empty-body PUT (`{}`) is a no-op against an existing row: the INSERT
    path falls back to `ON CONFLICT DO NOTHING` (Postgres rejects an empty
    SET clause). New rows still get an INSERT with whatever fields were set.

    PK NOTE: `FileMetadata.id` (models/metadata.py:17) declares
    `default=uuid.uuid4` as a Python-only default. The default fires only
    through ORM `session.add()`, NOT through `pg_insert(...).values()`. We
    therefore stamp `payload["id"] = uuid.uuid4()` explicitly so a fresh
    INSERT doesn't raise `NotNullViolationError`. ON CONFLICT DO UPDATE
    preserves the existing row's id (excluded.id is not in the SET clause).

    Gap closure: CR-01 (25-VERIFICATION.md). Previously the dump call was
    invoked without `exclude_unset=True`, so every Optional field with
    default `None` was written to the SET clause, NULLing prior column
    values on partial replays. Verified end-to-end in 25-VERIFICATION.md.
    """
    # CR-01 fix: only fields the client explicitly set participate in the UPDATE.
    dumped = body.model_dump(exclude_unset=True)
    # Stamp PK explicitly because FileMetadata.id has only a Python-side default,
    # which pg_insert bypasses.
    payload = {**dumped, "file_id": file_id, "id": uuid.uuid4()}
    stmt = pg_insert(FileMetadata).values([payload])
    if dumped:
        # `set_` covers ONLY the user-provided fields (D-14 field-level LWW);
        # excludes file_id AND id from the SET clause (both are conflict-target /
        # immutable PK -- existing row keeps its existing id).
        stmt = stmt.on_conflict_do_update(
            index_elements=["file_id"],
            set_={k: stmt.excluded[k] for k in dumped},
        )
    else:
        # Empty body -- no-op for existing rows; INSERT still happens for fresh ones.
        # Avoids Postgres "SET clause empty" syntax error.
        stmt = stmt.on_conflict_do_nothing(index_elements=["file_id"])
    await session.execute(stmt)
    # Phase 45 (L-02): clear the extract_file_metadata:<file_id> ledger row in the SAME
    # transaction as the metadata upsert. Key from the PATH file_id ONLY (AUTH-01 / T-45-05).
    await clear_ledger_entry(session, f"extract_file_metadata:{file_id}")
    await session.commit()
    return MetadataWriteResponse(agent_id=agent.id, file_id=file_id)
