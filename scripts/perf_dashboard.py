"""Profile full Summary loads and five-second polls on an isolated synthetic corpus.

Run after migrating/seeding a dedicated local test or benchmark database. The app lifespan is
intentionally omitted: it migrates, starts background tasks and can create a development agent.
Real SAQ handles are installed instead (their initial connect provisions SAQ's own tables). No
workers run. This harness therefore permits ONLY the local harness ports/names, never production.
It holds pytest's database-scoped exclusive session lock before any queue or request setup, and
refuses concurrent suites or measurements. It does not honor pytest's shared-database bypass.

Reports contain SQL fingerprints, never SQL text, parameters, URLs, rendered HTML or agent IDs.
Spans overlap: sum of concurrent SQL/read durations is NOT request wall time. Pool acquisition
includes connection establishment; SQLAlchemy pre-ping is outside that span. Warmups are not samples.
Summaries report n, median and maximum only; no tail percentile is inferred from short runs.
Use identical corpus, backend configuration, active tabs and source base for before/after runs.
The idle case is not evidence about active production workload or infrastructure saturation.

Example (after ``just test-db-for dashboard-latency`` and copying its exports)::

    uv run python scripts/perf_dashboard.py --dsn "$TEST_DATABASE_URL" \\
        --redis-url "$PHAZE_REDIS_URL" --iterations 10 --tabs 1 > profile.json
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack, asynccontextmanager, redirect_stdout
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from functools import wraps
import hashlib
import inspect
import json
import statistics
import sys
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import patch
from urllib.parse import urlsplit

from httpx import ASGITransport, AsyncClient
from psycopg import AsyncConnection, AsyncCursor
from psycopg_pool import AsyncConnectionPool
import redis.asyncio as redis_async
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool
from starlette.templating import Jinja2Templates

from phaze import database
from phaze.config import get_settings
from phaze.main import create_app
from phaze.services.agent_task_router import AgentTaskRouter
from phaze.services.pipeline import stages
from phaze.tasks._shared.queue_factory import build_pipeline_queue


if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@dataclass
class Sample:
    path: str
    wall_ms: float = 0
    status: int = 0
    payload_bytes: int = 0
    events: dict[str, list[float]] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)


_CURRENT: ContextVar[Sample | None] = ContextVar("dashboard_profile", default=None)
# Same database-scoped session lock as tests/db_guard.py. A regression test pins compatibility;
# the standalone script does not depend on importing the repository's test package.
_DATABASE_SESSION_LOCK_KEY = 0x7048415A_45544553


@asynccontextmanager
async def exclusive_database(dsn: str) -> AsyncIterator[None]:
    """Hold pytest's actual database-scoped lock through setup, requests and resource cleanup.

    Fail closed on connection errors or contention, regardless of pytest's optional sharing
    bypass. PostgreSQL releases this session lock when the dedicated connection closes.
    """
    raw_dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
    async with await AsyncConnection.connect(
        raw_dsn, autocommit=True, connect_timeout=5, application_name="phaze-dashboard-profile-lock"
    ) as connection:
        cursor = await connection.execute("SELECT pg_try_advisory_lock(%s)", (_DATABASE_SESSION_LOCK_KEY,))
        row = await cursor.fetchone()
        if row is None or not row[0]:
            raise RuntimeError("Database is owned by a pytest session or another measurement; wait or allocate an isolated seat")
        try:
            yield
        finally:
            await connection.execute("SELECT pg_advisory_unlock(%s)", (_DATABASE_SESSION_LOCK_KEY,))


def record(label: str, start: float) -> None:
    if (sample := _CURRENT.get()) is not None:
        sample.events.setdefault(label, []).append((time.perf_counter() - start) * 1000)


def record_error(label: str) -> None:
    if (sample := _CURRENT.get()) is not None:
        sample.errors[label] = sample.errors.get(label, 0) + 1


def fingerprint(statement: str) -> str:
    """A stable opaque query identifier; values and SQL never enter the report."""
    return hashlib.sha256(" ".join(statement.split()).encode()).hexdigest()[:16]


def measured_async(label: str, fn: Any) -> Any:
    @wraps(fn)
    async def measured(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            record_error(f"{label}.{type(exc).__name__}")
            raise
        finally:
            record(label, start)

    return measured


class MeasuredPool(AsyncAdaptedQueuePool):
    """Measure the actual SQLAlchemy queue/connection acquisition, before checkout events."""

    def _do_get(self) -> Any:
        start = time.perf_counter()
        try:
            return super()._do_get()
        finally:
            record("pool.sqlalchemy.acquire", start)


class MeasuredSemaphore:
    """Delegate to the original cap; instrumentation never changes its concurrency bound."""

    def __init__(self, semaphore: asyncio.Semaphore) -> None:
        self.semaphore = semaphore

    async def __aenter__(self) -> None:
        start = time.perf_counter()
        await self.semaphore.acquire()
        record("semaphore.wait", start)

    async def __aexit__(self, *_args: Any) -> None:
        self.semaphore.release()


def validate_targets(dsn: str, redis_url: str) -> None:
    """Queue.connect can provision tables: refuse every target except isolated local harnesses."""
    db = urlsplit(dsn)
    name = db.path.removeprefix("/")
    test_seat = db.port == 5433 and name.startswith("phaze_") and name.endswith("_test") and name != "phaze_test"
    benchmark = db.port == 5545 and name == "phaze_benchmark"
    cache = urlsplit(redis_url)
    if db.hostname not in {"localhost", "127.0.0.1"} or not (test_seat or benchmark):
        raise ValueError("Use an isolated local :5433/phaze_<seat>_test or :5545/phaze_benchmark database")
    if cache.hostname not in {"localhost", "127.0.0.1"} or cache.port != 6380 or not cache.path[1:].isdigit() or int(cache.path[1:]) < 1:
        raise ValueError("Use the dedicated local :6380/<positive seat index> Redis allocation")


def instrument(stack: ExitStack, engine: Any) -> None:
    def selected_engine_only(connection: Any) -> None:
        if connection.engine is not engine.sync_engine:
            record_error("routing.unexpected_engine")
            raise RuntimeError("A profile read attempted an engine outside the selected database")

    def before(_conn: Any, _cursor: Any, statement: str, _params: Any, context: Any, _many: bool) -> None:
        context.profile_start = time.perf_counter()
        context.profile_key = fingerprint(statement)

    def after(_conn: Any, _cursor: Any, _statement: str, _params: Any, context: Any, _many: bool) -> None:
        record(f"sql.sqlalchemy.{context.profile_key}", context.profile_start)

    def failed(error: Any) -> None:
        context = error.execution_context
        if context is not None and hasattr(context, "profile_start"):
            record(f"sql.sqlalchemy.{context.profile_key}", context.profile_start)
            record_error(f"sql.sqlalchemy.{context.profile_key}.{type(error.original_exception).__name__}")

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    event.listen(engine.sync_engine, "after_cursor_execute", after)
    event.listen(engine.sync_engine, "handle_error", failed)
    event.listen(Engine, "engine_connect", selected_engine_only)
    stack.callback(event.remove, engine.sync_engine, "before_cursor_execute", before)
    stack.callback(event.remove, engine.sync_engine, "after_cursor_execute", after)
    stack.callback(event.remove, engine.sync_engine, "handle_error", failed)
    stack.callback(event.remove, Engine, "engine_connect", selected_engine_only)
    original_execute = AsyncCursor.execute

    async def execute(cursor: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        sql = query.as_string(cursor.connection) if hasattr(query, "as_string") else str(query)
        return await measured_async(f"sql.psycopg.{fingerprint(sql)}", original_execute)(cursor, query, *args, **kwargs)

    stack.enter_context(patch.object(AsyncCursor, "execute", execute))
    stack.enter_context(patch.object(AsyncConnectionPool, "getconn", measured_async("pool.psycopg.acquire", AsyncConnectionPool.getconn)))
    stack.enter_context(patch.object(redis_async.Redis, "execute_command", measured_async("redis.command", redis_async.Redis.execute_command)))
    original_render = Jinja2Templates.TemplateResponse

    def render(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        try:
            return original_render(*args, **kwargs)
        finally:
            record("template.render", start)

    stack.enter_context(patch.object(Jinja2Templates, "TemplateResponse", render))
    # Imports hold aliases. Wrap every current router/service binding for these functions, so nested
    # calls and both shell/poll paths are attributed without editing the production implementation.
    targets = {
        "get_stage_progress",
        "_build_dag_context",
        "_read_summary_context",
        "build_pipeline_store_seed",
        "get_backend_lane_snapshot",
        "derive_cloud_hold_reason",
        "get_queue_activity",
        "get_running_analyses",
        "get_analysis_live_count",
        "get_metadata_selection_summary",
        "get_stage_activity_snapshot",
    }
    for module in tuple(sys.modules.values()):
        if module is None or not getattr(module, "__name__", "").startswith("phaze."):
            continue
        for name, fn in tuple(vars(module).items()):
            if name in targets and inspect.iscoroutinefunction(fn):
                stack.enter_context(patch.object(module, name, measured_async(f"read.{name}", fn)))
    # Resolve BEFORE replacement, preserving the existing event-loop global cap of four.
    semaphore = MeasuredSemaphore(stages._stats_fanout())
    stack.enter_context(patch.object(stages, "_STATS_FANOUT", semaphore))


def summarize(samples: list[Sample]) -> dict[str, Any]:
    def distribution(values: list[float]) -> dict[str, float | int]:
        values = sorted(values)
        return {
            "n": len(values),
            "median_ms": statistics.median(values),
            "max_ms": max(values),
        }

    paths = sorted({sample.path for sample in samples})
    return {
        path: {
            "wall": distribution([sample.wall_ms for sample in samples if sample.path == path]),
            "events": {
                label: distribution([value for sample in samples if sample.path == path for value in sample.events.get(label, [])])
                for label in sorted({label for sample in samples if sample.path == path for label in sample.events})
            },
        }
        for path in paths
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    validate_targets(args.dsn, args.redis_url)
    async with exclusive_database(args.dsn):
        return await _run_owned(args)


async def _run_owned(args: argparse.Namespace) -> dict[str, Any]:
    """Run only while holding the selected database's exclusive session lock."""
    cfg = get_settings()
    engine = create_async_engine(
        args.dsn.replace("postgresql://", "postgresql+asyncpg://", 1),
        poolclass=MeasuredPool,
        pool_size=cfg.db_pool_size,
        max_overflow=cfg.db_max_overflow,
        pool_timeout=cfg.db_pool_timeout,
        pool_pre_ping=cfg.db_pool_pre_ping,
        pool_recycle=cfg.db_pool_recycle,
    )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    raw_dsn = args.dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
    controller = build_pipeline_queue("controller", raw_dsn, cache_redis_url=args.redis_url, min_size=2, max_size=8)
    router = AgentTaskRouter(raw_dsn, args.redis_url, ledger_sessionmaker=factory)
    redis = redis_async.Redis.from_url(args.redis_url, decode_responses=True)
    app = create_app()
    app.state.controller_queue, app.state.task_router, app.state.redis = controller, router, redis

    async def session_override() -> Any:
        async with factory() as session:
            yield session

    app.dependency_overrides[database.get_session] = session_override
    samples: list[Sample] = []
    try:
        await controller.connect()
        with ExitStack() as stack:
            stack.enter_context(patch.object(database, "async_session", factory))
            instrument(stack, engine)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://profile") as client:

                async def request(path: str, collect: bool) -> None:
                    sample = Sample(path)
                    token = _CURRENT.set(sample)
                    start = time.perf_counter()
                    try:
                        response = await client.get(path)
                        sample.wall_ms = (time.perf_counter() - start) * 1000
                        sample.status, sample.payload_bytes = response.status_code, len(response.content)
                        response.raise_for_status()
                        # A service may catch/degrade a connection failure. Such a fallback cannot
                        # turn an accidental read of the process-default database into a valid profile.
                        if "routing.unexpected_engine" in sample.errors:
                            raise RuntimeError("Refusing a profile with reads outside the selected engine")
                        if collect:
                            samples.append(sample)
                    finally:
                        _CURRENT.reset(token)

                for path in ("/", "/pipeline/stats"):
                    for _ in range(args.warmups):
                        await request(path, False)
                    for _ in range(args.iterations):
                        await asyncio.gather(*(request(path, True) for _ in range(args.tabs)))
                        if args.interval:
                            await asyncio.sleep(args.interval)
    finally:
        for queue in (controller, *router._queues.values()):
            await queue.disconnect()
            cache = getattr(queue, "cache_redis", None)
            if cache is not None:
                await cache.aclose()
        await redis.aclose()
        await engine.dispose()
    return {
        "schema_version": 1,
        "iterations": args.iterations,
        "tabs": args.tabs,
        "interval_seconds": args.interval,
        "warmups": args.warmups,
        "pool_size": cfg.db_pool_size,
        "max_overflow": cfg.db_max_overflow,
        "fanout_cap": stages._STATS_FANOUT_CAP,
        "summary": summarize(samples),
        "samples": [asdict(sample) for sample in samples],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--redis-url", required=True)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--tabs", type=int, default=1)
    parser.add_argument("--interval", type=float, default=5, help="seconds between request waves; default matches the production poll")
    args = parser.parse_args()
    if min(args.iterations, args.tabs, args.warmups) < 1 or args.interval < 0:
        parser.error("iterations, tabs, warmups must be positive; interval must be nonnegative")
    return args


if __name__ == "__main__":
    # Default structlog output must not interleave with the machine-readable JSON report.
    with redirect_stdout(sys.stderr):
        report = asyncio.run(run(parse_args()))
    print(json.dumps(report, indent=2))  # noqa: T201
