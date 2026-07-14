---
phase: 80
slug: recovery-re-enqueue-cutover
status: verified
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-10
---

# Phase 80 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Derived from `80-RESEARCH.md` § Validation Architecture.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio (`uv run pytest` — never bare `pytest`) |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` · buckets in `tests/buckets.json` |
| **Quick run command** | `just test-bucket analyze` |
| **Full suite command** | `just integration-test` |
| **Estimated runtime** | ~60s quick · ~10min full |

**Mandatory env exports for integration/migration tests** (the 5433 footgun —
`tests/integration/test_migrations/conftest.py:37` defaults to 5432 but `just test-db`
provisions 5433, and `just test-bucket` does not export the URL):

```bash
just test-db   # provisions Postgres 5433 / Redis 6380
export TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test"
export MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_migrations_test"
export PHAZE_REDIS_URL="redis://localhost:6380/0"
just test-bucket integration
```

---

## Sampling Rate

- **After every task commit:** Run `just test-bucket analyze`
- **After every plan wave:** Run `just integration-test`
- **Before `/gsd:verify-work`:** Full suite green **AND** `036`-seeded `just shadow-compare` green
- **Max feedback latency:** 60 seconds

**Bucket-isolation constraint (CI):** every new test must pass via `just test-bucket <bucket>`
**in isolation**, not merely inside the full suite. The partition guard
(`tests/shared/test_partition_guard.py`) fails CI on any root-level test file.

---

## Per-Task Verification Map

*Completed post-execution (2026-07-10). One row per task; every row's named test was run green.*

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 80-01-1 | 01 | 1 | READ-03 / SC-4 | T-80-01, T-80-02 | Migration `036` backfills `analysis_completed_at` with the `failed_at IS NULL` NAND guard | migration | `just test-bucket integration` (5433 exports) | ✅ | ✅ green |
| 80-01-2 | 01 | 1 | SC-4 | T-80-01 | Idempotent backfill + empty autogenerate + no-op downgrade; NAND-guard mutation-proven | migration | `test_migration_036_backfill_analysis_completed_at.py` | ✅ | ✅ green |
| 80-01-3 | 01 | 1 | READ-03 (D-14) | T-80-03 | Prose-only destructive-migration de-numbering; docs-drift integrity preserved | docs-guard | `just docs-drift` | ✅ | ✅ green |
| 80-02-1 | 02 | 1 | READ-03 | T-80-04, T-80-06 | `awaiting_candidate_clause()` single-source builder composed from LOCKED builders | unit | `test_awaiting_candidate_clause.py` | ✅ | ✅ green |
| 80-02-2 | 02 | 1 | READ-03 | T-80-05 | Two `pipeline.py` call sites repointed; D-11 trap docstring | integration | `test_stage_status_equivalence.py` (36/36) | ✅ | ✅ green |
| 80-03-1 | 03 | 1 | READ-03 / SC-1 | T-80-07, T-80-08 | At-cap spill via `hold_awaiting_cloud` CAS; zero `FileRecord.state` write; `cloud_job.status='awaiting'` not `FAILED` | integration | `test_reconcile_cloud_jobs.py` | ✅ | ✅ green |
| 80-03-2 | 03 | 1 | SC-1 | T-80-09 | MKUE-04 clean-before-flip: `delete_staged_object` under lock before commit; attempts not incremented | integration | `test_reconcile_cloud_jobs.py` (spill ordering) | ✅ | ✅ green |
| 80-04-1 | 04 | 2 | READ-03 / SC-2, SC-3 | T-80-10, T-80-11, T-80-13 | State-free done-set derivation via LOCKED builders; `= ANY(array)` bind; import boundary | integration | `test_recovery.py` + `test_task_split.py` | ✅ | ✅ green |
| 80-04-2 | 04 | 2 | SC-2, SC-3, D-10, D-11 | T-80-12 | SC-2/SC-3/D-10 both-cells/D-11 regressions, each mutation-named | integration | `test_sc2_…`, `test_sc3_…`, `test_d10_cell_a/b_…` | ✅ | ✅ green |
| 80-05-1 | 05 | 3 | SC-1 | T-80-14 | Clean-absence AST guard over both cutover files; forms #1–#6 mutation-proven RED + GREEN false-positives | source-assertion | `test_reenqueue_reconcile_source_scan.py` (13/13) | ✅ | ✅ green |
| 80-05-2 | 05 | 3 | D-11 | T-80-15 | DERIV-04 SCOPE comment amended with the `~inflight_clause` rejected-option rationale | source-doc | `test_stage_status_equivalence.py` | ✅ | ✅ green |
| CR-01 | 04 | post | READ-03 (CLOUDROUTE-02) | T-80-04 | Held AWAITING_CLOUD file is compute-only, never analyzed locally on a fileserver; recovery predicate drops `~inflight_clause` | integration | `test_held_process_file_orphan_is_not_analyzed_locally_on_a_fileserver` / `…_routes_to_a_compute_agent` / `test_held_file_with_process_file_seed_is_in_the_held_set` | ✅ | ✅ green |
| CR-02 | 04 | post | D-10 | T-80-12 | D-10 gate coerces naive ledger `enqueued_at` to UTC-aware; no `TypeError` on the DB-read path | integration | `test_d10_gate_does_not_crash_on_db_read_ledger_row` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*
*CR-01/CR-02 rows are the post-execution code-review fixes (commit `5da4036e`); both RED-first mutation-proven.*

---

## Success-Criteria Validation Matrix

Every criterion names the **mutation that must turn its test RED**. A guard test that has
never been observed RED proves nothing (project rule: mutation-test your guard tests).

| SC | Behavior | Layer | Test file / command | Mutation that MUST turn it RED |
|----|----------|-------|---------------------|-------------------------------|
| **SC-1** | `recover_orphaned_work` + `reconcile_cloud_jobs` derive done/in-flight with zero `FileRecord.state` reads; at-cap spill writes the sidecar; MKUE-04 clean-before-flip preserved under the held advisory lock | source-assertion (AST) + integration | new AST guard in `tests/shared/` + `tests/analyze/tasks/test_reconcile_cloud_jobs.py` · `just test-bucket analyze` | (a) reintroduce a `FileRecord.state` read in `reenqueue.py` in each of forms #1–#6 → AST guard RED; (b) re-add `update(FileRecord)…values(state=…)` at `reconcile_cloud_jobs.py:212` → guard RED + spill test (`cloud_job.status == 'awaiting'`, not `FAILED`) RED; (c) move `delete_staged_object` after `session.commit()` → Pitfall-9 ordering test RED |
| **SC-2** | Scheduling-ledger recovery contract + "only previously-scheduled work recovers"; a never-scheduled `discovered` file (no ledger row) is NOT recovered | integration | `tests/analyze/tasks/test_recovery.py` (beside the `_DOMAIN_COMPLETED_STAGES` totality test) · `just test-bucket analyze` | Make recovery iterate the corpus instead of `get_ledger_rows` → test RED (re-creates the 2026-06-18 44.5K over-enqueue class) |
| **SC-3** | A failed **analyze** is never produced by any automatic recovery path — `FAILURE_IS_TERMINAL[analyze]` encoded at the recovery layer, not just the derivation layer | integration | `tests/analyze/tasks/test_recovery.py` · `just test-bucket analyze` | Drop the `failed_clause` disjunct from `domain_completed_clause(ANALYZE)`, or bypass it in `is_domain_completed` → test RED |
| **SC-4** | The Phase-79 shadow-compare gate stays green after the cutover | integration + migration | `036`-seeded corpus → `just shadow-compare`; `tests/integration/test_migrations/test_migration_036_*.py` · `just test-bucket integration` (with 5433 exports) | Skip/disable `036` → seeded `analyzed` rows keep `analysis_completed_at IS NULL` → `analyzed` HARD invariant RED. Also: re-add the `state = AWAITING_CLOUD` spill write → `awaiting_cloud` HARD invariant RED |

### D-10 — both metadata cells (WR-02)

`tests/analyze/tasks/test_recovery.py`, exercising `is_domain_completed` against
`SchedulingLedger.enqueued_at` vs `metadata.failed_at`:

- **Cell A — orphaned operator retry** (`enqueued_at > failed_at`): metadata is NOT
  domain-complete → the file MUST re-drive. *Mutation:* flip the comparison to `>=` / `<` → RED.
- **Cell B — callback-partial-failure** (`enqueued_at < failed_at`): metadata IS
  domain-complete → the file MUST stay terminal. *Mutation:* drop the `enqueued_at <= failed_at`
  gate (revert to bare `done ∨ failed`) → Cell A goes RED, proving the fix is non-vacuous.

Analyze has **no** D-10 cell because `retry_analysis_failed` clears `failed_at`
(`routers/pipeline.py:956`) while `retry_metadata_failed` deliberately leaves it set.
Assert that asymmetry so a future symmetric-retry change trips the test.

### D-11 — the `~inflight_clause` trap

Two layers:

1. `tests/analyze/tasks/test_recovery.py` — the reenqueue regression proving both metadata
   cells resolve correctly. Because **every** recovery candidate is a ledger row by
   construction, adding `~inflight_clause` to `domain_completed_clause` makes it return
   `False` for all of them (disabling the secondary over-enqueue net), so Cell B goes RED.
2. `tests/integration/test_stage_status_equivalence.py:415-427` — extend the SCOPE comment
   with the D-11 rejected-option rationale; mirror it in `domain_completed_clause`'s docstring.

*Mutation:* add `~inflight_clause` to `domain_completed_clause` → the DERIV-04 equivalence
test stays **green** (the trap is a silent no-op for the drain and the count card) but the
SC-2 / D-10 recovery regressions go RED. **The recovery-layer test is the real lock, not the
equivalence test.**

---

## Wave 0 Requirements

No test files need creating from scratch — `test_recovery.py`, `test_reenqueue.py`,
`test_reconcile_cloud_jobs.py`, and `test_stage_status_equivalence.py` all exist.

New additions:

- [ ] `tests/shared/test_reenqueue_reconcile_source_scan.py` — the AST "zero `FileRecord.state`
      reads" guard (model on `tests/shared/test_dedup_fingerprint_source_scan.py`, Phase 84;
      clean-absence, no allow-list needed)
- [ ] `tests/integration/test_migrations/test_migration_036_backfill_analysis_completed_at.py`
      — mirror `test_migration_034_backfill_cloud_awaiting.py`
- [ ] SC-2 / SC-3 / D-10 / D-11 cases appended to `tests/analyze/tasks/test_recovery.py`

*Framework and fixtures already installed — no Wave 0 infrastructure task required.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live-corpus shadow-compare after `036` | READ-03 / SC-4 | Requires the production corpus (1001 `analyzed` files with `analysis_completed_at` NULL); no CI fixture reproduces it | Deploy `036`, then run `just shadow-compare` against a read-only prod replica. Must exit 0. **Do not gate CI on a live-corpus run that predates `036`.** |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 60s
- [x] Every guard test observed RED under its named mutation, then restored *(SC-1 AST forms #1–#6 + reconcile spill by executor 80-05/80-03; SC-2/SC-3/D-10/D-11 by executor 80-04; CR-01 held-routing + CR-02 tz-gate mutation-proven by orchestrator at fix time)*
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-07-10 (plan-checker VERIFICATION PASSED) · verified 2026-07-10 (post-execution Nyquist audit — see trail below)

---

## Validation Audit 2026-07-10

State A audit of the completed phase. Every success criterion and requirement (READ-03) maps to a real, named test that was run green; no coverage gaps found; no gsd-nyquist-auditor gap-fill needed.

| Metric | Count |
|--------|-------|
| Requirements / SCs audited | READ-03 + SC-1..SC-4 + D-10 + D-11 |
| COVERED (automated, green) | all |
| PARTIAL | 0 |
| MISSING | 0 |
| Escalated to manual-only | 1 (live-corpus shadow-compare — pre-existing, prod-corpus-dependent) |

**Coverage map (SC → shipped test):**

- **SC-1** — `test_reenqueue_reconcile_source_scan.py` (13 tests, AST forms #1–#6 mutation-proven) + `test_reconcile_cloud_jobs.py` (spill status + MKUE-04 ordering).
- **SC-2** — `test_sc2_never_scheduled_discovered_file_with_no_ledger_row_is_not_recovered`.
- **SC-3** — `test_sc3_failed_analyze_with_surviving_ledger_row_is_terminal_never_reenqueued`.
- **SC-4** — `test_migration_036_backfill_analysis_completed_at.py` (4 tests, idempotent + downgrade + NAND mutation). Live shadow-compare stays Manual-Only (prod corpus).
- **D-10** — `test_d10_cell_a_…` / `test_d10_cell_b_…` / `test_d10_analyze_clears_failed_at_but_metadata_does_not` **+ CR-02 addition** `test_d10_gate_does_not_crash_on_db_read_ledger_row` (DB-round-trip, the gap the in-memory cells missed).
- **D-11** — recovery regression (Cell B goes RED under the trap) + `test_stage_status_equivalence.py:429` REJECTED-OPTION RATIONALE.
- **CR-01 (new)** — `test_held_process_file_orphan_is_not_analyzed_locally_on_a_fileserver` + `…_routes_to_a_compute_agent` + `test_held_file_with_process_file_seed_is_in_the_held_set` lock the CLOUDROUTE-02 held-routing behavior the review found broken.

**Note:** the last sign-off box (mutation-RED discipline) was the only item deferred to execution time in the plan-phase strategy; it is now satisfied — the executors mutation-proved their guards and the orchestrator additionally RED-first mutation-proved the CR-01 (2 tests) and CR-02 (1 test) fixes.
