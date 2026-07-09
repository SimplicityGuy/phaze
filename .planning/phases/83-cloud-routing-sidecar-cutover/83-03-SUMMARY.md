---
phase: 83-cloud-routing-sidecar-cutover
plan: 03
subsystem: api
tags: [fastapi, sqlalchemy, postgres, cloud_job, agent-callback, index-hygiene]

# Dependency graph
requires:
  - phase: 77-additive-schema-rescan-wipe-fix-migration-032
    provides: "cloud_job.status='awaiting' sidecar representation + ix_cloud_job_awaiting partial index (migration 032)"
provides:
  - "D-14 reaper: both analyze-terminal seams (put_analysis success, report_analysis_failed terminal) DELETE the file's cloud_job row WHERE status='awaiting', joining each seam's existing transaction"
  - "bounds ix_cloud_job_awaiting growth so the */5 drain tick never scans a monotonically growing dead set"
affects: [83-cloud-routing-sidecar-cutover drain cutover, cloud_job lifecycle, ix_cloud_job_awaiting]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Awaiting-row reaper at analyze-terminal callback seams: additive DELETE joins the seam's existing txn (no new session/commit), filtered to status='awaiting' so cloud-analyzed SUCCEEDED/RUNNING rows survive"

key-files:
  created: []
  modified:
    - src/phaze/routers/agent_analysis.py
    - tests/agents/routers/test_agent_analysis.py

key-decisions:
  - "Reaper filters strictly on CloudJobStatus.AWAITING.value so a cloud-analyzed file's SUCCEEDED/RUNNING row is never touched"
  - "DELETE joins each seam's existing transaction (alongside _delete_staged_object_if_cloud, before the existing commit) — no new session/commit introduced"

patterns-established:
  - "Index-hygiene reaper at callback terminal seams: reuse the scan_deletion.py delete(CloudJob).where(...) idiom, scoped to the PATH file_id (AUTH-01) and the inert status, executed inside the transaction the seam already owns"

requirements-completed: [SIDECAR-01]

# Metrics
duration: 12min
completed: 2026-07-09
---

# Phase 83 Plan 03: D-14 Awaiting-Row Reaper Summary

**Both analyze-terminal seams (put_analysis success, report_analysis_failed terminal) now DELETE the file's inert `cloud_job` row WHERE `status='awaiting'` inside the existing transaction, bounding `ix_cloud_job_awaiting` growth while leaving cloud-analyzed SUCCEEDED/RUNNING rows untouched.**

## Performance

- **Duration:** ~12 min
- **Started:** 2026-07-09T19:46:00Z
- **Completed:** 2026-07-09T19:56:00Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- Added the D-14 reaper at `put_analysis` (success) and `report_analysis_failed` (terminal) — each deletes the file's `awaiting` `cloud_job` hold-over row (the inert row `LocalBackend.dispatch` leaves behind per D-13) inside the transaction the seam already opens.
- The reaper is the real defense against the D-14 index-growth degradation: it caps the `ix_cloud_job_awaiting` dead set so the `*/5` drain tick never scans a monotonically growing set at 200K.
- Proved via 4 unit tests that awaiting rows are reaped at both seams and that SUCCEEDED / RUNNING (cloud-analyzed) rows are preserved by the `status='awaiting'` filter.
- No new transaction/commit added (handler `session.commit()` count unchanged at 3); AUTH-01 preserved (`file_id` from the URL path only, handler signatures unchanged).

## Task Commits

1. **Task 1: Reap the inert awaiting cloud_job row at both analyze-terminal seams** - `482c3f07` (feat)
2. **Task 2: Unit-test the reaper (awaiting deleted; SUCCEEDED/RUNNING untouched)** - `e817796f` (test)

_Note: this plan's two tasks split implementation (feat) and tests (test) per the plan's task decomposition._

## Files Created/Modified
- `src/phaze/routers/agent_analysis.py` - imported `CloudJobStatus`; added `delete(CloudJob).where(file_id, status='awaiting')` at both the `put_analysis` success seam and the `report_analysis_failed` terminal seam, before each existing `session.commit()`.
- `tests/agents/routers/test_agent_analysis.py` - added `_seed_cloud_job` / `_cloud_job_present` helpers and 4 reaper tests (awaiting reaped after put_analysis; awaiting reaped after report_analysis_failed; SUCCEEDED preserved; RUNNING preserved).

## Decisions Made
None beyond the plan — followed the D-14 delta and the `scan_deletion.py:110` DELETE idiom exactly. Left `staging_bucket` NULL in the seeded `cloud_job` rows so the seam's `_delete_staged_object_if_cloud` S3 guard short-circuits (zero S3 calls) and the tests exercise only the reaper.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

**Pre-existing non-hermetic agents-bucket flake (OUT OF SCOPE — deferred).**
`just test-bucket agents` (full bucket) intermittently errors with `IntegrityError: duplicate key value violates unique constraint "pk_agents"` during test setup; the count/victims vary by run/ordering (observed 1, 12, 14 errors). Every affected test — including this plan's 4 new reaper tests — passes in isolation.

Determined pre-existing and unrelated to this change:
- The source change touches only `cloud_job` — **zero** references to the `agents` table (the violated constraint is `pk_agents`).
- Deselecting the 4 new reaper tests still reproduces the errors (12 observed).
- The 4 new reaper tests pass in isolation (`-k "reaps or leaves_succeeded or leaves_running"` → 4 passed) and passed inside the default full-bucket run (444 passed, 1 unrelated error).
- Root cause is the shared-DB, function-scoped `async_engine` + fixed-id `seed_test_agent` fixture flaking under local colima VM pressure (the documented "local full-suite colima flake" / "CI bucket test-isolation" behavior).

Logged to `.planning/phases/83-cloud-routing-sidecar-cutover/deferred-items.md` for a future test-hermeticity hygiene task. Verification followed the documented guidance: re-run the failing subset in isolation to confirm infra-not-regression.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- The inert `awaiting` hold-over row is now reaped at analyze-terminal, so the drain-cutover work in this phase can rely on a bounded `ix_cloud_job_awaiting` dead set.
- No blockers introduced. Independent of the helper and drain cutover (pure additive DELETE at two existing seams).

---
*Phase: 83-cloud-routing-sidecar-cutover*
*Completed: 2026-07-09*
