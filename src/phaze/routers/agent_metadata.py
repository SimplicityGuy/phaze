"""PUT /api/internal/agent/metadata/{file_id} -- idempotent tag-metadata write (phase-25)."""

from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.file import FileRecord, FileState
from phaze.models.metadata import FileMetadata
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_metadata import MetadataFailurePayload, MetadataFailureResponse, MetadataWriteRequest, MetadataWriteResponse
from phaze.services.scheduling_ledger import clear_ledger_entry


router = APIRouter(prefix="/api/internal/agent/metadata", tags=["agent-internal"])

# Bound persisted failure detail to the payload's wire bound (T-81-03-04). The column is
# ``Text`` (unbounded), so truncate the composed ``reason: error`` string defensively before
# persist -- the same DoS-via-huge-string class the ``error`` field's ``max_length`` caps.
_ERROR_MESSAGE_MAX = 2000
# Fixed marker persisted when an OLD (bodyless) agent POSTs the terminal ack: the failure is
# still durable (``failed_at`` set) but carries no agent triage detail (D-10 / CR-02).
_BODYLESS_FAILURE_MESSAGE = "metadata extraction failed (no detail -- legacy agent)"


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

    State advance (260707-rc4): after the upsert, in the SAME transaction,
    guardedly advance the file DISCOVERED -> METADATA_EXTRACTED so a metadata
    callback actually unblocks the pipeline (get_files_by_state gates the
    fingerprint stage on METADATA_EXTRACTED; the UI renders f.state directly).
    The guard `state == FileState.DISCOVERED` mirrors agent_push.py:126 (WR-02):
    a parallel/late fingerprint or analyze callback that already advanced the
    file must NEVER be downgraded back to METADATA_EXTRACTED. The advance is
    NOT gated on `dumped` -- an empty-body success PUT still means extraction
    ran, so it must unblock the file too.
    """
    # CR-01 fix: only fields the client explicitly set participate in the UPDATE.
    dumped = body.model_dump(exclude_unset=True)
    # Stamp PK explicitly because FileMetadata.id has only a Python-side default,
    # which pg_insert bypasses.
    payload = {**dumped, "file_id": file_id, "id": uuid.uuid4()}
    stmt = pg_insert(FileMetadata).values([payload])
    if dumped:
        # `set_` covers the user-provided fields (D-14 field-level LWW) PLUS an
        # UNCONDITIONAL failure-marker clear (D-13): a real success must wipe any
        # `failed_at`/`error_message` left by a prior `report_metadata_failed`, else a
        # successful retry reads FAILED forever. `failed_at`/`error_message` sit OUTSIDE
        # `exclude_unset` (the wire body never carries them). Excludes file_id AND id from
        # the SET clause (both are conflict-target / immutable PK).
        stmt = stmt.on_conflict_do_update(
            index_elements=["file_id"],
            set_={**{k: stmt.excluded[k] for k in dumped}, "failed_at": None, "error_message": None},
        )
    else:
        # Empty body -- no user field to UPDATE, but STILL clear the failure marker on an
        # existing row (D-13 sharp edge): an empty-body success PUT after a failure means
        # extraction ran, so it must clear `failed_at`/`error_message`. A fresh INSERT sets
        # both NULL anyway; a `DO NOTHING` here would strand the marker. Never an empty SET.
        stmt = stmt.on_conflict_do_update(
            index_elements=["file_id"],
            set_={"failed_at": None, "error_message": None},
        )
    await session.execute(stmt)
    # 260707-rc4: guardedly advance DISCOVERED -> METADATA_EXTRACTED in the SAME transaction
    # as the upsert so a metadata callback unblocks the fingerprint stage + UI. Guarded on
    # state == DISCOVERED (mirrors agent_push.py:126 WR-02): a parallel/late fingerprint or
    # analyze callback that already advanced the file must not be downgraded. Fires on every
    # success PUT (NOT gated on `dumped`) -- an empty-body success still means extraction ran.
    await session.execute(
        update(FileRecord).where(FileRecord.id == file_id, FileRecord.state == FileState.DISCOVERED).values(state=FileState.METADATA_EXTRACTED)
    )
    # Phase 45 (L-02): clear the extract_file_metadata:<file_id> ledger row in the SAME
    # transaction as the metadata upsert. Key from the PATH file_id ONLY (AUTH-01 / T-45-05).
    await clear_ledger_entry(session, f"extract_file_metadata:{file_id}")
    await session.commit()
    return MetadataWriteResponse(agent_id=agent.id, file_id=file_id)


@router.post("/{file_id}/failed", status_code=status.HTTP_200_OK, response_model=MetadataFailureResponse)
async def report_metadata_failed(
    file_id: uuid.UUID,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
    body: MetadataFailurePayload | None = None,
) -> MetadataFailureResponse:
    """Terminal-ack for a retries-exhausted ``extract_file_metadata`` run (Phase 45 L-02 / CR-02; Phase 81 FAIL-02).

    Two effects, same transaction:

    1. **Persist a durable failure marker (FAIL-02 / D-01):** upsert a ``metadata`` row with
       ``failed_at`` set and payload columns NULL. ``done(metadata)`` is ``EXISTS metadata
       WHERE failed_at IS NULL`` (D-02, stage_status.py), so this failure-only row derives
       FAILED -- never DONE -- making a terminally-failed metadata file visible in derivation
       and counts. Before Phase 81 this endpoint wrote nothing (the latent bug FAIL-02 closes).
       Metadata deliberately has NO backfill (D-03): no historical source ever persisted a
       marker, so this is the go-forward writer only.
    2. **Clear the scheduling-ledger row (CR-02):** ``put_metadata`` clears on SUCCESS; this
       endpoint closes the terminal-failure hole so EVERY run clears
       ``extract_file_metadata:<file_id>`` exactly once. Without it, a terminally-failed file
       stays in ``get_metadata_pending_files`` forever and ``recover_orphaned_work`` re-enqueues
       it on every recovery pass (the unbounded loop the ledger prevents).

    Version-skew safety (D-10 / T-81-03-03): ``body`` is optional with a ``None`` default (NO
    ``Body()`` wrapper) so a BODYLESS POST from an OLD agent image binds ``None``, returns 200,
    and STILL persists the marker + clears the ledger. A NEW agent's triage body populates
    ``error_message`` as ``"<reason>: <error>"`` (bounded). A present body with an unknown field
    422s (``extra='forbid'``, T-81-03-02).

    ``agent`` is bound from the auth dep (token, never body -- AUTH-01); BOTH the failure-row
    ``file_id`` and the ledger clear key are reconstructed from the PATH ``file_id`` ONLY, never
    the body (T-45-05 / T-81-03-01), so a forged request cannot redirect either write.
    """
    # T-81-03-04: bound the persisted detail. The `error` field is already `max_length=2000`
    # at the wire; truncate the composed message defensively (the column is unbounded Text).
    error_message = f"{body.reason}: {body.error}"[:_ERROR_MESSAGE_MAX] if body is not None else _BODYLESS_FAILURE_MESSAGE
    # FAIL-02 / D-01: durable failure row -- payload columns stay NULL so `done(metadata)`
    # (EXISTS ... failed_at IS NULL) reads FAILED. Server-set `failed_at=func.now()`; the same
    # shared `pg_insert(...).on_conflict_do_update` idiom as `put_metadata`. Stamp the PK
    # explicitly because `FileMetadata.id` has a Python-only default that `pg_insert` bypasses.
    now = func.now()
    stmt = pg_insert(FileMetadata).values([{"file_id": file_id, "id": uuid.uuid4(), "failed_at": now, "error_message": error_message}])
    stmt = stmt.on_conflict_do_update(index_elements=["file_id"], set_={"failed_at": now, "error_message": error_message})
    await session.execute(stmt)
    # CR-02: clear the ledger row in the SAME transaction. Key from the PATH file_id ONLY (T-45-05).
    await clear_ledger_entry(session, f"extract_file_metadata:{file_id}")
    await session.commit()
    return MetadataFailureResponse(agent_id=agent.id, file_id=file_id, cleared=True)
