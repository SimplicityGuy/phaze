---
phase: 79-shadow-compare-gate-live-corpus
plan: 01
subsystem: testing
tags: [shadow-compare, invariants, sqlalchemy, anti-join, stage-status, migration-gate, filestate]

# Dependency graph
requires:
  - phase: 78-single-source-predicate-layer
    provides: done_clause / failed_clause ColumnElement builders reused as the derived side (D-03)
  - phase: 77-additive-schema-migration
    provides: cloud_job 'awaiting' status + dedup_resolution sidecar the raw-column invariants assert against
provides:
  - "src/phaze/services/shadow_compare.py — the ONE shared state↔derived assertion core (INVARIANTS registry + Invariant/InvariantResult/Report dataclasses + run_shadow_compare)"
  - "tests/integration/test_shadow_compare.py — hermetic fixture-corpus CI gate in the integration bucket, non-vacuous RED cell per HARD invariant"
  - "The standing shadow-compare gate phases 80–90 keep green and the hard precondition for the destructive 033 migration (Phase 90)"
affects: [80-recovery-reenqueue, 82-counts-pending-cutover, 90-destructive-033-migration]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "INVARIANTS registry of state⇒derived implications; run_shadow_compare iterates a corpus-wide anti-join (state=X AND NOT derived) per entry"
    - "zero-arg predicate factories returning correlated exists() clauses (fresh clause per run), reusing Phase-78 builders for the covered stages"
    - "soft allowlist as a boolean field on the registry entry (counted, never gated) commented back to design §6.1"

key-files:
  created:
    - src/phaze/services/shadow_compare.py
    - tests/integration/test_shadow_compare.py
  modified: []

key-decisions:
  - "PUSHING/PUSHED loosen to mere cloud_job row-existence (any status) per RESEARCH A3/OQ1 — a live-cloud file may advance past uploading/uploaded; AWAITING_CLOUD keeps the exact status='awaiting' check"
  - "Apply-outcome states (APPROVED/REJECTED/EXECUTED/FAILED/MOVED/UNCHANGED) assert proposals.status, never execution_log (RESEARCH A1 joint-write)"
  - "Soft-allowlist predicate is a benign false() placeholder: ~false() is always true, so every row at the state surfaces as expected divergence — makes NO derived claim, count-only"
  - "DISCOVERED is documented-vacuous (Pitfall 2: a rescan-wiped file can carry output rows) — omitted from the registry, present only as a comment"

patterns-established:
  - "Shared-core + dual-entry (service module + integration test import the SAME INVARIANTS/run_shadow_compare — no second copy of the assertion logic, D-01)"
  - "Registry cell parametrizes over INVARIANTS so a future FileState enum addition without an invariant fails the gate loud (D-04 comprehensiveness lock)"

requirements-completed: [MIG-02]

# Metrics
duration: ~35min
completed: 2026-07-08
---

# Phase 79 Plan 01: Shadow-Compare Gate Core Summary

**The ONE shared state↔derived assertion core — an INVARIANTS registry covering every FileState value (DISCOVERED documented-vacuous) with a {fingerprinted, local_analyzing} soft allowlist, plus a hermetic fixture-corpus CI gate with a non-vacuous RED cell per HARD invariant, reusing Phase-78's done_clause/failed_clause.**

## Performance

- **Duration:** ~35 min
- **Tasks:** 2
- **Files created:** 2

## Accomplishments
- `run_shadow_compare(session, *, sample_cap, verbose)` returns a `Report` of per-invariant divergent count + capped `file_id` sample + `hard_fail_total` (sums non-soft only) + `render(verbose)` output (D-05)
- `INVARIANTS` has one entry per FileState value except DISCOVERED; the soft allowlist is exactly `{fingerprinted, local_analyzing}` (D-04/D-06); derived side reuses `done_clause`/`failed_clause`, never `stage_status_case` (D-03)
- Hermetic gate: 14 HARD invariants each have a seeded-divergent RED cell + a consistent GREEN cell; the implication cell proves a more-derived-than-scalar file does not flag; the allowlist cell proves soft divergences are counted but `hard_fail_total == 0`; the DB-free core cell locks D-04 coverage + the D-06 allowlist
- 100% coverage of `shadow_compare.py`; `just test-bucket integration` green in isolation (128 passed); partition guard green

## Task Commits

Each task was committed atomically:

1. **Task 1: Shared assertion core (INVARIANTS + run_shadow_compare + Report)** - `fd4d3a14` (feat)
2. **Task 2: Hermetic fixture-corpus gate** - `302c2d76` (test)

## Files Created/Modified
- `src/phaze/services/shadow_compare.py` - INVARIANTS registry + Invariant/InvariantResult/Report dataclasses + async run_shadow_compare shared core (D-01)
- `tests/integration/test_shadow_compare.py` - hermetic fixture-corpus gate: divergent/consistent/implication/allowlist/core/report-shape cells, reuses the test_stage_status_equivalence db_session/DSN idiom

## Decisions Made
- **cloud_job existence for PUSHING/PUSHED:** the plan/RESEARCH A3 loosening — a live-cloud file may advance past uploading/uploaded, so asserting mere row existence avoids false positives; AWAITING_CLOUD keeps `status='awaiting'` (unambiguous).
- **proposals.status for apply outcomes:** RESEARCH A1 confirms the joint-write; execution_log has no file_id and is not authoritative.
- **false() soft placeholder:** makes no derived claim; keeps FINGERPRINTED/LOCAL_ANALYZING count-only per D-06 without inventing a spurious marker.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
- Initial ruff RUF002 on an EN DASH (`80–90`) and an EM DASH in the render f-string; replaced both with ASCII hyphens. Resolved before the Task 1 commit; not a logic change.

## Next Phase Readiness
- The standing gate is live and green in the `integration` bucket. Phase 80 (recovery/reenqueue) and Phase 82 (counts/pending cutover) can now cut readers over behind this gate; Phase 90's destructive `033` is gated on `hard_fail_total == 0` on the live corpus (D-02 records the live-corpus run in VERIFICATION).
- No blockers.

---
*Phase: 79-shadow-compare-gate-live-corpus*
*Completed: 2026-07-08*
