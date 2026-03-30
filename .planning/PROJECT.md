# Phaze

## What This Is

A music collection organizer that ingests ~200K music files (mp3, m4a, ogg) and concert video streams, fingerprints and analyzes them, uses AI to propose better filenames and destination paths, and provides an admin web UI to review and approve the renames/moves. Designed for a single user managing a large personal archive of music and live concert recordings (primarily full sets from events like Coachella).

## Core Value

Get 200K messy music and concert files properly named, organized into logical folders, deduplicated, with rich metadata in Postgres — and provide a human-in-the-loop approval workflow so nothing moves without review.

## Requirements

### Validated

- ✓ Containerized backend services running via Docker Compose — Phase 1
- ✓ PostgreSQL database for all metadata and state — Phase 1
- ✓ Companion files linked to media files via directory proximity — Phase 3
- ✓ Exact duplicate detection via SHA256 hash grouping, flagged for human review — Phase 3
- ✓ arq + Redis task queue with bounded worker pool, retry with backoff, and process pool for CPU-bound work — Phase 4
- ✓ Audio analysis for BPM, mood, style, and musical key using essentia-tensorflow with 34 pre-trained models, running through arq worker pool via ProcessPoolExecutor — Phase 5
- ✓ AI-powered filename proposals via litellm (batch prompting, structured output) stored as immutable records with extracted metadata context — Phase 6
- ✓ Admin web UI to review and approve/reject proposed renames with paginated table, status filtering, bulk actions, keyboard shortcuts, and undo — Phase 7
- ✓ Safe file execution via copy-verify-delete protocol with append-only audit log — Phase 8
- ✓ Pipeline orchestration: scan→analyze→propose triggers via explicit API endpoints, pipeline dashboard with stage counts, separate OUTPUT_PATH volume for writes — Phase 9

### Active

- [ ] Use AI to propose destination paths for file organization
- [ ] Resolve duplicate files (human decision via approval UI, detection done in Phase 3)

### Out of Scope

- Search frontend — deferred to post-v1
- Natural language querying across services — deferred to post-v1
- 1001tracklists integration/scraping — deferred to post-v1
- Discogsography cross-service linking — deferred to post-v1
- Public network access — private network only

## Context

- Files are mostly messy/chaotic — inconsistent naming, scattered across locations, minimal existing organization
- ~200K files total, mix of music files and full concert video streams
- Concert videos are primarily recordings of live streams (YouTube streams from festivals, etc.)
- Existing Python prototypes for music analysis (style/BPM/mood) — process one file at a time, designed to be parallelized
- Existing scraping code from earlier project iteration for 1001tracklists (future use)
- Discogsography is a separate project deployed locally with an accessible API (future integration)
- This is a personal tool running on a home server, not a multi-user SaaS

## Constraints

- **Language**: Python 3.13 exclusively
- **Package manager**: uv only
- **Deployment**: Docker Compose on home server, private network
- **Database**: PostgreSQL
- **Scale**: Must handle ~200K files efficiently — batch processing and parallelization required
- **Existing code**: Must integrate with provided analysis prototypes and respect their per-file interface
- **Naming format**: Live sets: `{Artist} - Live @ {Venue|Event} {YYYY.MM.DD}.{ext}`, Album tracks: `{Artist} - {Track #} - {Track Title}.{ext}`

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| PostgreSQL over SQLite | 200K files with complex metadata, relationships, and future cross-service queries need a real RDBMS | — Pending |
| Organization before search | Getting files organized is the primary win; search/NLQ is a follow-on | — Pending |
| Human-in-the-loop approval | No file moves without admin review — safety for a large, irreplaceable collection | — Pending |
| Containerized services | Clean separation of concerns, reproducible deployment on home server | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd:transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd:complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-03-30 after Phase 11 completion — all v1.0 milestone phases complete*
