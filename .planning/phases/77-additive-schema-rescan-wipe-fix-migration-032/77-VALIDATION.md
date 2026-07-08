---
phase: 77
slug: additive-schema-rescan-wipe-fix-migration-032
status: draft
nyquist_compliant: false
wave_0_complete: false
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
| Rescan-wipe fix (both upsert sites) | 1 | MIG-03 | — | Rescan cannot regress an authenticated agent's file state; `agent_id` from auth dep, never body | unit/integration | `uv run pytest tests/<discovery|agents>/test_rescan_preserves_state.py -x` | ❌ W0 | ⬜ pending |
| `032` upgrade: columns + `dedup_resolution` + CHECK-widen + partial indexes | 2 | MIG-01, PERF-01 | T-77-01 | Static-literal SQL only (no injection surface, S608) | integration | `uv run pytest tests/integration/test_migrations/test_migration_032_additive_schema.py -x` | ❌ W0 | ⬜ pending |
| `032` backfill: analyze failed-marker (upsert), dedup, cloud awaiting/pushing/pushed | 2 | MIG-01 | T-77-01 | Backfill row counts == legacy `files.state` counts | integration | (same file — data asserts) | ❌ W0 | ⬜ pending |
| `files.state` byte-unchanged + `saq_jobs` never referenced | 2 | MIG-01 | T-77-02 | Migration honors the SAQ-owned banner | integration + unit | (same file) + `test_migration_never_references_saq_jobs` | ❌ W0 | ⬜ pending |
| Empty `--autogenerate` diff (ORM `__table_args__` mirror parity) | 2 | PERF-01 | — | Index predicates spelled `= ANY(ARRAY[...])`/`IS NOT NULL`, never bare `IN` | integration | new empty-diff assertion (see Manual/Wave 0) | ❌ W0 | ⬜ pending |
| `032.downgrade()` reverses additive DDL | 2 | D-09 (min) | — | Best-effort DDL reversal (relaxed per CONTEXT D-09) | integration | (same file — downgrade body) | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/integration/test_migrations/test_migration_032_additive_schema.py` — covers MIG-01, PERF-01, D-09 (mirror `test_migration_031_route_control.py`; seed a small corpus with rows in each legacy `files.state`, run `upgrade 031→032`, assert schema + backfill counts + `files.state` unchanged + `pg_indexes` shapes; a downgrade smoke assert).
- [ ] `tests/<discovery|agents>/test_rescan_preserves_state.py` — covers MIG-03 for BOTH upsert sites (`services/ingestion.py` `bulk_upsert_files` + `routers/agent_files.py`): advance a file to `ANALYZED` + `analysis` row, re-upsert same `(agent_id, original_path)`, assert `state='ANALYZED'` and the `analysis` row survives.
- [ ] Empty-`--autogenerate`-diff assertion — **new capability, no in-tree precedent.** Either a scripted `alembic revision --autogenerate --sql` diff check (asserts no `op.add_column`/`op.create_index`/`op.*`), or a documented manual step recorded in VERIFICATION. Load-bearing for PERF-01 SC#2.
- [ ] Framework install: **none** — pytest/pytest-asyncio already present.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Empty autogenerate diff (if not automated in Wave 0) | PERF-01 | No in-tree automated precedent; `alembic revision --autogenerate` may need a live DB at head | With `032` at head on the ephemeral DB: `uv run alembic revision --autogenerate -m _probe` → assert the generated file body contains only `pass` (no `op.*`); delete the probe file. Record the result in VERIFICATION. |
| `/pipeline/stats` poll latency at ~200K scale (PERF-02 context — informational only this phase) | — | Requires a production-scale corpus not available in CI | Deferred: PERF-02 measurement belongs to the reader phase; note here only that the indexes exist. |

*Note: the primary path is to AUTOMATE the empty-diff check in Wave 0; the manual row is the documented fallback.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references (migration test, rescan test, empty-diff check)
- [ ] No watch-mode flags
- [ ] Feedback latency < 90s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
