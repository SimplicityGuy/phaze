---
phase: 69-tiered-drain-scheduler
plan: 05
subsystem: infra
tags: [scheduler, drain, backends, filestate, saq, postgres, cloud-burst]

# Dependency graph
requires:
  - phase: 69-tiered-drain-scheduler
    provides: "Backend protocol + resolve_backends + tiered stage_cloud_window drain + select_backend policy (plans 01-04)"
  - phase: 68-backend-protocol
    provides: "LocalBackend/ComputeAgentBackend/KueueBackend dispatch lifecycle + cloud_job in_flight substrate"
provides:
  - "FileState.LOCAL_ANALYZING — the drain-local in-analysis lane (code-only, no migration)"
  - "LocalBackend.dispatch flips file out of AWAITING_CLOUD before enqueue (CR-01 fix)"
  - "Honest LocalBackend.dispatch return value (WR-01): dedup no-op -> not-staged"
affects: [70-multi-kueue, backend-registry-validation, drain-recovery]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "dispatch-owns-the-state-flip: every Backend.dispatch removes its file from the AWAITING_CLOUD candidate set in the caller session before enqueue (local mirrors compute/kueue PUSHING flip)"

key-files:
  created: []
  modified:
    - src/phaze/models/file.py
    - src/phaze/services/backends.py
    - tests/analyze/services/test_backends.py
    - tests/analyze/core/test_staging_cron.py

key-decisions:
  - "LOCAL_ANALYZING is a NEW dedicated in-analysis state (not reuse of PUSHING/FINGERPRINTED): distinct from cloud-owned PUSHING (no cloud_job/reconcile owner), excluded from get_cloud_staging_candidates, and in neither reenqueue done-set so a lost local job re-drives."
  - "Code-only StrEnum over the existing String(30) column (15 chars ≤ 30) — no Alembic migration (ANALYSIS_FAILED/AWAITING_CLOUD/PUSHING precedent)."
  - "The state flip stands regardless of the enqueue dedup outcome — the file has left AWAITING_CLOUD either way; only the staged/skipped tally reads the dedup result."

patterns-established:
  - "Backend.dispatch contract parity: local/compute/kueue all flip file.state out of the drain candidate set, in the caller session, before enqueue, never committing (the drain owns the single post-loop commit under the advisory lock)."

requirements-completed: [SCHED-01, SCHED-03]

# Metrics
duration: ~30min
completed: 2026-07-04
---

# Phase 69 Plan 05: LOCAL_ANALYZING Gap-Closure Summary

**FileState.LOCAL_ANALYZING closes CR-01: LocalBackend.dispatch now flips a locally-spilled file out of AWAITING_CLOUD before enqueue, so it leaves the drain candidate set and can no longer be double-dispatched to a cloud backend while its process_file is in flight.**

## Performance

- **Duration:** ~30 min
- **Started:** 2026-07-04T15:10Z (approx)
- **Completed:** 2026-07-04T15:25Z (approx)
- **Tasks:** 3 (TDD: RED → GREEN CR-01 → GREEN WR-01)
- **Files modified:** 4

## Accomplishments
- Added `FileState.LOCAL_ANALYZING = "local_analyzing"` — a code-only StrEnum value over the existing `String(30)` column (no migration), the drain-local in-analysis lane.
- `LocalBackend.dispatch` now flips `file.state = FileState.LOCAL_ANALYZING` in the caller session after the fileserver gate and before enqueue (mirrors ComputeAgentBackend/KueueBackend's `FileState -> PUSHING` flip), removing a locally-spilled file from `get_cloud_staging_candidates`.
- `LocalBackend.dispatch` now returns `job is not None` (WR-01) — a deterministic-key `process_file:<id>` dedup no-op is reported as not-newly-staged, honoring the Backend.dispatch tally contract.
- New RED→GREEN regression coverage: state-flip, staging-candidate exclusion, WR-01 return values (test_backends.py) and the two-tick spill-to-local-then-cloud-frees no-re-dispatch scenario (test_staging_cron.py).
- Closes VERIFICATION success criteria 1 and 3 (SCHED-01 / SCHED-03); full 146-test phase-touched suite green (141 baseline + 5 new).

## Task Commits

Each task was committed atomically (TDD RED before GREEN):

1. **Task 1: RED — LOCAL_ANALYZING contract + regression tests** - `4f44e7c` (test)
2. **Task 2: GREEN (CR-01) — flip LocalBackend.dispatch out of AWAITING_CLOUD** - `333e990` (feat)
3. **Task 3: GREEN (WR-01) — honest dispatch return value** - `c973a03` (feat)

_TDD discipline: the failing `test(69-05)` commit precedes both `feat(69-05)` implementation commits._

## Files Created/Modified
- `src/phaze/models/file.py` - Added `FileState.LOCAL_ANALYZING` with a code-only/no-migration doc-comment describing the drain-local in-analysis lane and its exclusion from candidacy/done-sets.
- `src/phaze/services/backends.py` - `LocalBackend.dispatch` flips to `LOCAL_ANALYZING` before enqueue (CR-01) and returns `job is not None` (WR-01); docstring updated to state it removes the file from further drain consideration.
- `tests/analyze/services/test_backends.py` - Added `test_local_dispatch_flips_to_local_analyzing`, `test_local_dispatch_excluded_from_staging_candidates`, `test_local_dispatch_returns_true_on_enqueue`, `test_local_dispatch_returns_false_on_dedup_noop`.
- `tests/analyze/core/test_staging_cron.py` - Added `test_local_spill_not_redispatched_to_cloud` (two-tick verifier scenario: spill-to-local then cloud-frees → cloud_job COUNT stays 0, state stays LOCAL_ANALYZING).

## Decisions Made
- **LOCAL_ANALYZING is a NEW dedicated state**, not a reuse of PUSHING/FINGERPRINTED: it must be excluded from `get_cloud_staging_candidates` (not AWAITING_CLOUD), NOT analyze-done or push-done (so a lost local job re-drives via the ledger), and NOT PUSHING/PUSHED (which imply a cloud_job + reconcile/callback owner the local path does not have). Validated against `pipeline.get_cloud_staging_candidates`, `reenqueue._select_done_analyze_ids/_select_done_push_ids/_in_flight_cloud_job_ids`, and `agent_analysis.put_analysis` (flips → ANALYZED on a non-empty body, never gated on prior state).
- **Code-only, no migration** — 15 chars ≤ String(30), consistent with the ANALYSIS_FAILED/AWAITING_CLOUD/PUSHING precedent.

## Deviations from Plan

None - plan executed exactly as written. No deviation rules (1-4) triggered; no auth gates; no package installs.

## Issues Encountered
None. The RED tests failed for exactly the stated reasons (file stayed `awaiting_cloud`; dispatch returned `True` on dedup), and both GREEN steps turned them green with no regressions.

## Out of Scope (tracked, NOT implemented — per plan)
- **WR-02** (config `_validate_registry`: no ≥1-local invariant when `cloud_enabled`) — Phase-70 registry-validation territory.
- **WR-03** (compute reconcile no-op → stuck compute PUSHING row has zero recovery owner) — Phase-70 robustness.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- CR-01 closed: local is now a genuine drain-terminal target; the tiered scheduler's rank-first exclusivity (SCHED-01) and black-hole/local-terminal guard (SCHED-03) hold for the local leg.
- Ready for phase re-verification. WR-02/WR-03 remain open, explicitly deferred to Phase 70.

## Self-Check: PASSED
- Files: all 4 modified files present.
- Commits: `4f44e7c`, `333e990`, `c973a03` all present in git log.
- Suite: 146 passed (141 VERIFICATION baseline + 5 new); ruff + mypy clean on `src/phaze/services/backends.py` and `src/phaze/models/file.py`.

---
*Phase: 69-tiered-drain-scheduler*
*Completed: 2026-07-04*
