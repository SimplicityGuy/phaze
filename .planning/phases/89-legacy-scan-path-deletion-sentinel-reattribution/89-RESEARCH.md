# Phase 89: Legacy Scan-Path Deletion & Sentinel Reattribution - Research

**Researched:** 2026-07-11
**Domain:** Alembic data migration + dead-code deletion (refactor/migration phase)
**Confidence:** HIGH (all claims grounded in codebase reads; Alembic `-x` API verified via official docs)

## Summary

This is a **deletion + data-migration** phase with a large but well-bounded blast radius. Every source claim below is grounded in a direct codebase read. Three moves in strict order: delete the orphaned legacy scan path (LEGACY-01), reattribute historical `legacy-application-server`-owned rows to a real fileserver agent via an Alembic data migration (LEGACY-02), then drop the model-level `default=` and delete the sentinel `Agent` row (LEGACY-03).

The legacy trio (`run_scan` → `discover_and_hash_files` → `bulk_upsert_files`) and `POST /api/v1/scan` form a **closed severable unit**: verified that no source code outside `routers/scan.py` and `services/ingestion.py` references them. The surviving scan path (`pipeline_scans.py` → `scan_directory` → `agent_files.py`) is fully independent and always supplies a real `agent_id` — so both the deletion and the default-removal are provably safe against the live pipeline.

**One locked decision (D-03) is technically infeasible as literally written** and must be adjusted at plan time (or bounced to discuss-phase): blindly reattributing the legacy `status='live'` sentinel scan_batch to the target fileserver will **violate the `uq_scan_batches_agent_id_live` partial unique index** because the target agent already owns its own live batch. See Pitfall 1 — this is the single most important finding.

**Primary recommendation:** New migration `038` (down_revision `037`), raw-SQL in the migration-012 style, single transaction, `context.get_x_argument(as_dictionary=True)` for the `reattribute_to` override, `NotImplementedError` downgrade. Handle the legacy live-batch by **DELETE-ing it** (it is a zero-value vestigial watcher sentinel), not reattributing it. Delete `ingestion.py` and `schemas/scan.py` wholesale (both fully orphaned after the trio/router go), keep the `LEGACY_AGENT_ID` constant (still test-referenced + labels historical data).

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Legacy scan trigger (delete) | API / Backend (`routers/scan.py`) | — | Orphaned HTTP entrypoint; no UI/consumer |
| Legacy ingestion (delete) | Backend service (`services/ingestion.py`) | — | Only caller is the deleted router |
| Historical row reattribution | Database / migration (`alembic/versions/038`) | — | Bulk `UPDATE` + `DELETE` under one txn |
| Model default removal | ORM models (`models/file.py`, `models/scan_batch.py`) | — | Pure Python-code change; no DDL |
| Surviving scan path (untouched) | API + agent worker (`pipeline_scans.py`, `tasks/scan.py`, `agent_files.py`) | Database | Already stamps real `agent_id` from auth |
| Test fixture reseed | Test harness (`tests/conftest.py`) | — | `create_all`-based; sentinel exists only via seed |

## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Migration auto-detects target = single non-revoked `kind='fileserver'` agent (`SELECT id FROM agents WHERE revoked_at IS NULL AND kind='fileserver'`). Exactly 1 → use it. 0 → abort. >1 → abort unless override.
- **D-02:** Override escape hatch `alembic upgrade head -x reattribute_to=<agent_id>`; validate id exists, `kind='fileserver'`, not revoked. Prod has one real fileserver (nox) → auto path resolves with no operator input.
- **D-03:** Reattribution keys on `agent_id` (FK → `agents.id`, string PK); writes the target's `id` not `name`. Scope = ALL legacy-owned `files` AND `scan_batches`, **including the `status='live'` sentinel batch** created by migration 012. ⚠️ *See Pitfall 1 — the literal "including the live batch → reattribute" is infeasible; must be DELETE, not reattribute.*
- **D-04:** Delete `routers/scan.py` wholesale (POST + GET). Unregister `include_router()` in `main.py`. Delete `tests/discovery/routers/test_scan.py`.
- **D-05:** Contingent gate: confirm GET status has no live consumer before deleting. → **VERIFIED: no consumer** (see Surviving-Path & Consumer Sweep).
- **D-06:** Drop Python `default="legacy-application-server"` from BOTH `models/file.py` and `models/scan_batch.py`. `agent_id` becomes required.
- **D-07:** NO DB-level `server_default` exists (migration 012 added columns nullable + backfilled). LEGACY-03 is a pure model-code change; migration needs NO `ALTER COLUMN ... DROP DEFAULT`. → **VERIFIED against migration 012 source.**
- **D-08:** Update `tests/conftest.py` to seed a real fileserver (`Agent(id='test-fileserver', name='test-fileserver', kind='fileserver', scan_roots=[])`, non-revoked) instead of the sentinel. Repoint the ~10 integration tests' `_LEGACY_AGENT_ID` constant.
- **D-09:** Single-transaction migration. Order: (1) `UPDATE files` + `UPDATE scan_batches`; (2) assert `COUNT(*)` remaining legacy-owned across both = 0 else `RAISE` (rollback); (3) `DELETE FROM agents WHERE id='legacy-application-server'`.
- **D-10:** `downgrade()` raises `NotImplementedError` (ownership unrecoverable once merged).

### Claude's Discretion
- Migration revision number (assigned at plan time). → **Recommend `038`** (branch head is `037`).
- Batching/lock strategy for the bulk `UPDATE` (~11,428 files). → See Pattern 2.
- Exact `NotImplementedError` message + abort-message text for 0 / >1 fileserver cases.

### Deferred Ideas (OUT OF SCOPE)
- Rollout/release sequencing (tag, homelab redeploy timing).
- Any future scan-batch status API for surviving `scan_directory` batches — build fresh on the pipeline surface if needed.
- `analysis-completed-at-backfill.md` and `wr-01-review-builder-limit-before-filter.md` (keyword collisions only; other phases).

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| LEGACY-01 | Delete `POST /api/v1/scan`, `run_scan`, `discover_and_hash_files` (+`bulk_upsert_files`) | Deletion Completeness Map below — closed severable unit, no dangling source refs |
| LEGACY-02 | Data-migration reattributes all legacy-owned files + scan_batches to a real fileserver, with backfill verification | Migration Mechanics + Migration Test Pattern below |
| LEGACY-03 | Drop `agent_id` `default=`, delete sentinel row (RESTRICT-FK-ordered) | Default-Removal Safety Proof + Pitfall 1 (live-batch ordering) |

## Project Constraints (from CLAUDE.md)

- **Python 3.14 exclusively**; **`uv` only** — never bare `pip`/`python`/`pytest`/`mypy`. Always `uv run ...`.
- **Ruff** line-length 150, `target-version = py313`; enabled sets include `ARG`, `B`, `F`, `I`, `PTH`, `S`, `SIM`, `UP`, `TCH`. Tests ignore `S101`, `PLC`, `S105`.
- **mypy strict** (`disallow_untyped_defs`, etc.), `exclude = ^(tests/|prototype/|services/)`.
- **90% coverage floor** (Codecov, precision 2, patch target 80%). Deleting source + its tests must not drop net coverage — see Coverage/CI section.
- **Migrations via Alembic** (async template). Pre-commit frozen SHAs, **never `--no-verify`**.
- **PR per phase** on a worktree branch; do not push to main.
- Migration bare-number convention: revision identifiers are plain numbers (`"038"`), enforced by a static test (see test_024/test_037).

## Deletion Completeness Map (LEGACY-01 / LEGACY-03)

Every non-test reference to the deleted symbols, verified by grep across `src/`:

| Symbol | Source references (non-test) | Action |
|--------|------------------------------|--------|
| `routers/scan.py` (whole file) | Imported only in `main.py:43` (import) + `main.py:193` (`app.include_router(scan.router)`) | DELETE file; remove both lines in `main.py` |
| `run_scan` | `routers/scan.py:18,71` only | DELETED with `ingestion.py` |
| `discover_and_hash_files` | `ingestion.py:47,174` (self) only | DELETED |
| `bulk_upsert_files` | `ingestion.py:94,181` (self, sole caller `run_scan`) only | DELETED |
| `schemas/scan.py` (`ScanRequest`/`ScanResponse`/`ScanStatusResponse`) | `routers/scan.py:16` only | **DELETE file** (fully orphaned after router deletion) |
| `LEGACY_AGENT_ID` (constant, `models/agent.py:14`) | `ingestion.py:20,79,160` only (all deleted). Comments in `main.py:147`, `admin_agents.py:85`, `enqueue_router.py:39,159`, `agent_bootstrap.py:4,86` reference the *string*, not the constant | **KEEP the constant** — still test-referenced + labels historical data; retire only if planner wants a follow-up. `agent_bootstrap.py` has ZERO code use (grep-confirmed) — comments only. |
| `settings.scan_path` (`config.py:223`) | Also used by `agent_bootstrap.py:113,128` | **DO NOT REMOVE** — surviving code depends on it |
| `default="legacy-application-server"` | `models/file.py:92`, `models/scan_batch.py:33` | DELETE the `default=` kwarg (D-06) |

### `ingestion.py` residue decision (planner micro-choice)

After deleting the trio, `ingestion.py` retains only `normalize_path` and `classify_file`. **Neither has any source caller** — `tasks/scan.py:72` has its OWN `_classify` (a comment says "Mirrors services.ingestion.classify_file" but does not import it). Both are referenced ONLY by `tests/discovery/services/test_ingestion.py`.

- **Recommended:** Delete `ingestion.py` wholesale + `tests/discovery/services/test_ingestion.py` wholesale. Removes dead code; cleanest end state. [ASSUMED impact: none — grep shows no other importer.]
- **Alternative:** Keep `normalize_path`/`classify_file` as tested utilities, trim only the trio. Leaves two dead-in-source functions (ruff will NOT flag module-level functions as unused). Not recommended.

CONTEXT locked only the trio + router, so this is discretion. Flagged in Assumptions Log (A1).

## Surviving-Path & Consumer Sweep (satisfies D-05)

**GET `/api/v1/scan/{batch_id}` has NO live consumer — VERIFIED.** Grep of `src/phaze/templates/**` and any `*.js` for `api/v1/scan` returns **zero matches**. All UI scan traffic targets `/pipeline/scans/*` (trigger, recent, progress-card, delete) and `/tracklists/scan`. The D-05 contingency is satisfied — delete both endpoints.

**Surviving write sites all stamp `agent_id` explicitly** (proves default-removal cannot regress the live path):

| Write site | agent_id source | Verified |
|------------|-----------------|----------|
| `routers/agent_files.py:110` | `data["agent_id"] = agent.id` (from auth dep, AUTH-01 — never body, always present) | ✓ |
| `routers/pipeline_scans.py:388` | `agent_id=form.agent_id` (validated `Agent` lookup at :350) | ✓ |
| `services/agent_bootstrap.py:126` | `sentinel_batch = ScanBatch(agent_id=_DEV_AGENT_ID, ...)` (explicit) | ✓ |
| `services/ingestion.py:158,79` | `LEGACY_AGENT_ID` (the DELETED sites) | n/a (deleted) |

`scan_directory` (`tasks/scan.py:169`) never writes `FileRecord`/`ScanBatch` directly — it POSTs chunks over HTTP to `agent_files.py`, which stamps the auth-derived `agent_id`. It reads `agent_id` from `ctx["agent_identity"].agent_id`. **No dependency on the Python model default anywhere in the surviving path.**

## Migration Mechanics (LEGACY-02 / LEGACY-03)

**Head revision on branch:** `037` (`037_add_stage_skip.py`, `down_revision="036"`). New migration = **`038`** (down_revision `"038"` → wait: `revision="038"`, `down_revision="037"`). Note: project memory says prod is at Alembic **031**, but the repo branch head is **037** — the new migration slots after 037. (032–037 are unreleased-to-prod but present in the repo; the migration chains off the on-disk head, not prod's applied revision.)

**Style:** Raw SQL via `op.get_bind().execute(sa.text(...), {...})` / `op.execute(...)`, NO model imports (mirrors migration 012's D-08/D-14 convention). This keeps the migration immune to future model drift.

**Reading the `-x` override** — `[CITED: alembic.sqlalchemy.org/en/latest/api/runtime.html]`:
```python
from alembic import context

# key=value form; does NOT require MigrationContext be configured
target_override = context.get_x_argument(as_dictionary=True).get("reattribute_to")
```
`get_x_argument(as_dictionary=True)` parses `-x reattribute_to=<id>` into `{"reattribute_to": "<id>"}`. Verified API. (The no-`=` form needs Alembic ≥1.13.1, but we always use `key=value`, so any modern Alembic is fine.)

**Auto-detect / validation logic (inside `upgrade()`):**
```python
bind = op.get_bind()
override = context.get_x_argument(as_dictionary=True).get("reattribute_to")
if override:
    row = bind.execute(sa.text(
        "SELECT id FROM agents WHERE id=:id AND kind='fileserver' AND revoked_at IS NULL"
    ), {"id": override}).first()
    if row is None:
        raise RuntimeError(f"reattribute_to={override!r} is not a valid non-revoked fileserver agent")
    target = override
else:
    rows = bind.execute(sa.text(
        "SELECT id FROM agents WHERE revoked_at IS NULL AND kind='fileserver'"
    )).all()
    if len(rows) == 0:
        raise RuntimeError("No non-revoked fileserver agent exists; cannot reattribute. Aborting.")
    if len(rows) > 1:
        raise RuntimeError(
            f"Multiple fileserver agents found ({[r[0] for r in rows]}); "
            "pass -x reattribute_to=<id> to choose one."
        )
    target = rows[0][0]
```
The legacy agent is **excluded automatically**: migration 012 seeds it with `revoked_at=NOW()` (VERIFIED at `012:...revoked_at, ... VALUES (..., NOW(), ...)`), and migration 024 backfills its `kind` to `'fileserver'`. So `revoked_at IS NULL` filters it out. In prod, nox (non-revoked fileserver) is the sole match → auto-resolves.

**Transaction scope:** Alembic runs `upgrade()` inside a transaction (default `transaction_per_migration` via `context.begin_transaction()` in `env.py`). Any `raise` rolls the whole thing back — the `DELETE` is never reached if the `COUNT=0` assertion fails. This is exactly D-09's contract; no extra `BEGIN` needed.

**Ordering (D-09) with the live-batch fix (see Pitfall 1):**
1. `DELETE FROM scan_batches WHERE agent_id='legacy-application-server' AND status='live'` (vestigial sentinel — collides otherwise).
2. `UPDATE files SET agent_id=:target WHERE agent_id='legacy-application-server'`.
3. `UPDATE scan_batches SET agent_id=:target WHERE agent_id='legacy-application-server'` (now no live rows remain to collide).
4. Assert `SELECT COUNT(*) FROM (files ∪ scan_batches) WHERE agent_id='legacy-application-server' = 0` else `RAISE`.
5. `DELETE FROM agents WHERE id='legacy-application-server'` (RESTRICT FK now satisfiable).

**Downgrade:** `raise NotImplementedError("...reattribution merged legacy-owned rows into the target agent; original ownership is unrecoverable...")` (D-10).

## Pitfall 1 (CRITICAL): The `status='live'` sentinel-batch unique-index collision

**What goes wrong:** D-03 says reattribute ALL legacy-owned `scan_batches` *including the `status='live'` sentinel batch* created by migration 012. But there is a **partial unique index** `uq_scan_batches_agent_id_live` — `UNIQUE (agent_id) WHERE status='live'` (VERIFIED at `012_add_agents_table_and_backfill.py:105-111`). The **target fileserver (nox) already owns its own `status='live'` sentinel batch** (every real fileserver gets one via `agent_bootstrap`/the watcher, and `agent_files.py:99` relies on a `scalar_one()` LIVE-batch lookup). Reattributing the legacy live batch to nox produces **two live rows for `agent_id='nox'` → `IntegrityError` on `uq_scan_batches_agent_id_live` → the migration aborts.**

**Why it happens:** The blanket-`UPDATE` reading of D-03 conflicts with the one-live-batch-per-agent invariant the same migration (012) established.

**How to avoid:** **DELETE the legacy `status='live'` sentinel batch instead of reattributing it.** It carries zero historical value (`scan_path='<watcher>'`, `total_files=0`, `processed_files=0`, `status='live'`). Do this as step 1 (before the bulk `UPDATE scan_batches`). This is collision-proof whether or not the target already has a live batch, and downgrade is already irreversible so no reversibility is lost.

**Warning signs:** A migration test that seeds BOTH a legacy live batch and a target-agent live batch, then runs 038, will surface the `IntegrityError` immediately if the blanket-UPDATE approach is used. Write that test.

**Planner action:** This modifies the literal wording of locked decision **D-03** ("reattribute ALL including the live batch"). Recommend the planner adopts DELETE-the-live-batch and records it as a plan-level refinement; if your process requires it, bounce this one micro-decision to discuss-phase. Intent of D-03 (no legacy-owned rows survive) is fully preserved.

## Pitfall 2: Tests that construct `FileRecord`/`ScanBatch` without `agent_id`

**What goes wrong:** After D-06 removes the Python `default=`, any test that does `FileRecord(...)` / `ScanBatch(...)` **without** an explicit `agent_id` will fail the NOT NULL + FK constraint at flush.

**How to avoid:** Grep the whole `tests/` tree for `FileRecord(` and `ScanBatch(` constructions and confirm each passes `agent_id`. Most already do (they pass `LEGACY_AGENT_ID` explicitly), but a comprehensive sweep is required before merge. The shared `tests/conftest.py` `async_engine` fixture (create_all) previously let the default cover this — that crutch is gone.

## Pitfall 3: Deleting the rescan-preserve-state regression guard

**What goes wrong:** `tests/discovery/test_rescan_preserves_state.py` (Phase 77 MIG-03 guard) imports `bulk_upsert_files` and must be deleted with the function. That test proves a rescan does NOT regress `state` to `DISCOVERED` via the ON-CONFLICT set. The **surviving twin** of that behavior lives in `agent_files.py:136` (same "never overwrite state on conflict" logic).

**How to avoid:** Before deleting the legacy guard, confirm `tests/agents/services/test_agent_upsert.py` (exists) asserts the agent_files ON-CONFLICT state-preservation. If it does not, port the assertion so the invariant remains covered.

## Test Blast Radius (LEGACY-01 / D-08)

### Category A — DELETE the whole test file (source gone)
| File | Reason |
|------|--------|
| `tests/discovery/routers/test_scan.py` | Tests the deleted router (D-04) |
| `tests/discovery/services/test_ingestion.py` | Tests trio + classify/normalize (if `ingestion.py` deleted wholesale — see residue decision) |
| `tests/discovery/test_rescan_preserves_state.py` | Imports `bulk_upsert_files` (L32) — see Pitfall 3 |

### Category B — Edit: remove `run_scan`-specific tests, keep the rest
| File | Lines | Edit |
|------|-------|------|
| `tests/shared/core/test_phase02_gaps.py` | 22, 70–229 | Delete all `run_scan` orchestration tests (happy/failure/empty). Check if any non-`run_scan` tests remain in the file; if none, delete the file |
| `tests/shared/core/test_no_auto_metadata_enqueue.py` | 8, 28, 103–143 | Remove the `run_scan` portion (the surviving `scan_directory`/agent path portion, if any, stays) |
| `tests/metadata/tasks/test_metadata_extraction.py` | 232–235 | Stale comment referencing removed `run_scan` test — update/remove comment only |

### Category C — Constant repoint (`_LEGACY_AGENT_ID` → new fileserver id) per D-08
These are `:5433` real-PG integration tests with their OWN `db_session` fixture that **self-add** an `Agent(id=_LEGACY_AGENT_ID, name="legacy")` as an arbitrary FK target. They are **independent of the conftest seed** and would technically still pass unchanged (the string is a valid agent id they insert themselves), but D-08 mandates repointing for cleanliness:

| File | Lines |
|------|-------|
| `tests/integration/test_stage_progress_buckets.py` | 75, 101–102 |
| `tests/integration/test_pending_set_divergence.py` | 70, 92–95 (has "re-add if missing" sibling-bucket guard — update stale comment) |
| `tests/integration/test_stage_status_equivalence.py` | 76, 104 |
| `tests/integration/test_dedup_divergence.py` | 77, 101 |
| `tests/integration/test_orphan_count.py` | 73, 94–95 |
| `tests/integration/test_dedup_resolve_undo_shadow.py` | 62, 84 |
| `tests/integration/test_fingerprint_progress.py` | 69, 94 |
| `tests/integration/test_files_page.py` | 57, 84 |
| `tests/integration/test_shadow_compare_skipped.py` | 73, 94 |
| `tests/integration/test_enrich_pending_independence.py` | 87, 130–133 (re-add guard — update stale comment) |
| `tests/integration/test_shadow_compare.py` | 77, 107, 327, 334–335 |

### Category D — conftest seed change (D-08), highest reach
`tests/conftest.py:17,202,212` — the shared `async_engine` fixture. Change:
```python
# was: setup_session.add(Agent(id=LEGACY_AGENT_ID, name=LEGACY_AGENT_ID, scan_roots=[]))
setup_session.add(Agent(id="test-fileserver", name="test-fileserver", kind="fileserver", scan_roots=[]))
```
**Subtlety:** the *current* seed omits `kind`, relying on the DB `server_default='fileserver'` (VERIFIED at `agent.py:28`). Make the new seed set `kind="fileserver"` **explicitly**. This fixture backs many buckets; any unit test that built a `FileRecord`/`ScanBatch` expecting `agent_id='legacy-application-server'` to exist as the FK target must switch to `'test-fileserver'` (overlaps Category C and Pitfall 2).

### Category E — DO NOT TOUCH (historical migration tests, pinned to pre-038 revisions)
| File | Why safe |
|------|----------|
| `tests/integration/test_migrations/test_012_upgrade.py` | Runs migrations to rev **012** only; asserts the sentinel is *created*. Never reaches 038. **Must stay pinned** to historical behavior. |
| `test_013…test_037` migration tests, and `test_016/017/024` | Each `upgrade_to(cfg, "0XX")` to a **specific revision ≤ 037** where the sentinel still exists; they seed files with `'legacy-application-server'` as a valid FK target at that revision. VERIFIED via test_037 (`upgrade_to(cfg,"036")`→`"037"`, not head) and test_024. **Safe — do not modify.** |

**Head-migration caution:** The `migrated_engine` fixture upgrades to **head** (which will include 038). Any test using `migrated_engine` that then inserts a row with `agent_id='legacy-application-server'` **without adding that agent** will hit an FK violation post-038 (the agent is deleted). Audit `migrated_engine` consumers; today they either self-add the agent or target the surviving fileserver. The new 038 test must also add its own target-fileserver seed rather than rely on the sentinel.

## Migration Test Pattern (follow test_024.py, but NON-reversible)

Existing pattern (VERIFIED in `tests/integration/test_migrations/test_024.py` + `conftest.py`):
- Static, DB-free assertions: `revision=="038"`, `down_revision=="037"`, `branch_labels is None`, and the `saq_jobs`-never-referenced grep guard.
- Integration body uses `_build_alembic_config`, `upgrade_to`, `downgrade_to`, `migrated_engine` from `tests/integration/test_migrations/conftest.py`. Drive `downgrade_to(cfg,"base")` → `upgrade_to(cfg,"037")`, seed state, then `upgrade_to(cfg,"038")`, assert.

**038-specific scenarios the test must cover (LEGACY-02 verification):**
1. **Reattribution moves rows:** seed legacy-owned `files` + non-live `scan_batches` + a real `kind='fileserver'` non-revoked agent; run 038; assert those rows now have `agent_id=<target>` and `COUNT(legacy-owned)=0`.
2. **Sentinel deleted:** assert `SELECT COUNT(*) FROM agents WHERE id='legacy-application-server' == 0` post-038.
3. **Live-batch collision (Pitfall 1):** seed BOTH a legacy `status='live'` batch AND a target-agent `status='live'` batch; run 038; assert no `IntegrityError` and the legacy live batch is gone (DELETE, not reattributed).
4. **Abort on zero fileserver:** revoke/remove the real fileserver so only the (revoked) legacy remains; assert `upgrade_to("038")` raises and the sentinel still exists (rollback proof).
5. **Abort on multiple fileservers (no override):** seed two non-revoked fileservers; assert raise with the "pass -x reattribute_to" message.
6. **`-x` override path:** with two fileservers, pass the override and assert it reattributes to the chosen id. **Testing nuance:** `command.upgrade` does not take `-x` directly; set it on the Config before the call, e.g. `cfg.cmd_opts = argparse.Namespace(x=["reattribute_to=<id>"])` (argparse Namespace with an `x` list). Verify `get_x_argument` reads it. [ASSUMED — no in-repo precedent; validate during planning.] (A2)
7. **NON-reversible downgrade:** unlike every prior migration test (which asserts "downgrade drops X"), assert `downgrade_to(cfg,"037")` raises `NotImplementedError`. **Do not copy the reversibility mirror from test_024.**
8. **Empty autogenerate diff:** since 038 has NO DDL, `alembic` autogenerate against the 038 head should be an empty diff for schema (optional, mirrors test_037's parity check).

**MIGRATIONS_TEST_DATABASE_URL footgun** (memory-confirmed + VERIFIED in conftest): the migration-test conftest defaults to `postgresql+asyncpg://phaze:phaze@localhost:5432/phaze_migrations_test` (port **5432**, DB `phaze_migrations_test`), whereas the ephemeral integration bucket runs on port **5433**. Run 038's migration test via `just integration-test` / `just test-db` and ensure `phaze_migrations_test` exists on 5432 (or export `MIGRATIONS_TEST_DATABASE_URL`). Isolated `uv run pytest tests/integration/test_migrations/...` will fail with connection errors if the 5432 DB is absent — this looks like a colima flake but is the port/DB footgun.

## Ordering & Phase 90 Relationship

- This phase **must NOT touch `files.state` or the `FileState` enum** — that is Phase 90 (gated on shadow-compare green + drained cloud-push lanes). VERIFIED against ROADMAP Phase 90 (§628–636).
- Deleting `discover_and_hash_files` + `run_scan` removes **two `FileState` *writer* sites** (`ingestion.py:86` sets `state=FileState.DISCOVERED`; the ON-CONFLICT set was already state-preserving). This shrinks Phase 90's writer-removal surface — the stated purpose of grouping 89 near 90.
- **DO NOT remove the `FileState` import from `models/file.py`** — `state` is still a live column (`file.py:86`) with many surviving readers/writers (`agent_analysis.py`, `agent_push.py`, `agent_metadata.py`, `dedup.py`, `pipeline.py`, etc., all grep-confirmed) and Phase 90 owns its removal.

## Runtime State Inventory

This phase's data migration IS the runtime-state remediation, but enumerate what runtime systems hold the sentinel string:

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | (a) `agents` row `id='legacy-application-server'` (revoked); (b) historical `files.agent_id='legacy-application-server'` (~portion of 11,428 corpus); (c) `scan_batches.agent_id='legacy-application-server'` incl. the migration-012 `status='live'` sentinel batch | **Data migration 038**: reattribute (a→delete after) + (b,c UPDATE) + delete the live batch. This IS LEGACY-02/03. |
| Live service config | None — no n8n/Datadog/Tailscale/Cloudflare config references the sentinel (grep of templates/JS clean; no monitoring poller hits `/api/v1/scan`). Verified. | None |
| OS-registered state | None — no Task Scheduler / pm2 / systemd unit references the sentinel or the deleted endpoint. The homelab compose runs the FastAPI app + agents; no cron/task names embed the string. | None (verify at ship time per Deferred rollout) |
| Secrets/env vars | None — `SCAN_PATH` env var is consumed by `settings.scan_path` which STAYS (used by `agent_bootstrap`). No secret keys reference the sentinel. | None |
| Build artifacts / installed packages | None — pure-Python source deletion; `uv sync` reinstall covers the removed module. No egg-info/compiled artifact carries the name. | `uv sync` after branch checkout (standard) |

**Canonical question answered:** After every repo file is updated, the only runtime state still holding `'legacy-application-server'` is the **Postgres data** (agents/files/scan_batches rows) — exactly what migration 038 remediates. Nothing else (config UIs, OS registrations, secrets, artifacts) references it. VERIFIED by grep sweep across `src/`, templates, `*.js`, and config.

## Code Examples

### Pattern 1: Auto-detect + override target selection (raw SQL, no model imports)
See "Migration Mechanics" above — `context.get_x_argument(as_dictionary=True).get("reattribute_to")` + the `revoked_at IS NULL AND kind='fileserver'` predicate. This mirrors `services/enqueue_router.py:select_active_agent(session, kind="fileserver")` (same predicate, ordered by name) but written as raw `sa.text`.

### Pattern 2: Bulk UPDATE strategy for ~11,428 rows
```python
# Single UPDATE per table is fine at this scale under one txn. The rows are being
# re-pointed to a new FK value (indexed: ix_scan_batches_agent_id; files covered by
# composite UQ uq_files_agent_id_original_path). No batching loop needed — 11k rows
# is a sub-second UPDATE. A lock_timeout guard is NOT required here (unlike Phase 90's
# ACCESS EXCLUSIVE DDL) because plain row UPDATEs take only ROW EXCLUSIVE locks.
op.execute(sa.text(
    "UPDATE files SET agent_id = :t WHERE agent_id = 'legacy-application-server'"
).bindparams(t=target))
```
[ASSUMED: sub-second at 11k rows — reasonable for an indexed FK re-point but not benchmarked in this session.] (A3)

### Pattern 3: The single-transaction assert-then-delete (D-09)
```python
remaining = bind.execute(sa.text(
    "SELECT (SELECT COUNT(*) FROM files WHERE agent_id='legacy-application-server') "
    "     + (SELECT COUNT(*) FROM scan_batches WHERE agent_id='legacy-application-server')"
)).scalar_one()
if remaining != 0:
    raise RuntimeError(f"Reattribution incomplete: {remaining} legacy-owned rows remain; aborting before sentinel DELETE")
op.execute(sa.text("DELETE FROM agents WHERE id='legacy-application-server'"))
```

## Common Pitfalls (summary)

1. **Live-batch unique-index collision** (Pitfall 1) — CRITICAL; DELETE the legacy live batch.
2. **Tests constructing rows without `agent_id`** (Pitfall 2) — sweep after default removal.
3. **Losing the rescan-preserve-state guard** (Pitfall 3) — confirm surviving twin coverage.
4. **`migrated_engine`/head consumers inserting the sentinel post-038** — audit; they must self-seed a target fileserver.
5. **MIGRATIONS_TEST_DATABASE_URL 5432 vs 5433** — run via `just integration-test`.
6. **Copying the reversible-downgrade mirror** from test_024 — 038 is deliberately irreversible; assert `NotImplementedError`.

## Coverage / CI

- Deleting source **and its tests together** is net-neutral-to-positive for the 90% floor: `routers/scan.py`, the `ingestion.py` trio, and `schemas/scan.py` are removed alongside `test_scan.py`, `test_ingestion.py`, and the `run_scan` tests in `test_phase02_gaps.py`. No orphaned uncovered source is introduced.
- The NEW migration `038` adds executable lines that MUST be covered by the migration test (scenarios 1–8). Ensure the abort branches (0 / >1 fileserver, COUNT≠0) are exercised so they don't sink patch coverage (patch target 80%).
- Run the full gate via `uv run pytest` + `pre-commit run --all-files`. Migration tests need the real PG (`just integration-test`). Never `--no-verify`.
- Orphaned test files to remove alongside source: `test_scan.py`, `test_ingestion.py`, `test_rescan_preserves_state.py`, and the `run_scan` block of `test_phase02_gaps.py` / `test_no_auto_metadata_enqueue.py` (Categories A/B).

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (`uv run pytest`) |
| Config file | `pyproject.toml` (`[tool.pytest]` / `tests/BUCKETS.md` parallel buckets) |
| Quick run command | `uv run pytest tests/integration/test_migrations/test_038_*.py -x` |
| Full suite command | `just integration-test` (ephemeral PG 5433 + Redis 6380) |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| LEGACY-01 | Router + trio deleted, no dangling import | unit/import + suite | `uv run pytest tests/ -k "not test_scan and not test_ingestion"` + import-lint | ❌ Wave 0 (delete tests) |
| LEGACY-01 | App boots without `scan` router | smoke | `uv run pytest tests/shared/routers/test_pipeline.py -x` (app fixture) | ✅ existing app fixture |
| LEGACY-02 | Reattribution + abort branches + `-x` override | integration (real PG) | `uv run pytest tests/integration/test_migrations/test_038_*.py` | ❌ Wave 0 |
| LEGACY-03 | Default removed; surviving writers still pass agent_id; sentinel deleted | integration | same 038 test + `uv run pytest tests/agents/services/test_agent_upsert.py` | partial (038 new) |

### Sampling Rate
- **Per task commit:** `uv run pytest <touched test module> -x`
- **Per wave merge:** `just integration-test` (migration + integration buckets)
- **Phase gate:** full suite green + `pre-commit run --all-files` before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/integration/test_migrations/test_038_*.py` — new migration test (scenarios 1–8), covers LEGACY-02/03
- [ ] Delete `tests/discovery/routers/test_scan.py`, `tests/discovery/services/test_ingestion.py`, `tests/discovery/test_rescan_preserves_state.py`
- [ ] Edit `tests/shared/core/test_phase02_gaps.py`, `test_no_auto_metadata_enqueue.py` (remove `run_scan` tests)
- [ ] Update `tests/conftest.py` seed (D-08) + repoint Category C constants
- [ ] Confirm `tests/agents/services/test_agent_upsert.py` covers the ON-CONFLICT state-preservation (Pitfall 3)

## Security Domain

Low security surface — this is deletion + an internal data migration.

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V4 Access Control | yes | Deleting `POST /api/v1/scan` removes an unauthenticated-ish legacy trigger; the surviving `agent_files` path enforces AUTH-01 (agent_id from bearer-token auth dep, never body) — unchanged |
| V5 Input Validation | yes | The `-x reattribute_to` value is validated against `agents` (exists + kind + not revoked) before use — no raw interpolation; use `bindparams`/parameterized `sa.text` (never f-string the id into SQL) |
| V6 Cryptography | no | — |

| Pattern | STRIDE | Mitigation |
|---------|--------|-----------|
| SQL injection via `-x` override | Tampering | Parameterized `sa.text(...).bindparams(...)`; validate against `agents` first |
| Accidental mass-reattribution to wrong agent | Tampering/Repudiation | COUNT=0 assert + single-txn rollback; auto-detect requires exactly one fileserver else aborts |

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Agentless ingestion via `POST /api/v1/scan` + `LEGACY_AGENT_ID` default | Distributed agents: `POST /pipeline/scans` → `scan_directory` → `agent_files` stamps real `agent_id` from auth | Phase 24–27 (v4.0) | Legacy path already dead in prod; this phase removes the corpse + the sentinel |

**Deprecated/outdated:** `routers/scan.py`, `services/ingestion.py` trio, `schemas/scan.py`, and the `default="legacy-application-server"` model kwargs — all removed by this phase.

## Package Legitimacy Audit

**N/A** — this phase installs NO new packages. It only deletes source and adds one Alembic migration using already-present dependencies (`alembic`, `sqlalchemy`). No `## Standard Stack` additions.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `normalize_path`/`classify_file` have no source callers → `ingestion.py` can be deleted wholesale | Deletion Completeness | LOW — grep-verified; if a dynamic import exists, keep the two functions |
| A2 | `cfg.cmd_opts = argparse.Namespace(x=["reattribute_to=..."])` makes `get_x_argument` read the override in tests | Migration Test Pattern | MED — no in-repo precedent; validate during planning; alternative is a direct `env.py`-level test harness |
| A3 | Single `UPDATE` over ~11,428 rows is sub-second, no batching/lock_timeout needed | Code Example Pattern 2 | LOW — indexed FK re-point; if slow, add a batched loop (still one txn) |
| A4 | The target fileserver (nox) has its own `status='live'` batch, guaranteeing the Pitfall-1 collision | Pitfall 1 | LOW — the DELETE-the-legacy-live-batch fix is collision-proof either way (safe even if nox has no live batch) |

## Open Questions

1. **D-03 live-batch handling (BLOCKING micro-decision).**
   - What we know: blanket reattribution of the legacy `status='live'` batch violates `uq_scan_batches_agent_id_live`.
   - What's unclear: whether the planner may refine D-03 directly or must bounce to discuss-phase.
   - Recommendation: refine to **DELETE the legacy live batch**; record as a plan-level clarification of D-03 (intent preserved: no legacy-owned rows survive).
2. **`ingestion.py` wholesale delete vs trio-only** (A1) — recommend wholesale delete; confirm at plan time.
3. **`LEGACY_AGENT_ID` constant retention** — recommend KEEP (test-referenced + historical label); the string still exists in DB history and migration-012 tests. Retiring it is a separate cleanup, not required by LEGACY-01..03.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| PostgreSQL (`phaze_migrations_test` on :5432) | 038 migration test | assumed via `just test-db` | 16+ | — (test skips if PG absent; migration body still unit-assertable statically) |
| Alembic | migration | ✓ (in `uv` lock) | ≥1.18.4 (supports `get_x_argument(as_dictionary)`) | — |
| Ephemeral PG :5433 / Redis :6380 | `just integration-test` | via colima | — | — |

**Missing dependencies with no fallback:** none (all in the existing stack).

## Sources

### Primary (HIGH confidence)
- Codebase reads (VERIFIED): `services/ingestion.py`, `routers/scan.py`, `main.py`, `models/{file,scan_batch,agent}.py`, `schemas/scan.py`, `services/agent_bootstrap.py`, `routers/{agent_files,pipeline_scans}.py`, `tasks/scan.py`, `alembic/versions/012_add_agents_table_and_backfill.py`, `tests/conftest.py`, `tests/integration/test_migrations/conftest.py`, `tests/integration/test_migrations/test_024.py`, `tests/integration/test_migrations/test_037_stage_skip.py`, `.planning/{REQUIREMENTS,ROADMAP}.md`
- Grep sweeps (VERIFIED): `LEGACY_AGENT_ID`/`legacy-application-server`, `run_scan`/`discover_and_hash_files`/`bulk_upsert_files`, `api/v1/scan` in templates/JS, `FileRecord(`/`ScanBatch(` write sites
- [CITED: https://alembic.sqlalchemy.org/en/latest/api/runtime.html] — `get_x_argument(as_dictionary=True)` API + `-x key=value` invocation

### Secondary (MEDIUM confidence)
- Project memory: prod at Alembic 031; MIGRATIONS_TEST_DATABASE_URL 5432 vs 5433 footgun; sentinel-retirement scoping

## Metadata

**Confidence breakdown:**
- Deletion completeness: HIGH — exhaustive grep of `src/` + templates/JS
- Default-removal safety: HIGH — all surviving write sites enumerated and confirmed to pass `agent_id`
- Migration mechanics: HIGH — `-x` API doc-verified; 012 style confirmed in-repo
- Live-batch collision (Pitfall 1): HIGH — partial unique index + per-agent live-batch invariant both source-verified
- Test blast radius: HIGH — every `_LEGACY_AGENT_ID` / trio reference enumerated with line numbers
- `-x` test harness (A2): MEDIUM — no in-repo precedent for programmatic `-x`

**Research date:** 2026-07-11
**Valid until:** 2026-08-10 (stable internal codebase; re-verify head revision if other phases land migrations first)
