# Phase 12: Infrastructure & Audio Tag Extraction - Context

**Gathered:** 2026-03-30
**Status:** Ready for planning

<domain>
## Phase Boundary

Every music and video file gets its audio tags extracted (via mutagen) and stored in PostgreSQL, with infrastructure improvements (shared async engine pool replacing per-invocation engine creation, state machine expansion) and richer LLM proposal context from extracted tag data.

</domain>

<decisions>
## Implementation Decisions

### Pipeline Flow & State Ordering
- **D-01:** Tag extraction and audio analysis run **in parallel** from DISCOVERED state. They are independent operations — tags are file header reads (I/O-bound), analysis is essentia waveform processing (CPU-bound). No resource contention.
- **D-02:** Convergence gate before proposal generation uses a **dual state check**: proposal generation queries for files where BOTH FileMetadata row exists AND AnalysisResult row exists. No new composite state needed.
- **D-03:** FileState enum already includes METADATA_EXTRACTED and FINGERPRINTED. Tag extraction transitions files to METADATA_EXTRACTED independently of the ANALYZED transition.

### Backfill Strategy
- **D-04:** **Queue all files** for tag extraction regardless of current state. Tag extraction is fast (header reads) and all ~200K files benefit from metadata. Simple, complete, idempotent.
- **D-05:** Existing proposals are **not regenerated** with tag data. Only future proposal generation includes tag context. Avoids burning LLM credits on re-processing.

### Tag-to-LLM Integration
- **D-06:** Tag data nested under a **`'tags'` key** in the file context dict, keeping it visually distinct from analysis data in the prompt.
- **D-07:** Include **full raw_tags dump** alongside curated fields (artist, title, album, year, genre, track_number, duration, bitrate). Scene releases and live recordings often have useful metadata in obscure tags (COMMENT, GROUPING, DESCRIPTION, URL).
- **D-08:** **No prompt template changes.** Tags may contain nonsense (origination data, irrelevant extras). The LLM decides what's useful from available context — no explicit instructions to prioritize tag data.

### Extraction Trigger & Scope
- **D-09:** **Both auto and manual triggers.** Tag extraction jobs enqueued automatically during scan (new files), plus a manual API endpoint for backfill and re-extraction.
- **D-10:** Extract tags from **music and video files** (not companion files). Concert video streams from festivals may have useful metadata in container tags.
- **D-11:** Files with no tags get an **empty FileMetadata row** (all null fields, empty raw_tags). File transitions to METADATA_EXTRACTED regardless. Downstream knows extraction ran but found nothing.

### Claude's Discretion
- Shared async engine pool implementation (worker startup hook, module singleton, etc.)
- FileMetadata model additions (track_number, duration, bitrate columns)
- Alembic migration for new columns
- mutagen integration details (format detection, error handling for corrupt files)
- Manual extraction API endpoint design
- arq task function structure for tag extraction
- Pipeline dashboard updates to show extraction status

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project Configuration
- `CLAUDE.md` — Development setup, mutagen listed as recommended audio metadata library
- `.planning/REQUIREMENTS.md` — INFRA-01, INFRA-02, TAGS-01 through TAGS-05
- `.planning/PROJECT.md` — Project vision, constraints, tech stack (mutagen for read+write)

### Existing Code (MUST READ)
- `src/phaze/database.py` — Current engine creation pattern; `get_task_session()` creates engine per invocation (INFRA-01 target)
- `src/phaze/models/file.py` — FileRecord model, FileState enum (already has METADATA_EXTRACTED, FINGERPRINTED)
- `src/phaze/models/metadata.py` — FileMetadata model (exists, missing track_number/duration/bitrate)
- `src/phaze/services/proposal.py` — `build_file_context()` function (needs tag data integration for TAGS-05)
- `src/phaze/tasks/session.py` — `get_task_session()` factory (replace with shared pool)
- `src/phaze/tasks/worker.py` — WorkerSettings, on_startup/on_shutdown hooks, registered task functions
- `src/phaze/tasks/functions.py` — `process_file()` task pattern (reference for new extraction task)
- `src/phaze/constants.py` — Extension map, file type categories (music vs video vs companion)
- `src/phaze/prompts/naming.md` — Current LLM prompt template (no changes needed per D-08)

### Prior Phase Context
- `.planning/phases/04-task-queue-worker-infrastructure/04-CONTEXT.md` — Worker patterns (D-01: max_jobs, D-04: one job per file, D-05: retry with backoff)
- `.planning/phases/05-audio-analysis-pipeline/05-CONTEXT.md` — Analysis patterns (D-08: process_file via run_in_process_pool)
- `.planning/phases/06-ai-proposal-generation/06-CONTEXT.md` — Proposal context building (D-08: all context per file, D-09: structured JSON output)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- FileMetadata model with artist, title, album, year, genre, raw_tags columns (needs track_number, duration, bitrate)
- FileState enum with METADATA_EXTRACTED already defined
- arq worker infrastructure with retry, backoff, process pool patterns
- `build_file_context()` function in proposal service — add tag data here

### Established Patterns
- arq job functions with try/finally session management
- One job per file granularity
- ProcessPoolExecutor for CPU-bound work (tag extraction is I/O-bound, may not need this)
- Pydantic settings for configuration with env var overrides
- SQLAlchemy 2.0 async queries with upsert patterns

### Integration Points
- New `src/phaze/tasks/metadata_extraction.py` for tag extraction task
- New `src/phaze/services/metadata.py` for mutagen extraction logic
- Update `src/phaze/tasks/worker.py` to register new task and add shared engine pool
- Update `src/phaze/services/proposal.py` `build_file_context()` to include tag data
- Update `src/phaze/database.py` or `src/phaze/tasks/session.py` for shared engine pool
- New Alembic migration for FileMetadata column additions
- New API endpoint in routers for manual extraction trigger

</code_context>

<specifics>
## Specific Ideas

- Tags may contain nonsense (origination data, irrelevant extras) — LLM should decide what's useful, not rigid rules
- Scene releases often have rich metadata in COMMENT, GROUPING, DESCRIPTION tags
- Concert video streams from YouTube/festival recordings may have useful container tags
- Tag extraction is I/O-bound (header reads), not CPU-bound — no process pool needed, can run async directly

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 12-infrastructure-audio-tag-extraction*
*Context gathered: 2026-03-30*
