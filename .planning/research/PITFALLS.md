# Pitfalls Research

**Domain:** Cross-service intelligence and file enrichment (Discogs linking, tag writing, CUE sheets, search) for existing music collection organizer
**Researched:** 2026-04-02
**Confidence:** HIGH

## Critical Pitfalls

### Pitfall 1: Tag Writing Corrupts Audio Files or Produces Unreadable Tags

**What goes wrong:**
Calling `mutagen.save()` on a file modifies it in-place with no built-in backup or rollback. If the write is interrupted (process kill, disk full, power loss), the file is corrupted. Additionally, mutagen defaults to ID3v2.4 for MP3 files, which some players cannot read. Each format family (ID3 for mp3, Vorbis Comments for ogg/opus/flac, MP4 atoms for m4a) has different encoding rules and gotchas. The existing `metadata.py` only reads tags -- writing is fundamentally different because it modifies irreplaceable files on disk.

**Why it happens:**
Developers treat tag writing as "set field, call save()." They forget: (1) `save()` rewrites the file in-place with no transaction or backup, (2) mutagen writes ID3v2.4 by default but many players only read ID3v2.3, (3) ID3v1 tags are encoded in Latin-1 which cannot represent all Unicode characters, (4) MP4/M4A, Vorbis Comment, and ID3 have different APIs for the same logical operation. The existing tag extraction code uses `mutagen.File()` generic interface for reads, but the generic interface lacks fine-grained control over write parameters (ID3 version, encoding).

**How to avoid:**
- Write ONLY to destination copies, never to originals. The project already uses copy-verify-delete, so enforce that tag writes happen post-copy. Originals remain untouched.
- Create format-specific writer functions dispatched by file type: `_write_id3()` for MP3, `_write_vorbis()` for OGG/OPUS/FLAC, `_write_mp4()` for M4A. Do not rely on the generic `mutagen.File()` interface for writes.
- For ID3 files, explicitly set `save(v2_version=3)` for maximum player compatibility. If the user needs v2.4, make it configurable.
- Implement verify-after-write: after `save()`, re-open the file with `mutagen.File()` and confirm the written values match what was intended.
- Gate all tag writes behind human-in-the-loop approval: show a diff of "current tags vs proposed tags" in the UI before writing.
- Log the operation in the audit log with before/after snapshots.

**Warning signs:**
- Tag write tests only cover one format (usually MP3). OGG, OPUS, FLAC, M4A are untested.
- No error handling around `save()` calls.
- Writing tags to files in the original source directory instead of destination copies.
- No UI preview of what will change before writing.
- Tests use `mutagen.File()` generic interface for writes instead of format-specific classes.

**Phase to address:**
Tag writing phase. Must include format-specific round-trip tests for all 5 formats (mp3, m4a, ogg, opus, flac).

---

### Pitfall 2: Discogs Matching Links to Wrong Release (False Positive Linking)

**What goes wrong:**
Fuzzy matching artist+title against Discogs returns plausible but incorrect results. The same track appears on hundreds of Discogs releases -- original single, compilation albums, DJ mix CDs, bootlegs, different regional pressings. "Tiesto - Adagio for Strings" has dozens of Discogs entries across different labels, remixes, and compilations. Artist names vary ("Tiesto" vs "DJ Tiesto" vs "Tiesto" with diacritics). If the system auto-links to the wrong release, bad metadata propagates into tags and CUE sheets and is hard to undo.

**Why it happens:**
Discogs has enormous duplication by design -- it catalogs every pressing and variant. Discogs search relevancy is unreliable: the first result is often not the best match. Developers pick the highest-confidence fuzzy match without understanding that Discogs field-specific searches (`artist=X&release_title=Y`) behave differently from free-text `q=` searches. Tests use exact-match data that does not exercise ambiguous cases.

**How to avoid:**
- Never auto-link without human review. Store top 3-5 candidate matches with confidence scores rather than committing to one.
- Use Discogs field-specific search parameters (`artist=`, `track=`, `release_title=`) rather than free-text `q=` queries.
- Implement multi-signal scoring: exact artist match (high weight), title match including remix info (medium), label match (medium), year proximity (low).
- Since Discogsography is a separate service with its own local database, query its local data first. Only fall through to the Discogs API for cache misses.
- Store `discogs_release_id` and `discogs_master_id` separately. Master IDs group all pressings of the same release.
- Display match candidates in the UI with enough context (label, year, format, track listing) for the user to select the correct one.
- Distinguish "not yet searched" from "searched, no match found" from "searched, matched" in the data model.

**Warning signs:**
- Tests only use exact-match test data with no ambiguous cases.
- No concept of "candidate matches" -- code picks one result and stores it immediately.
- Matching logic ignores remix suffixes, featured artists, and label variations.
- No way for the user to manually enter a Discogs URL to correct a bad auto-link.

**Phase to address:**
Discogs linking phase. Must include candidate storage, confidence scoring, and manual override UI.

---

### Pitfall 3: Discogs API Rate Limiting Breaks Batch Operations

**What goes wrong:**
Discogs API allows 60 authenticated requests/minute (25 unauthenticated), enforced per IP address. With 200K files, even 10% needing Discogs lookups means 20,000 requests -- 5.5+ hours at max rate. Bursting above the limit returns 429 errors. Because rate limiting is per-IP (not per-token), Discogsography running on the same server consumes shared quota. Generic User-Agent strings trigger stricter undocumented throttling that is not reflected in response headers.

**Why it happens:**
Developers test with 10-50 files and never hit rate limits. They add naive retry logic (retry immediately on 429) which worsens the problem. They forget that Discogsography on the same IP is also making Discogs API calls, consuming shared quota.

**How to avoid:**
- Route ALL Discogs queries through the Discogsography service. Do not call the Discogs API directly from phaze. Discogsography should own rate limiting centrally.
- Implement batch matching at the database level: query Discogsography's local database first, only hit the Discogs API for cache misses.
- Use the `X-Discogs-Ratelimit-Remaining` header to throttle proactively, not reactively.
- Process Discogs linking as a background job with a global rate limiter (Redis-based token bucket shared across all workers).
- Set a custom, unique User-Agent string. Generic user agents get more aggressive throttling not reflected in headers.
- Prioritize: link tracks in approved tracklists first, defer orphan files.

**Warning signs:**
- No rate limiting logic in the Discogs client code.
- phaze calls the Discogs API directly instead of through Discogsography.
- Batch operations spawn concurrent Discogs requests without coordination.
- No exponential backoff -- just fixed-delay retry.

**Phase to address:**
Discogs linking phase. Rate limiting must be built into the client from day one.

---

### Pitfall 4: CUE Sheet Timestamps Use Wrong Frame Rate (Centiseconds Instead of 75fps)

**What goes wrong:**
CUE sheet INDEX timestamps use `MM:SS:FF` format where FF is frames at 75 frames per second (CD Red Book standard). Developers convert from seconds using `int(frac * 100)` (centiseconds) instead of `int(frac * 75)` (frames), producing invalid frame values (76-99) and incorrect playback positions. Additionally, fingerprint timestamps have variable sub-second accuracy, and 1001tracklists timestamps are often rounded to the nearest minute with no sub-second precision at all.

**Why it happens:**
The 75fps frame rate is unintuitive -- it comes from CD-DA's 2352-byte sector format, not from any modern convention. Most developers assume frames are centiseconds. Fingerprint services return timestamps in seconds (float) and the conversion is easy to get wrong. 1001tracklists positions are just "MM:SS" with no sub-second data.

**How to avoid:**
- Create a dedicated `CueTimestamp` value object or utility function that converts from seconds (float) to `MM:SS:FF` with explicit `frames = int(fractional_seconds * 75)`.
- Add validation that rejects FF values >= 75. This catches the centiseconds bug immediately.
- Document the precision source for each track timestamp: "fingerprint" (sub-second), "1001tracklists" (minute-level), "manual" (user-entered). Store this alongside the timestamp.
- When using 1001tracklists timestamps (no sub-second precision), always set FF to 00.
- Ensure INDEX 01 of TRACK 01 in each file starts at 00:00:00 (CUE spec requirement for the first track).
- Test with actual CUE-aware players (foobar2000, VLC, Kodi), not just syntax validation.
- Handle the case where some tracks have no timestamp at all -- estimate from position order or skip with a REM comment.

**Warning signs:**
- Frame values >= 75 in generated CUE sheets.
- No distinction between timestamp precision sources.
- Tests only validate string format, not that timestamps are correct.
- No handling for tracks with missing timestamps.

**Phase to address:**
CUE sheet generation phase. Timestamp conversion must be a tested utility with frame-rate validation.

---

### Pitfall 5: CUE Sheet Encoding Breaks Non-ASCII Artist and Track Names

**What goes wrong:**
CUE sheets have no official encoding standard. The format predates Unicode adoption. Many players expect Latin-1 or Windows-1252. Writing UTF-8 CUE files with non-ASCII characters (accented names common in electronic music: Royksopp, Amelie Lens, Bonobo feat. various artists with diacritics) produces garbled text in players that assume Latin-1. Adding a UTF-8 BOM helps some players but breaks others.

**Why it happens:**
CUE sheets originated in the CD burning era when Latin-1 was the de facto standard. The specification does not mandate an encoding. Modern players vary wildly: foobar2000 handles UTF-8 well, VLC handles it reasonably, but many other players (DeaDBeeF, older hardware players) do not detect encoding automatically and default to Latin-1.

**How to avoid:**
- Default to UTF-8 with BOM (`\xEF\xBB\xBF` prefix). This is the best compromise for modern player compatibility.
- Add a `REM ENCODING UTF-8` comment at the top (non-standard but recognized by some tools like CUETools).
- Validate that all track/artist strings can be encoded in the target encoding before writing. Catch `UnicodeEncodeError` and transliterate with `unidecode` as fallback.
- Open files with explicit encoding: `open(path, 'w', encoding='utf-8-sig')` (Python handles BOM automatically with `utf-8-sig`).
- Test with non-ASCII artist names and non-Latin scripts in the test suite.

**Warning signs:**
- CUE generation tests only use ASCII artist/track names.
- No encoding parameter in the CUE generation function.
- File opened with bare `open(path, 'w')` relying on platform default encoding.
- No test with accented characters.

**Phase to address:**
CUE sheet generation phase. Encoding must be explicitly handled.

---

### Pitfall 6: Search Across Multiple Tables is Slow Without Pre-Computed Index

**What goes wrong:**
Searching across FileRecord + FileMetadata + Tracklist + TracklistTrack requires JOINing 4+ tables with text matching on multiple columns. At 200K files with potentially hundreds of thousands of tracklist tracks, naive `ILIKE '%term%'` queries take seconds. PostgreSQL full-text search (tsvector/tsquery) is fast on single tables with GIN indexes, but you cannot create composite GIN indexes across JOINed tables. This forces either multiple separate searches stitched together in Python, or a pre-computed search index.

**Why it happens:**
Developers add search by putting `WHERE artist ILIKE '%query%' OR title ILIKE '%query%'` on existing queries. This works with 100 rows in development. At 200K rows with JOINs, it degrades to multi-second responses. Adding GIN indexes on individual columns helps per-table queries but does not solve the cross-table search problem.

**How to avoid:**
- Create a materialized view or dedicated `search_index` table with a pre-computed `tsvector` column that combines text from all relevant tables (artist, title, album, event, genre from files, tracklists, and metadata).
- Use `to_tsvector('simple', ...)` rather than `to_tsvector('english', ...)`. Music metadata is not natural language -- the `'english'` config stems words (e.g., "Remixes" becomes "remix") and may mangle artist names.
- Create a GIN index on the tsvector column.
- For faceted filtering (BPM range, year, genre), use regular B-tree indexes on those columns. Do not encode numeric filters into tsvectors.
- Use `REFRESH MATERIALIZED VIEW CONCURRENTLY` (requires a unique index on the view) on a schedule or after batch operations, not per-row triggers.
- For partial/fuzzy matching (user types "tiest" expecting "Tiesto"), combine tsvector with `pg_trgm` trigram extension and a GiST or GIN trigram index.
- Profile with realistic data volume before finalizing the approach.

**Warning signs:**
- Search uses `ILIKE` instead of `tsvector/tsquery`.
- No GIN index on any text search column.
- Search query JOINs 4+ tables without LIMIT.
- No `EXPLAIN ANALYZE` in tests or development workflow.
- Search returns all matching rows without pagination.

**Phase to address:**
Search phase. Must include index creation migration, materialized view or search table, and load testing.

---

### Pitfall 7: Tag Writing Creates Inconsistent State Between Database and Files

**What goes wrong:**
Tags are written to the file but the database metadata record is not updated, or vice versa. The UI shows "corrected" tags but the file still has old tags. Or the database has new values but the file write actually failed silently. Subsequent scans re-extract old tags because the write never completed, overwriting the "corrected" database values.

**Why it happens:**
Tag writing and database updates are separate operations with no transactional guarantee across the file system and database. Developers update the database optimistically before writing to the file, or write to the file but forget to update the database. Without re-reading tags after writing, there is no verification that the write succeeded.

**How to avoid:**
- Implement a strict write sequence: (1) write tags to file, (2) re-read tags from file with mutagen to verify, (3) update database metadata with the re-read values, (4) record in audit log with before/after snapshots.
- Never update database metadata based on "what we intended to write." Always re-read from the file after write.
- Add a tag-write state: `TAGS_PENDING` -> `TAGS_WRITTEN` -> `TAGS_VERIFIED`. Only advance state after re-read confirms correctness.
- If the file write fails, do not update the database. Leave the record in `TAGS_PENDING` with the error recorded.
- Use the existing audit log pattern to record tag writes.

**Warning signs:**
- Database update happens before file write or without verification.
- No re-read step after tag writing.
- Tag write operation has no audit log entry.
- No rollback path when file write fails after database was already updated.

**Phase to address:**
Tag writing phase. Must follow the project's verify-after-write philosophy (same as copy-verify-delete).

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| Using `mutagen.File()` generic interface for writes | Less code, single code path | Cannot control ID3 version, encoding, or format-specific write options; silent failures on unsupported formats | Never for writes -- use format-specific classes (ID3, VorbisComment, MP4Tags) |
| Storing Discogs link as a single `release_id` column | Simple schema, fast to implement | Cannot distinguish release vs master, loses candidate alternatives, no way to record "not yet searched" vs "searched, no match" | Never -- add `master_id`, `match_status` enum, `candidates` JSONB from the start |
| CUE sheet as string concatenation | Quick to build | No validation, encoding bugs surface late, impossible to test individual components, hard to handle edge cases | Never -- use a CUE builder class with typed fields and validation |
| Search via ILIKE on raw columns | Works immediately, no migration needed | O(n) scan on every query, unusable at 200K rows, no ranking | Development/testing only -- must add tsvector/GIN index before real data |
| Skipping tag write verification (no re-read) | Faster writes, simpler code | Silent corruption, database/file divergence goes undetected indefinitely | Never -- verification is mandatory for irreplaceable files |

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| Discogsography HTTP API | Assuming it mirrors the Discogs API exactly (same endpoints, same response shapes) | Verify actual endpoints, response shapes, and what Discogsography caches locally vs proxies to Discogs. Document the API contract before writing client code |
| Discogsography HTTP API | Not handling service unavailability (it is a separate container on the same server) | Use circuit breaker pattern with graceful degradation. If Discogsography is down, mark files as "linking pending" and retry later. Do not block the UI |
| mutagen tag writing | Writing ID3v2.4 and assuming all players can read it | Default to ID3v2.3 with `save(v2_version=3)` for maximum compatibility, or make the version configurable per-user preference |
| mutagen tag writing | Not handling read-only files, permission denied, or file locked by another process | Check write permissions before attempting. Return a clear error message. The worker may be trying to write while the fingerprint service has the file open |
| PostgreSQL full-text search | Using `'english'` text search config for music metadata | Use `'simple'` config (no stemming). Artist names, track titles, and event names are not natural language. Stemming "Remixes" to "remix" or mangling "Bass" loses precision |
| PostgreSQL full-text search | Not handling partial matches (user types "tiest" expecting "Tiesto") | `tsquery` requires full lexeme matches. Add prefix matching with `:*` operator or combine with `pg_trgm` for fuzzy search. `websearch_to_tsquery()` supports prefix syntax |
| CUE sheet generation | Using `FILE` directive with absolute paths | Exposes internal directory structure. Use relative paths in CUE FILE directives, relative to the CUE file's own location |
| CUE sheet generation | Ignoring tracks with no timestamp data | Some tracklist tracks have no timestamp (1001tracklists data may be incomplete). Decide explicitly: skip the track, estimate from position order, or error. Document the choice |

## Performance Traps

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| Sequential Discogs lookups for all 200K files | Batch linking takes 5+ hours, blocks workers | Query Discogsography's local DB first; only API-call for cache misses; prioritize tracks in approved tracklists | >1,000 lookups (~17 min at rate limit) |
| Full-table scan on search without tsvector index | Search page takes 3-10 seconds | GIN index on tsvector column, B-tree indexes on facet columns (year, bpm, genre) | >50K rows without index |
| Loading full tracklist version history for CUE generation | Memory spike when tracklist has many versions | Only load the latest approved version; use `.options(selectinload())` to avoid N+1 | >100 tracks per tracklist with many versions |
| Tag writing one file at a time in sync loop | 200K files at 50ms each = 2.8 hours | Batch via SAQ jobs; write concurrently (tag writing is IO-bound, safe to parallelize across different files) | >1,000 files needing tag updates |
| Refreshing materialized search view on every insert/update | View refresh blocks reads during rebuild at scale | Use `REFRESH MATERIALIZED VIEW CONCURRENTLY` with a unique index; schedule refreshes after batch operations, not per-row | >10K rows in view |
| Loading all Discogs candidates into memory for scoring | Memory growth proportional to unique tracks times candidates per track | Stream candidates, score on retrieval, store only top-N per track. Use database-side ranking where possible | >10K tracks with 5+ candidates each |

## Security Mistakes

| Mistake | Risk | Prevention |
|---------|------|------------|
| Passing user search input directly to `to_tsquery()` | SQL injection or query parse errors from special characters (`&`, `!`, `:`, `*`) | Use `plainto_tsquery()` or `websearch_to_tsquery()` which sanitize input. Never construct tsquery strings manually |
| Storing Discogs/Discogsography API credentials in code or unencrypted config | Token exposure in git history or container inspection | Use `pydantic-settings` `SecretStr` type, load from Docker secret or env var, never log token values |
| Tag writing to files outside the designated destination directory | Path traversal if proposed paths contain `../` or symlinks | Validate all write targets with `Path.resolve()` and `is_relative_to(destination_root)` before any write |
| CUE sheet FILE paths using absolute paths | Exposes internal server directory structure if CUE sheets are ever shared or viewed | Always use relative paths in CUE FILE directives |

## UX Pitfalls

| Pitfall | User Impact | Better Approach |
|---------|-------------|-----------------|
| Tag write with no preview | User cannot verify what will change before committing to irreversible file modification | Show side-by-side diff of current vs proposed tags, require explicit "Write Tags" button per file or batch |
| Discogs link with no context | User sees "Linked to Discogs #12345" -- meaningless without context | Show release title, label, year, format inline. Link to Discogs page for verification |
| Search with no results explanation | Empty results page with no guidance on what to try | Show "No results for X. Try: broader terms, different spelling. Or filter by BPM/year/genre instead" |
| CUE generation with no quality indicator | User does not know if timestamps are accurate or approximate | Badge each CUE: "Fingerprint timestamps (high accuracy)" vs "1001tracklists positions (approximate)" |
| Batch tag write with no progress | User clicks "Write All Tags" and sees nothing for minutes | SSE-based progress (already used in pipeline dashboard). Show count of written/failed/remaining |
| Search returning mixed entity types without grouping | User sees files, tracklists, and tracks mixed together in one flat list | Group results by type: "Files (42)", "Tracklists (7)", "Tracks (128)". Or use tabs |

## "Looks Done But Isn't" Checklist

- [ ] **Tag writing:** Often missing format-specific tests -- verify writes work for mp3 (ID3), m4a (MP4), ogg (Vorbis), opus (OggOpus), and flac (VorbisComment in FLAC container)
- [ ] **Tag writing:** Often missing re-read verification -- verify that after `save()`, re-opening the file with `mutagen.File()` returns the written values
- [ ] **Tag writing:** Often missing state management -- verify database reflects actual file state, not intended state
- [ ] **Discogs linking:** Often missing "no match" state -- verify the system distinguishes "not yet searched" from "searched, no match found" from "linked"
- [ ] **Discogs linking:** Often missing manual override -- verify the user can manually enter a Discogs URL/ID to link or correct a bad auto-link
- [ ] **CUE generation:** Often missing first-track-at-zero rule -- verify INDEX 01 of TRACK 01 is always 00:00:00
- [ ] **CUE generation:** Often missing tracks-without-timestamps -- verify behavior when some tracks have no timestamp (skip? estimate? error?)
- [ ] **CUE generation:** Often missing non-ASCII test -- verify CUE files with accented artist names render correctly in at least one reference player
- [ ] **Search:** Often missing NULL-field handling -- verify search works when metadata fields are NULL (200K files will have many NULL artists/titles)
- [ ] **Search:** Often missing pagination -- verify search returns paginated results, not all matching rows
- [ ] **Search:** Often missing index migration -- verify the Alembic migration creates GIN indexes, not just the SQLAlchemy model definition
- [ ] **Search:** Often missing `'simple'` tsconfig -- verify the text search config is `'simple'`, not `'english'`

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| Corrupted file from bad tag write | LOW (writes only target copies) | Delete corrupted copy, re-copy from original, re-attempt with fixed writer. Originals are never touched |
| Wrong Discogs link propagated to tags | MEDIUM | Query files linked to the wrong release ID, revert tags from audit log snapshots, re-link to correct release |
| CUE sheets with wrong frame format (centiseconds instead of 75fps) | LOW | Regenerate all CUE sheets with corrected conversion. CUE files are generated artifacts, not source data |
| Search index out of sync with data | LOW | `REFRESH MATERIALIZED VIEW CONCURRENTLY search_index;` -- one command, seconds at 200K rows |
| Database/file tag divergence | MEDIUM | Run a "tag audit" job: re-read tags from all destination files, compare with database, report discrepancies. Trust the file, update the DB |
| Discogs rate limit exceeded and IP throttled | LOW | Stop all requests, wait 60 seconds for window reset. Implement proper rate limiter to prevent recurrence |

## Pitfall-to-Phase Mapping

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| Tag writing corrupts files | Tag writing phase | Format-specific round-trip tests for all 5 formats; SHA256 of audio payload unchanged after tag write |
| Wrong Discogs match | Discogs linking phase | Tests with ambiguous input data; candidate storage verified; manual override UI functional |
| Discogs rate limiting | Discogs linking phase | Rate limiter test with mock 429 responses; backoff verified; Discogsography client respects shared rate state |
| CUE timestamp frame rate | CUE generation phase | Unit test: `seconds_to_cue_timestamp(61.5)` returns `"01:01:37"` (37 = 0.5 * 75), not `"01:01:50"`; frame values always 0-74 |
| CUE encoding | CUE generation phase | Test with non-ASCII artist names; output file has UTF-8 BOM; decode round-trip succeeds |
| Search performance | Search phase | `EXPLAIN ANALYZE` on search with 200K+ rows shows index scan, not seq scan; GIN index in migration; response < 500ms |
| Tag/DB inconsistency | Tag writing phase | Integration test: write tag -> re-read -> compare with DB; audit log entry present with before/after |

## Sources

- [Discogs API Documentation](https://www.discogs.com/developers) -- rate limits: 60 req/min authenticated, 25 unauthenticated, IP-based
- [Discogs Forum: Rate Limiting](https://www.discogs.com/forum/thread/392153) -- rate limit per-IP, generic user agents get stricter limits
- [Discogs Forum: Search Relevancy](https://www.discogs.com/forum/thread/323339) -- field-specific search vs free-text, relevancy unreliable
- [10 Tips for Better Discogs Searching](http://www.onemusicapi.com/blog/2013/06/12/better-discogs-searching/) -- scoring strategies, handling ambiguity
- [mutagen ID3 Documentation](https://mutagen.readthedocs.io/en/latest/user/id3.html) -- v2.4 default, v2.3 compatibility, encoding behavior
- [mutagen Issue #354: ID3v1 Latin-1 Encoding](https://github.com/quodlibet/mutagen/issues/354) -- ID3v1 uses Latin-1, lossy for Unicode
- [mutagen Vorbis Comment Documentation](https://mutagen.readthedocs.io/en/latest/user/vcomment.html) -- shared tag format for OGG/FLAC/OPUS
- [CUE Sheet Format Specification](https://wyday.com/cuesharp/specification.php) -- INDEX MM:SS:FF, 75 frames/second
- [Hydrogenaudio CUE Sheet Wiki](https://wiki.hydrogenaudio.org/index.php?title=Cue_sheet) -- encoding issues, compatibility
- [XLD CUE Encoding Issue](https://github.com/DanielPhoton/xld/issues/17) -- UTF-8 vs Latin-1 player compatibility
- [DeaDBeeF CUE Encoding Issue](https://github.com/DeaDBeeF-Player/deadbeef/issues/1962) -- GBK/UTF-8 encoding detection failures
- [PostgreSQL Full-Text Search Limitations](https://www.postgresql.org/docs/current/textsearch-limitations.html) -- official limitations
- [PostgreSQL FTS for 200M Rows](https://medium.com/@yogeshsherawat/using-full-text-search-fts-in-postgresql-for-over-200-million-rows-a-case-study-e0a347df14d0) -- tsvector + GIN optimization patterns
- [Meilisearch: When Postgres FTS Stops Being Good Enough](https://www.meilisearch.com/blog/postgres-full-text-search-limitations) -- composite index limitations
- [python3-discogs-client Rate Limiting](https://python3-discogs-client.readthedocs.io/en/v2.3.12/requests_rate_limit.html) -- header-based rate limit tracking
- Existing codebase analysis: `services/metadata.py` (read-only tag extraction), `models/metadata.py` (FileMetadata schema), `models/tracklist.py` (TracklistTrack with timestamp field), `models/file.py` (FileState enum)

---
*Pitfalls research for: Phaze v3.0 -- Cross-Service Intelligence & File Enrichment*
*Researched: 2026-04-02*
