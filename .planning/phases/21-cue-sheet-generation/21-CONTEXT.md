# Phase 21: CUE Sheet Generation - Context

**Gathered:** 2026-04-03
**Status:** Ready for planning

<domain>
## Phase Boundary

Generate .cue companion files from tracklist data. Prefer fingerprint timestamps over 1001tracklists positions. Enrich CUE files with Discogs metadata via REM comments. Provide CUE generation from the tracklist detail page and a dedicated CUE management page for batch operations. Write .cue files to the filesystem next to destination audio files.

</domain>

<decisions>
## Implementation Decisions

### Timestamp Resolution
- **D-01:** CUE timestamps use MM:SS:FF format at 75 frames per second per the CUE sheet specification. All source timestamps (fingerprint or 1001tracklists) must be converted to this format.
- **D-02:** Fingerprint timestamps always take priority over 1001tracklists timestamps when both exist for the same track.
- **D-03:** Tracks without any timestamp (no fingerprint offset, no 1001tracklists time) are omitted from the generated CUE file entirely.

### CUE Generation Trigger
- **D-04:** "Generate CUE" button on the tracklist detail page (inline, alongside existing actions like Match to Discogs). Primary per-tracklist action.
- **D-05:** Dedicated CUE management page (/cue nav tab) listing all tracklists with CUE generation status. Supports batch generation.
- **D-06:** Only tracklists with status='approved' are eligible for CUE generation. Consistent with human-in-the-loop approval pattern.
- **D-07:** CUE generation runs synchronously (pure string formatting + file write). No SAQ background task needed — instant feedback like tag writes.

### Discogs REM Enrichment
- **D-08:** REM comments are per-track, not disc-level. Each TRACK section gets REM GENRE, REM LABEL, REM YEAR from that track's accepted DiscogsLink.
- **D-09:** Tracks without an accepted DiscogsLink get no REM comments — clean omission, no placeholders.

### Output & Delivery
- **D-10:** .cue files are written to the filesystem next to the destination audio file (same directory, same base name with .cue extension). Requires the tracklist's linked file to have a destination path.
- **D-11:** FILE command in the CUE references the audio filename only (not a full or relative path). CUE and audio file are co-located.
- **D-12:** Re-generating a CUE file uses version suffix naming (e.g., `file.v2.cue`, `file.v3.cue`). Generated artifacts are versioned, not overwritten.
- **D-13:** CUE files use UTF-8 encoding with BOM (byte order mark) per CUE-02 requirement.

### Claude's Discretion
- CUE generation service implementation (string building, frame conversion math)
- CUE management page layout and filtering options
- HTMX partial structure for inline CUE status on tracklist page
- Batch generation loop on CUE management page
- Audio file type mapping for FILE command (MP3, WAVE, AIFF, etc.)
- Version number tracking strategy (scan existing files or DB counter)
- Nav tab ordering for /cue page

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### CUE Sheet Specification
- https://en.wikipedia.org/wiki/Cue_sheet_(computing) -- CUE format overview: MM:SS:FF at 75fps, commands (FILE, TRACK, INDEX, PERFORMER, TITLE, REM)
- https://docs.fileformat.com/disc-and-media/cue/ -- CUE file structure and encoding
- https://docs.fileformat.com/disc-and-media/cue-cdrwin/ -- CDRWIN CUE format reference

### Existing Data Models
- `src/phaze/models/tracklist.py` -- Tracklist, TracklistVersion, TracklistTrack models. TracklistTrack.timestamp is String(20), TracklistTrack.position is Integer.
- `src/phaze/models/discogs_link.py` -- DiscogsLink model with discogs_label, discogs_year, discogs_artist, discogs_title. Status 'accepted' means user-approved link.
- `src/phaze/models/file.py` -- FileRecord model with FileState enum, destination_path field.

### Existing Patterns to Follow
- `src/phaze/services/tag_writer.py` -- Synchronous file write pattern (tag writes), verify-after-write approach
- `src/phaze/services/tag_proposal.py` -- Service that gathers data from multiple sources (cascade logic)
- `src/phaze/routers/tags.py` -- Dedicated page router with nav tab, HTMX partials
- `src/phaze/routers/tracklists.py` -- Tracklist detail page where inline CUE button goes
- `src/phaze/tasks/scan.py` -- Shows how fingerprint timestamps flow into TracklistTrack.timestamp

### UI Patterns
- `src/phaze/templates/base.html` -- Nav bar structure (add CUE tab)
- `src/phaze/templates/tracklists/` -- Tracklist page templates (inline action button patterns)
- `src/phaze/templates/tags/` -- Dedicated page pattern with list + detail views

### Requirements
- `.planning/REQUIREMENTS.md` -- CUE-01 through CUE-03

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `TracklistTrack.timestamp` (String(20)): Source timestamps from fingerprint or 1001tracklists
- `TracklistTrack.position` (Integer): Track ordering within tracklist
- `DiscogsLink` model: Denormalized Discogs metadata (label, year, artist, title) per track
- `Tracklist.source` field: Distinguishes 'fingerprint' vs '1001tracklists' source
- HTMX inline action buttons: Pattern from tracklist page (Match to Discogs, Bulk-link)
- Nav tab pattern: Used by /search, /tags, /tracklists

### Established Patterns
- FastAPI router + Jinja2 templates + HTMX partials for all pages
- SQLAlchemy async models with UUID primary keys, TimestampMixin
- Synchronous per-file operations for fast user feedback (tag writes pattern)
- Alpine.js for collapsible panels and toggle states
- HTMX OOB swaps for toast notifications after actions

### Integration Points
- New service: `src/phaze/services/cue_generator.py` -- CUE content generation + file writing
- New router: `src/phaze/routers/cue.py` -- CUE management page endpoints
- Router update: `src/phaze/routers/tracklists.py` -- Add 'Generate CUE' button/endpoint
- New templates: `src/phaze/templates/cue/` -- Management page, list/status views
- Template update: Tracklist detail -- inline CUE generation button
- Nav update: `src/phaze/templates/base.html` -- add CUE tab

</code_context>

<specifics>
## Specific Ideas

- CUE sheet spec researched during discussion: MM:SS:FF format at 75fps, INDEX 01 for track start, PERFORMER/TITLE for CD-Text, REM for metadata comments
- Discogs data flows per-track via accepted DiscogsLinks (Phase 19), not per-file -- each track in a tracklist may have a different label/year from Discogs
- Version suffix naming for re-generated CUEs prevents losing previous versions

</specifics>

<deferred>
## Deferred Ideas

None -- discussion stayed within phase scope

</deferred>

---

*Phase: 21-cue-sheet-generation*
*Context gathered: 2026-04-03*
