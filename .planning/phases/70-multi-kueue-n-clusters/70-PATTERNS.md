# Phase 70: Multi-Kueue (N Clusters) - Pattern Map

**Mapped:** 2026-07-04
**Files analyzed:** 15 source + 5 test (18 modified, 2 new)
**Analogs found:** 20 / 20 (this is a behavior-preserving *generalization* — nearly every analog is the file's own current single-cluster body, plus one cross-phase analog per pattern)

> **Framing for the planner:** This phase almost never introduces a new file archetype. The dominant
> pattern is *lift a module-global read up to a per-backend parameter* and *reorder one delete*. The
> "closest analog" for most modified files is therefore **the same file's existing single-cluster
> shape** (the private helpers `_api(kube)` / `_client(bucket)` / `build_job_manifest(file_id, kube)`
> already take the config — only the public verbs still resolve it globally). Where a genuinely new
> construct appears (`pick_bucket`, migration 030, the clean-before-flip reorder), a concrete
> cross-phase analog is named with line numbers.

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `alembic/versions/030_add_cloud_job_staging_bucket.py` | migration | transform (DDL) | `alembic/versions/029_add_cloud_job_backend_id.py` | exact |
| `src/phaze/models/cloud_job.py` | model | CRUD | its own `backend_id` column (L96-98) | exact (self) |
| `src/phaze/services/kube_staging.py` | service | request-response (kube API) | its own `_api(kube)`/`build_job_manifest(file_id, kube)` (L92-114) | exact (self) |
| `src/phaze/services/s3_staging.py` | service | file-I/O (S3) | its own `_client(bucket)` (L86-102) | exact (self) |
| `src/phaze/services/cloud_staging.py` | service | CRUD + event-driven | its own `_stage_file_to_s3` upsert (L104-125) | exact (self) |
| `src/phaze/services/backends.py` | service | event-driven (dispatch) | `KueueBackend.dispatch`/`.reconcile` (L321-396) + `ComputeAgentBackend.dispatch` stamp (L268-282) | exact (self) |
| `src/phaze/tasks/release_awaiting_cloud.py` | task/cron | batch (drain snapshot loop) | its own per-candidate `try/except NoActiveAgentError` (L192-198) | exact (self) |
| `src/phaze/tasks/reconcile_cloud_jobs.py` | task/cron | event-driven (reconcile) | `_handle_no_callback_terminal` at-cap branch (L170-189) | exact (self) |
| `src/phaze/config.py` | config | transform (accessor) | `active_kube`/`active_bucket`/`active_compute_scratch_dir` (L482-518) | exact (self) |
| `src/phaze/config_backends.py` | config | — (model field add) | `KubeConfig` fields (L136-171) | exact (self) |
| `src/phaze/routers/agent_files.py` | route | request-response | `presign_get(file_id)` call site (L178) | role-match |
| `src/phaze/routers/agent_analysis.py` | route | request-response | `_delete_staged_object_if_cloud` (L98-119) | role-match |
| `src/phaze/routers/agent_s3.py` | route | request-response | upload-failure delete (L192) | role-match |
| `src/phaze/routers/agent_push.py` | route | request-response | `settings.active_compute_scratch_dir` read (L133) | role-match (companion fix) |
| `src/phaze/services/backend_selection.py` (read-only confirm) | service | transform | existing `BackendSlot` snapshot type | n/a |
| `tests/integration/test_migrations/test_migration_030_staging_bucket.py` | test | transform | `test_migration_029_backend_id.py` | exact |
| `tests/analyze/services/test_kube_staging.py` | test | seam (respx) | `_StubCfg` + respx fixtures (L45-75) | exact (self) |
| `tests/analyze/services/test_s3_staging.py` | test | seam (moto) | `moto_s3_server`/`s3_env` fixtures (L35-70) | exact (self) |
| `tests/analyze/services/test_backends.py` | test | seam (monkeypatch) | `_kueue`/`_seed_cloud_job`/`_stub_s3` (L63-116) | exact (self) |
| `tests/analyze/tasks/test_reconcile_cloud_jobs.py` | test | seam (fake-kube) | `DeleteJobSpy`/`S3DeleteSpy`/`_patch_seam` (L84-163) | exact (self) |

---

## Pattern Assignments

### `alembic/versions/030_add_cloud_job_staging_bucket.py` (migration, DDL transform) — NEW

**Analog:** `alembic/versions/029_add_cloud_job_backend_id.py` (mirror it verbatim — Claude's Discretion in CONTEXT §"Exact ... additive migration mechanics ... mirrors Phase 68 D-06").

**Full analog shape (029, L37-46):**
```python
def upgrade() -> None:
    op.add_column("cloud_job", sa.Column("backend_id", sa.String(255), nullable=True))
    op.alter_column("cloud_job", "s3_key", existing_type=sa.String(255), nullable=True)  # D-08

def downgrade() -> None:
    op.alter_column("cloud_job", "s3_key", existing_type=sa.String(255), nullable=False)
    op.drop_column("cloud_job", "backend_id")
```

**Copy for 030:** single `add_column("cloud_job", sa.Column("staging_bucket", sa.String(255), nullable=True))`
in `upgrade`; single `drop_column("cloud_job", "staging_bucket")` in `downgrade`. No `s3_key` leg (that
was 029-only). No CHECK/enum change (plain free-text like `backend_id`, D-01/D-06). No backfill (029's
rationale applies verbatim: "the a1/k8s paths were never deployed live so there are ~zero rows").

**Revision header (029 L18-34):** `revision = "030"`, `down_revision = "029"`, `branch_labels = None`,
`depends_on = None`, bare-number id. **Keep the CRITICAL "never reference `saq_jobs`" banner** (029 L14-16)
— the migration-test greps for it.

---

### `src/phaze/models/cloud_job.py` (model, CRUD) — MODIFIED

**Analog:** the existing `backend_id` column on the same model (L96-98) — copy its shape exactly.

**Column to copy (L96-98):**
```python
# Phase 68 (D-06): config-derived backend registry id stamped at dispatch ... NULLABLE with NO backfill
# ... Plain free-text (no CHECK/enum).
backend_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
```

**Add (D-01/D-02):** `staging_bucket: Mapped[str | None] = mapped_column(String(255), nullable=True)` —
records which `BucketConfig.id` staged the current object. **Do NOT touch `unique(file_id)`** (L72) — D-02
one-row-per-file invariant is preserved. No `__table_args__` CHECK change (L100-109) — free-text.

---

### `src/phaze/services/kube_staging.py` (service, kube request-response) — MODIFIED

**Analog:** the module's own already-parameterized private helpers. `_api(kube)` (L92), `build_job_manifest(file_id, kube)`
(L114), and `submit_job`'s internal `Job(build_job_manifest(file_id, kube), api=api)` (L230) **already take
`kube: KubeConfig`**. Only the public verbs resolve it via the module-global `_kube_config()` (L74-89, reads
`cfg.active_kube`).

**What to change — retire the global read (D-04, MKUE-01):**
- Delete `_kube_config()` (L74-89) — the `active_kube` reader.
- Add a `kube: KubeConfig` parameter to each public verb: `submit_job` (L221), `get_job` (L241),
  `get_local_queue` (L250), `list_inflight_jobs` (L269), `get_workload_for` (L281), `delete_job` (L306).
  Each currently opens with `kube = _kube_config()` (e.g. L228, L244, L261) — replace that line with the
  passed-in param.

**Token-hack to RETIRE (L101-111) — this is the crux of D-04:**
```python
# CURRENT (retire): post-construction session rebuild via kr8s private API
api = await kr8s.asyncio.api(url=kube.api_url, namespace=kube.namespace)
token = kube.sa_token.get_secret_value() if kube.sa_token else None
if token:
    api.auth.token = token
    await api._create_session()   # ← fragile private kr8s API, rebuilt per-client across N clients
return api
```
**Replace with the constructor-time synthesized-kubeconfig-dict form** (RESEARCH Pattern 1, verified live
against kr8s 0.20.15): build a dict via `yaml.safe_load(kube.kubeconfig.get_secret_value())` when
`kubeconfig` is set, else synthesize a minimal dict from `api_url`+`sa_token`+`namespace`; then
`return await kr8s.asyncio.api(kubeconfig=<dict>, namespace=kube.namespace, context=kube.context)`.
RESEARCH §Anti-Patterns: **never call `kr8s.asyncio.api()` with no args** (returns an arbitrary cached
client → wrong cluster).

**Companion (A1 / config_backends change):** `KubeConfig` has **no `context` field today** (verified
`config_backends.py` L136-157). Planner adds `context: str | None = None` (see that file's assignment).

**`get_local_queue` is the MKUE-03 reachability probe** (L250-266) — `KueueBackend.is_available` calls it
(`backends.py` L315). After parameterization the call becomes `await kube_staging.get_local_queue(self.config.kube)`.

**Purity constraint (kept):** module has NO ORM imports (L9-13 docstring + import-boundary test). Do not
add any; the caller resolves config.

---

### `src/phaze/services/s3_staging.py` (service, S3 file-I/O) — MODIFIED

**Analog:** the module's own `_client(bucket)` (L86-102) — **already takes `bucket: BucketConfig`**. Only the
public verbs resolve it via `_staging_config()` (L68-83, reads `cfg.active_bucket`), and the literal
TRANSITIONAL marker at **L77** (`# TRANSITIONAL — Phase 68 (per-file bucket selection = Phase 70 MKUE-02)`)
is the exact spot D-06 lands.

**What to change (MKUE-02):**
- Delete `_staging_config()` (L68-83).
- Add a `bucket: BucketConfig` param to each public verb: `create_multipart_upload` (L105),
  `presign_upload_parts` (L115), `complete_multipart_upload` (L135), `abort_multipart_upload` (L162),
  `presign_get` (L183), `delete_object`/`delete_staged_object` (L201), `ensure_bucket_lifecycle_ttl` (L220).
  Each opens with `cfg, bucket = _staging_config()` (or `_cfg, bucket`) — replace with the passed param.
  The kept-global tuning knobs (`s3_presign_put_ttl_sec` L129, `s3_presign_get_ttl_sec` L196,
  `s3_lifecycle_ttl_days` L237, `s3_multipart_part_size_bytes`) stay read from `cfg`/`get_settings()`.

**NEW pure helper `pick_bucket` (D-06) — add near the L77 marker.** RESEARCH Pattern 2, verified sound:
```python
def pick_bucket(file_id: uuid.UUID, bucket_ids: list[str]) -> str:
    ordered = sorted(bucket_ids)
    if not ordered:
        raise S3StagingError("kueue backend resolves to an empty bucket set")
    digest = hashlib.sha256(file_id.bytes).digest()
    index = int.from_bytes(digest, "big") % len(ordered)
    return ordered[index]
```
Stable hash (sha256 of UUID bytes) — **NOT** Python's salted `hash()`. `sorted()` for a stable order. The
chosen id is the authoritative record written to `cloud_job.staging_bucket`; cleanup/presign **read it, never
re-derive** (Anti-Pattern in RESEARCH §L279).

**Idempotent-delete idiom to reuse (D-03 best-effort):** `delete_staged_object` (L201-217) already swallows
`_DELETE_ABSENT_CODES = {"NoSuchKey", "NoSuchUpload", "404"}` (L43) — "a missing object is the desired end
state." This is exactly the primitive the clean-before-flip delete calls.

**Purity constraint (kept):** NO ORM imports (L11-14 docstring). The router/caller resolves the
`BucketConfig`; `s3_staging` never imports the model (Pitfall 4 / import-boundary test).

---

### `src/phaze/services/cloud_staging.py` (service, CRUD + event-driven) — MODIFIED

**Analog:** `_stage_file_to_s3` (L71-148, the no-commit core) — its `cloud_job` upsert (L104-125) is where
`staging_bucket` gets stamped.

**Current upsert (L104-125)** stamps `id`, `file_id`, `s3_key`, `status`, `upload_id` via
`pg_insert(CloudJob).on_conflict_do_update(index_elements=["file_id"], set_={...})`. **D-01/D-06 change:**
thread the D-06-selected `bucket: BucketConfig` down (the KueueBackend picks it — see `backends.py` below)
and add `staging_bucket=bucket.id` to both the `.values(...)` and the conflict `set_={...}`. The S3 SDK calls
at L98-100 (`create_multipart_upload`, `presign_upload_parts`) also need the `bucket` param passed through
(they currently call the no-arg forms).

**Contract to preserve:** this is the **no-commit** core (L72-77 docstring, Landmine L1) — the drain owns the
single post-loop commit. Do not add a commit.

---

### `src/phaze/services/backends.py` (service, event-driven dispatch) — MODIFIED

**Analog 1 (stamp-in-same-txn):** `ComputeAgentBackend.dispatch` (L268-282) shows the `pg_insert(CloudJob)...on_conflict_do_update`
+ `session.execute(stmt)` pattern that stamps `backend_id` in the caller's uncommitted txn. `KueueBackend.dispatch`
(L338) already does `update(CloudJob).where(CloudJob.file_id == file.id).values(backend_id=self.id)`.

**D-06/D-01 change to `KueueBackend.dispatch` (L321-339):** before/with the `_stage_file_to_s3` call,
compute the bucket and stamp it. RESEARCH §"Stamping staging_bucket at stage time":
```python
file.state = FileState.PUSHING
bucket_id = pick_bucket(file.id, self.config.buckets)                 # D-06; self.config is the KueueBackend submodel
await _stage_file_to_s3(session, file, task_router, bucket=_resolve_bucket_config(cfg, bucket_id))
await session.execute(
    update(CloudJob).where(CloudJob.file_id == file.id)
    .values(backend_id=self.id, staging_bucket=bucket_id)            # record BOTH (was backend_id only, L338)
)
```
Note `self.config` is already carried by `_BaseBackend.__init__(config=...)` (L155-159) and bound in
`resolve_backends` (L419-420: `KueueBackend(..., config=entry)`). `self.config.buckets` is the id-list
(config_backends `KueueBackend.buckets`, L117). `self.config.kube` is the `KubeConfig`.

**D-04 threading:** `KueueBackend.is_available` (L306-319) currently calls `kube_staging.get_local_queue()`
with no args → change to `get_local_queue(self.config.kube)`. `KueueBackend.reconcile` (L341-396) delegates
to `_reconcile_one` which calls `kube_staging.get_job`/`get_workload_for`/`delete_job` — those get the
`self.config.kube` threaded through (see reconcile file below).

**Per-row reconcile guard to REUSE (D-07 basis):** `KueueBackend.reconcile` (L379-394) already wraps each row
in `try/except: await session.rollback()` so one bad row never aborts the tick. D-07 extends this exact
"never raise out of the tick" discipline to the **drain snapshot** (see `release_awaiting_cloud.py`).

**Advisory-lock acquire idiom (L385):** `await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY})`
— acquired at the TOP of each per-row unit; `_reconcile_one` commits per row → auto-releases. This is the
lock the clean-before-flip delete runs under.

---

### `src/phaze/tasks/release_awaiting_cloud.py` (task/cron, batch drain) — MODIFIED

**Analog:** the drain's own existing per-candidate guard `try/except NoActiveAgentError` (L192-198) — the
exact "catch → clean hold → continue/break, never raise" shape D-07 replicates at the snapshot loop.

**Current snapshot loop (L134-140) has NO per-backend guard (Pitfall 8 / D-07):**
```python
for backend in backends:
    snapshot[backend.id] = {
        "backend": backend,
        "available": await backend.is_available(session),
        "remaining": max(0, backend.cap - await backend.in_flight_count(session)),
        "cap": backend.cap,
    }
```
**D-07 change:** wrap each backend's `is_available()` + `in_flight_count()` in its own `try/except Exception`
→ treat a raise/timeout as `available=False, remaining=0`, log `{backend_id}`, continue. One flaky cluster
becomes 0 slots for that tick; healthy backends proceed.

**Current dispatch guard (L192-198)** already catches `NoActiveAgentError` per candidate. **D-07 widens** the
`except` on the `await target.dispatch(...)` call (L193) to also treat a generic kube/S3 raise as a clean hold
of that candidate (file stays `AWAITING_CLOUD`), matching the snapshot-loop discipline.

**Advisory-lock key (L74):** `_STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY = 5_000_504` — the same key reconcile
takes per-row (backends.py L385). The drain holds it tick-wide (L127), so it physically cannot claim a file
until reconcile's txn commits — this is the mutual exclusion the clean-before-flip depends on.

---

### `src/phaze/tasks/reconcile_cloud_jobs.py` (task/cron, event-driven reconcile) — MODIFIED

**Analog:** the at-cap spill-back branch of `_handle_no_callback_terminal` (L170-189) — the load-bearing
ordering to REORDER.

**Current ordering (L178-184) — commit FIRST, delete AFTER (unsafe for multi-bucket, Pitfall 9):**
```python
cloud_job.status = CloudJobStatus.FAILED.value
cloud_job.inadmissible = False
cloud_job.cloud_phase = None
await session.execute(update(FileRecord).where(FileRecord.id == file_id).values(state=FileState.AWAITING_CLOUD))
await session.commit()                            # ← releases the per-row advisory lock; file becomes a drain candidate
await s3_staging.delete_staged_object(file_id)    # ← delete AFTER (Pitfall 9 collision window)
await kube_staging.delete_job(name)
```
**Phase 70 clean-before-flip (D-01/D-03/MKUE-04) — RESEARCH Pattern 3:** capture the old identity into locals,
delete UNDER the still-held lock BEFORE the flip commit:
```python
old_bucket_id = cloud_job.staging_bucket
bucket = _resolve_bucket_config(cfg, old_bucket_id)
with contextlib.suppress(Exception):                        # D-03 best-effort/idempotent — never blocks re-dispatch
    if bucket is not None:
        await s3_staging.delete_object(file_id, bucket)     # old (backend,bucket) object, BEFORE flip
cloud_job.status = CloudJobStatus.FAILED.value
cloud_job.staging_bucket = None                             # clear so the record can't mislead pre-repurpose
await session.execute(update(FileRecord)...values(state=FileState.AWAITING_CLOUD))
await session.commit()                                      # ← releases lock; old object ALREADY gone
await kube_staging.delete_job(name, kube=<this backend's KubeConfig>)  # Job delete stays post-commit (D-04)
```
**Why under the lock:** RESEARCH §L274 — capturing-then-deleting-after is necessary but not sufficient; a
re-dispatch reusing the same `file_id`-scoped key on the same D-06 bucket would race a delete that runs after
the lock releases. Deleting before the commit that makes the file a drain candidate guarantees the old object
is gone first.

**Best-effort swallow idiom:** mirror `redrive_upload`'s `with contextlib.suppress(Exception):` around
`abort_multipart_upload` (`cloud_staging.py` L163-164) — same "cleanup failure never blocks the re-stage."

**`delete_job` threading (D-04):** `_handle_no_callback_terminal` / `_record_success` (L139, L184, L192) and
`_reconcile_one` (L207-235) call `kube_staging.get_job`/`get_workload_for`/`delete_job` with no `kube` arg.
Thread this backend's `KubeConfig` through (the `KueueBackend.reconcile` caller has `self.config.kube`; pass
via `ctx` or as a param to `_reconcile_one`).

**Bucket-resolve helper to ADD (RESEARCH §"Resolving a BucketConfig by recorded id"):**
```python
def _resolve_bucket_config(cfg: ControlSettings, bucket_id: str | None) -> BucketConfig | None:
    if bucket_id is None:
        return None
    return {b.id: b for b in cfg.buckets}.get(bucket_id)
```
Mirrors the `bucket_by_id` lookup already in `config.py` `active_bucket` (L511).

---

### `src/phaze/config.py` (config, accessor transform) — MODIFIED

**Analog:** the three transitional accessors `active_compute_scratch_dir` (L482-489), `active_kube` (L491-498),
`active_bucket` (L500-518), all reducing through `_single_non_local()` (L463-480, raises on >1 non-local).

**D-04/MKUE change:** retire `active_kube` and `active_bucket` (their consumers now thread per-backend config).
**Pitfall 1 companion fix (load-bearing, NOT the deferred D-05 agent_ref fix):** re-base
`active_compute_scratch_dir` (L483-489) off a **single-compute** reduction
(`[b for b in self.backends if b.kind == "compute"]`, ≤1 until PROV-01) instead of `_single_non_local()`.
Reason: with local + N-Kueue + 1-compute there are ≥2 non-local backends, so `_single_non_local()` **raises**
and the `/pushed` callback 500s (see `agent_push.py`). `resolved_non_local_kind` in `backends.py` (L425-444)
is the sibling reduction pattern to mirror for "single compute, fail-fast on >1."

---

### `src/phaze/config_backends.py` (config, model field add) — MODIFIED

**Analog:** the `KubeConfig` optional fields (L144-157) — `api_url`, `namespace`, `local_queue`, `kubeconfig`
(`SecretStr`), `sa_token` (`SecretStr`), `workload_api_version` (default).

**A1 change (MKUE-01):** add `context: str | None = None` to `KubeConfig` (REG-05 says "per-cluster
kubeconfig/context"; absent today). Additive, defaults to current-context when None. Not a secret — plain
`str | None` like `namespace` (L145). `kube_staging._api` reads it (`context=kube.context`).

**Note:** if importing PyYAML directly (for the kubeconfig-dict parse), declare it explicitly in
`pyproject.toml` `[project].dependencies` (alphabetical, per CLAUDE.md ordering). It is already a transitive
kr8s dep — this is hygiene, not a "new dependency."

---

### Router call sites — `agent_files.py` / `agent_analysis.py` / `agent_s3.py` (routes, request-response) — MODIFIED

**Analog:** each already queries `cloud_job` for the file and has `session` in scope; extend the query to also
read `staging_bucket`, resolve `BucketConfig` from `cfg.buckets`, pass it to the parameterized S3 verb
(Pitfall 4 — the router resolves the bucket so `s3_staging` stays ORM-free).

| Call site | Line | Current | Phase 70 |
|-----------|------|---------|----------|
| `agent_files.presign_get` | L178 | `await s3_staging.presign_get(file_id)` | read `cloud_job.staging_bucket` → `presign_get(file_id, bucket)` |
| `agent_analysis._delete_staged_object_if_cloud` | L119 | `await s3_staging.delete_staged_object(file_id)` | resolve recorded bucket → `delete_staged_object(file_id, bucket)` |
| `agent_s3` upload-failure delete | L192 | `await s3_staging.delete_staged_object(file_id)` | resolve recorded bucket → pass it |

**Anti-pattern to avoid (Pitfall 4 / RESEARCH §L279):** do **not** re-derive via `pick_bucket` at presign/delete
time — always read the recorded `staging_bucket` column (config drift / in-place `backend_id` repurpose would
point a re-derive at the wrong bucket).

### `agent_push.py` (route, request-response) — MODIFIED (Pitfall 1 companion)

**Analog:** `settings.active_compute_scratch_dir` read at L133 (`scratch_path = f"{settings.active_compute_scratch_dir}/{file_id}.{file.file_type}"`).
No code change here beyond consuming the re-based accessor (see `config.py`) — but the planner must verify
this call no longer routes through `_single_non_local()` once a 2nd non-local backend is configured, else
`/pushed` 500s. **Warning sign:** `/pushed` returns 500 with "multi-backend dispatch lands in Phase 69"
ValueError text.

---

## Shared Patterns

### Advisory-lock serialization (drain ↔ reconcile mutual exclusion)
**Source:** `release_awaiting_cloud.py` L74 (`_STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY = 5_000_504`), acquired
tick-wide in the drain (L127) and per-row in reconcile (`backends.py` L385).
**Apply to:** the clean-before-flip delete (`reconcile_cloud_jobs.py`) — the delete runs while this lock is
still held, before the flip commit that releases it.
```python
await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY})
```
**Keep per-row in reconcile** (Anti-Pattern: a whole-tick reconcile lock breaks the mid-tick commit
granularity the delete-before-flip ordering depends on — RESEARCH §L282).

### Idempotent best-effort delete
**Source:** `s3_staging.delete_staged_object` L201-217 (swallows `_DELETE_ABSENT_CODES` L43); `cloud_staging.redrive_upload`
L163-164 (`contextlib.suppress(Exception)` around abort).
**Apply to:** the clean-before-flip S3 delete (D-03 "swallow already-absent/failed-delete so a failed delete
never blocks re-dispatch"). Wrap the parameterized `delete_object(file_id, bucket)` in `contextlib.suppress(Exception)`.

### Config-derived id stamped in the caller's uncommitted txn (D-03 write ordering)
**Source:** `ComputeAgentBackend.dispatch` L268-282 (`pg_insert(CloudJob).on_conflict_do_update(index_elements=["file_id"], set_={...})`)
and `KueueBackend.dispatch` L338 (`update(CloudJob).where(file_id==...).values(backend_id=...)`).
**Apply to:** stamping `staging_bucket` alongside `backend_id` at stage time (`backends.py` / `cloud_staging.py`) —
same session, before/with the `FileState → PUSHING` flip, never after a separate commit.

### Never-raise-out-of-the-tick discipline
**Source:** `KueueBackend.reconcile` per-row `try/except: await session.rollback()` (L379-394);
`KueueBackend.is_available` broad `except Exception → return False` (L314-319); drain's per-candidate
`except NoActiveAgentError` (L192-198).
**Apply to:** D-07 per-backend `try/except` in the drain snapshot loop (`release_awaiting_cloud.py` L134-140)
and the widened dispatch guard.

### Secret hygiene (T-68-04)
**Source:** `backends.py` docstring L32-33 ("logs only `{id, kind, rank, cap}`, never a `SecretStr`/`*_file`/kube SA token").
**Apply to:** all new logging in this phase — never log the synthesized kubeconfig dict, `sa_token`, or bucket creds.

---

## Test Pattern Assignments

### `tests/integration/test_migrations/test_migration_030_staging_bucket.py` — NEW
**Analog:** `test_migration_029_backend_id.py` (mirror verbatim). Copy: the `_MIGRATION_PATH` + `skipif`
Wave-0 guard (L43-50), `_load_migration_030` importlib loader (L53-60), `test_revision_identifiers_are_bare_numbers`
(L68-74 → assert `"030"`/`"029"`), `test_migration_never_references_saq_jobs` (L77-82), `_seed_file` FK helper
(L85-95), and the upgrade/downgrade round-trip body (L98-178) using `information_schema.columns` probes for
`staging_bucket` existence + `is_nullable == "YES"`. **Drop** the 029-specific `s3_key`-NOT-NULL leg — 030 is
pure add/drop of one nullable column.

### `tests/analyze/services/test_kube_staging.py` — MODIFIED (MKUE-01)
**Analog:** `_StubCfg` (L45-69) building a real `KubeConfig` + the respx `kube_respx` seam (L1-15 docstring),
`_JOBS_PATH`/`_WL_PATH`/`_LQ_PATH` route constants (L40-42).
**Add:** synthesized-kubeconfig-dict auth cases for BOTH forms (`kubeconfig`+`context`, `api_url`+`sa_token`);
distinct-client-per-backend (two `KubeConfig`s → two cached `Api`); assert NO `_create_session` usage. After
parameterization, the stub passes a `KubeConfig` param directly instead of `monkeypatch`-ing `active_kube`.

### `tests/analyze/services/test_s3_staging.py` — MODIFIED (MKUE-02)
**Analog:** `moto_s3_server` (ThreadedMotoServer, L35-42) + `s3_env` driving a one-kueue-backend `backends.toml`
via `backends_toml_env` (L46-70).
**Add:** `pick_bucket` determinism + stability-across-restart + empty-set-raises unit cases; per-bucket
`BucketConfig`-param cases for `presign_get`/`delete_object` (multi-bucket set). `pick_bucket` cases are pure
(no moto needed).

### `tests/analyze/services/test_backends.py` — MODIFIED (MKUE-02/03)
**Analog:** `_kueue(**kw)` factory (L73-76), `_seed_cloud_job(session, backend_id=, status=)` (L92-107),
`_stub_s3` (L110-112), `_stub_kube_available` (L115-116), `_make_file` (L78-90).
**Add:** an N≥2 `KueueBackend` fixture (each with its own `config.kube` + `config.buckets`); assert `dispatch`
stamps `staging_bucket = pick_bucket(...)`; the `_kueue` factory needs a `config=` arg now (Wave-2 finalizes
the signature — the factory docstring L75 already flags "single registry entry").

### `tests/analyze/tasks/test_reconcile_cloud_jobs.py` — MODIFIED (MKUE-04)
**Analog:** `DeleteJobSpy` + `S3DeleteSpy` recording call order into a shared `events` list (L84-119),
`_patch_seam` monkeypatching `kube_staging.get_job`/`get_workload_for`/`delete_job` + `s3_staging.delete_staged_object`
(L148-163), `_patch_cap` (L132-145), `_seed`/`_read_cloud_job` (L188-214), `fake_job`/`EVICTED`/`INADMISSIBLE`
from `tests.kube_fakes` (L43).
**Add:** clean-before-flip ordering assertion (the `events` list shows `s3_delete` BEFORE the AWAITING_CLOUD
commit / before `delete_job`); same-bucket re-dispatch preservation; a drain↔reconcile concurrency test
asserting no file ends in two backends. The `S3DeleteSpy` (L113-119) grows a `bucket` arg once `delete_object`
is parameterized.

### D-07 drain-isolation test
**Analog:** `tests/analyze/core/test_dispatch_snapshot.py` / `test_staging_cron.py` (existing snapshot/drain
homes). **Add:** an N≥2 backend fixture where one backend raises on `is_available`/`in_flight_count`/`dispatch`;
assert the tick survives and healthy backends still get work.

### Import-boundary guard (kept)
Keep `s3_staging` and `kube_staging` **ORM-free** after parameterization (the routers resolve config). Extend
the existing purity tests (kube_staging test L13-14 docstring: "pure kr8s seam with NO ORM imports").

---

## No Analog Found

None. Every construct this phase needs has a concrete in-repo analog (per-config `_client(bucket)` /
`_api(kube)`, idempotent delete, the advisory lock, the per-backend registry + per-row reconcile guard, the
029 migration + its test). RESEARCH §L294 confirms: "every primitive Phase 70 needs already exists in the
codebase." The single genuinely-new pure function `pick_bucket` has a verified reference implementation
(RESEARCH Pattern 2) and lands in `s3_staging.py`.

## Metadata

**Analog search scope:** `src/phaze/services/{backends,kube_staging,s3_staging,cloud_staging}.py`,
`src/phaze/tasks/{release_awaiting_cloud,reconcile_cloud_jobs}.py`, `src/phaze/models/cloud_job.py`,
`src/phaze/config.py`, `src/phaze/config_backends.py`, `src/phaze/routers/{agent_files,agent_analysis,agent_s3,agent_push}.py`,
`alembic/versions/029_*.py`, `tests/analyze/services/`, `tests/analyze/tasks/`, `tests/integration/test_migrations/`.
**Files scanned:** ~20 source + 5 test.
**Pattern extraction date:** 2026-07-04
