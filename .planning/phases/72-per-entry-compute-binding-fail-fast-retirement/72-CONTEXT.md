# Phase 72: Per-Entry Compute Binding & Fail-Fast Retirement - Context

**Gathered:** 2026-07-05
**Status:** Ready for planning

<domain>
## Phase Boundary

Make `backends.toml` accept **N `compute` backends** — each bound at construction to a
specific registered compute `Agent` via its own per-entry reference — and **retire the two
`≤1-compute` fail-fasts** (`services/backends.resolved_non_local_kind`,
`config.active_compute_scratch_dir`), generalizing them for a `local + N-Kueue + N-compute`
registry.

This is **behavior-preserving groundwork** (MCOMP-01): existing **single-compute** and
**zero-compute (implicit all-local)** deploys must behave **identically** with no config edit
and no behavior change. It unblocks the Phase 73 dispatch core (per-agent liveness / push /
reconcile — MCOMP-02..06).

**In scope:** accept N compute entries at boot; resolve each compute entry to its bound Agent
through a recorded per-entry reference (not `select_active_agent(kind="compute")`'s
single-active pick); retire + generalize the two `≤1-compute` raises; prove the ≤1-compute and
all-local paths byte-identical.

**Out of scope (this phase):** per-agent dispatch / liveness / scratch destination / reconcile
attribution (Phase 73, MCOMP-02..06); N-lane UI + docs/runbook (Phase 74, MCOMP-07);
capability-aware routing (PROV-02); provisioning/autoscaling (PROV-03); any new routing
semantics beyond rank/cap; Kueue-side changes; the `2026.7.1` release PR/tag.

</domain>

<decisions>
## Implementation Decisions

### agent_ref → Agent binding key
- **D-01:** A compute backend's `agent_ref` (already present on the `ComputeBackend` submodel
  since Phase 67/REG-02, required at construction) resolves against **`Agent.id`** — the PK, a
  constrained slug (`^[a-z0-9]+(-[a-z0-9]+)*$`), already the FK target on `files` /
  `scan_batches` / `cloud_job.backend_id`. **Not** `Agent.name` (free-form, collidable, not the
  FK key) and **not** id-or-name fallback. Operator writes the agent's `id` in `backends.toml`.
- **D-02:** Each compute backend records its bound-agent reference **at construction**, mirroring
  the Phase-70 MKUE-01 pattern (`KueueBackend` threads `self.config.kube` per call, not a module
  global). Resolution reads `self.config.agent_ref` — it does **not** re-derive via
  `select_active_agent(kind="compute")`'s "the single active compute agent" assumption.

### Fail-fast retirement + replacement validation
- **D-03:** Remove the compute-only `>1` raise from **both** `resolved_non_local_kind`
  (`services/backends.py` ~L494) and `active_compute_scratch_dir` (`config.py` ~L483), and
  generalize them for a `local + N-Kueue + N-compute` registry.
- **D-04:** **Boot fail-fast (id-tagged) on duplicate `agent_ref`** across two compute backends —
  a static, deterministic config-validator check, mirroring the `KueueBackend._require_kube` /
  per-variant validator style (fail loud with the offending entry `id`).
- **D-05:** An `agent_ref` pointing to a **not-yet-registered / not-checked-in** agent is **NOT**
  a boot error — agents register dynamically via check-in, so a boot-time DB existence check is
  wrong. Runtime resolution **degrades to a hold** (absent agent → `is_available` False, never
  raises), preserving today's absent-agent behavior and the cron no-op discipline (T-68-05).

### Behavior-preserving proof
- **D-06:** Acceptance bar = **golden byte-identical characterization** of the ≤1-compute
  dispatch/resolution path (before vs after — the Phase-68 D-01 golden precedent) **PLUS** an
  explicit **zero-compute (all-local) regression** proving no cloud activity. Honors the
  project's behavior-preserving culture and the ≥90% coverage floor.

### 72/73 scope line (keep 72 pure groundwork)
- **D-07:** Phase 72 stays **pure groundwork**. It only retires the `>1` raise and keeps the
  ≤1-compute resolution of `active_compute_scratch_dir` and the `/pushed` callback
  (`routers/agent_push.py` ~L133) **byte-identical**. All **per-agent** scratch-dir / push
  destination / reconcile-attribution widening (MCOMP-03 / MCOMP-06) lands in **Phase 73**.
  `agent_push.py` and the reconcile callbacks are **untouched (or trivially touched)** in 72 —
  the cleanest behavior-preserving boundary.

### Claude's Discretion
- Exact placement of the duplicate-`agent_ref` validator (container-level `_validate_registry`
  vs a submodel-list validator) — follow the closest existing Phase-67/68 validator idiom.
- Whether the per-entry binding is stored as-is on `self.config.agent_ref` or lifted to a
  typed attribute at `resolve_backends` construction — pick the least-surface option that reads
  cleanly and mirrors `KueueBackend._kube()`.
- Whether `resolved_non_local_kind`'s compute-only branch still returns `"compute"` for N
  compute (it should — the generalization just drops the raise); confirm during planning.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Milestone scope (parity boundary — read first)
- `.planning/REQUIREMENTS.md` — 2026.7.2 Multi-Compute Agents; MCOMP-01 (this phase),
  MCOMP-02..07 (Phases 73–74), out-of-scope table, v2 PROV deferrals.
- `.planning/ROADMAP.md` §"Phase 72: Per-Entry Compute Binding & Fail-Fast Retirement"
  (~L1012) — goal, 4 success criteria, research flag; §"Phase 73" (~L1026) for the boundary
  this phase must NOT cross.

### Code to change (fail-fasts + binding)
- `src/phaze/services/backends.py` §`resolved_non_local_kind` (~L469–499) — retire the
  compute-only `>1` raise; `ComputeAgentBackend.is_available`/`dispatch` (~L235–297) — the
  `select_active_agent(kind="compute")` single-active seam to replace with per-entry binding.
- `src/phaze/config.py` §`active_compute_scratch_dir` (~L468–489) — retire the `>1` raise;
  keep ≤1 resolution byte-identical.
- `src/phaze/config_backends.py` §`ComputeBackend` (L79–104) — the existing `agent_ref` /
  `scratch_dir` submodel + `_require_dispatch_fields` validator; add the duplicate-`agent_ref`
  boot guard near here / in the container validator.
- `src/phaze/services/enqueue_router.py` §`select_active_agent` (L96–128) — the kind-scoped
  most-recently-seen selector the compute path currently leans on; per-entry binding must
  target a specific `Agent.id` instead.
- `src/phaze/models/agent.py` (L20–41) — `Agent.id` (PK slug, FK target) vs `Agent.name`;
  `kind IN ('fileserver','compute')` CHECK.

### Boundary-only (must NOT change in 72 — Phase 73 territory)
- `src/phaze/routers/agent_push.py` (~L77, L133) — the `/pushed` callback reads
  `active_compute_scratch_dir`; keep ≤1 behavior byte-identical, defer per-agent widening.

### Precedent to mirror (Phase 70 multi-Kueue — the direct twin)
- Phase 70 MKUE-01 pattern: per-backend config bound at construction, threaded per-call, no
  module global (see `KueueBackend._kube()` / `dispatch` in `services/backends.py` L300–354,
  and the RETIRED `active_kube` note in `config.py` L491–493).

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`agent_ref` field already exists** on `ComputeBackend` (config_backends.py:88) and is
  required at construction — Phase 72 only needs to **wire it to resolution**, not add the field.
- **`cloud_job.backend_id`** already scopes per-backend in-flight counts
  (`_BaseBackend.in_flight_count`, the D-02 substrate) — no schema change needed for MCOMP-01.
- **`KueueBackend._kube()`** (backends.py:313–324) is the exact template for a per-entry
  binding accessor (fail-loud if the bound config is missing; read `self.config`).
- **id-tagged fail-fast idiom** — `ComputeBackend._require_dispatch_fields` /
  `KueueBackend._require_kube` show the message style for the new duplicate-`agent_ref` guard.

### Established Patterns
- **Record-don't-rederive** (MKUE-01): the bound reference is set once at `resolve_backends`
  construction and read per-call; never re-derived from a global selector.
- **Degrade-safe absent-agent → hold** (T-68-05): `is_available` catches `NoActiveAgentError`
  → returns False, never raises; the drain no-ops. D-05 preserves this for unregistered agents.
- **Generalize-not-descope for fail-fasts** (WR-01, Phase 70): `resolved_non_local_kind`
  already tolerates N Kueue; the compute-only `>1` raise is the last `≤1` reduction to retire.

### Integration Points
- `resolve_backends` (backends.py:443–466) constructs one `ComputeAgentBackend` per entry —
  the natural place to record the per-entry agent binding.
- The tiered drain (`release_awaiting_cloud.stage_cloud_window`) iterates the resolved list and
  snapshots each backend's `is_available` / `in_flight_count` — already N-backend-shaped
  (Phase 69); Phase 72 just makes the compute lane's binding per-entry.

</code_context>

<specifics>
## Specific Ideas

- Mirror **Phase 70 (MKUE-01)** verbatim where it maps: distinct per-backend binding recorded
  at construction, read per-call. This phase is the deliberate compute-side twin.
- The two `≤1-compute` raises are currently **lazy** (fire at first invocation, not at
  `_validate_registry`) — a deliberate PROV-01-backlog descope noted in STATE.md deferred items.
  Phase 72 closes that item.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope. (Per-agent dispatch/liveness/scratch/reconcile →
Phase 73; N-lane UI + runbook → Phase 74; capability routing → PROV-02; provisioning → PROV-03,
all already tracked in REQUIREMENTS.md.)

</deferred>

---

*Phase: 72-per-entry-compute-binding-fail-fast-retirement*
*Context gathered: 2026-07-05*
