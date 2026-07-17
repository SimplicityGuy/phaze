# Test Bucket Mapping (Phase 63-02)

The single source of truth for the 9 bucket **names** is `tests/buckets.json`.
This file records the explicit file->bucket assignment for the reorg, plus the
pre-reorg baseline the reorg must preserve (CI-03).

> **Note — "buckets" here means CI *test* partitions, not S3 buckets.** The 9
> buckets below are the parallel-CI test partitions from Phase 63; they are
> unrelated to the S3 staging buckets. Phase 67 (REG-05) introduced the S3
> **staging-bucket registry** as an array-of-tables (`[[buckets]]` with an `id`,
> a `scope` of `shared` | `cluster-specific`, an `endpoint`, and per-bucket
> credentials) inside `backends.toml`. That flat single-global S3 config (the old
> `PHAZE_S3_*` env vars) was removed with no shim; the registry surface is
> documented in `docs/configuration.md`.
>
> **Phase 67 removal:** the three config test files
> `tests/shared/config/test_cloud_target.py`, `test_kube_settings.py`, and
> `test_s3_settings.py` (rows below, retained as the Phase-63 reorg record) were
> DELETED in Phase 67 because they asserted the now-removed flat cloud fields.

## Pre-reorg baseline (acceptance target for the reorg)

| Metric | Value | How captured |
|--------|-------|--------------|
| Full-suite passed | **2566 passed** | `just integration-test` (ephemeral PG 5433 + Redis 6380), 0 failed |
| Combined coverage | **96.89%** | `coverage report` over a full covered `--cov=phaze` run |
| Collected test files | 213 | `find tests \( -name 'test_*.py' -o -name '*_test.py' \)` |

The post-reorg full suite MUST report the same **2566 passed** (no test lost, none double-counted).

## Buckets

| bucket | file count |
|--------|-----------|
| discovery | 19 |
| metadata | 3 |
| fingerprint | 6 |
| analyze | 29 |
| identify | 12 |
| review | 24 |
| agents | 38 |
| integration | 21 |
| shared | 61 |

> **Post-reorg addition (phaze-uciu.1):** a tenth bucket `services` was added to
> `tests/buckets.json` to home `tests/services/` — regression tests for the top-level
> `services/` FastAPI sidecars (audfprint, panako), which live OUTSIDE `src/phaze` and so
> were previously outside every bucket and the whole pytest testpath. It rides the same
> matrix-shard → coverage-combine → single Codecov upload flow as every other bucket
> (`just test-bucket services`, `--cov=phaze`); the sidecars' own code stays out of the
> `source=["phaze"]` coverage gates by design.

## File -> bucket assignment

Destination convention: `tests/<bucket>/<layer>/<basename>`, where `<layer>` is the
file's current immediate parent dir name (`test_` prefix stripped) or `core` for files
currently at `tests/` root. Existing `tests/integration/*` files already sit in a bucket
and stay put. Migration tests move to `tests/integration/test_migrations/` so both the
`integration` and `test_migrations` path segments survive for the conftest auto-marker.
The root helper `tests/_queue_fakes_test.py` (a `*_test.py` file, 4 real tests) is renamed
to `queue_fakes_test.py` on move so it reads as the test file it is.

| current path | bucket | destination |
|--------------|--------|-------------|
| tests/test_agent_watcher/test_debouncer.py | discovery | tests/discovery/agent_watcher/test_debouncer.py |
| tests/test_agent_watcher/test_main.py | discovery | tests/discovery/agent_watcher/test_main.py |
| tests/test_agent_watcher/test_observer.py | discovery | tests/discovery/agent_watcher/test_observer.py |
| tests/test_queue_fakes_dedup.py | discovery | tests/discovery/core/test_queue_fakes_dedup.py |
| tests/test_routers/test_agent_files.py | discovery | tests/discovery/routers/test_agent_files.py |
| tests/test_routers/test_agent_files_batch_id.py | discovery | tests/discovery/routers/test_agent_files_batch_id.py |
| tests/test_routers/test_agent_scan_batches.py | discovery | tests/discovery/routers/test_agent_scan_batches.py |
| tests/test_routers/test_companion.py | discovery | tests/discovery/routers/test_companion.py |
| tests/test_routers/test_scan.py | discovery | tests/discovery/routers/test_scan.py |
| tests/test_schemas/test_agent_files.py | discovery | tests/discovery/schemas/test_agent_files.py |
| tests/test_schemas/test_agent_scan_batches.py | discovery | tests/discovery/schemas/test_agent_scan_batches.py |
| tests/test_services/test_collision.py | discovery | tests/discovery/services/test_collision.py |
| tests/test_services/test_companion.py | discovery | tests/discovery/services/test_companion.py |
| tests/test_services/test_dedup.py | discovery | tests/discovery/services/test_dedup.py |
| tests/test_services/test_ingestion.py | discovery | tests/discovery/services/test_ingestion.py |
| tests/test_services/test_scan_deletion.py | discovery | tests/discovery/services/test_scan_deletion.py |
| tests/test_tasks/test_scan.py | discovery | tests/discovery/tasks/test_scan.py |
| tests/test_tasks/test_scan_directory.py | discovery | tests/discovery/tasks/test_scan_directory.py |
| tests/test_tasks/test_scan_reaper.py | discovery | tests/discovery/tasks/test_scan_reaper.py |
| tests/test_routers/test_agent_metadata.py | metadata | tests/metadata/routers/test_agent_metadata.py |
| tests/test_services/test_metadata.py | metadata | tests/metadata/services/test_metadata.py |
| tests/test_tasks/test_metadata_extraction.py | metadata | tests/metadata/tasks/test_metadata_extraction.py |
| tests/test_models/test_fingerprint.py | fingerprint | tests/fingerprint/models/test_fingerprint.py |
| tests/test_routers/test_agent_fingerprint.py | fingerprint | tests/fingerprint/routers/test_agent_fingerprint.py |
| tests/test_routers/test_pipeline_fingerprint.py | fingerprint | tests/fingerprint/routers/test_pipeline_fingerprint.py |
| tests/test_services/test_fingerprint.py | fingerprint | tests/fingerprint/services/test_fingerprint.py |
| tests/test_services/test_fingerprint_locality.py | fingerprint | tests/fingerprint/services/test_fingerprint_locality.py |
| tests/test_tasks/test_fingerprint.py | fingerprint | tests/fingerprint/tasks/test_fingerprint.py |
| tests/test_deterministic_key.py | analyze | tests/analyze/core/test_deterministic_key.py |
| tests/test_job_runner.py | analyze | tests/analyze/core/test_job_runner.py |
| tests/test_models/test_analysis_window.py | analyze | tests/analyze/models/test_analysis_window.py |
| tests/test_models/test_scheduling_ledger.py | analyze | tests/analyze/models/test_scheduling_ledger.py |
| tests/test_pipeline_counters.py | analyze | tests/analyze/core/test_pipeline_counters.py |
| tests/test_process_file_scratch.py | analyze | tests/analyze/core/test_process_file_scratch.py |
| tests/test_push_pipeline.py | analyze | tests/analyze/core/test_push_pipeline.py |
| tests/test_queue_factory.py | analyze | tests/analyze/core/test_queue_factory.py |
| tests/test_queue_fakes.py | analyze | tests/analyze/core/test_queue_fakes.py |
| tests/test_reenqueue.py | analyze | tests/analyze/core/test_reenqueue.py |
| tests/test_routers/test_stage_endpoints.py | analyze | tests/analyze/routers/test_stage_endpoints.py |
| tests/test_services/test_analysis.py | analyze | tests/analyze/services/test_analysis.py |
| tests/test_services/test_analysis_enqueue.py | analyze | tests/analyze/services/test_analysis_enqueue.py |
| tests/test_services/test_analysis_long_file.py | analyze | tests/analyze/services/test_analysis_long_file.py |
| tests/test_services/test_cloud_staging.py | analyze | tests/analyze/services/test_cloud_staging.py |
| tests/test_services/test_kube_staging.py | analyze | tests/analyze/services/test_kube_staging.py |
| tests/test_services/test_s3_staging.py | analyze | tests/analyze/services/test_s3_staging.py |
| tests/test_services/test_scheduling_ledger.py | analyze | tests/analyze/services/test_scheduling_ledger.py |
| tests/test_stage_control.py | analyze | tests/analyze/core/test_stage_control.py |
| tests/test_stage_progress.py | analyze | tests/analyze/core/test_stage_progress.py |
| tests/test_staging_cron.py | analyze | tests/analyze/core/test_staging_cron.py |
| tests/test_tasks/test_controller_reenqueue.py | analyze | tests/analyze/tasks/test_controller_reenqueue.py |
| tests/test_tasks/test_ledger_backfill.py | analyze | tests/analyze/tasks/test_ledger_backfill.py |
| tests/test_tasks/test_pool.py | analyze | tests/analyze/tasks/test_pool.py |
| tests/test_tasks/test_queue_defaults.py | analyze | tests/analyze/tasks/test_queue_defaults.py |
| tests/test_tasks/test_reconcile_cloud_jobs.py | analyze | tests/analyze/tasks/test_reconcile_cloud_jobs.py |
| tests/test_tasks/test_recovery.py | analyze | tests/analyze/tasks/test_recovery.py |
| tests/test_tasks/test_s3_upload.py | analyze | tests/analyze/tasks/test_s3_upload.py |
| tests/test_tasks/test_submit_cloud_job.py | analyze | tests/analyze/tasks/test_submit_cloud_job.py |
| tests/test_models/test_discogs_link.py | identify | tests/identify/models/test_discogs_link.py |
| tests/test_models/test_tracklist.py | identify | tests/identify/models/test_tracklist.py |
| tests/test_routers/test_agent_tracklists.py | identify | tests/identify/routers/test_agent_tracklists.py |
| tests/test_routers/test_search.py | identify | tests/identify/routers/test_search.py |
| tests/test_routers/test_tracklists.py | identify | tests/identify/routers/test_tracklists.py |
| tests/test_schemas/test_agent_tracklists.py | identify | tests/identify/schemas/test_agent_tracklists.py |
| tests/test_services/test_discogs_matcher.py | identify | tests/identify/services/test_discogs_matcher.py |
| tests/test_services/test_search_queries.py | identify | tests/identify/services/test_search_queries.py |
| tests/test_services/test_tracklist_matcher.py | identify | tests/identify/services/test_tracklist_matcher.py |
| tests/test_services/test_tracklist_scraper.py | identify | tests/identify/services/test_tracklist_scraper.py |
| tests/test_tasks/test_discogs.py | identify | tests/identify/tasks/test_discogs.py |
| tests/test_tasks/test_tracklist.py | identify | tests/identify/tasks/test_tracklist.py |
| tests/test_models/test_tag_write_log.py | review | tests/review/models/test_tag_write_log.py |
| tests/test_routers/test_agent_exec_batches.py | review | tests/review/routers/test_agent_exec_batches.py |
| tests/test_routers/test_agent_execution.py | review | tests/review/routers/test_agent_execution.py |
| tests/test_routers/test_agent_proposals.py | review | tests/review/routers/test_agent_proposals.py |
| tests/test_routers/test_cue.py | review | tests/review/routers/test_cue.py |
| tests/test_routers/test_duplicates.py | review | tests/review/routers/test_duplicates.py |
| tests/test_routers/test_execution.py | review | tests/review/routers/test_execution.py |
| tests/test_routers/test_execution_dispatch.py | review | tests/review/routers/test_execution_dispatch.py |
| tests/test_routers/test_execution_helpers.py | review | tests/review/routers/test_execution_helpers.py |
| tests/test_routers/test_proposals.py | review | tests/review/routers/test_proposals.py |
| tests/test_routers/test_summarize_dict_to_string.py | review | tests/review/routers/test_summarize_dict_to_string.py |
| tests/test_routers/test_tags.py | review | tests/review/routers/test_tags.py |
| tests/test_schemas/test_agent_exec_batches.py | review | tests/review/schemas/test_agent_exec_batches.py |
| tests/test_schemas/test_agent_proposals.py | review | tests/review/schemas/test_agent_proposals.py |
| tests/test_services/test_cue_generator.py | review | tests/review/services/test_cue_generator.py |
| tests/test_services/test_execution_dispatch_grouping.py | review | tests/review/services/test_execution_dispatch_grouping.py |
| tests/test_services/test_proposal.py | review | tests/review/services/test_proposal.py |
| tests/test_services/test_proposal_queries.py | review | tests/review/services/test_proposal_queries.py |
| tests/test_services/test_tag_proposal.py | review | tests/review/services/test_tag_proposal.py |
| tests/test_services/test_tag_writer.py | review | tests/review/services/test_tag_writer.py |
| tests/test_tasks/test_execute_approved_batch.py | review | tests/review/tasks/test_execute_approved_batch.py |
| tests/test_tasks/test_execute_approved_batch_progress.py | review | tests/review/tasks/test_execute_approved_batch_progress.py |
| tests/test_tasks/test_execution.py | review | tests/review/tasks/test_execution.py |
| tests/test_tasks/test_proposal.py | review | tests/review/tasks/test_proposal.py |
| tests/test_cert_bootstrap.py | agents | tests/agents/core/test_cert_bootstrap.py |
| tests/test_cli/test_agents_add.py | agents | tests/agents/cli/test_agents_add.py |
| tests/test_config/test_agent_settings_kind.py | agents | tests/agents/config/test_agent_settings_kind.py |
| tests/test_config/test_agent_settings_redis_password.py | agents | tests/agents/config/test_agent_settings_redis_password.py |
| tests/test_config/test_agent_settings_windows.py | agents | tests/agents/config/test_agent_settings_windows.py |
| tests/test_deployment/test_agent_compose.py | agents | tests/agents/deployment/test_agent_compose.py |
| tests/test_deployment/test_api_filesystem_isolation.py | agents | tests/agents/deployment/test_api_filesystem_isolation.py |
| tests/test_deployment/test_cloud_agent_compose.py | agents | tests/agents/deployment/test_cloud_agent_compose.py |
| tests/test_deployment/test_job_image.py | agents | tests/agents/deployment/test_job_image.py |
| tests/test_deployment/test_k8s_runbook.py | agents | tests/agents/deployment/test_k8s_runbook.py |
| tests/test_parity/test_compare_analysis.py | agents | tests/agents/parity/test_compare_analysis.py |
| tests/test_routers/test_admin_agents.py | agents | tests/agents/routers/test_admin_agents.py |
| tests/test_routers/test_agent_analysis.py | agents | tests/agents/routers/test_agent_analysis.py |
| tests/test_routers/test_agent_analysis_inline_delete.py | agents | tests/agents/routers/test_agent_analysis_inline_delete.py |
| tests/test_routers/test_agent_auth.py | agents | tests/agents/routers/test_agent_auth.py |
| tests/test_routers/test_agent_heartbeat.py | agents | tests/agents/routers/test_agent_heartbeat.py |
| tests/test_routers/test_agent_identity.py | agents | tests/agents/routers/test_agent_identity.py |
| tests/test_routers/test_agent_presign_download.py | agents | tests/agents/routers/test_agent_presign_download.py |
| tests/test_routers/test_agent_push.py | agents | tests/agents/routers/test_agent_push.py |
| tests/test_routers/test_agent_s3.py | agents | tests/agents/routers/test_agent_s3.py |
| tests/test_schemas/test_agent_analysis.py | agents | tests/agents/schemas/test_agent_analysis.py |
| tests/test_schemas/test_agent_identity.py | agents | tests/agents/schemas/test_agent_identity.py |
| tests/test_schemas/test_agent_push.py | agents | tests/agents/schemas/test_agent_push.py |
| tests/test_schemas/test_agent_s3.py | agents | tests/agents/schemas/test_agent_s3.py |
| tests/test_schemas/test_agent_tasks.py | agents | tests/agents/schemas/test_agent_tasks.py |
| tests/test_services/test_agent_bootstrap.py | agents | tests/agents/services/test_agent_bootstrap.py |
| tests/test_services/test_agent_client.py | agents | tests/agents/services/test_agent_client.py |
| tests/test_services/test_agent_client_endpoints.py | agents | tests/agents/services/test_agent_client_endpoints.py |
| tests/test_services/test_agent_client_exec_batch_progress.py | agents | tests/agents/services/test_agent_client_exec_batch_progress.py |
| tests/test_services/test_agent_client_tls.py | agents | tests/agents/services/test_agent_client_tls.py |
| tests/test_services/test_agent_client_upload.py | agents | tests/agents/services/test_agent_client_upload.py |
| tests/test_services/test_agent_liveness.py | agents | tests/agents/services/test_agent_liveness.py |
| tests/test_services/test_agent_task_router.py | agents | tests/agents/services/test_agent_task_router.py |
| tests/test_services/test_agent_upsert.py | agents | tests/agents/services/test_agent_upsert.py |
| tests/test_services/test_model_bootstrap.py | agents | tests/agents/services/test_model_bootstrap.py |
| tests/test_tasks/test_agent_startup_banner.py | agents | tests/agents/tasks/test_agent_startup_banner.py |
| tests/test_tasks/test_agent_worker_heartbeat.py | agents | tests/agents/tasks/test_agent_worker_heartbeat.py |
| tests/test_tasks/test_shared_agent_bootstrap.py | agents | tests/agents/tasks/test_shared_agent_bootstrap.py |
| tests/integration/test_pg_dedup.py | integration | tests/integration/test_pg_dedup.py |
| tests/integration/test_pg_queue_priority.py | integration | tests/integration/test_pg_queue_priority.py |
| tests/integration/test_review_audit.py | integration | tests/integration/test_review_audit.py |
| tests/integration/test_stage_concurrency.py | integration | tests/integration/test_stage_concurrency.py |
| tests/integration/test_stage_pause.py | integration | tests/integration/test_stage_pause.py |
| tests/integration/test_stage_priority.py | integration | tests/integration/test_stage_priority.py |
| tests/integration/test_stage_resume.py | integration | tests/integration/test_stage_resume.py |
| tests/test_migrations/test_012_upgrade.py | integration | tests/integration/test_migrations/test_012_upgrade.py |
| tests/test_migrations/test_013_upgrade.py | integration | tests/integration/test_migrations/test_013_upgrade.py |
| tests/test_migrations/test_015_upgrade.py | integration | tests/integration/test_migrations/test_015_upgrade.py |
| tests/test_migrations/test_016_upgrade.py | integration | tests/integration/test_migrations/test_016_upgrade.py |
| tests/test_migrations/test_017_upgrade.py | integration | tests/integration/test_migrations/test_017_upgrade.py |
| tests/test_migrations/test_020.py | integration | tests/integration/test_migrations/test_020.py |
| tests/test_migrations/test_022.py | integration | tests/integration/test_migrations/test_022.py |
| tests/test_migrations/test_023.py | integration | tests/integration/test_migrations/test_023.py |
| tests/test_migrations/test_024.py | integration | tests/integration/test_migrations/test_024.py |
| tests/test_migrations/test_downgrade.py | integration | tests/integration/test_migrations/test_downgrade.py |
| tests/test_migrations/test_migration_018.py | integration | tests/integration/test_migrations/test_migration_018.py |
| tests/test_migrations/test_migration_025_cloud_job.py | integration | tests/integration/test_migrations/test_migration_025_cloud_job.py |
| tests/test_migrations/test_migration_026_kube_columns.py | integration | tests/integration/test_migrations/test_migration_026_kube_columns.py |
| tests/test_migrations/test_migration_027_cloud_phase.py | integration | tests/integration/test_migrations/test_migration_027_cloud_phase.py |
| tests/_queue_fakes_test.py | shared | tests/shared/core/queue_fakes_test.py |
| tests/test_a11y_guards.py | shared | tests/shared/core/test_a11y_guards.py |
| tests/test_analysis_progress_spike.py | shared | tests/shared/core/test_analysis_progress_spike.py |
| tests/test_base_html_sri.py | shared | tests/shared/core/test_base_html_sri.py |
| tests/test_config/test_cloud_route_threshold.py | shared | tests/shared/config/test_cloud_route_threshold.py |
| tests/test_config/test_cloud_target.py | shared | tests/shared/config/test_cloud_target.py |
| tests/test_config/test_kube_settings.py | shared | tests/shared/config/test_kube_settings.py |
| tests/test_config/test_llm_api_key_export.py | shared | tests/shared/config/test_llm_api_key_export.py |
| tests/test_config/test_push_config.py | shared | tests/shared/config/test_push_config.py |
| tests/test_config/test_s3_settings.py | shared | tests/shared/config/test_s3_settings.py |
| tests/test_config/test_secret_file_resolution.py | shared | tests/shared/config/test_secret_file_resolution.py |
| tests/test_config_role_split.py | shared | tests/shared/core/test_config_role_split.py |
| tests/test_config_worker.py | shared | tests/shared/core/test_config_worker.py |
| tests/test_constants.py | shared | tests/shared/core/test_constants.py |
| tests/test_database.py | shared | tests/shared/core/test_database.py |
| tests/test_dead_template_guard.py | shared | tests/shared/core/test_dead_template_guard.py |
| tests/test_docs_ia_current.py | shared | tests/shared/core/test_docs_ia_current.py |
| tests/test_enrich_analyze_workspaces.py | shared | tests/shared/core/test_enrich_analyze_workspaces.py |
| tests/test_entrypoint.py | shared | tests/shared/core/test_entrypoint.py |
| tests/test_health.py | shared | tests/shared/core/test_health.py |
| tests/test_identify_workspaces.py | shared | tests/shared/core/test_identify_workspaces.py |
| tests/test_logging_config.py | shared | tests/shared/core/test_logging_config.py |
| tests/test_logging_operational.py | shared | tests/shared/core/test_logging_operational.py |
| tests/test_main_lifespan.py | shared | tests/shared/core/test_main_lifespan.py |
| tests/test_migration_019_dedupe.py | shared | tests/shared/core/test_migration_019_dedupe.py |
| tests/test_models/test_agent.py | shared | tests/shared/models/test_agent.py |
| tests/test_models/test_cloud_job.py | shared | tests/shared/models/test_cloud_job.py |
| tests/test_models/test_core_models.py | shared | tests/shared/models/test_core_models.py |
| tests/test_no_auto_metadata_enqueue.py | shared | tests/shared/core/test_no_auto_metadata_enqueue.py |
| tests/test_no_default_queue_producers.py | shared | tests/shared/core/test_no_default_queue_producers.py |
| tests/test_phase01_gaps.py | shared | tests/shared/core/test_phase01_gaps.py |
| tests/test_phase02_gaps.py | shared | tests/shared/core/test_phase02_gaps.py |
| tests/test_phase03_gaps.py | shared | tests/shared/core/test_phase03_gaps.py |
| tests/test_phase04_gaps.py | shared | tests/shared/core/test_phase04_gaps.py |
| tests/test_pipeline_dag_context.py | shared | tests/shared/core/test_pipeline_dag_context.py |
| tests/test_proposals_upsert.py | shared | tests/shared/core/test_proposals_upsert.py |
| tests/test_rail_narrow_width.py | shared | tests/shared/core/test_rail_narrow_width.py |
| tests/test_record_palette_agents.py | shared | tests/shared/core/test_record_palette_agents.py |
| tests/test_redirect_resolution.py | shared | tests/shared/core/test_redirect_resolution.py |
| tests/test_review_apply_workspaces.py | shared | tests/shared/core/test_review_apply_workspaces.py |
| tests/test_routers/test_pipeline.py | shared | tests/shared/routers/test_pipeline.py |
| tests/test_routers/test_pipeline_inadmissible.py | shared | tests/shared/routers/test_pipeline_inadmissible.py |
| tests/test_routers/test_pipeline_localqueue.py | shared | tests/shared/routers/test_pipeline_localqueue.py |
| tests/test_routers/test_pipeline_scans.py | shared | tests/shared/routers/test_pipeline_scans.py |
| tests/test_routing_seam.py | shared | tests/shared/core/test_routing_seam.py |
| tests/test_schemas/test_pipeline_scans.py | shared | tests/shared/schemas/test_pipeline_scans.py |
| tests/test_scripts/test_download_models.py | shared | tests/shared/scripts/test_download_models.py |
| tests/test_services/test_enqueue_router.py | shared | tests/shared/services/test_enqueue_router.py |
| tests/test_services/test_pipeline.py | shared | tests/shared/services/test_pipeline.py |
| tests/test_services/test_pipeline_counts.py | shared | tests/shared/services/test_pipeline_counts.py |
| tests/test_shell_routes.py | shared | tests/shared/core/test_shell_routes.py |
| tests/test_task_split.py | shared | tests/shared/core/test_task_split.py |
| tests/test_tasks/test_controller_startup_banner.py | shared | tests/shared/tasks/test_controller_startup_banner.py |
| tests/test_tasks/test_controller_startup_localqueue.py | shared | tests/shared/tasks/test_controller_startup_localqueue.py |
| tests/test_tasks/test_functions.py | shared | tests/shared/tasks/test_functions.py |
| tests/test_tasks/test_heartbeat_cron.py | shared | tests/shared/tasks/test_heartbeat_cron.py |
| tests/test_tasks/test_heartbeat_failure.py | shared | tests/shared/tasks/test_heartbeat_failure.py |
| tests/test_tasks/test_heartbeat_loop.py | shared | tests/shared/tasks/test_heartbeat_loop.py |
| tests/test_template_helpers/test_progress_partial.py | shared | tests/shared/template_helpers/test_progress_partial.py |
| tests/test_utils/test_humanize.py | shared | tests/shared/utils/test_humanize.py |
| tests/test_web/test_saq_mount.py | shared | tests/shared/web/test_saq_mount.py |
