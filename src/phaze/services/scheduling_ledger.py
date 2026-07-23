"""Control-only scheduling-ledger service (Phase 45 Plan 01, Task 2).

CONTROL-ONLY BOUNDARY: this module imports ``phaze.models`` + SQLAlchemy, so it MUST
NEVER be imported by ``phaze.tasks._shared`` or ``phaze.tasks.agent_worker`` (the agent
worker is deliberately Postgres-free -- import-boundary test ``tests/shared/core/test_task_split.py``).
The ``before_enqueue`` WRITE hook reaches these helpers via a function-LOCAL lazy import
gated on ``getattr(job.queue, "ledger_sessionmaker", None)`` so the import only ever runs
control-side.

Five helpers:

- :func:`upsert_ledger_entry`     -- ``INSERT ... ON CONFLICT (key) DO UPDATE`` (idempotent;
  used by the WRITE hook). A repeat enqueue of a still-scheduled key refreshes
  ``payload`` / ``enqueued_at``.
- :func:`insert_ledger_if_absent` -- ``INSERT ... ON CONFLICT (key) DO NOTHING`` (the Plan-04
  backfill primitive; never overwrites a fresher hook-written row). Owned here so Plan 04
  adds no new contract and edits no Plan-01 test.
- :func:`clear_ledger_entry`      -- ``DELETE`` by key, GUARDED against a same-key re-enqueue
  race (phaze-3yln; see the function docstring) -- no-op if the row is absent OR currently owned
  by a live re-enqueue.
- :func:`get_ledger_rows`         -- all rows, for recovery.
- :func:`routing_for_function`    -- ``"agent"`` | ``"controller"`` classifier; raises
  ``ValueError`` on an unknown function (callers only pass the 11 keyed functions).

The caller owns the transaction: every helper executes its statement but does NOT commit,
so the WRITE/CLEAR hooks (which open their own short-lived session) and the backfill (one
batched transaction) each control their own commit boundary.

INVARIANT (phaze-3yln): ``clear_ledger_entry`` must NEVER delete a ledger row that a live
(``queued``/``active``) ``saq_jobs`` row for the SAME key currently depends on. Ledger rows are
keyed by the deterministic key ALONE, and SAQ re-queues a terminal key via
``ON CONFLICT (key) DO UPDATE`` -- so a same-key re-enqueue landing between a finishing job's
terminal write and its OWN ``after_process`` clear (or between an agent stage's terminal result
and its control-side callback clear) must win the race, not lose its row. See
:func:`clear_ledger_entry` for the guard's exact shape. This invariant holds for EVERY clear-by-key
call site in this codebase (the ``after_process`` hook in
:mod:`phaze.tasks._shared.deterministic_key` AND every control-side agent-stage callback clear
under ``phaze.routers.agent_*`` / ``phaze.services.backends``) because the guard lives inside this
one shared primitive -- no call site needs its own ownership check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
import structlog

from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.enqueue_router import AGENT_TASKS, CONTROLLER_TASKS


if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


def routing_for_function(function: str) -> str:
    """Classify a keyed task name as ``"agent"`` or ``"controller"``.

    Source of truth is :data:`enqueue_router.AGENT_TASKS` /
    :data:`enqueue_router.CONTROLLER_TASKS` (the same frozensets the live router uses), so
    the ledger's routing hint can never drift from the real dispatch destination. Callers
    only ever pass the 11 keyed functions; an unknown name is a programming error and raises
    ``ValueError`` (fail loud, never silently mis-route a replay).
    """
    if function in AGENT_TASKS:
        return "agent"
    if function in CONTROLLER_TASKS:
        return "controller"
    raise ValueError(f"{function!r} is not a routable task (absent from AGENT_TASKS and CONTROLLER_TASKS)")


async def upsert_ledger_entry(
    session: AsyncSession,
    *,
    key: str,
    function: str,
    kwargs: dict[str, Any],
    timeout: int | None = None,
    retries: int | None = None,
) -> None:
    """Upsert one ledger row (idempotent ON CONFLICT DO UPDATE) -- the WRITE hook primitive.

    A re-enqueue of a still-scheduled key refreshes ``payload`` / ``enqueued_at`` /
    ``function`` / ``routing`` / ``timeout`` / ``retries`` instead of erroring on the duplicate
    PK. ``timeout`` / ``retries`` are the SAQ Job policy captured at enqueue time so recovery can
    replay the SAME bound (None => producer set no explicit value; replay omits it). The caller
    commits.
    """
    routing = routing_for_function(function)
    values = {"key": key, "function": function, "routing": routing, "payload": kwargs, "timeout": timeout, "retries": retries}
    stmt = pg_insert(SchedulingLedger).values([values])
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"],
        set_={
            "function": stmt.excluded.function,
            "routing": stmt.excluded.routing,
            "payload": stmt.excluded.payload,
            "timeout": stmt.excluded.timeout,
            "retries": stmt.excluded.retries,
            "enqueued_at": func.now(),
        },
    )
    await session.execute(stmt)


async def insert_ledger_if_absent(
    session: AsyncSession,
    *,
    key: str,
    function: str,
    kwargs: dict[str, Any],
    timeout: int | None = None,
    retries: int | None = None,
) -> None:
    """Insert one ledger row ONLY if the key is absent (ON CONFLICT DO NOTHING).

    The Plan-04 backfill primitive: it seeds a row for a live broker key WITHOUT clobbering
    an existing (possibly fresher hook-written) row. Differs from :func:`upsert_ledger_entry`
    only in the conflict clause; the ``values()`` build is identical (including the captured
    ``timeout`` / ``retries`` policy). The caller commits.
    """
    routing = routing_for_function(function)
    values = {"key": key, "function": function, "routing": routing, "payload": kwargs, "timeout": timeout, "retries": retries}
    stmt = pg_insert(SchedulingLedger).values([values]).on_conflict_do_nothing(index_elements=["key"])
    await session.execute(stmt)


# Guarded CLEAR (phaze-3yln). A bare ``DELETE ... WHERE key = :key`` cannot tell "my own row" apart
# from a FRESHER row a same-key re-enqueue upserted after I (the finishing job) went terminal but
# before my after_process/callback clear ran -- SAQ's ``_enqueue`` re-queues a terminal key via
# ``ON CONFLICT (key) DO UPDATE``, so that interleaving is reachable, not hypothetical (see the
# module docstring's INVARIANT paragraph). The ``NOT EXISTS`` makes "is this key still live?" and
# "delete it" ONE atomic statement -- no separate check-then-act gap for a concurrent re-enqueue's
# saq_jobs write to land in -- so a key with a live (queued/active) saq_jobs row survives; only a
# key with NO live row (the normal terminal-and-done case, OR a re-enqueue whose own saq_jobs write
# has not landed yet -- see the docstring's residual-window note) is cleared. Mirrors the recovery
# liveness definition 1:1 (``get_live_job_keys`` / ``_LIVE_KEYS_SQL`` in ``services/pipeline.py``):
# queued/active are the only LIVE statuses.
_GUARDED_CLEAR_SQL = text(
    """
    DELETE FROM scheduling_ledger
    WHERE key = :key
      AND NOT EXISTS (
          SELECT 1 FROM saq_jobs WHERE saq_jobs.key = :key AND saq_jobs.status IN ('queued', 'active')
      )
    """
)


async def clear_ledger_entry(session: AsyncSession, key: str) -> None:
    """Delete the ledger row for ``key`` -- UNLESS a live saq_jobs row for the SAME key currently
    exists (phaze-3yln ownership guard). A clean no-op if the row is already absent, or if it is
    currently owned by a live re-enqueue that raced this clear. Caller commits.

    Residual window (documented, accepted): the WRITE hook's ledger upsert and SAQ's own
    ``saq_jobs`` insert are TWO SEPARATE transactions (``apply_deterministic_key`` commits the
    ledger row from its own session; ``Queue._enqueue`` commits the broker row afterward on a
    different connection). If this clear's liveness probe lands in the narrow gap between those two
    commits -- the re-enqueue's ledger row is already fresh but its saq_jobs row is not yet
    ``queued`` -- the probe sees "not live" and the fresh row is still cleared. This sub-race is
    strictly narrower than the race this guard closes (a network round trip vs. the finishing job's
    entire terminal-write-to-clear window) and matches the fix hint's own sanctioned alternative
    ("skip the clear when a non-terminal saq_jobs row exists for the key"); closing it fully would
    need the ledger upsert and the saq_jobs insert to share one transaction, which SAQ's queue API
    does not expose.

    Degrade-safe (mirrors ``aborting_reaper._REAP_ABORTING_SQL`` / ``get_live_job_keys``): the
    guarded statement runs inside a SAVEPOINT. On ANY error probing ``saq_jobs`` (a missing table
    in a pre-migration/test env is the only expected case) the nested scope rolls back ALONE and
    this falls back to the pre-phaze-3yln unconditional delete-by-key, so an environment without a
    live ``saq_jobs`` table keeps exactly today's behavior (a row is never left permanently
    un-clearable because the liveness probe itself is unavailable).
    """
    try:
        async with session.begin_nested():
            await session.execute(_GUARDED_CLEAR_SQL, {"key": key})
    except Exception:
        logger.warning("scheduling_ledger_clear_liveness_probe_degraded", key=key, exc_info=True)
        await session.execute(delete(SchedulingLedger).where(SchedulingLedger.key == key))


async def get_ledger_rows(session: AsyncSession) -> Sequence[SchedulingLedger]:
    """Return every ledger row (for recovery's ``ledger - live keys`` set difference)."""
    return (await session.execute(select(SchedulingLedger))).scalars().all()


__all__ = [
    "clear_ledger_entry",
    "get_ledger_rows",
    "insert_ledger_if_absent",
    "routing_for_function",
    "upsert_ledger_entry",
]
