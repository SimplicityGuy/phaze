"""Phase 88 (88-02, DRILL-01): the degrade-safe data helpers behind ``GET /pipeline/lanes/{backend_id}``.

Extracted from the former single-module ``services/backends.py`` (phaze-dr9df). Bounded, read-only,
secret-free reads for the lane drill-in pane (``_lane_detail.html``): recent completions
(:func:`get_lane_recent_completions`), which agent's SAQ queues a lane actually uses
(:func:`resolve_lane_queue_agent`) and that agent's per-tier depths
(:func:`get_lane_queue_depths`). Everything here degrades to ``[]`` / a ``note`` rather than raising
into the pane's own 5s tick (D-00b / PERF-01).

Sits BELOW :mod:`~phaze.services.backends.lane_metrics` in the package DAG:
``_local_lane_queued_working`` binds the local lane through :func:`resolve_lane_queue_agent` so the
lane cards and this pane can never disagree about WHICH agent's queue "the local lane" reads.

Every body here is verbatim -- nothing in this file needed restructuring to meet the package's
nesting budget.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import exists, func, select
import structlog

from phaze.config import get_settings
from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.services.backends.registry import resolve_compute_backend
from phaze.services.enqueue_router import LANES, NoActiveAgentError, select_active_agent
from phaze.services.pipeline import MUSIC_VIDEO_TYPES


if TYPE_CHECKING:
    from datetime import datetime
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings


logger = structlog.get_logger(__name__)


# --- Phase 88 (88-02, DRILL-01): degrade-safe lane-detail data helpers ---------------------
#
# Two bounded, read-only, secret-free reads that feed the `GET /pipeline/lanes/{backend_id}` body
# (_lane_detail.html). Both degrade to [] / 0 on any error so they can NEVER 500 the drill-in pane's
# own 5s tick (D-00b / PERF-01). Neither exposes any config/SecretStr/kube token -- only the CloudJob
# status/timestamp/file_id scalars and the broker depth counts.

# D-07: the fixed last-N cap on the recent-completions list -- predictable render cost under any
# throughput (no whole-corpus scan per poll). Newest-first, bounded by this LIMIT.
LANE_RECENT_N = 20


@dataclasses.dataclass(frozen=True)
class LaneCompletion:
    """One recent-completion row for a lane's recent-completions panel (phaze-2u8v.3).

    Fixes three defects found in the same panel:

    * ``label`` is the file's own name (``COALESCE(original_filename_repaired, original_filename)``),
      never a bare identifier. The panel previously rendered ``CloudJob.file_id.hex[:8]`` unlabelled and
      Robert could not tell what it was -- confirmed (ground truth query against the live archive) that
      it is a PREFIX OF THE FILE's UUID PRIMARY KEY (``files.id``), NOT a sha256 prefix as presumed --
      ``files.sha256_hash`` is a wholly separate column this code never touched. ``id`` is kept below so
      a caller/template can still compose an explicitly-labelled id fallback (never a bare hash/hex).
    * ``completed_at`` is the TRUE analysis completion time -- ``AnalysisResult.analysis_completed_at``,
      stamped synchronously by the ``/api/internal/agent/analysis/{file_id}`` callback at the instant
      analysis actually finished. For a kueue lane this is NOT the same instant as the previous
      ``CloudJob.updated_at`` this code used to render: ``cloud_job.status`` only flips to ``succeeded``
      the NEXT time ``reconcile_cloud_jobs`` runs, and that cron is registered
      ``CronJob(reconcile_cloud_jobs, cron="*/5 * * * *")`` (``tasks/controller.py``) -- a fixed-minute
      schedule, not an interval since the triggering event. So every kueue completion's ``updated_at``
      lands on the tick that happened to notice it, which is always exactly :00/:05/:10/.../:55 past the
      hour (confirmed against the live archive: rows read e.g. completed_at 20:05:21 but
      cloud_job.updated_at 20:10:00.03 -- the very next 5-minute tick) -- explaining the reported
      clustering. ``completed_at`` falls back to ``CloudJob.updated_at`` only in the defensive case where
      no ``AnalysisResult`` row is found (should not happen for a row this code selects as succeeded).
    """

    id: uuid.UUID
    label: str
    completed_at: datetime | None


def _completion_label(filename: str | None, file_id: uuid.UUID) -> str:
    """Return the human-legible completion label: the filename, or an explicitly-labelled id fallback.

    ``FileRecord.original_filename`` is a NOT NULL column, so the fallback below is defense-in-depth
    (an empty string, or a row this helper reaches through some future join this docstring doesn't
    anticipate) rather than the expected path. It is spelled ``file <hex> (id, not a hash)`` -- never a
    bare hex string -- so a fallback can never be mistaken for the sha256 the original bug presumed.
    """
    if filename:
        return filename
    return f"file {file_id.hex[:8]} (id, not a hash)"


# Two separately-typed base queries (kept apart, not a shared variably-shaped `stmt`, so each stays a
# single concrete `Select[...]` type -- mypy flags an in-place reassignment across an if/else with
# different column tuples). Both are `.limit()`-completed at the call site (the local one takes no
# extra WHERE; the cloud one is additionally scoped by backend_id/status there).
_LOCAL_RECENT_COMPLETIONS_SQL = (
    select(
        FileRecord.id,
        func.coalesce(FileRecord.original_filename_repaired, FileRecord.original_filename).label("label"),
        AnalysisResult.analysis_completed_at,
    )
    .select_from(FileRecord)
    .join(AnalysisResult, AnalysisResult.file_id == FileRecord.id)
    .where(
        FileRecord.file_type.in_(MUSIC_VIDEO_TYPES),
        AnalysisResult.analysis_completed_at.isnot(None),
        ~exists(select(CloudJob.id).where(CloudJob.file_id == FileRecord.id)),
    )
    .order_by(AnalysisResult.analysis_completed_at.desc(), FileRecord.id.desc())
)

_CLOUD_RECENT_COMPLETIONS_SQL = (
    select(
        CloudJob.id,
        CloudJob.file_id,
        func.coalesce(FileRecord.original_filename_repaired, FileRecord.original_filename).label("label"),
        AnalysisResult.analysis_completed_at,
        CloudJob.updated_at,
    )
    .select_from(CloudJob)
    .join(FileRecord, FileRecord.id == CloudJob.file_id)
    .outerjoin(AnalysisResult, AnalysisResult.file_id == CloudJob.file_id)
    .order_by(CloudJob.updated_at.desc(), CloudJob.id.desc())
)


async def get_lane_recent_completions(session: AsyncSession, backend_id: str, kind: str, limit: int = LANE_RECENT_N) -> list[LaneCompletion]:
    """Return up to ``limit`` most-recent completions for ANY lane kind, newest-first (D-07, phaze-2u8v.3).

    A ``local`` lane no longer returns ``[]`` unconditionally (Open Question 1's "omit, don't fabricate"
    resolution masked a real defect: the archive genuinely completes local work continuously -- 1501
    locally-completed files with no ``cloud_job`` row confirmed live -- and the panel showed "No
    completions" throughout). A :class:`LocalBackend` still writes NO ``cloud_job`` row (synchronous,
    no cron read), so local completions are read straight off ``files`` + ``analysis`` instead: a music/
    video file whose analysis has landed (``analysis_completed_at IS NOT NULL``) AND that carries no
    ``cloud_job`` row at all (mirrors the same local/cloud discriminator :meth:`LocalBackend.in_flight_count`
    already established -- the one documented gap there, a compute file's brief post-push window, is
    equally out of scope here). For compute/kueue lanes the query is unchanged in shape (``backend_id`` +
    ``status='succeeded'``, D-07 LIMIT) but now outer-joins ``analysis`` for the TRUE completion instant
    (see :class:`LaneCompletion`) and joins ``files`` for the display name. Any query error degrades to
    ``[]`` with a guarded rollback so it can never raise into the hot 5s tick (D-00b). Secret-free: only
    filename/timestamp/id scalars leave here.

    Ordering keeps its existing, already-regression-tested tiebreaker shape: local orders by
    ``analysis_completed_at`` DESC + ``FileRecord.id`` DESC; compute/kueue orders by ``CloudJob.updated_at``
    DESC + ``CloudJob.id`` DESC (unchanged from before this fix -- a monotonic-enough proxy for recency
    since reconcile ticks themselves run in increasing chronological order). Either way a partial ORDER BY
    alone would leave boundary ties in ANY order (heap order, which shifts with page layout, vacuum, and
    plan choice); appending the unique id makes the order TOTAL, so the LIMIT boundary is deterministic
    across repeated calls (mirrors the paging contract's mandatory unique tiebreaker).
    """
    try:
        if kind == "local":
            local_rows = (await session.execute(_LOCAL_RECENT_COMPLETIONS_SQL.limit(limit))).all()
        else:
            cloud_rows = (
                await session.execute(
                    _CLOUD_RECENT_COMPLETIONS_SQL.where(
                        CloudJob.backend_id == backend_id,
                        CloudJob.status == CloudJobStatus.SUCCEEDED.value,
                    ).limit(limit)
                )
            ).all()
    except Exception:
        logger.warning("lane_recent_completions_degraded", backend_id=backend_id, exc_info=True)
        try:
            await session.rollback()
        except Exception:
            logger.warning("lane_recent_completions_rollback_failed", backend_id=backend_id, exc_info=True)
        return []

    if kind == "local":
        return [
            LaneCompletion(id=row_id, label=_completion_label(label, row_id), completed_at=completed_at) for row_id, label, completed_at in local_rows
        ]
    return [
        LaneCompletion(id=cloud_job_id, label=_completion_label(label, file_id), completed_at=(analysis_completed_at or cloud_job_updated_at))
        for cloud_job_id, file_id, label, analysis_completed_at, cloud_job_updated_at in cloud_rows
    ]


# phaze-2u8v.1: the operator-facing copy for each way a lane can legitimately have NO per-tier SAQ
# figure. These are rendered INSTEAD of "analyze 0 · meta 0 · io 0" -- a fabricated
# zero row is indistinguishable from a genuinely idle agent, which is precisely how a saturated lane
# came to read as idle on every panel. Say WHY there is no number; never invent one.
KUEUE_NO_SAQ_QUEUE_NOTE = "Not applicable — a Kueue lane runs k8s Jobs, not SAQ agent-queue work."
NO_FILESERVER_AGENT_NOTE = "Unavailable — no live fileserver agent to read lane queues from."


@dataclasses.dataclass(frozen=True)
class LaneQueueIdentity:
    """WHICH agent's SAQ lane queues carry a compute-lane's work -- or WHY the lane has none (phaze-2u8v.1)."""

    agent_id: str | None
    note: str | None = None


@dataclasses.dataclass(frozen=True)
class LaneQueueDepths:
    """A lane's per-tier SAQ depths plus the identity they were read from (phaze-2u8v.1).

    ``depths is None`` means the lane has NO SAQ agent queue at all and ``note`` says why; it does NOT
    mean "zero". Callers must render the note, never a zero row.
    """

    agent_id: str | None
    depths: dict[str, int] | None
    note: str | None = None


async def resolve_lane_queue_agent(session: AsyncSession, backend_id: str, kind: str) -> LaneQueueIdentity:
    """Return the AGENT whose ``phaze-agent-<agent_id>-<lane>`` queues carry ``backend_id``'s work (phaze-2u8v.1).

    THE BUG THIS EXISTS TO CLOSE. ``AgentTaskRouter.queue_for``'s first parameter is an AGENT id, and a
    lane's registry ``id`` is NOT one. phaze-tbps established that for ``kind == "compute"`` (resolve the
    bound ``agent_ref``) but deliberately left local/kueue passing the raw ``backend_id`` through -- so a
    registry of ``[local, kueue vox]`` built ``phaze-agent-local-*`` and ``phaze-agent-vox-*``, queues no
    producer writes and no worker consumes. SAQ's ``count`` returns 0 for a queue that does not exist
    (not an error), so BOTH lane panels rendered "analyze 0 · meta 0 · io 0" while the
    agent-detail aggregate over the SAME work read the real figure off ``phaze-agent-<fileserver>-*``.
    A fully saturated lane was indistinguishable from an idle one.

    The resolution mirrors what each backend's ``dispatch`` ACTUALLY enqueues to -- that is the only
    definition of "this lane's queue" that cannot drift:

    * **local** -- :meth:`LocalBackend.dispatch` resolves ``select_active_agent(kind="fileserver")`` and
      enqueues ``process_file`` to ``queue_for(agent.id, ...)``. So a local lane's queues are the LIVE
      FILESERVER AGENT's queues, and a local lane legitimately reports the same per-tier figures as that
      agent's own detail pane -- they are the same queues, read twice. With no live fileserver agent
      there is nothing to read: report :data:`NO_FILESERVER_AGENT_NOTE`, not zeros (a dead fileserver is
      the one moment "0 queued" would be most dangerously wrong).
    * **compute** -- :meth:`ComputeAgentBackend` / ``routers/agent_push.py`` enqueue to the bound
      ``agent_ref``; unchanged from phaze-tbps, including its raw-``backend_id`` fallback when the
      registry read hiccups or the id names no compute entry.
    * **kueue** -- a Kueue lane enqueues NOTHING onto a ``phaze-agent-*`` queue. ``dispatch`` stages to
      S3 and submits a k8s Job; the analysis runs in that Job, which consumes no SAQ queue. Its ONE SAQ
      job (``s3_upload``) rides the FILESERVER agent's shared ``io`` lane alongside every other kueue
      lane's, so it is not attributable to one cluster -- keying a kueue lane off the fileserver would
      double-count it across N clusters AND paste the fileserver's local ``analyze`` backlog onto a
      cluster that is not running it. ``KueueBackend.agent_ref`` is NOT an escape hatch either: it names
      a bearer-token callback Agent row for the ``job_runner`` pods (phaze-ifcr) that no producer ever
      enqueues to, so keying off it would restore the exact silent-zero shape this fixes. The honest
      answer is :data:`KUEUE_NO_SAQ_QUEUE_NOTE`.

    Degrade-safe (D-00b): the local branch's ``select_active_agent`` read runs inside a SAVEPOINT so an
    error rolls back the NESTED scope ALONE -- recovering the aborted transaction WITHOUT expiring the
    caller's already-loaded lane/``CloudJob`` rows (a plain ``session.rollback()`` would) -- and returns
    a note. This never raises into the lane pane's 5s tick.
    """
    if kind == "local":
        try:
            # SAVEPOINT degrade (CR-01 / D-00b): a NoActiveAgentError (or any DB hiccup) rolls back the
            # nested scope alone. The read is read-only, so the rollback discards nothing.
            async with session.begin_nested():
                agent = await select_active_agent(session, kind="fileserver")
        except NoActiveAgentError:
            return LaneQueueIdentity(agent_id=None, note=NO_FILESERVER_AGENT_NOTE)
        except Exception:
            logger.warning("lane_queue_identity_degraded", backend_id=backend_id, kind=kind, exc_info=True)
            return LaneQueueIdentity(agent_id=None, note=NO_FILESERVER_AGENT_NOTE)
        return LaneQueueIdentity(agent_id=agent.id)

    if kind == "kueue":
        return LaneQueueIdentity(agent_id=None, note=KUEUE_NO_SAQ_QUEUE_NOTE)

    # compute (and any future agent-backed kind): phaze-tbps, verbatim.
    queue_key = backend_id
    try:
        cfg = cast("ControlSettings", get_settings())
        compute_backend = resolve_compute_backend(cfg, backend_id)
        if compute_backend is not None and compute_backend.agent_ref:
            queue_key = compute_backend.agent_ref
    except Exception:
        logger.warning("lane_queue_agent_ref_resolution_degraded", backend_id=backend_id, exc_info=True)
    return LaneQueueIdentity(agent_id=queue_key)


async def get_lane_queue_depths(session: AsyncSession, app_state: Any, backend_id: str, kind: str) -> LaneQueueDepths:
    """Return per-lane-tier queue depth ``{analyze, meta, io}`` for a lane's backing agent.

    Mirrors the ``get_queue_activity`` idiom (services/pipeline.py): each tier's depth is
    ``count("queued") + count("active")`` on the ``phaze-agent-<agent_id>-<lane>`` Queue of the agent
    :func:`resolve_lane_queue_agent` binds to this lane (read that docstring -- the binding IS the fix).
    Only the ``queued`` / ``active`` kinds are read (scheduled/cron jobs excluded). Every tier is isolated
    in its own ``try/except -> 0`` so a missing ``app.state.task_router`` (the test lifespan-skip) or a
    broker hiccup degrades that tier to 0 and NEVER 500s the 5s tick (D-00b). Bounded: one count pair per
    tier, no corpus scan.

    A lane with NO SAQ agent queue (kueue; or local with no live fileserver) returns ``depths=None`` and a
    ``note`` -- NOT a zero row. ``queue_for`` is not called at all in that case, so no phantom queue name
    is ever constructed.

    phaze-en7s7: connect-before-count (#217), the same fix applied to the sibling reader
    :func:`phaze.services.pipeline.get_agent_lane_depths`. ``queue_for`` constructs the lane's
    ``PostgresQueue`` with its psycopg pool ``open=False``; unless something else (the dashboard
    poll, an API-side enqueue on that exact lane) has already connected it, ``count()`` raises
    ``PoolClosed`` and the per-tier ``except`` below silently degrades it to 0 -- this docstring
    already claimed to "mirror the get_queue_activity idiom", which stopped being true the moment
    #217 added ``connect()`` there and not here. A lane never touched by an API-side enqueue is
    GUARANTEED to construct a virgin closed pool, so this was not a rare race but the common case
    for a lane :func:`resolve_lane_queue_agent` binds fresh. ``connect()`` is idempotent (SAQ
    guards on ``self._connected``).
    """
    identity = await resolve_lane_queue_agent(session, backend_id, kind)
    if identity.agent_id is None:
        return LaneQueueDepths(agent_id=None, depths=None, note=identity.note)

    depths: dict[str, int] = dict.fromkeys(LANES, 0)
    for lane in LANES:
        try:
            queue = app_state.task_router.queue_for(identity.agent_id, lane)
            await queue.connect()
            depths[lane] = await queue.count("queued") + await queue.count("active")
        except Exception:
            depths[lane] = 0
            logger.warning("lane_queue_depth_degraded", backend_id=backend_id, agent_id=identity.agent_id, lane=lane, exc_info=True)
    return LaneQueueDepths(agent_id=identity.agent_id, depths=depths)
