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


router = APIRouter(prefix="/api/internal/agent/metadata", tags=["agent-internal"])


@router.put("/{file_id}", status_code=status.HTTP_200_OK, response_model=MetadataWriteResponse)
async def put_metadata(
    file_id: uuid.UUID,
    body: MetadataWriteRequest,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MetadataWriteResponse:
    """Idempotently replace tag-metadata for a file. Natural key: metadata.file_id (UQ from models/metadata.py:18).

    Last-write-wins per D-14: every column in `body.model_dump()` lands in the
    UPDATE set clause. `agent_id` comes from the auth dep, NEVER from body (AUTH-01).

    PK NOTE: `FileMetadata.id` (models/metadata.py:17) declares
    `default=uuid.uuid4` as a Python-only default. The default fires only
    through ORM `session.add()`, NOT through `pg_insert(...).values()`. We
    therefore stamp `payload["id"] = uuid.uuid4()` explicitly so a fresh
    INSERT doesn't raise `NotNullViolationError`. ON CONFLICT DO UPDATE
    preserves the existing row's id (excluded.id is not in the SET clause).
    """
    # Stamp PK explicitly because FileMetadata.id has only a Python-side default,
    # which pg_insert bypasses.
    payload = {**body.model_dump(), "file_id": file_id, "id": uuid.uuid4()}
    stmt = pg_insert(FileMetadata).values([payload])
    # `set_` covers ONLY the user-provided fields (D-14 last-write-wins);
    # excludes file_id AND id from the SET clause (both are conflict-target /
    # immutable PK -- existing row keeps its existing id).
    update_keys = set(body.model_dump().keys())
    stmt = stmt.on_conflict_do_update(
        index_elements=["file_id"],  # UQ on metadata.file_id per models/metadata.py:18
        set_={k: stmt.excluded[k] for k in update_keys},
    )
    await session.execute(stmt)
    await session.commit()
    return MetadataWriteResponse(agent_id=agent.id, file_id=file_id)
