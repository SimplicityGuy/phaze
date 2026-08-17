"""Cross-cutting pipeline read primitives -- the corpus scope, the cloud double-dispatch
guard, and the degrade-safe COUNT wrapper every other pipeline read module composes against.

Extracted from the former monolithic ``services/pipeline.py`` (phaze-vsqpr). These three are the
only names shared by more than a couple of the domain modules; keeping them here is what lets each
domain module stay narrow instead of re-deriving them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from phaze.constants import EXTENSION_MAP, FileCategory
from phaze.models.cloud_job import CloudJobStatus


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select


logger = structlog.get_logger(__name__)


# Music + video file types -- the shared denominator for the per-file parallel
# stages (Metadata/Analyze). Mirrors the filter the trigger endpoints
# use at routers/pipeline.py:318-319 so the dashboard denominator matches the set
# of files those stages are actually enqueued for.
MUSIC_VIDEO_TYPES = [ext.lstrip(".") for ext, cat in EXTENSION_MAP.items() if cat in (FileCategory.MUSIC, FileCategory.VIDEO)]


# T-82-A1 (double-dispatch guard): a file whose ``cloud_job`` row is in any of these ACTIVE (non-terminal)
# statuses is currently being handled by the cloud path and MUST NOT be a local analyze candidate. The
# analyze-set trace (82-02 SUMMARY): the cloud hand-off enqueues ``push_file`` (never ``process_file``)
# and holds ``AWAITING_CLOUD``/``PUSHING`` with NO ``process_file:<id>`` scheduling-ledger row, so
# ``~inflight_clause(ANALYZE)`` alone does NOT exclude a cloud-dispatched file -- this explicit conjunct
# is load-bearing. ``FAILED`` is deliberately EXCLUDED: a terminally-failed cloud burst with no
# ``AnalysisResult`` is a legitimate local-retry candidate (the spill/recovery paths re-home it). A
# genuinely-done cloud burst (``SUCCEEDED`` with a landed ``AnalysisResult``) is already excluded by
# ``~done_clause`` inside ``eligible_clause``; listing ``SUCCEEDED`` here is the belt-and-suspenders that
# also covers the compute ``PUSHED`` window (``cloud_job.status='succeeded'`` while analysis still runs on
# the agent, before its ``process_file`` ledger row lands).
_ACTIVE_CLOUD_STATUSES: tuple[str, ...] = (
    CloudJobStatus.AWAITING.value,
    CloudJobStatus.UPLOADING.value,
    CloudJobStatus.UPLOADED.value,
    CloudJobStatus.SUBMITTED.value,
    CloudJobStatus.RUNNING.value,
    CloudJobStatus.SUCCEEDED.value,
)


async def _safe_count(session: AsyncSession, stmt: Select[Any], *, node: str) -> int:
    """Run a single-scalar COUNT statement, degrading to 0 on any failure.

    Per-source failure isolation mirroring :func:`get_queue_activity`: a bad source
    (a DB hiccup, an aborted transaction from a prior failed source) must degrade
    THIS node to 0, never raise into the 5s dashboard poll.

    The COUNT runs inside a SAVEPOINT (``session.begin_nested()``), mirroring
    :func:`get_stage_busy_counts`. On ANY error the nested scope is rolled back ALONE --
    recovering a Postgres "current transaction is aborted" state (so it cannot poison the
    COUNT queries for every subsequent stage) WITHOUT expiring the caller's already-loaded
    ORM objects. Several direct-request-session callers (``build_dashboard_context`` et al.)
    run this AFTER loading ``agents`` / ``recent_scans`` into the same session -- a plain
    ``session.rollback()`` would expire those rows and 500 the dashboard render on the next
    lazy load.
    """
    try:
        async with session.begin_nested():
            return int((await session.execute(stmt)).scalar() or 0)
    except Exception:
        logger.warning("stage_progress_degraded", node=node, exc_info=True)
        return 0
