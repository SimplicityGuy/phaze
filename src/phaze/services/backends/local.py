"""``LocalBackend`` -- the on-prem lane that analyses on the fileserver agent and writes no ``cloud_job``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import exists, func, select
import structlog

from phaze.config import get_settings
from phaze.enums.stage import Stage
from phaze.models.cloud_job import CloudJob
from phaze.models.file import FileRecord
from phaze.services.analysis_enqueue import enqueue_process_file
from phaze.services.backends.base import IN_FLIGHT, _BaseBackend
from phaze.services.enqueue_router import NoActiveAgentError, lane_for_task, select_active_agent
from phaze.services.pipeline import MUSIC_VIDEO_TYPES
from phaze.services.stage_status import inflight_clause, live_job_clause


if TYPE_CHECKING:
    from sqlalchemy import Select
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings
    from phaze.services.agent_task_router import AgentTaskRouter


logger = structlog.get_logger(__name__)


def _local_in_flight_stmt(*, require_live_job: bool) -> Select[tuple[int]]:
    """Build :meth:`LocalBackend.in_flight_count`'s COUNT -- with the ``saq_jobs`` liveness conjunct, or without it.

    ``require_live_job=False`` is the pre-phaze-1kowg ledger-only count, kept ONLY as the degrade path for
    an unreadable ``saq_jobs``. The live conjunct is a positive ``EXISTS`` in a conjunction, so the planner
    can semi-join it rather than cost a per-row SubPlan (the plan-shape lesson in
    :func:`phaze.services.stage_status.not_running_clause`).
    """
    stmt = (
        select(func.count(FileRecord.id))
        .where(FileRecord.file_type.in_(MUSIC_VIDEO_TYPES))
        .where(inflight_clause(Stage.ANALYZE))
        .where(~exists(select(CloudJob.id).where(CloudJob.file_id == FileRecord.id, CloudJob.status.in_([status.value for status in IN_FLIGHT]))))
    )
    if require_live_job:
        stmt = stmt.where(live_job_clause(Stage.ANALYZE))
    return stmt


class LocalBackend(_BaseBackend):
    """On-prem/all-local backend -- analysis runs on the fileserver agent via ``process_file`` (no cloud_job).

    ``is_available`` is unconditionally True (local dispatch needs no remote cloud agent);
    ``in_flight_count`` is the REAL running count -- ledger row AND live ``saq_jobs`` job (phaze-xd8k,
    phaze-1kowg, see below); ``reconcile`` is a no-op (local completion is synchronous, no cron read). ``dispatch`` re-homes the ``process_file``
    local enqueue path (backend scheduler uses it; unit-tested here, NOT wired into the single-path drain).
    """

    async def is_available(self, session: AsyncSession) -> bool:  # noqa: ARG002 -- protocol signature; local needs no session probe
        """Always True -- local dispatch never depends on a remote cloud agent."""
        return True

    async def in_flight_count(self, session: AsyncSession) -> int:
        """Return the REAL local-lane running count (phaze-xd8k): files whose ``process_file`` is RUNNING here.

        A local burst writes NO ``cloud_job`` row, so :class:`_BaseBackend`'s ``cloud_job``-derived
        COUNT is structurally always 0 for this lane -- that hardcode was the observability bug: the
        ANALYZE header's ``analyzeActive`` (:func:`phaze.services.stage_status.inflight_clause` on
        ``Stage.ANALYZE``, the SAME scheduling-ledger ``process_file:<file_id>`` predicate the DAG
        derives ``analyzeActive`` from, D-01 authoritative source) counts EVERY in-flight ``process_file``
        job regardless of which agent it was routed to, while this lane rendered a literal 0 even when
        thousands of files were actively analyzing locally.

        The count: music/video files that are ``inflight_clause(ANALYZE)`` (a ``process_file:<file_id>``
        scheduling-ledger row) AND ``live_job_clause(ANALYZE)`` (a ``queued``/``active`` ``saq_jobs`` row
        on that same key) AND carry NO in-flight ``cloud_job`` row (D-10's ``{UPLOADING, UPLOADED,
        SUBMITTED, RUNNING}`` set). This is the D-01a RUNNING refinement of in-flight, not the bare ledger.

        WHY THE BROKER PROBE (phaze-1kowg). A ledger row is "was scheduled", not "is running": an ORPHANED
        row (scheduled, running nowhere, not domain-complete -- see
        :func:`phaze.services.stage_status.orphaned_clause`) stands until ``recover_orphaned_work`` re-drives
        it, and the ledger is RIGHT to hold it. Counting those as occupied slots is wrong for a CAPACITY
        read: production measured 2374 such rows (enqueued 2026-07-16..08-08, zero ``process_file`` jobs in
        the broker) against ``cap=1``, so ``remaining`` was 0 forever and every cloud-attempt-exhausted
        file -- routable ONLY to local -- could never move (spike phaze-tch4d). An orphaned row does not
        occupy the fileserver agent, so it must not occupy the lane. The ledger rows themselves are left
        alone: reaping them would discard owed work (``phaze.tasks.ledger_reaper``'s first guard);
        re-driving them is recovery's job, scoped by phaze-5y73k.

        Consumers: the drain's once-per-tick capacity snapshot (``remaining = cap - in_flight``) and the
        lane grid's ``in_flight`` (:mod:`phaze.services.backends.lane_snapshot`, read by the Analyze page).
        Both want "occupying the lane now". A still-``queued`` job counts -- it holds its place on the
        agent's analyze queue, and a parked (paused) job keeps ``status='queued'`` too.

        DEGRADE. The broker probe runs in a SAVEPOINT; if ``saq_jobs`` is unreadable the count falls back
        to the ledger-only count this method returned before phaze-1kowg -- the CONSERVATIVE direction
        (over-count, so the lane holds rather than over-admits), mirroring D-01a's "revert to exactly
        today's ledger-only behavior" rule for every other broker-corroborated reader.

        Known gaps, both bounded and documented rather than fixed here:

        - The ledger row is written by ``before_enqueue`` in its own transaction BEFORE SAQ inserts the
          ``saq_jobs`` row, so for that instant a just-enqueued job is not yet counted. The drain holds its
          advisory lock across dispatch + commit, so no other tick can observe the gap.
        - (phaze-xd8k) The ledger key cannot distinguish "local" from "cloud": a cloud-routed file's
          ``process_file`` runs under the SAME deterministic key
          (:func:`phaze.services.analysis_enqueue.process_file_job_key`). The ``~exists(cloud_job
          in-flight)`` exclusion carves out the local-only slice, but a COMPUTE file's ``cloud_job`` row
          is terminalized SUCCEEDED by ``report_pushed`` in the SAME transaction that enqueues its remote
          ``process_file`` (``routers/agent_push.py``), so while a compute agent is actually running
          analysis this count cannot tell it from a genuinely local file and over-counts local by that
          amount. Harmless where no ``compute`` backend is configured (local + kueue) and bounded by
          compute lane concurrency caps elsewhere.
        """
        try:
            async with session.begin_nested():
                return int((await session.execute(_local_in_flight_stmt(require_live_job=True))).scalar() or 0)
        except Exception:
            logger.warning("local_in_flight_degraded: saq_jobs liveness probe failed -> ledger-only count", exc_info=True)
        return int((await session.execute(_local_in_flight_stmt(require_live_job=False))).scalar() or 0)

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: AgentTaskRouter) -> bool:
        """Flip ``file`` to LOCAL_ANALYZING then enqueue ``process_file`` on the fileserver queue -- one txn, no commit.

        Re-homes the local ``enqueue_process_file`` producer (``analysis_enqueue``). Writes NO
        ``cloud_job`` row. An absent agent degrades to a clean hold (NoActiveAgentError -> ``False``),
        matching the cron no-op discipline -- never a raise.

        CR-01 (SCHED-01/03): AFTER the fileserver gate (so an absent agent leaves the file untouched) and
        BEFORE the enqueue, the file is enqueued for local analysis in the caller-passed session. PR-A
        (D-09) removed the former LOCAL_ANALYZING files.state flip; the file leaves the cloud-staging
        candidate set via its ``process_file:<id>`` scheduling-ledger row (the derived inflight source), so a
        locally-spilled file is no longer a drain candidate and can NOT be double-dispatched to a cloud
        backend while its ``process_file`` is in flight (the Backend.dispatch contract: dispatch "removes
        the file from further drain consideration"). NEVER commits -- the drain owns the single post-loop
        commit under the advisory lock, so the flip+enqueue are atomic (a rollback leaves the file
        AWAITING_CLOUD, safe to re-try, never a limbo LOCAL_ANALYZING without a queued job).
        """
        cfg = cast("ControlSettings", get_settings())
        try:
            agent = await select_active_agent(session, kind="fileserver")
        except NoActiveAgentError:
            logger.info("LocalBackend.dispatch hold: no fileserver agent online", file_id=str(file.id))
            return False
        # D-09: the LOCAL_ANALYZING files.state dual-write was removed. The file leaves the
        # AWAITING_CLOUD candidate set via the process_file:<id> scheduling-ledger row that
        # enqueue_process_file's before_enqueue hook writes (the derived inflight_clause source PR-A reads).
        queue = task_router.queue_for(agent.id, lane_for_task("process_file"))
        job = await enqueue_process_file(queue, file, agent.id, cfg.models_path)
        # WR-01: a deterministic-key ``process_file:<id>`` dedup returns None (the file is already being
        # analyzed locally) -> report NOT-newly-staged so the drain's staged tally is honest; a genuine
        # enqueue returns a saq.Job -> staged. Mirrors ComputeAgentBackend/KueueBackend's return contract.
        # The state flip above stands regardless of the dedup outcome (the file has left AWAITING_CLOUD).
        return job is not None

    async def reconcile(self, session: AsyncSession, ctx: dict[str, Any] | None = None) -> dict[str, int] | None:  # noqa: ARG002 -- protocol signature; local has no cron read
        """No-op: local analysis completion is synchronous -- there is no cron read to run."""
        return None
