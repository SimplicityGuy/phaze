"""The terminal analyze-failure bucket and its precise stalled-kill subset.

Extracted from the former monolithic ``services/pipeline.py`` (phaze-vsqpr).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import exists, func, select

from phaze.enums.stage import Stage
from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
from phaze.services.pipeline.common import _safe_count
from phaze.services.stage_status import (
    failed_clause,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# --- ANALYSIS_FAILED bucket (Phase 44, D-02) --------------------------------------------
#
# The files that GAVE UP -- terminal windowed-analysis failure (Phase 43 sets
# FileState.ANALYSIS_FAILED). This is its OWN bucket, intentionally ABSENT from
# PIPELINE_STAGES (lines 40-49): adding it there would double-count failed files in the
# linear stat bar. Originally surfaced on the dashboard alongside a STRAGGLER bucket
# (still-running jobs past a running-age threshold) as two distinct outcomes of the
# 4h-timeout incident; phaze-g84sk removed that running-age proxy once phaze-w55w1's
# heartbeat-stall watchdog made a genuine stall land HERE (reason="timeout") instead, and
# replaced it with the precise STALLED bucket below (:func:`get_analysis_stalled_count`) --
# a subset of THIS bucket, not a separate live-job read. Reads the indexed files.state
# (ix_files_state, models/file.py:74) -- NOT saq_jobs (a failed file has no live job).


async def get_analysis_failed_files(session: AsyncSession) -> list[FileRecord]:
    """Return the FileRecords with a terminal analyze-failure marker (the analysis-gave-up bucket).

    Phase 90 (PR-A, D-09): DERIVED from ``failed_clause(Stage.ANALYZE)`` (an ``analysis`` row whose
    ``failed_at`` is non-NULL) -- no longer the retired ``files.state = 'analysis_failed'`` column.
    Composes the LOCKED clause verbatim. Includes files that stalled under phaze-w55w1's
    heartbeat watchdog (reason="timeout") as well as ones that crashed -- these files have
    terminally failed and carry no live job.
    """
    result = await session.execute(select(FileRecord).where(failed_clause(Stage.ANALYZE)))
    return list(result.scalars().all())


async def get_analysis_failed_count(session: AsyncSession) -> int:
    """Return COUNT of files in ``FileState.ANALYSIS_FAILED``, degrading to 0 on any DB error.

    Poll-safe via :func:`_safe_count` (the standard stage-count degrade discipline): a DB hiccup
    degrades this node to 0 and rolls back the aborted transaction rather than 500ing the hot 5s
    /pipeline/stats poll. ``ANALYSIS_FAILED`` is its
    own bucket and is deliberately NOT added to ``PIPELINE_STAGES`` (D-02 -- it would double-count
    in the linear bar).
    """
    return await _safe_count(
        session,
        # Phase 90 (PR-A, D-09): DERIVED from the analyze-failure marker (analysis.failed_at NOT NULL)
        # via the LOCKED ``failed_clause`` builder -- no longer the ``files.state`` column. Composes the
        # clause verbatim (never re-spells the inner exists) so the DERIV-04 equivalence guarantee holds.
        select(func.count(FileRecord.id)).where(failed_clause(Stage.ANALYZE)),
        node="analysis_failed",
    )


# The exact prefix `routers/agent_analysis.py::report_analysis_failed` composes onto
# `analysis.error_message` for a stalled kill: `sanitize_pg_text(f"{body.reason}: {body.error}")`
# where `body.reason` is the wire `AnalysisFailurePayload.reason` literal `tasks/functions.py`
# sends for a heartbeat-stall kill (`except TimeoutError` -> `reason="timeout"`, see
# `AnalysisStalledError` in services/analysis_exec.py). "crashed" (subprocess/exit-code failure)
# and "error" (everything else) are the only other wire values, so this prefix is unambiguous --
# never re-derive it from a substring/heuristic on the free-text `error` detail that follows.
_STALL_ERROR_PREFIX = "timeout: "


async def get_analysis_stalled_count(session: AsyncSession) -> int:
    """Return COUNT of ANALYSIS_FAILED files the heartbeat watchdog killed for stalling, degrading to 0 on any DB error.

    phaze-g84sk: the Phase 44 STRAGGLER bucket (still-running jobs past a running-age threshold)
    was removed because running age stopped meaning "stuck" once phaze-w55w1 made a multi-hour
    exhaustive analysis normal. The operator's replacement ask is a PRECISE stalled-kill count
    rather than that running-age guess -- and phaze-w55w1's plumbing already records exactly this:
    a heartbeat-stall kill is TERMINAL and lands in ANALYSIS_FAILED with ``error_message`` composed
    as ``"timeout: <detail>"`` (see :data:`_STALL_ERROR_PREFIX`), distinguishable from a crashed
    child (``"crashed: ..."``) or any other terminal error (``"error: ..."``). This is therefore a
    SUBSET of :func:`get_analysis_failed_count`, not a second live-job read and NOT an age
    comparison -- no new telemetry, no ``saq_jobs`` query, no threshold input at all, degrade-safe
    via the same :func:`_safe_count` SAVEPOINT discipline.
    """
    return await _safe_count(
        session,
        select(func.count(FileRecord.id)).where(
            failed_clause(Stage.ANALYZE),
            exists(
                select(AnalysisResult.id).where(
                    AnalysisResult.file_id == FileRecord.id,
                    AnalysisResult.failed_at.isnot(None),
                    AnalysisResult.error_message.startswith(_STALL_ERROR_PREFIX),
                )
            ),
        ),
        node="analysis_stalled",
    )


# --- Straggler detector: REMOVED, replaced with the precise STALLED bucket (phaze-g84sk) ------
#
# Phase 44 (D-01) added `get_straggler_count`: an active `process_file` job whose running age
# exceeded `straggler_threshold_sec` (config, default 6600 -- mirroring the then-existing
# `analysis_inner_timeout_sec`) was flagged a "straggler" on the dashboard. phaze-w55w1 (ADR-0007
# §7) retired wall-clock analysis timeouts for progress-heartbeat liveness: a job now runs however
# long real work takes, and the analysis driver's own stall watchdog (`analysis_stall_timeout_sec`)
# kills a genuinely wedged child well before any dashboard poll could observe it. That leaves
# running AGE with no honest meaning -- a multi-hour concert set legitimately crosses the old
# threshold and is perfectly healthy, exactly the false-positive risk `docs/configuration.md`
# already warned about post-w55w1. A killed-for-stall job does not vanish either: it terminates
# into the SAME ANALYSIS_FAILED bucket (`get_analysis_failed_count` below) with `reason="timeout"`
# and a stored stall-detail message (`tasks/functions.py::process_file`), so the "gave up" bucket
# already carries every case the straggler bucket used to approximate. Per operator follow-up, the
# amber tile was not dropped outright -- `get_analysis_stalled_count` below replaces the running-
# age GUESS with a PRECISE count of that same reason="timeout" subset, reusing the terminal record
# rather than building a new live "still running" gauge (which would just reintroduce age-as-a-
# stuck-proxy under a different name). See the bead comment on phaze-g84sk for the full writeup.
