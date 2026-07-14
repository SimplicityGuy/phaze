---
phase: 92-milestone-close-tech-debt-cleanup
plan: 04
subsystem: testing
tags: [pytest, sqlalchemy, create_savepoint, advisory-lock, hermeticity, integration-tests, asyncpg]

# Dependency graph
requires:
  - phase: 92-03
    provides: "the shared `verify` fixture + `create_savepoint` hermetic `session` fixture the verify-site migration binds to"
provides:
  - "21 independent-verify-session call sites (13 non-integration files) rebound to the per-test connection (17 by 92-03's first executor, reconciled here)"
  - "8 cross-connection concurrency cells relocated to tests/integration on a new committed_db real-engine fixture (Option B)"
  - "committed_db fixture: real port-5433 engine + committing sessionmaker + TRUNCATE-and-reseed cleanup for cross-connection concurrency tests"
affects: [92-05]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "committed_db: cross-connection concurrency tests live in tests/integration on a real engine (committed-visible independent connections), NOT the hermetic create_savepoint session"
    - "TRUNCATE-and-reseed cleanup: teardown TRUNCATEs ORM tables then re-seeds the session-scoped test-fileserver FK parent to preserve the async_engine invariant"

key-files:
  created:
    - tests/integration/test_staging_cron_concurrency.py
    - tests/integration/test_reconcile_concurrency.py
    - tests/integration/test_agent_push_concurrency.py
    - tests/integration/test_agent_s3_concurrency.py
  modified:
    - tests/integration/conftest.py
    - tests/analyze/core/test_staging_cron.py
    - tests/analyze/tasks/test_reconcile_cloud_jobs.py
    - tests/agents/routers/test_agent_push.py
    - tests/agents/routers/test_agent_s3.py

key-decisions:
  - "Option B: MOVE the 8 cross-connection concurrency cells to tests/integration rather than rebind them to the verify fixture — they need REAL independent committed-visible connections (advisory locks, row-locks, concurrent gather) which the single-connection create_savepoint session fundamentally cannot provide"
  - "committed_db re-seeds test-fileserver after its TRUNCATE so it never destroys the session-scoped FK parent that hermetic integration tests depend on"

patterns-established:
  - "Concurrency tests that assert real Postgres lock serialization belong in tests/integration on a committing real engine, never on the hermetic create_savepoint fixture"

requirements-completed: [CLEAN-02]

# Metrics
duration: ~110min
completed: 2026-07-14
---

# Phase 92 Plan 04: CLEAN-02 verify-site migration + Option-B concurrency move Summary

**Completed the CLEAN-02 verify-session migration (21 sites / 13 files) and relocated the 8 cross-connection concurrency cells the create_savepoint conversion broke to tests/integration on a new committed_db real-engine fixture — assertions preserved verbatim.**

## Performance

- **Duration:** ~110 min (continuation executor)
- **Started:** 2026-07-14T00:40Z (approx)
- **Completed:** 2026-07-14T02:32Z
- **Tasks:** Continuation of plan 04 — 17 verify sites already landed (commits 2b7fd2e8/be3d7687); this session implemented the Option-B decision for the 8 concurrency cells + a regression fix
- **Files modified:** 9 (4 created, 5 modified)

## Accomplishments

- Implemented the **Option B** decision: MOVED 8 cross-connection concurrency cells (from 4 donor files) into 4 new `tests/integration/` files, on a new `committed_db` fixture that gives each racing operation its OWN real pool connection so advisory-lock/row-lock serialization and concurrent `asyncio.gather` ticks work on genuinely committed-visible data. Every assertion preserved byte-for-byte.
- Added `committed_db` to `tests/integration/conftest.py` (real engine + committing sessionmaker + TRUNCATE-and-reseed cleanup; `*_test` DB guard).
- Cleaned the 4 donor files of the moved cells + now-orphaned helpers/imports/constants; each donor file passes hermetically with **zero** `async_sessionmaker(async_engine)` verify sites remaining.
- Caught and fixed a self-introduced regression: the `committed_db` TRUNCATE wiped the session-scoped `test-fileserver` FK parent, breaking later hermetic integration tests — fixed by re-seeding it at teardown.

## The 8 moved cells (Option B)

| Donor file | Cell(s) | New integration file |
|---|---|---|
| tests/analyze/core/test_staging_cron.py | `test_overlapping_ticks_never_exceed_window`, `test_k8s_overlapping_ticks_never_exceed_window`, `test_overlapping_ticks_never_overshoot_per_backend_cap` | tests/integration/test_staging_cron_concurrency.py |
| tests/analyze/tasks/test_reconcile_cloud_jobs.py | `test_delete_after_record_ordering`, `test_drain_reconcile_concurrency_delete_runs_under_advisory_lock` | tests/integration/test_reconcile_concurrency.py |
| tests/agents/routers/test_agent_push.py | `test_mismatch_concurrent_no_lost_update`, `test_mismatch_real_enqueue_hook_does_not_deadlock` | tests/integration/test_agent_push_concurrency.py |
| tests/agents/routers/test_agent_s3.py | `test_failed_concurrent_under_cap_no_lost_update` | tests/integration/test_agent_s3_concurrency.py |

## Verify-site migration (the 21-across-13, per plan objective)

- **17 sites** were migrated by the first executor in the two durable commits already on the branch (2b7fd2e8: 9 analyze files + `_make_ctx` controller-ctx factories; be3d7687: test_duplicates.py 5 sites + test_scan_reaper.py 1 site).
- The remaining **4 real `async_sessionmaker(async_engine)` code sites** were exactly the concurrency cells this plan moved (test_agent_push ×2, test_agent_s3 ×1, test_reconcile ×1) plus the 3 `asyncio.gather` staging-cron cells that sourced `phaze.database.async_session`. Rather than rebind, they were relocated (Option B).
- **Grep gate:** `grep -rn "async_sessionmaker(async_engine" tests/ | grep -v tests/integration/` returns only docstring/comment mentions (in ``backticks``); **zero** real verify-read code sites remain outside `tests/integration/`.

## Task Commits

1. **Move staging-cron two-tick overshoot cells + add committed_db fixture** - `fc8e0fea` (test)
2. **Move reconcile D-04 ordering + drain-lock cells** - `43915816` (test)
3. **Move /mismatch concurrent-RMW + no-deadlock cells** - `46df658a` (test)
4. **Move /upload-failed concurrent-RMW cell** - `ee85a34f` (test)
5. **Re-seed test-fileserver after committed_db TRUNCATE (regression fix)** - `46d554c4` (fix)

**Plan metadata:** this SUMMARY (docs commit to follow)

## Files Created/Modified

- `tests/integration/test_staging_cron_concurrency.py` - 3 WR-04/SCHED-02 overlapping-tick cap cells on committed_db
- `tests/integration/test_reconcile_concurrency.py` - D-04 committed-snapshot ordering + cross-connection advisory-lock probe
- `tests/integration/test_agent_push_concurrency.py` - concurrent /mismatch RMW + real before_enqueue no-deadlock
- `tests/integration/test_agent_s3_concurrency.py` - concurrent /upload-failed advisory-locked RMW
- `tests/integration/conftest.py` - added `committed_db` fixture (real engine + TRUNCATE-and-reseed cleanup)
- `tests/analyze/core/test_staging_cron.py` - removed 3 moved cells + unused `asyncio` import; breadcrumbs left
- `tests/analyze/tasks/test_reconcile_cloud_jobs.py` - removed 2 moved cells + unused `text` import + `_DRAIN_ADVISORY_LOCK_KEY`
- `tests/agents/routers/test_agent_push.py` - removed 2 moved cells + their gated/real-hook helper classes + orphaned imports
- `tests/agents/routers/test_agent_s3.py` - removed 1 moved cell + orphaned imports

## Decisions Made

- **Option B (move, not rebind):** the 8 cells assert REAL Postgres lock serialization / committed-snapshot visibility across independent connections. The 92-03 single-connection create_savepoint `session` cannot express that, so they belong in `tests/integration/` (auto-marked `integration`) on a real committing engine. This is the decision handed to this executor; implemented faithfully with assertions unchanged.
- **committed_db owns the FK-parent invariant carefully:** it TRUNCATEs for a clean global-count slate but re-seeds `test-fileserver` at teardown so it never destroys the session-scoped Agent that hermetic tests target.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] committed_db TRUNCATE destroyed the session-scoped test-fileserver FK parent**
- **Found during:** Option-B integration-bucket validation (running the full `tests/integration` bucket).
- **Issue:** The initial `committed_db` teardown did `TRUNCATE ... CASCADE` over all ORM tables, which also deleted the `test-fileserver` Agent that the session-scoped `async_engine` fixture seeds ONCE for the whole session. Later hermetic integration tests (`test_files_filter`, `test_review_audit`) then failed with FK violations (regression introduced by this plan's new fixture).
- **Fix:** Teardown now TRUNCATEs, then RE-SEEDs `test-fileserver`, restoring the invariant. Setup still TRUNCATEs for an accurate clean-slate committed-count baseline.
- **Files modified:** tests/integration/conftest.py
- **Verification:** Full `tests/integration` bucket returned to `3 failed, 171 passed, 74 errors` = the pre-existing baseline (`3 failed, 163 passed, 74 errors`) PLUS the 8 moved cells all green, ZERO new failures.
- **Committed in:** 46d554c4

---

**Total deviations:** 1 auto-fixed (1 bug — a regression from this plan's own new fixture, caught and fixed before completion).
**Impact on plan:** Necessary for correctness; no scope creep.

## Issues Encountered

**Pre-existing red surfaced (out of scope — logged to `deferred-items.md`):**

- **DI-92-04-01 — `tests/agents/cli/test_agents_add.py` leaks committed agents.** Running the combined source buckets shows 5 pre-existing `test_agent_bootstrap.py` failures caused by `test_agents_add.py` committing agent rows on its own engine without cleanup, contaminating the later bootstrap cells. **Proven pre-existing:** at base commit `be3d7687` the `tests/agents` bucket already fails these 5 identically (there it is 8 failures = 3 concurrency RED + the 5 bootstrap; after the 92-04 move it is 5 = just the bootstrap). NOT a verify-site and NOT one of the four donor files → outside plan 92-04 scope; close-out gate is 92-05 (full-suite D-08).
- **DI-92-04-02 — `tests/integration` carries pre-existing red under a combined run.** Baseline WITHOUT the 92-04 files: `3 failed, 163 passed, 74 errors` (`test_drain_double_dispatch` ×3 broke under 92-03's create_savepoint conversion, `test_lifespan_orphan_task` ×1, and `test_stage_status_equivalence` 74 full-suite-ordering errors that PASS in isolation, 59/59). After 92-04: `3 failed, 171 passed, 74 errors` — same pre-existing red + the 8 moved cells green. Close-out gate is 92-05.

## Acceptance Results

- **Grep gate:** no `async_sessionmaker(async_engine)` verify-read code site remains outside `tests/integration/` (only docstrings). PASS.
- **Source buckets** (`pytest tests/analyze tests/review tests/agents tests/discovery`): `1642 passed, 5 failed` — the 5 are the pre-existing `test_agent_bootstrap` contamination (DI-92-04-01); no new failures from the removals.
- **Integration bucket** (`pytest tests/integration`): `171 passed, 3 failed, 74 errors` — the 8 moved concurrency cells all GREEN; all remaining red is pre-existing (DI-92-04-02).
- **Isolated confirmation:** the 4 new integration files run 8/8 green; the 4 donor files run green on a fresh DB (86 passed).

## Next Phase Readiness

- CLEAN-02 verify-site migration is complete; the create_savepoint hermeticity contract is now compatible with the commit-then-independent-read pattern across the migrated buckets.
- 92-05 (full-suite D-08 gate) inherits the two logged deferred items (`test_agents_add.py` leak; `test_drain_double_dispatch` / `test_stage_status_equivalence` / `test_lifespan_orphan_task` integration red) — all pre-existing and independent of this plan's scope.

## Self-Check: PASSED

- All 4 created integration files exist on disk.
- All 5 task commits (fc8e0fea, 43915816, 46df658a, ee85a34f, 46d554c4) present in git history.
- `deferred-items.md` created with the two out-of-scope discoveries.

---
*Phase: 92-milestone-close-tech-debt-cleanup*
*Completed: 2026-07-14*
