"""Operator-facing SAQ ``active`` breakdown -- distinguish RUNNING from CLAIMED-but-buffered (phaze-grx3).

WHY
---
SAQ's ``PostgresQueue._dequeue`` marks rows ``status='active'`` and buffers the deserialized Job in
an in-process ``asyncio.Queue``, drained by only ``concurrency`` worker ``process()`` tasks. Under a
burst (e.g. the phaze-rf04.1 recovery of ~11k jobs) the queue marks FAR more rows ``active`` than the
lane can run -- ~3449 observed against lane concurrency 2 -- and those extra rows sit ``active`` with
``started``/``touched`` frozen at dequeue until the sweep eventually retries them.

So a raw ``SELECT count(*) ... WHERE status='active'`` is a TRAP: "active: 3449" reads as "3449 files
being processed" when the real figure is 2. This helper splits that count using the reliable
discriminator established empirically in spike phaze-qmc2.1:

- ``attempts >= 1`` (the ``attempts`` key present in the JSON blob -- SAQ ``to_dict`` omits it when 0)
  means the worker's ``process()`` loop actually picked the job up. This equals lane concurrency and
  is the TRUE in-flight number.
- ``attempts == 0`` (no ``attempts`` key) means the row was dequeued into the buffer but never
  executed -- a claim, not a run.

``touched`` is deliberately NOT used to judge "running": SAQ's sweeper bumps ``touched`` on every
abort pass, and the tasks measured here have no heartbeat, so ``touched`` distinguishes nothing.

``stuck_past_timeout`` surfaces how many claimed-but-unrun rows are currently in SAQ's sweep-eligible
window. It was originally documented here as a DRAINING indicator, on the theory that the sweep aborts
each such row and -- ``attempts`` still being 0 -- ``retry()``s it back to ``queued`` without burning
retry budget (spike phaze-qmc2.1), so a worker restart mid-backlog strands nothing unrecoverably.

**That theory did not survive contact with the deployment (phaze-o0n6).** The sweep is real but does
not keep up: 23 ``Sweeping`` passes in a whole worker lifetime, 22 of them ending in ``Could not
abort`` (a per-job ``asyncio.TimeoutError`` wait, processed SERIALLY), while 2,413 rows sat ``active``
against lane concurrency 4 and the count held STATIC across a 60-second sample. 2,411 of the files
those rows keyed had never been analyzed, and while an ``active`` row exists its deterministic key
``process_file:<file_id>`` is un-overwritable (SAQ's ``_enqueue`` upsert only clobbers
``('aborted','complete','failed')``), so those files were un-requeueable by EVERY path including the
recovery CLI. Nothing bounds how far behind the sweep may fall.

So ``stuck_past_timeout`` is a "the sweep OUGHT to be handling these" figure, and the population it
counts needs a real remedy and a real alarm, which is what the two phaze-o0n6 additions are:

- :func:`~phaze.tasks.active_reaper.reap_stranded_active_jobs` -- the control-side cron that DELETEs
  such a row once it is past its OWN timeout plus ``active_reap_slack_seconds``, releasing the key
  (and deliberately keeping the ``scheduling_ledger`` row, which is what recovery replays).
- ``stranded`` / :attr:`ActiveJobBreakdown.exceeds_concurrency` here -- the GUARD. ``stranded`` is
  exactly the set that reaper would delete (parity pinned by test), and a lane can only ever have had
  ``concurrency`` rows genuinely running, so ``stranded > concurrency`` is a condition that CANNOT
  arise from healthy operation. ``phaze queue status`` exits non-zero on it, so the next occurrence is
  detected by a monitor instead of discovered incidentally 2,400 rows later.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from sqlalchemy import text
import structlog

from phaze.config import get_settings
from phaze.services.enqueue_router import LANE_CONCURRENCY_SETTING
from phaze.tasks._saq_reap import SAQ_DEFAULT_TIMEOUT_SECONDS


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


def concurrency_for_queue(queue_name: str) -> int:
    """Return the worker concurrency the lane behind ``queue_name`` runs at (phaze-o0n6 guard bound).

    Lane queues are named ``<base>-<lane>`` (``phaze-agent-nox-analyze``), so the lane is the last
    hyphen-separated segment; the per-lane knob is looked up in
    :data:`~phaze.services.enqueue_router.LANE_CONCURRENCY_SETTING` -- the SAME map
    ``phaze.tasks.agent_worker`` sizes the real worker from -- and clamped by ``worker_max_jobs``,
    exactly as that worker clamps it (the quick-260707-g84 memory ceiling: an explicit lower cap is
    authoritative). An unlaned/all-mode or unrecognised queue name falls back to ``worker_max_jobs``,
    which is what such a worker actually runs at.
    """
    settings = get_settings()
    lane = queue_name.rsplit("-", 1)[-1]
    attr = LANE_CONCURRENCY_SETTING.get(lane)
    if attr is None:
        return int(settings.worker_max_jobs)
    return min(int(getattr(settings, attr)), int(settings.worker_max_jobs))


@dataclasses.dataclass(frozen=True)
class ActiveJobBreakdown:
    """Honest split of a queue's ``status='active'`` rows (phaze-grx3)."""

    queue: str
    total_active: int
    running: int
    claimed_unrun: int
    stuck_past_timeout: int
    # phaze-o0n6 guard. ``stranded`` = the rows reap_stranded_active_jobs would DELETE right now (past
    # their OWN timeout + active_reap_slack_seconds, measured on the FROZEN ``started``); parity with
    # the reaper's DELETE set is pinned by test. ``concurrency`` is what the lane behind this queue
    # actually runs at. Unlike ``stuck_past_timeout`` this counts BOTH sub-populations -- a stranded
    # row with ``attempts >= 1`` was picked up and then abandoned mid-flight by a dying process, which
    # blocks its key just as hard as a never-run claim.
    stranded: int = 0
    concurrency: int = 0
    degraded: bool = False

    @property
    def exceeds_concurrency(self) -> bool:
        """True when more rows are stranded ``active`` than the lane could EVER have been running.

        The guard condition (phaze-o0n6). A lane runs at most ``concurrency`` jobs at once, so at most
        ``concurrency`` rows can legitimately be ``active`` past their own timeout + slack at any
        instant. Anything above that is abandoned claims holding deterministic keys hostage -- files
        that are silently un-requeueable by every path. Never True on a ``degraded`` read (an
        unreadable ``saq_jobs`` is a missing measurement, not a healthy queue) and never True when
        ``concurrency`` could not be resolved to a positive number, so the guard cannot alarm on the
        absence of information.
        """
        if self.degraded or self.concurrency <= 0:
            return False
        return self.stranded > self.concurrency

    def as_lines(self) -> list[str]:
        """Operator-facing multi-line summary that never lets ``total_active`` masquerade as running."""
        if self.degraded:
            return [f"queue {self.queue!r}: active breakdown unavailable (saq_jobs unreadable -- pre-migration env?)."]
        lines = [
            f"queue {self.queue!r}: {self.total_active} row(s) in status='active' -- this is NOT the number running.",
            f"  running (attempts>=1, genuinely executing): {self.running}",
            f"  claimed-but-unrun (attempts=0, dequeued into the worker buffer, never executed): {self.claimed_unrun}",
            f"  of those claimed, past their timeout (sweep-eligible, but the sweep does not keep up -- phaze-o0n6): {self.stuck_past_timeout}",
            f"  STRANDED past their own timeout + reap slack (reap_stranded_active_jobs will delete these): {self.stranded}",
            f"  lane concurrency (the most that can legitimately be running): {self.concurrency}",
        ]
        if self.exceeds_concurrency:
            lines.append(
                f"  ALARM: {self.stranded} stranded > concurrency {self.concurrency}. These rows hold their deterministic "
                "keys, so their files cannot be re-enqueued by ANY path until reaped (phaze-o0n6)."
            )
        return lines


# ONE read: split the active rows by the attempts-key signal + a started+timeout staleness bound. The
# blob is JSON in a BYTEA column; convert_from(...)::jsonb exposes attempts/started/timeout.
#
# The ``stranded`` FILTER intentionally mirrors ``_saq_reap.REAP_STRANDED_SQL``'s WHERE predicate
# rather than importing it: SQLAlchemy ``text()`` does not compose, and building this aggregate by
# string-splicing a shared fragment would trade a readable statement for an unreadable one. The two
# are kept honest BEHAVIOURALLY instead -- ``test_active_reaper.py`` asserts the count this reports
# equals the number of rows the reaper actually deletes on the same fixture, which is the property
# that matters and which textual sharing would only have implied.
#
# Both the ``stuck_past_timeout`` and ``stranded`` FILTERs exclude rows whose serialized
# ``timeout`` is explicitly ``0`` (phaze-mllxc). In SAQ, ``timeout=0`` means UNBOUNDED --
# ``Job.stuck`` is ``(self.timeout and ...)``, so 0 is falsy and the job is never sweep-eligible --
# and ``scan_directory`` enqueues with ``timeout=0`` deliberately (a full SHA-256 walk of a large
# network-mounted archive legitimately takes 1-2h). ``Job.to_dict`` serializes ANY field that
# differs from its dataclass default, so ``timeout: 0`` is ALWAYS present in the blob; a bare
# ``? 'timeout'``/``COALESCE(...,  :default)`` treats a present 0 as a real (zero-second) bound
# instead of the "no bound at all" sentinel SAQ intends, flagging a healthy 1-2h scan as
# stuck/stranded within ``active_reap_slack_seconds`` of starting. The guard is an exclusion
# (``<> 0``), never a ``NULLIF(...,0)`` substitution -- that would silently convert 0 to the 10s
# default and still trip at 10s + slack.
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
          AND (convert_from(job, 'UTF8')::jsonb->>'timeout')::bigint <> 0
          AND (EXTRACT(EPOCH FROM NOW()) * 1000 - (convert_from(job, 'UTF8')::jsonb->>'started')::bigint) / 1000.0
              > (convert_from(job, 'UTF8')::jsonb->>'timeout')::bigint
      ) AS stuck_past_timeout,
      count(*) FILTER (
        WHERE (convert_from(job, 'UTF8')::jsonb ? 'started')
          AND COALESCE((convert_from(job, 'UTF8')::jsonb->>'timeout')::bigint, :default_timeout_seconds) <> 0
          AND (EXTRACT(EPOCH FROM NOW()) * 1000 - (convert_from(job, 'UTF8')::jsonb->>'started')::bigint) / 1000.0
              > COALESCE((convert_from(job, 'UTF8')::jsonb->>'timeout')::bigint, :default_timeout_seconds) + :slack_seconds
      ) AS stranded
    FROM saq_jobs
    WHERE queue = :queue AND status = 'active'
    """
)


async def summarize_active_jobs(session: AsyncSession, queue_name: str, *, concurrency: int | None = None) -> ActiveJobBreakdown:
    """Return the RUNNING vs CLAIMED-but-unrun vs STRANDED split of ``queue_name``'s ``active`` rows.

    ``concurrency`` overrides the lane concurrency the phaze-o0n6 guard compares ``stranded`` against;
    it defaults to :func:`concurrency_for_queue` (derived from the queue's lane suffix), which is right
    whenever the queue is read from the deployment that runs it and wrong when it is not -- hence the
    override.

    Degrade-safe: the read runs inside a SAVEPOINT; a missing/unreadable ``saq_jobs`` table (a
    pre-migration env) rolls the nested scope back alone and returns a ``degraded`` breakdown rather
    than raising. A degraded breakdown never trips the guard.
    """
    settings = get_settings()
    bound = concurrency if concurrency is not None else concurrency_for_queue(queue_name)
    try:
        async with session.begin_nested():
            row = (
                await session.execute(
                    _ACTIVE_BREAKDOWN_SQL,
                    {
                        "queue": queue_name,
                        "slack_seconds": settings.active_reap_slack_seconds,
                        "default_timeout_seconds": SAQ_DEFAULT_TIMEOUT_SECONDS,
                    },
                )
            ).one()
    except Exception:
        logger.warning("active_breakdown_degraded: saq_jobs read failed (pre-migration env?)", exc_info=True)
        return ActiveJobBreakdown(
            queue=queue_name, total_active=0, running=0, claimed_unrun=0, stuck_past_timeout=0, stranded=0, concurrency=bound, degraded=True
        )

    breakdown = ActiveJobBreakdown(
        queue=queue_name,
        total_active=int(row.total_active),
        running=int(row.running),
        claimed_unrun=int(row.claimed_unrun),
        stuck_past_timeout=int(row.stuck_past_timeout),
        stranded=int(row.stranded),
        concurrency=bound,
    )
    if breakdown.exceeds_concurrency:
        # Loud even when nobody is reading the CLI's exit code: this is the condition that went
        # unnoticed until it had reached 2,413 rows / 2,411 un-analyzable files.
        logger.error(
            "stranded 'active' rows exceed lane concurrency -- deterministic keys are held and their files are un-requeueable (phaze-o0n6)",
            queue=queue_name,
            stranded=breakdown.stranded,
            concurrency=breakdown.concurrency,
            total_active=breakdown.total_active,
        )
    return breakdown


__all__ = ["ActiveJobBreakdown", "concurrency_for_queue", "summarize_active_jobs"]
