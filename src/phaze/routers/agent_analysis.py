"""PUT /api/internal/agent/analysis/{file_id} -- idempotent audio-analysis upsert (Phase 26 D-26).

Mirrors `agent_metadata.py` exactly: `pg_insert` + `on_conflict_do_update` with
`exclude_unset` semantics (Phase 25 CR-01 fix). Natural key:
`AnalysisResult.file_id` (unique=True per models/analysis.py:19).

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

from typing import TYPE_CHECKING, Annotated, cast
import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from phaze.config import get_settings
from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult, AnalysisWindow
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_analysis import (
    AnalysisFailurePayload,
    AnalysisFailureResponse,
    AnalysisProgressPayload,
    AnalysisProgressResponse,
    AnalysisWritePayload,
    AnalysisWriteResponse,
)
from phaze.services import s3_staging
from phaze.services.bulk_insert import chunk_rows
from phaze.services.pg_text import sanitize_pg_text
from phaze.services.scheduling_ledger import clear_ledger_entry


if TYPE_CHECKING:
    from phaze.config import ControlSettings


logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/internal/agent/analysis", tags=["agent-internal"])

# Phase 81 (FAIL-01, D-07): bound the persisted analyze-failure detail to the payload's wire bound.
# `analysis.error_message` is `Text` (unbounded), so truncate the composed `reason: error` string
# defensively before persist -- the same DoS-via-huge-string class the `error` field's max_length caps.
# Mirrors agent_metadata.py's `_ERROR_MESSAGE_MAX` (FAIL-02) so both failure writers share the bound.
_ERROR_MESSAGE_MAX = 2000

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

    MKUE-02: the delete acts on the RECORDED ``staging_bucket`` (resolved to its ``BucketConfig``,
    never re-derived). A row whose ``staging_bucket`` is NULL (a compute row with no S3 object, or an
    unstaged file) ALSO short-circuits with zero S3 calls -- mirroring the all-local guard.

    When a bucketed staging row IS present, the staged object is provably no longer needed the moment
    the result lands, so it is deleted at that point (the inline-delete half of KSTAGE-04). The analysis
    result was already recorded above (record-first discipline), so a transient cleanup error is
    logged-and-swallowed -- a delete blip must never lose the recorded result (T-53-21). The
    bucket-lifecycle TTL (Plan 02) is the backstop for a missed delete; Phase 54's reconcile may invoke
    the same delete for an evicted Job. ``file_id`` is the PATH value only (AUTH-01).
    """
    row = (await session.execute(select(CloudJob.id, CloudJob.staging_bucket).where(CloudJob.file_id == file_id))).first()
    if row is None:
        # All-local path: no staged object exists -> no S3 call, no client build (T-53-22).
        return
    bucket = s3_staging.resolve_bucket_config(cast("ControlSettings", get_settings()), row.staging_bucket)
    if bucket is None:
        # Compute / unstaged row: no S3 object was staged -> skip the S3 op cleanly (no client build).
        return
    try:
        await s3_staging.delete_staged_object(file_id, bucket)
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
        # `set_` covers the user-provided fields (D-14 field-level LWW) PLUS an UNCONDITIONAL
        # failure-marker clear (Phase 81 FAIL-01 / D-13): a real analysis success must wipe any
        # `failed_at`/`error_message` left by a prior `report_analysis_failed`, else a successful
        # (re)analysis reads FAILED forever. `failed_at`/`error_message` sit OUTSIDE `exclude_unset`
        # (the wire body never carries them). Clearing `failed_at` here is ALSO what lets the
        # completion branch below stamp `analysis_completed_at` without violating the migration-033
        # XOR CHECK (both columns can never be non-NULL). Excludes file_id AND id from the SET clause
        # (both are conflict-target / immutable PK -- existing row keeps its existing id).
        stmt = stmt.on_conflict_do_update(
            index_elements=["file_id"],
            set_={**{k: stmt.excluded[k] for k in dumped}, "failed_at": None, "error_message": None},
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
            rows = [{"id": uuid.uuid4(), "file_id": file_id, **w.model_dump()} for w in body.windows]
            # phaze-syxv: CHUNKED, because an explicit multi-row VALUES binds
            # `len(rows) * params_per_row` parameters in ONE statement and PostgreSQL's Bind
            # message caps that at int16 (32767) -- 2,730 rows at this model's 12 parameters,
            # BELOW the ~2,880 windows a 24h recording produces and 18x below the schema's own
            # 50,000 cap. `chunk_rows` derives the split from the rows' actual parameter count
            # (see services/bulk_insert.py), so adding a column cannot silently reintroduce it.
            # ATOMICITY: every chunk executes on THIS session inside the SAME transaction as the
            # aggregate upsert and the preceding DELETE, so the replace stays all-or-nothing --
            # a half-written window set is never committed (it would read as a complete analysis).
            for chunk in chunk_rows(rows):
                await session.execute(pg_insert(AnalysisWindow).values(chunk))

    # Phase 43 state-advance: a non-empty write (any aggregate/coverage field the
    # client actually set) means analysis produced a real result, so advance the
    # file to ANALYZED in the SAME transaction. This is what was missing -- without
    # it every analyzed file stayed `discovered`, so re-triggers re-enqueued the
    # whole archive. An empty-body PUT (`{}`) leaves `dumped` falsy and is a no-op:
    # state is preserved. `file_id` is the PATH value only (AUTH-01).
    if dumped:
        # Phase 90 (D-09): the ANALYZED files.state write was removed; analysis_completed_at is now
        # the sole derived completion authority (done(analyze) reads it, not files.state).
        # Phase 57.1 (D-03 KEY RISK): stamp the completion discriminator in the SAME txn as the
        # result write. Server-set via func.now() ONLY -- analysis_completed_at is excluded from the
        # wire payload + _ANALYSIS_COLUMN_FIELDS, so a client cannot forge completion (T-57.1-12).
        # An in-flight/partial row (D-03 START upsert) skips this branch -> stays NULL, so the
        # proposal convergence gate (analysis_completed_at IS NOT NULL) can never batch it.
        await session.execute(update(AnalysisResult).where(AnalysisResult.file_id == file_id).values(analysis_completed_at=func.now()))

    # Phase 45 (L-02): clear the agent-stage scheduling-ledger row in the SAME transaction
    # as the result write. The agent worker is Postgres-free, so this control-side callback
    # is the earliest control-visible moment the analyze outcome is known. Key is reconstructed
    # from the fixed function name + the PATH file_id ONLY (never a body field -- AUTH-01 /
    # T-45-05: a body field cannot redirect the clear to another file's key).
    await clear_ledger_entry(session, f"process_file:{file_id}")

    # D-02 inline delete: the staged S3 object is provably no longer needed now that the
    # success result is recorded. No-op (zero S3 calls) when no cloud_job row exists (all-local).
    await _delete_staged_object_if_cloud(session, file_id)

    # D-14 reaper: reap the inert `awaiting` cloud_job hold-over row this file may carry. D-05's
    # conjunct (chosen over row deletion) means a locally-dispatched long file keeps its `awaiting`
    # row forever; without this reaper the `*/5` drain tick scans a monotonically growing dead set at
    # 200K, degrading `ix_cloud_job_awaiting`. The DELETE joins this seam's existing transaction (no new
    # commit). The `status='awaiting'` filter leaves a cloud-analyzed file's SUCCEEDED/RUNNING row
    # untouched. `file_id` is the PATH value only (AUTH-01).
    await session.execute(delete(CloudJob).where(CloudJob.file_id == file_id, CloudJob.status == CloudJobStatus.AWAITING.value))

    await session.commit()
    return AnalysisWriteResponse(agent_id=agent.id, file_id=file_id)


@router.post("/{file_id}/progress", status_code=status.HTTP_200_OK, response_model=AnalysisProgressResponse)
async def post_analysis_progress(
    file_id: uuid.UUID,
    body: AnalysisProgressPayload,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AnalysisProgressResponse:
    """Counter-only mid-flight progress upsert -- a SIBLING of ``put_analysis``, NOT a call into it (Phase 57.1 D-01/D-02).

    Upserts ONLY ``fine_windows_analyzed`` + ``fine_windows_total`` on the file's
    ``analysis`` row (``file_id`` UQ natural key). The START call carries
    ``(analyzed=0, total=N)``; bumps carry ``(analyzed=k, total=N)``. A second POST
    for the same ``file_id`` overwrites the counts (idempotent counter, NO second row).

    This handler reuses ``put_analysis``'s ``pg_insert…on_conflict_do_update``
    *mechanism* but STRIPS every completion side effect (KEY RISK / T-57.1-03):
    it does NOT flip ``FileState.ANALYZED``, does NOT write/replace
    ``analysis_window`` rows, does NOT ``clear_ledger_entry``, does NOT
    ``_delete_staged_object_if_cloud``, and does NOT stamp ``analysis_completed_at``.
    The partial row it writes therefore leaves ``analysis_completed_at`` NULL, so the
    proposal convergence gate (``analysis_completed_at IS NOT NULL``) keeps it out of
    ``generate_proposals``. Completion stays solely on ``put_analysis``.

    ``agent`` comes from ``get_authenticated_agent`` (token, NEVER body -- AUTH-01,
    T-57.1-01); ``file_id`` rides the PATH only; ``extra='forbid'`` on the payload
    makes a forged ``agent_id``/``file_id`` a 422 (T-57.1-02).
    """
    # Counter-only upsert: SET clause covers ONLY the two count columns. PK `id` is
    # stamped explicitly because AnalysisResult.id has a Python-only default that
    # pg_insert bypasses (mirrors put_analysis). file_id is the PATH value only.
    payload = {
        "fine_windows_analyzed": body.fine_windows_analyzed,
        "fine_windows_total": body.fine_windows_total,
        "file_id": file_id,
        "id": uuid.uuid4(),
    }
    stmt = pg_insert(AnalysisResult).values([payload])
    stmt = stmt.on_conflict_do_update(
        index_elements=["file_id"],
        set_={
            "fine_windows_analyzed": stmt.excluded.fine_windows_analyzed,
            "fine_windows_total": stmt.excluded.fine_windows_total,
        },
    )
    await session.execute(stmt)
    # NO FileRecord state flip, NO AnalysisWindow delete/insert, NO ledger clear,
    # NO staged-object delete, NO analysis_completed_at stamp -- this is what makes
    # it a sibling, not a reuse (T-57.1-03).
    await session.commit()
    return AnalysisProgressResponse(agent_id=agent.id, file_id=file_id)


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

    Phase 81 (FAIL-01, D-05 dual-write) introduced a durable per-stage marker on the file's
    ``analysis`` row -- ``failed_at = now()`` + ``error_message = "<reason>: <error>"``. Phase 80's
    recovery derives ``failed(analyze)`` from this marker (D-02). Phase 90 (D-09) has since removed
    the companion ``files.state = ANALYSIS_FAILED`` write now that its readers have cut over: the
    ``analysis.failed_at`` marker upserted here is the sole derived failure authority. The upsert is
    ``INSERT .. ON CONFLICT (file_id) DO UPDATE`` because a pure analyze failure never wrote an
    ``analysis`` row -- a bare UPDATE would silently no-op (D-06). It clears ``analysis_completed_at``
    in the same row so the migration-033 CHECK (``analysis_completed_at`` XOR ``failed_at``) can never
    see a mixed row (D-06). All writes commit in ONE transaction, ordered marker -> ledger ->
    staged-object-delete (RESEARCH Discretion #1).
    """
    # FAIL-01 / D-07: compose + defensively truncate the persisted detail (the column is unbounded Text;
    # `error` is already max_length=2000 at the wire). Mirrors report_metadata_failed's `reason: error`.
    # T-81-05-03 PG-invalid limb: NUL clears pydantic but Postgres rejects it
    # (CharacterNotInRepertoireError), aborting the transaction that also clears the ledger below ->
    # the file re-enqueues and fails identically forever. Sanitize BEFORE truncating; stripping can
    # only shorten, so the bound still holds.
    now = func.now()
    error_message = sanitize_pg_text(f"{body.reason}: {body.error}")[:_ERROR_MESSAGE_MAX]
    # FAIL-01 / D-05 dual-write, D-06: durable analyze-failure marker on the 1:1 `analysis` row. ON
    # CONFLICT DO UPDATE (not a bare UPDATE) because a pure analyze failure has no prior `analysis` row;
    # clear `analysis_completed_at` so the migration-033 XOR CHECK never sees a mixed row. Stamp the PK
    # explicitly because `AnalysisResult.id` has a Python-only default that `pg_insert` bypasses. Server-
    # set `failed_at=func.now()`; `file_id` is the PATH value only (AUTH-01 / T-81-05-01).
    stmt = pg_insert(AnalysisResult).values(
        [{"file_id": file_id, "id": uuid.uuid4(), "failed_at": now, "error_message": error_message, "analysis_completed_at": None}]
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["file_id"],
        set_={"failed_at": now, "error_message": error_message, "analysis_completed_at": None},
    )
    await session.execute(stmt)
    # Phase 90 (D-09): the ANALYSIS_FAILED files.state dual-write was removed. The durable
    # `analysis.failed_at` marker upserted above is now the sole derived failure authority
    # (failed_clause(Stage.ANALYZE), stage_status.py); readers cut over in PR-A.
    # Phase 45 (L-02, locked decision #1 -- THE POISON CASE): a terminal analyze failure must
    # NOT recovery-re-queue. Clear the process_file:<file_id> ledger row in the SAME transaction
    # as the failure marker. Key from the PATH file_id ONLY (AUTH-01 / T-45-05).
    await clear_ledger_entry(session, f"process_file:{file_id}")
    # D-02 inline delete: a terminal failure is also a result-callback terminal outcome -- the
    # staged object is no longer needed. No-op (zero S3 calls) when no cloud_job row exists.
    await _delete_staged_object_if_cloud(session, file_id)
    # D-14 reaper: a terminal failure is also an analyze-terminal seam -- reap the inert `awaiting`
    # cloud_job hold-over row (D-05's conjunct leaves it behind) so `ix_cloud_job_awaiting` stays
    # bounded and the `*/5` drain tick does not scan a growing dead set. Joins this seam's existing
    # transaction (no new commit); `status='awaiting'` leaves a SUCCEEDED/RUNNING row untouched.
    # `file_id` is the PATH value only (AUTH-01).
    await session.execute(delete(CloudJob).where(CloudJob.file_id == file_id, CloudJob.status == CloudJobStatus.AWAITING.value))
    await session.commit()
    logger.warning(
        "analysis_failed reported",
        file_id=str(file_id),
        agent_id=agent.id,
        reason=body.reason,
        error=body.error,
    )
    return AnalysisFailureResponse(agent_id=agent.id, file_id=file_id)
