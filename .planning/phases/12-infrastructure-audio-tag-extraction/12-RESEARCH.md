# Phase 12: Infrastructure & Audio Tag Extraction - Research

**Researched:** 2026-03-30
**Domain:** Shared async engine pooling, audio tag extraction (mutagen), Alembic migrations, arq task patterns
**Confidence:** HIGH

## Summary

Phase 12 has two distinct workstreams: (1) infrastructure improvements -- replacing the per-invocation engine creation in `get_task_session()` with a shared async engine pool, and (2) audio tag extraction -- using mutagen to read ID3/Vorbis/MP4/FLAC/OPUS tags from music and video files, storing them in an expanded FileMetadata model, and feeding tag data into LLM proposal context.

The existing codebase already has the FileMetadata model with artist/title/album/year/genre/raw_tags columns, the FileState enum with METADATA_EXTRACTED defined, and established arq task patterns. The work primarily involves: fixing `tasks/session.py` to share a pooled engine, adding three columns to FileMetadata (track_number, duration, bitrate), writing a mutagen-based extraction service, creating a new arq task function, adding a manual trigger API endpoint, updating `build_file_context()` to include tag data, and updating the pipeline dashboard.

**Primary recommendation:** Use `mutagen.File()` with `easy=True` for normalized tag access across formats, falling back to raw `mutagen.File()` for the full tag dump. Share the async engine via arq's `ctx` dict, initialized in the worker `startup` hook alongside the existing process pool.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Tag extraction and audio analysis run in parallel from DISCOVERED state. They are independent operations.
- **D-02:** Convergence gate before proposal generation uses a dual state check: proposal generation queries for files where BOTH FileMetadata row exists AND AnalysisResult row exists. No new composite state needed.
- **D-03:** FileState enum already includes METADATA_EXTRACTED and FINGERPRINTED. Tag extraction transitions files to METADATA_EXTRACTED independently of the ANALYZED transition.
- **D-04:** Queue all files for tag extraction regardless of current state. Simple, complete, idempotent.
- **D-05:** Existing proposals are not regenerated with tag data. Only future proposal generation includes tag context.
- **D-06:** Tag data nested under a 'tags' key in the file context dict.
- **D-07:** Include full raw_tags dump alongside curated fields (artist, title, album, year, genre, track_number, duration, bitrate).
- **D-08:** No prompt template changes. The LLM decides what is useful from available context.
- **D-09:** Both auto and manual triggers. Tag extraction jobs enqueued automatically during scan (new files), plus a manual API endpoint for backfill and re-extraction.
- **D-10:** Extract tags from music and video files (not companion files).
- **D-11:** Files with no tags get an empty FileMetadata row (all null fields, empty raw_tags). File transitions to METADATA_EXTRACTED regardless.

### Claude's Discretion
- Shared async engine pool implementation (worker startup hook, module singleton, etc.)
- FileMetadata model additions (track_number, duration, bitrate columns)
- Alembic migration for new columns
- mutagen integration details (format detection, error handling for corrupt files)
- Manual extraction API endpoint design
- arq task function structure for tag extraction
- Pipeline dashboard updates to show extraction status

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| INFRA-01 | Task session uses a shared async engine pool instead of creating a new engine per invocation | Shared engine via arq ctx dict, initialized in worker startup hook, replacing get_task_session() per-invocation pattern |
| INFRA-02 | FileRecord state machine expanded with METADATA_EXTRACTED and FINGERPRINTED states, all consumers updated atomically | States already exist in FileState enum; pipeline service PIPELINE_STAGES list needs updating; proposal generation gate needs dual-state check |
| TAGS-01 | User can trigger tag extraction that reads ID3/Vorbis/MP4/FLAC/OPUS tags from all music files | mutagen.File() auto-detects format; new arq task + manual API endpoint + auto-enqueue during scan |
| TAGS-02 | Extracted tags populate FileMetadata with artist, title, album, year, genre, track number | mutagen tag key mapping per format; upsert into FileMetadata model |
| TAGS-03 | Full raw tag dump stored in FileMetadata.raw_tags JSONB column | Serialize all mutagen tags to dict, handling non-JSON-serializable values |
| TAGS-04 | Duration and bitrate extracted from audio file info and stored in FileMetadata | mutagen FileType.info.length and info.bitrate; new columns needed |
| TAGS-05 | LLM proposal context includes extracted tag data for richer filename/path proposals | Update build_file_context() to query FileMetadata and nest under 'tags' key |
</phase_requirements>

## Project Constraints (from CLAUDE.md)

- Python 3.13 exclusively, `uv` only (never bare pip/python/pytest/mypy)
- 150-char line length, double quotes, type hints on all functions
- Strict mypy (disallow_untyped_defs, disallow_incomplete_defs, etc.) excluding tests
- Pre-commit hooks with frozen SHAs must pass
- 85% minimum code coverage
- Every feature gets its own git worktree and PR
- CI follows discogsography pattern (reusable workflows)
- `uv run` prefix for all tool commands

## Standard Stack

### Core (already in project)
| Library | Version | Purpose | Status |
|---------|---------|---------|--------|
| mutagen | >=1.47.0 | Audio metadata read/write | Listed in CLAUDE.md stack, NOT yet in pyproject.toml dependencies -- must be added |
| SQLAlchemy | >=2.0.48 | ORM / async engine pooling | Already installed |
| asyncpg | >=0.31.0 | PostgreSQL async driver | Already installed |
| Alembic | >=1.18.4 | Database migrations | Already installed |
| arq | >=0.27.0 | Async task queue | Already installed |
| FastAPI | >=0.135.2 | API endpoints | Already installed |

### New Dependency
| Library | Version | Purpose | Notes |
|---------|---------|---------|-------|
| mutagen | >=1.47.0 | Tag extraction from ID3/Vorbis/MP4/FLAC/OPUS/AIFF | Zero dependencies, pure Python, supports Python 3.13. Must add to pyproject.toml [project] dependencies. |

**Installation:**
```bash
# Add to pyproject.toml dependencies then:
uv sync
```

## Architecture Patterns

### Recommended New Files
```
src/phaze/
├── services/
│   └── metadata.py          # mutagen extraction logic (extract_tags function)
├── tasks/
│   └── metadata_extraction.py  # arq task function (extract_file_metadata)
└── routers/
    └── (pipeline.py update)  # manual extraction trigger endpoint
```

### Pattern 1: Shared Async Engine in Worker Context
**What:** Replace per-invocation `create_async_engine()` in `get_task_session()` with a shared engine created once in worker startup and stored in arq's `ctx` dict.
**Why:** Current `get_task_session()` creates a new engine (and connection pool) for every single task invocation. With 200K files, that means 200K engine creations. This causes connection exhaustion under concurrent load.
**Example:**
```python
# src/phaze/tasks/worker.py -- startup hook addition
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

async def startup(ctx: dict[str, Any]) -> None:
    # ... existing startup code ...

    # Shared engine pool for all task functions
    engine = create_async_engine(
        settings.database_url,
        echo=settings.debug,
        pool_size=10,
        max_overflow=5,
    )
    ctx["async_session"] = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    ctx["engine"] = engine

async def shutdown(ctx: dict[str, Any]) -> None:
    # ... existing shutdown code ...
    engine = ctx.get("engine")
    if engine is not None:
        await engine.dispose()
```

**Session usage in tasks:**
```python
# Instead of: session = await get_task_session()
# Use:
async with ctx["async_session"]() as session:
    # ... do work ...
    await session.commit()
```

### Pattern 2: mutagen Tag Extraction Service
**What:** A pure function that takes a file path, uses `mutagen.File()` to auto-detect format, extracts normalized fields + raw tag dump, returns a typed dict.
**Why:** Keeps mutagen logic isolated from arq task concerns. Testable without database or task queue.
**Example:**
```python
# src/phaze/services/metadata.py
from typing import Any
from pathlib import Path
import mutagen

class ExtractedTags:
    """Result of tag extraction from an audio file."""
    artist: str | None
    title: str | None
    album: str | None
    year: int | None
    genre: str | None
    track_number: int | None
    duration: float | None
    bitrate: int | None
    raw_tags: dict[str, Any]

def extract_tags(file_path: str) -> ExtractedTags:
    """Extract audio tags from a file using mutagen.

    Uses mutagen.File() for auto-format detection.
    Returns ExtractedTags with normalized fields + raw dump.
    Returns empty result (all None, empty raw_tags) for unreadable files.
    """
    audio = mutagen.File(file_path)
    if audio is None:
        return ExtractedTags(...)  # all None, empty raw_tags

    # Extract stream info (always available on FileType)
    duration = audio.info.length if audio.info else None
    bitrate = getattr(audio.info, "bitrate", None)

    # Extract normalized tag fields (format-dependent mapping)
    artist, title, album, year, genre, track_number = _extract_normalized(audio)

    # Full raw tag dump
    raw_tags = _serialize_tags(audio.tags)

    return ExtractedTags(...)
```

### Pattern 3: Format-Specific Tag Key Mapping
**What:** Different audio formats use different tag key names for the same data. Mutagen exposes format-native keys.
**Why:** Must handle all formats the project supports (mp3, flac, ogg, m4a, opus, wav, aiff, wma, aac).

| Field | ID3 (MP3/AIFF) | Vorbis (OGG/FLAC/OPUS) | MP4 (M4A) | ASF (WMA) |
|-------|-----------------|-------------------------|-----------|-----------|
| artist | TPE1 | artist | \xa9ART | Author |
| title | TIT2 | title | \xa9nam | Title |
| album | TALB | album | \xa9alb | WM/AlbumTitle |
| year | TDRC | date | \xa9day | WM/Year |
| genre | TCON | genre | \xa9gen | WM/Genre |
| track | TRCK | tracknumber | trkn | WM/TrackNumber |

**Recommended approach:** Use `mutagen.File(path, easy=True)` which normalizes keys for ID3 and MP4. For formats where easy mode is not available, map manually. The `easy=True` flag makes ID3 tags accessible via simple string keys like `"artist"`, `"title"`, `"album"`, `"date"`, `"genre"`, `"tracknumber"`.

**However, `easy=True` loses raw tag data.** The recommended pattern is:
1. Open with `mutagen.File(path)` (raw mode) for `raw_tags` dump and `info` access
2. Use a tag key mapping function for normalized field extraction
3. This avoids opening the file twice

### Pattern 4: arq Task Function for Tag Extraction
**What:** Follow the exact pattern from `process_file()` in `tasks/functions.py` -- same session management, retry logic, upsert pattern.
**Example:**
```python
async def extract_file_metadata(ctx: dict[str, Any], file_id: str) -> dict[str, Any]:
    """Extract audio tags from a single file and store in FileMetadata."""
    try:
        async with ctx["async_session"]() as session:
            # 1. Fetch file record
            # 2. Skip companion files (per D-10)
            # 3. Call extract_tags(file_record.current_path) -- sync, I/O-bound
            # 4. Upsert FileMetadata row
            # 5. Transition state to METADATA_EXTRACTED
            await session.commit()
    except Exception as exc:
        raise Retry(defer=ctx["job_try"] * 5) from exc
```

**Note on sync vs async:** mutagen reads file headers synchronously. For I/O-bound header reads this is fine -- no need for `run_in_executor` or process pool. The reads are small (a few KB from file headers), not full-file reads.

### Pattern 5: Dual-State Convergence Gate (D-02)
**What:** Proposal generation must wait for BOTH tag extraction AND audio analysis to complete before proceeding.
**How:** Modify the proposal trigger query to check for files where both a FileMetadata row and an AnalysisResult row exist, rather than checking a single state value.
```python
# In pipeline router or service:
stmt = (
    select(FileRecord)
    .where(FileRecord.state.in_([FileState.ANALYZED, FileState.METADATA_EXTRACTED]))
    .where(
        exists(select(FileMetadata.id).where(FileMetadata.file_id == FileRecord.id))
    )
    .where(
        exists(select(AnalysisResult.id).where(AnalysisResult.file_id == FileRecord.id))
    )
)
```

### Anti-Patterns to Avoid
- **Creating engine per task invocation:** Current `get_task_session()` pattern. Creates connection pool churn and risks exhaustion under 200K file load.
- **Using `easy=True` for raw tag dump:** Easy mode strips non-standard tags. Use raw mode for `raw_tags`, normalize manually for curated fields.
- **Blocking event loop with large file reads:** Not an issue here -- mutagen reads only headers (small), not full audio data.
- **Storing binary tag values in JSONB:** Some tags contain binary data (cover art, etc.). Must serialize to string or skip binary values.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Audio format detection | File extension matching | `mutagen.File()` auto-detect | Handles edge cases (wrong extension, container formats) |
| Tag key normalization | Manual if/else per format | Tag key mapping dict per format class | mutagen has ~10 format-specific tag classes; a mapping dict is cleaner than cascading conditionals |
| Connection pooling | Manual pool management | SQLAlchemy `create_async_engine` with pool_size/max_overflow | Built-in pool with health checks, overflow, recycling |
| JSONB serialization of tags | Manual recursive dict builder | Simple dict comprehension with str() fallback for non-serializable values | mutagen tag values can be lists, binary, or custom types |

## Common Pitfalls

### Pitfall 1: mutagen Tag Values Are Lists
**What goes wrong:** Accessing `audio.tags["TPE1"]` returns an ID3 frame object, not a string. Vorbis comments return lists of strings, not single strings.
**Why it happens:** Most tag formats support multiple values per key.
**How to avoid:** Always extract the first value: `tags.get("artist", [None])[0]` for Vorbis, or `str(tags.get("TPE1", ""))` for ID3.
**Warning signs:** TypeError when trying to store a list in a Text column.

### Pitfall 2: Non-JSON-Serializable Tag Values
**What goes wrong:** `json.dumps(dict(audio.tags))` fails because mutagen tag values include custom frame objects (ID3), bytes (cover art), and specialized types.
**Why it happens:** mutagen preserves format-native types.
**How to avoid:** Build a serialization function that converts all values to strings or lists of strings, skipping binary data (APIC frames, cover art). Use `str()` as fallback.
**Warning signs:** `TypeError: Object of type ... is not JSON serializable`.

### Pitfall 3: Corrupt or Truncated Files
**What goes wrong:** `mutagen.File()` raises `mutagen.MutagenError` or returns `None` for corrupt files.
**Why it happens:** Real-world music collections have partially downloaded files, truncated transfers, corrupted headers.
**How to avoid:** Wrap in try/except, return empty ExtractedTags on failure. Per D-11, still create empty FileMetadata row and transition to METADATA_EXTRACTED.
**Warning signs:** MutagenError exceptions in logs.

### Pitfall 4: Engine Disposal on Worker Shutdown
**What goes wrong:** Shared engine is not disposed on worker shutdown, leaving orphaned connections.
**Why it happens:** Easy to forget cleanup when adding to startup hook.
**How to avoid:** Always add corresponding `await engine.dispose()` in the shutdown hook.
**Warning signs:** PostgreSQL `max_connections` errors after worker restarts.

### Pitfall 5: Pipeline Stats Not Showing New States
**What goes wrong:** METADATA_EXTRACTED state files don't appear in pipeline dashboard.
**Why it happens:** `PIPELINE_STAGES` list in `services/pipeline.py` only includes DISCOVERED, ANALYZED, PROPOSAL_GENERATED, APPROVED, EXECUTED.
**How to avoid:** Add METADATA_EXTRACTED (and FINGERPRINTED for future use) to PIPELINE_STAGES.
**Warning signs:** Dashboard stats don't add up to total file count.

### Pitfall 6: Proposal Generation Gate Regression
**What goes wrong:** Proposal trigger still only checks for ANALYZED state, missing files that were extracted but not analyzed (or vice versa).
**Why it happens:** The existing trigger queries `FileState.ANALYZED` -- needs to change to dual-state check per D-02.
**How to avoid:** Update both API and UI proposal trigger endpoints to use the convergence gate query.
**Warning signs:** Proposals generated without tag data, or files stuck waiting for proposals.

### Pitfall 7: get_task_session Backward Compatibility
**What goes wrong:** Removing `get_task_session()` breaks existing `process_file()` and `generate_proposals()` tasks that call it.
**Why it happens:** Multiple task functions depend on the old pattern.
**How to avoid:** Update ALL existing task functions to use `ctx["async_session"]` simultaneously. Do not leave a mix of old and new patterns.
**Warning signs:** ImportError or AttributeError in task functions after partial migration.

## Code Examples

### Tag Extraction Service
```python
# src/phaze/services/metadata.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import mutagen
from mutagen.id3 import ID3
from mutagen.mp4 import MP4


@dataclass
class ExtractedTags:
    """Normalized tag extraction result."""
    artist: str | None = None
    title: str | None = None
    album: str | None = None
    year: int | None = None
    genre: str | None = None
    track_number: int | None = None
    duration: float | None = None
    bitrate: int | None = None
    raw_tags: dict[str, Any] = field(default_factory=dict)


# Vorbis-style keys (OGG, FLAC, OPUS)
_VORBIS_MAP = {
    "artist": "artist",
    "title": "title",
    "album": "album",
    "date": "year",
    "genre": "genre",
    "tracknumber": "track_number",
}

# ID3 frame keys (MP3, AIFF)
_ID3_MAP = {
    "TPE1": "artist",
    "TIT2": "title",
    "TALB": "album",
    "TDRC": "year",
    "TCON": "genre",
    "TRCK": "track_number",
}

# MP4 atom keys (M4A, MP4)
_MP4_MAP = {
    "\xa9ART": "artist",
    "\xa9nam": "title",
    "\xa9alb": "album",
    "\xa9day": "year",
    "\xa9gen": "genre",
    "trkn": "track_number",
}


def _first_str(val: Any) -> str | None:
    """Extract first string value from a tag value (may be list, frame, etc.)."""
    if val is None:
        return None
    if isinstance(val, list):
        return str(val[0]) if val else None
    return str(val)


def _parse_year(val: str | None) -> int | None:
    """Parse a year from a tag value like '2024' or '2024-03-15'."""
    if not val:
        return None
    try:
        return int(str(val)[:4])
    except (ValueError, IndexError):
        return None


def _parse_track(val: Any) -> int | None:
    """Parse track number from various formats ('3', '3/12', (3, 12))."""
    if val is None:
        return None
    if isinstance(val, tuple):
        return val[0] if val[0] else None
    if isinstance(val, list) and val:
        val = val[0]
        if isinstance(val, tuple):
            return val[0] if val[0] else None
    s = str(val).split("/")[0].strip()
    try:
        return int(s)
    except ValueError:
        return None


def _serialize_tags(tags: Any) -> dict[str, Any]:
    """Serialize mutagen tags to a JSON-safe dict."""
    if tags is None:
        return {}
    result: dict[str, Any] = {}
    for key in tags:
        try:
            val = tags[key]
            if isinstance(val, bytes):
                continue  # Skip binary data (cover art, etc.)
            if isinstance(val, list):
                serialized = []
                for item in val:
                    if isinstance(item, bytes):
                        continue
                    serialized.append(str(item))
                if serialized:
                    result[str(key)] = serialized
            else:
                result[str(key)] = str(val)
        except Exception:  # noqa: S110
            continue  # Skip any non-serializable values
    return result


def extract_tags(file_path: str) -> ExtractedTags:
    """Extract audio tags and stream info from a file.

    Returns ExtractedTags with normalized fields and raw dump.
    Returns empty result for unreadable/unrecognized files.
    """
    try:
        audio = mutagen.File(file_path)
    except Exception:
        return ExtractedTags()

    if audio is None:
        return ExtractedTags()

    # Stream info
    duration = audio.info.length if audio.info else None
    bitrate = getattr(audio.info, "bitrate", None) if audio.info else None

    # Raw tag dump (before normalized extraction)
    raw_tags = _serialize_tags(audio.tags)

    # Normalized fields -- dispatch based on tag type
    fields: dict[str, Any] = {}
    if isinstance(audio.tags, ID3):
        for frame_key, field_name in _ID3_MAP.items():
            fields[field_name] = _first_str(audio.tags.get(frame_key))
    elif isinstance(audio, MP4):
        for atom_key, field_name in _MP4_MAP.items():
            fields[field_name] = _first_str(audio.tags.get(atom_key) if audio.tags else None)
    elif audio.tags is not None:
        # Vorbis-style (OGG, FLAC, OPUS)
        for tag_key, field_name in _VORBIS_MAP.items():
            fields[field_name] = _first_str(audio.tags.get(tag_key))

    return ExtractedTags(
        artist=fields.get("artist"),
        title=fields.get("title"),
        album=fields.get("album"),
        year=_parse_year(fields.get("year")),
        genre=fields.get("genre"),
        track_number=_parse_track(fields.get("track_number")),
        duration=duration,
        bitrate=bitrate,
        raw_tags=raw_tags,
    )
```

### Shared Engine Pool in Worker
```python
# In src/phaze/tasks/worker.py startup hook:
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

async def startup(ctx: dict[str, Any]) -> None:
    # ... existing model checks and process pool ...

    # Shared async engine pool (INFRA-01)
    task_engine = create_async_engine(
        settings.database_url,
        echo=settings.debug,
        pool_size=10,
        max_overflow=5,
    )
    ctx["async_session"] = async_sessionmaker(
        task_engine, class_=AsyncSession, expire_on_commit=False
    )
    ctx["task_engine"] = task_engine

async def shutdown(ctx: dict[str, Any]) -> None:
    pool = ctx.get("process_pool")
    if pool is not None:
        pool.shutdown(wait=True)
    task_engine = ctx.get("task_engine")
    if task_engine is not None:
        await task_engine.dispose()
```

### Updated build_file_context (TAGS-05)
```python
def build_file_context(
    file_record: FileRecord,
    analysis: AnalysisResult | None,
    companion_contents: list[dict[str, str]],
    metadata: FileMetadata | None = None,
) -> dict[str, object]:
    # ... existing analysis_dict building ...

    tags_dict: dict[str, object] | None = None
    if metadata is not None:
        tags_dict = {
            "artist": metadata.artist,
            "title": metadata.title,
            "album": metadata.album,
            "year": metadata.year,
            "genre": metadata.genre,
            "track_number": metadata.track_number,
            "duration": metadata.duration,
            "bitrate": metadata.bitrate,
            "raw_tags": metadata.raw_tags,
        }

    return {
        "index": 0,
        "original_filename": file_record.original_filename,
        "original_path": file_record.original_path,
        "file_type": file_record.file_type,
        "analysis": analysis_dict,
        "tags": tags_dict,       # D-06: nested under 'tags' key
        "companions": companion_contents,
    }
```

### Alembic Migration for New Columns
```python
# alembic/versions/005_add_metadata_columns.py
def upgrade() -> None:
    op.add_column("metadata", sa.Column("track_number", sa.Integer, nullable=True))
    op.add_column("metadata", sa.Column("duration", sa.Float, nullable=True))
    op.add_column("metadata", sa.Column("bitrate", sa.Integer, nullable=True))

def downgrade() -> None:
    op.drop_column("metadata", "bitrate")
    op.drop_column("metadata", "duration")
    op.drop_column("metadata", "track_number")
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `get_task_session()` per invocation | Shared engine via arq ctx | This phase | Eliminates connection pool churn under 200K file load |
| Proposal gate: single ANALYZED state | Dual-state convergence (metadata + analysis) | This phase | Enables parallel extraction pipelines |
| build_file_context without tags | Tags included in LLM context | This phase | Richer AI proposals with artist/title/album data |

## Open Questions

1. **Track number format for MP4 `trkn`**
   - What we know: MP4 stores track as tuple `(track_num, total_tracks)` inside a list
   - What's unclear: Edge cases with non-standard MP4 taggers
   - Recommendation: `_parse_track` handles tuple and string formats; test with real files

2. **Pool size tuning for shared engine**
   - What we know: Current `database.py` uses pool_size=5, max_overflow=10
   - What's unclear: Optimal pool size for 8 concurrent arq workers
   - Recommendation: Start with pool_size=10, max_overflow=5 for workers (separate from API pool). Monitor with `SELECT count(*) FROM pg_stat_activity`.

3. **WAV file tag support**
   - What we know: WAV files can have INFO chunks or ID3 tags, but many have no tags at all
   - What's unclear: How common tagged WAV files are in the user's collection
   - Recommendation: `mutagen.File()` handles WAV with ID3 tags. Empty results for untagged WAVs (covered by D-11).

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio |
| Config file | pyproject.toml `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/ -x --timeout=30` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| INFRA-01 | Shared engine pool in worker ctx, no per-invocation engine | unit | `uv run pytest tests/test_tasks/test_session.py -x` | Needs rewrite |
| INFRA-02 | METADATA_EXTRACTED and FINGERPRINTED in pipeline stages and consumer queries | unit | `uv run pytest tests/test_services/test_pipeline.py -x` | Needs update |
| TAGS-01 | extract_tags reads tags from mp3/flac/ogg/m4a/opus files | unit | `uv run pytest tests/test_services/test_metadata.py -x` | Wave 0 |
| TAGS-02 | Extracted tags populate FileMetadata artist/title/album/year/genre/track_number | unit | `uv run pytest tests/test_services/test_metadata.py -x` | Wave 0 |
| TAGS-03 | Full raw tag dump stored in raw_tags JSONB | unit | `uv run pytest tests/test_services/test_metadata.py -x` | Wave 0 |
| TAGS-04 | Duration and bitrate extracted from audio info | unit | `uv run pytest tests/test_services/test_metadata.py -x` | Wave 0 |
| TAGS-05 | build_file_context includes tag data under 'tags' key | unit | `uv run pytest tests/test_services/test_proposal.py -x` | Needs update |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/ -x --timeout=30`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_services/test_metadata.py` -- covers TAGS-01 through TAGS-04 (extract_tags function, format mapping, serialization, edge cases)
- [ ] `tests/test_tasks/test_metadata_extraction.py` -- covers TAGS-01 arq task (extract_file_metadata task function)
- [ ] Update `tests/test_tasks/test_session.py` -- rewrite for shared engine pattern (INFRA-01)
- [ ] Update `tests/test_services/test_pipeline.py` -- add METADATA_EXTRACTED to stage counts (INFRA-02)
- [ ] Update `tests/test_services/test_proposal.py` -- test build_file_context with metadata param (TAGS-05)

## Sources

### Primary (HIGH confidence)
- Existing codebase: `src/phaze/tasks/session.py`, `src/phaze/tasks/functions.py`, `src/phaze/tasks/worker.py` -- current patterns
- Existing codebase: `src/phaze/models/metadata.py`, `src/phaze/models/file.py` -- current model state
- Existing codebase: `src/phaze/services/proposal.py` -- build_file_context pattern
- [mutagen GitHub docs](https://github.com/quodlibet/mutagen/blob/main/docs/user/gettingstarted.rst) -- File() API, format detection, tag access
- [mutagen PyPI](https://pypi.org/project/mutagen/) -- version 1.47.0 confirmed

### Secondary (MEDIUM confidence)
- [mutagen readthedocs](https://mutagen.readthedocs.io/) -- full API reference (blocked by Cloudflare during research, verified via GitHub docs)
- SQLAlchemy async engine pooling -- well-documented in SQLAlchemy 2.0 docs, already used in project's `database.py`

### Tertiary (LOW confidence)
- None

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- mutagen is the only viable Python library for read+write audio tags, already selected in project stack
- Architecture: HIGH -- follows established patterns from existing codebase (arq tasks, services, upsert)
- Pitfalls: HIGH -- based on direct codebase analysis (connection pool, pipeline stages, tag serialization)
- Tag format mapping: MEDIUM -- verified for major formats (ID3, Vorbis, MP4); edge cases (WMA/ASF) may need refinement with real files

**Research date:** 2026-03-30
**Valid until:** 2026-04-30 (stable domain, no fast-moving dependencies)
