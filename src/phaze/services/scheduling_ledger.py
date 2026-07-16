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
- :func:`clear_ledger_entry`      -- ``DELETE`` by key (no-op if absent).
- :func:`get_ledger_rows`         -- all rows, for recovery.
- :func:`routing_for_function`    -- ``"agent"`` | ``"controller"`` classifier; raises
  ``ValueError`` on an unknown function (callers only pass the 11 keyed functions).

The caller owns the transaction: every helper executes its statement but does NOT commit,
so the WRITE/CLEAR hooks (which open their own short-lived session) and the backfill (one
batched transaction) each control their own commit boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.enqueue_router import AGENT_TASKS, CONTROLLER_TASKS


if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


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


async def clear_ledger_entry(session: AsyncSession, key: str) -> None:
    """Delete the ledger row for ``key`` (a clean no-op if it is already absent). Caller commits."""
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
