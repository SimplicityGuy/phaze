---
phase: 77
slug: additive-schema-rescan-wipe-fix-migration-032
status: verified
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-08
---

# Phase 77 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x + pytest-asyncio (`uv run pytest`) — already installed, no Wave 0 framework install |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) + `tests/buckets.json` per-bucket isolation |
| **Quick run command** | `uv run pytest tests/integration/test_migrations/test_migration_032_additive_schema.py -x` |
| **Full suite command** | `just integration-test` (spins ephemeral PG `:5433` + `phaze_migrations_test`) |
| **Estimated runtime** | ~30–90 seconds (migration integration test on seeded corpus) |

**DB env note:** migration/DB tests require `TEST_DATABASE_URL` / `PHAZE_QUEUE_URL` pointed at the `:5433` ephemeral DB (`conftest.py` defaults to `:5432`). `just integration-test` sets this.

---

## Sampling Rate

- **After every task commit:** Run the quick command for the touched test (`test_rescan_preserves_state.py` after the rescan task; `test_migration_032_additive_schema.py` after `032` lands).
- **After every plan wave:** Run `just test-bucket integration` (+ the rescan test's bucket) **in isolation** — per-bucket hermeticity is enforced by `tests/shared/test_partition_guard.py`.
- **Before `/gsd:verify-work`:** `just integration-test` green + `uv run ruff check .` + `uv run mypy .` + `pre-commit run --all-files`.
- **Max feedback latency:** ~90 seconds.

---

## Per-Task Verification Map

> Task IDs are assigned by the planner; rows below are keyed by requirement + the Wave-0 test file that proves it. The planner's tasks MUST map onto these.

| Task group | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|------------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| Rescan-wipe fix (both upsert sites) | 1 | MIG-03 | — | Rescan cannot regress an authenticated agent's file state; `agent_id` from auth dep, never body | unit/integration | `uv run pytest tests/discovery/test_rescan_preserves_state.py tests/agents/test_rescan_preserves_state.py` | ✅ | ✅ green |
| `032` upgrade: columns + `dedup_resolution` + CHECK-widen + partial indexes | 2 | MIG-01, PERF-01 | T-77-01 | Static-literal SQL only (no injection surface, S608) | integration | `uv run pytest tests/integration/test_migrations/test_migration_032_additive_schema.py` | ✅ | ✅ green |
| `032` backfill: analyze failed-marker (upsert), dedup, cloud awaiting/pushing/pushed | 2 | MIG-01 | T-77-01 | Backfill row counts == legacy `files.state` counts | integration | (same file — data asserts) | ✅ | ✅ green |
| `files.state` byte-unchanged + `saq_jobs` never referenced | 2 | MIG-01 | T-77-02 | Migration honors the SAQ-owned banner | integration + unit | (same file) + `test_migration_never_references_saq_jobs` | ✅ | ✅ green |
| Empty `--autogenerate` diff (ORM `__table_args__` mirror parity) | 2 | PERF-01 | — | Index predicates spelled `= ANY(ARRAY[...])`/`IS NOT NULL`, never bare `IN` | integration | automated `compare_metadata` assert in migration test (scoped to 032 objects) | ✅ | ✅ green |
| `032.downgrade()` reverses additive DDL | 2 | D-09 (min) | — | Best-effort DDL reversal (relaxed per CONTEXT D-09) | integration | (same file — downgrade body) | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [x] `tests/integration/test_migrations/test_migration_032_additive_schema.py` — covers MIG-01, PERF-01, D-09 (mirrors `test_migration_031_route_control.py`; seeds a corpus with rows in each legacy `files.state`, runs `upgrade 031→032`, asserts schema + backfill counts + `files.state` unchanged + `pg_indexes` shapes + a downgrade smoke assert). **3 tests green.**
- [x] `tests/discovery/test_rescan_preserves_state.py` + `tests/agents/test_rescan_preserves_state.py` — cover MIG-03 for BOTH upsert sites (`services/ingestion.py` `bulk_upsert_files` + `routers/agent_files.py`): advance a file to `ANALYZED` + `analysis` row, re-upsert same `(agent_id, original_path)`, assert `state='ANALYZED'` and the `analysis` row survives. **2 tests green.**
- [x] Empty-`--autogenerate`-diff assertion — **AUTOMATED** in the migration test via `alembic.autogenerate.compare_metadata` (run through `conn.run_sync`, `compare_type=True`), scoped to the 032 objects. Passed with `ix_fprint_success` present (`= ANY (ARRAY['success','completed'])` spelling round-trips) — the plan's drop-and-defer-to-Phase-82 contingency was **NOT triggered.** Load-bearing PERF-01 SC#2 satisfied.
- [x] Framework install: **none** — pytest/pytest-asyncio already present.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| ~~Empty autogenerate diff~~ (RESOLVED → automated) | PERF-01 | ~~No in-tree automated precedent~~ | **Superseded** — now automated in `test_migration_032_additive_schema.py` (`compare_metadata` scoped to 032 objects). The manual `alembic revision --autogenerate -m _probe` fallback is no longer needed. |
| `/pipeline/stats` poll latency at ~200K scale (PERF-02 context — informational only this phase) | — | Requires a production-scale corpus not available in CI | Deferred: PERF-02 measurement belongs to the reader phase; note here only that the indexes exist. |

*Note: the empty-diff check was AUTOMATED in Wave 0 as planned; the manual fallback row above is retained only as historical context.*

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (migration test, rescan test, empty-diff check)
- [x] No watch-mode flags
- [x] Feedback latency < 90s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-07-08

---

## Validation Audit 2026-07-08

| Metric | Count |
|--------|-------|
| Requirements audited | 4 (MIG-03, MIG-01, PERF-01, D-09) |
| Gaps found | 0 |
| Resolved | 0 (all Wave-0 tests already present + green) |
| Escalated | 0 |

**Result:** NYQUIST-COMPLIANT. All 6 per-task-map rows are ✅ green, verified by re-running the Wave-0 suite against the ephemeral test DB (`:5433` / `phaze_migrations_test`):

- `tests/discovery/test_rescan_preserves_state.py` + `tests/agents/test_rescan_preserves_state.py` → **2 passed** (MIG-03, both upsert sites).
- `tests/integration/test_migrations/test_migration_032_additive_schema.py` → **3 passed** (MIG-01 upgrade/backfill/`files.state`-unchanged/`saq_jobs` guard, PERF-01 empty-autogenerate-diff, D-09 downgrade).

No gap-fill (auditor spawn) was required — coverage was already complete at merge. The empty-autogenerate-diff was automated in Wave 0; the `ix_fprint_success` drop-and-defer contingency was not triggered.
