---
phase: 53-s3-object-staging-leg
plan: 02
subsystem: cloud-burst
tags: [s3, aioboto3, moto, presign, multipart, fastapi, control-plane, object-staging]

# Dependency graph
requires:
  - phase: 53-s3-object-staging-leg
    plan: 01
    provides: "S3 ControlSettings config surface (endpoint/bucket/region/addressing/creds + bounded TTL/part knobs) + CloudJob sidecar + aioboto3/moto deps"
  - phase: 52-job-runner-image-one-shot-entrypoint
    provides: "PresignDownloadResponse schema + request_download_url client contract the GET route completes"
provides:
  - "s3_staging service: 7 control-plane aioboto3 ops (multipart init/presign-parts/complete/abort, just-in-time presigned GET, idempotent delete, lifecycle TTL backstop) — DIST-01/KSTAGE-01/03/04"
  - "staged_object_key(file_id) — the deterministic file_id-scoped key (single object identity, reconcile-by-file_id)"
  - "POST /api/internal/agent/files/{file_id}/presign-download — server side of the Phase 52 pod client"
affects: [54-kueue-submit-reconcile, 55-stage-cloud-window-routing, s3-upload-task, agent_s3-callbacks, agent_analysis-inline-delete]

# Tech tracking
tech-stack:
  added: ["moto[server] extra (flask/werkzeug) — ThreadedMotoServer for aioboto3 round-trip tests"]
  patterns:
    - "All S3 SDK calls confined to s3_staging.py (DIST-01): the control plane orchestrates, the agent/pod transfer bytes over presigned URLs only"
    - "ThreadedMotoServer (real HTTP) for aioboto3 tests — mock_aws is incompatible with aiobotocore async response parsing"
    - "expected_sha256 sourced server-side from FileRecord.sha256_hash, never echoed from the request (AUTH-01/T-53-06)"

key-files:
  created:
    - src/phaze/services/s3_staging.py
    - tests/test_services/test_s3_staging.py
    - tests/test_routers/test_agent_presign_download.py
  modified:
    - src/phaze/routers/agent_files.py
    - pyproject.toml
    - uv.lock

key-decisions:
  - "Enabled the moto[server] extra (Rule 3 blocking fix): mock_aws cannot drive aioboto3 (aiobotocore awaits response.content), so the plan's mandated presigned-URL round-trip test needs a wire-compatible ThreadedMotoServer (flask/werkzeug)"
  - "generate_presigned_url is awaited (coroutine under the real moto server) — matches the plan interface; the SDK presign is async via aiobotocore"
  - "presign GET TTL asserted via SigV2 absolute Expires epoch (botocore default) with an X-Amz-Expires fallback for SigV4 backends"

patterns-established:
  - "_staging_config() fail-loud guard (S3StagingError) before any client build when bucket/endpoint unset"
  - "delete_staged_object swallows NoSuchKey/NoSuchUpload/404 ClientError codes for idempotency; re-raises other failures as S3StagingError"

requirements-completed: [KSTAGE-01, KSTAGE-03]

# Metrics
duration: ~55min
completed: 2026-06-27
---

# Phase 53 Plan 02: S3 staging service + presign-download route Summary

**The control-plane S3 capability heart of the phase: a pure-aioboto3 `s3_staging` service (multipart init, per-part presign, complete/abort, just-in-time presigned GET, idempotent delete, lifecycle TTL backstop) plus the server-side presign-download route that completes the Phase 52 pod download contract — all S3 SDK calls confined to one control-plane module (DIST-01), tested against a real moto S3 server.**

## Performance

- **Duration:** ~55 min
- **Completed:** 2026-06-27
- **Tasks:** 2 (both TDD)
- **Files created:** 3
- **Files modified:** 3

## Accomplishments
- `s3_staging.py`: 7 stateless control-plane async ops — `create_multipart_upload`, `presign_upload_parts`, `complete_multipart_upload`, `abort_multipart_upload`, `presign_get`, `delete_staged_object`, `ensure_bucket_lifecycle_ttl` (KSTAGE-01/03/04, DIST-01).
- `staged_object_key(file_id)` → `phaze-staging/{file_id}`: the deterministic, file_id-scoped single object identity (reconcile-by-file_id, KSTAGE-04).
- aioboto3 client built from the operator's `ControlSettings` S3 surface (endpoint/bucket/region/addressing + SecretStr creds, region falls back to `us-east-1` for SigV4 presigning) — works against ANY S3-compatible backend (KSTAGE-05); secrets never logged.
- `delete_staged_object` is idempotent (swallows NoSuchKey/NoSuchUpload/404); `ensure_bucket_lifecycle_ttl` scopes an `Expiration` rule to the `phaze-staging/` prefix (KSTAGE-04 backstop, D-02).
- `POST /api/internal/agent/files/{file_id}/presign-download`: completes the Phase 52 client `request_download_url` — mints a fresh short-TTL presigned GET just-in-time (KSTAGE-03) and returns `expected_sha256` sourced server-side from `FileRecord.sha256_hash` (T-53-06/D-04); unknown file_id → clean 404; AUTH-01 (file_id on PATH, identity from token, no body).
- DIST-01 verified: aioboto3/botocore imports appear ONLY in `s3_staging.py`. Agent import-boundary (`test_task_split.py`) still green.

## Task Commits

Each task committed RED → GREEN (plus one blocking-fix chore):

0. **Blocking-fix: moto[server] extra** - `1b8412b` (chore)
1. **Task 1: s3_staging service (RED)** - `05a105f` (test)
2. **Task 1: s3_staging service (GREEN)** - `8341c85` (feat)
3. **Task 2: presign-download route (RED)** - `aad17ef` (test)
4. **Task 2: presign-download route (GREEN)** - `dd0ec1d` (feat)

_No REFACTOR commits — both implementations were minimal-and-clean at GREEN._

## Files Created/Modified
- `src/phaze/services/s3_staging.py` - control-plane aioboto3 staging service (7 ops + key builder + fail-loud config guard)
- `src/phaze/routers/agent_files.py` - added the `presign-download` route + imports (PresignDownloadResponse, s3_staging)
- `pyproject.toml` - `moto[server]` extra + mypy `ignore_missing_imports` override for aioboto3/aiobotocore/botocore
- `uv.lock` - resolved flask/werkzeug (and transitive moto server deps) under the existing supply-chain cooldown
- `tests/test_services/test_s3_staging.py` - 10 moto-server-backed tests (round-trip, TTL bound, abort, idempotent delete, lifecycle, fail-loud)
- `tests/test_routers/test_agent_presign_download.py` - 5 route tests (200 + server-sourced sha, schema validation, 401, 404, per-call minting)

## Decisions Made
- **moto[server] extra (Rule 3 blocking fix):** The plan mandates a moto-backed full round-trip ("PUT bytes to the URLs … get_object returns the concatenated bytes"). Empirically, moto's in-process `mock_aws` is incompatible with aiobotocore — its endpoint awaits `response.content`, which moto returns as plain `bytes`, raising `TypeError: 'bytes' object can't be awaited`. The only way to exercise aioboto3 against moto (and the only way to serve presigned URLs over real HTTP) is `ThreadedMotoServer`, which requires the `[server]` extra (flask/werkzeug). Changed the existing `moto>=5.1.0` dev dep to `moto[server]>=5.1.0` — same already-vetted package, enabling its documented server mode. Not a new/slopsquat package, so no human-verify checkpoint warranted.
- **`generate_presigned_url` is awaited:** Against the real moto server, aiobotocore's `generate_presigned_url` is a coroutine (matches the plan interface). It returned non-awaitable bytes only under the broken `mock_aws` path.
- **GET TTL assertion:** botocore's default presign is SigV2 (`Expires` = absolute epoch), so the test computes `Expires - now <= s3_presign_get_ttl_sec` (+5s clock slack) with an `X-Amz-Expires` branch for SigV4 backends.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Enabled the moto[server] extra so aioboto3 is testable against moto**
- **Found during:** Task 1 (s3_staging tests)
- **Issue:** `mock_aws` cannot drive aioboto3/aiobotocore (async response parsing awaits `response.content`, which moto returns as bytes), and presigned-URL byte transfers need a real HTTP S3 endpoint. The plan's round-trip behavior is untestable without a moto server, which the base `moto>=5.1.0` dep does not include (no flask/werkzeug).
- **Fix:** Changed `moto>=5.1.0` → `moto[server]>=5.1.0` in dev deps; `uv sync` resolved flask/werkzeug under the existing `exclude-newer` cooldown. All s3_staging tests run against `ThreadedMotoServer`.
- **Files modified:** pyproject.toml, uv.lock
- **Verification:** `from moto.server import ThreadedMotoServer` imports; 10 s3_staging tests pass
- **Committed in:** 1b8412b (chore, pre-RED)

**2. [Rule 3 - Blocking] mypy ignore_missing_imports override for the boto SDK libs**
- **Found during:** Task 1 (mypy on s3_staging.py)
- **Issue:** `aiobotocore.config` and `botocore.exceptions` ship no type stubs / py.typed, so mypy raised `import-untyped` errors on the new service.
- **Fix:** Added a `[[tool.mypy.overrides]]` block for `aioboto3*/aiobotocore*/botocore*` with `ignore_missing_imports = true` (mirrors the existing essentia/mutagen overrides).
- **Files modified:** pyproject.toml
- **Verification:** `uv run mypy .` → Success, 173 source files
- **Committed in:** 8341c85 (Task 1 GREEN)

---

**Total deviations:** 2 auto-fixed (both Rule 3 blocking: a test-substrate dep extra + a mypy stub override). No production-code deviations beyond the planned surface; no scope creep.

## Threat Surface Coverage
- **T-53-05** (presign scope/TTL): file_id-scoped key; GET bounded by short `s3_presign_get_ttl_sec`, minted just-in-time; PUT bounded by `s3_presign_put_ttl_sec`.
- **T-53-06** (sha from body): `expected_sha256` sourced ONLY from `FileRecord.sha256_hash` server-side; route accepts no body.
- **T-53-07** (unauth presign): `get_authenticated_agent` dependency → 401 on missing/invalid token (test-verified).
- **T-53-08** (file_id smuggling): file_id on URL PATH only; no body accepted.
- **T-53-09** (creds in logs): aioboto3 confined to `s3_staging.py`; `SecretStr.get_secret_value()` used only at client build; no secret values logged.
- **T-53-10** (object leak): `ensure_bucket_lifecycle_ttl` backstop + idempotent `delete_staged_object`.

## Issues Encountered
- The route tests need ephemeral Postgres+Redis. Ran against the shared `phaze-test-db`/`phaze-test-redis` containers (ports 5433/6380) with `TEST_DATABASE_URL`/`MIGRATIONS_TEST_DATABASE_URL`/`PHAZE_REDIS_URL` exported, per the parallel-executor harness note.

## Next Phase Readiness
- The control plane can now presign/complete/abort multipart uploads, mint just-in-time GETs, delete staged objects, and set the lifecycle backstop. Downstream Phase 53 plans build on this: the `s3_upload` agent task (httpx PUTs to `presign_upload_parts`), the `agent_s3` upload-complete/failure callbacks (call `complete_multipart_upload`/`abort` + `cloud_job` flip), and the `agent_analysis` inline-delete hook (call `delete_staged_object`). Phase 52's pod download contract is now fully served.
- No blockers.

## Self-Check: PASSED

All 3 created files verified present; all 5 task commits (`1b8412b`, `05a105f`, `8341c85`, `aad17ef`, `dd0ec1d`) verified in git log. Plan tests: 15 passed (10 service + 5 route); import-boundary: 9 passed. Project-wide ruff + mypy clean (173 files).

---
*Phase: 53-s3-object-staging-leg*
*Completed: 2026-06-27*
