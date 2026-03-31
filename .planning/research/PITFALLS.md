# Pitfalls Research

**Domain:** Adding audio tag extraction, web scraping, audio fingerprinting services, and periodic background jobs to existing music organization system (Phaze v2.0)
**Researched:** 2026-03-30
**Confidence:** HIGH (most pitfalls derived from v1.0 codebase analysis, documented library issues, and established domain knowledge)

## Critical Pitfalls

### Pitfall 1: State Machine Expansion Breaks Existing Pipeline

**What goes wrong:**
v1.0's `FileState` enum already declares `METADATA_EXTRACTED` and `FINGERPRINTED` states but they are unused. The v1.0 pipeline hardcodes a linear flow: `DISCOVERED -> ANALYZED -> PROPOSAL_GENERATED -> APPROVED -> EXECUTED`. The pipeline stats dashboard (`PIPELINE_STAGES` in `pipeline.py`) only lists these 5 states. Adding metadata extraction and fingerprinting as intermediate steps changes the meaning of "what comes next" for every file. Existing files already in `ANALYZED` or later states have never passed through the new steps. The `process_file` task function transitions directly from `DISCOVERED` to `ANALYZED` -- inserting new intermediate states breaks this hardcoded transition.

**Why it happens:**
States were forward-declared in v1.0 but transition logic, pipeline stats, dashboard UI, and task functions all assume the original 5-state linear flow. Developers add the new states to the enum and forget to update every consumer.

**How to avoid:**
1. Write an Alembic data migration that backfills existing `ANALYZED`+ files with stub metadata and fingerprint records, marking them "migrated from v1" so they can be reprocessed later if desired.
2. Make metadata extraction and fingerprinting optional enrichment steps -- files can proceed to `ANALYZED` if these steps fail, with a flag indicating incomplete enrichment. Do not block the existing pipeline.
3. Update `PIPELINE_STAGES`, `get_pipeline_stats()`, all UI templates that enumerate states, and the pipeline dashboard in the SAME migration/PR as the state transition changes.
4. Add a `reprocess` capability that lets you push files backward through new stages without losing existing analysis data.

**Warning signs:**
- Pipeline dashboard shows 0 files in new stages after migration
- Existing `ANALYZED` files cannot be re-proposed because they "skipped" a required state
- HTMX partial templates enumerate states from a hardcoded list that was not updated

**Phase to address:**
First v2 phase (metadata extraction). Must establish the expanded state machine before any new processing logic.

---

### Pitfall 2: Mutagen Error Explosion on 200K Wild Files

**What goes wrong:**
A music collection accumulated over decades will contain files with corrupted tags, missing headers, wrong extensions, and non-standard encodings. Mutagen raises distinct exceptions: `ID3NoHeaderError` (MP3 without ID3 header), `HeaderNotFoundError` (file is not actually the claimed format), and `MutagenError` (general corruption). Processing 200K files without granular error handling causes entire worker batches to fail via arq retry, or silently skips files that later break the LLM proposal pipeline because they have empty metadata.

**Why it happens:**
Developers test with clean files. Real collections contain Napster-era rips, truncated downloads, files whose extensions lie about their format (`.mp3` that is actually `.m4a`), and files with Latin-1 or Windows-1252 encoded tags.

**How to avoid:**
1. Use `mutagen.File(path, easy=True)` wrapped in try/except catching `MutagenError` (the base class), NOT specific subclasses.
2. Record extraction outcome in the database: create a `tag_status` enum on FileMetadata with values `extracted`, `no_tags`, `corrupt`, `unsupported_format`.
3. Test the extraction pipeline with intentionally broken files: truncated, wrong extension, empty, zero-byte, non-audio files with music extensions.
4. Handle encoding normalization: mutagen returns `str` for ID3v2.4+ (UTF-8) but may return Latin-1 bytes for ID3v1. Normalize everything to UTF-8 before writing to PostgreSQL.
5. Existing v1.0 `FileMetadata` model has `artist`, `title`, `album`, `year`, `genre`, `raw_tags` columns. The `raw_tags` JSONB column should store the complete mutagen output; the typed columns should store normalized, validated values.

**Warning signs:**
- Metadata extraction reports 100% success rate on a real collection (means error handling is too broad)
- FileMetadata table has significantly fewer rows than FileRecord table with no explanation
- LLM proposals fail because metadata fields are `None` for files that actually have tags

**Phase to address:**
Metadata extraction phase. Build the error taxonomy before running batch processing.

---

### Pitfall 3: Panako LMDB Database Corruption in Docker

**What goes wrong:**
Panako uses LMDB (Lightning Memory-Mapped Database) for its fingerprint store. LMDB relies on POSIX file locks and memory-mapped I/O. In Docker, if the LMDB data directory is on a bind mount with certain filesystems, file locking may fail silently. If the container is killed mid-write (`docker stop`, OOM kill), stale reader locks accumulate and can block future writes or cause unbounded database growth. Opening the same LMDB environment from multiple processes (e.g., accidentally running two Panako containers) corrupts the lock table.

**Why it happens:**
LMDB is designed for bare-metal performance. Docker's filesystem abstraction layers can break POSIX lock semantics. Panako's documentation does not warn about Docker-specific LMDB behavior. Developers test with clean starts and never test crash recovery.

**How to avoid:**
1. Use a Docker named volume (not a bind mount) for Panako's LMDB data directory. Named volumes use the local driver which supports POSIX locks correctly.
2. Run exactly ONE Panako container instance. LMDB does not safely support multi-process write access.
3. Add a container health check that verifies LMDB is readable (e.g., run `panako stats` or equivalent).
4. Implement SIGTERM trap in the container entrypoint for graceful LMDB environment closure.
5. Include an LMDB stale-reader check on container startup before accepting requests.
6. Back up the LMDB data directory on a schedule -- it is the sole source of truth for fingerprints. Named volumes survive `docker compose down` but not `docker compose down -v`.

**Warning signs:**
- Panako container restarts and reports "readers table is full" or `MDB_MAP_FULL`
- LMDB data file grows to many GB despite modest track count
- Container health check passes but fingerprint queries return no results

**Phase to address:**
Fingerprint service container setup. Must be validated before ingesting 200K fingerprints.

---

### Pitfall 4: Fingerprinting Live Concert Recordings Produces Mostly Noise

**What goes wrong:**
Audio fingerprinting (audfprint landmark-based and Panako) is designed to match clean studio recordings. Live concert recordings from festival streams deviate heavily: crowd noise, PA system coloring, DJ transitions with tempo/pitch shifts, crossfading, MC talking over music. Research literature reports up to 60-70% of live music goes unidentified by standard fingerprinting algorithms. The Phaze collection is primarily live concert recordings and festival streams -- the hardest possible input for fingerprinting. Developers build the service, test with studio tracks, declare it working, then get garbage results on the actual corpus.

**Why it happens:**
Every fingerprinting tutorial uses clean studio recordings. Landmark-based fingerprinting (audfprint) fails on live recordings because the landmarks shift with reverb, crowd noise, and tempo changes. Panako handles tempo/pitch modification better but still struggles with heavy environmental noise.

**How to avoid:**
1. Set expectations upfront: fingerprinting will identify studio-quality files reliably and live recordings poorly. Design the UI around this.
2. Use the hybrid audfprint + Panako approach with weighted scoring. Require BOTH systems to agree before showing high-confidence matches.
3. Require a minimum confidence threshold before surfacing matches. All matches must go through human review -- never auto-propose or auto-execute.
4. For live sets, combine fingerprinting with 1001tracklists data: use tracklist timestamps + fingerprint confirmation together for higher confidence than either alone.
5. Build the reference fingerprint database from studio tracks first, then query live set segments against it. Do not fingerprint live recordings as reference material.
6. Test with known live recordings from the actual collection during development, not just studio tracks.

**Warning signs:**
- Fingerprint match rate suspiciously high (>50%) on live recordings
- Same studio track matches dozens of different live recordings (technically correct but noisy)
- Users spend more time rejecting false fingerprint matches than approving real ones

**Phase to address:**
Fingerprint matching/query phase (after ingestion). Build review UI with confidence scores from day one.

---

### Pitfall 5: 1001Tracklists Scraping Silently Returns Bad Data

**What goes wrong:**
1001tracklists.com changes HTML structure, endpoint behavior, or anti-scraping measures without notice. The PROJECT.md notes "documented HTTP endpoints for search (POST) and detail pages (POST)" but these are undocumented unofficial endpoints subject to change. When the site changes, the scraper returns 200 OK with subtly wrong or empty data rather than failing loudly. The periodic refresh job keeps running, overwriting good cached tracklists with garbage parsed from a changed page structure.

**Why it happens:**
Scraping unofficial APIs creates a dependency on implementation details the site can change at will. There is no API contract, versioning, or deprecation notice. Anti-scraping measures in 2025-2026 increasingly use behavioral analysis and ML-based detection beyond simple rate limiting.

**How to avoid:**
1. Never overwrite existing tracklist data with scraped results. Store each scrape as a versioned snapshot with timestamp. Only promote to "active" after validation passes.
2. Build structural validation: check that parsed tracklists contain expected fields (artist name, track name, timestamps). If validation fails, log the error, keep the previous good data, surface an alert in the admin UI.
3. Implement response structure fingerprinting: hash the CSS class names / DOM structure of response pages. When structure changes, halt scraping and flag for manual review.
4. Rate limit aggressively: 3-5 second randomized delay between requests. Implement exponential backoff on non-200 responses. Respect robots.txt.
5. Cache raw HTTP responses alongside parsed data so you can re-parse without re-scraping when the parser is updated.
6. Store the source URL for every tracklist so the user can manually verify against the original page.

**Warning signs:**
- Scraper returns 200 OK but parsed results have empty fields
- Tracklist count drops suddenly during a refresh cycle
- All newly scraped tracklists have the same structural parsing error
- Scraper success rate drops below 90% (site may have added anti-scraping)

**Phase to address:**
1001tracklists integration phase. Validation and versioning must be designed into the initial implementation.

---

### Pitfall 6: Task Session Engine Leak Exhausts PostgreSQL Connections

**What goes wrong:**
v1.0's `get_task_session()` in `tasks/session.py` creates a NEW `AsyncEngine` on every invocation: `engine = create_async_engine(settings.database_url)`. With v1 this was acceptable because only one task type (audio analysis) ran through arq workers. v2 adds 3-4 concurrent task types: metadata extraction, fingerprint ingestion, tracklist scraping, and periodic refresh jobs. Each task creates its own engine, each engine opens its own connection. PostgreSQL's default `max_connections=100` gets exhausted, causing `asyncpg.TooManyConnectionsError` that cascades across all task types simultaneously.

**Why it happens:**
The v1 pattern worked with bounded concurrency for a single task type. v2 multiplies the concurrent task types without updating the connection management strategy. Each `create_async_engine()` call creates a separate connection pool.

**How to avoid:**
1. Refactor `get_task_session()` to use a module-level engine with a connection pool, created once per worker process. Use `pool_size=5, max_overflow=10`.
2. Initialize the pooled engine in arq's `on_startup` hook and pass it through the worker context (`ctx`).
3. Set PostgreSQL `max_connections` explicitly in `docker-compose.yml` (e.g., `command: postgres -c max_connections=200`).
4. Monitor active connections: add a health endpoint or periodic log that queries `pg_stat_activity`.

**Warning signs:**
- Intermittent `connection refused` or `too many connections` errors in worker logs
- Tasks succeed individually in dev but fail when multiple task types run concurrently
- PostgreSQL logs show rapid connect/disconnect churn (hundreds of connections per minute)

**Phase to address:**
First v2 phase. Refactor session management before adding any new task types.

---

### Pitfall 7: audfprint Hash Table Bucket Overflow at Scale

**What goes wrong:**
audfprint's fingerprint database uses a fixed-size hash table: 2^20 (~1M) distinct fingerprint bins, each holding up to 100 entries by default. With 200K files -- many being hour-long concert recordings that generate extremely dense fingerprint landmarks -- buckets fill and entries are dropped randomly with no error. Match accuracy silently degrades as more files are ingested.

**Why it happens:**
audfprint was designed for Shazam-style "identify this 10-second clip" use cases, not fingerprinting hundreds of thousands of full-length recordings. Long concert recordings generate 10-100x more landmarks per track than a 3-minute pop song.

**How to avoid:**
1. Reduce hash density with `--density 7.0` (default is higher) to generate fewer landmarks per track while maintaining match quality.
2. Shard the fingerprint database: separate databases for studio tracks vs. live recordings, or by genre/decade.
3. Monitor bucket fill statistics after each batch ingestion. audfprint reports dropped entries -- log and alert on this.
4. Reserve audfprint for studio track identification (its strength). Use Panako as the primary engine for live set matching (its tempo-robust design is better suited).
5. Run a validation test after each ingestion batch: query a known-good match and verify it still returns the correct result.

**Warning signs:**
- audfprint logs report "N entries dropped" during ingestion
- Match accuracy degrades as more files are added (established matches stop working)
- Database file size plateaus despite continued ingestion (entries being replaced, not added)

**Phase to address:**
Fingerprint ingestion phase. Configure density and sharding strategy before bulk ingestion.

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| Store fingerprints only in audfprint/Panako native formats | Faster initial implementation | Cannot query fingerprint metadata from PostgreSQL, no cross-referencing with file records | Never -- always store a fingerprint summary (algorithm, timestamp, landmark count, status) in PostgreSQL alongside the native store |
| Skip tracklist validation on scrape | Faster scraping pipeline | Bad data pollutes tracklist tables, corrupts future LLM context for proposals | Never -- validation is cheap, data corruption is expensive |
| Single arq worker for all v2 task types | Simpler deployment, one container | CPU-heavy fingerprinting (hours to complete) starves metadata extraction and scraping | Only during early development. Split to dedicated queues before ingesting the full corpus |
| Hardcode scraping selectors inline | Quick to build | Breaks when 1001tracklists changes HTML. Must search entire codebase to update | During prototyping only -- extract to a config/constants module within the same phase |
| Skip raw HTML caching for scraped pages | Less storage | Cannot re-parse when scraper needs updating. Must re-scrape all pages, risking rate limits | Never -- raw responses are cheap to store |
| Subprocess calls to Panako CLI from Python | Avoids building HTTP API wrapper | Process spawn overhead (2-5s JVM startup) per query, error handling via string parsing, no connection pooling | Never for production -- the entire point of the container service is to avoid this |
| Reuse v1.0 `get_task_session()` pattern for new task types | No refactoring needed | Connection exhaustion under concurrent multi-task load (see Pitfall 6) | Never -- refactor before adding new task types |

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| mutagen + existing FileMetadata model | Assuming all files have tags; storing raw tag dicts with format-specific keys | Use `mutagen.File(easy=True)` for normalized key names. Store raw tags in `raw_tags` JSONB, normalized values in typed columns |
| mutagen + file writing (future tag updates after rename) | Opening files read-write during extraction phase | Open read-only for extraction. Write capability only needed post-rename in a future phase. Separate concerns |
| 1001tracklists + arq periodic jobs | Using arq's built-in `cron()` which runs on every worker startup with no distributed lock | Store last-run timestamp in PostgreSQL. Use Redis SETNX as a distributed lock. Check minimum interval before executing |
| audfprint + Docker volumes | Mounting the fingerprint database as a bind mount from host | Use a Docker named volume. audfprint's hash table uses memory-mapped I/O requiring proper filesystem semantics |
| Panako + Python API caller | Calling Panako via `subprocess.run()` from Python arq workers | Run Panako as its own long-running JVM container with an HTTP API. JVM startup (2-5s) makes per-file subprocess calls prohibitive at 200K files |
| Panako + ARM64 (Apple Silicon) host | Assuming Panako Docker image works on ARM hosts | Panako's LMDB and JGaborator native libraries need platform-specific builds. Build for `linux/amd64`, use `platform: linux/amd64` on ARM hosts, accept emulation overhead |
| Fingerprint results + existing approval workflow | Auto-applying fingerprint matches without human review | All fingerprint matches must create records that feed into the existing approval workflow. Never auto-execute |
| Multiple task types + single arq queue | All tasks share one worker pool and one Redis queue | Use separate arq queues (separate Redis DB indexes or key prefixes) for CPU-heavy (fingerprinting), I/O-bound (scraping), and fast (metadata extraction) tasks |
| New Alembic migrations + v1 data | Running `alembic upgrade head` without considering existing data in v1 states | Every schema migration that adds columns or changes states needs a data migration component that handles existing rows |

## Performance Traps

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| Loading all 200K FileRecords to find unprocessed files | API/worker OOM, PostgreSQL temp disk usage | Always query with `.where(FileRecord.state == target_state).limit(batch_size)` using cursor-based pagination | >10K files in a single state |
| Fingerprinting audio files on network/bind-mount storage | 10x slower fingerprint generation, I/O timeouts | Ensure bind mounts point to local SSD paths. For NFS/remote storage, copy to local tmpfs before processing | >1000 files on non-local storage |
| Scraping 1001tracklists without delay between requests | IP blocked, empty responses, silent data corruption | 3-5 second randomized delay. Max 100 requests per session. Exponential backoff on non-200 responses | Immediately without rate limiting |
| audfprint single-threaded query against full database | 5-10 seconds per query, unusable for batch matching | Use `--ncores` for parallel query. Pre-filter candidates using PostgreSQL metadata before fingerprint matching | >50K entries in fingerprint database |
| JVM cold start for Panako container on each query | First request takes 5-15 seconds; container restart penalty | Keep Panako as a long-lived container service. Add warmup step that pre-loads LMDB into memory on startup | Every container restart |
| Storing raw_tags JSONB + analysis features JSONB + fingerprint metadata per file without indexes | PostgreSQL table bloat, slow aggregate queries on metadata table | Create GIN indexes only on JSONB fields you actually query. Use typed columns for frequent filter/sort operations | >100K rows with full JSONB payloads |
| Enqueueing 200K fingerprinting jobs at once | Redis memory exhaustion, arq job result buildup | Enqueue in batches of 500-1000. Set `keep_result` TTL to auto-expire completed results | >50K queued jobs in Redis |

## Security Mistakes

| Mistake | Risk | Prevention |
|---------|------|------------|
| Exposing Panako HTTP API on host network via `ports:` mapping | Anyone on local network can query/modify fingerprint database | Panako container should only be on the internal Docker Compose network. No host port mapping |
| Storing scraped HTML containing inline JavaScript, rendering in admin UI | XSS via stored scrape data displayed in Jinja2 templates | Sanitize all scraped content before storage. Rely on Jinja2's default autoescaping. Never use `|safe` on scraped content |
| Scraping with identifying User-Agent string | Site blocks IP, flags for abuse | Use a generic browser User-Agent. Rotate if needed. Respect robots.txt |
| Running Panako JVM as root in container | Container escape gives root on host | Use non-root user in Panako Dockerfile. JVM does not require root |
| Storing 1001tracklists session cookies or credentials in plaintext config | Credential exposure if .env file is committed | Use `SecretStr` in pydantic-settings. Ensure `.env` is in `.gitignore` (already is for v1) |

## UX Pitfalls

| Pitfall | User Impact | Better Approach |
|---------|-------------|-----------------|
| Showing fingerprint matches without confidence scores | User cannot distinguish good from bad matches, wastes time reviewing noise | Always display match confidence as a percentage. Sort by confidence descending. Allow filtering by minimum threshold |
| Duplicate resolution UI showing only filenames | User cannot decide which duplicate to keep | Show full metadata side-by-side: path, file size, bitrate (from mutagen tags), format, analysis results. Highlight differences |
| Tracklist display with no link to source page | User cannot verify tracklist accuracy | Always store and display the 1001tracklists source URL. Make it clickable for manual verification |
| No progress indication for long-running fingerprint ingestion | User thinks system is stuck, restarts container, potentially corrupts LMDB | Show real-time progress via SSE (already used in v1): "Fingerprinting: 4,523 / 200,000 (2.3%). ETA: 14h" |
| Mixing v1 and v2 pipeline stages in one flat dashboard | Dashboard becomes a wall of numbers. New stages confuse the pipeline view | Group stages logically: "Enrichment" (metadata, fingerprint), "Analysis" (BPM/mood/style), "Organization" (proposal, approval, execution). Collapsible sections |
| Showing all tracklist matches without grouping by set/event | User cannot see the context of a tracklist match | Group tracklist matches by event/venue/date. Show the set context alongside individual track matches |

## "Looks Done But Isn't" Checklist

- [ ] **Metadata extraction:** Often missing error categorization -- verify files with no tags vs. corrupt tags vs. unsupported format produce distinct, queryable `tag_status` values in the database
- [ ] **Metadata extraction:** Often missing encoding normalization -- verify Latin-1 ID3v1 tags convert to UTF-8 before PostgreSQL storage
- [ ] **Metadata extraction:** Often missing integration with LLM proposals -- verify extracted metadata actually flows into the prompt context for filename/path proposals
- [ ] **Fingerprint ingestion:** Often missing resumability -- verify you can restart the container mid-ingestion without re-processing already-fingerprinted files (check state in PostgreSQL)
- [ ] **Fingerprint ingestion:** Often missing idempotency -- verify re-ingesting a file does not create duplicate entries in audfprint/Panako databases
- [ ] **1001tracklists scraping:** Often missing backoff on rate limiting -- verify 429/503 responses trigger exponential backoff, not immediate retry
- [ ] **1001tracklists scraping:** Often missing data versioning -- verify a new scrape does not overwrite previous good data
- [ ] **Periodic refresh job:** Often missing distributed locking -- verify two arq workers do not execute the same periodic job simultaneously
- [ ] **Periodic refresh job:** Often missing minimum-interval enforcement -- verify the monthly minimum checks a database timestamp, not just the arq cron schedule
- [ ] **Duplicate resolution:** Often missing "keep both" option -- verify the UI supports marking duplicates as intentionally separate (same song, different contexts)
- [ ] **Panako container:** Often missing backup strategy -- verify fingerprint LMDB data persists across `docker compose down` (named volume, NOT anonymous)
- [ ] **AI destination paths:** Often missing integration with existing proposal workflow -- verify path proposals use the same approve/reject/execute flow as filename proposals from v1
- [ ] **State machine expansion:** Often missing pipeline dashboard update -- verify all new states appear in the dashboard with correct ordering and counts

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| State machine breaks existing pipeline flow | LOW | Alembic data migration to backfill missing intermediate states. No data loss, just state column updates |
| Mutagen batch failure on corrupt files | LOW | Fix error handling, re-run extraction on failed files. Original audio files untouched (read-only mount) |
| LMDB corruption in Panako container | MEDIUM | Rebuild fingerprint database from audio files. Hours of CPU time but no data loss -- audio files are the source of truth |
| False positive fingerprint matches auto-applied | HIGH | Must manually review and undo incorrect renames/moves. This is why auto-apply must NEVER be implemented |
| 1001tracklists data overwritten with bad scrape | LOW if raw HTML cached, HIGH if not | Re-parse from cached raw HTML. If no cache, must re-scrape all pages risking rate limits |
| PostgreSQL connection exhaustion from engine leak | LOW | Restart workers, deploy connection pooling fix. No data loss |
| audfprint bucket overflow | MEDIUM | Must rebuild hash table with lower density setting. Hours of reprocessing all files |
| Periodic job runs twice simultaneously | LOW | Idempotent design means double-run is wasteful but not destructive. Fix distributed lock for future runs |

## Pitfall-to-Phase Mapping

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| State machine expansion | Phase 1: metadata extraction | Pipeline dashboard shows all new states; existing v1 files still queryable and progressable |
| Mutagen error handling | Phase 1: metadata extraction | Test suite includes corrupt, missing-header, wrong-extension, and zero-byte files |
| Task session engine leak | Phase 1: metadata extraction (prerequisite refactor) | Load test with 3+ concurrent task types; monitor `pg_stat_activity` count stays bounded |
| 1001tracklists scraping fragility | Tracklist integration phase | Validation failures produce admin UI alerts, not silent data corruption |
| Panako LMDB corruption | Fingerprint service setup phase | Container crash-restart test: `docker kill` mid-ingestion, restart, verify database integrity and query accuracy |
| Fingerprint false positives on live sets | Fingerprint matching phase | Test with known live recordings; measure and log false positive rate; UI shows confidence scores |
| audfprint bucket overflow | Fingerprint ingestion phase | Monitor bucket stats after ingesting first 10K files before proceeding to full corpus |
| Periodic job distributed locking | Periodic refresh phase | Run two workers simultaneously; verify job executes exactly once per interval |
| Duplicate resolution UX | Duplicate resolution UI phase | Side-by-side metadata comparison including bitrate, format, analysis results |
| AI destination path integration | Path proposal phase | Path proposals visible in existing approval UI; approve/reject/execute cycle works identically to filename proposals |

## Sources

- [audfprint GitHub -- bucket overflow, density settings, hash table design](https://github.com/dpwe/audfprint)
- [Panako GitHub -- LMDB dependencies, container usage, platform support](https://github.com/JorenSix/Panako)
- [Panako documentation -- file path issues in containers](http://panako.be/releases/Panako-latest/readme.html)
- [mutagen issue #327 -- ID3NoHeaderError handling](https://github.com/quodlibet/mutagen/issues/327)
- [mutagen issue #562 -- HeaderNotFoundError on corrupt files](https://github.com/quodlibet/mutagen/issues/562)
- [mutagen issue #666 -- file corruption after writing ID3 tags](https://github.com/quodlibet/mutagen/issues/666)
- [LMDB documentation -- file locking, multi-process caveats, stale readers](http://www.lmdb.tech/doc/)
- [Landmark-based audio fingerprinting for DJ mix monitoring (ISMIR)](https://www.researchgate.net/publication/307547659_LANDMARK_BASED_AUDIO_FINGERPRINTING_FOR_DJ_MIX_MONITORING)
- [arq documentation -- cron jobs, pessimistic execution, worker lifecycle](https://arq-docs.helpmanual.io/)
- [JVM memory in Docker containers -- heap sizing, container awareness](https://medium.com/@svosh2/how-to-choose-jvm-and-docker-container-properties-for-our-java-service-a04bb9e2c855)
- [Rate limiting in web scraping 2026 -- anti-detection evolution](https://www.scrapehero.com/rate-limiting-in-web-scraping/)
- [Accuracy comparisons of fingerprint-based song recognition](https://pmc.ncbi.nlm.nih.gov/articles/PMC10028751/)
- v1.0 codebase analysis: `tasks/session.py` (engine-per-call pattern), `models/file.py` (FileState enum with unused states), `services/pipeline.py` (hardcoded PIPELINE_STAGES), `services/dedup.py` (SHA256-only dedup)

---
*Pitfalls research for: Phaze v2.0 -- audio tag extraction, web scraping, audio fingerprinting, periodic jobs*
*Researched: 2026-03-30*
