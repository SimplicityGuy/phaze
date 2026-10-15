# Runtime setting audit

This is a maintained implementation summary of the integrated configuration design. The live-policy table is current; validation narratives below describe the implementation dispatch and do not claim new production measurements. For operator commands and the full per-field catalog, use [Configuration](configuration.md) and [Configuration inventory](configuration-inventory.md).

The previous first-occurrence reader list is replaced by the [complete configuration inventory](configuration-inventory.md). A name occurrence alone does not establish a consumer or a restart requirement: for example, `debug` controls SQLAlchemy statement echo in `database.py`, whereas a watcher `logger.debug` call does not read that setting.

The Settings model still has **88 fields**. Runtime policy grows from **10 to 42 live keys** (39 model fields plus three native analysis controls); **47 fields** remain restart-only and **2 collections** use the separate live backend-registry path. No consumed setting is removed to satisfy a numerical target. Three inert deployment/example inputs are removed: `UID`, `GID`, and `PHAZE_DEBUG`. The first two were never wired to image users or Compose settings. The unnecessary `PHAZE_DEBUG` development-overlay input is removed because the accepted SQL echo input is `DEBUG`; use `PHAZE_LOG_LEVEL=DEBUG` for ordinary application diagnostics.

The inventory records purpose, defaults and validation, aliases and file-secret variants, roles, real consumers and per-setting timing/disposition. It also inventories nested registry fields, registered provider policy and credential names, direct entrypoint inputs and deployment/third-party knobs. Equal defaults are not proof of duplicate purpose: the ORM and broker URLs, throughput chunks and schema caps, and separate cloud/push/node-loss budgets remain distinct.

`cloud_submit_max_attempts` was initially proposed as live and then retained as restart-only after examining the persisted spent-chain fold. The fold encodes exhaustion by comparing attempts with that ceiling; lowering it can miss the edge and raising it can charge the same chain twice. Safe live changes require persisted per-chain cap identity. Lifetime chain/node-loss ceilings and cooldowns remain live because they compare retained durable counts and timestamps without changing that edge identity.

## Working live policy

Validated file/override values enter the existing atomic RuntimeConfig snapshot. API/controller notify and fallback poll behavior is unchanged; agent HTTP polling now carries booleans, fractions and retry-delay arrays without coercing them to integers. One-shot cloud jobs fetch the same validated layer before work. New operations capture policy together with their existing static credentials/registry identity. Running operations retain their captured policy and resources.

| Key | Activation and operator effect |
|---|---|
| `aborting_reap_slack_seconds` | Grace added to each aborting job's own serialized timeout on the next reap. Changing it never changes job timeouts or progress-only analysis exclusions. |
| `active_reap_slack_seconds` | Grace added to each stranded active job's own serialized timeout on the next reap. Progress-only heartbeat jobs remain excluded. |
| `analysis_intra_op_threads` | TensorFlow intra-op threads in each newly spawned analysis child. More threads can increase CPU use; fewer reduce it. Running children keep their original value. TF_NUM_INTRAOP_THREADS in an agent's environment is a ceiling that live changes cannot exceed. |
| `analysis_omp_threads` | OpenMP threads in each newly spawned analysis child. More threads can increase CPU use; fewer reduce it. Running children keep their original value. OMP_NUM_THREADS in an agent's environment is a ceiling that live changes cannot exceed. |
| `analysis_progress_interval_sec` | Minimum seconds between progress reports for the next analysis job. Final progress always flushes; zero disables throttling. |
| `analysis_stall_timeout_sec` | Time without analysis progress before a child is killed. Raising it tolerates longer silent work; lowering it detects stalls sooner. It applies to the next job, not one already running, and the outer job heartbeat is derived as twice this value. |
| `analysis_tf_batch_size` | TensorFlow inference patches per batch in new analysis children. Smaller batches trade throughput for lower inference memory demand; measured peak RSS is flat below the default 32. Running children keep their original batch. Fixed-batch graphs retain their required size. An agent rejects live values above its deployment baseline (32 when unset); raising that ceiling requires deployment configuration and restart. The API host does not judge agent memory ceilings. |
| `cloud_budget_cooldown_days` | Cloud cooldown checked at the next routing decision. Durable chain exhaustion timestamps remain intact; zero disables cooldown. |
| `cloud_budget_max_chains` | Lifetime cloud-chain ceiling checked at the next routing decision. Spent chains remain recorded; zero disables the ceiling. Local remains eligible. |
| `cloud_budget_max_node_loss` | Lifetime node-loss ceiling checked at the next routing decision. Spent redrives remain recorded; zero disables the ceiling. |
| `cloud_node_loss_max_redrives` | Node-loss redrive ceiling at the next failure decision. Existing charges are retained. |
| `cloud_redrive_backoff_sec` | JSON array of seconds before successive charged redrives. The last value repeats. New redrive decisions use it; already scheduled waits retain their deadline. |
| `cloud_route_threshold_sec` | Duration threshold used when choosing local versus cloud analysis. Raising it keeps more files on the local path; lowering it offers more files to cloud capacity. The next routing decision reads the new value. |
| `cloud_spill_to_local_after_seconds` | Waiting seconds before full cloud capacity permits local spill at the next routing decision. Existing waiting timestamps remain intact. |
| `convention_date_fallback_enabled` | Include learned date conventions in the next proposal batch. Approval still gates filesystem changes. |
| `convention_date_min_purity` | Minimum supporting share for a learned convention in the next proposal batch. One requires unanimity. |
| `convention_date_min_supporting` | Minimum supporting files for a learned convention in the next proposal batch. |
| `discogs_match_concurrency` | Matching concurrency in the next Discogs task. Running task semaphores drain normally. |
| `lane_analyze_concurrency` | Analyze-lane job limit. Raising it admits more analysis jobs; lowering it drains by attrition. Effective lane capacity is also capped by worker_max_jobs; worker_process_pool_size separately caps simultaneous analysis children. Applies on the agent's next config poll. |
| `lane_io_concurrency` | I/O-lane job limit for uploads and pushes. Raising it admits more transfers; lowering it lets current transfers finish. worker_max_jobs is a separate upper bound. Applies on the agent's next config poll. |
| `lane_meta_concurrency` | Meta-lane job limit for scans, extraction, and execution. Raising it admits more jobs; lowering it lets current jobs finish. worker_max_jobs is a separate upper bound. Applies on the agent's next config poll. |
| `llm_batch_size` | Proposal items per batch at the next Generate action. Already queued batches keep their item lists. |
| `llm_max_companion_chars` | Companion context character cap for the next proposal batch. More characters supply more context and tokens. |
| `llm_max_rpm` | LLM request ceiling per minute. Waiting callers recheck after each wait; the same Redis window and consumed requests remain intact. |
| `llm_model` | Model for the next proposal batch. Already running calls and their parsers retain their model. |
| `log_level` | Controls log detail across API, control worker, and agents. DEBUG emits more detail; ERROR or CRITICAL emits less. The new level applies immediately on each process after its config update. |
| `push_connect_timeout_sec` | SSH connection seconds for the next push. Running handshakes retain their limit. |
| `push_max_attempts` | Push redrive ceiling at the next failure decision. Existing attempt counters are retained. |
| `push_timeout_sec` | I/O silence timeout for the next transfer. Live values cannot exceed the 600-second policy used by already queued multipart jobs. Running transfers retain their captured timeout. |
| `s3_client_timeout_sec` | Connect and read seconds in newly constructed S3 clients. An active SDK operation retains its client and timeout. |
| `s3_multipart_part_size_bytes` | Part size for the next staged upload. Existing upload manifests and part boundaries remain intact. |
| `s3_presign_get_ttl_sec` | Lifetime of newly signed download URLs. Existing signatures retain their original lifetime. |
| `s3_presign_put_ttl_sec` | Lifetime of newly signed upload URLs. Existing signatures remain valid for their original lifetime; upload job safety minimums still apply. |
| `scan_chunk_size` | Files per chunk for the next scan. The server's separate chunk ceiling always clamps this value; running scans retain their chunk size. |
| `scan_stall_seconds` | No-progress seconds before the next scan reap considers a running scan stalled. Progress timestamps are retained. |
| `watcher_max_pending_seconds` | Maximum pending age before a newly discovered path receives its one grace extension. The standalone watcher adopts policy on its next bounded poll; existing pending entries retain their captured cap and grace state. |
| `watcher_settle_seconds` | Quiet seconds required before a newly discovered path is posted. The standalone watcher adopts policy on its next bounded poll; existing pending files keep their captured settle/grace policy and lineage. |
| `worker_job_timeout` | Default timeout for newly enqueued finite-duration jobs. Existing job limits remain serialized; exhaustive analysis always retains its zero wall-clock timeout. |
| `worker_keep_result` | Retention seconds on newly enqueued job results. Existing jobs retain their serialized retention. |
| `worker_max_jobs` | Maximum concurrent SAQ jobs per worker. Raising it starts more job loops immediately; lowering it lets running jobs finish. It caps each lane's effective concurrency, even when that lane's own limit is higher. |
| `worker_max_retries` | Default retry budget on newly enqueued jobs. Explicit job limits and the analysis retry ceiling remain in force. |
| `worker_process_pool_size` | Maximum simultaneous analysis child processes per agent. Raising it releases waiting analysis calls; lowering it waits for current children to finish. It also resizes telemetry worker slots and remains separate from SAQ job limits. Applies on the agent's next config poll. |

## Evidence and safety

Tests exercise every live key through real environment/file/override precedence and withdrawal. Consumer tests exercise new serialized SAQ defaults while preserving `process_file` timeout=0 and retry ceiling; per-operation S3 client timeouts with last-good rejection; persistent cloud budget thresholds without resetting spent counters; live scan chunking under the fixed request-schema cap; typed agent poll recovery; real child-process inference batch capture (old 32, new 8, fixed graph 64); transfer timeout/batch ceiling rejection before swap; concurrent proposal model/parser capture; and rate-limit wakeup against the same Redis request window and TTL.

The retained resource controls have concrete restart reasons in the inventory. Credentials/connection authority, schema or router topology, observer replacement, native-model identity and destructive bucket/recovery deadlines are not represented as successful scalar reloads. Existing progress-only liveness and exhaustive analysis windows remain intact. No production/archive experiments were used.

## Removed inert Settings fields

| Field | Finding |
|---|---|
| `analysis_fine_window_sec`, `analysis_coarse_window_sec`, `analysis_fine_min_sec` | No reader outside their AgentSettings declarations; analysis uses fixed window defaults in `src/phaze/services/analysis.py`. The three accepted env overrides did not affect analysis. |
| `worker_health_check_interval`, `output_path` | No reader outside BaseSettings declarations. |
| `api_host`, `api_port` | No application reader of the Settings fields. The API entrypoint binds its container port directly; `API_PORT` remains supported by `docker-compose.yml` for the host port. The separate `PHAZE_API_HOST` environment variable remains supported by `src/phaze/entrypoint.py` as the certificate common name. |
| `push_ssh_host` | Retired global push destination; `src/phaze/tasks/push.py` uses the per-file destination payload instead. `PHAZE_PUSH_SSH_HOST` had no effect. |
| `api_tls_sans` | No reader of the Settings field. `PHAZE_API_TLS_SANS` remains supported because `src/phaze/entrypoint.py` reads it directly when generating a certificate. |

The removals affect Settings model introspection and the read-only Runtime Config inventory, not the still-consumed environment variables.

## Shared agent policy boundary

The bounded authenticated policy fetch now belongs to `runtime_config_agent.py`, independently of agent liveness. Heartbeat polling retains its existing deadline, retry/digest state and compatibility seams. One-shot jobs call the same poller without importing heartbeat/worker machinery or touching last-seen timestamps. The baseline had green typed-poll tests and a failing one-shot import guard after the initial wiring; the extraction preserves polling behavior and fixes that boundary. Targeted polling and import-boundary checks precede the configured hive gate.

The standalone watcher polls policy every 30 seconds and supports SIGHUP for its local file layer. Each newly pending file captures its settle and maximum-pending timing together; existing entries retain their captured policy, grace flag and move lineage. Polling runs independently of discovery and is cancelled/drained before the HTTP client closes. Observer backend and emitter interval remain restart-only.

New clients request the typed policy envelope with `X-Phaze-Config-Version: 2`. Unversioned clients receive the original ten scalar keys; their int/string parser remains operational even when persisted overrides contain booleans, fractions or retry arrays. This is tested through the real authenticated endpoint with a legacy response model.


The shared transfer silence timeout has a live ceiling of 600 seconds, derived from the S3 producer's frozen per-part policy. An SSH-only increase can fit its single-transfer margin yet exceed the serialized sum for a multipart upload. Reload validation therefore checks both consumers before swapping the snapshot; it never resizes already queued jobs. Non-default startup settings retain their existing validation: a startup value above 600 may suit SSH-only use but is incompatible with default multipart envelopes unless producer and agent budgets are coordinated. Clearing an override restores that startup authority rather than changing its validation.
