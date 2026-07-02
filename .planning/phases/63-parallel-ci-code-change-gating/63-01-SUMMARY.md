---
phase: 63-parallel-ci-code-change-gating
plan: 01
subsystem: testing
tags: [pytest-xdist, coverage, coverage-combine, just, ci, buckets]

# Dependency graph
requires: []
provides:
  - "pytest-xdist>=3.8.0 in the dev group (intra-bucket -n auto for verified DB-free buckets)"
  - "relative_files = true coverage config so `coverage combine` unions per-bucket shards by relative path"
  - "`just test-bucket NAME XDIST=\"\"` recipe — runs one bucket to .coverage.<bucket> (serial DB-safe default)"
  - "`just coverage-combine` recipe — combine + xml + report --fail-under=85"
  - "tests/buckets.json — single source of truth for the 9 canonical bucket names"
affects: [63-02-reorg, 63-03-matrix, 63-04-change-gate, 64-coverage-uplift]

# Tech tracking
tech-stack:
  added: [pytest-xdist>=3.8.0]
  patterns:
    - "Per-bucket CI shard: COVERAGE_FILE=.coverage.<bucket> pytest tests/<bucket> --cov=phaze --cov-report="
    - "Local authoritative coverage combine (coverage combine + xml + report --fail-under=85) delegated to a just recipe"
    - "Single-source bucket list (tests/buckets.json) consumed by matrix + partition guard + recipe"

key-files:
  created:
    - tests/buckets.json
  modified:
    - pyproject.toml
    - uv.lock
    - justfile

key-decisions:
  - "pytest-xdist floor pinned >=3.8.0 (operator-approved package-legitimacy gate; pytest-dev official; 3.8.0 published 2025-07-01, ~12mo old, clears the 7-day exclude-newer cooldown)"
  - "relative_files added to [tool.coverage.run]; concurrency kept [greenlet, thread] with NO multiprocessing (research A2: xdist workers are execnet processes merged by pytest-cov, not multiprocessing children)"
  - "fail_under left at 85 (Phase 64 raises it, not this phase); coverage-combine mirrors it via --fail-under=85"
  - "test-bucket XDIST defaults to \"\" (serial, DB-safe per D-01 revised); DB-free buckets opt in with XDIST=\"-n auto\""
  - "scripts/update-project.sh needs no edit — its verify sweep calls just lint/typecheck/test, not the new declarative CI-shard recipes"

patterns-established:
  - "CI shard recipe: one bucket → one .coverage.<bucket> data file"
  - "Coverage gate enforced locally in coverage-combine (authoritative number before Codecov, single source for Phase 64 to raise)"

requirements-completed: [CI-01, CI-02, CI-03]

# Metrics
duration: ~8min
completed: 2026-07-02
---

# Phase 63 Plan 01: CI Sharding Foundation Summary

**pytest-xdist added, cross-shard coverage combine enabled (relative_files), `just test-bucket` + `just coverage-combine` recipes wired, and tests/buckets.json published as the single canonical 9-bucket source of truth.**

## Performance

- **Duration:** ~8 min
- **Completed:** 2026-07-02
- **Tasks:** 3 (1 checkpoint pre-approved, 2 auto)
- **Files modified:** 4 (3 modified + 1 created)

## Accomplishments
- Installed `pytest-xdist>=3.8.0` in the dev group, placed alphabetically between `pytest-cov` and `respx`; `uv sync` clean and `import xdist` succeeds.
- Enabled `relative_files = true` in `[tool.coverage.run]` so per-bucket `.coverage.<bucket>` shards union by relative path — the CI-03 combine plumbing — while keeping `concurrency = ["greenlet", "thread"]` (no multiprocessing) and `fail_under = 85` unchanged.
- Added two `[group('test')]` recipes: `test-bucket NAME XDIST=""` (per-bucket CI shard, serial DB-safe default) and `coverage-combine` (combine → xml → report --fail-under=85).
- Published `tests/buckets.json` with the 9 canonical bucket names in matrix order — the single source of truth Plan 03's `fromJSON` matrix and Plan 02's partition guard both consume.

## Task Commits

1. **Task 1: Package legitimacy gate — pytest-xdist** — pre-approved by operator (no code; gate cleared). Approved floor `>=3.8.0` (pytest-dev official package, `github.com/pytest-dev/pytest-xdist`, 71 releases; 3.8.0 uploaded 2025-07-01, ~12 months old, well outside the `exclude-newer = "7 days"` cooldown).
2. **Task 2: Add pytest-xdist + enable cross-shard coverage combine** — `78473e1` (chore)
3. **Task 3: Add test-bucket + coverage-combine recipes and buckets.json** — `4454990` (feat)

## Files Created/Modified
- `pyproject.toml` — added `pytest-xdist>=3.8.0` to `[dependency-groups] dev`; added `relative_files = true` to `[tool.coverage.run]`.
- `uv.lock` — locked pytest-xdist 3.8.0 + execnet 2.1.2.
- `justfile` — added `test-bucket` + `coverage-combine` recipes under `[group('test')]`.
- `tests/buckets.json` — canonical 9-name bucket list (discovery, metadata, fingerprint, analyze, identify, review, agents, integration, shared).

## Decisions Made
- **Floor `>=3.8.0`** — operator-approved at the blocking package-legitimacy checkpoint (Task 1); 3.8.0 clears the 7-day cooldown so no older-floor fallback was needed.
- **No `multiprocessing` in coverage concurrency** — kept `["greenlet", "thread"]` per research A2 and the threat register (T-63-01-02): adding multiprocessing would mis-merge shard data and corrupt the CI-03 baseline Phase 64 raises the gate on.
- **`fail_under` stays 85** — Phase 64 owns the raise; `coverage-combine` mirrors 85 via `--fail-under=85`.
- **`scripts/update-project.sh` unchanged** — confirmed its verify sweep (lines 1046-1060) delegates to `just lint`/`typecheck`/`test`, not the new CI-shard recipes; the declarative bucket/combine recipes are exercised by CI (Plan 03), not the local update sweep.

## Deviations from Plan
None - plan executed exactly as written. Task 1's blocking checkpoint was pre-resolved by the operator before this executor ran (floor `>=3.8.0` approved from verified PyPI facts); no re-prompt was needed.

## Issues Encountered
None. `uv add` resolved without hitting the cooldown; all acceptance-criteria greps and the buckets.json parse passed on first run.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Wave-0 foundation is in place: xdist dep, `relative_files` combine plumbing, the two delegated recipes, and the canonical bucket list.
- Plan 02 (directory reorg) can now create `tests/<bucket>/` dirs the `test-bucket` recipe points at; Plan 03 (matrix) can consume `tests/buckets.json` via `fromJSON` and call `just test-bucket` / `just coverage-combine`.
- Note for Plan 02/03: the bucket directories do not exist yet — `just test-bucket <name>` is declarative until the reorg lands (expected).

## Self-Check: PASSED

- FOUND: tests/buckets.json
- FOUND: .planning/phases/63-parallel-ci-code-change-gating/63-01-SUMMARY.md
- FOUND commit: 78473e1 (Task 2)
- FOUND commit: 4454990 (Task 3)

---
*Phase: 63-parallel-ci-code-change-gating*
*Completed: 2026-07-02*
