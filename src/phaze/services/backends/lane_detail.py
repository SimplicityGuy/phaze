"""Degrade-safe data helpers behind ``GET /pipeline/lanes/{backend_id}`` (DRILL-01).

Bounded, read-only, secret-free reads for the lane drill-in pane (``_lane_detail.html``): recent completions
(:func:`get_lane_recent_completions`), which agent's SAQ queues a lane actually uses
(:func:`resolve_lane_queue_agent`) and that agent's per-tier depths
(:func:`get_lane_queue_depths`). Everything here degrades to ``[]`` / a ``note`` rather than raising
into the pane's own 5s tick (D-00b / PERF-01).

Also the Analyze queue view (phaze-lwz8n) -- the local lane's honest running / waiting / stuck split
(:func:`read_local_analyze_queue`), the Running-now rows (:func:`get_running_analyses`) and the waiting
list (:func:`get_waiting_page`) -- which both the workspace and this pane render, bound through the same
:func:`resolve_lane_queue_agent`.

Sits BELOW :mod:`~phaze.services.backends.lane_metrics` in the package DAG:
``_local_lane_queued_working`` binds the local lane through :func:`resolve_lane_queue_agent` so the
lane cards and this pane can never disagree about WHICH agent's queue "the local lane" reads.

"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
import uuid

from sqlalchemy import exists, func, select, text
import structlog

from phaze.config import get_settings
from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.analysis_sizing import CLOUD_HEARTBEAT_LOST_INTERVALS, HEARTBEAT_SURFACE_INTERVAL_SEC
from phaze.services.backends.registry import resolve_compute_backend
from phaze.services.enqueue_router import LANES, NoActiveAgentError, select_active_agent
from phaze.services.pipeline import MUSIC_VIDEO_TYPES
from phaze.services.queue_introspection import summarize_active_jobs


if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings


logger = structlog.get_logger(__name__)


# Degrade-safe lane-detail data helpers (DRILL-01)
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
      the NEXT time ``reconcile_cloud_jobs`` runs. That periodic observation timestamp can lag and
      cluster independently of the true completion instant (historically by up to five minutes before
      phaze-i3pkb.1 shortened reconciliation to every minute). ``completed_at`` falls back to
      ``CloudJob.updated_at`` only in the defensive case where
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

    Mirrors the ``get_queue_activity`` idiom (``phaze.services.pipeline.agents``): each tier's depth is
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


# The Analyze queue view: what is RUNNING now, what is WAITING, and whether anything is genuinely stuck (phaze-lwz8n).
#
# WHY
# ---
# The Analyze page used to read the local lane's health off ``in_flight`` against ``cap``. Both halves were
# wrong for the local lane: ``in_flight`` counts work that is merely QUEUED (a scheduling-ledger row exists
# from enqueue time; since phaze-1kowg only rows with a live broker job, but queued and running alike), and
# ``cap`` is derived from the CONTROL host's cores while the agent runs its own concurrency. A healthy
# backlog of 695 files therefore rendered a red "UNSAFE · exceeds capacity" banner. The operator's words:
# "RED is scary. BUT there's nothing wrong here ... maybe a view of a queue, showing what's running and
# what's left?"
#
# This section reads the local lane's own SAQ analyze queue instead, and splits it honestly:
#
# - **running** -- ``status='active'`` rows the worker actually started (``attempts`` present). The split
#   itself is :func:`~phaze.services.queue_introspection.summarize_active_jobs`, not a re-derivation.
# - **waiting** -- ``status='queued'`` rows plus claimed-but-unrun ``active`` rows (dequeued into the
#   worker's buffer, never executed). Both are "not started yet" from the operator's point of view.
#
# and reserves the alarm for the three conditions that are real problems rather than a long queue:
#
# - **stranded** -- ``summarize_active_jobs``'s own ``stranded`` figure (rows the active reaper would
#   delete). ``process_file`` runs ``timeout=0`` and is excluded from it by design, so on the analyze queue
#   this is normally 0; it is carried so a non-analyze row stranded on the lane is still surfaced.
# - **claimed but not started** past :func:`claim_overdue_seconds` -- the phaze-tch4d signature (jobs
#   claimed and never run). SAQ 0.26.4 (as installed) claims rows only for worker tasks waiting at that
#   instant (``PostgresQueue._dequeue`` limits the claim to ``_waiting``), so a claim is normally taken at
#   once. One still unstarted after the stall bound is a buffer nobody is draining (the phaze-grx3 /
#   phaze-o0n6 shape), or -- less harmfully -- a surplus claim waiting behind workers busy with long sets,
#   which SAQ's heartbeat sweep eventually retries. Either way the file is not progressing, so it is red.
# - **heartbeat lost** -- a started row whose ``touched`` is older than its OWN serialized ``heartbeat``.
#   That is exactly SAQ's ``Job.stuck`` for a ``timeout=0`` job, i.e. the broker's own verdict that the
#   run is dead, read per row rather than against one fixed constant.
#
# Every read runs inside a SAVEPOINT and degrades to ``None`` (an explicit unknown), never to a
# fabricated zero: a dead broker is not an empty queue.


# The broker key prefix of an analysis job. ``process_file:<file_id>`` is the deterministic key
# (``tasks._shared.deterministic_key``); the running list maps a row back to its file through it.
_PROCESS_FILE_PREFIX = "process_file:"

# The running list is bounded by the lane's concurrency in practice; the LIMIT only guards the render
# against a pathological broker state (phaze-o0n6 held 2,413 ``active`` rows on one queue).
RUNNING_LIMIT = 50

# One page of the waiting list. The list is loaded on demand, never by the 5s poll.
WAITING_PAGE_SIZE = 50


def claim_overdue_seconds() -> int:
    """How long a claimed-but-unrun row may wait before it is a red signal rather than a buffer.

    Bound to ``analysis_stall_timeout_sec`` (default 1800 s) rather than a new knob: it is the repo's
    existing "no progress for this long means something is wrong" threshold for analysis. A claim is
    normally taken the instant it is made (see the section comment above), so the generous bound flags
    a buffer that is not draining, not a lane that is merely busy for a few minutes.
    """
    return int(get_settings().analysis_stall_timeout_sec)


@dataclasses.dataclass(frozen=True)
class LocalAnalyzeQueue:
    """The local lane's analyze queue, split honestly (phaze-lwz8n). Every count is an observed number."""

    agent_id: str
    queue: str
    queued: int
    running: int
    claimed_unrun: int
    claimed_overdue: int
    stranded: int
    heartbeat_lost: int

    @property
    def waiting(self) -> int:
        """Not started yet: queued plus claimed-but-unrun."""
        return self.queued + self.claimed_unrun

    @property
    def stuck(self) -> int:
        """Rows that are a real problem, not a long queue: stranded, overdue claims, lost heartbeats."""
        return self.stranded + self.claimed_overdue + self.heartbeat_lost


@dataclasses.dataclass(frozen=True)
class RunningAnalysis:
    """One analysis executing now. ``None`` fields are unobservable for that lane, never zero."""

    file_id: uuid.UUID | None
    label: str
    lane: str
    lane_kind: str
    started_at: datetime | None
    heartbeat_at: datetime | None
    heartbeat_lost: bool
    fine_done: int | None
    fine_total: int | None
    coarse_done: int | None
    coarse_total: int | None

    @property
    def windows_done(self) -> int | None:
        """Fine plus coarse windows analyzed so far, or ``None`` before the totals are known."""
        if self.fine_total is None and self.coarse_total is None:
            return None
        return (self.fine_done or 0) + (self.coarse_done or 0)

    @property
    def windows_total(self) -> int | None:
        """Fine plus coarse windows the file has, or ``None`` before analysis has sized the file."""
        if self.fine_total is None and self.coarse_total is None:
            return None
        return (self.fine_total or 0) + (self.coarse_total or 0)

    @property
    def percent(self) -> int | None:
        """Whole-number progress for the bar, clamped to 0..100."""
        total = self.windows_total
        if not total:
            return None
        return max(0, min(100, (self.windows_done or 0) * 100 // total))


@dataclasses.dataclass(frozen=True)
class WaitingAnalysis:
    """One analysis that has not started: ``claimed`` means the agent dequeued it but never ran it."""

    file_id: uuid.UUID | None
    label: str
    claimed: bool
    claimed_at: datetime | None
    enqueued_at: datetime | None
    overdue: bool


@dataclasses.dataclass(frozen=True)
class WaitingPage:
    """A page of the waiting list. ``note`` explains an unavailable read; it never means "empty"."""

    rows: list[WaitingAnalysis]
    page: int
    page_size: int
    has_next: bool
    note: str | None = None


def _ms_to_datetime(value: Any) -> datetime | None:
    """Convert a SAQ epoch-milliseconds field to an aware datetime (0 / absent -> ``None``)."""
    if not value:
        return None
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


async def db_now(session: AsyncSession) -> datetime:
    """The reference time every lane_detail / analyze-queue age, overdue, and "N ago" render reads (phaze-jz0fm).

    ``_QUEUE_EXTRAS_SQL``'s claim-overdue / heartbeat-lost counts age a row against Postgres
    ``NOW()`` -- the database container's clock. Before this, :func:`get_running_analyses` and
    :func:`get_waiting_page` aged the SAME rows with Python's ``datetime.now(UTC)`` -- the app
    host's clock -- so a host-vs-container skew moved a rendered "N ago" label and an overdue flag
    independently of the SQL-derived counts for the same row (phaze-neo4z). Public (not ``_``-
    prefixed): the routers that render ``_analyze_queue.html`` / ``_analyze_waiting.html`` /
    ``_lane_detail.html`` call this ONCE per request and thread the same value into both these two
    functions (via their ``now`` kwarg) and the template's ``humanize_relative_time(..., now=...)``,
    so the displayed text and the server-computed overdue/heartbeat_lost flags always agree -- one
    query per render, never per row and never a second, independent read.
    """
    now = await session.scalar(select(func.now()))
    if now is None:  # pragma: no cover -- NOW() is never NULL; a bare `assert` here trips bandit B101 in src/
        raise RuntimeError("SELECT now() returned NULL")
    return now


def _file_id_from_key(key: str) -> uuid.UUID | None:
    """Return the file id a ``process_file:<file_id>`` broker key names, or ``None`` for any other key."""
    if not key.startswith(_PROCESS_FILE_PREFIX):
        return None
    try:
        return uuid.UUID(key.removeprefix(_PROCESS_FILE_PREFIX))
    except ValueError:
        return None


def _label(filename: str | None, key: str) -> str:
    """The file's display name, or the broker key itself (explicitly a key, never a bare id)."""
    return filename or f"job {key}"


# ONE statement for the counts ``summarize_active_jobs`` does not carry. The blob is decoded only for
# ``active`` rows (a queued row's blob is never read). Filtered by (status, queue), which SAQ's own
# ``saq_jobs_status_queue_*`` indexes cover.
#
# heartbeat_lost mirrors SAQ's ``Job.stuck`` heartbeat half verbatim: a positive per-row ``heartbeat``
# and ``now - touched > heartbeat``. ``touched`` is the right clock here, unlike in the reapers: for a
# STARTED heartbeat job SAQ bumps it on every heartbeat, and that bump is the liveness it certifies.
_QUEUE_EXTRAS_SQL = text(
    """
    SELECT
      count(*) FILTER (WHERE status = 'queued') AS queued,
      count(*) FILTER (
        WHERE status = 'active' AND NOT (j ? 'attempts') AND (j ? 'started')
          AND (EXTRACT(EPOCH FROM NOW()) * 1000 - (j->>'started')::bigint) / 1000.0 > :claim_overdue_seconds
      ) AS claimed_overdue,
      count(*) FILTER (
        WHERE status = 'active' AND (j ? 'attempts') AND (j ? 'touched')
          AND COALESCE((j->>'heartbeat')::bigint, 0) > 0
          AND (EXTRACT(EPOCH FROM NOW()) * 1000 - (j->>'touched')::bigint) / 1000.0 > (j->>'heartbeat')::bigint
      ) AS heartbeat_lost
    FROM (
      SELECT status, CASE WHEN status = 'active' THEN convert_from(job, 'UTF8')::jsonb END AS j
      FROM saq_jobs
      WHERE queue = :queue AND status IN ('queued', 'active')
    ) rows
    """
)

_RUNNING_ROWS_SQL = text(
    """
    SELECT key, (j->>'started')::bigint AS started_ms, (j->>'touched')::bigint AS touched_ms,
           COALESCE((j->>'heartbeat')::bigint, 0) AS heartbeat_s
    FROM (
      SELECT key, convert_from(job, 'UTF8')::jsonb AS j
      FROM saq_jobs
      WHERE queue = :queue AND status = 'active'
    ) rows
    WHERE j ? 'attempts'
    ORDER BY started_ms NULLS LAST, key
    LIMIT :limit
    """
)

# Claimed-but-unrun rows first (the agent already took them), then SAQ's own dequeue order.
_WAITING_ROWS_SQL = text(
    """
    SELECT key, status, (j->>'started')::bigint AS started_ms
    FROM (
      SELECT key, status, priority, scheduled, CASE WHEN status = 'active' THEN convert_from(job, 'UTF8')::jsonb END AS j
      FROM saq_jobs
      WHERE queue = :queue AND status IN ('queued', 'active')
    ) rows
    WHERE status = 'queued' OR NOT (j ? 'attempts')
    ORDER BY (status = 'active') DESC, priority, scheduled, key
    LIMIT :limit OFFSET :offset
    """
)


async def resolve_local_analyze_queue_name(session: AsyncSession, app_state: Any) -> tuple[str, str] | None:
    """Return ``(agent_id, queue_name)`` of the local lane's analyze queue, or ``None`` when there is none to read.

    Bound through :func:`resolve_lane_queue_agent` (the live fileserver agent, which is where
    ``LocalBackend.dispatch`` enqueues) and named by the router's own ``queue_for`` -- never a
    re-spelled ``phaze-agent-<id>-analyze`` string that could drift from what producers write.
    """
    if app_state is None:
        return None
    identity = await resolve_lane_queue_agent(session, "local", "local")
    if identity.agent_id is None:
        return None
    try:
        queue = app_state.task_router.queue_for(identity.agent_id, "analyze")
    except Exception:
        logger.warning("local_analyze_queue_name_degraded", agent_id=identity.agent_id, exc_info=True)
        return None
    return identity.agent_id, str(queue.name)


async def read_local_analyze_queue(session: AsyncSession, app_state: Any) -> LocalAnalyzeQueue | None:
    """Return the local lane's honest queue split, or ``None`` when it cannot be observed.

    Two statements: :func:`summarize_active_jobs` (running / claimed-unrun / stranded, the phaze-grx3
    split this module deliberately does not re-derive) and :data:`_QUEUE_EXTRAS_SQL` (queued depth,
    overdue claims, lost heartbeats). A degraded breakdown or a failed read returns ``None``.
    """
    resolved = await resolve_local_analyze_queue_name(session, app_state)
    if resolved is None:
        return None
    agent_id, queue_name = resolved
    breakdown = await summarize_active_jobs(session, queue_name)
    if breakdown.degraded:
        return None
    try:
        async with session.begin_nested():
            extras = (await session.execute(_QUEUE_EXTRAS_SQL, {"queue": queue_name, "claim_overdue_seconds": claim_overdue_seconds()})).one()
    except Exception:
        logger.warning("local_analyze_queue_degraded", queue=queue_name, exc_info=True)
        return None
    return LocalAnalyzeQueue(
        agent_id=agent_id,
        queue=queue_name,
        queued=int(extras.queued),
        running=breakdown.running,
        claimed_unrun=breakdown.claimed_unrun,
        claimed_overdue=int(extras.claimed_overdue),
        stranded=breakdown.stranded,
        heartbeat_lost=int(extras.heartbeat_lost),
    )


async def _file_facts(session: AsyncSession, file_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Any]:
    """Return ``{file_id: row}`` with the display name and window progress for the given files."""
    ids = list(file_ids)
    if not ids:
        return {}
    stmt = (
        select(
            FileRecord.id,
            func.coalesce(FileRecord.original_filename_repaired, FileRecord.original_filename).label("label"),
            AnalysisResult.fine_windows_analyzed,
            AnalysisResult.fine_windows_total,
            AnalysisResult.coarse_windows_analyzed,
            AnalysisResult.coarse_windows_total,
        )
        .select_from(FileRecord)
        .outerjoin(AnalysisResult, AnalysisResult.file_id == FileRecord.id)
        .where(FileRecord.id.in_(ids))
    )
    return {row.id: row for row in (await session.execute(stmt)).all()}


async def _local_running(session: AsyncSession, queue_name: str, lane_id: str, now: datetime) -> list[RunningAnalysis]:
    """The local lane's started rows, joined to their file's name and window progress."""
    rows = (await session.execute(_RUNNING_ROWS_SQL, {"queue": queue_name, "limit": RUNNING_LIMIT})).all()
    keyed = [(row, _file_id_from_key(row.key)) for row in rows]
    facts = await _file_facts(session, (file_id for _, file_id in keyed if file_id is not None))
    running: list[RunningAnalysis] = []
    for row, file_id in keyed:
        fact = facts.get(file_id) if file_id is not None else None
        heartbeat_at = _ms_to_datetime(row.touched_ms)
        heartbeat_lost = bool(row.heartbeat_s and heartbeat_at is not None and (now - heartbeat_at).total_seconds() > row.heartbeat_s)
        running.append(
            RunningAnalysis(
                file_id=file_id,
                label=_label(fact.label if fact is not None else None, row.key),
                lane=lane_id,
                lane_kind="local",
                started_at=_ms_to_datetime(row.started_ms),
                heartbeat_at=heartbeat_at,
                heartbeat_lost=heartbeat_lost,
                fine_done=fact.fine_windows_analyzed if fact is not None else None,
                fine_total=fact.fine_windows_total if fact is not None else None,
                coarse_done=fact.coarse_windows_analyzed if fact is not None else None,
                coarse_total=fact.coarse_windows_total if fact is not None else None,
            )
        )
    return running


def cloud_heartbeat_lost_after_seconds() -> float:
    """Seconds of silence after which a Kueue row reads "lost", derived from the pod's surface cadence.

    Not the SAQ ``heartbeat_s``: a pod re-POSTs its counts about once per
    :data:`HEARTBEAT_SURFACE_INTERVAL_SEC`, so that cadence is the only honest yardstick. Display only;
    no kill, requeue or wall-clock bound hangs off this (D-08, phaze-1b39).
    """
    return HEARTBEAT_SURFACE_INTERVAL_SEC * CLOUD_HEARTBEAT_LOST_INTERVALS


async def _kueue_running(session: AsyncSession, backend_ids: list[str], now: datetime) -> list[RunningAnalysis]:
    """Kueue pods running now (``cloud_job.status == RUNNING``). Start time is not observable.

    The heartbeat is ``analysis.updated_at``, which the pod's progress POSTs move. A pod with no counts
    posted yet (no analysis row, or one not yet sized) has no heartbeat to age, so it shows none and is
    never "lost": job_runner only logs before its first window, which is silence, not death.

    The analysis row must also belong to THIS attempt. A re-driven file keeps its prior attempt's counts,
    so its ``analysis.updated_at`` is old while the new pod is still starting; aging that would flag a
    healthy pod lost on its first render. ``cloud_job.updated_at`` is stamped when reconcile flips the
    row to RUNNING (``tasks/reconcile_cloud_jobs``), so an analysis row older than it carries no beat
    from this attempt and shows none. Any later write to the cloud_job row (a phase or inadmissible
    flip) moves that stamp forward, which errs toward SUPPRESSING the heartbeat until the next POST
    (about a minute) -- never toward a false "lost".
    """
    if not backend_ids:
        return []
    stmt = (
        select(
            CloudJob.file_id,
            CloudJob.backend_id,
            func.coalesce(FileRecord.original_filename_repaired, FileRecord.original_filename).label("label"),
            AnalysisResult.fine_windows_analyzed,
            AnalysisResult.fine_windows_total,
            AnalysisResult.coarse_windows_analyzed,
            AnalysisResult.coarse_windows_total,
            AnalysisResult.updated_at.label("analysis_updated_at"),
            CloudJob.updated_at.label("job_updated_at"),
        )
        .select_from(CloudJob)
        .join(FileRecord, FileRecord.id == CloudJob.file_id)
        .outerjoin(AnalysisResult, AnalysisResult.file_id == CloudJob.file_id)
        .where(CloudJob.status == CloudJobStatus.RUNNING.value, CloudJob.backend_id.in_(backend_ids))
        .order_by(CloudJob.backend_id, CloudJob.updated_at, CloudJob.file_id)
        .limit(RUNNING_LIMIT)
    )
    lost_after = cloud_heartbeat_lost_after_seconds()
    running: list[RunningAnalysis] = []
    for row in (await session.execute(stmt)).all():
        has_counts = row.fine_windows_total is not None or row.coarse_windows_total is not None
        this_attempt = has_counts and row.analysis_updated_at >= row.job_updated_at
        heartbeat_at = row.analysis_updated_at if this_attempt else None
        running.append(
            RunningAnalysis(
                file_id=row.file_id,
                label=_label(row.label, str(row.file_id)),
                lane=str(row.backend_id),
                lane_kind="kueue",
                started_at=None,
                heartbeat_at=heartbeat_at,
                heartbeat_lost=heartbeat_at is not None and (now - heartbeat_at).total_seconds() > lost_after,
                fine_done=row.fine_windows_analyzed,
                fine_total=row.fine_windows_total,
                coarse_done=row.coarse_windows_analyzed,
                coarse_total=row.coarse_windows_total,
            )
        )
    return running


async def get_running_analyses(
    session: AsyncSession, app_state: Any, lanes: list[dict[str, Any]], *, now: datetime | None = None
) -> list[RunningAnalysis] | None:
    """Return every analysis executing now, across the local lane and every Kueue lane in ``lanes``.

    ``lanes`` is the lane snapshot the caller already holds (its ids and kinds, no extra read). Compute
    lanes contribute nothing: they expose no execution signal (see ``lane_metrics._cloud_lane_active``).
    ``None`` means the list could not be read at all; a local lane with no live fileserver agent simply
    contributes no rows (its lane card already says why).

    ``now`` lets the caller pass the SAME :func:`db_now` reading it also hands the template's
    ``humanize_relative_time(..., now=...)`` (phaze-jz0fm), so the "N ago" text and this function's
    own heartbeat_lost flag age the row identically. Omitted, it reads the database clock itself.
    """
    local_ids = [str(lane["id"]) for lane in lanes if lane.get("kind") == "local"]
    kueue_ids = [str(lane["id"]) for lane in lanes if lane.get("kind") == "kueue"]
    try:
        if now is None:
            async with session.begin_nested():
                now = await db_now(session)
        running: list[RunningAnalysis] = []
        if local_ids:
            resolved = await resolve_local_analyze_queue_name(session, app_state)
            if resolved is not None:
                async with session.begin_nested():
                    running.extend(await _local_running(session, resolved[1], local_ids[0], now))
        async with session.begin_nested():
            running.extend(await _kueue_running(session, kueue_ids, now))
    except Exception:
        logger.warning("running_analyses_degraded", exc_info=True)
        return None
    return running


async def get_waiting_page(
    session: AsyncSession, app_state: Any, *, page: int = 1, page_size: int = WAITING_PAGE_SIZE, now: datetime | None = None
) -> WaitingPage:
    """Return one page of the local lane's waiting list: claimed-but-unrun first, then queued in SAQ order.

    ``has_next`` rides a ``page_size + 1`` sentinel, never a COUNT. Loaded on demand by the operator,
    never by the 5s poll, so a large backlog costs nothing until someone asks to see it.

    ``now`` -- see :func:`get_running_analyses`'s docstring; same contract, same :func:`db_now`.
    """
    page = max(1, page)
    resolved = await resolve_local_analyze_queue_name(session, app_state)
    if resolved is None:
        return WaitingPage(
            rows=[], page=page, page_size=page_size, has_next=False, note="Unavailable — no live fileserver agent to read the queue from."
        )
    overdue_after = claim_overdue_seconds()
    try:
        async with session.begin_nested():
            if now is None:
                now = await db_now(session)
            rows = (await session.execute(_WAITING_ROWS_SQL, {"queue": resolved[1], "limit": page_size + 1, "offset": (page - 1) * page_size})).all()
            has_next = len(rows) > page_size
            rows = rows[:page_size]
            keys = [row.key for row in rows]
            file_ids = [file_id for file_id in (_file_id_from_key(key) for key in keys) if file_id is not None]
            facts = await _file_facts(session, file_ids)
            enqueued = (
                dict(
                    (await session.execute(select(SchedulingLedger.key, SchedulingLedger.enqueued_at).where(SchedulingLedger.key.in_(keys))))
                    .tuples()
                    .all()
                )
                if keys
                else {}
            )
    except Exception:
        logger.warning("waiting_page_degraded", queue=resolved[1], exc_info=True)
        return WaitingPage(rows=[], page=page, page_size=page_size, has_next=False, note="Unavailable — the queue could not be read this time.")
    waiting: list[WaitingAnalysis] = []
    for row in rows:
        file_id = _file_id_from_key(row.key)
        fact = facts.get(file_id) if file_id is not None else None
        claimed = row.status == "active"
        claimed_at = _ms_to_datetime(row.started_ms) if claimed else None
        waiting.append(
            WaitingAnalysis(
                file_id=file_id,
                label=_label(fact.label if fact is not None else None, row.key),
                claimed=claimed,
                claimed_at=claimed_at,
                enqueued_at=enqueued.get(row.key),
                overdue=claimed_at is not None and (now - claimed_at).total_seconds() > overdue_after,
            )
        )
    return WaitingPage(rows=waiting, page=page, page_size=page_size, has_next=has_next)
