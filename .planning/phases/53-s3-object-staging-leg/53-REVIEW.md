---
phase: 53-s3-object-staging-leg
reviewed: 2026-06-27T00:00:00Z
depth: standard
files_reviewed: 17
files_reviewed_list:
  - alembic/versions/025_add_cloud_job.py
  - pyproject.toml
  - src/phaze/config.py
  - src/phaze/main.py
  - src/phaze/models/__init__.py
  - src/phaze/models/cloud_job.py
  - src/phaze/routers/agent_analysis.py
  - src/phaze/routers/agent_files.py
  - src/phaze/routers/agent_s3.py
  - src/phaze/schemas/agent_s3.py
  - src/phaze/services/agent_client.py
  - src/phaze/services/cloud_staging.py
  - src/phaze/services/enqueue_router.py
  - src/phaze/services/s3_staging.py
  - src/phaze/tasks/_shared/deterministic_key.py
  - src/phaze/tasks/agent_worker.py
  - src/phaze/tasks/s3_upload.py
findings:
  critical: 2
  warning: 4
  info: 1
  total: 7
status: resolved
---

# Phase 53: Code Review Report

**Reviewed:** 2026-06-27
**Depth:** standard
**Files Reviewed:** 17
**Status:** issues_found

## Summary

Phase 53 introduces the S3 object-staging leg: control-plane multipart presign/complete/delete via aioboto3, a file-server agent upload task over httpx, upload-outcome callbacks, and the `cloud_job` sidecar table. The overall architecture is sound — DIST-01 boundary respected, AUTH-01 discipline consistent, `_FILE` secrets wired correctly, lifecycle TTL backstop configured, and the Phase 50 push-pipeline pattern cleanly adapted.

Two blockers were found: the `cloud_job` PK is missing from every `pg_insert` call (first-ever staging always crashes), and `abort_multipart_upload` is not idempotent (the terminal cleanup path can enter a permanent 500-retry loop leaving files stuck). Four warnings cover related partial-failure stuck states, a ledger-payload clobbering that corrupts recovery replay, a missing existence guard on `presign_download`, and an unvalidated ETag that can silently break `CompleteMultipartUpload`.

## Critical Issues

### CR-01: `pg_insert(CloudJob).values(...)` omits `id` PK — always fails on first INSERT

**File:** `src/phaze/services/cloud_staging.py:80-94`

**Issue:** `stage_file_to_s3` calls `pg_insert(CloudJob).values(file_id=..., s3_key=..., status=..., upload_id=...)` with no `id` field. The `CloudJob.id` mapped column has only a Python-side `default=uuid.uuid4`; SQLAlchemy explicitly does NOT invoke Python-level defaults for dialect-specific `INSERT ... ON CONFLICT DO UPDATE` constructs executed through `session.execute()` (this is documented by the project itself at `agent_analysis.py:181-182` — the identical pattern for `AnalysisResult.id` was already hit and fixed there). The `cloud_job.id` column is `nullable=False` with no server-side default in the migration, so the first INSERT for any file produces:

```
asyncpg.exceptions.NotNullViolationError: null value in column "id" of relation "cloud_job" violates not-null constraint
```

Subsequent calls (re-drives) would find the existing row and take the ON CONFLICT UPDATE path, so the bug only manifests on the first staging of each file — but that is every file.

**Fix:**
```python
stmt = pg_insert(CloudJob).values(
    id=uuid.uuid4(),          # ← add this; Python default does not fire on pg_insert
    file_id=file.id,
    s3_key=s3_staging.staged_object_key(file.id),
    status=CloudJobStatus.UPLOADING.value,
    upload_id=upload_id,
)
```

Also add `import uuid` at the top of `cloud_staging.py` (currently absent from that module's imports).

---

### CR-02: `abort_multipart_upload` is not idempotent — terminal cleanup creates a permanent 500-retry loop

**File:** `src/phaze/services/s3_staging.py:137-143` and `src/phaze/routers/agent_s3.py:141-143`

**Issue:** `s3_staging.abort_multipart_upload` propagates any `ClientError` unwrapped, including `NoSuchUpload` (returned by S3 when the multipart upload ID is already gone — aborted, completed, or expired by the bucket lifecycle rule). Compare with `delete_staged_object`, which explicitly swallows `_DELETE_ABSENT_CODES = {"NoSuchKey", "NoSuchUpload", "404"}`.

The terminal-failure path in `report_upload_failed` calls `abort_multipart_upload` without a try/except:

```python
# agent_s3.py:139-143 (terminal path, over the cap)
await session.execute(update(CloudJob)...values(status=CloudJobStatus.FAILED.value))
if cloud_job is not None and cloud_job.upload_id:
    await s3_staging.abort_multipart_upload(file_id, cloud_job.upload_id)  # ← unguarded
await s3_staging.delete_staged_object(file_id)
await clear_ledger_entry(session, ledger_key)
await session.commit()
```

If the multipart is already absent, the unhandled `ClientError` propagates. FastAPI returns 500, the session is never committed (cloud_job stays UPLOADING, ledger is not cleared), and the agent's tenacity retry policy re-POSTs the same callback. The ledger count is unchanged, so `next_attempt` is the same over-cap value every retry. Each retry hits the same failing abort → 500 again. After tenacity exhausts its 3 attempts the SAQ job fails, leaving the file permanently stuck in `UPLOADING` with the ledger intact, requiring manual intervention.

The condition is reachable via a prior partial run: if a previous terminal cleanup aborted the multipart (S3-side success) but failed before committing (DB-side failure), the next retry finds the multipart already gone.

**Fix in `s3_staging.py`** — make `abort_multipart_upload` idempotent, mirroring `delete_staged_object`:

```python
async def abort_multipart_upload(file_id: uuid.UUID, upload_id: str) -> None:
    """Abort an in-flight multipart upload; idempotent — a missing upload is the desired end state."""
    cfg = _staging_config()
    key = staged_object_key(file_id)
    async with _client(cfg) as client:
        try:
            await client.abort_multipart_upload(Bucket=cfg.s3_bucket, Key=key, UploadId=upload_id)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in _DELETE_ABSENT_CODES:   # "NoSuchUpload" is already in the set
                return
            raise S3StagingError(f"failed to abort multipart upload for {file_id}") from exc
```

---

## Warnings

### WR-01: `complete_multipart_upload` is not idempotent — partial failure creates a permanent stuck state

**File:** `src/phaze/routers/agent_s3.py:81` and `src/phaze/services/s3_staging.py:124-134`

**Issue:** In `report_uploaded`, `complete_multipart_upload` is called before the DB commit. If S3 `CompleteMultipartUpload` succeeds but the handler fails before committing (network blip, transient DB error), the agent's tenacity retry re-POSTs the callback. The retry reads `cloud_job.status == UPLOADING` (unchanged — the update was rolled back) and calls `complete_multipart_upload` again. S3 invalidates the UploadId after a successful completion; the second call returns `NoSuchUpload`, which `s3_staging.complete_multipart_upload` propagates unwrapped as a `ClientError`. FastAPI returns 500, the agent retries, same result — the file is permanently stuck in `UPLOADING` while the S3 object IS assembled (bytes not leaked, but the pipeline stalls).

**Fix:** Add idempotency handling in `s3_staging.complete_multipart_upload` for the "already-completed" case. After a successful `CompleteMultipartUpload`, the key exists as a regular object. A `HEAD` or a conditional check on the error code can distinguish "upload completed" from a genuine failure:

```python
async def complete_multipart_upload(...) -> None:
    ...
    async with _client(cfg) as client:
        try:
            await client.complete_multipart_upload(...)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            # NoSuchUpload after the object already exists means a prior completion succeeded.
            if code in ("NoSuchUpload", "404"):
                return   # idempotent — treat already-completed as success
            raise S3StagingError(f"failed to complete multipart upload for {file_id}") from exc
```

---

### WR-02: Re-drive ledger update clobbers fresh presigned URLs with stale ones

**File:** `src/phaze/routers/agent_s3.py:173-175`

**Issue:** In `report_upload_failed`'s under-cap path, `base_payload` is read from the `SchedulingLedger` row BEFORE `redrive_upload` is called:

```python
row = (await session.execute(select(SchedulingLedger)...)).scalar_one_or_none()
# ... row.payload here = {OLD file_id, OLD part_urls, OLD agent_id, ...}

await cloud_staging.redrive_upload(session, file, request.app.state.task_router)
# ↑ calls stage_file_to_s3, which: initiates a NEW multipart, presigns NEW part_urls,
#   commits cloud_job upsert, and in the before_enqueue hook writes NEW part_urls to
#   the ledger via its own inner session (committed separately).

base_payload = dict(row.payload) if ... else {}      # row.payload = stale OLD payload
merged = {**base_payload, "s3_upload_attempt": next_attempt}
await session.execute(update(SchedulingLedger)...values(payload=merged))
await session.commit()
# Final ledger payload = {OLD part_urls} + {s3_upload_attempt: N}
# This overwrites the fresh {NEW part_urls} the before_enqueue hook just committed.
```

If the recovery mechanism replays the ledger entry for `s3_upload:<file_id>`, it re-enqueues the upload with the OLD (expired) presigned URLs. The re-enqueued job fails immediately on every part PUT (403 Forbidden from S3 on an expired presigned URL), triggering another `/failed` callback and consuming an extra attempt.

**Fix:** Use a SQL JSONB merge expression instead of a Python-level overwrite, so the attempt counter is stamped ON TOP of whatever the before_enqueue hook wrote:

```python
from sqlalchemy.dialects.postgresql import JSONB
# ...
await session.execute(
    update(SchedulingLedger)
    .where(SchedulingLedger.key == ledger_key)
    .values(payload=func.jsonb_set(
        SchedulingLedger.payload,
        cast(["s3_upload_attempt"], ARRAY(TEXT)),
        cast(next_attempt, JSONB),
        True,   # create_missing=True
    ))
)
```

Or, re-fetch the row after the redrive commits:
```python
await cloud_staging.redrive_upload(session, file, request.app.state.task_router)
# Re-read the row now that the inner session has committed fresh part_urls.
refreshed_row = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == ledger_key))).scalar_one_or_none()
base_payload = dict(refreshed_row.payload) if (refreshed_row is not None and isinstance(refreshed_row.payload, dict)) else {}
merged = {**base_payload, "s3_upload_attempt": next_attempt}
```

---

### WR-03: `presign_download` mints a GET URL without verifying the staged object is ready

**File:** `src/phaze/routers/agent_files.py:160-165`

**Issue:** `presign_download` verifies the `FileRecord` exists but does not check for a `cloud_job` row with `status = UPLOADED`. The presign is purely computational (no S3 network call); it always succeeds, returning a well-formed but potentially dead URL if the object does not exist in the bucket (object deleted by inline cleanup, by Phase 54 eviction cleanup, or by the lifecycle TTL between staging and pod admission).

A pod that calls this at startup and then downloads to a 403/404 from S3 will report `analysis_failed` for the file, consuming one analysis attempt and leaving the file in `ANALYSIS_FAILED` state — a confusing failure that does not distinguish "bytes were never staged" from "analysis crashed."

**Fix:** Add a guard before calling `presign_get`:

```python
cloud_job_status = (
    await session.execute(
        select(CloudJob.status).where(CloudJob.file_id == file_id)
    )
).scalar_one_or_none()

if cloud_job_status != CloudJobStatus.UPLOADED.value:
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"staged object not ready (cloud_job status={cloud_job_status!r})",
    )
```

This converts the silent 403/404 from S3 into a clear 409 at the control plane, where the pod or Phase 54 reconcile can act on it.

---

### WR-04: `UploadedPart.etag` accepts empty string — silently breaks `CompleteMultipartUpload`

**File:** `src/phaze/schemas/agent_s3.py:79`

**Issue:** `UploadedPart.etag` is declared as `etag: str` with no length constraint. The upload task strips ETag quotes:

```python
# s3_upload.py:104
etag = response.headers.get("ETag", "").strip('"')
```

If the S3 presigned-PUT response omits the `ETag` header (non-compliant backend, SSRF to a non-S3 server, or part PUT that silently returned 2xx without a header), `etag` is `""`. This passes `UploadedPart` validation. The control plane then calls `CompleteMultipartUpload` with `{"ETag": ""}` per part, which AWS and MinIO both reject with a 400, causing the entire upload to fail.

**Fix:** Add `min_length=1` to the `etag` field, and log or reject an empty ETag in the upload task before it reaches the callback:

```python
class UploadedPart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    part_number: int = Field(ge=1)
    etag: str = Field(min_length=1)  # ETag must be non-empty (S3 always returns one)
```

In `s3_upload.py`, surface the missing header as a retryable error rather than silently passing `""`:

```python
etag = response.headers.get("ETag", "").strip('"')
if not etag:
    raise RuntimeError(
        f"upload_file_s3: part {part_number} PUT returned no ETag for file_id={payload.file_id}"
    )
```

---

## Info

### IN-01: Direct import of transitive dependency `aiobotocore`

**File:** `src/phaze/services/s3_staging.py:24`

**Issue:** `from aiobotocore.config import AioConfig` imports directly from `aiobotocore`, which is a transitive dependency of `aioboto3` — not declared as a direct dependency in `pyproject.toml`. If `aioboto3` changes its `aiobotocore` pinning or if uv resolves a different version, the import can break silently without any pyproject.toml signal.

**Fix:** Add `aiobotocore` as an explicit direct dependency:

```toml
"aiobotocore>=2.21.0",   # direct import in s3_staging.py (AioConfig)
```

Check the version with `uv pip show aiobotocore` in the project's virtual environment and pin the lower bound to the currently resolved version.

---

## Resolution

**Resolved:** 2026-06-27. All six actionable findings fixed; the one info finding (IN-01) was
deliberately deferred. Full suite green (2352 passed, 0 failures), `ruff check .` clean, `mypy .`
clean. Each fix is an atomic `fix(53):` commit with a focused regression test.

| Finding | Disposition | Commit |
|---------|-------------|--------|
| CR-01 | **Verified false positive** -- the single-row kwargs form of `pg_insert` DOES fire `CloudJob.id`'s Python default (integration test creates+reads against real Postgres and passes, so no current crash). Defensive explicit `id=uuid.uuid4()` stamp applied anyway for consistency with the `agent_analysis.py` precedent and robustness against a future list/multi-values conversion. | `df8967f` |
| CR-02 | Fixed -- `abort_multipart_upload` made idempotent (swallow `NoSuchUpload`/`404` via `_ABORT_ABSENT_CODES`, re-raise other `ClientError`). | `02495c1` |
| WR-01 | Fixed -- `complete_multipart_upload` made idempotent (same swallow). Confirmed the `report_uploaded` status pre-check does NOT fully prevent the double-call (an S3-success/DB-failure retry re-reads `UPLOADING`), so the swallow is the real fix, not merely defensive. | `1a2279d` |
| WR-02 | Fixed -- re-fetch the ledger row after `redrive_upload` commits its fresh payload and build `merged` on the fresh `part_urls` (READ COMMITTED + `populate_existing`), instead of stamping the attempt onto the stale top-of-handler snapshot. | `1f770de` |
| WR-03 | Fixed -- `presign_download` now requires a `CloudJob` with `status == UPLOADED`, else 409 (`file_id` path-only, AUTH-01; no per-agent ownership predicate -- single-user, by design). | `8a8ef7e` |
| WR-04 | Fixed -- `UploadedPart.etag` enforces `min_length=1`. | `5c3e3c2` |
| IN-01 | **Skipped (info-only).** The direct `from aiobotocore.config import AioConfig` import is a transitive-dependency hygiene nit, not a defect; deferred rather than churn `pyproject.toml` + `uv.lock` in this fix pass. Worth picking up as a standalone dependency-hardening change. | (none) |

---

_Reviewed: 2026-06-27_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
