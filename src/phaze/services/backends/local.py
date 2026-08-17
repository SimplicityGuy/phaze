"""``LocalBackend`` -- the on-prem lane that analyses on the fileserver agent and writes no ``cloud_job``.

Extracted verbatim from the former single-module ``services/backends.py`` (phaze-dr9df).
"""

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
from phaze.services.stage_status import inflight_clause


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings
    from phaze.services.agent_task_router import AgentTaskRouter


logger = structlog.get_logger(__name__)


class LocalBackend(_BaseBackend):
    """On-prem/all-local backend -- analysis runs on the fileserver agent via ``process_file`` (no cloud_job).

    ``is_available`` is unconditionally True (local dispatch needs no remote cloud agent);
    ``in_flight_count`` is the REAL ledger-derived running count (phaze-xd8k, see below); ``reconcile``
    is a no-op (local completion is synchronous, no cron read). ``dispatch`` re-homes the ``process_file``
    local enqueue path (Phase-69 scheduler uses it; unit-tested here, NOT wired into the single-path drain).
    """

    async def is_available(self, session: AsyncSession) -> bool:  # noqa: ARG002 -- protocol signature; local needs no session probe
        """Always True -- local dispatch never depends on a remote cloud agent."""
        return True

    async def in_flight_count(self, session: AsyncSession) -> int:
        """Return the REAL local-lane running count (phaze-xd8k), not a hardcoded 0.

        A local burst writes NO ``cloud_job`` row, so :class:`_BaseBackend`'s ``cloud_job``-derived
        COUNT is structurally always 0 for this lane -- that hardcode was the observability bug: the
        ANALYZE header's ``analyzeActive`` (:func:`phaze.services.stage_status.inflight_clause` on
        ``Stage.ANALYZE``, the SAME scheduling-ledger ``process_file:<file_id>`` predicate the DAG
        derives ``analyzeActive`` from, D-01 authoritative source) counts EVERY in-flight ``process_file``
        job regardless of which agent it was routed to, while this lane rendered a literal 0 even when
        thousands of files were actively analyzing locally.

        The fix: count music/video files that are ``inflight_clause(ANALYZE)`` (a live
        ``process_file:<file_id>`` scheduling-ledger row) AND carry NO in-flight ``cloud_job`` row
        (D-10's ``{UPLOADING, UPLOADED, SUBMITTED, RUNNING}`` set). A cloud-routed file's ``process_file``
        is enqueued on ITS agent under the SAME deterministic ledger key
        (:func:`phaze.services.analysis_enqueue.process_file_job_key`), so the ledger key alone cannot
        distinguish "local" from "cloud" -- the ``~exists(cloud_job in-flight)`` exclusion is what carves
        out the local-only slice: a compute/kueue file either has no ``cloud_job`` row yet (still
        pre-stage) or still carries its in-flight ``cloud_job`` row while its ``process_file`` runs
        remotely (kueue's Job IS its ``process_file`` execution; compute's row stays SUBMITTED until the
        ``/pushed`` callback flips it, well past when ITS ledger row would appear). The one known gap
        (documented, not fixed here -- out of phaze-xd8k's scope): a COMPUTE file's ``cloud_job`` row is
        terminalized SUCCEEDED by ``report_pushed`` in the SAME transaction that enqueues its remote
        ``process_file`` (``routers/agent_push.py``), so for the brief window a compute agent is actually
        running analysis, this lane's real-count query cannot distinguish it from a genuinely local file
        and would over-count local by that amount. Harmless where no ``compute`` backend is configured
        (this bug's confirmed scenario: local + kueue only) and bounded by compute lane concurrency caps
        elsewhere.
        """
        stmt = (
            select(func.count(FileRecord.id))
            .where(FileRecord.file_type.in_(MUSIC_VIDEO_TYPES))
            .where(inflight_clause(Stage.ANALYZE))
            .where(~exists(select(CloudJob.id).where(CloudJob.file_id == FileRecord.id, CloudJob.status.in_([status.value for status in IN_FLIGHT]))))
        )
        return int((await session.execute(stmt)).scalar() or 0)

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: AgentTaskRouter) -> bool:
        """Flip ``file`` to LOCAL_ANALYZING then enqueue ``process_file`` on the fileserver queue -- one txn, no commit.

        Re-homes the local ``enqueue_process_file`` producer (``analysis_enqueue``). Writes NO
        ``cloud_job`` row. An absent agent degrades to a clean hold (NoActiveAgentError -> ``False``),
        matching the cron no-op discipline -- never a raise.

        CR-01 (SCHED-01/03): AFTER the fileserver gate (so an absent agent leaves the file untouched) and
        BEFORE the enqueue, the file is enqueued for local analysis in the caller-passed session. Phase 90
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
        # Phase 90 (D-09): the LOCAL_ANALYZING files.state dual-write was removed. The file leaves the
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
