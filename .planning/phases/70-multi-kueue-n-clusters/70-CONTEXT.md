# Phase 70: Multi-Kueue (N Clusters) - Context

**Gathered:** 2026-07-04
**Status:** Ready for planning

<domain>
## Phase Boundary

Generalize today's **single-cluster** Kueue path into **N concurrently-dispatched Kueue backends**.
Each `KueueBackend` entry carries its own kube config (per-cluster kubeconfig/context) and stages long
files to a bucket drawn from its **REG-05-assigned bucket set** (shared/public or cluster-specific).
Delivers: per-cluster LocalQueue reachability probe, `backend_id`-scoped reconcile, **per-backend failure
isolation** (one flaky cluster cannot poison the whole drain tick), and **per-(backend,bucket) staged-object
cleanup** so a spillover re-dispatch never deletes an object another cluster/bucket still owns. The control
plane stays the **sole** S3 importer/presigner for every bucket (DIST-01 preserved); pods/agents stay
credential-free, receiving only presigned, `file_id`-scoped, TTL-bounded URLs. Covers **MKUE-01..04**.

**Behavior-changing** (cluster multiplicity — building on Phase 69's scheduler multiplicity) and a
**research-flagged** phase: deep mechanics (exact drain↔reconcile lock scope on the clean-before-flip,
the kr8s constructor form per distinct kubeconfig verified live) go to the researcher/planner.

**Out of scope (deferred):** N-lane UI + master revert toggle + config/docs (Phase 71, BEUI-01..03); new
concrete compute providers and the compute `agent_ref` fix (PROV-01, Future Requirements — see D-05);
instance provisioning, dollar-cost model, weighted fair-share (milestone non-goals).
</domain>

<decisions>
## Implementation Decisions

### Spillover cleanup identity — MKUE-04 (the crux; resolves research flag a)
- **D-01 (clean-before-flip + record staging bucket):** Add a **nullable `staging_bucket` column** to
  `cloud_job` (additive migration) recording **which bucket id** staged the current object. On spillover
  (a Kueue file returning to `AWAITING_CLOUD` to be re-dispatched to a different cluster/bucket), the
  **old `(backend_id, staging_bucket)` object is deleted in the SAME transition, BEFORE `backend_id` /
  `staging_bucket` are repurposed** for the new owner. Active, correctly-scoped cleanup is **primary**;
  the per-bucket lifecycle TTL is a pure **backstop** (matches MKUE-04's "scoped to the (backend,bucket)
  that staged the object" wording). Chosen over "re-derive deterministically" (breaks on in-place
  mutation — the old `backend_id` is gone after the flip; drifts if the backend's bucket set changes in
  config) and over "TTL-only for spilled objects" (under-delivers the active-cleanup requirement).
- **D-02 (one-row-per-file — confirms research flag a):** `cloud_job` **stays one-row-per-file**
  (`backend_id` + the new `staging_bucket` mutated in place; the existing `unique(file_id)` invariant is
  preserved). Consistent with Phase 69 D-06 (stateless re-rank, no per-file failure memory). The
  clean-before-flip step (D-01) handles the old object at transition time, so **no per-(file,backend)
  history table is needed**. Rejected one-row-per-(file,backend) as reopening Phase 69's settled attempts
  model, breaking `in_flight_count` queries + the unique-file_id invariant, for nil win on a single-user
  finite backlog.
- **D-03 (reconcile owns cleanup, best-effort):** The **`backend_id`-scoped `reconcile`** (which already
  detects fail/offline and returns the file to `AWAITING_CLOUD`) issues the `delete_object` for the
  recorded `(backend_id, staging_bucket)` in that same transition, before repurposing the row. It is
  **best-effort**: swallow already-absent/failed-delete errors (mirrors the existing idempotent S3 delete
  where a missing object IS the desired end state) so a failed delete **never blocks re-dispatch**; TTL
  backstops any miss. Cleanup is deliberately **NOT** in the hot dispatch path (keeps S3 latency out of
  the advisory-locked tick — cap-timing stays clean).

### Per-cluster kube auth form — MKUE-01
- **D-04 (support both forms, retire the token-mutation hack):** Build a **distinct kr8s client per
  backend** from its `KubeConfig`, threaded through every `kube_staging` call (retire the module-global
  `active_kube` read). When `kubeconfig` + `context` are set, use them (kr8s arg-caches distinct clients
  cleanly — no shared-client mutation); this is the **clean multi-cluster path**. When `api_url` +
  `sa_token` are set, keep that form **but replace the post-construction `api.auth.token = token;
  await api._create_session()` hack with the correct constructor-time auth form**. Keeps the one deployed
  cluster's `api_url` shape working while making `kubeconfig`+`context` the recommended N-cluster path.
  Rejected "kubeconfig+context only" (forces a deployment migration, removes a working form) and "keep
  the hack" (carries the fragile `_create_session()` rebuild across N clients — exactly what research
  flagged for retirement).

### Compute agent_ref resolution scope — resolves research flag b
- **D-05 (defer to PROV-01):** Phase 70 does **NOT** fix `ComputeAgentBackend`'s
  `select_active_agent(session, kind="compute")` "most-recently-seen" heuristic to resolve its bound
  agent via `agent_ref → Agent.id`. Rationale: MKUE-01..04 are **Kueue-only**; only one compute agent
  (`a1`) exists today so the current heuristic is still correct; the `agent_ref` gap only bites once a
  **2nd compute provider** coexists, which is **PROV-01** (explicitly deferred to Future Requirements).
  Fixing it now is scope creep into a Kueue-focused phase and cannot be end-to-end validated (no 2nd
  compute provider to test against). The `agent_ref` field already exists on `ComputeBackend` config
  (REG-02) but is unused by the service impl — this **latent gap is noted for PROV-01** (see Deferred).

### Deterministic per-file bucket selection — MKUE-02
- **D-06 (stable hash of file_id):** When a Kueue backend's assigned bucket set holds multiple buckets,
  staging selects one deterministically per file: **index = stable_hash(file_id) mod len(sorted(bucket_ids))**.
  Sort the bound bucket-id list for a stable order, and use a **stable hash (e.g. sha256 of the UUID) —
  NOT Python's salted `hash()`** — so the same file maps to the same bucket across process restarts. This
  determinism is **load-bearing**: cleanup/reconcile (D-01/D-03) must re-agree with staging on the file's
  bucket for a given set (and the `staging_bucket` column records the actual choice at stage time as the
  authoritative record). Rejected round-robin/least-loaded (needs shared mutable state in the locked tick,
  non-reproducible mapping) and first-bucket-only (wastes multi-bucket capacity).

### Per-cluster failure isolation — MKUE-03 (research Pitfall 8)
- **D-07 (per-backend try/except in snapshot + dispatch):** Wrap **each backend's per-tick
  `is_available()` / `in_flight_count()` snapshot AND each `dispatch()` call in its own try/except**: a
  raising or timing-out cluster is treated as **unavailable (0 slots) for that tick and logged**, while
  every other backend proceeds normally. Extends Phase 68's "`is_available` never raises" discipline to
  the N-cluster snapshot loop. (Reconcile is already per-row guarded from Phase 69.) Rejected "rely on the
  existing per-candidate guard" — a probe raising during the once-per-tick snapshot (before per-candidate
  selection) could still abort the whole tick, which is exactly Pitfall 8.

### Claude's Discretion (planner/researcher decides)
- Exact `cloud_job.staging_bucket` column type/name + the additive migration mechanics (nullable, no
  meaningful backfill — the a1/k8s cloud paths were never deployed live, so ~zero live rows exist;
  mirrors Phase 68 D-06).
- Whether the re-homed `kube_staging` functions (`submit_job`/`get_job`/`get_local_queue`/
  `list_inflight_jobs`/`get_workload_for`) take a `KubeConfig` parameter or become `KueueBackend` methods
  (follow the Phase 68 backend-method pattern where natural).
- The exact stable-hash primitive for D-06 (sha256 of the UUID bytes vs another stable digest) and how
  the bucket-id list is sorted.
- Exact `pg_advisory_xact_lock` scope for the clean-before-flip delete relative to the drain snapshot
  (**research flag** — the drain↔reconcile lock-ordering novel-correctness mechanic carried from Phase 69).
- Whether presigned-GET minting picks the same D-06 bucket at mint time (it must — the pod GETs the same
  object staged); planner confirms the presign path reads the recorded `staging_bucket`, not a re-derive.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements & roadmap (authoritative scope)
- `.planning/REQUIREMENTS.md` §MKUE (MKUE-01..04) — the multi-Kueue requirements; §REG-05 (the S3 staging
  bucket registry: `scope` shared/public vs cluster-specific, per-Kueue-backend bucket-set binding, the
  "cluster-specific bucket ≤1 backend, shared bucket many" cardinality) that MKUE-02/04 build the staging
  + cleanup behavior on. Also §PROV-01 (Future Requirements) — where D-05's deferred compute `agent_ref`
  fix is tracked; §Out of Scope (no provisioning, no dollar-cost, no weighted fair-share).
- `.planning/ROADMAP.md` → "Phase 70: Multi-Kueue (N Clusters)" — goal + the 2026.7.1 execution discipline
  (PR-per-phase on a worktree branch, **never a direct commit to `main`**; dependency-strict 67→71).
  Note the roadmap's "each cluster stages to its REG-05-assigned bucket set (DIST-01 preserved)" line and
  that REG-05 + revised MKUE-02/04 **supersede** the design doc's original one-shared-bucket §6/§7.

### Design spine (with the operator-directed supersession)
- `docs/superpowers/specs/2026-06-29-multi-cloud-backends-design.md` (PR #182): §4.2 Backend protocol,
  §4.4 per-backend in-flight registry, §4.5 failure/spillover, §5 what-stays-untouched (`put_analysis`
  result return, duration gating, agent HTTP surface, windowed analysis — all unchanged this phase).
  **IMPORTANT:** the design's §6/§7 "one shared S3 bucket / no per-cluster buckets" decision is
  **SUPERSEDED** by REG-05 + MKUE-02/04 (operator direction 2026-07-03). The research SUMMARY item 5/§58
  and its "one shared bucket" language predate that revision — **use REQUIREMENTS.md §REG-05 / §MKUE, not
  the stale one-bucket framing.**

### Prior-phase decisions this phase builds on
- `.planning/phases/69-tiered-drain-scheduler/69-CONTEXT.md` — D-04 (per-backend attempt bound reusing
  `cloud_submit_max_attempts`, fall-to-local, global total-attempt ceiling), **D-06 (stateless re-rank, no
  per-file failure memory — the basis for D-02 one-row-per-file here)**, SCHED-02 per-row advisory-locked
  reconcile.
- `.planning/phases/68-backend-protocol-3-implementations/68-CONTEXT.md` — **D-05 explicitly defers
  per-cluster `kube_staging` parameterization + retiring the token-mutation hack to THIS phase (MKUE-01)**;
  D-09 (the retained `active_kube` / `active_bucket` / `active_compute_scratch_dir` value accessors are to
  be **retired/replaced in Phase 70** — their docstrings were re-pointed here); D-08 (compute `cloud_job`
  row + `s3_key` nullable, migration 029); D-10 (in-flight status set `{UPLOADING,UPLOADED,SUBMITTED,RUNNING}`).
- `.planning/phases/67-backend-registry-config-model/67-CONTEXT.md` — the `backends` registry + REG-05
  `BucketConfig`/`KubeConfig` submodels the implementations bind to.

### Research (read before planning — the sharpest correctness edges)
- `.planning/research/SUMMARY.md` — Phase 70 section + Research Flags (flag a `cloud_job` schema shape
  **resolved here = D-02 one-row-per-file**; flag b `agent_ref`→`Agent.id` **resolved here = D-05 defer to
  PROV-01**; live-cluster kr8s auth verification carried from Phase 56). **NOTE its "one shared bucket"
  language is stale — see the design-spine supersession above.**
- `.planning/research/PITFALLS.md` — **Pitfall 8** (one flaky cluster's `is_available`/`dispatch` raising
  poisons the whole tick → D-07 per-backend try/except), **Pitfall 9** (cross-cluster S3 collision on
  spillover; `file_id`-scoped keys assume one owner at a time → D-01 clean-before-flip + `staging_bucket`),
  Pitfall 2 (drain↔reconcile lock race — the lock scope is the research-flagged mechanic).

### Existing code to modify / re-home (not rewrite)
- `src/phaze/services/backends.py` — `KueueBackend.is_available/dispatch/reconcile` (currently
  single-cluster, reading module-global `active_kube`/`active_bucket`); `resolve_backends` (already builds
  one impl per registry entry — N supported since Phase 69). D-04 threads `self.config` (the KueueBackend
  submodel + its `[kube]` config + `buckets`) through; D-07 wraps the drain's per-backend snapshot/dispatch.
- `src/phaze/services/kube_staging.py` — `_kube_config()` (reads `cfg.active_kube` — replace with the
  per-backend `KubeConfig`); `_api()` (the **token-mutation hack** at L101-110 to retire, D-04);
  `submit_job`/`get_job`/`get_local_queue`/`list_inflight_jobs`/`get_workload_for` (all no-arg, resolve
  the single cluster — parameterize per backend).
- `src/phaze/services/s3_staging.py` — `_staging_config()` (reads `cfg.active_bucket`, literal
  `# TRANSITIONAL — Phase 68 (per-file bucket selection = Phase 70 MKUE-02)` marker at L77 — implement
  D-06 here); `_client(bucket)`, `staged_object_key(file_id)`, `delete_object` (the cleanup leg for D-01/D-03).
- `src/phaze/models/cloud_job.py` — add nullable `staging_bucket` (D-01); one-row-per-file `unique(file_id)`
  preserved (D-02); existing `backend_id`, `s3_key` (nullable), `attempts`, `kueue_workload`.
- `src/phaze/services/cloud_staging.py` — `_stage_file_to_s3` (the shared no-commit staging core the
  KueueBackend calls; must stamp `staging_bucket` alongside `backend_id`).
- `src/phaze/tasks/release_awaiting_cloud.py` — `stage_cloud_window` drain (the per-tick snapshot loop
  D-07 guards) and `reconcile_cloud_jobs` cron (aggregates the per-backend `reconcile` tallies over N
  clusters).
- `src/phaze/config.py` — the retained `active_kube` / `active_bucket` value accessors (Phase 68 D-09)
  are the single-cluster reads this phase supersedes with per-backend resolution.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`resolve_backends()`** already builds one `Backend` impl per registry entry — N `KueueBackend`
  instances are produced today; Phase 70 makes each instance's kube/bucket reads use **its own config**
  instead of the module-global accessors. The scheduler wiring (Phase 69) already iterates the list.
- **`KubeConfig` / `BucketConfig` submodels** (Phase 67, `config_backends.py`) already carry per-entry
  `kubeconfig`+`context` / `api_url`+`sa_token`, and per-bucket `endpoint_url`+creds+`scope`+
  `addressing_style` — the per-cluster/per-bucket config surface exists; only the service reads are global.
- **Idempotent S3 delete** (`s3_staging.py` — "a missing object is the desired end state", swallowed error
  codes) is exactly the best-effort primitive D-03 needs for clean-before-flip.
- **Per-row `session.rollback()` reconcile guard** (Phase 69 `KueueBackend.reconcile`) already isolates a
  bad row; D-07 extends the same "never raise out of the tick" discipline to the per-backend *snapshot*.

### Established Patterns
- **Fail-fast-at-startup + cron-no-op-at-runtime** — REG-05 validation already fails fast on an
  unreachable/empty bucket set; the runtime probe/dispatch degrades to a hold (D-07), never raises.
- **`cloud_job` is a per-`file_id` sidecar** with a string-backed CHECK-constrained status — adding a
  nullable `staging_bucket` needs no enum migration (mirrors Phase 68's additive `backend_id`).
- **Control-plane-only S3 / DIST-01** — every presign/upload/delete lives in `s3_staging.py`; pods/agents
  never hold bucket creds. D-06's per-file bucket choice happens control-side; the pod only ever sees a
  presigned URL for the already-selected bucket.

### Integration Points
- **`stage_cloud_window`** (drain) — the per-tick snapshot + `select_backend` dispatch; D-07 wraps each
  backend here.
- **`reconcile_cloud_jobs`** cron — aggregates per-backend `reconcile` tallies across N clusters; owns the
  clean-before-flip delete (D-03) under the (research-flagged) advisory-lock scope.
- **Presigned-GET mint** (`s3_staging` + the agent/pod GET) — must mint against the D-06-selected /
  recorded `staging_bucket`, not a re-derive.

</code_context>

<specifics>
## Specific Ideas

- The **clean-before-flip ordering is the operator's explicit correctness requirement**: the old object
  must be deleted *before* the row is repurposed, because after `backend_id`/`staging_bucket` mutate there
  is no way to identify the stranded object except the TTL backstop (Pitfall 9).
- **Prefer determinism over balancing** for bucket selection (D-06): a reproducible file→bucket mapping is
  worth more than even load spread, because cleanup/reconcile must independently agree with staging.
- **Keep Phase 70 Kueue-focused**: the compute `agent_ref` fix (D-05) is deliberately out — don't let a
  research-flag lump pull compute-path work into a Kueue phase.
</specifics>

<deferred>
## Deferred Ideas

- **Compute `agent_ref → Agent.id` resolution** — the latent gap where `ComputeAgentBackend` ignores its
  `agent_ref` config field and uses `select_active_agent(kind="compute")` most-recently-seen. Deferred to
  **PROV-01** (Future Requirements — first-class AWS/GCP compute backends); harmless today with a single
  `a1` compute agent, breaks only when a 2nd compute provider coexists. (D-05.)
- **Live multi-cluster kr8s auth verification** — the exact kr8s constructor form per distinct
  kubeconfig/context (not per-mutated-token) is a **Phase-56-carryover live-E2E item**, to re-run FIRST
  against a real second cluster at rollout (joins the v6.0 deployment-gated UAT items in STATE.md). Not a
  library gap — flagged for the researcher + deployment verification.
- **N-lane admin UI, master revert-to-all-local toggle, operator runbook/config docs, the
  `cloud_target`→`backends` migration doc** — BEUI-01..03, **Phase 71**.
- **Duration-scaled / per-backend reconcile cron cadence split** (compute vs kueue) — SREF-01, Future
  Requirements; keep the single `*/5` cadence until a concrete latency problem appears.

### Reviewed Todos (not folded)
None — no pending todos matched this phase.

</deferred>

---

*Phase: 70-multi-kueue-n-clusters*
*Context gathered: 2026-07-04*
