# Phase 3: Companion Files & Deduplication - Context

**Gathered:** 2026-03-28
**Updated:** 2026-03-28
**Status:** Ready for planning

<domain>
## Phase Boundary

Link companion files (cue sheets, NFO files, cover art, playlists) to their nearby media files using directory proximity heuristics. Detect and flag exact SHA256 duplicates for later review. Both features build on the file inventory from Phase 2.

</domain>

<decisions>
## Implementation Decisions

### Companion Association
- **D-01:** file_companions join table (many-to-many) linking companion FileRecords to media FileRecords.
- **D-02:** Same-directory matching only — a companion in /album/ associates with all media files in /album/. No parent/child directory traversal.
- **D-03:** Association computed as post-processing step. Service function scans for unlinked companions and associates by directory path matching. Idempotent via NOT IN subquery.
- **D-04:** Triggered via POST /api/v1/associate or after scan completion. Returns count of newly linked companions.

### Duplicate Detection
- **D-05:** Query-time detection via GROUP BY sha256_hash HAVING COUNT > 1. No pre-computed groups.
- **D-06:** Exposed via GET /api/v1/duplicates with pagination (limit/offset). Returns grouped duplicate sets with file paths, sizes, types.
- **D-07:** No auto-resolution — duplicates flagged for human review in Phase 7 approval UI.

### Claude's Discretion
- Join table schema details (composite PK, FK constraints, indexes, CASCADE behavior)
- Alembic migration structure
- Pagination defaults for duplicate groups endpoint
- Test strategy

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project Configuration
- `CLAUDE.md` — Development setup, code quality rules
- `.planning/REQUIREMENTS.md` — ING-04 (dedup), ING-06 (companion association)

### Existing Code
- `src/phaze/models/file.py` — FileRecord model (sha256_hash, original_path, file_type)
- `src/phaze/constants.py` — FileCategory enum, EXTENSION_MAP
- `src/phaze/routers/scan.py` — Router pattern to follow
- `src/phaze/schemas/scan.py` — Schema pattern to follow

### Prior Phase Context
- `.planning/phases/02-file-discovery-ingestion/02-CONTEXT.md` — Ingestion decisions

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- FileRecord with sha256_hash, original_path, file_type
- FileCategory.COMPANION for identifying companion files
- get_session dependency, router/schema patterns

### Established Patterns
- SQLAlchemy 2.0 async queries, Pydantic schemas, FastAPI routers
- ON CONFLICT pattern from ingestion service

### Integration Points
- New file_companions table via Alembic migration
- New services: companion.py, dedup.py
- New router: companion.py with /associate and /duplicates endpoints

</code_context>

<specifics>
## Specific Ideas

- ~30% of content has companion files
- Duplicates common in messy collection — same file in multiple locations
- Companion files sit in same directory as their media

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 03-companion-files-deduplication*
*Context gathered: 2026-03-28*
*Context updated: 2026-03-28*
