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
import contextlib
import os
from pathlib import Path
import shutil
from typing import Any

import redis.asyncio as redis_async
import structlog

from phaze.config import AgentSettings, get_settings
from phaze.logging_config import configure_logging
from phaze.services.enqueue_router import LANE_TASKS, LANES
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
from phaze.tasks.heartbeat import _heartbeat_loop
from phaze.tasks.metadata_extraction import extract_file_metadata
from phaze.tasks.pool import create_process_pool
from phaze.tasks.push import push_file
from phaze.tasks.s3_upload import upload_file_s3
from phaze.tasks.scan import scan_directory, scan_live_set


logger = structlog.get_logger(__name__)


def _sweep_scratch(scratch_dir: Path) -> None:
    """Remove every file (and the ``.rsync-partial`` dir) under the compute scratch dir (D-14).

    The compute-only startup janitor's worker. Bounds scratch disk to the in-flight set: a
    hard-killed worker can leave a half-pushed file or a ``.rsync-partial`` directory behind, and
    any file still genuinely needed is re-pushed by the staging cron (the deterministic
    ``push_file:<file_id>`` key + FileState window make this safe). Tolerates a missing dir so a
    fresh compute host (scratch volume not yet created) starts cleanly. stdlib-only -- keeps the
    module Postgres-free (tests/test_task_split.py)."""
    if not scratch_dir.exists():
        return
    for entry in scratch_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


async def _maybe_sweep_scratch(cfg: AgentSettings) -> None:
    """Compute-only startup janitor gate (D-14).

    Sweeps ONLY when ``cfg.kind == "compute"`` AND a ``cloud_scratch_dir`` is configured. The
    file-server agent runs this SAME module (zero compute-specific worker code) and owns no scratch
    dir, so it must NOT sweep. Runs off the event loop via ``asyncio.to_thread`` (parity with the
    ``ensure_models_present`` startup step)."""
    if cfg.kind == "compute" and cfg.cloud_scratch_dir:
        await asyncio.to_thread(_sweep_scratch, Path(cfg.cloud_scratch_dir))


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

    # quick-260707-g84: record the EFFECTIVE dispatch concurrency (post-clamp), the lane, and
    # whether the worker_max_jobs ceiling bit. In lane mode WORKER_MAX_JOBS is a ceiling on the
    # per-lane knob (concurrency = min(lane knob, worker_max_jobs)); logging it here (AFTER
    # configure_logging, never at import time) makes the OCI A1 single-job cap auditable at boot.
    logger.info(
        "phaze.tasks.agent_worker concurrency effective=%s lane=%s worker_max_jobs_ceiling=%s clamped=%s",
        _concurrency,
        _lane or "<all-mode>",
        cfg.worker_max_jobs,
        _concurrency_clamped,
    )

    # D-13 invariant: NEVER log the full bearer; preview is first-12-chars + "..." only.
    # The variable name keeps `token_preview` for grepability of the D-13 invariant
    # in the codebase; the format-string key is "auth_id_prefix" (no secret keywords)
    # so static analyzers don't flag the format literal itself as a leak.
    token_preview = cfg.agent_token.get_secret_value()[:12] + "..."
    logger.info(
        "phaze.tasks.agent_worker startup role=agent api=%s auth_id_prefix=%s queue=%s lane=%s",
        cfg.agent_api_url,
        token_preview,
        _queue_name,
        _lane or "<all-mode>",
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

    # Step 3b (Phase 50 D-14): compute-only scratch janitor. Sweep orphaned push scratch off the
    # event loop BEFORE the worker starts dispatching jobs, so a hard-killed prior worker cannot
    # leak disk. Gated on kind == "compute" + cloud_scratch_dir (the fileserver runs the same
    # module and owns no scratch dir). This is the agent-side analog of the controller's startup
    # reconciliation. Placed after the models check (also off-loop) and before the dispatch loop.
    await _maybe_sweep_scratch(cfg)

    # Step 4: Queue-name mismatch guard (Pitfall 1). Compare the BASE (agent-level identity,
    # single across lanes) -- the lane suffix is orthogonal to the token->agent_id binding.
    expected_base = f"phaze-agent-{identity.agent_id}"
    if _base != expected_base:
        msg = (
            f"queue/token mismatch: token resolves to agent_id={identity.agent_id} "
            f"(expected PHAZE_AGENT_QUEUE={expected_base}), but env PHAZE_AGENT_QUEUE={_base}. "
            "Operator misconfiguration -- exiting non-zero."
        )
        raise RuntimeError(msg)
    ctx["agent_identity"] = identity
    # quick-260707-dh1: the effective consumed queue is the lane-suffixed name (or the base in all-mode).
    ctx["agent_queue_name"] = _queue_name

    # Phase 46: launch the liveness heartbeat as an asyncio background task OUTSIDE the
    # SAQ dispatch pool. As a CronJob it competed for the same worker_max_jobs slots as
    # multi-hour process_file jobs and got starved (~50min gaps vs the 300s DEAD
    # threshold), marking a healthy busy agent DEAD. The event loop is free (essentia
    # runs in a pebble ProcessPool, Phase 43), so a plain task ticks reliably. It needs
    # only api_client + agent_identity (already set); it reads ctx["worker"].queue lazily
    # and degrades queue_depth to 0 if the worker is not yet attached.
    #
    # quick-260707-dh1: the heartbeat is agent-level, not lane-level. With the 4-lane
    # split, compose sets PHAZE_AGENT_HEARTBEAT=true on EXACTLY ONE lane worker (analyze)
    # and false on the other three, so an agent reports one authoritative last_seen -- not
    # N duplicate heartbeats. A1 caveat: the heartbeat's queue_depth reads ctx["worker"].queue,
    # which in the analyze-lane worker is only the analyze lane's depth (the dashboard's
    # get_queue_activity is the authoritative all-lane figure -- see docs/agent-queue-lanes.md).
    if cfg.agent_heartbeat_enabled:
        ctx["heartbeat_task"] = asyncio.create_task(_heartbeat_loop(ctx))

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
        "phaze.tasks.agent_worker startup complete agent_id=%s queue=%s lane=%s",
        identity.agent_id,
        _queue_name,
        _lane or "<all-mode>",
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    """SAQ shutdown hook for the agent role."""
    logger.info("phaze.tasks.agent_worker shutdown")

    # Phase 46: cancel the background heartbeat FIRST, before closing api_client, so an
    # in-flight heartbeat POST never hits a closed client. Guarded: the key may be absent
    # if startup never reached the launch point.
    heartbeat_task = ctx.get("heartbeat_task")
    if heartbeat_task is not None:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task

    # Phase 43: pebble ProcessPool shuts down via stop()/join() (not shutdown()).
    pool = ctx.get("process_pool")
    if pool is not None:
        pool.stop()
        pool.join()

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
# this module and reads `settings` as a top-level attribute (RESEARCH §A2). All four
# lanes share this SAME entry-point (`saq phaze.tasks.agent_worker.settings`); only the
# PHAZE_AGENT_LANE env differs, so `settings` stays a static top-level attribute.
# Queue name base comes from PHAZE_AGENT_QUEUE env (operator-supplied at deploy time).
# The startup hook re-validates the BASE against the token-derived agent_id (Step 4 above).
_base = os.environ.get("PHAZE_AGENT_QUEUE")
if not _base:
    # Module-import-time failure surface -- container exits before SAQ event loop starts.
    # Common during local dev when env isn't set; clearer than a runtime "queue is empty" mystery.
    msg = "PHAZE_AGENT_QUEUE env var is required for agent_worker. Expected form: phaze-agent-<agent_id>. See Phase 26 D-16."
    raise RuntimeError(msg)

# quick-260707-dh1: registration entry per SAQ task name. Plain function objects register
# under their __name__; s3_upload is a (name, func) tuple so its SAQ name is "s3_upload".
# The lane worker selects the subset for its lane from LANE_TASKS; add a lane -> update
# LANE_TASKS in enqueue_router.py (the single source of truth), never this dict alone.
_FUNCTIONS_BY_NAME: dict[str, Any] = {
    "process_file": process_file,
    "extract_file_metadata": extract_file_metadata,
    "fingerprint_file": fingerprint_file,
    "scan_live_set": scan_live_set,
    "scan_directory": scan_directory,  # Phase 27 D-13: chunked HTTP-only directory walk
    "execute_approved_batch": execute_approved_batch,
    "push_file": push_file,  # Phase 50: fileserver rsync-over-SSH push to the compute scratch dir
    # Phase 53: fileserver httpx multipart-PUT upload to presigned S3 URLs. Registered under the
    # explicit SAQ name "s3_upload" (a (name, func) tuple) so the control-plane producer enqueues
    # by "s3_upload" (Plan 04) -- this name MUST mirror LANE_TASKS["io"] in enqueue_router.py.
    "s3_upload": ("s3_upload", upload_file_s3),
}
# Preserve today's all-mode registration order (used when no lane is set).
_ALL_FUNCTION_NAMES: tuple[str, ...] = (
    "process_file",
    "extract_file_metadata",
    "fingerprint_file",
    "scan_live_set",
    "scan_directory",
    "execute_approved_batch",
    "push_file",
    "s3_upload",
)
# Per-lane concurrency knob attribute on Settings (design defaults 4/2/2/4).
_LANE_CONCURRENCY_ATTR: dict[str, str] = {
    "analyze": "lane_analyze_concurrency",
    "fingerprint": "lane_fingerprint_concurrency",
    "meta": "lane_meta_concurrency",
    "io": "lane_io_concurrency",
}

# quick-260707-dh1: resolve the lane ONCE at import. Unset -> all-mode (back-compat: the
# single-worker behavior with the base queue, all 8 functions, worker_max_jobs concurrency).
_lane = os.environ.get("PHAZE_AGENT_LANE") or None
if _lane is not None and _lane not in LANES:
    # Fail loud at container boot (non-zero) rather than silently consuming an empty/wrong queue.
    msg = f"invalid PHAZE_AGENT_LANE={_lane!r}; valid lanes are {list(LANES)} (or unset for all-mode)."
    raise RuntimeError(msg)

_settings_obj = get_settings()
# quick-260707-g84 memory-safety ceiling: in lane mode WORKER_MAX_JOBS is a CEILING, not
# inert. PR #218 resolved lane concurrency SOLELY from the per-lane knob, so a compose that
# relied on WORKER_MAX_JOBS=1 (e.g. the OCI Ampere A1 12 GB compute agent, where process_file
# peaks ~8 GB) silently ran 4 concurrent analyze jobs and OOM-killed. Clamp the lane knob by
# worker_max_jobs -> concurrency = min(lane knob, worker_max_jobs), so an explicit lower cap
# is authoritative. All-mode is unchanged (concurrency == worker_max_jobs).
_lane_raw_concurrency: int | None = None
if _lane is not None:
    _queue_name = f"{_base}-{_lane}"
    _function_names: tuple[str, ...] = tuple(name for name in _ALL_FUNCTION_NAMES if name in LANE_TASKS[_lane])
    _lane_raw_concurrency = getattr(_settings_obj, _LANE_CONCURRENCY_ATTR[_lane])
    _concurrency: int = min(_lane_raw_concurrency, _settings_obj.worker_max_jobs)
else:
    _queue_name = _base
    _function_names = _ALL_FUNCTION_NAMES
    _concurrency = _settings_obj.worker_max_jobs
# True only when the worker_max_jobs ceiling actually clamped the lane knob (lane mode only).
_concurrency_clamped = _lane_raw_concurrency is not None and _concurrency < _lane_raw_concurrency

# Phase 36: built via the single `build_pipeline_queue` seam -- a PostgresQueue (broker =
# queue_url) with BOTH before_enqueue hooks already registered and a decoupled `cache_redis`
# handle, so the deterministic-key dedup + counter INCRs are per-lane and identical. The agent
# runs in a separate container but shares the central cache-Redis, so its enqueued/completed
# counter INCRs land in the same counters the dashboard reads. Conservative pool sizing (1/4)
# keeps the per-queue psycopg3 budget under Postgres max_connections (RESEARCH Pitfall 4).
queue = build_pipeline_queue(_queue_name, _settings_obj.queue_url, cache_redis_url=_settings_obj.redis_url, min_size=1, max_size=4)


settings = {
    "queue": queue,
    # Phase 35 (D-02): bump the maintained `completed` counter on each COMPLETE outcome
    # (Worker constructor kwarg, not a register_* call).
    "after_process": increment_completed,
    # quick-260707-dh1: register ONLY this lane's functions (LANE_TASKS[_lane]); all-mode
    # registers all 8. The union of the four lanes' registered names == AGENT_TASKS (mirror
    # contract, asserted in tests/shared/core/test_task_split.py).
    "functions": [_FUNCTIONS_BY_NAME[name] for name in _function_names],
    # Phase 46: NO heartbeat CronJob. The liveness heartbeat runs as an asyncio
    # background task launched in startup (ctx["heartbeat_task"]) so it cannot be
    # starved by a saturated worker_max_jobs dispatch pool. SAQ's Worker treats
    # cron_jobs as optional (defaults to []), so the key is omitted entirely.
    "concurrency": _concurrency,
    "startup": startup,
    "shutdown": shutdown,
}
