# Phase 73: Per-Agent Dispatch, Liveness, Scratch & Failure Isolation - Context

**Gathered:** 2026-07-05
**Status:** Ready for planning

<domain>
## Phase Boundary

Make **N cloud-compute agents** dispatch / route / reconcile / fail-isolate **simultaneously** —
each long file **pushed to and attributed to the specific compute agent that analyzes it**,
cost-tiered across a mixed arm64/x86 fleet by `rank` and per-agent `cap`, with one flaky agent
isolated to 0 slots. This is **the behavior core** of 2026.7.2 — the direct compute-side twin of
Phase 70's multi-Kueue work (MCOMP-02..06).

**In scope:**
- Per-agent push/scratch destination: resolve the rsync target **per file** from the file's
  recorded `cloud_job.backend_id` (not a single global env), including the `/pushed` + `/mismatch`
  callbacks (MCOMP-03).
- Per-backend reconcile attribution scoped to `backend_id` — no cross-agent mis-attribution
  (MCOMP-06).
- Prove rank/cap load-spread across N compute agents + one-flaky-compute failure isolation reuse
  the existing Phase-69/70 machinery (MCOMP-04/05).
- Liveness (MCOMP-02) is **largely already delivered** by Phase 72's per-entry `is_available`
  (`select_agent_by_id`); Phase 73 only relies on the Phase-69 scheduler for the spill/hold path.

**Out of scope (this phase):**
- N-lane compute UI verification + operator runbook / cost-tiering docs → **Phase 74 (MCOMP-07)**.
- Capability-aware / arch-matched routing → **PROV-02** (v2). Provisioning / autoscaling →
  **PROV-03** (v2). Any new routing semantics beyond rank/cap. Kueue-side changes. The `2026.7.2`
  release PR/tag.

</domain>

<decisions>
## Implementation Decisions

### Per-agent push destination (MCOMP-03)
- **D-01: Destination source = `backends.toml` `ComputeBackend`.** Each compute entry gains the
  **push host** (and an optional `ssh_user`) alongside its existing `scratch_dir` / `agent_ref`
  (`config_backends.py` `ComputeBackend`, L79–104). `backends.toml` stays the single registry —
  the host is **not** taken from the Agent DB row (check-in data) or a fileserver-side map.
- **D-02: Control resolves + stamps the destination; record-don't-rederive.** The control plane
  resolves the destination from the file's recorded `cloud_job.backend_id` → that `ComputeBackend`
  entry, and **stamps `host` + `scratch_dir` (+ `ssh_user`) into `PushFilePayload`**. Mirrors Phase
  70 stamping `staging_bucket` on the dispatch upsert. `push.py` `_build_rsync_argv` reads the
  destination **from the payload**, not from its own `AgentSettings`.
- **D-03: SSH secret material stays agent-side.** `push_ssh_key` + `push_known_hosts` remain on the
  fileserver agent (never cross into control config or the payload). The fileserver's `known_hosts`
  pins **all N** compute host keys; one fileserver key is authorized on each compute host.
- **D-04: Retire the fileserver's single push destination env.** The agent-side `push_ssh_host` +
  `cloud_scratch_dir` (the *remote-target* mirror on the fileserver) are superseded by the
  payload-carried per-backend destination — no `≤1` fallback path (a lingering single-compute
  assumption is exactly what this phase retires). NOTE: the **compute agent's own** local
  `cloud_scratch_dir` (its receive/read + scratch-janitor dir, `agent_worker.py:103`) is unchanged —
  it is that agent's local dir and must equal the backend entry's `scratch_dir`.

### cloud_job cardinality (MCOMP-06 — the plan-phase research flag, RESOLVED)
- **D-05: Stay one-row-per-file, keyed by `backend_id`.** Keep `cloud_job.file_id` `unique=True`
  (`models/cloud_job.py:72`). `backend_id` records the **current** dispatch target; on spill,
  `dispatch` re-upserts the **same** row with a new `backend_id` (a file is only ever in-flight to
  one backend at a time). **No migration, no schema change** — mirrors Phase 70 MKUE verbatim.
  Attribution derives entirely from the recorded `backend_id`.

### /pushed + /mismatch reconcile attribution (MCOMP-06)
- **D-06: Resolve scratch + terminalization from the recorded `backend_id`.** `/pushed` and
  `/mismatch` (`routers/agent_push.py` ~L93, L133) replace `select_active_agent(kind="compute")` +
  the global `active_compute_scratch_dir` with resolution from the file's `cloud_job.backend_id` →
  that `ComputeBackend`'s `scratch_dir`. Terminalization stays keyed by `file_id` (the row is
  already backend-scoped).
- **D-07: Validate the reporter; reject on mismatch.** The callback resolves
  `cloud_job.backend_id → ComputeBackend.agent_ref` and **verifies the bearer-token agent matches**.
  On mismatch → reject (4xx) and **do not terminalize** — a stale/wrong/duplicate agent can never
  mis-attribute another agent's file (directly satisfies MCOMP-06 "no cross-agent
  mis-attribution"). We do **not** re-stamp `backend_id` from the reporting token (that would invert
  record-don't-rederive and let a late report overwrite the dispatch decision).

### Rank/cap load-spread + failure isolation (MCOMP-04/05)
- **D-08: Pure verbatim reuse — no new scheduler policy.** Rank-first eligible dispatch + per-agent
  `cap` + spill-when-full/offline (Phase-69 `select_backend`) and per-backend snapshot `try/except`
  isolation (Phase-70 MKUE-03) already iterate the resolved backend list and cover compute backends
  as-is. Free arm64 = **lower** `rank` (preferred), paid/trial x86 = higher `rank` — pure operator
  config, no capability-matching. Phase 73 adds **regression tests only** (N-compute spread +
  one-flaky-compute isolation-to-0-slots without failing the drain tick); cost-tiering guidance is
  Phase 74 docs.

### Claude's Discretion
- **`PushFilePayload` field shape** — whether the destination is three flat fields (`dest_host`,
  `dest_scratch_dir`, `dest_ssh_user`) or a nested destination submodel; keep the `extra="forbid"` +
  absolute-path / argv-injection field validators (`schemas/agent_tasks.py:54–79`) and add matching
  validation for the new host/scratch fields.
- **`ssh_user` placement** — travels with the destination in the payload when a backend specifies it;
  otherwise defaults to the fileserver agent's configured user. Pick the least-surface option.
- **Config field for the host** — whether `ComputeBackend` names it `push_host` / `host` / `ssh_host`;
  follow the closest Phase-67/68 config idiom and keep the id-tagged `_require_dispatch_fields`
  validator style for any newly-required field.
- **Whether `active_compute_scratch_dir` (config.py `@property`, L484) is deleted outright or kept
  only for a transitional test** — it is documented as transitional and D-04/D-06 remove its last
  readers (`agent_push.py`); confirm no other reader remains during planning, then delete.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Milestone scope (parity boundary — read first)
- `.planning/REQUIREMENTS.md` — 2026.7.2 Multi-Compute Agents; MCOMP-02..06 (this phase), the
  out-of-scope table, and the v2 PROV-02/03 deferrals (parity-only, zero new deps).
- `.planning/ROADMAP.md` §"Phase 73: Per-Agent Dispatch, Liveness, Scratch & Failure Isolation" —
  goal, 5 success criteria, and the `cloud_job` cardinality research flag (RESOLVED by D-05);
  §"Phase 72" and §"Phase 74" for the boundaries this phase must not cross.
- `.planning/phases/72-per-entry-compute-binding-fail-fast-retirement/72-CONTEXT.md` — the Phase-72
  groundwork this phase builds on (per-entry `agent_ref` binding, retired `≤1-compute` fail-fasts,
  duplicate-`agent_ref` boot guard, D-07 scope line deferring per-agent push/scratch/reconcile HERE).

### Code to change (the single-active seams this phase widens)
- `src/phaze/routers/agent_push.py` — `/pushed` (~L65–150) reads `select_active_agent(kind="compute")`
  (~L93) + `active_compute_scratch_dir` (~L133); `/mismatch` (~L153–220) mirrors it. The MCOMP-03/06
  seam (D-06/D-07).
- `src/phaze/tasks/push.py` — `_build_rsync_argv` (L80–110) builds `remote_dest` from `cfg.push_ssh_*`
  + `cfg.cloud_scratch_dir`; `_require_push_config` (L112+). Read the destination from the payload
  (D-02/D-04).
- `src/phaze/schemas/agent_tasks.py` — `PushFilePayload` (L54–79, `extra="forbid"` + validators); add
  the per-backend destination fields (D-02, discretion).
- `src/phaze/services/backends.py` — `ComputeAgentBackend.dispatch` (~L280–316) writes `cloud_job`
  (`backend_id` set) then `_enqueue_push_file` (L84–114); stamp the resolved destination into the
  payload here (D-02). `in_flight_count` (`_BaseBackend`, L165–178) is already `backend_id`-scoped.
- `src/phaze/config_backends.py` — `ComputeBackend` submodel (L79–104, existing `agent_ref` /
  `scratch_dir` + `_require_dispatch_fields`); add the push host field (D-01).
- `src/phaze/config.py` — `active_compute_scratch_dir` `@property` (L484–501, documented transitional)
  — remove once D-06 drops its last reader.

### Precedent to mirror (Phase 70 multi-Kueue — the direct twin)
- `src/phaze/services/backends.py` — `KueueBackend._kube()` (L336–348, per-entry binding bound at
  construction) + `dispatch` stamping `staging_bucket` on the upsert (L365–402) + `reconcile`
  scoped `WHERE backend_id == self.id` (L404–431). The record-don't-rederive + per-backend-scoping
  + snapshot-isolation templates to copy verbatim.
- `src/phaze/models/cloud_job.py` — `backend_id` (L98) + `staging_bucket` (L104) columns added by
  Phase 68/70 with `unique(file_id)` preserved (L72, L103) — the one-row-per-file precedent (D-05).

### Scheduler / drain (reuse as-is, add tests)
- `src/phaze/tasks/release_awaiting_cloud.py` — the tiered drain tick that snapshots each backend's
  `is_available` / `in_flight_count` (already N-backend-shaped, Phase 69) — the D-08 reuse surface.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`cloud_job.backend_id`** (models/cloud_job.py:98) already records the dispatch target and scopes
  `in_flight_count` — the substrate for D-05 attribution; no schema work needed.
- **`ComputeAgentBackend.is_available`** (backends.py:265–279) is **already per-agent** (Phase 72:
  `select_agent_by_id(session, self._agent_ref(), kind="compute")`) — MCOMP-02 liveness is largely
  delivered; this phase only wires the spill/hold to the Phase-69 scheduler.
- **`ComputeBackend.scratch_dir` / `agent_ref`** (config_backends.py:88) already exist on the submodel
  with an id-tagged `_require_dispatch_fields` validator — extend with the push host (D-01).
- **`KueueBackend`** dispatch/reconcile/isolation (backends.py:323–431) is the verbatim per-backend
  template for compute's per-agent scratch stamp + reconcile attribution + failure isolation.
- **`PushFilePayload`** (schemas/agent_tasks.py:54) with `extra="forbid"` + argv-injection/absolute
  -path validators is the safe extension point for the per-backend destination fields.

### Established Patterns
- **Record-don't-rederive (MKUE-01):** bind/stamp the per-file destination + `backend_id` at
  dispatch; every downstream reader (push argv, `/pushed`, reconcile) reads the recorded value — never
  re-derives via `select_active_agent`.
- **Degrade-safe absent-agent → hold (T-68-05, D-05 of Phase 72):** `is_available` catches
  `NoActiveAgentError` → False, never raises; the drain no-ops for an offline compute agent.
- **Per-backend snapshot try/except (Phase 70 MKUE-03):** one flaky backend degrades to 0 slots
  without failing the drain tick or blocking healthy backends (D-08 isolation).
- **id-tagged fail-fast idiom** (`ComputeBackend._require_dispatch_fields` /
  `KueueBackend._require_kube`) — the message style for any newly-required config field.

### Integration Points
- `ComputeAgentBackend.dispatch` (backends.py:280) — resolve destination from `self` (the backend
  already knows its own `scratch_dir`/host/`agent_ref`) and stamp it into `_enqueue_push_file`'s
  payload. The dispatch owner is the natural stamp site.
- `routers/agent_push.py` `/pushed` + `/mismatch` — the two out-of-band callbacks that terminalize
  and read scratch; both re-keyed off `cloud_job.backend_id` + reporter validation (D-06/D-07).
- `tasks/push.py` `_build_rsync_argv` — the single argv builder; swap `cfg.*` destination reads for
  `payload.*` (D-02/D-04), keeping the `--` argv terminator + `StrictHostKeyChecking` invariants.

</code_context>

<specifics>
## Specific Ideas

- **Reuse Phase 70 (multi-Kueue) verbatim wherever it maps** — this phase is the deliberate
  compute-side twin: distinct per-backend binding recorded at construction, per-backend probe,
  per-backend reconcile scoping, per-backend snapshot-isolation. Diverge only where the transport
  differs (rsync-push destination vs S3 bucket).
- **Zero new dependencies** — a pure application-code extension of the existing `Backend` protocol +
  push/rsync pipeline. No new pip deps, no Kueue-side changes.
- **Ships as its own PR on a worktree branch** — never direct to main.
- **Behavior preservation matters** — the single-compute deploy must continue to work identically
  once the payload carries the (single) destination; add a regression proving the ≤1-compute push
  path is unchanged in observable behavior.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope. (N-lane compute UI verification + operator runbook +
mixed arm64/x86 cost-tiering docs → Phase 74 / MCOMP-07; capability-aware routing → PROV-02;
compute-agent provisioning/autoscaling → PROV-03; all already tracked in REQUIREMENTS.md.)

</deferred>

---

*Phase: 73-per-agent-dispatch-liveness-scratch-failure-isolation*
*Context gathered: 2026-07-05*
