# Phase 3: Companion Files & Deduplication - Research

**Researched:** 2026-03-27
**Domain:** SQLAlchemy many-to-many relationships, directory-based file association, SHA256 grouping queries
**Confidence:** HIGH

## Summary

Phase 3 adds two features on top of the Phase 2 file inventory: (1) associating companion files (cue, nfo, jpg, etc.) with media files in the same directory via a many-to-many join table, and (2) exposing SHA256-based exact duplicate groups via a query endpoint. Both features are well-understood patterns with no novel technical risk.

The codebase already has the `FileRecord` model with `sha256_hash`, `original_path`, and `file_type` columns, plus indexes on `sha256_hash` and `original_path`. The companion association requires a new `file_companions` join table and an Alembic migration. Duplicate detection requires no schema changes -- it is purely a `GROUP BY sha256_hash HAVING COUNT(*) > 1` query. Both features get new service functions and API endpoints following the established router/schema patterns.

**Primary recommendation:** Implement as three layers -- Alembic migration for the join table, service functions for association + dedup logic, and API endpoints wired into the existing FastAPI app. Follow established patterns exactly (scan router, Pydantic schemas, async SQLAlchemy queries).

<user_constraints>

## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Add a `file_companions` join table (many-to-many) linking companion FileRecords to media FileRecords. A companion in a directory associates with all media files in the same directory.
- **D-02:** Association is computed as a post-processing step after ingestion -- a service function scans for companions without a parent link and associates them based on directory path matching (`Path(companion.original_path).parent == Path(media.original_path).parent`).
- **D-03:** Association is triggered via API (`POST /api/v1/associate`) or automatically after a scan completes. Returns count of newly linked companions.
- **D-04:** Duplicates are detected by grouping FileRecords with the same `sha256_hash` where the group has more than one member. No new column needed -- query-time grouping is sufficient.
- **D-05:** Duplicates are exposed via `GET /api/v1/duplicates` endpoint returning grouped duplicate sets with file paths, sizes, and types. No auto-resolution -- duplicates are flagged for human review (deferred to Phase 7 approval UI).
- **D-06:** A dedicated service function identifies duplicate groups and returns them structured for the API.

### Claude's Discretion
- Join table schema details (composite PK, FK constraints, indexes)
- Alembic migration structure for the join table
- Whether to cache duplicate groups or compute on demand
- Test strategy for association logic (mock filesystem with known directory structure)
- API response pagination for large duplicate sets

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope.

</user_constraints>

<phase_requirements>

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| ING-04 | System detects exact duplicates via sha256 and flags them for review | Duplicate detection via `GROUP BY sha256_hash HAVING COUNT(*) > 1` query. Existing `ix_files_sha256_hash` index supports this efficiently. Service function + API endpoint. |
| ING-06 | System associates companion files with nearby music/video files using directory proximity heuristics | Many-to-many `file_companions` join table. Service function extracts `Path.parent` from `original_path` and matches companions to media files in same directory. |

</phase_requirements>

## Standard Stack

No new libraries needed. This phase uses only existing project dependencies.

### Core (already installed)
| Library | Version | Purpose | Already in pyproject.toml |
|---------|---------|---------|--------------------------|
| SQLAlchemy | >=2.0.48 | ORM for join table model, async queries | Yes |
| Alembic | >=1.18.4 | Migration for `file_companions` table | Yes |
| FastAPI | >=0.135.2 | API endpoints for associate + duplicates | Yes |
| Pydantic | >=2.10 | Request/response schemas | Yes (via FastAPI) |
| asyncpg | >=0.31.0 | Async PostgreSQL driver | Yes |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Join table | JSON array column on FileRecord | Join table is correct for M:M; JSON cannot be queried efficiently with FKs |
| Query-time dedup | Materialized view | Premature optimization; 200K rows GROUP BY on indexed column is fast enough |
| Directory path matching | Regex/glob patterns | Path.parent equality is simpler, correct for "same directory" requirement |

## Architecture Patterns

### Recommended Project Structure (new files)
```
src/phaze/
  models/
    file_companion.py    # FileCompanion join table model
  services/
    companion.py         # associate_companions() service function
    dedup.py             # find_duplicate_groups() service function
  routers/
    companion.py         # POST /api/v1/associate, GET /api/v1/duplicates
  schemas/
    companion.py         # AssociateResponse, DuplicateGroup, DuplicateGroupsResponse
alembic/versions/
  003_add_file_companions_table.py
tests/
  test_services/
    test_companion.py    # Association logic tests
    test_dedup.py        # Duplicate detection tests
  test_routers/
    test_companion.py    # API endpoint tests
```

### Pattern 1: Many-to-Many Join Table (Association Table)
**What:** A dedicated SQLAlchemy `Table` or model linking companion file IDs to media file IDs.
**When to use:** When a companion file can relate to multiple media files (and vice versa in theory, though companions are the "child" side conceptually).
**Example:**
```python
# src/phaze/models/file_companion.py
import uuid

from sqlalchemy import ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class FileCompanion(TimestampMixin, Base):
    """Many-to-many link between companion files and their media files."""

    __tablename__ = "file_companions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    companion_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("files.id", ondelete="CASCADE"),
        nullable=False,
    )
    media_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("files.id", ondelete="CASCADE"),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("companion_id", "media_id", name="uq_file_companions_pair"),
        Index("ix_file_companions_companion_id", "companion_id"),
        Index("ix_file_companions_media_id", "media_id"),
    )
```

### Pattern 2: Directory Path Extraction for Association
**What:** Extract parent directory from `original_path` to match companions with media files in the same directory.
**When to use:** For D-02 companion association logic.
**Example:**
```python
# In services/companion.py
from pathlib import PurePosixPath

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.constants import FileCategory, EXTENSION_MAP
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion


# Media extensions set for filtering
MEDIA_CATEGORIES = {FileCategory.MUSIC, FileCategory.VIDEO}
COMPANION_EXTENSIONS = {
    ext.lstrip(".") for ext, cat in EXTENSION_MAP.items()
    if cat == FileCategory.COMPANION
}
MEDIA_EXTENSIONS = {
    ext.lstrip(".") for ext, cat in EXTENSION_MAP.items()
    if cat in MEDIA_CATEGORIES
}


async def associate_companions(session: AsyncSession) -> int:
    """Find unlinked companions and associate with media in same directory.

    Returns count of new associations created.
    """
    # Get all companion files not yet in file_companions
    existing_companion_ids = select(FileCompanion.companion_id).scalar_subquery()

    companions_q = select(FileRecord).where(
        FileRecord.file_type.in_(COMPANION_EXTENSIONS),
        ~FileRecord.id.in_(existing_companion_ids),
    )
    result = await session.execute(companions_q)
    unlinked_companions = result.scalars().all()

    new_links = 0
    # Group companions by directory for batch processing
    by_dir: dict[str, list[FileRecord]] = {}
    for comp in unlinked_companions:
        parent = str(PurePosixPath(comp.original_path).parent)
        by_dir.setdefault(parent, []).append(comp)

    for directory, companions in by_dir.items():
        # Find media files in same directory
        media_q = select(FileRecord).where(
            FileRecord.file_type.in_(MEDIA_EXTENSIONS),
            FileRecord.original_path.like(f"{directory}/%"),
            # Exclude subdirectory matches
            ~FileRecord.original_path.like(f"{directory}/%/%"),
        )
        media_result = await session.execute(media_q)
        media_files = media_result.scalars().all()

        if not media_files:
            continue

        for comp in companions:
            for media in media_files:
                link = FileCompanion(
                    companion_id=comp.id,
                    media_id=media.id,
                )
                session.add(link)
                new_links += 1

    await session.commit()
    return new_links
```

### Pattern 3: Duplicate Group Query
**What:** GROUP BY sha256_hash HAVING COUNT > 1 to find duplicate sets.
**When to use:** For D-04/D-06 duplicate detection.
**Example:**
```python
# In services/dedup.py
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.file import FileRecord


async def find_duplicate_groups(
    session: AsyncSession,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Find groups of files sharing the same SHA256 hash.

    Returns list of groups, each with hash and list of file details.
    """
    # Subquery: hashes with more than one file
    dup_hashes = (
        select(FileRecord.sha256_hash)
        .group_by(FileRecord.sha256_hash)
        .having(func.count(FileRecord.id) > 1)
        .limit(limit)
        .offset(offset)
        .subquery()
    )

    # Get all files matching duplicate hashes
    files_q = select(FileRecord).where(
        FileRecord.sha256_hash.in_(select(dup_hashes.c.sha256_hash))
    ).order_by(FileRecord.sha256_hash, FileRecord.original_path)

    result = await session.execute(files_q)
    files = result.scalars().all()

    # Group by hash
    groups: dict[str, list] = {}
    for f in files:
        groups.setdefault(f.sha256_hash, []).append({
            "id": str(f.id),
            "original_path": f.original_path,
            "file_size": f.file_size,
            "file_type": f.file_type,
        })

    return [
        {"sha256_hash": h, "count": len(members), "files": members}
        for h, members in groups.items()
    ]
```

### Anti-Patterns to Avoid
- **Loading all files into Python for grouping:** Use SQL GROUP BY, not Python-side grouping of all 200K records.
- **Using ORM relationship() on the join table for this phase:** Relationships add complexity for eager/lazy loading config. Raw queries are simpler for this use case. Relationships can be added later if needed.
- **String manipulation instead of Path for directory extraction:** Always use `PurePosixPath` for path parsing to handle edge cases.
- **LIKE queries with user input:** The directory paths come from our own database (original_path), not user input, so SQL injection via LIKE is not a concern here. But still use parameterized queries (SQLAlchemy handles this).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Path parent extraction | Manual string splitting | `PurePosixPath(path).parent` | Handles trailing slashes, normalized paths, edge cases |
| Pagination | Manual LIMIT/OFFSET math | SQLAlchemy `.limit()/.offset()` | Correct, parameterized, no off-by-one |
| Unique constraint enforcement | Application-level check-then-insert | DB `UniqueConstraint` + `ON CONFLICT` | Race condition free |
| UUID generation | Custom ID schemes | `uuid.uuid4()` via SQLAlchemy default | Standard, unique, no coordination needed |

## Common Pitfalls

### Pitfall 1: LIKE Query Matching Subdirectories
**What goes wrong:** `original_path LIKE '/music/album/%'` also matches `/music/album/disc1/track.mp3` -- a file in a subdirectory, not the same directory.
**Why it happens:** LIKE `%` matches any characters including `/`.
**How to avoid:** Add a NOT LIKE clause excluding deeper paths: `AND original_path NOT LIKE '/music/album/%/%'`. Or extract the parent in Python and compare exactly.
**Warning signs:** Companion files being associated with media in child directories.

### Pitfall 2: Empty Directory Groups
**What goes wrong:** A companion file exists in a directory with no media files. The association function should skip it, not error.
**Why it happens:** Not all directories have both companion and media files.
**How to avoid:** Check `if not media_files: continue` before creating links.
**Warning signs:** Zero associations created when companions exist.

### Pitfall 3: Duplicate Association Records
**What goes wrong:** Running association twice creates duplicate links.
**Why it happens:** No unique constraint or "already linked" check.
**How to avoid:** (1) `UniqueConstraint("companion_id", "media_id")` on the join table. (2) Filter out companions that already have entries in the join table. (3) Use `INSERT ... ON CONFLICT DO NOTHING` for idempotency.
**Warning signs:** Duplicate rows in `file_companions` table.

### Pitfall 4: Pagination of Duplicate Groups
**What goes wrong:** LIMIT/OFFSET on the outer query limits files, not groups. You get partial groups.
**Why it happens:** Pagination applied to the wrong level.
**How to avoid:** Paginate the subquery (duplicate hashes), then fetch all files for those hashes.
**Warning signs:** A group showing 2 files when it actually has 5.

### Pitfall 5: Alembic Migration Import
**What goes wrong:** New model is not detected by `alembic revision --autogenerate`.
**Why it happens:** The model file is not imported in `models/__init__.py`.
**How to avoid:** Add `from phaze.models.file_companion import FileCompanion` to `models/__init__.py`.
**Warning signs:** Empty autogenerate output.

## Code Examples

### Alembic Migration for Join Table
```python
# alembic/versions/003_add_file_companions_table.py
"""Add file_companions join table.

Revision ID: 003
Revises: 002
Create Date: 2026-03-28
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision: str = "003"
down_revision: str | Sequence[str] | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "file_companions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("companion_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("media_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["companion_id"], ["files.id"], name="fk_file_companions_companion_id_files", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["media_id"], ["files.id"], name="fk_file_companions_media_id_files", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_file_companions"),
        sa.UniqueConstraint("companion_id", "media_id", name="uq_file_companions_pair"),
    )
    op.create_index("ix_file_companions_companion_id", "file_companions", ["companion_id"])
    op.create_index("ix_file_companions_media_id", "file_companions", ["media_id"])


def downgrade() -> None:
    op.drop_index("ix_file_companions_media_id", table_name="file_companions")
    op.drop_index("ix_file_companions_companion_id", table_name="file_companions")
    op.drop_table("file_companions")
```

### Pydantic Schemas
```python
# src/phaze/schemas/companion.py
from datetime import datetime
import uuid

from pydantic import BaseModel


class AssociateResponse(BaseModel):
    """Response from companion association endpoint."""
    new_associations: int
    message: str


class DuplicateFile(BaseModel):
    """A single file within a duplicate group."""
    id: uuid.UUID
    original_path: str
    file_size: int
    file_type: str


class DuplicateGroup(BaseModel):
    """A group of files sharing the same SHA256 hash."""
    sha256_hash: str
    count: int
    files: list[DuplicateFile]


class DuplicateGroupsResponse(BaseModel):
    """Paginated response of duplicate groups."""
    groups: list[DuplicateGroup]
    total_groups: int
    limit: int
    offset: int
```

### Router Endpoints
```python
# src/phaze/routers/companion.py
router = APIRouter(prefix="/api/v1", tags=["companion"])

@router.post("/associate")
async def trigger_association(
    session: AsyncSession = Depends(get_session),
) -> AssociateResponse:
    ...

@router.get("/duplicates")
async def list_duplicates(
    limit: int = 100,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> DuplicateGroupsResponse:
    ...
```

### Test Patterns (mock filesystem)
```python
# tests/test_services/test_companion.py
# Use tmp_path + discover_and_hash_files to create realistic DB state
# Then test associate_companions against that state

# tests/test_services/test_dedup.py
# Insert FileRecords with known sha256_hash duplicates
# Then test find_duplicate_groups returns correct grouping
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| SQLAlchemy `Table()` for join tables | Full model class extending Base | SQLAlchemy 2.0 | Mapped classes provide better type hints, IDE support |
| Sync session for queries | `AsyncSession` with `await` | SQLAlchemy 2.0 | Must use async patterns consistently |

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio |
| Config file | `pyproject.toml` [tool.pytest.ini_options] |
| Quick run command | `uv run pytest tests/ -x -q` |
| Full suite command | `uv run pytest --cov=phaze --cov-report=term-missing` |

### Phase Requirements to Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| ING-04 | SHA256 duplicate groups detected and queryable | unit + integration | `uv run pytest tests/test_services/test_dedup.py -x` | Wave 0 |
| ING-04 | Duplicates exposed via GET /api/v1/duplicates | integration | `uv run pytest tests/test_routers/test_companion.py -x` | Wave 0 |
| ING-06 | Companion files associated with media in same directory | unit + integration | `uv run pytest tests/test_services/test_companion.py -x` | Wave 0 |
| ING-06 | Association triggered via POST /api/v1/associate | integration | `uv run pytest tests/test_routers/test_companion.py -x` | Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/ -x -q`
- **Per wave merge:** `uv run pytest --cov=phaze --cov-report=term-missing`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_services/test_companion.py` -- covers ING-06 association logic
- [ ] `tests/test_services/test_dedup.py` -- covers ING-04 duplicate detection
- [ ] `tests/test_routers/test_companion.py` -- covers both ING-04 and ING-06 API endpoints

## Open Questions

1. **Large duplicate sets pagination performance**
   - What we know: 200K files, existing ix_files_sha256_hash index, GROUP BY on indexed column is fast
   - What's unclear: How many duplicate groups exist in practice (could be thousands)
   - Recommendation: Start with compute-on-demand. If slow (>500ms for typical queries), add a `duplicate_group_count` materialized view or caching. Monitor in production.

2. **Companion files in parent directories**
   - What we know: D-02 specifies same-directory matching only (`Path.parent == Path.parent`)
   - What's unclear: Are there cases where cover.jpg is one level up from the tracks?
   - Recommendation: Implement same-directory first per D-02. Parent-directory matching can be added later if needed.

## Project Constraints (from CLAUDE.md)

- **Python 3.13 only**, `uv run` for all commands
- **Pre-commit hooks** must pass before commits
- **85% code coverage** minimum
- **Type hints** on all functions (`disallow_untyped_defs = true`)
- **Double quotes**, 150-char line length
- **Ruff** for linting/formatting, **mypy** for type checking
- **Alembic** for migrations (async template)
- **Every feature gets its own PR** -- this phase is on branch `gsd/phase-03-companion-files-deduplication`
- **Commit frequently** during execution

## Sources

### Primary (HIGH confidence)
- Existing codebase: `src/phaze/models/file.py`, `services/ingestion.py`, `routers/scan.py`, `schemas/scan.py` -- established patterns
- Existing codebase: `alembic/versions/002_add_scan_batches_and_unique_path.py` -- migration pattern
- Existing codebase: `tests/test_services/test_ingestion.py` -- test patterns
- SQLAlchemy 2.0 documentation -- async session, mapped columns, relationships

### Secondary (MEDIUM confidence)
- PostgreSQL GROUP BY + HAVING performance on indexed columns -- well-established for datasets of this size

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - No new libraries, uses existing dependencies only
- Architecture: HIGH - Follows established codebase patterns exactly (router/schema/service/model layers)
- Pitfalls: HIGH - Well-understood SQL patterns; pitfalls are known and preventable
- Join table design: HIGH - Standard M:M pattern in SQLAlchemy 2.0
- Duplicate detection: HIGH - Simple GROUP BY query on existing indexed column

**Research date:** 2026-03-27
**Valid until:** 2026-04-27 (stable domain, no fast-moving dependencies)
