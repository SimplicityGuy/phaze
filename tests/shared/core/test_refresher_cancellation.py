"""A refresher loop must stop when cancelled, even if a library turns the cancel into an exception (phaze-gurn3).

The CI shard ``shared-telemetry`` hung to its 15-minute limit three times on 2026-10-14. Reproduced
on the real harness: ``test_main_lifespan.py::test_saq_disabled_flag_skips_mount`` blocked in
``TestClient.__exit__`` because the api lifespan's shutdown was stuck in ``await review_counts_task``.
The review loop's cancel had landed while SQLAlchemy 2.1.4's pool ``__connect`` was closing a
connection whose connect event had failed. That cleanup catches ``BaseException``, logs it ("Exception
closing connection ... stranded by a failed connect event"), and re-raises the connect event's own
error, so the cancel came out as a ``RuntimeError``. The loop's ``except Exception`` logged it and went
back to sleep, and nothing cancelled it again. The orphan loop, cancelled only after the review loop,
kept polling every 4 s, which is the Postgres-log signature of all three CI hangs.

SQLAlchemy 2.1.5 narrowed that cleanup to ``except Exception``, so the cancel now propagates. The guard
stays: any library that converts or swallows a cancel reproduces the hang.

The first two tests use the REAL SQLAlchemy + asyncpg + Postgres stack: one pins how the library treats
that cancel, the other runs the real review loop through it. The parametrized test covers every loop
on the api shutdown path with a stand-in for the 2.1.4 conversion, so it needs no database.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from phaze import runtime_config_notify
import phaze.main as main_module
from phaze.utils import raise_if_cancel_requested
from tests.conftest import TEST_DATABASE_URL


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncEngine


_STOP_WITHIN_SEC = 5.0


class _StrandedCloseEngine:
    """A real engine to the test database whose connect event fails, with the stranded-connection
    close parked until released, so a test can cancel exactly where the CI hang's cancel landed."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.engine: AsyncEngine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        self.closing = asyncio.Event()
        self.release = asyncio.Event()

        @event.listens_for(self.engine.sync_engine, "connect")
        def _fail_connect_event(_dbapi_connection: object, _record: object) -> None:
            raise RuntimeError("connect event failed")

        original = AsyncAdapt_asyncpg_connection._terminate_graceful_close

        async def _parked_close(adapted: AsyncAdapt_asyncpg_connection) -> None:
            self.closing.set()
            await self.release.wait()
            await original(adapted)

        monkeypatch.setattr(AsyncAdapt_asyncpg_connection, "_terminate_graceful_close", _parked_close)

    async def aclose(self) -> None:
        self.release.set()
        # Let the shielded graceful close (which SQLAlchemy does not cancel) run to completion.
        for _ in range(5):
            await asyncio.sleep(0)
        await self.engine.dispose()


@pytest_asyncio.fixture
async def stranded_close_engine(async_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_StrandedCloseEngine]:
    """``async_engine`` is requested only so this test needs the harness the same way every DB test does."""
    del async_engine
    harness = _StrandedCloseEngine(monkeypatch)
    try:
        yield harness
    finally:
        await harness.aclose()


@pytest.mark.asyncio
async def test_sqlalchemy_propagates_a_cancel_during_the_stranded_close(stranded_close_engine: _StrandedCloseEngine) -> None:
    """Pins the library behaviour since SQLAlchemy 2.1.5. If SQLAlchemy goes back to converting the
    cancel into the connect error, this fails, and the guard in ``phaze.utils`` is load-bearing again."""

    async def _query() -> None:
        async with stranded_close_engine.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    task = asyncio.create_task(_query())
    async with asyncio.timeout(_STOP_WITHIN_SEC):
        await stranded_close_engine.closing.wait()
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=_STOP_WITHIN_SEC)
    assert task in done
    assert task.cancelled(), f"the cancel was replaced by {task.exception()!r}"


@pytest.mark.asyncio
async def test_the_real_review_loop_stops_when_cancelled_during_the_stranded_close(
    stranded_close_engine: _StrandedCloseEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact CI hang, on the real stack: before the fix this task never finished."""
    monkeypatch.setattr("phaze.database.async_session", async_sessionmaker(stranded_close_engine.engine, class_=AsyncSession, expire_on_commit=False))

    task = asyncio.create_task(main_module._review_refresh_loop())
    try:
        async with asyncio.timeout(_STOP_WITHIN_SEC):
            await stranded_close_engine.closing.wait()
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=_STOP_WITHIN_SEC)
        assert task in done, "the review loop outlived its cancellation (the phaze-gurn3 hang)"
        assert task.cancelled()
    finally:
        stranded_close_engine.release.set()
        await _reap(task)


async def _reap(task: asyncio.Task[None]) -> None:
    """Stop a loop that survived its cancel, so a red run reports instead of hanging the event-loop teardown."""
    for _ in range(40):
        if task.done():
            return
        task.cancel()
        await asyncio.wait({task}, timeout=0.5)


def _converting_read(entered: asyncio.Event) -> Callable[..., Awaitable[Any]]:
    """A read that parks until cancelled, then raises an ordinary error in place of the FIRST cancel,
    as SQLAlchemy 2.1.4's stranded-connection cleanup did. Later reads
    return at once, so a loop that survived can still be reaped."""
    converted = False

    async def _read(*_args: object, **_kwargs: object) -> Any:
        nonlocal converted
        if converted:
            return None
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            converted = True
            raise RuntimeError("connect event failed") from None
        raise AssertionError("unreachable")  # pragma: no cover

    return _read


class _NullSession:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_exc: object) -> None:
        return None


async def _start_orphan_loop(monkeypatch: pytest.MonkeyPatch, entered: asyncio.Event) -> asyncio.Task[None]:
    monkeypatch.setattr(main_module, "refresh_stage_orphan_counts", _converting_read(entered))
    return asyncio.create_task(main_module._orphan_refresh_loop())


async def _start_review_loop(monkeypatch: pytest.MonkeyPatch, entered: asyncio.Event) -> asyncio.Task[None]:
    monkeypatch.setattr(main_module, "refresh_review_counts", _converting_read(entered))
    return asyncio.create_task(main_module._review_refresh_loop())


async def _start_attention_loop(monkeypatch: pytest.MonkeyPatch, entered: asyncio.Event) -> asyncio.Task[None]:
    monkeypatch.setattr(main_module, "async_session", _NullSession)
    monkeypatch.setattr(main_module, "_build_summary_context", _converting_read(entered))
    monkeypatch.setattr(main_module, "set_attention_snapshot", MagicMock())
    return asyncio.create_task(main_module._attention_refresh_loop(MagicMock()))


async def _start_runtime_config_poll_loop(monkeypatch: pytest.MonkeyPatch, entered: asyncio.Event) -> asyncio.Task[None]:
    async def _refuse_connect(*_args: object, **_kwargs: object) -> None:
        raise OSError("no LISTEN connection in this test")

    monkeypatch.setattr(runtime_config_notify.asyncpg, "connect", _refuse_connect)
    store = SimpleNamespace(reload=_converting_read(entered))
    handle = await runtime_config_notify.start_runtime_config_listener(store=store, database_url="postgresql://unused", poll_interval_sec=0)  # type: ignore[arg-type]
    return handle.poll_task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "start_loop",
    [_start_orphan_loop, _start_review_loop, _start_attention_loop, _start_runtime_config_poll_loop],
    ids=["orphan", "review", "attention", "runtime-config-poll"],
)
async def test_every_shutdown_path_loop_stops_when_its_cancel_is_converted(
    monkeypatch: pytest.MonkeyPatch, start_loop: Callable[[pytest.MonkeyPatch, asyncio.Event], Awaitable[asyncio.Task[None]]]
) -> None:
    """Each loop the api lifespan cancels and then awaits at shutdown. One that survives hangs shutdown."""
    entered = asyncio.Event()
    task = await start_loop(monkeypatch, entered)
    try:
        async with asyncio.timeout(_STOP_WITHIN_SEC):
            await entered.wait()
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=_STOP_WITHIN_SEC)
        assert task in done, "the loop outlived its cancellation"
        assert task.cancelled()
    finally:
        await _reap(task)


@pytest.mark.asyncio
async def test_raise_if_cancel_requested_is_a_no_op_without_a_pending_cancel() -> None:
    raise_if_cancel_requested()


@pytest.mark.asyncio
async def test_raise_if_cancel_requested_ignores_a_cancel_that_was_already_handled() -> None:
    """``asyncio.timeout`` uncancels its own cancel, so an expired timeout must not stop the loop."""
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0):
            await asyncio.sleep(1)
    raise_if_cancel_requested()
