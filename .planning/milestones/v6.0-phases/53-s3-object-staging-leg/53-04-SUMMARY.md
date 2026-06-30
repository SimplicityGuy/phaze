---
phase: 53-s3-object-staging-leg
plan: 04
subsystem: cloud-staging
tags: [s3, multipart, control-plane, producer, callbacks, saq, idempotency, cloud-burst, object-staging]

# Dependency graph
requires:
  - phase: 53-s3-object-staging-leg
    plan: 01
    provides: "CloudJob/CloudJobStatus model + S3 ControlSettings (part-size knob, push_max_attempts cap)"
  - phase: 53-s3-object-staging-leg
    plan: 02
    provides: "s3_staging control-plane ops (create/presign/complete/abort/delete) + staged_object_key"
  - phase: 53-s3-object-staging-leg
    plan: 03
    provides: "UploadFileS3Payload + Uploaded/UploadFailed schemas + s3_upload task name (single enqueue seam) + UPLOAD_FILE_SAQ_TIMEOUT_SEC + s3_upload:<file_id> ledger key"
  - phase: 50-cloud-push-pipeline
    provides: "agent_push producer/callback idiom (queue_for->connect->enqueue, rowcount-guarded flip, ledger attempt-counter re-drive)"
provides:
  - "cloud_staging.stage_file_to_s3 producer (the upload-trigger seam): cloud_job upsert + multipart init + part presign + single s3_upload enqueue through the per-agent seam (KSTAGE-01)"
  - "cloud_staging.redrive_upload helper: best-effort abort of the prior multipart + re-stage with a fresh upload"
  - "agent_s3 router: /uploaded (control completes the multipart + idempotent UPLOADING->UPLOADED flip) + /failed (bounded re-drive then terminal abort+delete) callbacks (KSTAGE-01/04)"
  - "agent_s3.router mounted on the app"
affects: [55-stage-cloud-window-routing]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Control plane completes the multipart upload itself (DIST-01/KSTAGE-01): the agent reports (part_number, etag) pairs; control calls complete_multipart_upload — the agent never touches the bucket"
    - "Idempotent state flip = status pre-check + rowcount-guarded UPDATE WHERE status==UPLOADING (a duplicate/late callback never re-completes the object, T-53-15)"
    - "Producer idempotency via pg_insert ON CONFLICT (file_id) DO UPDATE against the unique CloudJob FK"
    - "Re-drive attempt counter rides the s3_upload:<file_id> ledger payload JSONB (migration-free, capped at push_max_attempts)"

key-files:
  created:
    - src/phaze/services/cloud_staging.py
    - src/phaze/routers/agent_s3.py
    - tests/test_services/test_cloud_staging.py
    - tests/test_routers/test_agent_s3.py
  modified:
    - src/phaze/main.py

key-decisions:
  - "Reused ControlSettings.push_max_attempts as the upload re-drive cap (Plan 01 added no s3-specific cap; the bounded-loop semantics are identical to the push leg)"
  - "Re-drive attempt counter field named s3_upload_attempt in the ledger payload (parallels the push leg's push_attempt)"
  - "report_uploaded guards completion on a status pre-check (status==UPLOADING and upload_id present) BEFORE calling complete_multipart_upload, plus the rowcount-guarded flip — so a duplicate callback neither re-completes nor double-flips"

patterns-established:
  - "stage_file_to_s3 resolves the fileserver agent FIRST so NoActiveAgentError is a clean hold with no half-written cloud_job (nothing committed before the gate)"
  - "router mount asserted via app.openapi()['paths'] (FastAPI's _IncludedRouter wrappers hide paths from app.routes in this version)"

requirements-completed: [KSTAGE-01, KSTAGE-04]

# Metrics
duration: ~50min
completed: 2026-06-27
---

# Phase 53 Plan 04: Control-plane S3-staging producer + callbacks Summary

**The control-side orchestration of the S3 object-staging upload leg: a `cloud_staging` producer that stages a file (cloud_job upsert + multipart init + part presign + one `s3_upload` enqueue through the single per-agent seam) and a re-drive helper, plus the `agent_s3` callbacks that complete the multipart upload control-side (KSTAGE-01 — never the agent), flip `cloud_job` state idempotently, and run the bounded re-drive / terminal-cleanup loop (abort + delete, KSTAGE-04). The producer is the upload-trigger seam, built and unit-tested here but unwired from the live routing seam (Phase 55 owns that).**

## Performance

- **Duration:** ~50 min
- **Completed:** 2026-06-27
- **Tasks:** 2 (both TDD)
- **Files created:** 4
- **Files modified:** 1

## Accomplishments
- `cloud_staging.stage_file_to_s3(session, file, task_router)` — the upload-trigger seam (KSTAGE-01/D-01): resolves the active **fileserver** agent (NoActiveAgentError propagates for a clean hold), initiates the multipart upload, presigns `part_count = max(1, ceil(file_size / s3_multipart_part_size_bytes))` PUT URLs, upserts the `cloud_job` row ON CONFLICT (file_id) DO UPDATE (idempotent against the unique FK), and enqueues exactly one `s3_upload` job through the per-agent seam with the deterministic `s3_upload:<file_id>` key and the explicit `UPLOAD_FILE_SAQ_TIMEOUT_SEC` job-net timeout.
- `cloud_staging.redrive_upload(...)` — best-effort abort of the prior multipart (suppresses cleanup failures: the upload may already be gone) then re-stage with a fresh multipart; idempotent on the cloud_job FK.
- `agent_s3` router (`/api/internal/agent/s3`, mirrors `agent_push`):
  - `POST /{file_id}/uploaded` — completes the multipart upload **control-side** (KSTAGE-01/DIST-01) with the agent-reported `(part_number, etag)` pairs, then flips `cloud_job` `UPLOADING→UPLOADED` with a status pre-check + rowcount guard. A duplicate/late callback (already UPLOADED) is an idempotent 200 that does **not** re-complete the object (T-53-15).
  - `POST /{file_id}/failed` — reads the `s3_upload_attempt` counter from the `s3_upload:<file_id>` ledger payload; under `push_max_attempts` re-drives (`redrive_upload`) keeping `cloud_job` UPLOADING and stamps the incremented counter (cleared=False); at the cap sets `cloud_job` FAILED, aborts the multipart, deletes the staged object, clears the ledger (cleared=True, KSTAGE-04/T-53-17). No fileserver online → clean 200 hold (NoActiveAgentError caught), never a 500 (T-53-19).
- Router mounted in `main.py` alongside the other `agent_*` routers.
- AUTH-01 enforced: `file_id` on the PATH, identity from the token, request bodies carry no identity (`extra="forbid"`, T-53-18).

## Task Commits

Each TDD task committed RED → GREEN:

1. **Task 1: cloud_staging producer + re-drive (RED)** — `ac41cac` (test)
2. **Task 1: cloud_staging producer + re-drive (GREEN)** — `3dfa11a` (feat)
3. **Task 2: agent_s3 callbacks + router mount (RED)** — `e7f50b0` (test)
4. **Task 2: agent_s3 callbacks + router mount (GREEN)** — `e00cef7` (feat)

_No REFACTOR commits — both implementations were minimal-and-clean at GREEN._

## Files Created/Modified
- `src/phaze/services/cloud_staging.py` — the producer (`stage_file_to_s3`) + re-drive helper (`redrive_upload`); ORM + queue orchestration only (all S3 SDK calls delegated to `s3_staging`)
- `src/phaze/routers/agent_s3.py` — `/uploaded` + `/failed` control-side callbacks (multipart complete, idempotent flip, bounded re-drive, terminal abort+delete)
- `src/phaze/main.py` — `agent_s3` import + `app.include_router(agent_s3.router)`
- `tests/test_services/test_cloud_staging.py` — 5 moto-server + DB tests (end-to-end stage, deterministic key + timeout, idempotent FK, clean NoActiveAgentError hold, re-drive)
- `tests/test_routers/test_agent_s3.py` — 8 tests (control-side complete + flip, idempotent duplicate no-recomplete, extra=forbid 422, 401, under-cap re-drive + counter, at-cap terminal cleanup, router mounted)

## Decisions Made
- **Reused `push_max_attempts` as the upload re-drive cap.** Plan 01 added no s3-specific cap, and the bounded-loop semantics are identical to the push leg (a capped re-drive then terminal failure). Introducing a redundant `s3_upload_max_attempts` for the same concern was not warranted; the plan explicitly allowed "(or the Plan-01 s3 cap)".
- **`report_uploaded` guards completion with a status pre-check before the rowcount-guarded flip.** The plan's literal ordering (complete first, then check rowcount) would re-complete the multipart on a duplicate callback — contradicting the "duplicate does NOT re-complete" behavior. Loading the cloud_job and returning an idempotent 200 when `status != UPLOADING` (or `upload_id is None`) prevents the re-complete on the common duplicate; the rowcount-guarded UPDATE WHERE status==UPLOADING still defends the concurrent-duplicate race. Both the pre-check and the `rowcount == 0` guard are present.
- **Router-mount test uses `app.openapi()['paths']`.** This FastAPI/Starlette version wraps included routers in `_IncludedRouter` objects with no `.path`, so `app.routes` does not surface the s3 paths directly; the OpenAPI schema is the robust mount assertion.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] `report_uploaded` ordering: pre-check status before completing the multipart**
- **Found during:** Task 2
- **Issue:** The plan's literal step order completes the multipart, then checks the flip rowcount. On a duplicate `/uploaded` callback (cloud_job already UPLOADED) that would call `complete_multipart_upload` again before the rowcount check — violating the plan's own "duplicate does NOT re-complete the multipart" behavior (T-53-15).
- **Fix:** Load the cloud_job and return an idempotent 200 when `status != UPLOADING` (or no `upload_id`) BEFORE completing; keep the rowcount-guarded UPDATE for the concurrent-duplicate race.
- **Files modified:** src/phaze/routers/agent_s3.py
- **Commit:** e00cef7

**2. [Rule 3 - Blocking] Router-mount test via the OpenAPI schema, not `app.routes`**
- **Found during:** Task 2 (mount test)
- **Issue:** `app.routes` yields `_IncludedRouter` wrappers (no `.path`) in this FastAPI version, so the planned `{route.path ...}` assertion raised `AttributeError` / found nothing.
- **Fix:** Assert against `app.openapi()["paths"]`, which reflects the mounted routes regardless of the wrapper type.
- **Files modified:** tests/test_routers/test_agent_s3.py
- **Commit:** e00cef7

---

**Total deviations:** 2 auto-fixed (1 correctness bug in the callback ordering, 1 blocking test-harness fix). No scope creep; no production surface beyond the planned producer + callbacks + mount.

## Threat Surface Coverage
- **T-53-15** (duplicate/late callback clobbering state): status pre-check + rowcount-guarded `UPLOADING→UPLOADED` flip; a duplicate is an idempotent 200 with no re-complete (test-verified).
- **T-53-16** (unbounded re-drive loop): `s3_upload_attempt` ledger counter capped at `push_max_attempts` → terminal FAILED (test-verified at the cap).
- **T-53-17** (orphaned multipart / leaked object): terminal path calls `abort_multipart_upload` + `delete_staged_object` (test-verified); the Plan-02 lifecycle TTL is the backstop.
- **T-53-18** (forged identity in callback body): `file_id` on the PATH, identity from the token, `extra="forbid"` rejects identity in the body (422, test-verified).
- **T-53-19** (unmounted route / silent 500): router mounted + tested; NoActiveAgentError → clean 200 hold, never 500 (test-verified).

## Issues Encountered
- The route/service tests need ephemeral Postgres+Redis (and a real moto S3 server for the producer's presign). Ran against the shared `phaze-test-db`/`phaze-test-redis` containers (ports 5433/6380) with `TEST_DATABASE_URL`/`MIGRATIONS_TEST_DATABASE_URL`/`PHAZE_REDIS_URL` exported, per the parallel-executor harness note (containers left running for the sibling wave agent).
- A test helper's `session.expire_all()` expired the live `file` instance and broke `redrive_upload`'s lazy attribute access (the session fixture uses `expire_on_commit=False`); switched the helper to `execution_options(populate_existing=True)` to refresh only the queried row.

## Verification
- `uv run pytest tests/test_services/test_cloud_staging.py tests/test_routers/test_agent_s3.py` — **13 passed**.
- Adjacent + boundary: `tests/test_task_split.py tests/test_services/test_s3_staging.py tests/test_routers/test_agent_push.py` — green (41 passed in the combined run).
- Regression sweep: `tests/test_routers/ tests/test_tasks/test_scan_directory.py tests/test_deterministic_key.py` — **606 passed**.
- `uv run ruff check .` clean; `uv run mypy .` — no issues in 177 source files.
- Acceptance greps all satisfied; the producer is NOT referenced from the live cloud-window routing seam (Phase 55 wires it).

## Next Phase Readiness
- Phase 55 can now wire `cloud_staging.stage_file_to_s3` into the cloud-window routing seam (`stage_cloud_window`) to trigger uploads, and the full upload leg round-trips: control presigns → agent PUTs bytes (Plan 03) → control completes the multipart + flips state (this plan) → terminal cleanup on failure.
- No blockers.

## Self-Check: PASSED

All 4 created files verified present; all 4 task commits (`ac41cac`, `3dfa11a`, `e7f50b0`, `e00cef7`) verified in git log.

---
*Phase: 53-s3-object-staging-leg*
*Completed: 2026-06-27*
