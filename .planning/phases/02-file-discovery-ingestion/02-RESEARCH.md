# Phase 2: File Discovery & Ingestion - Research

**Researched:** 2026-03-27
**Domain:** Filesystem scanning, SHA256 hashing, bulk PostgreSQL ingestion, Unicode path normalization
**Confidence:** HIGH

## Summary

This phase adds the ability to scan a directory tree (mounted as a Docker volume), discover all music/video/companion files by extension, compute SHA256 hashes, normalize Unicode paths to NFC, classify each file by type, and persist everything to PostgreSQL via bulk inserts. The codebase already has the `FileRecord` model with all needed columns (`sha256_hash`, `original_path`, `original_filename`, `file_type`, `file_size`, `state`, `batch_id`), async SQLAlchemy with asyncpg, a `Settings` class, and a health router pattern to follow.

The primary technical challenges are: (1) efficient bulk insertion of up to 200K records using SQLAlchemy 2.0's ORM bulk INSERT pattern with asyncpg, (2) chunked SHA256 hashing to avoid loading multi-GB concert video files into memory, (3) Unicode NFC normalization at the ingestion boundary to prevent macOS NFD vs Linux NFC mismatches, and (4) making the scan endpoint non-blocking so the API remains responsive during long-running scans.

**Primary recommendation:** Use `os.walk` for fast synchronous directory traversal, extension-based classification from a single `dict` constant, chunked 64KB SHA256 hashing, SQLAlchemy 2.0 `session.execute(insert(FileRecord), list_of_dicts)` in batches of 1000 for bulk persistence, and `asyncio.to_thread()` to run the CPU/IO-bound scan off the event loop. Create a `ScanBatch` model to track scan progress.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Scan source is a Docker volume mount. The music directory is mounted into the container and the scan path is configured via `SCAN_PATH` environment variable in `.env` (e.g., `SCAN_PATH=/data/music`).
- **D-02:** Scanning is triggered via an API endpoint (`POST /api/v1/scan`). The endpoint accepts an optional `path` override but defaults to `SCAN_PATH` from config. Returns a batch ID for tracking.
- **D-03:** Each scan creates a batch record. Files discovered in a scan are linked to the batch via `batch_id` on `FileRecord`.
- **D-04:** File type classification uses extension-based detection. Extensions are mapped to categories: music (`mp3`, `m4a`, `ogg`, `flac`, `wav`, `aiff`, `wma`, `aac`), video (`mp4`, `mkv`, `avi`, `webm`, `mov`, `wmv`, `flv`), companion (`cue`, `nfo`, `txt`, `jpg`, `jpeg`, `png`, `gif`, `m3u`, `m3u8`, `pls`, `sfv`, `md5`). Unknown extensions are classified as `unknown`.
- **D-05:** Extension mapping is defined in a single constant/enum, not scattered. Easy to extend as new types are discovered.
- **D-06:** Directory scanning is synchronous (fast -- just `os.walk`). File hashing and DB persistence are batched using PostgreSQL `COPY` or bulk insert for performance (research flagged individual INSERTs as 10x slower at this scale).
- **D-07:** SHA256 hashing reads files in 64KB chunks to avoid loading large files into memory.
- **D-08:** Scans are resumable by batch -- if a scan is interrupted, re-running it creates a new batch. Files already in the DB (matched by `original_path`) are skipped or updated.
- **D-09:** Unicode paths are normalized to NFC at ingestion time (research pitfall: macOS uses NFD, Linux uses NFC).

### Claude's Discretion
- API router structure for the scan endpoint
- Batch table schema (if needed beyond existing `batch_id` on FileRecord)
- Progress reporting mechanism (SSE, polling, or just logs for now)
- Exact chunking strategy for bulk inserts (1000 rows per batch, or adaptive)
- Whether to add a `ScanBatch` model or just use UUID grouping
- Test strategy (unit tests with mocked filesystem, integration tests with temp dirs)

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| ING-01 | System can scan directories recursively to discover music files (mp3, m4a, ogg), video files, and companion files (cue, nfo, txt, jpg, png, m3u, pls) | `os.walk` with extension-based classification from a constant dict; see Architecture Patterns section |
| ING-02 | System extracts sha256 hash for every discovered file | Chunked 64KB hashing with `hashlib.sha256`; wrapped in `asyncio.to_thread()` for non-blocking; see Code Examples |
| ING-03 | System records original filename and original path for every file in PostgreSQL | FileRecord model already has `original_path` and `original_filename` columns; bulk insert via SQLAlchemy 2.0 ORM pattern |
| ING-05 | System classifies each file by type (music, video, companion) and stores the classification | Extension-to-category dict constant; `file_type` column on FileRecord stores the extension, category used for classification logic; see Architecture Patterns |
</phase_requirements>

## Project Constraints (from CLAUDE.md)

- **Python 3.13 exclusively** -- all code must target 3.13
- **`uv` only** -- never bare `pip`, `python`, `pytest`, `mypy`; always `uv run` prefix
- **Pre-commit must pass** -- ruff check, ruff format, mypy, bandit, etc.
- **Mypy strict mode** -- `disallow_untyped_defs`, `disallow_incomplete_defs`, etc. (tests excluded)
- **85% code coverage minimum** -- enforced in `pyproject.toml`
- **150 char line length** -- ruff configured
- **Double quotes** -- ruff format configured
- **Type hints on all functions** -- mypy enforces
- **Every feature gets its own PR** -- no mixing unrelated changes

## Standard Stack

### Core (already installed)
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| SQLAlchemy | >=2.0.48 | ORM bulk inserts | Already in pyproject.toml. Async bulk INSERT via `session.execute(insert(Model), list_of_dicts)` |
| asyncpg | >=0.31.0 | PostgreSQL async driver | Already in pyproject.toml. Fastest Python PG driver. |
| FastAPI | >=0.135.2 | API endpoint | Already in pyproject.toml. `POST /api/v1/scan` endpoint. |
| pydantic-settings | >=2.13.1 | Config (`SCAN_PATH`) | Already in pyproject.toml. Add `scan_path` field to Settings. |

### Standard Library (no install needed)
| Module | Purpose | Notes |
|--------|---------|-------|
| `os.walk` | Directory traversal | Synchronous, fast. Wrap in `asyncio.to_thread()`. |
| `hashlib.sha256` | File hashing | Chunked 64KB reads. |
| `unicodedata.normalize` | NFC normalization | `normalize("NFC", path_str)` at ingestion boundary. |
| `pathlib.Path` | Path manipulation | Use throughout, per CLAUDE.md PTH rules. |
| `uuid.uuid4` | Batch IDs | Already used in FileRecord model. |

### No New Dependencies Required

This phase uses only the standard library and already-installed packages. No new `uv add` needed.

## Architecture Patterns

### Recommended Project Structure (new files for Phase 2)
```
src/phaze/
├── config.py              # Add scan_path field
├── routers/
│   └── scan.py            # POST /api/v1/scan endpoint
├── services/
│   └── ingestion.py       # Scan logic, hashing, bulk insert
├── schemas/
│   └── scan.py            # ScanRequest, ScanResponse Pydantic models
├── models/
│   └── scan_batch.py      # ScanBatch model (optional, recommended)
│   └── file.py            # Existing FileRecord (no changes needed)
└── constants.py           # EXTENSION_MAP, FileCategory enum, HASH_CHUNK_SIZE

docker-compose.yml         # Add volume mount for SCAN_PATH
.env.example               # Add SCAN_PATH variable
alembic/versions/          # Migration for scan_batches table (if adding ScanBatch)
```

### Pattern 1: Extension-Based File Classification

**What:** A single `dict[str, str]` mapping lowercase extensions to category strings. A `StrEnum` for categories.
**When to use:** Every file discovered by `os.walk`.

```python
import enum


class FileCategory(enum.StrEnum):
    MUSIC = "music"
    VIDEO = "video"
    COMPANION = "companion"
    UNKNOWN = "unknown"


# Extension -> category mapping (single source of truth)
EXTENSION_MAP: dict[str, FileCategory] = {
    # Music
    ".mp3": FileCategory.MUSIC,
    ".m4a": FileCategory.MUSIC,
    ".ogg": FileCategory.MUSIC,
    ".flac": FileCategory.MUSIC,
    ".wav": FileCategory.MUSIC,
    ".aiff": FileCategory.MUSIC,
    ".wma": FileCategory.MUSIC,
    ".aac": FileCategory.MUSIC,
    # Video
    ".mp4": FileCategory.VIDEO,
    ".mkv": FileCategory.VIDEO,
    ".avi": FileCategory.VIDEO,
    ".webm": FileCategory.VIDEO,
    ".mov": FileCategory.VIDEO,
    ".wmv": FileCategory.VIDEO,
    ".flv": FileCategory.VIDEO,
    # Companion
    ".cue": FileCategory.COMPANION,
    ".nfo": FileCategory.COMPANION,
    ".txt": FileCategory.COMPANION,
    ".jpg": FileCategory.COMPANION,
    ".jpeg": FileCategory.COMPANION,
    ".png": FileCategory.COMPANION,
    ".gif": FileCategory.COMPANION,
    ".m3u": FileCategory.COMPANION,
    ".m3u8": FileCategory.COMPANION,
    ".pls": FileCategory.COMPANION,
    ".sfv": FileCategory.COMPANION,
    ".md5": FileCategory.COMPANION,
}

HASH_CHUNK_SIZE = 65536  # 64KB
```

### Pattern 2: Chunked SHA256 Hashing

**What:** Read file in 64KB chunks, feed incrementally to hashlib.
**When to use:** Every discovered file.

```python
import hashlib
from pathlib import Path


def compute_sha256(file_path: Path) -> str:
    """Compute SHA256 hash of a file using chunked reading."""
    sha256 = hashlib.sha256()
    with file_path.open("rb") as f:
        while chunk := f.read(HASH_CHUNK_SIZE):
            sha256.update(chunk)
    return sha256.hexdigest()
```

**Key:** This is a synchronous function. It MUST be run via `asyncio.to_thread()` when called from async code to avoid blocking the event loop.

### Pattern 3: NFC Unicode Normalization at Ingestion Boundary

**What:** Normalize all path strings to NFC before storing.
**When to use:** At the point where `os.walk` returns paths, before any DB operations.

```python
import unicodedata


def normalize_path(path: str) -> str:
    """Normalize a filesystem path to NFC Unicode form."""
    return unicodedata.normalize("NFC", path)
```

Apply to both `original_path` and `original_filename` before creating `FileRecord` dicts.

### Pattern 4: SQLAlchemy 2.0 ORM Bulk Insert

**What:** Use `session.execute(insert(Model), list_of_dicts)` for bulk insertion.
**When to use:** After collecting a batch of file records (1000 at a time).

```python
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.file import FileRecord


async def bulk_insert_files(session: AsyncSession, records: list[dict]) -> None:
    """Bulk insert file records in a single statement."""
    if not records:
        return
    await session.execute(insert(FileRecord), records)
    await session.commit()
```

**Critical:** Pass the list of dicts as the second argument to `session.execute()`, NOT via `.values()`. The former uses asyncpg's efficient executemany; the latter does not.

### Pattern 5: Non-Blocking Scan via asyncio.to_thread

**What:** The scan endpoint starts the scan in a background thread so the API remains responsive.
**When to use:** The `POST /api/v1/scan` handler.

For Phase 2 (before arq is available in Phase 4), use `asyncio.create_task` with `asyncio.to_thread` to run the synchronous directory walk and file hashing off the main event loop. The endpoint returns immediately with a batch ID.

```python
import asyncio


async def start_scan(scan_path: str, batch_id: uuid.UUID) -> None:
    """Run scan in background -- non-blocking."""
    # os.walk + hashing is sync/CPU-bound, run in thread
    discovered = await asyncio.to_thread(discover_files, scan_path)
    # Bulk insert to DB in async context
    async with async_session() as session:
        for chunk in batched(discovered, 1000):
            await bulk_insert_files(session, chunk)
```

**Recommendation for Claude's Discretion (progress reporting):** For Phase 2, use simple logging. The scan endpoint returns a batch_id, and a `GET /api/v1/scan/{batch_id}` status endpoint queries the count of files linked to that batch. SSE or WebSocket is premature before Phase 4.

### Pattern 6: ScanBatch Model (Recommended)

**What:** A dedicated model to track scan batches rather than just a UUID grouping.
**Why:** Enables tracking scan status (running/completed/failed), total file count, start/end times, and the scan path. Makes the `GET /api/v1/scan/{batch_id}` status endpoint trivial.

```python
class ScanStatus(enum.StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ScanBatch(TimestampMixin, Base):
    __tablename__ = "scan_batches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scan_path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ScanStatus.RUNNING)
    total_files: Mapped[int] = mapped_column(default=0)
    processed_files: Mapped[int] = mapped_column(default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
```

This requires an Alembic migration to add the `scan_batches` table and optionally a FK from `files.batch_id` to `scan_batches.id`.

### Anti-Patterns to Avoid

- **Individual INSERTs in a loop:** 10x slower than bulk insert at 200K records. Always batch.
- **Reading entire file for hashing:** Will OOM on multi-GB concert videos. Always use 64KB chunks.
- **Blocking the event loop with os.walk:** Directory traversal of 200K files takes seconds. Wrap in `asyncio.to_thread()`.
- **Storing file_type as category instead of extension:** The `file_type` column is `String(10)` -- store the extension (e.g., "mp3", "mkv"). Category can be derived from the extension map. Storing the extension preserves more information.
- **Following symlinks without protection:** `os.walk` follows symlinks by default and can infinite-loop. Use `followlinks=False` (the default) or detect cycles.
- **Skipping path validation:** Always verify `scan_path` exists and is a directory before walking. Reject path traversal attempts (e.g., `../../etc`).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| SHA256 hashing | Custom hash streaming | `hashlib.sha256` with chunked reads | stdlib is C-optimized, correct, fast |
| Unicode normalization | Custom char replacement | `unicodedata.normalize("NFC", s)` | Covers all Unicode edge cases, not just accented chars |
| Directory traversal | Custom recursive walk | `os.walk` | Handles OS-level edge cases, symlink protection |
| Bulk DB insert | Loop of `session.add()` | `session.execute(insert(Model), list_of_dicts)` | Uses asyncpg executemany, 10x faster |
| Path manipulation | String splitting/joining | `pathlib.Path` | Cross-platform, type-safe, ruff PTH rules enforce this |

## Common Pitfalls

### Pitfall 1: macOS NFD vs Linux NFC Unicode Mismatch
**What goes wrong:** Files copied from a macOS volume have NFD-normalized filenames. The same visual filename has different byte sequences, so DB lookups fail silently.
**Why it happens:** macOS HFS+/APFS stores filenames in NFD; Linux ext4/XFS stores NFC. Docker volume mounts pass through the host encoding.
**How to avoid:** Apply `unicodedata.normalize("NFC", path)` to every path string at the ingestion boundary, before any DB operation or comparison.
**Warning signs:** Files "not found" in the database that visually appear to match; test failures only on macOS-sourced data.

### Pitfall 2: OOM from Loading Large Files for Hashing
**What goes wrong:** `hashlib.sha256(file.read()).hexdigest()` loads entire file into RAM. A 4GB concert video causes the container to be OOM-killed.
**Why it happens:** Works fine in testing with small files. Only fails at scale with large video files.
**How to avoid:** Always use 64KB chunked reads. Set `HASH_CHUNK_SIZE = 65536` as a constant. Never call `file.read()` without a size argument in hashing code.
**Warning signs:** Docker container restarts during scanning, OOM in container logs.

### Pitfall 3: Bulk Insert Performance Cliff
**What goes wrong:** Inserting 200K records one at a time takes hours. The API is unresponsive during ingestion.
**Why it happens:** Individual INSERT has per-statement overhead (parse, plan, WAL write, index update).
**How to avoid:** Use SQLAlchemy 2.0 bulk insert pattern: `session.execute(insert(Model), list_of_dicts)` with batches of 1000. A single transaction per batch.
**Warning signs:** Ingestion taking more than 5 minutes for 200K records.

### Pitfall 4: Blocking the Event Loop During Scan
**What goes wrong:** The API becomes unresponsive while scanning 200K files because `os.walk` and `hashlib` are synchronous and block the async event loop.
**Why it happens:** `os.walk` is synchronous I/O. SHA256 hashing is CPU-bound. Neither yields to the event loop.
**How to avoid:** Run the synchronous scan+hash work via `asyncio.to_thread()`. Return the batch ID immediately from the endpoint.
**Warning signs:** Health check endpoint times out during active scan.

### Pitfall 5: file_type Column Too Small for Extensions
**What goes wrong:** `file_type` is `String(10)`. Extensions like `.jpeg` (5 chars without dot) fit, but storing with the dot or longer extensions could truncate.
**Why it happens:** The column was sized for short extensions.
**How to avoid:** Store extensions without the leading dot (e.g., `"mp3"`, `"jpeg"`, `"m3u8"`). All extensions in the EXTENSION_MAP fit within 10 characters without the dot. Validate this in the extension map constant.
**Warning signs:** Database constraint violations during insert.

### Pitfall 6: Docker Volume Not Mounted
**What goes wrong:** The scan path does not exist inside the container because the volume mount was not added to `docker-compose.yml`.
**Why it happens:** Phase 1 created `docker-compose.yml` without a music volume mount (it was out of scope).
**How to avoid:** Add the volume mount in this phase: `- ${SCAN_PATH}:/data/music:ro` on the `api` service. Validate the path exists at scan start time.
**Warning signs:** `FileNotFoundError` or `NotADirectoryError` when triggering scan.

### Pitfall 7: Resumability Edge Case -- Duplicate Paths
**What goes wrong:** Re-running a scan after interruption inserts duplicate records for files already processed.
**Why it happens:** No unique constraint on `original_path`, and `batch_id` differs between scans.
**How to avoid:** Before inserting, query existing records by `original_path`. Use INSERT ON CONFLICT (on a unique index on `original_path`) to skip or update existing records. Alternatively, query all existing paths for the scan directory into a set for fast in-memory lookup before scanning.
**Warning signs:** Duplicate file records in the database after multiple scan runs.

## Code Examples

### Complete Ingestion Service Pattern

```python
"""Ingestion service -- directory scanning, hashing, and bulk persistence."""

import asyncio
import hashlib
import os
import unicodedata
import uuid
from pathlib import Path

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.constants import EXTENSION_MAP, HASH_CHUNK_SIZE, FileCategory
from phaze.models.file import FileRecord, FileState


def normalize_path(path: str) -> str:
    """Normalize path string to NFC Unicode form."""
    return unicodedata.normalize("NFC", path)


def compute_sha256(file_path: Path) -> str:
    """Compute SHA256 hash using chunked reading. Synchronous -- run via to_thread."""
    sha256 = hashlib.sha256()
    with file_path.open("rb") as f:
        while chunk := f.read(HASH_CHUNK_SIZE):
            sha256.update(chunk)
    return sha256.hexdigest()


def classify_file(filename: str) -> FileCategory:
    """Classify a file by its extension using the extension map."""
    ext = Path(filename).suffix.lower()
    return EXTENSION_MAP.get(ext, FileCategory.UNKNOWN)


def discover_and_hash_files(
    scan_path: str,
    batch_id: uuid.UUID,
) -> list[dict]:
    """Walk directory, hash files, return list of record dicts. Synchronous."""
    scan_root = Path(scan_path)
    records: list[dict] = []

    for dirpath, _dirnames, filenames in os.walk(scan_root, followlinks=False):
        for filename in filenames:
            file_path = Path(dirpath) / filename
            if not file_path.is_file():
                continue

            category = classify_file(filename)
            if category == FileCategory.UNKNOWN:
                continue  # Skip unknown file types

            normalized_path = normalize_path(str(file_path))
            normalized_filename = normalize_path(filename)

            try:
                file_size = file_path.stat().st_size
                sha256_hash = compute_sha256(file_path)
            except OSError:
                continue  # Skip unreadable files

            records.append({
                "id": uuid.uuid4(),
                "sha256_hash": sha256_hash,
                "original_path": normalized_path,
                "original_filename": normalized_filename,
                "current_path": normalized_path,
                "file_type": file_path.suffix.lstrip(".").lower(),
                "file_size": file_size,
                "state": FileState.DISCOVERED,
                "batch_id": batch_id,
            })

    return records


async def bulk_insert_files(
    session: AsyncSession,
    records: list[dict],
    batch_size: int = 1000,
) -> int:
    """Bulk insert file records in batches. Returns total inserted."""
    total = 0
    for i in range(0, len(records), batch_size):
        batch = records[i : i + batch_size]
        await session.execute(insert(FileRecord), batch)
        await session.commit()
        total += len(batch)
    return total
```

### Scan Router Pattern

```python
"""Scan router -- POST /api/v1/scan endpoint."""

import asyncio
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.config import settings
from phaze.database import get_session


router = APIRouter(prefix="/api/v1", tags=["scan"])


class ScanRequest(BaseModel):
    path: str | None = None  # Optional override, defaults to SCAN_PATH


class ScanResponse(BaseModel):
    batch_id: uuid.UUID
    message: str


@router.post("/scan")
async def trigger_scan(
    request: ScanRequest,
    session: AsyncSession = Depends(get_session),
) -> ScanResponse:
    scan_path = request.path or settings.scan_path
    batch_id = uuid.uuid4()
    # Start scan as background task
    asyncio.create_task(run_scan(scan_path, batch_id))
    return ScanResponse(batch_id=batch_id, message="Scan started")
```

### itertools.batched (Python 3.12+)

Python 3.12+ includes `itertools.batched()` which is perfect for chunking records:

```python
from itertools import batched

for chunk in batched(records, 1000):
    await session.execute(insert(FileRecord), list(chunk))
    await session.commit()
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `session.bulk_insert_mappings()` | `session.execute(insert(Model), list_of_dicts)` | SQLAlchemy 2.0 (2023) | Legacy method deprecated; new pattern is more efficient with asyncpg |
| `os.path.join` / string manipulation | `pathlib.Path` | Python 3.4+ (mature) | Ruff PTH rules enforce pathlib usage |
| Manual batching | `itertools.batched()` | Python 3.12 | Stdlib replacement for hand-rolled chunking |
| `session.add_all()` for bulk | `session.execute(insert(), list)` | SQLAlchemy 2.0 | `add_all` still works but is slower for pure inserts |

## Open Questions

1. **Unique constraint on original_path**
   - What we know: D-08 says re-scans should skip or update files matched by `original_path`. This implies a unique constraint.
   - What's unclear: The existing schema has no unique constraint on `original_path`. Adding one requires an Alembic migration.
   - Recommendation: Add a unique index on `original_path` in this phase's migration. Use INSERT ON CONFLICT DO UPDATE for upsert semantics.

2. **file_type stores extension or category?**
   - What we know: The column is `String(10)`. The CONTEXT says "classified as music, video, or companion."
   - What's unclear: Whether to store the extension ("mp3") or the category ("music").
   - Recommendation: Store the extension. It preserves more information and the category is trivially derivable from the extension map. The success criteria say "classified and stored" which the extension satisfies since classification is deterministic from extension.

3. **ScanBatch as model vs UUID grouping**
   - What we know: `FileRecord.batch_id` exists as a nullable UUID. No FK constraint.
   - What's unclear: Whether a full `ScanBatch` model is worth the added migration complexity.
   - Recommendation: Add a `ScanBatch` model. It costs one small migration and enables the status endpoint (`GET /api/v1/scan/{batch_id}`), progress tracking, and error reporting. Without it, you cannot distinguish a running scan from a completed one.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| PostgreSQL | Data storage | Via Docker | 18-alpine (docker-compose.yml) | -- |
| Python 3.13 | Runtime | Local | 3.13 | -- |
| uv | Package management | Local | Installed | -- |
| Docker / Docker Compose | Container runtime | Required for volume mount | Assumed available | -- |

**Missing dependencies with no fallback:** None -- all dependencies are already in place from Phase 1.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio |
| Config file | `pyproject.toml` [tool.pytest.ini_options] |
| Quick run command | `uv run pytest tests/ -x --no-header -q` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| ING-01 | Recursive directory scan discovers music, video, companion files | unit | `uv run pytest tests/test_services/test_ingestion.py::test_discover_files -x` | Wave 0 |
| ING-01 | Unknown extensions are skipped | unit | `uv run pytest tests/test_services/test_ingestion.py::test_skip_unknown_extensions -x` | Wave 0 |
| ING-02 | SHA256 hash computed correctly for each file | unit | `uv run pytest tests/test_services/test_ingestion.py::test_compute_sha256 -x` | Wave 0 |
| ING-02 | Large files hashed without OOM (chunked) | unit | `uv run pytest tests/test_services/test_ingestion.py::test_chunked_hashing -x` | Wave 0 |
| ING-03 | Original path and filename stored in PostgreSQL | integration | `uv run pytest tests/test_services/test_ingestion.py::test_bulk_insert_stores_paths -x` | Wave 0 |
| ING-05 | Files classified by extension into music/video/companion | unit | `uv run pytest tests/test_services/test_ingestion.py::test_classify_file -x` | Wave 0 |
| ING-05 | Classification stored in file_type column | integration | `uv run pytest tests/test_services/test_ingestion.py::test_classification_persisted -x` | Wave 0 |
| -- | Unicode NFC normalization applied to paths | unit | `uv run pytest tests/test_services/test_ingestion.py::test_nfc_normalization -x` | Wave 0 |
| -- | POST /api/v1/scan returns batch_id | integration | `uv run pytest tests/test_routers/test_scan.py::test_trigger_scan -x` | Wave 0 |
| -- | Bulk insert handles 1000+ records | integration | `uv run pytest tests/test_services/test_ingestion.py::test_bulk_insert_performance -x` | Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_services/test_ingestion.py tests/test_routers/test_scan.py -x --no-header -q`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_services/__init__.py` -- package init
- [ ] `tests/test_services/test_ingestion.py` -- covers ING-01, ING-02, ING-03, ING-05, Unicode NFC
- [ ] `tests/test_routers/__init__.py` -- package init
- [ ] `tests/test_routers/test_scan.py` -- covers scan endpoint
- [ ] `tests/test_constants.py` -- covers extension map completeness

## Sources

### Primary (HIGH confidence)
- [SQLAlchemy 2.0 ORM Bulk INSERT](https://docs.sqlalchemy.org/en/20/orm/queryguide/dml.html) -- bulk insert patterns, `session.execute(insert(), list)` syntax
- [SQLAlchemy 2.0 Bulk Insert Examples](https://docs.sqlalchemy.org/en/20/_modules/examples/performance/bulk_inserts.html) -- performance comparison of insert methods
- [FastAPI Background Tasks](https://fastapi.tiangolo.com/tutorial/background-tasks/) -- BackgroundTasks vs create_task patterns
- Python stdlib docs: `hashlib`, `unicodedata.normalize`, `os.walk`, `pathlib.Path` -- all verified for Python 3.13

### Secondary (MEDIUM confidence)
- [SQLAlchemy/asyncpg executemany discussion](https://github.com/sqlalchemy/sqlalchemy/discussions/7651) -- confirmed `execute(insert(), list)` uses executemany
- [PostgreSQL Bulk Loading Performance](https://hakibenita.com/fast-load-data-python-postgresql) -- COPY vs INSERT benchmarks
- [Unicode Normalization Guide](https://unicodefyi.com/guide/unicode-normalization-guide/) -- NFC vs NFD explanation
- [Beets Path Encoding](https://beets.io/blog/paths.html) -- macOS NFD vs Linux NFC real-world documentation

### Tertiary (LOW confidence)
- None -- all findings verified against official documentation.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- all libraries already installed and tested in Phase 1
- Architecture: HIGH -- patterns verified against SQLAlchemy 2.0 docs and existing codebase
- Pitfalls: HIGH -- documented extensively in project's own PITFALLS.md research, verified against beets ecosystem experience
- Test strategy: HIGH -- test framework already configured and working from Phase 1

**Research date:** 2026-03-27
**Valid until:** 2026-04-27 (stable domain -- stdlib and SQLAlchemy 2.0 patterns are mature)
