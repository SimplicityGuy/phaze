# Project Research Summary

**Project:** phaze — 2026.7.1 Multi-Cloud Backends
**Domain:** Pluggable multi-backend, cost-tiered job dispatch (subsequent-milestone refactor of an existing async Python control plane)
**Researched:** 2026-07-03
**Confidence:** HIGH

## Executive Summary

This milestone generalizes phaze's single `cloud_target` selector (`local`/`a1`/`k8s`) into a declarative `backends:` registry that can drain long-running analysis files across local + N Kueue clusters + N cloud-compute agents **simultaneously**, ranked by an operator-assigned integer `rank` and bounded by a per-backend `cap`. The design is locked (`docs/superpowers/specs/2026-06-29-multi-cloud-backends-design.md`, PR #182); this research targeted only the design's deferred-to-plan-time open questions — it does not re-litigate architecture. All four research tracks (stack, features, architecture, pitfalls) independently converged on the same conclusion from different angles, which is the strongest signal in this research set: this is a pure application-code refactor with one genuinely hard correctness problem at its center.

No new dependencies are required. pydantic v2's discriminated unions, pydantic-settings' complex-field/`env_nested_delimiter` loading, kr8s's per-call arg-cached client (already N-cluster-capable), and aioboto3's single shared bucket already cover every capability the design calls for. The recommended approach is the design's own dependency-strict build order: land the registry and the `Backend` protocol as two behavior-preserving refactors first (phases 67-68, gated by a byte-identical characterization test), then flip on true multiplicity in the tiered scheduler (phase 69), then prove it scales to N Kueue clusters (phase 70), then close out with deploy/docs/UI (phase 71).

The central risk — surfaced independently by the features, architecture, and pitfalls research — is that phaze's two existing cloud targets track "in-flight work" through two incompatible substrates: compute/a1 counts via committed `FileState IN {PUSHING, PUSHED}` (with a `scheduling_ledger` recovery seed), while k8s counts via `cloud_job` rows (with no ledger seed, recovered only by `reconcile_cloud_jobs`). Fusing these into one uniform per-backend `in_flight_count()` — without double-counting, under-counting, racing with the unlocked reconcile cron, or reviving the Phase-30/44.5k-job over-enqueue incident class — is the milestone's sharpest correctness edge and the reason phases 67-68 exist as isolated, testable, behavior-preserving steps before phase 69 turns on multiplicity for real.

## Key Findings

### Recommended Stack

Every capability the design needs is already in the pinned stack (pydantic `>=2.13.4`, pydantic-settings `>=2.14.2`, kr8s `>=0.20.15`, aioboto3 `>=15.5.0`). This is confirmed as a zero-new-dependency, pure application-code milestone. All three load-bearing libraries are simultaneously the latest PyPI release, the pinned floor, and far older than the project's 7-day exclude-newer cooldown window — no version churn risk.

**Core technologies (all already present):**
- **pydantic v2** (discriminated unions) — a `list[BackendConfig]` with a `Literal["local","compute","kueue"]` discriminator gives fail-fast, per-entry, per-kind validation natively, replacing the three current cross-field `_enforce_*_when_*` validators with per-variant required fields.
- **pydantic-settings** — a `list[SubModel]` is a native "complex" settings field (single JSON env var, `env_nested_delimiter` numeric-index deep override, or a config-file source). Per-entry `_FILE` secrets are best served by extending the project's existing `_resolve_secret_files` before-validator via secret-name indirection (recommended) — not a new settings source.
- **kr8s** — `kr8s.asyncio.api(url=/kubeconfig=/context=)` is a per-call, arg-cached factory; distinct kubeconfig/context per backend entry yields distinct clients automatically. Natively N-cluster-capable; no `kubernetes`/`kubernetes_asyncio` or multi-cluster wrapper needed. One care point: the current post-construction token-mutation hack in `kube_staging.py` is unsafe across a shared cached client and should be retired in favor of a distinct `kubeconfig`/`context` per backend.
- **aioboto3** — unchanged; one shared S3 bucket across all Kueue clusters, control plane stays the sole importer (DIST-01 invariant preserved).

**What NOT to add:** `kubernetes`/`kubernetes_asyncio` (kr8s already covers it), `pluggy`/`stevedore`/entry-point plugin frameworks (the `Backend` protocol is explicitly internal-only, not third-party-pluggable), `pydantic-settings[yaml]`/PyYAML (prefer TOML/JSON-env to keep zero-dep), `secrets_dir`/`NestedSecretsSettingsSource` (would fork the project's established `<VAR>_FILE` convention), and any cloud-provider SDK or cost/pricing library (static routing, no provisioning, rank is not a dollar model — locked non-goals).

### Expected Features

The design's "free-first, spill-to-paid, slow-local-last" shape is a well-trodden industry pattern (Kubernetes cluster-autoscaler's `priority` expander for operator-set rank ordering; Karpenter's spot→on-demand fallback for automatic spillover; SQS+Spot for the one-queue/N-heterogeneous-worker shape). phaze's approach — operator-assigned integer rank (no dollar model, unlike Karpenter) combined with automatic fallback (like Karpenter) and no provisioning (unlike both references) — is coherent and well-precedented.

**Must have (table stakes) — four of the five deferred design questions resolve to INCLUDE:**
- Lowest-rank-first eligible dispatch, evaluated per candidate file (not once per tick), so a full top-rank backend cannot block files a lower-rank backend could take this tick.
- Spillover on offline mid-flight failure — file returns to `AWAITING_CLOUD`, re-dispatch picks fresh by rank against current availability (the "black-hole guard": never repeatedly reclaim onto a persistently-down backend).
- Uniform per-backend in-flight accounting across compute and Kueue (the sharpest correctness edge — see Architecture/Pitfalls below).
- Deterministic, stateless tie-break between equal-rank backends (design's own example has two rank-10 backends) — lowest-current-utilization, stable-`id` tiebreak; explicitly reject weighted fair-share.
- `cloud_target` → `backends` back-compat shim, INCLUDE but do not remove `cloud_target` this milestone (the only live deploy depends on it).
- N-lane admin surfacing (generalize v7.0 Phase 58's fixed 3 cards to N), read-only, riding the existing `/pipeline/stats` 5s poll — no second poll loop, no config editing in the UI.
- Per-backend `_FILE` secrets + a master revert-to-all-local toggle for incident response.

**Should have / thin include:** per-lane live utilization gauge (cheap, presentation-only extension of the N-lane work).

**Defer:** staleness guard on local (design's own default is rank-99 + cap-1 is sufficient structural protection; only add the cheap age-threshold form, off by default, if a real leak is observed — reject the stateful form as YAGNI); per-backend reconcile cron cadence split (keep the single `*/5` cadence until a concrete latency problem appears).

**Anti-features (explicitly locked out):** automated dollar-cost/spend model, instance provisioning/teardown/autoscaling, preemption/migration of running jobs, external/third-party plugin loading, per-cluster S3 buckets, weighted/proportional fair-share, and any new concrete cloud provider this milestone.

### Architecture Approach

This is an integration map, not a greenfield design — every seam cited resolves to a real function/module already in the codebase. The `Backend` Protocol (`is_available`/`in_flight_count`/`dispatch`/`reconcile`) is a thin adapter layer over bodies that already exist and are already isolated as module-level async functions (compute push via `tasks/push.py`, Kueue submit via `services/cloud_staging.py` + `tasks/submit_cloud_job.py`, local via `process_file`). This is precisely why phases 67-68 are genuinely behavior-preserving: no logic is rewritten, only re-homed behind the protocol.

**Major components:**
1. `ControlSettings.backends: list[BackendConfig]` (config.py) — single source of truth, replaces the 3-value `cloud_target` Literal and its three per-target validators; carries the `cloud_target` back-compat shim.
2. `Backend` Protocol + `LocalBackend`/`ComputeAgentBackend`/`KueueBackend` (new `services/backends.py`) — the seam that removes the `if/elif cloud_target` fork at every one of the ~10 grep-verified call sites (config.py, release_awaiting_cloud.py, pipeline.py, agent_s3.py, controller.py).
3. `cloud_job` sidecar + `backend_id` column (additive migration) — generalized from k8s-only to recording compute pushes too, so `in_flight_count()` is one uniform `COUNT(cloud_job WHERE backend_id=? AND <non-terminal>)` for every kind.
4. Tiered drain scheduler (`stage_cloud_window` in `release_awaiting_cloud.py`) — keeps its existing advisory-lock/FIFO/single-commit skeleton; the body inside the lock becomes: enumerate available backends → compute per-backend free slots once → for each candidate file, pick lowest-rank backend with a free slot → dispatch → decrement locally.
5. Multi-Kueue (`kube_staging.py` parameterized per-cluster) — one kr8s client per backend entry, ONE shared S3 bucket preserved (DIST-01), `reconcile_cloud_jobs` grouped by `backend_id`.

**Recommended build order** (dependency-strict, matches design §8):
- 67 — Backend registry & config model (behavior-preserving; shim yields a one-entry list).
- 68 — `Backend` protocol + 3 impls + `cloud_job.backend_id` migration + backfill + parameterized `kube_staging` (behavior-preserving; acceptance-gated by a byte-identical characterization test).
- 69 — Tiered scheduler: rank/cap drain loop, per-backend `in_flight_count`, spillover via return-to-`AWAITING_CLOUD` (behavior-changing — first tick where >1 backend runs simultaneously).
- 70 — Multi-Kueue: N clusters, shared bucket, per-cluster probe/reconcile (behavior-changing — cluster multiplicity).
- 71 — Deployment/config/docs, per-backend `_FILE` secrets, master revert toggle, N-lane UI (presentation/ops).

### Critical Pitfalls

All four research tracks independently converged on the same spine: uniform per-backend in-flight accounting is the load-bearing risk, and the known incident-class hazards (Phase 30 default-queue misrouting, the 44.5k-job recover-over-enqueue incident, dead-lettering) are exactly the failure shapes this milestone can reintroduce if the two existing substrates are fused carelessly.

1. **Double/under-counting compute in-flight** (FileState window vs. new `cloud_job` row) — pick ONE authoritative substrate (`cloud_job.backend_id`) for ALL backends and delete the other from the count path; write the registry row in the same transaction as the `FileState → PUSHING` flip, never after a separate commit. Add a `sum(in_flight_count(b)) == COUNT(FileState in-flight)` consistency assertion.
2. **Drain↔reconcile race on a per-backend cap** — today `reconcile_cloud_jobs` takes no advisory lock because the count was FileState-derived and self-healing; once counting becomes `cloud_job.status`-derived and per-backend, reconcile mutating status concurrently with the drain reading counts can overshoot a cap. Make reconcile acquire the same `pg_advisory_xact_lock` before mutating `cloud_job`/`FileState`.
3. **Two recovery mechanisms re-driving the same compute file** (the over-enqueue incident's ecosystem) — compute today has a `scheduling_ledger` seed that k8s deliberately lacks (the DIST invariant: no `process_file` re-enqueue for cloud-routed files). Generalizing must assign exactly ONE recovery owner per backend kind and extend the existing AST guard to compute-backed files, not accidentally give compute a second recovery path.
4. **Dispatch-partial limbo** — `dispatch()` must own both the `FileState` flip and the `cloud_job` upsert in one transaction; a scheduler-flips/backend-writes-separately split reintroduces silent capacity leaks the Phase 53/54 advisory lock was built to prevent.
5. **The `cloud_target`→`backends` shim silently producing an empty or wrong registry** — `local` is overloaded (means both "a backend" and "cloud disabled"); the shim must be explicit/total with a resolved-registry startup log line and a fail-fast conflict check, or misconfiguration looks exactly like the Phase 30 "silent, nothing happens" failure mode.

Additional pitfalls to carry into planning: per-entry validator gaps reintroducing the a1/k8s silent-`ANALYSIS_FAILED` trap at N-fold scale (P7); one flaky Kueue cluster's `is_available()`/`dispatch()` raising and poisoning the whole drain tick if not individually try/excepted (P8); cross-cluster S3 object collisions on spillover since the shared bucket's `file_id`-scoped keys assume one owner at a time (P9); the GATE-1/GATE-2 asymmetry (compute requires a live agent, Kueue deliberately skips that gate — "Landmine L2") being silently erased by a naively-uniform `is_available()` (P10); and a coverage-green suite that never actually instantiates N≥2 backends, hiding that the multiplicity logic — the entire point of the milestone — is untested (P11).

## Implications for Roadmap

Based on combined research, the roadmap should adopt the design's own dependency-strict five-phase structure. This is not just architecturally clean — it is the correctness strategy: phases 67-68 must be verifiably behavior-preserving (via a characterization test) precisely because phase 69 is where real risk is introduced, and de-risking it in isolation is the whole point.

### Phase 67: Backend Registry & Config Model
**Rationale:** Everything else depends on `backends: list[BackendConfig]` existing and validating correctly; must land first and must be behavior-preserving (shim synthesizes a one-entry list, so nothing observably changes).
**Delivers:** `ControlSettings.backends`, per-entry kind-dispatched fail-fast validators (replacing the three current `_enforce_*_when_*` validators), `cloud_target`→`backends` shim with resolved-registry startup logging and dual-config fail-fast.
**Addresses:** Q5 back-compat shim (FEATURES.md); the shim's empty-vs-off ambiguity (Pitfall 6).
**Avoids:** Pitfall 6 (silent empty/wrong registry — Phase-30-shaped failure), Pitfall 7 (per-entry validator gaps).

### Phase 68: `Backend` Protocol + Three Implementations
**Rationale:** Re-homes existing dispatch bodies behind the protocol without rewriting logic; must be provably behavior-preserving before multiplicity is allowed to exist, because this is where the accounting substrate changes.
**Delivers:** `services/backends.py` (`LocalBackend`/`ComputeAgentBackend`/`KueueBackend`), `cloud_job.backend_id` additive migration + backfill + compute-push recording, parameterized `kube_staging.py`.
**Uses:** pydantic discriminated unions (STACK.md), kr8s per-call client caching (STACK.md).
**Implements:** the `Backend` Protocol seam (ARCHITECTURE.md §1); the accounting unification (ARCHITECTURE.md §2).
**Acceptance gate:** a byte-identical characterization test proving single-backend dispatch decisions (including the GATE-1/GATE-2 asymmetry — Pitfall 10) are unchanged pre/post refactor.
**Avoids:** Pitfall 1 (double/under-count), Pitfall 4 (dispatch-partial limbo), Pitfall 10 (GATE asymmetry silently erased), Pitfall 11 (untested multiplicity — must add per-method-per-kind unit tests here even though only one backend is live).

### Phase 69: Tiered Drain Scheduler
**Rationale:** The first behavior-changing phase — the moment >1 backend can run simultaneously. Isolated here (after 67-68 de-risk the substrate) so the single new behavior under test is multiplicity itself.
**Delivers:** rank-first eligible dispatch evaluated per-file, spill-when-full, offline→next-eligible re-dispatch with the black-hole guard, dumb equal-rank tie-break, `recover_orphaned_work` delegating spillover back to the scheduler instead of re-homing to a named backend.
**Addresses:** Q1 (rank/cap/spill/tie-break, FEATURES.md) — the milestone's core promise.
**Avoids:** Pitfall 2 (drain↔reconcile race — needs the shared advisory lock), Pitfall 3 (double recovery), Pitfall 5 (backend thrash — needs attempt-budget split between global and per-backend cooldown).

### Phase 70: Multi-Kueue (N Clusters)
**Rationale:** Proves the registry's multiplicity extends to real N-cluster infrastructure without introducing a new provider type; depends on the Backend protocol and scheduler both existing.
**Delivers:** N `KueueBackend` entries sharing one S3 bucket (DIST-01 preserved), per-cluster kube config + LocalQueue probe/reconcile, `reconcile_cloud_jobs` grouped by `backend_id`, per-backend dashboard-reachability flags.
**Avoids:** Pitfall 8 (one cluster poisoning the whole tick — every per-backend call needs its own try/except), Pitfall 9 (cross-cluster S3 collision on spillover — cleanup must be scoped to "is this file still owned by the backend that staged it").

### Phase 71: Deployment, Config, Docs & N-Lane UI
**Rationale:** Closes out operator-facing surfaces once the scheduler and multi-Kueue are proven; presentation-only, rides existing infrastructure.
**Delivers:** per-backend `_FILE` secrets + operator runbook, master revert-to-all-local toggle, N-lane admin surfacing generalizing v7.0 Phase 58's fixed 3 cards to N (available/offline, in-flight/cap, rank — read-only, on the existing `/pipeline/stats` poll).
**Addresses:** Q4 (N-lane admin surfacing, FEATURES.md).

### Phase Ordering Rationale

- Dependency-strict, not just convenient: Q1's tiered scheduler hard-requires Q3's uniform in-flight accounting (a per-backend cap is unenforceable without a per-backend count) — this is why 68 (accounting) must precede 69 (scheduling), not run in parallel.
- Behavior-preservation as a testing strategy: phases 67-68 are scoped so the system's observable behavior is unchanged (verified by the characterization test), which means any bug surfaced in 69+ is attributable to the new multiplicity logic, not an accidental refactor regression.
- Incident-history avoidance: the dependency order is explicitly designed to avoid replaying Phase 30 (queue misrouting via a shim mapping to an empty/wrong backend list), the 44.5k-job over-enqueue incident (double recovery drivers), and the JOB-ENV-CONTRACT-style "looks covered but isn't" gap (Pitfall 11) — each of these has a named prevention phase in the Pitfalls mapping.
- UI last: the N-lane surface (Q4) enhances but does not block the scheduler — its data (`available`/`in_flight`/`cap`/`rank`) is already computed by phase 69-70, so it is correctly sequenced last and kept cheap/read-only.

### Research Flags

Needs deeper research during planning (`/gsd:plan-phase --research-phase <N>`):
- **Phase 69 (Tiered Scheduler):** the drain↔reconcile lock-ordering change and the attempt-budget/cooldown split (Pitfall 2, Pitfall 5) are novel correctness mechanisms with no existing phaze precedent — plan-time should work out the exact lock scope and the global-vs-per-backend attempt counters before implementation.
- **Phase 70 (Multi-Kueue):** the two plan-time schema open questions are unresolved and should be settled during phase planning, not roadmap creation:
  (a) does `cloud_job` stay one-row-per-file with `backend_id` mutated in place on spillover, or become one-row-per-(file, backend) for history/attempt-scoping (Pitfall 5's `attempts`-across-backends trap depends on this answer); and
  (b) how `ComputeAgentBackend.is_available()`/dispatch resolves its specific bound agent via `agent_ref` → `Agent.id`, rather than today's `select_active_agent()` "most-recently-seen" heuristic, which breaks once N compute providers coexist.
  Also verify live against a real cluster: kr8s auth/constructor form per distinct kubeconfig (STACK.md Q2 flags this as inherited from Phase 56), and the stale-Job-cleanup-before-re-dispatch ordering on cross-cluster spillover (Architecture §4).

Phases with well-documented, standard patterns (research-phase can likely be skipped):
- **Phase 67 (Registry & Config Model):** pydantic discriminated unions and pydantic-settings complex-field loading are stable, officially documented (Context7-verified) APIs already used elsewhere in the codebase.
- **Phase 68 (Backend Protocol):** the bodies it wraps already exist and are already isolated; this is a re-homing exercise with a clear acceptance test, not new design.
- **Phase 71 (Deploy/Docs/UI):** generalizes an existing, shipped v7.0 Phase 58 pattern (fixed-3 lane cards → N lane cards on the existing poll) — no new UI architecture.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | Zero new dependencies; every claim verified against Context7 official docs (pydantic-settings, kr8s) and PyPI release metadata; installed versions confirmed equal to latest and cooldown-safe. |
| Features | HIGH | Cross-checked against three independent, well-documented industry reference patterns (k8s cluster-autoscaler priority expander, Karpenter spot fallback, AWS SQS+Spot cost workers) plus direct reads of the locked design doc, `release_awaiting_cloud.py`, and PROJECT.md. |
| Architecture | HIGH | Every seam cited resolves to a real, grep-verified file/line in the actual `SimplicityGuy/Multi-Cloud-Backends` codebase, not generic pattern advice; cross-referenced against the locked design doc. |
| Pitfalls | HIGH | Grounded directly in the actual source (`release_awaiting_cloud.py`, `reconcile_cloud_jobs.py`, `cloud_staging.py`, `agent_push.py`, `cloud_job.py`, `config.py`, `pipeline.py`, `enqueue_router.py`) and the project's own documented incident history (Phase 30, v4.0.6/v4.0.8, the 44.5k over-enqueue purge, JOB-ENV-CONTRACT), not generic distributed-systems advice. |

**Overall confidence:** HIGH

### Gaps to Address

- `cloud_job` schema shape for spillover history — one-row-per-file (mutate `backend_id` in place) vs. one-row-per-(file, backend) is unresolved; directly determines whether `attempts` needs to be split into a global dispatch budget plus a per-backend cooldown counter (Pitfall 5). Resolve at Phase 68/69 plan-time, not roadmap time.
- `ComputeAgentBackend` agent resolution — must bind to a SPECIFIC agent via `agent_ref` → `Agent.id`, replacing today's `select_active_agent()` "most-recently-seen" heuristic once N compute providers coexist; the exact resolution mechanics (fallback behavior when `agent_ref` is unset, e.g. for the shim's synthesized `a1` entry) need to be nailed down at Phase 68/70 plan-time.
- Exact drain↔reconcile lock scope — whether reconcile takes the same advisory-lock key as the drain or a documented finer-grained lock-ordering is an open implementation choice (Pitfall 2); both are viable, but the choice affects reconcile-tick latency under load and should be settled during Phase 69 planning.
- Live-cluster verification carried over from Phase 56 — the exact kr8s auth/constructor form per distinct kubeconfig/context (not per-mutated-token) has not been verified against a real second cluster; flag as a Phase 70 live-E2E item, not a library gap.
- Staleness guard on local — deliberately left as an open, off-by-default knob; only add the cheap age-threshold form if real-world operation shows blip-driven leakage to local (Phase 69, optional).

## Sources

### Primary (HIGH confidence)
- `docs/superpowers/specs/2026-06-29-multi-cloud-backends-design.md` — the locked design spine (§1-8), source of all deferred-question framing
- Context7 `/pydantic/pydantic-settings` — nested submodels, `env_nested_delimiter`, complex-field JSON parsing, custom settings sources, `NestedSecretsSettingsSource`
- Context7 `/kr8s-org/kr8s` — `kr8s.api()`/`kr8s.asyncio.api()` parameters, arg-based client caching, explicit `api=` object binding
- PyPI JSON API — current versions/release dates for pydantic, pydantic-settings, kr8s
- Repo reads (direct): `src/phaze/config.py`, `src/phaze/services/kube_staging.py`, `src/phaze/tasks/release_awaiting_cloud.py`, `src/phaze/tasks/reconcile_cloud_jobs.py`, `src/phaze/services/cloud_staging.py`, `src/phaze/routers/agent_push.py`, `src/phaze/models/cloud_job.py`, `src/phaze/services/pipeline.py`, `src/phaze/services/enqueue_router.py`, `src/phaze/routers/pipeline.py`, `src/phaze/routers/agent_s3.py`, `src/phaze/tasks/controller.py`, `src/phaze/tasks/reenqueue.py`, `src/phaze/models/agent.py`
- `.planning/PROJECT.md` — DIST-01 boundary, v5.0/v6.0/v7.0 context, out-of-scope items, key decisions, documented incident history

### Secondary (MEDIUM confidence)
- Scaling Safely on AWS Spot Using the Cluster Autoscaler's Priority Expander (ZipRecruiter Tech) — confirms rank/weight-ordered "try cheapest first" is standard operator practice
- Karpenter vs Cluster Autoscaler (cast.ai) — confirms automatic spot→on-demand fallback (= spillover) is first-class expected behavior
- Running Cost-effective queue workers with Amazon SQS and EC2 Spot (AWS) — confirms one-queue/heterogeneous-priced-worker-pool shape

### Tertiary (LOW confidence)
- Design a Distributed Job Scheduler (System Design Handbook) — general spillover/threshold-redirect background only

---
*Research completed: 2026-07-03*
*Ready for roadmap: yes*
