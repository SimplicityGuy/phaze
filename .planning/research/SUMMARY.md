# Project Research Summary

**Project:** Phaze v3.0 — Cross-Service Intelligence & File Enrichment
**Domain:** Music collection organizer — post-organization enrichment layer
**Researched:** 2026-04-02
**Confidence:** HIGH

## Executive Summary

Phaze v3.0 adds four enrichment features on top of the fully operational v2.0 pipeline (200K-file ingestion, fingerprinting, tracklist matching, proposal workflow, and file execution). The v3.0 scope is: Discogs cross-service linking (connect TracklistTracks to discogsography's local Discogs database), tag writing (push corrected metadata from Postgres into destination file tags), CUE sheet generation (produce `.cue` companion files from tracklist timestamps), and a unified search page. All four features operate on already-executed files — this is enrichment, not pipeline extension.

The recommended approach leans heavily on the existing stack. Zero new Python dependencies are required: httpx, mutagen, rapidfuzz, SQLAlchemy, Alembic, arq/SAQ, HTMX/Jinja2, and PostgreSQL already cover everything. The architecture extends cleanly along established patterns — Protocol-based adapters for external services, SAQ tasks for batch operations, write-ahead audit logging for destructive operations, and HTMX partials for dynamic UI. The discogsography service (the user's separate project) is queried via HTTP on the private Docker network, avoiding Discogs API rate limits entirely.

The primary risks are concentrated in tag writing (file mutation without rollback) and Discogs matching (false positives on ambiguous track names). Both risks are well-understood and have established mitigations: write only to destination copies (never originals), verify-after-write with re-read, and store candidate matches rather than auto-committing the top result. CUE sheet generation and search have low risk profiles — one is string formatting with a well-specified format, the other is read-only PostgreSQL queries. The phase ordering (Search → Discogs → Tag Writing → CUE) reflects both dependency order and risk escalation: start with zero-risk read-only queries, end with the highest-risk file mutation feature.

## Key Findings

### Recommended Stack

v3.0 introduces no new pip/uv dependencies. All required capabilities are already in `pyproject.toml`. The four new infrastructure points are: (1) a `DISCOGSOGRAPHY_URL` environment variable to reach the discogsography service, (2) `CREATE EXTENSION IF NOT EXISTS pg_trgm` in a new Alembic migration for fuzzy search, (3) mutagen's write API (symmetric to the existing read API already in `services/metadata.py`), and (4) a custom CUE file generator — a 50-line string formatting function, no library needed.

**Core technologies (new usage in v3.0):**
- **httpx** (existing): Async HTTP client to call discogsography `/api/search` — same pattern as existing fingerprint service adapters
- **mutagen** (existing, write API): Write corrected tags to destination files across all five formats (MP3/ID3, M4A/MP4, OGG/Vorbis, FLAC, OPUS)
- **rapidfuzz** (existing): Fuzzy match artist+title against Discogs search results — same `token_set_ratio` pattern as `tracklist_matcher.py`
- **PostgreSQL pg_trgm** (extension, new): GIN trigram indexes on artist/title columns for fuzzy text search; `websearch_to_tsquery` for user input
- **Custom CUE writer**: 50-line function producing `MM:SS:FF` timestamps at 75fps (Red Book standard, not centiseconds)

**What NOT to use:**
- python3-discogs-client: bypasses discogsography's enriched local data, imposes 60 req/min rate limit
- Elasticsearch/Meilisearch: massive overhead for 200K records; PostgreSQL handles this trivially
- cuetools/CueParser (PyPI): adding a dependency for string formatting of 5 CUE keywords
- eyeD3: ID3-only, does not cover OGG/M4A/FLAC
- SQLAlchemy-Searchable: abandoned since 2021

### Expected Features

v3.0 research identified four feature areas with clear dependency ordering and a concrete prioritization matrix.

**Must have (table stakes):**
- Match TracklistTrack records to Discogs releases via artist+title fuzzy search against discogsography
- Store DiscogsLink records with confidence score and candidate set (not just the top result)
- Batch Discogs linking via SAQ job (10-30 HTTP calls per tracklist — must not block request path)
- "Find all sets containing track X" cross-query (DiscogsLink JOIN TracklistTrack JOIN Tracklist)
- Write corrected tags to EXECUTED destination files with per-field preview diff before writing
- Batch tag writing with audit log (before/after JSONB snapshots)
- Generate `.cue` files from tracklist timestamps with source preference (fingerprint > 1001tracklists)
- Bulk CUE generation for all approved tracklists
- Full-text search across artist, title, event, filename with BPM/date/genre filters
- Unified search page as first-class admin nav tab

**Should have (differentiators):**
- Tag source selection UI (choose between extracted metadata, LLM proposal, Discogs data per field)
- Dry-run validation before tag write (file exists, writable, mutagen can open it)
- Discogs REM comments in CUE sheets (label, year, release ID per track)
- CUE timestamp quality badge (fingerprint accuracy vs 1001tracklists approximation)
- Cross-entity drill-down from search results to existing detail pages
- Reverse Discogs lookup ("what tracks from my collection appear on this release?")

**Defer to v4+:**
- Direct Discogs API calls from phaze (use discogsography service always — no exceptions)
- Auto-write tags after execution (violates human-in-the-loop principle)
- Album art embedding (binary blob complexity, format-specific embedding)
- Natural language search queries (LLM-to-SQL, deferred to v4+ per PROJECT.md)
- Graph visualization of track relationships
- Audio splitting from CUE sheets

### Architecture Approach

v3.0 adds four new services, two new routers, three new SAQ task modules, two new models, and two Alembic migrations — all strictly additive. The existing architecture (single codebase, two runtime modes: API via uvicorn + worker via SAQ) is preserved without modification to core pipeline models or state machine. The FileState enum is explicitly NOT extended for tag writing, CUE generation, or Discogs linking — these are enrichment actions tracked via separate audit tables (TagWriteLog, DiscogsLink), not pipeline stages.

**Major new components:**
1. **DiscogsService** (`services/discogs.py`) — Protocol-based HTTP adapter to discogsography; fuzzy match with rapidfuzz; batch via SAQ; graceful degradation when service is unavailable
2. **TagWriterService** (`services/tag_writer.py`) — Format-specific mutagen writers; write-ahead audit log (TagWriteLog); verify-after-write re-read; hard EXECUTED-state guard
3. **CueGeneratorService** (`services/cue_generator.py`) — CUE string generation; timestamp resolution priority (fingerprint > 1001tracklists > position-based); UTF-8 BOM encoding; frame-rate conversion (75fps)
4. **SearchService** (`services/search.py`) — Dynamic SQLAlchemy query across FileRecord, FileMetadata, AnalysisResult, Tracklist, DiscogsLink; read-only; paginated
5. **DiscogsLink model** — Links TracklistTrack/FileMetadata to Discogs releases; stores candidates JSONB, confidence score, match_method enum, master_id
6. **TagWriteLog model** — Append-only audit trail following ExecutionLog pattern; tags_before + tags_after JSONB

**Patterns to follow (all established in v2.0):**
- Protocol-based external service adapters (follow FingerprintEngine pattern in `services/fingerprint.py`)
- Write-ahead audit logging for destructive ops (follow ExecutionLog pattern in `services/execution.py`)
- SAQ task for batch external calls (follow scan_live_set pattern with HTMX polling for progress)
- HTMX partials with HX-Request header check (all existing routers do this)
- Service layer session injection — no ORM model leaks into templates

### Critical Pitfalls

1. **Tag writing corrupts audio files** — Use format-specific mutagen writers (not generic `File()` interface for writes), write only to destination copies, always re-read after `save()` to verify, default to ID3v2.3 (`save(v2_version=3)`) for maximum MP3 player compatibility. Format-specific round-trip tests required for all 5 formats (mp3, m4a, ogg, opus, flac).

2. **False positive Discogs matches** — Never auto-link without human review. Store top 3-5 candidates with confidence scores; do not commit the top result immediately. Use field-specific discogsography search (`artist=`, `title=`) rather than free-text `q=`. Weighted scoring: artist similarity (0.6) + title similarity (0.4). Manual override UI is required.

3. **CUE timestamp frame rate error** — CUE `MM:SS:FF` uses 75fps (Red Book CD-DA standard), NOT centiseconds. Use a dedicated `CueTimestamp` value object with explicit `frames = int(fractional_seconds * 75)`. Validation must reject FF >= 75. Unit test: `seconds_to_cue_timestamp(61.5)` returns `"01:01:37"`, not `"01:01:50"`.

4. **Tag/database state divergence** — Write sequence must be: (1) write tags to file, (2) re-read from file with mutagen, (3) update database with re-read values, (4) record audit log. Never update database based on intended values. If file write fails, leave database unchanged.

5. **Search slow without pre-computed index** — PostgreSQL `ILIKE '%term%'` on 4+ JOINed tables is O(n) at 200K rows. Use `to_tsvector('simple', ...)` (NOT `'english'` — music metadata is not natural language; `'english'` config stems words and mangles artist names) with GIN index. Add `pg_trgm` GIN index for fuzzy/prefix matching. Index creation must be in the Alembic migration, not just the SQLAlchemy model.

6. **CUE encoding breaks non-ASCII names** — Write with `open(path, 'w', encoding='utf-8-sig')` for UTF-8 BOM. Add `REM ENCODING UTF-8` comment line. Test with accented artist names (Röyksopp, Amelie Lens, etc.).

7. **Discogsography unavailability blocks users** — Apply circuit breaker with graceful degradation: if discogsography is down, mark tracks as "linking pending" and retry via SAQ. Never block the UI waiting for the external service.

## Implications for Roadmap

Based on combined research, suggested phase structure is Phases 18-21 (continuing from v2.0's Phase 17):

### Phase 18: Search

**Rationale:** Zero new models, zero new infrastructure, zero external dependencies. Queries only existing tables. Provides immediate value, establishes search router/service patterns, and lays groundwork for "find all sets containing track X" that Phase 19 enriches. Starting here builds developer confidence and delivers a UI win with no risk.

**Delivers:** Unified search page across files, tracklists, tracks; BPM/date/genre/file-type filters; paginated entity-grouped results; new nav tab in base.html.

**Addresses:** Full-text search table stakes; filter features; cross-entity drill-down.

**Avoids:** Search performance pitfall — requires GIN indexes in Alembic migration and `'simple'` tsconfig (not `'english'`); pagination from the start (no full-table result returns).

**Research flag:** Standard patterns (skip research-phase). PostgreSQL FTS + pg_trgm is well-documented with established SQLAlchemy patterns.

### Phase 19: Discogs Cross-Service Linking

**Rationale:** Introduces the DiscogsLink model (the key enrichment table for v3.0) and the discogsography HTTP dependency. Building after search means the search page immediately benefits from Discogs data in results. DiscogsLink data (label, genre, year) then enriches Phase 20 tag writing.

**Delivers:** DiscogsLink table and migration, batch matching SAQ job, "find all sets containing track X" query endpoint, Discogs context in tracklist review UI, manual override UI for bad matches.

**Addresses:** Cross-service linking table stakes; batch matching job; candidate storage with confidence scoring.

**Avoids:** False positive linking pitfall — requires candidate storage (top 3-5, not single auto-commit), confidence scoring, manual override; discogsography unavailability pitfall — circuit breaker with "linking pending" fallback.

**Research flag:** Needs integration check during planning. Verify the discogsography `/api/search` response shape (field names, pagination structure, available query parameters) by inspecting `api/routers/search.py` in the discogsography codebase before writing the DiscogsographyAdapter client.

### Phase 20: Tag Writing

**Rationale:** Highest-risk feature (modifies file contents on disk). Building after Discogs linking means DiscogsLink data is available as an additional metadata source for tag enrichment (label, genre, year). Requires TagWriteLog audit table and format-specific writer implementations.

**Delivers:** Tag write service with format-specific mutagen writers, preview UI (current vs proposed diff), per-field checkboxes, batch tag writing, audit log with before/after snapshots, EXECUTED-state hard guard.

**Addresses:** Tag writing table stakes; per-field selection; batch write workflow; undo via audit log.

**Avoids:** File corruption pitfall — format-specific writers, verify-after-write re-read, write-only-to-destination rule; DB/file divergence pitfall — re-read from file before any DB update.

**Research flag:** Requires thorough format-specific testing during execution (not planning). Tag write round-trip tests for mp3 (ID3v2.3), m4a (MP4), ogg (Vorbis), opus (OggOpus), flac (FLAC) are mandatory before any integration test. This is not optional coverage.

### Phase 21: CUE Sheet Generation

**Rationale:** Simplest feature in isolation — string formatting with file write, no model changes. Benefits from Phase 19 Discogs data for REM comments and Phase 20 verified metadata for PERFORMER fields. Non-destructive (creates new files only, never modifies existing ones). Building last means full enrichment data is available to include.

**Delivers:** CUE file generator with timestamp source preference logic, MM:SS:FF conversion at 75fps, UTF-8 BOM encoding, "Generate CUE" button on tracklist detail page, bulk generation SAQ job.

**Addresses:** CUE sheet table stakes; bulk generation; timestamp quality indicators.

**Avoids:** Frame rate pitfall (75fps not centiseconds — validated by unit test); encoding pitfall (UTF-8 BOM, `utf-8-sig`); FILE path pitfall (relative paths not absolute in CUE FILE directives).

**Research flag:** Standard patterns (skip research-phase). CUE format specification is complete, stable, and well-documented.

### Phase Ordering Rationale

- Phase 18 (Search) has zero external dependencies — pure read over existing data. No reason to delay. Establishes search patterns the whole team uses.
- Phase 19 (Discogs) must come before Phase 20 (Tag Write) because DiscogsLink data is a metadata source for tag enrichment. Also produces the "find sets by track" feature that search (Phase 18) then surfaces.
- Phase 20 (Tag Write) is highest-risk; placing it third ensures Discogs data is available to enrich tags and search can help find files needing updates.
- Phase 21 (CUE) is non-destructive and can technically be built any time — placing it last ensures Discogs REM metadata is available and the full enrichment picture is in place.
- The FileState machine is intentionally NOT extended — enrichment actions are tracked via TagWriteLog and DiscogsLink tables, not pipeline states.

### Research Flags

Phases needing deeper research during planning:
- **Phase 19 (Discogs):** Inspect `api/routers/search.py` in the discogsography codebase to verify the exact response shape for `/api/search` before writing the DiscogsographyAdapter. Response field names, pagination structure, and available query parameters must be confirmed.

Phases with standard patterns (skip research-phase):
- **Phase 18 (Search):** PostgreSQL FTS + pg_trgm is well-documented. Established SQLAlchemy `func.to_tsvector` / `func.similarity` patterns confirmed in multiple sources.
- **Phase 20 (Tag Write):** mutagen write API is symmetric to the read API already in production in `services/metadata.py`. Write patterns are well-documented.
- **Phase 21 (CUE):** CUE format is a stable text specification using 5 keywords. Implementation is pure string formatting + file write.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | All technologies already in production in this codebase. Zero new dependencies. Stack research confirmed no gaps. |
| Features | HIGH | Four feature areas well-defined with clear table stakes / differentiator / anti-feature distinction. Dependency graph between features is explicit and acyclic. |
| Architecture | HIGH | v3.0 maps cleanly onto existing patterns. Protocol adapters, SAQ tasks, HTMX partials, write-ahead logging all have production examples in the v2.0 codebase. |
| Pitfalls | HIGH | 7 critical pitfalls identified with specific prevention strategies and phase assignments. Recovery strategies documented for all major risks. |

**Overall confidence:** HIGH

### Gaps to Address

- **Discogsography API contract:** The `/api/search` endpoint existence is confirmed (router file inspected), but the exact response schema (field names, pagination envelope, available filter parameters) should be verified against the live service before writing the DiscogsographyAdapter in Phase 19. Mitigation: inspect `api/routers/search.py` in discogsography and run a test query before Phase 19 kickoff.

- **Search index strategy with real data:** Research recommends GIN indexes on individual columns plus a combined tsvector. The optimal approach (per-column GIN vs materialized view with combined tsvector) should be validated with `EXPLAIN ANALYZE` on actual production data during Phase 18 implementation. Profile before committing to materialized view overhead.

- **mutagen ID3v2.3 vs v2.4 user preference:** Research recommends defaulting to ID3v2.3 (`save(v2_version=3)`) for maximum player compatibility. Consider adding a `tag_id3_version: int = 3` config setting in Phase 20 to make this user-configurable without code changes.

## Sources

### Primary (HIGH confidence)
- PostgreSQL `pg_trgm` documentation — trigram similarity, GIN index operators, `similarity()` function
- PostgreSQL Full Text Search documentation — tsvector, tsquery, ts_rank, websearch_to_tsquery, `'simple'` vs `'english'` text search configs
- mutagen documentation — tag write API, format-specific containers, ID3v2.3/v2.4 version control
- CUE Sheet Format Specification (wyday.com) — canonical reference: INDEX MM:SS:FF at 75fps Red Book standard
- Discogsography source code (`api/routers/search.py`) — `/api/search` endpoint verified as existing
- Existing phaze codebase — v2.0 production patterns: FingerprintEngine Protocol, ExecutionLog write-ahead, SAQ scan_live_set, HTMX partials, `tracklist_matcher.py` rapidfuzz scoring

### Secondary (MEDIUM confidence)
- SQLAlchemy pg_trgm discussion (GitHub) — `func.similarity()` integration patterns
- PostgreSQL FTS for large datasets (case study) — tsvector + GIN optimization at scale
- mutagen ID3 GitHub issues — v2.3 compatibility, Latin-1 encoding gotchas with ID3v1

### Tertiary (references for context)
- Discogs API documentation — rate limits (60 req/min authenticated, IP-based), field-specific search behavior
- CUE encoding compatibility issues (XLD, DeaDBeeF player reports) — UTF-8 BOM as best compromise for player compatibility
- CUE sheet Wikipedia — format overview, MSF timestamp origins in CD-DA sector format

---
*Research completed: 2026-04-02*
*Ready for roadmap: yes*
