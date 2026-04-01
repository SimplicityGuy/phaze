# Phase 15: 1001Tracklists Integration - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-04-01
**Phase:** 15-1001tracklists-integration
**Areas discussed:** Data model & storage, Search & scraping, Fuzzy matching logic, Admin UI presentation

---

## Data Model & Storage

### Tracklist-to-File Relationship

| Option | Description | Selected |
|--------|-------------|----------|
| One-to-one | Each live set recording maps to at most one tracklist. Simplest model. | ✓ |
| Many-to-many | Multiple files could reference the same tracklist. Requires junction table. | |
| One tracklist, many files | A tracklist can link to multiple files but each file has at most one tracklist. | |

**User's choice:** One-to-one
**Notes:** None

### Versioning Strategy

| Option | Description | Selected |
|--------|-------------|----------|
| Overwrite in place | Latest scrape replaces previous data. Simple, no history. | |
| Versioned snapshots | Each scrape creates a new version. Can compare old vs new. | ✓ |
| Append-only with timestamps | Never delete, only insert new versions. Full audit trail. | |

**User's choice:** Versioned snapshots
**Notes:** None

### Version UI Visibility

| Option | Description | Selected |
|--------|-------------|----------|
| DB only | Store versions in DB but UI always shows latest. History for debugging/auditing. | ✓ |
| Show version diff | UI shows what changed between scrapes (tracks added/removed). | |

**User's choice:** DB only
**Notes:** None

### Source Reference

| Option | Description | Selected |
|--------|-------------|----------|
| URL + external ID | Store both the 1001tracklists page URL and their internal ID. | ✓ |
| URL only | Just the page URL. Simpler but less precise for API lookups. | |

**User's choice:** URL + external ID
**Notes:** None

### Track Data Fields

| Option | Description | Selected |
|--------|-------------|----------|
| All available | Position, artist, title, label, timestamp/cue time, and mashup/remix metadata. | ✓ |
| Core only | Position, artist, title. Skip label and timestamps. | |
| Core + timestamps | Position, artist, title, timestamps. Skip label metadata. | |

**User's choice:** All available
**Notes:** None

### Model File Location

| Option | Description | Selected |
|--------|-------------|----------|
| New file (models/tracklist.py) | Tracklist and TracklistTrack models in new file. Clean separation. | ✓ |
| Extend metadata.py | Add tracklist tables to existing metadata module. | |

**User's choice:** New file
**Notes:** None

---

## Search & Scraping

### Search Trigger

| Option | Description | Selected |
|--------|-------------|----------|
| Manual + auto | Manual button per file plus automatic search after tag extraction. | ✓ |
| Manual only | User triggers search per file or batch from UI. | |
| Automatic only | System searches automatically after tag extraction. | |

**User's choice:** Manual + auto
**Notes:** None

### Rate Limiting

| Option | Description | Selected |
|--------|-------------|----------|
| Fixed delay | Simple sleep between requests (2-5 seconds). | ✓ |
| Adaptive backoff | Start fast, slow down on errors/429s. | |
| Token bucket | X requests per minute with burst capacity. | |

**User's choice:** Fixed delay
**Notes:** None

### Multiple Search Results

| Option | Description | Selected |
|--------|-------------|----------|
| Show all, user picks | Present all candidates ranked by relevance. User selects. | ✓ |
| Auto-pick best match | System auto-selects highest confidence match. | |
| Threshold filter | Only show matches above a confidence threshold. | |

**User's choice:** Show all, user picks
**Notes:** None

### HTTP Client

| Option | Description | Selected |
|--------|-------------|----------|
| httpx | Already a dev dependency. Async-native. | ✓ |
| aiohttp | Mature async HTTP client. New dependency. | |
| requests | Simple, sync. Would need thread pool. | |

**User's choice:** httpx
**Notes:** None

### Refresh Scope

| Option | Description | Selected |
|--------|-------------|----------|
| Unresolved only | Only re-scrape tracklists with unmatched tracks. | |
| All tracklists | Periodically re-scrape everything. | |
| Stale + unresolved | Re-scrape unresolved AND any not updated in 90+ days. | ✓ |

**User's choice:** Stale + unresolved
**Notes:** None

### Error Handling

| Option | Description | Selected |
|--------|-------------|----------|
| Log + retry silently | Errors logged, auto-retried on next refresh cycle. No UI noise. | ✓ |
| Show in UI | Failed scrapes visible in admin UI with error details. | |
| Log only | Errors in application logs. No UI visibility, no auto-retry. | |

**User's choice:** Log + retry silently
**Notes:** None

---

## Fuzzy Matching Logic

### Match Signals

| Option | Description | Selected |
|--------|-------------|----------|
| Artist + event + date | Primary: artist similarity. Secondary: event/venue. Tertiary: date proximity. | ✓ |
| Artist + filename parsing | Match artist from tags, parse event/date from filename pattern. | |
| Full-text similarity | Overall text similarity between tracklist and file metadata. | |

**User's choice:** Artist + event + date
**Notes:** None

### Confidence Expression

| Option | Description | Selected |
|--------|-------------|----------|
| Numeric score 0-100 | Weighted score combining individual signal similarities. Percentage. | ✓ |
| Tiered labels | High / Medium / Low confidence buckets. | |
| Individual signal scores | Show each signal's score separately. | |

**User's choice:** Numeric score 0-100
**Notes:** None

### Auto-Link Policy

| Option | Description | Selected |
|--------|-------------|----------|
| Always human approval | All matches queue for review regardless of confidence. | |
| Auto-link above 95% | Very high confidence matches auto-link. | |
| Auto-link with undo | Auto-link all matches with undo option. | |

**User's choice:** Auto-link above 90% with undo (custom — combines auto-link threshold with undo)
**Notes:** User specified 90% threshold with 10-second undo toast, modifying the presented options.

### String Similarity Library

| Option | Description | Selected |
|--------|-------------|----------|
| rapidfuzz | Fast C-extension. Levenshtein, Jaro-Winkler, token set ratio. MIT. | ✓ |
| thefuzz (fuzzywuzzy) | Well-known but slower (pure Python fallback). | |
| Custom scoring | Write matching logic from scratch. | |

**User's choice:** rapidfuzz
**Notes:** None

### Filename Parsing Priority

| Option | Description | Selected |
|--------|-------------|----------|
| Parse structured names | Extract artist, venue, date from v1.0 naming format as primary signal. | ✓ |
| Tags first, filename fallback | Prefer tag data, only parse filename if tags missing. | |
| Both equally weighted | Use both parsed filename and tags as independent signals. | |

**User's choice:** Parse structured names
**Notes:** None

### Disambiguation Strategy

| Option | Description | Selected |
|--------|-------------|----------|
| Present all candidates | Show all close matches ranked by score. User picks. | ✓ |
| Strict date matching | Only match if date within ±1 day. | |
| Group by event | Cluster by event/venue, sort by date within clusters. | |

**User's choice:** Present all candidates
**Notes:** None

---

## Admin UI Presentation

### Page Placement

| Option | Description | Selected |
|--------|-------------|----------|
| Dedicated page + file badge | New Tracklists page plus badge on file cards in other views. | ✓ |
| Dedicated page only | Standalone Tracklists page. No cross-page info. | |
| Inline on file views | Tracklist info on proposal/file detail pages. No separate page. | |

**User's choice:** Dedicated page + file badge
**Notes:** None

### Nav Position

| Option | Description | Selected |
|--------|-------------|----------|
| After Duplicates | Pipeline > Proposals > Preview > Duplicates > Tracklists > Audit Log | ✓ |
| After Preview | Pipeline > Proposals > Preview > Tracklists > Duplicates > Audit Log | |
| Before Audit Log | Always last before Audit Log. | |

**User's choice:** After Duplicates
**Notes:** None

### Page Layout

| Option | Description | Selected |
|--------|-------------|----------|
| Card-per-tracklist | Card with artist, event, date, track count, confidence. HTMX expand. | ✓ |
| Table with expandable rows | Tabular layout, click to expand. Denser display. | |
| Two-panel | Left: list. Right: selected tracklist details. | |

**User's choice:** Card-per-tracklist
**Notes:** Consistent with duplicates page pattern from Phase 14.

### Available Actions

| Option | Description | Selected |
|--------|-------------|----------|
| Unlink from file | Remove tracklist-file association. Tracklist stays in DB. | ✓ |
| Re-scrape now | Force fresh scrape from 1001tracklists. | ✓ |
| View on 1001tracklists | External link to source page. | ✓ |
| Search for better match | Trigger new search for alternative tracklists. | ✓ |

**User's choice:** All four actions selected
**Notes:** None

### Unmatched Files Display

| Option | Description | Selected |
|--------|-------------|----------|
| Separate tab/filter | Tabs: Matched / Unmatched / All. Unmatched shown with Search button. | ✓ |
| Mixed list with status badge | All files in one list. Unmatched files show badge. | |
| Unmatched not shown | Only show files WITH tracklists. | |

**User's choice:** Separate tab/filter
**Notes:** None

### Auto-Link Undo Window

| Option | Description | Selected |
|--------|-------------|----------|
| Persistent until dismissed | Auto-matched badge, user can unlink anytime. | |
| 10-second toast | Undo toast for 10 seconds, consistent with Phase 14 D-07. | ✓ |
| Review queue | Auto-linked matches go to Review tab for batch confirmation. | |

**User's choice:** 10-second toast
**Notes:** Consistent with duplicates page.

---

## Claude's Discretion

- Alembic migration details for new tables
- httpx client wrapper implementation
- arq task functions for search, scrape, refresh
- Exact weight distribution for fuzzy matching signals
- HTMX partial structure for card expand/collapse
- Pagination approach (follow proposals pattern)
- Tracklist badge design on file cards
- 1001tracklists endpoint parsing details

## Deferred Ideas

None — discussion stayed within phase scope.
