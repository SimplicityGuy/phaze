# Phase 2: File Discovery & Ingestion - Context

**Gathered:** 2026-03-28
**Status:** Ready for planning

<domain>
## Phase Boundary

Scan a mounted directory tree recursively, discover all music files (mp3, m4a, ogg, flac, wav, aiff), video files (mp4, mkv, avi, webm, mov), and companion files (cue, nfo, txt, jpg, png, m3u, pls), compute SHA256 hashes, classify each file by type, and persist everything to PostgreSQL. Must handle ~200K files efficiently.

</domain>

<decisions>
## Implementation Decisions

### Scan Trigger & Source
- **D-01:** Scan source is a Docker volume mount. The music directory is mounted into the container and the scan path is configured via `SCAN_PATH` environment variable in `.env` (e.g., `SCAN_PATH=/data/music`).
- **D-02:** Scanning is triggered via an API endpoint (`POST /api/v1/scan`). The endpoint accepts an optional `path` override but defaults to `SCAN_PATH` from config. Returns a batch ID for tracking.
- **D-03:** Each scan creates a batch record. Files discovered in a scan are linked to the batch via `batch_id` on `FileRecord`.

### File Type Detection
- **D-04:** File type classification uses extension-based detection. Extensions are mapped to categories: music (`mp3`, `m4a`, `ogg`, `flac`, `wav`, `aiff`, `wma`, `aac`), video (`mp4`, `mkv`, `avi`, `webm`, `mov`, `wmv`, `flv`), companion (`cue`, `nfo`, `txt`, `jpg`, `jpeg`, `png`, `gif`, `m3u`, `m3u8`, `pls`, `sfv`, `md5`). Unknown extensions are classified as `unknown`.
- **D-05:** Extension mapping is defined in a single constant/enum, not scattered. Easy to extend as new types are discovered.

### Batch Processing & Resumability
- **D-06:** Directory scanning is synchronous (fast — just `os.walk`). File hashing and DB persistence are batched using PostgreSQL `COPY` or bulk insert for performance (research flagged individual INSERTs as 10x slower at this scale).
- **D-07:** SHA256 hashing reads files in 64KB chunks to avoid loading large files into memory.
- **D-08:** Scans are resumable by batch — if a scan is interrupted, re-running it creates a new batch. Files already in the DB (matched by `original_path`) are skipped or updated.
- **D-09:** Unicode paths are normalized to NFC at ingestion time (research pitfall: macOS uses NFD, Linux uses NFC).

### Claude's Discretion
- API router structure for the scan endpoint
- Batch table schema (if needed beyond existing `batch_id` on FileRecord)
- Progress reporting mechanism (SSE, polling, or just logs for now)
- Exact chunking strategy for bulk inserts (1000 rows per batch, or adaptive)
- Whether to add a `ScanBatch` model or just use UUID grouping
- Test strategy (unit tests with mocked filesystem, integration tests with temp dirs)

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project Configuration
- `CLAUDE.md` — Development setup, code quality rules, pre-commit hooks, CI patterns
- `.planning/PROJECT.md` — Project vision, constraints, key decisions
- `.planning/REQUIREMENTS.md` — v1 requirements with REQ-IDs (ING-01, ING-02, ING-03, ING-05)

### Existing Code
- `src/phaze/models/file.py` — FileRecord model with sha256_hash, original_path, original_filename, file_type, state, batch_id
- `src/phaze/models/base.py` — Base model with TimestampMixin and naming conventions
- `src/phaze/database.py` — Async engine, session factory, get_session dependency
- `src/phaze/config.py` — Settings class (add SCAN_PATH here)
- `src/phaze/main.py` — FastAPI app with lifespan and health router
- `src/phaze/routers/health.py` — Health endpoint pattern to follow for new routers
- `docker-compose.yml` — Add volume mount for scan source
- `.env.example` — Add SCAN_PATH variable

### Research
- `.planning/research/ARCHITECTURE.md` — Pipeline state machine, bulk insert patterns
- `.planning/research/PITFALLS.md` — Unicode NFC normalization, chunked hashing, Docker volume permissions

### Prior Phase Context
- `.planning/phases/01-infrastructure-project-setup/01-CONTEXT.md` — Foundation decisions (D-01 through D-07)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `FileRecord` model is ready with all needed columns (sha256_hash, original_path, original_filename, file_type, file_size, state, batch_id)
- `FileState.DISCOVERED` is the initial state for newly ingested files
- `get_session` async dependency for database access
- `Settings` class with pydantic-settings for config (add `scan_path` field)
- Health router pattern for creating new API routers

### Established Patterns
- Async SQLAlchemy with asyncpg driver
- UUID primary keys, TIMESTAMPTZ timestamps
- Index on `sha256_hash` and `state` already defined
- Router -> Service -> Model layer separation

### Integration Points
- New `scan` router registers on the FastAPI app via `include_router`
- New `scan` service handles the business logic
- Config gets a new `scan_path: str` field
- Docker Compose gets a volume mount for the scan directory
- `.env.example` gets `SCAN_PATH=/data/music`

</code_context>

<specifics>
## Specific Ideas

- User wants Docker volume mount approach — mount the music directory into the container
- Files are ~200K total, mostly messy/chaotic naming, scattered across locations
- Mix of full concert video streams (from YouTube/festival recordings) and individual music files
- ~30% of content has companion files alongside (cue sheets, NFOs, cover art, playlists)

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 02-file-discovery-ingestion*
*Context gathered: 2026-03-28*
