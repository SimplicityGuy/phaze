"""The cloud lane read model -- awaiting/inadmissible/admission-phase cards, the staged vs
analyzing window split, the drain's candidate SELECT, and the long-failure backfill set.

Extracted from the former monolithic ``services/pipeline.py`` (phaze-vsqpr).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast as type_cast

from sqlalchemy import String, and_, cast, exists, false, func, literal, or_, select, tuple_
import structlog

from phaze.config import get_settings
from phaze.enums.stage import Stage
from phaze.models.cloud_job import CloudJob, CloudJobStatus, CloudPhase
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.agent_liveness import non_local_backend_kinds
from phaze.services.pipeline.common import _ACTIVE_CLOUD_STATUSES, _safe_count
from phaze.services.stage_status import (
    awaiting_candidate_clause,
    failed_clause,
)


if TYPE_CHECKING:
    from datetime import datetime
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select
    from sqlalchemy.sql.elements import ColumnElement

    from phaze.config import ControlSettings


logger = structlog.get_logger(__name__)


async def get_awaiting_cloud_count(session: AsyncSession) -> int:
    """Return COUNT of genuinely-parked awaiting cloud_job rows, degrading to 0 on any DB error (Phase 83, D-15).

    Drives the dashboard "Awaiting cloud" card. Re-anchored off the retired
    ``FileRecord.state == AWAITING_CLOUD`` display read onto the SAME clause the drain
    (:func:`get_cloud_staging_candidates`) uses -- ``COUNT(cloud_job) WHERE status='awaiting' AND
    ~inflight_clause(ANALYZE) AND ~domain_completed_clause(ANALYZE)`` -- so the card counts exactly the
    rows the drain would pick and the two can NEVER disagree. A LOCAL_ANALYZING long file that still
    carries its inert awaiting row (D-13 keeps the flip; D-14 reaps the row at the analyze-terminal seam)
    is excluded from BOTH by ``~inflight_clause``, so it never inflates the card. Composes the LOCKED
    clause builders verbatim (DERIV-04). Poll-safe via :func:`_safe_count` (mirrors
    :func:`get_analysis_failed_count`): a DB hiccup degrades this node to 0 and rolls back the aborted
    transaction rather than 500ing the hot 5s /pipeline/stats poll.
    """
    return await _safe_count(
        session,
        # INNER-join FileRecord so the correlated ``~exists(... file_id == FileRecord.id)`` clause builders
        # resolve (they reference FileRecord.id); cloud_job.file_id is unique, so the join is 1:1 and the
        # COUNT matches the drain's candidate set exactly.
        select(func.count(CloudJob.id)).select_from(CloudJob).join(FileRecord, FileRecord.id == CloudJob.file_id).where(awaiting_candidate_clause()),
        node="awaiting_cloud",
    )


async def get_inadmissible_count(session: AsyncSession) -> int:
    """Return COUNT of ``cloud_job`` rows flagged ``inadmissible``, degrading to 0 on any DB error.

    Drives the dashboard Inadmissible operator alert (D-06, KSUBMIT-04): a non-zero count means
    one or more Kueue Workloads are Inadmissible (a misconfigured LocalQueue/ClusterQueue), which
    the reconcile cron (Plan 06) stamps onto the row. A healthy quota wait (``Pending``) never
    sets the flag, so this count stays 0 and the alert stays silent. Poll-safe via
    :func:`_safe_count` (mirrors :func:`get_awaiting_cloud_count`): a DB hiccup degrades this node
    to 0 and rolls back the aborted transaction rather than 500ing the hot 5s /pipeline/stats poll
    (T-54-10).
    """
    return await _safe_count(
        session,
        # CR-01: scope to in-flight rows so a terminal row that was transiently Inadmissible (and whose
        # flag the reconcile cron clears anyway) can never inflate the alert -- belt-and-suspenders.
        select(func.count(CloudJob.id)).where(
            CloudJob.inadmissible.is_(True),
            CloudJob.status.in_([CloudJobStatus.SUBMITTED.value, CloudJobStatus.RUNNING.value]),
        ),
        node="inadmissible",
    )


async def get_cloud_phase_counts(session: AsyncSession) -> dict[str, int]:
    """Return per-``cloud_phase`` counts for the dashboard admission-state card, each degrading to 0.

    Drives the KROUTE-06 admission-state card (D-04): four COUNT(cloud_job) reads grouped by the
    Kueue admission progression (``queued_behind_quota`` -> ``admitted`` -> ``running`` ->
    ``finished``). Each count is an independent :func:`_safe_count`-backed read with a distinct
    ``node=`` tag, mirroring :func:`get_inadmissible_count`: a DB hiccup degrades THAT phase to 0
    (and rolls back the aborted transaction) rather than 500ing the hot 5s ``/pipeline/stats`` poll
    (T-55-CARD-01). The card then renders the quiet empty carrier.

    ``cloud_phase`` is NULL for a1/local rows (admission is a k8s-only concept), so those rows count
    toward NONE of the four phases — all-zero leaves the card a quiet empty carrier on non-k8s deploys.

    phaze-zyoag: ``finished`` is DELIBERATELY different in kind from its three siblings -- it counts
    ``cloud_phase == FINISHED`` with no time bound and no status scoping, and only ``awaiting`` rows are
    ever deleted (``routers/agent_analysis.py``), so a SUCCEEDED row's FINISHED phase persists forever.
    This is a LIFETIME CUMULATIVE total, not a live-at-this-instant count like ``queued_behind_quota`` /
    ``admitted`` / ``running``. The template (``admission_state_card.html``) renders it in its own row
    below a divider with an explicit "lifetime total, not a live snapshot" caption rather than as a
    fourth tile in the "per reconcile, updates ~5 min" grid, so it can never be misread as a snapshot of
    the same instant as the other three.
    """
    return {
        "queued_behind_quota": await _safe_count(
            session,
            select(func.count(CloudJob.id)).where(CloudJob.cloud_phase == CloudPhase.QUEUED_BEHIND_QUOTA.value),
            node="cloud_phase_queued_behind_quota",
        ),
        "admitted": await _safe_count(
            session,
            select(func.count(CloudJob.id)).where(CloudJob.cloud_phase == CloudPhase.ADMITTED.value),
            node="cloud_phase_admitted",
        ),
        "running": await _safe_count(
            session,
            select(func.count(CloudJob.id)).where(CloudJob.cloud_phase == CloudPhase.RUNNING.value),
            node="cloud_phase_running",
        ),
        "finished": await _safe_count(
            session,
            select(func.count(CloudJob.id)).where(CloudJob.cloud_phase == CloudPhase.FINISHED.value),
            node="cloud_phase_finished",
        ),
    }


def _kueue_backend_ids() -> frozenset[str]:
    """Return the registry backend ids whose ``kind == "kueue"``, degrading to empty on any registry error.

    A pure, no-DB projection over the SAME
    :func:`phaze.services.agent_liveness.non_local_backend_kinds` helper
    :func:`get_analyze_working_set`'s per-file lane badges already consume -- one registry answer,
    shared by the file badges and these two window-count cards, instead of a second guess at "which
    backend ids are kueue-kind". A settings/registry read failure degrades to an EMPTY set (never
    raises): with no known kueue ids, :func:`_cloud_window_clauses` below falls back to the pre-fix
    "SUBMITTED is staged" reading everywhere, which is wrong ONLY for a live kueue deployment whose
    registry momentarily failed to resolve -- an already-degraded poll tick, not a fresh failure mode.
    """
    try:
        kinds = non_local_backend_kinds(type_cast("ControlSettings", get_settings()))
    except Exception:
        logger.warning("cloud_window_kueue_ids_degraded", exc_info=True)
        return frozenset()
    return frozenset(backend_id for backend_id, kind in kinds.items() if kind == "kueue")


def _cloud_window_clauses() -> tuple[ColumnElement[bool], ColumnElement[bool]]:
    """Return the ``(staged, analyzing)`` boolean predicates that partition ``backends.IN_FLIGHT`` (phaze-zyoag).

    ROOT CAUSE this replaces: the two cards used to cut {UPLOADING, SUBMITTED} / {UPLOADED, RUNNING} --
    the lifecycle-order seam for NEITHER backend kind. The correct seam is
    :data:`phaze.services.backends.STAGING` (pre-submit: UPLOADING/UPLOADED) vs the rest of
    :data:`phaze.services.backends.IN_FLIGHT`, EXCEPT that SUBMITTED itself means opposite things per
    backend kind (D-10): mid-rsync on ``compute`` (``ComputeAgentBackend.dispatch`` writes it at
    DISPATCH time, terminalized only by the ``/pushed`` callback), but POST-upload / admitted-or-queued
    on ``kueue`` (``submit_cloud_job`` writes it only after the S3 upload has already landed). A single
    global status->tile mapping cannot say both, so SUBMITTED is split by the row's OWN
    ``backend_id`` via :func:`_kueue_backend_ids` -- the kueue-attributed half moves to "Analyzing"
    with everything else in :data:`STAGING` (always staged) and RUNNING (always analyzing, kueue-only
    in practice: compute never reaches RUNNING, it goes straight to SUCCEEDED off the ``/pushed``
    callback).

    ANALYZING DEFINITION -- option (a) of the bead's design doc, chosen deliberately: "post-submit, in
    the cloud window" = {SUBMITTED-on-kueue, RUNNING}. This keeps Staged + Analyzing summing to EXACTLY
    :data:`phaze.services.backends.IN_FLIGHT` for every row, matching the two card templates'
    pre-existing "together they account for every backend's busy in-flight slot" sub-caption contract.
    Option (b) ("a pod is actually executing" == RUNNING only) would leave a quota-queued kueue row
    counted in NEITHER tile, silently shrinking the visible in-flight total -- worse than the bug this
    fixes. The tile sub-captions (``staged_pushing_card.html`` / ``analyzing_cloud_card.html``) say so
    explicitly: Analyzing no longer means "landed", it means "submitted or running".

    A NULL / deregistered ``backend_id`` degrades to the STAGED side (never invisible, never claimed by
    both cards): both dispatch paths stamp ``backend_id`` in the SAME transaction as the write that
    would otherwise make a row's kind ambiguous (``ComputeAgentBackend.dispatch`` /
    ``KueueBackend.dispatch`` in ``services/backends.py``), so an unattributed in-flight SUBMITTED row
    is itself an anomaly, not the expected shape -- this is the SAME "unattributed cloud" fallback
    posture :func:`_project_analyze_rows` takes for the per-file lane badge.

    :data:`STAGING` supplies the always-staged half directly (no restated status literals); SUBMITTED
    is the one member that must stay a named literal because it is the specific point of per-backend
    disagreement -- deriving it generically is not possible without encoding that disagreement
    somewhere, and here is the one place it is documented. ``phaze.services.backends`` imports FROM
    this module at module scope (``MUSIC_VIDEO_TYPES`` / ``get_live_job_keys``), so the reverse import
    is deferred to call time to avoid a circular import at module load.
    """
    from phaze.services.backends import IN_FLIGHT, STAGING  # noqa: PLC0415 -- breaks a module-load import cycle, see docstring

    kueue_ids = _kueue_backend_ids()
    is_kueue_row = and_(CloudJob.backend_id.is_not(None), CloudJob.backend_id.in_(kueue_ids)) if kueue_ids else false()

    staging_values = [status.value for status in STAGING]
    submitted_value = CloudJobStatus.SUBMITTED.value
    unambiguous_analyzing_values = [status.value for status in IN_FLIGHT if status not in STAGING and status.value != submitted_value]

    staged = or_(
        CloudJob.status.in_(staging_values),
        and_(CloudJob.status == submitted_value, ~is_kueue_row),
    )
    analyzing = or_(
        CloudJob.status.in_(unambiguous_analyzing_values) if unambiguous_analyzing_values else false(),
        and_(CloudJob.status == submitted_value, is_kueue_row),
    )
    return staged, analyzing


async def get_pushing_count(session: AsyncSession) -> int:
    """Return COUNT of the "staged" (pre-submit / mid-transfer) half of the bounded cloud window (phaze-zyoag).

    Drives the dashboard "Staged (pushing)" card. DERIVED from :func:`_cloud_window_clauses` -- see
    that docstring for the full per-backend-kind rationale (D-10, phaze-zyoag). Poll-safe via
    :func:`_safe_count` (mirrors :func:`get_awaiting_cloud_count`): a DB hiccup degrades this node to 0
    and rolls back the aborted transaction rather than 500ing the hot 5s /pipeline/stats poll. This is
    the OBSERVATIONAL per-card count -- the load-bearing backpressure is per-backend
    ``Backend.in_flight_count`` (Phase 69, D-05), which the drain reads once per tick and which is
    intentionally NOT degrade-safe so the drain never over-dispatches on a transient error.
    """
    staged, _analyzing = _cloud_window_clauses()
    return await _safe_count(session, select(func.count(CloudJob.id)).where(staged), node="pushing")


async def get_pushed_count(session: AsyncSession) -> int:
    """Return COUNT of the "analyzing" (post-submit, in the cloud window) half of the bounded cloud window (phaze-zyoag).

    Drives the dashboard "Analyzing (cloud)" card. DERIVED from :func:`_cloud_window_clauses` -- see
    that docstring for the full per-backend-kind rationale and the option-(a) definition this
    implements (D-10, phaze-zyoag): SUBMITTED-on-kueue + RUNNING, NOT "landed" (a kueue row can sit
    here for hours waiting on cluster quota). Poll-safe via :func:`_safe_count`, exactly like
    :func:`get_pushing_count`. Observational only; the per-backend cap itself is enforced by
    ``Backend.in_flight_count`` (Phase 69, D-05) from committed cloud_job rows.
    """
    _staged, analyzing = _cloud_window_clauses()
    return await _safe_count(session, select(func.count(CloudJob.id)).where(analyzing), node="analyzing_cloud")


# --- Phase 50 bounded cloud-window helpers (D-03/D-08, CLOUDPIPE-01) ---------------------
#
# Phase 69 (D-05, SCHED-02) retired the global FileState-window count in favor of per-backend
# ``Backend.in_flight_count`` (a ``cloud_job``-derived COUNT scoped by ``backend_id``). The
# ``stage_cloud_window`` drain now snapshots each backend's free capacity once per tick and SELECTs
# candidates via ``get_cloud_staging_candidates`` below -- still ``FOR UPDATE SKIP LOCKED`` in ONE
# transaction so a concurrent tick cannot double-stage the same row (T-50-scratch-dos).


async def get_cloud_staging_candidates(
    session: AsyncSession,
    limit: int,
    *,
    after: tuple[datetime, uuid.UUID] | None = None,
) -> list[tuple[FileRecord, datetime]]:
    """Return up to ``limit`` oldest genuinely-parked cloud candidates + each row's staleness clock (Phase 83, D-05/D-06/D-07).

    Cut over from the retired ``FileRecord.state == AWAITING_CLOUD`` read (SC#1) to the ``cloud_job``
    sidecar + the derived ``in_flight(analyze)`` layer. A candidate is a file that:

    * carries a ``cloud_job(status='awaiting')`` sidecar row (INNER join -- D-05 conjunct 1), AND
    * is NOT analyze-in-flight (``~inflight_clause(ANALYZE)`` -- D-05 conjunct 2). A locally-dispatched
      file whose ``process_file`` ledger row is committed is excluded, and that exclusion SURVIVES a
      whole-tick rollback because the ledger row was committed by the ``before_enqueue`` hook's OWN
      session -- the exact reason D-05 chose a predicate conjunct over deleting the awaiting row (a
      deleted row restored on the rollback would re-pick the file and could cloud-dispatch it, the
      double-dispatch SC#3 forbids). AND
    * has NOT domain-completed its analyze (``~domain_completed_clause(ANALYZE)`` -- D-05 conjunct 3):
      ``FAILURE_IS_TERMINAL[analyze]`` is True, so a terminally-failed local analyze is domain-complete
      and never re-driven (the Phase-81 twin the ROADMAP dep-note names).

    Composes the LOCKED ``inflight_clause`` / ``domain_completed_clause`` builders VERBATIM -- re-spelling
    either breaks the DERIV-04 equivalence test (``tests/integration/test_stage_status_equivalence.py``).

    FIFO stays on the immutable ``FileRecord.created_at`` (D-07 -- byte-identical discovery order to the
    pre-cutover query; a file discovered months ago but held today still sorts to the front). The per-row
    ``cloud_job.updated_at`` is surfaced alongside each candidate as the lane-entry staleness clock the
    caller passes into ``select_backend`` (D-07): it lives on the awaiting row rather than
    ``file.updated_at`` so Phase 90's removal of the dual-written ``file.state`` cannot silently break the
    ``cloud_route_max_wait_sec`` spill clock.

    D-06: the lock moves to the candidacy table -- ``with_for_update(of=CloudJob, skip_locked=True)`` over
    the INNER join so Postgres re-evaluates ``cloud_job``'s ``WHERE`` after acquiring the lock (EvalPlanQual);
    locking only ``files`` would read the deciding ``cloud_job.status`` column stale against the concurrent
    callback routers / reconcile cron the tick's advisory lock does not cover. INNER (not outer) join is
    required -- Postgres rejects ``FOR UPDATE`` on the nullable side of an outer join. ``limit`` is the
    page size the caller wants; the caller must guarantee ``limit > 0`` (a ``LIMIT 0`` would be a pointless
    round-trip).

    phaze-9sqa -- ``after`` is the KEYSET cursor that lets the drain page PAST a head-of-line run of
    candidates it could not route. Without it the drain fetched exactly ``sum(free slots)`` rows and, when
    every one of those oldest rows was unroutable (``select_backend`` -> ``None``, e.g. the D-04 attempts
    cap with the local backend full), re-fetched the SAME rows every tick forever while every routable
    file behind them starved -- observed in production as ``staged: 0, skipped: 3`` every 5 min for >24 h
    behind 14 poisoned heads. Passing the previous page's last ``(created_at, id)`` slides the window
    forward instead. Deliberately a cursor rather than a smarter WHERE: routability is decided by the pure
    ``select_backend`` policy over an in-memory per-tick snapshot (backend availability, free slots, the
    staleness gate, the attempts cap), so re-spelling "unroutable" in SQL would fork that policy into a
    second, drifting definition -- and this clause is the DERIV-04-locked ``awaiting_candidate_clause``,
    which must stay byte-identical to the count card's spelling.

    The sort key gains ``FileRecord.id`` as a stable tie-break. ``created_at`` alone is NOT unique (a scan
    batch inserts many rows inside one transaction, and ``server_default=func.now()`` is transaction time,
    so whole batches share a timestamp) -- a keyset cursor over a non-unique key would either skip or
    re-serve the tied rows. FIFO semantics are unchanged: ``created_at`` still dominates, ``id`` only orders
    within a tie that previously had no defined order at all.
    """
    stmt = (
        select(FileRecord, CloudJob.updated_at)
        .join(CloudJob, CloudJob.file_id == FileRecord.id)
        .where(awaiting_candidate_clause())
        .order_by(FileRecord.created_at.asc(), FileRecord.id.asc())
        .limit(limit)
        .with_for_update(of=CloudJob, skip_locked=True)
    )
    if after is not None:
        # Row-value comparison -- the exact keyset form Postgres can drive from the composite ordering,
        # and the only spelling that is correct across a ``created_at`` tie.
        # BOUND params typed off the columns themselves (never interpolated SQL, T-42-03/T-49-02).
        cursor = tuple_(literal(after[0], FileRecord.created_at.type), literal(after[1], FileRecord.id.type))
        stmt = stmt.where(tuple_(FileRecord.created_at, FileRecord.id) > cursor)
    return [(file, updated_at) for file, updated_at in (await session.execute(stmt)).all()]


def _backfill_candidates_stmt(threshold_sec: int) -> Select[Any]:
    """Build the ANALYSIS_FAILED + ``duration >= threshold_sec`` + ledger-scoped candidate predicate.

    INNER JOIN ``FileMetadata`` so a null-duration ANALYSIS_FAILED file is structurally
    excluded; the ``duration >= threshold_sec`` filter then drops short failures. ``threshold_sec``
    is a bound int parameter (T-49-02) -- never interpolated SQL.

    Phase 55 (L4 / D-03 / KROUTE-05): an ``EXISTS`` predicate against ``scheduling_ledger`` keyed
    ``'process_file:' || file.id`` scopes candidates to **previously-scheduled work only**. A SAQ
    timeout abandons a long ``process_file`` job WITHOUT firing ``report_analysis_failed`` (which
    clears the row), so the orphaned ledger row persists into ``ANALYSIS_FAILED`` -- exactly the
    timed-out set this backfill re-drives. A never-scheduled (or cleanly report-failed, row-cleared)
    failure has NO ledger row and is excluded, preventing the v4.0.6 / v5.0 whole-backlog
    over-enqueue class. ORM / bound params only -- the key is concatenated via ``cast`` + a bound
    literal, never f-string SQL (T-49-02 / T-55-BF-04).

    phaze-l1km: this predicate cannot distinguish an ORPHANED ledger row (a timed-out process_file
    whose SAQ job is gone) from the LIVE-in-flight marker of a still-running re-analysis. That live/dead
    split is a READ of the SAQ-owned ``saq_jobs`` broker (absent in some envs), so it is applied by the
    caller (:func:`phaze.routers.pipeline.trigger_backfill_cloud`) via the degrade-safe
    :func:`get_live_job_keys`, NOT baked into this always-on candidate query.
    """
    return (
        select(FileRecord, FileMetadata.duration)
        .join(FileMetadata, FileMetadata.file_id == FileRecord.id)
        .where(
            # Phase 90 (PR-A, D-09): DERIVED terminal analyze-failure via ``failed_clause(ANALYZE)`` (an
            # analysis row with ``failed_at`` set), no longer ``files.state == ANALYSIS_FAILED``.
            failed_clause(Stage.ANALYZE),
            FileMetadata.duration >= threshold_sec,
            exists(select(SchedulingLedger.key).where(SchedulingLedger.key == "process_file:" + cast(FileRecord.id, String))),
            # Phase 90 (PR-A) idempotency guard: exclude a file already routed to the cloud path (it
            # carries an ACTIVE ``cloud_job`` sidecar). The retired ``state == ANALYSIS_FAILED`` gate WAS
            # the double-click guard -- a held file's state flipped ANALYSIS_FAILED -> AWAITING_CLOUD, so
            # a second backfill re-selected nothing. The derived ``failed_clause`` marker does NOT
            # transition when a file is held (the backfill routes to cloud without clearing it), so this
            # ``~exists(active cloud_job)`` conjunct restores the D-10 no-whole-backlog-sweep idempotency,
            # mirroring the identical guard in :func:`get_discovered_files_with_duration`.
            ~exists(select(CloudJob.id).where(CloudJob.file_id == FileRecord.id, CloudJob.status.in_(_ACTIVE_CLOUD_STATUSES))),
        )
    )


async def count_backfill_candidates(session: AsyncSession, threshold_sec: int) -> int:
    """Return COUNT of ANALYSIS_FAILED files whose joined duration >= ``threshold_sec``.

    This is the explicit filter that closes the over-enqueue class (D-09/D-10): it is NOT
    :func:`get_analysis_failed_count` (which counts ALL ANALYSIS_FAILED, including short and
    null-duration failures that must never be cloud-routed). Poll-safe via :func:`_safe_count`.
    """
    return await _safe_count(
        session,
        select(func.count()).select_from(_backfill_candidates_stmt(threshold_sec).subquery()),
        node="backfill_candidates",
    )


async def get_backfill_candidates(session: AsyncSession, threshold_sec: int) -> list[tuple[FileRecord, float | None]]:
    """Return ``(FileRecord, duration)`` for the same ANALYSIS_FAILED + duration>=threshold set.

    The list form the backfill producer (Plan 03) iterates to re-route long failed files to a
    cloud compute agent. duration is captured in-memory (FileRecord.file_metadata is
    ``lazy="noload"``) so a downstream background task never triggers a lazy load.
    """
    result = await session.execute(_backfill_candidates_stmt(threshold_sec))
    return [(record, duration) for record, duration in result.all()]
