---
phase: 80
slug: recovery-re-enqueue-cutover
status: draft
nyquist_compliant: false
wave_0_complete: false
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

*To be completed by the planner — one row per task in each `*-PLAN.md`.*

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 80-XX-YY | XX | N | READ-03 | — | — | unit/integration | `just test-bucket analyze` | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

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

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 60s
- [ ] Every guard test observed RED under its named mutation, then restored
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
