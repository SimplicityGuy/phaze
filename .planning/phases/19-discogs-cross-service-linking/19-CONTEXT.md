# Phase 19: Discogs Cross-Service Linking - Context

**Gathered:** 2026-04-02
**Status:** Ready for planning

<domain>
## Phase Boundary

Link live set tracks to Discogs releases via the discogsography HTTP API. Store candidate matches with confidence scores in a new DiscogsLink table, let users review and accept/dismiss candidates inline on the tracklist page, extend the existing search page with Discogs release results, and support bulk-linking all tracks in a tracklist to their top matches.

</domain>

<decisions>
## Implementation Decisions

### Match Triggering & Scope
- **D-01:** Per-tracklist "Match to Discogs" button on the tracklist detail page triggers matching. No batch-all-tracklists endpoint.
- **D-02:** Only tracks with both artist AND title populated are eligible for matching. Tracks with missing data are skipped.
- **D-03:** Matching runs as a SAQ background task (one job per tracklist). Each track fires an HTTP request to discogsography `/api/search`. Matches the existing fingerprint scan pattern.

### Candidate Review UX
- **D-04:** Candidates appear inline on the tracklist page — expand each track row to show candidate Discogs matches below it. No separate dedicated linking page.
- **D-05:** Each candidate row shows: artist, title, label, year, confidence score. Compact table row format.
- **D-06:** Store top 3 highest-confidence matches per track. Can re-match for more.
- **D-07:** Actions per candidate: Accept (links track to release, auto-dismisses other candidates) or Dismiss (removes candidate). One accepted link per track.

### Cross-System Query Design
- **D-08:** Extend the existing Phase 18 search page with a "Discogs releases" entity type. Reuse established search patterns.
- **D-09:** Search queries stored DiscogsLink data only — no live calls to discogsography during search. Fast, consistent with human-in-the-loop model.
- **D-10:** Discogs results shown as purple pill badges in the unified results table (blue = files, green = tracklists, purple = Discogs releases). Same dense row format.

### Bulk-Link Behavior
- **D-11:** "Bulk-link" button accepts the highest-confidence candidate for every track in the tracklist that has candidates. One-click action.
- **D-12:** Bulk-link requires matches to exist first — user must trigger "Match to Discogs" before bulk-linking. Two-step flow: match -> review (optional) -> bulk-link.

### Claude's Discretion
- DiscogsLink model schema details (columns, indexes, relationships)
- Fuzzy matching strategy (rapidfuzz algorithm choice, scoring normalization)
- discogsography API adapter implementation (retry logic, timeout handling)
- SAQ task structure (job naming, progress reporting)
- HTMX partial structure for inline candidate display
- Search integration implementation details (FTS config for Discogs data)

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Discogs Integration Target
- `/Users/Robert/Code/public/discogsography/api/routers/search.py` — discogsography `/api/search` endpoint: takes `q`, `types`, `genres`, `year_min`, `year_max`, `limit`, `offset`. Returns JSON with relevance-ranked results.
- `/Users/Robert/Code/public/discogsography/api/queries/search_queries.py` — Search query implementation and `ALL_TYPES` constant (artist, label, master, release)

### Existing Patterns to Follow
- `src/phaze/services/fingerprint.py` — httpx pattern for calling external services (discogsography adapter should follow this)
- `src/phaze/services/tracklist_scraper.py` — Another httpx external service call pattern
- `src/phaze/models/tracklist.py` — TracklistTrack model (artist, title, label fields used for matching)
- `src/phaze/routers/tracklists.py` — Tracklist admin UI router with approve/reject, inline editing, HTMX partials
- `src/phaze/routers/search.py` — Phase 18 search router to extend with Discogs entity type
- `src/phaze/services/search_queries.py` — Phase 18 search queries to extend

### UI Patterns
- `src/phaze/templates/base.html` — Nav bar, pill badge patterns
- `src/phaze/templates/tracklists/` — Tracklist page templates (inline expand pattern)
- `src/phaze/templates/search/` — Search page templates (entity type pills, results table)

### Requirements
- `.planning/REQUIREMENTS.md` — DISC-01 through DISC-04

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `httpx` async client: Used in `fingerprint.py` and `tracklist_scraper.py` — established pattern for external HTTP calls
- `rapidfuzz`: Already in dependencies — available for local fuzzy matching/scoring
- SAQ task queue: Established pattern from fingerprint scan tasks
- HTMX partials: All pages use `hx-get`/`hx-target`/`hx-swap` for dynamic updates
- Alpine.js: Used for collapsible panels, toggle states
- Pill badge component: Blue (files), green (tracklists) — extend with purple for Discogs

### Established Patterns
- FastAPI router + Jinja2 templates + HTMX partials for all pages
- SQLAlchemy async models with UUID primary keys, TimestampMixin
- Alembic migrations for schema changes
- SAQ background tasks for long-running operations (fingerprint scan, analysis)
- Inline editing on tracklist page via HTMX swaps

### Integration Points
- New model: `src/phaze/models/discogs_link.py` — DiscogsLink table
- New service: `src/phaze/services/discogs_matcher.py` — discogsography API adapter + matching logic
- New SAQ task: `src/phaze/tasks/` — Discogs matching background task
- Router update: `src/phaze/routers/tracklists.py` — Add match/accept/dismiss/bulk-link endpoints
- Router update: `src/phaze/routers/search.py` — Add Discogs release entity type
- New templates: Tracklist inline candidate partials, search result Discogs rows
- Alembic migration: Create discogs_links table

</code_context>

<specifics>
## Specific Ideas

No specific requirements — open to standard approaches

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope

</deferred>

---

*Phase: 19-discogs-cross-service-linking*
*Context gathered: 2026-04-02*
