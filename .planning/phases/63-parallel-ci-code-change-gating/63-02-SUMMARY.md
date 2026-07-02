---
phase: 63-parallel-ci-code-change-gating
plan: 02
subsystem: testing
tags: [pytest, coverage, test-layout, ci, partition-guard]

# Dependency graph
requires:
  - phase: 63-01
    provides: "tests/buckets.json canonical 9-bucket list; just test-bucket/coverage-combine recipes; relative_files coverage config; pytest-xdist"
provides:
  - "Test suite physically partitioned into 9 bucket directories (tests/<bucket>/<layer>/) — structurally-exclusive, one file per bucket"
  - "tests/BUCKETS.md — explicit file->bucket mapping for all 213 test files + the pre-reorg baseline (2566 passed, 96.89%)"
  - "tests/shared/test_partition_guard.py — D-06 guard that fails CI if any collected test escapes a known bucket dir (globs both test_*.py and *_test.py)"
  - "tests/integration/test_migrations/ — migrations relocated under integration so the conftest auto-marker still fires"
affects: [63-03, 63-04, "CI matrix fan-out", "coverage combine"]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Domain-bucket test layout: tests/<bucket>/<layer>/<basename> keeps colliding basenames unique as distinct dotted modules under import-mode=prepend"
    - "Structural partition guard reading the bucket set from a single source of truth (buckets.json), globbing BOTH pytest python_files patterns"

key-files:
  created:
    - tests/BUCKETS.md
    - tests/shared/test_partition_guard.py
  modified:
    - "tests/** (205 test files git mv'd into 9 bucket dirs)"
    - tests/integration/test_migrations/conftest.py

key-decisions:
  - "Preserve one <layer> sub-level inside each bucket (services/routers/tasks/models/schemas/core/…) so the ~18 pre-existing basename collisions stay unique dotted modules — no rename-on-move, no import-mode change"
  - "Migrations move to tests/integration/test_migrations/ (not a top-level bucket) so BOTH 'integration' and 'test_migrations' survive in path_parts for the conftest auto-marker"
  - "Partition guard walks the filesystem globbing test_*.py AND *_test.py rather than only collected items, so a reintroduced *_test.py at root is caught structurally"

patterns-established:
  - "Bucket dir == coverage shard == matrix leg; the guard enforces exactly-one-bucket membership"

requirements-completed: [CI-01, CI-03]

# Metrics
duration: ~40min
completed: 2026-07-02
---

# Phase 63 Plan 02: Test Suite Bucket Partition Summary

**Behavior-preserving `git mv` of 205 test files into 9 domain buckets (2566 passed, unchanged), plus a partition guard that fails CI on any unbucketed test.**

## Performance

- **Duration:** ~40 min (excludes ~3× 7-min full-suite verification runs)
- **Completed:** 2026-07-02
- **Tasks:** 3
- **Files modified:** 265 (205 renamed test files + 40 new package `__init__.py` + BUCKETS.md + guard + path fixes; 3 old dirs emptied)

## Accomplishments
- Every collected test (both `test_*.py` and `*_test.py`) now lives under exactly one of the 9 buckets: discovery(19) metadata(3) fingerprint(6) analyze(29) identify(12) review(24) agents(38) integration(21) shared(62 incl. guard)
- Full suite green at the **exact pre-reorg baseline of 2566 passed** — no test lost, none double-counted (CI-03 combined-coverage prerequisite)
- Partition guard (D-06) reads `KNOWN_BUCKETS` from `tests/buckets.json` (single source of truth) and fails loud, enumerating offenders, if any test escapes a bucket
- Zero same-directory basename collisions despite ~18 pre-existing duplicate basenames, via `<layer>` sub-nesting

## Task Commits

1. **Task 1: Baseline + file->bucket mapping (BUCKETS.md)** - `0385975` (docs)
2. **Task 2: Execute reorg (git mv + __init__ + migrations imports)** - `9c618cd` (refactor)
3. **Task 3: Partition-guard test + meta-test** - `aa7f849` (test)

## Files Created/Modified
- `tests/BUCKETS.md` - 213-row file->bucket->destination table + pre-reorg baseline (2566 passed, 96.89% coverage)
- `tests/shared/test_partition_guard.py` - D-06 guard + source-of-truth check + non-vacuous meta-test
- `tests/<bucket>/<layer>/*.py` - 205 relocated test files (git mv, history preserved)
- `tests/integration/test_migrations/*` - migrations dir (+ conftest, __init__) moved wholesale; 13 `from tests.test_migrations.conftest` imports rewritten
- `tests/_queue_fakes_test.py` -> `tests/shared/core/queue_fakes_test.py` (renamed on move — it is a real 4-test file, not a helper)

## Decisions Made
- **Layer sub-nesting over rename-on-move** to resolve basename collisions: `tests/<bucket>/<layer>/<basename>` where `(layer, basename)` is globally unique, so `(bucket, layer, basename)` never collides — collision-free by construction, no file renames needed.
- **Migrations under `integration/`** rather than a standalone bucket, preserving both auto-marker path triggers.
- **Filesystem-walk guard** (not a `pytest_collection_modifyitems` hook) — matches the `test_dead_template_guard.py` skeleton and explicitly globs both `python_files` patterns.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] `__file__`-relative repo-root anchors broke from deeper nesting**
- **Found during:** Task 2 (full-suite verification: 124 failed, 20 errors on first run)
- **Issue:** ~24 test files (and the migrations conftest) compute repo-root/alembic/template paths via `Path(__file__).resolve().parents[N]` or `.parent.parent[.parent]` chains. Moving each file 1-2 levels deeper made every such anchor resolve to the wrong directory (e.g. `alembic.ini` -> `tests/alembic.ini`, `CommandError: No 'script_location'`).
- **Fix:** Recomputed each anchor to the new depth — all moved files now sit at `tests/<bucket>/<layer>/file.py`, so repo root is uniformly `parents[3]` / 4× `.parent`. Bumped `parents[1]->[3]` (8 shared/core files), `parents[2]->[3]` (deployment ×5, test_analysis, migrations ×9 incl. conftest), and extended the `.parent.parent[.parent]` chains (phase03/04 gaps, pipeline_dag_context, pipeline_scans, progress_partial). Module `.__file__` reads (unaffected by relocation) were left untouched.
- **Verification:** Targeted subset (migrations + deployment + phase gaps + debouncer) 138 passed, then full suite 2566 passed.
- **Committed in:** `9c618cd` (Task 2 commit)

**2. [Rule 3 - Blocking] Orphaned `agent_watcher/conftest.py` (fake_clock) not relocated**
- **Found during:** Task 2 (second verification run: 5 errors, `fixture 'fake_clock' not found`)
- **Issue:** The reorg loop moved `test_*.py` files only, leaving `tests/test_agent_watcher/conftest.py` (defining the `fake_clock` fixture 5 debouncer tests consume) stranded in the emptied old dir.
- **Fix:** `git mv` the conftest to `tests/discovery/agent_watcher/conftest.py`; removed the 14 emptied old layer dirs (each holding only an orphaned `__init__.py`).
- **Verification:** Full suite 2566 passed.
- **Committed in:** `9c618cd` (Task 2 commit)

---

**Total deviations:** 2 auto-fixed (both Rule 3 - blocking). **Impact:** Both were mechanical consequences of the move required to keep the reorg behavior-preserving. No scope creep; no logic changed.

## Issues Encountered
- The plan's expected baseline was ~90.38%; the actual measured combined coverage is **96.89%** (recorded in BUCKETS.md as the true acceptance target). The pass-count baseline (2566) is the load-bearing equality check and held exactly.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Buckets are physically in place and independently path-selectable — Plans 03/04 can now wire the `tests.yml` matrix (`just test-bucket <name>`) and the combine job over these dirs.
- Partition guard is green and will fail CI if a future test lands outside a bucket, protecting the CI-03 combined-coverage number Phase 64 raises its gate against.

## Self-Check: PASSED

- Files verified present: tests/BUCKETS.md, tests/shared/test_partition_guard.py, 63-02-SUMMARY.md, tests/integration/test_migrations/conftest.py
- Commits verified present: 0385975, 9c618cd, aa7f849

---
*Phase: 63-parallel-ci-code-change-gating*
*Completed: 2026-07-02*
