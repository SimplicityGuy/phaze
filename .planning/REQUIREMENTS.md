# Requirements: Phaze

**Defined:** 2026-03-30
**Core Value:** Get 200K messy music and concert files properly named, organized into logical folders, deduplicated, with rich metadata in Postgres — and provide a human-in-the-loop approval workflow so nothing moves without review.

## v2.0 Requirements

Requirements for Metadata Enrichment & Tracklist Integration. Each maps to roadmap phases.

### Infrastructure

- [ ] **INFRA-01**: Task session uses a shared async engine pool instead of creating a new engine per invocation
- [ ] **INFRA-02**: FileRecord state machine expanded with METADATA_EXTRACTED and FINGERPRINTED states, all consumers updated atomically

### Audio Tags

- [ ] **TAGS-01**: User can trigger tag extraction that reads ID3/Vorbis/MP4/FLAC/OPUS tags from all music files
- [ ] **TAGS-02**: Extracted tags populate FileMetadata with artist, title, album, year, genre, track number
- [ ] **TAGS-03**: Full raw tag dump stored in FileMetadata.raw_tags JSONB column
- [ ] **TAGS-04**: Duration and bitrate extracted from audio file info and stored in FileMetadata
- [ ] **TAGS-05**: LLM proposal context includes extracted tag data for richer filename/path proposals

### AI Destination Paths

- [x] **PATH-01**: LLM prompt generates proposed_path alongside proposed_filename using v1.0 naming format
- [x] **PATH-02**: Proposed destination path displayed in approval UI alongside filename
- [x] **PATH-03**: Path collisions detected and flagged when two files would land at the same destination
- [x] **PATH-04**: User can view a directory tree preview of where approved files will land

### Duplicate Resolution

- [x] **DEDUP-01**: Admin UI page displays SHA256 duplicate groups with file details, paginated
- [ ] **DEDUP-02**: User can select the canonical file per duplicate group and mark others for deletion
- [ ] **DEDUP-03**: User can compare duplicates side-by-side (path, size, bitrate, tags, analysis)
- [x] **DEDUP-04**: System pre-selects the best duplicate based on bitrate, tag completeness, and path length

### Audio Fingerprinting

- [ ] **FPRINT-01**: Fingerprint service runs as a long-running Docker container with API/message interface
- [ ] **FPRINT-02**: Batch job fingerprints all music files via worker pool with persistent fingerprint database
- [ ] **FPRINT-03**: User can scan a live set recording against the fingerprint DB to identify tracks with timestamps
- [ ] **FPRINT-04**: Proposed tracklists from fingerprint matches displayed in admin UI for review and approval

### 1001Tracklists

- [ ] **TL-01**: System searches 1001tracklists by artist and event to find matching tracklists
- [ ] **TL-02**: Tracklist data (tracks, positions, timestamps) scraped and stored in PostgreSQL
- [ ] **TL-03**: Scraped tracklists fuzzy-matched to files using artist/event/date similarity
- [ ] **TL-04**: Background job periodically re-checks tracklists with unresolved IDs (monthly minimum, randomized)

## Future Requirements

Deferred to v3+. Tracked but not in current roadmap.

### Cross-Service Integration

- **XSVC-01**: Discogsography cross-service linking (e.g., "find all sets containing track X")
- **XSVC-02**: Search frontend for querying across files, tracklists, and metadata
- **XSVC-03**: Natural language querying across services

### Enhanced Fingerprinting

- **FPRINT-05**: Acoustic near-duplicate detection via fingerprint similarity (complement to SHA256 exact dedup)
- **FPRINT-06**: Cross-reference fingerprint matches with scraped 1001tracklists for high-confidence identification

### File Management

- **FILE-01**: Write corrected tags to destination copies after approved move
- **FILE-02**: Generate CUE sheets as companion files from tracklist data

## Out of Scope

Explicitly excluded. Documented to prevent scope creep.

| Feature | Reason |
|---------|--------|
| Tag writing to source files | Modifying originals is destructive; Postgres is the metadata store |
| Album art extraction/display | Scope creep; store presence as boolean only |
| AcoustID/Chromaprint web service lookup | Rate-limited, requires internet (private network), local matching is the goal |
| Real-time fingerprinting during playback | Different architecture (Shazam-like), not a collection organizer feature |
| Headless browser scraping | POST endpoints confirmed to work without it |
| Bulk scraping entire 1001tracklists database | Abusive; only scrape for files we have |
| Auto-delete duplicates without approval | Violates human-in-the-loop constraint |
| Merge metadata from duplicate files | Complex edge cases; keep the file with best metadata |
| Auto-create directories during proposal | Premature; create only during approved execution |
| User-editable path templates | Single-user tool; edit the LLM prompt directly |

## Traceability

Which phases cover which requirements. Updated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| INFRA-01 | Phase 12 | Pending |
| INFRA-02 | Phase 12 | Pending |
| TAGS-01 | Phase 12 | Pending |
| TAGS-02 | Phase 12 | Pending |
| TAGS-03 | Phase 12 | Pending |
| TAGS-04 | Phase 12 | Pending |
| TAGS-05 | Phase 12 | Pending |
| PATH-01 | Phase 13 | Complete |
| PATH-02 | Phase 13 | Complete |
| PATH-03 | Phase 13 | Complete |
| PATH-04 | Phase 13 | Complete |
| DEDUP-01 | Phase 14 | Complete |
| DEDUP-02 | Phase 14 | Pending |
| DEDUP-03 | Phase 14 | Pending |
| DEDUP-04 | Phase 14 | Complete |
| FPRINT-01 | Phase 16 | Pending |
| FPRINT-02 | Phase 16 | Pending |
| FPRINT-03 | Phase 17 | Pending |
| FPRINT-04 | Phase 17 | Pending |
| TL-01 | Phase 15 | Pending |
| TL-02 | Phase 15 | Pending |
| TL-03 | Phase 15 | Pending |
| TL-04 | Phase 15 | Pending |

**Coverage:**
- v2.0 requirements: 23 total
- Mapped to phases: 23
- Unmapped: 0

---
*Requirements defined: 2026-03-30*
*Last updated: 2026-03-30 after roadmap creation*
