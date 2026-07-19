"""Control-side SAQ cron: reap RUNNING scans that have stopped making progress.

CONTROL-ONLY (Phase 26 D-03 / control-vs-agent DB boundary). This task needs
PostgreSQL via ``ctx["async_session"]`` -- the async sessionmaker wired in
``phaze.tasks.controller.startup``. The agent worker is deliberately
Postgres-free (the import-boundary test ``tests/shared/core/test_task_split.py`` enforces
this), so this module MUST NEVER be imported or registered by
``phaze.tasks.agent_worker`` or anything under ``phaze.tasks._shared``. Register
it ONLY in ``phaze.tasks.controller``.

A RUNNING ScanBatch whose progress heartbeat (``last_progress_at``, falling back
to ``updated_at`` then ``created_at``) is older than ``scan_stall_seconds`` is
considered genuinely dead -- e.g. the agent worker that owned it crashed
mid-scan -- and is marked FAILED with a "stalled" error_message and a frozen
``completed_at`` so the admin UI's elapsed timer stops climbing.

The reaper guards on the explicit ``status == ScanStatus.RUNNING.value``
predicate: this is the only state it touches. The LIVE watcher sentinel
(``status='live'``), COMPLETED, and FAILED rows are all excluded. Do NOT broaden
this to a "not terminal" check, which would sweep up the LIVE sentinel.

phaze-5dfj (lost-update fix): the RUNNING+stale predicate is re-asserted INSIDE
the ``UPDATE ... WHERE`` clause itself, not just at a prior SELECT, and the
mutation + predicate are ONE statement. The former shape SELECTed candidate
rows, mutated them as Python ORM attributes, then committed a bare
``UPDATE ... WHERE id = :id`` with no status/heartbeat guard -- a classic
TOCTOU: if the owning agent's ``patch_scan_batch`` committed a terminal
COMPLETED (or just a fresh progress heartbeat) in the window between this
task's SELECT and its COMMIT, the reaper's guard-less write landed LAST and
clobbered the agent's committed row back to FAILED (lost update). A single
guarded ``UPDATE`` re-asserts ``status == RUNNING`` and the heartbeat cutoff at
write time: under PostgreSQL READ COMMITTED, a row a concurrent transaction has
since advanced past either predicate is re-checked via EvalPlanQual when this
statement's row lock is granted and, no longer matching, is left untouched --
0 rows affected for that id, no ``completed_at``/``error_message`` written on a
row this reaper did not actually claim. This mirrors the in-repo CAS precedent
(``KueueBackend._reap_stranded_staging`` / ``hold_awaiting_cloud`` spill mode in
``services/backends.py``): a conditional write whose WHERE clause re-asserts
the precondition observed at read time, rather than a blind attribute-mutate +
commit. ``RETURNING`` reports exactly which rows this statement actually
claimed, so the per-row structured log (``seconds_since_progress``) is only
emitted for rows genuinely reaped -- a batch that raced away to COMPLETED (or a
fresh heartbeat) is silently left alone, not logged as reaped.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, update
import structlog

from phaze.config import get_settings
from phaze.models.scan_batch import ScanBatch, ScanStatus


logger = structlog.get_logger(__name__)


async def reap_stalled_scans(ctx: dict[str, Any]) -> dict[str, int]:
    """Mark RUNNING scans with no progress for ``scan_stall_seconds`` as FAILED.

    Returns ``{"reaped": N}`` where N is the number of batches transitioned to
    FAILED in this run (0 when nothing is stalled).
    """
    cfg = get_settings()
    threshold = cfg.scan_stall_seconds
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=threshold)

    async with ctx["async_session"]() as session:
        # COALESCE picks last_progress_at, then updated_at, then created_at so a
        # legacy row without a heartbeat still compares against a real timestamp.
        heartbeat = func.coalesce(ScanBatch.last_progress_at, ScanBatch.updated_at, ScanBatch.created_at)
        # phaze-5dfj: the RUNNING + stale predicate is re-asserted HERE, in the
        # UPDATE's own WHERE clause -- not just at a prior SELECT -- so a row a
        # concurrent commit has since advanced (status flipped to COMPLETED, or a
        # fresh progress heartbeat) no longer matches and is left untouched
        # instead of being clobbered to FAILED. RETURNING the heartbeat value
        # actually matched lets the per-row log below report exactly what this
        # statement observed, without a second read.
        stmt = (
            update(ScanBatch)
            .where(
                ScanBatch.status == ScanStatus.RUNNING.value,
                heartbeat < cutoff,
            )
            .values(
                status=ScanStatus.FAILED.value,
                error_message=f"stalled: no progress for {threshold}s",
                completed_at=now,
            )
            .returning(ScanBatch.id, ScanBatch.scan_path, heartbeat)
        )
        reaped = (await session.execute(stmt)).all()
        await session.commit()

        for batch_id, scan_path, ref in reaped:
            # Assume-UTC for a tz-naive ref (mirrors elapsed_seconds in
            # routers/pipeline_scans.py) so the subtraction stays aware-to-aware.
            if ref.tzinfo is None:
                ref = ref.replace(tzinfo=UTC)
            seconds_since = int((now - ref).total_seconds())
            logger.warning(
                "scan reaped: stalled",
                batch_id=str(batch_id),
                scan_path=scan_path,
                seconds_since_progress=seconds_since,
            )

    return {"reaped": len(reaped)}
