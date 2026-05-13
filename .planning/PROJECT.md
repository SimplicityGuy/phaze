# Phaze

## What This Is

A music collection organizer that ingests ~200K music files (mp3, m4a, ogg, opus) and concert video streams, analyzes them for BPM/mood/style/key, uses AI to propose better filenames and destination paths, and provides an admin web UI to review and approve the renames/moves. Built as a Docker Compose stack with FastAPI, arq workers, PostgreSQL, and Redis. Designed for a single user managing a large personal archive of music and live concert recordings (primarily full sets from events like Coachella).

## Core Value

Get 200K messy music and concert files properly named, organized into logical folders, deduplicated, with rich metadata in Postgres — and provide a human-in-the-loop approval workflow so nothing moves without review.

## Current Milestone: v4.0 Distributed Agents

**Goal:** Split phaze into an application server (control plane: API, UI, Postgres, Redis, fileless workers) and one or more file servers (remote hosts running agents that own the music/video files, pull jobs locally, and write results back via HTTP) — so files can live anywhere while decisions stay on a single server.

**Target features:**
- Per-agent SAQ workers on each file server, pulling from the application server's Redis; absolute HTTP-only write-back boundary (no Postgres on file servers)
- `agent_id` stamped on `FileRecord` at scan time; unique key `(agent_id, original_path)`; new `agents` table with token-based auth
- Same Docker image, env-driven role; new `docker-compose.agent.yml` for file servers (worker + watcher + audfprint + panako); application server loses its `SCAN_PATH` + `MODELS_PATH` mounts
- User-initiated scan (UI form) + always-on `phaze-agent-watcher` service on each file server (watchdog lib, settle/debounce, sentinel scan batch)
- Per-file-server fingerprint sidecars (no cross-file-server fingerprint matching — documented v1 limitation)
- Group-by-file-server execution dispatch with per-PATCH ExecutionLog write-ahead audit preserved over HTTP
- Per-agent bearer tokens with `agent_id` derived from token on the application server (never from request body), private LAN, self-signed HTTPS, Redis `requirepass` + LAN-bound interface
- Task code reorg: `phaze.tasks.controller` (fileless, control role) vs `phaze.tasks.agent_worker` (file-bound, agent role); job payloads carry everything the agent needs
- Two-step Alembic migration with `legacy-application-server` backfill so existing v3.0 data survives

## Current State

**v3.0 shipped 2026-04-04.** Cross-service intelligence and file enrichment complete.

- 8,000+ lines of Python across 23 phases, 52 plans total (v1.0-v3.0)
- 650+ tests passing, 53/53 cumulative requirements satisfied (all milestones)
- Tech stack: FastAPI, SQLAlchemy (async), SAQ, litellm, essentia-tensorflow, mutagen, rapidfuzz, httpx, HTMX + Tailwind
- Docker Compose: api, worker, postgres, redis, audfprint, panako containers
- 12 Alembic migrations, 12 SQLAlchemy models, 3 fingerprint service containers
- Admin UI: proposals, duplicates, tracklists, pipeline dashboard, directory tree preview, unified search, Discogs linking, tag review, CUE management
- v3.0 added: unified FTS search with faceted filtering, Discogs cross-service linking with fuzzy matching and bulk-link, format-aware tag writing with 4-layer cascade (Discogs > tracklist > metadata > filename), CUE sheet generation with Discogs REM enrichment

## Previous State

<details>
<summary>v1.0 shipped 2026-03-30</summary>

Full pipeline operational: scan → analyze → propose → approve → execute.

- 7,975 lines of Python across 11 phases, 24 plans
- 282 tests passing, 19/19 requirements satisfied
- Tech stack: FastAPI, SQLAlchemy (async), arq, litellm, essentia-tensorflow, HTMX + Tailwind
- Docker Compose: api, worker, postgres, redis containers with health checks
- 4 Alembic migrations, 6 SQLAlchemy models, 28 file extensions classified

</details>

## Requirements

### Validated

- ✓ Containerized backend services running via Docker Compose — v1.0 Phase 1
- ✓ PostgreSQL database for all metadata and state — v1.0 Phase 1
- ✓ Alembic database migrations — v1.0 Phase 1, 10
- ✓ Recursive directory scanning for music/video/companion files — v1.0 Phase 2
- ✓ SHA256 hash computation and storage — v1.0 Phase 2
- ✓ Original filename and path recorded in PostgreSQL — v1.0 Phase 2
- ✓ File type classification (music, video, companion) — v1.0 Phase 2
- ✓ Companion files linked to media files via directory proximity — v1.0 Phase 3
- ✓ Exact duplicate detection via SHA256 hash grouping — v1.0 Phase 3
- ✓ arq + Redis task queue with bounded worker pool, retry with backoff, process pool — v1.0 Phase 4
- ✓ BPM detection for music files — v1.0 Phase 5
- ✓ Mood and style classification for music files — v1.0 Phase 5
- ✓ Analysis runs in parallel across worker pool — v1.0 Phase 4
- ✓ AI-powered filename proposals via litellm with batch prompting and structured output — v1.0 Phase 6
- ✓ Proposals stored as immutable records in PostgreSQL — v1.0 Phase 6
- ✓ Admin web UI with paginated proposal list, status filtering, bulk actions, keyboard shortcuts — v1.0 Phase 7
- ✓ Admin can approve/reject individual proposals with FileRecord state transition — v1.0 Phase 7, 11
- ✓ Safe file execution via copy-verify-delete protocol with proposed_path routing — v1.0 Phase 8, 11
- ✓ Append-only audit log for all file operations — v1.0 Phase 8
- ✓ Pipeline orchestration: scan→analyze→propose triggers via API endpoints — v1.0 Phase 9

- ✓ Audio tag extraction (ID3/Vorbis/MP4/FLAC/OPUS) feeding richer LLM context — v2.0 Phase 12
- ✓ Shared async engine pool replacing per-invocation engine creation — v2.0 Phase 12
- ✓ AI destination path proposals with collision detection and directory tree preview — v2.0 Phase 13
- ✓ Duplicate resolution UI with auto-scoring, side-by-side comparison, resolve/undo — v2.0 Phase 14
- ✓ 1001Tracklists integration with search, scrape, fuzzy match, periodic refresh — v2.0 Phase 15
- ✓ Dual fingerprint service (audfprint + Panako) with batch ingestion — v2.0 Phase 16
- ✓ Live set scanning with tracklist review, inline editing, approve/reject — v2.0 Phase 17

- ✓ Unified search across files, tracklists, and metadata with faceted filtering — v3.0 Phase 18
- ✓ Discogsography cross-service linking via HTTP API with fuzzy matching and confidence scores — v3.0 Phase 19
- ✓ Write corrected tags to destination copies with review UI, verify-after-write, and audit logging — v3.0 Phase 20
- ✓ CUE sheet generation from tracklist data with fingerprint-preferred timestamps and Discogs REM enrichment — v3.0 Phase 21

### Active

- File servers run agents that own files locally; the application server orchestrates and stores all state — v4.0
- HTTP-only boundary between agents and the application server (no shared filesystem, no shared database access) — v4.0
- Per-agent bearer token auth with `agent_id` derived from token, never from request body — v4.0
- Continuous file watcher service on each file server that streams new arrivals to the application server — v4.0
- Distributed approval execution: group approved proposals by agent and dispatch one sub-batch per file server — v4.0

### Out of Scope

- Cross-file-server fingerprint matching — per-agent fingerprint DB only in v4.0; document as limitation, defer to a later milestone
- Delete / move / rename detection in the file watcher — v4.0 watcher only handles `created` events; deferred
- Watcher catch-up on startup (rescan files that landed while watcher was down) — out of scope for v4.0; manual user-initiated scan covers this
- Natural language querying across services — deferred
- Acoustic near-duplicate detection via fingerprint similarity — deferred
- Cross-reference fingerprint matches with 1001tracklists — partially addressed by Discogs linking in v3.0, full cross-ref deferred
- Public network access — private network only
- Offline mode — real-time server tool, not a desktop app

## Context

- v1.0 + v2.0 shipped: full pipeline from scan → tag extract → analyze → propose (filename + path) → approve → execute
- ~200K files total, mix of music files and full concert video streams
- Concert videos are primarily recordings of live streams (YouTube streams from festivals, etc.)
- FileMetadata fully populated via mutagen tag extraction (ID3/Vorbis/MP4/FLAC/OPUS)
- Shared async engine pool eliminates per-invocation engine creation
- Dual fingerprint service (audfprint + Panako) with weighted scoring (60/40, 70% single-engine cap)
- 1001tracklists integration operational with monthly refresh cron
- This is a personal tool running on a home server, not a multi-user SaaS

## Constraints

- **Language**: Python 3.13 exclusively
- **Package manager**: uv only
- **Deployment**: Docker Compose on home server, private network
- **Database**: PostgreSQL
- **Scale**: Must handle ~200K files efficiently — batch processing and parallelization required
- **Naming format**: Live sets: `{Artist} - Live @ {Venue|Event} {YYYY.MM.DD}.{ext}`, Album tracks: `{Artist} - {Track #} - {Track Title}.{ext}`

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| PostgreSQL over SQLite | 200K files with complex metadata, relationships, and future cross-service queries need a real RDBMS | ✓ Good — handles async access, complex queries, JSON columns well |
| Organization before search | Getting files organized is the primary win; search/NLQ is a follow-on | ✓ Good — v1.0 delivers complete organization pipeline |
| Human-in-the-loop approval | No file moves without admin review — safety for a large, irreplaceable collection | ✓ Good — approval UI with undo prevents mistakes |
| Containerized services | Clean separation of concerns, reproducible deployment on home server | ✓ Good — Docker Compose with health checks works reliably |
| HTMX over React SPA | Single-user admin tool doesn't need SPA complexity | ✓ Good — zero build step, CDN delivery, full interactivity |
| arq over Celery | Async-first, simple config, Redis-native — single user doesn't need Celery complexity | ✓ Good — maintenance mode but stable |
| essentia-tensorflow for analysis | 34 pre-trained models, BPM/key/mood/style in one library | ✓ Good — baked into Docker image, process pool execution |
| litellm for LLM abstraction | Provider flexibility without vendor lock-in | ⚠️ Revisit — supply chain incident on 1.82.7/1.82.8, pin aggressively |
| copy-verify-delete protocol | Never direct move — SHA256 verification before deleting original | ✓ Good — safety for irreplaceable collection |
| State machine on FileRecord | Explicit state transitions (DISCOVERED→ANALYZED→PROPOSED→APPROVED→EXECUTED) | ✓ Good — enables pipeline dashboard stage counts |
| mutagen for tag read/write | Zero-dependency, supports all major tag formats | ✓ Good — reliable across ID3/Vorbis/MP4/FLAC/OPUS |
| audfprint + Panako hybrid | Complement each other: landmark-based vs tempo-robust | ✓ Good — weighted orchestrator with per-engine results |
| rapidfuzz for fuzzy matching | Fast token_set_ratio for tracklist-to-file matching | ✓ Good — weighted scoring with artist/event/date |
| Long-running fingerprint containers | HTTP API over subprocess calls for fingerprint services | ✓ Good — persistent DBs, Docker Compose integration |
| Distributed agents (v4.0) | Files stay on file servers; application server owns API, UI, Postgres, Redis | 🆕 Decided pre-v4.0 — enables remote file storage without losing centralized control |
| HTTP-only agent boundary (v4.0) | Agents have zero Postgres access; all writes go through `/api/internal/agent/*` | 🆕 Decided pre-v4.0 — seals DB inside application server, agents are version-skew tolerant |
| One SAQ queue per agent (v4.0) | `phaze-agent-<id>` queue per file server; enqueuer picks queue by `FileRecord.agent_id` | 🆕 Decided pre-v4.0 — matches SAQ's native pull model, clean per-agent maintenance |
| Per-agent bearer token auth (v4.0) | `agent_id` derived from token lookup on application server, never from request body | 🆕 Decided pre-v4.0 — eliminates spoofing risk, supports per-agent rotation |
| Per-agent fingerprint DB (v4.0) | Each file server runs its own audfprint+panako sidecars indexing only its files | 🆕 Decided pre-v4.0 — no cross-file-server fingerprint matching in v1; SHA-256 dedup still works |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition:**
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone:**
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-05-11 starting v4.0 milestone — Distributed Agents*
