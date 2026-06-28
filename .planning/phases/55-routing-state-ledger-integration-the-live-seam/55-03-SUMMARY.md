---
phase: 55-routing-state-ledger-integration-the-live-seam
plan: 03
subsystem: cloud-routing
tags: [routing, state-machine, cloud-staging, k8s, live-seam, KROUTE-02, KROUTE-03]
requires:
  - "55-01: cloud_target Literal selector on ControlSettings"
  - "55-02: submit_cloud_job seeds cloud_phase (CloudPhase enum + submit_cloud_job_key)"
provides:
  - "_stage_file_to_s3 no-commit core (L1) — advisory-lock-safe per-candidate staging"
  - "stage_cloud_window k8s branch — S3 staging inside the ≤N window (KROUTE-02)"
  - "report_uploaded PUSHING→PUSHED flip + routed submit_cloud_job enqueue (KROUTE-03)"
affects:
  - "a1 rsync path: unchanged (byte-for-byte) — fork keyed on cloud_target"
  - "k8s long files now traverse AWAITING_CLOUD→PUSHING→PUSHED→ANALYZED via S3 + Kueue"
tech-stack:
  added: []
  patterns:
    - "no-commit core extraction (defer commit to the advisory-locked caller)"
    - "single-branch fork on cloud_target (reuse advisory lock + FIFO + window math)"
    - "rowcount-guarded idempotent state flip (mirrors agent_push.report_pushed)"
    - "routed controller enqueue via enqueue_router.resolve_queue_for_task (never raw)"
key-files:
  created: []
  modified:
    - src/phaze/services/cloud_staging.py
    - src/phaze/tasks/release_awaiting_cloud.py
    - src/phaze/routers/agent_s3.py
    - tests/test_staging_cron.py
    - tests/test_routers/test_agent_s3.py
decisions:
  - "k8s staging calls the NO-COMMIT _stage_file_to_s3 core; the cron commits once post-loop so pg_advisory_xact_lock is held across the whole tick (L1)"
  - "GATE-1 (compute agent) is guarded to cloud_target=='a1'; skipped on k8s (no persistent compute agent — ephemeral Kueue pods) so k8s files never wedge in AWAITING_CLOUD (L2). GATE-2 (fileserver) stays for both"
  - "report_uploaded reuses PUSHING/PUSHED (no new FileRecord state); the submit_cloud_job enqueue is defensively guarded on cloud_target=='k8s' so a future a1-on-S3 path preserves cloud_job-only behavior"
metrics:
  duration: ~16 min
  completed: 2026-06-28
  tasks: 3
  files: 5
---

# Phase 55 Plan 03: Routing/State/Ledger Integration — The Live Seam Summary

Wired K8s in as the ONE new branch at the two coordinated `cloud_target` fork points inside the
existing v5.0 `stage_cloud_window` ≤N in-flight window, with the live a1 rsync path unchanged —
a long k8s file now traverses AWAITING_CLOUD→PUSHING→PUSHED via S3 + Kueue, draining identically to a1.

## What was built

### Task 1 — No-commit `_stage_file_to_s3` core (Landmine L1)
Extracted `_stage_file_to_s3(session, file, task_router)` from `cloud_staging.stage_file_to_s3`: it
runs the full staging body (fileserver gate → `create_multipart_upload` → `presign_upload_parts` →
`CloudJob` on-conflict upsert → `s3_upload` enqueue) but **defers** the commit. The public
`stage_file_to_s3` is now a thin wrapper that awaits the core then commits once — so `redrive_upload`
(its single-file caller) is unaffected, while the cron loop can call the core per-candidate and commit
once after the loop, holding `pg_advisory_xact_lock` across the whole tick.

### Task 2 — `stage_cloud_window` k8s branch (D-01a, L1, L2)
`stage_cloud_window` now forks on `cfg.cloud_target` inside the same window:
- GATE-1 (`select_active_agent(kind="compute")`) is guarded to `cloud_target=="a1"` and **skipped on
  k8s** (L2) — k8s has no persistent compute agent, so requiring one would wedge every k8s file.
- GATE-2 (fileserver) stays for both targets.
- Per candidate: a1 flips PUSHING then `_enqueue_push_file` (rsync); k8s flips PUSHING then awaits the
  **no-commit** `_stage_file_to_s3` (S3, enqueues `s3_upload` not `push_file`).
- The single post-loop commit is retained — exactly one `session.commit` in the function (verified by
  grep); the advisory lock + FIFO `FOR UPDATE SKIP LOCKED` claim + window/slots math are reused, not
  duplicated.

### Task 3 — `report_uploaded` extension (D-01b)
Added `request: Request` to `report_uploaded` so it can reach `app.state`. After the existing
`cloud_job` UPLOADING→UPLOADED flip, inside a defensive `cloud_target=="k8s"` guard:
- a rowcount-guarded `FileRecord` PUSHING→PUSHED flip (mirrors `agent_push.report_pushed`) — a
  duplicate/late callback matches 0 rows and returns an idempotent no-op with **no** re-enqueue;
- on a successful flip, `submit_cloud_job` is enqueued via
  `enqueue_router.resolve_queue_for_task("submit_cloud_job", request.app.state, session)` on the
  controller queue (never a raw `controller_queue.enqueue`/default queue) with the deterministic
  `submit_cloud_job_key(file_id)`.
A non-k8s target preserves today's cloud_job-only behavior. AUTH-01 intact (file_id on the path, agent
from the token).

## Tests
- `tests/test_staging_cron.py`: `_stage_file_to_s3` no-commit assertion + public-wrapper-still-commits;
  k8s branch (GATE-1 skipped, reaches PUSHING, enqueues `s3_upload`); k8s GATE-2 hold with no
  fileserver; k8s window cap under concurrent advisory-locked ticks.
- `tests/test_routers/test_agent_s3.py`: first k8s `/uploaded` → PUSHED + one routed `submit_cloud_job`
  (deterministic key); duplicate → idempotent no-op (no second submit); non-k8s → cloud_job-only.

## Verification
- `uv run pytest tests/test_staging_cron.py tests/test_routers/test_agent_s3.py -x` → 29 passed.
- a1 regression (window-cap, FIFO, double-tick, overlapping-ticks, cloud-local/a1) all green unchanged.
- Seam-adjacent sweep (staging_cron + agent_s3 + agent_push + reconcile_cloud_jobs +
  no_default_queue_producers) → 66 passed.
- `uv run mypy .` → clean (182 source files). `uv run ruff check` → clean on all touched files.
- Acceptance greps: `_stage_file_to_s3` referenced in the cron (import+call); exactly one
  `session.commit` in `stage_cloud_window`; `resolve_queue_for_task` referencing `submit_cloud_job` in
  `agent_s3.py`; no raw `controller_queue.enqueue`/`*.state.queue` in the function.

## Threat model coverage
All six mitigations in the plan's STRIDE register are implemented and tested:
T-55-SEAM-01 (no-commit core holds the advisory lock — window-cap test), T-55-SEAM-02 (PUSHED flip
frees the slot), T-55-SEAM-03 (routed enqueue only), T-55-SEAM-04 (token-auth unchanged),
T-55-SEAM-05 (rowcount-guarded idempotent flip, no double-submit), T-55-SEAM-06 (GATE-1 skipped on
k8s). No new security surface beyond the modeled callbacks — no threat flags.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Added missing `FileState` import to `agent_s3.py`**
- **Found during:** Task 3 (GREEN)
- **Issue:** `agent_s3.py` imported only `FileRecord` from `phaze.models.file`; the new PUSHING→PUSHED
  flip references `FileState`, so mypy failed with `Name "FileState" is not defined` and the new tests
  errored at runtime.
- **Fix:** Changed the import to `from phaze.models.file import FileRecord, FileState`.
- **Files modified:** src/phaze/routers/agent_s3.py
- **Commit:** c4c4d3b (folded into the Task 3 feat commit)

## Known Stubs
None — all code paths are fully wired (no-commit core, k8s stage branch, PUSHED flip, routed submit).

## Deferred Issues
None from this plan's scope. **Observation (out of scope, pre-existing):** the function-scoped
`async_engine` fixture (`tests/conftest.py`) runs `create_all`/`drop_all` per test on the shared
`phaze_test` DB; under `pytest-randomly` reordering, combining several DB-backed modules in one process
can intermittently collide on `CREATE TABLE agents` (`pg_type_typname_nsp_index` UniqueViolation). This
affects untouched modules equally and disappears with deterministic ordering — not a regression from
this plan; flagged for a future test-infra hardening (e.g. session-scoped schema or per-worker DBs).

## Self-Check: PASSED
- All 5 modified files exist on disk.
- All per-task commits exist (test/refactor/feat pairs + docs): 50be2b3, ea081fd, a653920, 7b5b982,
  0fa8de0, c4c4d3b, 2ac8098.
