# Phase 77: Additive Schema & Rescan-Wipe Fix (migration `032`) - Pattern Map

**Mapped:** 2026-07-08
**Files analyzed:** 8 (1 new migration, 3 modified models, 1 new model, 2 modified upsert sites, 2 new test files)
**Analogs found:** 8 / 8 (every file has an exact or role-match in-tree analog)

This is a "follow the established idiom exactly" phase — zero new dependency, zero new pattern. Every
object below is precedented in-tree. All excerpts carry file path + line numbers so the planner can
reference the exact source of each pattern in the PLAN action steps.

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `alembic/versions/032_*.py` (NEW) | migration | batch (DDL + set-based backfill) | `028` (add column) + `026` (widen CHECK) + `018` (create table) + `019` (partial index) + `016` (backfill) | composite exact |
| `src/phaze/models/analysis.py` (MOD) | model | CRUD | self (`AnalysisResult`) + `fingerprint.py` (`__table_args__` Index mirror) | exact |
| `src/phaze/models/metadata.py` (MOD) | model | CRUD | self (`FileMetadata`) + `analysis.py` | exact |
| `src/phaze/models/cloud_job.py` (MOD) | model | CRUD | self (`CloudJobStatus` StrEnum + CHECK) | exact |
| `src/phaze/models/dedup_resolution.py` (NEW) | model | CRUD | `cloud_job.py` (1:1 sidecar, unique FK) + `scheduling_ledger.py` (standalone sidecar) | exact |
| `src/phaze/services/ingestion.py` (MOD) | service | request-response (upsert) | self (`bulk_upsert_files`) | exact (2-line deletion) |
| `src/phaze/routers/agent_files.py` (MOD) | router | request-response (upsert) | self (`upsert_files`) + mirror of ingestion site | exact (2-line deletion) |
| `tests/integration/test_migrations/test_migration_032_additive_schema.py` (NEW) | test | request-response | `test_migration_031_route_control.py` | exact |
| `tests/<discovery\|agents>/test_rescan_preserves_state.py` (NEW) | test | request-response | (rescan regression, see Shared Patterns) | role-match |

**Model registration requirement (load-bearing):** the new `dedup_resolution.py` model MUST be imported
in `src/phaze/models/__init__.py` (add `from phaze.models.dedup_resolution import DedupResolution` +
add to `__all__`). `alembic/env.py` sets `target_metadata = Base.metadata`; autogenerate only sees a
model if its class is imported so it attaches to `Base.metadata`. Every existing model is registered
there `[VERIFIED: src/phaze/models/__init__.py]`.

## Pattern Assignments

### `alembic/versions/032_*.py` (NEW — migration, additive DDL + backfill)

This migration is a composite of five in-tree idioms. Each sub-pattern's analog is called out below.

**Revision header + docstring banner** — copy from `028_add_analysis_completed_at.py:1-38`:

```python
"""<one-line summary>. (Phase 77, MIG-01/PERF-01)

Additive-only migration. ... call out D-02 (metadata done predicate will tighten to
`failed_at IS NULL`) and D-03 (analyze backfills, metadata does NOT) in this docstring so the
reader phase honors it.

CRITICAL: this migration must NEVER reference ``saq_jobs`` (SAQ owns that table via init_db +
saq_versions; an Alembic migration touching it would collide -- 020 CRITICAL banner).

Revision ID: 032
Revises: 031
Create Date: 2026-07-08
"""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "032"
down_revision: str | Sequence[str] | None = "031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
```
The head is confirmed `031` (`models/__init__.py` imports `RouteControl` from `031`; `031` test asserts
`down_revision == "030"`), so this is `032`, `down_revision = "031"`.

**Sub-pattern A — additive nullable columns** (analog `028_add_analysis_completed_at.py:43`):
```python
op.add_column("analysis", sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True))
op.add_column("analysis", sa.Column("error_message", sa.Text(), nullable=True))
op.add_column("metadata", sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True))
op.add_column("metadata", sa.Column("error_message", sa.Text(), nullable=True))
```

**Sub-pattern B — create the `dedup_resolution` table** (analog `018_add_analysis_window_table.py:44-67`,
which shows the full `op.create_table` + `PrimaryKeyConstraint(name=op.f(...))` + `ForeignKeyConstraint(name=op.f(...))`
naming-convention idiom):
```python
op.create_table(
    "dedup_resolution",
    sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("canonical_file_id", postgresql.UUID(as_uuid=True), nullable=True),  # NULLABLE per Pitfall 4
    sa.Column("resolved_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint("id", name=op.f("pk_dedup_resolution")),
    sa.UniqueConstraint("file_id", name=op.f("uq_dedup_resolution_file_id")),
    sa.ForeignKeyConstraint(["file_id"], ["files.id"], name=op.f("fk_dedup_resolution_file_id_files")),
    sa.ForeignKeyConstraint(["canonical_file_id"], ["files.id"], name=op.f("fk_dedup_resolution_canonical_file_id_files")),
)
```
Note: `018` imports `from sqlalchemy.dialects import postgresql` for `postgresql.UUID` — add that import.
The `op.f(...)` explicit names must match the ORM model's naming-convention-derived names exactly (see
`base.py:9-15` convention) or autogenerate churns.

**Sub-pattern C — widen the `cloud_job` status CHECK** (analog `026_add_cloud_job_kube_columns.py:45-57`):
```python
_STATUS_ENUM_OLD = "status IN ('uploading', 'uploaded', 'submitted', 'running', 'succeeded', 'failed')"
_STATUS_ENUM_NEW = "status IN ('uploading', 'uploaded', 'submitted', 'running', 'succeeded', 'failed', 'awaiting')"
# Bare name "status_enum": the ck_%(table_name)s_%(constraint_name)s convention re-applies the
# ck_cloud_job_ prefix. Passing the already-prefixed name double-prefixes it.
op.drop_constraint("status_enum", "cloud_job", type_="check")
op.create_check_constraint("status_enum", "cloud_job", _STATUS_ENUM_NEW)
```

**Sub-pattern D — partial indexes** (analog `019_add_proposals_pending_unique_index.py:72-78` and
`018:71-72`). PERF-01 requires `IS NOT NULL`-shaped predicates for the failure/dedup markers (never
`status IN (...)`):
```python
op.create_index("ix_analysis_failed", "analysis", ["file_id"],
                postgresql_where=sa.text("failed_at IS NOT NULL"))
op.create_index("ix_metadata_failed", "metadata", ["file_id"],
                postgresql_where=sa.text("failed_at IS NOT NULL"))
op.create_index("ix_analysis_completed", "analysis", ["file_id"],
                postgresql_where=sa.text("analysis_completed_at IS NOT NULL"))
# dedup + awaiting lookup partial indexes, each sized to its exact predicate
op.create_index("ix_cloud_job_awaiting", "cloud_job", ["file_id"],
                postgresql_where=sa.text("status = 'awaiting'"))  # simple equality round-trips cleanly
```
PERF-01 HAZARD (RESEARCH Pitfall 1): if `ix_fprint_success` is included, spell its predicate as
`status = ANY (ARRAY['success','completed'])` in BOTH the migration and the ORM mirror (Postgres
reserializes `IN (...)` to `= ANY(ARRAY[...])`, breaking the empty-diff). No in-tree partial index has
ever used `IN` — `018` uses `tier = 'fine'`, `019` uses `status = 'pending'`. Plain house style is
transactional `op.create_index` (no `CONCURRENTLY` anywhere in-tree — follow house style).

**Sub-pattern E — set-based backfill via static SQL** (analog `016_backfill_scan_batches_completed_at.py:46`;
static literals only, no interpolation, no `saq_jobs`). Note the analyze-failed backfill MUST be an
UPSERT, not a plain UPDATE (RESEARCH Pitfall 2: `report_analysis_failed` writes no `analysis` row):
```python
# analyze-failed: UPSERT keyed on unique analysis.file_id (a partial row may or may not exist)
op.execute(sa.text("""
    INSERT INTO analysis (id, file_id, failed_at, error_message, created_at, updated_at)
    SELECT gen_random_uuid(), f.id, COALESCE(f.updated_at, now()),
           'backfilled from ANALYSIS_FAILED', now(), now()
    FROM files f WHERE f.state = 'analysis_failed'
    ON CONFLICT (file_id) DO UPDATE
      SET failed_at = COALESCE(analysis.failed_at, EXCLUDED.failed_at),
          error_message = COALESCE(analysis.error_message, EXCLUDED.error_message)
"""))
# dedup: insert-if-missing, canonical derived deterministically (NULL if none) — Pitfall 4
# awaiting / pushing / pushed: insert-or-promote cloud_job rows for gap rows only (D-04/D-06)
# metadata: NO backfill (D-03) — genuinely no historical source
```

**`downgrade()` — minimal DDL reversal only** (D-09). Follow `018:78-85` (drop indexes then table,
reverse order) and `026:60-66` (restore old CHECK then drop columns). Data backfills use a NO-OP
downgrade (`016:49-58` precedent — a data backfill is not reversibly undoable). Do NOT gold-plate.

---

### `src/phaze/models/analysis.py` (MOD — model, CRUD)

**Analog:** self (`AnalysisResult`, lines 13-38) + `fingerprint.py:25` for the `__table_args__` Index mirror.

Add two `Mapped[... | None]` columns after `analysis_completed_at` (line 38), matching the existing
nullable-column style (`analysis.py:20-38`). `Text` for `error_message` (already imported, line 6):
```python
failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
```
`AnalysisResult` has NO `__table_args__` today — add one (mirror pattern from `fingerprint.py:7,25`,
which imports `Index` and declares `__table_args__ = (Index(...),)`). Import `Index` and `text`:
```python
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy import text

__table_args__ = (
    Index("ix_analysis_completed", "file_id", postgresql_where=text("analysis_completed_at IS NOT NULL")),
    Index("ix_analysis_failed",    "file_id", postgresql_where=text("failed_at IS NOT NULL")),
)
```
Every `op.create_index` name in the migration MUST equal the ORM `Index(...)` name exactly, or
autogenerate proposes a create (Pitfall 5, non-empty diff).

---

### `src/phaze/models/metadata.py` (MOD — model, CRUD)

**Analog:** self (`FileMetadata`, lines 12-27) + `analysis.py` (same two-column + `__table_args__` add).

Add the same two nullable columns after `raw_tags` (line 27). `Text` already imported (line 5); add
`DateTime`, `Index`, `text`:
```python
failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

__table_args__ = (
    Index("ix_metadata_failed", "file_id", postgresql_where=text("failed_at IS NOT NULL")),
)
```
`FileMetadata` has no `datetime` import today — add `from datetime import datetime`. No backfill for
metadata (D-03), but the column + index still land now.

---

### `src/phaze/models/cloud_job.py` (MOD — model, CRUD)

**Analog:** self (`CloudJobStatus` StrEnum lines 30-46 + CHECK lines 106-110).

Append the enum member (after `SUCCEEDED`, line 46), matching the existing StrEnum-member comment style:
```python
AWAITING = "awaiting"  # Phase 77 (D-04): AWAITING_CLOUD sidecar representation (string-backed, CHECK only)
```
Update the CHECK membership list at `cloud_job.py:107-110` to the 7-member list (add `'awaiting'`):
```python
CheckConstraint(
    "status IN ('uploading', 'uploaded', 'submitted', 'running', 'succeeded', 'failed', 'awaiting')",
    name="status_enum",
),
```
`'awaiting'` is 8 chars, fits `status String(16)` (line 78) — no column widening. If the awaiting-lookup
partial index is added, add it to this existing `__table_args__` tuple (cloud_job already has one, lines
106-115) — mirror the migration's `ix_cloud_job_awaiting`.

---

### `src/phaze/models/dedup_resolution.py` (NEW — model, CRUD, 1:1 sidecar)

**Analog:** `cloud_job.py:65-104` (1:1 sidecar with `unique=True` FK to `files.id`, `TimestampMixin`) +
`scheduling_ledger.py:54-69` (standalone sidecar with module docstring + `__table_args__` Index tuple).

Follow the `cloud_job` FK-uniqueness idiom (`cloud_job.py:72`) exactly — `unique=True` gives the 1:1
constraint the dedup marker needs and the `ON CONFLICT (file_id)` backfill target:
```python
"""DedupResolution model -- per-file marker that a duplicate resolved to a canonical file (Phase 77, D-07)."""

import uuid

from sqlalchemy import DateTime, ForeignKey, Index
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class DedupResolution(TimestampMixin, Base):
    """One row per resolved duplicate file -- existence = resolved; undo = DELETE the row (D-07)."""

    __tablename__ = "dedup_resolution"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Unique FK: one resolution marker per file (cloud_job.py:72 precedent).
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), unique=True, nullable=False)
    # NULLABLE (Pitfall 4): the canonical pointer is best-effort; the marker's primary job is "resolved".
    canonical_file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), nullable=True)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_dedup_resolution_file", "file_id"),  # or the IS-NOT-NULL/lookup partial the reader needs
    )
```
`created_at` / `updated_at` come from `TimestampMixin` — do NOT redeclare (see `cloud_job.py:17`,
`scheduling_ledger.py:38-39`). REGISTER the class in `src/phaze/models/__init__.py` (import + `__all__`).

---

### `src/phaze/services/ingestion.py` (MOD — service, upsert) — D-08, standalone task 1

**Analog:** self (`bulk_upsert_files`, lines 94-122).

DELETE exactly one line from the `on_conflict_do_update` `set_` dict (line 114):
```python
set_={
    "sha256_hash": stmt.excluded.sha256_hash,
    "file_size": stmt.excluded.file_size,
    "state": stmt.excluded.state,     # ← DELETE this line (D-08 / MIG-03)
    "batch_id": stmt.excluded.batch_id,
    "file_type": stmt.excluded.file_type,
},
```
New-file INSERT still stamps `state = DISCOVERED` via the VALUES dict built in `discover_and_hash_files`
(`ingestion.py:86`). Batching via `itertools.batched` (line 106) is unaffected.

---

### `src/phaze/routers/agent_files.py` (MOD — router, upsert) — D-08, mirror site

**Analog:** self (`upsert_files`, lines 126-142) — near-identical mirror of the ingestion site.

DELETE the same one line from the `set_` dict (line 132):
```python
set_={
    "sha256_hash": base_stmt.excluded.sha256_hash,
    "file_size": base_stmt.excluded.file_size,
    "state": base_stmt.excluded.state,   # ← DELETE this line (D-08 / MIG-03)
    "batch_id": base_stmt.excluded.batch_id,
    "file_type": base_stmt.excluded.file_type,
},
```
New-file INSERT still stamps `state = DISCOVERED` via `data["state"] = FileState.DISCOVERED`
(`agent_files.py:111`, stamped from server, never body — AUTH-01 preserved). BOTH sites must be edited
or the bug survives on one path.

---

### `tests/integration/test_migrations/test_migration_032_additive_schema.py` (NEW — test)

**Analog:** `test_migration_031_route_control.py` (entire file — copy structure exactly).

Copy the three-part structure from the analog:
1. **`_load_migration_032()`** — the by-path importlib loader (031 lines 34-44), pointing at
   `alembic/versions/032_*.py`. Digit-prefixed module names require this loader.
2. **DB-free static assertions** (031 lines 50-62): `test_revision_identifiers_are_bare_numbers`
   (assert `revision == "032"`, `down_revision == "031"`, `branch_labels is None`) and
   `test_migration_never_references_saq_jobs` (grep guard — copy the exact list-comprehension at
   031:60-62). This banner-grep guard is mandatory (RESEARCH anti-pattern).
3. **Integration body** (031 lines 65-98): use the conftest helpers
   `from tests.integration.test_migrations.conftest import (MIGRATIONS_TEST_DATABASE_URL,
   _build_alembic_config, downgrade_to, upgrade_to)`. Pattern: `downgrade_to(cfg, "base")` →
   `upgrade_to(cfg, "031")` → seed a corpus (files in `analysis_failed`/`duplicate_resolved`/
   `awaiting_cloud`/`pushing`/`pushed` + a matching `sha256_hash` group) → `upgrade_to(cfg, "032")` →
   assert columns/table/CHECK exist, backfill counts match legacy state counts, `metadata.failed_at`
   all NULL, `files.state` byte-unchanged, each partial index present in `pg_indexes` → `downgrade_to(cfg, "031")`
   → assert additive objects gone. Wrap the engine in `try/finally` with `engine.dispose()` +
   `downgrade_to(cfg, "base")` (031:96-98).

Bucket: `integration`. Run: `just integration-test` (ephemeral PG :5433) or `just test-bucket integration`.

---

### `tests/<discovery|agents>/test_rescan_preserves_state.py` (NEW — test) — D-08, task 1

**Analog:** rescan regression (no exact single-file analog; role-match). Assertion shape from RESEARCH:
upsert a file → advance it to `ANALYZED` + create its `analysis` row → re-upsert the same
`(agent_id, original_path)` → assert `state` stays `ANALYZED` AND the `analysis` row survives. Cover BOTH
upsert sites (`bulk_upsert_files` and the `agent_files` `upsert_files` endpoint). This test has NO
dependency on `032` — ship it in task 1 with the two-line fix. Place in the bucket matching the touched
module (`discovery` for ingestion, `agents` for agent_files); enforce one bucket per file via
`tests/shared/test_partition_guard.py`.

## Shared Patterns

### Naming convention (applies to every DDL object + ORM index/constraint)
**Source:** `src/phaze/models/base.py:9-15`
```python
convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",   # ← why CHECK uses bare name "status_enum"
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
```
`Base.metadata = MetaData(naming_convention=convention)`. In migrations, name constraints with
`op.f("pk_dedup_resolution")` etc. so the emitted name matches what the ORM's convention derives — or
autogenerate reports a diff. The CHECK bare-name (`"status_enum"` → `ck_cloud_job_status_enum`) footgun
is the reason `026` passes `"status_enum"`, not the prefixed name.

### TimestampMixin (every new/1:1 table)
**Source:** `src/phaze/models/base.py:24-28`
```python
class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
```
**Apply to:** the new `dedup_resolution` model — subclass `(TimestampMixin, Base)`, never redeclare
`created_at`/`updated_at` (see `cloud_job.py:17`, `scheduling_ledger.py:38-39`).

### "NEVER reference saq_jobs" banner + grep guard
**Source:** `alembic/versions/028:18-20` (banner) + `test_migration_031_route_control.py:58-62` (guard)
**Apply to:** the `032` migration docstring (banner) AND `test_migration_032_*.py` (grep guard). Every
migration since `020` carries this; the test greps the migration body for `saq_jobs` outside comments.

### Static-literal backfill SQL (no injection surface)
**Source:** `alembic/versions/016:39-46` and `019:47-57`
**Apply to:** all `032` backfills — `op.execute(sa.text(...))` with only static state literals, no model
imports, no interpolation (bandit `S608` / project `-s B608`). D-09 no-op downgrade for data backfills.

### Autogenerate empty-diff contract (env.py)
**Source:** `alembic/env.py` — `target_metadata = Base.metadata`, `context.configure(..., compare_type=True)`
**Apply to:** every migration-created index/column MUST have a matching ORM declaration
(`__table_args__` Index / `Mapped` column) with byte-identical name and normalized `postgresql_where`
text. This is the SC#2 acceptance gate (PERF-01). No automated empty-diff check exists in-tree — the plan
must add one (scripted `alembic revision --autogenerate --sql`) or record a manual verification step.

## No Analog Found

None. Every file in this phase maps to an exact or strong in-tree analog. The single genuinely-new
capability is the **empty-autogenerate-diff verification** (no precedent in-tree) — not a file with a
missing analog but a new test/verification step the planner must author (RESEARCH Wave 0 gap).

## Metadata

**Analog search scope:** `alembic/versions/` (016, 018, 019, 025, 026, 028, 031),
`src/phaze/models/` (analysis, metadata, cloud_job, fingerprint, scheduling_ledger, base, file, __init__),
`src/phaze/services/ingestion.py`, `src/phaze/routers/agent_files.py`,
`tests/integration/test_migrations/`.
**Files scanned:** 14
**Pattern extraction date:** 2026-07-08
