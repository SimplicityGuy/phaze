# Feature Research: v2.0 Metadata Enrichment & Tracklist Integration

**Domain:** Music collection enrichment -- audio tags, tracklist scraping, audio fingerprinting, duplicate resolution
**Researched:** 2026-03-30
**Confidence:** HIGH (audio tags, duplicate resolution) / MEDIUM (tracklist scraping, fingerprinting hybrid)

---

## Feature Area 1: Audio Tag Extraction (mutagen)

### Table Stakes

| Feature | Why Expected | Complexity | v1.0 Dependency | Notes |
|---------|--------------|------------|------------------|-------|
| Read tags from all supported formats (MP3/ID3, M4A/MP4, OGG/Vorbis, FLAC, OPUS, WAV, AIFF) | Files already contain metadata. Cannot propose good names without reading what exists. Every competitor (beets, Picard, Mp3tag) reads tags first. | LOW | FileRecord (scan), FileMetadata model (scaffolded but empty) | `mutagen.File()` auto-detects format. Use `easy=True` for normalized interface across ID3/MP4/Vorbis. Falls back to raw tags for format-specific fields. |
| Extract core fields: artist, title, album, year, genre, track number | The minimum useful set for LLM context and dedup heuristics. These 6 fields map directly to the existing FileMetadata columns. | LOW | FileMetadata model columns already defined | Map to FileMetadata.artist, .title, .album, .year, .genre. Track number goes in raw_tags JSONB. |
| Store raw tag dump in JSONB | Tags contain far more than 6 fields (comment, encoder, label, ISRC, album artist, disc number, compilation flag, replay gain). Raw dump preserves everything for future use. | LOW | FileMetadata.raw_tags column exists | Serialize all tags to dict. Mutagen values are lists; flatten single-element lists for readability. |
| Handle missing/corrupt tags gracefully | Real-world collections have files with no tags, partial tags, corrupt tags, wrong encodings. Must not crash the pipeline. | LOW | arq retry with backoff (v1.0 Phase 4) | `mutagen.File()` returns None for unrecognized formats. Catch `MutagenError` base exception. Store what you can, log what fails. |
| Batch extraction via worker pool | 200K files need parallel tag reading. Must integrate with existing arq task queue. | LOW | arq + process pool (v1.0 Phase 4) | Tag extraction is I/O-bound and fast (~10ms per file). Can run in async batches without process pool. Reserve process pool for CPU-heavy work (analysis, fingerprinting). |
| Feed extracted tags into LLM proposal context | Entire point of extraction: richer LLM context produces better filename proposals. | LOW | `build_file_context()` in proposal service | Extend `build_file_context()` to include FileMetadata fields alongside AnalysisResult. Artist + title + album from tags are the most valuable signals for filename proposals. |

### Differentiators

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Duration extraction from tags | Mutagen exposes `info.length` for all formats. Duration helps distinguish full sets (60-120min) from individual tracks (3-7min). Valuable signal for concert detection and naming format selection. | LOW | No additional library needed. `mutagen.File().info.length` gives seconds as float. |
| Bitrate and codec info extraction | `info.bitrate`, `info.sample_rate`, `info.channels`. Useful for quality-based duplicate resolution ("keep the 320kbps, discard the 128kbps"). | LOW | Available via `mutagen.File().info`. Store in raw_tags JSONB. |
| Encoding-safe text handling | ID3v2 allows Latin-1, UTF-16, and UTF-8 text encodings. Some files have mojibake from incorrect encoding assumptions. Detect and normalize. | MEDIUM | Mutagen handles encoding internally but surfaces the raw bytes. For truly garbled tags, store as-is in raw_tags and let the LLM interpret. |

### Anti-Features

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| Tag writing / updating embedded tags | "Fix the bad tags while extracting" | Modifying originals is destructive. If the write fails mid-file you corrupt the source. Not needed for organization -- Postgres IS the metadata store. | Store corrected metadata in Postgres only. Optionally write tags to the *destination* copy after approved move (post-v2 feature). |
| Album art extraction/display | "Show cover art in the UI" | Binary blob storage, image processing, serving static assets. Scope creep for an approval workflow UI. | Store album art presence as boolean in raw_tags. Display placeholder. Album art rendering is a post-v2 polish item. |
| Re-reading tags after file move | "Tags might change" | Files are copied byte-for-byte via copy-verify-delete. Tags do not change. Re-reading 200K files wastes hours. | Tags are extracted once, stored in Postgres. If a user manually edits tags, they can trigger a re-scan. |

---

## Feature Area 2: AI Destination Path Proposals

### Table Stakes

| Feature | Why Expected | Complexity | v1.0 Dependency | Notes |
|---------|--------------|------------|------------------|-------|
| Generate proposed_path alongside proposed_filename | v1.0 already stores `proposed_path` on RenameProposal but it is always NULL. Users expect files to land in organized folders, not a flat directory. | MEDIUM | RenameProposal.proposed_path column exists, LLM prompt template, naming format constraints | Extend the existing LLM prompt to also produce a destination path. Path format: `{Genre}/{Artist}/` for albums, `{Event}/{Year}/` for live sets. |
| Path follows naming convention from constraints | Live sets: hierarchy by event/year. Album tracks: hierarchy by genre/artist. Must be consistent and predictable. | LOW | Naming format defined in PROJECT.md constraints | Encode path templates in the prompt. Let LLM fill in variables. Validate output matches expected structure. |
| Display proposed_path in approval UI | Users must see WHERE a file will land before approving. Path is as important as filename for organization. | LOW | Proposals list UI, row_detail template | Add path column to proposal table. Show full destination in detail panel. |
| Path collision detection | Two files proposed to same destination path = data loss. Must detect and flag. | MEDIUM | Proposal storage, PostgreSQL unique constraints | Query proposed_path for duplicates before storing. Flag collisions in UI for manual resolution. Can be a database unique partial index on (proposed_path, status='pending' OR status='approved'). |

### Differentiators

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Path preview tree | Show a visual directory tree of where approved files will land. Gives user a "before/after" view of their collection structure. | MEDIUM | HTMX partial rendering a tree view from approved proposals grouped by path prefix. |
| Dry-run path validation | Verify destination directories exist (or will be created) and sufficient disk space exists before approving. | LOW | `pathlib.Path.exists()` check on parent dirs. Disk space via `shutil.disk_usage()`. |

### Anti-Features

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| User-editable path templates | "Let me define my own folder structure" | Single user tool. Templates add config UI complexity for one person. | Hard-code path logic in the LLM prompt. Edit the prompt text directly when formats need to change. |
| Auto-create directory hierarchy | "Just make the folders" | Should only create dirs during approved execution, not during proposal. Premature dir creation leaves empty folders on rejection. | Create directories only in the execution phase, inside the copy-verify-delete flow. |

---

## Feature Area 3: Duplicate Resolution Workflow

### Table Stakes

| Feature | Why Expected | Complexity | v1.0 Dependency | Notes |
|---------|--------------|------------|------------------|-------|
| Display duplicate groups in admin UI | v1.0 detects SHA256 duplicates and stores them. Users need to see and act on them. The `find_duplicate_groups()` service exists but has no UI. | MEDIUM | `services/dedup.py` (find_duplicate_groups, count_duplicate_groups) | New HTMX page: `/duplicates/`. List duplicate groups with file details (path, size, type, tags). Paginated like proposals. |
| "Keep this one, delete the rest" per group | Core duplicate resolution action. User picks the canonical file and marks others for deletion. | MEDIUM | FileRecord state machine, execution service | Add a "keeper" selection per duplicate group. Non-keeper files get marked for deletion (new state or flag). Execute via existing copy-verify-delete (just the delete part). |
| Show file metadata side-by-side | Users need to compare duplicates to decide which to keep. Path, size, bitrate, tags, analysis results all matter. | MEDIUM | FileMetadata (v2 tag extraction), AnalysisResult (v1 analysis) | Side-by-side or table comparison view. Highlight differences (e.g., one has tags, one does not; different bitrates). |
| Bulk resolution for identical groups | Many duplicate groups will have obvious winners (same file in two directories). Bulk "keep first found, delete rest" action. | LOW | Bulk action pattern from v1.0 proposals UI | Reuse the checkbox + bulk action pattern from proposals. "Auto-resolve: keep file with shortest path" or "keep highest quality". |
| Audit trail for deletions | Deleting duplicates is destructive. Must log what was deleted and why, with ability to review. | LOW | Append-only audit log (v1.0 Phase 8) | Reuse ExecutionLog model. Log deletion reason (duplicate of file X). |

### Differentiators

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Quality-based auto-suggestion | Pre-select the "best" duplicate based on bitrate, tag completeness, path length. User still approves but gets a smart default. | LOW | Requires bitrate from tag extraction (Feature Area 1). Score: higher bitrate + more tags + shorter path = better. |
| Acoustic duplicate detection (near-duplicates) | Find files that are the *same recording* but different encodings (mp3 vs m4a, 128k vs 320k). SHA256 misses these entirely. | HIGH | Requires audio fingerprinting (Feature Area 4). Group by fingerprint similarity threshold instead of exact hash match. |
| Duplicate group dashboard stats | "You have 1,247 duplicate groups containing 3,891 files wasting 42GB." Motivates action. | LOW | Aggregate query on duplicate groups + file sizes. Display on dashboard. |

### Anti-Features

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| Auto-delete duplicates without approval | "Just delete the obvious ones" | Violates human-in-the-loop constraint. "Obvious" is subjective. Different paths might mean intentional copies (backup, DJ crate, playlist folder). | Auto-suggest but always require approval. Provide "approve all suggestions" bulk action for speed. |
| Merge metadata from duplicates before deleting | "Combine the best tags from both copies" | Tag merging logic is complex (conflicting artists, years, genres). Which source wins? Adds significant complexity for edge cases. | Keep the file with the best metadata. Lost tags from deleted copy are stored in Postgres raw_tags anyway. |

---

## Feature Area 4: Audio Fingerprinting (audfprint + Panako hybrid)

### Table Stakes

| Feature | Why Expected | Complexity | v1.0 Dependency | Notes |
|---------|--------------|------------|------------------|-------|
| Fingerprint all music files during ingestion | Every file needs a fingerprint for matching. Run alongside or after analysis. | HIGH | arq task queue, process pool, FileRecord state machine (FINGERPRINTED state already exists) | audfprint is Python-native (librosa dependency, already installed). Generates spectral landmarks. ~2-5 sec per file. At 200K files = ~110-280 hours of CPU time. Must parallelize across workers. |
| Persistent fingerprint database | Fingerprints must survive container restarts. audfprint uses an in-memory hash table that can be serialized to disk. | MEDIUM | Docker volume mounts | audfprint stores its database as a numpy-based file. Mount as Docker volume. Panako uses LMDB. Both need persistent storage outside the container. |
| Match live set audio against fingerprint database | Core use case: take a 90-minute festival set recording, scan it against the fingerprint DB to identify individual tracks within it. Produces timestamped list of matched tracks. | HIGH | Fingerprinted music library, audfprint match mode | audfprint `match` mode returns hit times and track IDs. Panako returns time-offset matches robust to tempo changes. Combine results with weighted scoring. |
| Fingerprint service as long-running container | PROJECT.md specifies: fingerprint service runs as a container with API/message interface, not subprocess calls. | HIGH | Docker Compose infrastructure (v1.0 Phase 1) | Separate container with FastAPI or gRPC interface. Holds fingerprint DB in memory for fast matching. Receives "fingerprint this file" and "match this file" requests via API or Redis messages. |
| Store match results in PostgreSQL | Match results (which tracks appear in which live sets, at what timestamps) are the core data product of fingerprinting. | MEDIUM | PostgreSQL, Alembic migrations | New model: TrackMatch or SetTracklist. Fields: set_file_id, matched_file_id, start_time, end_time, confidence, algorithm (audfprint/panako). |

### Differentiators

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Hybrid audfprint + Panako scoring | audfprint is fast but brittle to tempo changes (DJs speed up/slow down tracks). Panako handles tempo/pitch shifts up to 10%. Running both and combining scores catches more matches. | HIGH | audfprint = Python, Panako = Java (separate process/container). Need a scoring/fusion layer that weighs results from both. Panako needs JDK 11+ in its container. |
| Proposed tracklist generation for live sets | After matching, generate a proposed tracklist (ordered list of tracks with timestamps) for admin review. This is the "killer feature" -- automated setlist creation. | MEDIUM | Match results in PostgreSQL, UI for review | Present matched tracks in time order with confidence scores. Admin reviews and can correct/add/remove tracks. Store approved tracklist. |
| Incremental fingerprinting | Only fingerprint new files, not the entire library on each scan. | LOW | Track fingerprint status on FileRecord (FINGERPRINTED state exists) | State machine already has FINGERPRINTED state. Skip files already fingerprinted. |

### Anti-Features

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| AcoustID/Chromaprint web service lookup | "Identify unknown tracks via the internet" | AcoustID rate limit is 3 req/sec. At 200K files = 18+ hours just for API calls. Also requires internet access (private network constraint). The value is in LOCAL matching (what tracks appear in your live sets), not internet lookup. | Build a local fingerprint database from your own collection. Match locally. No external API dependency. |
| Real-time fingerprinting during playback | "Identify what's playing right now" | This is Shazam, not a collection organizer. Requires audio capture, streaming analysis, completely different architecture. | Batch fingerprint files after ingestion. Match live sets against library in background jobs. |
| Fingerprint-based dedup as primary dedup | "Replace SHA256 dedup with fingerprint dedup" | Fingerprint matching has false positives (remixes, samples). SHA256 is exact and fast. Fingerprint dedup is a complement, not a replacement. | SHA256 for exact dedup (fast, certain). Fingerprint similarity for near-duplicate detection (slower, probabilistic). Present both in duplicate resolution UI. |

---

## Feature Area 5: 1001Tracklists Integration

### Table Stakes

| Feature | Why Expected | Complexity | v1.0 Dependency | Notes |
|---------|--------------|------------|------------------|-------|
| Search 1001tracklists by artist + event | Given a live set file, search 1001tracklists for the corresponding tracklist. PROJECT.md confirms: "documented HTTP endpoints for search (POST) and detail pages (POST) -- no headless browser needed." | MEDIUM | FileMetadata (artist from tags), LLM-extracted event/date from proposal context | POST-based search endpoint. Parse HTML response for tracklist URLs. Rate limit requests (be a good citizen). |
| Scrape tracklist detail pages | Extract ordered track list (artist, track name, timestamps if available) from a tracklist detail page. | MEDIUM | None directly, but enriches fingerprint match results | Parse HTML for track entries. Handle partial tracklists (some tracks marked "ID" = unidentified). Store structured data. |
| Store tracklists in PostgreSQL | Tracklist data must persist and be queryable. Link tracklists to FileRecord (which live set file they correspond to). | MEDIUM | Alembic migrations, FileRecord | New models: Tracklist (id, file_id, source_url, artist, event, date), TracklistEntry (id, tracklist_id, position, artist, title, timestamp). |
| Fuzzy matching tracklist to file | Search results may not exactly match filename. Need fuzzy matching on artist name + event name + date to link tracklist to file. | MEDIUM | FileMetadata.artist, proposal context_used (event_name, date) | Use string similarity (rapidfuzz or difflib). Match threshold ~80%. Present matches for admin confirmation. |

### Differentiators

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Periodic refresh for unresolved tracklists | Some live sets have no tracklist yet on 1001tracklists. Check back periodically (monthly minimum, randomized). PROJECT.md explicitly includes this feature. | MEDIUM | arq cron jobs (arq supports scheduled tasks) | Maintain a "last_checked" timestamp on tracklists with unresolved tracks. Randomize re-check to avoid hammering the site on a schedule. |
| Cross-reference fingerprint matches with scraped tracklist | Validate fingerprint-identified tracks against the scraped 1001tracklists data. Agreement = high confidence. Disagreement = flag for review. | MEDIUM | Both fingerprint match results and scraped tracklist data | Compare track lists. Mark agreements, flag disagreements. Present unified view to admin. |
| Link to original 1001tracklists page | Store source URL. Allow admin to click through to the original page for manual verification. | LOW | Store URL in Tracklist model. Render as link in UI. |

### Anti-Features

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| Headless browser scraping | "Use Selenium/Playwright for JavaScript-rendered content" | PROJECT.md confirms POST endpoints work without headless browser. Headless browsers are heavy, flaky, and slow. Adds Chromium to Docker image (500MB+). | Use httpx/aiohttp with POST requests directly. Parse HTML with BeautifulSoup or lxml. |
| Bulk scraping entire 1001tracklists database | "Download all tracklists for future matching" | Abusive to the service. Gets you IP-banned. Legally questionable. Unnecessary -- only need tracklists for files you actually have. | Search on-demand for files that need tracklists. Cache results. Respect rate limits. |
| Writing tracklist data back to audio files as chapters | "Embed chapter markers in the audio file" | Modifying source files (anti-feature from Area 1). Chapter format varies by container (MP4 chapters, CUE sheets). Complex. | Store tracklists in Postgres. Generate CUE sheets as companion files if needed (post-v2). |

---

## Feature Dependencies (v2.0 scope)

```
Audio Tag Extraction (mutagen)
    |
    +---> Enriched LLM Context
    |       |
    |       +---> AI Destination Path Proposals (extend existing LLM prompt)
    |
    +---> Duration/Bitrate for Duplicate Resolution quality scoring
    |
    +---> Artist/Event metadata for 1001Tracklists search queries

Duplicate Resolution UI
    |-- requires --> SHA256 duplicate groups (v1.0 Phase 3 -- EXISTS)
    |-- requires --> Tag extraction (for side-by-side comparison)
    |-- enhanced by --> Acoustic fingerprint similarity (Feature Area 4)

Audio Fingerprinting (audfprint + Panako)
    |-- requires --> Music files ingested (v1.0 Phase 2 -- EXISTS)
    |-- requires --> Fingerprint service container (new Docker service)
    |
    +---> Match live sets against library
    |       |
    |       +---> Proposed tracklists for admin review
    |
    +---> Acoustic near-duplicate detection
            |
            +---> Enhanced duplicate resolution groups

1001Tracklists Integration
    |-- requires --> Artist/event metadata (from tag extraction + LLM proposals)
    |-- enhanced by --> Fingerprint match results (cross-reference validation)
    |
    +---> Stored tracklists linked to files
    |
    +---> Periodic refresh (arq cron)
```

### Dependency Notes

- **Tag extraction is foundational for v2.0**: Path proposals, duplicate resolution, and tracklist search all depend on having extracted tags. Build this first.
- **Path proposals depend only on tag extraction + existing LLM infra**: Minimal new work, extend existing prompt and store proposed_path.
- **Duplicate resolution UI is mostly a frontend task**: Backend (dedup service) exists. UI is the gap. Tag extraction enhances it with quality comparison.
- **Fingerprinting is the largest and most independent workstream**: Can be built in parallel with tracklist scraping. Container setup is the main complexity.
- **1001Tracklists search needs artist/event metadata**: Either from tag extraction or from v1.0 LLM proposal context_used (which already stores artist, event_name, date).
- **Fingerprint matches + 1001Tracklists are complementary**: Both produce tracklists. Cross-referencing them is the high-confidence path. But each is valuable independently.

---

## v2.0 Phase Recommendations

### Phase 1: Tag Extraction + Path Proposals (build together)
- [ ] mutagen tag extraction for all formats, populate FileMetadata
- [ ] Extend LLM prompt to generate proposed_path
- [ ] Display proposed_path in approval UI
- [ ] Path collision detection

**Why first:** Lowest complexity, highest immediate value. Tags feed everything else. Path proposals complete the v1.0 rename workflow (proposed_path was always NULL).

### Phase 2: Duplicate Resolution UI
- [ ] Duplicate groups page with side-by-side comparison
- [ ] "Keep this, delete rest" workflow
- [ ] Quality-based auto-suggestion (bitrate, tag completeness)
- [ ] Bulk resolution actions

**Why second:** Backend exists. UI is the gap. Benefits from tag extraction (Phase 1) for quality comparison.

### Phase 3: Audio Fingerprinting Infrastructure
- [ ] Fingerprint service container (audfprint, potentially Panako)
- [ ] API interface for fingerprint/match requests
- [ ] Fingerprint all music files (batch job)
- [ ] Persistent fingerprint database

**Why third:** Largest workstream. Independent of Phases 1-2. Sets up infrastructure for Phase 4-5.

### Phase 4: Live Set Tracklist Matching
- [ ] Match live sets against fingerprint DB
- [ ] Proposed tracklist generation with timestamps
- [ ] Admin review UI for proposed tracklists

**Why fourth:** Depends on fingerprint database from Phase 3.

### Phase 5: 1001Tracklists Integration
- [ ] Search and scrape 1001tracklists
- [ ] Store tracklists in PostgreSQL
- [ ] Fuzzy match tracklists to files
- [ ] Cross-reference with fingerprint matches
- [ ] Periodic refresh for unresolved tracklists

**Why fifth:** Benefits from both tag extraction (search queries) and fingerprint matches (cross-reference). Can start earlier if Phase 3 is slow, but full value comes after fingerprinting.

---

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority | Phase |
|---------|------------|---------------------|----------|-------|
| Audio tag extraction (mutagen) | HIGH | LOW | P1 | 1 |
| AI destination path proposals | HIGH | LOW | P1 | 1 |
| Duplicate resolution UI | HIGH | MEDIUM | P1 | 2 |
| Fingerprint service container | HIGH | HIGH | P1 | 3 |
| Fingerprint all music files | HIGH | MEDIUM | P1 | 3 |
| Live set tracklist matching | HIGH | HIGH | P1 | 4 |
| 1001tracklists search + scrape | MEDIUM | MEDIUM | P2 | 5 |
| Proposed tracklist admin UI | HIGH | MEDIUM | P1 | 4 |
| Periodic tracklist refresh | LOW | LOW | P2 | 5 |
| Acoustic near-duplicate detection | MEDIUM | HIGH | P2 | 3+ |
| Panako hybrid scoring | MEDIUM | HIGH | P3 | 3+ |
| Path preview tree | LOW | MEDIUM | P3 | 1+ |
| Cross-reference fingerprints vs tracklists | MEDIUM | MEDIUM | P2 | 5 |

**Priority key:**
- P1: Must have for v2.0 milestone
- P2: Should have, builds on P1 features
- P3: Nice to have, defer if timeline pressure

---

## Sources

- [mutagen documentation](https://mutagen.readthedocs.io/) -- tag extraction API, format support, EasyID3/EasyMP4 interfaces
- [mutagen on PyPI](https://pypi.org/project/mutagen/) -- version 1.47.0, zero dependencies
- [mutagen GitHub](https://github.com/quodlibet/mutagen) -- source, issues, format-specific tag handling
- [audfprint GitHub](https://github.com/dpwe/audfprint) -- landmark-based fingerprinting, database structure, match mode
- [Panako GitHub](https://github.com/JorenSix/Panako) -- tempo-robust fingerprinting, JDK 11+ requirement, LMDB storage
- [Panako ISMIR 2014 paper](https://archives.ismir.net/ismir2014/paper/000122.pdf) -- handles time-scale and pitch modification up to 10%
- [1001tracklists-api (unofficial)](https://github.com/leandertolksdorf/1001-tracklists-api) -- Python scraping patterns, BeautifulSoup approach
- [1001tracklists-scraper](https://github.com/GodLesZ/1001tracklists-scraper) -- alternate scraping approach
- [Landmark-Based Fingerprinting for DJ Mix Monitoring](https://www.researchgate.net/publication/307547659_LANDMARK-BASED_AUDIO_FINGERPRINTING_FOR_DJ_MIX_MONITORING) -- academic reference for live set matching challenges
- [bliss duplicate resolution strategies](https://www.blisshq.com/music-library-management-blog/2013/10/22/four-strategies-to-resolve-duplicate-music-files/) -- checksum, metadata, fingerprint, and dedicated tool approaches
- [DJ Set Analyzer](https://dj-set-analyzer.com/) -- reference for tracklist identification workflow
- [TrackSniff blog](https://tracksniff.com/blog/how-to-find-tracklists-from-dj-sets/) -- overview of DJ set identification methods and tools

---
*Feature research for: v2.0 Metadata Enrichment & Tracklist Integration*
*Researched: 2026-03-30*
