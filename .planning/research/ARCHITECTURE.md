# Architecture Patterns

**Domain:** Music collection organizer -- v3.0 cross-service intelligence & file enrichment
**Researched:** 2026-04-02
**Focus:** How Discogs linking, tag writing, CUE sheets, and search integrate with existing architecture

## Existing Architecture (v2.0 Baseline)

```
Containers:  api (FastAPI), worker (SAQ), postgres (PG18), redis (Redis 8), audfprint, panako
Models:      FileRecord, FileMetadata, AnalysisResult, RenameProposal, ExecutionLog,
             FileCompanion, ScanBatch, FingerprintResult, Tracklist, TracklistVersion, TracklistTrack
State machine: DISCOVERED -> METADATA_EXTRACTED -> FINGERPRINTED -> ANALYZED ->
               PROPOSAL_GENERATED -> APPROVED -> DUPLICATE_RESOLVED -> EXECUTED
Routers:     health, scan, companion, proposals, execution, preview, duplicates, tracklists, pipeline
Services:    pipeline, dedup, collision, companion, metadata (read), analysis, execution,
             fingerprint (Protocol adapters), tracklist_scraper, tracklist_matcher,
             proposal, proposal_queries, execution_queries, ingestion
Tasks:       process_file, generate_proposals, execute_approved_batch, extract_file_metadata,
             fingerprint_file, search_tracklist, scrape_and_store_tracklist, scan_live_set
             + refresh_tracklists cron (monthly)
Volumes:     /data/music (ro), /data/output (rw), /models (ro), audfprint_data, panako_data
```

Key architectural properties v3.0 must preserve:
- Single Python codebase, two runtime modes (API via uvicorn + worker via SAQ)
- SAQ tasks are thin wrappers calling service-layer functions
- Protocol-based adapters for external HTTP services (FingerprintEngine)
- httpx.AsyncClient for inter-service communication
- Write-ahead audit logging for destructive file operations
- HTMX partials for dynamic UI (check HX-Request header)
- Worker context pattern: startup/shutdown hooks initialize shared resources
- FileState machine guards idempotent pipeline progression

## v3.0 System Diagram

```
+--------------------------------------------------------------------------------+
|                       Admin Web UI (HTMX + Jinja2)                             |
|  +------------+ +----------+ +----------+ +-----------+ +--------+ +--------+  |
|  | Proposals  | | Dupes    | | Track-   | | Pipeline  | | Search | | Discogs|  |
|  | (existing) | | (exists) | | lists    | | (exists)  | | (NEW)  | | (NEW)  |  |
|  +-----+------+ +----+-----+ | (exists) | +-----+-----+ +---+----+ +---+----+  |
+--------+-------------+-------+----+------+-------+-----------+---------+-------+
|                       API Layer (FastAPI) -- 11 routers                         |
|  +----------+ +----------+ +---------+ +--------+ +----------+ +----------+    |
|  | existing | | existing | | track-  | | pipe-  | | search   | | discogs  |    |
|  | 7 rtrs   | | tracklst | | lists   | | line   | | (NEW)    | | (NEW)    |    |
|  +----+-----+ +----+-----+ +----+----+ +---+----+ +----+-----+ +----+-----+    |
+-------+-----------+--------------+----------+-----------+-----------+-----------+
|                      Service Layer                                              |
|  +---------------+ +--------------+ +---------------+ +---------------------+   |
|  | existing      | | discogs      | | tag_writer    | | cue_generator       |   |
|  | 14 services   | | (NEW)        | | (NEW)         | | (NEW)               |   |
|  +-------+-------+ +------+-------+ +------+--------+ +----------+----------+   |
|          |                |                |                      |              |
|  +-------+-------+ +-----+--------+ +-----+--------+                           |
|  | search         | | (existing   | | (existing    |                           |
|  | (NEW)          | | services)   | | services)    |                           |
|  +----------------+ +-------------+ +--------------+                           |
+-----+-------------------+-------------------+----------------------------------+
|                      Task Queue (SAQ + Redis)                                   |
|  +------------------------------------------------------------------------+     |
|  |  NEW tasks: link_discogs_batch | write_tags_batch | generate_cue       |     |
|  |  existing: process_file | generate_proposals | execute_approved_batch  |     |
|  |            extract_file_metadata | fingerprint_file | scan_live_set    |     |
|  |            search_tracklist | scrape_and_store_tracklist               |     |
|  |  cron: refresh_tracklists (monthly, existing)                         |     |
|  +------------------------------------------------------------------------+     |
+-----+-------------------+-------------------+----------------------------------+
|                      Data Layer                                                 |
|  +--------------+  +-----------+  +-----------+                                |
|  | PostgreSQL   |  |   Redis   |  | Filesystem |                                |
|  | +2 NEW tbls  |  | (broker)  |  | (dest rw)  |                                |
|  +--------------+  +-----------+  +-----------+                                |
+-------+---------------------------+--------------------------------------------+
        |                           |
+-------+------+   +---------------+---------------+
| discogsography|   | audfprint    |    panako     |
| (EXTERNAL    |   | (existing)   |  (existing)   |
|  HTTP API)   |   +--------------+---------------+
+--------------+
```

## New Component Map

| Component | Type | New/Modified | Integrates With |
|-----------|------|--------------|-----------------|
| `services/discogs.py` | Service | **NEW** | Discogsography HTTP API, TracklistTrack, FileMetadata |
| `models/discogs_link.py` | Model | **NEW** | TracklistTrack, FileMetadata |
| `services/tag_writer.py` | Service | **NEW** | mutagen, FileRecord, FileMetadata, RenameProposal, DiscogsLink |
| `models/tag_write_log.py` | Model | **NEW** | FileRecord (audit trail) |
| `services/cue_generator.py` | Service | **NEW** | Tracklist, TracklistTrack, FileRecord |
| `services/search.py` | Service | **NEW** | FileRecord, FileMetadata, AnalysisResult, Tracklist, DiscogsLink |
| `routers/search.py` | Router | **NEW** | SearchService, HTMX templates |
| `routers/discogs.py` | Router | **NEW** | DiscogsService, TagWriterService, HTMX templates |
| `tasks/discogs.py` | Tasks | **NEW** | DiscogsService (batch linking) |
| `tasks/tag_write.py` | Tasks | **NEW** | TagWriterService |
| `tasks/cue.py` | Tasks | **NEW** | CueGeneratorService |
| `templates/search/` | Templates | **NEW** | Search page + partials |
| `templates/discogs/` | Templates | **NEW** | Discogs linking UI + partials |
| `config.py` | Config | MODIFIED | Add discogsography_url, tag write settings |
| `main.py` | App | MODIFIED | Register search + discogs routers |
| `tasks/worker.py` | Worker | MODIFIED | Register new tasks, init discogs client in startup |
| `models/__init__.py` | Models | MODIFIED | Export DiscogsLink, TagWriteLog |
| `templates/base.html` | Template | MODIFIED | Add Search nav tab |
| `routers/tracklists.py` | Router | MODIFIED | Add "Generate CUE" + "Link to Discogs" buttons |

### Component Boundaries

| Component | Responsibility | Communicates With |
|-----------|---------------|-------------------|
| DiscogsService | HTTP client to discogsography API; fuzzy match track artist+title to Discogs releases | Discogsography external API (HTTP), PostgreSQL (DiscogsLink model) |
| TagWriterService | Write corrected tags to DESTINATION copies using mutagen; preview diffs | Filesystem (destination files only, never originals), PostgreSQL (TagWriteLog, FileMetadata) |
| CueGeneratorService | Generate .cue files from tracklist data with resolved timestamps | Filesystem (write .cue next to destination), PostgreSQL (Tracklist, TracklistTrack, FileRecord) |
| SearchService | Query layer across FileRecord, FileMetadata, AnalysisResult, Tracklist, DiscogsLink | PostgreSQL only (read-only queries) |

## Data Flow: Feature by Feature

### 1. Discogs Linking Flow

```
TracklistTrack (artist + title)
        |
        v
DiscogsService.match_track(artist, title)
        |
        v
HTTP GET discogsography/api/search?artist=X&title=Y
        |
        v
DiscogsLink record created (track_id -> discogs_release_id, discogs_master_id)
        |
        v
UI shows Discogs release info on tracklist detail page
        |
        v
Cross-query enabled: "find all sets containing track X" via DiscogsLink JOIN TracklistTrack
```

**Key decision: Store links in phaze's DB.** DiscogsLink is a join table in phaze's PostgreSQL, NOT in discogsography's DB. Phaze owns the relationship. Discogsography is a read-only lookup service -- phaze sends search queries, gets release data back, stores the link locally.

**Key decision: Batch linking via SAQ tasks.** A tracklist has 10-30 tracks. Linking all tracks means 10-30 HTTP calls to discogsography. Do not block the request path. Enqueue a `link_discogs_batch` SAQ task, show progress via HTMX polling (same pattern as fingerprint scan progress in `tracklists.py`).

**Key decision: Use rapidfuzz for client-side confidence scoring.** Discogsography may return multiple candidate releases. Compute a confidence score using rapidfuzz token_set_ratio on artist+title, store the best match. Allow manual override via UI.

### 2. Tag Writing Flow

```
FileRecord (state=EXECUTED, current_path = destination copy)
        |
        v
User clicks "Write Tags" on file detail / bulk action in UI
        |
        v
TagWriterService.preview_tags(file_id)
  -> Returns diff: current tags vs proposed tags
  -> Proposed tags aggregated from: FileMetadata + RenameProposal + DiscogsLink
        |
        v
User reviews diff, confirms write
        |
        v
SAQ task: write_tags enqueued
        |
        v
TagWriterService.write_tags(file_id)
  1. Snapshot current tags -> tags_before (TagWriteLog, write-ahead)
  2. Build corrected tag dict:
     - artist, title from RenameProposal.proposed_filename (parsed)
     - album, year, genre, label from DiscogsLink (if linked)
     - track_number from FileMetadata (if present)
  3. Write tags to destination file via mutagen (current_path)
  4. Snapshot written tags -> tags_after
  5. Update TagWriteLog status to "completed"
  6. Update FileMetadata.raw_tags with new values
```

**Key decision: Tag writing does NOT change FileRecord.state.** It is an enrichment action on already-executed files, not a pipeline stage. The FileState machine represents the core pipeline (DISCOVERED through EXECUTED). Tag writing can happen multiple times on the same file. Track it via TagWriteLog instead.

**Key decision: NEVER modify original files.** Tag writing operates ONLY on destination copies (files at `current_path` after EXECUTED state). This preserves the copy-verify-delete safety model. If tag writing corrupts a file, the original at `original_path` is still intact.

**Key decision: Write-ahead audit logging.** Follow the existing ExecutionLog pattern from `services/execution.py`: log the operation with `tags_before` snapshot BEFORE executing the write, then update status after. This enables rollback if needed.

### 3. CUE Sheet Generation Flow

```
Tracklist (file_id linked, status=approved)
  with TracklistVersion -> TracklistTracks (with timestamps)
        |
        v
User clicks "Generate CUE" on tracklist detail page
        |
        v
CueGeneratorService.generate(tracklist_id)
  1. Load latest TracklistVersion with tracks
  2. Resolve timestamps:
     Priority: fingerprint tracklist timestamps (TracklistTrack.confidence > 0)
       -> fall back to 1001tracklists timestamps (TracklistTrack.timestamp field)
       -> fall back to position-based even spacing
  3. Look up FileRecord.current_path for the linked file
  4. Format CUE sheet:
     - PERFORMER from Tracklist.artist
     - TITLE from Tracklist event + date
     - FILE from destination filename
     - TRACK entries with INDEX 01 timestamps (MM:SS:FF format)
  5. Write .cue file adjacent to destination file (same base name)
  6. Return path for UI confirmation
```

**Key decision: CUE files placed next to destination files.** For `Artist - Live @ Event 2024.01.01.mp3`, the CUE is `Artist - Live @ Event 2024.01.01.cue`. This follows standard CUE sheet conventions and media player expectations.

**Key decision: Timestamp resolution priority.** Fingerprint-sourced timestamps are more accurate than 1001tracklists timestamps (which are often approximate). If a file has both a fingerprint-sourced tracklist and a 1001tracklists tracklist, prefer the fingerprint one's timestamps.

**Key decision: CUE generation is non-destructive.** It only creates new files, never modifies existing ones. No audit log needed (unlike tag writing). If the .cue already exists, prompt the user before overwriting.

### 4. Search Flow

```
User navigates to /search page
        |
        v
HTMX form with filter fields:
  artist, title, event, date_from, date_to, bpm_min, bpm_max,
  genre, file_type, state, has_tracklist, has_discogs_link
        |
        v
HTMX POST /search -> SearchService.search(filters)
  - Dynamic SQLAlchemy query:
    SELECT files.* FROM files
    JOIN metadata ON metadata.file_id = files.id
    LEFT JOIN analysis ON analysis.file_id = files.id
    LEFT JOIN tracklists ON tracklists.file_id = files.id
    LEFT JOIN discogs_links ON discogs_links.file_metadata_id = metadata.id
    WHERE [dynamic filter clauses]
    ORDER BY [sortable columns]
    LIMIT page_size OFFSET offset
  - Returns paginated SearchResult objects
        |
        v
HTMX partial renders result cards with file info, metadata, analysis data
```

**Key decision: PostgreSQL only, no search engine.** At 200K records with proper indexes, PostgreSQL handles ILIKE and filtered queries in milliseconds. Adding Elasticsearch or Meilisearch would mean another Docker container, index synchronization, and consistency concerns for zero benefit at this scale. If text search becomes slow later, add `pg_trgm` extension with GIN indexes.

**Key decision: Search is read-only.** No mutations. The SearchService is a pure query layer. This means it can use the API session directly (no task queue needed).

## New Models

### DiscogsLink

```python
class DiscogsLink(TimestampMixin, Base):
    """Links a tracklist track or file to a Discogs release."""

    __tablename__ = "discogs_links"

    id: Mapped[uuid.UUID]             # PK
    track_id: Mapped[uuid.UUID | None]       # FK -> tracklist_tracks.id
    file_metadata_id: Mapped[uuid.UUID | None]  # FK -> metadata.id (for standalone file matches)
    discogs_release_id: Mapped[int]   # Discogs release ID (integer from their API)
    discogs_master_id: Mapped[int | None]  # Discogs master release ID
    artist: Mapped[str]               # Matched artist name (from Discogs)
    title: Mapped[str]                # Matched track title (from Discogs)
    label: Mapped[str | None]         # Record label from Discogs
    year: Mapped[int | None]          # Release year from Discogs
    genre: Mapped[str | None]         # Genre from Discogs
    match_confidence: Mapped[float]   # Fuzzy match score (0-100)
    match_method: Mapped[str]         # "fuzzy", "exact", "manual"
```

Indexes: `(track_id, discogs_release_id)` unique composite for dedup, `discogs_release_id` for reverse lookups ("find all tracks from this release"), `file_metadata_id` for standalone file lookups.

### TagWriteLog

```python
class TagWriteLog(TimestampMixin, Base):
    """Append-only audit log for tag write operations."""

    __tablename__ = "tag_write_log"

    id: Mapped[uuid.UUID]             # PK
    file_id: Mapped[uuid.UUID]        # FK -> files.id
    tags_before: Mapped[dict]         # JSONB snapshot before write
    tags_after: Mapped[dict]          # JSONB snapshot after write
    status: Mapped[str]               # "completed", "failed"
    error_message: Mapped[str | None]
    file_path: Mapped[str]            # Path where tags were written (audit)
```

Follows the ExecutionLog pattern -- append-only, write-ahead, enables tag restoration from `tags_before` snapshot.

## Patterns to Follow

### Pattern 1: Protocol-Based External Service Adapter

Follow the existing `FingerprintEngine` Protocol pattern for the Discogs HTTP client.

```python
@runtime_checkable
class DiscogsSearchProvider(Protocol):
    async def search_release(self, artist: str, title: str) -> list[DiscogsSearchResult]: ...
    async def get_release(self, release_id: int) -> DiscogsRelease | None: ...
    async def health(self) -> bool: ...

class DiscogsographyAdapter:
    """HTTP client adapter for the discogsography service."""

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def search_release(self, artist: str, title: str) -> list[DiscogsSearchResult]:
        resp = await self._client.get("/api/search", params={"artist": artist, "title": title})
        ...
```

Testable via Protocol mock, consistent with existing adapter pattern, swappable if discogsography API changes.

### Pattern 2: Write-Ahead Audit Logging for Destructive Operations

Tag writing modifies file contents. Follow the `execution.py` pattern exactly:

```python
# 1. Log BEFORE executing
log_entry = TagWriteLog(file_id=file_id, tags_before=current_tags, status="in_progress")
session.add(log_entry)
await session.commit()

# 2. Execute the write
write_tags_to_file(file_path, new_tags)

# 3. Update log entry
log_entry.tags_after = read_tags_from_file(file_path)
log_entry.status = "completed"
await session.commit()
```

### Pattern 3: SAQ Task for Batch External Calls

Discogs linking for a tracklist (10-30 HTTP calls) must not block the request path. Follow the scan_live_set pattern:

```python
# Router: enqueue task, return progress partial
job = await queue.enqueue("link_discogs_batch", tracklist_id=str(tracklist_id))
return TemplateResponse("discogs/partials/link_progress.html", ...)

# Task: iterate tracks, call service
async def link_discogs_batch(ctx, tracklist_id: str):
    discogs_service = ctx["discogs_service"]
    session_factory = ctx["async_session"]
    async with session_factory() as session:
        tracks = await get_tracks_for_tracklist(session, tracklist_id)
        for track in tracks:
            result = await discogs_service.search_release(track.artist, track.title)
            # store DiscogsLink...
```

### Pattern 4: HTMX Partial Responses

Check `HX-Request` header, return full page or partial. Every existing router does this.

```python
if request.headers.get("HX-Request") == "true":
    return templates.TemplateResponse(request=request, name="search/partials/results.html", context=ctx)
return templates.TemplateResponse(request=request, name="search/page.html", context=ctx)
```

### Pattern 5: Service Layer Session Injection

Services receive AsyncSession, perform queries, return domain objects. Routers handle HTTP/HTMX concerns. No ORM model leaks into templates -- convert to dicts or Pydantic schemas.

## Anti-Patterns to Avoid

### Anti-Pattern 1: Modifying Original Files

**What:** Writing tags to files at `original_path` before or instead of destination copies.
**Why bad:** Violates copy-verify-delete safety model. Original files are the source of truth for the irreplaceable collection.
**Instead:** Only write tags to files at `current_path` after FileRecord reaches EXECUTED state. Guard with state check.

### Anti-Pattern 2: Adding FileState for Non-Pipeline Actions

**What:** Adding TAG_WRITTEN, CUE_GENERATED, or DISCOGS_LINKED to the FileState enum.
**Why bad:** The FileState machine represents the linear pipeline (DISCOVERED through EXECUTED). Tag writing, CUE generation, and Discogs linking are enrichment actions that can happen multiple times, in any order, after execution. Adding states would break the pipeline dashboard stage counts, the state transition guards, and the processing progression logic.
**Instead:** Track via separate audit/log tables (TagWriteLog, DiscogsLink). Use computed properties or boolean flags if the UI needs status indicators.

### Anti-Pattern 3: Calling Discogsography Synchronously in Request Path

**What:** Making HTTP calls to discogsography in the API router while the user waits.
**Why bad:** Linking an entire tracklist (10-30 tracks) means 10-30 sequential HTTP calls. Network failures would cause 500 errors. User sees a spinner for 30+ seconds.
**Instead:** Enqueue batch linking as SAQ task. Show progress via HTMX polling (same pattern as fingerprint scan progress in `routers/tracklists.py`).

### Anti-Pattern 4: Deploying a Search Engine for 200K Records

**What:** Adding Elasticsearch, Meilisearch, or Typesense to the Docker Compose stack.
**Why bad:** PostgreSQL handles 200K records with indexed queries trivially (sub-millisecond for B-tree, low milliseconds for ILIKE with pg_trgm). A search engine adds container overhead, index sync complexity, and consistency concerns for zero benefit.
**Instead:** PostgreSQL with proper indexes. Add `pg_trgm` GIN indexes later if ILIKE performance degrades.

### Anti-Pattern 5: Shared Mutable State Between Tag Writer and Execution Service

**What:** Tag writer reading `original_path` to determine what tags to write, or modifying FileRecord.state.
**Why bad:** Creates coupling between the execution pipeline and the enrichment layer. Race conditions if a file is being re-executed while tags are being written.
**Instead:** Tag writer reads `current_path` (post-execution destination), never touches FileRecord.state. Completely decoupled from pipeline progression.

## Integration Points: New vs Modified Files

### New Files (~1,100 lines estimated)

| File | Est. Lines | Depends On |
|------|-----------|------------|
| `models/discogs_link.py` | ~40 | Base, TimestampMixin |
| `models/tag_write_log.py` | ~30 | Base, TimestampMixin |
| `services/discogs.py` | ~150 | httpx, DiscogsLink model, rapidfuzz |
| `services/tag_writer.py` | ~130 | mutagen, FileRecord, FileMetadata, TagWriteLog, DiscogsLink |
| `services/cue_generator.py` | ~100 | Tracklist, TracklistTrack, FileRecord |
| `services/search.py` | ~120 | FileRecord, FileMetadata, AnalysisResult, Tracklist, DiscogsLink |
| `routers/search.py` | ~90 | SearchService, Jinja2Templates |
| `routers/discogs.py` | ~130 | DiscogsService, TagWriterService, Jinja2Templates |
| `tasks/discogs.py` | ~50 | DiscogsService, async_session |
| `tasks/tag_write.py` | ~50 | TagWriterService, async_session |
| `tasks/cue.py` | ~40 | CueGeneratorService, async_session |
| `templates/search/` | ~150 | HTMX partials (page, results, filters) |
| `templates/discogs/` | ~120 | HTMX partials (link progress, release card, tag preview) |
| Alembic migration (discogs_links) | ~40 | DiscogsLink model |
| Alembic migration (tag_write_log) | ~30 | TagWriteLog model |

### Modified Files (low risk, additive changes)

| File | Change | Risk |
|------|--------|------|
| `config.py` | Add `discogsography_url: str`, `tag_write_enabled: bool` | Low |
| `main.py` | Register search + discogs routers (2 lines) | Low |
| `tasks/worker.py` | Add discogs adapter to startup, register 3 new task functions | Low |
| `models/__init__.py` | Export DiscogsLink, TagWriteLog (2 imports + 2 __all__ entries) | Low |
| `templates/base.html` | Add "Search" nav tab + "Discogs" nav tab | Low |
| `routers/tracklists.py` | Add "Generate CUE" and "Link to Discogs" action buttons on tracklist detail | Medium |
| `templates/tracklists/` | Add CUE/Discogs buttons to existing tracklist card partials | Medium |

## Suggested Build Order

Order based on dependency analysis, risk assessment, and value delivery:

### Phase 18: Search

**Why first:** Zero new models, zero new infrastructure, zero external dependencies. Queries existing tables only. Provides immediate value by making the 200K-file dataset queryable. Establishes the search router + service pattern. The search page is the foundation for "find all sets containing track X" which later uses DiscogsLink data.

**Scope:** New router, service, templates. One Alembic migration for any new indexes (pg_trgm if needed). No model changes.

**Risk:** Low. Read-only queries over existing data.

### Phase 19: Discogs Linking

**Why second:** Requires DiscogsLink model + Alembic migration + external HTTP dependency (discogsography). Should be built after search because "find all sets containing track X" is a search feature enriched by DiscogsLink data. Discogs data also enriches tag writing (genre, label, year from Discogs releases).

**Scope:** New model, service, router, tasks, templates. Alembic migration. DiscogsographyAdapter following Protocol pattern. Batch linking via SAQ.

**Risk:** Medium. External HTTP dependency on discogsography. Needs discogsography API contract to be stable. Fuzzy matching confidence thresholds need tuning.

### Phase 20: Tag Writing

**Why third:** Highest-risk feature (modifies file contents). Benefits from Discogs data being available (enriched tags with label, genre, year from DiscogsLink). Requires TagWriteLog model for audit trail.

**Scope:** New model, service, tasks, templates added to discogs router. Alembic migration. mutagen write operations (library already imported for read). Write-ahead audit logging.

**Risk:** High. File content mutation. Requires thorough testing with all tag formats (ID3, Vorbis, MP4, FLAC, OPUS). Must verify tag writes do not corrupt files.

### Phase 21: CUE Sheet Generation

**Why last:** Simplest feature in isolation. Only creates new files, never modifies existing ones. Depends on approved tracklists with timestamps (already exist). Building last means Discogs-enriched track metadata (from Phase 19) can be included in CUE sheet comments (PERFORMER, SONGWRITER fields).

**Scope:** New service, task, templates added to tracklists router. CUE format string generation + file write. No model changes needed.

**Risk:** Low. Non-destructive (creates files, never modifies). CUE format is a simple text standard.

### Phase Ordering Rationale

```
Phase 18 (Search)    -- no dependencies, immediate value, establishes patterns
    |
Phase 19 (Discogs)   -- new model, enriches search + tag writing
    |
Phase 20 (Tag Write) -- benefits from Discogs data, highest risk = needs most context
    |
Phase 21 (CUE)       -- simplest, benefits from all prior enrichments
```

Dependencies:
- Search has no data dependencies on other v3.0 features
- Discogs linking enriches search results (show Discogs info on search cards)
- Tag writing aggregates data from FileMetadata + RenameProposal + DiscogsLink
- CUE generation benefits from Discogs-enriched track metadata

## Scalability Considerations

| Concern | At 200K files | At 500K files | Notes |
|---------|---------------|---------------|-------|
| Search query speed | Milliseconds with B-tree indexes | Add pg_trgm GIN index if ILIKE slows | Monitor EXPLAIN ANALYZE |
| Discogs API rate | ~30 tracks/tracklist, rate limited by discogsography | Same | Phaze does not call Discogs directly |
| Tag write throughput | Sequential per-file, I/O bound | Batch with SAQ concurrency (worker_max_jobs=8) | Already parallelized via task queue |
| CUE generation | Instant (string formatting + file write) | Same | Zero scaling concerns |
| DiscogsLink table size | ~50K rows (5K tracklists x 10 tracks avg) | ~125K rows | Standard B-tree indexes sufficient |

## Configuration Additions

```python
class Settings(BaseSettings):
    # ... existing settings ...

    # Discogsography service
    discogsography_url: str = "http://discogsography:8000"
    discogsography_timeout: float = 30.0

    # Tag writing
    tag_write_enabled: bool = True  # Safety toggle
```

No new Docker containers needed. Discogsography is already running as a separate service on the home network. Phaze just needs its URL configured.

## Sources

- Existing codebase analysis: 11 models, 9 routers, 14+ service files, 8 task functions, 9 Alembic migrations
- Established patterns: FingerprintEngine Protocol (services/fingerprint.py), write-ahead ExecutionLog (services/execution.py), HTMX partial rendering (routers/tracklists.py), SAQ task enqueue + poll (routers/tracklists.py scan endpoints)
- mutagen tag writing: same library already used for read (services/metadata.py), write API is symmetric (mutagen.File.save())
- CUE sheet format: standard text format (PERFORMER, TITLE, FILE, TRACK, INDEX directives), no external dependencies
- PostgreSQL performance: well within single-node capacity for 200K-record indexed queries
- rapidfuzz: already in use (services/tracklist_matcher.py) for fuzzy matching

---
*Architecture patterns for: Phaze v3.0 -- Cross-Service Intelligence & File Enrichment*
*Researched: 2026-04-02*
