# Phase 3: Companion Files & Deduplication - Context

**Gathered:** 2026-03-28
**Status:** Ready for planning

<domain>
## Phase Boundary

Link companion files (cue sheets, NFO files, cover art, playlists) to their nearby media files using directory proximity heuristics. Detect and flag exact SHA256 duplicates for later review. Both features build on the file inventory from Phase 2.

</domain>

<decisions>
## Implementation Decisions

### Companion Association
- **D-01:** Add a `file_companions` join table (many-to-many) linking companion FileRecords to media FileRecords. A companion in a directory associates with all media files in the same directory. This handles the case where a cue sheet or cover art applies to multiple tracks in a folder.
- **D-02:** Association is computed as a post-processing step after ingestion — a service function scans for companions without a parent link and associates them based on directory path matching (`Path(companion.original_path).parent == Path(media.original_path).parent`).
- **D-03:** Association is triggered via API (`POST /api/v1/associate`) or automatically after a scan completes. Returns count of newly linked companions.

### Duplicate Detection
- **D-04:** Duplicates are detected by grouping FileRecords with the same `sha256_hash` where the group has more than one member. No new column needed — query-time grouping is sufficient.
- **D-05:** Duplicates are exposed via `GET /api/v1/duplicates` endpoint returning grouped duplicate sets with file paths, sizes, and types. No auto-resolution — duplicates are flagged for human review (deferred to Phase 7 approval UI).
- **D-06:** A dedicated service function identifies duplicate groups and returns them structured for the API.

### Claude's Discretion
- Join table schema details (composite PK, FK constraints, indexes)
- Alembic migration structure for the join table
- Whether to cache duplicate groups or compute on demand
- Test strategy for association logic (mock filesystem with known directory structure)
- API response pagination for large duplicate sets

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project Configuration
- `CLAUDE.md` — Development setup, code quality rules
- `.planning/PROJECT.md` — Project vision, constraints
- `.planning/REQUIREMENTS.md` — ING-04 (dedup), ING-06 (companion association)

### Existing Code
- `src/phaze/models/file.py` — FileRecord model (sha256_hash, original_path, file_type)
- `src/phaze/constants.py` — FileCategory enum (MUSIC, VIDEO, COMPANION)
- `src/phaze/services/ingestion.py` — Ingestion service with discover/hash/classify functions
- `src/phaze/routers/scan.py` — Scan API pattern to follow
- `src/phaze/schemas/scan.py` — Pydantic schema pattern to follow
- `src/phaze/database.py` — Async engine, session factory

### Prior Phase Context
- `.planning/phases/02-file-discovery-ingestion/02-CONTEXT.md` — Ingestion decisions

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `FileRecord` model with `sha256_hash`, `original_path`, `file_type` — all needed for both features
- `FileCategory.COMPANION` — identifies companion files
- `get_session` dependency — async DB access
- Scan router/schema pattern for creating new endpoints

### Established Patterns
- SQLAlchemy 2.0 async queries
- Pydantic schemas for API request/response
- FastAPI router with dependency injection
- Alembic migrations for schema changes

### Integration Points
- New `file_companions` table via Alembic migration
- New service functions in `services/` (association + dedup)
- New router endpoints wired into `main.py`
- New schemas in `schemas/`

</code_context>

<specifics>
## Specific Ideas

- ~30% of content has companion files (user estimate)
- Companion files sit in the same or nearby directory as their media
- Duplicates are common in the messy collection — same file in multiple locations

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 03-companion-files-deduplication*
*Context gathered: 2026-03-28*
