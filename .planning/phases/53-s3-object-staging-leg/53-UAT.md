---
status: partial
phase: 53-s3-object-staging-leg
source: [53-01-SUMMARY.md, 53-02-SUMMARY.md, 53-03-SUMMARY.md, 53-04-SUMMARY.md, 53-05-SUMMARY.md]
started: 2026-06-28T00:00:00Z
updated: 2026-06-28T00:00:00Z
---

## Current Test

[testing complete]

## Tests

### 1. S3 config surface — fail-fast, SSRF guard, bounded knobs
expected: Operator sets S3 endpoint/bucket/region/creds on the control plane only; `_FILE` secrets resolve into `SecretStr`; a non-http(s) or netloc-less `s3_endpoint_url` is rejected at startup; out-of-range presign/lifecycle/part-size ints fail fast; cloud-enabled-without-config fails fast. Agent settings carry no S3 creds.
result: pass
evidence: `tests/test_config/test_s3_settings.py` — 20/20 green (run 2026-06-28)

### 2. CloudJob sidecar model + migration 025 (reversible)
expected: `alembic upgrade head` creates the `cloud_job` per-`file_id` table (unique FK, DB-checked status, `upload_id`); `downgrade -1` cleanly removes it; the ORM model round-trips.
result: pass
evidence: `tests/test_models/test_cloud_job.py` + `tests/test_migrations/test_migration_025_cloud_job.py` — 14/14 green

### 3. Control-plane s3_staging service round-trip
expected: Against an S3 backend, the control plane initiates a multipart upload, presigns part URLs, completes (or aborts) the multipart, mints a just-in-time presigned GET, deletes the staged object idempotently, and sets a bucket lifecycle TTL backstop — all without reading file bytes.
result: pass
evidence: `tests/test_services/test_s3_staging.py` — 13/13 green (moto `ThreadedMotoServer` real round-trip)

### 4. Agent presign-download route (just-in-time GET)
expected: A pod requests `POST /api/internal/agent/files/{file_id}/presign-download` at startup and gets a fresh short-TTL presigned GET + `expected_sha256` (from `FileRecord`, not the body); unauthenticated requests 401; a not-yet-`UPLOADED` object returns 409 (readiness guard); `file_id` is path-only.
result: pass
evidence: `tests/test_routers/test_agent_presign_download.py` — 7/7 green

### 5. Agent upload leg — httpx multipart PUT, no S3 SDK/creds (DIST-01)
expected: The file-server agent uploads bytes to presigned PUT URLs over httpx (bounded memory, bounded error snippet, layered timeouts), collects per-part ETags, and posts upload-complete/upload-failed callbacks — carrying no S3 SDK and no bucket credentials. `aioboto3`/`botocore` are statically banned from the agent_worker import graph.
result: pass
evidence: `tests/test_tasks/test_s3_upload.py` + `tests/test_services/test_agent_client_upload.py` + `tests/test_schemas/test_agent_s3.py` (19/19) and `tests/test_task_split.py` DIST-01 boundary (10/10) — green

### 6. Control-side cloud_staging producer
expected: `stage_file_to_s3` resolves an online file-server agent (clean hold if none), inits the multipart, presigns the right number of parts, idempotently upserts the `cloud_job` row, and enqueues exactly one `s3_upload` job via the per-agent seam; `redrive_upload` aborts + re-stages.
result: pass
evidence: `tests/test_services/test_cloud_staging.py` — 5/5 green

### 7. Agent_s3 callback router — idempotent, bounded re-drive
expected: `/uploaded` completes the multipart control-side and flips `cloud_job` UPLOADING→UPLOADED with a rowcount guard (duplicate callback is an idempotent 200, no re-complete); `/failed` re-drives under a `push_max_attempts` cap then runs terminal abort+delete cleanup; identity in the body is rejected; the router is mounted; no-agent is a clean 200 hold, never a silent 500.
result: pass
evidence: `tests/test_routers/test_agent_s3.py` — 9/9 green (incl. the CR-02/WR-01/WR-02 idempotency regressions)

### 8. Inline staged-object delete on analysis result (D-02)
expected: After analysis, the staged S3 object is deleted on BOTH the success and terminal-failure callback paths; an all-local file (no `cloud_job` row) short-circuits with zero S3 calls; a delete error never corrupts the recorded result (record-first, log-and-swallow); `file_id` is path-only.
result: pass
evidence: `tests/test_routers/test_agent_analysis_inline_delete.py` — 5/5 green

### 9. Live end-to-end against a real deployed stack + real S3
expected: With a deployed control plane, a real S3-compatible bucket (operator `_FILE` creds), an online file-server agent, and a compute pod: a file is staged → uploaded via presigned PUT → fetched via presigned GET → analyzed → staged object deleted, with the lifecycle TTL reclaiming any orphan.
result: blocked
blocked_by: prior-phase
reason: "Deployment-gated. The cloud_staging producer is built + unit-tested but not yet wired into the live routing seam (Phase 55 owns that), and no deployed phaze stack / real S3 backend exists in this environment. Matches the manual-only item recorded in 53-VALIDATION.md (live KSTAGE-05 round-trip)."

## Summary

total: 9
passed: 8
issues: 0
pending: 0
skipped: 0
blocked: 1

## Gaps

[none — 0 issues. The single blocked item is a deployment/Phase-55 prerequisite gate, not a code defect.]
