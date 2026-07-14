---
phase: 92-milestone-close-tech-debt-cleanup
plan: 05
subsystem: testing
tags: [pytest, hermeticity, create_savepoint, committed_db, sqlalchemy, asyncpg, ci-buckets, traceability]

# Dependency graph
requires:
  - phase: 92-01
    provides: CLEAN-03 doc-hygiene comment edits (verified independently in wave 1)
  - phase: 92-02
    provides: CLEAN-01 get_stage_progress asyncio.gather parallelization + PERF-02 re-measure
  - phase: 92-04
    provides: CLEAN-02 verify-site migration + committed_db fixture + Option-B concurrency move
provides:
  - CLEAN-01/02/03 registered in .planning/REQUIREMENTS.md (checkboxes + Phase-92 Traceability rows)
  - All 9 CI buckets green under per-bucket isolation (CLEAN-02 D-08 acceptance gate satisfied)
  - Order-independent hermetic FK-parent seeding across the suite (no blind test-fileserver INSERT)
affects: [complete-milestone, milestone-2026.7.5-close, future-phase-test-fixtures]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Idempotent get-or-insert FK-parent seed: session.get(Agent, id) is None -> add, else reuse — makes every hermetic db_session fixture order-independent against a committed test-fileserver"
    - "committed_db migration: cross-connection integration tests seed via a COMMITTING session so production code reading through its own pool connections sees the rows (vs the invisible single-connection create_savepoint session)"
    - "Self-cleaning committed-row fixture: own NullPool engine deletes leaked rows in teardown, preserving the session-scoped FK parent"

key-files:
  created:
    - .planning/phases/92-milestone-close-tech-debt-cleanup/92-05-SUMMARY.md
  modified:
    - .planning/REQUIREMENTS.md
    - .planning/phases/92-milestone-close-tech-debt-cleanup/92-VERIFICATION.md
    - tests/conftest.py
    - tests/agents/cli/test_agents_add.py
    - tests/integration/test_drain_double_dispatch.py
    - tests/integration/test_lifespan_orphan_task.py
    - tests/integration/test_stage_status_equivalence.py
    - tests/integration/test_files_page.py
    - tests/integration/test_dedup_resolve_undo_shadow.py
    - tests/integration/test_fingerprint_progress.py

key-decisions:
  - "Fixed the D-08 gate at the ROOT (idempotent FK-parent seed everywhere a blind test-fileserver INSERT existed) rather than pinning collection order — the suite is now hermetic BY CONSTRUCTION, order-independent"
  - "Migrated the 3 drain double-dispatch cells to committed_db (not a verify-fixture rebind) because stage_cloud_window reads through its own pool connections — the create_savepoint single-connection session is fundamentally invisible to them"
  - "Rebound phaze.main.engine/async_session (not just settings) in the lifespan test because the module engine binds to settings.database_url AT IMPORT — TEST_DATABASE_URL never steered it"

patterns-established:
  - "Idempotent FK-parent seed (get-or-insert) is the canonical hermetic pattern for any integration db_session fixture sharing the port-5433 test DB with committed_db / the session-scoped async_engine"

requirements-completed: [CLEAN-01, CLEAN-02, CLEAN-03]

# Metrics
duration: 52min
completed: 2026-07-13
---

# Phase 92 Plan 05: CLEAN-02 D-08 Acceptance Gate + Milestone-Close Bookkeeping Summary

**All 9 CI buckets pass green under per-bucket isolation (D-08) after fixing four latent hermeticity defects the 92-03 session-scoped-engine conversion exposed, plus CLEAN-01/02/03 registered in REQUIREMENTS.md traceability.**

## Performance

- **Duration:** ~52 min
- **Started:** 2026-07-13T~02:40Z
- **Completed:** 2026-07-13T~03:32Z
- **Tasks:** 2 (+ the expected files_modified expansion the plan scope pre-authorized)
- **Files modified:** 10

## Accomplishments
- **D-08 gate SATISFIED:** every bucket in `tests/buckets.json` passes cold in its own process — discovery 172, metadata 93, fingerprint 84, analyze 571, identify 242, review 444, agents 460, integration 248, shared 1084. Recorded per-bucket in `92-VERIFICATION.md`.
- **Four latent hermeticity defects fixed** (DI-92-04-01 / DI-92-04-02 close-out): the `tests/agents` committed-row leak (5 failures), the `test_drain_double_dispatch` invisible-seed (3 failures), the `test_lifespan_orphan_task` unreachable-engine (`socket.gaierror`), and the `test_stage_status_equivalence` 74-error blind-FK-seed ordering cascade.
- **CLEAN-01/02/03 registered** in `.planning/REQUIREMENTS.md` (checkboxes + `| CLEAN-0X | Phase 92 | Pending |` Traceability rows) — closes the DOCS-01 guard blind spot; guard green (10 passed).
- **Suite made order-independent** by making every blind `test-fileserver` INSERT idempotent (get-or-insert), matching the guard three sibling fixtures already carried.

## Task Commits

Each task committed atomically:

1. **Task A (agents hermeticity — DI-92-04-01)** - `5d418d5f` (fix)
2. **Task B (integration hermeticity — DI-92-04-02)** - `b8b8c083` (fix)
3. **Task 1 (register CLEAN-01/02/03)** - `7d2fc40b` (docs)
4. **Task 2 (D-08 per-bucket gate results)** - `0075a387` (docs)

## Files Created/Modified
- `.planning/REQUIREMENTS.md` - CLEAN-01/02/03 subsection + 3 Phase-92 Traceability rows (Pending) + Coverage note
- `.planning/phases/92-.../92-VERIFICATION.md` - appended the CLEAN-02 D-08 per-bucket gate section (perf section preserved)
- `tests/conftest.py` - session-scoped `async_engine` FK-parent seed made idempotent (get-or-insert)
- `tests/agents/cli/test_agents_add.py` - `_cleanup_committed_agents` fixture (own NullPool engine) wired to the 4 committing `test_main_*` cells
- `tests/integration/test_drain_double_dispatch.py` - 3 cells migrated `session`/`async_engine` → `committed_db` + a `_seed_fk_fileserver` helper
- `tests/integration/test_lifespan_orphan_task.py` - rebind `phaze.main.engine`/`async_session` to the reachable test DB
- `tests/integration/test_stage_status_equivalence.py` - idempotent FK-parent seed
- `tests/integration/test_files_page.py` - idempotent FK-parent seed
- `tests/integration/test_dedup_resolve_undo_shadow.py` - idempotent FK-parent seed
- `tests/integration/test_fingerprint_progress.py` - idempotent FK-parent seed

## Decisions Made
- **Root-cause over order-pinning:** the D-08 gate did not pass first-run; the session-scoped-engine conversion (no per-test `drop_all`) plus `committed_db`'s by-design committed `test-fileserver` re-seed exposed blind-INSERT collisions whose manifestation depended on collection order. Fixed by making every FK-parent seed idempotent so the suite is hermetic by construction, not by ordering luck.
- **committed_db, not a verify rebind, for the drain cells:** `stage_cloud_window` opens its own pool connections; only a committed seed is visible to them. This mirrors the 92-04 Option-B concurrency-cell precedent exactly.
- **Rebind the module engine in the lifespan test:** the `postgres:5432` docker default is baked at import; steering `TEST_DATABASE_URL` alone could never reach it.

## Deviations from Plan

The plan's `files_modified` listed only `REQUIREMENTS.md` + `92-VERIFICATION.md`, but its acceptance criterion (D-08: every bucket green) REQUIRED fixing the remaining non-hermetic failures. `deferred-items.md` explicitly names 92-05 as the close-out owner, and the plan `<scope>` pre-authorized this expansion. The additional test-file edits below are that pre-authorized work, not scope creep.

### Auto-fixed Issues

**1. [Rule 1 - Bug] `tests/agents` committed-agent leak (DI-92-04-01)**
- **Found during:** Task B (agents bucket, baseline 5 failed / 455 passed)
- **Issue:** `test_agents_add.py`'s `test_main_*` cells COMMIT agent rows via a real `create_async_engine` CLI path; under the session-scoped engine they survived into `test_agent_bootstrap.py`, making `ensure_dev_agent` see a non-empty table.
- **Fix:** `_cleanup_committed_agents` fixture (own NullPool engine) deletes every agent except the `test-fileserver` FK parent in teardown; requested by the 4 committing cells.
- **Files modified:** `tests/agents/cli/test_agents_add.py`
- **Verification:** `just test-bucket agents` → 460 passed.
- **Committed in:** `5d418d5f`

**2. [Rule 1 - Bug] `test_drain_double_dispatch` invisible seed (DI-92-04-02)**
- **Found during:** Task B (integration bucket, 3 failed)
- **Issue:** Cells seeded via the single-connection `create_savepoint` `session`, but `stage_cloud_window` reads through its own pool connections → zero candidates → RED.
- **Fix:** Migrated the 3 cells to the `committed_db` fixture (committed, cross-connection-visible seed) + a `_seed_fk_fileserver` helper.
- **Files modified:** `tests/integration/test_drain_double_dispatch.py`
- **Verification:** the 3 cells pass in isolation and in the full bucket.
- **Committed in:** `b8b8c083`

**3. [Rule 1 - Bug] `test_lifespan_orphan_task` unreachable engine (DI-92-04-02)**
- **Found during:** Task B (integration bucket)
- **Issue:** the module-level `engine` binds to the docker `postgres:5432` default at import → `socket.gaierror` at the lifespan `SELECT 1`; `TEST_DATABASE_URL` never steered it.
- **Fix:** rebind `phaze.main.engine`/`async_session` to the reachable test DB in the test.
- **Files modified:** `tests/integration/test_lifespan_orphan_task.py`
- **Verification:** test passes in isolation and in the full bucket.
- **Committed in:** `b8b8c083`

**4. [Rule 1 - Bug] Blind FK-parent seed ordering cascade (DI-92-04-02)**
- **Found during:** Task B (integration bucket, 74 errors in `test_stage_status_equivalence` + a follow-on 31-error cascade after the drain migration shifted ordering)
- **Issue:** the session-scoped `async_engine` seed and several `db_session` fixtures blind-INSERT `test-fileserver`, which collides on `pk_agents` once `committed_db` re-seeds a committed parent before the seeder runs.
- **Fix:** made every such seed idempotent (get-or-insert) — `tests/conftest.py` `async_engine`, plus the `test_stage_status_equivalence` / `test_files_page` / `test_dedup_resolve_undo_shadow` / `test_fingerprint_progress` fixtures (matching the guard 3 sibling fixtures already carried).
- **Files modified:** `tests/conftest.py`, `tests/integration/test_stage_status_equivalence.py`, `tests/integration/test_files_page.py`, `tests/integration/test_dedup_resolve_undo_shadow.py`, `tests/integration/test_fingerprint_progress.py`
- **Verification:** `just test-bucket integration` → 248 passed; full D-08 sweep green.
- **Committed in:** `b8b8c083`

---

**Total deviations:** 4 auto-fixed (all Rule 1 — pre-existing hermeticity bugs the plan scope pre-authorized this plan to close).
**Impact on plan:** All fixes were required to satisfy the D-08 acceptance criterion. No production code changed — every edit is test-fixture hermeticity or planning-doc bookkeeping. No scope creep beyond the pre-authorized close-out.

## Issues Encountered
- The drain→`committed_db` migration initially perturbed collection order and surfaced a fresh 31-error cascade in previously-green files (`test_files_page`, `test_dedup_resolve_undo_shadow`, `test_fingerprint_progress`, and the hermetic `test_files_filter`/`test_review_audit` via the shared `async_engine`). Root cause was the same latent blind-INSERT fragility; the idempotent-seed fix resolved all of it and made the suite order-independent.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- CLEAN-02's D-08 blast-radius verification is complete and green — the whole suite is hermetic under per-bucket CI isolation.
- Milestone-close bookkeeping done: CLEAN-01/02/03 have real traceability rows (DOCS-01 no longer blind).
- Phase 92 is ready for verification; on `status: passed` the CLEAN rows flip `[ ]`→`[x]` / `Pending`→`Complete` (downstream). The 2026.7.5 milestone can then close.

---
*Phase: 92-milestone-close-tech-debt-cleanup*
*Completed: 2026-07-13*

## Self-Check: PASSED

- Created/modified files verified present on disk (SUMMARY, REQUIREMENTS.md, 92-VERIFICATION.md).
- All 4 task commits verified in git log (`5d418d5f`, `b8b8c083`, `7d2fc40b`, `0075a387`).
- Content assertions: CLEAN-01 checkbox present, `| CLEAN-02 | Phase 92 |` row present, "CLEAN-02 D-08 per-bucket gate" section present.
