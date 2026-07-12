---
phase: 90
slug: destructive-migration-writer-removal
status: approved
nyquist_compliant: true
wave_0_complete: false
created: 2026-07-12
---

# Phase 90 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Source: `90-RESEARCH.md` §Validation Architecture. Task IDs are assigned by the planner; rows below are
> requirement-level until plans exist.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio (`uv run pytest`) |
| **Config file** | `pyproject.toml` + `tests/buckets.json` (per-bucket isolation; `tests/shared/test_partition_guard.py` enforces one bucket per file) |
| **Quick run command** | `uv run pytest tests/shared/services/test_pipeline.py -x` |
| **Migration run command** | `MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_migrations_test" just test-bucket integration` |
| **Full suite command** | `uv run pytest` |
| **Estimated runtime** | ~180 s full suite; ~5 s quick |

---

## Sampling Rate

- **After every task commit:** Run the quick run command (or the affected bucket).
- **After every plan wave:** Run `uv run pytest` (90% floor; re-run failed subset in isolation on colima flake — memory `reference_local_fullsuite_colima_flake`).
- **Before `/gsd:verify-work`:** Full suite must be green, including the `:5433` migration bucket.
- **Max feedback latency:** 180 seconds.

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| TBD (PR-A) | readers | 1 | MIG-04 / D-09 | — | Each converted reader returns correct rows from derived sources (counts, analyze workspace, proposal batches, backfill, search-facet removal) | unit/integration | `just test-bucket analyze` / `metadata` / `shared` | Partial — extend `test_pipeline.py` | ⬜ pending |
| TBD (PR-A) | readers | 1 | D-09 | — | `held_files` ledger-seed path stays correct after cutover (currently likely uncovered) | integration | `just test-bucket integration` | ❌ Wave 0 | ⬜ pending |
| TBD (PR-B) | writers | 2 | MIG-04 | — | No writer of `state` survives; SQL⇔Python equivalence still green | integration | `test_stage_status_equivalence.py` | ✅ exists | ⬜ pending |
| TBD (PR-C) | destructive | 3 | MIG-04 | T-90-guard | `039` drops column+index, deletes enum; guard aborts on shadow-compare violation / mid-flight rows; runs clean on empty DB; `files_state_archive` populated | integration (migration) | `just test-bucket integration` (`:5433` export) | ❌ Wave 0 — `test_migration_039_*.py` | ⬜ pending |
| TBD (PR-C) | destructive | 3 | MIG-04 / D-10 | — | `downgrade()` restores column+index verbatim from `files_state_archive`; derived fallback for post-039 rows | integration | same | ❌ Wave 0 | ⬜ pending |
| TBD (PR-C) | destructive | 3 | MIG-04 / D-08 | — | Anti-drift: `FileState` / `files.state` / `.state =` cannot reappear in `src/` (mutation-tested) | unit (source-grep) | `uv run pytest tests/shared/.../test_no_filestate_guard.py` | ❌ Wave 0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/integration/test_migrations/test_migration_039_*.py` — upgrade (guard: shadow-compare violation→raise, empty DB→pass, mid-flight rows→raise), `files_state_archive` populated, index+column gone, `FileState` deleted; downgrade restores durable states from archive. Model on `test_migration_038`.
- [ ] `tests/shared/.../test_no_filestate_guard.py` — mutation-tested source-grep (D-08); add a fake `.state=` line, watch RED, restore.
- [ ] Extend `tests/shared/services/test_pipeline.py` + analyze/metadata bucket tests for each reader cutover; **add coverage for the `held_files` ledger-seed path** (Pitfall 1 warning sign — likely uncovered today).
- [ ] Delete/repoint `get_files_by_state` tests once that helper is removed; delete search `file_state`-facet tests (D-11).
- [ ] Export `MIGRATIONS_TEST_DATABASE_URL` at `:5433` before the migration bucket (memory `reference_migrations_test_db_port`).

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Migration rehearsal against a restore of the real corpus passes (ROADMAP success-criterion 3) | MIG-04 | Requires a restore of live prod data (~11,428 rows @ Alembic 031); cannot run in CI | Restore prod snapshot → apply 032–038 → run shadow-compare green (drained) → run `039` → assert column/index gone + `files_state_archive` row count matches → `downgrade()` → assert durable states restored. Recipe in `90-RESEARCH.md` Pattern 5. |
| Dashboard cards + analyze workspace render correctly on live traffic after drain lifts (D-12 cloud cards, reader cutovers) | MIG-04 | Browser UAT — no test runs JS/templates (memory `project_htmx_hxon_alpine_scope_trap`) | Load pipeline dashboard post-deploy; confirm Staged(pushing)/Analyzing(cloud) counts, analyze workspace states, failed-count card, search (facet removed). |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies (confirmed by plan-checker Dimension 8, iteration 1)
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (039 migration test, anti-drift guard, held_files coverage)
- [x] No watch-mode flags
- [x] Feedback latency < 180s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-07-12
