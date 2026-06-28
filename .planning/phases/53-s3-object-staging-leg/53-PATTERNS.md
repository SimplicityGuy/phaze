# Phase 53: S3 object-staging leg - Pattern Map

**Mapped:** 2026-06-27
**Files analyzed:** 9 new / 4 modified
**Analogs found:** 13 / 13 (every new/modified file has a concrete in-repo analog)

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/phaze/services/s3_staging.py` (new) | service | file-I/O / request-response (aioboto3 presign+delete) | `src/phaze/services/agent_client.py` (external-client wrapper) + `src/phaze/services/enqueue_router.py` (stateless service) | role-match (no aioboto3 analog exists) |
| `src/phaze/tasks/s3_upload.py` (new) | task | streaming / file-I/O (agent httpx-PUT parts) | `src/phaze/tasks/push.py` (`push_file`) | exact |
| `src/phaze/routers/agent_s3.py` (new — upload presign/complete + complete/mismatch callbacks) | router/controller | request-response | `src/phaze/routers/agent_push.py` | exact |
| GET-side presign route `POST .../files/{file_id}/presign-download` (new — likely in `agent_files.py` or `agent_s3.py`) | router/controller | request-response | `src/phaze/routers/agent_files.py` + presign behind it from `s3_staging.py` | role-match |
| `src/phaze/models/cloud_job.py` (new) | model | CRUD (per-file_id sidecar) | `src/phaze/models/scheduling_ledger.py` + `src/phaze/models/metadata.py` (unique FK to `files.id`) | role-match |
| `alembic/versions/025_add_cloud_job.py` (new) | migration | DDL | `alembic/versions/022_add_scheduling_ledger.py` + `024_add_agents_kind.py` (CHECK enum) | exact |
| `src/phaze/schemas/agent_s3.py` (new — upload payload + callback responses) | schema | transform | `src/phaze/schemas/agent_push.py` + `src/phaze/schemas/agent_tasks.py` (`PushFilePayload`) | exact |
| `PresignDownloadResponse` (already exists in `schemas/agent_analysis.py`) | schema | transform | — already defined Phase 52; server response MUST match | n/a (reuse) |
| `src/phaze/config.py` ControlSettings S3 fields (modified) | config | — | `ControlSettings` cloud-burst fields + `SECRET_FILE_FIELDS` machinery | exact |
| `src/phaze/services/agent_client.py` `report_upload_*` methods (modified) | service | request-response | existing `report_pushed` (line 326) | exact |
| `src/phaze/routers/agent_analysis.py` inline-delete hook (modified, D-02) | router | request-response | `put_analysis` + `report_analysis_failed` (success+failure paths) | exact |
| `src/phaze/services/enqueue_router.py` `AGENT_TASKS` add `s3_upload` (modified) | service | — | existing `AGENT_TASKS` frozenset (line 60) | exact |
| `src/phaze/models/__init__.py` register `CloudJob` (modified) | config | — | existing model registration list | exact |

---

## Pattern Assignments

### `src/phaze/tasks/s3_upload.py` (task, streaming) — NEW

**Analog:** `src/phaze/tasks/push.py` (`push_file`) — exact. This is the agent-side transfer leg; swap rsync-over-SSH for httpx-multipart-PUT to presigned part URLs.

**Postgres-free import boundary** (`push.py:28-40`) — the task MUST carry ONLY stdlib + `phaze.config` + `phaze.schemas` + the runtime `ctx["api_client"]` handle. NO `phaze.database`, `phaze.models.*`, `sqlalchemy`, and (KSTAGE-02) NO aioboto3/botocore/bucket-credential import:
```python
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from phaze.config import AgentSettings, get_settings
from phaze.schemas.agent_tasks import PushFilePayload  # -> new UploadFilePayload

if TYPE_CHECKING:
    from phaze.services.agent_client import PhazeAgentClient
```
The new payload (carrying the presigned part URLs to PUT to) is enforced by `tests/test_task_split.py` (extend the existing `test_agent_worker_does_not_import_phaze_database` to also ban aioboto3/botocore from the agent import graph).

**Agent-settings narrowing** (`push.py:65-77`): the upload task runs only on the agent role, so resolve via `get_settings()` and narrow to `AgentSettings` (the module-level `settings` is Control-typed). Note: KSTAGE-02 means the agent has NO S3 config of its own — the presigned URLs arrive in the job payload, so the agent may not need new AgentSettings fields at all (only httpx tunables like part concurrency, Claude's Discretion).

**Outer/inner timeout layering** (`push.py:43-62`, `184-192`): the SAQ job-net timeout sits strictly ABOVE the asyncio outer guard which sits above the per-part httpx timeout, so the kill order is deterministic and a SAQ `CancelledError` (NOT `TimeoutError`) is reaped before cleanup. Reproduce the three-layer constant block and the `_SAQ_TIMEOUT_SEC` module constant the producer stamps on enqueue:
```python
_OUTER_TIMEOUT_BUFFER_SEC = 30
_SAQ_JOB_TIMEOUT_MARGIN_SEC = 30
UPLOAD_FILE_SAQ_TIMEOUT_SEC = 600 + _OUTER_TIMEOUT_BUFFER_SEC + _SAQ_JOB_TIMEOUT_MARGIN_SEC
```

**TERMINAL-vs-retryable error handling** (`push.py:139-206`): the canonical shape — validate payload, fail-fast on missing config (TERMINAL `RuntimeError`, never local fallback), wrap the transfer, catch `(TimeoutError, asyncio.CancelledError)` to reap before cleanup, non-success → `RuntimeError` (SAQ retry), then on success call the control callback and return a status dict:
```python
async def push_file(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    payload = PushFilePayload.model_validate(kwargs)
    api: PhazeAgentClient = ctx["api_client"]
    cfg = _agent_settings()
    _require_push_config(cfg)
    ...
    try:
        _out, err = await asyncio.wait_for(proc.communicate(), timeout=cfg.push_timeout_sec + _OUTER_TIMEOUT_BUFFER_SEC)
    except (TimeoutError, asyncio.CancelledError):
        proc.kill(); await proc.wait(); raise
    if proc.returncode != 0:
        raise RuntimeError(...)  # SAQ retry
    await api.report_pushed(payload.file_id)   # -> api.report_upload_complete(file_id, parts/etag)
    return {"file_id": str(payload.file_id), "status": "pushed"}
```
**Adapt:** multipart upload means the agent PUTs each part to a presigned URL over httpx, collects each part's `ETag`, and the upload-complete callback carries the ordered `(part_number, etag)` list so the control plane can `complete_multipart_upload`. On any part failure → `RuntimeError` for SAQ retry (parts already uploaded can be skipped, or the whole multipart re-presigned — Claude's Discretion). Bound the error-snippet length crossing into the SAQ error like `_STDERR_SNIPPET_MAX = 500` (`push.py:45`).

---

### `src/phaze/routers/agent_s3.py` (router, request-response) — NEW upload-complete / upload-failure callbacks

**Analog:** `src/phaze/routers/agent_push.py` — exact. `/pushed` → upload-complete, `/mismatch` → upload-failure re-drive.

**Router prefix + auth + path-only file_id** (`agent_push.py:59-68`):
```python
router = APIRouter(prefix="/api/internal/agent/s3", tags=["agent-internal"])

@router.post("/{file_id}/uploaded", status_code=status.HTTP_200_OK, response_model=UploadedResponse)
async def report_uploaded(
    file_id: uuid.UUID,
    request: Request,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UploadedResponse:
```
AUTH-01: `file_id` rides the URL PATH only; agent identity comes from the token dependency, never the body (`agent_push.py:26-29`, `62-68`).

**Idempotent state flip guarded on current state** (`agent_push.py:103-118`) — guard the `UPDATE` on the expected current state and read `rowcount` via the `cast("CursorResult[Any]", ...)` idiom so a duplicate/late callback (SAQ retry after the first callback committed) is an idempotent no-op rather than clobbering an advanced file:
```python
res = cast("CursorResult[Any]", await session.execute(
    update(FileRecord).where(FileRecord.id == file_id, FileRecord.state == FileState.PUSHING).values(state=FileState.PUSHED)))
if res.rowcount == 0:
    await session.commit()
    return UploadedResponse(file_id=file_id)  # idempotent 200
```
For Phase 53 the upload-complete callback ALSO calls `s3_staging.complete_multipart_upload(file_id, parts)` (aioboto3, control-side) before the state flip — the control plane completes the multipart, never the agent (DIST-01/KSTAGE-01). Update the `cloud_job` row (status enum → uploaded/complete) in the same transaction.

**Ledger clear + re-drive loop** (`agent_push.py:118`, `141-233`): the `/mismatch` (→ upload-failure) handler mirrors `report_push_mismatch` exactly — read an attempt counter, compare to a `ControlSettings.push_max_attempts`-style cap, terminal-fail at the cap (`FileState.ANALYSIS_FAILED` + clear ledger), else re-enqueue the upload task on the selected agent's queue with the explicit SAQ timeout. Reuse `select_active_agent` / `NoActiveAgentError` for the clean-200-hold-when-no-agent path (`agent_push.py:89-93`, `190-201`). On terminal failure the handler must also `abort_multipart_upload` + `delete_staged_object` so an orphaned in-flight upload is cleaned (D-02 note).

**Clean 200 hold, never 500** (`agent_push.py:78-79`, `89-93`): when no eligible agent is online, return a 200 with no state change/enqueue so the staging cron re-drives later — never raise a 500.

---

### GET-side presign route `POST /api/internal/agent/files/{file_id}/presign-download` (router) — NEW (completes Phase 52 client)

**Analog:** route shape from `src/phaze/routers/agent_files.py` (`prefix="/api/internal/agent/files"`, `agent_files.py:32`) + the auth/path-only discipline from every `agent_*` router. The presign itself comes from the new `s3_staging.py` service.

**Client contract this route MUST satisfy** (`services/agent_client.py:282-307`):
```python
async def request_download_url(self, file_id: uuid.UUID) -> tuple[str, str]:
    response = await self._request("POST", f"/api/internal/agent/files/{file_id}/presign-download")
    resp = PresignDownloadResponse.model_validate(response.json())
    return resp.download_url, resp.expected_sha256
```
**Response schema is ALREADY defined — match it exactly** (`schemas/agent_analysis.py:105-129`):
```python
class PresignDownloadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    download_url: str
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")  # from FileRecord.sha256_hash, lowercase hex
```
Handler logic: auth via `get_authenticated_agent`, load `FileRecord` by PATH `file_id`, mint a short-TTL presigned GET via `s3_staging.presign_get(file_id_scoped_key)` JUST-IN-TIME (KSTAGE-03 — never at submit time), and return `expected_sha256=file.sha256_hash` (server-sourced, never from the agent). The 64-char-hex `Field(pattern=...)` means a format skew fails at the wire boundary, not silently mid-download.

---

### `src/phaze/services/s3_staging.py` (service, aioboto3) — NEW

**Analog:** no aioboto3 analog exists in the repo (this is the only "no analog" surface — see below). Closest structural conventions: `services/enqueue_router.py` (stateless module-level async functions, `__future__` annotations, `TYPE_CHECKING` import guard, raise-loud custom errors) and `services/agent_client.py` (external-client wrapper, secrets never logged, one method per operation).

**Conventions to copy from `enqueue_router.py:30-42`, `79-81`:**
```python
from __future__ import annotations
from typing import TYPE_CHECKING, Any
if TYPE_CHECKING:
    from aioboto3 import Session  # type-only

class S3StagingError(RuntimeError):  # fail-loud custom error (cf. NoActiveAgentError:79)
    ...
```
**Surface to build (control-plane only, never touches file bytes — DIST-01/KSTAGE-01):**
- `create_multipart_upload(file_id) -> upload_id`
- `presign_upload_parts(file_id, upload_id, part_count) -> list[str]` (presigned PUT URLs)
- `complete_multipart_upload(file_id, upload_id, parts)` / `abort_multipart_upload(file_id, upload_id)`
- `presign_get(file_id) -> str` (short-TTL, just-in-time; KSTAGE-03)
- `delete_staged_object(file_id)` (D-02 inline-delete capability)
- `ensure_bucket_lifecycle_ttl()` (KSTAGE-04 backstop)

Build the `aioboto3.Session().client("s3", endpoint_url=..., aws_access_key_id=..., aws_secret_access_key=..., region_name=..., config=Config(s3={"addressing_style": ...}))` from `ControlSettings` (S3-compatible, not just AWS — KSTAGE-05). Use `async with` for the client. The `file_id`-scoped key is the single object identity (reconcile-by-file_id). Verify aioboto3/botocore API surface via Context7 before implementing (no in-repo precedent).

---

### `src/phaze/models/cloud_job.py` (model, per-file_id sidecar) — NEW

**Analog:** `src/phaze/models/scheduling_ledger.py` (standalone sidecar, `TimestampMixin`, JSONB, indexed) + `src/phaze/models/metadata.py:18` (unique FK to `files.id`).

**Unique FK to file_id** (`metadata.py:18` — the one-row-per-file_id precedent, D-03):
```python
file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), unique=True, nullable=False)
```
**Model skeleton** (mirror `scheduling_ledger.py:42-69` conventions — `from __future__ import annotations`, `TimestampMixin, Base`, `Mapped`/`mapped_column`, `datetime` with the `# noqa: TC003` SQLAlchemy-runtime-resolution comment):
```python
from __future__ import annotations
import uuid
from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from phaze.models.base import Base, TimestampMixin

class CloudJob(TimestampMixin, Base):
    __tablename__ = "cloud_job"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), unique=True, nullable=False)
    s3_key: Mapped[str] = mapped_column(String(...), nullable=False)
    status: Mapped[str] = mapped_column(String(...), nullable=False)   # stage/upload status enum (string-backed)
    upload_id: Mapped[str | None] = mapped_column(String(...), nullable=True)  # multipart upload_id
    # created_at/updated_at from TimestampMixin — do NOT redeclare
```
**Status enum** — follow `FileState` (a `StrEnum` over a `String(N)` column, `models/file.py:20-58`) so new members never need an enum migration. Phase 53 columns ONLY (D-03); Phases 54/55 add `kueue_workload`/`cloud_phase` in their own migrations. Register `CloudJob` in `src/phaze/models/__init__.py` (the import + `__all__` list) so Alembic autogenerate sees it.

---

### `alembic/versions/025_add_cloud_job.py` (migration) — NEW

**Analog:** `alembic/versions/022_add_scheduling_ledger.py` (create-table, additive-only) + `024_add_agents_kind.py` (CHECK-constraint enum, naming-convention note).

**Revision chaining + docstring discipline** (`022:29-46`, `024:23-39`) — sequential numeric id, `down_revision` points at `024`, dated `Create Date`, and the CRITICAL banner that the migration touches ONLY its own table and NEVER `saq_jobs`:
```python
revision: str = "025"
down_revision: str | Sequence[str] | None = "024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
```
**create_table + index** (`022:49-62`):
```python
def upgrade() -> None:
    op.create_table(
        "cloud_job",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("s3_key", sa.String(...), nullable=False),
        sa.Column("status", sa.String(...), nullable=False),
        sa.Column("upload_id", sa.String(...), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cloud_job")),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], name=op.f("fk_cloud_job_file_id_files")),
        sa.UniqueConstraint("file_id", name=op.f("uq_cloud_job_file_id")),
    )
```
**Optional status CHECK enum** (`024:44-45`) — if the status is constrained at the DB:
```python
op.create_check_constraint("status_enum", "cloud_job", "status IN (...)")
```
`downgrade()` mirrors upgrade in reverse (drop constraint/index → drop table; pass the BARE constraint name `status_enum`, the `ck_%(table_name)s_%(constraint_name)s` convention re-applies the prefix — `024:48-56`).

---

### `src/phaze/schemas/agent_s3.py` (schema, transform) — NEW

**Analog:** `src/phaze/schemas/agent_push.py` (callback responses) + `src/phaze/schemas/agent_tasks.py` `PushFilePayload` (task payload with field validators).

**Every model `extra="forbid"`, identity NEVER in body** (`agent_push.py:18-21`, `29-35`):
```python
class UploadedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: uuid.UUID
    status: Literal["uploaded"] = "uploaded"
```
**Upload task payload** — mirror `PushFilePayload` (`agent_tasks.py:54-84`): `extra="forbid"`, `file_id` present (deterministic-key builder reads it), and field validators (`@field_validator`) for any path/identifier the agent hands to httpx. For multipart, carry the ordered presigned part URLs + part size; the agent returns `(part_number, etag)` pairs in the upload-complete callback body (still no `file_id`/`agent_id` in body — AUTH-01).

---

### `src/phaze/config.py` — ADD S3 fields to `ControlSettings` (modified)

**Analog:** the cloud-burst `ControlSettings` fields (`config.py:376-430`) + the `SECRET_FILE_FIELDS` machinery (`79`, `342`, `473`).

**Field declaration pattern** (`config.py:376-382`, `426-430`) — `Field(default=..., validation_alias=AliasChoices("PHAZE_S3_...", "s3_..."), description=...)`; bound numeric TTLs with `gt=/lt=` like `cloud_route_threshold_sec`:
```python
s3_endpoint_url: str | None = Field(default=None, validation_alias=AliasChoices("PHAZE_S3_ENDPOINT_URL", "s3_endpoint_url"), description="...")
s3_bucket: str | None = Field(default=None, ...)
s3_region: str | None = Field(default=None, ...)
s3_addressing_style: Literal["path", "virtual"] = Field(default="path", ...)
s3_access_key_id: SecretStr | None = Field(default=None, ...)
s3_secret_access_key: SecretStr | None = Field(default=None, ...)
```
**`_FILE`-secret auto-resolution** (KSTAGE-05) — add the credential fields to the class-var set; the inherited `_resolve_secret_files` before-validator resolves their `<VAR>_FILE` siblings with ZERO new code (`config.py:90-148`, `342`):
```python
SECRET_FILE_FIELDS: ClassVar[frozenset[str]] = BaseSettings.SECRET_FILE_FIELDS | {"openai_api_key", "anthropic_api_key", "s3_access_key_id", "s3_secret_access_key"}
```
**Fail-fast guard when cloud enabled** — mirror `_enforce_compute_scratch_dir_when_cloud_enabled` (`config.py:432-451`): a `@model_validator(mode="after")` that requires `s3_bucket`/`s3_endpoint_url` when `cloud_burst_enabled` is True. These fields land on `ControlSettings` ONLY — the agent gets none of them (KSTAGE-02).

---

### `src/phaze/services/agent_client.py` — ADD upload-callback methods (modified)

**Analog:** existing `report_pushed` (`agent_client.py:326-339`). Add `report_upload_complete(file_id, parts)` and `report_upload_failed(file_id, detail)` following the exact funnel: lazy `from phaze.schemas.agent_s3 import ...` (`noqa: PLC0415`), `await self._request("POST", f"/api/internal/agent/s3/{file_id}/uploaded", json=...)`, `Model.validate(response.json())`. Inherits the tenacity 5xx-retry/4xx-surface policy and never logs the token. `file_id` on the path; the multipart `(part, etag)` list rides the body (it is upload metadata, not identity).

---

### `src/phaze/routers/agent_analysis.py` — INLINE OBJECT DELETE (modified, D-02)

**Analog:** the file itself — both `put_analysis` (success, `agent_analysis.py:94-199`) and `report_analysis_failed` (failure, `202-234`). Both already end with `clear_ledger_entry(...)` + `session.commit()`.

**Hook point** — after the result/terminal state is recorded and before commit, call the control-side delete capability (KSTAGE-04 inline-delete, the point the object is provably no longer needed):
```python
# D-02: the staged object is provably no longer needed once the result lands.
await s3_staging.delete_staged_object(file_id)   # idempotent; safe if no cloud_job row
```
Add to BOTH the success path (after line 196's `clear_ledger_entry`) and the failure path (after line 225). Make `delete_staged_object` a no-op when no `cloud_job` row exists so the all-local path is unaffected (the bucket lifecycle TTL is the backstop for the no-callback / Kueue-eviction case; Phase 54's reconcile may also invoke this same delete).

---

### `src/phaze/services/enqueue_router.py` — REGISTER the upload task (modified)

**Analog:** the `AGENT_TASKS` frozenset (`enqueue_router.py:60-70`). Add `"s3_upload"` (the new task name) so the upload-trigger enqueue routes through `resolve_queue_for_task` / `select_active_agent` (the Phase 30 single-enqueue-seam invariant). MUST stay in sync with `phaze.tasks.agent_worker.settings["functions"]`. The producer enqueues via `select_active_agent(session, kind=...)` + `request.app.state.task_router.queue_for(agent.id)` + the explicit SAQ timeout constant (cf. `agent_push.py:203-216`, `release_awaiting_cloud._enqueue_push_file`).

---

## Shared Patterns

### Authentication (every `/api/internal/agent/*` route)
**Source:** `src/phaze/routers/agent_push.py:62-68`, `agent_analysis.py:94-100`
**Apply to:** `agent_s3.py`, the presign-download route
```python
agent: Annotated[Agent, Depends(get_authenticated_agent)]
session: Annotated[AsyncSession, Depends(get_session)]
```
AUTH-01: `file_id` on the URL PATH only; agent identity from the token dep; request bodies carry only diagnostics/upload-metadata, never identity. Every Pydantic body/response declares `model_config = ConfigDict(extra="forbid")`.

### Postgres-free agent import boundary
**Source:** `src/phaze/tasks/push.py:9-12, 28-40`; `tests/test_task_split.py`
**Apply to:** `tasks/s3_upload.py`, `schemas/agent_s3.py`
The upload task + its schemas carry NO `phaze.database`/`phaze.models`/`sqlalchemy` import, AND (KSTAGE-02) NO aioboto3/botocore/bucket-credential import. Extend the subprocess import-boundary test to ban the S3 SDK from the agent graph.

### DIST-01 / KSTAGE-01 no-bytes-on-control boundary
**Source:** decision context; enforced by keeping all aioboto3 in `s3_staging.py` (control-plane service) and all byte transfer in `tasks/s3_upload.py` (agent, httpx-only)
**Apply to:** every new file
The control plane presigns/completes/deletes (orchestrates); the agent and pod transfer bytes over httpx via presigned URLs only. Mirrors the `push.py` (agent transfers) vs `agent_push.py` (control orchestrates) split.

### `_FILE`-convention secrets
**Source:** `src/phaze/config.py:79-148` (`_resolve_secret_files`) + the per-subclass `SECRET_FILE_FIELDS` extension (`342`, `473`)
**Apply to:** the S3 credential fields on `ControlSettings`
Add the credential fields to `SECRET_FILE_FIELDS`; resolution is automatic. Never log secret values (`SecretStr`).

### Idempotent state-flip with rowcount guard
**Source:** `src/phaze/routers/agent_push.py:103-118`
**Apply to:** the upload-complete callback in `agent_s3.py`
Guard the `UPDATE` on the expected current state and `cast("CursorResult[Any]", ...)`; `rowcount == 0` → idempotent 200 (handles SAQ-retry / late callbacks).

### Ledger clear in the same transaction as the result write
**Source:** `agent_analysis.py:196, 225` + `services/scheduling_ledger.py:clear_ledger_entry` / `upsert_ledger_entry`
**Apply to:** upload callbacks + the inline-delete hook
Clear/upsert the `process_file`/`push_file`-style ledger key (PATH `file_id` only, AUTH-01) inside the same committed transaction as the state change.

### Timeout layering (inner < asyncio-outer < SAQ-net)
**Source:** `src/phaze/tasks/push.py:43-62, 120-136, 184-192`
**Apply to:** `tasks/s3_upload.py` + the producer's enqueue (`timeout=UPLOAD_FILE_SAQ_TIMEOUT_SEC`)
Catch `(TimeoutError, asyncio.CancelledError)`, reap children before cleanup. Stamp the explicit SAQ timeout on every enqueue (cf. `agent_push.py:216`).

---

## No Analog Found

| File | Role | Data Flow | Reason |
|------|------|-----------|--------|
| `src/phaze/services/s3_staging.py` | service | aioboto3 presign/multipart/delete | No aioboto3/boto3/botocore usage exists anywhere in the repo (confirmed: `pyproject.toml` has no boto3/aioboto3/moto; only `respx>=0.23.1` is present). The aioboto3 client construction, multipart presign API, and lifecycle-config API have NO in-repo precedent. **Planner action:** add `aioboto3` (runtime dep) + `moto` (test dep) to `pyproject.toml`; verify the aioboto3/botocore API via Context7; the SERVICE STRUCTURE (stateless async functions, `__future__`/`TYPE_CHECKING`, fail-loud custom error, secrets-never-logged) follows `enqueue_router.py` + `agent_client.py` conventions, but the S3 SDK calls themselves are net-new. Test with moto / botocore stubber + respx (no live cluster — per phase boundary). |

Everything else has a concrete in-repo analog (push pipeline, scheduling-ledger sidecar, agent callbacks, config secrets, migration conventions).

---

## Metadata

**Analog search scope:** `src/phaze/tasks/`, `src/phaze/routers/`, `src/phaze/services/`, `src/phaze/models/`, `src/phaze/schemas/`, `alembic/versions/`, `src/phaze/config.py`, `tests/test_task_split.py`, `pyproject.toml`
**Files scanned:** ~20 (push.py, agent_push.py, agent_analysis.py, agent_files.py, agent_client.py, enqueue_router.py, release_awaiting_cloud.py, config.py, scheduling_ledger model+service, file.py, metadata.py, base.py, models/__init__.py, agent_tasks.py, agent_push.py schema, agent_analysis.py schema, migrations 022/024, test_task_split.py)
**Pattern extraction date:** 2026-06-27
</content>
</invoke>
