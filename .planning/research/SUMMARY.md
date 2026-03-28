# Project Research Summary

**Project:** Phaze
**Domain:** Music file management, batch processing, and AI-powered organization
**Researched:** 2026-03-27
**Confidence:** HIGH

## Executive Summary

Phaze is a music collection organizer for ~200K audio and video files that uses AI to propose standardized filenames and folder structures, with a human-in-the-loop approval workflow before any file operations execute. This class of tool (beets, MusicBrainz Picard, AudioRanger) is well-understood, but Phaze differentiates by replacing rigid template-based renaming with LLM-powered proposals and by providing a web-based batch approval UI instead of desktop-only or CLI interfaces. The recommended approach is an async Python monolith (FastAPI + arq workers) sharing a PostgreSQL database, with HTMX for the admin UI -- no SPA, no microservices, no build pipeline.

The architecture follows a pipeline state machine pattern: files progress through discrete states (discovered, metadata extracted, fingerprinted, analyzed, proposal generated, approved, executed) with each transition tracked in PostgreSQL. This enables resumability after crashes, parallel processing across workers, and per-file progress visibility. The stack is well-proven: every major component (FastAPI, SQLAlchemy, mutagen, librosa, arq) has verified Python 3.13 compatibility and extensive documentation.

The dominant risk is data loss from irreversible file operations on an irreplaceable collection. This is mitigated by three non-negotiable design invariants: (1) an append-only operations journal in PostgreSQL written before any file move, (2) copy-verify-delete protocol with SHA256 verification, and (3) no file operation executes without explicit human approval. Secondary risks include Unicode path corruption (mitigate with NFC normalization at ingestion boundary), Docker volume permission mismatches (mitigate with parameterized UID/GID from day one), and LLM output inconsistency (mitigate with template-based naming where AI fills structured fields, not freeform filenames). The litellm supply chain incident (March 2026) requires exact version pinning with hash verification.

## Key Findings

### Recommended Stack

The stack is a fully async Python monolith deployed as two Docker Compose services (API server and worker) plus PostgreSQL and Redis. All libraries have been verified for Python 3.13 compatibility. See `.planning/research/STACK.md` for full version matrix and compatibility notes.

**Core technologies:**
- **FastAPI + Jinja2 + HTMX**: Async API with server-rendered admin UI -- no SPA build pipeline, no JS framework
- **SQLAlchemy 2.0 + asyncpg + Alembic**: Async ORM with migration support for 200K+ record workload
- **arq + Redis**: Lightweight async task queue for parallel file analysis (replaces Celery complexity)
- **mutagen**: Audio metadata read/write across all formats (ID3, Vorbis, MP4, FLAC)
- **librosa**: BPM detection, key estimation, spectral features (requires `standard-aifc` + `standard-sunau` on Python 3.13)
- **pyacoustid + chromaprint**: Audio fingerprinting for AcoustID identification and acoustic dedup
- **litellm**: Unified LLM API client -- pin to >=1.82.6,<1.82.7 due to supply chain attack on later versions

### Expected Features

**Must have (table stakes):**
- File ingestion with SHA256 hash dedup (~200K files, batch processing)
- Metadata extraction from embedded tags (ID3, Vorbis, MP4)
- Audio fingerprinting via AcoustID
- BPM detection via librosa
- AI-generated filename and path proposals (core differentiator)
- Human approval workflow UI with batch operations
- Safe file rename/move with copy-verify-delete protocol
- Video stream metadata extraction via ffprobe
- Progress tracking for long-running batch operations
- Docker Compose deployment

**Should have (differentiators):**
- Acoustic duplicate detection (cross-encoding dedup via fingerprint similarity)
- Batch approval with smart grouping (by album, artist, confidence score)
- Concert/event auto-detection from filenames and metadata
- Undo/rollback for executed moves via operations journal

**Defer (v2+):**
- Mood/style classification (depends on prototype maturity)
- MusicBrainz metadata enrichment (rate-limited: 55+ hours for 200K files)
- Full-text search UI (requires clean metadata first)
- Natural language querying, 1001tracklists, Discogsography integration

### Architecture Approach

Async monolith with two runtime modes from one codebase: FastAPI API server and arq background workers. Files progress through a state machine (DISCOVERED through EXECUTED) tracked in PostgreSQL. Fan-out batch processing distributes individual file tasks to a bounded worker pool. Services contain business logic; routers and workers are thin wrappers. See `.planning/research/ARCHITECTURE.md` for full system diagram, project structure, and database schema.

**Major components:**
1. **API Server (FastAPI)** -- HTTP endpoints, HTMX admin UI, job submission, SSE progress updates
2. **Worker Process (arq)** -- Audio analysis, metadata extraction, fingerprinting, LLM calls, file operations
3. **PostgreSQL** -- All persistent state: files, metadata, analysis, proposals, approval status, execution log
4. **Redis** -- Job queue broker for arq, optional result caching
5. **Filesystem (Docker volumes)** -- Source music (read-only) and organized output (write by worker only)

### Critical Pitfalls

See `.planning/research/PITFALLS.md` for the complete list with recovery strategies.

1. **Irreversible file operations without transaction log** -- Implement an append-only operations journal in PostgreSQL (WAL pattern) before any file move; use copy-verify-delete protocol; commit state per-file, not per-batch
2. **Unicode filename corruption** -- Normalize all paths to NFC at the ingestion boundary; store as UTF-8 TEXT in PostgreSQL; test with non-ASCII filenames (accented, CJK, emoji)
3. **Docker volume permission mismatch** -- Parameterize UID/GID in docker-compose.yml from day one; mount music source as read-only on all services except the file-mover
4. **Blocking async event loop with CPU-bound work** -- Wrap librosa and chromaprint calls in `asyncio.to_thread()` or `ProcessPoolExecutor`; bound the worker pool
5. **litellm supply chain risk** -- Pin exact version with verified hash; run pip-audit in CI; consider network-isolating LLM containers from file-access containers

## Implications for Roadmap

Based on research, suggested phase structure:

### Phase 1: Foundation (Database, Config, Docker, Ingestion)
**Rationale:** Everything depends on the database schema, Docker environment, and file ingestion pipeline. The operations journal, state machine, and NFC normalization must be foundational -- retrofitting any of these is extremely expensive.
**Delivers:** Working Docker Compose environment (API + worker + Postgres + Redis), database schema with all core tables (files, metadata, analysis, proposals, execution_log), Alembic migrations, file ingestion with SHA256 hashing, hash-based dedup detection, pipeline state machine.
**Addresses:** File ingestion, hash dedup, database schema, Docker deployment, progress tracking foundation.
**Avoids:** Irreversible file ops (journal table exists from start), Unicode corruption (NFC normalization from start), Docker permissions (UID/GID parameterized from start), bulk insert performance (COPY/batch strategy from start), supply chain risk (CI security scanning from start).

### Phase 2: Metadata and Analysis Pipeline
**Rationale:** AI proposals need rich metadata context to be useful. Metadata extraction and audio analysis are independent of each other and can be parallelized, but both must complete before proposal generation.
**Delivers:** Mutagen-based metadata extraction, librosa BPM/key detection, pyacoustid fingerprinting, AcoustID identification, worker pipeline with bounded parallelism.
**Addresses:** Metadata extraction, BPM detection, audio fingerprinting, video metadata (ffprobe).
**Avoids:** Event loop blocking (CPU work in process pool), unbounded memory (chunked reads), worker starvation (bounded pool + backpressure).

### Phase 3: AI Proposal Generation
**Rationale:** Depends on Phase 2 (metadata + analysis results). This is the core differentiator. Prompt engineering and structured output validation are the main challenges.
**Delivers:** LLM-powered filename and path proposals with confidence scores, batch prompting (20-50 files per call), Pydantic validation of all proposals, collision detection across proposal batch.
**Addresses:** AI filename proposals, AI path proposals, concert/event detection (as LLM classification).
**Avoids:** AI inconsistency (template-based naming, prompt versioning, model version tracking), cost explosion (batch prompting), supply chain risk (pinned litellm).

### Phase 4: Approval Workflow UI
**Rationale:** No file operations happen without approval. The UI is the gateway between proposals and execution. Depends on proposals existing in the database (Phase 3).
**Delivers:** HTMX-powered approval interface with pagination, filtering, bulk approve/reject, inline editing of proposed names, grouping by album/artist/confidence.
**Addresses:** Approval workflow UI, batch approval with smart grouping, progress tracking UI.
**Avoids:** Overwhelming UI (pagination + grouping), concurrent session conflicts (optimistic locking).

### Phase 5: Safe File Execution and Audit
**Rationale:** Final pipeline step. Only processes approved proposals. Must be bulletproof -- this is the irreversible action on irreplaceable files.
**Delivers:** Copy-verify-delete file operations, per-file journal entries, execution status tracking, undo/rollback capability, disk space pre-check.
**Addresses:** Safe file rename/move, undo/rollback, audit trail.
**Avoids:** Data loss (copy-verify-delete), partial batch corruption (per-file commits), permission issues (least-privilege volume mounts).

### Phase 6: Refinements and Differentiators
**Rationale:** Post-core-pipeline features that add value but are not required for the primary workflow to function.
**Delivers:** Acoustic duplicate detection, MusicBrainz enrichment, mood/style classification, dashboard with collection statistics.
**Addresses:** Acoustic dedup, MusicBrainz enrichment, mood classification.

### Phase Ordering Rationale

- **Phase 1 before everything:** The database schema, Docker environment, and ingestion pipeline are dependencies for all subsequent work. 7 of 11 pitfalls map to Phase 1.
- **Phase 2 before Phase 3:** AI proposals without metadata produce garbage. The analysis pipeline must populate the database before LLM calls make sense.
- **Phase 3 before Phase 4:** The approval UI needs proposals to display. Building UI before proposals exist means designing against an imagined data shape.
- **Phase 4 before Phase 5:** The human-in-the-loop constraint means execution cannot be built before the approval gate exists.
- **Phase 6 is independent:** Enrichment features run against existing data and can be added incrementally.

### Research Flags

Phases likely needing deeper research during planning:
- **Phase 3 (AI Proposals):** Prompt engineering for music file naming is novel territory. No established patterns. Batch prompting strategy, template design, and confidence scoring all need experimentation.
- **Phase 4 (Approval UI):** HTMX patterns for complex batch approval workflows with inline editing are less documented than standard CRUD. May need prototyping.

Phases with standard patterns (skip research-phase):
- **Phase 1 (Foundation):** FastAPI + SQLAlchemy + Alembic + Docker Compose is an extremely well-documented stack. Many production references available.
- **Phase 2 (Analysis Pipeline):** mutagen, librosa, and pyacoustid have straightforward APIs. arq worker patterns are well-documented.
- **Phase 5 (File Execution):** Copy-verify-delete is a simple pattern. The complexity is in the safety invariants, not the technology.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | All versions verified on PyPI. Python 3.13 compatibility confirmed. Known workarounds documented. |
| Features | HIGH | Domain well-understood. Competitor analysis (beets, Picard, AudioRanger) validates feature set. Clear MVP vs. defer boundaries. |
| Architecture | HIGH | Async monolith + workers is a battle-tested pattern. Database schema covers all identified use cases. |
| Pitfalls | HIGH | Multiple sources per pitfall (beets blog posts, PostgreSQL docs, Docker community). Recovery strategies documented. |

**Overall confidence:** HIGH

### Gaps to Address

- **Prompt engineering for music file naming:** No existing reference for LLM-based music file renaming. Will need iterative experimentation during Phase 3. Start with a small test batch (~100 files) before scaling.
- **Naming format template:** The filename format is TBD per project requirements. Must be decided before Phase 3 can be fully specified. Recommend defining a structured template (e.g., `{artist} - {title} [{bpm}bpm] [{key}].{ext}`) with AI filling fields, not generating freeform names.
- **arq long-term maintenance status:** arq is in maintenance-only mode. If it becomes abandoned, taskiq is the most likely migration target. Monitor before Phase 2.
- **pyacoustid last release 2023:** Functionally stable but no recent releases. If AcoustID API changes, may need to fork or find alternative. Low risk but monitor.
- **librosa Python 3.13 extras:** The `standard-aifc` and `standard-sunau` workaround is documented but not in librosa's official install guide. Verify during Phase 2 setup.
- **LLM cost estimation:** Batch prompting 200K files at 20-50 per prompt = 4,000-10,000 API calls. Cost depends heavily on model choice. Budget estimation needed during Phase 3 planning.
- **Existing prototype integration:** Need to examine actual prototype code for BPM/style/mood analysis to understand its interface and dependencies. Affects Phase 2 architecture.

## Sources

### Primary (HIGH confidence)
- [FastAPI releases](https://github.com/fastapi/fastapi/releases) -- version verification
- [SQLAlchemy PyPI](https://pypi.org/project/SQLAlchemy/) -- async support confirmed
- [mutagen PyPI](https://pypi.org/project/mutagen/) -- format support verified
- [librosa PyPI + GitHub #1883](https://github.com/librosa/librosa/issues/1883) -- Python 3.13 compatibility
- [PostgreSQL bulk loading](https://www.cybertec-postgresql.com/en/postgresql-bulk-loading-huge-amounts-of-data/) -- COPY performance
- [beets path encoding blog](https://beets.io/blog/paths.html) -- Unicode pitfalls
- [Docker Compose permissions](https://dev.to/visuellverstehen/docker-docker-compose-and-permissions-2fih) -- UID/GID patterns
- [FastAPI best practices](https://github.com/zhanymkanov/fastapi-best-practices) -- project structure patterns

### Secondary (MEDIUM confidence)
- [arq PyPI](https://pypi.org/project/arq/) -- maintenance mode status
- [pyacoustid PyPI](https://pypi.org/project/pyacoustid/) -- last release 2023
- [litellm security incident](https://docs.litellm.ai/blog/security-update-march-2026) -- supply chain attack details
- [HTMX + FastAPI patterns](https://johal.in/htmx-fastapi-patterns-hypermedia-driven-single-page-applications-2025/) -- UI architecture
- [Python task queue benchmarks](https://stevenyue.com/blogs/exploring-python-task-queue-libraries-with-load-test) -- arq performance
- [beets large library discussion](https://discourse.beets.io/t/using-beets-to-manage-huge-music-libraries-best-practices-and-suggestions/2598) -- scale pitfalls

### Tertiary (LOW confidence)
- LLM-based music file naming -- no established patterns found; approach is novel and needs validation

---
*Research completed: 2026-03-27*
*Ready for roadmap: yes*
