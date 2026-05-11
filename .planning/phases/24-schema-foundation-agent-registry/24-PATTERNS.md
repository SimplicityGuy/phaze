# Phase 24: Schema Foundation & Agent Registry - Pattern Map

**Mapped:** 2026-05-11
**Files analyzed:** 16 (9 new, 7 modified)
**Analogs found:** 16 / 16

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/phaze/models/agent.py` | model | declarative-schema | `src/phaze/models/tag_write_log.py` + `src/phaze/models/scan_batch.py` | exact (StrEnum-less hybrid) |
| `src/phaze/models/file.py` (mod) | model | declarative-schema | self (`__table_args__` block) | exact |
| `src/phaze/models/scan_batch.py` (mod) | model | declarative-schema | self + `src/phaze/models/file.py` (`__table_args__`) | exact |
| `src/phaze/models/__init__.py` (mod) | model-export-barrel | declarative-schema | self | exact |
| `src/phaze/services/ingestion.py` (mod) | service | CRUD / bulk-upsert | self (`bulk_upsert_files`) | exact |
| `alembic/versions/012_add_agents_table_and_backfill.py` | migration | DDL + raw-SQL DML | `alembic/versions/011_add_tag_write_log.py` (create_table shape) + `alembic/versions/002_add_scan_batches_and_unique_path.py` (FK + unique-index pattern) + `alembic/versions/009_add_search_vectors.py` (raw `op.execute` SQL) | exact composite |
| `alembic/versions/013_enforce_agent_id_not_null_and_swap_uniqueness.py` | migration | DDL constraint tightening | `alembic/versions/002_add_scan_batches_and_unique_path.py` (unique-index swap) + `alembic/versions/005_add_metadata_columns.py` (simple alter shape) | role-match |
| `tests/test_migrations/__init__.py` | test-package-marker | n/a | `tests/test_models/__init__.py` | exact |
| `tests/test_migrations/conftest.py` | test-fixture | alembic-driven DB lifecycle | `tests/conftest.py` (`async_engine` fixture) | role-match (must diverge: alembic upgrade instead of `create_all`) |
| `tests/test_migrations/test_012_upgrade.py` | test (integration) | DDL + DML assertions | `tests/test_models/test_tag_write_log.py` + `tests/test_services/test_ingestion.py` (integration block) | role-match |
| `tests/test_migrations/test_013_upgrade.py` | test (integration) | DDL constraint assertions | same as 012 | role-match |
| `tests/test_migrations/test_downgrade.py` | test (integration) | DDL rollback + error path | same as 012 | role-match |
| `tests/test_models/test_agent.py` | test (unit) | model assertions | `tests/test_models/test_tag_write_log.py` | exact |
| `tests/test_models/test_core_models.py` (mod) | test (unit) | model registry assertion | self (`test_all_tables_defined`) | exact |
| `tests/test_phase02_gaps.py` (mod) | test (unit) | StrEnum assertion | self (`test_scan_status_has_three_values`) | exact |
| `tests/test_services/test_ingestion.py` (mod) | test (integration) | bulk upsert with composite conflict target | self (`test_bulk_upsert_handles_duplicates`) | exact |

## Shared Patterns

### Constraint Naming Convention (project-wide invariant)

**Source:** `src/phaze/models/base.py` (lines 9–15)
**Apply to:** every new `Index`, `ForeignKey`, `PrimaryKeyConstraint`, `CheckConstraint`, `UniqueConstraint` in Phase 24

```python
convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
```

**Concrete consequences for Phase 24:**
- `CheckConstraint(..., name="id_charset")` on `Agent` → emits as `ck_agents_id_charset` (the `name="..."` you pass is appended *after* `ck_<tablename>_`).
- The migration `op.create_table` block also passes `sa.CheckConstraint(..., name="ck_agents_id_charset")` — but here the name is **already fully-qualified** because `op.create_table` does NOT run through the model `MetaData.naming_convention`. **Be intentional:** model side uses short `name="id_charset"`; migration side uses full `name="ck_agents_id_charset"`. Verify both render to the same constraint name in Postgres after upgrade.
- FK constraint names: `fk_files_agent_id_agents`, `fk_scan_batches_agent_id_agents` — exact pattern from `fk_files_batch_id_scan_batches` in migration 002.
- Unique index names: `uq_files_agent_id_original_path`, `uq_scan_batches_agent_id_live` — both manually-named via `Index(..., unique=True)` (NOT via the `MetaData` convention, since both are composite/partial).
- Plain index: `ix_scan_batches_agent_id`.

### TimestampMixin Inheritance

**Source:** `src/phaze/models/base.py` (lines 24–28)
**Apply to:** `Agent` model

```python
class TimestampMixin:
    """Mixin providing created_at and updated_at timestamp columns."""

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
```

**Concrete consequences:** `class Agent(TimestampMixin, Base)` — **mixin first, then `Base`** (matches every existing model: `FileRecord`, `ScanBatch`, `TagWriteLog`, etc.). DO NOT redeclare `created_at`/`updated_at` in `Agent`. The migration must still explicitly emit those columns in `op.create_table` since `create_table` does not consult Python mixins.

### Migration File Header / Revision Identifiers

**Source:** `alembic/versions/011_add_tag_write_log.py` (lines 1–19)
**Apply to:** migrations 012 and 013

```python
"""Add tag_write_log table for tag write audit trail.

Revision ID: 011
Revises: 010
Create Date: 2026-04-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "011"
down_revision: str | Sequence[str] | None = "010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
```

**Concrete consequences:**
- isort ordering: stdlib (`collections.abc`) → third-party (`sqlalchemy`) → `alembic.op` last (two blank lines after, per project ruff config `lines-after-imports = 2`).
- DO NOT use the boilerplate from `alembic/script.py.mako` (uses `from typing import Sequence, Union`) — existing 001–011 already migrated to `from collections.abc import Sequence` + PEP 604 unions.
- `revision: str = "012"`, `down_revision: str | Sequence[str] | None = "011"` for migration 012.
- `revision: str = "013"`, `down_revision: str | Sequence[str] | None = "012"` for migration 013.
- Docstring: one-line summary; no extended description needed (matches 002, 005, 011).
- 150-char line limit applies (CLAUDE.md). For long INSERT/UPDATE SQL, wrap inside `sa.text("""...""")` triple-quoted block or break the Python expression across lines using `(...)`.
- `print()` is forbidden by ruff `T20`; use `logger = logging.getLogger("alembic.runtime.migration")` when logging in migrations.

### Raw-SQL DML via `op.execute(sa.text(...))`

**Source:** `alembic/versions/009_add_search_vectors.py` (lines 22–48, 64–71)
**Apply to:** migration 012 backfill UPDATEs

```python
op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
op.execute(
    """
    ALTER TABLE files ADD COLUMN search_vector tsvector
    GENERATED ALWAYS AS (...) STORED
    """
)
op.execute("CREATE INDEX ix_files_search_vector ON files USING gin(search_vector)")
```

**Note:** 009 uses `op.execute("string-literal")`. The Phase 24 pattern (per D-14 and RESEARCH Pattern 4) is `op.execute(sa.text("..."))` for parameterized statements. Both are valid; **prefer `sa.text(...)` consistently in 012/013 to keep static analysis happy**. For parameter-binding INSERTs use `op.get_bind().execute(sa.text("INSERT ... VALUES (:id, :name, ...)"), {"id": "...", "name": "..."})` — NEVER f-string-interpolate values.

---

## Pattern Assignments

### `src/phaze/models/agent.py` (model, declarative-schema) — NEW

**Analog:** `src/phaze/models/tag_write_log.py` (most-recent model, established TimestampMixin + JSONB + StrEnum-less pattern) blended with `src/phaze/models/scan_batch.py` (TimestampMixin order, enum-less helper section).

**Imports pattern** (mirror `tag_write_log.py` lines 1–14, drop unused imports):

```python
"""Agent model - file-server identity for the v4.0 distributed-agents milestone."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — SQLAlchemy resolves Mapped[] annotations at runtime

from sqlalchemy import CheckConstraint, DateTime, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin
```

- The `# noqa: TC003` comment is project-specific and present on `tag_write_log.py:5`. Use the same comment verbatim — runtime mapping of `datetime` needs it (TCH would otherwise want it under `TYPE_CHECKING`).
- `from __future__ import annotations` is established in `file.py:3` for PEP 604 `str | None` style annotations. Keep it.
- `text` is imported from top-level `sqlalchemy` (NOT `sqlalchemy.sql`) — matches the `scan_batch.py` Example 2 in RESEARCH.

**Core model pattern** (mirror `tag_write_log.py:29–53`):

```python
class Agent(TimestampMixin, Base):
    """Agent (file server identity) that owns FileRecord and ScanBatch rows."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scan_roots: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
            name="id_charset",
        ),
    )
```

**Critical conventions copied:**
- `Mapped[type]` + `mapped_column(...)` 2.0 style (every model in `src/phaze/models/`).
- `nullable=True` is explicit on optional columns (matches `tag_write_log.py:44–45`, `scan_batch.py:31`).
- `server_default=text("'[]'::jsonb")` follows the JSONB-default pattern; cast in single quotes inside `text("...")` is the form Alembic and SQLAlchemy both render verbatim.
- `Mapped[list[str]]` for JSONB array — per RESEARCH Pitfall 4, this passes mypy strict without `# type: ignore`. Verify with `uv run mypy .`.
- `CheckConstraint` `name="id_charset"` (NOT `"ck_agents_id_charset"`) — the naming-convention dict in `base.py` will prefix `ck_agents_` automatically. The full constraint name in Postgres will be `ck_agents_id_charset`.
- DO NOT add reverse `relationship("FileRecord")` / `relationship("ScanBatch")` back-references — explicitly deferred per CONTEXT (`<deferred>` section).
- DO NOT use `sa.dialects.postgresql.JSONB` as a module-path; the established import is `from sqlalchemy.dialects.postgresql import JSONB` (see `tag_write_log.py:11`).

---

### `src/phaze/models/file.py` (modified, model, declarative-schema)

**Analog:** self — the existing `__table_args__` block at lines 52–56 is the constraint being swapped.

**Imports pattern** (must add `ForeignKey` is **already** imported at line 9; no import edits needed beyond Alembic verifying):

```python
# CURRENT (line 9):
from sqlalchemy import BigInteger, ForeignKey, Index, String, Text
```

**Current `__table_args__`** (lines 52–56) — what is being changed:

```python
__table_args__ = (
    Index("ix_files_state", "state"),
    Index("ix_files_sha256_hash", "sha256_hash"),
    Index("uq_files_original_path", "original_path", unique=True),
)
```

**Target `__table_args__`** (post-Phase 24 final model state — represents the schema after migration 013):

```python
__table_args__ = (
    Index("ix_files_state", "state"),
    Index("ix_files_sha256_hash", "sha256_hash"),
    Index("uq_files_agent_id_original_path", "agent_id", "original_path", unique=True),
)
```

**Target column addition** (insert immediately after `batch_id` at line 48, mirroring the FK declaration style there):

```python
agent_id: Mapped[str] = mapped_column(
    String(64),
    ForeignKey("agents.id", ondelete="RESTRICT"),
    nullable=False,
)
```

**Pattern from `batch_id` declaration to mirror** (line 48):

```python
batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("scan_batches.id"), nullable=True)
```

- FK target syntax `"agents.id"` matches `"scan_batches.id"`.
- `ondelete="RESTRICT"` is **new** (existing `batch_id` FK is unrestricted; per D-03 the agent FK must RESTRICT).
- `nullable=False` is the *final* state. RESEARCH Example 1 callout: between migration 012 and 013 the DB column is nullable, but the model declaration represents post-013 truth and the Phase 24 plan must run both migrations together.

---

### `src/phaze/models/scan_batch.py` (modified, model, declarative-schema)

**Analog:** self + `src/phaze/models/file.py:9–11, 48, 52–56` (FK shape, Index in `__table_args__`).

**Current full file** (32 lines) needs three edits: add `LIVE` enum value, add `agent_id` FK column, add `__table_args__` with two indexes.

**Imports pattern** (current line 6 needs additions):

```python
# CURRENT:
from sqlalchemy import Integer, String, Text

# TARGET:
from sqlalchemy import ForeignKey, Index, Integer, String, Text, text
```

- Alphabetical isort order, per ruff `I` rule + project `combine-as-imports = true`.
- `text` (lowercase) is needed for `postgresql_where=text("status = 'live'")` predicate (mirrors `tag_write_log.py:10` which uses `func`).
- `from sqlalchemy.orm import Mapped, mapped_column` already present at line 8.

**ScanStatus enum target** (lines 13–18):

```python
class ScanStatus(enum.StrEnum):
    """Status of a scan batch operation."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    LIVE = "live"  # Watcher-originated sentinel; one per agent (D-09, D-10)
```

- Lowercase `"live"` value — RESEARCH Example 2 callout: matches existing enum-value casing convention (`"running"`, `"completed"`, `"failed"`); CONTEXT D-12 says `'LIVE'` but that's the Python name, not the stored string. **The partial-index WHERE predicate must use `'live'` (lowercase)** to match what the column actually stores.

**Add `agent_id` column** (between line 26 `id` and line 27 `scan_path`):

```python
agent_id: Mapped[str] = mapped_column(
    String(64),
    ForeignKey("agents.id", ondelete="RESTRICT"),
    nullable=False,
)
```

**Add `__table_args__`** (new block after line 31):

```python
__table_args__ = (
    Index("ix_scan_batches_agent_id", "agent_id"),
    Index(
        "uq_scan_batches_agent_id_live",
        "agent_id",
        unique=True,
        postgresql_where=text("status = 'live'"),
    ),
)
```

- `postgresql_where=text("status = 'live'")` — verbatim from RESEARCH Pattern 2. The string literal must match between this model declaration and the `op.create_index(..., postgresql_where=sa.text("status = 'live'"))` call in migration 012 (RESEARCH Pitfall 3 — whitespace/case drift would confuse `alembic check`).
- Index ordering: `ix_` plain index first, then partial-unique index second (mirrors `file.py:52–56` ordering of plain indexes then unique).
- No relationship needed (deferred).

---

### `src/phaze/models/__init__.py` (modified, model-export-barrel)

**Analog:** self — pattern is alphabetical, isort-sorted imports + alphabetical `__all__` list.

**Pattern** (current lines 3–13 + lines 16–35):

```python
from phaze.models.analysis import AnalysisResult
from phaze.models.discogs_link import DiscogsLink
# ...
from phaze.models.scan_batch import ScanBatch, ScanStatus
```

**Edits required:**
1. Add `from phaze.models.agent import Agent` — insert alphabetically (after `from phaze.models.analysis import AnalysisResult` is wrong because `agent` < `analysis`; the new line goes **first** in the from-block at line 3).
2. Add `"Agent"` to `__all__` — insert alphabetically first (line 17, before `"AnalysisResult"`).

**Critical:** `alembic/env.py:12` does `from phaze.models import *  # noqa: F403`, so the `__all__` list is consulted for autogenerate. Missing `"Agent"` here would silently exclude the new model from autogen diffs.

---

### `src/phaze/services/ingestion.py` (modified, service, CRUD/bulk-upsert)

**Analog:** self — the `bulk_upsert_files` function at lines 89–117 plus `discover_and_hash_files` at lines 44–86.

**Current `discover_and_hash_files` record-dict shape** (lines 72–84) — the change adds one key:

```python
records.append(
    {
        "id": uuid.uuid4(),
        "sha256_hash": sha256_hash,
        "original_path": normalized_path,
        "original_filename": normalized_filename,
        "current_path": normalized_path,
        "file_type": file_ext,
        "file_size": file_size,
        "state": FileState.DISCOVERED,
        "batch_id": batch_id,
    }
)
```

**Target shape:** add `"agent_id": LEGACY_AGENT_ID,` to every record dict.

**Module-level constant to add** (place near other module-level identifiers above `logger`, line ~26):

```python
LEGACY_AGENT_ID = "legacy-application-server"  # Phase 24 placeholder; Phase 25 wires real attribution per agent.
```

- All-caps module constant per ruff `N` conventions (not currently enforced but matches `BULK_INSERT_BATCH_SIZE` style used elsewhere in `phaze.constants`).
- Inline comment cross-references the Phase 25 follow-up per RESEARCH Pitfall 1 recommendation (b).

**Current `bulk_upsert_files` conflict target** (lines 103–113):

```python
stmt = pg_insert(FileRecord).values(batch_list)
stmt = stmt.on_conflict_do_update(
    index_elements=["original_path"],
    set_={
        "sha256_hash": stmt.excluded.sha256_hash,
        "file_size": stmt.excluded.file_size,
        "state": stmt.excluded.state,
        "batch_id": stmt.excluded.batch_id,
        "file_type": stmt.excluded.file_type,
    },
)
```

**Target conflict target:**

```python
stmt = stmt.on_conflict_do_update(
    index_elements=["agent_id", "original_path"],  # composite UQ swapped in migration 013
    set_={
        "sha256_hash": stmt.excluded.sha256_hash,
        "file_size": stmt.excluded.file_size,
        "state": stmt.excluded.state,
        "batch_id": stmt.excluded.batch_id,
        "file_type": stmt.excluded.file_type,
    },
)
```

- `index_elements` list order MUST match the column order in `uq_files_agent_id_original_path` (which is `("agent_id", "original_path")` per RESEARCH Example 1).
- `set_` block is unchanged.

**Also update `run_scan` ScanBatch construction** (lines 133–139):

```python
batch = ScanBatch(
    id=batch_id,
    scan_path=scan_path,
    status=ScanStatus.RUNNING,
    total_files=0,
    processed_files=0,
)
```

**Target:** add `agent_id=LEGACY_AGENT_ID,` (per RESEARCH Example 3 footnote: "`run_scan` must also set `agent_id=LEGACY_AGENT_ID` when creating the ScanBatch row.")

---

### `alembic/versions/012_add_agents_table_and_backfill.py` (NEW, migration, DDL + raw-SQL DML)

**Analog:** Combine three established patterns:
1. `alembic/versions/011_add_tag_write_log.py` for create_table shape + named constraints + FK declarations + index creation + downgrade ordering.
2. `alembic/versions/002_add_scan_batches_and_unique_path.py` for the FK + unique-index + downgrade structure (very close shape to what 012 does).
3. `alembic/versions/009_add_search_vectors.py` for raw `op.execute(...)` SQL emission.

**Header + identifiers** (mirror `011_add_tag_write_log.py:1–19` verbatim with revision IDs swapped):

```python
"""Add agents table, agent_id columns, FKs, and backfill legacy agent.

Revision ID: 012
Revises: 011
Create Date: 2026-05-11
"""

from collections.abc import Sequence
import json
import logging
import os
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "012"
down_revision: str | Sequence[str] | None = "011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


logger = logging.getLogger("alembic.runtime.migration")
```

- isort ordering: stdlib block (`collections.abc`, `json`, `logging`, `os`, `uuid`) → third-party (`sqlalchemy`, `sqlalchemy.dialects`) → first-party (`alembic`). Match this **exactly** — pre-commit ruff `I` will reject deviation.
- Two blank lines after import block (project ruff config `lines-after-imports = 2`).
- `logger` at module level — same pattern as `services/ingestion.py:27`.
- `from sqlalchemy.dialects import postgresql` matches `002_add_scan_batches_and_unique_path.py:11` (used for `postgresql.UUID(as_uuid=True)` and `postgresql.JSONB`).

**`upgrade()` core pattern** (combines 011 create_table style + 002 FK/index + 009 raw SQL):

```python
def upgrade() -> None:
    """Create agents table, seed legacy agent + sentinel, add agent_id columns + FKs, backfill."""
    # 1. Create agents table — pattern from 011_add_tag_write_log.py:24–39
    op.create_table(
        "agents",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("token_hash", sa.String(128), nullable=True),
        sa.Column("scan_roots", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agents"),
        sa.CheckConstraint("id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="ck_agents_id_charset"),
    )

    # 2. Resolve SCAN_PATH from env, log resolution, seed legacy agent
    raw_scan_path = os.environ.get("SCAN_PATH", "/data/music")
    scan_roots_json = json.dumps([raw_scan_path])
    logger.info(
        "phaze-024: resolved legacy-application-server scan_roots=%s (SCAN_PATH=%r)",
        scan_roots_json,
        raw_scan_path,
    )
    op.get_bind().execute(
        sa.text(
            "INSERT INTO agents (id, name, token_hash, scan_roots, revoked_at, created_at, updated_at) "
            "VALUES (:id, :name, NULL, CAST(:scan_roots AS jsonb), NOW(), NOW(), NOW())"
        ),
        {"id": "legacy-application-server", "name": "legacy-application-server", "scan_roots": scan_roots_json},
    )

    # 3. Add nullable agent_id columns — pattern from 005_add_metadata_columns.py:24–26
    op.add_column("files", sa.Column("agent_id", sa.String(64), nullable=True))
    op.add_column("scan_batches", sa.Column("agent_id", sa.String(64), nullable=True))

    # 4. FKs — pattern from 002_add_scan_batches_and_unique_path.py:43
    op.create_foreign_key(
        "fk_files_agent_id_agents", "files", "agents", ["agent_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_scan_batches_agent_id_agents", "scan_batches", "agents", ["agent_id"], ["id"], ondelete="RESTRICT",
    )

    # 5. Plain index on scan_batches.agent_id
    op.create_index("ix_scan_batches_agent_id", "scan_batches", ["agent_id"])

    # 6. Backfill — pattern from 009_add_search_vectors.py (op.execute) but parameterized via sa.text
    op.execute(sa.text("UPDATE files SET agent_id = 'legacy-application-server' WHERE agent_id IS NULL"))
    op.execute(sa.text("UPDATE scan_batches SET agent_id = 'legacy-application-server' WHERE agent_id IS NULL"))

    # 7. Sentinel LIVE scan_batch for legacy agent (Python-generated UUID per RESEARCH Pattern 4 recommendation)
    sentinel_id = uuid.uuid4()
    op.get_bind().execute(
        sa.text(
            "INSERT INTO scan_batches (id, agent_id, scan_path, status, total_files, processed_files, "
            "created_at, updated_at) "
            "VALUES (:id, :agent_id, '<watcher>', 'live', 0, 0, NOW(), NOW())"
        ),
        {"id": sentinel_id, "agent_id": "legacy-application-server"},
    )

    # 8. Partial UQ for sentinel — after the INSERT so the first run cannot violate it
    op.create_index(
        "uq_scan_batches_agent_id_live",
        "scan_batches",
        ["agent_id"],
        unique=True,
        postgresql_where=sa.text("status = 'live'"),
    )
```

**Critical conventions copied:**
- `sa.Column("col", sa.Type, ...)` form (NOT `sa.Column("col", sa.Type(), ...)` with parentheses on simple types) — matches 011 line 26–37 and 002 line 28–35. EXCEPTION: `sa.String(64)` is parameterized so it stays `sa.String(64)`; `sa.DateTime(timezone=True)` is parameterized; for `sa.Text` and `sa.Integer` no parens.
- `sa.PrimaryKeyConstraint("id", name="pk_<table>")` is **explicitly** emitted in `op.create_table` per 002:36 and 011:37 — even though `primary_key=True` is on the column, the explicit constraint with the convention-prefixed name is the established pattern.
- `sa.CheckConstraint(..., name="ck_agents_id_charset")` uses the **fully-qualified** name (NOT `"id_charset"`) because `op.create_table` does NOT consult `Base.metadata.naming_convention`.
- `server_default=sa.func.now()` for timestamp columns (matches 011:34–36, 002:34–35).
- `server_default=sa.text("'[]'::jsonb")` for JSONB array default — `sa.text("'<sql-literal>'")` is the consistent project form.
- Raw SQL value `'<watcher>'` is the literal string with angle brackets, per D-10.
- Sentinel `status` value is `'live'` lowercase (matches enum storage convention per RESEARCH Example 2 callout).
- ON DELETE RESTRICT is via the `ondelete="RESTRICT"` kwarg on `op.create_foreign_key` (matches the kwarg signature in 002:43; verified via Alembic docs).
- `op.get_bind().execute(sa.text(...), {bind_params})` is the parameterized-INSERT form. NEVER use f-string interpolation on operator-controlled values (RESEARCH Security Domain "SQL injection via slug in migration").
- The CHECK regex literal `'^[a-z0-9]+(-[a-z0-9]+)*$'` must match **byte-for-byte** the regex in `src/phaze/models/agent.py` (RESEARCH Pitfall 3).

**`downgrade()` pattern** (reverse order, drop everything 012 created — pattern from 011:45–49 + 002:46–50):

```python
def downgrade() -> None:
    """Drop partial UQ, sentinel, agent_id columns, FKs, and agents table."""
    op.drop_index("uq_scan_batches_agent_id_live", table_name="scan_batches")
    op.execute(sa.text("DELETE FROM scan_batches WHERE status = 'live'"))
    op.drop_index("ix_scan_batches_agent_id", table_name="scan_batches")
    op.drop_constraint("fk_scan_batches_agent_id_agents", "scan_batches", type_="foreignkey")
    op.drop_constraint("fk_files_agent_id_agents", "files", type_="foreignkey")
    op.drop_column("scan_batches", "agent_id")
    op.drop_column("files", "agent_id")
    op.drop_table("agents")
```

- `op.drop_index(name, table_name=...)` — kwarg form matches 002:49.
- `op.drop_constraint(name, table, type_="foreignkey")` — matches 002:48.
- `op.drop_column(table, col)` then `op.drop_table(table)` — natural reverse order.
- Backfill UPDATEs do not need to be reversed (NULLs are fine after column drop).

---

### `alembic/versions/013_enforce_agent_id_not_null_and_swap_uniqueness.py` (NEW, migration, DDL constraint tightening)

**Analog:** `alembic/versions/002_add_scan_batches_and_unique_path.py` (the unique-index pattern being swapped) + `alembic/versions/005_add_metadata_columns.py` (simple alter shape).

**Header** (same shape as 012, with revision IDs `"013"` / down `"012"`).

**Imports** (minimal — no JSONB, no uuid):

```python
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
```

**`upgrade()`** (per RESEARCH Pattern 4 Revision 013 + drop/create pair from 002):

```python
def upgrade() -> None:
    """Enforce NOT NULL on agent_id columns and swap files unique constraint to composite."""
    op.alter_column("files", "agent_id", nullable=False, existing_type=sa.String(64))
    op.alter_column("scan_batches", "agent_id", nullable=False, existing_type=sa.String(64))

    op.drop_index("uq_files_original_path", table_name="files")
    op.create_index(
        "uq_files_agent_id_original_path",
        "files",
        ["agent_id", "original_path"],
        unique=True,
    )
```

- `op.alter_column(table, col, nullable=False, existing_type=...)` — Alembic's standard signature; `existing_type` is required for asyncpg to render correct DDL.
- `op.drop_index("uq_files_original_path", table_name="files")` — exact mirror of 002:49 in reverse.
- `op.create_index(..., unique=True)` — composite form. Column order MUST be `["agent_id", "original_path"]` so the leading-column lookup property (RESEARCH D-15) holds.

**`downgrade()`** (RESEARCH Pattern 5 — dupe-detection guard):

```python
def downgrade() -> None:
    """Reverse 013: dupe-check first, then swap unique back to single column and relax NOT NULL."""
    bind = op.get_bind()
    dupes = bind.execute(
        sa.text(
            "SELECT original_path FROM files "
            "GROUP BY original_path HAVING COUNT(*) > 1 LIMIT 5"
        )
    ).scalars().all()
    if dupes:
        raise RuntimeError(
            "Cannot downgrade 013->012: original_path is no longer unique across agents. "
            f"Example collisions: {dupes!r}. "
            "Resolve manually before retrying. Silent dedup is FORBIDDEN per phase-24 D-16."
        )

    op.drop_index("uq_files_agent_id_original_path", table_name="files")
    op.create_index("uq_files_original_path", "files", ["original_path"], unique=True)
    op.alter_column("scan_batches", "agent_id", nullable=True, existing_type=sa.String(64))
    op.alter_column("files", "agent_id", nullable=True, existing_type=sa.String(64))
```

- `bind.execute(sa.text(...)).scalars().all()` is the SQLAlchemy 2.0 sync-connection pattern (RESEARCH Pattern 5 citation).
- `RuntimeError` is the canonical exception (RESEARCH verified that no existing migration raises click exceptions — they all rely on Alembic's natural exception propagation).
- Error message limits to 5 examples for readability; uses `repr()` (`!r`) for safe quoting.
- The string `"Cannot downgrade 013->012:"` uses ASCII `->`, not `→`, per ruff/yamllint conventions (no unicode in error strings without justification).

---

### `tests/test_migrations/__init__.py` (NEW, test-package-marker)

**Analog:** `tests/test_models/__init__.py` (and `tests/test_services/__init__.py`).

```bash
$ cat /Users/Robert/Code/public/phaze/tests/test_models/__init__.py
# (empty file — package marker only)
```

**Pattern:** create an empty file (zero bytes). Pre-commit `end-of-file-fixer` may add a trailing newline but the file must contain no module-level code.

---

### `tests/test_migrations/conftest.py` (NEW, test-fixture)

**Analog:** `tests/conftest.py` (lines 27–36) — the `async_engine` fixture pattern. **Must diverge** because `tests/conftest.py` uses `Base.metadata.create_all`, which sidesteps the actual migration file (RESEARCH Pitfall 2). The new conftest must run `alembic.command.upgrade(...)` against a real Postgres DB.

**Reference pattern from `tests/conftest.py:1–53`:**

```python
"""Shared test fixtures for Phaze test suite."""

from collections.abc import AsyncGenerator

from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.database import get_session
from phaze.main import create_app
from phaze.models.base import Base


TEST_DATABASE_URL = "postgresql+asyncpg://phaze:phaze@localhost:5432/phaze_test"

@pytest_asyncio.fixture
async def async_engine():  # type: ignore[no-untyped-def]
    """Create async engine, set up tables, yield, then tear down."""
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
```

**What the new conftest must change:**
- Replace `await conn.run_sync(Base.metadata.create_all)` with an alembic upgrade call.
- Provide helpers `upgrade_to(cfg, rev)` and `downgrade_to(cfg, rev)` (callable from tests to step between 011 / 012 / 013).
- Use a **distinct test database** (e.g., `phaze_migrations_test`) OR an isolated Postgres SCHEMA per test, so the alembic upgrade history does not collide with the default `async_engine` fixture's parallel test DB.
- The `[Alembic Config]` object must be constructed in-Python (NOT loaded from `alembic.ini`) so the `sqlalchemy.url` can be overridden per test.

**Target skeleton** (concrete from RESEARCH Pitfall 2 + Validation Architecture):

```python
"""Fixtures for tests that actually run Alembic migrations against a real Postgres DB."""

from collections.abc import AsyncGenerator
from pathlib import Path

from alembic.config import Config
from alembic import command
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


MIGRATIONS_TEST_DATABASE_URL = "postgresql+asyncpg://phaze:phaze@localhost:5432/phaze_migrations_test"
ALEMBIC_INI_PATH = Path(__file__).resolve().parents[2] / "alembic.ini"


def _build_alembic_config(database_url: str) -> Config:
    """Build an in-memory Alembic Config pointing at the test DB.

    Uses sync URL (postgresql+psycopg2 or postgresql+asyncpg auto-resolves at alembic env).
    """
    cfg = Config(str(ALEMBIC_INI_PATH))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


@pytest_asyncio.fixture
async def migrated_engine() -> AsyncGenerator:  # type: ignore[no-untyped-def]
    """Upgrade to head, yield engine, downgrade to base on teardown."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    command.upgrade(cfg, "head")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        yield engine
    finally:
        await engine.dispose()
        command.downgrade(cfg, "base")
```

- DO NOT add this fixture to the parent `tests/conftest.py` — that would slow down every unrelated test. Keep it scoped to `tests/test_migrations/`.
- Use `pytest_asyncio.fixture` (matches existing pattern at `tests/conftest.py:27`).
- The `# type: ignore[no-untyped-def]` comment mirrors the parent conftest exactly (mypy strict requires it on bare `async def` fixtures lacking yield-type annotations).
- Path to `alembic.ini` is computed relative to this file (parents[2] = `tests/test_migrations/conftest.py` → `tests/test_migrations/` → `tests/` → repo root).

**Pre-condition (operator setup):** the database `phaze_migrations_test` must exist on `localhost:5432`. RESEARCH Environment Availability table notes this is already the standing requirement for integration tests in this project. Add a note to the conftest docstring.

---

### `tests/test_migrations/test_012_upgrade.py` (NEW, test integration)

**Analog:** `tests/test_models/test_tag_write_log.py` (model assertions style) + `tests/test_services/test_ingestion.py:209–299` (async DB integration block pattern).

**Imports + structure pattern** (from `test_tag_write_log.py:1–8` + `test_ingestion.py:201–211`):

```python
"""Tests for migration 012: agents table + backfill."""

import json
import os
from unittest.mock import patch

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_legacy_agent_exists(migrated_engine) -> None:  # type: ignore[no-untyped-def]
    """The legacy-application-server agent row is present after upgrade."""
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text("SELECT id, name, token_hash, revoked_at FROM agents WHERE id = 'legacy-application-server'"))
        row = result.one()
    assert row.id == "legacy-application-server"
    assert row.name == "legacy-application-server"
    assert row.token_hash is None
    assert row.revoked_at is not None
```

**Critical conventions copied:**
- `@pytest.mark.asyncio` matches the existing pattern at `tests/test_services/test_ingestion.py:211`.
- `# type: ignore[no-untyped-def]` matches the convention used wherever pytest fixtures take untyped fixture parameters (`session` fixture, `async_engine` fixture, etc.).
- `async with migrated_engine.connect() as conn:` is the SQLAlchemy 2.0 async-engine query pattern.
- `text(...)` (lowercase from `sqlalchemy`) used for raw SQL assertions — matches the established style in `tests/test_services/test_ingestion.py:214` (`from sqlalchemy import func, select`).

**Test coverage map** (from RESEARCH Validation Architecture):
- `test_agents_table_columns` — DATA-01
- `test_scan_roots_is_jsonb` — DATA-01
- `test_id_charset_check` — DATA-01 (SQL INSERT failures for bad slugs)
- `test_token_hash_nullable` — DATA-01
- `test_legacy_sentinel_exists` — DATA-03
- `test_partial_uq_rejects_dup_live` — DATA-03
- `test_partial_uq_allows_multiple_non_live` — DATA-03
- `test_legacy_agent_scan_roots_from_env` — DATA-04 (use `patch.dict(os.environ, {"SCAN_PATH": "/custom"}, clear=False)` BEFORE calling `command.upgrade` — but the fixture pre-upgrades, so this test needs a per-test cfg/upgrade pair OR a separate fixture)
- `test_legacy_agent_scan_roots_fallback` — DATA-04 (env var absent → `/data/music`)
- `test_legacy_agent_born_revoked` — DATA-04
- `test_sentinel_scan_path_literal` — DATA-04 (assert `scan_path == "<watcher>"`)
- `test_backfill_files`, `test_backfill_scan_batches` — DATA-04 (insert pre-012 rows via psql before upgrade, or use migration 011 head as a pre-state)

**Special note on env-var tests:** since the conftest fixture upgrades to head *before* yielding, tests that need a specific `SCAN_PATH` need either (a) a different fixture that delays upgrade or (b) accept that they re-run upgrade with `patch.dict(os.environ, ...)` from a known intermediate revision. **Design choice deferred to planner** — both patterns are valid; recommend (a) for cleanliness.

---

### `tests/test_migrations/test_013_upgrade.py` (NEW, test integration)

**Analog:** `tests/test_migrations/test_012_upgrade.py` (same fixture, same style) + `tests/test_services/test_ingestion.py:251–299` (`test_bulk_upsert_handles_duplicates`) for the dup-rejection assertion shape.

**Coverage map** (from RESEARCH Validation Architecture):
- `test_files_agent_id_not_null` — DATA-02 (`SELECT is_nullable FROM information_schema.columns WHERE table_name='files' AND column_name='agent_id'`)
- `test_same_path_different_agent` — DATA-02 (insert two files with same `original_path` but different `agent_id`; both succeed)
- `test_composite_unique_rejects_dup` — DATA-02 (insert same `(agent_id, original_path)` twice; second raises `IntegrityError`)
- `test_old_unique_dropped` — DATA-02 (assert `uq_files_original_path` no longer in `pg_indexes` for `files`)
- `test_scan_batches_agent_id_not_null` — DATA-03

**IntegrityError-on-dup pattern** (mirror `test_ingestion.py:289–298` style but expect failure):

```python
from sqlalchemy.exc import IntegrityError

with pytest.raises(IntegrityError):
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO files (...) VALUES (..., 'agent-x', '/p/a'), (..., 'agent-x', '/p/a')"))
```

---

### `tests/test_migrations/test_downgrade.py` (NEW, test integration)

**Analog:** the same conftest fixture; tests use `command.downgrade(cfg, rev)` explicitly. There is **no existing analog** for downgrade tests — Phase 24 establishes this pattern.

**Coverage map** (from RESEARCH Validation Architecture):
- `test_downgrade_013_clean` — DATA-04 (downgrade 013→012 on a clean DB succeeds; `uq_files_original_path` restored)
- `test_downgrade_013_fails_on_dupes` — DATA-04 + D-16 (insert two files with same `original_path` under different agents at head, attempt downgrade, assert `RuntimeError` raised with "Cannot downgrade 013->012")
- `test_downgrade_012_clean` — DATA-04 (downgrade 012→011 on clean DB: `agents` table dropped, `agent_id` columns removed, all v3.0 indexes restored)

**Error-match pattern** (use `pytest.raises(RuntimeError, match=...)` per existing `test_phase02_gaps.py:171`):

```python
with pytest.raises(RuntimeError, match="Cannot downgrade 013->012"):
    command.downgrade(cfg, "012")
```

---

### `tests/test_models/test_agent.py` (NEW, test unit)

**Analog:** `tests/test_models/test_tag_write_log.py` (lines 1–80) — exact same shape: imports → `TestX` class for enum → `TestY` class for model.

**Imports + structure** (mirror `test_tag_write_log.py:1–8`):

```python
"""Tests for Agent model."""

from phaze.models.agent import Agent
from phaze.models.base import Base
```

**Test class pattern** (mirror `test_tag_write_log.py:25–80`):

```python
class TestAgent:
    """Tests for Agent model."""

    def test_table_name(self) -> None:
        assert Agent.__tablename__ == "agents"

    def test_table_in_metadata(self) -> None:
        assert "agents" in Base.metadata.tables

    def test_required_columns(self) -> None:
        columns = {c.name for c in Agent.__table__.columns}
        required = {"id", "name", "token_hash", "scan_roots", "last_seen_at", "revoked_at", "created_at", "updated_at"}
        assert required.issubset(columns)

    def test_id_is_primary_key(self) -> None:
        pk_cols = [c.name for c in Agent.__table__.primary_key.columns]
        assert pk_cols == ["id"]

    def test_token_hash_nullable(self) -> None:
        col = Agent.__table__.c.token_hash
        assert col.nullable is True

    def test_scan_roots_jsonb(self) -> None:
        col = Agent.__table__.c.scan_roots
        assert "JSONB" in str(col.type)

    def test_check_constraint_present(self) -> None:
        names = {c.name for c in Agent.__table__.constraints}
        assert "ck_agents_id_charset" in names
```

- Class-based grouping (`class TestAgent:`) matches `test_tag_write_log.py:25`.
- Functions are bare `def`, no `self` type annotation, `-> None` return — matches `test_tag_write_log.py:28`.
- `Agent.__table__.c.<col>` access pattern matches `test_tag_write_log.py:56`.
- `"JSONB" in str(col.type)` matches `test_core_models.py:49` `test_metadata_has_jsonb_column`.

---

### `tests/test_models/test_core_models.py` (modified, test unit)

**Analog:** self — `test_all_tables_defined` at lines 7–25.

**Current expected set** (lines 10–24):

```python
expected = {
    "files",
    "metadata",
    "analysis",
    "proposals",
    "execution_log",
    "scan_batches",
    "file_companions",
    "fingerprint_results",
    "tracklists",
    "tracklist_versions",
    "tracklist_tracks",
    "discogs_links",
    "tag_write_log",
}
```

**Target:** add `"agents",` (insert alphabetically near top, after `"analysis"` works fine — current set ordering is loose). Update the docstring `"""All 13 expected tables should be defined in metadata."""` to **"All 14 expected tables..."**.

---

### `tests/test_phase02_gaps.py` (modified, test unit)

**Analog:** self — `test_scan_status_has_three_values` at lines 30–37.

**Current assertion** (lines 30–37):

```python
def test_scan_status_has_three_values() -> None:
    """ScanStatus enum contains exactly RUNNING, COMPLETED, FAILED."""
    members = list(ScanStatus)
    assert len(members) == 3
    assert ScanStatus.RUNNING == "running"
    assert ScanStatus.COMPLETED == "completed"
    assert ScanStatus.FAILED == "failed"
```

**Target:** rename or expand this test. Options:
- **Option A (rename + expand):** `test_scan_status_has_four_values` → `len(members) == 4` + `assert ScanStatus.LIVE == "live"`.
- **Option B (add new test):** Keep existing renamed to `test_scan_status_has_legacy_three_plus_live` or add a second `test_scan_status_includes_live` test that only checks `LIVE`.

Recommend Option A: update docstring + count + add the `LIVE` assertion. Match the existing one-line-per-value style.

---

### `tests/test_services/test_ingestion.py` (modified, test integration)

**Analog:** self — `test_bulk_upsert_handles_duplicates` at lines 251–299.

**Pattern to copy and adapt:** the existing test creates one `ScanBatch`, calls `bulk_upsert_files` twice with the same `original_path`, asserts row count = 1 and updated hash. The new behavior requires:
1. Every record dict must now include `"agent_id": "legacy-application-server"` (or other valid agent slug).
2. The test setup must insert a corresponding `Agent` row first (`session.add(Agent(id="legacy-application-server", name="legacy-application-server", scan_roots=["/music"]))`).
3. Need a new test `test_bulk_upsert_same_path_different_agent` showing that `(agent_id="a", original_path="/p")` and `(agent_id="b", original_path="/p")` coexist — directly exercises the composite conflict target.
4. The existing `test_bulk_upsert_stores_paths` and `test_bulk_upsert_handles_duplicates` tests need their record dicts updated to include `agent_id`.

**Record-dict shape change** (current lines 223–236, 262–272, 278–288):

```python
# CURRENT:
records = [
    {
        "id": uuid.uuid4(),
        "sha256_hash": f"{'a' * 63}{i}",
        "original_path": f"/music/song{i}.mp3",
        "original_filename": f"song{i}.mp3",
        "current_path": f"/music/song{i}.mp3",
        "file_type": "mp3",
        "file_size": 1000 + i,
        "state": FileState.DISCOVERED,
        "batch_id": batch_id,
    }
    for i in range(5)
]

# TARGET: add "agent_id": LEGACY_AGENT_ID (after batch_id, before closing brace)
```

**Imports addition** (line 13):

```python
# Add to existing imports:
from phaze.services.ingestion import LEGACY_AGENT_ID, bulk_upsert_files, classify_file, discover_and_hash_files, normalize_path
from phaze.models.agent import Agent
```

**Pre-test Agent seeding (in each integration test):**

```python
# Insert before the ScanBatch:
session.add(Agent(id=LEGACY_AGENT_ID, name=LEGACY_AGENT_ID, scan_roots=["/music"]))
await session.commit()
```

---

## No Analog Found

None. Every Phase 24 file has at least a role-match analog in the codebase. The closest-to-novel pattern is **`tests/test_migrations/conftest.py`** — there is no existing alembic-driven test fixture in the repo, but `tests/conftest.py`'s `async_engine` fixture is the structural template; the new fixture diverges only in *what* it runs (alembic upgrade vs. metadata.create_all).

## Metadata

**Analog search scope:**
- `src/phaze/models/` (all 13 model files; deep-read: `base.py`, `file.py`, `scan_batch.py`, `tag_write_log.py`, `metadata.py`, `__init__.py`)
- `alembic/versions/` (all 11 existing migrations; deep-read: `002_*.py`, `005_*.py`, `009_*.py`, `011_*.py`; listing-only for the others)
- `alembic/env.py`, `alembic/script.py.mako`
- `src/phaze/services/ingestion.py` (full read — direct call-site)
- `src/phaze/config.py` (env-var reference)
- `tests/conftest.py`, `tests/test_models/__init__.py`, `tests/test_models/test_core_models.py`, `tests/test_models/test_tag_write_log.py`, `tests/test_phase02_gaps.py`, `tests/test_services/test_ingestion.py`

**Files scanned:** 16 source files read end-to-end; directory listings for migrations, models, tests.
**Pattern extraction date:** 2026-05-11

## PATTERN MAPPING COMPLETE

**Phase:** 24 — Schema Foundation & Agent Registry
**Files classified:** 16
**Analogs found:** 16 / 16

### Coverage
- Files with exact analog: 13
- Files with role-match analog: 3 (`tests/test_migrations/conftest.py`, `tests/test_migrations/test_012_upgrade.py`, `tests/test_migrations/test_013_upgrade.py`, `tests/test_migrations/test_downgrade.py` — share a brand-new fixture but each test file mirrors `test_tag_write_log.py` + `test_ingestion.py` integration style)
- Files with no analog: 0

### Key Patterns Identified
- All models follow `class X(TimestampMixin, Base)` with `__tablename__`, `Mapped[type] = mapped_column(...)`, and `__table_args__ = (Index(...), ...)`. Naming convention dict in `base.py` auto-prefixes constraint names — model side uses short `name="..."`; migration side uses fully-qualified `name="ck_<table>_<name>"`.
- Migrations use `from collections.abc import Sequence` + PEP 604 unions (NOT the boilerplate `from typing import Union` from `script.py.mako`); revision IDs are zero-padded 3-digit strings; isort order is stdlib → third-party (`sqlalchemy`, `sqlalchemy.dialects`) → first-party (`alembic`).
- Raw SQL in migrations uses `op.execute(sa.text("..."))` for parameterized statements; `op.get_bind().execute(sa.text(...), {bind_params})` for INSERTs with operator-controlled values; never f-string interpolation.
- Composite/partial indexes are declared manually via `Index(name, *cols, unique=True, postgresql_where=text(...))` in `__table_args__`, with **identical** predicate strings in the model and the migration (whitespace/case-sensitive).
- Existing test fixtures use `Base.metadata.create_all` and never exercise alembic; Phase 24 must add a separate `tests/test_migrations/conftest.py` to run real `alembic.command.upgrade()` against a dedicated test DB.

### File Created
`/Users/Robert/Code/public/phaze/.planning/phases/24-schema-foundation-agent-registry/24-PATTERNS.md`

### Ready for Planning
Pattern mapping complete. Planner can now reference analog file paths + line numbers + code excerpts in every PLAN.md task's `<read_first>` and `<code_shape>` sections.
