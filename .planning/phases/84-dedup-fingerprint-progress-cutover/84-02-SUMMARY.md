---
phase: 84-dedup-fingerprint-progress-cutover
plan: 02
subsystem: database
tags: [sqlalchemy, dedup, stage_status, predicate, exists, derive-dont-store]

# Dependency graph
requires:
  - phase: 77-additive-schema-rescan-wipe-fix-migration-032
    provides: dedup_resolution marker table + DedupResolution model (marker-existence = resolved)
  - phase: 78-derivation-layer-eligibility-anti-drift-test-harness
    provides: services/stage_status.py single-source predicate module + SQL⇔Python equivalence drift-lock test
  - phase: 79-shadow-compare-gate-live-corpus
    provides: services/shadow_compare._dedup_exists (the private clause body reproduced here as the public one)
provides:
  - "dedup_resolved_clause() — the single-source, stage-less, file-level correlated-exists dedup predicate in services/stage_status.py"
  - "D-08 documentation of scan_deletion's dual-FK un-resolve behavior in models/dedup_resolution.py"
affects: [84-03-dedup-readers, 84-04-get_fingerprint_progress, phase-82-pending-sets]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "File-level (non-Stage) predicate lives in the single-source predicate module but stays out of the drift-locked Stage dispatch ladders"
    - "Correlated exists(select(...).where(fk == FileRecord.id)) — never outer-join-null / negated-membership"

key-files:
  created: []
  modified:
    - src/phaze/services/stage_status.py
    - src/phaze/models/dedup_resolution.py

key-decisions:
  - "D-13: dedup_resolved_clause() takes NO stage argument and is kept out of done/failed/inflight/domain_completed/stage_status_case so the Phase-78 equivalence test is untouched"
  - "Reproduced shadow_compare._dedup_exists verbatim as the new public clause rather than refactoring shadow_compare.py (out of scope; its private copy is harmless)"
  - "D-08: scan_deletion dual-FK un-resolve documented in the model docstring only; scan_deletion.py intentionally unchanged"

patterns-established:
  - "A file-level predicate belongs in stage_status.py (the single answer, Phase 78) but must not enter the Stage ladders — those raise on unknown stages and are drift-locked"

requirements-completed: [READ-04, SIDECAR-02]

# Metrics
duration: 12min
completed: 2026-07-09
---

# Phase 84 Plan 02: Shared dedup-resolved predicate Summary

**Added the single-source, stage-less `dedup_resolved_clause()` correlated-exists predicate to `services/stage_status.py` (D-13) — out of the drift-locked Stage ladders — and documented `scan_deletion`'s dual-FK un-resolve behavior in the model docstring (D-08), unblocking the two Wave-2 cutover plans to consume it in parallel.**

## Performance

- **Duration:** ~12 min
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- `dedup_resolved_clause() -> ColumnElement[bool]` in `services/stage_status.py`: a file-level (not per-`Stage`) correlated `exists(select(DedupResolution.id).where(DedupResolution.file_id == FileRecord.id))`, identical in body to `shadow_compare._dedup_exists`. Existence = resolved; `~dedup_resolved_clause()` = "not resolved".
- Predicate deliberately kept OUT of every `Stage` dispatch ladder (`done_clause` / `failed_clause` / `inflight_clause` / `domain_completed_clause` / `stage_status_case`), so the Phase-78 SQL⇔Python equivalence drift-lock is untouched (36 passed).
- `models/dedup_resolution.py` docstring now records the D-08 `scan_deletion` dual-FK behavior: deleting a keeper's scan batch un-resolves its duplicates (they reappear for re-review) — accepted, not a bug — with `scan_deletion.py` left unchanged.

## Task Commits

Each task was committed atomically:

1. **Task 1: Add dedup_resolved_clause() to stage_status.py (D-13)** — `0903f12c` (feat)
2. **Task 2: Document scan_deletion dual-FK behavior in the model (D-08)** — `aac04b2f` (docs)

## Files Created/Modified
- `src/phaze/services/stage_status.py` — added `DedupResolution` import + `dedup_resolved_clause()` file-level predicate (does not appear in any Stage ladder)
- `src/phaze/models/dedup_resolution.py` — added the D-08 scan_deletion dual-FK un-resolve docstring note (documentation only)

## Decisions Made
- Placed `dedup_resolved_clause()` immediately after the `_DONE_FP` constant and before `done_clause`, visually separated from the Stage dispatch ladder, with a docstring stating it is file-level, that `~` means "not resolved", and that it is deliberately excluded from the ladders.
- Left `shadow_compare.py`'s private `_dedup_exists` copy in place (refactoring it to import the new public clause is optional and out of scope).

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
- The integration equivalence test defaults its DB URL to port 5432, but `just test-db` provisions the ephemeral Postgres on 5433 (a known footgun). Exported `TEST_DATABASE_URL`/`MIGRATIONS_TEST_DATABASE_URL`/`PHAZE_QUEUE_URL` against 5433 and the suite ran clean (36 passed).

## Verification
- `dedup_resolved_clause()` imports and compiles to `EXISTS (SELECT dedup_resolution.id FROM dedup_resolution, files WHERE dedup_resolution.file_id = files.id)` — a correlated EXISTS over `dedup_resolution`, correlating on `file_id = files.id`.
- `dedup_resolved_clause` appears in NONE of the five `Stage` dispatch ladders (grep + ladder-scoped scan confirmed).
- Phase-78 equivalence test unchanged: `tests/integration/test_stage_status_equivalence.py` — 36 passed.
- Model docstring contains `scan_deletion` + `canonical`; `git status` shows `services/scan_deletion.py` unchanged.
- `uv run ruff check .` and `uv run mypy .` both exit 0 (206 source files).

## Next Phase Readiness
- Wave-2 plans can now import `dedup_resolved_clause` from `services/stage_status.py`: 84-03 dedup readers at module level (`~dedup_resolved_clause()`), 84-04 `get_fingerprint_progress` function-locally (agent-worker boundary, D-00e). No add/add conflict remains on `stage_status.py`.
- Phase 82's pending sets (READ-01) will need this same file-level predicate — flagged in 84-CONTEXT deferred ideas.

## Self-Check: PASSED

- FOUND: src/phaze/services/stage_status.py
- FOUND: src/phaze/models/dedup_resolution.py
- FOUND: .planning/phases/84-dedup-fingerprint-progress-cutover/84-02-SUMMARY.md
- FOUND commit: 0903f12c (Task 1)
- FOUND commit: aac04b2f (Task 2)

---
*Phase: 84-dedup-fingerprint-progress-cutover*
*Completed: 2026-07-09*
