---
phase: 53-s3-object-staging-leg
plan: 03
subsystem: cloud-staging
tags: [s3, httpx, multipart-upload, presigned-url, saq, agent, cloud-burst, object-staging]

# Dependency graph
requires:
  - phase: 53-s3-object-staging-leg
    plan: 01
    provides: "S3 ControlSettings (presign/part-size knobs), CloudJob model — the control-plane contracts the producer (Plan 04) presigns against"
  - phase: 50-cloud-push-pipeline
    provides: "push.py task shape (Postgres-free import boundary, three-layer timeout, TERMINAL-vs-retryable) + agent_client report_pushed/report_push_mismatch callback idiom + AGENT_TASKS single-enqueue seam"
provides:
  - "agent_s3 schemas: UploadFileS3Payload + UploadedPart/UploadedRequest/UploadedResponse + UploadFailedRequest/UploadFailedResponse (extra=forbid, AUTH-01)"
  - "upload_file_s3 SAQ task: httpx multipart-PUT to presigned part URLs, ETag collection (D-04), no S3 SDK/creds (KSTAGE-02)"
  - "agent_client.report_upload_complete / report_upload_failed callbacks (file_id on path)"
  - "s3_upload registered through the single enqueue seam (AGENT_TASKS + agent_worker functions) + s3_upload:<file_id> deterministic-key builder"
  - "test_task_split import boundary extended: aioboto3/botocore banned from the agent_worker graph"
affects: [53-04-control-plane-producer-callbacks, 55-stage-cloud-window-routing]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "httpx-only byte transfer to presigned URLs (agent holds no S3 SDK or bucket credentials, KSTAGE-02)"
    - "SAQ (name, func) tuple registration to decouple the enqueue task-name (s3_upload) from the function name (upload_file_s3)"
    - "per-part ETag collection with no S3-side checksums — the pod's end-to-end sha256 is the single integrity gate (D-04)"

key-files:
  created:
    - src/phaze/schemas/agent_s3.py
    - src/phaze/tasks/s3_upload.py
    - tests/test_schemas/test_agent_s3.py
    - tests/test_services/test_agent_client_upload.py
    - tests/test_tasks/test_s3_upload.py
  modified:
    - src/phaze/services/agent_client.py
    - src/phaze/tasks/agent_worker.py
    - src/phaze/services/enqueue_router.py
    - src/phaze/tasks/_shared/deterministic_key.py
    - tests/test_task_split.py
    - tests/test_tasks/test_scan_directory.py

key-decisions:
  - "Registered the task as a ('s3_upload', upload_file_s3) SAQ tuple so the function keeps the planned name upload_file_s3 while the enqueue/route name is s3_upload (the name Plan 04 enqueues by). SAQ Worker (worker.py:163) unwraps (name, func) tuples."
  - "Reused AgentSettings.push_timeout_sec as the upload transport bound + UPLOAD_FILE_SAQ_TIMEOUT_SEC layering (no new config knob; config.py is out of this plan's scope and the agent's generic byte-transfer timeout already exists)."
  - "Added the s3_upload:<file_id> deterministic-key builder so the scheduling-ledger attempt counter the Plan-04 callback re-drive loop reads actually gets written (the before_enqueue hook returns early for unkeyed tasks)."

patterns-established:
  - "Upload leg mirrors the push leg exactly: same _agent_settings() narrowing, same outer<SAQ-net timeout layering, same TERMINAL (missing source) vs retryable (non-2xx) split, same lazy-import httpx-only callbacks."

requirements-completed: [KSTAGE-02]

# Metrics
duration: ~50min
completed: 2026-06-27
---

# Phase 53 Plan 03: S3 object-staging upload leg Summary

**The agent-side upload leg (KSTAGE-02): `agent_s3` schemas, the `upload_file_s3` httpx-multipart-PUT task that streams parts to presigned URLs and reports ETags, the agent_client upload callbacks, and single-enqueue-seam registration — the byte-transfer half the control-plane presign (Plan 02) and callbacks (Plan 04) bracket, with no S3 SDK or bucket credentials on the agent.**

## Performance

- **Duration:** ~50 min
- **Completed:** 2026-06-27
- **Tasks:** 2 (both TDD)
- **Files created:** 5
- **Files modified:** 6

## Accomplishments
- `agent_s3` schemas (ORM-free + S3-SDK-free, every model `extra="forbid"`): `UploadFileS3Payload` (file_id + ordered presigned `part_urls` + `part_size_bytes` + agent_id; absolute-path + http(s) field validators), `UploadedPart`/`UploadedRequest` (ordered `(part_number, etag)` list, no identity — AUTH-01), `UploadedResponse`, `UploadFailedRequest` (bounded `detail`) + `UploadFailedResponse` (file_id/status/cleared).
- `agent_client.report_upload_complete` → `POST /api/internal/agent/s3/{file_id}/uploaded`, `report_upload_failed` → `POST /api/internal/agent/s3/{file_id}/failed` (lazy import, file_id on path, inherits the `_request` tenacity 5xx-retry/4xx-surface policy, token never logged).
- `upload_file_s3` SAQ task adapting `push.py`: streams one `part_size_bytes` chunk at a time (peak memory bounded to a single part, T-53-12), PUTs each chunk to `part_urls[N-1]` over an httpx `AsyncClient`, collects+unquotes each `ETag` (D-04), wraps the transfer in an `asyncio.wait_for` outer guard, re-raises `(TimeoutError, asyncio.CancelledError)` after reaping (no partial-success callback), raises `RuntimeError` on a non-2xx part (SAQ retry) and a TERMINAL `RuntimeError` on a missing source (no local fallback). Postgres-free **and** S3-SDK-free import boundary.
- Single enqueue seam: registered `("s3_upload", upload_file_s3)` in `agent_worker.settings["functions"]` and added `"s3_upload"` to `AGENT_TASKS` (the name Plan 04 enqueues by), plus an `s3_upload:<file_id>` deterministic-key builder.
- `test_task_split` extended: `aioboto3`/`botocore` banned from the `agent_worker` import graph (T-53-11) + a dedicated `s3_upload` boundary test.

## Task Commits

Each TDD task committed RED → GREEN:

1. **Task 1: agent_s3 schemas + upload callbacks (RED)** - `a99b8b8` (test)
2. **Task 1: agent_s3 schemas + upload callbacks (GREEN)** - `74f6561` (feat)
3. **Task 2: upload_file_s3 task + registration + boundary (RED)** - `d0649e2` (test)
4. **Task 2: upload_file_s3 task + registration + boundary (GREEN)** - `7f9efe4` (feat)
5. **Docstring SDK-token grep fix** - `2485080` (docs)
6. **scan_directory tuple-registration test fix** - `440ab7b` (test)

_Note: no REFACTOR commits; both implementations were minimal-and-clean at GREEN. Commits 5–6 are in-scope follow-ups (an acceptance-grep wording fix and a Rule-3 blocking test fix described below)._

## Files Created/Modified
- `src/phaze/schemas/agent_s3.py` - upload payload + callback request/response schemas
- `src/phaze/tasks/s3_upload.py` - the httpx multipart-PUT upload task (`upload_file_s3`)
- `src/phaze/services/agent_client.py` - `report_upload_complete` / `report_upload_failed` callbacks + TYPE_CHECKING imports
- `src/phaze/tasks/agent_worker.py` - register `("s3_upload", upload_file_s3)` in the functions list
- `src/phaze/services/enqueue_router.py` - add `"s3_upload"` to `AGENT_TASKS`
- `src/phaze/tasks/_shared/deterministic_key.py` - `s3_upload:<file_id>` key builder + comment refresh
- `tests/test_schemas/test_agent_s3.py` - schema tests (extra=forbid, AUTH-01, http(s) part-URL guard, bounds)
- `tests/test_services/test_agent_client_upload.py` - respx callback tests (file_id on path, parts body)
- `tests/test_tasks/test_s3_upload.py` - task tests (per-part PUT + ETag, non-2xx retry, cancellation re-raise, TERMINAL missing source)
- `tests/test_task_split.py` - ban aioboto3/botocore from agent_worker graph + dedicated s3_upload boundary test
- `tests/test_tasks/test_scan_directory.py` - unwrap (name, func) tuples in the functions-name assertion

## Decisions Made
- **SAQ tuple registration for name decoupling:** Plan 04 explicitly enqueues `queue.enqueue("s3_upload", ...)`, but SAQ routes by the registered task name, which for a bare callable is `func.__qualname__` (`upload_file_s3`). The plan's acceptance criteria require the function to be named `upload_file_s3` AND `enqueue_router` to contain `s3_upload`. Registering the SAQ `(name, func)` tuple `("s3_upload", upload_file_s3)` (supported at `saq/worker.py:163`) satisfies all of: function named `upload_file_s3`, SAQ/route name `s3_upload`, AGENT_TASKS mirrors the registered name, and Plan 04's `enqueue("s3_upload")` resolves correctly.
- **Reuse `push_timeout_sec` as the upload transport bound:** the agent already has a generic byte-transfer I/O timeout (`AgentSettings.push_timeout_sec`, default 600). `config.py` is out of this plan's `files_modified` scope, so the upload leg reuses it and reproduces the `UPLOAD_FILE_SAQ_TIMEOUT_SEC = 600 + 30 + 30` layering rather than introducing a redundant `s3_upload_timeout_sec` knob for the same deployment concern.
- **`s3_upload` keyed for the scheduling ledger:** the `before_enqueue` deterministic-key hook returns early for tasks absent from `_KEY_BUILDERS`, which would also skip the scheduling-ledger WRITE. Plan 04's callback re-drive loop reads the `s3_upload:<file_id>` attempt counter from that ledger row, so an `s3_upload` key builder is required (mirrors the `push_file` precedent).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Added an `s3_upload` deterministic-key builder**
- **Found during:** Task 2 (AGENT_TASKS registration)
- **Issue:** Adding `"s3_upload"` to `AGENT_TASKS` tripped the drift-guard test `test_every_routable_task_is_keyed_or_exempt` (every routable task must have a `_KEY_BUILDERS` entry or an `_UNKEYED_TASKS` exemption). Leaving it unkeyed would also skip the scheduling-ledger WRITE that Plan 04's re-drive loop depends on.
- **Fix:** Added `"s3_upload": lambda k: str(k["file_id"])` to `_KEY_BUILDERS` (mirrors `push_file`) and refreshed the surrounding comment; dashboard-counter wiring (`PIPELINE_FUNCTIONS`) is intentionally deferred to Phase 55's routing seam.
- **Files modified:** src/phaze/tasks/_shared/deterministic_key.py
- **Committed in:** 7f9efe4 (Task 2 GREEN)

**2. [Rule 3 - Blocking] Unwrapped `(name, func)` tuples in a registration assertion**
- **Found during:** post-implementation regression sweep
- **Issue:** `test_scan_directory_registered_in_agent_worker_settings` did `{f.__name__ for f in settings["functions"]}`; the new `("s3_upload", upload_file_s3)` tuple has no `__name__` (AttributeError).
- **Fix:** `{(f[0] if isinstance(f, tuple) else f.__name__) for f in settings["functions"]}`. (Other functions-list assertions already use `getattr(fn, "__name__", "")` or membership checks and were unaffected.)
- **Files modified:** tests/test_tasks/test_scan_directory.py
- **Committed in:** 440ab7b

**3. [Rule 3 - Non-blocking] Docstring wording so the SDK-token acceptance grep reads zero**
- **Found during:** Task 1 + Task 2 acceptance-grep verification
- **Issue:** The acceptance greps count literal `aioboto3|botocore|sqlalchemy|phaze.models|phaze.database` tokens in the source; my docstrings named those tokens in prose, so the grep returned >0 even though there are zero imports.
- **Fix:** Reworded the `agent_s3.py` and `s3_upload.py` module docstrings to describe the boundary without the literal tokens (the real boundary is test-enforced in `test_task_split.py`).
- **Files modified:** src/phaze/schemas/agent_s3.py, src/phaze/tasks/s3_upload.py
- **Committed in:** 74f6561 (schema), 2485080 (task)

---

**Total deviations:** 3 auto-fixed (2 blocking — one production key-builder required by the AGENT_TASKS addition + Plan-04 ledger, one test-contract update from tuple registration; 1 non-blocking docstring wording). No scope creep beyond the planned surface and its direct knock-on effects.

## Issues Encountered
- Python 3.14: `asyncio.CancelledError` is a `BaseException` (not `Exception`), so respx rejects it as a bare `side_effect` type. The cancellation test raises it from a callable `side_effect` instead.
- No local Postgres on 5432; the routing/reenqueue/scan_directory tests need the test DB. Resolved via `just test-db` (ephemeral PG on 5433 + Redis on 6380) with the matching `TEST_DATABASE_URL`/`MIGRATIONS_TEST_DATABASE_URL`/`PHAZE_REDIS_URL`.

## Verification
- `uv run pytest tests/test_schemas/test_agent_s3.py tests/test_tasks/test_s3_upload.py tests/test_services/test_agent_client_upload.py tests/test_task_split.py` — **28 passed**.
- Regression sweep (`test_deterministic_key`, `test_pipeline_counters`, `test_reenqueue`, `test_routing_seam`, `test_staging_cron`, `test_push_pipeline`, `test_services/`, `test_tasks/`, `test_schemas/`) — **1076 passed** after the scan_directory tuple fix.
- `uv run ruff check .` clean; `uv run mypy .` — no issues in 174 source files.
- `AGENT_TASKS` and `agent_worker.settings["functions"]` both carry the upload task (`s3_upload` / `upload_file_s3`).

## Next Phase Readiness
- Plan 04 (control-plane producer + callbacks) can now: build `UploadFileS3Payload`, `enqueue("s3_upload", key="s3_upload:<file_id>", timeout=UPLOAD_FILE_SAQ_TIMEOUT_SEC, ...)`, and implement the `/uploaded` + `/failed` routers that consume `UploadedRequest`/`UploadFailedRequest` (the agent client already POSTs them).
- No blockers. The agent transfers bytes httpx-only with no S3 SDK/creds; the SDK-free boundary is test-enforced.

## Self-Check: PASSED

All 5 created files verified present; all 6 task commits (`a99b8b8`, `74f6561`, `d0649e2`, `7f9efe4`, `2485080`, `440ab7b`) verified in git log.

---
*Phase: 53-s3-object-staging-leg*
*Completed: 2026-06-27*
