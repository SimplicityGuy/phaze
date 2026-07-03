# Architecture Research — Multi-Cloud Backends Integration Map

**Domain:** Pluggable analysis-backend registry over phaze's v6.0 cloud-burst stack (Python 3.13, FastAPI, SQLAlchemy async, SAQ/Postgres queue, kr8s, aioboto3)
**Researched:** 2026-07-03
**Confidence:** HIGH (mapped against real files/symbols on `SimplicityGuy/Multi-Cloud-Backends`; design spine locked in `docs/superpowers/specs/2026-06-29-multi-cloud-backends-design.md`)

> This is an **integration map**, not a greenfield design. Every seam below cites the real
> function/module it collapses behind the `Backend` protocol. The existing v4.0–v6.0
> architecture is treated as fixed; only the `cloud_target` fork, the `cloud_job` sidecar, the
> drain scheduler, and the flat `kube_*` config surface change. `put_analysis`,
> `_route_discovered_by_duration`, the agent HTTP surface, the shared S3 leg, and windowed
> analysis stay untouched (design §5).

---

## Standard Architecture

### System Overview (target state)

```
┌──────────────────────────────────────────────────────────────────────────┐
│  CONTROL PLANE (app-server, PHAZE_ROLE=control — no media mount, DIST-01)  │
│                                                                            │
│  ControlSettings.backends:[]  ◄── single source of truth (replaces        │
│        │  (per-entry id/kind/rank/cap; cloud_target→backends shim)         │
│        ▼                                                                    │
│  ┌───────────────────────── build_backends(cfg) ───────────────────────┐   │
│  │  LocalBackend   ComputeAgentBackend(×N)   KueueBackend(×N)           │   │
│  │  rank/cap       rank/cap  agent_ref       rank/cap  kube{...}         │   │
│  │  is_available / in_flight_count / dispatch / reconcile  (Protocol)    │   │
│  └───────┬──────────────────┬──────────────────────────┬───────────────┘   │
│          │ dispatch          │ dispatch                  │ dispatch          │
│  process_file          push_file (rsync)          _stage_file_to_s3 +        │
│  (agent queue)         tasks/push.py              submit_cloud_job           │
│          │                    │                          │                   │
│  ┌───────┴────────────────────┴──────────────────────────┴──────────────┐   │
│  │ tiered drain scheduler (stage_cloud_window, ONE pg_advisory_xact_lock) │   │
│  │ enumerate → is_available() filter → per AWAITING_CLOUD file pick        │   │
│  │ lowest-rank backend with in_flight_count()<cap → dispatch             │   │
│  └───────────────────────────────────────────────────────────────────────┘  │
│          │ per-backend in-flight + reconcile                                  │
│  cloud_job(+backend_id)  ── reconcile_cloud_jobs cron (backend_id-aware) ──   │
│          │ shared, global                          per-cluster kube cfg        │
│  s3_staging (SOLE S3 SDK importer — one bucket, DIST-01)                       │
│  kube_staging (parameterized per KueueBackend, was flat kube_* + get_settings)│
└──────────────────────────────────────────────────────────────────────────┘
     │ presigned PUT/GET (bytes)          │ suspended batch/v1 Job (per cluster)
     ▼                                    ▼
 file-server agent (rsync/upload)    Kueue cluster(s) — ephemeral analyze pods
     └──────────────── PUT result by file_id ──────► /api/internal/agent/analysis/{file_id}
                       (put_analysis — SOLE result writer, backend-agnostic, UNCHANGED)
```

### Component Responsibilities (new vs modified vs untouched)

| Component | Status | Responsibility after milestone |
|-----------|--------|-------------------------------|
| `ControlSettings.backends` list + per-entry validator + `cloud_target`→`backends` shim (`config.py`) | **NEW** field, **MODIFIED** file | Single source of truth for what execution targets exist; replaces the 3-value `cloud_target` Literal (`config.py:406`) and consolidates the 3 per-target validators (`config.py:615/636/657`) |
| `Backend` Protocol + `LocalBackend`/`ComputeAgentBackend`/`KueueBackend` (`services/backends.py`, new) | **NEW** | The seam that removes the `if/elif cloud_target`; each `is_available`/`in_flight_count`/`dispatch`/`reconcile` wraps an existing body |
| `stage_cloud_window` (`tasks/release_awaiting_cloud.py:110`) | **MODIFIED** | Becomes the tiered drain: enumerate→filter→pick-lowest-rank-under-cap→`dispatch`; the hardcoded `cloud_target` fork (lines 126/142/177) is deleted |
| `CloudJob` sidecar (`models/cloud_job.py`) | **MODIFIED** (additive) | Gains `backend_id` column so in-flight counts + reconcile are per-backend; generalized to record compute pushes too, not just k8s |
| `reconcile_cloud_jobs` cron (`tasks/reconcile_cloud_jobs.py`) | **MODIFIED** | Iterates `cloud_job` grouped by `backend_id`; per-kueue-row uses THAT backend's kube config |
| `kube_staging` service (`services/kube_staging.py`) | **MODIFIED** | Every function parameterized by a per-`KueueBackend` kube config instead of `get_settings()`/`_kube_config()`; still control-plane-only creds |
| `s3_staging` service (`services/s3_staging.py`) | **UNTOUCHED** | Stays the SOLE S3 SDK importer against ONE shared bucket (DIST-01); read by all kueue backends |
| `cloud_staging.stage_file_to_s3` / `_stage_file_to_s3` (`services/cloud_staging.py`) | **LIGHTLY MODIFIED** | Becomes `KueueBackend.dispatch`'s upload body; gains `backend_id` on the `cloud_job` upsert |
| `tasks/push.py` + `_enqueue_push_file` (`release_awaiting_cloud.py:82`) | **LIGHTLY MODIFIED** | Becomes `ComputeAgentBackend.dispatch`'s body; writes a `cloud_job(backend_id=…)` in-flight row |
| `recover_orphaned_work` recovery ledger (`tasks/reenqueue.py:282`) | **MODIFIED** | Held-file re-dispatch becomes `backend_id`-aware / delegates spillover back to the scheduler |
| `put_analysis` (`routers/agent_analysis.py:126`) | **UNTOUCHED** | Sole result writer, keyed by `file_id`, already backend-agnostic (design §5) |
| `_route_discovered_by_duration` duration gate (`routers/pipeline.py:257`) | **UNTOUCHED** | Still decides long→`AWAITING_CLOUD` / short→local `process_file` (design §5) |

---

## 1. The `Backend` Protocol Seam — collapsing the `if/elif cloud_target`

### Every live `cloud_target` call site (grep-verified)

| File:line | Current switch | Collapses to |
|-----------|----------------|--------------|
| `config.py:406` | `cloud_target: Literal["local","a1","k8s"]` | `backends: list[BackendConfig]` + shim |
| `config.py:615 _enforce_s3_config_when_k8s` | k8s ⇒ require `s3_*` | one per-entry validator: `kind=="kueue"` entries share the global bucket check |
| `config.py:636 _enforce_compute_scratch_dir_when_a1` | a1 ⇒ require `compute_scratch_dir` | per-entry validator: `kind=="compute"` requires scratch + `agent_ref` |
| `config.py:657 _enforce_kube_config_when_k8s` | k8s ⇒ require `kube_api_url/namespace/local_queue` | per-entry validator: `kind=="kueue"` requires that entry's `kube{...}` block |
| `tasks/release_awaiting_cloud.py:126` | `if cloud_target=="local": no-op` | `if not enabled_backends: no-op` (enumerate step) |
| `tasks/release_awaiting_cloud.py:142` | `if cloud_target=="a1": GATE 1 compute online` | `ComputeAgentBackend.is_available()` |
| `tasks/release_awaiting_cloud.py:177` | `if cloud_target=="k8s": _stage_file_to_s3 else _enqueue_push_file` | `backend.dispatch(file)` |
| `routers/pipeline.py:395,699` | `settings.cloud_target != "local"` (cloud-eligible flag into duration router + backfill) | `any(b.enabled for b in backends)` helper (`cloud_enabled(cfg)`) |
| `routers/pipeline.py:572` | dashboard context `cloud_target` | N per-backend lane rows (Phase 5, generalizes v7.0 Phase 58 lane cards) |
| `routers/pipeline.py:763,805` | backfill `local`/`k8s` ledger-seed fork | backend-kind dispatch (kueue seeds `cloud_job`, compute seeds ledger) |
| `routers/agent_s3.py:112` | `if cloud_target=="k8s"` S3-callback gate | kueue backends always present ⇒ gate on the row's `backend_id` kind |
| `tasks/controller.py:167` | `if cloud_target=="k8s": LocalQueue probe` | `for b in kueue_backends: b.is_available()` probe loop |

### The `Backend` protocol → existing body mapping

```python
class Backend(Protocol):
    id: str; rank: int; cap: int
    async def is_available(self, session) -> bool
    async def in_flight_count(self, session) -> int
    async def dispatch(self, session, file, task_router) -> None
    async def reconcile(self, ctx, session) -> None
```

| Method | LocalBackend | ComputeAgentBackend | KueueBackend |
|--------|-------------|---------------------|--------------|
| `is_available` | always `True` | `select_active_agent(kind="compute")` scoped to `agent_ref` (heartbeat) | `kube_staging.get_local_queue(kube_cfg)` probe (reuse `controller.startup:174`) |
| `in_flight_count` | analyzing-locally count (rank 99 last-resort only) | `cloud_job WHERE backend_id AND status in-flight` | `cloud_job WHERE backend_id AND status IN (UPLOADING,UPLOADED,SUBMITTED,RUNNING)` |
| `dispatch` | enqueue `process_file` on fileserver queue | `_enqueue_push_file` (`release_awaiting_cloud.py:82`) → `push_file` (`tasks/push.py`) + write `cloud_job` row | `_stage_file_to_s3` (`services/cloud_staging.py:71`) → `s3_upload` → `submit_cloud_job` (`tasks/submit_cloud_job.py`) |
| `reconcile` | n/a (`put_analysis` closes it) | existing `/pushed` + push-mismatch callback path (`routers/agent_push.py`); FileState-driven | `reconcile_cloud_jobs` (`tasks/reconcile_cloud_jobs.py`) scoped to this backend's rows + kube config |

**Key insight from the map:** the bodies already exist and are already isolated (each is a
module-level async function). The protocol is a thin adapter layer — no logic rewrite, only
re-homing. This is why phases 1–2 are genuinely behavior-preserving.

---

## 2. `cloud_job.backend_id` Additive Migration

### Current state of the sidecar
`CloudJob` (`models/cloud_job.py`) is **k8s-only today** — rows are written ONLY by
`_stage_file_to_s3` (`cloud_staging.py:104`) and `submit_cloud_job` (`submit_cloud_job.py:79`).
Compute/a1 pushes write **no** `cloud_job` row; their in-flight window is counted purely from
`FileState IN {PUSHING, PUSHED}` via `get_cloud_window_count` (`pipeline.py:1243`). Both a1 and
k8s files flip to `PUSHING` in `stage_cloud_window` (line 176), so today's single global window
covers both targets uniformly.

### Migration sequencing (design §4.4 + §7 "migration sequencing")
1. **Additive column** — `backend_id: Mapped[str | None]` on `CloudJob`, nullable, no NOT NULL/CHECK yet (mirrors the two-step Alembic idiom in Key Decisions). New Alembic migration (next after 026/028).
2. **Backfill existing rows** — every current `cloud_job` row is a k8s row, so `UPDATE cloud_job SET backend_id = '<synthesized-kueue-id>'` where the synthesized id comes from the `cloud_target=k8s` shim (§5). Behind a settings-derived constant so a shimmed single-cluster deploy backfills to the one id it will use going forward.
3. **Generalize the registry to record compute pushes** — `ComputeAgentBackend.dispatch` now also upserts a `cloud_job(backend_id=…, status=UPLOADING→…)` row so spillover and recovery are uniform across kinds. This is the one place the sidecar's meaning widens (from "k8s staging record" to "any-backend in-flight record").
4. **Tighten** (optional, later) — once all writers stamp `backend_id`, a follow-up migration can make it NOT NULL.

### `in_flight_count` becomes per-backend
- Replace the single global `get_cloud_window_count` (`pipeline.py:1243`, `state IN {PUSHING,PUSHED}`) with `count(cloud_job WHERE backend_id=:id AND status IN (<in-flight set>))`.
- The advisory-locked correctness property is preserved: the count reads committed `cloud_job` rows, and the tick still holds `pg_advisory_xact_lock` across count+claim+dispatch (`release_awaiting_cloud.py:135`) so two ticks cannot both under-count and over-dispatch.
- **Behavior-preserving equivalence** holds only when compute rows are written (step 3) AND `FileState PUSHING/PUSHED` stays in lockstep with the `cloud_job` in-flight set. Keep the `FileState` flip as the load-bearing gate through phase 2; switch the *counting source* to `cloud_job.backend_id` in phase 3 when per-backend caps go live.

### `reconcile_cloud_jobs` + recovery ledger become `backend_id`-aware
- `reconcile_cloud_jobs` (`tasks/reconcile_cloud_jobs.py:282`) currently iterates `cloud_job WHERE status IN (SUBMITTED,RUNNING)` and calls `kube_staging` against the single global config. Change: `GROUP BY backend_id`; for each kueue backend resolve its kube config and pass it into the (now-parameterized) `kube_staging.get_job/get_workload_for/delete_job`. Compute-backend rows reconcile via their FileState/callback path, not kube.
- `recover_orphaned_work` (`tasks/reenqueue.py:327-340`) today partitions held `process_file` rows → compute agent, `push_file` → fileserver. It becomes `backend_id`-aware by the simplest safe route: on spillover, **return the file to `AWAITING_CLOUD`** and let the next scheduler tick re-pick the lowest-rank eligible backend (design §4.5). Recovery stops trying to re-home to a specific target and instead delegates to the tiered scheduler — which also removes the "held rows → most-recently-seen compute agent" fragility (`reenqueue.py:330`).

---

## 3. The Tiered Scheduler Loop (replaces the single global window)

`stage_cloud_window` (`release_awaiting_cloud.py:110-193`) keeps its skeleton — the
`_STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY` (`:69`), the FIFO `FOR UPDATE SKIP LOCKED` claim
(`get_cloud_staging_candidates`, `pipeline.py:1257`), and the single post-loop commit
(`:190`). What changes is the body inside the lock:

```
under pg_advisory_xact_lock(5_000_504):                      # UNCHANGED lock
  backends = [b for b in build_backends(cfg) if await b.is_available(session)]   # NEW
  if not backends: return no-op                              # replaces cloud_target=="local"
  # compute per-backend free slots ONCE, decrement locally as we assign this tick
  free = {b.id: b.cap - await b.in_flight_count(session) for b in backends}
  total_slots = sum(max(0, v) for v in free.values())
  if total_slots <= 0: return no-op
  candidates = get_cloud_staging_candidates(session, total_slots)   # UNCHANGED helper, new limit
  for file in candidates:
    pick = min((b for b in backends if free[b.id] > 0), key=lambda b: b.rank, default=None)
    if pick is None: break                                   # all backends full → leave in AWAITING_CLOUD
    file.state = FileState.PUSHING                            # UNCHANGED window-honesty flip
    await pick.dispatch(session, file, task_router)           # replaces the if/elif
    free[pick.id] -= 1
  await session.commit()                                      # UNCHANGED single commit
```

- **Where the single global window is replaced:** `max_in_flight = cfg.cloud_max_in_flight` +
  `slots = max_in_flight - window` (`release_awaiting_cloud.py:128,151`) becomes the per-backend
  `free[b.id] = b.cap - b.in_flight_count()` map. `cloud_max_in_flight` (`config.py:417`)
  survives only as the shim's synthesized single-entry `cap` (§5).
- **Lowest-rank-under-cap** is a per-file selection so a burst fills the free/fast backend
  (rank 10) first and only reaches local (rank 99, cap 1) when every higher-ranked backend is
  full or offline (design §4.3, decisions #4/#6).
- **Spillover** is emergent: a backend that fails `is_available()` is simply absent from
  `backends` this tick; a mid-flight failure terminalizes its `cloud_job` row and returns the
  file to `AWAITING_CLOUD`, where the next tick re-picks the next eligible backend (design §4.5).
- **Optional staleness guard** (design §4.3, deferred): only release to local after a file has
  waited beyond a threshold. Default position: skip it (rank 99 + cap 1 is enough). Flag for
  plan-time.

**Behavior-changing:** this is the first tick where >1 backend can run simultaneously. It is the
milestone's central risk and the reason phases 1–2 de-risk it.

---

## 4. Multi-Kueue — N clusters, ONE shared bucket (DIST-01 preserved)

The clean split that keeps DIST-01 intact:

| Leg | Scope | Module | Change |
|-----|-------|--------|--------|
| **S3 staging** (bytes) | **GLOBAL / shared** — one bucket, one set of `s3_*` fields | `s3_staging.py` | **UNTOUCHED.** Stays the sole S3 SDK importer. `staged_object_key(file_id)` (`s3_staging.py:58`) is already `file_id`-scoped, not cluster-scoped, so N clusters reading the same bucket by the same key is inherent — no per-cluster bucket, no per-cluster key (design decision #7, non-goal "No per-cluster S3 buckets") |
| **Kube submit/watch** (control) | **PER-CLUSTER** | `kube_staging.py` | **MODIFIED.** `_kube_config()` (`:72`, reads `get_settings()`) and `_api()` (`:87`) become `_api(kube_cfg: KueueBackendConfig)`; `submit_job/get_job/get_local_queue/get_workload_for/delete_job` take the per-cluster config. Credentials still control-plane-only |

- **DIST-01 boundary:** the CI-enforced "only `s3_staging.py` imports aioboto3" guard is
  preserved verbatim — multi-Kueue touches only `kube_staging`, never the S3 importer. The pod
  still fetches a just-in-time presigned GET (`s3_staging.presign_get`, `:176`) against the one
  shared bucket regardless of which cluster admitted it.
- **`reconcile_cloud_jobs`** resolves each row's cluster from `cloud_job.backend_id`, looks up
  that `KueueBackend`'s kube config, and reconciles against the right API server. The `*/5` cron
  cadence stays single (one cron, N clusters iterated) — per-backend cadence is a deferred
  plan-time knob (design §7).
- **`get_local_queue` probe** (`controller.startup:174`, `kube_staging.py:245`) runs once per
  kueue backend at boot; the single Redis dashboard flag `phaze:k8s:localqueue_unreachable`
  (`controller.py:184`) generalizes to a per-backend key.
- **Job-name uniqueness:** `phaze-analyze-<file_id>` (`kube_staging.py:61`) stays unique because
  a file is dispatched to exactly ONE backend at a time. **Pitfall for plan-time:** on spillover
  re-dispatch to a *different* cluster, ensure the prior cluster's Job is deleted (reconcile's
  delete-after-record, `:136`) before the new cluster submits — otherwise a stale Job could
  linger until its 900s TTL. The `cloud_job` row terminalizing before re-dispatch handles this,
  but it must be verified in the multi-Kueue phase's live E2E.

---

## 5. The `cloud_target` → `backends` Back-Compat Shim

At settings-load time, a `ControlSettings` `model_validator(mode="after")` synthesizes `backends`
from a legacy `cloud_target` when `backends` is unset (design §4.1 "Decision: ship the shim"):

| Legacy `cloud_target` | Synthesized `backends` | Preserves |
|-----------------------|------------------------|-----------|
| `local` (default) | `[]` (cloud off) | Duration router's `cloud_target != "local"` gate (`pipeline.py:395`) → `AWAITING_CLOUD` never populated; pure-local behavior byte-identical |
| `a1` | `[{id:"a1", kind:"compute", rank:10, cap:cloud_max_in_flight, agent_ref:None}]` | `agent_ref:None` ⇒ `ComputeAgentBackend` falls back to `select_active_agent(kind="compute")` (today's exact behavior); `cap=cloud_max_in_flight` preserves the global window |
| `k8s` | `[{id:"k8s", kind:"kueue", rank:10, cap:cloud_max_in_flight, kube:<flat kube_* fields>}]` | The flat `kube_api_url/namespace/local_queue/job_*` fields (`config.py:534-595`) map into the one entry's `kube{}` block; `s3_*` stays the shared global surface |

- **Validator equivalence:** the synthesized single-entry list is validated by the SAME per-entry
  validator that the three current validators (`config.py:615/636/657`) collapse into. A shimmed
  `k8s` entry still fails fast on missing `kube_api_url`, a shimmed `a1` entry still fails fast on
  missing `compute_scratch_dir` — the fail-fast semantics the current comments guard
  (`_enforce_compute_scratch_dir_when_a1`, RESEARCH Pitfall 3) survive because they run per-entry
  by kind, not on a collapsed `!= "local"` gate.
- **Shim lifetime / deprecation** is a deferred plan-time decision (design §7). Recommendation:
  ship the shim in Phase 1, keep it through the milestone, log a deprecation notice, and remove
  `cloud_target` in a later cleanup — never in this milestone (it is the only back-compat bridge).

---

## Recommended Build Order (dependency-strict, matches design §8)

| Phase | Deliverable | Behavior | Key seams touched |
|-------|-------------|----------|-------------------|
| **67 · Backend registry & config model** | `backends:` list on `ControlSettings`, one per-entry fail-fast validator, `cloud_target`→`backends` shim. Registry is READ but the single dispatch path is retained (shim yields a one-entry list). | **Behavior-preserving** (shim = no-op equivalence) | `config.py` only |
| **68 · `Backend` protocol + 3 impls** | `services/backends.py` with `LocalBackend`/`ComputeAgentBackend`/`KueueBackend`; refactor `stage_cloud_window`'s `if/elif` (`release_awaiting_cloud.py:126/142/177`) behind `dispatch`/`is_available`; `cloud_job.backend_id` additive migration + backfill + compute-push recording; parameterize `kube_staging` by a backend config object. Still single-backend driven by the shim's one entry. | **Behavior-preserving** | `services/backends.py` (new), `release_awaiting_cloud.py`, `cloud_staging.py`, `models/cloud_job.py`, `kube_staging.py`, `reconcile_cloud_jobs.py` |
| **69 · Tiered scheduler** | rank/cap drain loop; per-backend `in_flight_count` from `cloud_job.backend_id` (retire global `get_cloud_window_count`); multiple backends live simultaneously; spillover via return-to-`AWAITING_CLOUD`; `recover_orphaned_work` delegates spillover to the scheduler. | **Behavior-CHANGING** (first >1-backend tick) | `release_awaiting_cloud.py`, `pipeline.py` (window helpers), `reenqueue.py` |
| **70 · Multi-Kueue** | N kueue backends sharing one S3 bucket; per-cluster kube config + LocalQueue probe/reconcile; `reconcile_cloud_jobs` resolves cluster per `backend_id`; per-backend dashboard-reachability flags. | **Behavior-CHANGING** (cluster multiplicity) | `kube_staging.py`, `reconcile_cloud_jobs.py`, `controller.py` startup probe |
| **71 · Deployment, config & docs** | per-backend `_FILE` secrets, operator runbook, master revert toggle, admin/UI N-lane surfacing (generalizes v7.0 Phase 58 local/A1/k8s lane cards to N lanes). | Presentation/ops | `docs/`, `routers/pipeline.py:572` dashboard context, templates |

**Why 1→2 de-risk 3:** phases 67–68 land the registry, the protocol, the `backend_id` column,
and the per-backend kube parameterization while the shim keeps exactly one backend live — so the
system's *observable* behavior is unchanged and every refactor is testable against the v6.0
golden path. Phase 69 then flips on multiplicity as an isolated, reviewable behavior change.

---

## Anti-Patterns (specific to this integration)

### Anti-Pattern 1: Collapsing the 3 validators into a single `!= "local"` gate
**What people do:** replace the three per-target validators with one "cloud on ⇒ require
everything" check. **Why it's wrong:** it silently changes a1's fail-fast semantics (a1 needs
`compute_scratch_dir` but NOT `kube_*`/`s3_*`; k8s needs `kube_*`+`s3_*` but not scratch — see the
explicit comments at `config.py:621/643/665`). **Instead:** one validator that runs **per
`backends[]` entry, keyed on `kind`**, preserving each kind's exact required set.

### Anti-Pattern 2: Making `in_flight_count` read `FileState` instead of `cloud_job.backend_id`
**What people do:** keep counting `FileState IN {PUSHING,PUSHED}` per backend. **Why it's wrong:**
`FileState` carries no `backend_id`, so it cannot distinguish which backend a `PUSHING` file
belongs to — the whole point of per-backend caps. **Instead:** count `cloud_job WHERE backend_id`
(after ensuring compute pushes write a `cloud_job` row too). Keep the `FileState` flip only as the
window-honesty signal within the advisory-locked tick.

### Anti-Pattern 3: Per-cluster S3 buckets or letting `kube_staging` import aioboto3
**What people do:** give each Kueue cluster its own bucket / staging module. **Why it's wrong:**
breaks DIST-01's "single S3 importer" CI guard and duplicates the byte path. **Instead:** ONE
shared bucket via the untouched `s3_staging`; only the kube *submit* leg is per-cluster.

### Anti-Pattern 4: Re-homing spilled files to a specific backend in recovery
**What people do:** extend `recover_orphaned_work` to re-dispatch a failed file to a named
backend. **Why it's wrong:** duplicates the scheduler's rank/cap logic and races it. **Instead:**
return the file to `AWAITING_CLOUD` and let the next `stage_cloud_window` tick re-pick (design
§4.5).

---

## Integration Points

### Internal boundaries (what changes vs what holds)

| Boundary | Communication | Change |
|----------|---------------|--------|
| scheduler ↔ backends | in-process Protocol calls | NEW — replaces in-process `if/elif` |
| backends ↔ `cloud_job` | ORM per-`backend_id` rows | MODIFIED — additive column, compute now writes rows |
| control ↔ Kueue cluster(s) | kr8s over Tailscale/WireGuard, per-cluster config | MODIFIED — parameterized, N clusters |
| control ↔ S3 | aioboto3 presign, ONE shared bucket | UNTOUCHED (DIST-01) |
| agent/pod → control | `PUT /api/internal/agent/analysis/{file_id}` | UNTOUCHED — sole result writer, backend-agnostic |
| duration router → `AWAITING_CLOUD` | FileState gate | UNTOUCHED — still `long → AWAITING_CLOUD` |

### External services

| Service | Integration Pattern | Notes |
|---------|---------------------|-------|
| Kueue cluster(s) | kr8s suspended `batch/v1` Job + Workload watch, per-cluster kubeconfig/SA-token `_FILE` secrets | Job name `phaze-analyze-<file_id>` unique per file; verify stale-Job cleanup on cross-cluster spillover |
| S3-compatible bucket | aioboto3 presigned multipart PUT / JIT GET, one shared bucket | `file_id`-scoped keys already cluster-agnostic |
| Compute agents (OCI/AWS/GCP VMs) | `kind=compute` SAQ agent, rsync-over-SSH push, bound via `agent_ref` | `agent_ref` must resolve a SPECIFIC agent (by `Agent.id`/`name` — both exist on `models/agent.py`), not "most recently seen"; a real change from today's `select_active_agent(kind="compute")` when N compute providers coexist |

## Sources

- `docs/superpowers/specs/2026-06-29-multi-cloud-backends-design.md` (LOCKED design §1–8) — HIGH
- Real files/symbols on `SimplicityGuy/Multi-Cloud-Backends`: `config.py`, `tasks/release_awaiting_cloud.py`, `services/{s3_staging,kube_staging,cloud_staging,pipeline,enqueue_router}.py`, `models/{cloud_job,agent}.py`, `tasks/{reconcile_cloud_jobs,submit_cloud_job,reenqueue,controller}.py`, `routers/pipeline.py` — HIGH (read directly)
- `.planning/PROJECT.md` (DIST-01 boundary, v5/v6 Key Decisions, validated requirements) — HIGH

---
*Architecture research for: pluggable multi-cloud analysis-backend registry (phaze 2026.7.1)*
*Researched: 2026-07-03*
