"""FastAPI application factory with lifespan management."""

import asyncio
from collections.abc import AsyncGenerator
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
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
    agent_fingerprint,
    agent_heartbeat,
    agent_identity,
    agent_metadata,
    agent_proposals,
    agent_push,
    agent_s3,
    agent_scan_batches,
    agent_tracklists,
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
    search,
    shell,
    tags,
    tracklists,
)
from phaze.services.agent_bootstrap import ensure_dev_agent
from phaze.services.agent_task_router import AgentTaskRouter
from phaze.services.pipeline import _ORPHAN_TTL_SECONDS, refresh_stage_orphan_counts
from phaze.tasks._shared.queue_factory import build_pipeline_queue
from phaze.web.saq_mount import build_saq_app


logger = structlog.get_logger(__name__)


async def _orphan_refresh_loop() -> None:
    """Background loop: refresh the orphan-count cache off-request (HYG-01 / WR-02).

    Mirrors the agent heartbeat loop discipline (:func:`phaze.tasks.heartbeat._heartbeat_loop`):
    launched as an asyncio task in the FastAPI lifespan so the potentially-large
    ``scheduling_ledger`` materialization runs OUTSIDE the hot 5s /pipeline/stats request path
    (D-01/D-02). Each iteration recomputes via :func:`refresh_stage_orphan_counts` (which rebinds
    the module cache on success ONLY -- D-03); a single failed refresh logs + keeps the last-good
    value and never kills the loop, and ``asyncio.CancelledError`` is re-raised so shutdown can
    cancel + await cleanly before ``engine.dispose()``.
    """
    while True:
        try:
            await refresh_stage_orphan_counts()
        except asyncio.CancelledError:
            raise
        except Exception:
            # One bad refresh must not kill the loop -- log and keep the last-good cache value.
            logger.warning("orphan refresh loop iteration failed; keeping last-good", exc_info=True)
        await asyncio.sleep(_ORPHAN_TTL_SECONDS)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Manage application lifespan: verify DB; create SAQ queue + task_router + redis on startup; dispose on shutdown.

    Resources wired (in order):
        1. ``app.state.controller_queue`` -- the named ``controller`` SAQ Queue consumed by the
                                         application-server ``phaze-worker``. Phase 30 replaced the
                                         Phase 25 unnamed default queue here: that queue had NO
                                         consumer, so every control-plane enqueue routed to it was
                                         stranded (the v4.0.6 incident). Registers the project
                                         ``apply_project_job_defaults`` before_enqueue hook so jobs
                                         inherit the policy timeout/retry budget (mirrors controller.py).
        2. ``app.state.task_router``  -- Phase 26 D-20 AgentTaskRouter for per-agent SAQ enqueues
                                         (used by agent_files.py auto-enqueue path).
        3. ``app.state.redis``        -- Phase 26 D-27 shared async Redis client for the tracklists
                                         idempotency cache (decode_responses=True so .get/.set yield str).

    Shutdown is reverse-order (task_router, redis, controller_queue) so the SAQ-backed router
    closes before the underlying controller queue's Redis pool is gone.

    Phase 27 UAT Gap 2/3: ``run_migrations`` and ``ensure_dev_agent`` run BEFORE
    the queue / task_router / redis are wired so a fresh docker compose stack
    can boot to a working state with one ``docker compose up``. Both are gated
    by config knobs (``settings.auto_migrate`` and ``settings.dev_seed_agent``)
    so operators can opt out in production.
    """
    # PR3 observability: configure the central structlog pipeline FIRST -- before
    # run_migrations() or any DB access -- so every startup log line (including
    # migration / connectivity failures) flows through the JSON/console pipeline.
    configure_logging(level=settings.log_level, json_logs=settings.log_json)

    # Phase 27 UAT Gap 2: bring the schema to head BEFORE the engine is used
    # for normal traffic. Idempotent + gated by settings.auto_migrate.
    await run_migrations()

    # Verify connectivity. The SELECT 1 also raises early with a clear error if
    # the database is unreachable -- the lifespan crash surface aborts FastAPI
    # startup instead of letting routers 500 later.
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))

    # Phase 27 UAT Gap 3: seed a dev agent on a fresh agents table so the
    # watcher can authenticate on first start. No-op on existing deployments
    # (idempotent) and disabled by default (settings.dev_seed_agent=false).
    async with async_session() as bootstrap_session:
        await ensure_dev_agent(bootstrap_session)

    # Named controller queue (Phase 30) -- consumed by the application-server phaze-worker
    # (phaze.tasks.controller.settings). Replaces the Phase 25 unnamed default queue, which had
    # no consumer and stranded every control-plane enqueue routed to it (the v4.0.6 incident).
    # Phase 36: built via the single `build_pipeline_queue` seam -- a PostgresQueue (broker =
    # queue_url) with BOTH before_enqueue hooks (apply_project_job_defaults + apply_deterministic_key)
    # already registered and a decoupled `cache_redis` handle. Conservative pool sizing (2/8) for
    # the control role keeps the per-queue psycopg3 budget under Postgres max_connections.
    # Phase 45 (L-01): pass the control-side scheduling-ledger sessionmaker (``async_session``,
    # bound to the API engine) so the before_enqueue WRITE hook records every manual DAG-trigger
    # enqueue in the durable ledger -- the manual and recovery paths must both write it or recovery
    # cannot distinguish "scheduled-and-lost" from "never scheduled" (the 2026-06-18 incident).
    _app.state.controller_queue = build_pipeline_queue(
        "controller",
        settings.queue_url,
        cache_redis_url=settings.redis_url,
        min_size=2,
        max_size=8,
        ledger_sessionmaker=async_session,
    )
    # Phase 36: open the controller broker pool now. The PostgresQueue pool is built open=False
    # (no socket at construction) and, unlike the old redis-backed Queue, does NOT auto-connect
    # on first enqueue -- so the API-side producer must open it explicitly. init_db is idempotent
    # (CREATE TABLE IF NOT EXISTS saq_jobs/saq_stats/saq_versions); first boot self-creates them.
    await _app.state.controller_queue.connect()
    # AgentTaskRouter -- per-agent SAQ enqueuer (Phase 26 Plan 04, D-20). Phase 36: takes
    # (queue_url, cache_redis_url) -- Postgres broker + Redis cache.
    # Phase 45: pass the ledger sessionmaker so each per-agent queue the router builds attaches it
    # (manual agent-routed DAG triggers record their ledger rows control-side).
    _app.state.task_router = AgentTaskRouter(queue_url=settings.queue_url, cache_redis_url=settings.redis_url, ledger_sessionmaker=async_session)
    # Shared Redis client for tracklists idempotency cache (Phase 26 Plan 07, D-27).
    # decode_responses=True so .get/.set return str (matches agent_tracklists.py expectations).
    _app.state.redis = redis_async.Redis.from_url(settings.redis_url, decode_responses=True)

    # Phase 33: mount the SAQ monitoring dashboard at /saq, gated by settings.enable_saq_ui.
    #
    # Why HERE (in the lifespan) and not in create_app(): the dashboard reads through the
    # exact Queue instances wired above -- the named ``controller`` queue plus one
    # ``phaze-agent-<id>`` queue per non-revoked agent -- and those instances (and the agent
    # roster they depend on) only exist AFTER startup. Building the mount here reuses the
    # SAME lifespan-created Queue instances, so the dashboard reads through their existing
    # Redis pools with NO second connection pool. Starlette wraps its router by reference, so
    # a mount added inside the lifespan (before the first ``yield``) is served by every
    # subsequent HTTP request (RESEARCH-VERIFIED, Q1).
    #
    # ``revoked_at IS NULL`` is the exact non-revoked-agent query shape used at
    # pipeline.py:186; it auto-excludes the permanently-revoked ``legacy-application-server``
    # (whose ``revoked_at == created_at``). ``task_router.queue_for`` returns the CACHED
    # per-agent Queue (with the ``apply_project_job_defaults`` hook already applied), i.e. the
    # same instance the enqueue path uses -- never a freshly constructed pool.
    #
    # ``build_saq_app`` MUST be called exactly ONCE per process: ``saq_web`` clobbers its
    # module-level queue registry on every call (RESEARCH Pitfall 1), so the single mount here
    # is the only call site. Agents registered AFTER startup appear only after the next api
    # restart -- operator-acceptable; hot-reload is intentionally NOT built (LOCKED).
    #
    # The ``/saq`` sub-app holds no resources of its own (it reads via the passed queues, whose
    # Redis pools the shutdown block already disconnects), so the reverse-order shutdown below
    # is left untouched. No auth middleware, no new port, no ``saq[web]`` -- the reverse proxy's
    # internal-realm auth on the private LAN is the sole access control (LOCKED, threat T-33-03).
    #
    # phaze-k1vy: SER-01 kind-scoping (mirrors shell.py:216, pipeline.py:584) -- kueue-routed
    # kind="compute" agents (e.g. k8s-vox / k8s-xenolab) never have SAQ queues; their work
    # bypasses SAQ entirely (KSUBMIT-06 forbids seeding SAQ jobs for the kueue path). Mounting
    # their phantom per-lane queues here made the dashboard show 10 permanently-0/0/0 queues
    # that were misread as "workers are down" on 2026-07-17, and opened an unused psycopg pool
    # per phantom queue. Scope to kind="fileserver" so only agents that actually run SAQ workers
    # are enumerated.
    if settings.enable_saq_ui:
        async with async_session() as session:
            agents_stmt = select(Agent).where(Agent.revoked_at.is_(None), Agent.kind == "fileserver").order_by(Agent.name)
            agents = (await session.execute(agents_stmt)).scalars().all()
        # quick-260707-dh1: mount ALL FOUR lane queues per agent (analyze/fingerprint/meta/io)
        # PLUS the legacy base queue so the migration drain window is visible in the dashboard.
        agent_queues = [
            q for agent in agents for q in (*_app.state.task_router.all_lane_queues(agent.id), _app.state.task_router.legacy_base_queue(agent.id))
        ]
        # Phase 36: the dashboard reads each queue's `.info()`, which needs an open psycopg pool.
        # The PostgresQueue pools are built open=False, so open them here (idempotent connect()).
        for q in agent_queues:
            await q.connect()
        _app.mount("/saq", build_saq_app([_app.state.controller_queue, *agent_queues]))

    # HYG-01 (WR-02): launch the off-request orphan-count refresher AFTER DB connectivity is
    # established and BEFORE yield. It runs OUTSIDE the hot 5s /pipeline/stats poll so the amber
    # badge read is O(1) and can never block on scheduling_ledger size (the 2026-06-18 ~44.5K-row
    # incident). Cancelled + awaited on shutdown BEFORE engine.dispose() (below).
    _app.state.orphan_task = asyncio.create_task(_orphan_refresh_loop())

    yield
    # Shutdown in reverse construction order.
    # HYG-01: stop the orphan refresher FIRST so no in-flight refresh touches a disposed engine;
    # cancel-then-await under suppress absorbs the expected CancelledError (mirrors the heartbeat
    # cancel discipline). This runs BEFORE engine.dispose() at the end of this block.
    _app.state.orphan_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _app.state.orphan_task
    await _app.state.task_router.close()
    await _app.state.redis.aclose()
    # Phase 36 (WR-01): close the factory-attached cache_redis before the pool — disconnect()
    # closes only the psycopg3 pool, leaving the controller queue's Redis client open.
    controller_cache_redis = getattr(_app.state.controller_queue, "cache_redis", None)
    if controller_cache_redis is not None:
        await controller_cache_redis.aclose()
    await _app.state.controller_queue.disconnect()
    await engine.dispose()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="Phaze", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(companion.router)
    app.include_router(proposals.router)
    app.include_router(execution.router)
    app.include_router(preview.router)
    app.include_router(duplicates.router)
    app.include_router(tracklists.router)
    app.include_router(pipeline.router)
    # Phase 57 (SHELL-01): the v7.0 shell router owns GET / (Analyze default) + GET
    # /s/{stage}. Prefix-less (like pipeline.router) so it can claim the root path; the
    # legacy /pipeline/ now 302-redirects here. NO extra prefix= (required by
    # tests/_route_introspection.iter_effective_routes).
    app.include_router(shell.router)
    # Phase 61 (61-02, RECORD-01 / D-01): the per-file full-record read-only fragment
    # route (GET /record/{file_id}). Typed uuid.UUID path param + strictly file_id-scoped
    # reads (T-61-03); a missing file renders the friendly 404 fragment (T-61-05).
    app.include_router(record.router)
    # Phase 37: per-stage control-plane endpoints (POST /pipeline/stages/{stage}/
    # {priority,pause,resume}). Distinct from `pipeline.router` (dashboard + triggers);
    # mutates the pipeline_stage_control intent row + the live saq_jobs backlog together.
    app.include_router(pipeline_stages.router)
    # Phase 71 (71-04, BEUI-02): the force-local master routing override thin write endpoint
    # (POST /pipeline/routing/force-local). Mirrors the pipeline_stages thin-endpoint pattern;
    # flips the durable route_control 'global' row + returns the header pill (swapped in place).
    app.include_router(routing.router)
    app.include_router(search.router)
    app.include_router(tags.router)
    app.include_router(cue.router)
    # Phase 25 internal-agent routers (D-10)
    app.include_router(agent_files.router)
    app.include_router(agent_metadata.router)
    app.include_router(agent_fingerprint.router)
    app.include_router(agent_execution.router)
    app.include_router(agent_heartbeat.router)
    # Phase 26 internal-agent routers (D-15, D-26, D-27, D-28)
    app.include_router(agent_identity.router)
    app.include_router(agent_analysis.router)
    app.include_router(agent_push.router)
    app.include_router(agent_s3.router)
    app.include_router(agent_tracklists.router)
    app.include_router(agent_proposals.router)
    # Phase 27 internal-agent router (D-10).
    app.include_router(agent_scan_batches.router)
    # Phase 28 internal-agent router (D-05): per-proposal terminal-state progress reporting
    # — the single mutation point for exec:{batch_id} Redis hash (D-02).
    app.include_router(agent_exec_batches.router)
    # Phase 27 admin-UI router (D-05..D-08): POST /pipeline/scans + the HTMX
    # poll partial + the agent-roots swap. Distinct from `pipeline.router`,
    # which serves the dashboard page and existing pipeline-stage triggers.
    app.include_router(pipeline_scans.router)
    # Phase 29 admin-UI router (D-11..D-14): GET /admin/agents (operator-facing
    # liveness page) + GET /admin/agents/_table (HTMX 5s poll partial). The
    # router is read-only and does NOT use get_authenticated_agent (consistent
    # with other admin-UI routers on the private LAN).
    app.include_router(admin_agents.router)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    return app


app = create_app()
