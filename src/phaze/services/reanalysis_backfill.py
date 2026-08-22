"""One-time re-enqueue of every file whose prior analysis did not cover the whole file (phaze-kj8dl).

The payoff step of the exhaustive-analysis decision (ADR-0007 section 7, implemented by
phaze-w55w1): every ``analyses`` row written under the old fine/coarse caps carries a partial,
strided pass for any file whose natural window count exceeded the (now-removed) caps. This module
is the selection + enqueue logic behind ``phaze.cli.backfill``'s ``reenqueue-incomplete-analyses``
command, split out so it is independently unit-testable (mirrors ``services/text_repair_backfill.py``'s
split from ``scripts/backfill_mojibake_filenames.py``).

DEPLOYMENT ORDERING: see ``docs/runbook.md``, "One-time exhaustive-analysis re-enqueue backfill
(phaze-kj8dl)" -- that section is the single authoritative copy of the five-step sequence (merge,
release/build, deploy with the startup migration, run this command, watch the drain). Do not
re-copy it here; it would drift.

SELECTION -- WINDOWS COLUMNS ONLY, NEVER ``sampled`` (binding constraint)
--------------------------------------------------------------------------
By the time this module can run in a deployed environment, the ``analysis`` table's ``sampled``
column has ALREADY been dropped by migration ``060_drop_analysis_sampled.py`` (shipped in
phaze-w55w1, which this bead depends on and which the deploy ordering above guarantees runs
first). The selection below therefore never references it -- doing so would be a hard runtime
error against the deployed schema, not just a style violation.

No signal is lost. ``sampled=True`` was set (Phase 43) exactly when a tier's natural window count
exceeded its cap and the tier was strided instead of analyzed window-by-window; a strided pass
always finished with fewer analyzed windows than the natural total for that tier. So
``fine_windows_analyzed < fine_windows_total OR coarse_windows_analyzed < coarse_windows_total``
identifies precisely the same rows ``sampled=True`` used to, without reading the dropped column --
migration 060's docstring verifies this equivalence against the live schema, not just reasons
about it.

THE NULL-WINDOWS-COLUMNS DECISION -- explicit, and made once, here
--------------------------------------------------------------------------
Rows written before Phase 43 (migration 021) predate the four ``*_windows_analyzed`` /
``*_windows_total`` columns entirely and carry ``NULL`` in all four. **Decision: these rows are
SKIPPED, not re-run.** There were no caps before Phase 43, so a pre-Phase-43 analysis was already
exhaustive -- re-running it would spend an analysis pass to reproduce a result that is already
complete. This is not extra logic to write: SQL's three-valued comparison makes ``NULL < NULL``
and ``NULL < <int>`` both evaluate to UNKNOWN, which a ``WHERE`` clause treats as excluding the
row, so :data:`_INCOMPLETE_COVERAGE_CLAUSE` already skips every fully-NULL row with no explicit
NULL-check needed. :func:`count_null_windows_columns_rows` exists purely so the operator can see,
before running the enqueue, how many such legacy rows are being deliberately left alone (an
auditable count, not a gate on anything) -- it plays no part in the selection itself.

A row CAN carry one tier's pair populated while the other tier's pair is still NULL: a fine-tier
progress bump (``POST /agent/analysis/{id}/progress``, D-03) upserts ONLY
``fine_windows_analyzed``/``fine_windows_total`` on the file's partial in-flight row, leaving the
coarse pair NULL until that tier's own pass writes it. Such a row IS selected by
:data:`_INCOMPLETE_COVERAGE_CLAUSE` whenever its populated tier is itself incomplete (``analyzed <
total`` for that tier while the other tier reads NULL/NULL, i.e. UNKNOWN, which does not veto an
``OR`` whose other arm is TRUE) -- correctly so: it enqueues a file that may already be
mid-analysis. That is not a bug here, because :func:`enqueue_incomplete_reanalysis` never trusts
"selected" to mean "not already running" -- the SAME deterministic-key dedup that makes a re-run
of this whole module idempotent also classifies a collision against that file's own in-flight job
as ``"in_flight"``/``"blocked"`` rather than double-enqueuing it. Only a row with ALL FOUR columns
NULL (no progress upsert has ever run for it -- the pre-Phase-43 legacy case) is excluded by the
predicate itself; a partially-NULL row is a live in-flight file, not a legacy one, and is meant to
reach the enqueue call.

THE ALREADY-EXECUTED/MOVED DECISION -- excluded, not attempted, and counted
--------------------------------------------------------------------------
A file's ``original_path`` is where an agent last had it on disk; the human-approved apply
workflow COPIES it to a new location and DELETES the original (``services/stage_status.py``'s
``applied_clause`` -- ``exists(proposals WHERE status='executed')`` is the single authoritative
apply-outcome source). A file with incomplete windows coverage that has ALSO been applied is
un-re-analyzable at the path this module would enqueue against: the standard analyze producers
never reach this case because :func:`~phaze.services.pipeline.get_discovered_files_with_duration`
gates on ``eligible_clause(Stage.ANALYZE)`` (``~done`` -- and every row selected here already has
``analysis_completed_at`` set, see below), which a normal live trigger never has to reconcile
against ``applied_clause`` at all. This module deliberately bypasses that ``~done`` gate (that is
the whole point of a backfill over rows the pipeline considers "done" under the OLD, non-exhaustive
regime), so it is the one place that must add the apply exclusion by hand.

:data:`_INCOMPLETE_COVERAGE_CLAUSE` combined with ``~applied_clause()`` in
:func:`select_incomplete_analyses` therefore EXCLUDES every applied/moved row from the routed set;
:func:`count_applied_incomplete_analyses_rows` is the diagnostic twin (mirrors
:func:`count_null_windows_columns_rows`'s shape) that surfaces how many rows that exclusion
applies to, so the operator can see the count without this module inventing a re-analyze-at-the-new-
path feature it was not asked to build.

CLOUD ROUTING -- duration-threshold semantics, never bypassed (CLOUDROUTE-02 / T-49-03)
--------------------------------------------------------------------------
Every row selected here already carries ``analysis_completed_at IS NOT NULL`` (a strided Phase-43
pass still stamped completion on write, ``routers/agent_analysis.py``'s state-advance) -- concert
sets past the OLD ~30/90 min caps are exactly the multi-hour files CLOUDROUTE-02 exists for, so
routing every candidate straight to the local fileserver queue (bypassing
``routers/pipeline.py::_route_discovered_by_duration``'s duration gate entirely) would silently
re-analyze the backfill's primary target on the wrong side of the pipeline AND risk a second,
concurrent cloud-side analysis for a file the drain has already picked up (dedup keys are
per-queue, not global).

:func:`enqueue_incomplete_reanalysis` therefore applies the SAME decision function
``_route_discovered_by_duration`` does (``is_long = cloud_enabled and duration is not None and
duration >= cloud_route_threshold_sec``, itself gated on the force-local override via
``route_control.get_route_control``) and the SAME single writer for the hold
(``services.backends.hold_awaiting_cloud`` -- "A held long file is NEVER silently analyzed
locally", T-49-03), inline and synchronous rather than imported. It cannot reuse
``_route_discovered_by_duration`` verbatim: that helper BACKGROUNDS the local leg
(``asyncio.create_task``, so an HTTP handler can return before a large enqueue finishes) and
reports only aggregate counts, both wrong for a script that must ``await`` every enqueue to
completion and print a per-file audit line. A file that already carries an ACTIVE ``cloud_job``
(the same double-dispatch guard ``get_discovered_files_with_duration`` applies, via
``phaze.services.pipeline._ACTIVE_CLOUD_STATUSES``) is skipped with its own outcome rather than
routed again -- it is already being handled by the cloud pipeline.

ENQUEUE -- the standard per-agent-routed funnel, no cap overrides, individually contained
--------------------------------------------------------------------------
Short/null-duration candidates are grouped by owning agent and routed via
``enqueue_router.resolve_queues_for_owned_files`` (the same ownership-affinity routing the
dashboard "Run Analysis" trigger uses, phaze-c9w9) and enqueued one-by-one through
``analysis_enqueue.enqueue_process_file`` -- the SAME funnel every other ``process_file`` producer
uses, so the deterministic key (``process_file:<file_id>``), the complete payload, and the job
policy (``timeout=0`` + heartbeat liveness, ADR-0007 section 8 / phaze-w55w1) are identical to a
live analyze trigger. No cap override is passed because analysis is exhaustive by default now --
there is nothing left to override.

Each per-file enqueue is individually contained, mirroring ``routers/pipeline.py::
_enqueue_analysis_jobs``'s two containment fixes so this producer cannot regress behind them:
phaze-4ter (the enqueue call itself can raise -- a transient broker/pool error -- and is caught so
ONE failure cannot abort every remaining file) and phaze-p2qvv (the diagnostic collision-classification
probe is a SECOND await against the broker and can independently raise; its failure degrades that
one file to an ``"unknown"`` outcome rather than escaping and losing the rest of the run). Outcomes
are reported to the caller via the optional ``on_outcome`` callback AS THEY HAPPEN, not batched
until the whole run returns -- so a crash partway through (this module's own containment aside, a
KeyboardInterrupt or a truly unrecoverable error elsewhere) still leaves a durable, printed record
of what was actually done, not silence.

Operator decision (2026-08-11, phaze-kj8dl): enqueue every selected local-routed file AT ONCE and let the
existing lane scheduling drain the queue over days. No throttling or batching here -- the
deterministic-key dedup already makes a re-run of this module idempotent (a still in-flight file
re-enqueues to a no-op), so there is no correctness reason to trickle the enqueue, and the lanes'
own concurrency caps are what actually bound the drain rate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
import structlog

from phaze.config import get_settings
from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.services.analysis_enqueue import classify_process_file_collision, enqueue_process_file, process_file_job_key
from phaze.services.backends import hold_awaiting_cloud
from phaze.services.enqueue_router import NoActiveAgentError, resolve_queues_for_owned_files
from phaze.services.pipeline import _ACTIVE_CLOUD_STATUSES  # T-82-A1 double-dispatch guard, reused verbatim
from phaze.services.route_control import get_route_control
from phaze.services.stage_status import applied_clause


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings


logger = structlog.get_logger(__name__)


_INCOMPLETE_COVERAGE_CLAUSE = or_(
    AnalysisResult.fine_windows_analyzed < AnalysisResult.fine_windows_total,
    AnalysisResult.coarse_windows_analyzed < AnalysisResult.coarse_windows_total,
)
"""``analyzed < total`` in either tier -- zero references to the dropped ``sampled`` column.

A superset predicate by design (migration 060's docstring): it also matches a row where
individual windows failed to decode, not only a deliberately-strided one. For a one-time
re-enqueue that over-selection is desirable -- a partially-failed file is worth re-analyzing too.
NULL columns compare UNKNOWN and are excluded by the surrounding ``WHERE``, which is exactly the
skip decision recorded in the module docstring above.
"""


async def select_incomplete_analyses(session: AsyncSession) -> list[tuple[FileRecord, float | None]]:
    """Return ``(FileRecord, duration)`` for every file whose ``AnalysisResult`` did not cover it.

    Selection is windows-columns only (:data:`_INCOMPLETE_COVERAGE_CLAUSE`) -- see the module
    docstring for why ``sampled`` is never read and why NULL-columns rows are correctly excluded
    without an explicit NULL check -- AND ``~applied_clause()``, excluding every file whose rename
    has already been executed (the already-executed/moved decision, module docstring). Duration is
    the LEFT OUTER JOINed ``FileMetadata.duration`` (``None`` when no metadata row exists yet),
    captured here (not read later) for the same reason ``get_discovered_files_with_duration`` does:
    ``FileRecord.file_metadata`` is ``lazy="noload"``. Ordered by file id for a deterministic,
    auditable run.
    """
    stmt = (
        select(FileRecord, FileMetadata.duration)
        .join(AnalysisResult, AnalysisResult.file_id == FileRecord.id)
        .outerjoin(FileMetadata, FileMetadata.file_id == FileRecord.id)
        .where(_INCOMPLETE_COVERAGE_CLAUSE, ~applied_clause())
        .order_by(FileRecord.id)
    )
    result = await session.execute(stmt)
    return [(record, duration) for record, duration in result.all()]


async def count_null_windows_columns_rows(session: AsyncSession) -> int:
    """Count ``analysis`` rows whose windows columns are all NULL (pre-Phase-43 legacy rows).

    Diagnostic only -- see the module docstring's NULL-windows-columns decision. These rows are
    deliberately never selected by :func:`select_incomplete_analyses`; this count lets the
    operator see, before running the enqueue, how many rows that skip decision applies to.
    """
    stmt = (
        select(func.count())
        .select_from(AnalysisResult)
        .where(
            AnalysisResult.fine_windows_analyzed.is_(None),
            AnalysisResult.fine_windows_total.is_(None),
            AnalysisResult.coarse_windows_analyzed.is_(None),
            AnalysisResult.coarse_windows_total.is_(None),
        )
    )
    return (await session.scalar(stmt)) or 0


async def count_applied_incomplete_analyses_rows(session: AsyncSession) -> int:
    """Count incomplete-coverage rows whose file has ALREADY been applied/moved.

    Diagnostic only -- see the module docstring's already-executed/moved decision. These rows are
    deliberately excluded by :func:`select_incomplete_analyses` (``~applied_clause()``); this count
    lets the operator see, before running the enqueue, how many rows that exclusion applies to.
    """
    stmt = (
        select(func.count())
        .select_from(AnalysisResult)
        .join(FileRecord, FileRecord.id == AnalysisResult.file_id)
        .where(_INCOMPLETE_COVERAGE_CLAUSE, applied_clause())
    )
    return (await session.scalar(stmt)) or 0


@dataclass(frozen=True)
class ReanalysisOutcome:
    """One selected file's routing outcome, for the operator's per-file audit line."""

    file_id: uuid.UUID
    original_path: str
    outcome: str
    """One of:

    - ``"queued"`` -- a fresh ``process_file`` job was enqueued.
    - ``"in_flight"`` -- the deterministic key is already held by a live job (a prior run, or a
      concurrent trigger); no new job was enqueued.
    - ``"blocked"`` -- the deterministic key is held by a DEAD job (aborting/failed/stuck); see
      ``docs/runbook.md``'s outcomes section for remediation.
    - ``"unknown"`` -- the deterministic key collided but the diagnostic collision-classification
      probe itself failed (phaze-p2qvv containment); the file was NOT enqueued, but whether it is
      genuinely in-flight or blocked could not be determined this run.
    - ``"enqueue_error"`` -- the enqueue call itself raised (phaze-4ter containment); unknown
      whether a job now exists for this file.
    - ``"no_active_agent"`` -- the file's owning fileserver agent is offline; never rerouted.
    - ``"awaiting_cloud"`` -- the file is long enough to route to cloud analysis and was HELD
      (``cloud_job.status='awaiting'``) for the bounded drain, never enqueued locally
      (CLOUDROUTE-02 / T-49-03).
    - ``"in_cloud_pipeline"`` -- the file already carries an ACTIVE ``cloud_job`` (the T-82-A1
      double-dispatch guard); skipped without being routed again.
    - ``"vanished"`` -- the file was concurrently deleted while this run was holding it for cloud
      routing (an ``IntegrityError`` on the hold, mirroring
      ``routers/pipeline.py::_route_discovered_by_duration``'s own guard).
    """


_PRODUCTIVE_OUTCOMES: frozenset[str] = frozenset({"queued", "in_flight", "awaiting_cloud", "in_cloud_pipeline"})
"""Outcomes that represent genuine progress (or "already being handled") -- see :func:`compute_exit_code`."""


def compute_exit_code(outcomes: Sequence[ReanalysisOutcome]) -> int:
    """Return the CLI's process exit code for one run (finding #7).

    - ``0`` -- either genuinely nothing to do (``outcomes`` empty -- :func:`enqueue_incomplete_reanalysis`
      returns ``[]`` exactly when :func:`select_incomplete_analyses` found no candidate, which is a
      success, not a failure) OR at least one outcome represents real progress
      (:data:`_PRODUCTIVE_OUTCOMES`: a fresh enqueue, an already-in-flight job from a prior run, a
      file newly held for the cloud drain, or a file already being handled by the cloud pipeline).
    - ``1`` -- candidates existed but NONE of them could be productively routed (every outcome was
      ``blocked``/``unknown``/``enqueue_error``/``no_active_agent``/``vanished``) -- e.g. every owning
      agent was offline. Worth an operator's attention, unlike "nothing to do".
    """
    if not outcomes:
        return 0
    return 0 if any(o.outcome in _PRODUCTIVE_OUTCOMES for o in outcomes) else 1


def _split_cloud_pipeline_candidates(
    candidates: list[tuple[FileRecord, float | None]],
    active_cloud_ids: set[uuid.UUID],
    record: Callable[[ReanalysisOutcome], None],
) -> list[tuple[FileRecord, float | None]]:
    """Split off every candidate already carrying an ACTIVE ``cloud_job`` (T-82-A1 double-dispatch
    guard, module docstring). Each is reported ``"in_cloud_pipeline"`` via ``record`` and routed no
    further; everything else is returned for the caller to route.
    """
    routable: list[tuple[FileRecord, float | None]] = []
    for f, duration in candidates:
        if f.id in active_cloud_ids:
            record(ReanalysisOutcome(f.id, f.original_path, "in_cloud_pipeline"))
            continue
        routable.append((f, duration))
    return routable


def _split_by_duration_threshold(
    routable: list[tuple[FileRecord, float | None]],
    *,
    cloud_enabled: bool,
    threshold: int,
) -> tuple[list[FileRecord], list[FileRecord]]:
    """Apply the SAME duration-threshold decision ``_route_discovered_by_duration`` does (module
    docstring, "CLOUD ROUTING"): ``(long_candidates, local_candidates)``.
    """
    long_candidates: list[FileRecord] = []
    local_candidates: list[FileRecord] = []
    for f, duration in routable:
        is_long = cloud_enabled and duration is not None and duration >= threshold
        (long_candidates if is_long else local_candidates).append(f)
    return long_candidates, local_candidates


async def _hold_long_candidates_for_cloud(
    session: AsyncSession,
    long_candidates: list[FileRecord],
    record: Callable[[ReanalysisOutcome], None],
) -> None:
    """Hold every long candidate for the bounded cloud drain via ``hold_awaiting_cloud``, then
    commit once (module docstring, "CLOUD ROUTING" -- mirrors ``_route_discovered_by_duration``'s
    own commit placement). A concurrent delete during the hold is reported ``"vanished"``, mirroring
    that helper's own ``IntegrityError`` guard.
    """
    held = 0
    for f in long_candidates:
        try:
            async with session.begin_nested():
                await hold_awaiting_cloud(session, f)
        except IntegrityError:
            logger.info("enqueue_incomplete_reanalysis: file deleted before hold could commit; skipping", file_id=str(f.id))
            record(ReanalysisOutcome(f.id, f.original_path, "vanished"))
            continue
        held += 1
        record(ReanalysisOutcome(f.id, f.original_path, "awaiting_cloud"))
    if held:
        # Commit the AWAITING_CLOUD holds BEFORE the local enqueues (mirrors
        # _route_discovered_by_duration: get_session-style callers never auto-commit).
        await session.commit()


async def _enqueue_local_candidates(
    routed_groups: Sequence[tuple[Any, list[FileRecord]]],
    skipped: Sequence[FileRecord],
    models_path: Any,
    record: Callable[[ReanalysisOutcome], None],
) -> None:
    """Enqueue every short/null-duration candidate through the standard per-agent-routed funnel
    (module docstring, "ENQUEUE"). Both the enqueue call (phaze-4ter) and the collision-
    classification probe (phaze-p2qvv) are individually contained so one file's failure cannot
    abort the rest of the run.
    """
    for f in skipped:
        record(ReanalysisOutcome(f.id, f.original_path, "no_active_agent"))

    for routed, group in routed_groups:
        agent_id = cast("str", routed.agent_id)
        for f in group:
            try:
                job = await enqueue_process_file(routed.queue, f, agent_id, models_path)
            except Exception:
                # phaze-4ter containment: a transient broker/pool error at file N must not abort
                # every remaining file in this run.
                logger.exception("enqueue_incomplete_reanalysis: failed to enqueue process_file job", file_id=str(f.id))
                record(ReanalysisOutcome(f.id, f.original_path, "enqueue_error"))
                continue
            if job is not None:
                record(ReanalysisOutcome(f.id, f.original_path, "queued"))
                continue
            try:
                collision = classify_process_file_collision(await routed.queue.job(process_file_job_key(f.id)))
            except Exception:
                # phaze-p2qvv containment: the diagnostic probe is a SECOND await against the
                # broker and can independently raise; its failure must degrade to "unknown" for
                # this one file, never abort the run.
                logger.warning(
                    "enqueue_incomplete_reanalysis: collision-classification probe failed -- outcome unknown, enqueue outcome unaffected",
                    file_id=str(f.id),
                    key=process_file_job_key(f.id),
                )
                record(ReanalysisOutcome(f.id, f.original_path, "unknown"))
                continue
            record(ReanalysisOutcome(f.id, f.original_path, collision))


async def enqueue_incomplete_reanalysis(
    session: AsyncSession,
    app_state: Any,
    *,
    on_outcome: Callable[[ReanalysisOutcome], None] | None = None,
) -> list[ReanalysisOutcome]:
    """Select every incomplete-coverage, non-applied file and route it: hold long, enqueue short.

    ``app_state`` needs only a ``task_router`` attribute (an ``AgentTaskRouter`` or test double) --
    ``process_file`` is an agent task (``enqueue_router.AGENT_TASKS``), never a controller task, so
    ``app_state.controller_queue`` is never read on this path.

    ``on_outcome``, when given, is called with each :class:`ReanalysisOutcome` AS IT IS PRODUCED
    (before this coroutine returns) so a caller can print an incremental, crash-durable audit trail
    (module docstring, "individually contained"). The full list is always returned too.

    Routing, per selected ``(file, duration)`` pair, in order:

    1. A file already carrying an ACTIVE ``cloud_job`` (module docstring, "T-82-A1 double-dispatch
       guard") is reported ``"in_cloud_pipeline"`` and routed no further.
    2. Otherwise, the SAME duration-threshold decision ``routers/pipeline.py::
       _route_discovered_by_duration`` applies (module docstring, "CLOUD ROUTING"): long enough
       (with cloud enabled and not force-localed) -> HELD via ``services.backends.hold_awaiting_cloud``
       and reported ``"awaiting_cloud"``; a concurrent delete during the hold is reported
       ``"vanished"`` (mirrors that helper's own ``IntegrityError`` guard). Held rows are committed
       once, after the whole long-candidate loop (mirrors the same helper's commit placement).
    3. Short/null-duration files are grouped by owning agent via ``resolve_queues_for_owned_files``
       (phaze-c9w9 ownership affinity -- never rerouted to a different agent's mount). A file whose
       owner is offline is reported ``"no_active_agent"`` (never rerouted); if NO owner is live at
       all for the remaining short/null set, every one of them is reported ``"no_active_agent"``.
       Otherwise each file is enqueued individually via
       :func:`~phaze.services.analysis_enqueue.enqueue_process_file`, with BOTH the enqueue call and
       the collision-classification probe individually contained (module docstring, "ENQUEUE"): a
       fresh job is ``"queued"``; a deterministic-key collision is classified ``"in_flight"`` or
       ``"blocked"`` via :func:`~phaze.services.analysis_enqueue.classify_process_file_collision`; an
       enqueue-call failure is ``"enqueue_error"``; a probe failure is ``"unknown"``.

    Idempotent by construction: SAQ's deterministic-key dedup makes a repeat enqueue of a still-live
    file a clean no-op (tallied ``"in_flight"``), holding an already-held file is a plain re-stamp
    (``hold_awaiting_cloud``'s upsert), and the selection query always re-derives from the current
    ``analysis``/``proposals``/``cloud_job`` rows, so a re-run only ever touches files still
    genuinely incomplete, unmoved, and not already being handled elsewhere.
    """
    candidates = await select_incomplete_analyses(session)
    if not candidates:
        return []

    outcomes: list[ReanalysisOutcome] = []

    def _record(outcome: ReanalysisOutcome) -> None:
        outcomes.append(outcome)
        if on_outcome is not None:
            on_outcome(outcome)

    file_ids = [f.id for f, _ in candidates]
    active_cloud_ids: set[uuid.UUID] = set(
        (await session.scalars(select(CloudJob.file_id).where(CloudJob.file_id.in_(file_ids), CloudJob.status.in_(_ACTIVE_CLOUD_STATUSES)))).all()
    )

    routable = _split_cloud_pipeline_candidates(candidates, active_cloud_ids, _record)
    if not routable:
        return outcomes

    settings = cast("ControlSettings", get_settings())
    # Phase 71 (BEUI-02, D-08) fold, same as every other duration-router caller: effective
    # cloud_enabled is "registry cloud_enabled AND NOT force_local".
    cloud_enabled = settings.cloud_enabled and not await get_route_control(session)
    threshold = settings.cloud_route_threshold_sec

    long_candidates, local_candidates = _split_by_duration_threshold(routable, cloud_enabled=cloud_enabled, threshold=threshold)

    await _hold_long_candidates_for_cloud(session, long_candidates, _record)

    if not local_candidates:
        return outcomes

    try:
        routed_groups, skipped = await resolve_queues_for_owned_files("process_file", app_state, session, local_candidates)
    except NoActiveAgentError:
        for f in local_candidates:
            _record(ReanalysisOutcome(f.id, f.original_path, "no_active_agent"))
        return outcomes

    await _enqueue_local_candidates(routed_groups, skipped, settings.models_path, _record)
    return outcomes
