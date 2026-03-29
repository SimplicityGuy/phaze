# Phase 2: File Discovery & Ingestion - Context

**Gathered:** 2026-03-28
**Updated:** 2026-03-28
**Status:** Ready for planning

<domain>
## Phase Boundary

Scan a mounted directory tree recursively, discover all music files (mp3, m4a, ogg, flac, wav, aiff), video files (mp4, mkv, avi, webm, mov), and companion files (cue, nfo, txt, jpg, png, m3u, pls), compute SHA256 hashes, classify each file by type, and persist everything to PostgreSQL. Must handle ~200K files efficiently.

</domain>

<decisions>
## Implementation Decisions

### Scan Trigger & Batch Tracking
- **D-01:** Scan triggered via POST /api/v1/scan API endpoint. Runs as asyncio background task. Returns batch ID for status tracking.
- **D-02:** ScanBatch model tracks status (PENDING/RUNNING/COMPLETED/FAILED) with total_files count and error_message. GET /api/v1/scan/{batch_id} for polling.
- **D-03:** Each scan creates a new batch. Files linked to batch via batch_id on FileRecord.

### File Type Detection
- **D-04:** Extension-based classification via EXTENSION_MAP constant (27 extensions). Categories: MUSIC, VIDEO, COMPANION, UNKNOWN. Unknown files skipped during discovery.
- **D-05:** Extension map is complete for current needs. Defined in single constants.py module.

### Performance & Resumability
- **D-06:** Bulk upsert via PostgreSQL ON CONFLICT DO UPDATE on original_path. Handles resumability — re-running a scan updates existing records.
- **D-07:** SHA256 hashing reads in 64KB chunks (HASH_CHUNK_SIZE constant) to avoid memory issues with large files.
- **D-08:** Batch size of 1000 rows (BULK_INSERT_BATCH_SIZE) for bulk insert operations.
- **D-09:** Unicode paths normalized to NFC at ingestion time (macOS NFD → NFC).

### Claude's Discretion
- API router structure for scan endpoints
- Progress reporting mechanism (logs for now — no SSE needed)
- Test strategy (unit tests with mocked filesystem, integration tests with temp dirs)

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project Configuration
- `CLAUDE.md` — Development setup, code quality rules, pre-commit hooks, CI patterns
- `.planning/PROJECT.md` — Project vision, constraints, key decisions
- `.planning/REQUIREMENTS.md` — v1 requirements (ING-01, ING-02, ING-03, ING-05)

### Existing Code
- `src/phaze/models/file.py` — FileRecord model with sha256_hash, original_path, original_filename, file_type, state, batch_id
- `src/phaze/models/base.py` — Base model with TimestampMixin and naming conventions
- `src/phaze/database.py` — Async engine, session factory, get_session dependency
- `src/phaze/config.py` — Settings class (scan_path field)
- `docker-compose.yml` — Volume mount for scan source

### Research
- `.planning/research/ARCHITECTURE.md` — Pipeline state machine, bulk insert patterns
- `.planning/research/PITFALLS.md` — Unicode NFC normalization, chunked hashing, Docker volume permissions

### Prior Phase Context
- `.planning/phases/01-infrastructure-project-setup/01-CONTEXT.md` — Foundation decisions

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- FileRecord model ready with all columns
- FileState.DISCOVERED as initial state
- get_session async dependency
- Health router pattern for new routers

### Established Patterns
- Async SQLAlchemy with asyncpg driver
- UUID primary keys, TIMESTAMPTZ timestamps
- Router → Service → Model layer separation

### Integration Points
- Scan router registered on FastAPI app
- Constants module for extension map
- ScanBatch model for batch tracking
- Config scan_path field

</code_context>

<specifics>
## Specific Ideas

- Docker volume mount approach for scan source
- ~200K files, mostly messy naming, scattered across locations
- Mix of concert video streams and individual music files
- ~30% of content has companion files alongside

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 02-file-discovery-ingestion*
*Context gathered: 2026-03-28*
*Context updated: 2026-03-28*
