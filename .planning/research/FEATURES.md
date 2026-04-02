# Feature Research: v3.0 Cross-Service Intelligence & File Enrichment

**Domain:** Music collection cross-service linking, tag writing, CUE sheet generation, unified search
**Researched:** 2026-04-02
**Confidence:** HIGH (tag writing, CUE sheets, search) / MEDIUM (Discogs cross-service linking)

---

## Feature Area 1: Discogs Cross-Service Linking

Phaze tracks individual files. Discogsography is a separate service with a full Discogs knowledge graph (artists, releases, labels, masters) in PostgreSQL and Neo4j. Linking them lets phaze answer "what Discogs release is this track from?" and "find all sets containing tracks from this release."

### Table Stakes

| Feature | Why Expected | Complexity | Existing Dependency | Notes |
|---------|--------------|------------|---------------------|-------|
| Match TracklistTrack to Discogs release via artist+title fuzzy search | Core value of cross-service linking. TracklistTrack already has artist + title from 1001Tracklists scraping and fingerprint matching. Discogsography has `/api/search` endpoint accepting artist/title queries. | MEDIUM | TracklistTrack.artist, TracklistTrack.title, discogsography `/api/search` endpoint | Use httpx async client to call discogsography API. Fuzzy match on artist+title. Store Discogs release ID + URL on a new linking table. Reuse rapidfuzz scoring pattern from tracklist_matcher.py. |
| Store cross-service links in PostgreSQL | Links must persist. A TracklistTrack can match zero or many Discogs releases (remixes exist on multiple releases). | LOW | Alembic migrations | New model: `DiscogsLink` with tracklist_track_id, discogs_release_id, discogs_artist_id, confidence, match_method. Foreign key to tracklist_tracks. |
| Confidence scoring for Discogs matches | Not all matches are correct. Artist name variations ("Deadmau5" vs "deadmau5" vs "Dead Mau5"), remix disambiguation ("Original Mix" vs "Club Mix"). Must score and let admin review. | MEDIUM | rapidfuzz (already in dependencies) | Score artist similarity + title similarity. Weight artist higher (0.6 artist, 0.4 title). Auto-link at 90+, propose at 70-89, skip below 70. Same pattern as tracklist_matcher.py. |
| Display Discogs links in tracklist review UI | When viewing a tracklist, each track should show its Discogs match (if any) with link to discogsography. | LOW | Tracklist review UI (v2.0 Phase 17), HTMX partials | Add a column or expandable row to the tracklist track table. Show release name, label, year from Discogs. Link to discogsography web UI. |
| "Find all sets containing track X" query | The killer cross-service query. Given a Discogs release/track, find all tracklists (live sets) that contain it. Answers "which DJs played this track at festivals?" | MEDIUM | DiscogsLink table, TracklistTrack -> TracklistVersion -> Tracklist -> FileRecord join chain | SQL join from discogs_links -> tracklist_tracks -> tracklist_versions -> tracklists -> files. Expose as API endpoint + UI page. Results show set name, DJ, event, date. |
| Batch matching for all unlinked tracks | Cannot expect users to link tracks one at a time. Need a background job that iterates unlinked TracklistTracks and queries discogsography in batch. | MEDIUM | arq task queue, rate limiting | arq job that pages through unlinked tracks. Rate-limit discogsography API calls (10 req/sec is reasonable for local network). Store results, skip already-linked. |

### Differentiators

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Reverse lookup: "What tracks from my collection appear on this Discogs release?" | Given a Discogs release URL/ID, find all files in phaze that match tracks on that release. Useful for discovering what you own from a specific album. | MEDIUM | Fetch release tracklist from discogsography API, match against FileMetadata.artist + FileMetadata.title. |
| Label statistics | Aggregate which labels appear most across your live set tracklists. "Your collection is 30% Drumcode, 20% mau5trap." Interesting for the user, trivial to compute from linked data. | LOW | GROUP BY on DiscogsLink joined to discogsography label data. |
| Link health monitoring | Discogsography data refreshes periodically. Links may become stale if releases are merged or deleted on Discogs. Periodic validation job. | LOW | arq cron job, HTTP HEAD or GET to discogsography to verify release still exists. |

### Anti-Features

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| Direct Discogs API calls from phaze | "Why go through discogsography? Just call Discogs directly." | Discogs API rate limit is 60 req/min (authenticated). At thousands of tracks, this takes hours. Discogsography already has the full database locally with no rate limits. Avoids external network dependency (private network constraint). | Always route through discogsography HTTP API on local network. Zero rate limit concerns. |
| Automatic tag enrichment from Discogs | "Pull genre/label/year from Discogs and update FileMetadata." | FileMetadata stores what was extracted from the actual file. Discogs data belongs in the cross-service link, not overwriting source-of-truth metadata. Mixing sources creates confusion about what came from where. | Store Discogs metadata on the DiscogsLink record. Display both sources in UI. Let tag writing (Feature Area 2) explicitly merge from chosen sources. |
| Graph visualization of track relationships | "Show a network graph of which tracks connect which sets." | Cool but zero practical value for file organization. Significant frontend complexity (D3/Cytoscape). Discogsography already has graph exploration. | Link to discogsography's explore UI for graph needs. Phaze focuses on tabular data and approval workflows. |

---

## Feature Area 2: Tag Writing

v2.0 explicitly deferred tag writing as an anti-feature during extraction ("modifying originals is destructive"). v3.0 implements it correctly: write to destination copies only, after explicit user action, with review before writing.

### Table Stakes

| Feature | Why Expected | Complexity | Existing Dependency | Notes |
|---------|--------------|------------|---------------------|-------|
| Write corrected tags to destination copies | After a file has been executed (copied to destination via copy-verify-delete), the destination copy may have wrong/missing tags. User should be able to push corrected metadata (from Postgres) into the actual file tags. | MEDIUM | mutagen (already in dependencies for read), FileRecord.state == EXECUTED, FileMetadata, destination file path from ExecutionLog | mutagen supports write for all formats: `EasyID3` for MP3, `EasyMP4` for M4A, `OggVorbis` for OGG, `FLAC` for FLAC, `OggOpus` for OPUS. Use the Easy interfaces for normalized field names. Write to current_path (post-execution destination). |
| Preview tags before writing | Human-in-the-loop principle. Show "current tags in file" vs "proposed new tags" side-by-side. User approves, then tags are written. | MEDIUM | HTMX partial rendering, FileMetadata model | New UI page or modal: left column = current file tags (re-read from file), right column = proposed tags (from Postgres FileMetadata + any Discogs enrichment). Highlight differences. Approve/reject per file. |
| Selective field writing | User may want to update artist + title but leave album alone. Per-field checkboxes in the review UI. | LOW | UI checkbox state, passed to write endpoint | Accept a list of fields to write. Only modify selected tag frames. Mutagen preserves unmodified tags on save. |
| Batch tag writing with review | Cannot write tags one file at a time for 200K files. Need batch selection with a summary review step. | MEDIUM | Bulk action pattern from proposals UI | Select files (checkboxes or filter), show aggregate preview ("updating artist on 47 files"), confirm, enqueue arq jobs. |
| Audit log for tag writes | Tags are being modified. Must record what was changed, when, and the before/after values. | LOW | Existing ExecutionLog / audit pattern | New audit entry type: TAG_WRITE. Store file_id, field_name, old_value, new_value, timestamp. |
| Only write to EXECUTED files | Safety constraint. Never write tags to source files, only to destination copies that have been verified. Prevents data loss on the irreplaceable original collection. | LOW | FileRecord.state check | Endpoint rejects tag write requests for files not in EXECUTED state. Hard guard, not just UI filtering. |

### Differentiators

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Tag source selection | When writing tags, let user choose source: "from extracted metadata", "from LLM proposal", "from Discogs link". Each source may have different data. | LOW | UI radio buttons per source. Merge logic picks fields from selected source. |
| Dry-run validation | Before writing, verify the destination file exists, is writable, and mutagen can open it. Report any issues before committing. | LOW | `pathlib.Path.exists()`, `os.access(W_OK)`, `mutagen.File()` open check. Fast pre-flight. |
| Undo tag write | Re-read original tags from raw_tags JSONB (stored during v2.0 extraction) and write them back. Restores file to pre-write state. | MEDIUM | raw_tags stored in FileMetadata. Parse back into mutagen format-specific frames. |

### Anti-Features

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| Auto-write tags after execution | "Just update tags automatically when files are moved." | Violates human-in-the-loop. Tag data might be wrong (LLM hallucination, wrong Discogs match). Must review before writing. | Tag writing is always an explicit user action, separate from file execution. |
| Album art writing | "Embed cover art from Discogs into files." | Binary blob handling, image resizing, format-specific embedding (APIC for ID3, covr for MP4, METADATA_BLOCK_PICTURE for Vorbis). Significant complexity for marginal value in a CLI-managed collection. | Store album art URL from Discogs in the link table. Display in UI. Do not embed in files. |
| ReplayGain calculation and writing | "Normalize volume tags across collection." | Requires reading entire audio stream (not just tags). CPU-intensive. Different standards (ReplayGain 1.0 vs 2.0, R128). Not part of the organization mission. | Out of scope. Use a dedicated tool (r128gain, loudgain) if needed. |

---

## Feature Area 3: CUE Sheet Generation

CUE sheets are simple text files that describe track boundaries within a single audio file. For live DJ sets, a CUE sheet lets music players show individual track markers within a long recording. The format is well-specified and widely supported by foobar2000, VLC, Kodi, and others.

### Table Stakes

| Feature | Why Expected | Complexity | Existing Dependency | Notes |
|---------|--------------|------------|---------------------|-------|
| Generate CUE file from tracklist data | Given a Tracklist with TracklistTracks that have timestamps, produce a valid `.cue` file. This is the core feature. | LOW | Tracklist + TracklistTrack models with timestamp field, approved tracklists | CUE format is trivial: FILE, PERFORMER, TITLE header, then TRACK/INDEX entries per track. Timestamps need conversion from "MM:SS" or "HH:MM:SS" to CUE "MM:SS:FF" (frames = 1/75 sec). Python string formatting, no library needed. |
| Prefer fingerprint timestamps over 1001Tracklists | Fingerprint-sourced tracklists have precise audio-aligned timestamps. 1001Tracklists timestamps are approximate (often rounded to nearest minute). Use the best available source. | LOW | TracklistTrack.confidence field, Tracklist.source field ("fingerprint" vs "1001tracklists") | If tracklist has source="fingerprint", use those timestamps directly. If source="1001tracklists", use as fallback. If both exist for same file (via tracklist versions), prefer fingerprint version. |
| Place CUE file alongside the audio file | CUE sheet must reference the audio file by relative filename. Standard practice is same directory, same base name (e.g., `Artist - Live @ Event 2024.01.15.mp3` and `Artist - Live @ Event 2024.01.15.cue`). | LOW | FileRecord.current_path (post-execution destination) | Write to `Path(current_path).with_suffix('.cue')`. Only generate for EXECUTED files (destination exists). |
| Handle missing timestamps gracefully | Some tracklists have tracks without timestamps (common on 1001Tracklists). Generate CUE entries for tracks with timestamps, add comments for tracks without. | LOW | TracklistTrack.timestamp nullable field | If timestamp is NULL, include as REM comment: `REM TRACK {position} - {artist} - {title} (no timestamp)`. Player will skip these but the information is preserved. |
| UI action to generate CUE | Explicit button on tracklist review page: "Generate CUE Sheet". Shows preview of the CUE content, then writes on confirm. | LOW | Tracklist review UI, HTMX | Button triggers HTMX request. Response shows CUE content in a code block (monospace). Confirm button writes the file. |
| Bulk CUE generation for all approved tracklists | "Generate CUE sheets for all 150 matched live sets." Must be a batch action, not one at a time. | LOW | arq task queue for batch processing | arq job iterates approved tracklists where file_id is not NULL and file state is EXECUTED. Generate CUE for each. Report count of generated/skipped/failed. |

### Differentiators

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Include Discogs metadata in CUE REM comments | CUE format supports REM (remark) lines. Include Discogs release ID, label, year for each track as comments. Players ignore these but they enrich the metadata for tools that parse CUE files. | LOW | DiscogsLink data joined to TracklistTrack. Add `REM DISCOGS_RELEASE {id}` per track. |
| CUE sheet validation | After generation, verify the CUE file is parseable and that timestamps are monotonically increasing. Catch errors before writing. | LOW | Parse own output. Check each INDEX time > previous INDEX time. Flag violations. |
| Re-generate on tracklist update | If a tracklist is edited (inline editing from v2.0 Phase 17), the CUE sheet is stale. Detect and offer re-generation. | LOW | Compare tracklist updated_at vs CUE file mtime. Show "outdated" badge in UI. |

### Anti-Features

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| Split audio files using CUE sheet | "Use the CUE to extract individual tracks from the mix." | Audio splitting requires FFmpeg, produces N new files per set, doubles storage, creates files that may have quality issues at split points. Not part of the organization mission. | Generate CUE sheets for players that support cue-based playback (foobar2000, Kodi). The set stays as one file with virtual track markers. |
| CUE sheet parsing/import | "Import existing CUE files from the collection." | Adds a whole input pipeline for a format that is rarely present in messy collections. The value is in GENERATING CUE sheets from tracklist data, not consuming them. | If existing CUE files are found during scan, classify them as companion files (already handled in v1.0 Phase 3). Do not parse their content. |
| Chapter markers in MP4/M4A | "Write chapter metadata instead of CUE for video files." | MP4 chapter writing requires ffmpeg or mp4v2. Different format per container. CUE sheets work universally and are simpler. | Generate CUE sheets for all formats. If MP4 chapter support is needed later, add as a separate feature. |

---

## Feature Area 4: Search Page

A unified search across all phaze entities: files, tracklists, tracks, metadata, analysis results. Currently the admin UI has separate pages for proposals, duplicates, tracklists, and pipeline status, but no way to search across everything.

### Table Stakes

| Feature | Why Expected | Complexity | Existing Dependency | Notes |
|---------|--------------|------------|---------------------|-------|
| Full-text search across artist, title, event, filename | The minimum search: type "Deadmau5" and see all files, tracklists, and tracks matching that artist. | MEDIUM | PostgreSQL `to_tsvector` / `to_tsquery`, existing indexed columns | Create GIN indexes on files.original_filename, metadata.artist, metadata.title, tracklists.artist, tracklists.event, tracklist_tracks.artist, tracklist_tracks.title. Use `websearch_to_tsquery` for natural input parsing. |
| Filter by entity type | "Show me only files" or "show me only tracklist tracks." Tabs or dropdown on the search page. | LOW | Query routing based on selected entity type | Run separate queries per entity type, combine results. Or use UNION ALL with a type discriminator column. |
| Filter by date range | Live sets have dates. "Show me everything from Coachella 2024" needs a date filter. | LOW | tracklists.date, metadata.year columns | Date picker in UI (HTMX + native HTML date input). Filter WHERE date BETWEEN start AND end. |
| Filter by BPM range | "Show me all tracks between 124-128 BPM." Useful for finding DJ-compatible tracks. | LOW | analysis.bpm column (already populated from v1.0 Phase 5) | Two number inputs: min BPM, max BPM. WHERE bpm BETWEEN min AND max. |
| Filter by genre/style | "Show me all techno tracks." Genre from tags, style from essentia analysis. | LOW | metadata.genre, analysis.style columns | Dropdown or text input. ILIKE match for flexibility (genre tags are inconsistent). |
| Paginated results | Cannot return 200K results. Standard pagination with page numbers and result count. | LOW | Existing Pagination class from proposal_queries.py | Reuse the Pagination dataclass. LIMIT/OFFSET with total count. |
| New admin UI tab | Search page must be a first-class navigation item alongside proposals, duplicates, tracklists, pipeline. | LOW | base.html template, HTMX navigation pattern | Add "Search" tab to the nav bar in base.html. New router + template. |

### Differentiators

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Combined results with entity type grouping | Search "Deadmau5" and see grouped results: "3 files, 2 tracklists, 47 tracklist tracks" with expandable sections. Better UX than showing a flat list. | MEDIUM | Multiple queries, grouped rendering in Jinja2 template. |
| Sort by relevance, date, or BPM | After searching, reorder results by different criteria. PostgreSQL `ts_rank` for relevance, column sorting for others. | LOW | ORDER BY clause variation. HTMX sort controls. |
| Saved searches / quick filters | Preset filters like "Untagged files", "Live sets without tracklists", "High BPM tracks (>140)". Quick access to common queries. | LOW | Hard-coded filter presets in the template. No persistence needed for a single-user tool. |
| Cross-entity drill-down | Click a search result to navigate to its detail page (proposal, tracklist review, etc.). Contextual navigation. | LOW | Link to existing detail pages. Each result includes a URL to its entity-specific view. |

### Anti-Features

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| Elasticsearch/Meilisearch integration | "PostgreSQL full-text search is slow." | At 200K records, PostgreSQL FTS with GIN indexes is sub-second. Adding a search engine means another Docker service, data sync pipeline, index management. Massive complexity for zero user-perceptible benefit at this scale. | PostgreSQL `to_tsvector` + GIN indexes. Benchmark first. Add dedicated search engine only if proven insufficient (unlikely at 200K). |
| Natural language query | "Find me energetic tracks from festivals in 2024." | Requires LLM-to-SQL translation, prompt engineering for schema understanding, error handling for malformed queries. Interesting but v4+ scope per PROJECT.md ("Natural language querying across services -- deferred to v4+"). | Structured filters (dropdowns, ranges, text inputs). Covers 95% of search needs without AI complexity. |
| Fuzzy search across all fields | "Match even if I misspell the artist name." | PostgreSQL trigram similarity (`pg_trgm`) across all indexed fields on every query is expensive. GIN trigram indexes on every text column bloat the database. | Use `pg_trgm` only on artist and title columns where misspellings are common. Use exact match or `ILIKE` for event, filename. Install `pg_trgm` extension only if needed after basic FTS. |

---

## Feature Dependencies (v3.0 scope)

```
Search Page (independent)
    |-- requires --> GIN indexes on existing columns (migration)
    |-- NO dependency on other v3.0 features
    |-- enhanced by --> Discogs links (adds Discogs release to search results)

CUE Sheet Generation
    |-- requires --> Approved tracklists with timestamps (v2.0 Phase 17 -- EXISTS)
    |-- requires --> EXECUTED files at destination (v1.0 Phase 8 -- EXISTS)
    |-- enhanced by --> Discogs links (REM metadata in CUE)
    |-- enhanced by --> Fingerprint timestamps (more precise than 1001TL)

Tag Writing
    |-- requires --> EXECUTED files at destination (v1.0 Phase 8 -- EXISTS)
    |-- requires --> FileMetadata populated (v2.0 Phase 12 -- EXISTS)
    |-- enhanced by --> Discogs links (additional metadata source)
    |-- enhanced by --> Search (find files that need tag updates)

Discogs Cross-Service Linking
    |-- requires --> TracklistTrack with artist+title (v2.0 Phase 15/17 -- EXISTS)
    |-- requires --> Discogsography service running on local network
    |-- enhanced by --> Search ("find all sets containing track X")
    |-- enhances --> Tag writing (Discogs metadata as tag source)
    |-- enhances --> CUE sheets (Discogs metadata in REM comments)
```

### Dependency Notes

- **Search is fully independent**: Zero dependency on other v3.0 features. Operates on existing data. Can be built first or in parallel.
- **CUE sheets are nearly independent**: Only depends on existing v2.0 data. The Discogs enhancement is optional polish.
- **Tag writing is nearly independent**: Depends on existing v2.0 data. Discogs metadata is an optional source, not required.
- **Discogs linking enriches everything**: Links feed into search results, CUE metadata, and tag writing sources. Build early so other features can incorporate the data.
- **No circular dependencies**: All features can proceed in any order. The optimal order is based on value delivery and enhancement potential, not hard blocking.

---

## v3.0 Phase Recommendations

### Phase 1: Discogs Cross-Service Linking
- [ ] DiscogsLink model + migration
- [ ] httpx async client to discogsography `/api/search`
- [ ] Fuzzy match scoring (rapidfuzz, reuse pattern from tracklist_matcher)
- [ ] Batch matching arq job for unlinked TracklistTracks
- [ ] Display Discogs links in tracklist review UI
- [ ] "Find all sets containing track X" query endpoint + UI

**Why first:** Produces the data that enriches all other v3.0 features. CUE sheets and tag writing both benefit from Discogs metadata. Search results are richer with cross-service links.

### Phase 2: Tag Writing
- [ ] Tag write service using mutagen (EasyID3/EasyMP4/OggVorbis/FLAC/OggOpus)
- [ ] Preview UI: current tags vs proposed tags, per-field checkboxes
- [ ] Batch tag writing with review step
- [ ] Audit log for tag writes
- [ ] EXECUTED-state guard

**Why second:** Depends on existing data. Discogs links (Phase 1) provide an additional metadata source. Core user value: files finally have correct tags.

### Phase 3: CUE Sheet Generation
- [ ] CUE file generator from TracklistTrack data
- [ ] Timestamp source preference (fingerprint > 1001tracklists)
- [ ] "MM:SS" to CUE "MM:SS:FF" conversion
- [ ] UI action on tracklist review page
- [ ] Bulk generation for all approved tracklists
- [ ] Discogs REM comments (from Phase 1 links)

**Why third:** Low complexity, benefits from Phase 1 Discogs data for REM comments. Produces companion files alongside organized audio.

### Phase 4: Search Page
- [ ] GIN index migration on text columns
- [ ] Full-text search with `websearch_to_tsquery`
- [ ] Entity type filter tabs
- [ ] Date range, BPM range, genre/style filters
- [ ] New admin UI tab in base.html nav
- [ ] Paginated results with entity grouping
- [ ] Cross-entity drill-down links

**Why last:** Benefits from all prior phases. Discogs links appear in search results. CUE generation status can be shown. Tag write status visible. The search page is the culmination of v3.0 data enrichment.

---

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority | Phase |
|---------|------------|---------------------|----------|-------|
| Discogs track-to-release matching | HIGH | MEDIUM | P1 | 1 |
| Discogs batch matching job | HIGH | MEDIUM | P1 | 1 |
| "Find all sets containing track X" | HIGH | MEDIUM | P1 | 1 |
| Tag writing with preview UI | HIGH | MEDIUM | P1 | 2 |
| Batch tag writing | HIGH | MEDIUM | P1 | 2 |
| Tag write audit log | MEDIUM | LOW | P1 | 2 |
| CUE sheet generation from tracklists | HIGH | LOW | P1 | 3 |
| Bulk CUE generation | MEDIUM | LOW | P1 | 3 |
| Full-text search page | HIGH | MEDIUM | P1 | 4 |
| BPM/date/genre filters | MEDIUM | LOW | P1 | 4 |
| Discogs REM in CUE sheets | LOW | LOW | P2 | 3 |
| Tag source selection (extracted/LLM/Discogs) | MEDIUM | LOW | P2 | 2 |
| Reverse Discogs lookup | MEDIUM | MEDIUM | P2 | 1+ |
| Label statistics | LOW | LOW | P3 | 1+ |
| Saved search presets | LOW | LOW | P3 | 4 |
| Undo tag write | LOW | MEDIUM | P3 | 2+ |

**Priority key:**
- P1: Must have for v3.0 milestone
- P2: Should have, builds on P1 features
- P3: Nice to have, defer if timeline pressure

---

## Competitor Feature Analysis

| Feature | beets | MusicBrainz Picard | Mp3tag | Phaze v3.0 |
|---------|-------|-------------------|--------|------------|
| Tag writing | Auto-write from MusicBrainz | Auto-write from MusicBrainz | Manual edit + batch write | Review-then-write with multiple sources |
| CUE sheet support | Plugin (cue) for import only | None | CUE sheet export from tags | Generate CUE from tracklist timestamps |
| Cross-service linking | MusicBrainz only | MusicBrainz only | None | Discogs via local discogsography service |
| Search | CLI query syntax | GUI filter | GUI filter + search | Web UI with FTS + structured filters |
| Live set awareness | None | None | None | First-class: tracklists, timestamps, set matching |

**Phaze's differentiation is live set intelligence.** No competitor understands DJ sets, tracklists, or festival recordings. The combination of fingerprint-identified tracklists + 1001Tracklists data + Discogs cross-linking + CUE sheet generation is unique to phaze.

---

## Sources

- [CUE Sheet Format Specification](https://wyday.com/cuesharp/specification.php) -- canonical CUE format reference
- [CUE Sheet - Hydrogenaudio Knowledgebase](https://wiki.hydrogenaudio.org/index.php?title=Cue_sheet) -- field descriptions, player compatibility
- [CUE Sheet - Wikipedia](https://en.wikipedia.org/wiki/Cue_sheet_(computing)) -- format overview, INDEX timing (MM:SS:FF at 75fps)
- [mutagen ID3 documentation](https://mutagen.readthedocs.io/en/latest/user/id3.html) -- write API, EasyID3 interface, encoding handling
- [mutagen Getting Started](https://mutagen.readthedocs.io/en/latest/user/gettingstarted.html) -- format-agnostic File() interface, save patterns
- [python3-discogs-client docs](https://python3-discogs-client.readthedocs.io/en/latest/quickstart.html) -- search API patterns (not used directly, but reference for what discogsography exposes)
- [Discogs API documentation](https://www.discogs.com/developers) -- search endpoint parameters, rate limits (60 req/min)
- [youtube-cue](https://github.com/captn3m0/youtube-cue) -- reference implementation for timestamp-to-CUE generation
- [CueSheetGenerator](https://github.com/ApplePie420/CueSheetGenerator) -- reference for tracklist-to-CUE conversion
- [PostgreSQL Full Text Search](https://www.postgresql.org/docs/16/textsearch.html) -- `to_tsvector`, `websearch_to_tsquery`, GIN indexes
- Discogsography `/api/search` endpoint -- local service, verified available (search.py router inspected)

---
*Feature research for: v3.0 Cross-Service Intelligence & File Enrichment*
*Researched: 2026-04-02*
