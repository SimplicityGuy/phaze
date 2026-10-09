# Runtime setting audit

Every currently displayed Settings key is listed below. A reader is the first source file containing the field name outside the declarations in `config_base.py`, `config_control.py`, or `config_agent.py`; the full tree was searched to confirm use. This is a static reader inventory, with source-specific behavior described in [configuration.md](configuration.md). All retained keys have a runtime source reference.

## Live controls

| Key | Verdict | Reader |
|---|---|---|
| `analysis_intra_op_threads` | Keep: live control | `src/phaze/tasks/functions.py` |
| `analysis_omp_threads` | Keep: live control | `src/phaze/tasks/functions.py` |
| `analysis_stall_timeout_sec` | Keep: live control | `src/phaze/job_runner.py` |
| `cloud_route_threshold_sec` | Keep: live control | `src/phaze/routers/pipeline/analysis.py` |
| `lane_analyze_concurrency` | Keep: live control | `src/phaze/services/enqueue_router.py` |
| `lane_io_concurrency` | Keep: live control | `src/phaze/services/enqueue_router.py` |
| `lane_meta_concurrency` | Keep: live control | `src/phaze/services/enqueue_router.py` |
| `log_level` | Keep: live control | `src/phaze/logging_config.py` |
| `worker_max_jobs` | Keep: live control | `src/phaze/tasks/_shared/live_worker.py` |
| `worker_process_pool_size` | Keep: live control | `src/phaze/tasks/agent_worker.py` |

## Restart-only settings

| Key | Verdict | Reader |
|---|---|---|
| `aborting_reap_slack_seconds` | Keep: read at startup | `src/phaze/tasks/aborting_reaper.py` |
| `active_reap_slack_seconds` | Keep: read at startup | `src/phaze/services/queue_introspection.py` |
| `agent_api_url` | Keep: read at startup | `src/phaze/agent_watcher/__main__.py` |
| `agent_ca_file` | Keep: read at startup | `src/phaze/tasks/_shared/agent_bootstrap.py` |
| `agent_env` | Keep: read at startup | `src/phaze/config_agent.py` |
| `agent_file_chunk_max` | Keep: read at startup | `src/phaze/schemas/agent_files.py` |
| `agent_heartbeat_enabled` | Keep: read at startup | `src/phaze/tasks/agent_worker.py` |
| `agent_token` | Keep: read at startup | `src/phaze/agent_watcher/__main__.py` |
| `agent_token_prefix` | Keep: read at startup | `src/phaze/services/agent_bootstrap.py` |
| `analysis_progress_interval_sec` | Keep: read at startup | `src/phaze/job_runner.py` |
| `anthropic_api_key` | Keep: read at startup | `src/phaze/tasks/controller.py` |
| `auto_migrate` | Keep: read at startup | `src/phaze/database.py` |
| `cloud_budget_cooldown_days` | Keep: read at startup | `src/phaze/services/cloud_budget.py` |
| `cloud_budget_max_chains` | Keep: read at startup | `src/phaze/services/cloud_budget.py` |
| `cloud_budget_max_node_loss` | Keep: read at startup | `src/phaze/services/cloud_budget.py` |
| `cloud_node_loss_max_redrives` | Keep: read at startup | `src/phaze/tasks/reconcile_cloud_jobs.py` |
| `cloud_redrive_backoff_sec` | Keep: read at startup | `src/phaze/tasks/reconcile_cloud_jobs.py` |
| `cloud_scratch_dir` | Keep: read at startup | `src/phaze/tasks/agent_worker.py` |
| `cloud_spill_to_local_after_seconds` | Keep: read at startup | `src/phaze/services/backend_selection.py` |
| `cloud_submit_max_attempts` | Keep: read at startup | `src/phaze/routers/agent_push.py` |
| `cloud_submitted_stale_after_sec` | Keep: read at startup | `src/phaze/services/backends/compute_agent.py` |
| `cloud_uploaded_stale_after_sec` | Keep: read at startup | `src/phaze/services/backends/kueue.py` |
| `cloud_uploading_stale_after_sec` | Keep: read at startup | `src/phaze/services/backends/kueue.py` |
| `control_env` | Keep: read at startup | `src/phaze/config_control.py` |
| `convention_date_fallback_enabled` | Keep: read at startup | `src/phaze/tasks/proposal.py` |
| `convention_date_min_purity` | Keep: read at startup | `src/phaze/tasks/proposal.py` |
| `convention_date_min_supporting` | Keep: read at startup | `src/phaze/tasks/proposal.py` |
| `database_url` | Keep: read at startup | `src/phaze/database.py` |
| `db_max_overflow` | Keep: read at startup | `src/phaze/database.py` |
| `db_pool_pre_ping` | Keep: read at startup | `src/phaze/database.py` |
| `db_pool_recycle` | Keep: read at startup | `src/phaze/database.py` |
| `db_pool_size` | Keep: read at startup | `src/phaze/database.py` |
| `db_pool_timeout` | Keep: read at startup | `src/phaze/database.py` |
| `debug` | Keep: read at startup | `src/phaze/agent_watcher/__main__.py` |
| `dev_agent_token` | Keep: read at startup | `src/phaze/services/agent_bootstrap.py` |
| `dev_seed_agent` | Keep: read at startup | `src/phaze/services/agent_bootstrap.py` |
| `discogs_match_concurrency` | Keep: read at startup | `src/phaze/tasks/discogs.py` |
| `discogsography_url` | Keep: read at startup | `src/phaze/tasks/controller.py` |
| `dispatch_queue_max_size` | Keep: read at startup | `src/phaze/services/agent_task_router.py` |
| `dispatch_queue_min_size` | Keep: read at startup | `src/phaze/services/agent_task_router.py` |
| `enable_saq_ui` | Keep: read at startup | `src/phaze/main.py` |
| `kind` | Keep: read at startup | `src/phaze/cli/__init__.py` |
| `llm_batch_size` | Keep: read at startup | `src/phaze/routers/pipeline/dashboard_stats.py` |
| `llm_max_companion_chars` | Keep: read at startup | `src/phaze/tasks/proposal.py` |
| `llm_max_rpm` | Keep: read at startup | `src/phaze/tasks/controller.py` |
| `llm_model` | Keep: read at startup | `src/phaze/routers/shell/stage_context.py` |
| `log_json` | Keep: read at startup | `src/phaze/main.py` |
| `models_path` | Keep: read at startup | `src/phaze/job_runner.py` |
| `openai_api_key` | Keep: read at startup | `src/phaze/tasks/controller.py` |
| `push_connect_timeout_sec` | Keep: read at startup | `src/phaze/tasks/push.py` |
| `push_known_hosts` | Keep: read at startup | `src/phaze/tasks/push.py` |
| `push_max_attempts` | Keep: read at startup | `src/phaze/routers/agent_push.py` |
| `push_ssh_key` | Keep: read at startup | `src/phaze/tasks/push.py` |
| `push_ssh_user` | Keep: read at startup | `src/phaze/tasks/push.py` |
| `push_timeout_sec` | Keep: read at startup | `src/phaze/tasks/agent_worker.py` |
| `queue_url` | Keep: read at startup | `src/phaze/cli/__init__.py` |
| `redis_password` | Keep: read at startup | `src/phaze/config_redis.py` |
| `redis_url` | Keep: read at startup | `src/phaze/cli/__init__.py` |
| `runtime_config_dir` | Keep: read at startup | `src/phaze/runtime_config_triggers.py` |
| `runtime_config_watch_debounce_seconds` | Keep: read at startup | `src/phaze/runtime_config_triggers.py` |
| `runtime_config_watch_poll_interval_seconds` | Keep: read at startup | `src/phaze/runtime_config_triggers.py` |
| `runtime_config_watch_polling` | Keep: read at startup | `src/phaze/runtime_config_triggers.py` |
| `s3_client_timeout_sec` | Keep: read at startup | `src/phaze/services/s3_staging.py` |
| `s3_lifecycle_ttl_days` | Keep: read at startup | `src/phaze/services/s3_staging.py` |
| `s3_multipart_part_size_bytes` | Keep: read at startup | `src/phaze/services/cloud_staging.py` |
| `s3_presign_get_ttl_sec` | Keep: read at startup | `src/phaze/services/s3_staging.py` |
| `s3_presign_put_ttl_sec` | Keep: read at startup | `src/phaze/services/cloud_staging.py` |
| `scan_chunk_size` | Keep: read at startup | `src/phaze/tasks/scan.py` |
| `scan_path` | Keep: read at startup | `src/phaze/routers/agent_orphan_companions.py` |
| `scan_roots` | Keep: read at startup | `src/phaze/agent_watcher/__main__.py` |
| `scan_stall_seconds` | Keep: read at startup | `src/phaze/routers/pipeline_scans.py` |
| `watcher_max_pending_seconds` | Keep: read at startup | `src/phaze/agent_watcher/__main__.py` |
| `watcher_polling_mode` | Keep: read at startup | `src/phaze/agent_watcher/__main__.py` |
| `watcher_settle_seconds` | Keep: read at startup | `src/phaze/agent_watcher/__main__.py` |
| `watcher_sweep_interval_seconds` | Keep: read at startup | `src/phaze/agent_watcher/__main__.py` |
| `worker_job_timeout` | Keep: read at startup | `src/phaze/tasks/_shared/queue_defaults.py` |
| `worker_keep_result` | Keep: read at startup | `src/phaze/tasks/_shared/queue_defaults.py` |
| `worker_max_retries` | Keep: read at startup | `src/phaze/tasks/_shared/queue_defaults.py` |

## Removed inert Settings fields

| Field | Finding |
|---|---|
| `analysis_fine_window_sec`, `analysis_coarse_window_sec`, `analysis_fine_min_sec` | No reader outside their AgentSettings declarations; analysis uses fixed window defaults in `src/phaze/services/analysis.py`. The three accepted env overrides did not affect analysis. |
| `worker_health_check_interval`, `output_path` | No reader outside BaseSettings declarations. |
| `api_host`, `api_port` | No application reader of the Settings fields. The API entrypoint binds its container port directly; `API_PORT` remains supported by `docker-compose.yml` for the host port. The separate `PHAZE_API_HOST` environment variable remains supported by `src/phaze/entrypoint.py` as the certificate common name. |
| `push_ssh_host` | Retired global push destination; `src/phaze/tasks/push.py` uses the per-file destination payload instead. `PHAZE_PUSH_SSH_HOST` had no effect. |
| `api_tls_sans` | No reader of the Settings field. `PHAZE_API_TLS_SANS` remains supported because `src/phaze/entrypoint.py` reads it directly when generating a certificate. |

The removals affect Settings model introspection and the read-only Runtime Config inventory, not the still-consumed environment variables.
