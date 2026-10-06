"""Scanned / deduped / unique reconciliation -- the Discovery DAG-node subtitle and its
per-agent annotations.

"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import aliased
import structlog

from phaze.models.file import FileRecord
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.services.pipeline.common import _safe_count


if TYPE_CHECKING:
    from collections.abc import Collection
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


# Scanned / deduped / unique reconciliation
# The Discovery DAG node shows COUNT(files) while the agent scan total is SUM(scan_batches
# .total_files). The two legitimately differ: an agent walks total_files paths but each path
# upserts onto the NFC-normalized composite unique key (agent_id, original_path), so duplicate
# / normalization-collision walks collapse onto an existing row instead of inserting a new one.
# That gap is "deduped", NOT lost work. These helpers compute it degrade-safely so the apparent
# bug reads as a self-explaining reconciliation.
#
# LOCKED formulas:
#   scanned   = SUM over agents of (each agent's MOST RECENT completed ScanBatch).total_files
#               (re-scan-safe: a re-scan makes a NEW completed batch; summing ALL would inflate).
#   deduped   = max(0, scanned - discovery_done); discovery_done = COUNT(all FileRecord rows).
#   per-agent = max(0, agent_latest_completed.total_files - COUNT(files WHERE agent_id = X)).
# A None scanned (no completed batches OR a DB error) is the "hide the whole line" sentinel,
# deliberately distinct from a real 0.


def deduped_count(scanned: int | None, unique: int) -> int | None:
    """Pure reconciliation arithmetic: None passthrough + clamp-to-zero (no I/O, unit-testable).

    Returns None when ``scanned`` is None (the UI then HIDES the reconciliation line — a None
    scan total is "unavailable", not "zero deduped"). Otherwise returns ``max(0, scanned - unique)``
    so the deduped count can never go negative when more files exist than the latest scan walked
    (a stale/older scan total against a freshly-grown file table).
    """
    if scanned is None:
        return None
    return max(0, scanned - unique)


async def get_scanned_total(session: AsyncSession) -> int | None:
    """SUM each agent's LATEST completed ``ScanBatch.total_files``, degrading to None on any error.

    Re-scan-safe: a re-scan creates a NEW completed batch for the same agent, so summing ALL
    completed batches would double-count. Instead a window function ranks each agent's completed
    batches by ``created_at`` DESC, ``ScanBatch.id`` DESC and only ``rn == 1`` (the most recent) is
    summed. The ``id`` tiebreaker (phaze-imih) matters because ``ScanBatch.created_at`` carries no
    uniqueness constraint (``TimestampMixin``'s ``server_default=func.now()`` is transaction-time
    constant): two completed batches for one agent can share ``created_at``, and without the
    tiebreaker the ``rn == 1`` pick on a tie is executor-arbitrary -- matching the corrected window
    in :func:`get_agent_reconciliations` (phaze-n2d2) so the two sums can never disagree.

    Returns None (NOT 0) both when there are no completed batches and on any DB error: None is the
    "hide the reconciliation" sentinel, distinct from a genuine scanned total of 0. Mirrors the
    :func:`_safe_count` / :func:`get_stage_controls` degrade discipline (log → SAVEPOINT rollback →
    sentinel) so it never raises into the 5s dashboard poll.

    The read runs inside a SAVEPOINT (``session.begin_nested()``) so a query error rolls back the
    NESTED scope ALONE, never the caller's shared session -- a plain ``session.rollback()`` here
    would expire the ``agents`` / ``recent_scans`` rows ``build_dashboard_context`` already loaded
    on this session and 500 the render on the next lazy load.
    """
    try:
        async with session.begin_nested():
            ranked = (
                select(
                    ScanBatch.total_files.label("total_files"),
                    func.row_number().over(partition_by=ScanBatch.agent_id, order_by=(ScanBatch.created_at.desc(), ScanBatch.id.desc())).label("rn"),
                )
                .where(ScanBatch.status == ScanStatus.COMPLETED.value)
                .subquery()
            )
            total = (await session.execute(select(func.sum(ranked.c.total_files)).where(ranked.c.rn == 1))).scalar()
        return int(total) if total is not None else None
    except Exception:
        logger.warning("scanned_total_degraded", exc_info=True)
        return None


async def get_global_reconciliation(session: AsyncSession) -> dict[str, int | None]:
    """Return ``{"scanned": int|None, "deduped": int|None}`` for the Discovery DAG-node subtitle.

    ``scanned`` is :func:`get_scanned_total`; when it degrades to None the whole reconciliation is
    the hidden state ``{"scanned": None, "deduped": None}`` (no DB work attempted). Otherwise
    ``discovery_done`` is COUNT(ALL FileRecord rows) via :func:`_safe_count` — note total_files
    counts only extractable music/video while discovery_done counts ALL rows; the LOCKED formula is
    still ``scanned - discovery_done`` (the gap IS the dedup/collision count). ``deduped`` clamps to
    0 when discovery_done ≥ scanned. Both reads degrade independently, so the dict never raises into
    the 5s poll.
    """
    scanned = await get_scanned_total(session)
    if scanned is None:
        return {"scanned": None, "deduped": None}
    # discovery_done counts ALL rows (no file_type filter) so the subtraction is consistent with the
    # Discovery node's COUNT(files); total_files counts only music/video, but scanned - all-rows is
    # the LOCKED dedup formula.
    discovery_done = await _safe_count(session, select(func.count(FileRecord.id)), node="reconcile_discovery")
    return {"scanned": scanned, "deduped": deduped_count(scanned, discovery_done)}


async def get_agent_reconciliations(session: AsyncSession) -> dict[str, dict[str, int]]:
    """Per-agent ``{agent_id: {"scanned", "unique", "deduped"}}``, degrading to ``{}`` on any error.

    For each agent with a latest completed batch: ``scanned`` = that batch's ``total_files`` (re-scan
    -safe via the same ``row_number()`` rank as :func:`get_scanned_total`), ``unique`` = COUNT of the
    agent's FileRecord rows, ``deduped`` = ``max(0, scanned - unique)`` (mirrors :func:`deduped_count`
    — ``scanned`` is never None here so the value is always a plain int). The per-agent file counts
    come from one grouped ``SELECT agent_id, COUNT(id) GROUP BY agent_id`` joined in Python.

    An empty map means "no annotations"; the template hides any agent whose deduped is 0.

    phaze-n2d2: ``ScanBatch.created_at`` carries no uniqueness constraint (``TimestampMixin``'s
    ``server_default=func.now()``), so two completed batches for the same agent can share a value
    and the window's ``rn == 1`` pick is executor-arbitrary on that tie -- the per-agent ``deduped``
    annotation can then differ between renders (and flip hidden/shown, since the template hides an
    agent whose deduped is 0). Appending the unique ``ScanBatch.id`` (DESC, matching the descending
    ``created_at``) makes the window's ``order_by`` total, mirroring the tiebreaker already applied
    to the sibling LIMIT queries (phaze-rgxg / commit dd5f2a2).

    Both reads run inside ONE SAVEPOINT (``session.begin_nested()``) so a failure rolls back the
    NESTED scope ALONE (CR-01): ``build_recent_scans`` (``routers.pipeline_scans``) loads ``ScanBatch``
    ORM rows on this SAME session BEFORE calling here, and ``build_dashboard_context`` similarly loads
    ``agents`` before it -- a plain ``session.rollback()`` would expire those already-loaded rows and
    500 the render on the next lazy load. On any exception this logs a warning and degrades to ``{}``,
    never raising into the dashboard poll.
    """
    try:
        async with session.begin_nested():
            ranked = (
                select(
                    ScanBatch.agent_id.label("agent_id"),
                    ScanBatch.total_files.label("total_files"),
                    func.row_number().over(partition_by=ScanBatch.agent_id, order_by=(ScanBatch.created_at.desc(), ScanBatch.id.desc())).label("rn"),
                )
                .where(ScanBatch.status == ScanStatus.COMPLETED.value)
                .subquery()
            )
            latest_rows = (await session.execute(select(ranked.c.agent_id, ranked.c.total_files).where(ranked.c.rn == 1))).all()

            count_rows = (await session.execute(select(FileRecord.agent_id, func.count(FileRecord.id)).group_by(FileRecord.agent_id))).all()
        counts_by_agent = {agent_id: int(count) for agent_id, count in count_rows}

        out: dict[str, dict[str, int]] = {}
        for agent_id, total_files in latest_rows:
            scanned = int(total_files)
            unique = counts_by_agent.get(agent_id, 0)
            out[agent_id] = {"scanned": scanned, "unique": unique, "deduped": max(0, scanned - unique)}
        return out
    except Exception:
        logger.warning("agent_reconciliations_degraded", exc_info=True)
        return {}


# Watcher-ingested reconciliation (phaze-3sgw0) -- the sidebar Discover count is
# COUNT(all FileRecord), and the completed-batch counters above only ever describe the
# batch a directory WALK produced. Files the agent filewatcher ingests after that walk are
# stamped onto the agent's LIVE sentinel batch (routers/agent_files.py D-18), whose own
# total_files/processed_files counters stay pinned at 0 forever -- nothing in the reconcile-
# deduped arithmetic above accounts for them, so a watcher-fed archive shows a Discover count
# strictly larger than every visible scan total with no explanation. These two helpers close
# that gap by counting the LIVE batch's rows directly ON READ (never a stored counter) -- no
# write-path change, no counter to keep in sync, no counter to go stale. The per-scan read
# attributes each LIVE file to the scan rows it was added under and after (phaze-eeqd0); the
# global read is the plain total across every agent's LIVE batch.


async def get_scan_watcher_counts(session: AsyncSession, batch_ids: Collection[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Exact per-SCAN "added via watcher" count (phaze-eeqd0), one query for every row asked for.

    For each scan batch in ``batch_ids``, counts the FileRecords on that scan's agent's LIVE
    sentinel batch that (a) sit at or under the scan's ``scan_path`` and (b) were ADDED after
    the scan finished. Operator decision 2026-10-05 (AskUserQuestion; durable record: the
    description of bead phaze-eeqd0), question as put, in part: "the same path is often scanned
    more than once, and a subpath scan sits inside its root's scan. Which rows should show a
    path's count?"; answer as given (selected option label): "Since that scan only". So an older
    scan of a path reads larger than a newer scan of the same path, and a root scan counts
    everything its subpath scans count. The rules below are the implementer's, all in the one
    statement:

    * **Per agent.** The files come from the scan's OWN agent's LIVE batch, and the file's
      ``agent_id`` must match too -- the same path on two agents never cross-counts.
    * **Path.** Component containment, never a bare string prefix: equal to ``scan_path``, or
      ``starts_with`` ``scan_path`` plus ``/`` (a trailing slash trimmed first, so ``/`` works),
      so ``/a/b`` never claims ``/a/bc``.
      ``starts_with`` rather than ``LIKE`` so a ``%`` or ``_`` in a real path is literal.
    * **Added means first discovered.** ``FileRecord.created_at`` is stamped once at first
      insert and the upsert never moves it (routers/agent_files.py pins it, bumping only
      ``updated_at``), so a file the watcher merely RE-TOUCHED -- its ``batch_id`` reassigned
      onto LIVE although a walk found it before this scan finished -- is not counted here.
    * **Finished means** ``completed_at``; a terminal (completed/failed) row that never got
      one falls back to ``updated_at``, the same end the elapsed timer freezes at
      (``routers/pipeline_scans.elapsed_seconds``). A RUNNING scan has not finished, so
      nothing has been added "since" it: the cutoff is NULL and the row counts nothing.

    A scan with no matching file is ABSENT from the map (the template hides the hint for it).
    Degrades to ``{}`` on any error inside its own SAVEPOINT (CR-01), the never-500 discipline
    every reconciliation read follows.
    """
    if not batch_ids:
        return {}
    scan = aliased(ScanBatch)
    live = aliased(ScanBatch)
    finished_at = func.coalesce(
        scan.completed_at,
        case((scan.status.in_((ScanStatus.COMPLETED.value, ScanStatus.FAILED.value)), scan.updated_at)),
    )
    scan_dir = func.rtrim(scan.scan_path, "/")
    under_scan_path = or_(FileRecord.original_path == scan_dir, func.starts_with(FileRecord.original_path, scan_dir + "/"))
    stmt = (
        select(scan.id, func.count(FileRecord.id))
        .select_from(scan)
        .join(live, and_(live.agent_id == scan.agent_id, live.status == ScanStatus.LIVE.value))
        .join(FileRecord, and_(FileRecord.batch_id == live.id, FileRecord.agent_id == scan.agent_id))
        .where(scan.id.in_(list(batch_ids)), FileRecord.created_at > finished_at, under_scan_path)
        .group_by(scan.id)
    )
    try:
        async with session.begin_nested():
            rows = (await session.execute(stmt)).all()
        return {batch_id: int(count) for batch_id, count in rows}
    except Exception:
        logger.warning("scan_watcher_counts_degraded", exc_info=True)
        return {}


async def get_global_watcher_count(session: AsyncSession) -> int:
    """Exact global "added via watcher" count: COUNT(FileRecord) across EVERY agent's LIVE
    sentinel batch, computed fresh on every read.

    Unlike :func:`get_scanned_total`'s ``None`` "hide the line" sentinel, 0 is a genuine,
    common reading here (no watcher activity yet) rather than "unavailable" -- so this
    degrades to a plain 0 via :func:`_safe_count`, the same degrade primitive
    :func:`get_global_reconciliation` uses for ``discovery_done``.
    """
    stmt = (
        select(func.count(FileRecord.id))
        .select_from(FileRecord)
        .join(ScanBatch, FileRecord.batch_id == ScanBatch.id)
        .where(ScanBatch.status == ScanStatus.LIVE.value)
    )
    return await _safe_count(session, stmt, node="reconcile_watcher_added")
