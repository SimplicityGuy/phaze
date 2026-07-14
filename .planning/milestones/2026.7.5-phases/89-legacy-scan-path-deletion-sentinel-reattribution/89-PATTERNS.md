# Phase 89: Legacy Scan-Path Deletion & Sentinel Reattribution - Pattern Map

**Mapped:** 2026-07-11
**Files analyzed:** 2 new (migration + migration test) + ~18 modified/deleted
**Analogs found:** 2 / 2 new artifacts (both exact) + all deletion sites grounded

This phase is **deletion-heavy plus one new data migration and its test**. The two NEW artifacts
(`alembic/versions/038_*.py`, `tests/integration/test_migrations/test_038_*.py`) carry all the
copy-from work; every other file is a delete or a small edit whose exact edit site is mapped below.

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `alembic/versions/038_*.py` (NEW) | migration | batch / transform | `012` (raw-SQL INSERT/UPDATE + sentinel + RESTRICT FK) + `035`/`036` (module-level static SQL, no-op downgrade docstring, saq banner) | exact (composite) |
| `tests/integration/test_migrations/test_038_*.py` (NEW) | test | batch | `test_024.py` (seed agents, upgrade→assert, IntegrityError, downgrade) + `test_035` (seed-corpus helper, autogenerate-empty, no-op downgrade assert) | exact (composite) |
| `tests/conftest.py` L198-217 | test (fixture) | — | itself L198-217 (repoint the seed) | in-place |
| `src/phaze/routers/scan.py` | route/controller | request-response | DELETE wholesale (no analog needed) | delete |
| `src/phaze/services/ingestion.py` | service | file-I/O + CRUD | DELETE wholesale (D-recommended) | delete |
| `src/phaze/schemas/scan.py` | schema | — | DELETE wholesale | delete |
| `src/phaze/main.py` L43, L193 | config (router wiring) | — | remove import + `include_router` | edit |
| `src/phaze/models/file.py` L88-93 | model | — | drop `default=` kwarg | edit |
| `src/phaze/models/scan_batch.py` L29-34 | model | — | drop `default=` kwarg | edit |
| ~11 `tests/integration/test_*.py` + 2 `tests/shared/core/*` | test | — | constant repoint / block delete | edit |

---

## Pattern Assignments

### `alembic/versions/038_*.py` (NEW migration — LEGACY-02 / LEGACY-03)

The 038 migration is a **composite** of three in-repo analogs. Copy structure from each:

**Analog A — `012_add_agents_table_and_backfill.py`** (raw-SQL writes, the sentinel it created, RESTRICT FK):

- Raw-SQL parameterized write idiom (`012:55-61`) — copy verbatim shape for the UPDATE/DELETE:
```python
op.get_bind().execute(
    sa.text(
        "INSERT INTO agents (id, name, token_hash, scan_roots, revoked_at, created_at, updated_at) "
        "VALUES (:id, :name, NULL, CAST(:scan_roots AS jsonb), NOW(), NOW(), NOW())"
    ),
    {"id": "legacy-application-server", "name": "legacy-application-server", "scan_roots": scan_roots_json},
)
```
- The `status='live'` sentinel batch this migration must DELETE was created here (`012:92-101`,
  `scan_path='<watcher>'`, `total_files=0`) and the colliding partial unique index is `012:104-110`:
  `uq_scan_batches_agent_id_live` = `UNIQUE (agent_id) WHERE status='live'`. This is the source of
  **Pitfall 1** — reattributing the legacy live batch to the target violates this index. DELETE it.
- Backfill UPDATE shape (`012:89-90`): `op.execute(sa.text("UPDATE files SET agent_id = '…' WHERE agent_id IS NULL"))`.

**Analog B — `035_reconcile_dedup_resolution.py` / `036_backfill_analysis_completed_at.py`** (module-level static SQL constants; no-op/irreversible downgrade docstring; header contract; saq banner):

- Revision-identifier block (`035:66-70` / `036:54-58`) — head is `037`, so:
```python
revision: str = "038"
down_revision: str | Sequence[str] | None = "037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
```
- Module-level named SQL constants (`035:80-100`, `036:66-74`) — 038 should hoist each statement to a
  `_DELETE_LEGACY_LIVE_BATCH`, `_REATTRIBUTE_FILES`, `_REATTRIBUTE_SCAN_BATCHES`, `_COUNT_REMAINING`,
  `_DELETE_SENTINEL` constant so the migration test can import + re-exec them (`test_035:226-228`
  imports `migration_035._BACKFILL_DEDUP` for its idempotency re-run).
- Header-docstring contract style (`035:30-52`, `036:19-40`): document SYNC migration, single-txn
  rollback semantics, the "NEVER reference `saq_jobs`" CRITICAL banner (a static test greps for it —
  see the test analog), and the irreversible-downgrade rationale.
- **`downgrade()` — DEVIATE from 035/036.** 035/036 use a documented **no-op** `pass` body. Per D-10,
  038 must instead **raise** (ownership is unrecoverable once merged):
```python
def downgrade() -> None:
    raise NotImplementedError(
        "038 merged legacy-application-server-owned files/scan_batches into the target fileserver; "
        "original ownership is unrecoverable, so the reattribution and sentinel row cannot be reconstructed."
    )
```

**Analog C — the `-x` override + auto-detect (NEW logic, no in-repo precedent; grounded in RESEARCH Migration Mechanics + `enqueue_router.select_active_agent`):**

- Read override via `context.get_x_argument(as_dictionary=True).get("reattribute_to")`.
- Auto-detect predicate mirrors `services/enqueue_router.py:select_active_agent(session, kind="fileserver")`
  written as raw SQL: `SELECT id FROM agents WHERE revoked_at IS NULL AND kind='fileserver'`.
  0 rows → `raise RuntimeError(...)`; >1 → `raise RuntimeError("… pass -x reattribute_to=<id> …")`.
- **Security (V5):** the override id MUST be parameterized (`sa.text(...).bindparams(...)` or a params
  dict), never f-stringed, and validated against `agents` (exists + `kind='fileserver'` + not revoked)
  before use. RESEARCH §Security + Code Example Pattern 1.

**Ordering inside the single txn (D-09 + Pitfall 1 fix):**
1. `DELETE FROM scan_batches WHERE agent_id='legacy-application-server' AND status='live'`  ← Pitfall-1 fix, first.
2. `UPDATE files SET agent_id=:target WHERE agent_id='legacy-application-server'`.
3. `UPDATE scan_batches SET agent_id=:target WHERE agent_id='legacy-application-server'` (no live rows remain).
4. Assert `COUNT(*)` legacy-owned across files ∪ scan_batches `= 0` else `raise RuntimeError` (rolls back before the sentinel DELETE). Copy the assert-then-delete shape from RESEARCH Pattern 3.
5. `DELETE FROM agents WHERE id='legacy-application-server'` (RESTRICT FK now satisfiable).

No DDL — this migration adds NO columns/constraints (D-07: no `server_default` exists). Autogenerate
against the 038 head must stay an EMPTY diff.

---

### `tests/integration/test_migrations/test_038_*.py` (NEW migration test — LEGACY-02/03 verification)

**Primary analog:** `tests/integration/test_migrations/test_024.py` (seeds `agents`, drives
downgrade→upgrade, asserts post-upgrade row state, uses `IntegrityError`, asserts a downgrade effect).
**Secondary analog:** `test_migration_035_reconcile_dedup_resolution.py` (module-level seed-corpus
helper, autogenerate-empty-diff helper, no-op-downgrade assert — but 038 asserts `NotImplementedError`).

**Harness (identical across all migration tests — copy from `test_024:27-45` + `conftest.py`):**
```python
from tests.integration.test_migrations.conftest import (
    MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, downgrade_to, upgrade_to,
)
# load-by-path because the module name starts with a digit (test_024:38-45):
spec = importlib.util.spec_from_file_location("migration_038", _MIGRATION_PATH)
```
Drive-sequence per test (`test_024:71-73`, `test_035:173-185`):
```python
cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
await asyncio.to_thread(downgrade_to, cfg, "base")
await asyncio.to_thread(upgrade_to, cfg, "037")     # seed at 037 (pre-038)
engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
# ... seed rows via engine.begin() + sa.text INSERT ...
await asyncio.to_thread(upgrade_to, cfg, "038")     # run the migration under test
# ... assert ...
finally: await asyncio.to_thread(downgrade_to, cfg, "base")
```
Seed-row idiom (`test_035:71-75` / `test_037:57-61`) — reuse the exact `_SEED_FILE_SQL` INSERT shape;
note it currently hardcodes `agent_id='legacy-application-server'`, which is correct for the 038 test's
legacy-owned rows, but the test must ALSO seed a real `kind='fileserver'` non-revoked target agent
(the 038 target) via an `INSERT INTO agents (...)` like `test_024:102-105`.

**Static, DB-free assertions (copy from `test_024:53-65` / `test_035:88-100`):**
- `revision=="038"`, `down_revision=="037"`, `branch_labels is None`.
- `test_migration_never_references_saq_jobs` grep guard — copy verbatim from `test_024:61-65`.
- Optional static-SQL guard (`test_035:103-111`) asserting the parameterized-`sa.text` / no-f-string shape.

**038-specific scenarios (RESEARCH §Migration Test Pattern, scenarios 1-8):**
1. Reattribution moves rows: legacy files + non-live scan_batches → `agent_id=<target>`, `COUNT(legacy)=0`.
2. Sentinel deleted: `SELECT COUNT(*) FROM agents WHERE id='legacy-application-server' == 0`.
3. **Live-batch collision (Pitfall 1):** seed BOTH a legacy `status='live'` batch AND a target-agent
   `status='live'` batch; run 038; assert **no `IntegrityError`** and the legacy live batch is gone.
   Use `pytest.raises(IntegrityError)` shape from `test_024:108-113` — but here assert it does NOT raise.
4. Abort on 0 fileserver: revoke the real fileserver so only revoked legacy remains; assert `upgrade_to("038")`
   raises and the sentinel still exists (rollback proof).
5. Abort on >1 fileserver (no override): seed two non-revoked fileservers; assert raise with the
   "pass -x reattribute_to" message.
6. **`-x` override path (A2 — no in-repo precedent, validate at plan time):** set
   `cfg.cmd_opts = argparse.Namespace(x=["reattribute_to=<id>"])` on the Config before `upgrade_to`,
   then assert `get_x_argument` reads it and reattributes to the chosen id.
7. **NON-reversible downgrade — DEVIATE from every prior migration test.** `test_024:116-119` asserts
   "downgrade drops X"; `test_035:241-244` asserts "no-op downgrade leaves markers unchanged". 038
   instead: `with pytest.raises(NotImplementedError): await asyncio.to_thread(downgrade_to, cfg, "037")`.
   **Do NOT copy the reversibility mirror.**
8. Empty autogenerate diff (optional): copy `_diffs_touching_*` + `compare_metadata` helper from
   `test_035:144-167` / `test_037:97-116`, scoped to an empty object set (038 adds no schema).

**FOOTGUN (memory-confirmed + `test_035:22-29` header):** `MIGRATIONS_TEST_DATABASE_URL` defaults to
port **5432**/`phaze_migrations_test`; the ephemeral bucket runs **5433**. `just test-bucket` does NOT
export it. Run via `just integration-test` or export the 5433 URL explicitly, or the harness silently
talks to the wrong DB and fails like an infra flake.

---

### `tests/conftest.py` L198-217 (D-08 seed repoint — highest reach)

**Analog: itself.** Current seed (`tests/conftest.py:212`, import at `:17`):
```python
from phaze.models.agent import LEGACY_AGENT_ID, Agent        # L17
setup_session.add(Agent(id=LEGACY_AGENT_ID, name=LEGACY_AGENT_ID, scan_roots=[]))   # L212
```
Change to a real fileserver, setting `kind` EXPLICITLY (the current seed omits it and leans on the DB
`server_default='fileserver'` at `agent.py:28`):
```python
setup_session.add(Agent(id="test-fileserver", name="test-fileserver", kind="fileserver", scan_roots=[]))
```
Update the fixture docstring (`:200-206`) which currently says it seeds the sentinel "relying on the
model-level default added in phase 24" — that default is being removed (D-06). Then repoint the ~11
integration tests' `_LEGACY_AGENT_ID` constant to `"test-fileserver"` (Category C list below).

---

## Deletion Sites (LEGACY-01 / LEGACY-03) — every edit site the planner must touch

| Target | Site | Action |
|--------|------|--------|
| `src/phaze/routers/scan.py` | whole file (95 lines; POST `/scan` L31-75, GET `/scan/{batch_id}` L78-95) | DELETE (D-04). No live consumer — verified (D-05). |
| `src/phaze/main.py` | L43 `scan,` in the router import tuple; L193 `app.include_router(scan.router)` | remove both lines |
| `src/phaze/services/ingestion.py` | whole file (220 lines). Trio: `discover_and_hash_files` L47, `bulk_upsert_files` L94, `run_scan` L128. Residue `normalize_path` L33 / `classify_file` L38 have no source caller (A1). | DELETE wholesale (recommended); also drop `LEGACY_AGENT_ID` import L20 |
| `src/phaze/schemas/scan.py` | whole file (31 lines; `ScanRequest`/`ScanResponse`/`ScanStatusResponse`) | DELETE (orphaned once router goes) |
| `src/phaze/models/file.py` | L88-93 — drop `default="legacy-application-server"` on `agent_id` (keep `nullable=False`, the `ForeignKey(..., ondelete="RESTRICT")`) | edit (D-06) |
| `src/phaze/models/scan_batch.py` | L29-34 — drop `default="legacy-application-server"` on `agent_id` | edit (D-06) |
| `src/phaze/models/agent.py` | L14 `LEGACY_AGENT_ID = "legacy-application-server"` | **KEEP** — still test-referenced + labels historical data (RESEARCH Open Q3) |

**Test blast radius:**

- **Category A — DELETE whole file:** `tests/discovery/routers/test_scan.py`,
  `tests/discovery/services/test_ingestion.py`, `tests/discovery/test_rescan_preserves_state.py`
  (imports `bulk_upsert_files` — Pitfall 3: first confirm `tests/agents/services/test_agent_upsert.py`
  covers the surviving `agent_files.py:136` ON-CONFLICT state-preservation twin).
- **Category B — edit (remove `run_scan` block, keep rest):** `tests/shared/core/test_phase02_gaps.py`
  (L22, L70-229), `tests/shared/core/test_no_auto_metadata_enqueue.py` (L8, L28, L103-143),
  `tests/metadata/tasks/test_metadata_extraction.py` (L232-235, stale comment only).
- **Category C — `_LEGACY_AGENT_ID` constant repoint → `"test-fileserver"` (D-08):**
  `test_stage_progress_buckets.py`, `test_pending_set_divergence.py`, `test_stage_status_equivalence.py`,
  `test_dedup_divergence.py`, `test_orphan_count.py`, `test_dedup_resolve_undo_shadow.py`,
  `test_fingerprint_progress.py`, `test_files_page.py`, `test_shadow_compare_skipped.py`,
  `test_enrich_pending_independence.py`, `test_shadow_compare.py` (all under `tests/integration/`).
- **Category E — DO NOT TOUCH (historical migration tests pinned to ≤037):** `test_012_upgrade.py`
  … `test_037_stage_skip.py`, `test_016/017/024`. They `upgrade_to` a specific pre-038 revision where
  the sentinel still exists. The `migrated_engine` fixture (upgrades to **head**, now incl. 038) —
  audit its consumers: any that insert `agent_id='legacy-application-server'` post-038 without adding
  that agent will hit an FK violation (the agent is deleted). The 038 test must self-seed its target fileserver.

**Pitfall 2 sweep (after D-06):** grep the whole `tests/` tree for `FileRecord(` and `ScanBatch(`
constructions; any WITHOUT an explicit `agent_id` will now fail NOT NULL + FK at flush (the model
default that used to cover it is gone). Most already pass `LEGACY_AGENT_ID`; repoint those to the seed id.

---

## Shared Patterns

### Raw-SQL migration writes (no model imports)
**Source:** `012:55-90`, `035:80-106`, `036:66-79`
**Apply to:** every statement in migration 038.
Parameterized `op.get_bind().execute(sa.text(...), {params})` or `op.execute(sa.text(CONSTANT))`.
Never import ORM models into a migration (immune to future model drift — 012 D-08/D-14 convention).

### Single-transaction assert-then-delete
**Source:** RESEARCH Pattern 3 (grounded in 012's ordered writes; Alembic `transaction_per_migration`)
**Apply to:** the 038 upgrade body. Any `raise` rolls back the whole txn — the sentinel DELETE is never
reached if the `COUNT=0` assert fails. No explicit `BEGIN` needed.

### `saq_jobs`-never-referenced guard
**Source:** `test_024:61-65`, `test_035:96-100`, `test_037:84-88` (identical grep guard)
**Apply to:** the 038 migration source (add the CRITICAL banner) + its test (copy the grep assertion).

### Migration-test drive harness
**Source:** `tests/integration/test_migrations/conftest.py:59-92` (`_build_alembic_config`,
`upgrade_to`/`downgrade_to` that patch `settings.database_url` around `command.*`), `migrated_engine:114-136`
**Apply to:** the 038 test — always `asyncio.to_thread(upgrade_to/downgrade_to, ...)` (nested
`asyncio.run` inside `env.py` crashes if called directly from an async test).

---

## No Analog Found

| Element | Role | Reason |
|---------|------|--------|
| `context.get_x_argument(as_dictionary=True)` `-x` override read | migration | No in-repo migration reads `-x`. API-verified (RESEARCH, alembic docs). Planner writes fresh. |
| Programmatic `-x` in a migration test (`cfg.cmd_opts = argparse.Namespace(x=[...])`) | test | No in-repo precedent (Assumption A2). Validate during planning; fallback is an env.py-level harness. |
| `downgrade()` that `raise NotImplementedError` | migration | Every existing migration downgrade either reverses DDL (012/024/037) or is a documented no-op (032/034/035/036). None raise. D-10 is a deliberate first. |

For these, the planner should follow RESEARCH.md §Migration Mechanics + §Migration Test Pattern rather
than an existing file.

## Metadata

**Analog search scope:** `alembic/versions/` (012, 024, 035, 036, 037), `tests/integration/test_migrations/`
(conftest, test_024, test_035, test_037), `src/phaze/{routers/scan,services/ingestion,schemas/scan,main,
models/file,models/scan_batch,models/agent}.py`, `tests/conftest.py`.
**Files scanned:** ~15 read in full/targeted + grep sweeps for `LEGACY_AGENT_ID` / scan-router wiring.
**Pattern extraction date:** 2026-07-11
