# Requirements: Phaze

**Defined:** 2026-03-27
**Core Value:** Get 200K messy music and concert files properly named, organized into logical folders, deduplicated, with rich metadata in Postgres — with human-in-the-loop approval.

## v1 Requirements

Requirements for initial release. Each maps to roadmap phases.

### Ingestion

- [x] **ING-01**: System can scan directories recursively to discover music files (mp3, m4a, ogg), video files, and companion files (cue, nfo, txt, jpg, png, m3u, pls)
- [x] **ING-02**: System extracts sha256 hash for every discovered file
- [x] **ING-03**: System records original filename and original path for every file in PostgreSQL
- [x] **ING-04**: System detects exact duplicates via sha256 and flags them for review
- [x] **ING-05**: System classifies each file by type (music, video, companion) and stores the classification
- [x] **ING-06**: System associates companion files with nearby music/video files using directory proximity heuristics

### Analysis

- [ ] **ANL-01**: System detects BPM for music files using librosa/existing prototypes
- [ ] **ANL-02**: System classifies mood and style for music files using existing prototypes
- [ ] **ANL-03**: Analysis runs in parallel across worker pool for throughput at scale

### AI Proposals

- [ ] **AIP-01**: System uses LLM to propose a new filename for each file based on available metadata, analysis results, and companion file content where available
- [ ] **AIP-02**: Proposals are stored as immutable records in PostgreSQL (not regenerated on the fly)

### Approval

- [ ] **APR-01**: Admin can view paginated list of all proposed renames in a web UI
- [ ] **APR-02**: Admin can approve or reject individual proposals
- [ ] **APR-03**: Admin can filter proposals by status (pending, approved, rejected)

### Execution

- [ ] **EXE-01**: System executes approved renames using copy-verify-delete protocol (never direct move)
- [ ] **EXE-02**: System logs every file operation to an append-only audit table in PostgreSQL

### Infrastructure

- [x] **INF-01**: All services run via Docker Compose (API, workers, PostgreSQL, Redis)
- [ ] **INF-02**: Task queue (arq + Redis) manages parallel worker processing
- [ ] **INF-03**: Database migrations managed via Alembic

## v2 Requirements

Deferred to future release. Tracked but not in current roadmap.

### Metadata

- **META-01**: System extracts existing tags (ID3, Vorbis, MP4) from music files
- **META-02**: System extracts video metadata (duration, codec, resolution) via ffprobe
- **META-03**: MusicBrainz metadata enrichment via audio fingerprinting

### AI Enhancements

- **AIP-03**: AI proposes destination folder paths based on genre/mood/artist/event
- **AIP-04**: Concert/event auto-detection from filename patterns and metadata

### Approval Enhancements

- **APR-04**: Batch approval with smart grouping (by artist/album/event)
- **APR-05**: Inline editing of proposals before approval

### Execution Enhancements

- **EXE-03**: Full undo/rollback via audit trail
- **EXE-04**: Acoustic duplicate detection via Chromaprint fingerprint similarity
- **EXE-05**: Progress tracking / job status visibility in UI

### External Integrations

- **EXT-01**: Search frontend for all music/content
- **EXT-02**: Natural language querying across services
- **EXT-03**: 1001tracklists tracklist scraping and enrichment
- **EXT-04**: Discogsography cross-service linking and search

## Out of Scope

Explicitly excluded. Documented to prevent scope creep.

| Feature | Reason |
|---------|--------|
| Music player / streaming | Different product — Plex, Jellyfin, Navidrome exist |
| Multi-user auth | Single user on private network |
| Real-time file watching | Batch organization tool — on-demand scans are sufficient |
| Tag writing / metadata embedding | Modifying original files is destructive at 200K scale |
| Streaming service integration | Not needed for file organization |
| Auto-approve mode | Violates human-in-the-loop safety constraint |

## Traceability

Which phases cover which requirements. Updated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| ING-01 | Phase 2: File Discovery & Ingestion | Complete |
| ING-02 | Phase 2: File Discovery & Ingestion | Complete |
| ING-03 | Phase 2: File Discovery & Ingestion | Complete |
| ING-04 | Phase 3: Companion Files & Deduplication | Complete |
| ING-05 | Phase 2: File Discovery & Ingestion | Complete |
| ING-06 | Phase 3: Companion Files & Deduplication | Complete |
| ANL-01 | Phase 5: Audio Analysis Pipeline | Pending |
| ANL-02 | Phase 5: Audio Analysis Pipeline | Pending |
| ANL-03 | Phase 4: Task Queue & Worker Infrastructure | Pending |
| AIP-01 | Phase 6: AI Proposal Generation | Pending |
| AIP-02 | Phase 6: AI Proposal Generation | Pending |
| APR-01 | Phase 7: Approval Workflow UI | Pending |
| APR-02 | Phase 7: Approval Workflow UI | Pending |
| APR-03 | Phase 7: Approval Workflow UI | Pending |
| EXE-01 | Phase 8: Safe File Execution & Audit | Pending |
| EXE-02 | Phase 8: Safe File Execution & Audit | Pending |
| INF-01 | Phase 1: Infrastructure & Project Setup | Complete |
| INF-02 | Phase 4: Task Queue & Worker Infrastructure | Pending |
| INF-03 | Phase 1: Infrastructure & Project Setup | Pending |

**Coverage:**
- v1 requirements: 17 total
- Mapped to phases: 17
- Unmapped: 0

---
*Requirements defined: 2026-03-27*
*Last updated: 2026-03-27 after roadmap creation*
