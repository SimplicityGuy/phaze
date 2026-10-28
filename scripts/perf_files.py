"""Repeat Files list and route measurements against a prepared, isolated synthetic test corpus.

Run as ``uv run python -m scripts.perf_files --redis-url "$PHAZE_REDIS_URL"`` after exporting
TEST_DATABASE_URL for a dedicated ``just provision-test-db-seat`` seat. Never point pytest at the benchmark
seat: its session fixtures drop application tables. This tool never seeds or resets application
rows; SAQ's real queue handles can initialize their own broker tables. No workers or lifespan run.

The richer investigation recorded PostgreSQL plans, per-reader spans and sampled server waits.
This repeatable probe reports raw request/list samples, SQL durations and template time without
claiming small-sample population percentiles. Concurrent SQL durations overlap; do not sum them
as request wall time. Use identical data, backend configuration, source base and warmup policy.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import statistics
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import patch
from urllib.parse import urlsplit

from httpx import ASGITransport, AsyncClient
from psycopg import AsyncConnection
from redis.asyncio import Redis
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.templating import Jinja2Templates

from phaze import database
from phaze.config import get_settings
from phaze.enums.stage import Stage
from phaze.main import create_app
from phaze.routers.pipeline import FILES_SORT
from phaze.services.agent_task_router import AgentTaskRouter
from phaze.services.pagination import DEFAULT_PAGE_SIZE
from phaze.services.pipeline import files as files_service
from phaze.services.pipeline.files import get_files_page
from phaze.tasks._shared.queue_factory import build_pipeline_queue


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from sqlalchemy.ext.asyncio import AsyncSession


_DATABASE_SESSION_LOCK_KEY = 0x7048415A_45544553
CASES: dict[str, dict[str, str | int]] = {
    "default": {},
    "page-two": {"page": 2},
    "page-late": {"page": 50},
    "type-desc": {"sort": "type", "order": "desc"},
    "metadata-failed": {"stage": "metadata", "bucket": "failed"},
    "any-failed": {"bucket": "failed"},
    "analyze-sort": {"sort": "analyze"},
    "media-only": {"scope": "media"},
}


def validate_targets(dsn: str, redis_url: str) -> None:
    """Refuse production, shared test DBs and unallocated/default cache indices before setup."""
    db, cache = urlsplit(dsn), urlsplit(redis_url)
    name = db.path.removeprefix("/")
    if (
        db.hostname not in {"localhost", "127.0.0.1"}
        or db.port != 5433
        or not name.startswith("phaze_")
        or not name.endswith("_test")
        or name == "phaze_test"
        or db.query
        or db.fragment
    ):
        raise ValueError("Use a dedicated local :5433/phaze_<seat>_test database")
    if (
        cache.hostname not in {"localhost", "127.0.0.1"}
        or cache.port != 6380
        or not cache.path[1:].isdigit()
        or int(cache.path[1:]) < 1
        or cache.query
        or cache.fragment
    ):
        raise ValueError("Use the dedicated local :6380/<positive seat index> Redis allocation")


async def profile(dsn: str, redis_url: str, iterations: int) -> dict[str, Any]:
    """Hold the real pytest-compatible exclusive DB lock through setup, reads and cleanup."""
    validate_targets(dsn, redis_url)
    raw_dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
    async with await AsyncConnection.connect(raw_dsn, autocommit=True, connect_timeout=5) as lease:
        cursor = await lease.execute("SELECT pg_try_advisory_lock(%s)", (_DATABASE_SESSION_LOCK_KEY,))
        row = await cursor.fetchone()
        if row is None or not row[0]:
            raise RuntimeError("Database is owned by a pytest session or another measurement")
        # Closing this dedicated connection releases the lock on every failure path too.
        return await _profile_owned(dsn, redis_url, iterations)


@contextmanager
def healthy_files_reads(errors: list[str]) -> Iterator[None]:
    """Reject the service's actual degradation signal, including failures before SQL executes.

    Observing only SQLAlchemy errors misses statement-building exceptions swallowed by the
    presentation service. The probe propagates this specific warning as a failed measurement;
    successful empty/past-end pages emit no warning and remain valid. Restore logging on exit.
    """
    original_warning = files_service.logger.warning

    def warning(event: str, *args: Any, **kwargs: Any) -> Any:
        if event == "files_page_degraded":
            # Retain the failure even if a route-level handler catches the observer exception.
            errors.append(event)
        result = original_warning(event, *args, **kwargs)
        if event == "files_page_degraded":
            raise RuntimeError("Refusing degraded Files profile")
        return result

    with patch.object(files_service.logger, "warning", warning):
        yield
        if "files_page_degraded" in errors:
            raise RuntimeError("Refusing degraded Files profile")


async def _profile_owned(dsn: str, redis_url: str, iterations: int) -> dict[str, Any]:
    cfg = get_settings()
    engine = create_async_engine(
        dsn.replace("postgresql://", "postgresql+asyncpg://", 1),
        pool_size=cfg.db_pool_size,
        max_overflow=cfg.db_max_overflow,
        pool_timeout=cfg.db_pool_timeout,
        pool_pre_ping=cfg.db_pool_pre_ping,
        pool_recycle=cfg.db_pool_recycle,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    controller = build_pipeline_queue("controller", dsn.replace("postgresql+asyncpg://", "postgresql://", 1), cache_redis_url=redis_url)
    router = AgentTaskRouter(dsn.replace("postgresql+asyncpg://", "postgresql://", 1), redis_url, ledger_sessionmaker=factory)
    cache = Redis.from_url(redis_url, decode_responses=True)
    app = create_app()
    app.state.controller_queue, app.state.task_router, app.state.redis = controller, router, cache
    sql_samples: list[float] = []
    renders: list[float] = []
    errors: list[str] = []

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[database.get_session] = session_override

    def selected_engine_only(connection: Any) -> None:
        if connection.engine is not engine.sync_engine:
            errors.append("routing.unexpected_engine")
            raise RuntimeError("Read escaped the selected profiling engine")

    def before(_conn: Any, _cursor: Any, _sql: str, _params: Any, context: Any, _many: bool) -> None:
        context.files_profile_started = time.perf_counter()

    def after(_conn: Any, _cursor: Any, _sql: str, _params: Any, context: Any, _many: bool) -> None:
        sql_samples.append((time.perf_counter() - context.files_profile_started) * 1000)

    def failed(error: Any) -> None:
        errors.append(type(error.original_exception).__name__)

    original_render = Jinja2Templates.TemplateResponse

    def render(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        try:
            return original_render(*args, **kwargs)
        finally:
            renders.append((time.perf_counter() - start) * 1000)

    output: dict[str, Any] = {"warmups": 1, "iterations": iterations, "page_size": DEFAULT_PAGE_SIZE, "cases": {}}
    try:
        await controller.connect()
        with ExitStack() as stack:
            stack.enter_context(healthy_files_reads(errors))
            for target, event_name, listener in (
                (Engine, "engine_connect", selected_engine_only),
                (engine.sync_engine, "before_cursor_execute", before),
                (engine.sync_engine, "after_cursor_execute", after),
                (engine.sync_engine, "handle_error", failed),
            ):
                event.listen(target, event_name, listener)
                stack.callback(event.remove, target, event_name, listener)
            stack.enter_context(patch.object(database, "async_session", factory))
            stack.enter_context(patch.object(Jinja2Templates, "TemplateResponse", render))
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://synthetic") as client:
                for name, params in CASES.items():
                    for shape, headers in (
                        ("direct", None),
                        ("document", {}),
                        ("rail", {"HX-Request": "true", "HX-Target": "stage-workspace"}),
                        ("table", {"HX-Request": "true", "HX-Target": "files-table-view"}),
                    ):
                        samples: list[dict[str, Any]] = []
                        for attempt in range(iterations + 1):
                            sql_samples.clear()
                            renders.clear()
                            start = time.perf_counter()
                            if headers is None:
                                async with factory() as session:
                                    page = await get_files_page(
                                        session,
                                        page=int(params.get("page", 1)),
                                        page_size=DEFAULT_PAGE_SIZE,
                                        stage=Stage(str(params["stage"])) if params.get("stage") else None,
                                        bucket=str(params["bucket"]) if params.get("bucket") else None,
                                        sort=FILES_SORT.resolve(
                                            sort=str(params["sort"]) if params.get("sort") else None,
                                            order=str(params["order"]) if params.get("order") else None,
                                            view_state={},
                                        ),
                                        media_only=params.get("scope") == "media",
                                    )
                                    assert len(page.rows) <= DEFAULT_PAGE_SIZE
                            else:
                                response = await client.get("/s/files", params=params, headers=headers)
                                response.raise_for_status()
                            wall = (time.perf_counter() - start) * 1000
                            if errors:
                                raise RuntimeError(f"Refusing degraded/misrouted profile: {errors}")
                            if attempt:
                                samples.append({"wall_ms": wall, "sql_ms": list(sql_samples), "render_ms": list(renders)})
                        values = [sample["wall_ms"] for sample in samples]
                        output["cases"][f"{shape}/{name}"] = {
                            "n": len(samples),
                            "median_ms": statistics.median(values),
                            "max_ms": max(values),
                            "samples": samples,
                        }
    finally:
        for queue in (controller, *router._queues.values()):
            await queue.disconnect()
            queue_cache = getattr(queue, "cache_redis", None)
            if queue_cache is not None:
                await queue_cache.aclose()
        await cache.aclose()
        await engine.dispose()
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("TEST_DATABASE_URL"), required="TEST_DATABASE_URL" not in os.environ)
    parser.add_argument("--redis-url", required=True)
    parser.add_argument("--iterations", type=int, choices=range(1, 101), default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(asyncio.run(profile(args.dsn, args.redis_url, args.iterations)), indent=2) + "\n")


if __name__ == "__main__":
    main()
