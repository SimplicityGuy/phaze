# Phase 20: Tag Writing - Context

**Gathered:** 2026-04-03
**Status:** Ready for planning

<domain>
## Phase Boundary

Write corrected metadata tags to destination file copies (never originals). Show a dedicated tag review page with proposed vs current tags side-by-side, let users edit and approve writes, verify correctness by re-reading after write, and log everything in an append-only TagWriteLog audit table. Only EXECUTED files are eligible for tag writing.

</domain>

<decisions>
## Implementation Decisions

### Tag Source & Proposal Logic
- **D-01:** Tag sources are FileMetadata (existing tags from file), filename parsing, and 1001tracklists data (artist, event from Tracklist model). NOT Discogs — DiscogsLinks are per-track within a tracklist, not per-file. Discogs data is for CUE sheets (Phase 21).
- **D-02:** Priority cascade for merging: tracklist data wins over FileMetadata wins over filename parsing. Each field resolved independently.
- **D-03:** Only EXECUTED files (with destination copies) are eligible for tag writing.

### Review Page UX
- **D-04:** Dedicated '/tags' page as a nav tab. Shows files with pending tag proposals in a table. Click to expand and see proposed vs current tags side-by-side.
- **D-05:** Two-column table layout: Field | Current | Proposed. Changed fields highlighted (bold or colored). Empty fields show '—'.
- **D-06:** Core music fields only: artist, title, album, year, genre, track number. Duration/bitrate are read-only, not writable.
- **D-07:** Proposed column cells are editable inline — user can tweak values before approving. Same HTMX inline edit pattern as tracklist tracks.

### Write Verification & Error Handling
- **D-08:** Verify-after-write: re-open file with mutagen and compare each written field against what was sent. Flag mismatches.
- **D-09:** Discrepancies flagged and logged (which field, expected vs actual) with 'discrepancy' status in TagWriteLog. Don't block user — cosmetic discrepancies (encoding normalization) are common.
- **D-10:** Tag writes run synchronously per-file when user approves. Single file write is fast (~100ms). No SAQ background task needed. Immediate feedback.

### Audit Trail Design
- **D-11:** Per-file snapshot granularity. One TagWriteLog entry per file write with before_tags and after_tags as JSONB snapshots.
- **D-12:** Source field tracks which data source was used (tracklist, metadata, manual_edit). No user attribution needed — single-user app.
- **D-13:** Append-only table — no updates or deletes. Matches ExecutionLog pattern from Phase 8.

### Claude's Discretion
- TagWriteLog model schema details (columns, indexes, relationships)
- Tag write service implementation (mutagen write API per format)
- Proposed tag computation service (cascade merge logic)
- HTMX partial structure for tag review page
- Filename parsing strategy for extracting metadata
- Nav tab ordering (where '/tags' appears relative to other tabs)

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Existing Tag Infrastructure
- `src/phaze/services/metadata.py` — mutagen tag extraction service with format mappings (_VORBIS_MAP, _ID3_MAP). Same library used for writing.
- `src/phaze/models/metadata.py` — FileMetadata model (current tag storage, 1:1 with files)
- `src/phaze/tasks/metadata_extraction.py` — Tag extraction task pattern

### Existing Patterns to Follow
- `src/phaze/models/execution.py` — ExecutionLog append-only audit model pattern (Phase 8)
- `src/phaze/models/file.py` — FileRecord model with FileState enum (EXECUTED state check)
- `src/phaze/routers/proposals.py` — Proposals page pattern (table with approve/reject actions)
- `src/phaze/routers/tracklists.py` — Inline editing pattern, HTMX partials, expand/collapse
- `src/phaze/templates/proposals/list.html` — Dense table layout pattern
- `src/phaze/templates/tracklists/` — Inline edit HTMX patterns

### Data Sources for Tag Proposals
- `src/phaze/models/tracklist.py` — Tracklist model (artist, event fields as tag source)
- `src/phaze/models/metadata.py` — FileMetadata (existing tag data)

### UI Patterns
- `src/phaze/templates/base.html` — Nav bar structure (add Tags tab)

### Requirements
- `.planning/REQUIREMENTS.md` — TAGW-01 through TAGW-04

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `mutagen` library: Already imported in `services/metadata.py` — supports read AND write for ID3, Vorbis, MP4, FLAC, OPUS
- `ExtractedTags` dataclass: Normalized tag representation, reuse for proposed tags
- `_VORBIS_MAP` and `_ID3_MAP`: Tag key mappings per format — reverse these for writing
- HTMX inline edit pattern: Used in tracklist track editing
- Alpine.js expand/collapse: Used across multiple pages
- ExecutionLog model: Append-only audit pattern to follow for TagWriteLog

### Established Patterns
- FastAPI router + Jinja2 templates + HTMX partials for all pages
- SQLAlchemy async models with UUID primary keys, TimestampMixin
- Alembic migrations for schema changes
- JSONB columns for flexible data (raw_tags in FileMetadata)
- Format-aware tag handling (ID3 for MP3, Vorbis for OGG/FLAC/OPUS, MP4 for M4A)

### Integration Points
- New model: `src/phaze/models/tag_write_log.py` — TagWriteLog audit table
- New service: `src/phaze/services/tag_writer.py` — tag write + verify logic
- New service: `src/phaze/services/tag_proposal.py` — proposed tag computation (cascade merge)
- New router: `src/phaze/routers/tags.py` — Tag review page endpoints
- New templates: `src/phaze/templates/tags/` — Review page, comparison partials
- Alembic migration: Create tag_write_log table
- Nav update: `src/phaze/templates/base.html` — add Tags tab

</code_context>

<specifics>
## Specific Ideas

- Tag sources explicitly exclude Discogs data — DiscogsLinks are per-track within tracklists, not per-file (the file is the full live set). Discogs metadata feeds CUE sheets in Phase 21, not file tags.
- Tracklist data (artist, event) maps to file tags as: artist -> artist tag, event -> album tag (concert event as album name).

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope

</deferred>

---

*Phase: 20-tag-writing*
*Context gathered: 2026-04-03*
