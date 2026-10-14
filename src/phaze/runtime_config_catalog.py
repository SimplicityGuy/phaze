"""Operator copy for the Runtime Config pane. Never include resolved restart-only values."""

from phaze.config import AgentSettings, ControlSettings
from phaze.runtime_config import RELOADABLE_KEYS, RESTART_ONLY_KEYS


# Each live control explains direction, activation, and the important separate limits.
LIVE_HELP: dict[str, str] = {
    "log_level": "Controls log detail across API, control worker, and agents. DEBUG emits more detail; ERROR or CRITICAL emits less. The new level applies immediately on each process after its config update.",
    "worker_max_jobs": "Maximum concurrent SAQ jobs per worker. Raising it starts more job loops immediately; lowering it lets running jobs finish. It caps each lane's effective concurrency, even when that lane's own limit is higher.",
    "lane_analyze_concurrency": "Analyze-lane job limit. Raising it admits more analysis jobs; lowering it drains by attrition. Effective lane capacity is also capped by worker_max_jobs; worker_process_pool_size separately caps simultaneous analysis children. Applies on the agent's next config poll.",
    "lane_meta_concurrency": "Meta-lane job limit for scans, extraction, and execution. Raising it admits more jobs; lowering it lets current jobs finish. worker_max_jobs is a separate upper bound. Applies on the agent's next config poll.",
    "lane_io_concurrency": "I/O-lane job limit for uploads and pushes. Raising it admits more transfers; lowering it lets current transfers finish. worker_max_jobs is a separate upper bound. Applies on the agent's next config poll.",
    "worker_process_pool_size": "Maximum simultaneous analysis child processes per agent. Raising it releases waiting analysis calls; lowering it waits for current children to finish. It also resizes telemetry worker slots and remains separate from SAQ job limits. Applies on the agent's next config poll.",
    "analysis_intra_op_threads": "TensorFlow intra-op threads in each newly spawned analysis child. More threads can increase CPU use; fewer reduce it. Running children keep their original value. TF_NUM_INTRAOP_THREADS in an agent's environment is a ceiling that live changes cannot exceed.",
    "analysis_omp_threads": "OpenMP threads in each newly spawned analysis child. More threads can increase CPU use; fewer reduce it. Running children keep their original value. OMP_NUM_THREADS in an agent's environment is a ceiling that live changes cannot exceed.",
    "analysis_stall_timeout_sec": "Time without analysis progress before a child is killed. Raising it tolerates longer silent work; lowering it detects stalls sooner. It applies to the next job, not one already running, and the outer job heartbeat is derived as twice this value.",
    "cloud_route_threshold_sec": "Duration threshold used when choosing local versus cloud analysis. Raising it keeps more files on the local path; lowering it offers more files to cloud capacity. The next routing decision reads the new value.",
}

LIVE_HELP.update(
    {
        "worker_job_timeout": "Default timeout for newly enqueued finite-duration jobs. Existing job limits remain serialized; exhaustive analysis always retains its zero wall-clock timeout.",
        "worker_max_retries": "Default retry budget on newly enqueued jobs. Explicit job limits and the analysis retry ceiling remain in force.",
        "worker_keep_result": "Retention seconds on newly enqueued jobs; zero retains results indefinitely and negative values delete them immediately. Existing jobs retain their serialized retention.",
        "aborting_reap_slack_seconds": "Grace added to each aborting job's own serialized timeout on the next reap. Changing it never changes job timeouts or progress-only analysis exclusions.",
        "active_reap_slack_seconds": "Grace added to each stranded active job's own serialized timeout on the next reap. Progress-only heartbeat jobs remain excluded.",
        "scan_stall_seconds": "No-progress seconds before the next scan reap considers a running scan stalled. Progress timestamps are retained.",
        "scan_chunk_size": "Files per chunk for the next scan. The server's separate chunk ceiling always clamps this value; running scans retain their chunk size.",
        "analysis_progress_interval_sec": "Minimum seconds between progress reports for the next analysis job. Final progress always flushes; zero disables throttling.",
        "discogs_match_concurrency": "Matching concurrency in the next Discogs task. Running task semaphores drain normally.",
        "llm_model": "Model for the next proposal batch. Already running calls and their parsers retain their model.",
        "llm_max_rpm": "LLM request ceiling per minute. Waiting callers recheck after each wait; the same Redis window and consumed requests remain intact.",
        "llm_batch_size": "Proposal items per batch at the next Generate action. Already queued batches keep their item lists.",
        "llm_max_companion_chars": "Companion context character cap for the next proposal batch. More characters supply more context and tokens.",
        "convention_date_fallback_enabled": "Include learned date conventions in the next proposal batch. Approval still gates filesystem changes.",
        "convention_date_min_supporting": "Minimum supporting files for a learned convention in the next proposal batch.",
        "convention_date_min_purity": "Minimum supporting share for a learned convention in the next proposal batch. One requires unanimity.",
        "push_max_attempts": "Push redrive ceiling at the next failure decision. Existing attempt counters are retained.",
        "cloud_node_loss_max_redrives": "Node-loss redrive ceiling at the next failure decision. Existing charges are retained.",
        "cloud_redrive_backoff_sec": "JSON array of seconds before successive charged redrives. The last value repeats. New redrive decisions use it; already scheduled waits retain their deadline.",
        "cloud_budget_cooldown_days": "Cloud cooldown checked at the next routing decision. Durable chain exhaustion timestamps remain intact; zero disables cooldown.",
        "cloud_budget_max_chains": "Lifetime cloud-chain ceiling checked at the next routing decision. Spent chains remain recorded; zero disables the ceiling. Local remains eligible.",
        "cloud_budget_max_node_loss": "Lifetime node-loss ceiling checked at the next routing decision. Spent redrives remain recorded; zero disables the ceiling.",
        "cloud_spill_to_local_after_seconds": "Waiting seconds before full cloud capacity permits local spill at the next routing decision. Existing waiting timestamps remain intact.",
        "s3_presign_put_ttl_sec": "Lifetime of newly signed upload URLs. Existing signatures remain valid for their original lifetime; upload job safety minimums still apply.",
        "s3_presign_get_ttl_sec": "Lifetime of newly signed download URLs. Existing signatures retain their original lifetime.",
        "s3_multipart_part_size_bytes": "Part size for the next staged upload. Existing upload manifests and part boundaries remain intact.",
        "s3_client_timeout_sec": "Connect and read seconds in newly constructed S3 clients. An active SDK operation retains its client and timeout.",
        "push_timeout_sec": "I/O silence timeout for the next transfer. Live values cannot exceed the 600-second policy used by already queued multipart jobs. Running transfers retain their captured timeout.",
        "push_connect_timeout_sec": "SSH connection seconds for the next push. Running handshakes retain their limit.",
    }
)

LIVE_HELP["analysis_tf_batch_size"] = (
    "TensorFlow inference patches per batch in new analysis children. Smaller batches trade throughput for lower inference memory demand; measured peak RSS is flat below the default 32. Running children keep their original batch. Fixed-batch graphs retain their required size. An agent rejects live values above its deployment baseline (32 when unset); raising that ceiling requires deployment configuration and restart. The API host does not judge agent memory ceilings."
)

LIVE_HELP["watcher_settle_seconds"] = (
    "Quiet seconds required before a newly discovered path is posted. The standalone watcher adopts policy on its next bounded poll; existing pending files keep their captured settle/grace policy and lineage."
)
LIVE_HELP["watcher_max_pending_seconds"] = (
    "Maximum pending age before a newly discovered path receives its one grace extension. The standalone watcher adopts policy on its next bounded poll; existing pending entries retain their captured cap and grace state."
)

# Fields without a useful Pydantic description. These describe the effect without showing a
# configured value, token, DSN, or filesystem path from the running installation.
RESTART_HELP: dict[str, str] = {
    "agent_api_url": "API endpoint contacted by an agent. Changing it redirects that agent's control and heartbeat requests after restart.",
    "agent_file_chunk_max": "Upper limit on files in one agent API chunk. Raising it makes larger requests; lowering it makes smaller, more numerous requests.",
    "agent_token": "Bearer credential used by an agent to authenticate with the API. Changing it requires matching server registration and an agent restart.",  # nosec B105
    "agent_token_prefix": "Prefix expected for generated agent tokens. Changing it affects token generation and validation on restart.",  # nosec B105
    "anthropic_api_key": "Credential for Anthropic LLM calls. Changing it selects a different credential for requests after restart.",
    "database_url": "Postgres connection for application data. Changing it points the process at another database after restart.",
    "debug": "Enable SQLAlchemy SQL statement echo on new database connections after restart. This is separate from application log detail.",
    "discogs_match_concurrency": "Number of simultaneous Discogs matching tasks. Raising it increases parallel requests; lowering it reduces load.",
    "discogsography_url": "Discogsography service endpoint. Changing it redirects lookup traffic after restart.",
    "llm_batch_size": "Number of proposal items sent in one LLM batch. Raising it reduces call count but increases request size; lowering it does the reverse.",
    "llm_max_companion_chars": "Maximum companion-text characters included in LLM context. Raising it supplies more context and tokens; lowering it trims both.",
    "llm_max_rpm": "Maximum LLM requests per minute. Raising it allows faster calls; lowering it reduces provider load.",
    "llm_model": "Model used for AI proposals. Changing it alters proposal behavior and provider cost after restart.",
    "models_path": "Directory containing audio analysis models. Changing it makes agents load models from another directory after restart.",
    "openai_api_key": "Credential for OpenAI LLM calls. Changing it selects a different credential for requests after restart.",
    "redis_url": "Redis connection for cache and rate limits. Changing it points the process at another Redis instance after restart.",
    "scan_path": "Default directory scanned for media. Changing it moves the default scan root after restart.",
    "worker_job_timeout": "Default SAQ job timeout in seconds. Raising it allows longer jobs; lowering it aborts stalled work sooner after restart.",
    "worker_keep_result": "Seconds SAQ retains completed job results. Raising it keeps results longer and uses more storage; lowering it expires them sooner.",
    "worker_max_retries": "Maximum SAQ attempts for a failing job. Raising it gives transient failures more chances; lowering it fails sooner.",
}


def _restart_group(key: str) -> str:
    if key.startswith(("cloud_", "push_", "s3_")):
        return "Cloud and transfer"
    if key.startswith(("tracklist_", "scraper_", "discogs_", "convention_", "llm_")) or key in {
        "anthropic_api_key",
        "openai_api_key",
        "discogsography_url",
    }:
        return "Metadata and proposals"
    if key.startswith(("db_", "dispatch_queue_", "redis_")) or key in {"database_url", "queue_url"}:
        return "Data connections"
    if key.startswith(("agent_", "watcher_", "scan_", "analysis_", "models_", "kind")):
        return "Agents and analysis"
    return "Process and operations"


def restart_only_groups() -> list[dict[str, object]]:
    """Build a complete, ordered name-and-description catalog without reading secret values."""
    fields = ControlSettings.model_fields | AgentSettings.model_fields
    groups: dict[str, list[dict[str, str]]] = {}
    for key in sorted(RESTART_ONLY_KEYS):
        description = RESTART_HELP.get(key) or fields[key].description
        if not description:
            raise AssertionError(f"Missing Runtime Config description: {key}")
        groups.setdefault(_restart_group(key), []).append({"key": key, "description": description})
    return [{"name": name, "items": items} for name, items in groups.items()]


if LIVE_HELP.keys() != RELOADABLE_KEYS:
    raise RuntimeError("Runtime Config help must cover every reloadable key")
