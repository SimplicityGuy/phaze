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
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
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
        # Only RUNNING rows whose freshest progress marker predates the cutoff.
        # COALESCE picks last_progress_at, then updated_at, then created_at so a
        # legacy row without a heartbeat still compares against a real timestamp.
        stmt = select(ScanBatch).where(
            ScanBatch.status == ScanStatus.RUNNING.value,
            func.coalesce(ScanBatch.last_progress_at, ScanBatch.updated_at, ScanBatch.created_at) < cutoff,
        )
        rows = list((await session.execute(stmt)).scalars().all())

        for batch in rows:
            ref = batch.last_progress_at or batch.updated_at or batch.created_at
            # Assume-UTC for a tz-naive ref (mirrors elapsed_seconds in
            # routers/pipeline_scans.py) so the subtraction stays aware-to-aware.
            if ref.tzinfo is None:
                ref = ref.replace(tzinfo=UTC)
            seconds_since = int((now - ref).total_seconds())
            batch.status = ScanStatus.FAILED.value
            batch.error_message = f"stalled: no progress for {threshold}s"
            batch.completed_at = now
            logger.warning(
                "scan reaped: stalled",
                batch_id=str(batch.id),
                scan_path=batch.scan_path,
                seconds_since_progress=seconds_since,
            )

        await session.commit()

    return {"reaped": len(rows)}
