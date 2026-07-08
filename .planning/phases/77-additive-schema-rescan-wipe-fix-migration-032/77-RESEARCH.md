# Phase 77: Additive Schema & Rescan-Wipe Fix (migration `032`) - Research

**Researched:** 2026-07-08
**Domain:** Alembic additive schema migration + set-based backfill on a live ~200K-row Postgres corpus; ORM partial-index mirroring; idempotent-upsert bugfix
**Confidence:** HIGH (every claim below verified against the live tree at `SimplicityGuy/true-parallel`)

## Summary

Phase 77 lands a single additive Alembic migration `032` plus a two-line idempotency bugfix. Everything the plan needs is already precedented in-tree: the CHECK-membership widening (migrations `025`→`026`), the additive nullable column (`028`), the partial-index-plus-ORM-mirror house style (`018`, `019`), the set-based `op.execute(text(...))` backfill (`016`), and the per-migration integration-test shape (`test_migration_031_route_control.py`). There is **zero new dependency** and **zero new pattern** — this is a "follow the established idiom exactly" phase. The migration head is confirmed `031`, so this is `032` `[VERIFIED: alembic/versions/ listing]`.

Two findings materially shape the plan and are **not** obvious from the design doc. (1) `report_analysis_failed` writes **no `analysis` row** — it only updates `files.state` `[VERIFIED: routers/agent_analysis.py:329]`. Therefore the D-03 analyze-failed backfill **cannot be a plain `UPDATE analysis`**; it must be an idempotent **`INSERT ... ON CONFLICT (file_id) DO UPDATE`** so files that failed before any analysis-start partial row still get a marker. (2) The `ix_fprint_success` partial index uses a `status IN ('success','completed')` predicate, which Postgres reserializes internally as `= ANY (ARRAY[...])`; **no in-tree partial index has ever used `IN`** (all use simple `col = 'literal'`), so the empty-autogenerate-diff guarantee (SC#2) is *unproven for that shape* and is the single sharpest risk to PERF-01. Recommend authoring that predicate as `status = ANY (ARRAY['success','completed'])` in both the migration and the ORM mirror, and verifying the empty diff explicitly.

**Primary recommendation:** Ship the rescan fix (D-08) as task 1 (independently verifiable, no schema dependency), then `032` as: add columns → add `dedup_resolution` table → widen `cloud_job` status CHECK → create partial indexes (mirrored into ORM `__table_args__`) → backfill via set-based static SQL (upsert for analyze-failed, insert-if-missing for dedup/awaiting/pushing/pushed) → minimal DDL-only `downgrade()` (D-09). Verify empty autogenerate diff explicitly; it is not covered by any existing test.

## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** analyze + metadata `failed` markers = nullable `failed_at` + `error_message` columns on the existing 1:1 `analysis` / `metadata` tables (NOT a generic `stage_failure` table).
- **D-02:** metadata `done` predicate will later tighten to `EXISTS metadata WHERE file_id=… AND failed_at IS NULL`. This phase only creates the column — **call the tightening out in the migration docstring** so the reader phase honors it.
- **D-03:** Backfill asymmetry — **analyze backfills, metadata does not.** Set `analysis.failed_at` (`= updated_at`, else `now()`) with placeholder `error_message = 'backfilled from ANALYSIS_FAILED'` for every `state = ANALYSIS_FAILED` file. `metadata.failed_at` gets **no** backfill (no historical source). Document in docstring + VERIFICATION.
- **D-04:** `AWAITING_CLOUD` → add `AWAITING = "awaiting"` to `CloudJobStatus` StrEnum + the `ck_cloud_job_status_enum` CHECK. Awaiting file = `cloud_job` row `status='awaiting'`, `s3_key`/`upload_id` NULL. Reuses `uq_cloud_job_file_id`.
- **D-05:** `LOCAL_ANALYZING` gets **no sidecar row** (derived from `in_flight(analyze)` in a later phase).
- **D-06:** `PUSHING`/`PUSHED` backfill only fills gaps for legacy rows missing a `cloud_job` row (statuses `uploading`/`uploaded`).
- **D-07:** New `dedup_resolution(file_id UNIQUE FK, canonical_file_id FK, resolved_at)`. Marker-row existence = resolved; undo = DELETE the row. Backfill from `state = DUPLICATE_RESOLVED`, deriving `canonical_file_id` as the non-resolved member of each `sha256_hash` group.
- **D-08:** Rescan fix — remove `"state": excluded.state` from the ON CONFLICT `set_` in **both** `services/ingestion.py` (`bulk_upsert_files`) and `routers/agent_files.py`. Standalone first task.
- **D-09:** `032.downgrade()` is **minimal — simplest correct DDL reversal only.** Explicitly relaxes ROADMAP SC#4. Do not gold-plate reversal.

### Claude's Discretion
- Backfill batching: default set-based single statement per object; chunk only if a measured 200K-row statement proves problematic.
- `error_message` column type: `Text` (unbounded).
- Exact partial-index set (see PERF-01 finalization below).
- Index build lock behavior: decide CONCURRENTLY vs plain; **follow in-tree house style** (finding: house style is plain transactional builds — see Common Pitfalls).

### Deferred Ideas (OUT OF SCOPE — later phases)
- `stage_status()` derivation + `NOT EXISTS` pending-query rewrites (READ-*) — Phase 78/82.
- Shadow-compare invariant gate (MIG-02) — Phase 79.
- Destructive `033`: drop `files.state`, drop `ix_files_state`, delete `FileState` enum (MIG-04) — Phase 90.
- The six latent-bug fixes (design §4.1) — reader/writer rework phases.

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| **MIG-01** | `032` additive-only: create failure markers, dedup marker, cloud sidecar representation, partial indexes; backfill from `FileRecord.state`; never touch `files.state`. | Column/table/CHECK/index/backfill idioms all verified in-tree (§Standard Stack, §Code Examples). `files.state` byte-unchanged = never appears in any `op.*` write. |
| **MIG-03** | Rescanning a file no longer resets progress — remove `ON CONFLICT DO UPDATE SET state = excluded.state` from both upsert sites. | Both sites located + confirmed (§Architecture Patterns → Rescan fix). INSERT still stamps `DISCOVERED` via the VALUES dict. |
| **PERF-01** | Partial indexes sized to exact `done`/`failed` predicates (`IS NOT NULL`-shaped, never `status IN (...)` where `IS NOT NULL` is the real query), mirrored into ORM `__table_args__` for an empty autogenerate diff. | Index set finalized below; `IN`-predicate autogenerate hazard flagged (§Common Pitfalls Pitfall 1). |

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Additive DDL (columns, table, CHECK, indexes) | Database / Storage (Alembic migration) | — | Schema objects are DDL; migrations own them. Sync `def upgrade()`. |
| Backfill from `files.state` | Database / Storage (set-based SQL in migration) | — | Data motion co-located with the schema that receives it; static `op.execute(text(...))`. |
| ORM `__table_args__` index mirror | API / Backend (SQLAlchemy models) | Database | Autogenerate parity (SC#2) requires the ORM to declare every index the migration creates. |
| Rescan idempotency fix | API / Backend (ingestion service + agent router) | — | The `ON CONFLICT` `set_` dict is application code at the two upsert chokepoints; pure code edit, no schema. |

## Standard Stack

No new packages. The milestone constraint is **zero new dependencies** `[VERIFIED: REQUIREMENTS.md "Out of Scope"]`. Everything uses the existing stack:

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Alembic | 1.18.x (installed) | Migration `032` | Project migration tool; sync `upgrade()`/`downgrade()`, 3-digit zero-padded string revisions `[VERIFIED: alembic/versions/*]`. |
| SQLAlchemy | 2.0.x (installed) | ORM models + `pg_insert` upsert | `Index(..., postgresql_where=text(...))` for the ORM mirror; `sqlalchemy.dialects.postgresql.insert` at the rescan sites `[VERIFIED: services/ingestion.py:16]`. |
| asyncpg | 0.30.x (installed) | Async driver (app + tests) | Migrations run sync via `env.py`'s `asyncio.run`; tests use `create_async_engine`. |
| pytest / pytest-asyncio | installed | Per-migration integration test | Pattern in `tests/integration/test_migrations/` `[VERIFIED]`. |

**Installation:** none. `uv sync` already provides all of the above.

## Package Legitimacy Audit

**Not applicable — this phase installs no external packages.** Milestone hard constraint: "New runtime dependencies → Hard milestone constraint — the existing PostgreSQL + SQLAlchemy 2.x stack suffices" `[VERIFIED: REQUIREMENTS.md Out of Scope]`. No `slopcheck` / registry verification required.

## Architecture Patterns

### System Data Flow (migration `032`)

```
alembic upgrade head (sync, env.py → asyncio.run(run_async_migrations))
        │
        ▼
032.upgrade()  ── touches ONLY: analysis, metadata, dedup_resolution, cloud_job
        │         (NEVER files.state; NEVER saq_jobs)
        ├─ op.add_column("analysis",  failed_at TIMESTAMPTZ NULL, error_message TEXT NULL)
        ├─ op.add_column("metadata",  failed_at TIMESTAMPTZ NULL, error_message TEXT NULL)
        ├─ op.create_table("dedup_resolution", file_id UQ-FK, canonical_file_id FK NULL, resolved_at)
        ├─ op.drop_constraint("status_enum","cloud_job",type_="check")
        │  op.create_check_constraint("status_enum","cloud_job", <7-member list incl 'awaiting'>)
        ├─ op.create_index(<partial indexes>)          ← mirrored into ORM __table_args__
        └─ BACKFILL (set-based static SQL, one stmt per object):
             ├─ analyze-failed : INSERT..SELECT..FROM files WHERE state='analysis_failed'
             │                   ON CONFLICT (file_id) DO UPDATE SET failed_at=…, error_message=…
             │                   (UPSERT — an analysis row is NOT guaranteed to exist)
             ├─ dedup          : INSERT..SELECT resolved files + derived canonical_file_id
             ├─ awaiting       : INSERT/promote cloud_job status='awaiting' WHERE state='awaiting_cloud'
             └─ pushing/pushed : INSERT cloud_job status='uploading'/'uploaded' for gap rows only
        │
        ▼
Reader phases (78/82+) consume these objects — NOTHING reads them in Phase 77.
```

### Pattern 1: Widen the `cloud_job` status CHECK (add `AWAITING`)
**What:** String-backed StrEnum member + CHECK-membership widening. No Postgres enum-type migration.
**When:** Adding `AWAITING = "awaiting"` (D-04). `'awaiting'` is 8 chars, fits `status String(16)` `[VERIFIED: models/cloud_job.py:78]`.
**Example (exact in-tree idiom, from migration 026):**
```python
# Source: alembic/versions/026_add_cloud_job_kube_columns.py:45-57  [VERIFIED]
_STATUS_ENUM_OLD = "status IN ('uploading', 'uploaded', 'submitted', 'running', 'succeeded', 'failed')"
_STATUS_ENUM_NEW = "status IN ('uploading', 'uploaded', 'submitted', 'running', 'succeeded', 'failed', 'awaiting')"
# Bare name "status_enum" — the ck_%(table_name)s_%(constraint_name)s convention re-applies the
# ck_cloud_job_ prefix, resolving to the live ck_cloud_job_status_enum. Passing the already-prefixed
# name double-prefixes it.  [VERIFIED: models/base.py:9-15 naming convention]
op.drop_constraint("status_enum", "cloud_job", type_="check")
op.create_check_constraint("status_enum", "cloud_job", _STATUS_ENUM_NEW)
```
Model change: append `AWAITING = "awaiting"` to `CloudJobStatus` and update the `CheckConstraint("status IN (...)", name="status_enum")` list in `models/cloud_job.py:107-110` `[VERIFIED]`.

### Pattern 2: Additive nullable columns on an existing table
```python
# Source: alembic/versions/028_add_analysis_completed_at.py:43  [VERIFIED]
op.add_column("analysis", sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True))
op.add_column("analysis", sa.Column("error_message", sa.Text(), nullable=True))
# same two columns on "metadata"
```
`analysis` and `metadata` models currently have **no `__table_args__`** `[VERIFIED: grep]` — the plan must add the columns as `Mapped[... | None]` AND add a `__table_args__` tuple for the new partial indexes.

### Pattern 3: Partial index + ORM `__table_args__` mirror (the empty-diff contract)
```python
# MIGRATION — Source idiom: alembic/versions/018:71, 019:72  [VERIFIED]
op.create_index("ix_analysis_completed", "analysis", ["file_id"],
                postgresql_where=sa.text("analysis_completed_at IS NOT NULL"))
op.create_index("ix_analysis_failed", "analysis", ["file_id"],
                postgresql_where=sa.text("failed_at IS NOT NULL"))
op.create_index("ix_metadata_failed", "metadata", ["file_id"],
                postgresql_where=sa.text("failed_at IS NOT NULL"))

# ORM MIRROR — Source idiom: design §5, models/fingerprint.py:25  [VERIFIED]
# analysis.py
__table_args__ = (
    Index("ix_analysis_completed", "file_id", postgresql_where=text("analysis_completed_at IS NOT NULL")),
    Index("ix_analysis_failed",    "file_id", postgresql_where=text("failed_at IS NOT NULL")),
)
```
The explicit index name is what autogenerate compares on — the ORM name must equal the DB name exactly, and the `postgresql_where` text must render to the same normalized Postgres expression (see Pitfall 1).

### Pattern 4: Set-based backfill via static SQL
```python
# Source idiom: alembic/versions/016_backfill_scan_batches_completed_at.py:46  [VERIFIED]
# Static literals only — no model import, no interpolated input (bandit S608 / no injection surface).
op.execute(sa.text("UPDATE ... WHERE state = 'analysis_failed'"))
```
Default is one set-based statement per object (D-09 discretion). At ~200K files the analyze-failed / dedup / cloud backfills touch small subsets (only the rows in those specific states), so chunking is almost certainly unnecessary — but the partial *index builds* are the real lock concern, not the backfill (Pitfall 3).

### Rescan fix (MIG-03 / D-08) — the two upsert sites (VERIFIED, line numbers current)
Both sites carry `"state": excluded.state` in the ON CONFLICT `set_` dict and **nothing else state-related depends on that clause** — removing it is a clean two-line deletion; the INSERT branch still stamps `DISCOVERED` via the VALUES dict.

- **Site 1 — `services/ingestion.py` `bulk_upsert_files`, line 114** `[VERIFIED]`:
  ```python
  set_={
      "sha256_hash": stmt.excluded.sha256_hash,
      "file_size": stmt.excluded.file_size,
      "state": stmt.excluded.state,   # ← DELETE this line (D-08)
      "batch_id": stmt.excluded.batch_id,
      "file_type": stmt.excluded.file_type,
  },
  ```
  New-file INSERT still gets `state = DISCOVERED` from `discover_and_hash_files` (`ingestion.py:86`) `[VERIFIED]`.

- **Site 2 — `routers/agent_files.py` `upsert_files`, line 133** `[VERIFIED]`:
  ```python
  set_={
      "sha256_hash": base_stmt.excluded.sha256_hash,
      "file_size": base_stmt.excluded.file_size,
      "state": base_stmt.excluded.state,   # ← DELETE this line (D-08)
      "batch_id": base_stmt.excluded.batch_id,
      "file_type": base_stmt.excluded.file_type,
  },
  ```
  New-file INSERT still gets `state = DISCOVERED` from `data["state"] = FileState.DISCOVERED` (`agent_files.py:111`) `[VERIFIED]`.

Regression test (D-08): upsert a file → advance to `ANALYZED` + create its `analysis` row → re-upsert same `(agent_id, original_path)` → assert `state` stays `ANALYZED` **and** the `analysis` row survives. This test has **no dependency on `032`** and should ship as task 1.

### Anti-Patterns to Avoid
- **Referencing `saq_jobs` in the migration** — every migration since `020` carries a "NEVER reference saq_jobs" banner; `test_migration_never_references_saq_jobs` greps for it `[VERIFIED: test_migration_031:58-62]`. The migration and its test must both carry the banner + guard.
- **Plain `UPDATE analysis SET failed_at` for the analyze backfill** — an `analysis` row is not guaranteed to exist (see Pitfall 2). Must be an upsert.
- **`status IN (...)` on any new `failed_at` index** — PERF-01 forbids `IN`-shaped where `IS NOT NULL` is the real predicate. The failed markers are genuinely `IS NOT NULL`.
- **Over-building `downgrade()`** — D-09: DDL reversal only; data backfills use a no-op downgrade (016 precedent).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| CHECK-membership change | Custom `ALTER TABLE ... DROP/ADD CONSTRAINT` strings | `op.drop_constraint` + `op.create_check_constraint` with bare name `"status_enum"` | Naming convention re-prefixing is a known footgun; the 026 idiom handles it. |
| Partial index parity | Hand-diffing DB vs ORM | ORM `Index(..., postgresql_where=text(...))` mirror | Autogenerate is the parity check (SC#2); mirror or it churns. |
| Migration test harness | New DB spin-up | `tests/integration/test_migrations/conftest.py` helpers (`upgrade_to`/`downgrade_to`/`_reset_schema`) | Handles nested-event-loop + schema reset; reused by every migration test. |
| Analyze-failed marker insert | Manual row-exists check + branch | `INSERT ... ON CONFLICT (file_id) DO UPDATE` (unique `file_id`) | Postgres upsert is atomic and idempotent; handles both "partial row exists" and "no row" in one statement. |

**Key insight:** This phase is entirely a matter of matching five existing in-tree idioms. Any bespoke SQL is a smell.

## Runtime State Inventory

This is an additive schema + data-backfill phase against a **live ~200K-file corpus**. The migration itself IS the data-state change, but the following runtime considerations apply:

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | `files.state` values `analysis_failed` / `duplicate_resolved` / `awaiting_cloud` / `pushing` / `pushed` are the backfill sources. `analysis` rows may or may not exist for `analysis_failed` files (report_analysis_failed writes none). `cloud_job` rows already exist for live-cloud `pushing`/`pushed`/`awaiting` files. | Backfill new markers/sidecar rows. Use UPSERT for analyze-failed; insert-if-missing for cloud/dedup. `files.state` itself is **byte-unchanged** (never written). |
| Live service config | None — no external service (n8n / scheduler / registry) embeds this schema. | None. |
| OS-registered state | None. | None. |
| Secrets/env vars | Migration test DB URL `MIGRATIONS_TEST_DATABASE_URL` (defaults `:5432/phaze_migrations_test`; `just integration-test` overrides to `:5433`) `[VERIFIED: conftest.py:35, justfile:215]`. | No secret change. |
| Build artifacts | None — no package rename. | None. |

**Live-corpus snapshot caveat:** files in `PUSHING`/`uploading` at deploy time are mid-transfer. The design (§6.2) defers the quiesce/drain requirement to the **destructive `033`** (Phase 90), not `032` — `032` is additive and non-destructive, so a moving target only means a handful of cloud rows may be re-derived on the next reconcile. **No drain required for `032`.** Note this in VERIFICATION so it is not confused with the `033` quiesce gate.

## Common Pitfalls

### Pitfall 1: `status IN (...)` partial index causes autogenerate churn (breaks SC#2)
**What goes wrong:** `ix_fprint_success` with `postgresql_where=text("status IN ('success','completed')")` — Postgres stores the predicate normalized as `status::text = ANY (ARRAY['success'::text, 'completed'::text])`. On the next `alembic revision --autogenerate`, SQLAlchemy compares the ORM's `IN (...)` text against Postgres's reserialized `= ANY(ARRAY[...])` and reports a spurious drop+create — a **non-empty diff**, failing SC#2 and PERF-01's mirror guarantee.
**Why it happens:** No in-tree partial index has ever used `IN` — `018` uses `tier = 'fine'`, `019` uses `status = 'pending'`, `012` uses `status = 'live'`, `014` uses `revoked_at IS NULL` `[VERIFIED: grep]`. All are simple equality / IS-NULL, which round-trip cleanly. `IN` is a new, unproven shape.
**How to avoid:** Author the fingerprint-success predicate as `status = ANY (ARRAY['success','completed'])` in BOTH the migration `postgresql_where` and the ORM mirror so the text matches Postgres's normalized form; OR restrict `032` to the `IS NOT NULL`/simple-equality indexes and defer `ix_fprint_success` to the reader phase (Phase 82) that actually queries it. **Recommendation:** include it but with the `= ANY(ARRAY[...])` spelling, and add an explicit empty-diff check (see Validation Architecture) — do not assume it round-trips.
**Warning signs:** `alembic revision --autogenerate` emits an `op.drop_index`/`op.create_index` pair for a just-created index.

### Pitfall 2: analyze-failed backfill assumes an `analysis` row exists — it does not
**What goes wrong:** `UPDATE analysis SET failed_at=… WHERE file_id IN (SELECT id FROM files WHERE state='analysis_failed')` silently misses every failed file that has no `analysis` row.
**Why it happens:** `report_analysis_failed` (`routers/agent_analysis.py:329`) updates only `files.state` — it writes **no** `analysis` row `[VERIFIED]`. A partial `analysis` row exists only if analysis progressed far enough to hit the start-upsert (`agent_analysis.py:294`); a file that timed out/crashed during download never gets one.
**How to avoid:** Backfill as an idempotent upsert keyed on the unique `analysis.file_id`:
```sql
INSERT INTO analysis (id, file_id, failed_at, error_message, created_at, updated_at)
SELECT gen_random_uuid(), f.id, COALESCE(f.updated_at, now()),
       'backfilled from ANALYSIS_FAILED', now(), now()
FROM files f
WHERE f.state = 'analysis_failed'
ON CONFLICT (file_id) DO UPDATE
  SET failed_at = COALESCE(analysis.failed_at, EXCLUDED.failed_at),
      error_message = COALESCE(analysis.error_message, EXCLUDED.error_message);
```
`analysis_completed_at` stays NULL for these rows (a failed analysis is not complete) — correct for the future `done ≻ failed` precedence. (Confirm `gen_random_uuid()` availability, i.e. pgcrypto/pg13+ built-in; the DB is Postgres 16+/18.4, so it is built-in.)
**Warning signs:** post-backfill `COUNT(analysis WHERE failed_at IS NOT NULL)` < `COUNT(files WHERE state='analysis_failed')`.

### Pitfall 3: partial-index build takes an ACCESS-EXCLUSIVE-class lock on the live 200K table
**What goes wrong:** Plain `CREATE INDEX` takes a `SHARE` lock that blocks writes for the build duration; on a busy 200K table the 5s `/pipeline/stats`-adjacent writers can stall.
**Why it happens:** House style uses plain transactional `op.create_index` — **no migration in the tree uses `CREATE INDEX CONCURRENTLY`** `[VERIFIED: grep "CONCURRENTLY" → none]`. CONCURRENTLY cannot run inside Alembic's implicit transaction and would require a non-transactional migration.
**How to avoid:** **Follow house style — plain `op.create_index`** (CONTEXT D-09 discretion says "follow in-tree house style"). The new partial indexes are on `analysis`/`metadata`/`dedup_resolution`/`cloud_job`, which are far smaller than `files` (only rows that reached those stages), so build time is short and the lock window is small. Do **not** invent a CONCURRENTLY policy for `032`. If a live-index-lock concern is ever real, it belongs to the reader/perf phase (82) with a measurement, per YAGNI. Report the tradeoff in VERIFICATION; do not gold-plate.
**Warning signs:** none expected at this table size; flag only if a measured build blocks writers.

### Pitfall 4: dedup `canonical_file_id` derivation is ambiguous for 0 or >1 non-resolved members
**What goes wrong:** D-07 derives `canonical_file_id` as "the non-resolved member of each `sha256_hash` group." A correlated subquery `WHERE sha256_hash = g AND state <> 'duplicate_resolved'` can return **zero** rows (whole group resolved / canonical later moved to `MOVED`/`APPROVED`) or **more than one** (multiple non-resolved copies).
**Why it happens:** The original keeper choice was never persisted — `resolve_group` takes `canonical_id` from the UI and `undo_resolve` restored an ephemeral `previous_state` list `[VERIFIED: services/dedup.py:251-286]`. There is no historical canonical record to recover.
**How to avoid:**
1. Make `dedup_resolution.canonical_file_id` **NULLABLE** — the marker's primary job is "this file is resolved"; the canonical pointer is best-effort.
2. Derive deterministically: pick a stable single canonical per group among non-resolved members (recommend `MIN(id)` or shortest `original_path` — a stable, index-friendly tiebreak; note it may not match the original human keeper, which is acceptable and documented).
3. If zero non-resolved members → `canonical_file_id = NULL` (still record the resolved marker).
```sql
INSERT INTO dedup_resolution (id, file_id, canonical_file_id, resolved_at)
SELECT gen_random_uuid(), f.id,
       (SELECT c.id FROM files c
        WHERE c.sha256_hash = f.sha256_hash AND c.state <> 'duplicate_resolved'
        ORDER BY c.id LIMIT 1),          -- NULL if none; deterministic if many
       COALESCE(f.updated_at, now())
FROM files f
WHERE f.state = 'duplicate_resolved'
ON CONFLICT (file_id) DO NOTHING;
```
**Warning signs:** FK violation on `canonical_file_id` (only if declared NOT NULL); duplicate `file_id` (prevented by the unique constraint + `ON CONFLICT DO NOTHING`).

### Pitfall 5: forgetting the ORM mirror → autogenerate wants to CREATE the index
**What goes wrong:** Index created in migration but not declared in `__table_args__` → autogenerate proposes creating it (non-empty diff).
**How to avoid:** Every `op.create_index` in `032` has a matching `Index(...)` in the owning model's `__table_args__`. `analysis`/`metadata` need `__table_args__` added from scratch; `cloud_job` already has one (add the awaiting-lookup index there if used). Verify with an explicit empty-diff step.

## Code Examples

### Per-migration integration test skeleton (mirror of the in-tree pattern)
```python
# Source: tests/integration/test_migrations/test_migration_031_route_control.py  [VERIFIED]
# 1) DB-free static assertions (run even without Postgres):
def test_revision_identifiers_are_bare_numbers() -> None:
    m = _load_migration_032()
    assert m.revision == "032"
    assert m.down_revision == "031"
    assert m.branch_labels is None

def test_migration_never_references_saq_jobs() -> None:
    body = _MIGRATION_PATH.read_text(encoding="utf-8").splitlines()
    offending = [l for l in body if "saq_jobs" in l and not l.lstrip().startswith("#") and "never reference" not in l.lower()]
    assert not offending

# 2) Integration body (needs phaze_migrations_test DB):
@pytest.mark.asyncio
async def test_upgrade_032_creates_and_backfills_then_downgrade_reverses() -> None:
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "031")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        # seed a corpus at 031: files in analysis_failed / duplicate_resolved / awaiting_cloud / pushing / pushed
        # (+ a matching sha256 group for dedup canonical derivation)
        await asyncio.to_thread(upgrade_to, cfg, "032")
        # assert: columns exist; dedup_resolution table exists; ck has 'awaiting';
        #         analyze-failed markers == count(files state=analysis_failed);
        #         metadata.failed_at all NULL (D-03 no backfill);
        #         cloud_job awaiting/uploading/uploaded rows present; files.state UNCHANGED;
        #         each partial index present in pg_indexes.
        await asyncio.to_thread(downgrade_to, cfg, "031")
        # assert: additive objects gone (DDL reversal; backfilled data loss acceptable per D-09).
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")
```
Run: `just integration-test` (spins ephemeral PG on `:5433`, creates `phaze_migrations_test`, sets `MIGRATIONS_TEST_DATABASE_URL`) `[VERIFIED: justfile:209-215]`, or `just test-db` + `just test-bucket integration`.

### `env.py` autogenerate context (why the mirror must match exactly)
```python
# Source: alembic/env.py  [VERIFIED]
target_metadata = Base.metadata        # line 33 — Base.metadata carries the naming convention
# ... context.configure(..., compare_type=True, ...)   # line 64
```
`compare_type=True` compares column types; index `postgresql_where` text is compared literally, which is why Pitfall 1 matters.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Linear `files.state` scalar as pipeline authority | Derived per-stage status from output tables (this milestone) | Phase 77+ | `032` builds the substrate; nothing reads it yet. |
| Failure only via `state='analysis_failed'` | Durable `failed_at`/`error_message` marker columns | Phase 77 (schema) / 81 (writers) | Backfill populates analyze markers now; metadata go-forward only. |

**Deprecated/outdated:** nothing removed in `032` (additive-only). `files.state`, `ix_files_state`, and the `FileState` enum are dropped in `033` (Phase 90), not here.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `gen_random_uuid()` is available as a built-in in the target Postgres (16+/18.4) for backfill INSERTs. | Pitfall 2/4 | If on <13 or pgcrypto absent, backfill INSERT fails; use `uuid_generate_v4()` or app-side. Verify Postgres version on the corpus. REQUIREMENTS.md cites "PostgreSQL 18.4" → built-in, low risk. |
| A2 | `MIN(id)` / shortest-path is an acceptable deterministic canonical tiebreak for dedup backfill (original keeper not recoverable). | Pitfall 4 | If operator wants the "best-quality" canonical (bitrate/tags per `score_group`), backfill needs a metadata join. Flag for discuss-phase; recommend simple deterministic + nullable column. |
| A3 | Plain (non-CONCURRENT) index builds are acceptable on `analysis`/`metadata`/`dedup_resolution`/`cloud_job` at corpus scale (these tables are smaller than `files`). | Pitfall 3 | If any of these tables is unexpectedly large/hot, a plain build could stall writers. House style is plain builds; measurement deferred to Phase 82 per YAGNI. |

## Open Questions

1. **Include `ix_fprint_success` in `032` or defer to Phase 82?**
   - What we know: CONTEXT lists it in the "at minimum" set, but its `IN`-predicate is an autogenerate hazard (Pitfall 1) and nothing in `032` reads it.
   - What's unclear: whether the empty-diff round-trips with `= ANY(ARRAY[...])` spelling on this Postgres.
   - Recommendation: include it with the `= ANY(ARRAY[...])` spelling AND an explicit empty-diff verification; if the diff is non-empty, defer it to Phase 82 (the reader that queries it) rather than fighting normalization.

2. **Does the operator want quality-ranked dedup canonical, or is deterministic-stable sufficient?**
   - Recommendation: deterministic-stable + nullable column now (Pitfall 4); a quality re-rank can be a later enrichment. Surface at plan/discuss time.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| PostgreSQL (ephemeral test) | Migration integration test | ✓ via `just test-db` | 16+ container | — |
| `phaze_migrations_test` DB | `tests/integration/test_migrations/` | ✓ created by `just test-db` | — | operator creates manually |
| alembic / sqlalchemy / asyncpg | migration + models + tests | ✓ (`uv sync`) | installed | — |

**Missing dependencies with no fallback:** none.
**Missing dependencies with fallback:** none — all in the existing stack.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (`uv run pytest`) |
| Config file | `pyproject.toml` (`[tool.pytest.ini_options]`) + `tests/buckets.json` per-bucket isolation |
| Quick run command | `uv run pytest tests/integration/test_migrations/test_migration_032_*.py -x` |
| Full suite command | `just integration-test` (spins ephemeral PG :5433 + `phaze_migrations_test`) |
| Bucket | **`integration`** (migration tests live under `tests/integration/`; the rescan-fix unit tests may sit in `discovery`/`agents` per their touched module — enforce one bucket per file via `tests/shared/test_partition_guard.py`) |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| MIG-03 | Rescan of an `ANALYZED` file preserves `state` + its `analysis` row | unit/integration | `uv run pytest tests/.../test_rescan_preserves_state.py -x` | ❌ Wave 0 |
| MIG-01 | `032` upgrade creates columns/table/CHECK; backfill row counts match legacy state counts; `files.state` byte-unchanged; `saq_jobs` never referenced | integration | `uv run pytest tests/integration/test_migrations/test_migration_032_*.py -x` | ❌ Wave 0 |
| MIG-01 | analyze-failed markers == `COUNT(files state='analysis_failed')`; metadata.failed_at all NULL; awaiting/pushing/pushed cloud_job rows present | integration | (same file, data asserts) | ❌ Wave 0 |
| PERF-01 | Each new partial index exists in `pg_indexes` and is `IS NOT NULL`/`= ANY`-shaped (never bare `IN`) | integration | (same file, `pg_indexes` assert) | ❌ Wave 0 |
| PERF-01 | `alembic revision --autogenerate` produces an EMPTY diff (ORM mirror parity) | integration | new empty-diff check (see below) | ❌ Wave 0 |
| MIG-01 | `saq_jobs` never referenced (banner grep) | unit (DB-free) | `test_migration_never_references_saq_jobs` | ❌ Wave 0 |
| D-09 | `032.downgrade()` reverses additive DDL objects | integration | (same migration test, downgrade body) | ❌ Wave 0 |

### Observable Behaviors to Validate (for VALIDATION.md)
1. **Migration applies on a corpus copy** — `upgrade 031→032` succeeds against a seeded `phaze_migrations_test`.
2. **Backfill row counts match legacy state counts** — `analysis.failed_at IS NOT NULL` count == `files.state='analysis_failed'` count; `dedup_resolution` count == `files.state='duplicate_resolved'` count; `cloud_job status='awaiting'` count == gap for `files.state='awaiting_cloud'`.
3. **`files.state` byte-unchanged** — snapshot `files.state` before/after `032`, assert identical.
4. **Empty autogenerate diff** — `alembic revision --autogenerate` against the `032` head yields no `op.create_index`/`op.add_column`/`op.*` operations. *(No such automated check exists today — this phase should add one, or the plan must include a manual verification step recorded in VERIFICATION.)*
5. **Rescan preserves progress** — advance a file to `ANALYZED` + `analysis` row, re-upsert, assert `state='ANALYZED'` and row survives (both sites).
6. **Per-migration integration test green** — in `integration` bucket, passes in isolation via `just test-bucket integration`.

### Sampling Rate
- **Per task commit:** `uv run pytest <the touched test file> -x` (rescan test after task 1; migration test after `032` lands).
- **Per wave merge:** `just test-bucket integration` (+ `discovery`/`agents` if the rescan tests land there) in isolation — per-bucket hermeticity is enforced.
- **Phase gate:** `just integration-test` green + `uv run ruff check .` + `uv run mypy .` + `pre-commit run --all-files`; explicit empty-autogenerate-diff verification recorded in VERIFICATION.

### Wave 0 Gaps
- [ ] `tests/integration/test_migrations/test_migration_032_additive_schema.py` — covers MIG-01, PERF-01, D-09 (mirror `test_migration_031_route_control.py`).
- [ ] `tests/<discovery|agents>/test_rescan_preserves_state.py` — covers MIG-03 (both upsert sites).
- [ ] Empty-autogenerate-diff assertion — **new capability**, no precedent in-tree; either a scripted `alembic revision --autogenerate --sql` diff check or a documented manual step.
- [ ] Framework install: none — pytest/pytest-asyncio already present.

## Security Domain

Migration `032` runs **static, parameterless SQL** (literal state strings only) — no user input is interpolated, so there is no injection surface (bandit `S608` is project-configured; the backfills follow the 016 "static literals only, no injection surface" precedent) `[VERIFIED: 016 docstring, CLAUDE.md bandit -s B608]`.

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V5 Input Validation | yes | Migration SQL uses only static literals (no interpolation); the rescan fix is a pure `set_`-dict deletion at an already-authenticated agent endpoint (`agent_id` from auth dep, never body — AUTH-01 preserved) `[VERIFIED: agent_files.py:110]`. |
| V6 Cryptography | no | No crypto touched (`sha256_hash` is read for dedup grouping only, not computed here). |
| V2/V3/V4 (Authn/Session/Access) | no | No auth/session/access-control change; the rescan fix does not alter the endpoint's auth contract. |

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| SQL injection in backfill | Tampering | Static literal SQL via `op.execute(sa.text(...))`; no interpolated input (S608). |
| Migration touching SAQ-owned `saq_jobs` | Tampering / repudiation of queue state | Hard banner + `test_migration_never_references_saq_jobs` grep guard. |

## Sources

### Primary (HIGH confidence — verified in this session against the live tree)
- `alembic/versions/016,018,019,025,026,028,031_*.py` — backfill, partial-index, table-create, CHECK-widen, additive-column idioms + migration head confirmation.
- `src/phaze/models/{analysis,metadata,cloud_job,file,fingerprint,base}.py` — column shapes, `CloudJobStatus` StrEnum + `ck_cloud_job_status_enum` CHECK, naming convention, absent `__table_args__`.
- `src/phaze/services/ingestion.py:94-122`, `src/phaze/routers/agent_files.py:104-141` — the two rescan upsert sites (line numbers current).
- `src/phaze/routers/agent_analysis.py:294,329` — analysis start-upsert vs. `report_analysis_failed` (no analysis row written).
- `src/phaze/services/dedup.py:251-286` — `resolve_group`/`undo_resolve`, ephemeral `previous_state`, `state != DUPLICATE_RESOLVED` exclusions.
- `tests/integration/test_migrations/{conftest.py,test_migration_031_route_control.py}` — integration-test pattern + DB URL/bucket.
- `alembic/env.py` — `target_metadata = Base.metadata`, `compare_type=True`.
- `justfile:209-215`, `tests/buckets.json` — `integration` bucket + `just integration-test` on `:5433`.
- `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md`, `.planning/phases/77-*/77-CONTEXT.md`, `.planning/{ROADMAP,REQUIREMENTS}.md` — locked decisions + requirements.

### Secondary / Tertiary
- None required; all findings verified against primary sources in-repo.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — zero new deps; all idioms verified in-tree.
- Architecture (migration shape, backfill, rescan fix): HIGH — every site + pattern read directly.
- Pitfalls: HIGH for 2/3/4/5 (verified from code); MEDIUM for 1 (the `IN`→`= ANY` autogenerate normalization is a well-known Postgres behavior but not empirically tested on this exact DB this session — hence the explicit empty-diff verification recommendation).

**Research date:** 2026-07-08
**Valid until:** 2026-08-07 (stable — internal codebase, no fast-moving external deps)

## RESEARCH COMPLETE
