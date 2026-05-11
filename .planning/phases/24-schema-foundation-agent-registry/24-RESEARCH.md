# Phase 24: Schema Foundation & Agent Registry - Research

**Researched:** 2026-05-11
**Domain:** Alembic schema migration on a populated table (Postgres 18 + SQLAlchemy 2.0 async)
**Confidence:** HIGH

## Summary

Phase 24 introduces an `agents` table, swaps `files` uniqueness from `(original_path)` to `(agent_id, original_path)`, and ships a two-step Alembic migration that preserves all v3.0 data via a `legacy-application-server` agent. Most decisions are locked in CONTEXT.md (D-01..D-16); the implementation gaps centre on **Alembic mechanics** (autocommit blocks, partial unique indexes, downgrade safety) and **call-site compatibility** (`bulk_upsert_files` uses `ON CONFLICT (original_path)` and will break under the new composite unique constraint unless updated).

Two notable deviations from the CONTEXT framing were uncovered during research:

1. **`ScanStatus` is stored as `VARCHAR(20)`, not a Postgres ENUM type** [VERIFIED: `alembic/versions/002_add_scan_batches_and_unique_path.py:30`]. Adding `LIVE` is therefore a pure Python `StrEnum` edit — no `ALTER TYPE ... ADD VALUE`, no `autocommit_block`, no transaction-block concerns. This simplifies migration 012 substantially.
2. **The env var is `SCAN_PATH`, not `PHAZE_SCAN_PATH`** [VERIFIED: `src/phaze/config.py:24`, `docker-compose.yml:12`]. CONTEXT.md says "PHAZE_SCAN_PATH (or equivalent)" — the equivalent is `SCAN_PATH`. The migration must read `os.environ.get("SCAN_PATH", "/data/music")` to match the actual deployment env. The default in `Settings` is `/data/music`, not `/music`.

**Primary recommendation:** Build migration 012 as a single transactional revision (additive + backfill, no enum DDL needed), and migration 013 as a constraint-tightening revision. Use plain `SET NOT NULL` (table is small at single-user scale; multi-minute lock is acceptable per D-13's "operator can pause between steps" framing). Update `bulk_upsert_files` in Phase 24 to use `("agent_id", "original_path")` as conflict target — this is in scope because the column is being added now, even though `agent_id` won't start being *set by call sites* until Phase 25.

## User Constraints (from CONTEXT.md)

### Locked Decisions

**Agent ID Format & FK Semantics:**
- **D-01:** `agents.id` is a kebab-case slug string (e.g. `legacy-application-server`, `fileserver-01`). Operator picks the id at registration. The slug flows through as the SAQ queue name `phaze-agent-<id>` and into log lines.
- **D-02:** Column type for `agents.id`, `files.agent_id`, and `scan_batches.agent_id` is `VARCHAR(64)`. A CHECK constraint enforces `[a-z0-9-]+` only (lowercase letters, digits, hyphens). No leading/trailing hyphens or double hyphens.
- **D-03:** `files.agent_id` and `scan_batches.agent_id` are **real FOREIGN KEYs** to `agents.id`. `ON DELETE` policy: `RESTRICT`.
- **D-04:** The `legacy-application-server` row is inserted **inside** the upgrade migration (revision 012), before any backfill UPDATE runs.

**Legacy Agent Backfill:**
- **D-05:** `scan_roots` populated by reading `PHAZE_SCAN_PATH` (or equivalent — actual var is `SCAN_PATH`) from environment at migration time. Stored as JSONB array. Falls back to `["/music"]` if unset. The migration logs which value it used.
- **D-06:** Legacy agent is **born revoked**: `token_hash = NULL` and `revoked_at = NOW()`.
- **D-07:** `token_hash` is **nullable** on the `agents` table.
- **D-08:** Every pre-existing `FileRecord` and every pre-existing `ScanBatch` is attributed to the legacy agent during backfill.

**Sentinel LIVE ScanBatch:**
- **D-09:** New `ScanStatus.LIVE` enum value added alongside `RUNNING`, `COMPLETED`, `FAILED`.
- **D-10:** Sentinel's `scan_path` is literal string `"<watcher>"`.
- **D-11:** Each agent's LIVE sentinel created at agent-registration time; legacy sentinel inserted in revision 012.
- **D-12:** Idempotency enforced by partial unique index `uq_scan_batches_agent_id_live` on `(agent_id) WHERE status = 'LIVE'`.

**Migration Shape & Rollback:**
- **D-13:** Two-step = two separate Alembic revisions (012 additive+backfill; 013 enforce NOT NULL + swap unique constraint).
- **D-14:** Backfill via raw SQL `op.execute(sa.text(...))` — no SQLAlchemy model imports.
- **D-15:** Minimal index strategy — drop `uq_files_original_path`, create `uq_files_agent_id_original_path`, add `ix_scan_batches_agent_id`, no others.
- **D-16:** Downgrade fails loudly if `(original_path)` is no longer unique; no silent dedup.

### Claude's Discretion

- Exact regex for the CHECK constraint on `agents.id` — pick whichever Postgres-renderable form is cleanest.
- Whether to express CHECK in SQLAlchemy `__table_args__`, in the Alembic migration, or both — keep them consistent.
- New `Agent` SQLAlchemy model file layout and `relationship()` declarations.
- Pydantic schemas for agent-related types (likely none in Phase 24).
- Test fixture shape/scope for "DB with legacy agent + sentinel pre-seeded."
- Whether to add `ScanStatus.LIVE` mapping in `scan_batch.py` now (recommended) or wait for Phase 27.
- Logging format and verbosity of the backfill step.

### Deferred Ideas (OUT OF SCOPE)

- Agent self-registration / multi-tenant onboarding (OPS-06)
- mTLS in addition to bearer tokens (OPS-05)
- Cross-file-server fingerprint matching (XAGENT-01)
- Watcher catch-up / delete / move detection (WATCH-05/06/07)
- Agent metrics scraping endpoint (OPS-07)
- Reverse `Agent.files` / `Agent.scan_batches` relationships
- Per-agent `scan_path` validation against `agents.scan_roots` (Phase 27)

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| DATA-01 | `agents` table with `id`, `name`, `token_hash`, `scan_roots` (jsonb), `created_at`, `last_seen_at`, `revoked_at` | New `Agent` model in `src/phaze/models/agent.py`; raw `op.create_table` in migration 012; column shapes documented below |
| DATA-02 | `FileRecord.agent_id` non-null FK; uniqueness moves to `(agent_id, original_path)` | Two-step migration; column added nullable in 012, NOT NULL in 013; constraint swap in 013 |
| DATA-03 | `ScanBatch.agent_id` non-null; one sentinel LIVE ScanBatch per agent | Add `agent_id` column in 012; `LIVE` value to `ScanStatus` StrEnum (no DB enum type change needed); partial unique index `uq_scan_batches_agent_id_live` in 012 |
| DATA-04 | Two-step Alembic migration seeds legacy agent + backfills | Migration 012 = additive + INSERT legacy + INSERT sentinel + UPDATE backfill; Migration 013 = NOT NULL + constraint swap |

## Architectural Responsibility Map

Phase 24 is single-tier (database schema + ORM models). No HTTP, no UI, no task code path is added.

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| `agents` table definition | Database / Storage | — | Pure schema; no app code reads/writes it yet |
| Backfill of pre-existing rows | Database / Storage | — | One-shot DDL/DML inside Alembic migration; no app code involved |
| `Agent` SQLAlchemy model | API / Backend (model layer) | — | Lives in `src/phaze/models/`; consumed by Phase 25 routers, not by Phase 24 itself |
| `ScanStatus.LIVE` enum value | API / Backend (model layer) | Database | Python `StrEnum` change only; DB column is `VARCHAR(20)` so no type ALTER |
| Reading `SCAN_PATH` env var at migration time | Deployment / Config | Database | Operator-controlled env value is the source of truth for legacy `scan_roots`; migration reads it directly (`os.environ`) |
| Fix `bulk_upsert_files` conflict target | API / Backend (services) | — | `services/ingestion.py` currently uses `index_elements=["original_path"]`; must change to `["agent_id", "original_path"]` once unique constraint swaps |

## Standard Stack

### Core (already in `pyproject.toml`)
| Library | Version (installed) | Purpose | Why Standard |
|---------|---------------------|---------|--------------|
| alembic | >=1.18.4 [VERIFIED: `pyproject.toml`] | Schema migrations | Project standard; async env template already configured in `alembic/env.py` |
| SQLAlchemy | >=2.0.49 [VERIFIED: `pyproject.toml`] | ORM + DDL emission | Project standard; declarative-2.0 style with `Mapped[]` + `mapped_column` |
| asyncpg | >=0.31.0 [VERIFIED: `pyproject.toml`] | Postgres async driver | Project standard |
| PostgreSQL | 18-alpine [VERIFIED: `docker-compose.yml:42`] | Database engine | Project standard; supports `ALTER TYPE ... ADD VALUE IF NOT EXISTS` natively [CITED: postgresql.org/docs/current/sql-altertype.html] — though we don't need it (see ScanStatus note below) |

**No new dependencies required.** Phase 24 is a pure migration + model change.

### Version Compatibility Notes

- `alembic 1.18.4` supports `op.get_context().autocommit_block()` for non-transactional DDL [CITED: alembic.sqlalchemy.org/en/latest/api/runtime.html] — **but we don't need it because `ScanStatus` is `VARCHAR(20)`, not a Postgres ENUM.**
- `SQLAlchemy 2.0.49` `Index(..., postgresql_where=...)` correctly emits `WHERE` clause for partial unique indexes [CITED: docs.sqlalchemy.org/en/20/dialects/postgresql.html "Partial Indexes"].
- The Alembic `op.create_index` operation accepts `postgresql_where` as a kwarg via the underlying `Index` construct — confirmed by Alembic docs and standard SQLAlchemy Index forwarding [CITED: alembic.sqlalchemy.org/en/latest/ops.html#alembic.operations.Operations.create_index].

## Architecture Patterns

### System Architecture Diagram

```
Operator (psql / just db-upgrade)
        │
        ▼
┌─────────────────────────────────────────────┐
│ alembic upgrade head                        │
│                                             │
│  ┌──────────────────────────────────────┐   │
│  │ Revision 012: additive + backfill    │   │
│  │ 1. CREATE TABLE agents               │   │
│  │ 2. INSERT legacy-application-server  │   │
│  │ 3. INSERT sentinel LIVE scan_batch   │   │
│  │ 4. ADD COLUMN files.agent_id (NULL)  │   │
│  │ 5. ADD COLUMN scan_batches.agent_id  │   │
│  │ 6. ADD FK + ix_scan_batches_agent_id │   │
│  │ 7. CREATE partial UQ for sentinel    │   │
│  │ 8. UPDATE files SET agent_id=...     │   │
│  │ 9. UPDATE scan_batches SET agent_id  │   │
│  └──────────────────────────────────────┘   │
│                  │                          │
│                  ▼                          │
│  ┌──────────────────────────────────────┐   │
│  │ Revision 013: enforce constraints    │   │
│  │ 1. SET NOT NULL on files.agent_id    │   │
│  │ 2. SET NOT NULL on s_b.agent_id      │   │
│  │ 3. DROP uq_files_original_path       │   │
│  │ 4. CREATE uq_files_agent_id_original │   │
│  └──────────────────────────────────────┘   │
└─────────────────────────────────────────────┘
        │
        ▼
   Postgres state: agents + agent_id FKs +
   composite unique constraint enforced
```

Data flow: env var (`SCAN_PATH`) → migration 012 reads via `os.environ.get` → JSONB array literal embedded in INSERT → Postgres `agents.scan_roots`.

### Recommended File Layout

```
src/phaze/models/
├── agent.py              # NEW: Agent model
├── file.py               # MODIFIED: add agent_id column, swap unique index
├── scan_batch.py         # MODIFIED: add LIVE to ScanStatus, add agent_id column, add partial unique index
└── __init__.py           # MODIFIED: export Agent

alembic/versions/
├── 012_add_agents_table_and_backfill.py      # NEW
└── 013_enforce_agent_id_not_null_and_swap_uniqueness.py  # NEW

src/phaze/services/
└── ingestion.py          # MODIFIED: bulk_upsert_files conflict target

tests/test_models/
└── test_agent.py         # NEW: Agent model field tests

tests/test_migrations/    # NEW directory
├── __init__.py
├── conftest.py           # Alembic-driven test DB fixture
├── test_012_upgrade.py   # Upgrade roundtrip; backfill assertions
├── test_013_upgrade.py   # NOT NULL + constraint swap assertions
└── test_downgrade.py     # Clean downgrade + dupe-detection failure path
```

### Pattern 1: `Agent` Model Skeleton

```python
# src/phaze/models/agent.py
"""Agent model — file-server identity for the v4.0 distributed-agents milestone."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class Agent(TimestampMixin, Base):
    """An agent (file server identity) that owns FileRecord and ScanBatch rows."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scan_roots: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        CheckConstraint(
            "id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
            name="agents_id_charset",
        ),
    )
```

Notes:
- `Base`/`TimestampMixin` give us `pk_agents`, `created_at`, `updated_at` for free [VERIFIED: `src/phaze/models/base.py`].
- `CheckConstraint` `name=` is "agents_id_charset" because the project's `convention["ck"]` is `"ck_%(table_name)s_%(constraint_name)s"` — SQLAlchemy will prefix automatically to produce `ck_agents_agents_id_charset`. Actually wait — that's `ck_agents_<constraint_name>`, so passing `name="id_charset"` would give `ck_agents_id_charset`. **Use `name="id_charset"`** to match the naming convention dict cleanly. [VERIFIED by reading `convention` dict in base.py.]
- `scan_roots` server_default keeps existing-style INSERTs that omit the column working, though the migration passes an explicit value for the legacy agent.

### Pattern 2: Partial Unique Index in SQLAlchemy + Alembic

**Model declaration** (in `scan_batch.py` `__table_args__`):

```python
from sqlalchemy import Index

__table_args__ = (
    Index(
        "uq_scan_batches_agent_id_live",
        "agent_id",
        unique=True,
        postgresql_where=text("status = 'LIVE'"),
    ),
    Index("ix_scan_batches_agent_id", "agent_id"),
)
```

[CITED: docs.sqlalchemy.org/en/20/dialects/postgresql.html — "partial index with WHERE criterion … Use postgresql_where parameter with Index"]

**Alembic migration emission:**

```python
op.create_index(
    "uq_scan_batches_agent_id_live",
    "scan_batches",
    ["agent_id"],
    unique=True,
    postgresql_where=sa.text("status = 'LIVE'"),
)
```

Both forms emit identical SQL:
```sql
CREATE UNIQUE INDEX uq_scan_batches_agent_id_live
  ON scan_batches (agent_id) WHERE status = 'LIVE';
```

**Important:** Use the same `sa.text("status = 'LIVE'")` predicate string in both the model and the migration. Tiny whitespace/case differences would cause `alembic check` / autogenerate to see them as different.

### Pattern 3: Read SCAN_PATH inside Migration (no SQLAlchemy model import)

```python
# inside upgrade() in 012:
import json
import logging
import os

logger = logging.getLogger("alembic.runtime.migration")

raw_scan_path = os.environ.get("SCAN_PATH", "/data/music")
scan_roots_json = json.dumps([raw_scan_path])
logger.info(
    "phaze-024: resolved legacy-application-server scan_roots=%s (SCAN_PATH=%r)",
    scan_roots_json,
    raw_scan_path,
)
```

Note that `Settings.scan_path` default is `/data/music` [VERIFIED: `src/phaze/config.py:24`], not `/music`. Use `/data/music` as the migration fallback to match the production config exactly. (CONTEXT.md says `/music` but that's a stale value from earlier drafts; `/data/music` matches `docker-compose.yml` and `Settings`.)

### Pattern 4: Two-Step Migration — Upgrade Skeleton

**Revision 012** (additive + backfill, fits in one transaction):

```python
def upgrade() -> None:
    # 1. Create agents table
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

    # 2. Resolve scan_roots from env and seed legacy agent
    raw_scan_path = os.environ.get("SCAN_PATH", "/data/music")
    scan_roots_json = json.dumps([raw_scan_path])
    op.get_bind().execute(
        sa.text(
            "INSERT INTO agents (id, name, token_hash, scan_roots, revoked_at, created_at, updated_at) "
            "VALUES (:id, :name, NULL, CAST(:scan_roots AS jsonb), NOW(), NOW(), NOW())"
        ),
        {"id": "legacy-application-server", "name": "legacy-application-server", "scan_roots": scan_roots_json},
    )

    # 3. Add nullable agent_id columns
    op.add_column("files", sa.Column("agent_id", sa.String(64), nullable=True))
    op.add_column("scan_batches", sa.Column("agent_id", sa.String(64), nullable=True))

    # 4. FKs (ON DELETE RESTRICT)
    op.create_foreign_key(
        "fk_files_agent_id_agents", "files", "agents", ["agent_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_scan_batches_agent_id_agents", "scan_batches", "agents", ["agent_id"], ["id"], ondelete="RESTRICT",
    )

    # 5. Index on scan_batches.agent_id (composite UQ on files covers files lookups)
    op.create_index("ix_scan_batches_agent_id", "scan_batches", ["agent_id"])

    # 6. Backfill: every existing FileRecord and ScanBatch → legacy agent
    op.execute(sa.text("UPDATE files SET agent_id = 'legacy-application-server' WHERE agent_id IS NULL"))
    op.execute(sa.text("UPDATE scan_batches SET agent_id = 'legacy-application-server' WHERE agent_id IS NULL"))

    # 7. Sentinel LIVE scan_batch for legacy agent (idempotent via partial UQ, but we insert once here)
    op.get_bind().execute(
        sa.text(
            "INSERT INTO scan_batches (id, agent_id, scan_path, status, total_files, processed_files, created_at, updated_at) "
            "VALUES (gen_random_uuid(), :agent_id, '<watcher>', 'LIVE', 0, 0, NOW(), NOW())"
        ),
        {"agent_id": "legacy-application-server"},
    )

    # 8. Partial UQ for sentinel — created AFTER the INSERT so the INSERT can't violate it on first run
    op.create_index(
        "uq_scan_batches_agent_id_live",
        "scan_batches",
        ["agent_id"],
        unique=True,
        postgresql_where=sa.text("status = 'LIVE'"),
    )
```

**Note on ordering:** Creating the partial unique index *before* the sentinel INSERT is also fine (and arguably safer for re-running) — but in a clean v3.0 → v4.0 migration there are no existing LIVE rows so order doesn't matter functionally. Either ordering is acceptable; the above mirrors "do real work, then add the safety net."

**Caution about `gen_random_uuid()`:** PG18 has this in core (no extension required) [CITED: postgresql.org/docs/18/functions-uuid.html]. PG13+ no longer requires `pgcrypto`. If support for older Postgres is needed, use `uuid_generate_v4()` from `uuid-ossp` or, cleaner, generate the UUID in Python (`uuid.uuid4()`) and bind it as a parameter.

**Recommended:** generate UUID in Python to keep the migration self-contained:
```python
import uuid
sentinel_id = uuid.uuid4()
op.get_bind().execute(
    sa.text("INSERT INTO scan_batches (id, agent_id, scan_path, status, total_files, processed_files, created_at, updated_at) "
            "VALUES (:id, :agent_id, '<watcher>', 'LIVE', 0, 0, NOW(), NOW())"),
    {"id": sentinel_id, "agent_id": "legacy-application-server"},
)
```

**Revision 013** (enforce + swap):

```python
def upgrade() -> None:
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

[CITED: alembic.sqlalchemy.org/en/latest/ops.html "alter_column ... nullable"]
[CITED: postgresql.org/docs/current/sql-altertable.html — "SET NOT NULL may only be applied to a column provided none of the records in the table contain a NULL value … Ordinarily this is checked during the ALTER TABLE by scanning the entire table"]

For a 200K-row table this scan is fast (sub-second to a few seconds) [ASSUMED — order-of-magnitude reasoning, not benchmarked on the target DB]. The CONTEXT explicitly accepts that "operator can pause between them if backfill on a real 200K-file DB takes longer than expected" (D-13), so a brief `ACCESS EXCLUSIVE` lock is acceptable. **No `CREATE UNIQUE INDEX CONCURRENTLY` is necessary at single-user scale.**

### Pattern 5: Downgrade with Dupe Detection (D-16)

```python
def downgrade() -> None:
    # 013 downgrade: undo composite unique + NOT NULL
    bind = op.get_bind()
    dupes = bind.execute(
        sa.text(
            "SELECT original_path FROM files "
            "GROUP BY original_path HAVING COUNT(*) > 1 LIMIT 5"
        )
    ).scalars().all()
    if dupes:
        raise RuntimeError(
            "Cannot downgrade 013→012: original_path is no longer unique across agents. "
            f"Example collisions: {dupes!r}. "
            "Resolve manually by either (a) deleting/relocating duplicates, or "
            "(b) keeping the agents-aware constraint and skipping this downgrade. "
            "Silent dedup is FORBIDDEN per phase-24 D-16."
        )

    op.drop_index("uq_files_agent_id_original_path", table_name="files")
    op.create_index("uq_files_original_path", "files", ["original_path"], unique=True)
    op.alter_column("scan_batches", "agent_id", nullable=True, existing_type=sa.String(64))
    op.alter_column("files", "agent_id", nullable=True, existing_type=sa.String(64))
```

**Why `RuntimeError`:**
- It's the standard Python exception type Alembic-runtime-friendly errors use. [VERIFIED by reading existing migrations — none raise click exceptions; they all rely on Alembic's natural exception propagation.]
- `op.get_bind()` returns a sync connection inside the Alembic runtime; `bind.execute(...).scalars().all()` is the correct SQLAlchemy 2.0 pattern. [CITED: docs.sqlalchemy.org/en/20/core/connections.html#sqlalchemy.engine.Connection.execute]
- Limit the SELECT to 5 examples so the error message stays readable even with hundreds of collisions.

For 012 downgrade, the order is reversed:
```python
def downgrade() -> None:
    op.drop_index("uq_scan_batches_agent_id_live", table_name="scan_batches")
    op.execute(sa.text("DELETE FROM scan_batches WHERE status = 'LIVE'"))
    op.drop_index("ix_scan_batches_agent_id", table_name="scan_batches")
    op.drop_constraint("fk_scan_batches_agent_id_agents", "scan_batches", type_="foreignkey")
    op.drop_constraint("fk_files_agent_id_agents", "files", type_="foreignkey")
    op.drop_column("scan_batches", "agent_id")
    op.drop_column("files", "agent_id")
    op.drop_table("agents")
```

### Anti-Patterns to Avoid

- **DO NOT** import `from phaze.models import Agent` inside the migration. Migrations must be model-version-frozen [D-14; reinforced by Alembic best practices]. Use raw SQL via `op.execute(sa.text(...))`.
- **DO NOT** use `op.bulk_insert()` for the legacy agent row — it offers no advantage over `op.execute` for a single row and is harder to parameterize safely.
- **DO NOT** call `op.get_context().autocommit_block()` for the LIVE INSERT — there's no Postgres ENUM type involved (see ScanStatus note in Summary), so we don't need to escape the transaction. Keep everything in one atomic transaction so a partial failure rolls back cleanly.
- **DO NOT** declare the CHECK constraint *only* in the migration. Declare it in both the SQLAlchemy model (`__table_args__`) and the migration so `Base.metadata.create_all` (used by the test fixture in `tests/conftest.py:32`) produces a faithful schema for unit tests.
- **DO NOT** add a separate `ix_files_agent_id` index. D-15 explicitly says Postgres uses the leading column of `uq_files_agent_id_original_path` for `agent_id`-only filters — verified by Postgres planner behavior on b-tree composite indexes [CITED: postgresql.org/docs/current/indexes-multicolumn.html].

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Partial unique index in SQLAlchemy | Raw `op.execute("CREATE UNIQUE INDEX … WHERE")` only | `Index(..., postgresql_where=text("status = 'LIVE'"))` model + `op.create_index(..., postgresql_where=...)` migration | Round-trip safe with autogenerate; matches naming convention; SQLAlchemy 2.0 native [CITED: sqlalchemy.org] |
| CHECK constraint for slug regex | Application-layer validation only | Postgres `CHECK (id ~ '^…$')` at DB level | DB-side is the only enforcement that survives any code path (direct psql, future bulk loaders, etc.) — keep the regex literal identical in model and migration |
| UUID generation in migrations | Calling `gen_random_uuid()` and hoping pgcrypto/PG18 supports it | `import uuid; uuid.uuid4()` in Python, bind as parameter | Postgres-version-independent; explicit |
| Re-implementing the legacy backfill in Python | Loading ORM objects, iterating, saving | Single raw `UPDATE files SET agent_id = '...'` | 1 statement vs. N round-trips; correct under any row count; matches D-14 |
| Custom dupe-detection downgrade logic | Reading every row and grouping in Python | `SELECT original_path GROUP BY HAVING COUNT(*) > 1` | Postgres does this in milliseconds; pull only the offending paths |

## Runtime State Inventory

Phase 24 is a schema migration, not a rename. The "runtime state" check focuses on what migration consumers must know:

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | 200K-row v3.0 FileRecord population that must survive [VERIFIED via STATE.md "Blockers/Concerns"] | Backfill UPDATE inside migration 012 attributes all rows to `legacy-application-server`; verified non-null after 013 |
| Live service config | None — Phase 24 changes no compose service config | None |
| OS-registered state | None — no scheduled tasks reference these table names | None |
| Secrets/env vars | `SCAN_PATH` env var read at migration time (D-05); fallback `/data/music`. **CONTEXT.md says `PHAZE_SCAN_PATH` but the actual var is `SCAN_PATH`** [VERIFIED: `docker-compose.yml`, `config.py`]. | Use `os.environ.get("SCAN_PATH", "/data/music")` in migration; log the resolved value |
| Build artifacts | None — no compiled artifacts encode the affected table names | None |

**The canonical question — after `alembic upgrade head` runs, what runtime systems still have the old schema cached?** Just the running Python processes (FastAPI + SAQ worker). They'll reconnect to Postgres and pick up the new schema on next query. SQLAlchemy doesn't cache schema between queries; the only restart-required artifact would be a Python `Mapped[]` column that doesn't match the DB (e.g., the new `agent_id` column on `FileRecord`) — fixed by the model edits in Phase 24.

## Common Pitfalls

### Pitfall 1: `bulk_upsert_files` silently breaks after 013

**What goes wrong:** `src/phaze/services/ingestion.py:105` does `stmt.on_conflict_do_update(index_elements=["original_path"], ...)`. Once 013 drops `uq_files_original_path`, Postgres has no unique index matching that conflict target and will raise:
> `there is no unique or exclusion constraint matching the ON CONFLICT specification`

**Why it happens:** ON CONFLICT requires the conflict_target to match an existing unique index/constraint on the table. The single-column index no longer exists after migration 013.

**How to avoid:**
- **Update `bulk_upsert_files` in Phase 24** to use `index_elements=["agent_id", "original_path"]`. This requires that the input `records` dicts contain an `agent_id` key.
- For Phase 24, since `agent_id` isn't being set by call sites yet (Phase 25 wires it up via the new HTTP API), one of two paths:
  - **(a) Update `discover_and_hash_files` and `bulk_upsert_files` together** to attribute newly-discovered files to a configurable agent slug (Phase 24 default: pass through whatever `run_scan` is given; in practice still `legacy-application-server` until Phase 25 lands).
  - **(b) Stamp `agent_id = 'legacy-application-server'` server-side in the discovery code** as a temporary measure that goes away in Phase 25.

**Recommended:** (b). Update `discover_and_hash_files` to add `"agent_id": "legacy-application-server"` to every record dict. Update `bulk_upsert_files` conflict target to `["agent_id", "original_path"]`. Add a TODO comment pointing at Phase 25 for the real fix.

**Warning signs:** Integration tests calling `run_scan` against the new schema, or operator running a manual `just scan` after `alembic upgrade head`.

### Pitfall 2: `tests/conftest.py` uses `Base.metadata.create_all`, not migrations

**What goes wrong:** Existing tests build the test DB from SQLAlchemy models (`tests/conftest.py:32`), not from Alembic. So model-level CHECK and partial-unique declarations are tested by accident, but the **migration file itself is never exercised** by the default fixture. A migration could be totally broken and all `pytest` runs would still pass.

**Why it happens:** This was a pragmatic choice in v1.0 to speed up tests. It works for ORM coverage but it's blind to migration bugs.

**How to avoid:** Add a separate `tests/test_migrations/conftest.py` that:
1. Spins up a fresh Postgres test schema (or DB) per test
2. Runs `alembic.command.upgrade(cfg, "head")` against it
3. Yields an `AsyncConnection` (or `AsyncSession`) for assertions
4. On teardown, runs `alembic.command.downgrade(cfg, "base")` (or drops the schema)

**Warning signs:** New migration goes green on `pytest` but breaks `just db-upgrade` in dev.

### Pitfall 3: CHECK constraint regex literal drift

**What goes wrong:** The regex in the model `__table_args__` and in the migration drift apart (e.g., model has `^[a-z0-9]+(-[a-z0-9]+)*$` but migration has `^[a-z0-9-]+$`). Subsequent `alembic revision --autogenerate` would either ignore the drift (autogenerate doesn't compare CHECK constraint expressions by default) or — worse — generate a spurious migration that flip-flops the constraint.

**Why it happens:** Two source-of-truth locations for the same regex.

**How to avoid:**
- Pick **one canonical form**: `^[a-z0-9]+(-[a-z0-9]+)*$` (forbids leading/trailing/double hyphens — matches D-02 wording).
- Use the exact same string literal in `src/phaze/models/agent.py` and `alembic/versions/012_*.py`.
- Optionally extract to a module-level constant (e.g., `AGENT_ID_REGEX = r"^[a-z0-9]+(-[a-z0-9]+)*$"`) that both files import — but this risks the migration importing app code, which violates D-14's spirit. Safer: duplicate the literal and add a comment in both spots cross-referencing the other.

**Warning signs:** `alembic check` complains about a constraint name mismatch; or tests pass against the model schema but fail against the migrated schema.

### Pitfall 4: `Mapped[list[str]]` mypy strict friction for JSONB columns

**What goes wrong:** mypy strict with `disallow_untyped_defs = true` (per CLAUDE.md) doesn't know that `mapped_column(JSONB, ...)` returns `list[str]`. May complain or accept any type.

**Why it happens:** JSONB has no Python equivalent at the type-system level — it could be `dict`, `list`, scalar, etc.

**How to avoid:** Existing models handle this fine: see `FileMetadata.raw_tags: Mapped[dict[str, Any] | None]` and `TagWriteLog.before_tags: Mapped[dict[str, Any]]`. Use `Mapped[list[str]]` with `mapped_column(JSONB, ...)` — SQLAlchemy 2.0 stub types are permissive enough that this passes mypy without `# type: ignore`. **Verify by running `uv run mypy .` after writing the model.**

### Pitfall 5: Forgetting to update `__init__.py` model exports

**What goes wrong:** Add `Agent` model file but don't add to `phaze/models/__init__.py`. Alembic's autogenerate (`alembic/env.py:12` does `from phaze.models import *`) won't see it, future migrations could be wrong, and tests that do `from phaze.models import Agent` fail.

**Why it happens:** Easy to forget; not caught by lint.

**How to avoid:** Update `src/phaze/models/__init__.py` in the same commit that adds `agent.py`. Existing test `tests/test_models/test_core_models.py:7-25` (`test_all_tables_defined`) will catch missing tables — update the expected set to include `"agents"`.

## Code Examples

### Example 1: Model declaration for `FileRecord.agent_id`

```python
# src/phaze/models/file.py — additions
from sqlalchemy import ForeignKey
# ... existing imports

class FileRecord(TimestampMixin, Base):
    __tablename__ = "files"
    # ... existing columns
    agent_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agents.id", ondelete="RESTRICT"),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_files_state", "state"),
        Index("ix_files_sha256_hash", "sha256_hash"),
        Index("uq_files_agent_id_original_path", "agent_id", "original_path", unique=True),
    )
```

**Important:** After v4.0 ships, `nullable=False` is the truth. But during Phase 24's intermediate state (post-012, pre-013), the column is nullable in the DB. **The model declaration represents the final state** — after 013 runs — so `nullable=False` here is correct. The Phase 24 plan must run both migrations together; partial upgrade to 012 only would create a model-DB mismatch that the test suite (which uses `Base.metadata.create_all`) wouldn't catch.

### Example 2: Model declaration for `ScanBatch.agent_id` + `LIVE` enum

```python
# src/phaze/models/scan_batch.py — full file
"""ScanBatch model - tracks file discovery scan operations."""

import enum
import uuid

from sqlalchemy import ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class ScanStatus(enum.StrEnum):
    """Status of a scan batch operation."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    LIVE = "live"  # Watcher-originated sentinel; one per agent (D-09, D-10)


class ScanBatch(TimestampMixin, Base):
    __tablename__ = "scan_batches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    scan_path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ScanStatus.RUNNING)
    total_files: Mapped[int] = mapped_column(Integer, default=0)
    processed_files: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

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

**Important detail about the partial index predicate:** `ScanStatus.LIVE = "live"` (lowercase, matching existing enum convention). The partial-index WHERE clause must match the *string value as stored in the column*, so it's `status = 'live'`, not `'LIVE'`. **CONTEXT.md D-12 shows `status = 'LIVE'` but the existing enum values are all lowercase (`"running"`, `"completed"`, `"failed"`) so for consistency the new value should be `"live"` and the predicate `'live'`.** This is a minor discrepancy in CONTEXT but worth flagging — using `LIVE` (uppercase) would break consistency with the existing `ScanStatus` string mapping.

### Example 3: Updated `bulk_upsert_files`

```python
# src/phaze/services/ingestion.py — Phase 24 edits

LEGACY_AGENT_ID = "legacy-application-server"  # Phase 24 placeholder; Phase 25 wires real attribution


def discover_and_hash_files(scan_path: str, batch_id: uuid.UUID) -> list[dict[str, Any]]:
    # ... existing logic
    records.append(
        {
            "id": uuid.uuid4(),
            "agent_id": LEGACY_AGENT_ID,  # NEW
            "sha256_hash": sha256_hash,
            "original_path": normalized_path,
            # ... rest unchanged
        }
    )


async def bulk_upsert_files(...) -> int:
    # ...
    stmt = stmt.on_conflict_do_update(
        index_elements=["agent_id", "original_path"],  # CHANGED
        set_={...},
    )
```

`run_scan` must also set `agent_id=LEGACY_AGENT_ID` when creating the ScanBatch row.

## State of the Art

| Old Approach (v3.0) | Current Approach (v4.0) | When Changed | Impact |
|--------------------|------------------------|--------------|--------|
| `(original_path)` unique constraint | `(agent_id, original_path)` composite | Phase 24 | Multiple agents can have files at the same path; conflict detection now requires `agent_id` |
| Single fileless worker | (kept in v3.0; v4.0 splits via Phase 26) | Phase 26 (not 24) | Phase 24 does not change runtime; only schema |
| Scan batch identified by `scan_path` alone | `(agent_id, status='live')` sentinel via partial UQ | Phase 24 | Watcher-originated files (Phase 27+) attach to the sentinel; manual scans get fresh batch IDs |
| `bulk_upsert_files` conflict target = single column | Conflict target = `(agent_id, original_path)` | Phase 24 | Phase 24 must update ingestion code in lockstep with migration 013 |

**Deprecated/outdated in v3.0 schema (post-Phase 24):**
- `uq_files_original_path` index — dropped in revision 013
- `ScanBatch` without `agent_id` — column becomes mandatory after 013

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | A `SET NOT NULL` scan over 200K rows in the `files` table completes in "sub-second to a few seconds" on the production Postgres 18 instance | Pattern 4 — Revision 013 | If hardware is slow or rows are large (Text columns), the operator sees a longer ACCESS EXCLUSIVE lock. Single-user, off-peak; acceptable per D-13. Mitigation if it matters: add a `CHECK (agent_id IS NOT NULL) NOT VALID`, then `VALIDATE CONSTRAINT`, then `SET NOT NULL` (scan skipped per CITED docs). |
| A2 | The existing `tests/conftest.py` `async_engine` fixture's `Base.metadata.create_all` correctly emits the new partial unique index and CHECK constraint for unit tests | Pattern 2, Pitfall 5 | If SQLAlchemy 2.0 has a quirk with partial indexes in `create_all`, model-level tests miss it. Easy mitigation: run `uv run alembic upgrade head` against the test DB in a separate fixture; verified manually before merge. |
| A3 | The migration default `/data/music` matches what the operator currently has set as `SCAN_PATH` in production env | Pattern 3 | If operator has `SCAN_PATH` unset, the legacy agent gets `["/data/music"]` even though the v3.0 worker was scanning something else. Mitigation: D-05's logging contract ("emit Resolved scan_roots = …") makes this auditable; operator can correct via `UPDATE agents SET scan_roots = '["…"]'::jsonb WHERE id = 'legacy-application-server';`. |

**If this table is empty:** All claims were verified. (It's not empty — three small assumptions worth surfacing.)

## Open Questions

1. **Should Phase 24 update `bulk_upsert_files` to use the composite conflict target, or defer to Phase 25?**
   - What we know: Phase 25 will rewrite the upsert path entirely via the HTTP API; Phase 24 is the only chance to keep `just scan` working between phases.
   - What's unclear: Does the team plan to use `just scan` between Phase 24 and Phase 25 lands? If no, deferral is fine.
   - Recommendation: **Update in Phase 24.** Cheap fix; preserves dev-loop continuity; avoids stale-test risk. Add an inline comment pointing at Phase 25 for the real refactor.

2. **Should the `Agent` model have `relationship("FileRecord", ...)` back-references?**
   - What we know: CONTEXT lists this as deferred ("planner can decide … not strictly needed for Phase 24").
   - What's unclear: Whether Phase 25's CRUD endpoints will need them.
   - Recommendation: **Do not add in Phase 24.** Add a one-line TODO comment in `agent.py` so Phase 25 knows where to wire them. Keeps Phase 24 surface area minimal.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| PostgreSQL | All schema work | ✓ (via docker-compose) | 18-alpine [VERIFIED: `docker-compose.yml:42`] | — |
| Python `os.environ` | Reading SCAN_PATH in migration | ✓ (stdlib) | 3.13 | Default `/data/music` |
| alembic CLI | Running migrations | ✓ [VERIFIED: `pyproject.toml`] | 1.18.4 | — |
| `just db-upgrade` | Operator command | ✓ [VERIFIED: `justfile:184`] | — | Direct `uv run alembic upgrade head` |

**Missing dependencies with no fallback:** None.

**Missing dependencies with fallback:** None.

**Note:** Tests that run actual migrations (recommended new fixture set in `tests/test_migrations/`) require Postgres to be reachable at `localhost:5432` per `tests/conftest.py:15`. This is already the standing requirement for integration tests in this project.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 8.x + pytest-asyncio (auto mode) [VERIFIED: `pyproject.toml`] |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/test_models/test_agent.py tests/test_services/test_ingestion.py -x -v` |
| Full suite command | `uv run pytest -x -q` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| DATA-01 | `agents` table has correct columns | unit | `uv run pytest tests/test_models/test_agent.py::test_agents_table_columns -x` | ❌ Wave 0 |
| DATA-01 | `agents.scan_roots` is JSONB | unit | `uv run pytest tests/test_models/test_agent.py::test_scan_roots_is_jsonb -x` | ❌ Wave 0 |
| DATA-01 | CHECK constraint rejects bad slugs (`--double`, `-leading`, `trailing-`, `UPPER`, `under_score`) | integration | `uv run pytest tests/test_models/test_agent.py::test_id_charset_check -x` | ❌ Wave 0 |
| DATA-01 | `token_hash` is nullable | unit | `uv run pytest tests/test_models/test_agent.py::test_token_hash_nullable -x` | ❌ Wave 0 |
| DATA-02 | `FileRecord.agent_id` is non-null after 013 | integration (migration) | `uv run pytest tests/test_migrations/test_013_upgrade.py::test_files_agent_id_not_null -x` | ❌ Wave 0 |
| DATA-02 | Same `original_path` under different `agent_id` succeeds | integration | `uv run pytest tests/test_migrations/test_013_upgrade.py::test_same_path_different_agent -x` | ❌ Wave 0 |
| DATA-02 | Same `(agent_id, original_path)` twice fails | integration | `uv run pytest tests/test_migrations/test_013_upgrade.py::test_composite_unique_rejects_dup -x` | ❌ Wave 0 |
| DATA-02 | `uq_files_original_path` no longer exists after 013 | integration | `uv run pytest tests/test_migrations/test_013_upgrade.py::test_old_unique_dropped -x` | ❌ Wave 0 |
| DATA-03 | `ScanBatch.agent_id` is non-null after 013 | integration | `uv run pytest tests/test_migrations/test_013_upgrade.py::test_scan_batches_agent_id_not_null -x` | ❌ Wave 0 |
| DATA-03 | One LIVE sentinel exists per registered agent | integration | `uv run pytest tests/test_migrations/test_012_upgrade.py::test_legacy_sentinel_exists -x` | ❌ Wave 0 |
| DATA-03 | Partial UQ rejects duplicate LIVE per agent | integration | `uv run pytest tests/test_migrations/test_012_upgrade.py::test_partial_uq_rejects_dup_live -x` | ❌ Wave 0 |
| DATA-03 | Partial UQ allows multiple non-LIVE rows per agent | integration | `uv run pytest tests/test_migrations/test_012_upgrade.py::test_partial_uq_allows_multiple_non_live -x` | ❌ Wave 0 |
| DATA-03 | `ScanStatus.LIVE` value present in Python enum | unit | `uv run pytest tests/test_phase02_gaps.py::test_scan_status_enum_values -x` (extend existing) | ✅ extend |
| DATA-04 | Migration 012 on v3.0 snapshot seeds legacy agent with correct `scan_roots` from `SCAN_PATH` env | integration | `uv run pytest tests/test_migrations/test_012_upgrade.py::test_legacy_agent_scan_roots_from_env -x` | ❌ Wave 0 |
| DATA-04 | Migration 012 with `SCAN_PATH` unset falls back to `/data/music` | integration | `uv run pytest tests/test_migrations/test_012_upgrade.py::test_legacy_agent_scan_roots_fallback -x` | ❌ Wave 0 |
| DATA-04 | Legacy agent has `revoked_at IS NOT NULL` and `token_hash IS NULL` | integration | `uv run pytest tests/test_migrations/test_012_upgrade.py::test_legacy_agent_born_revoked -x` | ❌ Wave 0 |
| DATA-04 | Sentinel `scan_path` is literal `"<watcher>"` | integration | `uv run pytest tests/test_migrations/test_012_upgrade.py::test_sentinel_scan_path_literal -x` | ❌ Wave 0 |
| DATA-04 | All pre-existing FileRecord rows have `agent_id = 'legacy-application-server'` after 012 | integration | `uv run pytest tests/test_migrations/test_012_upgrade.py::test_backfill_files -x` | ❌ Wave 0 |
| DATA-04 | All pre-existing ScanBatch rows have `agent_id = 'legacy-application-server'` after 012 | integration | `uv run pytest tests/test_migrations/test_012_upgrade.py::test_backfill_scan_batches -x` | ❌ Wave 0 |
| DATA-04 | Downgrade 013→012 on a clean DB succeeds and restores `uq_files_original_path` | integration | `uv run pytest tests/test_migrations/test_downgrade.py::test_downgrade_013_clean -x` | ❌ Wave 0 |
| DATA-04 | Downgrade 013→012 with duplicate paths under different agents raises clear error | integration | `uv run pytest tests/test_migrations/test_downgrade.py::test_downgrade_013_fails_on_dupes -x` | ❌ Wave 0 |
| DATA-04 | Downgrade 012→base on clean DB succeeds (full v3.0 schema restored) | integration | `uv run pytest tests/test_migrations/test_downgrade.py::test_downgrade_012_clean -x` | ❌ Wave 0 |
| compat | `bulk_upsert_files` works with new composite conflict target | integration | `uv run pytest tests/test_services/test_ingestion.py::test_bulk_upsert_with_agent_id -x` | ✅ extend existing |
| compat | `test_all_tables_defined` includes `"agents"` | unit | `uv run pytest tests/test_models/test_core_models.py::test_all_tables_defined -x` | ✅ extend existing |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_models/test_agent.py tests/test_migrations/ -x -q` (~5-15s)
- **Per wave merge:** `uv run pytest -x -q` (full suite ~30-60s based on v3.0 baselines)
- **Phase gate:** Full suite green + `just db-upgrade` + `just db-downgrade` + `just db-upgrade` cycle on a snapshot DB before `/gsd-verify-work`

### Wave 0 Gaps
- [ ] `tests/test_migrations/__init__.py` — new package
- [ ] `tests/test_migrations/conftest.py` — alembic-driven test DB fixture; isolated schema per test; `alembic.command.upgrade(cfg, rev)` helper
- [ ] `tests/test_migrations/test_012_upgrade.py` — covers DATA-01, DATA-03 (partial), DATA-04 (backfill + legacy agent shape + env var resolution)
- [ ] `tests/test_migrations/test_013_upgrade.py` — covers DATA-02, DATA-03 (NOT NULL), constraint swap
- [ ] `tests/test_migrations/test_downgrade.py` — covers D-16 (dupe detection) and clean roundtrip
- [ ] `tests/test_models/test_agent.py` — Agent model field assertions, CHECK constraint behavior under live SQL
- [ ] Extend `tests/test_models/test_core_models.py::test_all_tables_defined` — add `"agents"` to expected set
- [ ] Extend `tests/test_phase02_gaps.py::test_scan_status_values` — add `LIVE` to expected enum members
- [ ] Extend `tests/test_services/test_ingestion.py` — exercise new composite conflict target

**Recommendation on test scope (unit vs. integration):** For a schema-only phase, **model-level unit tests are insufficient.** The migration file is the artifact being shipped, and the existing `Base.metadata.create_all` fixture doesn't exercise it. Add a `test_migrations/` package that actually runs `alembic.command.upgrade()` against a real Postgres test DB and asserts on resulting state. This adds ~5-10s to the test suite and catches an entire class of bugs (migration ordering, constraint name typos, raw SQL syntax errors) that pure ORM tests cannot.

## Security Domain

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | yes (indirectly) | Phase 24 introduces the `agents.token_hash` column that Phase 25 will populate. Phase 24 itself stores no real tokens (legacy agent has NULL token_hash) — auth surfaces in Phase 25. |
| V3 Session Management | no | No sessions in this phase |
| V4 Access Control | yes (indirectly) | The `revoked_at` column is the schema substrate for Phase 25's "reject revoked-agent requests" check. Phase 24 wires the data model only. |
| V5 Input Validation | yes | CHECK constraint `id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'` is DB-level input validation on agent slug. Defends against shell-injection / Redis-key-collision via slug crafting (D-02). |
| V6 Cryptography | yes (forward-looking) | `token_hash` column is `String(128)` — sized for any practical hash. Phase 25 will choose the hash algorithm. Phase 24 must NOT default this column to a fixed value or expose plaintext tokens anywhere. |

### Known Threat Patterns for Postgres + Alembic

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| SQL injection via slug in migration | Tampering | Use parameterized `sa.text(...)` with bind params — never f-string interpolation of operator-controlled values. The legacy agent slug is hardcoded so risk is theoretical, but the pattern matters. |
| Backfill UPDATE accidentally widens scope | Tampering | `WHERE agent_id IS NULL` predicate — only touches rows that lack attribution. Idempotent on re-run. |
| Downgrade silently drops data | Tampering / Repudiation | D-16 dupe-detection guard before unique-constraint downgrade; raises `RuntimeError` instead of mutating. |
| `SCAN_PATH` env var with a path-traversal payload (e.g., `../../etc`) gets persisted to JSONB | Tampering | The path goes into JSONB only as an audit/operator hint; no code consumes it as a filesystem operation in Phase 24. Phase 27 (watcher) will validate. Out of scope for Phase 24 to defend. |
| Operator picks a hostile slug for a future agent (e.g., `; DROP TABLE`) | Injection | CHECK constraint on `agents.id` rejects anything outside `[a-z0-9-]+`. Also enforced at application layer in Phase 25, but DB constraint is the last line. |

## Sources

### Primary (HIGH confidence)
- [/websites/alembic_sqlalchemy via Context7] — `autocommit_block`, `create_index` with `postgresql_where`, `alter_column nullable`, `create_unique_constraint` — fetched during research
- [/websites/sqlalchemy_en_20 via Context7] — `Index(..., postgresql_where=...)` partial unique index syntax
- [PostgreSQL 18 ALTER TYPE docs](https://www.postgresql.org/docs/current/sql-altertype.html) — `IF NOT EXISTS` supported on PG18; transaction restriction; BEFORE/AFTER positioning
- [PostgreSQL 18 ALTER TABLE docs](https://www.postgresql.org/docs/current/sql-altertable.html) — `SET NOT NULL` scan behavior, `CHECK NOT VALID` optimization path
- `src/phaze/models/base.py`, `file.py`, `scan_batch.py`, `__init__.py` — codebase truth
- `alembic/versions/001..011_*.py` — existing migration patterns (op.execute, op.create_table, op.create_index, downgrade structure)
- `alembic/env.py` — async migration runner pattern + `from phaze.models import *` autoload pattern
- `src/phaze/config.py`, `docker-compose.yml` — confirmed env var name is `SCAN_PATH` (not `PHAZE_SCAN_PATH`); default `/data/music`
- `src/phaze/services/ingestion.py` — confirmed `bulk_upsert_files` will break under the new constraint
- `tests/conftest.py` — confirmed tests use `Base.metadata.create_all`, not alembic upgrade

### Secondary (MEDIUM confidence)
- [Alembic GitHub issue #123 — non-transactional DDL discussion](https://github.com/sqlalchemy/alembic/issues/123)
- [Alembic discussion #1578 — enum migrations](https://github.com/sqlalchemy/alembic/discussions/1578)
- [PostgreSQL: ADD CONSTRAINT USING INDEX pattern (Medium write-up)](https://medium.com/dovetail-engineering/how-to-safely-create-unique-indexes-in-postgresql-e35980e6beb5) — not used by us at single-user scale but documented for completeness

### Tertiary (LOW confidence)
- None — all critical claims verified.

## Project Constraints (from CLAUDE.md)

- **Python 3.13 exclusively** — `Mapped[str | None]` PEP-604 union syntax is fine; `enum.StrEnum` is fine
- **uv only** — never bare `pytest`/`python`/`mypy`; always `uv run X`
- **Pre-commit hooks must pass** — ruff (150-char lines, isort, T201, S101 exempt), bandit (-x tests -s B608), shellcheck, yamllint, actionlint, **local mypy with `pass_filenames: false`**
- **mypy strict** with `disallow_untyped_defs = true`, `warn_unused_ignores = true`. Migration `def upgrade() -> None:` / `def downgrade() -> None:` signatures already match. New model code must have full annotations.
- **mypy excludes** `tests/`, `prototype/`, `services/`. Test files can have looser typing; migration files in `alembic/versions/` are **not excluded** → must type-check clean.
- **150-character line length** — relevant for long raw SQL in `op.execute(sa.text("..."))` calls. Wrap with `(`...`)` over multiple lines or use `sa.text("""...""")`.
- **Ruff rule set** includes `B`, `E`, `F`, `S` (security), `SIM`, `T20` (no print). Migrations use `logging`, never `print`.
- **`S608` is suppressed in bandit** for legitimate uses but not in ruff; the `S` rule set includes string-based SQL warnings. Use `sa.text(...)` consistently — that's the project's existing pattern (see migration 009/010).
- **`isort` rules**: `combine-as-imports = true`, `split-on-trailing-comma = true`, 2 blank lines after imports. Match existing migration import block exactly.
- **Test coverage minimum 85%** — applies project-wide; new model + migration code must be covered by new tests in `tests/test_migrations/`
- **Frequent commits** [from MEMORY.md] — commit per task during phase execution, not batched at end
- **PR per phase from a worktree branch** [from MEMORY.md] — Phase 24 must land via a `gsd/phase-24-schema-foundation-agent-registry` branch
- **Justfile commands updated** [from MEMORY.md] — current `db-upgrade`/`db-revision`/`db-current`/`db-downgrade`/`db-history` are sufficient for Phase 24; no new commands needed unless a migration smoke-test command is added (recommend: optional `just db-roundtrip` that runs upgrade→downgrade→upgrade on a throwaway DB to catch downgrade regressions)
- **README per service** [from MEMORY.md] — Phase 24 doesn't add a service; no README updates needed
- **Workflows use just** [from MEMORY.md] — N/A for Phase 24 (no new workflow)

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — all deps already in pyproject.toml; versions verified
- Architecture: HIGH — patterns lifted directly from existing migrations 002, 009, 010, 011
- Pitfalls: HIGH — `bulk_upsert_files` breakage verified by reading `src/phaze/services/ingestion.py:105`; conftest blindness verified by reading `tests/conftest.py:32`; ScanStatus-is-VARCHAR verified by reading migration 002
- ScanStatus.LIVE handling: HIGH — verified that DB column is `String(20)`, eliminating need for `ALTER TYPE`
- Postgres 18 features: HIGH — `ALTER TYPE ADD VALUE IF NOT EXISTS` verified via postgresql.org docs (not relied upon, but documented in case future changes need it)
- Env var name: HIGH — `SCAN_PATH` (not `PHAZE_SCAN_PATH`) verified in both `config.py` and `docker-compose.yml`

**Research date:** 2026-05-11
**Valid until:** 2026-06-10 (30 days — stable schema/migration domain; nothing in active flux)

## RESEARCH COMPLETE

**Phase:** 24 — Schema Foundation & Agent Registry
**Confidence:** HIGH

### Key Findings
- **`ScanStatus` is `VARCHAR(20)`, not a Postgres ENUM type** — adding `LIVE` is a pure Python `StrEnum` edit; no `ALTER TYPE ... ADD VALUE`, no `autocommit_block`, no transaction-block escape needed. Significant simplification vs. CONTEXT's implicit assumption.
- **Env var name is `SCAN_PATH`, not `PHAZE_SCAN_PATH`** — verified in `config.py` and `docker-compose.yml`. Fallback `/data/music` (not `/music`).
- **`bulk_upsert_files` in `src/phaze/services/ingestion.py` will break under migration 013** — uses `index_elements=["original_path"]` for ON CONFLICT. Must be updated in Phase 24 in lockstep with the constraint swap.
- **Existing test fixtures use `Base.metadata.create_all`, not Alembic** — migration files are never exercised by default `pytest` runs. New `tests/test_migrations/` package required to actually validate migration 012 + 013 forward and downgrade paths.
- **Partial unique index syntax confirmed for both layers** — `Index(..., postgresql_where=text(...))` in model `__table_args__` and `op.create_index(..., postgresql_where=sa.text(...))` in migration emit identical SQL.
- **CHECK constraint regex recommendation: `^[a-z0-9]+(-[a-z0-9]+)*$`** — forbids leading/trailing/double hyphens per D-02 wording, declared in both model and migration.
- **Sentinel uniqueness uses lowercase value `'live'` to match existing enum-value casing convention** — CONTEXT shows `'LIVE'` but existing values are `'running'`, `'completed'`, `'failed'`; lowercase keeps consistency.
- **No new dependencies** — alembic 1.18.4, sqlalchemy 2.0.49, asyncpg 0.31.0 already in pyproject.toml.

### Validation Architecture
24 test cases mapped to DATA-01..DATA-04 plus compatibility. Recommended split: model-level unit tests + new `tests/test_migrations/` integration tests that actually run `alembic.command.upgrade()` against a real test DB. Phase gate: full suite + upgrade→downgrade→upgrade roundtrip on snapshot DB.

### Ready for Planning
Research complete. Planner can now produce PLAN.md files covering: (1) new Agent model + tests, (2) FileRecord/ScanBatch model edits + LIVE enum + partial UQ, (3) migration 012 with backfill + sentinel, (4) migration 013 with NOT NULL + constraint swap + safe downgrade, (5) ingestion service update to use composite conflict target, (6) new `tests/test_migrations/` package with conftest and three test modules.
