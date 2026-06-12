<!-- generated-by: gsd-doc-writer -->
# Project Structure

```
phaze/
├── src/phaze/                  # Application package
│   ├── config.py               # Pydantic settings (env vars, role split)
│   ├── constants.py            # File categories, extension map, tuning constants
│   ├── database.py             # Async SQLAlchemy engine + session factory
│   ├── main.py                 # FastAPI app factory with lifespan
│   ├── entrypoint.py           # Container entrypoint shim: runs cert bootstrap, then execvp's uvicorn
│   ├── cert_bootstrap.py       # Pre-uvicorn TLS/mTLS cert bootstrap for distributed agents (DB-free, idempotent)
│   ├── enums/                  # DB-free enums (importable without SQLAlchemy)
│   │   └── execution.py        #   ExecutionStatus enum (re-exported by models/execution.py)
│   ├── utils/                  # Pure helpers (no deps)
│   │   └── humanize.py         #   Relative-time formatter ("4m ago", "2h ago")
│   ├── scripts/                # Python-callable utility scripts
│   │   └── download_models.py  #   Fetch essentia weight files (shared by bash + agent bootstrap)
│   ├── static/                 # Static assets (favicons, web manifest, OG image)
│   ├── models/                 # SQLAlchemy ORM models
│   │   ├── base.py             #   DeclarativeBase + TimestampMixin
│   │   ├── file.py             #   FileRecord + FileState enum
│   │   ├── scan_batch.py       #   ScanBatch progress tracking
│   │   ├── metadata.py         #   FileMetadata (audio tags)
│   │   ├── analysis.py         #   AnalysisResult (BPM, key, mood, style)
│   │   ├── fingerprint.py      #   FingerprintResult (per-engine)
│   │   ├── proposal.py         #   RenameProposal + ProposalStatus
│   │   ├── execution.py        #   ExecutionLog (audit trail)
│   │   ├── tracklist.py        #   Tracklist + TracklistVersion + TracklistTrack
│   │   ├── file_companion.py   #   FileCompanion (companion-media join)
│   │   ├── agent.py            #   Agent (file-server identity for distributed agents)
│   │   ├── discogs_link.py     #   DiscogsLink (candidate Discogs release matches per track)
│   │   └── tag_write_log.py    #   TagWriteLog (append-only tag-write audit trail)
│   ├── routers/                # API + UI endpoints
│   │   ├── health.py           #   GET /health
│   │   ├── scan.py             #   File discovery scan
│   │   ├── pipeline.py         #   Pipeline dashboard + processing triggers
│   │   ├── pipeline_scans.py   #   Admin scan trigger + HTMX scan-batch polling
│   │   ├── proposals.py        #   Proposal review + approval UI
│   │   ├── execution.py        #   Batch execution + SSE progress
│   │   ├── preview.py          #   Directory tree preview
│   │   ├── duplicates.py       #   Duplicate resolution UI
│   │   ├── tracklists.py       #   Tracklist management UI
│   │   ├── companion.py        #   Companion file association
│   │   ├── cue.py              #   CUE sheet management UI (generation + batch)
│   │   ├── search.py           #   Unified cross-entity search UI
│   │   ├── tags.py             #   Tag review UI (side-by-side compare, inline edit, write)
│   │   ├── admin_agents.py     #   Admin agents page + HTMX table partial
│   │   └── agent_*.py          #   Distributed-agent internal API (12 routers under /api/internal/agent):
│   │       │                   #     auth, identity, heartbeat, files, metadata, fingerprint,
│   │       │                   #     analysis, proposals, execution, exec_batches,
│   │       │                   #     scan_batches, tracklists
│   ├── schemas/                # Pydantic request/response models
│   │   ├── scan.py             #   Scan API schemas
│   │   ├── companion.py        #   Companion/duplicate schemas
│   │   ├── pipeline_scans.py   #   Pipeline scan-trigger schemas
│   │   ├── agent_tasks.py      #   Agent task-routing payload schemas
│   │   └── agent_*.py          #   Distributed-agent contract schemas (DB-free, loaded in agent worker):
│   │       │                   #     identity, heartbeat, files, metadata, fingerprint, analysis,
│   │       │                   #     proposals, execution, exec_batches, scan_batches, tracklists
│   ├── services/               # Business logic
│   │   ├── ingestion.py        #   File discovery, hashing, bulk upsert
│   │   ├── hashing.py          #   Shared hashing utilities
│   │   ├── metadata.py         #   Tag extraction via mutagen
│   │   ├── analysis.py         #   BPM/key/mood via essentia
│   │   ├── analysis_enqueue.py #   FastAPI-free producer for process_file jobs (deterministic key + payload)
│   │   ├── fingerprint.py      #   Multi-engine fingerprint orchestrator
│   │   ├── proposal.py         #   LLM calling + context building
│   │   ├── proposal_queries.py #   Proposal queries + pagination
│   │   ├── execution_queries.py#   Execution log queries + pagination
│   │   ├── execution_dispatch.py # Dispatch grouping, revoked-agent filter, chunking
│   │   ├── enqueue_router.py   #   Task-name → consumed-queue routing (avoids consumer-less default queue)
│   │   ├── companion.py        #   Companion file association
│   │   ├── dedup.py            #   Duplicate detection + resolution
│   │   ├── collision.py        #   Destination path collision detection
│   │   ├── pipeline.py         #   Pipeline stats, per-stage progress (get_stage_progress), file state queries
│   │   ├── pipeline_counters.py#   Maintained Redis per-job-type enqueued/completed counters (cache, not truth)
│   │   ├── scan_deletion.py    #   Ordered transactional cascade delete of a scan batch + dependent rows
│   │   ├── tracklist_scraper.py#   1001Tracklists web scraper
│   │   ├── tracklist_matcher.py#   Fuzzy match tracklists to files
│   │   ├── cue_generator.py    #   CUE sheet generation
│   │   ├── discogs_matcher.py  #   Discogsography API adapter + fuzzy Discogs matching
│   │   ├── search_queries.py   #   Cross-entity full-text search (files + tracklists)
│   │   ├── tag_proposal.py     #   Compute merged tags from multiple sources
│   │   ├── tag_writer.py       #   Format-aware tag writing with verify-after-write
│   │   ├── agent_bootstrap.py  #   Dev-agent seeding for the api lifespan
│   │   ├── agent_client.py     #   PhazeAgentClient (internal-agent HTTP wrapper)
│   │   ├── agent_liveness.py   #   Agent liveness classification (status pills)
│   │   └── agent_task_router.py#   Controller-side per-agent SAQ enqueuer
│   ├── tasks/                  # SAQ async background jobs
│   │   ├── controller.py       #   SAQ controller settings (application-server entry point)
│   │   ├── agent_worker.py     #   SAQ agent_worker settings (agent process entry point)
│   │   ├── functions.py        #   process_file (full pipeline per file)
│   │   ├── metadata_extraction.py # extract_file_metadata
│   │   ├── fingerprint.py      #   fingerprint_file (multi-engine)
│   │   ├── proposal.py         #   generate_proposals (batch LLM)
│   │   ├── execution.py        #   execute_approved_batch
│   │   ├── scan.py             #   scan_live_set (fingerprint matching)
│   │   ├── reenqueue.py        #   Control-side reboot recovery: re-enqueue DISCOVERED files for analysis
│   │   ├── scan_reaper.py      #   Control-side cron: reap stalled RUNNING scans (no-progress)
│   │   ├── tracklist.py        #   scrape/search/refresh tracklists
│   │   ├── discogs.py          #   match tracklist tracks to Discogs releases
│   │   ├── heartbeat.py        #   30s cron: POST agent heartbeat
│   │   ├── pool.py             #   ProcessPoolExecutor for CPU work
│   │   └── _shared/            #   Cross-process startup helpers (DB-free where required)
│   │       ├── agent_bootstrap.py  # Shared agent-startup helpers
│   │       ├── deterministic_key.py # Central before_enqueue deterministic-key + after_process completion hooks
│   │       ├── model_bootstrap.py  # Auto-download essentia weights when /models empty
│   │       └── queue_defaults.py   # Shared SAQ before_enqueue Job defaults
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
│       ├── _partials/          #   Shared cross-page partials
│       ├── pipeline/           #   Pipeline dashboard (9-node SVG DAG canvas)
│       ├── proposals/          #   Proposal approval UI
│       ├── execution/          #   Execution dashboard + audit log
│       ├── duplicates/         #   Duplicate resolution UI
│       ├── tracklists/         #   Tracklist management UI
│       ├── preview/            #   Directory tree preview
│       ├── cue/                #   CUE sheet management UI
│       ├── search/             #   Cross-entity search UI
│       ├── tags/               #   Tag review UI
│       └── admin/              #   Admin agents UI
├── services/                   # Fingerprint microservices
│   ├── audfprint/              #   Landmark-based fingerprinting
│   └── panako/                 #   Tempo-robust fingerprinting
├── tests/                      # Test suite (85%+ coverage)
│   ├── conftest.py             #   Fixtures + test DB setup
│   ├── test_models/            #   ORM model tests
│   ├── test_routers/           #   Endpoint integration tests
│   ├── test_schemas/           #   Pydantic schema tests
│   ├── test_services/          #   Business logic unit tests
│   ├── test_tasks/             #   SAQ job tests
│   ├── test_agent_watcher/     #   Watcher service tests
│   ├── test_config/            #   Settings + role-split tests
│   ├── test_migrations/        #   Alembic migration tests
│   ├── test_deployment/        #   Docker/deployment hardening tests
│   ├── test_scripts/           #   Utility-script tests
│   ├── test_utils/             #   Helper tests
│   └── test_template_helpers/  #   Template/Jinja helper tests
├── alembic/                    # Database migrations (async template)
│   └── versions/               #   Migration scripts (001-019)
├── .github/workflows/          # CI/CD pipelines
│   ├── ci.yml                  #   Main orchestrator
│   ├── code-quality.yml        #   Pre-commit hooks
│   ├── tests.yml               #   Pytest + Codecov
│   ├── security.yml            #   pip-audit, bandit, Semgrep, Trivy
│   ├── docker-publish.yml      #   Build + publish container images
│   ├── docker-validate.yml     #   Validate Docker build/compose
│   ├── cleanup-cache.yml       #   Prune GitHub Actions caches
│   └── cleanup-images.yml      #   Prune published container images
├── scripts/                    # Utility scripts
│   ├── download-models.sh      #   Download essentia ML models
│   └── update-project.sh       #   Sync/update project tooling
├── docker-compose.yml          # Service orchestration
├── docker-compose.override.yml # Local development overrides
├── docker-compose.agent.yml    # Distributed file-server agent stack
├── Dockerfile                  # Single-stage image (shared by API, worker, agent, watcher)
├── justfile                    # Developer commands
├── pyproject.toml              # Project config + tool settings
└── uv.lock                     # Frozen dependency versions
```
