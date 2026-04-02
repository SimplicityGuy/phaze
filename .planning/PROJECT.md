# Phaze

## What This Is

A music collection organizer that ingests ~200K music files (mp3, m4a, ogg, opus) and concert video streams, analyzes them for BPM/mood/style/key, uses AI to propose better filenames and destination paths, and provides an admin web UI to review and approve the renames/moves. Built as a Docker Compose stack with FastAPI, arq workers, PostgreSQL, and Redis. Designed for a single user managing a large personal archive of music and live concert recordings (primarily full sets from events like Coachella).

## Core Value

Get 200K messy music and concert files properly named, organized into logical folders, deduplicated, with rich metadata in Postgres — and provide a human-in-the-loop approval workflow so nothing moves without review.

## Current State

**v2.0 shipped 2026-04-02.** Metadata enrichment and tracklist integration complete.

- 5,966 lines of Python across 17 phases (6 in v2.0), 40 plans total
- 538 tests passing, 23/23 v2.0 requirements satisfied (42/42 cumulative)
- Tech stack: FastAPI, SQLAlchemy (async), arq, litellm, essentia-tensorflow, mutagen, rapidfuzz, HTMX + Tailwind
- Docker Compose: api, worker, postgres, redis, audfprint, panako containers
- 9 Alembic migrations, 9 SQLAlchemy models, 3 fingerprint service containers
- Admin UI: proposals, duplicates, tracklists, pipeline dashboard, directory tree preview

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

### Active

(No active requirements — next milestone not yet planned)

### Out of Scope

- Search frontend — deferred to v3+
- Natural language querying across services — deferred to v3+
- Discogsography cross-service linking — deferred to v3+ (tracklist infrastructure built in v2.0)
- Acoustic near-duplicate detection via fingerprint similarity — deferred to v3+
- Cross-reference fingerprint matches with 1001tracklists — deferred to v3+
- Write corrected tags to destination copies — deferred to v3+
- CUE sheet generation from tracklist data — deferred to v3+
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
*Last updated: 2026-04-02 after v2.0 milestone — Metadata Enrichment & Tracklist Integration*
