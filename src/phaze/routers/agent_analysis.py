"""PUT /api/internal/agent/analysis/{file_id} -- idempotent audio-analysis upsert (Phase 26 D-26).

Mirrors `agent_metadata.py` exactly: `pg_insert` + `on_conflict_do_update` with
`exclude_unset` semantics (Phase 25 CR-01 fix). Natural key:
`AnalysisResult.file_id` (unique=True per models/analysis.py:18).

Storage representation: `AnalysisResult.mood` and `.style` are `String(50)`
columns (Phase 5 schema). D-26's wire format is `dict[str, float]`. The handler
converts each incoming dict to a "key1=score1,key2=score2,key3=score3" summary
string bounded at 50 chars (top-3 highest-score keys, deterministic alphabetical
tiebreak on equal scores). This avoids an Alembic migration in Phase 26 while
preserving enough information for downstream displays. A future migration to
JSONB columns is deferred.

Overflow funnel: D-26's wire schema also includes `danceability` and `energy`
fields that have no dedicated column on `AnalysisResult` yet. The handler
funnels any wire field without a backing column into the existing `features`
JSONB column, so the wire contract is honored end-to-end without a migration
this phase. Plan 11's process_file rewrite produces the same wire shape; a
future migration can promote these to dedicated columns.

PK NOTE: `AnalysisResult.id` has a Python-only `default=uuid.uuid4`, which fires
only through ORM `session.add()`, NOT through `pg_insert(...).values()`. We
therefore stamp `payload["id"] = uuid.uuid4()` explicitly so a fresh INSERT
doesn't raise `NotNullViolationError`. `ON CONFLICT DO UPDATE` preserves the
existing row's id (`excluded.id` is not in the SET clause).
"""

from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult, AnalysisWindow
from phaze.models.cloud_job import CloudJob
from phaze.models.file import FileRecord, FileState
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_analysis import (
    AnalysisFailurePayload,
    AnalysisFailureResponse,
    AnalysisWritePayload,
    AnalysisWriteResponse,
)
from phaze.services import s3_staging
from phaze.services.scheduling_ledger import clear_ledger_entry


logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/internal/agent/analysis", tags=["agent-internal"])

# Columns that physically exist on the `analysis` table. Wire-format fields
# accepted by AnalysisWritePayload but absent here (e.g. `danceability`,
# `energy`) are bundled into the `features` JSONB column instead -- the model
# stays unchanged this phase (no Alembic migration), while D-26's wire contract
# is fully honored end-to-end. Plan 11's process_file rewrite produces the same
# wire shape; a future migration can promote these to dedicated columns.
_ANALYSIS_COLUMN_FIELDS: frozenset[str] = frozenset(
    {
        "bpm",
        "musical_key",
        "mood",
        "style",
        "fingerprint",
        "features",
        # Phase 43 windowed-analysis coverage -- dedicated columns (migration 021),
        # so these hit real columns instead of the `features` JSONB overflow (Pitfall 3).
        "fine_windows_analyzed",
        "fine_windows_total",
        "coarse_windows_analyzed",
        "coarse_windows_total",
        "sampled",
    }
)


def _summarize_dict_to_string(value: dict[str, float]) -> str:
    """Convert ``dict[str, float]`` to ``"k=v,k=v,k=v"`` summary, top-3 by score, max 50 chars.

    Sort order: primary by ``-score`` (descending), secondary by ``key``
    (ascending alphabetical) for deterministic tiebreak when scores are equal
    (W6 invariant -- verified by `test_summarize_dict_to_string`). Hard 50-char
    cap matches the existing `AnalysisResult.mood/style` `String(50)` columns.
    """
    items = sorted(value.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    summary = ",".join(f"{k}={v:.2f}" for k, v in items)
    return summary[:50]


async def _delete_staged_object_if_cloud(session: AsyncSession, file_id: uuid.UUID) -> None:
    """Delete the staged S3 object inline once the analysis result has landed (D-02, KSTAGE-04).

    Guarded on a ``cloud_job`` row existing for ``file_id``: an all-local file (no staging
    row) makes ZERO S3 calls -- no aioboto3 client is built and no S3 config is required, so
    deploys without object storage are completely unaffected (T-53-22). This is CRITICAL: the
    guard must short-circuit BEFORE any ``s3_staging`` call so the all-local path never raises
    on an unconfigured backend.

    When a staging row IS present, the staged object is provably no longer needed the moment the
    result lands, so it is deleted at that point (the inline-delete half of KSTAGE-04). The
    analysis result was already recorded above (record-first discipline), so a transient cleanup
    error is logged-and-swallowed -- a delete blip must never lose the recorded result (T-53-21).
    The bucket-lifecycle TTL (Plan 02) is the backstop for a missed delete; Phase 54's reconcile
    may invoke the same delete for an evicted Job. ``file_id`` is the PATH value only (AUTH-01).
    """
    has_cloud_job = (await session.execute(select(CloudJob.id).where(CloudJob.file_id == file_id))).scalar_one_or_none()
    if has_cloud_job is None:
        # All-local path: no staged object exists -> no S3 call, no client build (T-53-22).
        return
    try:
        await s3_staging.delete_staged_object(file_id)
    except Exception:
        # Record-first: the result is already written; a cleanup blip is logged, never raised,
        # so the recorded analysis result is preserved (T-53-21). The lifecycle TTL reaps the
        # object the inline delete missed.
        logger.warning("inline staged-object delete failed; lifecycle TTL will reap", file_id=str(file_id), exc_info=True)


@router.put("/{file_id}", status_code=status.HTTP_200_OK, response_model=AnalysisWriteResponse)
async def put_analysis(
    file_id: uuid.UUID,
    body: AnalysisWritePayload,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AnalysisWriteResponse:
    """Idempotently upsert AnalysisResult for a file. Natural key: ``analysis.file_id`` (UQ).

    Field-level last-write-wins per D-14 / Phase 25 CR-01: only fields the
    client *explicitly set* (via Pydantic's ``exclude_unset`` dump semantics)
    land in the UPDATE SET clause. Unset Optional fields preserve whatever was
    already on the row. `agent_id` comes from the auth dep, NEVER from body
    (AUTH-01); `extra='forbid'` on the payload returns 422 on attempted forgery.

    Storage conversion: incoming ``mood`` and ``style`` dicts are reduced to a
    bounded summary string before storage (see `_summarize_dict_to_string`),
    because the existing columns are ``String(50)``. Future migration to JSONB
    will lift this constraint without changing the wire contract.

    Empty-body PUT (``{}``) is a no-op against an existing row: the INSERT path
    falls back to ``ON CONFLICT DO NOTHING`` (Postgres rejects an empty SET
    clause). New rows still get an INSERT (with all NULL fields) on first call.
    """
    # CR-01 fix: only fields the client explicitly set participate in the UPDATE.
    dumped = body.model_dump(exclude_unset=True)

    # `windows` is the per-window child time-series, NOT an aggregate column. Pop it
    # out of the aggregate dump BEFORE the overflow funnel so it never lands in the
    # `features` JSONB. Child rows are replaced separately, after the aggregate upsert,
    # guarded on `body.windows is not None` (partial-PUT). The bool of an empty list is
    # falsy, so read the field off `body` directly to distinguish [] from None.
    dumped.pop("windows", None)

    # Storage conversion at the boundary: AnalysisResult.mood/.style are String(50).
    # Wire format from essentia is dict[str, float]; we serialize to a "k=v,k=v"
    # summary bounded at 50 chars. The conversion stays inside ``dumped`` so the
    # rest of the upsert pipeline is identical to agent_metadata.py.
    for field in ("mood", "style"):
        raw = dumped.get(field)
        if isinstance(raw, dict):
            dumped[field] = _summarize_dict_to_string(raw)

    # Funnel any wire-format fields without a dedicated column into `features` JSONB.
    # D-26's wire schema includes `danceability`/`energy` (and future-proofs additions);
    # the model currently only has columns for bpm/musical_key/mood/style. The funnel
    # keeps the wire contract intact without requiring a migration this phase.
    overflow = {k: dumped.pop(k) for k in list(dumped) if k not in _ANALYSIS_COLUMN_FIELDS}
    if overflow:
        # Merge into any features the caller also set explicitly (avoid clobbering).
        existing_features = dumped.get("features")
        merged_features: dict[str, object] = dict(existing_features) if isinstance(existing_features, dict) else {}
        merged_features.update(overflow)
        dumped["features"] = merged_features

    # Stamp PK explicitly because AnalysisResult.id has a Python-only default,
    # which pg_insert bypasses.
    payload = {**dumped, "file_id": file_id, "id": uuid.uuid4()}
    stmt = pg_insert(AnalysisResult).values([payload])
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
        # Avoids Postgres "SET clause empty" syntax error (matches agent_metadata.py:65-68).
        stmt = stmt.on_conflict_do_nothing(index_elements=["file_id"])
    await session.execute(stmt)

    # Child-row replace (Phase 31, ANL-01): idempotently REPLACE this file's windows
    # in the SAME transaction as the aggregate upsert. Guarded on `is not None` so an
    # aggregate-only PUT (windows omitted) leaves existing windows untouched (partial-PUT).
    # A present-but-empty list explicitly clears all windows. The DELETE predicate and each
    # inserted row's file_id use the PATH `file_id` ONLY -- the body never carries a
    # file/window-owner id (cross-file-deletion mitigation, AUTH-01).
    if body.windows is not None:
        await session.execute(delete(AnalysisWindow).where(AnalysisWindow.file_id == file_id))
        if body.windows:
            # pg_insert bypasses the Python-only `default=uuid.uuid4` PK, so stamp `id`
            # explicitly per row (mirrors the aggregate path above).
            await session.execute(
                pg_insert(AnalysisWindow).values([{"id": uuid.uuid4(), "file_id": file_id, **w.model_dump()} for w in body.windows])
            )

    # Phase 43 state-advance: a non-empty write (any aggregate/coverage field the
    # client actually set) means analysis produced a real result, so advance the
    # file to ANALYZED in the SAME transaction. This is what was missing -- without
    # it every analyzed file stayed `discovered`, so re-triggers re-enqueued the
    # whole archive. An empty-body PUT (`{}`) leaves `dumped` falsy and is a no-op:
    # state is preserved. `file_id` is the PATH value only (AUTH-01).
    if dumped:
        await session.execute(update(FileRecord).where(FileRecord.id == file_id).values(state=FileState.ANALYZED))

    # Phase 45 (L-02): clear the agent-stage scheduling-ledger row in the SAME transaction
    # as the result write. The agent worker is Postgres-free, so this control-side callback
    # is the earliest control-visible moment the analyze outcome is known. Key is reconstructed
    # from the fixed function name + the PATH file_id ONLY (never a body field -- AUTH-01 /
    # T-45-05: a body field cannot redirect the clear to another file's key).
    await clear_ledger_entry(session, f"process_file:{file_id}")

    # D-02 inline delete: the staged S3 object is provably no longer needed now that the
    # success result is recorded. No-op (zero S3 calls) when no cloud_job row exists (all-local).
    await _delete_staged_object_if_cloud(session, file_id)

    await session.commit()
    return AnalysisWriteResponse(agent_id=agent.id, file_id=file_id)


@router.post("/{file_id}/failed", status_code=status.HTTP_200_OK, response_model=AnalysisFailureResponse)
async def report_analysis_failed(
    file_id: uuid.UUID,
    body: AnalysisFailurePayload,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AnalysisFailureResponse:
    """Mark a file's analysis as terminally failed (Phase 43).

    The Postgres-free worker calls this when windowed analysis terminally fails
    (timeout / crash / error) so the file leaves `discovered`/in-flight and lands
    in ``ANALYSIS_FAILED`` -- making the outcome durable and visible, and keeping
    re-triggers from re-enqueuing it. Mirrors ``put_analysis``'s auth + path-only
    ``file_id`` shape: ``agent`` comes from ``get_authenticated_agent`` (token,
    never body) and the UPDATE is scoped strictly to the PATH ``file_id`` so a
    forged body cannot fail an arbitrary file (AUTH-01, T-43-05). The
    ``reason``/``error`` detail is validated + bounded by the payload (T-43-06);
    the terminal state itself is the durable signal recorded here.
    """
    await session.execute(update(FileRecord).where(FileRecord.id == file_id).values(state=FileState.ANALYSIS_FAILED))
    # Phase 45 (L-02, locked decision #1 -- THE POISON CASE): a terminal analyze failure must
    # NOT recovery-re-queue. Clear the process_file:<file_id> ledger row in the SAME transaction
    # as the ANALYSIS_FAILED state write. Key from the PATH file_id ONLY (AUTH-01 / T-45-05).
    await clear_ledger_entry(session, f"process_file:{file_id}")
    # D-02 inline delete: a terminal failure is also a result-callback terminal outcome -- the
    # staged object is no longer needed. No-op (zero S3 calls) when no cloud_job row exists.
    await _delete_staged_object_if_cloud(session, file_id)
    await session.commit()
    logger.warning(
        "analysis_failed reported",
        file_id=str(file_id),
        agent_id=agent.id,
        reason=body.reason,
        error=body.error,
    )
    return AnalysisFailureResponse(agent_id=agent.id, file_id=file_id)
