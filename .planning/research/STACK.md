# Stack Research: v2.0 New Capabilities

**Domain:** Audio tag extraction, 1001tracklists scraping, audio fingerprinting (audfprint + Panako hybrid), fingerprint service container
**Researched:** 2026-03-30
**Confidence:** MEDIUM (audfprint compatibility uncertain, Panako API wrapper is custom work)

**Scope:** This research covers ONLY new stack additions for v2.0. The existing validated stack (FastAPI, SQLAlchemy/asyncpg, arq/Redis, litellm, essentia-tensorflow, HTMX/Jinja2/Tailwind, Alembic, Docker Compose, pyacoustid/chromaprint) is not re-researched.

---

## New Dependencies

### Audio Tag Extraction

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| mutagen | >=1.47.0 | Read/write audio metadata (ID3v1/v2, Vorbis, MP4, FLAC, OGG, AIFF) | Only Python library with both read AND write support. Zero dependencies. Stable API -- 1.47.0 is current (no release since mid-2024, but the library is mature and complete). Already in v1.0 STACK research, just not yet added to pyproject.toml. Use `mutagen.File()` auto-detection, never format-specific classes directly. |

**Integration with existing stack:**
- mutagen is CPU-light (just reads binary tag headers), so it can run in async context via `asyncio.to_thread()` without needing ProcessPoolExecutor.
- Extract tags into the existing `FileMetadata` SQLAlchemy model (table already exists from v1.0 migration 001).
- Run as an arq task during ingestion, before analysis and LLM proposal steps, so extracted tags feed into LLM context.

### Web Scraping (1001tracklists)

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| httpx | >=0.28.1 | Async HTTP client for 1001tracklists requests | Already a dev dependency (test client). Promote to production dependency. Native async support fits the async stack. Supports custom headers, cookies, timeouts, retries. No need for a separate requests library. |
| beautifulsoup4 | >=4.14.3 | HTML parsing for 1001tracklists pages | Industry standard HTML parser. All existing 1001tracklists scrapers (leandertolksdorf, Tel0sity, GodLesZ) use BeautifulSoup. Well-tested with malformed HTML. Mature, stable, well-typed. |
| lxml | >=5.3.0 | Fast HTML/XML parser backend for BeautifulSoup | Use as BeautifulSoup's parser (`"lxml"` backend) for 5-10x speed over the default html.parser. C-based, handles malformed HTML well. Already the standard recommendation for production BS4 usage. |

**Why NOT use existing scrapers from GitHub:**
- leandertolksdorf/1001-tracklists-api: Minimal, no search support, unclear maintenance.
- Tel0sity/1001-tracklist-scraper: Jupyter notebook, not a library.
- ryin1/1001tracklists: Single commit from 2017, abandoned.
- All three are incomplete and unmaintained. Build a focused scraper module using httpx + BS4 directly. PROJECT.md confirms "documented HTTP endpoints for search (POST) and detail pages (POST) -- no headless browser needed."

**Anti-scraping considerations:**
- 1001tracklists.com rate-limits aggressively. Implement exponential backoff and respect rate limits.
- Use realistic User-Agent headers via httpx.
- Add configurable delay between requests (default 2-5 seconds).
- Store scraped data in PostgreSQL to avoid re-scraping.
- No need for Selenium/Playwright/headless browser -- POST endpoints work with standard HTTP clients.

### Audio Fingerprinting: audfprint

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| audfprint | master (vendored/forked) | Landmark-based audio fingerprinting | Dan Ellis's (Columbia) landmark algorithm. Python-native, uses numpy/scipy for spectrogram + peak-finding. Fingerprint database is in-memory numpy arrays serialized to compressed files. Good for matching short segments against a corpus of full tracks. **Not on PyPI** -- must vendor or install from git. |

**Critical compatibility concerns (MEDIUM confidence):**
- Last commit: April 2015. Repository has not been updated in 11 years.
- Dependencies: numpy, scipy, docopt, joblib, psutil. All support Python 3.13, but audfprint code itself may use deprecated Python 2 patterns.
- No setup.py or pyproject.toml -- it is a collection of scripts, not a proper package.
- Uses `docopt` for CLI parsing (still works but unmaintained).
- **Recommendation:** Fork into the project as a vendored module under `src/phaze/fingerprint/audfprint/`. Strip the CLI layer, extract the core `Analyzer`, `Matcher`, and `HashTable` classes. Adapt to modern Python 3.13. This is manageable because the core algorithm is roughly 1500 lines across 5 files.

**audfprint dependencies to add:**
| Library | Version | Purpose | Notes |
|---------|---------|---------|-------|
| scipy | >=1.14.0 | Signal processing for spectrogram/peak-finding | Already pulled in by essentia-tensorflow or librosa if present. Verify no version conflicts. |
| joblib | >=1.4.0 | Parallel processing of fingerprint operations | Small, stable library. Used by audfprint for parallel file processing. |

**Note:** numpy is already a dependency (in pyproject.toml as `>=1.26.0`). docopt and psutil are NOT needed if we vendor and strip the CLI.

### Audio Fingerprinting: Panako

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| Panako | 2.1 (Docker) | Tempo-robust acoustic fingerprinting | Java-based. Handles time-stretching and pitch-shifting -- critical for matching live concert recordings where tempo may differ from studio versions. Uses LMDB for fingerprint storage. Complements audfprint: audfprint is fast for exact/near-exact matches, Panako handles tempo-modified audio. |
| OpenJDK | 17+ (in container) | Java runtime for Panako | JDK 17 LTS is the minimum for Panako 2.1. Use Eclipse Temurin base image. |
| FFmpeg | 7+ (in container) | Audio decoding for Panako | Required by Panako for reading audio formats. Already in the main Docker image; also needed in the Panako container. |

**Panako container architecture:**
- Panako is CLI-only -- **no built-in REST API or HTTP server**. The v2.1 documentation confirms only CLI subcommands: `store`, `query`, `monitor`, `stats`, `delete`, `resolve`, `same`, `config`.
- **The fingerprint service container must wrap Panako with a custom API.** Two options:

| Approach | Pros | Cons | Recommendation |
|----------|------|------|----------------|
| Python FastAPI sidecar calling Panako CLI via subprocess | Reuses existing FastAPI patterns, Python-native, easy to integrate with arq | Subprocess overhead per call, JVM startup time per invocation | NO -- JVM startup is 2-5 seconds per call, unacceptable for 200K files |
| Long-running Java process with thin HTTP API | JVM starts once, LMDB stays open, sub-second queries | Requires writing a small Java HTTP wrapper or using Javalin/Spark | **YES** -- use Javalin (lightweight Java HTTP framework) to wrap Panako's core library as a persistent service |
| Long-running Panako process with Python FastAPI proxy reading LMDB directly | No Java wrapper needed | LMDB is single-writer; concurrent access from Python is fragile | NO -- LMDB locking issues |

**Recommended Panako container stack:**

| Technology | Version | Purpose | Notes |
|------------|---------|---------|-------|
| Javalin | 6.x | Lightweight Java HTTP framework | Minimal overhead. Minimal routes: POST /store, POST /query, GET /stats. Wraps Panako's Java API directly without subprocess. |
| Eclipse Temurin | 17-jre | Java runtime base image | Smaller than full JDK for production. ~200MB image. |
| LMDB | (bundled with Panako) | Fingerprint key-value store | Persistent volume mount at `/data/panako`. |

**Panako service API design (custom):**

```
POST /store     -- body: multipart audio file -> store fingerprints, return ID
POST /query     -- body: multipart audio file -> return matches with scores/offsets
GET  /stats     -- return database statistics
DELETE /{id}    -- remove fingerprint by ID
GET  /health    -- health check for Docker Compose
```

**Communication from Python workers:**
- Python arq workers call the Panako service via httpx (async HTTP).
- No message queue needed between services -- direct HTTP is simpler and sufficient for single-user workload.
- Timeout: set 30-second timeout for query operations on long audio files.

### Fingerprint Service Container (Hybrid Orchestration)

The fingerprint service is a Docker Compose service that orchestrates both audfprint (in-process Python) and Panako (HTTP sidecar).

| Component | Runs In | Communication |
|-----------|---------|---------------|
| audfprint | Python worker process (vendored module) | Direct function call from arq task |
| Panako | Separate Docker container (Java + Javalin) | HTTP via httpx from arq worker |
| Scoring | Python worker process | Combines audfprint + Panako results with weighted scoring |

**Weighted scoring approach:**
- audfprint: fast, high confidence for exact matches. Weight: 0.6 for exact, 0.3 for partial.
- Panako: slower, handles tempo/pitch shifts. Weight: 0.4 for exact, 0.7 for tempo-modified.
- Combined score threshold determines match confidence (configurable).

---

## Existing Dependencies to Promote/Adjust

| Dependency | Current Status | Change Needed |
|------------|----------------|---------------|
| httpx | Dev dependency (>=0.28.1) | Promote to production dependency -- needed for 1001tracklists scraping and Panako service communication |
| numpy | Production (>=1.26.0) | No change -- already satisfies audfprint needs |

---

## Installation

```bash
# New production dependencies for v2.0
uv add mutagen beautifulsoup4 lxml joblib

# Promote httpx from dev to production
uv add httpx

# System dependencies (Dockerfile additions)
# No new system deps for main container -- ffmpeg and chromaprint-tools already present

# Panako container (separate Dockerfile)
# Base: eclipse-temurin:17-jre
# Install: ffmpeg, panako.jar, javalin wrapper
# Volume: /data/panako for LMDB persistence
```

**audfprint is vendored, not installed as a package:**
```
src/phaze/fingerprint/audfprint/
    __init__.py
    analyzer.py      # extracted from audfprint_analyze.py
    matcher.py       # extracted from audfprint_match.py
    hash_table.py    # extracted from hash_table.py
    peak_finder.py   # extracted from stft.py
```

---

## Alternatives Considered

| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| beautifulsoup4 + lxml | selectolax (lexbor backend) | If scraping 100K+ pages where 5-30x parse speed matters. We scrape hundreds of tracklist pages, not millions. BS4's robustness with malformed HTML and mature ecosystem wins over raw speed. |
| beautifulsoup4 + lxml | parsel (Scrapy's parser) | If you want XPath + CSS selectors without full Scrapy. BS4 is simpler for our focused use case (extract tracklist tables from known page structure). |
| httpx | aiohttp | If you need WebSocket support. httpx is already in the project, has a cleaner API, and handles our needs (POST requests with headers). |
| audfprint (vendored) | dejavu | If you want a more packaged solution. dejavu uses MySQL, adds database complexity, and hasn't been updated since 2021. audfprint's numpy-based hash table is simpler and fits our architecture better. |
| audfprint (vendored) | chromaprint/pyacoustid alone | If you only need track identification via AcoustID web service. pyacoustid is already a dependency. But audfprint provides LOCAL fingerprint matching against our own corpus without external API calls -- essential for matching segments of live sets against known tracks. |
| Panako (Java container) | olaf (Panako author's Rust rewrite) | If olaf matures. Joren Six is rewriting Panako in Rust as "olaf" but it is experimental. Panako 2.1 is proven and documented. Monitor olaf for v3.0+. |
| Javalin (Panako wrapper) | Spring Boot | Massively over-engineered for 4 endpoints. Javalin is minimal, Spring Boot is 30MB+. |
| Javalin (Panako wrapper) | Python subprocess calls | JVM startup per call (2-5 sec) is unacceptable at scale. Long-running JVM with HTTP API eliminates this. |
| Direct HTTP (Python to Panako) | Redis message queue | Adds complexity for no benefit. Single-user app with sequential fingerprinting doesn't need async message passing between services. Direct HTTP with retries is simpler and sufficient. |

---

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| Selenium/Playwright for 1001tracklists | Headless browser is massive overhead. PROJECT.md confirms POST endpoints work without JS rendering. Adds 500MB+ to Docker image. | httpx + beautifulsoup4 with POST requests |
| scrapy for 1001tracklists | Full crawling framework is overkill for scraping one site with known endpoints. Adds spider infrastructure, middleware, pipelines. | httpx + beautifulsoup4 -- focused and simple |
| fake-useragent library | Unnecessary complexity. A single realistic hardcoded User-Agent string works fine for a personal tool making occasional requests. | Hardcoded User-Agent in httpx headers |
| dejavu (audio fingerprinting) | Requires MySQL, last updated 2021, different architecture assumptions. | audfprint (vendored) for landmark matching |
| audfprint via pip install | Not on PyPI. Installing from git URL adds fragile dependency on an unmaintained 2015 repo. | Vendor the core modules, adapt to Python 3.13 |
| LMDB Python bindings for direct Panako DB access | LMDB is single-writer. Python reading while Java writes causes locking issues. | HTTP API to Panako container -- clean separation |
| GraalVM for Panako | Adds complexity for marginal startup improvement. Panako runs as a long-lived service, so startup time is irrelevant. | Standard Eclipse Temurin JRE |

---

## Version Compatibility

| Package A | Compatible With | Notes |
|-----------|-----------------|-------|
| mutagen >=1.47.0 | Python 3.13 | Pure Python, no C extensions. Zero compatibility concerns. |
| beautifulsoup4 >=4.14.3 | Python 3.13, lxml >=5.3.0 | Use `BeautifulSoup(html, "lxml")` for speed. Falls back to `html.parser` if lxml unavailable. |
| lxml >=5.3.0 | Python 3.13 | Wheels available for 3.13 on all platforms. C extension, but pre-built wheels eliminate build issues. |
| httpx >=0.28.1 | Python 3.13, anyio >=4.0 | Already validated in v1.0 test suite. |
| scipy >=1.14.0 | Python 3.13, numpy >=1.26.0 | scipy 1.14+ supports Python 3.13. Verify no conflicts with essentia-tensorflow's numpy pin. |
| joblib >=1.4.0 | Python 3.13 | Pure Python with optional C speedups. No compatibility concerns. |
| Panako 2.1 | Java 17+, LMDB, FFmpeg | Self-contained in Docker. No Python version dependency. |
| Javalin 6.x | Java 17+ | Lightweight, minimal dependencies. Package as fat JAR with Panako. |

---

## Confidence Assessment

| Area | Confidence | Reasoning |
|------|------------|-----------|
| mutagen (tag extraction) | HIGH | Mature, stable, zero-dependency, well-documented. Already researched in v1.0. |
| httpx + BS4 (1001tracklists scraping) | HIGH | Standard Python scraping stack. httpx already in project. BS4 is battle-tested. |
| audfprint (vendored) | MEDIUM | Core algorithm is solid (academic, well-cited), but code is from 2015. Vendoring and modernizing adds development effort. Need to verify Python 3.13 compatibility of the numpy/scipy calls. |
| Panako (Docker container) | MEDIUM | Proven fingerprinting system, Docker support documented. But wrapping with Javalin HTTP API is custom work -- no existing examples. Need to assess Panako's Java API (not just CLI). |
| Javalin wrapper for Panako | LOW | Technically straightforward but untested. Need to verify Panako exposes usable Java classes (not just a CLI main()). If Panako is tightly coupled to its CLI, may need to call CLI from Java code instead of using library API. |
| Hybrid scoring (audfprint + Panako) | LOW | The weighted scoring approach is a design decision, not a proven pattern. Will need tuning with real data. Flag for deeper research during implementation phase. |

---

## Sources

- [audfprint GitHub (dpwe/audfprint)](https://github.com/dpwe/audfprint) -- landmark fingerprinting, last commit 2015, dependencies: numpy, scipy, docopt, joblib, psutil
- [Panako GitHub (JorenSix/Panako)](https://github.com/JorenSix/Panako) -- v2.1, Java, LMDB, Docker support, CLI-only (no REST API)
- [Panako 2.1 documentation](https://0110.be/releases/Panako/Panako-2.1/readme.html) -- confirmed CLI-only interface, LMDB backend
- [Pixelartist/docker-panako](https://github.com/Pixelartist/docker-panako) -- community Docker setup for Panako
- [mutagen on PyPI](https://pypi.org/project/mutagen/) -- v1.47.0 current, pure Python
- [beautifulsoup4 on PyPI](https://pypi.org/project/beautifulsoup4/) -- v4.14.3 (Nov 2025)
- [leandertolksdorf/1001-tracklists-api](https://github.com/leandertolksdorf/1001-tracklists-api) -- evaluated and rejected (incomplete, unmaintained)
- [Tel0sity/1001-tracklist-scraper](https://github.com/Tel0sity/1001-tracklist-scraper) -- evaluated and rejected (Jupyter notebook, not a library)
- [ScrapingBee: Best Python Web Scraping Libraries 2025](https://www.scrapingbee.com/blog/best-python-web-scraping-libraries/) -- httpx + BS4 recommended for async scraping
- [Panako JOSS paper](https://www.theoj.org/joss-papers/joss.04554/10.21105.joss.04554.pdf) -- tempo-robust fingerprinting algorithm details

---
*Stack research for: Phaze v2.0 new capabilities*
*Researched: 2026-03-30*
