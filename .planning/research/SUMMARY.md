# Project Research Summary

**Project:** Phaze v2.0
**Domain:** Music collection enrichment — audio metadata, fingerprinting, tracklist scraping, duplicate resolution
**Researched:** 2026-03-30
**Confidence:** MEDIUM

## Executive Summary

Phaze v2.0 extends a completed v1.0 music organization system (FastAPI + arq + PostgreSQL + HTMX) with four new capability areas: audio tag extraction via mutagen, AI-generated destination path proposals, duplicate resolution UI, audio fingerprinting with an audfprint/Panako hybrid, and 1001Tracklists scraping. The existing architecture is well-suited to absorb these features with minimal disruption — the v1.0 state machine already declares `METADATA_EXTRACTED` and `FINGERPRINTED` states; the duplicate detection backend exists but has no UI; `RenameProposal.proposed_path` is wired but always NULL. v2.0 completes these unfinished threads while adding two genuinely new capabilities: fingerprint-based live set identification and external tracklist scraping.

The recommended approach builds incrementally from least to most complex. Start with tag extraction (low complexity, unblocks everything else), then surface the dedup UI (backend exists, only frontend work needed), then integrate 1001Tracklists scraping (adds one new Docker dependency), then deploy the fingerprint service container (highest technical risk and longest calendar time). This ordering respects data dependencies — extracted tags feed LLM path proposals, 1001Tracklists search queries, and duplicate quality scoring — while deferring the two highest-risk items (fingerprinting and live set matching) until foundational work is stable and the library is de-duplicated.

The primary risks are: (1) the audfprint library dates from 2015 and must be vendored and modernized, with Python 3.13 compatibility unverified (MEDIUM confidence); (2) Panako's Javalin HTTP wrapper is custom work with no existing examples (LOW confidence); (3) live concert recordings — the majority of the Phaze corpus — are the hardest input for fingerprinting algorithms, with published research showing 60-70% of live music goes unidentified by standard landmark-based algorithms. Fingerprint match quality on live sets must be treated as a probabilistic aid requiring human review, not an authoritative identification. All three risks are manageable with the mitigations in PITFALLS.md but require staged validation and realistic expectations.

## Key Findings

### Recommended Stack

The v1.0 stack (FastAPI, SQLAlchemy 2.x + asyncpg, arq/Redis, litellm, HTMX/Jinja2/Tailwind, Alembic, Docker Compose) is unchanged and fully validated. v2.0 adds only targeted new dependencies: **mutagen** (audio tag read/write), **beautifulsoup4 + lxml** (HTML scraping), **httpx** promoted from dev to production (scraping + fingerprint service HTTP calls), **joblib + scipy** (audfprint vendored dependency), and a new **fingerprint-service** Docker container running Python 3.13 + JDK 17 + Panako wrapped behind a thin FastAPI HTTP API.

The critical stack decision for v2.0 is the fingerprint service architecture. Running Panako via subprocess-per-call is prohibitive (2-5 second JVM startup x 200K files). The correct design is a long-running container with its own HTTP API (POST /ingest, POST /query, POST /compare, GET /stats, GET /health) at port 8001, keeping the JVM alive and LMDB open between requests. audfprint is vendored as a Python module (`src/phaze/fingerprint/audfprint/`) rather than installed from its unmaintained 2015 GitHub repo.

**Core new technologies:**
- **mutagen >=1.47.0**: Audio tag read/write — the only Python library with both capabilities across all formats (ID3v1/v2, Vorbis, MP4, FLAC, OGG, AIFF). Zero dependencies.
- **beautifulsoup4 >=4.14.3 + lxml >=5.3.0**: HTML scraping — battle-tested for malformed HTML; lxml backend provides 5-10x parse speed over html.parser.
- **httpx >=0.28.1** (promote from dev): Async HTTP — already validated in test suite; used for 1001tracklists POST requests and fingerprint service calls.
- **audfprint (vendored)**: Landmark-based fingerprinting — vendor core classes from 2015 repo, strip CLI layer, modernize to Python 3.13. Fast for exact/near-exact matches.
- **Panako 2.1 (Docker container + Javalin wrapper)**: Tempo-robust fingerprinting — handles up to 10% tempo/pitch shifts common in DJ sets. Runs as long-lived JVM service with custom HTTP wrapper.
- **joblib >=1.4.0**: Parallel processing — lightweight library used by audfprint vendored code.

**What to avoid:** Selenium/Playwright for scraping (POST endpoints confirmed to work without headless browser); Scrapy (full framework overkill for one site); subprocess-per-call Panako (JVM startup penalty makes this prohibitive); dejavu (MySQL dependency, abandoned 2021); LMDB Python bindings for direct Panako DB access (single-writer corruption risk); litellm >=1.82.7 (supply chain attack March 2026 — remain pinned at <1.82.7).

### Expected Features

v2.0 research identified five feature areas with a clear dependency order.

**Must have (table stakes):**
- Audio tag extraction for all formats (MP3/M4A/OGG/FLAC/OPUS) via mutagen — populates the existing but empty `FileMetadata` table; feeds LLM context for all downstream features
- AI destination path proposals — `RenameProposal.proposed_path` column exists but is always NULL; extend the LLM prompt to fill it; add path display and collision detection to the approval UI
- Duplicate resolution UI — `find_duplicate_groups()` service exists; build the review page with side-by-side metadata comparison and "keep this, delete rest" workflow
- Fingerprint service container — run audfprint + Panako as a long-lived HTTP sidecar; ingest all music files; match live set audio against the resulting library
- Live set tracklist matching with admin review UI — proposed tracklists from fingerprint matches, ordered by timestamp, pending human confirmation

**Should have (differentiators):**
- 1001Tracklists search, scraping, and storage — fuzzy-match tracklists to files; cross-reference with fingerprint matches for high-confidence identification
- Periodic tracklist refresh via arq cron — monthly re-scrape with randomized jitter for unresolved tracklists
- Quality-based duplicate auto-suggestion — pre-select keeper by bitrate + tag completeness + path length
- Acoustic near-duplicate detection — fingerprint similarity groups to complement SHA256 exact dedup

**Defer to v3+:**
- Tag writing back to audio files after rename (destructive; Postgres is the metadata store)
- Album art extraction and display in UI (binary blob complexity, scope creep)
- User-editable path templates (single-user tool; edit the prompt text directly)
- Real-time fingerprinting during playback (Shazam architecture; out of scope)
- Bulk scraping of 1001tracklists database (abusive, IP-ban risk)

### Architecture Approach

v2.0 follows the established v1.0 pattern: thin arq task wrappers calling service classes, CPU-bound work via `run_in_process_pool`, per-task sessions initialized in arq `on_startup`. The additions are six new service classes, four new routers, six new arq task functions, one new Docker container (fingerprint-service), and one Alembic migration adding four tables and two columns to existing tables. The fingerprint service is intentionally domain-ignorant — it accepts file paths and returns match scores; all Phaze business logic stays in the main codebase.

**Major components:**
1. **MetadataExtractService** — reads audio tags with mutagen, normalizes to typed FileMetadata columns, stores raw dump in `raw_tags` JSONB; runs at `DISCOVERED -> METADATA_EXTRACTED` transition
2. **Fingerprint Service Container** — Python 3.13 + JDK 17 image; audfprint (imported directly) + Panako (subprocess to long-running JVM); exposes POST /ingest, POST /query, POST /compare, GET /stats, GET /health at :8001; two named Docker volumes (`/data/audfprint`, `/data/panako`)
3. **TracklistScraper** — httpx + BeautifulSoup; POST-based requests to 1001tracklists.com; 3-5 second randomized delay; raw HTML cached alongside parsed data; versioned scrape snapshots
4. **DedupResolutionService** — surfaces SHA256 duplicate groups (existing) plus acoustic near-duplicates from FingerprintMatch table; all deletions require human approval
5. **New SQLAlchemy Models** — `Tracklist`, `TracklistEntry`, `FingerprintMatch`, `TracklistFileLink`; `FileMetadata` gains `duration_seconds` and `track_number` columns

**Critical design decisions:**
- Tag extraction inserts a new step at the front of the pipeline; the state machine transition `process_file` must check for `METADATA_EXTRACTED` state, not `DISCOVERED`
- Fingerprinting is additive enrichment, not a pipeline prerequisite — track status in `AnalysisResult.features` JSONB rather than blocking the critical path
- Periodic tracklist refresh uses Redis SETNX as a distributed lock to prevent double-execution across workers
- Panako gets higher combined score weight (0.6) than audfprint (0.4) because the primary use case — DJ sets — inherently involves tempo adjustment

### Critical Pitfalls

1. **State machine expansion breaks existing pipeline** — `process_file` transitions directly `DISCOVERED -> ANALYZED`; inserting intermediate states requires updating `PIPELINE_STAGES`, all UI templates, and the dashboard in the same PR. Write an Alembic data migration to backfill existing `ANALYZED+` files. Make new stages non-blocking so existing files remain processable.

2. **Task session engine leak exhausts PostgreSQL connections** — v1.0's `get_task_session()` creates a new `AsyncEngine` per invocation. With 3-4 concurrent task types in v2.0, connection exhaustion (`asyncpg.TooManyConnectionsError`) cascades. Refactor to a module-level engine with connection pooling (`pool_size=5, max_overflow=10`) initialized in arq `on_startup` — do this before adding any new task type.

3. **Panako LMDB corruption in Docker** — LMDB uses memory-mapped I/O with POSIX locks that can break on bind mounts. Use Docker named volumes only. Run exactly one Panako container instance. Add stale-reader check on startup. Implement SIGTERM trap for graceful LMDB close.

4. **Fingerprinting live concert recordings produces mostly noise** — Landmark-based algorithms fail on live recordings with crowd noise, reverb, and tempo shifts. Published research shows 60-70% of live material goes unidentified. Design the UI around probabilistic confidence scores and mandatory human review from day one. Never auto-apply fingerprint matches.

5. **audfprint hash table bucket overflow at 200K files** — Default hash table (2^20 bins, 100 entries/bin) silently drops entries when full. Long concert recordings generate 10-100x more landmarks than studio tracks. Use `--density 7.0`, shard by file type (studio vs. live), and monitor bucket fill statistics after the first 10K files before proceeding to full ingestion.

6. **1001Tracklists scraping overwrites good data with bad** — Unofficial endpoints change without notice. Cache raw HTTP responses. Version every scrape as a snapshot. Validate parsed structure before promoting to active. Surface validation failures as admin UI alerts, not silent corruption.

## Implications for Roadmap

Based on research, suggested phase structure (7 phases total):

### Phase 1: Audio Tag Extraction + Path Proposals + Session Refactor
**Rationale:** Lowest complexity, highest leverage. Tags feed every other v2.0 feature: path proposals, duplicate quality comparison, and tracklist search queries all depend on extracted artist, title, event, duration, and bitrate. `proposed_path` is already wired in `RenameProposal` — this completes v1.0 unfinished work. The `get_task_session()` connection pooling refactor (Pitfall 2) is a prerequisite for all new task types and must happen in Phase 1.
**Delivers:** Populated `FileMetadata` table; LLM proposals with destination paths; path collision detection in approval UI; connection-pooled arq session management.
**Addresses:** Audio tag extraction (all table-stakes items), AI destination path proposals.
**Avoids:** Task session engine leak (Pitfall 2), state machine disruption (Pitfall 1) — establish the expanded state machine pattern here with non-blocking transitions.
**Research flag:** Standard patterns. mutagen is thoroughly documented; LLM prompt extension follows v1.0 patterns.

### Phase 2: Duplicate Resolution UI
**Rationale:** Backend fully exists (`find_duplicate_groups()`, SHA256 dedup). Tag extraction from Phase 1 enables bitrate and format comparison in the side-by-side view. Resolving duplicates before fingerprint ingestion keeps the audfprint hash table from filling prematurely with redundant audio.
**Delivers:** `/duplicates/` page with duplicate groups, side-by-side metadata comparison including bitrate, "keep this / delete rest" workflow, quality-based auto-suggestion, bulk resolution actions.
**Addresses:** Duplicate resolution workflow (all table-stakes items).
**Avoids:** UX pitfall of showing only filenames — side-by-side comparison uses bitrate and tag completeness data from Phase 1.
**Research flag:** Standard patterns. HTMX table + comparison view follows established v1.0 approval UI patterns.

### Phase 3: 1001Tracklists Integration
**Rationale:** Depends on Phase 1 (artist/event metadata drives search queries). Can be built before fingerprinting since scraped tracklist data will be used to cross-reference fingerprint matches in Phase 5. httpx is already in the stack; BeautifulSoup is the only new dependency.
**Delivers:** `Tracklist` + `TracklistEntry` tables populated; fuzzy file-to-tracklist linking via rapidfuzz; admin UI for tracklist browsing; periodic refresh cron job with distributed locking.
**Addresses:** 1001Tracklists search and scraping (all table-stakes items), periodic refresh.
**Avoids:** Scraping without rate limiting (Pitfall 5), silent data corruption — versioned snapshots and structural validation built into initial implementation.
**Research flag:** Needs phase-level research. The 1001tracklists POST endpoints are undocumented and subject to change. Validate endpoint behavior and HTML structure before building the parser.

### Phase 4: Fingerprint Service Container
**Rationale:** Largest workstream with the highest technical risk. Isolated behind an HTTP API so discovery work does not affect the main codebase. Phase 2 produces a cleaner library (fewer duplicates) before bulk ingestion, reducing wasted CPU and hash table pressure.
**Delivers:** `fingerprint-service` Docker container with Dockerfile; audfprint vendored module modernized to Python 3.13; Panako + Javalin HTTP wrapper; POST /ingest /query /compare /health endpoints; bulk ingestion of studio files with staged validation.
**Addresses:** Fingerprint service container (all table-stakes items), persistent fingerprint databases.
**Avoids:** JVM startup overhead (long-running container not subprocess-per-call), LMDB corruption (named volumes, single-writer, SIGTERM trap), audfprint bucket overflow (density tuning, staged ingestion with validation after 10K files).
**Research flag:** Needs phase-level research. Two unknowns at LOW confidence: (1) audfprint Python 3.13 compatibility — run the vendored module against Python 3.13 before committing to the architecture; (2) Panako Java API accessibility — verify Panako exposes usable Java classes beyond CLI `main()` before designing the Javalin wrapper. Also validate ARM64 Docker compatibility if home server is ARM-based.

### Phase 5: Live Set Fingerprint Matching + Tracklist Cross-Reference
**Rationale:** Depends on both the fingerprint database (Phase 4) and scraped tracklists (Phase 3). This phase produces the "killer feature" — automated setlist identification with cross-validated confidence from two independent sources (acoustic fingerprint + human-curated tracklist).
**Delivers:** `FingerprintMatch` table; batch live set querying against fingerprint DB; proposed tracklist UI for admin review with confidence scores; cross-reference of fingerprint matches against 1001Tracklists data; `TracklistFileLink` table.
**Addresses:** Live set tracklist matching (all table-stakes items), acoustic near-duplicate detection.
**Avoids:** False positive matches auto-applied (Pitfall 4) — all matches routed through review UI; fingerprint matching on live sets treated as probabilistic aid requiring calibration.
**Research flag:** Needs phase-level research. The hybrid audfprint + Panako scoring weights (0.4/0.6) are a design decision, not a proven pattern. Requires calibration against real concert recordings from the actual corpus. Build scoring as a configurable parameter from day one.

### Phase Ordering Rationale

- **Tags before everything:** Mutagen extraction is the foundation. Path proposals, duplicate quality comparison, and 1001tracklists search all depend on extracted metadata. No other phase delivers full value without it.
- **Session refactor in Phase 1:** The `get_task_session()` engine leak is a prerequisite for every subsequent phase that adds new task types. Fixing it first prevents cascading connection exhaustion.
- **Dedup before fingerprinting:** Running fingerprint ingestion on a de-duplicated library avoids wasting CPU on redundant files and keeps the audfprint hash table from filling prematurely with duplicate landmarks.
- **Tracklists before fingerprint matching:** Scraped tracklist data is needed for Phase 5's cross-reference validation. Building it independently in Phase 3 also validates the httpx + BeautifulSoup stack before fingerprint integration.
- **Container setup before live matching:** The fingerprint service must exist and be populated before the main codebase can query it. Phase 4 and Phase 3 can run in parallel if resourcing allows — they have no direct dependency on each other.

### Research Flags

Phases needing deeper research during planning:
- **Phase 3 (1001Tracklists):** Validate the undocumented POST endpoints and current HTML structure before building the parser. One-time manual investigation to confirm endpoints still work.
- **Phase 4 (Fingerprint Service):** Two unknowns at LOW confidence require spikes: (1) audfprint Python 3.13 compatibility with vendored module; (2) Panako Java API class structure for Javalin integration. Also confirm server architecture (x86 vs. ARM64) for Docker platform targeting.
- **Phase 5 (Live Set Matching):** The hybrid scoring formula requires calibration against real live recordings from the corpus. Plan for an iteration cycle; do not treat initial weights as final.

Phases with standard patterns (skip research-phase):
- **Phase 1 (Tag Extraction + Path Proposals):** mutagen is thoroughly documented; LLM prompt extension follows v1.0 patterns; arq session pooling is straightforward.
- **Phase 2 (Duplicate Resolution UI):** HTMX bulk-action table view follows the existing v1.0 proposals UI pattern exactly.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | MEDIUM | New prod dependencies (mutagen, BS4, lxml, httpx) are HIGH. audfprint vendoring is MEDIUM (Python 3.13 unverified). Javalin/Panako HTTP wrapper is LOW (no existing examples). |
| Features | HIGH | All five feature areas have clear scope, dependency order, and existing v1.0 hooks to build on. Anti-features are explicitly out of scope. |
| Architecture | MEDIUM | Main codebase integration is HIGH confidence following v1.0 patterns. Fingerprint service container design is MEDIUM (Panako Java API structure unverified). Hybrid scoring weights are LOW (calibration required). |
| Pitfalls | HIGH | Most pitfalls derive from v1.0 codebase analysis, documented library issues (mutagen GitHub issues), and established domain knowledge. Recovery strategies defined for all major risks. |

**Overall confidence:** MEDIUM

### Gaps to Address

- **audfprint Python 3.13 compatibility:** The library uses numpy/scipy patterns from 2015. Before Phase 4 planning, vendor the module and run its test suite under Python 3.13. Document any failures before committing to the architecture.
- **Panako Java API accessibility:** Research confirms Panako is CLI-only at the user level, but does not verify whether its core Java classes are importable by Javalin without going through `main()`. Inspect the panako.jar class structure as a Phase 4 spike before designing the wrapper.
- **Hybrid scoring calibration:** The 0.4/0.6 weighting between audfprint and Panako is a starting hypothesis. Real validation requires testing against known live recordings from the actual corpus. Build scoring as a configurable parameter and plan a calibration iteration.
- **1001tracklists endpoint stability:** Undocumented POST endpoints are the foundation of Phase 3. Manual validation before Phase 3 kickoff; if endpoints have changed, adjust the scraping strategy before building the parser.
- **ARM64 Panako Docker:** If the home server runs on Apple Silicon or ARM64, Panako's LMDB and JGaborator native libraries require platform-specific builds. Confirm server architecture before Phase 4 planning.

## Sources

### Primary (HIGH confidence)
- [mutagen 1.47.0 — PyPI, GitHub, readthedocs](https://pypi.org/project/mutagen/) — tag formats, EasyID3/EasyMP4 API, error taxonomy (ID3NoHeaderError, HeaderNotFoundError)
- [beautifulsoup4 4.14.3 — PyPI](https://pypi.org/project/beautifulsoup4/) — HTML parsing, lxml backend performance
- [httpx 0.28.1 — PyPI, v1.0 test suite](https://pypi.org/project/httpx/) — already validated in project
- [LMDB documentation](http://www.lmdb.tech/doc/) — file locking, multi-process caveats, Docker volume requirements
- [v1.0 codebase — tasks/session.py, models/file.py, services/pipeline.py, services/dedup.py] — engine-per-call pattern, unused FileState values, hardcoded PIPELINE_STAGES

### Secondary (MEDIUM confidence)
- [audfprint GitHub (dpwe/audfprint)](https://github.com/dpwe/audfprint) — landmark algorithm, hash table design, density settings — last commit 2015
- [Panako 2.1 GitHub + documentation](https://github.com/JorenSix/Panako) — tempo-robust fingerprinting, CLI-only interface, LMDB storage, Docker support
- [Panako ISMIR 2014 + JOSS papers](https://archives.ismir.net/ismir2014/paper/000122.pdf) — handles time-scale and pitch modification up to 10%
- [Docker Panako community container](https://github.com/Pixelartist/docker-panako) — confirms Docker feasibility
- [leandertolksdorf/1001-tracklists-api + GodLesZ/1001tracklists-scraper](https://github.com/leandertolksdorf/1001-tracklists-api) — confirms POST endpoints, BeautifulSoup approach; both evaluated and rejected as libraries
- [Landmark-based fingerprinting for DJ mix monitoring (ISMIR/ResearchGate)](https://www.researchgate.net/publication/307547659_LANDMARK-BASED_AUDIO_FINGERPRINTING_FOR_DJ_MIX_MONITORING) — live recording matching challenges
- [Accuracy comparisons of fingerprint-based song recognition (PMC)](https://pmc.ncbi.nlm.nih.gov/articles/PMC10028751/) — 60-70% unidentified live music statistic

### Tertiary (LOW confidence)
- Javalin 6.x wrapping Panako core Java API — technically plausible but no existing examples; needs spike to verify class structure
- Hybrid audfprint + Panako scoring weights (0.4/0.6) — design decision, not proven pattern; needs calibration with real corpus data

---
*Research completed: 2026-03-30*
*Ready for roadmap: yes*
