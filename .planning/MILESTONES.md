# Milestones

## v5.0 Cloud Burst Analysis (Shipped: 2026-06-26)

**Phases completed:** 5 phases, 23 plans, 39 tasks
**Requirements:** 19/19 satisfied (CLOUDIMG, CLOUDAGENT, CLOUDROUTE, CLOUDPIPE, CLOUDDEPLOY)

**Delivered:** Long-duration audio that times out locally is now analyzed unattended on a free OCI Ampere A1 (arm64) compute agent over Tailscale — duration-routed, rsync-pushed, sha256-verified, and reverted to all-local by a single master toggle.

**Key accomplishments:**

- **Phase 47 — Official arm64 essentia image:** `Dockerfile.agent-arm64` builds essentia from source (the wheel is x86-only) on Python 3.13 + TF 2.20.0, built on a native `ubuntu-24.04-arm` CI runner (no QEMU) and published to GHCR only after a numeric-parity guard compares arm64 `analyze_file` output against an x86 golden (BPM/key exact, model scores within epsilon).
- **Phase 48 — Compute-agent type:** a media-less `kind="compute"` agent (empty scan roots, no app-ORM access, DIST-04) that drains its per-agent SAQ queue and PUTs results over HTTP, surfaced on the Agents admin page with a kind badge.
- **Phase 49 — Duration routing & backfill:** a per-file duration router holds long files in `AWAITING_CLOUD` (never silently analyzed locally) and a ledger-scoped backfill re-drives the timed-out long files — no whole-backlog over-enqueue.
- **Phase 50 — Push pipeline:** a `stage_cloud_window` cron keeps ≤N files staged-or-in-flight; `push_file` rsyncs over SSH-over-Tailscale (shell-free argv, pinned host keys, 0600 secret temps); the compute agent sha256-verifies the scratch copy before analyzing and cleans it up; idempotent, ledger-tracked re-drive.
- **Phase 51 — Deployment, config & docs:** `docker-compose.cloud-agent.yml` (arm64, host-Tailscale, no media, named scratch), the `cloud_burst_enabled` master toggle gating all three cloud entry points, the homelab OCI A1 + Tailscale-ACL + least-privilege Postgres broker runbook, and the full config/docs surface.

**Post-audit hardening (PRs #161/#162):** cloud-agent compose `python3 -m saq` start command (the `uv run` override would have prevented the arm64 container from starting), `compute_scratch_dir` fail-fast guard, scratch-dir-skew diagnostic, and the WR-03 push-timeout-coupling guard.

**Deferred (deployment-gated, unblock on the live OCI A1 rollout):** 48 live admin-badge render, 50-UAT tests 4-7 (real rsync transfer / mismatch / recovery). See STATE.md Deferred Items.

---

## v4.0 Distributed Agents (Shipped: 2026-05-17)

**Phases completed:** 6 phases, 47 plans

**Delivered:** Phaze is now a two-host system — an application-server control plane (API, UI, Postgres, Redis, fileless workers; no file mounts) and one or more file-server agents that own music/video files locally, pull jobs from per-agent SAQ queues, and write every state change back over authenticated HTTPS.

**Key accomplishments:**

- `agents` table + `agent_id` columns on FileRecord/ScanBatch, two-step Alembic migration (012 add+backfill, 013 NOT NULL+UQ swap) with `legacy-application-server` seed preserving v3.0 corpus end-to-end
- Internal `/api/internal/agent/*` HTTP surface (files, metadata, fingerprint, analysis, tracklists, proposals, execution-log, scan-batches, exec-batches, heartbeat, whoami) with token-hash auth deriving `agent_id` from bearer token — never from request body — and 403-before-state-machine cross-tenant guard on every multi-tenant route
- Idempotent natural-key upserts across the agent surface: `(agent_id, original_path)`, `file_id`, `proposal_id`, agent-generated log UUIDs; replays produce zero duplicate rows and zero same-state DB writes
- Task code split: `phaze.tasks.controller` (fileless: generate_proposals, tracklist scrapers, refresh cron) vs `phaze.tasks.agent_worker` (file-bound: process_file, extract_file_metadata, fingerprint_file, scan_live_set, execute_approved_batch); subprocess import-boundary test enforces no `phaze.database` in the agent chain
- `PHAZE_ROLE={control,agent}` env-driven settings split (ControlSettings vs AgentSettings via `get_settings()` factory); same Docker image for both roles; per-agent SAQ queue (`phaze-agent-<id>`); AgentTaskRouter picks queue from `FileRecord.agent_id`
- `PhazeAgentClient` with tenacity retry funnel, 4-class error hierarchy, bearer token never stored as instance attribute (lives only in httpx headers); respx contract tests across all routes
- `phaze-agent-watcher` service: watchdog observer + asyncio-owned single-loop sweep, mtime settle (10s default) + stuck-file cap (3600s); LIVE-sentinel ScanBatch per agent; admin "Trigger Scan" form with HTMX agent-roots swap + 2s/5s polling partials
- `scan_directory` agent task with chunked HTTP upserts (500/chunk), per-chunk PATCH progress, terminal PATCH; same `/files` endpoint serves bulk scans and per-file watcher events
- Distributed execution dispatch: group-by-`FileRecord.agent_id` (in-Python `defaultdict`), one `execute_approved_batch` sub-job per affected agent under shared parent `batch_id`; per-proposal terminal progress POST; SAQ-meta UUID lift for retry-safe `execution_log_id` and `progress_request_id`
- Unified SSE progress aggregating across agents (3 Jinja partials rendered via `_render_partial()` for Semgrep XSS compliance); per-agent breakdown table; revoked-agent banner
- Per-file-server fingerprint sidecars (audfprint + panako allow-list validator blocks non-localhost URLs at config load); cross-file-server fingerprint matching documented as v4.0 limitation with dismissible banner on Duplicate Resolution page
- Self-signed internal CA + leaf x509 generated on first start in the api container via `phaze.cert_bootstrap` + pre-uvicorn entrypoint shim (signals/PID-1 propagate cleanly); `PhazeAgentClient` honors `verify=` kwarg defaulting to `AgentSettings.agent_ca_file`; wrong-CA → ConnectError integration test
- Redis hardening: `requirepass` + `${REDIS_BIND_IP:-127.0.0.1}` LAN bind on app-server compose; `AgentSettings` rejects passwordless `redis_url` at boot when `PHAZE_AGENT_ENV=production`
- Application-server `docker-compose.yml` stripped of `SCAN_PATH`/`MODELS_PATH` mounts and watcher/audfprint/panako services; YAML-parse tests enforce filesystem isolation
- New `docker-compose.agent.yml` (4 services: worker, watcher, audfprint, panako) + `.env.example.agent`; `${SCAN_PATH:?...}` fail-fast on misconfigured file-server hosts; docker-publish.yml extended for both compose-file image tags
- `phaze.scripts.download_models` Python helper + `phaze.tasks._shared.model_bootstrap` wired into agent_worker/watcher startup (rejects partial-download `.part` state); `just download-models` populates per-file-server `/models` volume
- 30-second SAQ CronJob heartbeat from each agent updating `agents.last_seen_at`; Agents admin page (`/admin/agents`) with liveness classifier (alive/stale/revoked), queue depth, last-seen humanize helper; HTMX 5s auto-refresh
- Operator workflow: `just up` (app-server), `just up-agent` (each file-server), `just up-all` (single-host dev); full deployment walkthrough in `docs/deployment.md`; PROJECT.md Constraints + Deployment subsections updated

---

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
