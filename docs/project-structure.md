<!-- generated-by: gsd-doc-writer -->
# Project Structure

```
phaze/
├── src/phaze/                  # Application package
│   ├── config.py               # Pydantic settings (env vars, role split)
│   ├── config_backends.py      # Backend-registry schema (backends.toml: local + Kueue + cloud)
│   ├── constants.py            # File categories, extension map, tuning constants
│   ├── database.py             # Async SQLAlchemy engine + session factory
│   ├── main.py                 # FastAPI app factory with lifespan
│   ├── entrypoint.py           # Container entrypoint shim: runs cert bootstrap, then execvp's uvicorn
│   ├── cert_bootstrap.py       # Pre-uvicorn TLS/mTLS cert bootstrap for distributed agents (DB-free, idempotent)
│   ├── job_runner.py           # One-shot Kueue Job entrypoint (cloud compute-agent analysis)
│   ├── analysis_child.py       # `python -m phaze.analysis_child` CLI: runs analyze_file in a real child process
│   ├── logging_config.py       # Central structlog configuration for every Phaze process
│   ├── cli/                    # phaze management CLI (argparse): `agents add` mints token + agents row
│   │   └── __init__.py         #   Command groups + entry point
│   ├── web/                    # Web mount helpers
│   │   └── saq_mount.py        #   Testable mount for the SAQ monitoring dashboard (/saq)
│   ├── enums/                  # DB-free enums (importable without SQLAlchemy)
│   │   ├── execution.py        #   ExecutionStatus enum (re-exported by models/execution.py)
│   │   └── stage.py            #   Stage/status enums + DB-free per-row status resolver + eligibility DAG
│   ├── utils/                  # Pure helpers (no deps)
│   │   └── humanize.py         #   Relative-time formatter ("4m ago", "2h ago")
│   ├── scripts/                # Python-callable utility scripts
│   │   └── download_models.py  #   Fetch essentia weight files (shared by bash + agent bootstrap)
│   ├── static/                 # Static assets (favicons, web manifest, OG image)
│   ├── models/                 # SQLAlchemy ORM models
│   │   ├── base.py             #   DeclarativeBase + TimestampMixin
│   │   ├── file.py             #   FileRecord (no state column; per-stage status derived on read)
│   │   ├── scan_batch.py       #   ScanBatch progress tracking
│   │   ├── metadata.py         #   FileMetadata (audio tags)
│   │   ├── analysis.py         #   AnalysisResult (BPM, key, mood, style) + AnalysisWindow (per-window rows)
│   │   ├── proposal.py         #   RenameProposal + ProposalStatus
│   │   ├── execution.py        #   ExecutionLog (audit trail)
│   │   ├── tracklist.py        #   Tracklist + TracklistVersion + TracklistTrack
│   │   ├── file_companion.py   #   FileCompanion (companion-media join)
│   │   ├── agent.py            #   Agent (file-server identity for distributed agents)
│   │   ├── discogs_link.py     #   DiscogsLink (candidate Discogs release matches per track)
│   │   ├── cloud_job.py        #   CloudJob (per-file S3 object-staging / cloud-burst sidecar)
│   │   ├── pipeline_stage_control.py # PipelineStageControl (durable per-stage pause/priority intent)
│   │   ├── scheduling_ledger.py #   SchedulingLedger (durable "stage scheduled" recovery record)
│   │   ├── route_control.py    #   RouteControl (single-row force-local routing override)
│   │   ├── dedup_resolution.py #   DedupResolution (per-file "resolved to canonical file" marker)
│   │   ├── stage_skip.py       #   StageSkip (per-(file, stage) force-skip marker for enrich stages)
│   │   ├── tag_write_log.py    #   TagWriteLog (append-only tag-write audit trail)
│   │   ├── filename_convention.py # FilenameConvention (corpus-learned filename conventions, e.g. date order)
│   │   ├── tracklist_lookup_cache.py # TracklistLookupCache (persisted per-unique-set 1001TL lookup outcome)
│   │   └── tracklist_priority_flag.py # TracklistPriorityFlag (operator "answer this next" drain flag)
│   ├── routers/                # API + UI endpoints
│   │   ├── health.py           #   GET /health
│   │   ├── shell.py            #   v7.0 console shell: GET / (actionable Summary landing) + GET /s/<stage> workspace swaps
│   │   ├── pipeline.py         #   Stage triggers + /pipeline/stats poll (/pipeline/ 302-redirects to the shell)
│   │   ├── pipeline_scans.py   #   Admin scan trigger + HTMX scan-batch polling
│   │   ├── proposals.py        #   Proposal review + approval UI
│   │   ├── execution.py        #   Batch execution + SSE progress
│   │   ├── preview.py          #   `/preview/` legacy route: 302-redirect into the shell's Move workspace
│   │   ├── duplicates.py       #   Duplicate resolution UI
│   │   ├── tracklists.py       #   Tracklist management UI
│   │   ├── companion.py        #   Companion file association
│   │   ├── cue.py              #   CUE sheet management UI (generation + batch)
│   │   ├── search.py           #   Unified cross-entity search UI
│   │   ├── tags.py             #   Tag review UI (side-by-side compare, inline edit, write)
│   │   ├── admin_agents.py     #   Admin agents page + HTMX table partial
│   │   ├── pipeline_stages.py  #   Per-stage control plane: pause/resume/priority endpoints
│   │   ├── record.py           #   Per-file full-record read-only fragment route
│   │   ├── routing.py          #   Force-local master routing override (thin write endpoint)
│   │   ├── column_sort.py      #   THE sortable-column contract for every operator-facing table (whitelist + direction)
│   │   ├── proposal_sort.py    #   The RenameProposal sortable-column whitelist (shared by its two surfaces)
│   │   ├── request_guards.py   #   THE untrusted-input contract: envelope parse/shape guards (422, never a 500)
│   │   ├── response_shape.py   #   THE htmx response-shape contract: what document shape + status a handler owes
│   │   ├── view_state.py       #   THE list-view state carrier (filter/search/page/sort) for htmx-swapped tables
│   │   ├── agent_auth.py       #   NOT a router: exports the get_authenticated_agent bearer-token dependency
│   │   └── agent_*.py          #   Distributed-agent internal API (13 routers under /api/internal/agent):
│   │       │                   #     files, metadata, execution, heartbeat, identity,
│   │       │                   #     analysis, push, s3, proposals,
│   │       │                   #     scan_batches, exec_batches, scratch (compute-scratch janitor
│   │       │                   #     liveness probe), tag_writes (result + before-snapshot callbacks)
│   ├── schemas/                # Pydantic request/response models
│   │   ├── companion.py        #   Companion/duplicate schemas
│   │   ├── pipeline_scans.py   #   Pipeline scan-trigger schemas
│   │   ├── agent_tasks.py      #   Agent task-routing payload schemas
│   │   ├── wire_bounds.py      #   THE Wire Bounds Contract: every inbound value bounded to fit its column
│   │   └── agent_*.py          #   Distributed-agent contract schemas (13, DB-free, loaded in agent worker):
│   │       │                   #     identity, heartbeat, files, metadata, analysis,
│   │       │                   #     proposals, execution, exec_batches, scan_batches,
│   │       │                   #     push, s3, scratch, tag_writes
│   ├── services/               # Business logic
│   │   ├── hashing.py          #   Shared hashing utilities
│   │   ├── metadata.py         #   Tag extraction via mutagen
│   │   ├── pagination.py       #   THE paging contract: bounded page size + mandatory unique tiebreaker
│   │   ├── bulk_insert.py      #   Split a multi-row INSERT to fit PostgreSQL's bind-parameter limit
│   │   ├── like_escape.py      #   Escape LIKE/ILIKE metacharacters so operator-typed search text matches literally
│   │   ├── queue_introspection.py # Operator-facing SAQ `active` breakdown: RUNNING vs CLAIMED-but-buffered
│   │   ├── text_repair.py      #   Repair double-encoded UTF-8 ("mojibake") in already-decoded str values
│   │   ├── text_repair_backfill.py # Idempotent backfill of files.original_filename_repaired for pre-045 rows
│   │   ├── pg_text.py          #   Sanitize free text for PostgreSQL UTF8 storage (NUL/surrogate stripping)
│   │   ├── containment.py      #   Path containment check shared by every consumer of agent-supplied paths
│   │   ├── analysis.py         #   BPM/key/mood via essentia
│   │   ├── analysis_enqueue.py #   FastAPI-free producer for process_file jobs (deterministic key + payload)
│   │   ├── analysis_exec.py    #   Shared async subprocess driver for essentia analysis (analysis_child)
│   │   ├── analysis_sizing.py  #   Host-derived thread + concurrency sizing for the analyze path
│   │   ├── proposal.py         #   LLM calling + context building
│   │   ├── proposal_queries.py #   Proposal queries + pagination
│   │   ├── release_group.py    #   Extract the scene release-group token from a filename
│   │   ├── filename_convention_learner.py # Learn per-release-group date-order conventions from the corpus
│   │   ├── date_convention.py  #   Apply learned date-order conventions as a gated fallback in the proposal path
│   │   ├── execution_queries.py#   Execution log queries + pagination
│   │   ├── execution_dispatch.py # Dispatch grouping, revoked-agent filter, chunking
│   │   ├── enqueue_router.py   #   Task-name → consumed-queue routing (avoids consumer-less default queue)
│   │   ├── companion.py        #   Companion file association
│   │   ├── companion_read.py   #   Bounded companion-sidecar read — the pure on-disk half
│   │   ├── dedup.py            #   Duplicate detection + resolution
│   │   ├── collision.py        #   Destination path collision detection
│   │   ├── pipeline.py         #   Pipeline stats, per-stage progress (get_stage_progress), file state queries
│   │   ├── pipeline_counters.py#   Maintained Redis per-job-type enqueued/completed counters (cache, not truth)
│   │   ├── stage_status.py     #   SQL ColumnElement per-stage predicate builders (done/failed/inflight/status CASE)
│   │   ├── scan_deletion.py    #   Ordered transactional cascade delete of a scan batch + dependent rows
│   │   ├── tracklist_scraper.py#   1001Tracklists SEARCH + the whole-host request schedule
│   │   ├── tracklist_query.py  #   Derive a clean 1001Tracklists search query from a messy, scene-tagged filename
│   │   ├── tracklist_result_scorer.py # Pick and validate the right 1001Tracklists search result
│   │   ├── tracklist_render.py #   Browser render engine for 1001Tracklists detail pages (headful, Turnstile retry)
│   │   ├── tracklist_parser.py #   Detail-page parser for 1001Tracklists
│   │   ├── tracklist_lookup_cache.py # Read/write layer over `tracklist_lookup_cache` — never spend a request twice
│   │   ├── tracklist_candidates.py # Candidate-set builder for the drain: classify, dedup, identify unique sets
│   │   ├── tracklist_candidate_queue.py # Turn the corpus into an ordered queue of unique sets the drain should look up
│   │   ├── tracklist_priority.py # Operator priority flags + the per-file lookup review the admin UI reads
│   │   ├── tracklist_drain.py  #   The 1001Tracklists drain engine: turns the built pieces into one resumable pass
│   │   ├── tracklist_matcher.py#   Fuzzy match tracklists to files
│   │   ├── cue_generator.py    #   CUE sheet generation
│   │   ├── discogs_matcher.py  #   Discogsography API adapter + fuzzy Discogs matching
│   │   ├── search_queries.py   #   Cross-entity full-text search (files + tracklists)
│   │   ├── tag_proposal.py     #   Compute merged tags from multiple sources
│   │   ├── tag_writer.py       #   Format-aware tag writing with verify-after-write
│   │   ├── tag_write_disk.py   #   Format-aware tag writing + verify-after-write — the pure on-disk half
│   │   ├── agent_bootstrap.py  #   Dev-agent seeding for the api lifespan
│   │   ├── agent_client.py     #   PhazeAgentClient (internal-agent HTTP wrapper)
│   │   ├── agent_liveness.py   #   Agent liveness classification (status pills)
│   │   ├── agent_task_router.py#   Controller-side per-agent SAQ enqueuer
│   │   ├── review.py           #   Degrade-safe read helpers for the Review diff workspaces
│   │   ├── stage_control.py    #   Raw saq_jobs backlog-mutation helpers for per-stage control
│   │   ├── scheduling_ledger.py#   Control-only scheduling-ledger service (recovery source of truth)
│   │   ├── route_control.py    #   Degrade-safe reader for the force-local routing override
│   │   ├── backends.py         #   Internal Backend protocol + its 3 implementations (local/kube/cloud)
│   │   ├── backend_selection.py#   Pure select_backend policy over the Backend substrate
│   │   ├── analysis_wire.py    #   Shared wire-format converters for essentia analysis features
│   │   ├── cloud_staging.py    #   Control-plane cloud-staging producer + re-drive helper
│   │   ├── cloud_budget.py     #   The durable cloud-budget ledger: its one writer + the pure policy that reads it
│   │   ├── s3_staging.py       #   Control-plane S3 object-staging service (presign/complete/abort)
│   │   └── kube_staging.py     #   Control-plane Kubernetes (Kueue) Job-staging service
│   ├── tasks/                  # SAQ async background jobs
│   │   ├── controller.py       #   SAQ controller settings (application-server entry point)
│   │   ├── agent_worker.py     #   SAQ agent_worker settings (agent process entry point)
│   │   ├── functions.py        #   process_file (full pipeline per file)
│   │   ├── metadata_extraction.py # extract_file_metadata
│   │   ├── proposal.py         #   generate_proposals (batch LLM)
│   │   ├── filename_convention.py # SAQ entry point for the filename-convention learner
│   │   ├── execution.py        #   execute_approved_batch
│   │   ├── scan.py             #   scan_directory (agent-side chunked file discovery)
│   │   ├── reenqueue.py        #   Control-side recover_orphaned_work: gated all-stages queue-loss recovery (Phase 42)
│   │   ├── scan_reaper.py      #   Control-side cron: reap stalled RUNNING scans (no-progress)
│   │   ├── aborting_reaper.py  #   Control-side every-minute cron: reap SAQ rows stuck in status='aborting' (phaze-e57w)
│   │   ├── active_reaper.py    #   Control-side every-minute cron: reap SAQ rows stranded in status='active' (phaze-o0n6)
│   │   ├── ledger_reaper.py    #   Control-side cron: clear scheduling_ledger rows whose work is finished
│   │   ├── stage_park_reconcile.py # Control-side cron: retro-heal stage backlog rows stranded SENTINEL-parked
│   │   ├── _saq_reap.py        #   The one stranded-row DELETE both key reapers issue (frozen started + per-row timeout + status CAS)
│   │   ├── tracklist.py        #   on-demand refresh: re-arm the drain for chosen pages
│   │   ├── tracklist_drain.py  #   SAQ entry point for the 1001Tracklists drain
│   │   ├── discogs.py          #   match tracklist tracks to Discogs releases
│   │   ├── heartbeat.py        #   30s agent heartbeat POST, run as a startup asyncio background task (Phase 46, not a cron)
│   │   ├── push.py             #   push_file: rsync-over-SSH push of media to compute scratch
│   │   ├── s3_upload.py        #   upload_file_s3: multipart-PUT upload to presigned URLs
│   │   ├── companion_read.py   #   read_companion_files: bounded sidecar read on the agent's media mount
│   │   ├── cue_write.py        #   write_cue_sheet: CUE file write on the agent's media mount
│   │   ├── tag_write.py        #   write_file_tags: mutagen tag write + verify on the agent, reported via HTTP
│   │   ├── submit_cloud_job.py #   Control-plane fast Kube-submit producer
│   │   ├── reconcile_cloud_jobs.py # Every-minute cron: reconcile in-flight K8s cloud jobs
│   │   ├── release_awaiting_cloud.py # Control-side tiered multi-backend drain (route AWAITING_CLOUD)
│   │   └── _shared/            #   Cross-process startup helpers (DB-free where required)
│   │       ├── agent_bootstrap.py  # Shared agent-startup helpers
│   │       ├── deterministic_key.py # Central before_enqueue deterministic-key + after_process completion hooks
│   │       ├── model_bootstrap.py  # Auto-download essentia weights when /models empty
│   │       ├── queue_defaults.py   # Shared SAQ before_enqueue Job defaults
│   │       ├── queue_factory.py    # Single PostgresQueue construction seam for the pipeline
│   │       ├── replay_safety.py    # The ledger replay-safety invariant: a scheduling_ledger payload must replay
│   │       └── stage_control.py    # Canonical per-stage control constants (DB-free)
│   ├── agent_watcher/          # Filesystem watcher service (file-server role, not a SAQ worker)
│   │   ├── __main__.py         #   Entry point: asyncio.run(main())
│   │   ├── observer.py         #   watchdog observer over agent scan_roots
│   │   ├── debouncer.py        #   mtime-stability debouncer (settle period)
│   │   ├── poster.py           #   POSTs settled files to /api/internal/agent/files
│   │   └── README.md           #   Watcher service docs
│   ├── prompts/                # LLM prompt templates
│   │   └── naming.md           #   Filename/path proposal prompt
│   └── templates/              # Jinja2 HTML templates (HTMX + Tailwind)
│       ├── base.html           #   Base layout (SRI-pinned CDN assets)
│       ├── shell/              #   v7.0 console shell (three-column DAG-centric layout)
│       │   ├── shell.html      #     Three-column shell served by GET / (actionable Summary landing)
│       │   ├── _stage_fragment.html #  The bare /s/<stage> fragment returned to an HX-Request swap
│       │   └── partials/       #     rail.html (DAG rail nav), header.html (⌘K + status strip),
│       │       │               #     cmdk_modal.html (⌘K command palette), record_host.html (record slide-in),
│       │       │               #     summary_overview.html (the actionable Summary landing),
│       │       │               #     _force_local_pill.html (force-local routing override pill)
│       ├── record/             #   Per-file record slide-in body
│       ├── pipeline/           #   /s/<stage> workspace partials (mostly partials/<stage>_workspace.html; rename,
│       │                       #   tagwrite and move all share changes_workspace.html) + stats_bar poll partial
│       ├── proposals/          #   Proposal approval UI
│       ├── execution/          #   Execution dashboard + audit log
│       ├── duplicates/         #   Duplicate resolution UI
│       ├── cue/                #   CUE sheet management UI
│       ├── search/             #   Cross-entity search UI
│       ├── tags/               #   Tag review UI
│       └── admin/              #   Admin agents UI
├── tests/                      # Test suite (95%+ coverage), reorganized into 8 CI-parallel
│   │                           # buckets (Phase 63-02; the fingerprint and services buckets were
│   │                           # removed with the engines, phaze-0jpe); see tests/BUCKETS.md for the mapping
│   ├── conftest.py             #   Fixtures + test DB setup
│   ├── buckets.json            #   Source of truth for the 8 bucket names + file->bucket map
│   ├── discovery/               #   File-discovery, agent_watcher, and core routing tests
│   ├── metadata/                #   Tag-extraction (mutagen) tests
│   ├── analyze/                  #   Essentia analysis (BPM/key/mood) tests
│   ├── identify/                 #   Proposal/LLM-naming tests
│   ├── review/                   #   Execution + review-workflow tests
│   ├── agents/                   #   Distributed-agent (file-server/compute) tests
│   ├── integration/               #   End-to-end + Alembic migration tests (test_migrations/)
│   └── shared/                    #   Config, template-helper, utils, and cross-cutting tests
├── alembic/                    # Database migrations (async template)
│   └── versions/               #   Migration scripts (24): the flattened 039_baseline_schema.py plus the
│                               #   post-baseline chain 040..062 (tag_write_log timestamptz, tracklist_version
│                               #   unique, scheduling_ledger redrive_attempt, discogs one-accepted-per-track,
│                               #   scan_batches no-duplicate-running, files.original_filename_repaired,
│                               #   046 drop fingerprint schema, 047 drop analysis.fingerprint,
│                               #   048 files (original_filename, id) btree, 049 all timestamps timestamptz,
│                               #   050 tracklist_lookup_cache, 051 tracklists propagation, 052 tracklist
│                               #   priority flags, 053 filename_convention, 054 cloud_job node-loss
│                               #   redrives, 055 cloud_budget ledger, 056 fix double-prefixed CHECK
│                               #   constraints, 057 cloud_job node-loss pending, 058 analysis
│                               #   completed_at btree, 059 tracklist-drain ARM/DISARM state, 060 drop sampled,
│                               #   061 durable duplicate-review plans, 062 reviewed-before tags/source versions)
│                               #   — head is 062; see database.md#migrations
├── .github/workflows/          # CI/CD pipelines
│   ├── ci.yml                  #   Main orchestrator
│   ├── code-quality.yml        #   Pre-commit hooks
│   ├── tests.yml               #   Pytest + Codecov
│   ├── security.yml            #   pip-audit, bandit, osv-scanner, Semgrep, TruffleHog, Trivy container scan
│   ├── docker-publish.yml      #   Build + publish container images
│   ├── docker-validate.yml     #   Validate Docker build/compose
│   ├── cleanup-cache.yml       #   Prune GitHub Actions caches
│   └── cleanup-images.yml      #   Prune published container images
├── scripts/                    # Utility, CI, and perf-tooling scripts
│   ├── download-models.sh      #   Download essentia ML models
│   ├── update-project.sh       #   Sync/update project tooling
│   ├── classify-changed-files.sh # Classify changed files as docs-only vs code (CI doc-only skip gate)
│   ├── derive-seat-name.sh     #   Derive the safe, collision-free Postgres/Redis identifier for a `test-db-for` seat
│   ├── ensure-pg-database.sh   #   Idempotently ensure Postgres databases exist on the test harness (TOCTOU-safe)
│   ├── redis-seat-registry.sh  #   Allocate/release/sweep the per-worktree Redis logical DBs behind `test-db-for`
│   ├── coverage_floor.py       #   Enforce per-module coverage floor from `coverage json` output
│   ├── normalize_schema_dump.py #  Normalize a pg_dump schema-only file for migration-chain equivalence
│   ├── seed_perf_corpus.py     #   Seed a synthetic ~200K-file corpus for perf measurement
│   ├── perf_explain.py         #   EXPLAIN (ANALYZE, BUFFERS) hot queries + time /pipeline/stats
│   ├── perf_analyze_workspace.py # Baseline the Analyze-workspace slowdown at 200K scale
│   ├── analyze_browser_soak.py #   Real-browser verification of the Analyze workspace at 200K scale
│   ├── capture_tracklist_render.py # Live-capture 1001Tracklists detail pages through the render engine
│   ├── capture_tracklist_search.py # Live-capture 1001Tracklists SEARCH-RESULT pages
│   ├── backfill_mojibake_filenames.py # One-shot operator backfill of files.original_filename_repaired (phaze-x4ux)
│   └── parity/                 #   Essentia-analysis parity fixtures (compare/dump/generate analysis, reference.wav)
├── docker-compose.yml          # Service orchestration
├── docker-compose.dev.yml      # Local development overlay (opt-in: just up-dev; never auto-merged)
├── docker-compose.agent.yml    # Distributed file-server agent stack
├── docker-compose.cloud-agent.yml # OCI A1 cloud compute-agent stack
├── Dockerfile                  # Multi-stage image (css-builder → base) shared by API, worker, agent, watcher;
│                               # the css-builder stage compiles Tailwind v4 with the pinned standalone binary (no Node)
├── Dockerfile.agent-arm64      # Production arm64 analysis agent — builds essentia from source (no aarch64 wheel exists)
├── Dockerfile.job              # x86 Kueue Job-runner image FROM the api base; adds only the one-shot CMD, zero new deps
├── justfile                    # Developer commands
├── pyproject.toml              # Project config + tool settings
└── uv.lock                     # Frozen dependency versions
```

## Shell templates & `/s/<stage>` routing

The v7.0 admin UI is a three-column DAG-centric console (see
[Architecture → User Interface](architecture.md#-user-interface--information-architecture-v70)).
Its structural templates live under `templates/shell/`, while each rail node's content is a
workspace partial under `templates/pipeline/partials/`:

| Template | Role |
| -------- | ---- |
| `templates/shell/shell.html` | The three-column shell served by `GET /` (the Summary overview is selected by default) |
| `templates/shell/partials/rail.html` | The DAG rail — the navigation spine (stage nodes + live counts) |
| `templates/shell/partials/header.html` | Header: wave logo, ⌘K trigger, and the compute/agent status strip |
| `templates/shell/partials/cmdk_modal.html` | The ⌘K command palette (unified search + commands) |
| `templates/shell/partials/record_host.html` + `templates/record/record_body.html` | The per-file record slide-in overlay |

`src/phaze/routers/shell.py` maps rail-node ids to workspace partials through the static
`STAGE_PARTIALS` whitelist. A rail click issues `GET /s/<stage>`, which returns only that
stage's workspace fragment to swap into the `#stage-workspace` target:

| `/s/<stage>` | Workspace partial |
| ------------ | ----------------- |
| `/s/summary` | `shell/partials/summary_overview.html` — the actionable `/` landing (phaze-tzy6s.9 replaced the DB-free SQ3-02 placeholder); `_render_stage` branches on `stage == "summary"` to build its context |
| `/s/files` | `pipeline/partials/files_workspace.html` — the Phase-87 per-file stage-matrix workspace (host wrapper for the `files_table_view.html` swap fragment) |
| `/s/discover` | `pipeline/partials/discover_workspace.html` |
| `/s/metadata` · `/s/analyze` | `pipeline/partials/{metadata,analyze}_workspace.html` |
| `/s/tracklist` | `pipeline/partials/tracklist_workspace.html` |
| `/s/propose` | `pipeline/partials/propose_workspace.html` |
| `/s/rename` · `/s/tagwrite` · `/s/move` | `pipeline/partials/changes_workspace.html` — ONE Changes Review workspace. phaze-tzy6s.11 / ADR-0008 deleted `rename_workspace.html`, `tagwrite_workspace.html` and `move_workspace.html`; the three stage keys are retained as compatibility aliases that all render this partial, so old bookmarks keep working |
| `/s/dedupe` · `/s/cue` | `pipeline/partials/{dedupe,cue}_workspace.html` |
| `/s/apply` | `pipeline/partials/apply_workspace.html` — Execute, with the phaze-tzy6s.12 preflight manifest |
| `/s/operations` · `/s/audit` · `/s/agents` | `UTILITY_PANES`, not `STAGE_PARTIALS`: `shell/partials/operations.html`, `execution/audit_log.html`, `admin/agents.html` |

`stage` is only ever matched against the `STAGE_PARTIALS` keys — it is never interpolated
into a template path (template-path-injection mitigation) — and an unknown stage returns
`404`. The legacy top-level page routes (`/proposals`, `/tracklists`, `/tags`, `/cue`,
`/duplicates`, `/preview`, `/pipeline`, `/search`) `302`-redirect into their corresponding
shell stage so existing bookmarks keep working.

## Module layering

The flat tree above lists *where* modules live; this graph shows *how they depend*. The core
rule is one-directional: `routers → services → models` (routers never touch the ORM directly,
models never import services). Two SAQ settings modules define the two-process split — the
control plane (`tasks/controller.py`) and the compute/file-server agent (`tasks/agent_worker.py`)
— which is why background jobs are grouped by which process registers them. The cloud-burst
dispatch fans out through the `Backend` protocol so the drain and the staging tasks never
hard-code a single backend.

```mermaid
graph TD
    subgraph edge["HTTP / UI"]
        routers[routers/*]
    end
    subgraph logic["Business logic"]
        services[services/*]
    end
    subgraph data["Persistence"]
        models[models/*]
    end
    routers --> services --> models

    subgraph procs["Two-process split (SAQ settings)"]
        controller[tasks/controller.py<br/>control plane]
        worker[tasks/agent_worker.py<br/>compute / file-server agent]
    end
    controller --> services
    worker --> services

    subgraph dispatch["Backend dispatch fan-out"]
        sel[services/backend_selection.py]
        be[services/backends.py<br/>Backend protocol]
        cloud[services/cloud_staging.py]
        kube[services/kube_staging.py]
        s3[services/s3_staging.py]
    end
    controller --> sel --> be
    be --> cloud
    be --> kube
    be --> s3

    subgraph ctasks["Cloud-burst tasks"]
        submit[tasks/submit_cloud_job.py]
        push[tasks/push.py]
        upload[tasks/s3_upload.py]
        reconcile[tasks/reconcile_cloud_jobs.py]
    end
    controller --> submit
    controller --> reconcile
    worker --> push
    worker --> upload
    submit --> kube
    upload --> s3
```
