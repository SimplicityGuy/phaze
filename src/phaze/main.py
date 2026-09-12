"""FastAPI application factory with lifespan management."""

import asyncio
from collections.abc import AsyncGenerator
import contextlib
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
import redis.asyncio as redis_async
from sqlalchemy import select, text
import structlog

from phaze.config import settings
from phaze.database import async_session, engine, run_migrations
from phaze.logging_config import configure_logging
from phaze.models.agent import Agent
from phaze.routers import (
    admin_agents,
    agent_analysis,
    agent_exec_batches,
    agent_execution,
    agent_files,
    agent_heartbeat,
    agent_identity,
    agent_metadata,
    agent_proposals,
    agent_push,
    agent_s3,
    agent_scan_batches,
    agent_scratch,
    agent_tag_writes,
    companion,
    cue,
    duplicates,
    execution,
    health,
    pipeline,
    pipeline_scans,
    pipeline_stages,
    preview,
    proposals,
    record,
    routing,
    scan,
    search,
    shell,
    tags,
    tracklists,
)
from phaze.services.agent_bootstrap import ensure_dev_agent
from phaze.services.agent_task_router import AgentTaskRouter
from phaze.services.pipeline import _ORPHAN_TTL_SECONDS, refresh_stage_orphan_counts
from phaze.tasks._shared.queue_factory import build_pipeline_queue
from phaze.telemetry import configure_telemetry, shutdown_telemetry
from phaze.telemetry.db import instrument_engine
from phaze.telemetry.http import TelemetryMiddleware
from phaze.web.saq_mount import build_saq_app
from phaze.web.static import STATIC_DIR, STATIC_VERSION, RevalidatingStaticFiles


logger = structlog.get_logger(__name__)


async def _orphan_refresh_loop() -> None:
    """Refresh orphan counts off-request, retaining the last good value after failures."""
    while True:
        try:
            await refresh_stage_orphan_counts()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("orphan refresh loop iteration failed; keeping last-good", exc_info=True)
        await asyncio.sleep(_ORPHAN_TTL_SECONDS)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Construct application resources in dependency order and close them in reverse."""
    # Configure logging before migrations so startup failures use the normal pipeline.
    configure_logging(level=settings.log_level, json_logs=settings.log_json)

    # Telemetry is opt-in and failure-isolated; configure it before migrations for startup traces.
    configure_telemetry("api")
    instrument_engine(engine)

    # Migrate before the first normal database use.
    await run_migrations()

    # Refuse startup on an unreachable database instead of deferring failure to requests.
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))

    async with async_session() as bootstrap_session:
        await ensure_dev_agent(bootstrap_session)

    # The named controller queue has a real worker. Factory hooks apply project defaults,
    # deterministic keys, and durable ledger writes to both manual and recovery paths.
    _app.state.controller_queue = build_pipeline_queue(
        "controller",
        settings.queue_url,
        cache_redis_url=settings.redis_url,
        min_size=2,
        max_size=8,
        ledger_sessionmaker=async_session,
    )
    # PostgresQueue is built with ``open=False`` and never connects implicitly.
    await _app.state.controller_queue.connect()
    # Per-agent queues share broker/cache configuration and the same ledger hook.
    _app.state.task_router = AgentTaskRouter(queue_url=settings.queue_url, cache_redis_url=settings.redis_url, ledger_sessionmaker=async_session)
    # Tracklist cache consumers expect decoded strings.
    _app.state.redis = redis_async.Redis.from_url(settings.redis_url, decode_responses=True)

    # Build the dashboard once, after queues and the agent roster exist. It reuses cached queues,
    # owns no resources, and includes only fileserver agents because compute work bypasses SAQ.
    if settings.enable_saq_ui:
        async with async_session() as session:
            agents_stmt = select(Agent).where(Agent.revoked_at.is_(None), Agent.kind == "fileserver").order_by(Agent.name)
            agents = (await session.execute(agents_stmt)).scalars().all()
        # Include every named lane plus the legacy drain queue.
        agent_queues = [
            q for agent in agents for q in (*_app.state.task_router.all_lane_queues(agent.id), _app.state.task_router.legacy_base_queue(agent.id))
        ]
        # Dashboard ``info()`` needs each lazily constructed Postgres pool open.
        for q in agent_queues:
            await q.connect()
        _app.mount("/saq", build_saq_app([_app.state.controller_queue, *agent_queues]))

    # Materialize orphan counts off the 5 s stats request path; the cache keeps badge reads O(1).
    _app.state.orphan_task = asyncio.create_task(_orphan_refresh_loop())

    yield
    # Shutdown in reverse construction order.
    # Stop the refresher before disposing its engine.
    _app.state.orphan_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _app.state.orphan_task
    await _app.state.task_router.close()
    await _app.state.redis.aclose()
    # WR-01: close the factory-attached cache_redis before the pool — disconnect()
    # closes only the psycopg3 pool, leaving the controller queue's Redis client open.
    controller_cache_redis = getattr(_app.state.controller_queue, "cache_redis", None)
    if controller_cache_redis is not None:
        await controller_cache_redis.aclose()
    await _app.state.controller_queue.disconnect()
    await engine.dispose()
    # LAST, after every resource that could still emit. Bounded by
    # PHAZE_TELEMETRY_FLUSH_TIMEOUT_MS (default 3,000 ms) and never raises, so a collector
    # that is down cannot hold a container restart open.
    shutdown_telemetry()


# ORDER IS LOAD-BEARING: FastAPI/Starlette match routes in registration order, and
# `shell.router` deliberately claims the root path ahead of `pipeline.router`'s legacy
# redirect -- this tuple preserves the EXACT original registration order.
_ROUTERS: tuple[APIRouter, ...] = (
    health.router,
    companion.router,
    proposals.router,
    execution.router,
    preview.router,
    duplicates.router,
    tracklists.router,
    pipeline.router,
    # SHELL-01: the v7.0 shell router owns GET / (Analyze default) + GET
    # /s/{stage}. Prefix-less (like pipeline.router) so it can claim the root path; the
    # legacy /pipeline/ now 302-redirects here. NO extra prefix= (required by
    # tests/_route_introspection.iter_effective_routes).
    shell.router,
    # 61-02, RECORD-01 / D-01: the per-file full-record read-only fragment
    # route (GET /record/{file_id}). Typed uuid.UUID path param + strictly file_id-scoped
    # reads (T-61-03); a missing file renders the friendly 404 fragment (T-61-05).
    record.router,
    # per-stage control-plane endpoints (POST /pipeline/stages/{stage}/
    # {priority,pause,resume}). Distinct from `pipeline.router` (dashboard + triggers);
    # mutates the pipeline_stage_control intent row + the live saq_jobs backlog together.
    pipeline_stages.router,
    # 71-04, BEUI-02: the force-local master routing override thin write endpoint
    # (POST /pipeline/routing/force-local). Mirrors the pipeline_stages thin-endpoint pattern;
    # flips the durable route_control 'global' row + returns the header pill (swapped in place).
    routing.router,
    search.router,
    tags.router,
    cue.router,
    agent_files.router,
    agent_metadata.router,
    agent_execution.router,
    agent_heartbeat.router,
    agent_identity.router,
    agent_analysis.router,
    agent_push.router,
    agent_s3.router,
    agent_proposals.router,
    agent_scan_batches.router,
    # phaze-5cvbz: compute-scratch janitor liveness probe -- the agent-side startup sweep asks
    # here before deleting an age-eligible scratch entry a durable queued/active job still claims.
    agent_scratch.router,
    # Single mutation point for the ``exec:{batch_id}`` progress hash.
    agent_exec_batches.router,
    # phaze-6bkk internal-agent router (DIST-01): terminal outcome of an on-agent tag write. The
    # api container has no media mount, so the mutagen write runs on the owning agent's meta lane
    # and its result reaches the tag_write_log audit table only through this callback.
    agent_tag_writes.router,
    # HTMX poll partial, Recent
    # Scans table and the agent-roots swap. Distinct from `pipeline.router`,
    # which serves the dashboard page and existing pipeline-stage triggers.
    pipeline_scans.router,
    # phaze-bk9el.17 split the POST /pipeline/scans trigger out of `pipeline_scans`
    # into its own module; both carry the same `/pipeline/scans` prefix and neither
    # shadows the other (distinct method+path pairs), so registration order is free.
    scan.router,
    # GET /admin/agents (operator-facing
    # liveness page) + GET /admin/agents/_table (HTMX 5s poll partial). The
    # router is read-only and does NOT use get_authenticated_agent (consistent
    # with other admin-UI routers on the private LAN).
    admin_agents.router,
)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="Phaze", version="0.1.0", lifespan=lifespan)
    # Install before routers so all requests use bounded-cardinality route labels.
    app.add_middleware(TelemetryMiddleware)
    for router in _ROUTERS:
        app.include_router(router)
    # Matching content fingerprints may cache forever; stale or absent versions revalidate.
    app.mount("/static", RevalidatingStaticFiles(directory=STATIC_DIR, version=STATIC_VERSION), name="static")
    return app


app = create_app()
