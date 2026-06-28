# Phase 54: Kube submit / watch + reconcile cron - Pattern Map

**Mapped:** 2026-06-27
**Files analyzed:** 9 new/modified
**Analogs found:** 9 / 9 (every moving part has a direct in-repo precedent)

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/phaze/services/kube_staging.py` (NEW) | service (pure SDK seam) | request-response (kube REST) | `src/phaze/services/s3_staging.py` | exact (seam pattern) |
| `src/phaze/tasks/submit_cloud_job.py` (NEW) | task (fast producer) | request-response → enqueue | `src/phaze/services/cloud_staging.py` (`stage_file_to_s3`) | exact (upsert+enqueue) |
| `src/phaze/tasks/reconcile_cloud_jobs.py` (NEW) | task (cron) | batch / event-driven poll | `src/phaze/tasks/release_awaiting_cloud.py` (`stage_cloud_window`) | exact (narrow `*/5` cron) |
| re-drive cap logic (inside reconcile) | service (bounded retry) | event-driven | `src/phaze/routers/agent_push.py` (`report_push_mismatch`) | exact (attempt-cap → ANALYSIS_FAILED) |
| `src/phaze/models/cloud_job.py` (MODIFY) | model | CRUD | itself (extend in place) | exact |
| `alembic/versions/026_*.py` (NEW) | migration | schema change | `alembic/versions/025_add_cloud_job.py` | exact |
| `src/phaze/config.py` (MODIFY) | config | n/a | `push_max_attempts` / `cloud_max_in_flight` / `SECRET_FILE_FIELDS` | exact |
| `src/phaze/services/enqueue_router.py` (MODIFY) | service (routing) | n/a | `CONTROLLER_TASKS` frozenset | exact (one-line add) |
| `src/phaze/tasks/controller.py` (MODIFY) | config (cron registry) | n/a | `CronJob(stage_cloud_window, "*/5...")` block | exact |
| Inadmissible alert (templates + pipeline router) | component | request-response | `awaiting_cloud_card.html` + `routers/pipeline.py` dashboard | role-match |
| `tests/test_services/test_kube_staging.py` (NEW) | test | n/a | `test_s3_staging.py` (moto) + `test_s3_upload.py` (respx) | exact |
| `tests/test_tasks/test_{submit,reconcile}_cloud_jobs.py` (NEW) | test | n/a | `test_staging_cron.py` / `test_cloud_staging.py` (monkeypatched seam) | role-match |
| `tests/test_migrations/test_migration_026_*.py` (NEW) | test | n/a | `test_migration_025_cloud_job.py` | exact |

---

## Pattern Assignments

### `src/phaze/services/kube_staging.py` (NEW — pure kr8s seam)

**Analog:** `src/phaze/services/s3_staging.py` (the single SDK home; pure, keyed by `file_id`, NO ORM imports). RESEARCH names this the precedent verbatim. The AST/import-boundary test must assert `kube_staging` has no ORM imports (mirror `s3_staging` purity).

**Module-docstring + import discipline** (`s3_staging.py:1-36`):
```python
from __future__ import annotations
from typing import TYPE_CHECKING, Any, cast
import aioboto3                              # → replace with: import kr8s.asyncio
from phaze.config import get_settings
if TYPE_CHECKING:
    import uuid
    from phaze.config import ControlSettings
```
Copy the header note: "The single home of every <SDK> call ... NO ORM imports here -- the service is pure ... keyed by ``file_id``".

**Fail-loud custom error** (`s3_staging.py:49-55`) — define `KubeStagingError(RuntimeError)` mirroring `S3StagingError`; an unset `kube_api_url`/`kube_namespace`/`kube_local_queue` is an operator misconfig that must surface immediately.

**Config-validation gate** (`s3_staging.py:67-76`) — mirror `_staging_config()`: read `cast("ControlSettings", get_settings())`, raise if the kube surface is unset.

**Client construction** (`s3_staging.py:79-95` `_client`) — mirror as an async kr8s api factory:
```python
# kr8s analog of _client(cfg): build from the ControlSettings kube surface; creds via _FILE secrets, never logged.
api = await kr8s.asyncio.api(url=cfg.kube_api_url, namespace=cfg.kube_namespace, ...)
```

**Idempotent delete idiom** (`s3_staging.py:194-210` `delete_staged_object`) — THE template for `delete_job`: swallow the already-absent error so a re-run after a partial tick is a no-op. Mirror the `_DELETE_ABSENT_CODES` frozenset shape with a kr8s `NotFoundError` catch:
```python
async def delete_staged_object(file_id: uuid.UUID) -> None:
    ...
    try:
        await client.delete_object(Bucket=cfg.s3_bucket, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in _DELETE_ABSENT_CODES:   # → kr8s: `except kr8s.NotFoundError: return`
            return
        raise S3StagingError(...) from exc
```
Seam functions to expose (RESEARCH §Fake-Kube Layer 1): `submit_job`, `list_inflight_jobs`, `get_job`, `get_workload_for`, `delete_job` — each one operation, one function (mirror `s3_staging`'s one-op-per-function discipline).

---

### `src/phaze/tasks/submit_cloud_job.py` (NEW — fast controller-queue producer)

**Analog:** `src/phaze/services/cloud_staging.py` `stage_file_to_s3` (upsert-then-act) + `push.py` SAQ-timeout-constant idiom.

**Upsert-then-enqueue shape** (`cloud_staging.py:52-126`) — the canonical producer body:
```python
# Idempotent upsert against the unique file_id FK (mirrors scheduling_ledger upsert idiom).
stmt = pg_insert(CloudJob).values(
    id=uuid.uuid4(),                         # stamp PK explicitly (CR-01 defensive; pg_insert list-form skips the default)
    file_id=file.id,
    s3_key=s3_staging.staged_object_key(file.id),
    status=CloudJobStatus.UPLOADING.value,   # → SUBMITTED for Phase 54
    upload_id=upload_id,                      # → kueue_workload=<job-name> for Phase 54
)
stmt = stmt.on_conflict_do_update(
    index_elements=["file_id"],              # id intentionally OUT of set_ (PK immutable on re-stage)
    set_={"s3_key": stmt.excluded.s3_key, "status": stmt.excluded.status, "upload_id": stmt.excluded.upload_id},
)
await session.execute(stmt)
```
For Phase 54: set `status=SUBMITTED`, `kueue_workload=<job-name>`, and increment `attempts` on a re-drive. Submit path writes ONLY the `cloud_job` row — NO `SchedulingLedger` `process_file:<id>` row (KSUBMIT-06, the CLOUDROUTE-02 hazard).

**Deterministic-key enqueue + explicit SAQ timeout** (`cloud_staging.py:111-118`):
```python
queue = task_router.queue_for(agent.id)      # Phase 54: controller queue, not per-agent
await queue.connect()
await queue.enqueue("s3_upload", key=f"s3_upload:{file.id}", timeout=UPLOAD_FILE_SAQ_TIMEOUT_SEC, **payload.model_dump(mode="json"))
await session.commit()
```
Phase 54 uses key `submit_cloud_job:<file_id>` (matches the `s3_upload:<file_id>` idiom). Mirror `s3_upload.py:52-58` for the module-constant SAQ timeout pattern if a job-net timeout is needed.

**Re-drive helper** (`cloud_staging.py:129-143` `redrive_upload`) — best-effort prior cleanup then re-stage. For Phase 54: delete the prior terminal Job + S3 object (D-04/D-05), then call submit again (same deterministic name, now free):
```python
async def redrive_upload(session, file, task_router) -> None:
    existing = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one_or_none()
    if existing is not None and existing.upload_id:
        with contextlib.suppress(Exception):           # best-effort; prior object may be gone
            await s3_staging.abort_multipart_upload(file.id, existing.upload_id)
    await stage_file_to_s3(session, file, task_router)
```

**Idempotency guard ("Job already exists")** — RESEARCH §Submit Task: catch `kr8s.ServerError` with `response.status_code == 409`, then `await job.refresh()`. This lives in the seam (`kube_staging.submit_job`), keeping the task thin.

---

### `src/phaze/tasks/reconcile_cloud_jobs.py` (NEW — narrow `*/5` cron)

**Analog:** `src/phaze/tasks/release_awaiting_cloud.py` `stage_cloud_window` (the narrow recovery-only `*/5` cron precedent).

**Control-only cron discipline** (`release_awaiting_cloud.py:1-36, 109-129`):
```python
from __future__ import annotations
from typing import TYPE_CHECKING, Any
import structlog
from phaze.config import get_settings
# CONTROL-ONLY: needs ctx["async_session"] + ctx["task_router"], like recover_orphaned_work.
# Register ONLY in phaze.tasks.controller -- never the agent worker (test_task_split enforces this).
# FastAPI-free: imports neither fastapi nor phaze.routers.

async def reconcile_cloud_jobs(ctx: dict[str, Any]) -> dict[str, int]:
    cfg = get_settings()
    async with ctx["async_session"]() as session:
        ...
```
Carry the SAME guard comment as the existing crons (`controller.py:220-233`): "DO NOT re-add a general auto-advance / `recover_orphaned_work` cron — this is narrow, in-flight K8s reconcile only."

**Per-tick algorithm** (RESEARCH §Reconcile Loop) — iterate the DB sidecar (D-02), NOT a watch:
```python
# 1. Find in-flight work from the cloud_job sidecar (D-02), not a kube watch, not the recovery ledger.
rows = (await session.execute(select(CloudJob).where(CloudJob.status.in_([SUBMITTED, RUNNING])))).scalars().all()
# 2-3. per row: get Job by kueue_workload name; resolve Workload by job-uid; map conditions → outcome.
# 4. TERMINAL ordering is load-bearing (D-04): record+commit → s3 delete (no-callback) → job.delete().
```

**Terminal ordering (D-04, KSUBMIT-06)** — the single most important correctness property. Reuse the S3-delete-then-advance shape from `agent_analysis._delete_staged_object_if_cloud` (see Shared Patterns). Order: `record outcome + commit` → `s3_staging.delete_staged_object(file_id)` (no-callback path only) → `await job.delete(propagation_policy="Background")`.

**Status→outcome table** — implement exactly the `(type, status, reason)` tuples in RESEARCH §Status → Outcome Mapping. `Pending`=silent, `Inadmissible`=loud (no cap), `Failed`/`Evicted`=cap-consuming re-drive.

---

### Re-drive cap (inside reconcile)

**Analog:** `src/phaze/routers/agent_push.py` `report_push_mismatch` (`agent_push.py:141-233`) — the attempt-cap → `ANALYSIS_FAILED` precedent.

**Cap-then-terminal pattern** (`agent_push.py:172-186`):
```python
next_attempt = current_attempt + 1
if next_attempt > settings.push_max_attempts:                 # → cloud_submit_max_attempts (D-08)
    await session.execute(update(FileRecord).where(FileRecord.id == file_id).values(state=FileState.ANALYSIS_FAILED))
    await clear_ledger_entry(session, ledger_key)             # → terminate cloud_job lifecycle instead
    await session.commit()
    logger.warning("push cap reached -> ANALYSIS_FAILED", file_id=str(file_id), attempt=next_attempt, cap=settings.push_max_attempts)
    return ...
# under cap → re-drive (fresh submit), increment attempts.
```
Phase 54 difference: `attempts` lives on the `cloud_job` row (an integer column), NOT in a ledger payload JSONB. No cross-target fallback (KSUBMIT-05). Inadmissible NEVER enters this path (D-07).

---

### `src/phaze/models/cloud_job.py` (MODIFY — D-09)

**Analog:** itself. Extend `CloudJobStatus` (string-backed StrEnum — no enum-type migration, only the CHECK list) and add columns.

**StrEnum extension** (`cloud_job.py:28-39`):
```python
class CloudJobStatus(enum.StrEnum):
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    FAILED = "failed"
    # Phase 54 (D-09): add submit/reconcile lifecycle members (planner finalizes names):
    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
```

**New columns + CHECK** (`cloud_job.py:46-62`) — mirror the existing `mapped_column` + `CheckConstraint(name="status_enum")` shape:
```python
kueue_workload: Mapped[str | None] = mapped_column(String(255), nullable=True)   # Kueue/Job name (reserved by 53-CONTEXT D-03)
attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
inadmissible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false", default=False)  # drives D-06 alert
__table_args__ = (
    CheckConstraint("status IN ('uploading', 'uploaded', 'submitted', 'running', 'succeeded', 'failed')", name="status_enum"),
)
```
`status` is `String(16)` — `succeeded` (9 chars) fits. Leave `cloud_phase` UNTOUCHED (Phase 55).

---

### `alembic/versions/026_*.py` (NEW migration)

**Analog:** `alembic/versions/025_add_cloud_job.py` — additive, reversible, scoped to `cloud_job` only.

**Revision header + CHECK swap** (`025:31-64`):
```python
revision: str = "026"
down_revision: str | Sequence[str] | None = "025"

def upgrade() -> None:
    op.add_column("cloud_job", sa.Column("kueue_workload", sa.String(255), nullable=True))
    op.add_column("cloud_job", sa.Column("attempts", sa.Integer(), server_default="0", nullable=False))
    op.add_column("cloud_job", sa.Column("inadmissible", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.drop_constraint("status_enum", "cloud_job", type_="check")     # bare name; convention re-applies ck_cloud_job_ prefix
    op.create_check_constraint("status_enum", "cloud_job", "status IN ('uploading', 'uploaded', 'submitted', 'running', 'succeeded', 'failed')")

def downgrade() -> None:
    op.drop_constraint("status_enum", "cloud_job", type_="check")
    op.create_check_constraint("status_enum", "cloud_job", "status IN ('uploading', 'uploaded', 'failed')")
    op.drop_column("cloud_job", "inadmissible")
    op.drop_column("cloud_job", "attempts")
    op.drop_column("cloud_job", "kueue_workload")
```
CRITICAL banner to copy (`025:15-17`): "this migration touches ONLY ``cloud_job``; never reference ``saq_jobs``." Note the bare-constraint-name idiom (`025:56-63`): pass `status_enum`, NOT the prefixed `ck_cloud_job_status_enum` (the naming convention re-prefixes).

---

### `src/phaze/config.py` (MODIFY — D-08 + kube fields)

**Analog:** `push_max_attempts` (`config.py:421-427`) + `cloud_max_in_flight` (`config.py:411-417`) + `SECRET_FILE_FIELDS` (`config.py:345-350`).

**`cloud_submit_max_attempts`** — mirror `push_max_attempts` exactly:
```python
cloud_submit_max_attempts: int = Field(
    default=3, gt=0, lt=20,
    validation_alias=AliasChoices("PHAZE_CLOUD_SUBMIT_MAX_ATTEMPTS", "cloud_submit_max_attempts"),
    description="Max kube-submit re-drives before a failed/evicted file is marked ANALYSIS_FAILED (Phase 54, D-08). Default 3; bounded gt=0, lt=20.",
)
```

**Kube config fields** (RESEARCH §kr8s Client & Config table) — all OPTIONAL (`default=None`) in Phase 54; do NOT couple to `cloud_burst_enabled` (the fail-fast validator is Phase 55's). Mirror the `s3_endpoint_url`/`s3_bucket` optional-field shape (`config.py:447-466`): `kube_api_url`, `kube_namespace`, `kube_local_queue`, `kube_job_image`, `kube_job_cpu_request`, `kube_job_memory_request`, `kube_workload_api_version` (default `"kueue.x-k8s.io/v1beta1"`).

**`_FILE` secret machinery** (`config.py:345-350`) — add kube creds to `ControlSettings.SECRET_FILE_FIELDS`:
```python
SECRET_FILE_FIELDS: ClassVar[frozenset[str]] = BaseSettings.SECRET_FILE_FIELDS | {
    "openai_api_key", "anthropic_api_key", "s3_access_key_id", "s3_secret_access_key",
    "kube_kubeconfig", "kube_sa_token",     # Phase 54: kube creds via <VAR>_FILE convention (T-53-01 discipline)
}
```
The `_resolve_secret_files` before-validator (`config.py:90-148`) auto-resolves `<VAR>_FILE` siblings — no new resolver code needed. Kube creds are `SecretStr | None`, never logged.

---

### `src/phaze/services/enqueue_router.py` (MODIFY)

**Analog:** the `CONTROLLER_TASKS` frozenset (`enqueue_router.py:44-58`). Add `submit_cloud_job` (control-plane work — kube creds live there). Do NOT add `reconcile_cloud_jobs` (cron-only, like `reap_stalled_scans` — intentionally omitted from the routable set, see the comment at `enqueue_router.py:53-58`):
```python
CONTROLLER_TASKS: frozenset[str] = frozenset({
    "generate_proposals", "search_tracklist", "scrape_and_store_tracklist",
    "match_tracklist_to_discogs", "refresh_tracklists",
    "submit_cloud_job",          # Phase 54: fast kube-submit producer (control-plane; kube creds here)
})
```

---

### `src/phaze/tasks/controller.py` (MODIFY)

**Analog:** the `cron_jobs` block (`controller.py:215-235`) + `functions` list (`controller.py:205-213`) + the import block (`controller.py:40-43`).

- Import `submit_cloud_job` and `reconcile_cloud_jobs` (mirror the `stage_cloud_window` import at `controller.py:41`).
- Add `submit_cloud_job` to `settings["functions"]` (it is operator/cron-enqueueable).
- Register the cron mirroring `stage_cloud_window` (`controller.py:234`), with the narrow-scope guard comment:
```python
CronJob(reconcile_cloud_jobs, cron="*/5 * * * *"),  # type: ignore[type-var]
```
Do NOT add `reconcile_cloud_jobs` to `functions` if it is cron-only (mirror how `reap_stalled_scans` IS in functions but is cron-only — confirm SAQ requires cron fns to also be registered; `reap_stalled_scans` appears in both `functions` and `cron_jobs`, so add `reconcile_cloud_jobs` to both like it).

---

### Inadmissible alert (D-06) — pipeline UI

**Analog:** `src/phaze/templates/pipeline/partials/awaiting_cloud_card.html` (the OOB count-card pattern) + `routers/pipeline.py` dashboard/stats wiring (`pipeline.py:431-548`) + `services/pipeline.py` degrade-safe count readers (e.g. `get_awaiting_cloud_count` at `pipeline.py:805`).

**Count reader** (`services/pipeline.py:805-836` shape) — add `get_inadmissible_count(session)` returning `COUNT(cloud_job WHERE inadmissible = true)`, degrade-safe (a failed read surfaces as 0).

**Card partial** (`awaiting_cloud_card.html` — copy structure): a `<section id="inadmissible-card" {% if oob %}hx-swap-oob="true"{% endif %}>` rendered inline in `dashboard.html` (outside `#pipeline-stats`) and re-pushed by `stats_bar.html` as an OOB fragment on the 5s poll. Show the banner ONLY when count > 0 (healthy `Pending` stays invisible). Suggested copy: "K8s Jobs not admitting — check LocalQueue config".

**Dashboard wiring** (`pipeline.py:477-518`) — seed `inadmissible_count` into both the `dashboard()` context and the `/pipeline/stats` poll context (mirror `straggler_count` / `awaiting_cloud_count` at `pipeline.py:477-547`). Register the include in `dashboard.html:19-30` alongside the existing cloud cards.

Plus a WARNING log line in reconcile when the flag is set (D-06).

---

### Tests

**Seam tests** `tests/test_services/test_kube_staging.py` — Layer 2 (respx). Analog: `test_s3_upload.py:56-124` for the `@respx.mock` + `respx.put(url).mock(return_value=httpx.Response(...))` idiom, and `test_s3_staging.py:36-58` for the env-fixture + `get_settings.cache_clear()` discipline. Stub kr8s discovery endpoints (`GET /api`, `/apis`, `/apis/{group}/{version}`) in a shared `kube_respx` conftest fixture (RESEARCH Pitfall 5). Cover create/201, create/409 (`ServerError`), get, list-by-label, delete/200, delete/404 (idempotent).

**Logic tests** `tests/test_tasks/test_{submit,reconcile}_cloud_jobs.py` — Layer 1 (monkeypatch the seam). Analog: `test_cloud_staging.py` / `test_staging_cron.py` (monkeypatched producer + fake queue). Use the `fake_workload`/`fake_job` `SimpleNamespace` factories from RESEARCH §Fake-Kube Layer 1. Cover the full condition→outcome state machine (the highest-value coverage) per the VALIDATION table in RESEARCH lines 477-495.

**Migration test** `tests/test_migrations/test_migration_026_*.py` — Analog: `tests/test_migrations/test_migration_025_cloud_job.py`. Assert upgrade/downgrade + the new CHECK membership includes the new status members.

---

## Shared Patterns

### S3 delete reuse on no-callback terminal (D-05)
**Source:** `src/phaze/services/s3_staging.py:194-210` (`delete_staged_object`, idempotent) + the cloud-guarded call site `src/phaze/routers/agent_analysis.py:96-117` (`_delete_staged_object_if_cloud`).
**Apply to:** `reconcile_cloud_jobs` on no-callback terminal outcomes (Failed/Evicted/lost). NO new delete logic — call the existing capability:
```python
# agent_analysis.py:96-117 — the cloud-guard short-circuits before any s3_staging call (all-local safe).
has_cloud_job = (await session.execute(select(CloudJob.id).where(CloudJob.file_id == file_id))).scalar_one_or_none()
if has_cloud_job is not None:
    await s3_staging.delete_staged_object(file_id)   # idempotent (swallows already-absent)
```
Success path does NOT delete here — the `/api/internal/agent/*` callback already deleted inline (Phase 53 D-02). Reconcile only deletes on the no-callback terminal.

### Callback authority — reconcile NEVER writes a result (KSUBMIT-03)
**Source:** `src/phaze/routers/agent_analysis.py` (`put_analysis` line 126 / `report_analysis_failed` line 238 — the ONLY analysis-result writers, keyed by `file_id`).
**Apply to:** `reconcile_cloud_jobs` — it reads kube state ONLY to drive cleanup/re-drive/alerting; it must never call `put_analysis`/`report_analysis_failed` or write an `AnalysisResult`. A `Complete` Job with a prior callback is just a cleanup trigger.

### No-ledger-seed invariant (KSUBMIT-06 / CLOUDROUTE-02)
**Source:** `src/phaze/tasks/reenqueue.py:190-199, 327-340` (`recover_orphaned_work` replays `SchedulingLedger` `process_file` rows onto a local agent queue).
**Apply to:** `submit_cloud_job` — write ONLY the `cloud_job` row, NEVER a `SchedulingLedger` `process_file:<id>` row. The `cloud_job` row is the in-flight registry the cron iterates (D-02); a ledger row would let recovery re-enqueue the K8s file onto a local agent queue. Add an AST/boundary test asserting the submit path takes no `SchedulingLedger` write.

### Deterministic enqueue key + idempotency
**Source:** `cloud_staging.py:114` (`key=f"s3_upload:{file.id}"`) / `release_awaiting_cloud.py:71-78` (`push_file_job_key`) / `agent_push.py:106-117` (current-state-guarded UPDATE rowcount idempotency).
**Apply to:** `submit_cloud_job` key `submit_cloud_job:<file_id>`; the deterministic Job name `phaze-analyze-<file_id>` gives kube-side idempotency (409 → refresh).

### Fail-fast settings access on the control role
**Source:** `release_awaiting_cloud.py:118-127` + `controller.py:85-95` (read `ControlSettings`-only fields via `get_settings()` with a `# type: ignore[attr-defined]`; the control role guarantees the type).
**Apply to:** reading `cloud_submit_max_attempts` / `kube_*` in the cron + task.

---

## No Analog Found

| File | Role | Data Flow | Reason |
|------|------|-----------|--------|
| (none) | — | — | Every component has a direct in-repo precedent. The only genuinely new dependency is `kr8s` itself; its API shape is documented in RESEARCH §kr8s Client & Config and §Status → Outcome Mapping. |

The Kueue `Workload` CRD read (`new_class(kind="Workload", version="kueue.x-k8s.io/v1beta1")`) and the suspended-Job manifest dict have no in-repo precedent — use the RESEARCH §Suspended-Job Spec and §Status → Outcome Mapping sections directly (verified against Context7 `/kr8s-org/kr8s` + `/kubernetes-sigs/kueue`).

---

## Metadata

**Analog search scope:** `src/phaze/services/`, `src/phaze/tasks/`, `src/phaze/models/`, `src/phaze/routers/`, `src/phaze/config.py`, `alembic/versions/`, `src/phaze/templates/pipeline/`, `tests/test_services/`, `tests/test_tasks/`, `tests/test_migrations/`.
**Files scanned:** 14 read in full/part + directory listings.
**Pattern extraction date:** 2026-06-27
</content>
</invoke>
