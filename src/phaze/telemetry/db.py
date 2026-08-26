"""Database instrumentation -- statement duration and count, by LEADING KEYWORD only.

The statement text is never a label and never a metric attribute. It is unbounded (every
parameter binding, every ``IN`` list length is a distinct string) and it carries operator
data -- a ``WHERE path = '...'`` on this archive is exactly the local identifier
CONVENTIONS.md forbids from leaving the machine in a form that gets stored forever. The
leading keyword is bounded at eight values and answers the question the metric exists for:
*how much of a request is database time, and is it reads or writes.*

Attached to the SQLAlchemy **sync** engine underneath the async one, because
``before_cursor_execute`` / ``after_cursor_execute`` are core Engine events and
``AsyncEngine`` is a facade over ``sync_engine``. That is also why this is safe on the
event loop: the events fire on the greenlet the async driver already runs the statement in,
so the handler adds no await and no thread hop.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any
import weakref

from sqlalchemy import event

from phaze.telemetry.instruments import add, record


log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

#: The bounded label domain. Anything outside it reports OTHER -- so a statement starting
#: with a keyword nobody anticipated adds observations to one existing series rather than
#: minting a new one.
_OPERATIONS = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "COMMIT", "ROLLBACK"})
_DDL = frozenset({"CREATE", "ALTER", "DROP", "TRUNCATE"})

_START_KEY = "_phaze_telemetry_started"

#: Engines already carrying the hooks. Weak, so a disposed engine is collectable.
_instrumented: weakref.WeakSet[Any] = weakref.WeakSet()


def _operation(statement: str) -> str:
    """First keyword of ``statement``, folded into the catalogued domain.

    Only the first token is ever inspected; the rest of the statement is not read, so
    there is no path by which a bound value or a table name reaches a metric.
    """
    stripped = statement.lstrip()
    if not stripped:
        return "OTHER"
    keyword = stripped.split(None, 1)[0].upper()
    if keyword in _OPERATIONS:
        return keyword
    if keyword in _DDL:
        return "DDL"
    return "OTHER"


def _before(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:  # noqa: ARG001
    conn.info[_START_KEY] = time.perf_counter()


def _after(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:  # noqa: ARG001
    started = conn.info.pop(_START_KEY, None)
    operation = _operation(statement)
    add("phaze.db.statements", 1, db_operation=operation)
    if started is not None:
        record("phaze.db.statement.duration", time.perf_counter() - started, db_operation=operation)


def instrument_engine(engine: AsyncEngine) -> None:
    """Attach the statement hooks to ``engine``. Idempotent per engine.

    Called for every engine phaze builds -- ``database.build_async_engine`` is the single
    seam both the api's process-wide engine and the control worker's per-process task
    engine come through, so one call site covers both.
    """
    try:
        _attach(engine)
    except Exception:
        # NEVER raises into a startup path. This runs inside the api lifespan and the
        # control worker's SAQ startup hook, and a telemetry attachment that could abort
        # either one would be strictly worse than having no database metrics -- which is
        # exactly what this except leaves behind. It also absorbs the case where `engine`
        # is not a real AsyncEngine at all (a test double), where `event.listen` raises
        # InvalidRequestError rather than returning.
        log.warning("telemetry_db_instrumentation_skipped", exc_info=True)


def _attach(engine: AsyncEngine) -> None:
    target = engine.sync_engine
    # Idempotence matters because `event.listen` happily registers the same handler twice
    # and a doubly-registered handler double-counts every statement -- a silent 2x on a
    # metric, which is worse than no metric. A WeakSet rather than an attribute on the
    # Engine: it needs no type the library does not declare, and it does not keep a
    # disposed engine alive.
    if target in _instrumented:
        return
    event.listen(target, "before_cursor_execute", _before)
    event.listen(target, "after_cursor_execute", _after)
    _instrumented.add(target)
