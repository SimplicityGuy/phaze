# Milestones

## v3.0 Cross-Service Intelligence & File Enrichment (Shipped: 2026-04-04)

**Phases completed:** 4 phases, 11 plans, 22 tasks

**Key accomplishments:**

- PostgreSQL full-text search with tsvector GENERATED columns, GIN indexes, and cross-entity UNION ALL search service returning ranked, paginated results from files and tracklists
- Search page with FastAPI router, HTMX partial swaps, Alpine.js collapsible filters, type-badged results table, and nav bar integration as first tab
- DiscogsLink model, discogsography HTTP adapter with rapidfuzz confidence scoring, and SAQ background task for batch matching tracklist tracks to Discogs releases
- Five HTMX endpoints and three template partials for Discogs match triggering, inline candidate review with accept/dismiss, and bulk-link functionality
- Discogs release UNION ALL branch in unified search with purple pill badges and accepted-only filtering per D-09
- TagWriteLog audit model, tag proposal cascade merge (tracklist > metadata > filename), and format-aware tag writer with verify-after-write for MP3/OGG/FLAC/OPUS/M4A via mutagen
- Tag review page with side-by-side comparison, inline editing of proposed values, Write Tags CTA, format/status badges, and 10 integration tests
- Fixed two HTMX wiring bugs: collapsed Write Tags button now computes proposed tags server-side, post-write response targets main row by stable ID with OOB detail row cleanup
- Pure-Python CUE sheet generator with 75fps timestamp conversion, Discogs REM enrichment, version suffix naming, and UTF-8 BOM file writing
- CUE management page with stats, batch generation, inline tracklist card buttons, and nav tab integration
- Source badges on CUE management rows with fingerprint-first sorting, and Regenerate CUE button state on tracklist cards via HX-Target detection

---

## v2.0 Metadata Enrichment & Tracklist Integration (Shipped: 2026-04-02)

**Phases completed:** 6 phases, 16 plans, 31 tasks

**Key accomplishments:**

- Shared async engine pool for arq workers with FileMetadata column expansion and METADATA_EXTRACTED pipeline stage
- 1. [Rule 3 - Blocking] Added track_number/duration/bitrate to FileMetadata model
- Tag data piped to LLM context via build_file_context, dual-state convergence gate prevents proposal generation until both metadata extraction and audio analysis complete
- Extended LLM prompt with 3-step directory path decision tree and added proposed_path field to FileProposalResponse with slash normalization in store_proposals
- SQL collision detection service, recursive tree builder, and /preview/ route with collapsible directory tree for approved proposals
- Wired collision detection and proposed_path display into the approval table and execution router, adding a Destination column with three visual states and an execution gate that blocks batch start when duplicate destination paths exist
- Duplicate resolution backend with auto-selection scoring (bitrate > tags > path), metadata-enriched queries, resolve/undo state machine, and stats aggregation
- FastAPI router + 9 Jinja2 templates delivering full duplicate resolution workflow: card-per-group layout, expandable comparison tables with green best-value highlighting, radio pre-selection, resolve/undo via HTMX OOB swaps, 10-second undo toast, bulk Accept All, and nav integration
- Three-table tracklist data model with async scraper (rate-limited) and weighted fuzzy matcher using rapidfuzz token_set_ratio
- arq task functions for tracklist search/scrape/refresh with monthly cron job, plus full HTMX admin UI with card layout, filter tabs, expand/collapse tracks, and undo toasts
- Two Docker containers (audfprint + Panako) with FastAPI HTTP APIs exposing /ingest, /query, /health endpoints, integrated into Docker Compose with named volumes and internal networking
- FingerprintEngine Protocol with httpx adapters, weighted orchestrator (60/40, 70% single-engine cap), FingerprintResult model, and Alembic migration
- arq fingerprint_file task with per-engine result storage, pipeline trigger/progress endpoints, FINGERPRINTED stage in pipeline stats, and justfile commands
- Tracklist source/status columns, track confidence, fingerprint dataclass extensions, and scan_live_set arq task for fingerprint-to-tracklist pipeline
- Scan tab with batch file selection, arq-based fingerprint scanning with polling progress, and source/status badge partials on tracklist cards
- HTMX inline editing, approve/reject status transitions, bulk reject low-confidence tracks, and fingerprint track detail with color-coded confidence badges

---

## v1.0 MVP (Shipped: 2026-03-30)

**Phases completed:** 11 phases, 24 plans, 43 tasks

**Key accomplishments:**

- Python 3.13 project skeleton with pyproject.toml (ruff/mypy/pytest config), pre-commit hooks with frozen SHAs, Docker Compose stack (api/worker/postgres/redis), and justfile developer commands
- FastAPI app with health endpoint, 5 SQLAlchemy models (files/metadata/analysis/proposals/execution_log), async DB layer with pydantic-settings config, and Alembic initial migration creating the full v1 schema
- Directory scanning with chunked SHA-256 hashing, NFC path normalization, extension classification, and PostgreSQL bulk upsert with ON CONFLICT resumability
- REST API endpoints for triggering file discovery scans and querying status, with Pydantic schemas, background task management, and path validation
- FileCompanion join table with directory-based companion association and SHA256 duplicate group detection services
- REST API endpoints for companion association (POST) and duplicate detection (GET) with paginated responses and full integration tests
- arq task queue with WorkerSettings, skeleton process_file with exponential retry backoff, and ProcessPoolExecutor for CPU-bound audio analysis
- ArqRedis pool wired into FastAPI lifespan for job enqueuing, docker-compose worker placeholder replaced with real arq command, justfile worker management commands added
- essentia-tensorflow dependency with 68-file model download script baked into Docker image, plus models_path config
- Essentia-based audio analysis service with 34 model registry (33 characteristic + 1 genre), BPM/key/mood/style detection, wired into arq worker via process pool
- litellm dependency pinned, Settings extended with 5 LLM config fields, Pydantic response models for structured output, naming prompt template with live set and album track rules, and companion cleaning + context building helpers tested
- ProposalService calling litellm acompletion with structured output, Redis rate limiting with configurable RPM, immutable proposal storage, and generate_proposals arq batch job wired into WorkerSettings
- Read-only proposal list UI with HTMX-powered filtering, search, sorting, pagination, and stats bar using Jinja2 templates and Tailwind CSS
- HTMX approve/reject/undo with OOB stats updates, expandable row details, bulk actions, keyboard navigation, and toast notifications
- Execution UI with SSE live progress, paginated audit log, execute button, and navigation bar connecting Proposals and Audit Log pages
- Pipeline trigger endpoints and dashboard wiring scan->analyze->propose flow via API with background enqueue for 200K+ file scale
- ORM model fix to match DB-level constraint from migration 002
- Fixed four v1.0 audit gaps: APPROVED state transition, .opus extension, proposed_path execution routing, and settings_batch_size dashboard injection
- Synced VERIFICATION statuses, SUMMARY requirements-completed fields, Phase 9 Nyquist validation, and config.json EOF to match actual implementation state
- Phase 10 Nyquist VALIDATION.md created and full quality gate sweep confirmed green (282 tests, 17 pre-commit hooks, ruff, mypy)

---
