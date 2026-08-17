"""phaze-5c6i2: the lane cards' four operator numbers -- total queued, queued, working, processed.

Extracted verbatim from the former single-module ``services/backends.py`` (phaze-dr9df). These reads
replace the misleading ``{in_flight}/{cap}`` numeral + saturation bar: ``in_flight`` conflates
"enqueued" with "executing" (the scheduling-ledger row exists at ENQUEUE time), so a saturated-looking
bar could mean nothing is actually running. Each figure comes from a source that ALREADY exists --
SAQ's own queued/active counts for local, the phaze-zyoag staged/analyzing seam for cloud -- rather
than a third re-derivation.

Every read degrades to ``None`` (an explicit unknown the template renders as an em-dash), NEVER to a
fabricated 0: see :func:`_safe_count_or_none` for why this file deliberately does not reuse
``pipeline._safe_count``'s 0-degrade.

Consumed by :mod:`~phaze.services.backends.lane_snapshot`, which composes them into the per-lane
dicts; builds on :mod:`~phaze.services.backends.lane_detail` for the local lane's agent binding.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import exists, func, select
import structlog

from phaze.enums.stage import Stage
from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.services.backends.lane_detail import resolve_lane_queue_agent
from phaze.services.pipeline import MUSIC_VIDEO_TYPES, _cloud_window_clauses, _safe_bucket_counts


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement


logger = structlog.get_logger(__name__)


# --- phaze-5c6i2: lane-card queued/working/processed metrics --------------------------------
#
# Replaces the misleading ``{in_flight}/{cap}`` numeral + saturation bar with the operator's four
# numbers: TOTAL QUEUED (analyze, global), QUEUED per lane, WORKING per lane, and PROCESSED per lane
# (24h primary + lifetime caption). See the bead description for the full rationale; the short version:
# ``in_flight`` conflates "enqueued" with "executing" (the scheduling-ledger row exists at ENQUEUE
# time), so a saturated-looking bar could mean nothing is actually running. These reads carry the
# split queued-vs-working sources that already exist (SAQ's own queued/active counts for local, the
# phaze-zyoag staged/analyzing seam for cloud) instead of re-deriving a third definition.

# The processed-count rolling window -- the "24h" half of the operator's "412 (24h) / 545 all time"
# target shape. Not a Settings knob (yet): a fixed, well-known window matching the target shape
# verbatim; promote to config if an operator ever needs a different one.
_PROCESSED_WINDOW = timedelta(hours=24)


async def _safe_count_or_none(session: AsyncSession, stmt: Any, *, node: str) -> int | None:
    """Run a single-scalar COUNT statement, degrading to ``None`` (NOT 0) on any failure (phaze-5c6i2).

    The sibling of :func:`phaze.services.pipeline._safe_count` with a DELIBERATELY different degrade
    value. ``_safe_count``'s 0-degrade is correct for a count whose true zero is itself a real, safe
    state (e.g. 0 in-flight). It is WRONG for a queue-depth-shaped read: a DB hiccup that silently
    renders "queued 0" on a healthy backlog of thousands is a worse lie than the ``{in_flight}/{cap}``
    numeral this bead replaces (acceptance rule 8 / the bead's DEGRADE POSTURE design note). Every
    lane-metric read below uses this wrapper so a failure surfaces to the template as an explicit
    unknown (an em-dash) instead of a fabricated zero. Same SAVEPOINT discipline as ``_safe_count``: the
    nested scope rolls back alone on error, recovering an aborted transaction without expiring the
    caller's already-loaded ORM objects.
    """
    try:
        async with session.begin_nested():
            return int((await session.execute(stmt)).scalar() or 0)
    except Exception:
        logger.warning("lane_metric_degraded", node=node, exc_info=True)
        return None


async def _local_lane_queued_working(session: AsyncSession, app_state: Any) -> tuple[int | None, int | None]:
    """Return the LOCAL lane's ``(queued, working)`` from SAQ's OWN ``analyze`` lane, kept SEPARATE (phaze-5c6i2).

    :func:`phaze.services.pipeline.get_agent_lane_depths` sums ``count("queued") + count("active")``
    into one number per lane; this reads the SAME two SAQ counts on the SAME ``analyze`` lane -- bound
    to the live fileserver agent via :func:`resolve_lane_queue_agent` (the IDENTICAL binding
    :func:`get_lane_queue_depths` uses for the lane-detail pane, so the two panels can never disagree
    about WHICH agent's queue "the local lane" reads) -- but keeps the two counts distinct instead of
    collapsing them. Neither figure is derived from
    :func:`phaze.services.stage_status.inflight_clause` (which cannot distinguish "enqueued" from
    "started" -- the exact defect this bead exists to fix; acceptance rule 3).

    Degrades to ``(None, None)`` -- never ``(0, 0)`` -- when there is no live fileserver agent to read
    (mirrors :data:`NO_FILESERVER_AGENT_NOTE`'s condition), when ``app_state`` itself is absent (callers
    that only need availability/admission, e.g. :func:`derive_cloud_hold_reason`, pass none), or on any
    broker hiccup: a missing agent or a dead broker is not evidence of an empty queue.
    """
    if app_state is None:
        return None, None
    identity = await resolve_lane_queue_agent(session, "local", "local")
    if identity.agent_id is None:
        return None, None
    try:
        queue = app_state.task_router.queue_for(identity.agent_id, "analyze")
        await queue.connect()
        queued = await queue.count("queued")
        working = await queue.count("active")
    except Exception:
        logger.warning("local_lane_queued_working_degraded", agent_id=identity.agent_id, exc_info=True)
        return None, None
    return queued, working


async def _cloud_lane_queued_working(session: AsyncSession, backend_id: str) -> tuple[int | None, int | None]:
    """Return a CLOUD lane's ``(queued, working)``, scoped to ``backend_id`` via the phaze-zyoag seam (phaze-5c6i2).

    ``queued`` = the pre-execution half of the bounded cloud window (:data:`STAGING` plus a
    compute-attributed SUBMITTED row); ``working`` = the executing half (a kueue-attributed SUBMITTED
    row -- admitted-or-queued-behind-quota counts as "in the cloud window, post-submit" under the
    zyoag option-(a) definition -- plus RUNNING). Reuses
    :func:`phaze.services.pipeline._cloud_window_clauses` verbatim (the SAME per-backend-kind split the
    "Staged (pushing)"/"Analyzing (cloud)" cards use) ANDed with ``backend_id`` so this lane's figures
    can never drift from those two cards' definition of the seam -- CONSUMING zyoag's decision rather
    than re-deriving it a third time (the bead's explicit dependency reason; acceptance rule 4).

    Degrades to ``(None, None)`` on any error -- an unknown queue depth must never render as a
    fabricated 0 (acceptance rule 8).
    """
    try:
        staged, analyzing = _cloud_window_clauses()
    except Exception:
        logger.warning("cloud_lane_queued_working_degraded", backend_id=backend_id, exc_info=True)
        return None, None
    queued = await _safe_count_or_none(session, select(func.count(CloudJob.id)).where(staged, CloudJob.backend_id == backend_id), node="lane_queued")
    working = await _safe_count_or_none(
        session, select(func.count(CloudJob.id)).where(analyzing, CloudJob.backend_id == backend_id), node="lane_working"
    )
    return queued, working


def _cloud_job_succeeded_for_backend(backend_id: str) -> ColumnElement[bool]:
    """Return ``EXISTS(a SUCCEEDED cloud_job for this file attributed to backend_id)`` (phaze-5c6i2)."""
    return exists(
        select(CloudJob.id).where(
            CloudJob.file_id == FileRecord.id, CloudJob.status == CloudJobStatus.SUCCEEDED.value, CloudJob.backend_id == backend_id
        )
    )


async def _lane_processed_counts(session: AsyncSession, *, backend_id: str | None) -> tuple[int | None, int | None]:
    """Return ``(processed_24h, processed_lifetime)`` for one lane, attributed by EXECUTION (phaze-5c6i2).

    Attribution keys on ``cloud_job.backend_id`` (the lane that EXECUTED the analysis), never on
    ``FileRecord.agent_id`` (the FILESERVER that scanned/owns the file -- a different axis entirely; see
    the bead's ATTRIBUTION design note). ``backend_id`` given -> direct: a completed file with a
    ``succeeded`` cloud_job row attributed to it. ``backend_id=None`` -> the LOCAL negation: completed
    with NO ``succeeded`` cloud_job row at all -- mirroring :meth:`LocalBackend.in_flight_count`'s own
    carve-out, ADAPTED from its live ``IN_FLIGHT``-status negation to a TERMINAL-status one (this reads
    completed history, not a live race).

    This adaptation is explicitly NOT subject to that method's documented compute-timing gap (a
    transient window where a compute row's cloud_job is already SUCCEEDED -- stamped at PUSH time --
    before its remote ``process_file`` has actually STARTED, which can misattribute a LIVE in-flight
    probe): every caller here gates on ``AnalysisResult.analysis_completed_at IS NOT NULL`` first, which
    is stamped only once execution genuinely FINISHES, wherever it ran. By the time that gate opens, a
    SUCCEEDED cloud_job's ``backend_id`` is a settled historical fact, so a completed file can never be
    double-counted or lost between the local and cloud attributions -- the gap cannot reach a PROCESSED
    count the way it can reach a live in-flight one. (No ``compute`` backend is configured in the
    current deployment -- local + kueue -- so this is documented for completeness per the bead's
    instruction to say so explicitly, not because it is presently load-bearing.)

    Degrades to ``(None, None)`` on any error (acceptance rule 8).
    """
    attribution = (
        ~exists(select(CloudJob.id).where(CloudJob.file_id == FileRecord.id, CloudJob.status == CloudJobStatus.SUCCEEDED.value))
        if backend_id is None
        else _cloud_job_succeeded_for_backend(backend_id)
    )
    base = (
        select(func.count(AnalysisResult.id))
        .select_from(AnalysisResult)
        .join(FileRecord, FileRecord.id == AnalysisResult.file_id)
        .where(
            AnalysisResult.analysis_completed_at.is_not(None),
            FileRecord.file_type.in_(MUSIC_VIDEO_TYPES),
            attribution,
        )
    )
    node = f"lane_processed_{backend_id or 'local'}"
    lifetime = await _safe_count_or_none(session, base, node=f"{node}_lifetime")
    cutoff = datetime.now(UTC) - _PROCESSED_WINDOW
    windowed = await _safe_count_or_none(session, base.where(AnalysisResult.analysis_completed_at >= cutoff), node=f"{node}_24h")
    return windowed, lifetime


async def get_analyze_queue_totals(session: AsyncSession, lanes: list[dict[str, Any]]) -> dict[str, int | None]:
    """Return the global "TOTAL QUEUED (analyze)" figure + its unrouted remainder (phaze-5c6i2, acceptance rule 2).

    ``unrouted_queued`` = Stage.ANALYZE's ``not_started`` bucket
    (:func:`phaze.services.pipeline._safe_bucket_counts`) -- files with NO scheduling-ledger row at all
    for analyze, i.e. not yet routed to ANY lane (the ``in_flight`` bucket already counts every
    routed-but-not-yet-done file, local or cloud, per the same ledger-existence-at-enqueue-time read the
    bead's motivation cites). ``total_queued`` sums that with every lane's OWN ``queued`` figure -- work
    assigned to a lane but not yet executing -- so ``total_queued >= sum(lane["queued"] for lane in
    lanes)`` holds by construction and the unrouted remainder is always the visible, non-negative
    difference between the two rendered numbers, never silently dropped.

    Degrades to ``{"total_queued": None, "unrouted_queued": <bucket value>}`` when ANY lane's own
    ``queued`` is itself degraded (``None``): a partial sum that silently omitted an unknown lane would
    UNDERSTATE the total exactly the way a 0-degrade would, so one unknown component propagates to the
    whole total rather than being quietly dropped. ``unrouted_queued`` keeps ``_safe_bucket_counts``'s
    OWN pre-existing degrade discipline (0 on error) unchanged -- it is not a new read this bead adds.
    """
    buckets = await _safe_bucket_counts(session, Stage.ANALYZE)
    unrouted = buckets["not_started"]
    queued_values = [lane.get("queued") for lane in lanes]
    if any(value is None for value in queued_values):
        return {"total_queued": None, "unrouted_queued": unrouted}
    lane_sum = sum(cast("int", value) for value in queued_values)
    return {"total_queued": unrouted + lane_sum, "unrouted_queued": unrouted}
