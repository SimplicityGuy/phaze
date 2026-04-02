# Phase 15: 1001Tracklists Integration - Context

**Gathered:** 2026-04-01
**Status:** Ready for planning

<domain>
## Phase Boundary

Search 1001tracklists.com for matching tracklists by artist/event/date, scrape and store tracklist data in PostgreSQL with versioned snapshots, fuzzy-match tracklists to files using rapidfuzz, auto-link high-confidence matches (90%+) with undo, and periodically refresh stale/unresolved entries. Covers TL-01 through TL-04.

</domain>

<decisions>
## Implementation Decisions

### Data Model & Storage
- **D-01:** One-to-one relationship between tracklists and files. Each live set recording maps to at most one tracklist. Simple foreign key on tracklist pointing to file_id.
- **D-02:** Versioned snapshots for tracklist data. Each scrape creates a new version row. UI always shows latest version; history is for DB-level auditing/debugging only, not a user-facing feature.
- **D-03:** Store source URL and 1001tracklists external ID on every tracklist record. Enables re-scraping and source linking.
- **D-04:** Store all available track fields: position, artist, title, label, timestamp/cue time, and any mashup/remix metadata. Capture everything the page provides.
- **D-05:** New `models/tracklist.py` file with Tracklist and TracklistTrack models. Clean separation — tracklists are a distinct domain from file metadata.

### Search & Scraping
- **D-06:** Both manual and automatic triggers. Tag extraction completion auto-enqueues a tracklist search. Manual search button available per file in UI. User always reviews matches (except auto-linked 90%+).
- **D-07:** Fixed delay rate limiting between HTTP requests (e.g., 2-5 seconds). Simple, respectful, easy to tune.
- **D-08:** httpx as HTTP client. Already a dev dependency (FastAPI test client). Async-native with good timeout/retry support.
- **D-09:** Multiple search results presented to user ranked by relevance. User picks the correct tracklist or dismisses all. Consistent with human-in-the-loop approach.
- **D-10:** Periodic refresh targets stale + unresolved tracklists. Re-scrape unresolved ones AND any tracklist not updated in 90+ days. Monthly minimum cadence with randomized jitter per TL-04.
- **D-11:** Scraping failures logged and auto-retried on next refresh cycle. No UI visibility for errors — user only sees successful results.

### Fuzzy Matching Logic
- **D-12:** Match signals: primary is artist name similarity, secondary is event/venue, tertiary is date proximity. Uses artist + event + date combination as most discriminating for live sets.
- **D-13:** Numeric confidence score 0-100. Weighted score combining individual signal similarities. Displayed as percentage. Sortable and filterable.
- **D-14:** Auto-link matches above 90% confidence with 10-second undo toast. Below 90% requires explicit human approval. Balances efficiency with the human-in-the-loop principle.
- **D-15:** rapidfuzz library for string similarity (Levenshtein, token set ratio, etc.). Fast C-extension, MIT licensed, actively maintained.
- **D-16:** Parse structured filenames matching the v1.0 naming format (`Artist - Live @ Venue YYYY.MM.DD.ext`) as a primary matching signal. Gives strong signals even when tags are sparse. Fall back to tags for non-matching filenames.
- **D-17:** When multiple tracklists match with similar confidence (e.g., same artist, same festival, different years), present all candidates ranked by score. User picks the correct one — date becomes the key differentiator.

### Admin UI Presentation
- **D-18:** Dedicated Tracklists page plus small badge/link on file cards in proposals/duplicates showing tracklist status.
- **D-19:** Nav position: Pipeline > Proposals > Preview > Duplicates > **Tracklists** > Audit Log. Groups data views together.
- **D-20:** Card-per-tracklist layout. Card shows artist, event, date, track count, match confidence, linked file. Expand inline (HTMX) to see full track listing. Consistent with duplicates page pattern.
- **D-21:** Four actions per tracklist: Unlink from file, Re-scrape now, View on 1001tracklists (external link), Search for better match.
- **D-22:** Tabs/filter on Tracklists page: Matched / Unmatched / All. Unmatched files shown with a "Search" button.
- **D-23:** Auto-linked matches (90%+) use 10-second undo toast, consistent with duplicates page (Phase 14 D-07).

### Claude's Discretion
- Alembic migration details for new tracklist/track tables
- httpx client wrapper implementation (session management, headers, retries)
- arq task functions for search, scrape, and refresh jobs
- Exact weight distribution for fuzzy matching signals
- HTMX partial structure for tracklist card expand/collapse
- Pagination approach on Tracklists page (follow existing proposals pattern)
- Tracklist badge design on file cards in other pages
- 1001tracklists endpoint details and response parsing

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements
- `.planning/REQUIREMENTS.md` — TL-01 through TL-04 acceptance criteria

### Project Context
- `.planning/PROJECT.md` — Notes that 1001tracklists.com has POST endpoints for search and detail pages, no headless browser needed
- `CLAUDE.md` — httpx listed as recommended test client, rapidfuzz to be added as new dependency

### Existing Code (MUST READ)
- `src/phaze/models/file.py` — FileRecord model with state machine, FileState enum, sha256_hash, original_path, original_filename
- `src/phaze/models/metadata.py` — FileMetadata with artist, title, album, year, genre, raw_tags (matching signals source)
- `src/phaze/models/base.py` — Base and TimestampMixin for new models
- `src/phaze/services/metadata.py` — Tag extraction service pattern (reference for new tracklist service)
- `src/phaze/tasks/functions.py` — Task function patterns (reference for search/scrape tasks)
- `src/phaze/tasks/worker.py` — WorkerSettings, task registration
- `src/phaze/routers/duplicates.py` — Card layout, HTMX partials, expand/collapse pattern (reference for tracklists router)
- `src/phaze/templates/duplicates/` — Card template patterns, undo toast, Alpine.js interactivity
- `src/phaze/templates/base.html` — Navigation bar (add Tracklists link after Duplicates)
- `src/phaze/routers/proposals.py` — Pagination pattern, Jinja2Templates setup

### Prior Phase Context
- `.planning/phases/12-infrastructure-audio-tag-extraction/12-CONTEXT.md` — D-09: auto + manual triggers pattern, D-10: extract from music and video files
- `.planning/phases/14-duplicate-resolution-ui/14-CONTEXT.md` — D-01: card-per-group layout, D-06: bulk action with undo toast (10s), D-07: undo toast timing, D-10: nav link position pattern

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- Duplicates router: Card layout with HTMX expand/collapse, undo toast pattern — direct template for tracklists page
- Proposals router: Pagination with limit/offset, Jinja2Templates, filter tabs
- arq task infrastructure: Retry with backoff, task registration in WorkerSettings
- FileMetadata model: artist, title, album, year, genre fields for matching signals

### Established Patterns
- HTMX for dynamic updates (swap, trigger, out-of-band swaps)
- Tailwind CSS via CDN for styling
- Alpine.js for client-side interactivity
- Jinja2 partials in `templates/{feature}/partials/` directory structure
- Router per feature domain with APIRouter

### Integration Points
- New `models/tracklist.py` with FK to FileRecord
- New `services/tracklist.py` for search, scrape, match logic
- New `routers/tracklists.py` for UI endpoints
- New `tasks/tracklist.py` for arq search/scrape/refresh tasks
- Nav link in `base.html` after Duplicates
- Tracklist badge on file cards in proposals/duplicates templates

</code_context>

<specifics>
## Specific Ideas

- Auto-link threshold at 90% (not 95%) — user wants efficiency without sacrificing accuracy for obvious matches
- Stale refresh: any tracklist not updated in 90+ days gets re-scraped even if resolved, to catch corrections
- Parse the v1.0 naming format from filenames as a primary matching signal — many files will already be renamed by this point in the pipeline
- All four tracklist actions (unlink, re-scrape, view source, search for better match) available on every tracklist card

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 15-1001tracklists-integration*
*Context gathered: 2026-04-01*
