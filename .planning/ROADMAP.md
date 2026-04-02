# Roadmap: Phaze

## Milestones

- ✅ **v1.0 MVP** — Phases 1-11 (shipped 2026-03-30)
- 🚧 **v2.0 Metadata Enrichment & Tracklist Integration** — Phases 12-17 (in progress)

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

### v2.0 Metadata Enrichment & Tracklist Integration (In Progress)

**Milestone Goal:** Enrich the file corpus with audio tags, tracklist data, and audio fingerprinting -- building queryable infrastructure for cross-service linking and automated track identification.

- [ ] **Phase 12: Infrastructure & Audio Tag Extraction** - Shared engine pool, expanded state machine, and mutagen-based tag extraction feeding richer LLM context
- [x] **Phase 13: AI Destination Paths** - LLM-generated destination paths with collision detection and directory tree preview in approval UI (completed 2026-03-31)
- [x] **Phase 14: Duplicate Resolution UI** - Admin page for reviewing, comparing, and resolving SHA256 duplicate groups (completed 2026-04-01)
- [x] **Phase 15: 1001Tracklists Integration** - Search, scrape, fuzzy-match, and periodically refresh tracklists from 1001tracklists.com (completed 2026-04-01)
- [x] **Phase 16: Fingerprint Service & Batch Ingestion** - Long-running fingerprint container with audfprint/Panako hybrid and batch ingestion of all music files (completed 2026-04-01)
- [ ] **Phase 17: Live Set Matching & Tracklist Review** - Scan live sets against fingerprint DB and display proposed tracklists for human review

## Phase Details

### Phase 12: Infrastructure & Audio Tag Extraction
**Goal**: Every music file has its audio tags extracted and stored in PostgreSQL, with richer metadata feeding all downstream features
**Depends on**: Phase 11 (v1.0 complete)
**Requirements**: INFRA-01, INFRA-02, TAGS-01, TAGS-02, TAGS-03, TAGS-04, TAGS-05
**Success Criteria** (what must be TRUE):
  1. Worker tasks share a pooled async engine instead of creating one per invocation, and no connection exhaustion occurs under concurrent task load
  2. FileRecord state machine includes METADATA_EXTRACTED and FINGERPRINTED states, and existing files are backfilled without breaking the pipeline
  3. User can trigger tag extraction that reads ID3/Vorbis/MP4/FLAC/OPUS tags from music files and see artist, title, album, year, genre, track number, duration, and bitrate in FileMetadata
  4. Full raw tag dump is stored in FileMetadata.raw_tags JSONB column and is queryable
  5. LLM proposal prompts include extracted tag data, producing noticeably richer filename and path proposals
**Plans**: TBD

### Phase 13: AI Destination Paths
**Goal**: Users can review and approve AI-proposed destination paths alongside filenames, with collision warnings and a preview of the resulting directory structure
**Depends on**: Phase 12
**Requirements**: PATH-01, PATH-02, PATH-03, PATH-04
**Success Criteria** (what must be TRUE):
  1. LLM generates a proposed_path alongside proposed_filename using the v1.0 naming format (live sets and album tracks)
  2. Proposed destination path is visible in the approval UI next to the filename proposal
  3. When two approved files would land at the same destination path, a collision warning is displayed before execution
  4. User can view a directory tree preview showing where all approved files will land
**Plans:** 3/3 plans complete
Plans:
- [x] 13-01-PLAN.md -- Extend LLM prompt and Pydantic model with proposed_path
- [x] 13-02-PLAN.md -- Collision detection service, tree builder, preview route and templates
- [x] 13-03-PLAN.md -- Wire destination column, collision UI, execution gate, and visual verification
**UI hint**: yes

### Phase 14: Duplicate Resolution UI
**Goal**: Users can review duplicate groups, compare file quality side-by-side, and resolve duplicates through a human-in-the-loop workflow
**Depends on**: Phase 12
**Requirements**: DEDUP-01, DEDUP-02, DEDUP-03, DEDUP-04
**Success Criteria** (what must be TRUE):
  1. Admin UI displays paginated SHA256 duplicate groups with file details (path, size, format)
  2. User can select the canonical file in each group and mark the rest for deletion
  3. User can compare duplicates side-by-side showing path, size, bitrate, tags, and analysis results
  4. System pre-selects the best duplicate per group based on bitrate, tag completeness, and path length
**Plans:** 2/2 plans complete
Plans:
- [x] 14-01-PLAN.md -- Backend: model changes, scoring logic, enriched queries, resolve/undo service
- [x] 14-02-PLAN.md -- Router, templates, integration tests, and visual verification
**UI hint**: yes

### Phase 15: 1001Tracklists Integration
**Goal**: The system can search 1001tracklists.com for matching tracklists, store them in PostgreSQL, link them to files via fuzzy matching, and keep them fresh with periodic re-checks
**Depends on**: Phase 12
**Requirements**: TL-01, TL-02, TL-03, TL-04
**Success Criteria** (what must be TRUE):
  1. System searches 1001tracklists by artist and event metadata and returns matching tracklist results
  2. Tracklist data (tracks, positions, timestamps) is scraped, validated, and stored in PostgreSQL with versioned snapshots
  3. Scraped tracklists are fuzzy-matched to files using artist/event/date similarity, and matches are visible in the admin UI
  4. A background job periodically re-checks tracklists with unresolved IDs on a monthly minimum cadence with randomized jitter
**Plans:** 2/2 plans complete
Plans:
- [x] 15-01-PLAN.md -- Data model (3 tables), async scraper, fuzzy matcher with weighted scoring
- [x] 15-02-PLAN.md -- arq task integration, admin UI, periodic refresh
**UI hint**: yes

### Phase 16: Fingerprint Service & Batch Ingestion
**Goal**: A dedicated fingerprint service container is running with audfprint and Panako, and all music files are fingerprinted into a persistent database
**Depends on**: Phase 14
**Requirements**: FPRINT-01, FPRINT-02
**Success Criteria** (what must be TRUE):
  1. Fingerprint service runs as a long-lived Docker container with HTTP API endpoints (ingest, query, health) accessible from the main application
  2. Batch job fingerprints all music files through the worker pool, with progress tracking, and results persist in the fingerprint database across container restarts
**Plans**: TBD

### Phase 17: Live Set Matching & Tracklist Review
**Goal**: Users can scan live set recordings against the fingerprint database and review proposed tracklists with confidence scores before accepting them
**Depends on**: Phase 15, Phase 16
**Requirements**: FPRINT-03, FPRINT-04
**Success Criteria** (what must be TRUE):
  1. User can trigger a scan of a live set recording against the fingerprint DB and receive a list of identified tracks with timestamps and confidence scores
  2. Proposed tracklists from fingerprint matches are displayed in the admin UI with per-track confidence, and the user can approve, reject, or edit individual track identifications
**Plans**: TBD
**UI hint**: yes

## Progress

**Execution Order:**
Phases execute in numeric order: 12 -> 13 -> 14 -> 15 -> 16 -> 17
Note: Phases 13, 14, and 15 all depend only on Phase 12 and could theoretically execute in parallel.

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
| 12. Infrastructure & Audio Tag Extraction | v2.0 | 0/0 | Not started | - |
| 13. AI Destination Paths | v2.0 | 3/3 | Complete    | 2026-03-31 |
| 14. Duplicate Resolution UI | v2.0 | 2/2 | Complete    | 2026-04-01 |
| 15. 1001Tracklists Integration | v2.0 | 2/2 | Complete    | 2026-04-01 |
| 16. Fingerprint Service & Batch Ingestion | v2.0 | 3/3 | Complete    | 2026-04-01 |
| 17. Live Set Matching & Tracklist Review | v2.0 | 0/0 | Not started | - |
