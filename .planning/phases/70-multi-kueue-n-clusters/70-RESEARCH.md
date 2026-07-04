# Phase 70: Multi-Kueue (N Clusters) - Research

**Researched:** 2026-07-04
**Domain:** Multi-cluster Kubernetes/Kueue dispatch, per-bucket S3 staging, kr8s client construction, Postgres advisory-lock concurrency
**Confidence:** HIGH (mechanics verified against live kr8s 0.20.15 + the actual repo code); the only LOW items are deployment-gated live-second-cluster E2E items, explicitly flagged.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01 (clean-before-flip + record staging bucket):** Add a nullable `staging_bucket` column to `cloud_job` recording which bucket id staged the current object. On spillover (a Kueue file returning to `AWAITING_CLOUD` to be re-dispatched to a different cluster/bucket), the old `(backend_id, staging_bucket)` object is deleted in the SAME transition, **BEFORE** `backend_id` / `staging_bucket` are repurposed. Active cleanup is primary; the per-bucket lifecycle TTL is a pure backstop.
- **D-02 (one-row-per-file):** `cloud_job` stays one-row-per-file (`backend_id` + new `staging_bucket` mutated in place; `unique(file_id)` preserved). No per-(file,backend) history table.
- **D-03 (reconcile owns cleanup, best-effort):** The `backend_id`-scoped `reconcile` issues the `delete_object` for the recorded `(backend_id, staging_bucket)` in the same transition, before repurposing the row. Best-effort: swallow already-absent/failed-delete errors so a failed delete never blocks re-dispatch; TTL backstops any miss. Cleanup is NOT in the hot dispatch path.
- **D-04 (support both kube-auth forms, retire the token-mutation hack):** Build a distinct kr8s client per backend from its `KubeConfig`, threaded through every `kube_staging` call (retire the module-global `active_kube` read). `kubeconfig`+`context` is the clean N-cluster path; `api_url`+`sa_token` stays supported but must replace the `api.auth.token = token; await api._create_session()` hack with a correct constructor-time auth form.
- **D-05 (defer compute agent_ref to PROV-01):** Phase 70 does NOT fix `ComputeAgentBackend`'s `select_active_agent(kind="compute")` heuristic. Kueue-only phase.
- **D-06 (stable hash of file_id):** `index = stable_hash(file_id) mod len(sorted(bucket_ids))`. Sort the bound bucket-id list; use a stable hash (sha256 of the UUID) — NOT Python's salted `hash()`. `staging_bucket` records the actual choice as the authoritative record. Cleanup/reconcile/presign re-agree via the recorded value, not a re-derive.
- **D-07 (per-backend try/except in snapshot + dispatch):** Wrap each backend's per-tick `is_available()` / `in_flight_count()` snapshot AND each `dispatch()` call in its own try/except; a raising/timing-out cluster is treated as unavailable (0 slots) and logged, other backends proceed.

### Claude's Discretion
- `cloud_job.staging_bucket` column type/name + additive migration mechanics (nullable, no backfill).
- Whether re-homed `kube_staging` functions take a `KubeConfig` parameter or become `KueueBackend` methods.
- Exact stable-hash primitive for D-06 + how the bucket-id list is sorted.
- Exact `pg_advisory_xact_lock` scope for the clean-before-flip delete relative to the drain snapshot (research flag).
- Whether presigned-GET minting picks the same D-06 bucket at mint time (it must — planner confirms the presign path reads the recorded `staging_bucket`, not a re-derive).

### Deferred Ideas (OUT OF SCOPE)
- Compute `agent_ref → Agent.id` resolution (PROV-01).
- Live multi-cluster kr8s auth verification against a real second cluster (Phase-56 carryover live-E2E item).
- N-lane admin UI, master revert-to-all-local toggle, operator runbook/config docs, `cloud_target`→`backends` migration doc (Phase 71, BEUI-01..03).
- Duration-scaled / per-backend reconcile cron cadence split (SREF-01).
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| MKUE-01 | N Kueue-cluster backends, each with its own kube config, dispatched concurrently from one control plane. | §kr8s per-backend client construction (retire token hack via synthesized in-memory kubeconfig dict — verified live). §Component Responsibilities (parameterize `kube_staging` with `KubeConfig`). |
| MKUE-02 | Each cluster stages to a bucket from its REG-05 set; multi-bucket set → deterministic per-file selection; control plane sole importer/presigner; pods credential-free. | §D-06 bucket-selection primitive (sha256(file_id.bytes) mod len(sorted(bucket_ids)) — verified sound). §Presign-GET must read recorded `staging_bucket`. §Call-site inventory (presign_get, inline deletes). |
| MKUE-03 | Per-cluster LocalQueue reachability probe + `backend_id`-scoped reconcile; one cluster's failure isolated (per-backend try/except) so it can't poison the drain tick. | §D-07 snapshot+dispatch guards. §Pitfall 8. `is_available` already returns bool/never-raises; add the loop-level guard in `stage_cloud_window`. |
| MKUE-04 | Cross-cluster/bucket staged-object cleanup scoped to the (backend,bucket) that staged it; spillover never deletes an object another cluster/bucket still uses; per-bucket TTL backstop. | §Clean-before-flip advisory-lock ordering (the crux — delete under the per-row lock, BEFORE the AWAITING_CLOUD flip commit). §Pitfall 9. §Per-(backend,bucket) delete identity. |
</phase_requirements>

## Summary

This is a **behavior-preserving generalization**, not a rewrite. The Phase 67–69 substrate already resolves N `KueueBackend` instances (`resolve_backends` no longer boot-guards on >1 non-local), iterates them once per tick in `stage_cloud_window`, and reconciles them per-backend/per-row under a shared advisory lock. What remains single-cluster is exactly two module-global reads — `cfg.active_kube` (in `kube_staging`) and `cfg.active_bucket` (in `s3_staging`) — plus the `api.auth.token` mutation hack. Phase 70 replaces those global reads with per-backend config threaded from `KueueBackend.config`, adds a deterministic per-file bucket choice recorded on a new `cloud_job.staging_bucket` column, and reorders the spillover cleanup so the old object is deleted **before** the file re-enters the drain candidate set.

The single sharpest correctness edge (research flag) is the **clean-before-flip advisory-lock scope**. The current at-cap spill-back in `reconcile_cloud_jobs._handle_no_callback_terminal` does `commit()` (which auto-releases the per-row `pg_advisory_xact_lock(5_000_504)` and flips the file to `AWAITING_CLOUD`) and only *then* deletes the S3 object. In a multi-bucket world that opens Pitfall 9: once committed+unlocked, a concurrent drain tick can re-dispatch the file and re-stage a new object under the *same* `file_id`-scoped key — and if the new dispatch's D-06 choice lands on the same bucket, the reconcile's trailing delete would destroy the freshly-staged object the new pod needs. The fix is to **delete the old `(backend_id, staging_bucket)` object while still holding the per-row lock, before the flip commit** — capturing the old identity into locals first. This is the literal meaning of "clean-before-flip."

The kr8s auth question is fully resolved and **verified live against the installed kr8s 0.20.15**: `KubeAuth` loads a bearer token, server URL, and namespace directly from an **in-memory kubeconfig dict** with no network call and no `_create_session()` rebuild. Both required auth forms unify onto one clean constructor-time mechanism (parse-or-synthesize a kubeconfig dict, pass it to `kr8s.asyncio.api(kubeconfig=<dict>, ...)`), and kr8s's arg-keyed cache treats distinct dicts as distinct clients with no shared-global mutation.

**Primary recommendation:** Thread `KueueBackend.config.kube` (a `KubeConfig`) and the D-06-selected `BucketConfig` down through the already-parameterized private helpers in `kube_staging`/`s3_staging`; retire `active_kube`/`active_bucket`; replace `_api()` with a synthesized-kubeconfig-dict client factory; add `cloud_job.staging_bucket` (migration 030, nullable, no backfill); move the spillover S3 delete to before the flip-commit inside the per-row advisory lock; and wrap each backend's snapshot+dispatch in the drain in its own try/except.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Per-cluster kube client build + Job submit/probe/reconcile | API/Backend (control plane) | — | DIST-01: kube creds live control-side only; `kube_staging` is the single kr8s home. Pods are credential-free. |
| Per-file bucket selection (D-06) + multipart stage + presign | API/Backend (control plane) | Database/Storage (S3) | Control plane is the sole S3 importer/presigner (MKUE-02). Pods only ever receive presigned URLs. |
| Deterministic bucket choice record | Database/Storage (`cloud_job.staging_bucket`) | — | The recorded choice is authoritative; presign/cleanup read it, never re-derive. |
| Spillover cleanup ordering (clean-before-flip) | API/Backend (`reconcile` under advisory lock) | Database/Storage | Correctness lives in the Postgres transaction/lock boundary, not in S3. |
| Per-backend failure isolation | API/Backend (drain snapshot+dispatch loop) | — | A flaky cluster is a routine steady state; isolation is a control-plane scheduling concern. |
| Credential-free pod GET | Browser/Client analog (one-shot pod) | — | Pod holds no bucket creds; receives a `file_id`-scoped TTL-bounded presigned GET only. |

## Standard Stack

No new dependencies. This is a pure application-code refactor on the pinned stack (milestone constraint: "Zero new dependencies").

### Core (already present, versions verified in-repo)
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| kr8s | 0.20.15 | Async Kubernetes/Kueue client (single home = `kube_staging`) | Already the project's kube client; supports in-memory kubeconfig-dict construction. `[VERIFIED: uv run python -c "import kr8s; kr8s.__version__"]` |
| aioboto3 / aiobotocore / botocore | (pinned) | Async S3 SDK (single home = `s3_staging`) | Already the project's S3 client; `_client(bucket)` already takes a per-bucket `BucketConfig`. `[VERIFIED: repo import]` |
| SQLAlchemy (async) + asyncpg | 2.0.x | ORM + Postgres advisory locks via `text("SELECT pg_advisory_xact_lock(:key)")` | Existing pattern in `release_awaiting_cloud.py` / `backends.py`. `[VERIFIED: repo grep]` |
| pydantic v2 | (pinned) | `KubeConfig` / `BucketConfig` submodels (Phase 67) | Config surface already exists per-entry. `[VERIFIED: config_backends.py]` |
| PyYAML | 6.0.3 | Parse inline kubeconfig YAML content → dict for kr8s | **Transitive** dependency of kr8s (guaranteed present). See Package Legitimacy note. `[VERIFIED: uv run python -c "import yaml; yaml.__version__"]` |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Synthesized in-memory kubeconfig dict for the `api_url`+`sa_token` form | Keep the `api.auth.token = token; await api._create_session()` hack | Rejected by D-04 — the hack rebuilds the httpx session per client across N clients; fragile, undocumented private API. |
| Parse kubeconfig YAML content → dict via PyYAML | Write kubeconfig content to a `NamedTemporaryFile` and pass the path to `kr8s.asyncio.api(kubeconfig=<path>)` | Temp-file lifecycle/cleanup is messier and touches disk with secret material; the dict path is in-memory only. Both are viable — planner's call (Discretion). |
| `int.from_bytes(sha256(file_id.bytes)) % n` | `int(file_id) % n` (raw 128-bit UUID modulo) | D-06 explicitly requires a stable digest; sha256 decorrelates in case file_ids ever become non-random (e.g., time-ordered UUIDv7). Negligible cost. |

## Package Legitimacy Audit

No packages are installed this phase (zero new dependencies). PyYAML is the only new *import* and it is already a transitive dependency of the pinned kr8s.

| Package | Registry | Age | Downloads | Source Repo | slopcheck | Disposition |
|---------|----------|-----|-----------|-------------|-----------|-------------|
| PyYAML (`yaml`) | PyPI | 15+ yrs | ~300M/mo | github.com/yaml/pyyaml | not run (already-resolved transitive dep of kr8s) | Approved — no new install; already in `uv.lock` via kr8s. |

**Packages removed due to slopcheck [SLOP] verdict:** none
**Packages flagged as suspicious [SUS]:** none

*Note for the planner (Discretion):* if importing PyYAML directly in phaze code, declare it explicitly in `pyproject.toml` under `[project].dependencies` (alphabetical, per CLAUDE.md `pyproject.toml` section order) rather than relying on a transitive edge — otherwise a future kr8s that drops PyYAML would silently break phaze. This is not a "new dependency" in the milestone sense (it is already resolved), but making the edge explicit is correct hygiene. Alternatively, the `NamedTemporaryFile` approach avoids any YAML import.

## Architecture Patterns

### System Architecture Diagram

```
                        stage_cloud_window (drain, */5 cron)
                                   │
              ┌────────────────────┴─────────────────────────┐
              │  pg_advisory_xact_lock(5_000_504)  [held all tick]
              │                                                │
              │  SNAPSHOT (once/tick, per backend):            │
              │    for b in resolve_backends(cfg):             │
              │      try:  is_available(b)  in_flight_count(b)  │  ← D-07 per-backend try/except
              │      except: treat as 0 slots, log, continue   │
              │                                                │
              │  SELECT AWAITING_CLOUD ... FOR UPDATE SKIP LOCKED
              │                                                │
              │  for file in candidates:                       │
              │    target = select_backend(...)  (rank-first)  │
              │    try:  target.dispatch(file, session, ...)   │  ← D-07 per-backend try/except
              │    except NoActiveAgentError|Exception: hold    │
              │         │                                      │
              │         ▼  KueueBackend.dispatch               │
              │    file.state = PUSHING                        │
              │    bucket = pick_bucket(file.id, sorted(cfg.buckets_for(b)))   ← D-06
              │    _stage_file_to_s3(session, file, ..., bucket)  (multipart, presign PUT)
              │    UPDATE cloud_job SET backend_id=b.id, staging_bucket=bucket.id  ← D-01 record
              │                                                │
              │  COMMIT once (releases lock + row locks)       │
              └────────────────────┬───────────────────────────┘
                                   │
   ┌───────────────────────────────┼─────────────────────────────────┐
   │ pod (cluster N, credential-free)                                  │
   │   GET /agent/files/{id}/download  → presign_get(file_id, bucket)  │  ← reads recorded staging_bucket
   │   downloads object via presigned URL from THAT bucket             │
   │   POST /agent/analysis/{id}  → put_analysis (sole result writer)  │
   │        → _delete_staged_object_if_cloud(file_id, recorded bucket) │  ← inline delete, recorded bucket
   └───────────────────────────────────────────────────────────────────┘

                     reconcile_cloud_jobs (*/5 cron)
                                   │
              for b in resolve_backends(cfg):
                 b.reconcile(session, ctx)   [KueueBackend only does work]
                                   │
              per row (backend_id == b.id, status IN {SUBMITTED,RUNNING}):
                 pg_advisory_xact_lock(5_000_504)   ← per-row, same key as drain
                 _reconcile_one(...)
                     at-cap spill-back (MKUE-04 clean-before-flip):
                        old_bid, old_bkt = cloud_job.backend_id, cloud_job.staging_bucket
                        delete_object(file_id, resolve(old_bkt))   ← BEFORE flip, UNDER lock, best-effort
                        cloud_job.status = FAILED
                        FileRecord.state = AWAITING_CLOUD
                        COMMIT  (releases lock; file now a drain candidate — but object already gone)
                        delete_job(name)   ← after commit (D-04 status-read-vs-GC, Job only)
```

### Recommended Change Shape (re-home, not rewrite)
```
src/phaze/
├── services/
│   ├── kube_staging.py     # _api(kube) already takes KubeConfig; lift active_kube read OUT of
│   │                       # submit_job/get_job/get_local_queue/list_inflight_jobs/get_workload_for
│   │                       # → add a `kube: KubeConfig` param to each; rewrite _api() to synth a
│   │                       #   kubeconfig dict (retire the token hack)
│   ├── s3_staging.py       # _client(bucket) already takes BucketConfig; lift active_bucket read OUT of
│   │                       # create_multipart_upload/presign_upload_parts/complete/abort/presign_get/
│   │                       #   delete_object/ensure_bucket_lifecycle_ttl → add a `bucket: BucketConfig` param
│   │                       # + add pick_bucket(file_id, bucket_ids) D-06 helper (pure)
│   ├── cloud_staging.py    # _stage_file_to_s3 → accept + stamp staging_bucket alongside the cloud_job upsert
│   └── backends.py         # KueueBackend: thread self.config.kube + D-06 bucket into dispatch/reconcile/is_available
├── models/cloud_job.py     # + staging_bucket: Mapped[str | None]  (nullable, plain free-text like backend_id)
├── tasks/
│   ├── release_awaiting_cloud.py   # stage_cloud_window: per-backend try/except in snapshot + dispatch (D-07)
│   └── reconcile_cloud_jobs.py     # _handle_no_callback_terminal: clean-before-flip S3 delete ordering (D-01/MKUE-04)
├── routers/
│   ├── agent_files.py      # presign_get call site → resolve cloud_job.staging_bucket → BucketConfig
│   ├── agent_analysis.py   # _delete_staged_object_if_cloud → resolve recorded bucket
│   └── agent_s3.py         # upload-failure delete → resolve recorded bucket
└── alembic/versions/030_add_cloud_job_staging_bucket.py   # additive, nullable, no backfill (mirror 029)
```

### Pattern 1: Per-backend kr8s client from a synthesized in-memory kubeconfig dict (D-04, MKUE-01)
**What:** Replace `_api()`'s post-construction token mutation with a constructor-time auth form built from an in-memory kubeconfig dict.
**When to use:** Both auth forms — `kubeconfig`+`context` (parse the YAML content to a dict) and `api_url`+`sa_token` (synthesize a minimal dict).
**Verified behavior (live, kr8s 0.20.15):**

```python
# Source: verified via `uv run python` against installed kr8s 0.20.15 + Context7 /kr8s-org/kr8s
# KubeAuth loads server + token + namespace from an in-memory dict with NO network call:
#   AUTH-ONLY server = https://k8s.example:6443
#   AUTH-ONLY token  = SECRET-SA-TOKEN
#   AUTH-ONLY ns     = phaze-ns

def _kubeconfig_dict_from(kube: KubeConfig) -> dict[str, Any]:
    """Build an in-memory kubeconfig dict from the per-backend KubeConfig (D-04)."""
    if kube.kubeconfig is not None:
        # kubeconfig field holds raw YAML *content* (SecretStr), not a path (config_backends._read_secret_file
        # reads *_file verbatim). Parse to a dict so no secret touches disk.
        return yaml.safe_load(kube.kubeconfig.get_secret_value())
    # api_url + sa_token form → synthesize the minimal kubeconfig (matches current default-verify behavior).
    token = kube.sa_token.get_secret_value() if kube.sa_token else None
    return {
        "apiVersion": "v1", "kind": "Config",
        "clusters": [{"name": "phaze", "cluster": {"server": kube.api_url}}],
        "users": [{"name": "phaze", "user": ({"token": token} if token else {})}],
        "contexts": [{"name": "phaze", "context": {"cluster": "phaze", "user": "phaze", "namespace": kube.namespace}}],
        "current-context": "phaze",
    }

async def _api(kube: KubeConfig) -> Any:
    """Build the async kr8s client for THIS backend — constructor-time auth, no token hack (D-04)."""
    kc = _kubeconfig_dict_from(kube)
    # kr8s.asyncio.api() args: (url, kubeconfig, serviceaccount, namespace, context) — VERIFIED signature.
    # Passing a dict kubeconfig: hash_kwargs json.dumps's it, so distinct dicts → distinct cached clients
    # per (thread, loop); identical dict → cheap cached reuse. No shared-global mutation.
    context = kube.context if getattr(kube, "context", None) else None
    return await kr8s.asyncio.api(kubeconfig=kc, namespace=kube.namespace, context=context)
```

**Verified caching facts:**
- `kr8s.asyncio.api()` signature = `(url, kubeconfig, serviceaccount, namespace, context)` — **no `token=` parameter exists**, which is exactly why the current code resorts to the hack. `[VERIFIED: inspect.signature]`
- `hash_kwargs` does `json.dumps` on dict values before building the frozenset cache key, so a **dict kubeconfig is a valid, stable cache key**; distinct dicts (distinct clusters) → distinct cached `Api` instances. `[VERIFIED: source read of kr8s._api.hash_kwargs]`
- `KubeAuth`/`KubeConfigSet` accept a dict directly (`Union[PathType, dict]`) and `_load_kubeconfig` sets `self.token = self._user["token"]`, `self.server = self._cluster["server"]`, and namespace from the context — **no network, no `_create_session`**. `[VERIFIED: KubeAuth source + live test]`

### Pattern 2: Deterministic per-file bucket selection (D-06, MKUE-02)
```python
# Source: verified sound against Python stdlib hashlib; matches D-06 wording.
import hashlib
import uuid

def pick_bucket(file_id: uuid.UUID, bucket_ids: list[str]) -> str:
    """Deterministically map a file to one of the backend's bound bucket ids (D-06).

    Stable across process restarts (sha256 of the UUID bytes, NOT Python's salted hash()).
    sorted() gives a stable order independent of TOML/registry ordering. The chosen id is then
    recorded on cloud_job.staging_bucket as the AUTHORITATIVE record — presign/cleanup read that,
    they never re-derive (avoids drift if the backend's bucket set changes in config).
    """
    ordered = sorted(bucket_ids)
    if not ordered:
        raise S3StagingError("kueue backend resolves to an empty bucket set")  # config validator already guards this
    digest = hashlib.sha256(file_id.bytes).digest()
    index = int.from_bytes(digest, "big") % len(ordered)
    return ordered[index]
```
Distribution is uniform for the small `n` (bucket-set sizes) this system uses; sha256-mod bias is negligible.

### Pattern 3: Clean-before-flip spillover delete (D-01/D-03/MKUE-04) — the crux
**What:** In `reconcile_cloud_jobs._handle_no_callback_terminal`, the **at-cap spill-back branch** must delete the old object *before* the commit that flips the file to `AWAITING_CLOUD` (and thus releases the per-row `pg_advisory_xact_lock`).
**Current (single-cluster, unsafe for multi-bucket) ordering:**
```python
# reconcile_cloud_jobs.py:178-185 — commit FIRST, then delete. Releases the lock before the delete.
cloud_job.status = CloudJobStatus.FAILED.value
await session.execute(update(FileRecord)...values(state=FileState.AWAITING_CLOUD))
await session.commit()                       # ← lock released, file now a drain candidate
await s3_staging.delete_staged_object(file_id)   # ← old object deleted AFTER (Pitfall 9 window)
await kube_staging.delete_job(name)
```
**Phase 70 ordering (delete under the lock, before the flip):**
```python
# Source: derived from Pitfall 2 (drain↔reconcile lock scope) + Pitfall 9 (cross-bucket collision).
# The per-row pg_advisory_xact_lock(5_000_504) is already held (acquired at the top of each
# KueueBackend.reconcile per-row unit, backends.py:385). Capture the OLD identity, delete UNDER the
# lock, THEN flip + commit.
old_backend_id = cloud_job.backend_id
old_bucket_id = cloud_job.staging_bucket
bucket = _resolve_bucket_config(cfg, old_bucket_id)  # settings.buckets lookup by id
with contextlib.suppress(Exception):                 # D-03 best-effort/idempotent — never blocks re-dispatch
    if bucket is not None:
        await s3_staging.delete_object(file_id, bucket)   # old (backend,bucket) object, file_id-scoped key
cloud_job.status = CloudJobStatus.FAILED.value
cloud_job.staging_bucket = None                      # optional: clear so the record can't mislead pre-repurpose
await session.execute(update(FileRecord)...values(state=FileState.AWAITING_CLOUD))
await session.commit()                               # ← releases lock; old object is ALREADY gone
await kube_staging.delete_job(name, kube=<this backend's KubeConfig>)  # Job delete stays post-commit (D-04)
```
**Why under the lock, not just capture-and-delete-after:** capturing the old bucket id into a local is necessary but **not sufficient**. The re-dispatch reuses the same `file_id`-scoped S3 key. If D-06 maps the re-dispatch to the *same* bucket (same backend re-selected, or a shared bucket present in the new backend's set that hashes identically), the new object's key equals the old object's key. A delete that runs after the lock is released races the new stage and can destroy the new object. Deleting **before** the file becomes an `AWAITING_CLOUD` drain candidate (i.e., before the commit that releases the lock) guarantees the old object is gone before any re-stage can occur. The drain holds the *same* `pg_advisory_xact_lock(5_000_504)` across its whole candidate-claim, so it physically cannot pick up the file until reconcile's txn commits.

**Lock-hold note:** this places one best-effort S3 `delete_object` inside the per-row lock hold. Acceptable: reconcile is a `*/5` background cron (not the latency-sensitive cap-timing path), the delete is idempotent and error-swallowed (so a slow/failed S3 call cannot pin the lock beyond one network timeout), and the TTL is the backstop. The "keep S3 latency out of the advisory-locked tick" guidance in D-03 refers to the **drain's hot dispatch path**, which is unaffected — cleanup remains in reconcile, not dispatch.

### Anti-Patterns to Avoid
- **Re-deriving the bucket at cleanup/presign time instead of reading `staging_bucket`:** breaks the moment the backend's bucket set changes in config, or after the in-place `backend_id` mutation loses the old owner. Always read the recorded column. (D-01/D-06.)
- **Calling `kr8s.asyncio.api()` with no args anywhere in multi-cluster code:** the factory's "all args None" branch returns an *arbitrary* already-cached client (`return await list(_cls._instances[thread_loop_id].values())[0]`). In N-cluster mode this silently targets the wrong cluster. Always pass explicit `kubeconfig=`/`context=` and pass `api=` to every kr8s object (the code already does the latter). `[VERIFIED: kr8s._api factory source]`
- **Making every backend's `is_available` check "is an agent online":** reintroduces Landmine L2 (Kueue has no persistent agent). The asymmetry is already correct in `KueueBackend.is_available` (probes the LocalQueue, no compute-agent dependency) — do not regress it.
- **A whole-tick lock in reconcile:** breaks the per-row commit granularity the delete-after-record ordering depends on (Pitfall 2). Keep the lock per-row.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Per-cluster kube auth | A custom httpx session with a manually-baked `Authorization: Bearer` header (the current `_create_session()` hack) | `kr8s.asyncio.api(kubeconfig=<dict>)` — constructor-time auth | kr8s already loads token/CA/server from a kubeconfig dict; the hack duplicates and destabilizes this. |
| Distinct client per cluster | A hand-rolled client registry/dict keyed by backend id | kr8s's arg-keyed factory cache (distinct kubeconfig dict → distinct client) | Verified: `hash_kwargs` json-serializes dict args; caching is correct and per-(thread,loop). |
| Idempotent S3 delete | Existence-check-then-delete | `s3_staging.delete_object` swallowing `{NoSuchKey, NoSuchUpload, 404}` (existing idiom) | "A missing object is the desired end state" — already the project's idempotent-delete primitive; extend it per-bucket. |
| Mutual exclusion of drain vs reconcile | A new lock/table/flag | The existing `pg_advisory_xact_lock(5_000_504)` (drain holds tick-wide; reconcile per-row) | Already wired in Phase 69; Phase 70 only reorders the delete relative to the commit that releases it. |
| Deterministic file→bucket map | Round-robin / least-loaded (needs shared mutable state in the locked tick) | `sha256(file_id.bytes) % len(sorted(ids))` (pure, reproducible) | Cleanup/presign must independently re-agree with staging; only a pure function does that. |

**Key insight:** every primitive Phase 70 needs already exists in the codebase — per-config client/`_client(bucket)`, idempotent delete, the advisory lock, the per-backend registry, per-row reconcile guards. The phase is almost entirely *lifting a global read up to a per-backend parameter* plus *reordering one delete*.

## Runtime State Inventory

This is a refactor/generalization phase. A grep finds files; it does not find runtime state. Explicit answers per category:

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| **Stored data** | `cloud_job` rows: gain a new nullable `staging_bucket` column. **No meaningful live rows exist** — the a1/k8s cloud paths were never deployed live (per Phase 68 D-06 and 029's migration comment: "~zero rows to migrate"). No backfill needed; new rows stamp it going forward. | Additive migration 030 (nullable). No data migration. |
| **Live service config** | The N Kueue clusters' LocalQueues/kubeconfigs live in `backends.toml` (control-plane file, in git per operator). S3 bucket registry (`[[buckets]]`) likewise. No UI/DB-resident config to migrate — all declarative TOML. The **second cluster's kubeconfig/context and its bucket set are operator-supplied at rollout**, not in this repo. | None in-repo. Deployment-gated: operator declares cluster 2 + its bucket set at rollout. |
| **OS-registered state** | None. No Task Scheduler / launchd / pm2 / cron-in-DB entries reference cluster identity. The `*/5` crons are code-registered on the controller (`stage_cloud_window`, `reconcile_cloud_jobs`) and iterate `resolve_backends(cfg)` dynamically — adding a backend needs no re-registration. | None — verified by grep of `controller.py` cron registration (single narrow crons, backend-count-agnostic). |
| **Secrets/env vars** | `KubeConfig.kubeconfig` / `sa_token` and `BucketConfig.access_key_id` / `secret_access_key` are `SecretStr`, resolved from inline `*_file` paths at construction (config_backends `_resolve_inline_secret_files`). Adding a 2nd cluster/bucket adds new `*_file` entries in `backends.toml`; **no key rename** — code reads them per-entry. | None (code rename only). Operator adds new `*_file` secret mounts at rollout. |
| **Build artifacts / installed packages** | None. No compiled binaries or egg-info carry cluster identity. No `pyproject.toml` package rename. (If PyYAML is declared explicit — see Package Legitimacy — that is an additive dependency edit, not an artifact rename.) | None. |

**The canonical question — after every file is updated, what runtime systems still have the old single-cluster assumption cached?** Answer: only the two module-global accessors (`active_kube`, `active_bucket`) and, indirectly, `active_compute_scratch_dir` (see Pitfall 3 below). All three route through `ControlSettings._single_non_local()`, which **raises on >1 non-local backend** — so they are not stale-but-silent; they fail loudly the moment a 2nd non-local backend is configured. That is the safety net that makes this refactor detectable, but it also means `active_compute_scratch_dir` is a **required companion fix** (below), not optional.

## Common Pitfalls

### Pitfall 1: `active_compute_scratch_dir` breaks the /pushed callback under N non-local backends (companion-scope, load-bearing)
**What goes wrong:** `agent_push.py:133` builds the compute scratch path from `settings.active_compute_scratch_dir`, which calls `_single_non_local()`. With the milestone's target deploy (local + N Kueue + 1 compute), there are **≥2 non-local backends**, so `_single_non_local()` **raises `ValueError`** and the `/pushed` callback 500s. Adding a 2nd Kueue backend to a registry that also has a compute backend crashes the compute path.
**Why it happens:** D-05 defers the compute *`agent_ref`* fix, but the *scratch_dir reduction* is a different concern that also routes through `_single_non_local`. The moment N>1 non-local backends coexist, the single-non-local reduction is invalid for all three accessors, not just the two Kueue ones.
**How to avoid:** Re-base `active_compute_scratch_dir` on a **single-compute** reduction (`[b for b in backends if b.kind == "compute"]`, still ≤1 until PROV-01) instead of single-non-local. This is a scratch_dir resolution change, NOT the deferred agent_ref fix — it stays within Phase 70's Kueue-focus while not regressing the compute path. Flag to the planner as a required companion change. **Confidence: MEDIUM** (verified the call chain raises; the fix is straightforward but the planner should confirm no other single-non-local consumer exists — see `resolved_non_local_kind`/WR-01 which the drain no longer consults).
**Warning signs:** `/pushed` returns 500 with "multi-backend dispatch lands in Phase 69" ValueError text once a 2nd non-local backend is declared.

### Pitfall 2 (research Pitfall 9): Cross-bucket collision on spillover
**What goes wrong:** deterministic `file_id`-scoped S3 keys assume one owner at a time. Spillover to another cluster/bucket, or a same-bucket re-dispatch, can make cluster A's cleanup delete an object cluster B's pod still needs.
**How to avoid:** the clean-before-flip ordering (Pattern 3) + reading the recorded `staging_bucket` for every delete/presign. The unique `cloud_job(file_id)` FK guarantees one owner; the advisory lock serializes drain vs reconcile.
**Warning signs:** cluster-B pods 404 on S3 GET right after a cluster-A cleanup; two `phaze-analyze-<same file_id>` Jobs alive in two clusters.

### Pitfall 3 (research Pitfall 8): One flaky cluster poisons the tick
**What goes wrong:** `stage_cloud_window`'s snapshot loop (lines 134-140) and dispatch loop have **no per-backend guard** today (only `NoActiveAgentError` is caught around dispatch). A `dispatch()` raising a kube/S3 error, or `in_flight_count` raising a DB error, aborts the whole tick — starving healthy clusters.
**How to avoid (D-07):** wrap each backend's `is_available()` + `in_flight_count()` snapshot in try/except (treat a raise as unavailable/0 slots + log), and wrap each `dispatch()` call in try/except (treat a raise as a clean hold of that candidate — file stays `AWAITING_CLOUD`). `KueueBackend.is_available` already catches broadly and returns bool; D-07 adds the loop-level defense-in-depth so a probe/count/dispatch raise never escapes the tick.
**Warning signs:** all backends stop dispatching whenever one cluster goes down; a drain tick logs an exception with `staged: 0` while healthy backends have capacity.

### Pitfall 4: Presign-GET / inline-delete re-derive the bucket instead of reading the record
**What goes wrong:** `agent_files.py:178` (`presign_get`) and `_delete_staged_object_if_cloud` currently read `active_bucket`. If Phase 70 makes them *re-derive* via D-06 instead of reading `cloud_job.staging_bucket`, a config change to the backend's bucket set (or the in-place `backend_id` repurpose) points them at the wrong bucket → dead presigned URLs / missed deletes.
**How to avoid:** both call sites already query `cloud_job` for the file (status readiness guard / has-cloud-job guard) and have `session` in scope — extend those queries to also read `staging_bucket`, resolve the `BucketConfig` from `settings.buckets`, and pass it to the (parameterized) `presign_get(file_id, bucket)` / `delete_object(file_id, bucket)`. Since `s3_staging` must stay ORM-free (import-boundary test), the **router/caller** resolves the bucket, never `s3_staging`.
**Warning signs:** pod GET 404s despite `cloud_job.status == UPLOADED`; the import-boundary test fails if an ORM import creeps into `s3_staging`.

### Pitfall 5: Live-cluster auth verification cannot be unit-tested
**What goes wrong:** the synthesized-kubeconfig auth form is verified against kr8s's auth loader in-process, but the actual TLS handshake / CA trust against a real second cluster's API server (self-signed CA over Tailscale/WireGuard) is only observable live. The current form uses default TLS verification (no CA injected) — same as the retired hack.
**How to avoid:** flag as a **deployment-gated E2E item** (joins the Phase-56 carryover live-cluster verification + the v6.0 deployment-gated UAT items in STATE.md). If the second cluster uses a private CA, the synthesized dict can carry `certificate-authority-data` (base64) on the cluster entry — `_load_kubeconfig` handles it — but that is a rollout-time config decision. **Confidence for live TLS: LOW (deployment-gated); Confidence for the auth-mechanism-loads-correctly: HIGH (verified in-process).**

## Code Examples

### Threading KubeConfig through a re-homed kube_staging function (MKUE-01)
```python
# Source: derived from the existing kube_staging.py signatures — the private helpers ALREADY take
# `kube: KubeConfig`; only the public verbs resolve it via the global _kube_config(). Lift it up.
async def submit_job(file_id: uuid.UUID, kube: KubeConfig) -> tuple[str, str]:
    api = await _api(kube)                       # per-backend client (Pattern 1)
    job = Job(build_job_manifest(file_id, kube), api=api)   # build_job_manifest already takes kube
    ...

async def get_local_queue(kube: KubeConfig) -> Any:   # per-cluster reachability probe (MKUE-03)
    api = await _api(kube)
    local_queue_cls = new_class(kind="LocalQueue", version=kube.workload_api_version, namespaced=True)
    ...
# KueueBackend.is_available then calls: await kube_staging.get_local_queue(self.config.kube)
```

### Resolving a BucketConfig by recorded id (caller-side, for presign/delete)
```python
# Source: mirrors ControlSettings._validate_registry's bucket_by_id lookup (config.py:432).
def _resolve_bucket_config(cfg: ControlSettings, bucket_id: str | None) -> BucketConfig | None:
    if bucket_id is None:
        return None
    return {b.id: b for b in cfg.buckets}.get(bucket_id)
```

### Stamping staging_bucket at stage time (D-01/D-06)
```python
# In KueueBackend.dispatch (backends.py), after _stage_file_to_s3 upserts the cloud_job row:
bucket_id = pick_bucket(file.id, self.config.buckets)          # D-06 (self.config.buckets is the id-list)
await _stage_file_to_s3(session, file, task_router, bucket=_resolve_bucket_config(cfg, bucket_id))
await session.execute(
    update(CloudJob).where(CloudJob.file_id == file.id)
    .values(backend_id=self.id, staging_bucket=bucket_id)      # record BOTH in the same uncommitted txn
)
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `api.auth.token = token; await api._create_session()` (post-construction session rebuild) | `kr8s.asyncio.api(kubeconfig=<in-memory dict>)` constructor-time auth | This phase (D-04) | Removes reliance on kr8s private `_create_session`; distinct dicts → distinct cached clients cleanly. |
| `cfg.active_kube` / `cfg.active_bucket` global single-cluster reads | Per-backend `KubeConfig` / D-06-selected `BucketConfig` threaded as params | This phase | Enables N concurrent Kueue clusters + per-file bucket choice. |
| S3 delete AFTER commit (single-owner assumption) | S3 delete BEFORE flip commit, under per-row lock (clean-before-flip) | This phase (MKUE-04) | Closes the cross-bucket collision window (Pitfall 9). |

**Deprecated/outdated:** the `_single_non_local()`-based transitional accessors (`active_kube`, `active_bucket`, and — required companion — `active_compute_scratch_dir`) are retired/replaced this phase; they were explicitly marked "retained through Phase 70 / MKUE-01" in config.py.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `KubeConfig` has (or the planner adds) a `context` field to select a kubeconfig context. The submodel today has `kubeconfig` + `sa_token` + `api_url` but **no explicit `context` field** in config_backends.py. | Pattern 1 (`kube.context`) | If absent, the `kubeconfig`+`context` path can't select a non-default context. Planner should add `context: str | None = None` to `KubeConfig` (REG-05 says "per-cluster kubeconfig/context"). `[ASSUMED]` — verify against config_backends.py before planning; it is a small additive field. |
| A2 | `active_compute_scratch_dir` must be re-based on a single-compute reduction to avoid breaking `/pushed` under N non-local backends. | Pitfall 1 | If the target deploy never runs compute + multi-Kueue simultaneously, this is unnecessary; but the milestone premise says it can. Confirm with operator/roadmap intent. `[ASSUMED: milestone premise]` |
| A3 | Declaring PyYAML explicitly (vs relying on the kr8s transitive edge) is the preferred hygiene. | Package Legitimacy | Low risk either way; the `NamedTemporaryFile` alternative avoids the import entirely. `[ASSUMED]` |
| A4 | Default TLS verification (no CA injected) against the second cluster works over the mesh, matching the retired hack's behavior. | Pitfall 5 | If cluster 2 uses a private CA, presign/submit fails at rollout until `certificate-authority-data` is added. Deployment-gated. `[ASSUMED]` |

## Open Questions (RESOLVED)

Both questions were answered during research (see each "Recommendation:") and are carried by concrete plan tasks. Markers added at plan time.

1. **Does `KubeConfig` carry a `context` field today?**
   - What we know: config_backends.py `KubeConfig` has `api_url`, `namespace`, `local_queue`, `kubeconfig` (SecretStr content), `sa_token`, and object-name fields.
   - What's unclear: whether a `context` field exists to pick a context out of a multi-context kubeconfig (REG-05 wording says "per-cluster kubeconfig/context").
   - Recommendation: planner adds `context: str | None = None` to `KubeConfig` (additive, defaults to current-context when None) as part of MKUE-01. Cheap and matches the requirement text.
   - **RESOLVED:** planner adopted the recommendation — `context: str | None = None` is added to `KubeConfig` in **Plan 01, Task 3** (config_backends field add) and consumed by `kube_staging._api` (`context=kube.context`) in **Plan 03, Task 1**.

2. **Which of the ~6 `active_bucket`/`active_kube` consumer call sites become caller-resolves-bucket vs backend-method?**
   - What we know: `presign_get` (agent_files), `_delete_staged_object_if_cloud` (agent_analysis ×3), `agent_s3` upload-failure delete, `submit_cloud_job` (s3_key only, bucket-agnostic), reconcile at-cap delete.
   - What's unclear: the cleanest seam for router call sites that are not `KueueBackend` methods.
   - Recommendation: router/caller resolves `cloud_job.staging_bucket` → `BucketConfig` and passes it; `s3_staging` stays ORM-free (import-boundary test enforces this).
   - **RESOLVED:** router/caller-resolves-bucket seam adopted — the router call sites (`agent_files.presign_get`, `agent_analysis._delete_staged_object_if_cloud`, `agent_s3` upload-failure delete) read `cloud_job.staging_bucket` → `BucketConfig` and pass it to the parameterized `s3_staging` verbs in **Plan 02, Task 3**, keeping `s3_staging` ORM-free (import-boundary test).

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| kr8s | per-cluster kube client | ✓ | 0.20.15 | — |
| PyYAML | parse inline kubeconfig content → dict | ✓ (transitive) | 6.0.3 | `NamedTemporaryFile` path instead of dict |
| aioboto3 / botocore | per-bucket S3 | ✓ | pinned | — |
| PostgreSQL (advisory locks) | drain↔reconcile mutual exclusion | ✓ | 16+ | — |
| A second live Kueue cluster | MKUE-01/03/04 live E2E | ✗ | — | Deployment-gated E2E at rollout (Pitfall 5); unit/seam tests use respx + fake buckets |

**Missing dependencies with no fallback:** a real second cluster — unavoidably a rollout-time verification, not a build-time blocker. All logic is unit/seam-testable in-process.
**Missing dependencies with fallback:** none blocking.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (`asyncio_mode = "auto"`), respx for kr8s httpx seam, moto/fake S3 for aioboto3 (existing) |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]`, `testpaths = ["tests"]` |
| Quick run command | `uv run pytest tests/analyze/services/test_backends.py tests/analyze/services/test_kube_staging.py tests/analyze/services/test_s3_staging.py -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` (85% min per CLAUDE.md) |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| MKUE-01 | Distinct kr8s client per backend from its KubeConfig; token-hack retired; N≥2 clusters get distinct clients | unit + seam | `uv run pytest tests/analyze/services/test_kube_staging.py -k "auth or client or multi" -x` | ✅ (extend) |
| MKUE-02 | `pick_bucket` deterministic + stable across "restart"; staging_bucket recorded; presign reads recorded bucket | unit | `uv run pytest tests/analyze/services/test_s3_staging.py -k "pick_bucket or staging_bucket" -x` | ❌ Wave 0 (new cases) |
| MKUE-03 | Per-backend try/except: one backend raising on snapshot/dispatch doesn't abort the tick; others get work | unit | `uv run pytest tests/analyze/tasks/ -k "stage_cloud_window and isolation" -x` | ❌ Wave 0 (needs N≥2 fixture) |
| MKUE-04 | Clean-before-flip: at-cap spill deletes old (backend,bucket) BEFORE flip commit; same-bucket re-dispatch preserves new object; concurrency test | unit + concurrency | `uv run pytest tests/analyze/tasks/test_reconcile_cloud_jobs.py -k "clean_before_flip or spillover" -x` | ❌ Wave 0 |
| MKUE-04 | migration 030 upgrade/downgrade round-trips; staging_bucket nullable, no backfill | integration | `uv run pytest tests/integration/test_migrations/ -k "030 or staging_bucket" -x` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** the Quick run command above (affected service tests, < 30s).
- **Per wave merge:** `uv run pytest tests/analyze tests/agents tests/discovery -q` (touches backends, kube/s3 staging, reconcile, agent routers).
- **Phase gate:** full suite green + 85% coverage before `/gsd:verify-work`.

### Wave 0 Gaps
- [ ] `tests/analyze/services/test_s3_staging.py` — add `pick_bucket` determinism + stability + empty-set-raises cases; add per-bucket `BucketConfig`-param cases for presign/delete (covers MKUE-02).
- [ ] `tests/analyze/services/test_kube_staging.py` — add synthesized-kubeconfig-dict auth cases (both forms) + distinct-client-per-backend; assert no `_create_session` usage (covers MKUE-01).
- [ ] `tests/analyze/tasks/test_release_awaiting_cloud*.py` (or the stage_cloud_window test home) — add an N≥2 backend fixture where one backend raises on `is_available`/`in_flight_count`/`dispatch`; assert the tick survives and healthy backends get work (covers MKUE-03).
- [ ] `tests/analyze/tasks/test_reconcile_cloud_jobs.py` — add clean-before-flip ordering test (old object deleted before the AWAITING_CLOUD commit) + a same-bucket re-dispatch preservation test + a drain↔reconcile concurrency test asserting no file ends in two backends and no object the new pod needs is deleted (covers MKUE-04 / Pitfall 9).
- [ ] `tests/integration/test_migrations/test_030_staging_bucket.py` — upgrade/downgrade round-trip (mirror `test_migration_027_cloud_phase.py` / the 029 pattern).
- [ ] Import-boundary guard: keep `s3_staging` ORM-free and `kube_staging` ORM-free after parameterization (extend existing purity tests).

## Security Domain

`security_enforcement` is absent in `.planning/config.json` → treated as enabled.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | yes | Per-cluster SA bearer token / kubeconfig creds as `SecretStr`, control-plane-only (DIST-01). Pods credential-free (presigned URLs only). |
| V4 Access Control | yes | Presigned, `file_id`-scoped, TTL-bounded S3 URLs — objects never world-readable despite an Internet-reachable endpoint (MKUE-02 explicit). |
| V5 Input Validation | yes | `BucketConfig.endpoint_url` SSRF guard (http(s)+host, Phase 67); `file_id` is a server UUID (no free-text in keys/Job names). |
| V6 Cryptography | partial | Rely on kr8s/httpx TLS; do NOT set `insecure-skip-tls-verify` — prefer `certificate-authority-data` if a private CA is needed (Pitfall 5). Never hand-roll TLS. |
| V7 Logging | yes | Secret hygiene: log only `{id, kind, rank, cap}`; never log the kubeconfig dict, `SecretStr`, SA token, or bucket creds (existing `log_effective_registry` discipline; T-68-04). |

### Known Threat Patterns for {control-plane multi-cluster dispatch + S3 staging}
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Kubeconfig/SA token leaked in logs (N clusters × more secrets) | Information Disclosure | `SecretStr` everywhere; the synthesized kubeconfig dict is in-memory only and never logged; extend the id/kind/rank/cap-only log projection. |
| Cross-bucket object destruction on spillover | Tampering / DoS | Clean-before-flip under the advisory lock; read recorded `staging_bucket`; unique `cloud_job(file_id)` FK ensures one owner (Pitfall 9). |
| SSRF via a malicious `endpoint_url` on a new bucket | Tampering | Per-bucket http(s)+host validator (Phase 67, reused unchanged). |
| Wrong-cluster dispatch via no-arg `kr8s.api()` cache fallback | Spoofing / EoP | Never call no-arg `api()`; always pass explicit `kubeconfig=`/`api=` (verified factory fallback returns an arbitrary cached client). |
| World-readable staged object on an Internet-reachable ("public") bucket | Information Disclosure | Objects are private; access only via short-TTL presigned URLs minted control-side (MKUE-02 explicit requirement). |

## Sources

### Primary (HIGH confidence)
- Live introspection of installed **kr8s 0.20.15** via `uv run python` — `kr8s.asyncio.api` / `Api.__init__` / `KubeAuth.__init__` signatures; `KubeAuth.reauthenticate`/`_load_kubeconfig` source; `hash_kwargs`; live test that a dict kubeconfig loads server+token+namespace with no network and yields distinct cached clients.
- Context7 `/kr8s-org/kr8s` — `kr8s.api()` parameters (`url`, `kubeconfig`, `serviceaccount`, `namespace`, `context`), client caching semantics ("same args → same object", "different args → new instance"), `bypass_factory`.
- Repo source (authoritative): `services/backends.py`, `services/kube_staging.py`, `services/s3_staging.py`, `services/cloud_staging.py`, `models/cloud_job.py`, `tasks/release_awaiting_cloud.py`, `tasks/reconcile_cloud_jobs.py`, `config.py`, `config_backends.py`, `alembic/versions/029_*.py`, `routers/agent_files.py`, `routers/agent_analysis.py`, `routers/agent_push.py`.
- `.planning/phases/70-multi-kueue-n-clusters/70-CONTEXT.md` (D-01..D-07, authoritative decisions).
- `.planning/REQUIREMENTS.md` §MKUE-01..04, §REG-05, §PROV-01.

### Secondary (MEDIUM confidence)
- `.planning/research/PITFALLS.md` Pitfalls 2, 8, 9, 10 (cross-verified against the actual code paths cited).
- `docs/superpowers/specs/2026-06-29-multi-cloud-backends-design.md` §4.2/4.4/4.5 (with the operator-directed §6/§7 supersession applied — REG-05 governs bucket behavior, not the design's one-shared-bucket framing).

### Tertiary (LOW confidence — flagged for validation)
- Live second-cluster TLS/CA behavior over the mesh (Pitfall 5 / A4) — deployment-gated E2E, not verifiable in this session.

## Metadata

**Confidence breakdown:**
- kr8s per-backend client / token-hack retirement: **HIGH** — mechanism verified live against installed 0.20.15 (auth loads from dict, no network, distinct cached clients); only live-cluster TLS is deferred.
- Clean-before-flip advisory-lock scope (the crux): **HIGH** — derived directly from the existing lock wiring (`pg_advisory_xact_lock(5_000_504)` held tick-wide in drain, per-row in reconcile) + Pitfall 9's same-key collision; the reorder is small and well-bounded.
- D-06 bucket selection: **HIGH** — pure stdlib, verified sound.
- Component re-home (parameterize kube_staging/s3_staging): **HIGH** — private helpers already take the config; only the public verbs need the param lifted up.
- `active_compute_scratch_dir` companion fix (Pitfall 1): **MEDIUM** — call chain verified to raise; the fix is straightforward but planner should confirm scope intent.
- `KubeConfig.context` field existence (A1): **MEDIUM** — needs a quick confirm/additive field.

**Research date:** 2026-07-04
**Valid until:** ~2026-08-04 for the ecosystem facts; the repo-code facts are valid until the affected files change (verify against HEAD at plan time).
</content>
</invoke>
