# Phase 73: Per-Agent Dispatch, Liveness, Scratch & Failure Isolation - Research

**Researched:** 2026-07-05
**Domain:** Internal control-plane dispatch seams (rsync-push destination, callback reconcile attribution, drain-tick failure isolation) — a pure application-code widening of the existing `Backend` protocol + push pipeline.
**Confidence:** HIGH (every seam verified against live code at cited line numbers; zero new dependencies; decisions locked in CONTEXT.md D-01..D-08)

## Summary

Phase 73 is the deliberate **compute-side twin of the already-shipped Phase 70 (multi-Kueue)**. Every architectural decision is locked in `73-CONTEXT.md` (D-01..D-08). This research is a **verification pass**, not an exploration: it confirms the code seams named in CONTEXT still exist at the cited locations, corrects the few approximate line numbers, extracts the exact Phase-70 `KueueBackend` template to copy, and surfaces three implementation landmines the planner must task explicitly.

The headline finding: **the rank/cap load-spread (MCOMP-04) and one-flaky-agent failure isolation (MCOMP-05) machinery already exists and is live** — the drain tick at `release_awaiting_cloud.py:151-156` already wraps each backend's snapshot in a per-backend `try/except` that degrades a flaky lane to 0 slots, and `select_backend` already iterates the N-backend snapshot rank-first. Per CONTEXT D-08, Phase 73 **adds regression tests only** for these two. The genuine code changes concentrate in three files: `services/backends.py` (`ComputeAgentBackend.dispatch` stamps the destination), `tasks/push.py` (`_build_rsync_argv` reads the payload not `cfg`), and `routers/agent_push.py` (`/pushed` + `/mismatch` resolve scratch/agent/queue from the recorded `cloud_job.backend_id` + validate the reporting agent).

**Primary recommendation:** Mirror Phase 70 verbatim. Add a `resolve_compute_backend(cfg, backend_id) -> ComputeBackend | None` helper (the exact inverse-lookup twin of `s3_staging.resolve_bucket_config`, backends.py precedent), stamp `dest_host`/`dest_scratch_dir`/`dest_ssh_user` onto `PushFilePayload` at dispatch, and switch all four single-global reads to read the recorded value. `active_compute_scratch_dir` has exactly one runtime reader (`agent_push.py:133`) and is safely deletable once D-06 lands.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

**Per-agent push destination (MCOMP-03)**
- **D-01: Destination source = `backends.toml` `ComputeBackend`.** Each compute entry gains the **push host** (and an optional `ssh_user`) alongside its existing `scratch_dir` / `agent_ref` (`config_backends.py` `ComputeBackend`, L79–104). `backends.toml` stays the single registry — the host is **not** taken from the Agent DB row or a fileserver-side map.
- **D-02: Control resolves + stamps the destination; record-don't-rederive.** The control plane resolves the destination from the file's recorded `cloud_job.backend_id` → that `ComputeBackend` entry, and **stamps `host` + `scratch_dir` (+ `ssh_user`) into `PushFilePayload`**. Mirrors Phase 70 stamping `staging_bucket` on the dispatch upsert. `push.py` `_build_rsync_argv` reads the destination **from the payload**, not from its own `AgentSettings`.
- **D-03: SSH secret material stays agent-side.** `push_ssh_key` + `push_known_hosts` remain on the fileserver agent (never cross into control config or the payload). The fileserver's `known_hosts` pins **all N** compute host keys; one fileserver key is authorized on each compute host.
- **D-04: Retire the fileserver's single push destination env.** The agent-side `push_ssh_host` + `cloud_scratch_dir` (the *remote-target* mirror on the fileserver) are superseded by the payload-carried per-backend destination — no `≤1` fallback path. NOTE: the **compute agent's own** local `cloud_scratch_dir` (its receive/read + scratch-janitor dir, `agent_worker.py:103`) is unchanged — it is that agent's local dir and must equal the backend entry's `scratch_dir`.

**cloud_job cardinality (MCOMP-06 — RESOLVED)**
- **D-05: Stay one-row-per-file, keyed by `backend_id`.** Keep `cloud_job.file_id` `unique=True` (`models/cloud_job.py:72`). `backend_id` records the **current** dispatch target; on spill, `dispatch` re-upserts the **same** row with a new `backend_id`. **No migration, no schema change** — mirrors Phase 70 MKUE verbatim. Attribution derives entirely from the recorded `backend_id`.

**/pushed + /mismatch reconcile attribution (MCOMP-06)**
- **D-06: Resolve scratch + terminalization from the recorded `backend_id`.** `/pushed` and `/mismatch` (`routers/agent_push.py` ~L93, L133) replace `select_active_agent(kind="compute")` + the global `active_compute_scratch_dir` with resolution from the file's `cloud_job.backend_id` → that `ComputeBackend`'s `scratch_dir`. Terminalization stays keyed by `file_id`.
- **D-07: Validate the reporter; reject on mismatch.** The callback resolves `cloud_job.backend_id → ComputeBackend.agent_ref` and **verifies the bearer-token agent matches**. On mismatch → reject (4xx) and **do not terminalize**. We do **not** re-stamp `backend_id` from the reporting token.

**Rank/cap load-spread + failure isolation (MCOMP-04/05)**
- **D-08: Pure verbatim reuse — no new scheduler policy.** Rank-first eligible dispatch + per-agent `cap` + spill-when-full/offline (Phase-69 `select_backend`) and per-backend snapshot `try/except` isolation (Phase-70 MKUE-03) already cover compute backends as-is. Free arm64 = **lower** `rank`, paid/trial x86 = higher `rank` — pure operator config. Phase 73 adds **regression tests only**; cost-tiering guidance is Phase 74 docs.

### Claude's Discretion
- **`PushFilePayload` field shape** — three flat fields (`dest_host`, `dest_scratch_dir`, `dest_ssh_user`) vs a nested destination submodel; keep the `extra="forbid"` + absolute-path / argv-injection field validators (`schemas/agent_tasks.py:72–83`) and add matching validation for the new host/scratch fields.
- **`ssh_user` placement** — travels with the destination in the payload when a backend specifies it; otherwise defaults to the fileserver agent's configured user. Pick the least-surface option.
- **Config field for the host** — `push_host` / `host` / `ssh_host`; follow the closest Phase-67/68 config idiom and keep the id-tagged `_require_dispatch_fields` validator style.
- **Whether `active_compute_scratch_dir` (config.py `@property`, L483) is deleted outright or kept for a transitional test** — confirm no other reader remains (this research confirms: only `agent_push.py:133`), then delete.

### Deferred Ideas (OUT OF SCOPE)
None from this phase's discussion. Explicitly out of scope: N-lane compute UI + operator runbook + mixed arch cost-tiering docs → **Phase 74 (MCOMP-07)**; capability-aware/arch-matched routing → **PROV-02** (v2); compute-agent provisioning/autoscaling → **PROV-03** (v2); any new routing semantics beyond rank/cap; Kueue-side changes; the `2026.7.2` release PR/tag.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| MCOMP-02 | Per-agent liveness — each compute backend gates on ITS bound agent | **Largely already delivered** by Phase 72: `ComputeAgentBackend.is_available` (backends.py:265-278) resolves `self._agent_ref()` via `select_agent_by_id`. Phase 73 adds an N-compute regression (one bound agent online, one offline → only the online lane eligible in the drain snapshot). |
| MCOMP-03 | Per-agent push/scratch destination resolved per file from `cloud_job.backend_id` | Verified seams: `ComputeAgentBackend.dispatch` (backends.py:280-316) is the stamp site; `PushFilePayload` (agent_tasks.py:54-84) is the safe extension point; `_build_rsync_argv` (push.py:80-109) is the single argv builder reading `cfg.*` at L99; `/pushed` scratch at agent_push.py:133. |
| MCOMP-04 | Rank/cap load-spread across N compute agents | **Machinery exists** — `select_backend` iterates the rank-ordered N-backend snapshot; the drain (`stage_cloud_window`) enforces `remaining = cap - in_flight_count`. Add regression only (D-08). |
| MCOMP-05 | One flaky compute agent isolated to 0 slots without failing the drain | **Machinery exists** — per-backend snapshot `try/except` at release_awaiting_cloud.py:151-156 degrades a raising/timed-out lane to `{available: False, remaining: 0}` and continues. Add regression only (D-08). |
| MCOMP-06 | Per-backend reconcile attribution — no cross-agent mis-attribution | Verified seams: `/pushed` (agent_push.py:65-150) + `/mismatch` (agent_push.py:153-261). D-07 reporter-validation is the security core; template is `KueueBackend.reconcile`'s `WHERE backend_id == self.id` scoping (backends.py:426-432). |
</phase_requirements>

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Resolve per-file push destination | **Control (API/backend)** | — | Only the control plane has the ORM + registry to map `cloud_job.backend_id → ComputeBackend`. Record-don't-rederive (D-02): resolve once at dispatch, stamp into the payload. |
| Execute rsync push | **Fileserver agent (SAQ worker)** | — | The agent owns the media mount + SSH secret material (D-03). It reads the destination **from the payload**, never re-derives it. |
| Terminalize + re-drive callbacks | **Control (internal API router)** | — | Agents are Postgres-free; `/pushed` + `/mismatch` are the only ORM-holding terminalization path. |
| Reporter authentication | **Control (token dependency + D-07 backend_id match)** | — | `get_authenticated_agent` gives identity; D-07 adds the backend-scoped authorization check. |
| Rank/cap scheduling + failure isolation | **Control (drain cron)** | — | `stage_cloud_window` snapshots every backend once/tick under an advisory lock; per-backend `try/except` isolates a flaky lane. |
| SSH key / known_hosts custody | **Fileserver agent (SecretStr → 0600 temp file, shredded)** | — | D-03: secret material never crosses into control config or the payload. |

## Standard Stack

**Zero new dependencies.** This is a pure application-code extension of the existing `Backend` protocol + push/rsync pipeline (CONTEXT `<specifics>`). No new pip deps, no Kueue-side changes.

Relevant existing stack (all already in `pyproject.toml`, verified present in the codebase):

| Library | Purpose | Role in this phase |
|---------|---------|--------------------|
| Pydantic v2 | Payload validation | Extend `PushFilePayload` with destination fields + validators (`extra="forbid"`) |
| SQLAlchemy 2.0 (async) | ORM | `cloud_job.backend_id` reads/upserts in `/pushed`, `/mismatch`, `dispatch` |
| SAQ | Task queue | `push_file` job carries the widened payload |
| FastAPI | Internal API | `/pushed` + `/mismatch` routers gain reporter validation |
| structlog | Logging | Secret-free `{id, kind, rank, cap}` projections only (existing discipline) |

**No installation step. No `## Package Legitimacy Audit` required** — this phase installs no external packages.

## Architecture Patterns

### Push destination flow (end-to-end) — every single-global read that must switch

```
                          CONTROL PLANE                                    FILESERVER AGENT
  ┌─────────────────────────────────────────────────┐        ┌──────────────────────────────────┐
  │ drain tick: stage_cloud_window                   │        │                                  │
  │   select_backend → ComputeAgentBackend.dispatch  │        │                                  │
  │     ├─ upsert cloud_job(backend_id=self.id) ─────┼──[D-05 one row/file, unique(file_id)]     │
  │     ├─ [D-02 NEW] resolve dest from self.config: │        │                                  │
  │     │     host / scratch_dir / ssh_user          │        │                                  │
  │     └─ _enqueue_push_file(payload+dest) ─────────┼──SAQ──▶│ push_file(payload)               │
  │                                                  │        │   _build_rsync_argv:             │
  │                                                  │        │   ★ push.py:99 remote_dest reads │
  │                                                  │        │     cfg.push_ssh_* + cfg.cloud_  │
  │                                                  │        │     scratch_dir  → SWITCH to     │
  │                                                  │        │     payload.dest_*  (D-02/D-04)  │
  │                                                  │        │   rsync ──▶ compute agent scratch│
  │ POST /pushed  ◀──────────────────────────────────┼──HTTP──┤   api.report_pushed(file_id)     │
  │   ★ L93  select_active_agent(kind="compute")     │        └──────────────────────────────────┘
  │        → SWITCH: resolve compute agent from      │
  │          cloud_job.backend_id → agent_ref (D-07) │
  │   ★ L131 queue_for(compute_agent.id)             │
  │        → SWITCH: queue_for the resolved agent_ref │
  │   ★ L133 active_compute_scratch_dir              │
  │        → SWITCH: resolved ComputeBackend.scratch_dir (D-06)
  │   D-07: verify token agent == resolved agent_ref │
  │         else 4xx, DO NOT terminalize             │
  └──────────────────────────────────────────────────┘
```

**The five single-global read points that must become `backend_id`-scoped:**

| # | Location | Current read | Switch to | Decision |
|---|----------|--------------|-----------|----------|
| 1 | `push.py:99` (`_build_rsync_argv`) | `cfg.push_ssh_user@cfg.push_ssh_host:cfg.cloud_scratch_dir` | `payload.dest_ssh_user@payload.dest_host:payload.dest_scratch_dir` | D-02/D-04 |
| 2 | `push.py:112-115` (`_require_push_config`) | requires `push_ssh_host, push_ssh_user, cloud_scratch_dir, push_ssh_key, push_known_hosts` on `cfg` | drop `push_ssh_host` + `cloud_scratch_dir` from the fileserver's required set (payload carries them); **keep** `push_ssh_key` + `push_known_hosts` (secret material, D-03) | D-03/D-04 |
| 3 | `agent_push.py:133` (`/pushed`) | `settings.active_compute_scratch_dir` | resolve `cloud_job.backend_id → ComputeBackend.scratch_dir` | D-06 |
| 4 | `agent_push.py:93,131` (`/pushed`) | `select_active_agent(kind="compute")` + `queue_for(compute_agent.id)` | resolve compute agent from `cloud_job.backend_id → agent_ref`, then `queue_for(agent_ref)` | D-06/D-07 |
| 5 | **`agent_push.py:232-237` (`/mismatch` re-drive)** | builds a fresh `PushFilePayload` with **no destination** | must ALSO stamp `dest_*` from `cloud_job.backend_id → ComputeBackend` — **see Landmine 1** | D-02 |

### Pattern: the Phase-70 `KueueBackend` verbatim template (copy this)

Phase 73 is the compute-side twin. Copy these three Phase-70 idioms concretely:

**(a) Per-entry binding bound at construction, read per-call** — `KueueBackend._kube()` (backends.py:336-347):
```python
# backends.py:336 — the exact template. Compute's per-entry accessor ALREADY EXISTS:
# ComputeAgentBackend._agent_ref() (backends.py:251-263, added Phase 72). No new accessor needed —
# for the destination, dispatch reads self.config.scratch_dir / self.config.<host field> directly.
def _kube(self) -> KubeConfig:
    kube = getattr(self.config, "kube", None)
    if kube is None:
        raise kube_staging.KubeStagingError(f"kueue backend {self.id!r} has no [kube] config bound")
    return cast("KubeConfig", kube)
```

**(b) Dispatch stamps the destination onto the record** — `KueueBackend.dispatch` (backends.py:399-401):
```python
# backends.py:401 — Kueue stamps staging_bucket. Compute's twin: stamp dest_* into the PushFilePayload
# built in _enqueue_push_file (backends.py:95-100). self.config IS the ComputeBackend, so dispatch already
# has host/scratch_dir/agent_ref in hand — no re-lookup needed at the dispatch site (CONTEXT Integration Points).
await session.execute(
    update(CloudJob).where(CloudJob.file_id == file.id).values(backend_id=self.id, staging_bucket=bucket_id)
)
```

**(c) Reconcile scoped `WHERE backend_id == self.id`** — `KueueBackend.reconcile` (backends.py:426-432):
```python
# backends.py:426 — the per-backend attribution scoping. Compute's /pushed + /mismatch are callback-driven
# (not a cron), so the equivalent is: read the cloud_job row by file_id, then resolve its RECORDED backend_id
# to the ComputeBackend — never select_active_agent(kind="compute").
rows = (await session.execute(
    select(CloudJob).where(
        CloudJob.status.in_([CloudJobStatus.SUBMITTED.value, CloudJobStatus.RUNNING.value]),
        CloudJob.backend_id == self.id,
    )
)).scalars().all()
```

**(d) The by-id resolver to mirror** — `s3_staging.resolve_bucket_config` (s3_staging.py:91-105):
```python
# s3_staging.py:91 — the AUTHORITATIVE inverse-lookup template. Add a twin in services/backends.py:
#   def resolve_compute_backend(cfg: ControlSettings, backend_id: str | None) -> ComputeBackend | None:
#       if backend_id is None: return None
#       return {b.id: b for b in cfg.backends if b.kind == "compute"}.get(backend_id)
# /pushed + /mismatch call this with the file's cloud_job.backend_id (record-don't-rederive).
def resolve_bucket_config(cfg: ControlSettings, bucket_id: str | None) -> BucketConfig | None:
    if bucket_id is None:
        return None
    return {bucket.id: bucket for bucket in cfg.buckets}.get(bucket_id)
```

### Per-backend snapshot isolation ALREADY LIVE (D-08, MCOMP-05)

```python
# release_awaiting_cloud.py:151-156 — the flaky-lane isolation. Compute backends flow through this
# UNCHANGED. A ComputeAgentBackend.is_available that raises/hangs degrades THAT lane to 0 slots; the
# tick completes and healthy lanes still dispatch. Phase 73 adds a regression, not code (D-08).
try:
    available = await backend.is_available(session)
    remaining = max(0, backend.cap - await backend.in_flight_count(session))
except Exception:
    logger.warning("stage_cloud_window: backend snapshot probe failed -> treating as unavailable (0 slots)", backend_id=backend.id)
    snapshot[backend.id] = {"backend": backend, "available": False, "remaining": 0, "cap": backend.cap}
    continue
```

### Anti-Patterns to Avoid
- **Re-deriving the destination anywhere downstream of dispatch.** The recorded `backend_id` is authoritative. `_build_rsync_argv`, `/pushed`, `/mismatch` all READ the recorded value — never call `select_active_agent(kind="compute")` or `active_compute_scratch_dir`.
- **Re-stamping `backend_id` from the reporting token (D-07).** A late/wrong report must NOT overwrite the dispatch decision. Validate-and-reject, never validate-and-adopt.
- **Leaking SSH secret material into the payload or control config (D-03).** `push_ssh_key`/`push_known_hosts` stay agent-side SecretStr, materialized to 0600 temp files, shredded in `finally` (push.py:198-202). Only `host`/`scratch_dir`/`ssh_user` (non-secret) travel in the payload.
- **Deleting `cloud_scratch_dir` from `AgentSettings` config.** It has a **dual use** — see Landmine 2.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Backend-id → config lookup | A bespoke dict/loop in each callback | `resolve_compute_backend(cfg, backend_id)` helper mirroring `s3_staging.resolve_bucket_config` (s3_staging.py:91) | Single authoritative inverse-lookup; pure + ORM-free; one test surface |
| Per-entry agent resolution | New selector | `select_agent_by_id(session, agent_id, kind="compute")` (enqueue_router.py:131) — already used by `is_available` | Phase 72 already built + tested this per-entry path |
| Payload destination validation | Ad-hoc string checks | Pydantic `field_validator` on the new `dest_*` fields (agent_tasks.py:72-83 precedent) | `extra="forbid"` + absolute-path/alnum validators are the established argv-injection defense |
| Rank/cap spread + flaky isolation | Any new scheduler logic | `select_backend` + the drain's `try/except` snapshot (release_awaiting_cloud.py:151) | D-08: machinery exists; regression tests only |
| Reporter authentication | New auth flow | `get_authenticated_agent` token dependency + a `backend_id → agent_ref` equality check | AUTH-01 discipline already in place; D-07 adds one comparison |

**Key insight:** almost nothing here is new construction. The phase is a *rewiring* — swap three global reads for recorded-value reads, add one inverse-lookup helper, add two payload fields, and add regression tests for machinery that already runs.

## Runtime State Inventory

> Rename/refactor-adjacent (retiring a global config accessor + agent-side env). Categories answered explicitly.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | `cloud_job.backend_id` already records the dispatch target for every compute row (models/cloud_job.py:98). **No migration** (D-05) — the column exists, `unique(file_id)` preserved (L72). In-flight rows at deploy time already carry a `backend_id` from Phase 68+. | None — verified no schema change needed |
| Live service config | `backends.toml` gains a per-compute-entry push host field (D-01). Operator edits the file; no DB/UI state. The compute agent's `PHAZE_CLOUD_SCRATCH_DIR` env (its local janitor dir) **must equal** the backend entry's `scratch_dir` (D-04 note) — an operator-side invariant, not code. | Config edit (operator); Phase 74 documents it |
| OS-registered state | None — no OS-registered names embed the retired env. Verified: `push_ssh_host`/`cloud_scratch_dir` are read only in `push.py` (fileserver) + `agent_worker.py:103` (compute janitor). | None |
| Secrets/env vars | `push_ssh_key` + `push_known_hosts` (SecretStr) stay agent-side, **unchanged** (D-03). `push_ssh_host` env read by the fileserver as the remote target is retired from the push path (D-04) but the FIELD may remain (least-surface). `cloud_scratch_dir` env has a dual reader — **see Landmine 2**. | Code rename only (stop reading host/scratch as destination on the fileserver); no secret-key change |
| Build artifacts | None — pure source change, no packaged/compiled artifact carries these names. | None — verified by grep |

**The canonical question — after every file is updated, what still reads the old global?** Answer: the `active_compute_scratch_dir` property (config.py:483) has exactly one runtime reader (`agent_push.py:133`); once D-06 switches it, the property is dead and deletable. All other matches are docstrings/comments/tests (see reader audit below).

### `active_compute_scratch_dir` reader audit (D-06 deletability)

```
RUNTIME readers (must switch before delete):
  src/phaze/routers/agent_push.py:133   scratch_path = f"{settings.active_compute_scratch_dir}/..."   ← THE ONLY ONE

Non-runtime (comments/docstrings — no switch needed):
  src/phaze/config.py:484 (the property def), :610, :827   (doc comments)
  src/phaze/routers/agent_push.py:14, :77                   (docstrings)
  src/phaze/services/backends.py:500                        (docstring)

Tests (update alongside D-06):
  tests/agents/routers/test_agent_push.py       (5 refs — the /pushed scratch-path assertions)
  tests/shared/config/test_bucket_registry.py   (5 refs — the property's ≤1/N reduction tests)
  tests/analyze/services/test_compute_binding_golden.py (5 refs — Phase 72 golden)
  tests/analyze/services/test_backends.py:914-938 (Pitfall-1 reduction test)
```

**Verdict: D-06 can delete `active_compute_scratch_dir` outright** — its single runtime reader (`agent_push.py:133`) is exactly the line D-06 rewires. No blocker. The property's tests either move to characterize the new `resolve_compute_backend` path or are removed with the property.

## Common Pitfalls

### Pitfall 1 (LANDMINE): the `/mismatch` re-drive builds a destination-less payload
**What goes wrong:** `report_push_mismatch` (agent_push.py:232-237) rebuilds a fresh `PushFilePayload(file_id, original_path, file_type, agent_id)` to re-enqueue `push_file` under the cap. Under D-02 this payload now **needs the destination fields** — a rebuild without them re-drives a push to nowhere (or, worse, an empty/`None` destination). The `/pushed` path gets attention naturally; this re-drive is easy to miss.
**Why it happens:** the destination is a NEW field; the existing rebuild predates it. Two build sites now exist (`_enqueue_push_file` at backends.py:95 AND this re-drive at agent_push.py:232).
**How to avoid:** in `/mismatch`, resolve `cloud_job.backend_id → ComputeBackend` (via the new `resolve_compute_backend` helper) and stamp `dest_*` into the re-driven payload, identically to dispatch. Consider factoring the payload-build into one helper both sites call.
**Warning signs:** a re-driven push after a sha256 mismatch fails or lands in the wrong scratch dir; the `dest_*` fields are `None` in the ledger payload JSONB after `/mismatch`.

### Pitfall 2 (LANDMINE): `cloud_scratch_dir` has TWO readers with opposite fates
**What goes wrong:** D-04 says "retire the fileserver's `cloud_scratch_dir`". But `cfg.cloud_scratch_dir` is read in **two** places with different meaning:
- `push.py:99` — the **fileserver** reads it as the *remote* push target → **retire** (payload carries it).
- `agent_worker.py:103` — the **compute agent** reads it as its *local* receive/scratch-janitor dir → **keep unchanged** (D-04 explicit note).
Deleting the config field entirely breaks the compute agent's scratch sweep.
**Why it happens:** the same field name serves both roles because in the ≤1-compute world the fileserver's remote target and the compute agent's local dir were the same path.
**How to avoid:** keep the `cloud_scratch_dir` field on `AgentSettings`; only stop the **fileserver's** `push.py` from reading it as the destination (read `payload.dest_scratch_dir` instead). The compute agent keeps using its own `cloud_scratch_dir` locally. Drop only `cloud_scratch_dir` + `push_ssh_host` from `_require_push_config`'s fileserver-required set (push.py:115).
**Warning signs:** the compute agent's scratch janitor stops sweeping; `agent_worker.py:103` gets an `AttributeError`.

### Pitfall 3: `reenqueue.py:374` remains a single-active-compute reader (recovery path)
**What goes wrong:** `recover_orphaned_work` (reenqueue.py:374) re-drives orphaned `process_file` rows for AWAITING_CLOUD-held files to `select_active_agent(kind="compute")` — "the single active compute agent". In a true N-compute deploy this routes a held file's analysis to an arbitrary compute agent, not necessarily the one it was pushed to.
**Why it happens:** it predates per-entry binding; it is a **recovery** path, not the dispatch/push/reconcile core.
**How to avoid:** **This is likely out of scope for Phase 73** (CONTEXT does not list `reenqueue.py` among the seams; the phase boundary is dispatch/push/reconcile). But it is a lingering `≤1-compute` assumption. **Recommendation:** flag it in the plan as a documented known-limitation / follow-up (PROV-01 backlog), NOT silently widen it — widening recovery re-drive semantics risks the 44.5k over-enqueue incident class (STATE.md). See Open Questions.
**Warning signs:** an operator with 2+ compute agents notices a recovered held file analyzed on the wrong agent. Low frequency (recovery-only, held-file-only edge).

### Pitfall 4: `process_file` queue in `/pushed` must target the file's OWN compute agent
**What goes wrong:** after a successful push, `/pushed` enqueues `process_file` on `queue_for(compute_agent.id)` (agent_push.py:131) where `compute_agent` came from `select_active_agent(kind="compute")`. In an N-compute world the file was pushed to a SPECIFIC agent's scratch dir — its analysis MUST run on THAT agent (the scratch copy only exists there).
**Why it happens:** the scratch copy is local to the target compute agent; routing analysis elsewhere finds no file.
**How to avoid:** D-06/D-07 already require resolving the compute agent from `cloud_job.backend_id → agent_ref`. Use that same resolved agent for `queue_for(...)` — do NOT use the token agent or `select_active_agent`. (The token agent and the resolved agent should match after D-07 validation, but the **recorded** `backend_id` is the source of truth.)
**Warning signs:** `process_file` fails with a missing scratch file on a multi-compute deploy.

## Code Examples

### Extending `PushFilePayload` with destination fields (discretion: flat fields)
```python
# Source: schemas/agent_tasks.py:54-84 (extend the existing model, mirror its validators)
class PushFilePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: uuid.UUID
    original_path: str
    file_type: str
    agent_id: str
    # D-02 NEW — per-backend destination stamped by ComputeAgentBackend.dispatch (record-don't-rederive).
    dest_host: str
    dest_scratch_dir: str
    dest_ssh_user: str | None = None  # defaults to the fileserver's configured user when None (discretion)

    @field_validator("original_path")
    @classmethod
    def _original_path_absolute(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("original_path must be an absolute path")
        return v

    @field_validator("dest_scratch_dir")
    @classmethod
    def _dest_scratch_absolute(cls, v: str) -> str:
        # Same argv-injection / path-traversal defense as original_path (the -- terminator + absolute path).
        if not v.startswith("/"):
            raise ValueError("dest_scratch_dir must be an absolute path")
        return v
    # Add a dest_host validator rejecting shell metacharacters / whitespace (it lands in the ssh remote spec).
```

### `/pushed` reporter validation + scratch resolution (D-06/D-07)
```python
# Source: intended change to routers/agent_push.py:84-133 (mirrors the existing WR-02 guard style)
settings = cast("ControlSettings", get_settings())
file = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one()

# D-06: read the RECORDED backend, never select_active_agent(kind="compute").
cloud_job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one_or_none()
backend = resolve_compute_backend(settings, cloud_job.backend_id if cloud_job else None)
if backend is None:
    # No recorded compute backend (unattributed / spilled) -> clean 200 hold, mirroring the existing no-agent hold.
    return PushedResponse(file_id=file_id)

# D-07: verify the reporting token agent IS the file's dispatched agent. Reject on mismatch; do NOT terminalize.
if agent.id != backend.agent_ref:
    logger.warning("report_pushed rejected: reporter != dispatched agent", file_id=str(file_id),
                   reporter=agent.id, expected=backend.agent_ref)
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="reporting agent is not the dispatched compute agent")

# D-06: scratch + queue come from the resolved backend, not the global accessor.
scratch_path = f"{backend.scratch_dir}/{file_id}.{file.file_type}"
compute_queue = request.app.state.task_router.queue_for(backend.agent_ref)
# ... existing WR-02 PUSHING->PUSHED rowcount guard + cloud_job SUCCEEDED terminalization stay byte-identical ...
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Global `active_compute_scratch_dir` accessor (config.py:483) | Per-file `cloud_job.backend_id → ComputeBackend.scratch_dir` | Phase 73 (D-06) | Deletes the last transitional global compute read |
| Fileserver `cfg.push_ssh_host`/`cloud_scratch_dir` as the single remote target | Payload-carried `dest_*` per backend | Phase 73 (D-02/D-04) | N compute agents each get files pushed to their own host/dir |
| `select_active_agent(kind="compute")` in `/pushed`, `/mismatch` | Resolve + validate from recorded `backend_id` | Phase 73 (D-06/D-07) | No cross-agent mis-attribution (the MCOMP-06 security property) |

**Deprecated/outdated after this phase:**
- `active_compute_scratch_dir` (config.py:483) — deleted (D-06); last reader was `agent_push.py:133`.
- The fileserver's use of `push_ssh_host` + `cloud_scratch_dir` as the push destination — retired (D-04); the payload now carries the destination. (The compute agent's local `cloud_scratch_dir` stays — Pitfall 2.)

## Validation Architecture

> `workflow.nyquist_validation: true` in config.json — this section is REQUIRED and drives VALIDATION.md generation.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (`asyncio_mode = "auto"`), pyproject.toml:136-141 |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]`, `testpaths = ["tests"]` |
| Quick run command | `uv run pytest tests/analyze/services/test_backends.py tests/agents/routers/test_agent_push.py -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` (≥90% floor, per CLAUDE.md) |

### Phase Requirements → Test Map
| Req ID | Behavior to validate | Test Type | Automated Command | File Exists? |
|--------|----------------------|-----------|-------------------|-------------|
| MCOMP-02 | N compute backends: agent A online, agent B offline → only A's lane eligible in the drain snapshot (per-entry `is_available` gates on the bound agent) | regression | `uv run pytest tests/analyze/services/test_backends.py -k "is_available and compute" -x` | ✅ extend (test_backends.py) |
| MCOMP-03 | Two compute backends with distinct `scratch_dir`/host: each file's rsync argv (`_build_rsync_argv`) + `/pushed` scratch_path uses ITS OWN backend's destination, read from the payload/recorded `backend_id` — no cross-contamination | integration + unit | `uv run pytest tests/analyze/services/test_backends.py tests/agents/routers/test_agent_push.py -k "dest or scratch" -x` | ✅ extend + ❌ new cases |
| MCOMP-03 | `_build_rsync_argv` reads `payload.dest_*`, not `cfg.*` (pure unit) | unit | `uv run pytest tests -k "build_rsync_argv" -x` | ❌ Wave 0 (new: push.py argv from payload) |
| MCOMP-04 | N compute backends rank-ordered: drain fills lower-`rank` up to `cap`, then spills to the next-rank backend (load-spread) | regression | `uv run pytest tests/analyze/services/test_backend_selection.py -k "rank or spread" -x` | ✅ extend (test_backend_selection.py) |
| MCOMP-05 | One flaky compute backend (`is_available` raises/times out) → its lane degrades to 0 slots, the drain tick COMPLETES, healthy lanes still dispatch (isolation) | regression | `uv run pytest tests -k "stage_cloud_window and (flaky or isolat)" -x` | ❌ Wave 0 (new: N-compute one-flaky drain test) |
| MCOMP-06 | File dispatched to backend A; agent B reports `/pushed` → 4xx reject, file NOT terminalized, no cross-attribution (D-07 reporter validation) | regression | `uv run pytest tests/agents/routers/test_agent_push.py -k "reporter or mismatch_agent" -x` | ❌ Wave 0 (new: wrong-reporter rejection) |
| MCOMP-06 | `/pushed` + `/mismatch` resolve terminalization/scratch/queue from recorded `backend_id` (not `select_active_agent`) | unit | `uv run pytest tests/agents/routers/test_agent_push.py -x` | ✅ extend |
| Behavior-preservation | ≤1-compute deploy: payload now carries the (single) destination, yet `_build_rsync_argv` produces the SAME `remote_dest` string and `/pushed` the SAME `scratch_path` as before (golden characterization) | regression | `uv run pytest tests/analyze/services/test_compute_binding_golden.py -x` | ✅ extend (Phase 72 golden precedent) |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/analyze/services/test_backends.py tests/agents/routers/test_agent_push.py tests/analyze/services/test_backend_selection.py -x` (the four touched suites)
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing` (≥90% floor gate)
- **Phase gate:** full suite green + `uv run ruff check . && uv run ruff format --check . && uv run mypy .` before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/analyze/services/test_backends.py` — N-compute one-flaky-isolation drain test (MCOMP-05) + per-entry `is_available` N-compute liveness (MCOMP-02); a fixture for a `local + N-compute` `backends.toml` (the `backends_toml_env` fixture already exists at test_backends.py:917 — extend it for 2 compute entries with distinct scratch/host).
- [ ] `tests/analyze/services/test_backend_selection.py` — N-compute rank/cap load-spread (MCOMP-04).
- [ ] `tests/agents/routers/test_agent_push.py` — wrong-reporter 4xx rejection + no-terminalization (MCOMP-06/D-07); per-backend scratch resolution (D-06).
- [ ] `tests/analyze/services/test_task_push.py` (or extend existing push tests) — `_build_rsync_argv` reads `payload.dest_*` (MCOMP-03 unit).
- [ ] `tests/analyze/services/test_compute_binding_golden.py` — extend the golden to assert the ≤1-compute `remote_dest`/`scratch_path` stay byte-identical after the payload carries the single destination (behavior-preservation).
- [ ] Framework install: none — pytest/pytest-asyncio/httpx already present.

## Security Domain

> `security_enforcement` not explicitly `false` in config → enabled. This phase touches auth (reporter validation), input validation (payload dest fields), and secret custody — security review is material.

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | yes | `get_authenticated_agent` bearer-token dependency (existing, AUTH-01) |
| V4 Access Control | **yes (the phase's security core)** | **D-07: authorize the reporter against the file's dispatched `backend_id → agent_ref`; reject 4xx on mismatch, do not terminalize.** This is what makes MCOMP-06 "no cross-agent mis-attribution" a security property, not just a correctness one. |
| V5 Input Validation | yes | Pydantic `extra="forbid"` + `field_validator` on the new `dest_host`/`dest_scratch_dir`/`dest_ssh_user` (absolute-path, no shell metachars) — the destination lands in an `ssh` remote spec + rsync operand |
| V6 Cryptography / Secret custody | yes | **D-03: SSH key + known_hosts (`SecretStr`) stay agent-side**, materialized to 0600 temp files, shredded in `finally` (push.py:198-202), never logged. New `dest_*` fields are NON-secret and MUST NOT include key material. |
| V7 Logging | yes | Existing discipline: log `{id, kind, rank, cap}` / `backend_id` only — never a `SecretStr`/`*_file`/token (backends.py module docstring, config.py:512) |

### Known Threat Patterns for this stack

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| A stale/wrong/duplicate compute agent reports `/pushed` for another agent's file → mis-attribution / premature terminalization | **Spoofing / Elevation** | **D-07 reporter validation** — token agent must equal the recorded `backend_id → agent_ref`; else 4xx, no terminalize (the direct MCOMP-06 mitigation) |
| Argv/flag injection via `dest_host` / `dest_scratch_dir` reaching the `ssh` remote spec + rsync operand | **Tampering** | `--` argv terminator (already at push.py:106) + Pydantic validators on the new dest fields (absolute path, no shell metachars/whitespace); argv is a list spawned via `create_subprocess_exec`, never a shell (push.py:171) |
| SSH private key / known_hosts leaking into the payload, control config, ledger JSONB, or logs | **Information disclosure** | D-03 — secret material never crosses the agent boundary; only non-secret host/scratch/user travel in the payload; log projections stay secret-free |
| A flaky compute agent's probe failure cascading to abort the whole drain tick (DoS on healthy lanes) | **Denial of service** | Per-backend snapshot `try/except` isolation (release_awaiting_cloud.py:151-156) — already live; MCOMP-05 regression proves it |
| Wrong-agent `process_file` routing finds no scratch copy (functional break, potential retry storm) | **Tampering / DoS** | Route `process_file` to the recorded `backend_id → agent_ref` (Pitfall 4), not `select_active_agent` |

## Open Questions

1. **Is `reenqueue.py:374` (`recover_orphaned_work` held-file re-drive to `select_active_agent(kind="compute")`) in scope?**
   - What we know: it is a single-active-compute reader; CONTEXT does NOT list `reenqueue.py` among the phase seams; the phase boundary is dispatch/push/reconcile.
   - What's unclear: whether leaving it single-active is acceptable for the milestone's target deploy (which may run only 1 compute agent live initially — Kueue is the N target).
   - Recommendation: **treat as out of scope; document as a known limitation / PROV-01-backlog follow-up in the plan.** Widening recovery re-drive semantics risks the 44.5k over-enqueue incident class (STATE.md) and is not required by MCOMP-02..06. Do NOT silently change it.

2. **`ssh_user` default source when a backend omits it (discretion).**
   - What we know: D-03 keeps secret material agent-side; the fileserver already has `push_ssh_user` configured (config.py:838).
   - What's unclear: whether to default `dest_ssh_user` to the fileserver's `cfg.push_ssh_user` at the agent (least payload surface) or require it per backend.
   - Recommendation: make `dest_ssh_user` optional in the payload; when `None`, `_build_rsync_argv` falls back to `cfg.push_ssh_user` (the fileserver's configured user). Least surface, preserves ≤1-compute behavior byte-identical.

3. **Config field name for the push host (discretion).**
   - Recommendation: `push_host` on `ComputeBackend` (mirrors the agent-side `push_ssh_host` naming while staying registry-scoped); add it to the id-tagged `_require_dispatch_fields` validator (config_backends.py:91-104) as a required field, matching the `scratch_dir` fail-fast style. Confirm with the planner against the closest Phase-67/68 idiom.

## Environment Availability

> The phase is code/config + tests only — no NEW external runtime dependency is introduced. The existing rsync/ssh transport and Postgres are already provisioned. Live multi-compute E2E is a Phase 74 / deployment concern (CONTEXT out-of-scope).

Step 2.6: Effectively SKIPPED for new dependencies — this phase adds regression tests + rewires existing seams; the toolchain (`uv`, pytest, ruff, mypy) is already present per CLAUDE.md.

## Project Constraints (from CLAUDE.md)

- **Python 3.14 exclusively**; **`uv` only** — every command prefixed `uv run` (never bare `pip`/`pytest`/`mypy`).
- **Ruff**: line length 150; strict rule set incl. `S` (bandit), `ARG`, `PTH`, `TCH`; `target-version = py313` (PEP 649 annotation safety) — new dest-field validators + the `resolve_compute_backend` helper must pass `ruff check` + `ruff format`.
- **Mypy strict** (`disallow_untyped_defs`, `warn_unused_ignores`, etc.) excluding `tests/` — the new payload fields, helper, and callback changes need full type hints. `cast` usage matches existing `agent_push.py` idiom.
- **≥90% coverage** (Codecov, service flags) — the new callback branches (reporter reject, backend-not-found hold, re-drive dest stamp) must be covered; the Wave 0 test gaps above target this.
- **Pre-commit must pass** (frozen SHAs): ruff, bandit (`-x tests -s B608`), mypy local hook, yamllint, shellcheck. Never `--no-verify` (MEMORY: feedback_no_verify).
- **PR per phase on a worktree branch** — never direct to main (CLAUDE.md Workflow; MEMORY: feedback_pr_per_phase).
- **`pyproject.toml` untouched** likely (zero new deps) — if a dev-only test dep is somehow needed, keep deps alphabetized + section order.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `reenqueue.py:374` held-file re-drive is out of scope and acceptable as a single-active-compute reader for this milestone | Open Questions / Pitfall 3 | If in scope, an N-compute deploy mis-routes recovered held-file analysis; mitigated by documenting as a known limitation |
| A2 | The target live deploy runs ≤1 compute agent initially (Kueue is the N target), so the single-active-compute recovery path is low-impact | Pitfall 3 | Higher impact if operators run N compute agents day-one; still non-blocking (recovery-only edge) |
| A3 | `dest_ssh_user` defaulting to the fileserver's `cfg.push_ssh_user` preserves ≤1-compute behavior byte-identical | Open Questions / Code Examples | If a backend needs a distinct user and none is stamped, the push auths as the wrong user — caught by the behavior-preservation golden + integration tests |

**Note:** All code-seam line numbers, the reader audit, and the "machinery already exists" claims are `[VERIFIED: codebase grep + Read]` against the live Phase-73 branch — not assumptions.

## Sources

### Primary (HIGH confidence — verified against live code)
- `src/phaze/services/backends.py` — `Backend` protocol, `ComputeAgentBackend` (L235-320), `KueueBackend` template (L323-463), `resolve_backends` (L466-489), `resolved_non_local_kind` (L492-516)
- `src/phaze/routers/agent_push.py` — `/pushed` (L65-150), `/mismatch` (L153-261) — the D-06/D-07 seams
- `src/phaze/tasks/push.py` — `_build_rsync_argv` (L80-109, the `cfg.*` read at L99), `_require_push_config` (L112-136)
- `src/phaze/schemas/agent_tasks.py` — `PushFilePayload` (L54-84) + its validators
- `src/phaze/config_backends.py` — `ComputeBackend` submodel + `_require_dispatch_fields` (L79-104)
- `src/phaze/config.py` — `active_compute_scratch_dir` property (L483-501); agent-side `push_ssh_host`/`push_ssh_user`/`cloud_scratch_dir` fields (L833-847)
- `src/phaze/models/cloud_job.py` — `unique(file_id)` (L72), `backend_id` (L98), `staging_bucket` (L104)
- `src/phaze/services/s3_staging.py` — `resolve_bucket_config` (L91-105), the inverse-lookup template
- `src/phaze/tasks/release_awaiting_cloud.py` — drain snapshot + per-backend `try/except` isolation (L137-260)
- `src/phaze/tasks/reenqueue.py` — `recover_orphaned_work` compute re-drive (L374)
- `src/phaze/services/enqueue_router.py` — `select_agent_by_id` (L131), `select_active_agent` (L96)
- `src/phaze/models/agent.py` — `Agent.id` PK slug (L25), `kind` CHECK (L40)
- Grep audits: `active_compute_scratch_dir` (1 runtime reader), `select_active_agent(kind="compute")` (agent_push.py:93 + reenqueue.py:374), `cloud_scratch_dir` dual reader (push.py:99 + agent_worker.py:103)
- `.planning/phases/73-.../73-CONTEXT.md` (D-01..D-08, canonical refs, code_context); `.planning/phases/72-.../72-CONTEXT.md` (groundwork)

### Secondary
- `CLAUDE.md` — Python 3.14 / uv / ruff-mypy strict / 90% coverage / PR-per-phase constraints

### Tertiary
- None — every claim verified against live code or CONTEXT; no unverified web sources needed (zero new deps).

## Metadata

**Confidence breakdown:**
- Seam verification (line numbers): **HIGH** — every cited seam Read directly; drift is minimal (see line-number notes: `/mismatch` extends to L261, `reconcile` to L463; all others within ±2 lines of CONTEXT).
- Phase-70 template mapping: **HIGH** — `KueueBackend` + `resolve_bucket_config` read in full; the compute twin is a direct structural mirror.
- Landmines (mismatch re-drive, dual cloud_scratch_dir, reenqueue reader, process_file queue): **HIGH** — each traced to an exact line with a grep-confirmed reader set.
- Validation architecture: **HIGH** — test framework + existing files confirmed; gaps enumerated against the four touched suites.

**Research date:** 2026-07-05
**Valid until:** 2026-08-04 (stable internal code; valid until the branch's next material refactor of `backends.py` / `agent_push.py`)
