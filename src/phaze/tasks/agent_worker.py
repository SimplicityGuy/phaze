"""SAQ agent_worker settings -- entry point for ``saq phaze.tasks.agent_worker.settings`` (Phase 26 D-01..D-04, D-16, D-25).

CRITICAL: this module MUST NOT transitively import phaze.database,
phaze.tasks.session, or sqlalchemy.ext.asyncio. Enforced by tests/test_task_split.py
(subprocess import-boundary test, D-25). The invariant guarantees the agent role
can run on a host with no Postgres reachability.

Startup sequence (D-16 + B1):
1. Models check -- agents need essentia .pb files mounted at MODELS_PATH.
2. Construct PhazeAgentClient(base_url=agent_api_url, token=agent_token, timeout=30.0).
3. Call client.whoami() with bounded exponential retry (1s, 2s, 4s, 8s, 16s, 32s
   = up to 63s total) -- raises RuntimeError if still failing.
4. Assert identity.agent_id matches the operator-supplied PHAZE_AGENT_QUEUE env
   suffix; if not, raise RuntimeError (anti-misconfiguration probe per Pitfall 1).
5. Construct FingerprintOrchestrator(engines=[AudfprintAdapter, PanakoAdapter])
   and stash at ctx["fingerprint_orchestrator"] (B1 -- Plan 11 fingerprint.py +
   scan.py read it). AudfprintAdapter + PanakoAdapter are HTTP wrappers around
   local sidecars; they do NOT pull phaze.database into the import graph.
6. Create CPU-bound essentia process pool.

Queue name resolution (D-16 step 5 + D-18):
- SAQ requires the Queue at module-import time.
- Queue name comes from env var PHAZE_AGENT_QUEUE (operator-supplied; expected
  form `phaze-agent-<agent_id>`).
- Startup hook then verifies token-derived agent_id matches the env-supplied suffix.
- This dual-source design (env at import time, /whoami at startup) is the canonical
  guard against operator misconfig per Pitfall 1.

Docker invocation (Phase 29 docker-compose.agent.yml):
    services:
      worker:
        command: uv run saq phaze.tasks.agent_worker.settings
        environment:
          PHAZE_ROLE: agent
          PHAZE_AGENT_API_URL: http://app-server:8000
          PHAZE_AGENT_TOKEN: phaze_agent_<...>
          PHAZE_AGENT_QUEUE: phaze-agent-fileserver-01
          PHAZE_REDIS_URL: redis://app-server:6379/0
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import redis.asyncio as redis_async
from saq import CronJob
import structlog

from phaze.config import AgentSettings, get_settings
from phaze.logging_config import configure_logging
from phaze.services.fingerprint import AudfprintAdapter, FingerprintOrchestrator, PanakoAdapter
from phaze.tasks._shared.agent_bootstrap import (
    _WHOAMI_BACKOFF_S,  # noqa: F401  # re-export for back-compat / test patching
    construct_agent_client,
    whoami_with_retry as _whoami_with_retry,
)
from phaze.tasks._shared.deterministic_key import increment_completed
from phaze.tasks._shared.model_bootstrap import ensure_models_present
from phaze.tasks._shared.queue_factory import build_pipeline_queue
from phaze.tasks.execution import execute_approved_batch
from phaze.tasks.fingerprint import fingerprint_file
from phaze.tasks.functions import process_file
from phaze.tasks.heartbeat import heartbeat_tick
from phaze.tasks.metadata_extraction import extract_file_metadata
from phaze.tasks.pool import create_process_pool
from phaze.tasks.scan import scan_directory, scan_live_set


logger = structlog.get_logger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    """SAQ startup hook for the agent role (D-16)."""
    cfg = get_settings()
    if not isinstance(cfg, AgentSettings):
        msg = f"agent_worker requires PHAZE_ROLE=agent; get_settings() returned {type(cfg).__name__}"
        raise RuntimeError(msg)

    # PR3 observability: each SAQ worker is its OWN OS process and does NOT inherit
    # the api's logging config. Configure the central pipeline here, BEFORE the first
    # logger.info, so worker logs render through the same JSON/console pipeline.
    configure_logging(level=cfg.log_level, json_logs=cfg.log_json)

    # D-13 invariant: NEVER log the full bearer; preview is first-12-chars + "..." only.
    # The variable name keeps `token_preview` for grepability of the D-13 invariant
    # in the codebase; the format-string key is "auth_id_prefix" (no secret keywords)
    # so static analyzers don't flag the format literal itself as a leak.
    token_preview = cfg.agent_token.get_secret_value()[:12] + "..."
    logger.info(
        "phaze.tasks.agent_worker startup role=agent api=%s auth_id_prefix=%s queue=%s",
        cfg.agent_api_url,
        token_preview,
        os.environ.get("PHAZE_AGENT_QUEUE", "<unset>"),
    )

    # Phase 36: dedicated cache-redis handle. The broker is Postgres now, so cache-plane
    # readers must use a Redis client decoupled from the queue. Symmetric with the control
    # role's ctx["redis"]; created here, closed in shutdown. from_url is lazy (no socket here).
    ctx["redis"] = redis_async.Redis.from_url(cfg.redis_url)

    # Step 2: Construct PhazeAgentClient (shared bootstrap helper -- Phase 27 D-17).
    client = construct_agent_client(cfg)
    ctx["api_client"] = client

    # Step 3: /whoami probe with bounded retry.
    identity = await _whoami_with_retry(client)

    # Step 3a (Phase 29 D-21 / 260608-u8g): ensure essentia weights present.
    # The healthy path is a pure local os.stat size-manifest check (zero network,
    # near-instant). Placed AFTER whoami so auth fails fast (~60s). WORKER-ONLY
    # (Phase 29 WARNING-7): the watcher does not call this -- only the worker owns
    # the download to avoid a .part-file race on fresh /models volumes.
    # asyncio.to_thread keeps even the rare repair path (network + time.sleep
    # backoff) off the event loop, preventing the scan_directory job
    # starvation/timeout that motivated this change (260608-u8g). to_thread accepts
    # a sync callable and propagates its return value and exceptions unchanged.
    await asyncio.to_thread(ensure_models_present, Path(cfg.models_path))

    # Step 4: Queue-name mismatch guard (Pitfall 1).
    expected_queue = f"phaze-agent-{identity.agent_id}"
    actual_queue = os.environ.get("PHAZE_AGENT_QUEUE")
    if actual_queue != expected_queue:
        msg = (
            f"queue/token mismatch: token resolves to agent_id={identity.agent_id} "
            f"(expected PHAZE_AGENT_QUEUE={expected_queue}), but env PHAZE_AGENT_QUEUE={actual_queue}. "
            "Operator misconfiguration -- exiting non-zero."
        )
        raise RuntimeError(msg)
    ctx["agent_identity"] = identity
    ctx["agent_queue_name"] = expected_queue

    # Step 5: Construct fingerprint orchestrator (B1 -- fingerprint_file + scan_live_set
    # read ctx["fingerprint_orchestrator"]). AudfprintAdapter + PanakoAdapter are
    # HTTP wrappers around local sidecars; they do NOT pull phaze.database into the
    # import graph. Transitive-import chain verified clean:
    #   phaze.services.fingerprint -> httpx + structlog only (no SQLAlchemy/DB)
    ctx["fingerprint_orchestrator"] = FingerprintOrchestrator(
        engines=[
            AudfprintAdapter(base_url=cfg.audfprint_url),
            PanakoAdapter(base_url=cfg.panako_url),
        ],
    )

    # Step 6: CPU-bound essentia process pool (mirror worker.py:41).
    ctx["process_pool"] = create_process_pool()

    logger.info(
        "phaze.tasks.agent_worker startup complete agent_id=%s queue=%s",
        identity.agent_id,
        expected_queue,
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    """SAQ shutdown hook for the agent role."""
    logger.info("phaze.tasks.agent_worker shutdown")

    pool = ctx.get("process_pool")
    if pool is not None:
        pool.shutdown(wait=True)

    orchestrator = ctx.get("fingerprint_orchestrator")
    if orchestrator is not None:
        for eng in orchestrator.engines:
            if hasattr(eng, "close"):
                await eng.close()

    client = ctx.get("api_client")
    if client is not None:
        await client.close()

    # Phase 36: close the dedicated cache-redis client.
    cache_redis = ctx.get("redis")
    if cache_redis is not None:
        await cache_redis.aclose()

    # Phase 36 (WR-01): also close the factory-attached cache_redis on the module-level queue.
    # The counter hooks read THIS handle (getattr(job.queue, "cache_redis", ...)), and SAQ's
    # Worker.stop() -> queue.disconnect() closes only the psycopg3 pool, leaving it open.
    queue_cache_redis = getattr(queue, "cache_redis", None)
    if queue_cache_redis is not None:
        await queue_cache_redis.aclose()


# Module-level Queue construction. SAQ's `saq <module>.settings` CLI imports
# this module and reads `settings` as a top-level attribute (RESEARCH §A2).
# Queue name comes from PHAZE_AGENT_QUEUE env (operator-supplied at deploy time).
# The startup hook re-validates this against the token-derived agent_id (Step 4 above).
_queue_name = os.environ.get("PHAZE_AGENT_QUEUE")
if not _queue_name:
    # Module-import-time failure surface -- container exits before SAQ event loop starts.
    # Common during local dev when env isn't set; clearer than a runtime "queue is empty" mystery.
    msg = "PHAZE_AGENT_QUEUE env var is required for agent_worker. Expected form: phaze-agent-<agent_id>. See Phase 26 D-16."
    raise RuntimeError(msg)
# Phase 36: built via the single `build_pipeline_queue` seam -- a PostgresQueue (broker =
# queue_url) with BOTH before_enqueue hooks already registered and a decoupled `cache_redis`
# handle. The agent runs in a separate container but shares the central cache-Redis, so its
# enqueued/completed counter INCRs (folded into the hooks via cache_redis) land in the same
# counters the dashboard reads. Conservative pool sizing (1/4) keeps the per-queue psycopg3
# budget under Postgres max_connections (RESEARCH Pitfall 4). The factory owns the hook chain.
queue = build_pipeline_queue(_queue_name, get_settings().queue_url, cache_redis_url=get_settings().redis_url, min_size=1, max_size=4)


settings = {
    "queue": queue,
    # Phase 35 (D-02): bump the maintained `completed` counter on each COMPLETE outcome
    # (Worker constructor kwarg, not a register_* call).
    "after_process": increment_completed,
    "functions": [
        process_file,
        extract_file_metadata,
        fingerprint_file,
        scan_live_set,
        scan_directory,  # Phase 27 D-13: chunked HTTP-only directory walk
        execute_approved_batch,
        heartbeat_tick,  # Phase 29 D-08: SAQ-dispatched 30s cron handler
    ],
    "cron_jobs": [
        # Phase 29 D-08 + RESEARCH Critical Discovery #2: trailing-seconds
        # 6-field form (croniter 6.x default). "*/30 * * * * *" would fire
        # every second. Smoke-tested at module import time via croniter.
        CronJob(heartbeat_tick, cron="* * * * * */30", unique=True, timeout=10),  # type: ignore[type-var]
    ],
    "concurrency": get_settings().worker_max_jobs,
    "startup": startup,
    "shutdown": shutdown,
}
