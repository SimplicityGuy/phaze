"""FastAPI application factory with lifespan management."""

import asyncio
from collections.abc import AsyncGenerator
import contextlib
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, FastAPI
import redis.asyncio as redis_async
from sqlalchemy import select, text
import structlog

from phaze.config import get_settings
from phaze.database import async_session, engine, run_migrations
from phaze.logging_config import configure_logging, log_level_applier
from phaze.models.agent import Agent
from phaze.routers import (
    admin_agents,
    admin_runtime_config,
    agent_analysis,
    agent_companion_capture,
    agent_companion_features,
    agent_config,
    agent_exec_batches,
    agent_execution,
    agent_files,
    agent_heartbeat,
    agent_identity,
    agent_junk_quarantine,
    agent_metadata,
    agent_orphan_companions,
    agent_proposals,
    agent_push,
    agent_s3,
    agent_scan_batches,
    agent_scratch,
    agent_tag_writes,
    companion,
    cue,
    deployments,
    duplicates,
    execution,
    health,
    junk_review,
    local_sources,
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
from phaze.runtime_config import get_runtime_config_store
from phaze.runtime_config_backends import build_backends_registry_reloader
from phaze.runtime_config_notify import install_runtime_config_overrides, start_runtime_config_listener
from phaze.runtime_config_triggers import build_backends_watcher, build_watcher, install_sighup_handler
from phaze.services.agent_bootstrap import ensure_dev_agent
from phaze.services.agent_task_router import AgentTaskRouter
from phaze.services.pipeline import _ORPHAN_TTL_SECONDS, refresh_stage_orphan_counts
from phaze.tasks._shared.queue_factory import build_pipeline_queue
from phaze.telemetry import configure_telemetry, shutdown_telemetry
from phaze.telemetry.db import instrument_engine
from phaze.telemetry.http import TelemetryMiddleware
from phaze.version import APP_VERSION
from phaze.web.saq_mount import build_saq_app
from phaze.web.static import STATIC_DIR, STATIC_VERSION, RevalidatingStaticFiles


if TYPE_CHECKING:
    from phaze.config import ControlSettings


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
    settings = get_settings()

    # Configure logging before migrations so startup failures use the normal pipeline.
    configure_logging(level=settings.log_level, json_logs=settings.log_json)

    # phaze-mvq8z.5: the reloadable-config store and its SIGHUP + directory-watch triggers, as
    # early as logging allows. RuntimeConfigStore's constructor VALIDATES the process's start-time
    # reloadable settings (e.g. WORKER_MAX_JOBS) and raises ValueError on an invalid one -- the
    # same fail-fast contract as a pydantic settings ValidationError elsewhere in this function,
    # and an implementer decision (phaze-mvq8z.5): an invalid reloadable setting refuses api
    # startup entirely rather than degrading silently, exactly like AgentSettings/ControlSettings
    # already do for every other setting. Doing this before telemetry/migrations means that
    # failure is reported through the just-configured logging pipeline.
    #
    # phaze-mvq8z.6 deliberately does NOT install the DB-override provider here: the override
    # table only exists once `run_migrations()` below has run, and this reload happens BEFORE
    # that -- before the database is even confirmed reachable (the `SELECT 1` check further
    # down). Reading the override table this early would fail on every fresh-DB first boot (the
    # reader raises on a DB error, phaze-mvq8z.19), rejecting this reload for no benefit, since
    # there is nothing yet to override. This first reload validates the env/file
    # layers only; a SECOND reload once the DB-override layer is wired in (below) folds in
    # anything an operator already set, so it takes effect immediately rather than waiting for
    # this process's first NOTIFY/poll tick or a SIGHUP.
    runtime_config_store = get_runtime_config_store()
    # phaze-mvq8z.8: re-apply configure_logging (already idempotent) on every reload that
    # changes log_level, in every process type. Registered BEFORE reload("startup") so that
    # first reload's file/override-layer log_level (folded in below, after the settings-only
    # value the bare configure_logging() call above used) is what actually takes effect.
    runtime_config_store.register_applier("log_level", log_level_applier(json_logs=settings.log_json))
    await runtime_config_store.reload("startup")
    install_sighup_handler(asyncio.get_running_loop(), runtime_config_store)
    _app.state.runtime_config_watcher = build_watcher(runtime_config_store)
    _app.state.runtime_config_watcher.start()

    # Telemetry is opt-in and failure-isolated; configure it before migrations for startup traces.
    configure_telemetry("api")
    instrument_engine(engine)

    # Migrate before the first normal database use.
    await run_migrations()

    # Refuse startup on an unreachable database instead of deferring failure to requests.
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))

    # phaze-mvq8z.8: the backends.toml registry hook + its own directory watch, now that the
    # database (cloud_job, for the in-flight-removal veto) is confirmed reachable. Registered
    # AFTER the SELECT 1 check above, unlike log_level, because a SIGHUP racing startup must
    # never query cloud_job before migrations have run.
    build_backends_registry_reloader(runtime_config_store, cast("ControlSettings", settings), async_session)
    _app.state.runtime_config_backends_watcher = build_backends_watcher(runtime_config_store)
    _app.state.runtime_config_backends_watcher.start()

    async with async_session() as bootstrap_session:
        await ensure_dev_agent(bootstrap_session)

    # phaze-mvq8z.6: wire the DB-override layer into the process-wide runtime-config store (the
    # migration above has now run, so the table exists), then start this process's LISTEN
    # connection + fallback poll so an admin-API/UI write (this process's own or another's)
    # reaches the store without a restart (ADR-0019 (runtime config hot-reload) §3/§14). The
    # provider is installed BEFORE this reload, and this is a SECOND "startup" reload -- see the
    # comment above the first one for why it is not simply merged into it.
    install_runtime_config_overrides(runtime_config_store, async_session)
    _app.state.runtime_config_listener = await start_runtime_config_listener(store=runtime_config_store, database_url=settings.database_url)
    await runtime_config_store.reload("startup")

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
    # phaze-mvq8z.6: stop the LISTEN connection + fallback poll before the engine it reloads
    # through goes away.
    await _app.state.runtime_config_listener.stop()
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
    # Reverse of construction: the backends.toml watcher was built right after the DB
    # reachability check, so it stops before the engine it depends on is disposed.
    await _app.state.runtime_config_backends_watcher.stop()
    await engine.dispose()
    # Reverse of construction: the runtime-config watcher/SIGHUP trigger was built right after
    # logging, before everything else -- stop it last among the app's own resources (it may be
    # mid-debounce-sleep; stop() bounds the observer-thread join to 10s).
    await _app.state.runtime_config_watcher.stop()
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
    # phaze-l1j35: the junk review's group decisions (approve / reject / undo / bulk) and its excerpt.
    junk_review.router,
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
    deployments.router,
    agent_identity.router,
    # phaze-mvq8z.9: agent-authenticated GET for the DB-override layer (RELOADABLE_KEYS only) --
    # the remote-agent half of ADR-0019 (runtime config hot-reload) §14's propagation story. Polled by
    # tasks/heartbeat.py on the heartbeat cadence.
    agent_config.router,
    agent_analysis.router,
    agent_push.router,
    agent_s3.router,
    agent_proposals.router,
    agent_scan_batches.router,
    agent_orphan_companions.router,
    # phaze-osy6j: what the agent read inside each companion (references, tracklist flag, junk class).
    agent_companion_capture.router,
    local_sources.router,
    agent_companion_features.router,
    # phaze-5cvbz: compute-scratch janitor liveness probe -- the agent-side startup sweep asks
    # here before deleting an age-eligible scratch entry a durable queued/active job still claims.
    agent_scratch.router,
    # Single mutation point for the ``exec:{batch_id}`` progress hash.
    agent_exec_batches.router,
    # phaze-6bkk internal-agent router (DIST-01): terminal outcome of an on-agent tag write. The
    # api container has no media mount, so the mutagen write runs on the owning agent's meta lane
    # and its result reaches the tag_write_log audit table only through this callback.
    agent_tag_writes.router,
    # phaze-lwuf6: the outcome of an on-agent junk quarantine move (the meta-lane quarantine_companion task).
    agent_junk_quarantine.router,
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
    # phaze-mvq8z.6: DB-override admin API for hot-reloadable config (GET/POST/DELETE
    # /admin/runtime-config/*). Also reachable at /s/runtime-config (shell.router above,
    # UTILITY_PANES["runtime-config"]) -- both share build_runtime_config_pane_context.
    admin_runtime_config.router,
)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="Phaze", version=APP_VERSION, lifespan=lifespan)
    # Install before routers so all requests use bounded-cardinality route labels.
    app.add_middleware(TelemetryMiddleware)
    for router in _ROUTERS:
        app.include_router(router)
    # Matching content fingerprints may cache forever; stale or absent versions revalidate.
    app.mount("/static", RevalidatingStaticFiles(directory=STATIC_DIR, version=STATIC_VERSION), name="static")
    return app


app = create_app()
