# Feature Research

**Domain:** Tiered / priority-with-capacity job scheduling + pluggable multi-backend dispatch (phaze Multi-Cloud Backends milestone, 2026.7.1, phases 67+)
**Researched:** 2026-07-03
**Confidence:** HIGH (established scheduler patterns cross-checked against Kubernetes cluster-autoscaler priority expander, Karpenter spot→on-demand fallback, AWS SQS+Spot cost-tiered workers; phaze-specific integration read directly from `release_awaiting_cloud.py`, the locked design doc, and PROJECT.md)

> **Scope note for the requirements author.** This is a backend-generalization milestone: the single `cloud_target` selector (`local`/`a1`/`k8s`) becomes a declarative, cost-tiered `backends:` registry draining long files across local + 1+ Kueue + 1+ cloud-compute **simultaneously**. Static routing, **no provisioning**. The design spine (`docs/superpowers/specs/2026-06-29-multi-cloud-backends-design.md`) is locked — do not re-litigate the shape. "Table stakes / differentiator / anti-feature" below means *what a rank+cap tiered scheduler must do vs. what to deliberately skip (YAGNI)* — and each of the five deferred-to-plan-time questions gets an explicit **include/defer verdict** the author can turn into REQ-IDs.

## How These Systems Actually Work (grounding)

The design's "free-first, spill-to-paid, slow-local-last" scheduler is a well-trodden industry pattern. The three closest reference implementations, and what phaze inherits from each:

- **Kubernetes cluster-autoscaler `priority` expander** — operator assigns integer weights to node groups; the scaler tries the highest-priority (cheapest) group first and only moves to the next when the preferred one cannot satisfy the request. This is phaze's **rank ordering** verbatim (lower rank = tried first). Confirms: rank is operator-assigned, static, and needs no dollar model.
- **Karpenter spot→on-demand fallback** — Spot is an accepted capacity type; Karpenter prefers Spot when available and **automatically falls back to On-Demand when Spot is unavailable or infeasible**. This is phaze's **spillover** verbatim: a full/offline backend yields to the next eligible one, no operator intervention. Confirms: fallback is a first-class, *expected* behavior — not a bonus.
- **AWS SQS + EC2 Spot cost workers** — one work queue drained by heterogeneous, differently-priced worker pools. Confirms phaze's shape: one `AWAITING_CLOUD` backlog, N drain targets, per-target capacity.

The **key operator expectation** across all three: the scheduler is *work-conserving with a preference order* — it never leaves a file idle when any eligible backend has a free slot, and it always reaches for the cheapest/preferred capacity first. Everything below is scoped against that expectation.

**What operators do NOT expect** (and the design correctly excludes): provisioning/teardown, an automated dollar-cost model, live rebalancing of already-dispatched work, or preemption of a running job to move it to a cheaper backend. **Rank is a dispatch-time preference only** — once a file is dispatched it runs where it landed.

## Feature Landscape

### Table Stakes (Operators Expect These)

Features that make the tiered scheduler behave the way any "cheapest-first, spill-over" scheduler behaves. Missing these = the milestone's core promise (simultaneous multi-backend drain) is broken.

| Feature | Why Expected | Complexity | Notes / phaze integration |
|---------|--------------|------------|---------------------------|
| **Lowest-rank-first eligible dispatch** | The entire "cost-tiered" premise. Per file, pick the available backend with lowest `rank` whose `in_flight_count() < cap`. | MEDIUM | Design §4.3. Replaces the `if/elif` on `cloud_target` in `stage_cloud_window`. Sort registry by rank once/tick; iterate candidates. |
| **Spill to next rank when preferred is full** | Karpenter/cluster-autoscaler baseline. A full rank-10 backend must not block a file a rank-20 backend can take *this tick*. | MEDIUM | Falls out of "first eligible backend whose in_flight < cap" **only if** the loop evaluates eligibility per candidate file, not once per tick. Must not short-circuit the whole tick when the top backend is full. |
| **Offline mid-flight → return to `AWAITING_CLOUD` → re-dispatch to NEXT eligible backend** | Karpenter fallback semantics. A dead A1 agent or unreachable Kueue cluster must not strand its in-flight files. | MEDIUM | Design §4.5. Mechanics exist: `reconcile_cloud_jobs` + recovery ledger, both made `backend_id`-aware. Re-dispatch target chosen fresh by rank — **not** pinned to the failed backend (see black-hole guard, Q1). |
| **Per-backend in-flight cap (replaces global `cloud_max_in_flight`)** | Without per-backend caps, "cost-tiering" is meaningless — you can't cap paid at 2 while free runs 8. | MEDIUM | Design §4.4. `cloud_job.backend_id` column (additive migration); `in_flight_count()` becomes a `WHERE backend_id = ?` scoped count. Preserve the existing advisory-lock count+claim-in-one-transaction discipline (WR-04) per-backend or a cap is overshot. |
| **Uniform in-flight accounting across compute PUSHING/PUSHED and Kueue cloud_job rows** | A backend's cap must be honored regardless of *how* its in-flight work is tracked. Today compute uses FileState, Kueue uses `cloud_job` rows — two truths. | MEDIUM-HIGH | **Q3, verdict INCLUDE (sharpest correctness edge).** See dedicated section. |
| **`is_available()` gating per kind** | A file dispatched to an offline backend is a stall. compute = agent heart-beating; kueue = LocalQueue probe; local = always. | LOW-MEDIUM | Design §4.1/§4.2. Probes already exist (Phase 51 heartbeat, Phase 56 LocalQueue startup probe), generalized behind `Backend.is_available()`. |
| **Tie-breaking between equal-rank backends** | Two rank-10 backends (A1 + homelab Kueue) is the design's *own* example config. Undefined tie-break = starvation or thundering-herd onto one. | LOW | **Q1 (tie-break), verdict INCLUDE, keep dumb.** Recommend lowest-utilization-first, stable-`id` tiebreak. |
| **`cloud_target` → `backends` back-compat shim** | The one existing deploy (homelab) must keep working across upgrade with no config rewrite on day one. | LOW-MEDIUM | **Q5, verdict INCLUDE.** Design §4.1 already locks "ship the shim." |
| **N-lane admin surfacing (generalize the fixed 3 cards)** | v7.0 Phase 58 shows fixed local/A1/k8s cards; the moment there are 2 Kueue clusters the fixed-3 UI lies about where work goes. | MEDIUM | **Q4, verdict INCLUDE, read-only.** Rides the existing `/pipeline/stats` 5s poll. |
| **Per-backend `_FILE` secrets + master revert toggle** | Each backend needs credentials via the `_FILE` convention; one flip must return to all-local for incident response. | LOW-MEDIUM | Design §8 phase 5. The revert is the `backends`-era equivalent of today's `cloud_target=local` no-op gate at the top of `stage_cloud_window`. |

### Differentiators (Defensible, Not Required to Ship the Core)

Features that make the scheduler *smarter* than the minimum. Each is defensible; each is also where scope can quietly balloon.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| **Staleness guard on local (rank-99 hold-off)** | Prevents a momentary backlog blip from dumping long files onto slow local. | LOW (cheap form) / MEDIUM (stateful) | **Q2, verdict DEFER (thin-include only if trivial).** See dedicated section. |
| **Per-lane live in-flight/cap gauge + rank badge in the UI** | Turns the N-lane surface from "is it up" into "is it saturated / is spillover happening." | LOW | Extends the table-stakes N-lane surface. The `in_flight_count`/`cap`/`rank` are already computed for the scheduler — surfacing them is presentation-only on the existing poll. Recommend **include** as part of the N-lane work. |
| **Per-backend reconcile cron cadence (compute vs kueue)** | Kueue Jobs and rsync-push compute have different lifecycle latencies; one `*/5` cadence may fit neither. | LOW | Design §7 defers this. Default: keep the single `*/5 reconcile_cloud_jobs`; split only on a concrete latency problem. **Defer.** |

### Anti-Features (Seem Reasonable, Would Blow Scope or Correctness)

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| **Automated dollar-cost model / spend tracking** | "Cost-tiered" sounds like it should know prices | Design §6 excludes it. Real cost = provider billing APIs + per-second math — a whole subsystem, and *wrong* for a free-but-slow-local tier (proves rank ≠ dollars) | Operator-assigned integer `rank` + `cap`. Already locked. |
| **Instance provisioning / teardown / autoscaling backends** | "Multi-cloud" implies spinning up VMs | Design §3 locked "static routing, no provisioning." Adds cloud SDKs, lifecycle state machines, teardown races — an order of magnitude more surface | Operator deploys backends; phaze routes to whatever is online |
| **Preempt / migrate a running job to a cheaper backend that just freed up** | "Optimal cost = never run on the expensive tier longer than needed" | Analysis jobs are long (that's *why* they're offloaded); results are out-of-band by `file_id`; migrating mid-flight kills real compute + risks double-dispatch. Rank is dispatch-time only | Rank decides where a file *starts*; let it finish. Spillover handles the *next* file. |
| **External / third-party backend plugin loading** | "Pluggable" sounds like entry-points / dynamic import | Design §6 — the `Backend` protocol is *internal*. Dynamic loading = arbitrary-code-execution surface + versioning hell for a single-user tool | Internal `Protocol`, three in-repo impls. New providers are trivial in-repo follow-ons (compute path already has zero OCI-specific code) |
| **Per-cluster S3 buckets** | Each Kueue cluster "should own" its staging | Design §7/§6 lock one shared bucket; control plane stays sole S3 importer (DIST-01). Per-cluster buckets multiply credential/cleanup surface with zero benefit — bytes are identical | One shared bucket; per-cluster kube config only |
| **Weighted / fractional / proportional fair-share across equal-rank backends** | "Fair" load balancing across two rank-10 backends | Real weighted fair-share needs per-backend throughput accounting + scheduler state; for a single-user finite backlog the win is nil | Dumb tie-break (lowest current utilization, stable-`id`). See Q1 |
| **New concrete providers (AWS/GCP compute) this milestone** | The seam is *for* multi-cloud | Design §3/§6: no new providers. The seam makes them trivial later; building one now conflates "prove the abstraction" with "prove a provider" | Ship the `Backend` seam + multi-Kueue. Providers are a follow-on |

## The Five Deferred-to-Plan-Time Questions — Explicit Verdicts

### Q1. Rank + cap tiered drain semantics (spill, offline re-dispatch, tie-break) — **INCLUDE (all three, table-stakes)**

This *is* the milestone. All three sub-behaviors are the industry-standard shape (cluster-autoscaler priority + Karpenter fallback), and operators expect all three:

- **Spill when full:** per file, walk backends in rank order, dispatch to the first whose `in_flight_count() < cap`. The loop must evaluate eligibility **per candidate file**, not decide one backend for the whole tick — else a full top rank blocks the tick. Complexity MEDIUM.
- **Offline mid-flight → return to `AWAITING_CLOUD` → next eligible (not the failed one):** re-dispatch chooses fresh by rank each tick. **Critical invariant (black-hole guard):** the re-dispatch target must be selected against *current* availability so a persistently-down backend is skipped rather than repeatedly reclaiming and re-failing its own files. Existing `reconcile_cloud_jobs` + recovery ledger provide the mechanics; both become `backend_id`-aware. Complexity MEDIUM.
- **Tie-break between equal-rank backends:** the design's own example has two rank-10 backends, so this is not hypothetical. Recommend **deterministic, stateless, dumb**: among eligible equal-rank backends pick the one with lowest current utilization (`in_flight_count / cap`), tie-broken by stable `id` sort. Naturally spreads load, needs no cross-tick state. Explicitly reject weighted fair-share (anti-feature). Complexity LOW.

**Dependencies:** per-backend `in_flight_count()` (Q3), `backend_id` column, `Backend.is_available()`, and the existing `pg_advisory_xact_lock` + FIFO `SELECT ... FOR UPDATE SKIP LOCKED` drain in `release_awaiting_cloud.py` (the tiered loop replaces the `if/elif`, not the locking).

**Requirements guidance:** one REQ for rank-ordered eligible dispatch; one for spillover/offline re-dispatch (call out the black-hole guard as an invariant); one for the tie-break rule (keep it its own REQ so its "dumb on purpose" scope is explicit).

### Q2. Staleness guard on local — **DEFER (differentiator; thin-include only if trivial)**

**Verdict: not table-stakes.** The design's own default (§4.3) is "keep it simple — rank 99 + cap 1, no staleness logic." The structural protection is already strong: local is rank 99 with a small cap, reached only when *every* higher-ranked backend is simultaneously full-or-offline. In steady state with any cloud/Kueue backend online and un-saturated, local is never selected. The failure mode the guard addresses (a *momentary* blip where all cloud backends are briefly full and a file leaks to slow local) is:
- rare (requires all higher ranks saturated at the exact tick a slot is evaluated),
- self-limited (cap 1 on local → at most one file leaks per blip),
- low-consequence (the file is still analyzed, just slowly).

**Recommendation:** DEFER as a hard requirement. If plan-time finds the *cheap* form is genuinely a few lines — an age predicate on the AWAITING_CLOUD → local transition (`AND now() - created_at > local_staleness_threshold` in the local-candidate select) — it's a reasonable thin **differentiator** include, gated behind a config knob defaulting to 0 (off = today's behavior). Avoid the expensive stateful form (tracking per-file "time first became local-eligible") — YAGNI for a single-user tool. Complexity: LOW (cheap) / MEDIUM (stateful — reject).

**Requirements guidance:** if included, one optional off-by-default REQ, explicitly the cheap age-threshold form. Otherwise omit and note in the roadmap "considered, deferred — rank-99+cap-1 is sufficient structural protection."

### Q3. Per-backend in-flight accounting across compute PUSHING/PUSHED vs Kueue cloud_job — **INCLUDE (table-stakes; sharpest correctness edge)**

The load-bearing correctness feature. Today two mechanisms track "in flight" differently:
- **compute (rsync push):** counted from committed **FileState** — `get_cloud_window_count` counts `state IN {PUSHING, PUSHED}` (`release_awaiting_cloud.py` docstring: "D-08, NOT the SAQ ledger").
- **Kueue:** tracked via **`cloud_job` rows**.

For a per-backend `cap` to be honored, `in_flight_count(backend)` must return the same truth regardless of kind. The design's answer (§4.4) is right: **generalize `cloud_job` to record compute-agent pushes too, with a `backend_id` column**, so `in_flight_count()` is a uniform `SELECT count(*) FROM cloud_job WHERE backend_id = ? AND <non-terminal states>` for every kind. The alternative (each impl counts its own way — FileState for compute, rows for Kueue) works but leaves two sources of truth that must agree; the single `cloud_job` count is cleaner and makes spillover/recovery uniform (design §4.4 explicitly wants this).

**Critical edges the requirements must name:**
- The count+claim must stay in **one transaction under the advisory lock**, now scoped per-backend, or overlapping ticks overshoot a cap (exactly the WR-04 hazard already documented in the code, generalized to N backends).
- The compute `PUSHING → PUSHED → (analyzing) → terminal` progression must map cleanly onto `cloud_job` non-terminal states so a file isn't double-counted or dropped from the window during handoff.
- Migration is **additive** (`backend_id` column, backfill existing rows to the single current backend). Behavior-preserving per the design's phase-2 framing.

**Complexity:** MEDIUM-HIGH (data-model change + reconciling two mechanisms to one count). **Dependencies:** `cloud_job` model, migration, the advisory-lock drain. Most likely feature to need its own deeper phase-level design note — **flag it**.

**Requirements guidance:** one REQ for the `backend_id` column + uniform `in_flight_count()`; one REQ (or explicit invariant) for the per-backend cap-honoring-under-overlapping-ticks guarantee.

### Q4. N-lane admin surfacing — **INCLUDE (table-stakes), read-only, ride the existing poll**

The moment the registry has 2+ Kueue clusters or 2+ compute backends, v7.0 Phase 58's fixed 3 cards misrepresent reality. Generalize to **N cards driven by the registry**. Per-lane live signals operators need, in priority order:

1. **Available / offline** — from `is_available()` (agent heartbeat / LocalQueue probe / always-local). Without it the operator can't tell "why isn't spillover happening." Table-stakes.
2. **In-flight / cap** — the saturation gauge; tells whether this lane is the bottleneck and whether spillover is triggered. Table-stakes.
3. **Rank** — a badge; explains *why* work lands where it does. Cheap, high-signal.
4. (nice) **kind** (local/compute/kueue) and, for Kueue lanes, the existing **quota-wait-vs-Inadmissible** distinction Phase 58 already surfaces — preserve it per lane.

**Scope discipline:** read-only, presentation-only, **rides the existing `/pipeline/stats` 5s poll fanout** (the Phase 58 pattern — no second poll loop). No per-lane controls, no config editing in the UI (registry is config-file/`_FILE`-secret sourced). The scheduler-internal numbers are already computed for the drain loop, so surfacing them is nearly free.

**Complexity:** MEDIUM (template generalization fixed-3 → N + wiring registry-derived per-lane stats into the existing poll). **Dependencies:** the scheduler (Q1/Q3) must expose per-backend state; v7.0 Phase 58 Analyze workspace + `/pipeline/stats`; the registry (design phase 1).

**Requirements guidance:** one REQ for N-lane rendering from the registry (available/offline + in-flight/cap + rank), explicitly presentation-only and on the existing poll. Coordinate with the Phase 58 lane-card component (design §7 notes this).

### Q5. `cloud_target` → `backends` back-compat shim — **INCLUDE (table-stakes for the live deploy); define deprecation explicitly**

Design §4.1 already locks "ship the shim." Expected migration behavior:

- **Synthesis:** when `cloud_target` is set (`a1`/`k8s`) and no `backends:` list is present, synthesize a one-entry `backends` list at config-load so every downstream consumer (scheduler, registry, UI) sees the unified model. `cloud_target=local` → empty/local-only registry (the existing all-local no-op). Keeps the homelab's single-target deploy working with **zero config rewrite on upgrade day**.
- **Precedence:** if both are present, `backends:` wins (explicit new model over legacy); warn on the ignored legacy key.
- **Deprecation path (define now, execute later):** ship the shim → emit a deprecation warning when only `cloud_target` is set → document the `backends:` migration in the runbook → remove `cloud_target` in a *later* milestone. Do **not** remove `cloud_target` this milestone (it would break the only deploy that exists). Single operator ⇒ the shim can be short-lived, but must survive the upgrade transition.

**Complexity:** LOW-MEDIUM (config-load synthesis + precedence/validator rule + deprecation warning). **Dependencies:** the `backends:` registry + validators (design phase 1); the shim ships *inside* phase 1.

**Requirements guidance:** one REQ for the shim (synthesis + precedence + deprecation warning), explicitly stating `cloud_target` is *not removed* this milestone and that `cloud_target=local` maps to the all-local no-op.

## Feature Dependencies

```
Backend config registry (backends: list, validators, cloud_target shim)   [design phase 1]
    └──requires──> nothing (pure config; back-compat shim Q5 = table-stakes)

Backend protocol + 3 impls (is_available/in_flight_count/dispatch/reconcile)   [design phase 2]
    └──requires──> registry
    └──requires──> cloud_job.backend_id column (Q3, additive migration)

Tiered drain scheduler (rank-first, spill, offline re-dispatch, tie-break)   [design phase 3]
    └──requires──> Backend protocol (is_available + in_flight_count)
    └──requires──> per-backend in-flight accounting (Q3)   [HARD dependency — cap needs one count]
    └──requires──> existing pg_advisory_xact_lock drain (release_awaiting_cloud.py)

Multi-Kueue (N clusters, shared S3)   [design phase 4]
    └──requires──> Backend protocol (KueueBackend) + registry (N kueue entries)

N-lane admin surface (Q4)   [design phase 5]
    └──requires──> scheduler exposes per-backend {available, in_flight, cap, rank}
    └──enhances──> v7.0 Phase 58 Analyze lane cards + /pipeline/stats poll

Staleness guard on local (Q2, OPTIONAL)
    └──enhances──> tiered drain scheduler (age predicate on the local-candidate select)

Master revert toggle
    └──enhances──> registry (empty/local-only registry == today's cloud_target=local no-op)
```

### Dependency Notes

- **Scheduler (Q1) hard-requires per-backend accounting (Q3):** a per-backend `cap` is unenforceable without a per-backend `in_flight_count()`. Q3's `backend_id` column + uniform count is a prerequisite, not a parallel track. Matches the design's dependency-strict order (phases 1→2 behavior-preserving refactors de-risk 3).
- **N-lane UI (Q4) enhances but does not block the scheduler:** it works headless; the UI reads its state. But the state (`available/in_flight/cap/rank`) is *already computed*, so the UI is cheap and should ship this milestone (the fixed-3 cards actively mislead once N>3).
- **Staleness guard (Q2) enhances the scheduler, conflicts with nothing** — an optional predicate. Its absence is fully covered by rank-99+cap-1. Keep it separable so it can be dropped without touching the core loop.
- **Back-compat shim (Q5) lives inside phase 1 (registry)** — a config-load concern, not a scheduler concern; lands with the registry and de-risks the whole upgrade.

## MVP Definition

("MVP" = the must-land core of this milestone. Design phases are numbered per the doc's §8.)

### Launch With (the milestone core)

- [ ] **Backend config registry + validators + `cloud_target` back-compat shim (Q5)** — single source of truth; upgrade-safe. *Design phase 1.*
- [ ] **`Backend` protocol + Local/Compute/Kueue impls + `cloud_job.backend_id`** — removes the `if/elif`; behavior-preserving. *Design phase 2. Includes Q3 data model.*
- [ ] **Uniform per-backend `in_flight_count()` (Q3)** — compute PUSHING/PUSHED and Kueue rows reconcile to one count under the advisory lock. *Correctness cornerstone.*
- [ ] **Tiered drain scheduler (Q1)** — rank-first eligible dispatch, spill-when-full, offline→next-eligible re-dispatch (black-hole guard), dumb equal-rank tie-break. *Design phase 3.*
- [ ] **Multi-Kueue (N clusters, shared S3, per-cluster probe/reconcile)** — proves multiplicity without a new provider. *Design phase 4.*
- [ ] **N-lane admin surface (Q4)** — registry-driven cards: available/offline + in-flight/cap + rank, on the existing poll. *Design phase 5.*
- [ ] **Per-backend `_FILE` secrets, runbook, master revert toggle** — operator close-out. *Design phase 5.*

### Add After Validation (thin, optional)

- [ ] **Staleness guard on local (Q2)** — only the cheap age-threshold form, off by default; add only if the blip-to-local leak is observed.
- [ ] **Per-lane utilization gauge polish** — richer saturation/spillover visualization once N-lane cards exist and the operator wants more.

### Future Consideration (explicit non-goals — do NOT scope here)

- [ ] **New concrete providers (AWS/GCP compute agents)** — the seam makes them trivial follow-ons; building one now conflates seam-proving with provider-proving. *Design §6.*
- [ ] **Per-backend reconcile cron cadence split (compute vs kueue)** — keep the single `*/5` until a concrete latency problem appears. *Design §7.*
- [ ] **Automated dollar-cost model, provisioning, preemption/migration, external plugins, per-cluster buckets** — permanent non-goals. *Design §6.*

## Feature Prioritization Matrix

| Feature | Operator Value | Implementation Cost | Priority |
|---------|----------------|---------------------|----------|
| Backend registry + validators + `cloud_target` shim (Q5) | HIGH (upgrade-safe) | LOW-MEDIUM | P1 |
| `Backend` protocol + 3 impls + `backend_id` column | HIGH (removes fork; enables all) | MEDIUM | P1 |
| Uniform per-backend `in_flight_count()` (Q3) | HIGH (cap correctness) | MEDIUM-HIGH | P1 |
| Tiered drain: rank-first + spill + offline re-dispatch (Q1) | HIGH (the milestone) | MEDIUM | P1 |
| Equal-rank tie-break (Q1) | MEDIUM (2×rank-10 is the design's own example) | LOW | P1 |
| Multi-Kueue (N clusters, shared S3) | HIGH (proves multiplicity) | MEDIUM | P1 |
| N-lane admin surface (Q4) | MEDIUM-HIGH (fixed-3 lies once N>3) | MEDIUM | P1 |
| Per-backend `_FILE` secrets + master revert toggle | MEDIUM (incident safety) | LOW-MEDIUM | P1 |
| Staleness guard on local (Q2) | LOW (rare, self-limited, low-consequence) | LOW (cheap form) | P3 |
| Per-lane utilization polish | LOW | LOW | P3 |
| Per-backend reconcile cadence split | LOW | LOW | P3 |
| Dollar-cost / provisioning / preemption / plugins / per-cluster buckets | NEGATIVE | HIGH | never (anti-feature) |

**Priority key:** P1 = must have to ship the milestone · P2 = should have · P3 = nice-to-have / defer.

## Reference Pattern Analysis (how the industry does the same thing)

| Behavior | k8s cluster-autoscaler (`priority` expander) | Karpenter (spot fallback) | phaze approach |
|----------|----------------------------------------------|---------------------------|----------------|
| Preference order | Integer weight per node group; highest tried first | Capacity-type preference (Spot > On-Demand) | Operator `rank`, lowest tried first (rank 99 local = last) |
| Capacity ceiling | ASG max size per group | Instance availability | Per-backend `cap` |
| Spillover | Falls to next-priority group when preferred can't satisfy | **Auto-fallback to On-Demand when Spot unavailable/infeasible** | Next eligible backend by rank when preferred full/offline |
| Cost model | None (operator sets weights) | Price-capacity-optimized (has price data) | **None — operator-assigned ranks** (matches cluster-autoscaler, not Karpenter — correct for free-but-slow-local) |
| Provisioning | Yes (scales node groups) | Yes (launches nodes) | **No — static routing, backends pre-deployed** (deliberate scope cut) |
| Migrate running work | No | No (drains on interruption only) | **No — rank is dispatch-time only** |

**Takeaway for requirements:** phaze deliberately takes cluster-autoscaler's *operator-set-rank* model (no price data) plus Karpenter's *automatic fallback* behavior, and drops both systems' provisioning. That combination is coherent and well-precedented — the requirements author can lean on "this is the priority-expander + spot-fallback pattern, minus provisioning" as the mental model.

## Dependencies on Existing phaze Features (do NOT re-scope)

| Existing feature (shipped) | How this milestone touches it |
|----------------------------|-------------------------------|
| Duration router `_route_discovered_by_duration` holds long files in `AWAITING_CLOUD` | **Untouched** (design §5). Still the sole cloud-eligibility gate. |
| `stage_cloud_window` drain loop + advisory-lock window (`release_awaiting_cloud.py`) | **Generalized** — the `if/elif` on `cloud_target` becomes the rank+cap tiered loop; the advisory-lock/FIFO/window discipline (WR-04) is preserved and scoped per-backend. |
| Global `cloud_max_in_flight` counter (default 2) | **Replaced** by per-backend `cap`. |
| `cloud_job` per-file_id registry (Kueue-only today) | **Extended** with `backend_id`; generalized to also record compute pushes (Q3). |
| `reconcile_cloud_jobs` cron + recovery ledger | **Made `backend_id`-aware** to drive spillover/offline re-dispatch (§4.5). |
| `put_analysis` result callback keyed by `file_id` | **Untouched** (design §5) — already backend-agnostic; all backends reconcile through it. |
| Agent HTTP surface `/api/internal/agent/*`, shared S3 staging leg, windowed analysis | **Untouched** (design §5). |
| v7.0 Phase 58 Analyze lane cards + `/pipeline/stats` 5s poll | **Generalized fixed-3 → N** (Q4); reuses the same single-poll fanout. |
| Phase 51 compute-agent heartbeat, Phase 56 LocalQueue startup probe | **Reused** as `Backend.is_available()` bodies. |
| `_FILE`-secret convention (v4.0.1) | **Reused** per-backend (design phase 5). |

## Sources

- [Scaling Safely on AWS Spot Using the Cluster Autoscaler's Priority Expander (ZipRecruiter Tech)](https://medium.com/ziprecruiter-tech/scaling-safely-on-aws-spot-using-the-cluster-autoscalers-priority-expander-part-2-fe5aa4998bd2) — MEDIUM; confirms rank/weight-ordered "try cheapest group first" is standard operator practice
- [Karpenter vs Cluster Autoscaler (cast.ai)](https://cast.ai/blog/karpenter-vs-cluster-autoscaler/) — MEDIUM; confirms automatic spot→on-demand fallback (= spillover) is a first-class expected behavior
- [Running Cost-effective queue workers with Amazon SQS and EC2 Spot (AWS)](https://aws.amazon.com/blogs/compute/running-cost-effective-queue-workers-with-amazon-sqs-and-amazon-ec2-spot-instances/) — MEDIUM; confirms one-queue / heterogeneous-priced-worker-pools shape
- [Design a Distributed Job Scheduler (System Design Handbook)](https://www.systemdesignhandbook.com/guides/design-a-distributed-job-scheduler/) — LOW; general spillover/threshold-redirect background
- phaze design doc `docs/superpowers/specs/2026-06-29-multi-cloud-backends-design.md` — HIGH; locked decisions §3, scheduler §4.3, spillover §4.5, non-goals §6, deferred §7
- phaze `src/phaze/tasks/release_awaiting_cloud.py` — HIGH; the exact drain loop, advisory-lock/window discipline (WR-04), FileState-based in-flight count being generalized
- phaze `.planning/PROJECT.md` — HIGH; milestone target features, v5.0/v6.0/v7.0 context, Out of Scope, Key Decisions

---
*Feature research for: tiered/capacity-aware multi-backend job dispatch (phaze Multi-Cloud Backends milestone, 2026.7.1)*
*Researched: 2026-07-03*
