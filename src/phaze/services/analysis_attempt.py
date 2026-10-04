"""Serialize analysis producers without borrowing a second ledger-pool connection."""

from __future__ import annotations

from contextlib import asynccontextmanager
import sys
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, String, bindparam, func, literal, select
from sqlalchemy.ext.asyncio import AsyncEngine
import structlog

from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.tasks._shared.attempt_context import ledger_enqueue_session


if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)

_LOCK_KEY = func.hashtextextended(
    func.current_database(type_=String).concat(literal(":phaze_analysis_attempt:", String)).concat(bindparam("key", type_=String)),
    literal(0, BigInteger),
)


@asynccontextmanager
async def _on_connection(maker: Any, connection: Any, key: str, *, owned: bool = False) -> AsyncIterator[None]:
    # An explicitly bound connection stays checked out when the borrowed session commits.
    # Thus the session-level lock survives the ledger commit until SAQ commits its insert.
    try:
        await connection.execute(select(func.pg_advisory_lock(_LOCK_KEY)), {"key": key})
        if owned:
            # Lock acquisition autobegins a Connection transaction. End it before binding the
            # Session, otherwise Session.commit joins that outer transaction without committing
            # ledger writes. The session-level advisory lock survives this commit.
            await connection.commit()
        async with maker(bind=connection) as session:
            token = ledger_enqueue_session.set(session)
            try:
                yield
            finally:
                ledger_enqueue_session.reset(token)
                await session.rollback()
    finally:
        # Acquisition can be cancelled after the server acquired the session lock. Never
        # return an uncertain connection to the pool. Asyncpg cancellation normally
        # invalidates it; otherwise unlock explicitly, including acquisition-completion errors.
        pending = sys.exception()
        if not connection.invalidated:
            try:
                await connection.execute(select(func.pg_advisory_unlock(_LOCK_KEY)), {"key": key})
            except BaseException:
                await connection.invalidate()
                if pending is None:
                    raise
                logger.warning("analysis_attempt_unlock_invalidated_connection", key=key, exc_info=True)


@asynccontextmanager
async def serialize_analysis_enqueue(maker: Any, key: str) -> AsyncIterator[None]:
    """Hold one pool connection across the hook and broker write, including exceptions.

    Tests may bind a caller-owned connection with an outer transaction; production binds an
    engine. Never commit that caller-owned transaction. Both paths reuse the same session
    inside the WRITE hook, so even a pool of size one supports concurrent producers.
    """
    bind = maker.kw["bind"]
    if isinstance(bind, AsyncEngine):
        async with bind.connect() as connection:
            async with _on_connection(maker, connection, key, owned=True):
                yield
            await connection.commit()
    else:
        async with _on_connection(maker, bind, key):
            yield


async def analysis_failure_ack_matches(session: AsyncSession, key: str, enqueued_at: datetime) -> bool:
    """Serialize a token-bearing callback with producers before any outcome mutation.

    A missing ledger retains best-effort domain reporting. A present newer obligation makes
    this an ignored stale acknowledgement. Acquisition errors propagate: uncertainty must
    never authorize mutation, and the HTTP client's existing 5xx policy can retry the ACK.
    The transaction lock uses the caller's existing connection and releases at finalize commit.
    """
    await session.execute(select(func.pg_advisory_xact_lock(_LOCK_KEY)), {"key": key})
    epoch = await session.scalar(select(SchedulingLedger.enqueued_at).where(SchedulingLedger.key == key))
    return epoch is None or epoch == enqueued_at
