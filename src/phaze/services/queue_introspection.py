"""Operator-facing SAQ ``active`` breakdown -- distinguish RUNNING from CLAIMED-but-buffered (phaze-grx3).

WHY
---
SAQ's ``PostgresQueue._dequeue`` marks rows ``status='active'`` and buffers the deserialized Job in
an in-process ``asyncio.Queue``, drained by only ``concurrency`` worker ``process()`` tasks. Under a
burst (e.g. the phaze-rf04.1 recovery of ~11k jobs) the queue marks FAR more rows ``active`` than the
lane can run -- ~3449 observed against lane concurrency 2 -- and those extra rows sit ``active`` with
``started``/``touched`` frozen at dequeue until the sweep eventually retries them.

So a raw ``SELECT count(*) ... WHERE status='active'`` is a TRAP: "active: 3449" reads as "3449 files
fingerprinting" when the real figure is 2. This helper splits that count using the reliable
discriminator established empirically in spike phaze-qmc2.1:

- ``attempts >= 1`` (the ``attempts`` key present in the JSON blob -- SAQ ``to_dict`` omits it when 0)
  means the worker's ``process()`` loop actually picked the job up. This equals lane concurrency and
  is the TRUE in-flight number.
- ``attempts == 0`` (no ``attempts`` key) means the row was dequeued into the buffer but never
  executed -- a claim, not a run.

``touched`` is deliberately NOT used to judge "running": SAQ's sweeper bumps ``touched`` on every
abort pass, and the fingerprint task has no heartbeat, so ``touched`` distinguishes nothing here.

A claimed-but-unrun row is NOT lost: once it passes ``timeout`` the sweep aborts it, and because
``attempts`` is still 0 it is ``retry()``'d back to ``queued`` (not aborted) WITHOUT burning retry
budget (spike phaze-qmc2.1) -- so a worker restart mid-backlog does not strand these rows
unrecoverably; the sweep re-queues them. ``stuck_past_timeout`` surfaces how many are currently in
that sweep-eligible window so an operator can see the backlog is draining, not wedged.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from sqlalchemy import text
import structlog


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


@dataclasses.dataclass(frozen=True)
class ActiveJobBreakdown:
    """Honest split of a queue's ``status='active'`` rows (phaze-grx3)."""

    queue: str
    total_active: int
    running: int
    claimed_unrun: int
    stuck_past_timeout: int
    degraded: bool = False

    def as_lines(self) -> list[str]:
        """Operator-facing multi-line summary that never lets ``total_active`` masquerade as running."""
        if self.degraded:
            return [f"queue {self.queue!r}: active breakdown unavailable (saq_jobs unreadable -- pre-migration env?)."]
        return [
            f"queue {self.queue!r}: {self.total_active} row(s) in status='active' -- this is NOT the number running.",
            f"  running (attempts>=1, genuinely executing): {self.running}",
            f"  claimed-but-unrun (attempts=0, dequeued into the worker buffer, never executed): {self.claimed_unrun}",
            f"  of those claimed, past their timeout (sweep-eligible; will retry to 'queued', no retry burned): {self.stuck_past_timeout}",
        ]


# ONE read: split the active rows by the attempts-key signal + a started+timeout staleness bound. The
# blob is JSON in a BYTEA column; convert_from(...)::jsonb exposes attempts/started/timeout.
_ACTIVE_BREAKDOWN_SQL = text(
    """
    SELECT
      count(*) AS total_active,
      count(*) FILTER (WHERE (convert_from(job, 'UTF8')::jsonb ? 'attempts'))       AS running,
      count(*) FILTER (WHERE NOT (convert_from(job, 'UTF8')::jsonb ? 'attempts'))   AS claimed_unrun,
      count(*) FILTER (
        WHERE NOT (convert_from(job, 'UTF8')::jsonb ? 'attempts')
          AND (convert_from(job, 'UTF8')::jsonb ? 'started')
          AND (convert_from(job, 'UTF8')::jsonb ? 'timeout')
          AND (EXTRACT(EPOCH FROM NOW()) * 1000 - (convert_from(job, 'UTF8')::jsonb->>'started')::bigint) / 1000.0
              > (convert_from(job, 'UTF8')::jsonb->>'timeout')::bigint
      ) AS stuck_past_timeout
    FROM saq_jobs
    WHERE queue = :queue AND status = 'active'
    """
)


async def summarize_active_jobs(session: AsyncSession, queue_name: str) -> ActiveJobBreakdown:
    """Return the RUNNING vs CLAIMED-but-unrun split of ``queue_name``'s ``active`` rows.

    Degrade-safe: the read runs inside a SAVEPOINT; a missing/unreadable ``saq_jobs`` table (a
    pre-migration env) rolls the nested scope back alone and returns a ``degraded`` breakdown rather
    than raising.
    """
    try:
        async with session.begin_nested():
            row = (await session.execute(_ACTIVE_BREAKDOWN_SQL, {"queue": queue_name})).one()
    except Exception:
        logger.warning("active_breakdown_degraded: saq_jobs read failed (pre-migration env?)", exc_info=True)
        return ActiveJobBreakdown(queue=queue_name, total_active=0, running=0, claimed_unrun=0, stuck_past_timeout=0, degraded=True)

    return ActiveJobBreakdown(
        queue=queue_name,
        total_active=int(row.total_active),
        running=int(row.running),
        claimed_unrun=int(row.claimed_unrun),
        stuck_past_timeout=int(row.stuck_past_timeout),
    )


__all__ = ["ActiveJobBreakdown", "summarize_active_jobs"]
