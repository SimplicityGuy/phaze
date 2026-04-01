# Phaze

## What This Is

A music collection organizer that ingests ~200K music files (mp3, m4a, ogg, opus) and concert video streams, analyzes them for BPM/mood/style/key, uses AI to propose better filenames and destination paths, and provides an admin web UI to review and approve the renames/moves. Built as a Docker Compose stack with FastAPI, arq workers, PostgreSQL, and Redis. Designed for a single user managing a large personal archive of music and live concert recordings (primarily full sets from events like Coachella).

## Core Value

Get 200K messy music and concert files properly named, organized into logical folders, deduplicated, with rich metadata in Postgres — and provide a human-in-the-loop approval workflow so nothing moves without review.

## Current Milestone: v2.0 Metadata Enrichment & Tracklist Integration

**Goal:** Enrich the file corpus with audio tags, tracklist data, and audio fingerprinting — building queryable infrastructure for cross-service linking and automated track identification.

**Target features:**
- Audio tag extraction (mutagen) → populate FileMetadata → richer LLM context
- AI destination path proposals using v1.0 naming format
- Duplicate resolution workflow in admin UI
- 1001tracklists integration (search, scrape, fuzzy-match, store)
- Periodic tracklist refresh for unresolved IDs (randomized, monthly minimum)
- Audio fingerprinting (audfprint + Panako hybrid) → fingerprint all files during ingestion, scan live sets to generate proposed tracklists for review
- Fingerprint service as a long-running container with API/message interface

## Previous State

**v1.0 shipped 2026-03-30.** Full pipeline operational: scan → analyze → propose → approve → execute.

- 7,975 lines of Python across 11 phases, 24 plans
- 282 tests passing, 19/19 requirements satisfied
- Tech stack: FastAPI, SQLAlchemy (async), arq, litellm, essentia-tensorflow, HTMX + Tailwind
- Docker Compose: api, worker, postgres, redis containers with health checks
- 4 Alembic migrations, 6 SQLAlchemy models, 28 file extensions classified

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

### Active

- [ ] Use AI to propose destination paths for file organization (proposed_path wiring exists, LLM prompt needs path generation)
- [ ] Resolve duplicate files (human decision via approval UI, detection done in v1.0 Phase 3)
- [ ] Extract and use existing audio tags (ID3, Vorbis, MP4) for richer LLM context (FileMetadata model scaffolded)

### Out of Scope

- Search frontend — deferred to post-v2
- Natural language querying across services — deferred to post-v2
- Discogsography cross-service linking — deferred to v3 (tracklist infrastructure built in v2.0)
- Public network access — private network only
- Offline mode — real-time server tool, not a desktop app

## Context

- v1.0 shipped with full pipeline: scan → analyze → propose → approve → execute
- ~200K files total, mix of music files and full concert video streams
- Concert videos are primarily recordings of live streams (YouTube streams from festivals, etc.)
- FileMetadata model and table exist but are unpopulated (mutagen integration in v2.0)
- Task session creates new engine per invocation — acceptable for v1 but worth pooling for v2 scale
- This is a personal tool running on a home server, not a multi-user SaaS
- 1001tracklists.com has documented HTTP endpoints for search (POST) and detail pages (POST) — no headless browser needed
- Audio fingerprinting: audfprint (Python-native, landmark-based) and Panako (Java, tempo-robust) as hybrid approach with weighted scoring
- Fingerprint service runs as a long-running container with API/message interface, not subprocess calls

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
*Last updated: 2026-04-01 after Phase 14 complete — Duplicate resolution UI with card-per-group layout, scoring, and resolve/undo workflow*
