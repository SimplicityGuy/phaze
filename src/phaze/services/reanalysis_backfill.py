"""Re-enqueue analyses whose recorded windows do not cover the whole file (phaze-kj8dl).

Deployment ordering belongs to ``docs/runbook.md`` under "One-time exhaustive-analysis re-enqueue
backfill". The governing exhaustive-analysis and heartbeat-liveness decisions are in
``docs/design/0007-windowed-analysis.md``.

Selection uses only ``*_windows_analyzed < *_windows_total``; the retired ``sampled`` column must
never be queried. SQL's three-valued comparisons intentionally exclude rows with all four window
columns NULL, while a partially populated row remains eligible when its populated tier is
incomplete. Applied/moved files are also excluded and counted because ``original_path`` is no
longer analyzable after apply.

Routing retains the live duration threshold and force-local control. Active cloud jobs are skipped,
long files use the shared awaiting-cloud writer, and local files pass through the standard
ownership-affinity enqueue funnel. Deterministic job keys make reruns idempotent; enqueue and
collision-classification failures are contained per file, and ``on_outcome`` reports each result as
it happens. All selected local files are enqueued immediately; lane concurrency caps bound the
drain rate.
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
    """Count ``analysis`` rows whose windows columns are all NULL (legacy rows predating window counters).

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
    # BEUI-02/D-08 fold, same as every other duration-router caller: effective
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
