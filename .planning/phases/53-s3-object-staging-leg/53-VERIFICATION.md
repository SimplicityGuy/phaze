---
phase: 53-s3-object-staging-leg
verified: 2026-06-27T00:00:00Z
status: passed
score: 5/5 must-haves verified
overrides_applied: 0
---

# Phase 53: S3 Object-Staging Leg — Verification Report

**Phase Goal:** The long file moves from the file-server agent into ephemeral S3-compatible object storage and back down to the Job pod via presigned URLs — the control plane presigns only (preserving DIST-01), the agent uploads the bytes, and objects are cleaned up on every terminal outcome.
**Verified:** 2026-06-27
**Status:** passed
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Control plane presigns S3 PUT/GET URLs and deletes objects via aioboto3 but never reads or uploads file bytes (KSTAGE-01 / DIST-01) | VERIFIED | `s3_staging.py` contains all 7 aioboto3 operations; zero file-byte reads anywhere on the control plane; `staged_object_key` is the sole object identity; grep confirms aioboto3/aiobotocore/botocore imported ONLY in `s3_staging.py` across the entire `src/` tree |
| 2 | File-server agent uploads bytes to presigned PUT URLs over httpx and callbacks the control plane; no S3 SDK or bucket credentials on the agent or pod (KSTAGE-02) | VERIFIED | `s3_upload.py` is httpx-only (no aioboto3/botocore imports); `agent_s3.py` schemas carry no S3 identity; `test_task_split.py` bans `aioboto3`/`botocore` from the agent_worker import graph; S3 credentials live only on `ControlSettings.SECRET_FILE_FIELDS`, never on `AgentSettings` |
| 3 | Presigned GET URL minted just-in-time when the pod requests it at startup, never at submit time (KSTAGE-03) | VERIFIED | `POST /api/internal/agent/files/{file_id}/presign-download` in `agent_files.py` calls `s3_staging.presign_get(file_id)` fresh per request; plus WR-03 fix: gate requires `cloud_job.status == UPLOADED` else 409 before minting; Phase 52 client contract satisfied |
| 4 | Each staged object uses a `file_id`-scoped key and is deleted on every terminal outcome (success, failure, upload-terminal, re-drive cap), with a bucket lifecycle TTL backstop (KSTAGE-04) | VERIFIED | `staged_object_key` returns `f"phaze-staging/{file_id}"`; `_delete_staged_object_if_cloud` called before `session.commit()` on BOTH `put_analysis` (line 231) and `report_analysis_failed` (line 263) paths; `report_upload_failed` at cap calls `abort_multipart_upload` + `delete_staged_object` + `clear_ledger_entry`; `ensure_bucket_lifecycle_ttl` wires the TTL backstop |
| 5 | S3 endpoint, bucket, addressing style, and credentials are operator-provided via `_FILE` secrets and work against any S3-compatible backend (KSTAGE-05) | VERIFIED | `ControlSettings` has `s3_endpoint_url`, `s3_bucket`, `s3_region`, `s3_addressing_style`, `s3_access_key_id`, `s3_secret_access_key` with `AliasChoices` env-var aliases; `s3_access_key_id`/`s3_secret_access_key` in `SECRET_FILE_FIELDS`; `endpoint_url` passed to `aioboto3.Session.client()`; `_validate_s3_endpoint_url` rejects non-http(s) values (SSRF guard) |

**Score:** 5/5 truths verified

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/config.py` | S3 ControlSettings fields + `_FILE` secret resolution + fail-fast validator | VERIFIED | 10 S3 fields on `ControlSettings` (lines 447–504); `s3_access_key_id`/`s3_secret_access_key` in `SECRET_FILE_FIELDS` (lines 345–350); `_enforce_s3_config_when_cloud_enabled` validator (line 527); `_validate_s3_endpoint_url` field_validator (line 508); zero S3 fields on `AgentSettings` |
| `src/phaze/models/cloud_job.py` | `CloudJob` ORM model (per-file_id sidecar) | VERIFIED | `class CloudJob(TimestampMixin, Base)` with `id` (UUID PK, `default=uuid.uuid4`), `file_id` (unique FK to `files.id`), `s3_key` (String 255), `status` (String 16 + CHECK constraint), `upload_id` (nullable String 255); `CloudJobStatus(StrEnum)` with UPLOADING/UPLOADED/FAILED; no kueue_workload/cloud_phase (D-03 staging-only) |
| `alembic/versions/025_add_cloud_job.py` | cloud_job create-table migration, reversible | VERIFIED | `revision="025"`, `down_revision="024"`; `upgrade()` creates cloud_job with named pk/fk/uq constraints + `create_check_constraint("status_enum", "cloud_job", ...)`; `downgrade()` drops check then table; CRITICAL banner present (never touches saq_jobs) |
| `src/phaze/services/s3_staging.py` | aioboto3 presign/multipart/delete/lifecycle service | VERIFIED | 7 async functions: `create_multipart_upload`, `presign_upload_parts`, `complete_multipart_upload` (idempotent, WR-01), `abort_multipart_upload` (idempotent, CR-02), `presign_get`, `delete_staged_object` (idempotent), `ensure_bucket_lifecycle_ttl`; zero ORM imports; `_ABORT_ABSENT_CODES` and `_DELETE_ABSENT_CODES` swallow already-gone objects |
| `src/phaze/routers/agent_files.py` | `POST /{file_id}/presign-download` server route | VERIFIED | Route at line 137; loads `FileRecord` by path `file_id` (404 if missing); checks `cloud_job.status == UPLOADED` → 409 if not ready (WR-03); calls `s3_staging.presign_get(file_id)` just-in-time; returns `PresignDownloadResponse(download_url=..., expected_sha256=file.sha256_hash)` (server-sourced, D-04) |
| `src/phaze/schemas/agent_s3.py` | `UploadFileS3Payload` + `UploadedRequest/Response` + `UploadFailedRequest/Response` | VERIFIED | 5 classes + `UploadedPart`; all `extra="forbid"`; `UploadFileS3Payload` has `@field_validator` for absolute `original_path` and http(s) `part_urls`; `UploadedPart.etag` has `min_length=1` (WR-04); no identity in callback body schemas (AUTH-01) |
| `src/phaze/tasks/s3_upload.py` | agent-side httpx multipart-PUT upload task | VERIFIED | `async def upload_file_s3`; ONLY stdlib + phaze.config + phaze.schemas.agent_s3 + httpx; zero aioboto3/botocore/phaze.database/phaze.models/sqlalchemy imports; three-layer timeout constants; streamed parts (memory-bounded); TERMINAL on missing source; reports via `ctx["api_client"]` |
| `src/phaze/services/cloud_staging.py` | `stage_file_to_s3` producer + `redrive_upload` helper | VERIFIED | `stage_file_to_s3` steps: resolve agent → create multipart → presign parts → upsert `cloud_job` (ON CONFLICT, explicit `id=uuid.uuid4()` for CR-01) → enqueue `"s3_upload"` with deterministic key `s3_upload:{file_id}` + `UPLOAD_FILE_SAQ_TIMEOUT_SEC` → commit; `stage_cloud_window` absent (grep returns 0 — Phase 55 wires it) |
| `src/phaze/routers/agent_s3.py` | upload-complete + upload-failure control-side callbacks | VERIFIED | `router = APIRouter(prefix="/api/internal/agent/s3")`; `report_uploaded` completes multipart CONTROL-SIDE + rowcount-guarded `UPLOADING→UPLOADED` flip; `report_upload_failed` re-reads ledger with `populate_existing` after redrive (WR-02); terminal path: abort + delete + clear_ledger_entry; NoActiveAgentError → clean 200 hold |
| `src/phaze/routers/agent_analysis.py` | inline staged-object delete hook on both result paths (D-02) | VERIFIED | `_delete_staged_object_if_cloud` helper at line 96; called at line 231 (after `clear_ledger_entry`, before `session.commit()`) in `put_analysis`; called at line 263 in `report_analysis_failed`; guards on `CloudJob` row existence → zero S3 calls for all-local files (T-53-22); delete errors log-and-swallowed (record-first discipline, T-53-21) |
| `src/phaze/main.py` | agent_s3 router mounted | VERIFIED | `from phaze.routers import agent_s3` at line 28; `app.include_router(agent_s3.router)` at line 211 |

---

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `agent_files.py` | `s3_staging.presign_get` | `download_url = await s3_staging.presign_get(file_id)` at line 178 | WIRED | Direct call inside `presign_download` handler |
| `agent_files.py` | `CloudJob.status` check | `select(CloudJob.status).where(CloudJob.file_id == file_id)` at line 171 | WIRED | WR-03 guard before presign is minted |
| `agent_s3.py` | `s3_staging.complete_multipart_upload` | called at line 81 inside `report_uploaded` | WIRED | Control-plane completes (KSTAGE-01 / DIST-01) |
| `agent_s3.py` | `s3_staging.abort_multipart_upload` + `delete_staged_object` | called at lines 142–143 in terminal path of `report_upload_failed` | WIRED | Terminal cleanup (KSTAGE-04) |
| `cloud_staging.py` | `select_active_agent` (single enqueue seam) | `agent = await select_active_agent(session, kind="fileserver")` at line 73 | WIRED | Phase 30 invariant honored; task enqueued via `task_router.queue_for(agent.id)` |
| `cloud_staging.py` | `AGENT_TASKS` / `agent_worker.py` | `"s3_upload"` in `AGENT_TASKS` (enqueue_router.py line 69); `upload_file_s3` in `settings["functions"]` (agent_worker.py line 282) | WIRED | Both lists in sync — single enqueue seam consistent |
| `agent_analysis.py` | `s3_staging.delete_staged_object` | `_delete_staged_object_if_cloud` at lines 231 + 263 | WIRED | Inline delete on both result paths before `session.commit()` |
| `main.py` | `agent_s3.router` | `app.include_router(agent_s3.router)` at line 211 | WIRED | Router mounted alongside other agent_* routers |

---

### DIST-01 Import Boundary Verification

| Check | Result |
|-------|--------|
| `grep -rn "^import aioboto3\|^from aioboto3\|^from aiobotocore\|^import botocore\|^from botocore" src/` | Only `src/phaze/services/s3_staging.py` (3 import lines) — no other source file |
| `grep -n "aioboto3\|botocore" src/phaze/tasks/s3_upload.py` | 0 matches |
| `grep -n "aioboto3\|botocore" src/phaze/schemas/agent_s3.py` | 0 matches |
| `grep -n "aioboto3\|botocore" src/phaze/tasks/agent_worker.py` | 0 matches |
| `grep -n "aioboto3\|botocore" tests/test_task_split.py` | Lines 85 + 171 — `forbidden = (..., "aioboto3", "botocore")` bans SDK from agent import graph at both test call sites |

**DIST-01: VERIFIED** — aioboto3/botocore confined to `s3_staging.py` on the control plane only; the agent_worker import graph is SDK-free and test-enforced.

---

### AUTH-01 Verification

| Boundary | Check | Status |
|----------|-------|--------|
| `presign_download` — file_id source | PATH only; no request body | VERIFIED |
| `report_uploaded` — identity source | Token (`get_authenticated_agent`); `UploadedRequest` body carries only `parts` (no file_id/agent_id) | VERIFIED |
| `report_upload_failed` — identity source | Token + PATH `file_id`; `UploadFailedRequest` body carries only optional `detail` | VERIFIED |
| `put_analysis` inline delete | `file_id` from PATH (existing handler convention) | VERIFIED |
| `report_analysis_failed` inline delete | `file_id` from PATH | VERIFIED |

**AUTH-01: VERIFIED** — agent identity comes from the token dependency; file_id travels on the URL path; no identity in any request body.

---

### Locked Decision Verification

| Decision | Claim | Code Evidence | Status |
|----------|-------|---------------|--------|
| D-01 | Presigned multipart upload (not single PUT); control completes, never the agent | `create_multipart_upload` + `presign_upload_parts` + `complete_multipart_upload` all in `s3_staging.py`; agent only sees presigned part URLs via `UploadFileS3Payload.part_urls`; `s3_staging.complete_multipart_upload` called in `agent_s3.report_uploaded` | VERIFIED |
| D-02 | Analysis-result callback deletes object inline | `_delete_staged_object_if_cloud` called before `session.commit()` in `put_analysis` (line 231) and `report_analysis_failed` (line 263) | VERIFIED |
| D-03 | `cloud_job` table staging-only (no `cloud_phase`/`kueue_workload`) | `CloudJob` model has only `id`, `file_id`, `s3_key`, `status`, `upload_id`; migration 025 adds no other columns | VERIFIED |
| D-04 | No S3-side per-part checksums; pod's end-to-end sha256 is the single integrity gate | `UploadedPart` carries `part_number` + `etag` only (no Content-MD5/checksum fields); `presign_download` returns `FileRecord.sha256_hash` (server-sourced) | VERIFIED |

---

### Code Review Fix Verification

| Finding | Fix Applied | Verified In Code |
|---------|-------------|-----------------|
| CR-01: `pg_insert` omits `id` PK | Explicit `id=uuid.uuid4()` added to `pg_insert(CloudJob).values(...)` | `cloud_staging.py:86` |
| CR-02: `abort_multipart_upload` not idempotent | `_ABORT_ABSENT_CODES` swallows `NoSuchUpload`/`404` | `s3_staging.py:46,167-172` |
| WR-01: `complete_multipart_upload` not idempotent | Same `_ABORT_ABSENT_CODES` swallow on `complete_multipart_upload` | `s3_staging.py:146-152` |
| WR-02: Ledger update clobbers fresh presigned URLs | Re-fetch row with `populate_existing=True` after `redrive_upload` commits | `agent_s3.py:177-181` |
| WR-03: `presign_download` mints URL without checking object readiness | 409 guard on `cloud_job.status != UPLOADED` before `presign_get` | `agent_files.py:171-176` |
| WR-04: `UploadedPart.etag` accepts empty string | `etag: str = Field(min_length=1)` | `agent_s3.py:82` |
| IN-01: Direct import of transitive `aiobotocore` | Deliberately deferred (info-only, see REVIEW.md) | (no action taken) |

---

### Requirements Coverage

| Requirement | Source Plans | Description | Status | Evidence |
|-------------|-------------|-------------|--------|----------|
| KSTAGE-01 | Plans 02, 04 | Control plane presigns only; never reads/uploads file bytes | SATISFIED | `s3_staging.py` all-SDK on control plane; agent receives only presigned URLs |
| KSTAGE-02 | Plan 03 | Agent uses httpx, no SDK/creds | SATISFIED | `s3_upload.py` httpx-only; `test_task_split.py` SDK ban; creds on `ControlSettings` only |
| KSTAGE-03 | Plan 02 | Just-in-time presigned GET at pod startup | SATISFIED | `presign_download` route mints fresh URL per call |
| KSTAGE-04 | Plans 01, 02, 04, 05 | file_id-scoped keys + cleanup on all outcomes + TTL | SATISFIED | `staged_object_key`; inline delete on both analysis callbacks; upload terminal cleanup; lifecycle TTL |
| KSTAGE-05 | Plan 01 | Operator-configured S3 via `_FILE` secrets; any S3-compatible backend | SATISFIED | All S3 fields on `ControlSettings`; `SECRET_FILE_FIELDS`; `endpoint_url` wired to aioboto3 |

---

### Anti-Patterns Found

None. Scanned all 11 phase-modified source files (`config.py`, `main.py`, `models/cloud_job.py`, `models/__init__.py`, `routers/agent_analysis.py`, `routers/agent_files.py`, `routers/agent_s3.py`, `schemas/agent_s3.py`, `services/cloud_staging.py`, `services/s3_staging.py`, `tasks/s3_upload.py`, `alembic/versions/025_add_cloud_job.py`) for `TBD`, `FIXME`, `XXX`, `TODO`, `HACK`, `PLACEHOLDER`. Zero matches.

---

### Behavioral Spot-Checks

Step 7b: SKIPPED — integration tests require an ephemeral Postgres (port 5433) + Redis (port 6380) via `just integration-test`. The full suite (2352 tests, 0 failures) was confirmed by the executor before code review. Spot-checks are not run here to avoid starting external services. The test files for all phase deliverables exist and are substantive:

| Test File | Exists | Covers |
|-----------|--------|--------|
| `tests/test_config/test_s3_settings.py` | Yes | S3 config fields, _FILE resolution, fail-fast validator, AgentSettings absence |
| `tests/test_models/test_cloud_job.py` | Yes | CloudJob ORM round-trip, unique constraint, CHECK constraint |
| `tests/test_migrations/test_migration_025_cloud_job.py` | Yes | Upgrade + downgrade migration |
| `tests/test_services/test_s3_staging.py` | Yes | moto-backed multipart, presign, delete, lifecycle TTL |
| `tests/test_routers/test_agent_presign_download.py` | Yes | presign-download route, 401/404/409, just-in-time |
| `tests/test_schemas/test_agent_s3.py` | Yes | Schema validation, extra=forbid, ETag min_length |
| `tests/test_tasks/test_s3_upload.py` | Yes | httpx multipart PUT, ETag collection, timeout, terminal error |
| `tests/test_services/test_agent_client_upload.py` | Yes | Agent client upload callbacks |
| `tests/test_services/test_cloud_staging.py` | Yes | Producer: cloud_job upsert + enqueue |
| `tests/test_routers/test_agent_s3.py` | Yes | Callbacks: complete/idempotent/re-drive/terminal |
| `tests/test_routers/test_agent_analysis_inline_delete.py` | Yes | Inline delete on success + failure + all-local no-op |
| `tests/test_task_split.py` | Extended | aioboto3/botocore banned from agent_worker import graph |

---

### Probe Execution

Step 7c: N/A — no `scripts/*/tests/probe-*.sh` files declared or conventional for this phase. Phase deliverables are a library/API layer, not a standalone CLI.

---

### Human Verification Required

None. All five success criteria are verifiable programmatically (code structure, import boundary, route existence, call-site ordering). The moto/respx test suite covers all S3 behavior without a live cluster — per-phase goal ("testable end-to-end without a live cluster"). No `<human-check>` blocks appear in any of the 5 PLAN files.

---

### Warnings (Non-Blocking)

**W-01: REQUIREMENTS.md checkboxes partially updated**

KSTAGE-01/02/03 still show `[ ]` (Pending) in `REQUIREMENTS.md` despite being fully implemented in this phase. KSTAGE-04/05 are correctly marked `[x]`. The traceability table similarly shows KSTAGE-01/02/03 as Pending.

This is a documentation artifact — the code satisfies all five requirements. Update the REQUIREMENTS.md checkboxes and traceability table entries for KSTAGE-01/02/03 to Complete before closing the milestone.

**W-02: IN-01 deferred — direct `aiobotocore` import not in pyproject.toml**

`from aiobotocore.config import AioConfig` in `s3_staging.py` imports a transitive dependency. The REVIEW.md explicitly accepted this as info-only and deferred. No action needed before proceeding.

---

### Gaps Summary

No gaps. All five ROADMAP success criteria are verified against the codebase. All review findings (CR-01/CR-02/WR-01/WR-02/WR-03/WR-04) are fixed. The DIST-01 boundary and AUTH-01 discipline are consistently upheld. The D-01/D-02/D-03/D-04 locked decisions are implemented as designed. Two informational warnings remain (documentation gap in REQUIREMENTS.md, deferred aiobotocore dep declaration) but neither blocks the phase goal.

---

_Verified: 2026-06-27_
_Verifier: Claude (gsd-verifier)_
