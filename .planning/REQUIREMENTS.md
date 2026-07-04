# Requirements: Phaze — Milestone 2026.7.1 Multi-Cloud Backends

**Defined:** 2026-07-03
**Core Value:** Get 200K messy music and concert files properly named, organized, deduplicated, with rich metadata — human-in-the-loop, nothing moves without review. Files stay on file-server agents; decisions stay on the application server.

**Milestone framing:** Generalize the single `cloud_target` selector (`local`/`a1`/`k8s`) into a declarative, cost-tiered `backends:` config registry that drains long, locally-timing-out audio files across **local + Kueue (1+ clusters) + cloud-compute (1+ providers) simultaneously**, ranked by operator-assigned `rank` and bounded by per-backend `cap`. Static routing, **no provisioning** — phaze routes to whatever backends the operator has deployed and are online. The "user" for these requirements is the operator/maintainer. The design is locked and merged (`docs/superpowers/specs/2026-06-29-multi-cloud-backends-design.md`, PR #182); it supersedes the v6.0 Phase 55 `cloud_target` selector. Result-return (`put_analysis` by `file_id`), duration gating (`_route_discovered_by_duration`), the agent HTTP surface, and windowed analysis all stay untouched. **One approved revision to the design doc (operator direction, 2026-07-03):** the design's "one shared S3 bucket / no per-cluster buckets" decision (§6/§7) is superseded — the S3 staging leg is generalized to a bucket registry so each Kueue cluster can stage to either a shared Internet-reachable ("public") bucket set or its own cluster-specific bucket set (see REG-05 / MKUE-02). **Zero new dependencies** — pure application-code refactor on the pinned pydantic / pydantic-settings / kr8s / aioboto3 / SAQ-Postgres stack.

## Milestone 2026.7.1 Requirements

Requirements for this milestone. Each maps to exactly one roadmap phase (67+).

### REG — Backend config registry & model

- [x] **REG-01**: Operator can declare a list of execution backends in config (`backends:`) — each entry carrying an `id`, a `kind` (`local` / `compute` / `kueue`), an integer `rank`, and an integer `cap` — as the single source of truth for which execution targets exist, replacing the 3-value `cloud_target` Literal.
- [x] **REG-02**: Each backend entry is validated per-kind at startup with fail-fast errors (kueue requires its kube config; compute requires its bound-agent reference; local needs neither), consolidating the three current per-target `_enforce_*_when_*` validators into one per-entry discriminated-union validator.
- [x] **REG-03**: Per-backend secrets (kube tokens/kubeconfigs, S3 credentials, agent tokens) load via the existing `<VAR>_FILE` convention, scoped per backend entry.
- [x] **REG-04**: `cloud_target` and the flat `s3_*` / `kube_*` / `compute_scratch_dir` `ControlSettings` fields (plus the three `_enforce_*_when_*` validators) are **removed** this phase — `backends.toml` is the sole config surface, with **no back-compat shim** (neither the `a1` nor `k8s` cloud path was ever deployed live; only `cloud_target=local` ever ran, and nothing in the wild depends on the flat fields). Absence of any `backends` config resolves to an implicit all-local registry (the zero-config no-op), a resolved-registry line is logged at startup (`id`/`kind`/`rank`/`cap` only), and a registry that resolves to empty fails fast rather than wedging the backlog. *(Operator decision 2026-07-03, CONTEXT D-11..D-14 — supersedes the earlier shim clause; the ~10 `settings.cloud_target` call sites are rewired to registry-derived reads this phase.)*
- [x] **REG-05**: Operator can declare an S3 staging-bucket registry — one or more buckets, each with its own endpoint + credentials (via `<VAR>_FILE` secrets) and a scope of either **shared/public** (Internet-reachable, usable by any Kueue cluster) or **cluster-specific** (bound to one Kueue backend) — and assign each Kueue backend the bucket set it stages to (its cluster-specific set, or the shared/public set). Validation is fail-fast (a Kueue backend must resolve to a non-empty, reachable-by-that-cluster bucket set; a `cluster-specific` bucket may be referenced by **at most one** Kueue backend, a `shared`/public bucket by many). The flat single global S3 config is **removed** with the other flat fields (REG-04, no shim). (Config model only — bucket selection/presigning/cleanup behavior is MKUE-02/04 in Phase 70.)

### BACK — Backend protocol & implementations

- [x] **BACK-01**: A single internal `Backend` protocol (`is_available` / `in_flight_count` / `dispatch` / `reconcile`) with `LocalBackend` / `ComputeAgentBackend` / `KueueBackend` implementations replaces the hardcoded `if/elif cloud_target` switch at every call site — the existing staging/push/submit bodies are re-homed as protocol-method bodies, not rewritten.
- [x] **BACK-02**: The `cloud_job` sidecar gains a `backend_id` column via an additive migration (with a backfill of existing rows to the current single backend) so in-flight counts and reconcile are per-backend.
- [x] **BACK-03**: Compute-agent pushes are recorded in the `cloud_job` registry (generalized from Kueue-only) so `in_flight_count()` returns one uniform count across all backend kinds instead of counting compute via `FileState{PUSHING,PUSHED}` and Kueue via rows separately.
- [x] **BACK-04**: The protocol refactor is proven behavior-preserving by a characterization test asserting single-backend dispatch decisions are byte-identical pre/post refactor — including the compute-requires-a-live-agent vs. Kueue-deliberately-skips-that-gate asymmetry.

### SCHED — Tiered drain scheduler

- [x] **SCHED-01**: Per drain tick, each `AWAITING_CLOUD` file is dispatched to the *available* backend with the lowest `rank` whose `in_flight_count() < cap`, with eligibility evaluated per candidate file so a full top-rank backend spills to the next rank rather than blocking the tick.
- [x] **SCHED-02**: The global `cloud_max_in_flight` window becomes a per-backend `cap`, enforced by counting and claiming a slot in one transaction under the existing `pg_advisory_xact_lock`, so overlapping ticks never overshoot a backend's cap.
- [x] **SCHED-03**: A backend going offline, or a job failing mid-flight, returns the file to `AWAITING_CLOUD`; the next tick re-dispatches it to the next eligible backend chosen against *current* availability — a black-hole guard ensures a persistently-down backend does not repeatedly reclaim and re-fail its own files.
- [x] **SCHED-04**: Two or more equal-`rank` backends are tie-broken deterministically and statelessly (lowest current utilization `in_flight/cap`, then stable `id`) — no weighted or proportional fair-share.
- [x] **SCHED-05**: Exactly one recovery owner exists per backend kind — `reconcile_cloud_jobs` and the recovery ledger become `backend_id`-aware, and the existing AST over-enqueue guard is extended so compute-backed cloud files do not gain a second recovery path (no replay of the 44.5k-job over-enqueue incident class).

### MKUE — Multi-Kueue (N clusters)

- [x] **MKUE-01**: Operator can declare N Kueue-cluster backends, each with its own kube config (per-cluster kubeconfig/context), dispatched to concurrently from the one control plane.
- [ ] **MKUE-02**: Each Kueue cluster stages long files to a bucket drawn from its REG-05-assigned set — either one or more Internet-reachable ("public") buckets shared across clusters, or one or more cluster-specific buckets — so a cluster that cannot reach the homelab bucket (e.g. a cloud cluster) uses a reachable one; when a set holds multiple buckets, staging selects one deterministically per file. The control plane remains the **sole** S3 importer/presigner for every bucket (DIST-01 no-media boundary preserved), and pods/agents stay credential-free, receiving only presigned, `file_id`-scoped, TTL-bounded URLs — objects are never world-readable despite an Internet-reachable endpoint.
- [ ] **MKUE-03**: Each cluster has its own LocalQueue reachability probe and a `backend_id`-scoped reconcile, and one cluster's probe/dispatch failure is isolated (per-backend try/except) so it cannot poison the whole drain tick.
- [ ] **MKUE-04**: Cross-cluster/cross-bucket staged-object cleanup is scoped to the (backend, bucket) that staged the object, so a spillover re-dispatch never deletes an object another cluster or bucket is still using; a per-bucket lifecycle TTL remains the backstop.

### BEUI — Deployment, config, docs & N-lane UI

- [ ] **BEUI-01**: The admin UI renders N per-backend lanes derived from the registry — each showing available/offline, in-flight/cap, and rank (preserving the Kueue quota-wait-vs-Inadmissible distinction per lane) — read-only and riding the existing `/pipeline/stats` 5s poll, generalizing v7.0 Phase 58's fixed 3 local/A1/k8s cards to N dynamic lanes.
- [ ] **BEUI-02**: A master toggle reverts all routing to local for incident response (the `backends`-era equivalent of today's `cloud_target=local` no-op gate).
- [ ] **BEUI-03**: The operator runbook and configuration docs cover the `backends:` schema, per-backend `_FILE` secrets, and the `cloud_target`→`backends` migration and deprecation path.

## Future Requirements

Deferred — tracked but not in this milestone's roadmap.

### New concrete cloud-compute providers

- **PROV-01**: First-class AWS / GCP compute-agent backends. The `Backend` seam makes them trivial in-repo follow-ons (the compute path already has zero OCI-specific code); building one now would conflate proving-the-abstraction with proving-a-provider.

### Scheduler refinements

- **SREF-01**: Per-backend reconcile cron cadence split (compute vs. kueue). Keep the single `*/5` `reconcile_cloud_jobs` cadence until a concrete latency problem appears.
- **SREF-02**: Staleness guard on local (age-threshold hold-off before a file leaks to slow rank-99 local). Considered and deferred — rank-99 + cap-1 is sufficient structural protection; add only the cheap off-by-default age-predicate form if a real blip-to-local leak is observed in operation.

## Out of Scope

Explicitly excluded to prevent scope creep. Anti-features locked out by the design (§6).

| Feature | Reason |
|---------|--------|
| Automated dollar-cost / spend-tracking model | Rank + cap are operator-assigned integers, not dollars; local is free-but-ranked-last (rank ≠ dollar cost). Real cost = provider billing APIs, a whole subsystem, and wrong for this model |
| Instance provisioning / teardown / autoscaling of backends | Static routing only — operators deploy backends; phaze routes to whatever is online. No cloud SDKs, no lifecycle state machines |
| Preemption / migration of a running job to a cheaper backend | Analysis jobs are long (that's why they're offloaded); rank is dispatch-time only — a file finishes where it landed |
| External / third-party backend plugin loading | The `Backend` protocol is internal only — no entry-points, no dynamic import (arbitrary-code-execution surface for a single-user tool) |
| Weighted / fractional / proportional fair-share across equal-rank backends | Real weighted fair-share needs per-backend throughput accounting + scheduler state; nil win for a single-user finite backlog. Dumb utilization+id tie-break instead |
| New concrete providers (AWS/GCP compute) this milestone | Deferred to PROV-01 — ship the seam + multi-Kueue; providers are a follow-on |

## Traceability

Which phases cover which requirements. Populated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| REG-01 | Phase 67 | Complete |
| REG-02 | Phase 67 | Complete |
| REG-03 | Phase 67 | Complete |
| REG-04 | Phase 67 | Complete |
| REG-05 | Phase 67 | Complete |
| BACK-01 | Phase 68 | Complete |
| BACK-02 | Phase 68 | Complete |
| BACK-03 | Phase 68 | Complete |
| BACK-04 | Phase 68 | Complete |
| SCHED-01 | Phase 69 | Complete |
| SCHED-02 | Phase 69 | Complete |
| SCHED-03 | Phase 69 | Complete |
| SCHED-04 | Phase 69 | Complete |
| SCHED-05 | Phase 69 | Complete |
| MKUE-01 | Phase 70 | Complete |
| MKUE-02 | Phase 70 | Pending |
| MKUE-03 | Phase 70 | Pending |
| MKUE-04 | Phase 70 | Pending |
| BEUI-01 | Phase 71 | Pending |
| BEUI-02 | Phase 71 | Pending |
| BEUI-03 | Phase 71 | Pending |

**Coverage:**
- Milestone requirements: 21 total
- Mapped to phases: 21 (REG→67 · BACK→68 · SCHED→69 · MKUE→70 · BEUI→71) — 1:1 category→phase, dependency-strict
- Unmapped: 0

---
*Requirements defined: 2026-07-03*
*Last updated: 2026-07-03 — traceability populated at roadmap creation: all 21 requirements mapped 1:1 to phases 67–71, 0 orphans, 0 duplicates (REG→67 · BACK→68 · SCHED→69 · MKUE→70 · BEUI→71). Earlier: added REG-05 (S3 bucket registry: public/shared vs cluster-specific) + revised MKUE-02/MKUE-04 per operator direction, superseding the design's one-shared-bucket decision (§6/§7)*
