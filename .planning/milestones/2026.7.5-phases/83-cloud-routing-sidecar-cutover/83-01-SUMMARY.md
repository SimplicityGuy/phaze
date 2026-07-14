---
phase: 83-cloud-routing-sidecar-cutover
plan: 01
subsystem: cloud-routing
tags: [backends, cloud_job, awaiting, sidecar, dispatch-discipline]
requires:
  - "cloud_job sidecar model (Phase 77 D-04: 'awaiting' status member)"
  - "ComputeAgentBackend.dispatch on_conflict_do_update upsert (donor)"
provides:
  - "hold_awaiting_cloud() — the single shared go-forward writer of cloud_job.status='awaiting'"
affects:
  - "83-04 (both over-cap spill paths reuse this helper)"
  - "83-05 (the trigger_analysis hold path reuses this helper)"
tech-stack:
  added: []
  patterns:
    - "pg_insert(...).on_conflict_do_update(index_elements=['file_id']) upsert on uq_cloud_job_file_id"
    - "dispatch discipline: mutate in the caller's session, NEVER commit"
key-files:
  created: []
  modified:
    - "src/phaze/services/backends.py"
    - "tests/analyze/services/test_backends.py"
decisions:
  - "D-02: the awaiting writer is a shared services/ helper (not inline, not a bulk upsert)"
  - "D-03: spill re-stamp keeps attempts in the upsert set_ so a terminalized row re-stamps to awaiting retaining spent budget; 'awaiting' stays out of IN_FLIGHT"
  - "D-13: LocalBackend.dispatch keeps its LOCAL_ANALYZING flip and writes/deletes no cloud_job row (verified by test, not changed)"
metrics:
  duration: "~45m"
  completed: "2026-07-09"
  tasks: 2
  files: 2
---

# Phase 83 Plan 01: Shared Awaiting-Cloud Writer Summary

Added `hold_awaiting_cloud()` — the single shared go-forward writer of `cloud_job.status='awaiting'` —
to `services/backends.py`, closing the D-01 discovery (no such writer existed) so every future
`AWAITING_CLOUD` hold carries its sidecar row and the hard shadow invariant
`AWAITING_CLOUD ⇒ cloud_job(status='awaiting')` holds.

## What Was Built

- **`hold_awaiting_cloud(session, file, *, attempts=0)`** (`services/backends.py:84`): dual-writes
  `file.state = FileState.AWAITING_CLOUD` (D-00c) then upserts the sidecar row via
  `pg_insert(CloudJob).on_conflict_do_update(index_elements=["file_id"], set_={"status": ..., "attempts": ...})`.
  A fresh hold INSERTs `status='awaiting'` / `attempts=0`; a spill re-stamp of a terminalized `FAILED`
  row UPDATEs the same row (one row per file, `uq_cloud_job_file_id`) back to `awaiting`, taking
  `attempts` from the argument so a spill caller retains `attempts=cloud_submit_max_attempts` as the
  budget-spent marker `select_backend` reads to route to local (D-03). `backend_id` is left unset
  (a hold has none), `cloud_phase` NULL. **Never commits** — the caller owns the commit boundary
  (dispatch discipline; a commit here would drop the tick advisory lock, Landmine L1).

- **Four unit tests** (`tests/analyze/services/test_backends.py`):
  - Test A — fresh hold writes exactly one `awaiting` row and flips `file.state`, visible in-session
    with no commit.
  - Test B — re-stamping a seeded `FAILED` row (`attempts=cloud_submit_max_attempts`) stays one row,
    status `awaiting`, retaining the spent budget (D-03).
  - Test C — `CloudJobStatus.AWAITING ∉ backends.IN_FLIGHT` (D-03: no in-flight-count inflation).
  - Test D — `LocalBackend.dispatch` on a held file flips it to `LOCAL_ANALYZING` and leaves the inert
    `awaiting` row present and unchanged (D-13: no-cloud_job-row writer/deleter; reaped later by D-14).

## Verification

- `uv run ruff check` + `uv run mypy src/phaze/services/backends.py`: clean.
- Grep audit: `hold_awaiting_cloud` defined exactly once; helper body contains no `session.commit`.
- `analyze` bucket in isolation (deterministic order `-p no:randomly`): **523 passed, 0 failed, 0 errors**.
  The four new tests pass explicitly and against the freshly-provisioned test DB.

## Deviations from Plan

None — plan executed exactly as written. No code deviations (Rules 1–4) were required; the helper and
tests match the D-02/D-03/D-13 shapes specified in 83-PATTERNS / 83-RESEARCH.

## Out-of-Scope Discovery (logged, not fixed)

Running the whole `tests/analyze` bucket under pytest-randomly's **random** ordering produces a
variable number of setup ERRORs / occasional FAILUREs (177 under one seed, 55 under another),
concentrated in `test_recovery.py`, `test_release_awaiting_cloud.py`, and `test_submit_cloud_job.py`.
Each of those files passes cleanly **in isolation**, and the whole bucket passes green with
deterministic ordering. This is the pre-existing "colima full-suite flake" / "CI bucket test-isolation"
non-hermetic class already recorded in project memory — it is NOT introduced by this plan (which only
adds an additive helper + four hermetic tests). Recorded in
`.planning/phases/83-cloud-routing-sidecar-cutover/deferred-items.md`.

## Notes for Downstream Plans

- **83-05 (hold path):** replace `trigger_analysis`'s bare `file.state = FileState.AWAITING_CLOUD`
  (`routers/pipeline.py:346`) with `await hold_awaiting_cloud(session, file)` (attempts defaults to 0);
  the existing post-loop `await session.commit()` at `:356` stays (it is the writer's own commit
  boundary, not a dispatch loop).
- **83-04 (spill paths):** `report_upload_failed` and `report_push_mismatch` reuse this helper with
  `attempts=settings.cloud_submit_max_attempts` (D-03 budget-spent marker) instead of hand-writing a
  `FAILED` terminalization.

## Self-Check: PASSED

- `src/phaze/services/backends.py` — FOUND (contains `async def hold_awaiting_cloud`).
- `tests/analyze/services/test_backends.py` — FOUND (four new tests present).
- Commit `2c20145d` (feat) — FOUND.
- Commit `9eb569b1` (test) — FOUND.
