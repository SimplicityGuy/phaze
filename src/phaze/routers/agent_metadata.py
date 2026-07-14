"""PUT /api/internal/agent/metadata/{file_id} -- idempotent tag-metadata write (phase-25)."""

from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.metadata import FileMetadata
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_metadata import MetadataFailurePayload, MetadataFailureResponse, MetadataWriteRequest, MetadataWriteResponse
from phaze.services.pg_text import sanitize_pg_text
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

    Phase 90 (D-09): the former DISCOVERED -> METADATA_EXTRACTED FileRecord.state
    CAS advance was removed. Pipeline progress no longer reads files.state -- the
    `metadata` marker upserted here is the sole derived authority (done(metadata) =
    EXISTS metadata WHERE failed_at IS NULL, stage_status.py), and the ON CONFLICT
    upsert makes a duplicate/late callback a safe no-op with no state guard needed.
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
    # Phase 90 (D-09): the DISCOVERED -> METADATA_EXTRACTED FileRecord.state CAS advance was removed
    # here. The `metadata` marker upserted above is now the sole idempotency + progress authority --
    # done(metadata) derives from `EXISTS metadata WHERE failed_at IS NULL` (stage_status.py), so the
    # ON CONFLICT upsert already makes a duplicate callback a safe no-op without a state guard.
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
       marker, so this is the go-forward writer only. **WR-01 guard (81-REVIEW):** the
       ``ON CONFLICT DO UPDATE`` carries a ``WHERE metadata.failed_at IS NOT NULL`` predicate so it
       only REFRESHES an already-failed row -- it never stamps ``failed_at`` onto a row that already
       reads DONE (a prior successful extraction with real tags, re-enqueued by
       ``POST /api/v1/extract-metadata`` and then timed out). A DONE row keeps its usable metadata;
       the marker (and thus the payload-NULL invariant this contract rests on) is untouched.
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
    # T-81-03-04 has two limbs and BOTH must hold:
    #   (a) oversized -- the `error` field is already `max_length=2000` at the wire; truncate the
    #       composed message defensively (the column is unbounded Text).
    #   (b) PG-invalid -- NUL passes pydantic validation (only lone surrogates are rejected there,
    #       as `string_unicode`), but Postgres rejects it with CharacterNotInRepertoireError. That
    #       aborts the transaction, which rolls back the marker upsert AND the ledger clear below,
    #       so the file is re-enqueued and fails identically forever. Sanitize BEFORE truncating:
    #       stripping can only shorten, so the bound still holds.
    error_message = sanitize_pg_text(f"{body.reason}: {body.error}")[:_ERROR_MESSAGE_MAX] if body is not None else _BODYLESS_FAILURE_MESSAGE
    # FAIL-02 / D-01: durable failure row -- payload columns stay NULL so `done(metadata)`
    # (EXISTS ... failed_at IS NULL) reads FAILED. Server-set `failed_at=func.now()`; the same
    # shared `pg_insert(...).on_conflict_do_update` idiom as `put_metadata`. Stamp the PK
    # explicitly because `FileMetadata.id` has a Python-only default that `pg_insert` bypasses.
    now = func.now()
    stmt = pg_insert(FileMetadata).values([{"file_id": file_id, "id": uuid.uuid4(), "failed_at": now, "error_message": error_message}])
    # WR-01 (81-REVIEW): guard the failure stamp so it NEVER downgrades a row that already reads DONE.
    # `POST /api/v1/extract-metadata` re-enqueues ALL music/video files regardless of state, so a file
    # that already succeeded (a `metadata` row with populated payload columns AND `failed_at IS NULL`)
    # can be re-extracted; a timeout on that re-run would otherwise stamp `failed_at` onto the good row,
    # deriving FAILED and losing `propose` eligibility. The `WHERE metadata.failed_at IS NOT NULL`
    # conflict predicate means the UPDATE fires ONLY on a row that is ALREADY a failure row (refresh the
    # marker); a DONE row is left untouched (a benign no-op, not an error). This preserves the
    # "a failure row has NULL payload columns" invariant `done(metadata) = EXISTS metadata WHERE failed_at
    # IS NULL` (D-02, stage_status.py) rests on -- `failed_at` still only ever coexists with NULL payload:
    # the INSERT path creates a payload-NULL failure row, and the UPDATE path only touches an
    # already-payload-NULL failure row. The ledger clear below stays UNCONDITIONAL either way (the run
    # terminated, so the row must clear to avoid the CR-02 unbounded recovery loop).
    stmt = stmt.on_conflict_do_update(
        index_elements=["file_id"], set_={"failed_at": now, "error_message": error_message}, where=FileMetadata.failed_at.isnot(None)
    )
    await session.execute(stmt)
    # CR-02: clear the ledger row in the SAME transaction. Key from the PATH file_id ONLY (T-45-05).
    await clear_ledger_entry(session, f"extract_file_metadata:{file_id}")
    await session.commit()
    return MetadataFailureResponse(agent_id=agent.id, file_id=file_id, cleared=True)
