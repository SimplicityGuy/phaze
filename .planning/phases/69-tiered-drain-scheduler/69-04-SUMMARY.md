---
phase: 69-tiered-drain-scheduler
plan: 04
subsystem: scheduler
tags: [recovery, single-owner, cloud-spill, in-flight, SCHED-03, SCHED-05, D-04]
requires:
  - "src/phaze/tasks/reenqueue.py recover_orphaned_work orphan comprehension + _get_awaiting_cloud_ids mirror"
  - "src/phaze/services/backends.py IN_FLIGHT status set (Phase 68 D-10)"
  - "src/phaze/models/cloud_job.py CloudJob.file_id / .status / .attempts / .backend_id"
  - "src/phaze/config.py cloud_submit_max_attempts (the select_backend cloud-exclusion cap)"
provides:
  - "src/phaze/tasks/reenqueue._in_flight_cloud_job_ids single-owner exclusion set for recover_orphaned_work (SCHED-05)"
  - "src/phaze/routers/agent_push.report_push_mismatch at-cap spill to AWAITING_CLOUD + cloud budget spent (SCHED-03/D-04)"
  - "src/phaze/routers/agent_s3.report_upload_failed at-cap spill to AWAITING_CLOUD + cleanup preserved (SCHED-03/D-04)"
affects:
  - "Completes the Phase-69 tiered scheduler: every cloud-failure path (reconcile + both callbacks) now spills to local; ANALYSIS_FAILED comes only from local failure"
tech-stack:
  added: []
  patterns:
    - "single-owner-per-backend-kind: exclude any ledger row whose file has an in-flight cloud_job from the recovery orphan set"
    - "cloud-budget-spent stamp: set cloud_job.attempts = cloud_submit_max_attempts on spill so select_backend forces local next tick (D-04)"
key-files:
  created: []
  modified:
    - "src/phaze/tasks/reenqueue.py"
    - "src/phaze/routers/agent_push.py"
    - "src/phaze/routers/agent_s3.py"
    - "src/phaze/config.py"
    - "tests/analyze/tasks/test_recovery.py"
    - "tests/agents/routers/test_agent_push.py"
    - "tests/agents/routers/test_agent_s3.py"
decisions:
  - "SCHED-05: _in_flight_cloud_job_ids mirrors _get_awaiting_cloud_ids (SELECT cloud_job.file_id WHERE status IN IN_FLIGHT); IN_FLIGHT imported from services.backends (no re-hardcode, no import cycle)"
  - "SCHED-03/D-04: both at-cap callbacks flip FileState ANALYSIS_FAILED -> AWAITING_CLOUD and stamp cloud_job.attempts = cloud_submit_max_attempts so select_backend excludes cloud and the drain routes the file to local; cloud_job stays FAILED so in_flight_count drains"
  - "agent_s3 spill keeps cloud_phase=None (WR-01) and the full cleanup (abort multipart + delete staged object + ledger clear)"
metrics:
  duration: "~1h"
  completed: "2026-07-04"
  tasks: 2
  files_created: 0
  files_modified: 7
---

# Phase 69 Plan 04: Single Recovery Owner + Compute/Kueue Callback Spill Summary

Closes the SCHED-05 double-owner vector and finishes the SCHED-03 compute-spill uniformity. `recover_orphaned_work` now excludes any ledger row whose file carries an in-flight `cloud_job` row (any `backend_id`), so a cloud-backed file is owned SOLELY by its backend reconcile/`/pushed` callback — no second recovery path, no replay of the 44.5k over-enqueue incident class. The compute (`agent_push`) and kueue-upload (`agent_s3`) at-cap terminal callbacks now SPILL the file back to `AWAITING_CLOUD` with its cloud budget marked spent instead of hard-failing to `ANALYSIS_FAILED`, so a cloud-flaky long file falls to local (D-04) on the next drain tick. `ANALYSIS_FAILED` now comes ONLY from a local analysis failure.

## What Was Built

### Task 1 — In-flight cloud_job exclusion in recover_orphaned_work (SCHED-05) — commit `0fef498`
- Added `_in_flight_cloud_job_ids(session) -> set[str]` to `reenqueue.py`, an exact mirror of `_get_awaiting_cloud_ids`: `SELECT cloud_job.file_id WHERE cloud_job.status IN [s.value for s in IN_FLIGHT]`, returning `{str(fid) ...}`. `IN_FLIGHT` = {UPLOADING, UPLOADED, SUBMITTED, RUNNING} is imported from `phaze.services.backends` (no re-hardcode of the enum; verified no import cycle — `backends` and its deps do not import `reenqueue`).
- Extended the orphan comprehension in `recover_orphaned_work` (read the set ONCE alongside `live`/`done_sets`): `... and _natural_id(r) not in in_flight`. After Phase-68 BACK-03 a compute file has BOTH an in-flight `cloud_job` row AND a `process_file`/`push_file` ledger row; excluding it here makes the backend reconcile/callback the single owner. KEPT the existing AWAITING_CLOUD held-file path intact for files with NO in-flight `cloud_job` (genuinely orphaned → still route to a compute agent).
- Added three `single_owner` tests: (1) an in-flight-cloud_job compute file with a ledger row is skipped even with a compute agent online; (2) a TERMINAL (FAILED) cloud_job is NOT in the in-flight set → the held file still recovers via the compute-only path; (3) a held file with NO cloud_job keeps the held recovery path (no regression / no over-exclusion).

### Task 2 — Compute/kueue callback at-cap spill to AWAITING_CLOUD (SCHED-03/D-04, TDD) — RED `14305df`, GREEN `98d1a8b`, docs `0ef5903`
- **RED** (`14305df`): rewrote the two existing at-cap callback tests (`test_agent_push.py`, `test_agent_s3.py`) to assert the spill contract — FileState → `AWAITING_CLOUD`, `cloud_job.attempts >= cloud_submit_max_attempts`, cleanup preserved. Confirmed 3 failing against the old `ANALYSIS_FAILED` source.
- **GREEN** (`98d1a8b`): `report_push_mismatch` at-cap branch now flips `FileRecord` to `AWAITING_CLOUD` (not `ANALYSIS_FAILED`), keeps `cloud_job -> FAILED` (drains it from the D-10 in-flight set so `in_flight_count(compute)` stays honest), and sets `cloud_job.attempts = settings.cloud_submit_max_attempts` in the same UPDATE so `select_backend` excludes cloud and the next drain tick routes the file to local (D-04). `report_upload_failed` at-cap branch mirrors it: `AWAITING_CLOUD` + `cloud_job` FAILED + `cloud_phase=None` (WR-01) + `attempts` spent, with the abort-multipart + delete-staged-object + ledger-clear cleanup PRESERVED. Under-cap re-drive paths are UNCHANGED; both callbacks stay 200 / `cleared=True`.
- **docs** (`0ef5903`): updated the `agent_push` module + `report_push_mismatch` docstrings and the `config.py` `push_max_attempts` description to describe the spill (was still documenting the old hard-fail).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 — Blocking] Existing at-cap callback tests updated (outside declared files)**
- **Found during:** Task 2 RED.
- **Issue:** The plan's `files_modified` listed only `tests/analyze/tasks/test_recovery.py`, but the existing at-cap tests that assert `ANALYSIS_FAILED` live in `tests/agents/routers/test_agent_push.py` and `test_agent_s3.py`. Flipping the source terminal breaks those tests, so they MUST be updated — and they are the natural home for the spill contract (they carry the HTTP-client + cloud_job seed harness that `test_recovery.py` lacks). The sibling plan (69-03) touches neither file, so no merge conflict.
- **Fix:** Rewrote `test_mismatch_over_cap_*` → `test_push_mismatch_over_cap_*` and `test_failed_at_cap_*` → `test_upload_failed_at_cap_*` to assert the spill (AWAITING_CLOUD + budget spent + preserved cleanup); updated both module docstrings.
- **Files modified:** `tests/agents/routers/test_agent_push.py`, `tests/agents/routers/test_agent_s3.py`.
- **Commits:** `14305df` (RED), assertions verified GREEN at `98d1a8b`.

**2. [Rule 1 — Stale doc] config.py push_max_attempts description corrected**
- **Found during:** Task 2 grep-clean of `ANALYSIS_FAILED` references.
- **Issue:** `config.py` still described `push_max_attempts` as the cap "before a sha256-mismatched file is marked ANALYSIS_FAILED" — now factually wrong (it spills to AWAITING_CLOUD). `config.py` was not in the plan's `files_modified` but is directly implicated by the behavior change; the sibling plan does not touch it.
- **Fix:** Reworded the comment + Field description to describe the spill to AWAITING_CLOUD → local.
- **Files modified:** `src/phaze/config.py`.
- **Commit:** `98d1a8b`.

No architectural changes; no authentication gates; no new dependencies; no migrations.

## Threat Model Compliance

- **T-69-04-01 (double recovery → queue detonation):** `recover_orphaned_work` excludes every file with an in-flight `cloud_job` from the orphan set → single owner per backend kind; verified by `test_single_owner_in_flight_cloud_job_skips_ledger_recovery`.
- **T-69-04-02 (unbounded cloud retry):** the spill stamps `cloud_job.attempts = cloud_submit_max_attempts` (bounded gt=0 lt=20) so `select_backend` forces local; the file cannot re-thrash cloud. Verified by the `compute_spill` / `spills_to_awaiting_cloud` budget assertions.
- **T-69-04-03 (SQLi):** all writes are ORM `update(...).values(...)` + bound params; the in-flight query uses `CloudJob.status.in_([...])` with a bound list — no f-string SQL.
- **T-69-04-04 / -05 (spoofing / info disclosure):** callbacks keep the existing bearer-token agent dependency (untouched); logs carry only file_id/agent_id/attempt/cap.

## Verification

- `uv run pytest tests/analyze/tasks/test_recovery.py -k single_owner -x` — 3 passed. Full file — 41 passed.
- `uv run pytest tests/agents/routers/test_agent_push.py tests/agents/routers/test_agent_s3.py -k "push_mismatch or upload_failed or compute_spill or spill"` — 3 passed (RED→GREEN confirmed).
- `just test-bucket analyze` — 412 passed (a first run flaked 6 errors in the UNRELATED `test_scheduling_ledger.py`; root-caused to a pre-existing hermeticity artifact — that module imports only `SchedulingLedger`, never `FileRecord`, so in a partial-metadata context `create_all` fails building `file_companions`'s FK to `files`; running it alongside a FileRecord-importer → 52 passed; the retry ran clean at 412/0, matching the documented run-to-run colima flake class).
- `just test-bucket agents` — 395 passed.
- `grep -n "ANALYSIS_FAILED" src/phaze/routers/agent_push.py src/phaze/routers/agent_s3.py` — only docstring/comment references remain; neither at-cap branch flips to it.
- `uv run mypy src/phaze` — clean (157 source files). `pre-commit` ran on every commit (never `--no-verify`); all hooks pass.

## Known Stubs

None. Both mechanisms are fully wired: recovery reads the live `cloud_job` in-flight set and excludes those files from the orphan comprehension; both callbacks spill to `AWAITING_CLOUD` with the cloud budget stamped spent so the tiered drain routes the file to local. No placeholder data, no TODO/FIXME introduced.

## Self-Check: PASSED

- SUMMARY.md present at `.planning/phases/69-tiered-drain-scheduler/69-04-SUMMARY.md`.
- All modified source + test files present on disk.
- All four commits present: `0fef498` (SCHED-05), `14305df` (RED), `98d1a8b` (GREEN), `0ef5903` (docs).
- TDD gate sequence for Task 2: `test(...)` `14305df` → `feat(...)` `98d1a8b` (RED before GREEN, confirmed by the observed 3 failures on the RED run).
