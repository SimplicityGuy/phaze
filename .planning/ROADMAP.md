# Roadmap: Phaze

## Overview

Phaze transforms a chaotic collection of ~200K music and concert files into a properly named, organized archive through an eight-phase pipeline. We start with infrastructure and Docker environment, then build file discovery and ingestion, add companion file association and deduplication, stand up the parallel worker system, run audio analysis, generate AI-powered rename proposals, build the human-in-the-loop approval UI, and finally execute safe file operations with full audit trails. Each phase delivers a verifiable capability that the next phase depends on.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [ ] **Phase 1: Infrastructure & Project Setup** - Docker Compose environment, PostgreSQL, Alembic migrations, project skeleton
- [ ] **Phase 2: File Discovery & Ingestion** - Recursive scanning, SHA256 hashing, file classification, database persistence
- [ ] **Phase 3: Companion Files & Deduplication** - Associate companion files with media, detect and flag hash duplicates
- [ ] **Phase 4: Task Queue & Worker Infrastructure** - arq + Redis worker pool with bounded parallelism
- [ ] **Phase 5: Audio Analysis Pipeline** - BPM detection, mood/style classification via worker pool
- [ ] **Phase 6: AI Proposal Generation** - LLM-powered filename proposals stored as immutable records
- [ ] **Phase 7: Approval Workflow UI** - Web-based review interface for approving/rejecting proposals
- [ ] **Phase 8: Safe File Execution & Audit** - Copy-verify-delete file operations with append-only audit log

## Phase Details

### Phase 1: Infrastructure & Project Setup
**Goal**: A running Docker Compose environment with PostgreSQL, Redis, Alembic migrations, and a FastAPI skeleton that responds to health checks
**Depends on**: Nothing (first phase)
**Requirements**: INF-01, INF-03
**Success Criteria** (what must be TRUE):
  1. Running `docker compose up` starts API server, worker, PostgreSQL, and Redis containers without errors
  2. Alembic migrations apply cleanly to create the initial database schema (files, metadata, analysis, proposals, execution_log tables)
  3. FastAPI health endpoint returns 200 OK confirming database connectivity
  4. Project structure follows the async monolith pattern (separate router/service/worker layers)
  5. GitHub Actions CI pipeline runs code quality, tests, and security checks on every push/PR
**Plans**: 3 plans

Plans:
- [x] 01-01-PLAN.md — Project skeleton, tooling config, Docker infrastructure, justfile
- [ ] 01-02-PLAN.md — Application code (models, config, database, routers, Alembic), tests, README
- [ ] 01-03-PLAN.md — GitHub Actions CI workflows (code quality, tests, security) and Codecov config

### Phase 2: File Discovery & Ingestion
**Goal**: The system can scan a directory tree and populate PostgreSQL with every discovered file's hash, path, name, and type classification
**Depends on**: Phase 1
**Requirements**: ING-01, ING-02, ING-03, ING-05
**Success Criteria** (what must be TRUE):
  1. Pointing the system at a directory recursively discovers all music files (mp3, m4a, ogg), video files, and companion files (cue, nfo, txt, jpg, png, m3u, pls)
  2. Every discovered file has its SHA256 hash computed and stored in PostgreSQL
  3. Every discovered file has its original filename and original absolute path recorded in PostgreSQL
  4. Every file is classified as music, video, or companion and that classification is stored
  5. Paths containing Unicode characters (accented, CJK) are normalized to NFC and stored correctly
**Plans**: TBD

### Phase 3: Companion Files & Deduplication
**Goal**: Companion files are linked to their nearby media files and exact duplicates are flagged for review
**Depends on**: Phase 2
**Requirements**: ING-04, ING-06
**Success Criteria** (what must be TRUE):
  1. Companion files (cue, nfo, txt, jpg, etc.) are associated with music/video files in the same or parent directory
  2. Files sharing the same SHA256 hash are identified as exact duplicates and flagged in the database
  3. A user can query the database to see all duplicate groups and their file locations
**Plans**: TBD

### Phase 4: Task Queue & Worker Infrastructure
**Goal**: An arq + Redis task queue distributes work to a bounded worker pool with backpressure and resumability
**Depends on**: Phase 1
**Requirements**: INF-02, ANL-03
**Success Criteria** (what must be TRUE):
  1. arq workers connect to Redis and process enqueued tasks
  2. Multiple workers process tasks in parallel up to a configurable concurrency limit
  3. Failed tasks are retried with backoff and do not block the queue
  4. CPU-bound work (audio analysis) runs in a process pool without blocking the async event loop
**Plans**: 2 plans

Plans:
- [x] 04-01-PLAN.md -- arq dependency, worker config, WorkerSettings, task functions, process pool
- [ ] 04-02-PLAN.md -- Redis Docker, enqueue API, integration testing

### Phase 5: Audio Analysis Pipeline
**Goal**: Music files are analyzed for BPM, mood, and style using existing prototypes running through the worker pool
**Depends on**: Phase 2, Phase 4
**Requirements**: ANL-01, ANL-02
**Success Criteria** (what must be TRUE):
  1. BPM is detected for music files using librosa and stored in the analysis table
  2. Mood and style are classified for music files using existing prototype code and stored in the analysis table
  3. Analysis results are linked to their source file records in PostgreSQL
  4. Analysis runs through the arq worker pool and can process files in parallel
**Plans**: TBD

### Phase 6: AI Proposal Generation
**Goal**: The system uses an LLM to propose new filenames for files, storing proposals as immutable records
**Depends on**: Phase 5
**Requirements**: AIP-01, AIP-02
**Success Criteria** (what must be TRUE):
  1. The system sends file metadata, analysis results, and companion file content to an LLM and receives proposed filenames
  2. Each proposal is stored as an immutable record in PostgreSQL (not regenerated on the fly)
  3. Proposals include the original filename, proposed filename, and the metadata context used to generate them
  4. Batch prompting processes multiple files per LLM call for cost efficiency
**Plans**: TBD

### Phase 7: Approval Workflow UI
**Goal**: An admin can review all proposed renames in a web interface and approve or reject them
**Depends on**: Phase 6
**Requirements**: APR-01, APR-02, APR-03
**Success Criteria** (what must be TRUE):
  1. Admin can view a paginated list of all proposed renames in a web browser
  2. Admin can approve or reject individual proposals with a single click
  3. Admin can filter the proposal list by status (pending, approved, rejected)
  4. The UI updates without full page reloads (HTMX partial updates)
**Plans**: TBD
**UI hint**: yes

### Phase 8: Safe File Execution & Audit
**Goal**: Approved renames execute safely using copy-verify-delete with every operation logged to an append-only audit trail
**Depends on**: Phase 7
**Requirements**: EXE-01, EXE-02
**Success Criteria** (what must be TRUE):
  1. Executing an approved rename copies the file to the new path, verifies the SHA256 hash matches, then deletes the original
  2. Every file operation (copy, verify, delete) is logged to an append-only audit table in PostgreSQL before the operation executes
  3. If hash verification fails after copy, the operation aborts and the original file remains untouched
  4. Execution status is tracked per-file (pending, in-progress, completed, failed) in the database
**Plans**: TBD

## Progress

**Execution Order:**
Phases execute in numeric order: 1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7 -> 8
Note: Phases 2 and 4 can execute in parallel (both depend only on Phase 1). Phase 5 depends on both 2 and 4.

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Infrastructure & Project Setup | 0/3 | Planning | - |
| 2. File Discovery & Ingestion | 0/TBD | Not started | - |
| 3. Companion Files & Deduplication | 0/TBD | Not started | - |
| 4. Task Queue & Worker Infrastructure | 0/TBD | Not started | - |
| 5. Audio Analysis Pipeline | 0/TBD | Not started | - |
| 6. AI Proposal Generation | 0/TBD | Not started | - |
| 7. Approval Workflow UI | 0/TBD | Not started | - |
| 8. Safe File Execution & Audit | 0/TBD | Not started | - |
