# Roadmap: Phaze

## Milestones

- ✅ **v1.0 MVP** — Phases 1-11 (shipped 2026-03-30)
- ✅ **v2.0 Metadata Enrichment & Tracklist Integration** — Phases 12-17 (shipped 2026-04-02)
- 🚧 **v3.0 Cross-Service Intelligence & File Enrichment** — Phases 18-21 (in progress)

## Phases

<details>
<summary>v1.0 MVP (Phases 1-11) -- SHIPPED 2026-03-30</summary>

- [x] Phase 1: Infrastructure & Project Setup (3/3 plans) -- completed 2026-03-27
- [x] Phase 2: File Discovery & Ingestion (3/3 plans) -- completed 2026-03-27
- [x] Phase 3: Companion Files & Deduplication (2/2 plans) -- completed 2026-03-27
- [x] Phase 4: Task Queue & Worker Infrastructure (2/2 plans) -- completed 2026-03-27
- [x] Phase 5: Audio Analysis Pipeline (2/2 plans) -- completed 2026-03-28
- [x] Phase 6: AI Proposal Generation (2/2 plans) -- completed 2026-03-28
- [x] Phase 7: Approval Workflow UI (3/3 plans) -- completed 2026-03-29
- [x] Phase 8: Safe File Execution & Audit (2/2 plans) -- completed 2026-03-29
- [x] Phase 9: Pipeline Orchestration (1/1 plan) -- completed 2026-03-30
- [x] Phase 10: CI Config & Bug Fixes (1/1 plan) -- completed 2026-03-30
- [x] Phase 11: Polish & Cleanup (3/3 plans) -- completed 2026-03-30

Full details: `.planning/milestones/v1.0-ROADMAP.md`

</details>

<details>
<summary>v2.0 Metadata Enrichment & Tracklist Integration (Phases 12-17) -- SHIPPED 2026-04-02</summary>

- [x] Phase 12: Infrastructure & Audio Tag Extraction (3/3 plans) -- completed 2026-03-31
- [x] Phase 13: AI Destination Paths (3/3 plans) -- completed 2026-03-31
- [x] Phase 14: Duplicate Resolution UI (2/2 plans) -- completed 2026-04-01
- [x] Phase 15: 1001Tracklists Integration (2/2 plans) -- completed 2026-04-01
- [x] Phase 16: Fingerprint Service & Batch Ingestion (3/3 plans) -- completed 2026-04-01
- [x] Phase 17: Live Set Matching & Tracklist Review (3/3 plans) -- completed 2026-04-02

Full details: `.planning/milestones/v2.0-ROADMAP.md`

</details>

### v3.0 Cross-Service Intelligence & File Enrichment (In Progress)

- [x] **Phase 18: Unified Search** - Full-text search across files, tracklists, and metadata with faceted filtering (completed 2026-04-03)
- [x] **Phase 19: Discogs Cross-Service Linking** - Fuzzy-match tracks to Discogs releases via discogsography, store candidate links with confidence scores (completed 2026-04-03)
- [ ] **Phase 20: Tag Writing** - Write corrected tags to destination file copies with review UI, verify-after-write, and audit logging
- [ ] **Phase 21: CUE Sheet Generation** - Generate .cue companion files from tracklist timestamps with Discogs metadata enrichment

## Phase Details

### Phase 18: Unified Search
**Goal**: Users can find any file, tracklist, or track from a single search page with sub-second results at 200K files
**Depends on**: Phase 17 (existing data models in place)
**Requirements**: SRCH-01, SRCH-02, SRCH-03, SRCH-04
**Success Criteria** (what must be TRUE):
  1. User can type a query on the search page and see matching files and tracklists together with type indicators
  2. User can narrow results by artist, genre, date range, BPM range, and file state using facet controls
  3. Search returns results in under one second on a 200K-file database (PostgreSQL FTS with GIN indexes)
  4. Search page is accessible as a first-class tab in the admin navigation bar
**Plans**: 2 (01-search-data-layer, 02-search-ui)
**UI hint**: yes

### Phase 19: Discogs Cross-Service Linking
**Goal**: Users can link live set tracks to Discogs releases and query across both systems
**Depends on**: Phase 18 (search patterns established, search benefits from Discogs data)
**Requirements**: DISC-01, DISC-02, DISC-03, DISC-04
**Success Criteria** (what must be TRUE):
  1. User can trigger batch Discogs matching on a tracklist and see candidate matches with confidence scores in the admin UI
  2. User can review and approve/reject individual Discogs link candidates (not auto-committed)
  3. User can query "find all sets containing track X" and get results spanning phaze tracklists and Discogs releases
  4. User can bulk-link all tracks in a tracklist to their top Discogs matches in one action
**Plans**: 3 plans
Plans:
- [x] 19-01-PLAN.md -- Data layer: DiscogsLink model, migration, API adapter, fuzzy matcher, SAQ task
- [x] 19-02-PLAN.md -- Tracklist UI: match/accept/dismiss/bulk-link endpoints and templates
- [x] 19-03-PLAN.md -- Search extension: Discogs UNION ALL branch and purple pill badge
**UI hint**: yes

### Phase 20: Tag Writing
**Goal**: Users can push corrected metadata from Postgres into destination file tags with full review and audit trail
**Depends on**: Phase 19 (Discogs data available as additional metadata source for tag enrichment)
**Requirements**: TAGW-01, TAGW-02, TAGW-03, TAGW-04
**Success Criteria** (what must be TRUE):
  1. User can view proposed vs current tags side-by-side on a review page before approving a tag write
  2. User can write corrected tags to destination copies across all supported formats (MP3, M4A, OGG, OPUS, FLAC) and the system verifies correctness by re-reading the file
  3. All tag writes appear in an append-only audit log with before/after snapshots
  4. Tag writes are blocked on non-EXECUTED files (only destination copies are writable)
**Plans**: TBD
**UI hint**: yes

### Phase 21: CUE Sheet Generation
**Goal**: Users can generate .cue companion files from tracklist data enriched with Discogs metadata
**Depends on**: Phase 20 (verified metadata and Discogs REM data available)
**Requirements**: CUE-01, CUE-02, CUE-03
**Success Criteria** (what must be TRUE):
  1. User can generate a .cue file from any approved tracklist, with timestamps preferring fingerprint data over 1001tracklists positions
  2. Generated CUE files use correct 75fps frame conversion (MM:SS:FF) and UTF-8 with BOM encoding
  3. CUE files include REM comments with Discogs metadata (genre, label, catalog number, year) when available
**Plans**: TBD

## Progress

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 1. Infrastructure & Project Setup | v1.0 | 3/3 | Complete | 2026-03-27 |
| 2. File Discovery & Ingestion | v1.0 | 3/3 | Complete | 2026-03-27 |
| 3. Companion Files & Deduplication | v1.0 | 2/2 | Complete | 2026-03-27 |
| 4. Task Queue & Worker Infrastructure | v1.0 | 2/2 | Complete | 2026-03-27 |
| 5. Audio Analysis Pipeline | v1.0 | 2/2 | Complete | 2026-03-28 |
| 6. AI Proposal Generation | v1.0 | 2/2 | Complete | 2026-03-28 |
| 7. Approval Workflow UI | v1.0 | 3/3 | Complete | 2026-03-29 |
| 8. Safe File Execution & Audit | v1.0 | 2/2 | Complete | 2026-03-29 |
| 9. Pipeline Orchestration | v1.0 | 1/1 | Complete | 2026-03-30 |
| 10. CI Config & Bug Fixes | v1.0 | 1/1 | Complete | 2026-03-30 |
| 11. Polish & Cleanup | v1.0 | 3/3 | Complete | 2026-03-30 |
| 12. Infrastructure & Audio Tag Extraction | v2.0 | 3/3 | Complete | 2026-03-31 |
| 13. AI Destination Paths | v2.0 | 3/3 | Complete | 2026-03-31 |
| 14. Duplicate Resolution UI | v2.0 | 2/2 | Complete | 2026-04-01 |
| 15. 1001Tracklists Integration | v2.0 | 2/2 | Complete | 2026-04-01 |
| 16. Fingerprint Service & Batch Ingestion | v2.0 | 3/3 | Complete | 2026-04-01 |
| 17. Live Set Matching & Tracklist Review | v2.0 | 3/3 | Complete | 2026-04-02 |
| 18. Unified Search | v3.0 | 2/2 | Complete    | 2026-04-03 |
| 19. Discogs Cross-Service Linking | v3.0 | 3/3 | Complete    | 2026-04-03 |
| 20. Tag Writing | v3.0 | 0/? | Not started | - |
| 21. CUE Sheet Generation | v3.0 | 0/? | Not started | - |
