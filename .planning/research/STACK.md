# Stack Research: v3.0 Cross-Service Intelligence & File Enrichment

**Domain:** Discogs cross-service linking, audio tag writing, CUE sheet generation, unified search
**Researched:** 2026-04-02
**Confidence:** HIGH (all four areas use proven, well-documented approaches)

**Scope:** This research covers ONLY new stack additions for v3.0. The existing validated stack (FastAPI, SQLAlchemy/asyncpg, arq/SAQ, Redis, mutagen read, librosa, essentia-tensorflow, pyacoustid, audfprint, Panako, rapidfuzz, litellm, HTMX/Jinja2/Tailwind/Alpine.js, Alembic, Docker Compose, httpx, beautifulsoup4, lxml) is not re-researched.

---

## New Dependencies

### 1. Discogs Cross-Service Linking

**No new dependencies required.** httpx (>=0.28.1, already in pyproject.toml) is the only library needed.

| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| httpx | >=0.28.1 (existing) | Async HTTP client to call discogsography's `/api/search` endpoint | Already a production dependency. Native async, timeout/retry support. Used in fingerprint.py for audfprint/Panako HTTP calls -- same pattern applies to discogsography. |
| rapidfuzz | >=3.14.3 (existing) | Fuzzy match tracklist tracks against Discogs search results | Already used in tracklist_matcher.py for 1001tracklists matching. Same `token_set_ratio` approach works for artist+title matching against Discogs releases. |

**How it works:**
- Discogsography exposes `GET /api/search?q={query}&types=artist,release` returning relevance-ranked results with PostgreSQL full-text search.
- Phaze calls this endpoint via httpx with artist+title from TracklistTrack records.
- rapidfuzz scores the returned results against phaze's local track data.
- No Discogs API key or OAuth needed -- discogsography is on the same private Docker network, no rate limiting required (or at most a courtesy delay).

**Integration pattern (same as fingerprint service):**
```python
# Existing pattern from fingerprint.py:
async with httpx.AsyncClient(base_url=settings.panako_url, timeout=30.0) as client:
    response = await client.post("/query", ...)

# Discogsography follows the same pattern:
async with httpx.AsyncClient(base_url=settings.discogsography_url, timeout=10.0) as client:
    response = await client.get("/api/search", params={"q": query, "types": "release"})
```

**Configuration addition to pydantic-settings:**
- `DISCOGSOGRAPHY_URL` environment variable (e.g., `http://discogsography-api:8000`)
- No API key needed -- private network, same Docker Compose stack or reachable via host network.

### 2. Tag Writing to Audio Files

**No new dependencies required.** mutagen (>=1.47.0, already in pyproject.toml) supports both read and write.

| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| mutagen | >=1.47.0 (existing) | Write corrected tags (artist, title, album, genre, year, track number) to destination copies | Already used for tag extraction in `services/metadata.py`. Mutagen's write API is symmetric to its read API. Supports ID3v2.4, Vorbis Comments, MP4 atoms, FLAC tags. |

**Write API patterns by format (all mutagen, no new imports):**

| Format | Tag Container | Write Method |
|--------|--------------|--------------|
| MP3 | `mutagen.id3.ID3` | `audio.tags.add(TIT2(text=["Title"]))` then `audio.save()` |
| M4A/MP4 | `mutagen.mp4.MP4` | `audio["\xa9nam"] = ["Title"]` then `audio.save()` |
| OGG/OPUS | `mutagen.oggvorbis.OggVorbis` / `mutagen.oggopus.OggOpus` | `audio["title"] = ["Title"]` then `audio.save()` |
| FLAC | `mutagen.flac.FLAC` | `audio["title"] = ["Title"]` then `audio.save()` |

**Critical safety considerations:**
- Write ONLY to destination copies (files in `proposed_path`), NEVER to originals. The existing copy-verify-delete protocol guarantees originals are preserved.
- Use `mutagen.File(path)` auto-detection (same as read path) to determine format.
- The existing `_ID3_MAP`, `_VORBIS_MAP`, `_MP4_MAP` dictionaries in `services/metadata.py` can be reversed for writing.
- Tag write is CPU-light -- use `asyncio.to_thread()` like the existing extraction path, no ProcessPoolExecutor needed.
- SHA256 re-verification after tag write to confirm file integrity (tags change the hash, so record the new hash).

### 3. CUE Sheet Generation

**Recommendation: Write CUE sheets directly -- no library needed.**

| Approach | Recommendation | Why |
|----------|---------------|-----|
| Custom CUE writer (string formatting) | **YES** | CUE format is trivial plain text. A 50-line function handles it. No library dependency for generating 10 lines of structured text. |
| cuetools (PyPI) | NO | Pydantic-based, actively maintained (v1.1.0, Jan 2026), but adds a dependency for something that is literally string concatenation. |
| CueParser (PyPI) | NO | v1.3.3 (Jan 2026), supports generation, but its `cuegen.py` tool is designed for different input formats (Audacity labels). More complexity than benefit. |

**CUE sheet format for phaze live sets:**

The CUE format is a simple text specification. A live set CUE sheet maps tracklist timestamps to positions in a single audio file:

```
PERFORMER "Artist Name"
TITLE "Live @ Coachella 2025.04.12"
FILE "Artist Name - Live @ Coachella 2025.04.12.mp3" MP3
  TRACK 01 AUDIO
    TITLE "Track One (Original Mix)"
    PERFORMER "Producer A"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Track Two (Remix)"
    PERFORMER "Producer B"
    INDEX 01 05:23:00
```

**Timestamp format:** `MM:SS:FF` where FF = frames (1/75th second). Phaze tracklist timestamps are in `HH:MM:SS` or seconds -- conversion is: `frames = 0` (we don't have sub-second precision), `minutes = total_minutes`, `seconds = remaining_seconds`.

**Data sources for timestamps (preference order):**
1. **Fingerprint timestamps** from `FingerprintResult` -- most accurate, derived from actual audio matching via audfprint/Panako. The `QueryMatch.timestamp` field already exists.
2. **1001Tracklists timestamps** from `TracklistTrack.timestamp` -- user-submitted, variable accuracy.
3. **Position-based estimation** -- fallback: divide total duration by track count for evenly-spaced positions.

**Why no library:**
- The CUE spec has ~10 keywords. Phaze uses exactly 5: `PERFORMER`, `TITLE`, `FILE`, `TRACK`, `INDEX`.
- No need to parse CUE sheets, only generate them.
- No CD-specific features needed (CATALOG, ISRC, PREGAP, POSTGAP, FLAGS).
- A simple `generate_cue_sheet(tracklist, file_record) -> str` function is cleaner than adapting a library's data model.

### 4. Search / Query Capabilities

**Use PostgreSQL built-in features -- no new dependencies.**

| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| PostgreSQL `pg_trgm` extension | (bundled with PG 16) | Trigram-based fuzzy text search | Handles typos and partial matches. `similarity()` and `%` operator. GIN index on `gin_trgm_ops` for fast lookups. No external service (Elasticsearch, Meilisearch) needed for 200K records. |
| PostgreSQL `to_tsvector` / `to_tsquery` | (built-in) | Full-text search with ranking | Stemming, language-aware search, `ts_rank` for relevance scoring. Combined with pg_trgm for typo tolerance. |
| SQLAlchemy `func` | (existing) | Call PG functions from Python | `func.similarity()`, `func.to_tsvector()`, `func.to_tsquery()`, `func.ts_rank()` -- all available via SQLAlchemy's `func` namespace. No ORM extensions needed. |

**Why PostgreSQL native search over alternatives:**

| Alternative | Why NOT |
|-------------|---------|
| Elasticsearch / OpenSearch | Separate service, Java/JVM, 1GB+ RAM overhead. Overkill for 200K records on a home server. PostgreSQL handles this scale trivially. |
| Meilisearch / Typesense | Additional Docker container, data sync complexity, another service to maintain. Adds operational burden for marginal benefit at this scale. |
| pgvector (semantic search) | Requires embedding generation (LLM calls for every record). Interesting for v4+ NLQ feature, not needed for structured field search. |
| SQLAlchemy-Searchable | Abandoned (last release 2021). Just use `func.to_tsvector()` directly -- it's 3 lines of SQLAlchemy. |

**Implementation approach:**

1. **Alembic migration:** Enable `pg_trgm` extension, add GIN indexes on searchable text columns.

```sql
-- In Alembic migration
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- GIN index for full-text search on metadata
CREATE INDEX ix_metadata_search ON metadata
  USING GIN (to_tsvector('english', coalesce(artist, '') || ' ' || coalesce(title, '') || ' ' || coalesce(album, '')));

-- GIN trigram indexes for fuzzy matching
CREATE INDEX ix_metadata_artist_trgm ON metadata USING GIN (artist gin_trgm_ops);
CREATE INDEX ix_metadata_title_trgm ON metadata USING GIN (title gin_trgm_ops);

-- Tracklist search indexes
CREATE INDEX ix_tracklists_artist_trgm ON tracklists USING GIN (artist gin_trgm_ops);
CREATE INDEX ix_tracklists_event_trgm ON tracklists USING GIN (event gin_trgm_ops);
CREATE INDEX ix_tracklist_tracks_artist_trgm ON tracklist_tracks USING GIN (artist gin_trgm_ops);
CREATE INDEX ix_tracklist_tracks_title_trgm ON tracklist_tracks USING GIN (title gin_trgm_ops);
```

2. **SQLAlchemy query patterns:**

```python
from sqlalchemy import func, or_

# Full-text search with ranking
query = (
    select(FileMetadata)
    .where(func.to_tsvector("english", FileMetadata.artist + " " + FileMetadata.title)
           .match(search_term))
    .order_by(func.ts_rank(
        func.to_tsvector("english", FileMetadata.artist + " " + FileMetadata.title),
        func.to_tsquery("english", search_term)
    ).desc())
)

# Fuzzy trigram search (handles typos)
query = (
    select(FileMetadata)
    .where(func.similarity(FileMetadata.artist, search_term) > 0.3)
    .order_by(func.similarity(FileMetadata.artist, search_term).desc())
)
```

3. **Search spans multiple tables:**

| Table | Searchable Fields | Index Type |
|-------|------------------|------------|
| `metadata` | artist, title, album, genre | tsvector + trgm |
| `tracklists` | artist, event | trgm |
| `tracklist_tracks` | artist, title | trgm |
| `analysis` | style, mood | exact match (low cardinality) |
| `analysis` | bpm | range query (no text index needed) |
| `files` | original_filename | trgm |

**Filterable (non-text) fields:** BPM range, year range, date range, file_type, state.

---

## Existing Dependencies -- No Changes Needed

| Dependency | Current Version | v3.0 Usage | Notes |
|------------|----------------|------------|-------|
| httpx | >=0.28.1 | Discogs service HTTP calls | Same pattern as fingerprint service |
| mutagen | >=1.47.0 | Tag writing (new) + tag reading (existing) | Write API is part of mutagen core, no extras |
| rapidfuzz | >=3.14.3 | Fuzzy matching Discogs results | Same `token_set_ratio` as tracklist matcher |
| SQLAlchemy | >=2.0.48 | `func.to_tsvector`, `func.similarity` | Native PG function support, no extensions |
| Alembic | >=1.18.4 | Migration for `pg_trgm` extension + indexes | Standard `op.execute()` for CREATE EXTENSION |
| pydantic-settings | >=2.13.1 | `DISCOGSOGRAPHY_URL` config | One new env var |

---

## Installation

```bash
# No new pip/uv dependencies for v3.0.
# All required libraries are already in pyproject.toml.

# Docker Compose: add network connectivity to discogsography
# (either same compose stack or external_links / shared network)

# Alembic migration: enable pg_trgm extension in PostgreSQL
# CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

---

## Alternatives Considered

| Category | Recommended | Alternative | Why Not |
|----------|-------------|-------------|---------|
| CUE generation | Custom writer (50 lines) | cuetools (PyPI) | Adding a dependency for string formatting is over-engineering. CUE format uses 5 keywords. |
| CUE generation | Custom writer | CueParser (PyPI) | CueParser's generation API is designed for Audacity label import, not programmatic generation from structured data. |
| Search | PostgreSQL pg_trgm + tsvector | Elasticsearch | Separate JVM service, 1GB+ RAM, sync complexity. 200K records is trivial for PostgreSQL. |
| Search | PostgreSQL pg_trgm + tsvector | Meilisearch | Extra container, data sync, operational overhead. Benefits (typo-tolerance, facets) already provided by pg_trgm. |
| Search | PostgreSQL pg_trgm + tsvector | SQLAlchemy-Searchable | Last release 2021, abandoned. Raw `func.to_tsvector()` is equally simple and always current. |
| Discogs client | httpx to discogsography | python3-discogs-client (PyPI) | Direct Discogs API calls would bypass discogsography's enriched data (Neo4j graph, cross-references, MusicBrainz links). Discogsography already has the data indexed and searchable. |
| Tag writing | mutagen (existing) | eyeD3 | ID3-only. Mutagen handles all formats (ID3, Vorbis, MP4, FLAC, OPUS). Already in the project. |
| Tag writing | mutagen (existing) | mediafile | Wrapper around mutagen. Adds abstraction layer over what we already use directly. |

---

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| python3-discogs-client | Bypasses discogsography service which has enriched, pre-indexed data. Would require Discogs API key, rate limiting (60 req/min), OAuth. Discogsography is on the same network with no rate limits. | httpx calls to discogsography `/api/search` |
| Elasticsearch / OpenSearch | Massive operational overhead for 200K records. PostgreSQL's pg_trgm + tsvector handles this scale with zero additional infrastructure. | PostgreSQL native search |
| eyeD3 | ID3-only (MP3). Does not support Vorbis (OGG/OPUS), MP4 (M4A), or FLAC. | mutagen (already handles all formats) |
| cuetools / CueParser | Adding a PyPI dependency for 50 lines of string formatting. CUE generation is trivial. | Custom `generate_cue_sheet()` function |
| pgvector | Requires generating embeddings for every record via LLM. Interesting for semantic/NLQ search in v4+, not for structured field search in v3.0. | pg_trgm for fuzzy text, tsvector for full-text |
| SQLAlchemy-Searchable | Abandoned since 2021. Wraps the same `func.to_tsvector()` calls you'd write directly. Dead dependency adds risk for zero benefit. | Direct `func.to_tsvector()` / `func.similarity()` calls |
| mediafile (PyPI) | Thin wrapper over mutagen. Adds an abstraction layer and another dependency for no benefit when you already use mutagen directly. | mutagen directly |

---

## Version Compatibility

| Technology | Compatible With | Notes |
|------------|-----------------|-------|
| pg_trgm extension | PostgreSQL 16+ | Bundled with PostgreSQL, just needs `CREATE EXTENSION`. No version conflicts. |
| `to_tsvector` / `ts_rank` | PostgreSQL 16+, SQLAlchemy >=2.0 | Native PG functions, called via `func.*` in SQLAlchemy. Well-tested pattern. |
| mutagen write API | Python 3.13, mutagen >=1.47.0 | Write API stable since mutagen 1.x. Same `mutagen.File()` auto-detection used for reading. |
| httpx | Python 3.13, asyncio | Already validated across fingerprint service and test suite. |
| Discogsography API | FastAPI service on private network | Endpoint: `GET /api/search?q=...&types=release`. Returns JSON with `results[]`, `total`, `facets`. Rate limited at 30/min on discogsography side but configurable. |

---

## Confidence Assessment

| Area | Confidence | Reasoning |
|------|------------|-----------|
| Discogs linking (httpx to discogsography) | HIGH | Same pattern as existing fingerprint service HTTP calls. Discogsography API verified (routers/search.py inspected). httpx already battle-tested in project. |
| Tag writing (mutagen) | HIGH | Mutagen's write API is symmetric to its read API which is already in production. Well-documented, zero new dependencies. Format-specific write patterns are standard. |
| CUE sheet generation (custom) | HIGH | CUE format is a trivial text spec with 5 keywords. No library needed. Data sources (fingerprint timestamps, tracklist positions) already exist in the database. |
| Search (pg_trgm + tsvector) | HIGH | PostgreSQL native features, well-documented SQLAlchemy integration. Discogsography project already uses the same approach successfully. 200K records is well within PG's comfort zone. |

---

## Sources

- [PostgreSQL pg_trgm documentation](https://www.postgresql.org/docs/current/pgtrgm.html) -- trigram similarity, GIN index operators
- [PostgreSQL Full Text Search](https://www.postgresql.org/docs/current/textsearch.html) -- tsvector, tsquery, ts_rank
- [SQLAlchemy PostgreSQL FTS patterns](https://amitosh.medium.com/full-text-search-fts-with-postgresql-and-sqlalchemy-edc436330a0c) -- func.to_tsvector integration
- [SQLAlchemy pg_trgm discussion](https://github.com/sqlalchemy/sqlalchemy/discussions/7641) -- similarity() function usage
- [CUE Sheet Format Specification](https://wyday.com/cuesharp/specification.php) -- complete CUE keyword reference
- [CUE sheet Wikipedia](https://en.wikipedia.org/wiki/Cue_sheet_(computing)) -- format overview, MSF timestamp format
- [cuetools on PyPI](https://pypi.org/project/cuetools/) -- v1.1.0 (Jan 2026), Pydantic-based, evaluated and rejected
- [CueParser on PyPI](https://pypi.org/project/CueParser/) -- v1.3.3 (Jan 2026), evaluated and rejected
- [mutagen documentation](https://mutagen.readthedocs.io/) -- tag write API, format-specific containers
- [mutagen on PyPI](https://pypi.org/project/mutagen/) -- v1.47.0 verified
- Discogsography source code inspected: `api/routers/search.py` (search endpoint), `CLAUDE.md` (architecture)

---
*Stack research for: Phaze v3.0 Cross-Service Intelligence & File Enrichment*
*Researched: 2026-04-02*
