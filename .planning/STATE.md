---
gsd_state_version: 1.0
milestone: v5.0
milestone_name: Cloud Burst Analysis
status: executing
last_updated: "2026-06-26T18:14:03.797Z"
last_activity: 2026-06-26 -- Phase 51 execution started
progress:
  total_phases: 22
  completed_phases: 4
  total_plans: 23
  completed_plans: 19
  percent: 18
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-05-17 after v4.0 milestone)

**Core value:** Get 200K messy music and concert files properly named, organized, deduplicated, with rich metadata in Postgres -- human-in-the-loop approval so nothing moves without review. Files stay on file-server agents; decisions stay on the application server.
**Current focus:** Phase 51 — deployment-config-docs

## Current Position

Phase: 51 (deployment-config-docs) — EXECUTING
Plan: 1 of 4
Status: Executing Phase 51
Last activity: 2026-06-26 -- Phase 51 execution started

## Performance Metrics

**v1.0 Velocity:**

- Total plans completed: 73
- Total phases: 11
- Timeline: 4 days (2026-03-27 -> 2026-03-30)
- Tests: 282 passing
- LOC: 7,975 Python

**v2.0 Velocity:**

- Total plans completed: 16
- Total phases: 6
- Timeline: 3 days (2026-03-31 -> 2026-04-02)
- Tests: 538 passing
- LOC: 5,966 Python

**v3.0 Velocity:**

- Total plans completed: 11
- Total phases: 6
- Timeline: 2 days (2026-04-03 -> 2026-04-04)

**v4.0 Velocity:**

- Total plans completed: 47
- Total phases: 6
- Timeline: ~43 days (2026-04-03 -> 2026-05-17 incl. discuss/research/UI design per phase)
- LOC: ~23,242 Python lines added / 1,677 deleted (180 files changed since v3.0 tag)

## Accumulated Context

### Roadmap Evolution

- v5.0 Cloud Burst Analysis roadmap created (2026-06-24): 5 phases (47-51), one per requirement category, in dependency order — **47** Official arm64 essentia agent image (build from source on a native arm64 CI runner, GHCR publish, parity guard; CLOUDIMG-01..03); **48** Compute-agent type (`kind="compute"` media-less agent, drains per-agent SAQ queue + HTTP result PUT, Agents-page badge/liveness/depth; CLOUDAGENT-01..03); **49** Duration routing & backfill (capability-aware `enqueue_router` on `metadata.duration` threshold default 90min, "awaiting cloud" hold when no compute agent online, backfill the 144 `analysis_failed` long files via the Phase 45 scheduling ledger; CLOUDROUTE-01..04); **50** Push pipeline (control-plane "stay one ahead" orchestrator + file-server `push_file_to_cloud` rsync/SSH-over-Tailscale to A1 scratch, `ProcessFilePayload.ephemeral`, sha256 verify, scratch delete, idempotent re-drive; CLOUDPIPE-01..05); **51** Deployment/config/docs (`docker-compose.cloud-agent.yml` + Tailscale, all pydantic-settings knobs with `_FILE` secrets, OCI A1 + Tailscale-ACL runbook scoping A1→lux:{5432,6379,8000}+nox→A1:22 + least-privilege queue role, master enable toggle; CLOUDDEPLOY-01..04). 18 requirements, 100% coverage, no orphans. Design brainstormed + approved this session. Replaces the "Distributed cloud analysis" backlog item (now narrowed to rsync-over-Tailscale to A1 local disk — no object storage — because arm64 essentia builds from source, proven on `spike/arm64-essentia-analysis`). Each phase = own PR.
- Phase 46 added (2026-06-23): Heartbeat Starvation Fix — decouple the agent liveness heartbeat from the SAQ worker concurrency pool. Surfaced by live incident: agent `nox` showed `DEAD` (last seen 39m ago) while the `phaze-agent-worker` container was healthy and pegged at ~394% CPU. Root cause: `heartbeat_tick` is a SAQ `CronJob` registered in the same agent worker (`agent_worker.py:227`), so it competes for the same `worker_max_jobs=8` concurrency slots as `process_file`. With all 8 slots full of multi-hour analysis jobs (long concert sets, 2–3.6h each), the 30s heartbeat cron could only run when a slot freed (~every 50 min) → `last_seen` exceeded the 300s staleness threshold (`constants.py:61`) → control plane marked the busy agent DEAD, which also blocks new agent-task routing (fingerprint/metadata). Fix: run the heartbeat independent of the job concurrency pool so a saturated worker still reports liveness. Distinct from the Phase 43 analyze-throughput work (that bounds job cost; this guarantees liveness regardless of job cost). NOTE: phase.add mis-numbered it 43 (counted dirs, max=42; collided with shipped text-only 43/44/45) — manually renumbered to 46 + dir renamed to `46-heartbeat-starvation-fix`.
- Phase 45 added (2026-06-18): Scheduling Ledger for Orphan Recovery — surfaced by live incident: clicking "Recover orphaned work" (`recover_orphaned_work(force=True)`) bypassed the loss-detection gate and reconciled the ENTIRE complement-of-done backlog of all 8 stages, detonating the queue to ~44,500 jobs over ~11,400 never-scheduled DISCOVERED files. Root issue: no record anywhere that a stage was ever *scheduled* for an item (pending sets = complement-of-done). Operator principle: recovery must only re-queue work that was previously scheduled and lost; never-scheduled work is not yet orphaned. Approach: durable scheduling ledger written at the single `before_enqueue` chokepoint (`apply_deterministic_key`), cleared on completion (`increment_completed` after_process); `recover_orphaned_work` = ledger − live saq_jobs keys − completed. Survives a saq_jobs truncate (the only real post-Phase-36 loss case). `force` becomes "reconcile the ledger now," not "sweep the backlog." Successor to the Phase 39–42 DAG-manual-control + recovery line. NOTE: phase.add mis-numbered it 43 (collided with existing 43/44, wrong dir tree) — manually renumbered to 45 + dir moved to `.planning/phases/45-...`.
- Phase 30 added (2026-06-09): Fix systemic control-plane SAQ queue misrouting — every manually-triggered enqueue (9 sites across pipeline.py, tracklists.py, scan.py/ingestion.py) targets the consumer-less `default` queue. Surfaced by live incident: "Run analysis" stranded 11,428 `process_file` jobs. See phase CONTEXT.md.
- Phase 31 added (2026-06-10): Windowed Time-Series Audio Analysis — surfaced by live incident after v4.0.9 redeploy: `RhythmExtractor2013` `OnsetDetectionGlobal` buffer overflow crashes whole-file BPM on multi-hour sets (79% of the 11,428-file archive is >50 MB), 0 files analyzed. Fix = stream-decode + per-window analysis (two tiers: BPM/key 30s, mood/style/danceability 3min), queryable `analysis_window` child table + aggregates on `analysis`. Design spec: docs/superpowers/specs/2026-06-10-windowed-analysis-design.md. Brainstormed decisions: scope=everything-as-time-series via two tiers; storage=queryable child table (option B); UI=compact+HTMX-expand timeline (option B). Ships v4.0.10.
- Phase 34 added (2026-06-10): Pipeline Queue-Depth Status & Double-Enqueue Guard — surfaced by live UX bug: operator clicked "Run Analysis", refreshed, and all status vanished (DB shows files as `DISCOVERED` whether or not enqueued; verified 11,429 incomplete `process_file` jobs live on `phaze-agent-nox`, 0 analyzed, button still clickable → double-enqueue risk). Fix = read live SAQ queue depth (`Queue.count`) via `app.state.controller_queue` + per-agent `task_router`, new `get_queue_activity` service, surface through existing 5s `/pipeline/stats` poll, persistent OOB "Processing" card (progress = DB `analyzed`/(analyzed+agent_busy)), coarse Alpine `$store.pipeline` button disable (agent_busy gates Analyze/Fingerprint/Metadata; controller_busy gates Proposals). Brainstormed decisions (operator, 2026-06-10): disable scope = coarse (all agent buttons); indicator = progress bar + counts; progress `done` = DB analyzed count (not SAQ `complete`, survives worker restart). NOTE: numbered 34 because phase.add counted directories (max=31) and collided with the text-only Phase 32/33 entries; renumbered 32→34 + directory renamed. Ships a subsequent v4.0.x.
- Phase 35 added (2026-06-11): Pipeline Determinism, Idempotency & Per-Job-Type Observability — surfaced by the 2026-06-11 queue-doubling incident (random-uuid `process_file` jobs from the pre-Phase-32 "Run Analysis" path couldn't dedup against the new deterministic-key re-enqueue → live queue doubled to ~22,830 jobs over 11,428 files; cleaned via purge + cron rebuild). Generalizes the Phase 32 deterministic-key fix to the WHOLE pipeline. Five items: (1) centralized enqueue-layer deterministic keys `<task>:<natural_id>` for all job types (only `process_file` keyed today); (2) audit/ensure all task DB writes upsert (most already D-26; gaps = proposals, execution_log, tag_write_log); (3) remove auto metadata-extraction from discovery/scan (`agent_files.py:130-161`, `ingestion.py:183-191`) → manual-only; (4) add a "Metadata" stage card between Discovered and Fingerprinted; (5) per-job-type progress bars backed by maintained per-function counters. Locked decisions (operator): (A) centralized key enforcement; (B) maintained per-function counters. Ships a subsequent v4.0.x.
- Phases 36/37/38 added (2026-06-12): Stage Pause + Per-Stage Priority feature, brainstormed and approved (design kept INLINE in conversation — no spec doc; see auto-memory `project_stage_pause_priority_design`). **36** = migrate SAQ queue Redis→Postgres backend (`saq[redis]`→`saq[postgres]`, psycopg3 pool separate from SQLAlchemy/asyncpg, new `PHAZE_QUEUE_URL`) to unlock native Postgres-only per-job `priority`+`scheduled` control; regression-check Phases 32/33/35; includes Step D homelab change-prompt deliverable. **37** = `pipeline_stage_control` table + pause/priority API + enqueue hook, operating on `saq_jobs` via UPDATEs (pause=drain via `scheduled=SENTINEL` park, resume sentinel-guarded; priority default 50 range 0–100 LOWER=sooner=SAQ priority direct, reorders queued backlog live); scope = 3 agent stages (metadata/analyze/fingerprint). **38** = DAG UI pause toggle + priority stepper (▲Higher decrements number, ▼Lower increments) per agent node, extend `/pipeline/stats`, REMOVE the "Rescan Files" anchor (was a duplicate of Start Scan → same `POST /pipeline/scans`). Each phaze phase = own PR. Confirmed SAQ Postgres dequeue `ORDER BY priority, scheduled` + `now>=scheduled` gate in `saq/queue/postgres.py:644-662`.
- Phases 39/40/41/42 added (2026-06-14): **"DAG is the single manual control surface; automation only in recovery"** theme. Surfaced by operator audit of the DAG: the tracklist sub-chain (Scan/Search, Scrape, Match) is display-only on the DAG (triggers live on Tracklists/Proposals pages), the empty-state's "starts automatically" is unwired (`metadata_extraction` does NOT chain to `search_tracklist`; no cron sweeps unmatched files), and `reenqueue_discovered` runs unconditionally every 5 min → effectively auto-runs Analyze. Operator decisions (2026-06-14): run BOTH name-search AND fingerprint-scan over all files, no fallback, as TWO separate phases; Scrape/Match = bulk-over-pending; automatic enqueue ONLY in recovery mode, restoring ALL stages. **39** = Tracklist Search DAG node (bulk `search_tracklist`, gated on Metadata done). **40** = Tracklist Fingerprint-Scan DAG node (bulk `scan_live_set`, gated on discovered + online agent; independent of 39). **41** = Scrape + Match DAG triggers (bulk-over-pending, gated on ≥1 tracklist; depends 39+40). **42** = Recovery-only automation (replace the 5-min `reenqueue_discovered` cron with restart/queue-loss detection reconciling ALL in-flight stages; zero steady-state auto-enqueue; depends Phase 32). Plan order: 39 first. Each phase = own PR.

### Decisions

(Full milestone decision log archived in `.planning/milestones/v4.0-ROADMAP.md` Milestone Summary. Current-cycle decisions accumulate here.)

- [Phase ?]: Phase 37-01: pipeline_stage_control is a standalone app table separate from SAQ-owned saq_jobs; priority SmallInteger with DB CHECK 0-100 keeps stages inside SAQ's 0-32767 dequeue window
- [Phase ?]: Phase 37-01: STAGE_TO_FUNCTION/_FUNCTION_TO_STAGE/SENTINEL=9999999999 live in a DB-free constants module so the agent worker can import them without crossing the ORM import boundary
- [Phase ?]: 37-02: apply_stage_control reads pipeline_stage_control via job.queue.pool (psycopg3), never SQLAlchemy, keeping the agent import boundary intact (T-37-04)
- [Phase ?]: 37-02: 5s TTL cache (single monotonic window) collapses bulk-enqueue control reads; resume keeps AND scheduled=:SENTINEL guard so retry backoffs are never clobbered
- [Phase 37]: 37-03: assert dequeue ORDER + saq_jobs.priority COLUMN, never the deserialized Job.priority (a raw column UPDATE does not rewrite the serialized job blob)
- [Phase 37]: 37-03: shared tests/integration/conftest.py stage_env fixture (real build_pipeline_queue queue + SQLAlchemy session on the same DB + seeded pipeline_stage_control) proves the helpers on the live saq_jobs dequeue/count/row-lock contract
- [Phase ?]: 37-04: control endpoints return {stage, priority, paused} from the PipelineStageControl row (durable intent), never a serialized job's priority (Plan-03 column-vs-blob finding)
- [Phase ?]: 37-04: control-row ORM mutation + service-helper saq_jobs UPDATE land in one session.commit(); unknown stage -> 422 via allowlist guard; priority delta clamped [0,100]; no app-layer auth (reverse-proxy internal-realm)
- [Phase 38]: 38-01: removed the unused discovery cta 'Rescan Files' alongside the dead anchor (mk o={} default makes the omission safe); guarded the deletion with a string-absence render assertion
- [Phase 38]: 38-03: get_stage_controls degrade-safe reader returns paused=False/priority=50 defaults on any failure (mirrors _safe_count); _build_dag_context coerces paused to int 0/1 to hold the all-ints x-init invariant (T-35-11)
- [Phase 38]: 38-03: the 6 stage-control keys ride the existing dag.items() OOB loop with zero stats_bar.html edit; one _NEW_STORE_KEYS edit drives the store-literal, int-key, and OOB-seed tests
- [Phase 38]: 38-02: stage_controls reusable Jinja macro (id=stage-controls-<stage>) on the 3 agent chips; pause/resume = TWO x-show-gated static-hx-post buttons (not a bound :hx-post), authoritative-only @htmx:after-request JSON-parse store write, no optimistic mutation (T-38-OOB)
- [Phase 38]: 38-02: agent-chip NODE_LAYOUT gutter widened 182->276px (h 154->250) for the control row; overlap guard min_chip_height bumped 150->240; canvas/SVG grown 720->1000; col-0/col-2/col-3 nodes re-balanced to incoming-edge midpoints
- [Phase ?]: Phase 46-01: agent liveness heartbeat runs as an asyncio background task launched in agent_worker startup (cancelled in shutdown), NOT a SAQ CronJob — a CronJob competed for worker_max_jobs dispatch slots and was starved by multi-hour process_file jobs (busy-agent-DEAD incident); heartbeat_tick kept as a back-compat shim; one-time DELETE of orphaned cron:heartbeat_tick row from saq_jobs documented for redeploy

### Pending Todos

None.

### Blockers/Concerns

- 29-HUMAN-UAT.md: real two-host production smoke is verified-docs-only; deferred until file-server hardware is available
- Tech debt parked in v4.0 audit: WR-01..WR-04 (Phase 29), WR-03 (Phase 28 UI), P28-RACE-01 — see `.planning/milestones/v4.0-MILESTONE-AUDIT.md`

### Quick Tasks Completed

| # | Description | Date | Commit | Directory |
|---|-------------|------|--------|-----------|
| 260410-kco | Add Docker image publishing to GHCR following discogsography pattern | 2026-04-10 | 3f91f93 | [260410-kco-add-docker-image-publishing-to-ghcr-foll](./quick/260410-kco-add-docker-image-publishing-to-ghcr-foll/) |
| 260414-quo | Add Discord notification to docker-publish.yml workflow mirroring discogsography pattern | 2026-04-14 | 9c5cedb | [260414-quo-add-discord-notification-to-docker-publi](./quick/260414-quo-add-discord-notification-to-docker-publi/) |
| 260502-lqb | Remove Discord notification step from docker-publish.yml workflow | 2026-05-02 | ea84be2 | [260502-lqb-remove-discord-notification-step-from-do](./quick/260502-lqb-remove-discord-notification-step-from-do/) |
| 260520-bcl | Dedicated local integration-test database on a non-colliding port (env-configurable URLs + `just integration-test`/`test-db` recipes) | 2026-05-20 | adc2970 | [260520-bcl-dedicated-local-integration-test-databas](./quick/260520-bcl-dedicated-local-integration-test-databas/) |
| 260606-qgu | Fix flaky CDN SRI test: self-host audited Tailwind build (jsDelivr per-edge minification varied bytes); no SRI weakening | 2026-06-06 | b76d725 | [260606-qgu-fix-flaky-cdn-sri-test-jsdelivr-serves-t](./quick/260606-qgu-fix-flaky-cdn-sri-test-jsdelivr-serves-t/) |
| 260606-mpm | Fix release tags not publishing version-tagged Docker images to GHCR (push:tags trigger, tag-ref change detection, strengthened guard test, doc pin fixes) | 2026-06-06 | b811a9e | [260606-mpm-fix-release-tags-not-publishing-version-](./quick/260606-mpm-fix-release-tags-not-publishing-version-/) |
| 260606-pjd | Make ci.yml detect-changes robust to force-push: fall back to origin/main diff when github.event.before is unreachable (+ guard test) | 2026-06-06 | d89a00b | [260606-pjd-make-ci-yml-detect-changes-robust-to-for](./quick/260606-pjd-make-ci-yml-detect-changes-robust-to-for/) |
| 260608-mbc | Fix three scan-incident issues: container uid→1000:1000 (Dockerfile), scan_directory surfaces permission-denied walks as failed, ScanBatch.completed_at + migration 015 so elapsed freezes | 2026-06-08 | cdc3c59 | [260608-mbc-fix-three-scan-incident-issues-in-one-pr](./quick/260608-mbc-fix-three-scan-incident-issues-in-one-pr/) |
| 260606-n0y | Reconcile GHCR image paths: cleanup targets canonical bare `phaze`, orphan `phaze/api` documented as deprecated, publish/cleanup parity guard test | 2026-06-06 | a993aea | [260606-n0y-reconcile-ghcr-image-paths-stop-orphanin](./quick/260606-n0y-reconcile-ghcr-image-paths-stop-orphanin/) |
| 260606-n7g | Switch audfprint/panako sidecars in docker-compose.agent.yml to pull published GHCR images (commented build fallback); update deployment docs + tests | 2026-06-06 | 95cd630 | [260606-n7g-switch-audfprint-panako-sidecars-in-dock](./quick/260606-n7g-switch-audfprint-panako-sidecars-in-dock/) |
| 260622-i0w | Add scanned/deduped/unique reconciliation to pipeline UI (Discovery DAG node + Recent Scans FILES cell); explains the discovery-vs-agent count gap as NFC-collision dedup, not lost work | 2026-06-22 | 8d805bd | [260622-i0w-add-scanned-deduped-unique-reconciliatio](./quick/260622-i0w-add-scanned-deduped-unique-reconciliatio/) |
| 260606-nha | Add `phaze agents add` management CLI (token mint + sha256 hash + id-charset validation + queue-name output) and document PHAZE_AGENT_QUEUE = phaze-agent-<agent_id> convention | 2026-06-06 | 602488a | [260606-nha-add-a-phaze-agents-add-management-cli-ge](./quick/260606-nha-add-a-phaze-agents-add-management-cli-ge/) |
| 260620-jvu | Harden Phase 45 code-review warnings: WR-01 nested swallow+log guard on terminal-ack except blocks (scan match-failure, metadata, fingerprint) so a double-failure re-raises the original error; WR-02 `cleared: Literal[True]` on failure-response schemas | 2026-06-20 | d9123af | [260620-jvu-harden-ledger-ack-warnings](./quick/260620-jvu-harden-ledger-ack-warnings/) |
| 260608-i21 | Harden agent model bootstrap against transient download failures: per-file retry with bounded backoff+jitter, explicit httpx timeouts, atomic os.replace, Content-Length truncation check, fail-fast 4xx / retry 5xx (Verified) | 2026-06-08 | b0ddc4f | [260608-i21-harden-agent-model-bootstrap-against-tra](./quick/260608-i21-harden-agent-model-bootstrap-against-tra/) |
| 260608-jbg | Validate model integrity on bootstrap via per-file HEAD Content-Length size check (size-only); shared bounded-retry+timeout across HEAD+GET so no request can wedge the worker; remove count-only gate (always validate); re-download truncated/corrupt files; correct stale ~150MB estimate to ~3.1GB/34 files (Verified). Extends PR #91. | 2026-06-08 | b86babd | [260608-jbg-validate-model-integrity-on-bootstrap-vi](./quick/260608-jbg-validate-model-integrity-on-bootstrap-vi/) |
| 260609-f96 | Fix scan_directory 10s asyncio.TimeoutError: AgentTaskRouter._queue_for built per-agent SAQ queues without the apply_project_job_defaults before_enqueue hook, so agent-dispatched jobs inherited SAQ's 10s default instead of worker_job_timeout=600. Register the hook on each per-agent queue (3rd call site) + regression test. Found live on nox/lux v4.0.4. | 2026-06-09 | c6c7e20 | [260609-f96-fix-scan-directory-10s-timeouterror-regi](./quick/260609-f96-fix-scan-directory-10s-timeouterror-regi/) |
| 260609-glv | Scan-pipeline reliability bundle (3 fixes, surfaced sequentially on v4.0.5): (1) sanitize PG-invalid chars in mutagen tags — _sanitize_pg_text strips NUL U+0000 + lone surrogates U+D800-U+DFFF in _first_str + _serialize_tags (fixes asyncpg UntranslatableCharacterError 500 on metadata writes; preserves valid controls/noncharacters); (2) scan_directory enqueued with timeout=0 (unbounded; Job.stuck stays False) + retries=0 via AgentTaskRouter timeout/retries pass-through — a fixed 600s SAQ timeout killed healthy bulk scans that then retried from scratch and never finished; (3) config.scan_stall_seconds default 600→86400 (24h) so the progress stall reaper is the sole liveness guard. + regression tests. | 2026-06-09 | 4b37c13 | [260609-glv-fix-metadata-write-500-strip-nul-bytes-f](./quick/260609-glv-fix-metadata-write-500-strip-nul-bytes-f/) |
| 260610-fp9 | Add audio system-deps apt layer to shared Dockerfile (libatomic1 ffmpeg libsndfile1 libchromaprint-tools) — v4.0.8 `python:3.14-slim` image had NO apt layer, so every `process_file` job dead-lettered at `import essentia` (`ImportError: libatomic.so.1`), stranding all 11,428 files in `discovered`. Verified via ldd on live v4.0.8 image: prebuilt essentia-tensorflow wheel bundles its heavy deps; only libatomic1 was unbundled+missing — proven sufficient for full `import essentia`. ffmpeg/fpcalc kept for the broader pipeline (ffprobe video metadata, pyacoustid fingerprinting). Needs v4.0.9 release + nox/lux redeploy. | 2026-06-10 | f5fb6e7 | [260610-fp9-add-audio-system-deps-to-dockerfile-so-e](./quick/260610-fp9-add-audio-system-deps-to-dockerfile-so-e/) |
| 260613-t7k | Two pipeline-DAG fixes: (1) widen NODE_LAYOUT chips 180→240px (re-gridded columns 24/392/760/1128, canvas/SVG 1132→1392×1000) so the Phase-38 control row stops clipping "▼ Lower"→"Low"; (2) replace the global `agentBusy` enqueue gate with per-stage busy counts — new degrade-safe `get_stage_busy_counts` reads `saq_jobs` by deterministic-key prefix, seeded as metadataBusy/analyzeBusy/fingerprintBusy onto the dag map (rides existing dag.items() seed + 5s OOB loop, no stats_bar.html edit), so Metadata/Analyze/Fingerprint run in parallel. SAVEPOINT degrade (not rollback) to avoid 500ing the dashboard. 1755 tests pass, 97.55% cov. | 2026-06-14 | 11fc68f | [260613-t7k-widen-pipeline-dag-node-chips-and-make-m](./quick/260613-t7k-widen-pipeline-dag-node-chips-and-make-m/) |
| 260614-sg8 | Fix trigger_scan dead-letter (Tracklists "Scan" tab): `POST /tracklists/scan` enqueued `scan_live_set` with file_id only → dead-lettered against `ScanLiveSetPayload` (`extra="forbid"`, needs file_id+original_path+agent_id), the v4.0.8 incident class. Now loads each FileRecord and enqueues the full payload via `model_dump(mode="json")`; non-UUID / no-FileRecord ids skipped (never 500); `total` = jobs actually enqueued; routing + central deterministic key unchanged. Follow-up to Phase 40 (which fixed only the bulk pipeline path). 1807 pass, 97.59% cov. | 2026-06-14 | (pending PR) | [260614-sg8-fix-trigger-scan-dead-letter-enqueue-sca](./quick/260614-sg8-fix-trigger-scan-dead-letter-enqueue-sca/) |
| 260615-cyp | Fix pipeline DAG rendering as visible text: a JS comment inside the parent `#pipeline-dag` Alpine `x-data="..."` attribute used double quotes (`"no online agent"`), terminating the HTML attribute at the first inner `"` and dumping the entire `nodes` getter into the DOM as text (Phase-40 `fingerprint_scan` comment regression, live on nox/lux v4.2.0). Single-quote the comment + regression guard `test_xdata_getter_has_no_unescaped_double_quotes` asserting the `#pipeline-dag` x-data value holds zero literal `"`. 30 non-DB tests pass; all hooks clean. | 2026-06-15 | 928d229 | [260615-cyp-fix-dag-canvas-xdata-quote](./quick/260615-cyp-fix-dag-canvas-xdata-quote/) |
| 260618-sx6 | Bridge configured LLM API key into litellm (Bug A): generate_proposals failed every run with litellm AuthenticationError because the file-loaded ControlSettings.anthropic_api_key had zero consumers — litellm reads the bare ANTHROPIC_API_KEY env var, never set. Add config.export_llm_api_keys() (exports present SecretStr keys only when unset, operator wins, never logs) called from controller.startup(); unit + functional wiring-guard tests. Full suite 1888 passed, 97.64% cov. Bug B (nox panako/audfprint host alias) handled separately as a homelab fix. | 2026-06-19 | 9e6dd53 | [260618-sx6-pass-configured-anthropic-openai-api-key](./quick/260618-sx6-pass-configured-anthropic-openai-api-key/) |
| Phase 34 P01 | 12 min | 2 tasks | 2 files |
| Phase 34 P02 | ~10 min | 3 tasks | 4 files |
| Phase 34 P03 | ~8 min | 2 tasks | 4 files |
| Phase 34 P04 | ~18 min | 3 tasks | 4 files |
| Phase 37 P01 | 3min | 3 tasks | 6 files |
| Phase 37 P02 | 12min | 3 tasks | 6 files |
| Phase 37 P03 | ~20min | 2 tasks | 6 files |
| Phase 37 P04 | ~6min | 3 tasks | 5 files |
| Phase 38 P01 | 3min | 2 tasks | 2 files |
| Phase 38 P03 | ~12min | 3 tasks | 5 files |
| Phase 38 P02 | 8min | 3 tasks | 2 files |
| Phase 45 P05 | ~5 min | 1 tasks | 2 files |
| Phase 45 P06 | ~25 min | 2 tasks | 12 files |
| Phase 46 P01 | ~20min | 3 tasks | 8 files |

## Session Continuity

Last session: 2026-06-26T17:27:28.280Z
Stopped at: Phase 51 context gathered
Resume file: .planning/phases/51-deployment-config-docs/51-CONTEXT.md
