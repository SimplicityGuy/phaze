# Requirements: Phaze

**Defined:** 2026-04-02
**Core Value:** Get 200K messy music and concert files properly named, organized into logical folders, deduplicated, with rich metadata in Postgres — and provide a human-in-the-loop approval workflow so nothing moves without review.

## v3.0 Requirements

Requirements for Cross-Service Intelligence & File Enrichment. Each maps to roadmap phases.

### Search

- [x] **SRCH-01**: User can search across files, tracklists, and metadata from a single search page in the admin UI
- [x] **SRCH-02**: Search results are faceted by artist, genre, date range, BPM range, and file state
- [x] **SRCH-03**: Results show unified cross-entity hits (files and tracklists together with type indicators)
- [x] **SRCH-04**: Search uses PostgreSQL full-text search with GIN indexes for sub-second response at 200K files

### Discogs Linking

- [x] **DISC-01**: System fuzzy-matches live set tracks to Discogs releases via discogsography HTTP API
- [x] **DISC-02**: Candidate matches stored with confidence scores in DiscogsLink table, displayed in admin UI
- [x] **DISC-03**: User can query "find all sets containing track X" across phaze and discogsography data
- [x] **DISC-04**: User can bulk-link an entire tracklist's tracks to Discogs releases in one action

### Tag Writing

- [x] **TAGW-01**: User can write corrected tags to destination file copies (never originals) with format-aware encoding (ID3/Vorbis/MP4)
- [x] **TAGW-02**: Tag writes are verified by re-reading the file after write, with discrepancies flagged
- [x] **TAGW-03**: All tag writes logged in append-only TagWriteLog audit table
- [x] **TAGW-04**: Tag review page shows proposed vs current tags side-by-side before user approves the write

### CUE Sheets

- [ ] **CUE-01**: System generates .cue companion files from tracklist data, preferring fingerprint timestamps with 1001tracklists fallback
- [ ] **CUE-02**: CUE files use correct 75fps frame conversion and UTF-8 with BOM encoding
- [ ] **CUE-03**: CUE files include REM comments with Discogs metadata (genre, label, catalog number, year)

## Future Requirements

Deferred to v4+. Tracked but not in current roadmap.

### Enhanced Intelligence

- **NLQ-01**: Natural language querying across services
- **FPRINT-05**: Acoustic near-duplicate detection via fingerprint similarity

## Out of Scope

Explicitly excluded. Documented to prevent scope creep.

| Feature | Reason |
|---------|--------|
| Tag writing to original source files | Modifying originals is destructive; only write to destination copies |
| Auto-linking without review | Violates human-in-the-loop constraint; store candidates, user approves |
| Elasticsearch/Meilisearch | PostgreSQL FTS with GIN indexes handles 200K files; no external search engine needed |
| Real-time search-as-you-type | Single-user tool; standard form submit with instant response is sufficient |
| Auto-generate CUE without tracklist | CUE requires timing data; no way to generate meaningful CUE without track positions |
| Discogs API direct calls | Route through discogsography service; never call Discogs API directly from phaze |

## Traceability

Which phases cover which requirements. Updated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| SRCH-01 | Phase 18 | Complete |
| SRCH-02 | Phase 18 | Complete |
| SRCH-03 | Phase 18 | Complete |
| SRCH-04 | Phase 18 | Complete |
| DISC-01 | Phase 19 | Complete |
| DISC-02 | Phase 19 | Complete |
| DISC-03 | Phase 19 | Complete |
| DISC-04 | Phase 19 | Complete |
| TAGW-01 | Phase 20 | Complete |
| TAGW-02 | Phase 20 | Complete |
| TAGW-03 | Phase 20 | Complete |
| TAGW-04 | Phase 20 | Complete |
| CUE-01 | Phase 21 | Pending |
| CUE-02 | Phase 21 | Pending |
| CUE-03 | Phase 21 | Pending |

**Coverage:**
- v3.0 requirements: 15 total
- Mapped to phases: 15
- Unmapped: 0

---
*Requirements defined: 2026-04-02*
*Last updated: 2026-04-02 after roadmap creation*
