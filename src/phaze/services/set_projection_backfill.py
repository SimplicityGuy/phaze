"""The one-time (and re-runnable) backfill for the set projection (phaze-x1qr3.3).

Reads only already-stored ``analysis_window`` rows (``musical_key`` / ``bpm`` / ``features``) --
**no re-analysis, no agent involvement** -- matching the epic's "one projection, then everything
rides it" sequencing (``services/set_projection.py``'s module docstring). Batches BY FILE (bead
design: "all windows of one file per transaction ... ``SELECT ... FOR UPDATE SKIP LOCKED``
semantics unnecessary: the backfill runs single-process").

IDEMPOTENT AND RESUMABLE BY ``projection_version``: :func:`select_files_needing_projection` names
exactly the files with no ``set_profile`` row at all, or one whose ``projection_version`` is behind
:data:`~phaze.services.set_projection_writer.CURRENT_PROJECTION_VERSION`. A second run of this
module against an already-backfilled corpus therefore selects nothing (the acceptance bar's
"no-op on the second run"), and a future weight change that bumps that constant
(phaze-x1qr3.12's operator blind check) re-derives only the rows the change actually affects,
never the whole corpus -- the same selection predicate serves both requirements.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import TYPE_CHECKING

from sqlalchemy import distinct, or_, select
import structlog

from phaze.models.analysis import AnalysisWindow
from phaze.models.set_profile import SetProfile
from phaze.services.set_projection_writer import CURRENT_PROJECTION_VERSION, annotate_window_orm_objects, upsert_set_profile


if TYPE_CHECKING:
    from collections.abc import Sequence
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)

PROGRESS_LOG_EVERY: int = 500
"""Structlog progress cadence (bead design: "Progress via structlog every N files")."""


@dataclass
class BackfillReport:
    """Counts a `phaze backfill set-projection` run reports, printed by the CLI wrapper."""

    files_scanned: int = 0
    files_projected: int = 0
    files_skipped_no_windows: int = 0
    files_failed: int = 0
    wall_clock_sec: float = 0.0


async def select_files_needing_projection(session: AsyncSession) -> Sequence[uuid.UUID]:
    """Every ``file_id`` with at least one ``analysis_window`` row whose ``set_profile`` is
    missing entirely, or present but behind :data:`CURRENT_PROJECTION_VERSION`.

    The ``LEFT OUTER JOIN`` (rather than a ``NOT EXISTS`` plus a second stale-version query) makes
    "missing" and "stale" the same predicate over the same joined row, so a single scan of
    ``analysis_window`` answers both halves of the resumability contract at once.

    WHAT THIS PREDICATE CANNOT SEE, and why the write side must not produce it (phaze-qj926). A row
    at :data:`CURRENT_PROJECTION_VERSION` is, to this query, a finished file -- however wrong its
    contents are. So a profile that outlives the windows it was derived from is unreachable by
    every future run of this backfill, and a file with no window rows at all is not even a row this
    scan visits. ``routers/agent_analysis._replace_analysis_windows`` therefore DELETES the profile
    alongside the windows it replaces and rewrites it only on a successful projection, which leaves
    a failed re-projection in the "missing" state this predicate does select. Repairability lives
    in that invariant, not here: widening this predicate cannot recover a file whose windows are
    gone, because the scan starts from them.
    """
    stmt = (
        select(distinct(AnalysisWindow.file_id))
        .outerjoin(SetProfile, SetProfile.file_id == AnalysisWindow.file_id)
        .where(or_(SetProfile.file_id.is_(None), SetProfile.projection_version < CURRENT_PROJECTION_VERSION))
    )
    return (await session.execute(stmt)).scalars().all()


async def backfill_one_file(session: AsyncSession, file_id: uuid.UUID) -> bool:
    """Project one file's already-stored windows. Returns ``False`` (a no-op) when it has none.

    Loads the file's :class:`~phaze.models.analysis.AnalysisWindow` rows as tracked ORM instances,
    mutates them in place via :func:`~phaze.services.set_projection_writer.annotate_window_orm_objects`,
    and upserts ``set_profile`` from the SAME instances -- the caller's ``session.commit()`` is what
    turns the attribute mutations into an ``UPDATE`` (SQLAlchemy's unit of work), so this function
    issues no bulk ``UPDATE`` statement of its own.
    """
    windows = (
        (await session.execute(select(AnalysisWindow).where(AnalysisWindow.file_id == file_id).order_by(AnalysisWindow.window_index))).scalars().all()
    )
    if not windows:
        return False
    annotate_window_orm_objects(windows)
    await upsert_set_profile(session, file_id, windows)
    return True


async def run_backfill(session: AsyncSession, *, progress_every: int = PROGRESS_LOG_EVERY) -> BackfillReport:
    """Walk every file :func:`select_files_needing_projection` names, one transaction per file.

    A single file's failure is caught, logged and counted rather than aborting the run -- at
    corpus scale a batch job that dies on the first malformed row would leave every later file
    unbackfilled for no reason connected to them; ``session.rollback()`` clears the failed file's
    half-applied transaction state before moving on so its failure cannot poison the next file's.
    """
    report = BackfillReport()
    start = time.monotonic()
    file_ids = await select_files_needing_projection(session)
    total = len(file_ids)
    for file_id in file_ids:
        report.files_scanned += 1
        try:
            if await backfill_one_file(session, file_id):
                report.files_projected += 1
                await session.commit()
            else:
                report.files_skipped_no_windows += 1
        except Exception:
            await session.rollback()
            report.files_failed += 1
            logger.warning("set_projection_backfill_file_failed", file_id=str(file_id), exc_info=True)
        if report.files_scanned % progress_every == 0:
            logger.info(
                "set_projection_backfill_progress",
                files_scanned=report.files_scanned,
                files_total=total,
                files_projected=report.files_projected,
                files_failed=report.files_failed,
            )
    report.wall_clock_sec = time.monotonic() - start
    logger.info(
        "set_projection_backfill_complete",
        files_scanned=report.files_scanned,
        files_projected=report.files_projected,
        files_skipped_no_windows=report.files_skipped_no_windows,
        files_failed=report.files_failed,
        wall_clock_sec=report.wall_clock_sec,
    )
    return report


__all__ = [
    "PROGRESS_LOG_EVERY",
    "BackfillReport",
    "backfill_one_file",
    "run_backfill",
    "select_files_needing_projection",
]
