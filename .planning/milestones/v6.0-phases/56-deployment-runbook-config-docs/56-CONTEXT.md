# Phase 56: Deployment, runbook, config & docs - Context

**Gathered:** 2026-06-28
**Status:** Ready for planning

<domain>
## Phase Boundary

Make the v6.0 Kubernetes (Kueue) analysis offload **operable and fully operator-controlled**.
This is an **ops-only phase** (ROADMAP: "Research: Skip"), analogous to v5.0 Phase 51 which
produced `docs/cloud-burst.md` for the A1 target. All code paths exist (Phases 52–55); this
phase makes them deployable, documented, and observable.

In scope (KDEPLOY-01..05):
- **KDEPLOY-01** — a cluster-admin **runbook** for the objects phaze does **not** create:
  Kueue ResourceFlavor (CPU-only) / ClusterQueue (single-CQ, no-preemption quota) / LocalQueue,
  the least-privilege **namespaced** RBAC Role + ServiceAccount + RoleBinding (create/get/delete
  Jobs; get/watch/list Workloads — one namespace), and the cluster **Secret** carrying the
  compute-agent bearer token.
- **KDEPLOY-02** — K8s/S3 pydantic-settings + `_FILE` secrets + fail-fast config validators.
  **Largely already shipped** (Phases 53/54/55): the three per-target validators
  (`_enforce_s3_config_when_k8s`, `_enforce_kube_config_when_k8s`, `_enforce_compute_scratch_dir_when_a1`)
  and all `_FILE`-secret fields exist. This phase's KDEPLOY-02 work is **documentation** of those
  knobs in `docs/configuration.md` (no new config validators expected).
- **KDEPLOY-03** — transport-agnostic connectivity. **Already true by construction** (phaze
  consumes operator-provided endpoints; no mesh-specific code). This phase **documents** the
  Tailscale-or-WireGuard endpoint expectations; no code change.
- **KDEPLOY-04** — net-new: a startup **LocalQueue-reachability** check + an **ephemeral-identity**
  treatment for the k8s lane in the Agents UI. (The primary net-new code in this phase.)
- **KDEPLOY-05** — single-toggle revert to all-local/A1. The toggle itself (`cloud_target`) is
  **already shipped** (55 D-02 hard-removed `cloud_burst_enabled`). This phase **documents** the
  revert in `docs/deployment.md` and the full cluster + bucket + secret setup.

Out of scope (own phases / deferred):
- phaze creating Kueue admin objects (cluster-admin/runbook only — phaze references a LocalQueue by name).
- The full ephemeral "k8s burst" Agents card with workload-derived liveness — **deferred to v7.0
  RECORD-03** (which explicitly carries this KDEPLOY-04 intent into the DAG-console redesign).
- KDEPLOY-06 (ConfigMap-mounted CA so rotation skips a Job-image rebuild) — deferred.
- Authoring live infra (OpenTofu/cluster provisioning) inside the phaze repo — workspace boundary;
  phaze emits a homelab change-prompt (see D-02).

</domain>

<decisions>
## Implementation Decisions

### Runbook scope & format (KDEPLOY-01)
- **D-01: Copy-paste-ready YAML for every operator-owned object.** The runbook ships complete,
  apply-ready manifests with `kubectl apply` steps for: the CPU-only **ResourceFlavor**, the
  single-CQ **no-preemption ClusterQueue** quota, the **LocalQueue**, the **namespaced
  least-privilege RBAC** (Role with verbs `create/get/delete` on `jobs` + `get/watch/list` on
  `workloads.kueue.x-k8s.io`, scoped to one namespace; ServiceAccount; RoleBinding), and the
  cluster **Secret** carrying the compute-agent bearer token. Operator edits names/quota/namespace
  and applies. Mirrors the v5.0 "exact ACL JSON + PG role SQL" precedent (51 D-10/D-11) — phaze
  stays the authoritative spec, operator applies.
- **D-02: Cluster setup is also delivered as a ready-to-paste homelab change-prompt.** Following
  v5.0 D-09/D-10: phaze ships the runbook/manifests as the source-of-truth **SPEC** AND emits a
  ready-to-paste **homelab repo change-prompt** (deploy ordering via `datum@nox` / `datum@lux`,
  where the manifests get applied, secret provisioning). Workspace boundary holds: **phaze = spec,
  homelab = live infra.** No live `kubectl`/cluster mutation authored in the phaze repo.

### Doc layout / home (KDEPLOY-03, KDEPLOY-05)
- **D-03: One new `docs/k8s-burst.md` holds the whole K8s feature.** Mirrors the v5.0
  `docs/cloud-burst.md` precedent (51 D-13): cluster-admin runbook (the D-01 manifests), the D-02
  homelab change-prompt, deploy ordering, transport-agnostic endpoint notes (Tailscale **or**
  WireGuard — KDEPLOY-03), and a smoke test. Keeps each cloud target's runbook self-contained;
  avoids bloating `cloud-burst.md` (already ~23KB, A1-specific) or `deployment.md` with
  vendor-specific Kueue detail.
- **D-04: Config knobs documented in `docs/configuration.md`; `deployment.md` carries the toggle +
  a pointer.** The K8s/S3 knob table (cloud_target, kube_api_url, kube_namespace, kube_local_queue,
  kube_workload_api_version, s3_* fields, presign/lifecycle/part-size/max-attempts knobs) lands in
  `configuration.md` (canonical config home, sourced from the existing `Field(...)` descriptions in
  `config.py`), flagging the `_FILE`-secret fields. **`docs/deployment.md` gets the single-`cloud_target`
  revert section** (KDEPLOY-05 names deployment.md specifically — satisfy the literal criterion) **+
  a pointer to `docs/k8s-burst.md`** for the full cluster/bucket/secret setup. `docs/README.md`
  indexes the new doc.

### LocalQueue startup reachability check (KDEPLOY-04 — primary net-new code)
- **D-05: Warn + surface on the dashboard; do NOT crash the app.** When `cloud_target=="k8s"`, the
  startup probe logs a clear WARNING and surfaces "K8s LocalQueue unreachable" on the pipeline
  dashboard (reuse the Phase 54 Inadmissible alert surface — `cloud_job.inadmissible` flag +
  `inadmissible_card.html` pattern, 54 D-06), but lets the control plane start. Rationale: a
  transient kube-API/mesh blip at boot shouldn't take down Postgres/Redis/UI/local-analysis; the
  operator sees the error without losing the app. This is a **live kube-API probe** — a different
  failure class from the three config fail-fast validators (which already exist and stay
  fail-fast). Matches criterion 4's "surfaces a clear error otherwise" wording.
- **D-06: Probe runs once at the controller SAQ worker's `startup` hook.** The controller is the
  role that owns kube submission and holds kube creds (`tasks/controller.py:startup`, where the
  Phase 54 kr8s client + reconcile cron already live). It GETs the configured LocalQueue object
  (`kube_local_queue` in `kube_namespace`) via the existing kr8s client. The api lifespan is
  rejected (it doesn't submit Jobs and may not hold kube creds). Surface state via a flag the
  dashboard reads, consistent with the reconcile cron's Inadmissible plumbing.

### Ephemeral Agents-UI identity (KDEPLOY-04)
- **D-07: Minimal v6.0 treatment — no perpetual-DEAD pill + an informational note.** Ensure no k8s
  compute-agent registers as a heartbeating `Agent` row (so the 5-state liveness `classify`
  (`services/agent_liveness.py:68`) never renders a perpetually-DEAD/`dead` pill for the k8s lane),
  and add a static informational note on the Agents page explaining the k8s lane is **ephemeral /
  Job-based**, with live liveness visible via in-flight Kueue workloads on the pipeline dashboard
  (the existing `analyzing_cloud_card` / admission-state surface). The **full synthetic "k8s burst"
  card with workload-derived liveness is deferred to v7.0 RECORD-03**, which explicitly carries this
  intent into the DAG-console redesign — avoids building Agents UI twice weeks before the v7.0
  rewrite. Matches the roadmap's "ephemeral-identity Agents-UI **note**" wording.

### Claude's Discretion
- Exact RBAC verb/resource list refinement against the live kr8s submit/reconcile call graph (the
  Job create/get/delete + Workload get/watch/list set is the floor; planner verifies nothing else
  is touched).
- Smoke test as a doc checklist vs a scripted check (v5.0 left this to discretion — 51).
- The kr8s mechanism for the LocalQueue GET and exact dashboard flag/field plumbing (mirror the
  Phase 54 `inadmissible` flag + reconcile pattern rather than inventing a new surface).
- Exact wording/structure of the homelab change-prompt, within the D-02 spec content.
- Precise `deployment.md` vs `k8s-burst.md` split wording, as long as deployment.md literally
  documents the revert toggle + the cluster/bucket/secret setup is reachable from it (criterion 5).
- Whether transport-agnostic (KDEPLOY-03) gets its own doc subsection or is folded into the
  endpoint-config table — doc structure only; no code.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Phase scope & requirements
- `.planning/ROADMAP.md` §"Phase 56: Deployment, runbook, config & docs" — goal, 5 success criteria, dependency on Phase 55.
- `.planning/REQUIREMENTS.md` — KDEPLOY-01..05 (in scope); KDEPLOY-06 + Out-of-Scope (phaze does NOT create Kueue admin objects).

### Precedent (the analogous v5.0 deploy/config/docs phase — follow its shape)
- `.planning/milestones/v5.0-phases/51-deployment-config-docs/51-CONTEXT.md` — D-09/D-10 (homelab change-prompt + phaze-as-spec), D-12 (config-knob table in configuration.md), D-13 (one new feature doc + deployment.md pointer). The template for D-01..D-04 here.

### Prior-phase decisions this phase deploys/documents
- `.planning/phases/55-routing-state-ledger-integration-the-live-seam/55-CONTEXT.md` — D-02 (`cloud_target` is the single toggle; `cloud_burst_enabled` REMOVED — KDEPLOY-05 toggle already exists); the three per-target validators KDEPLOY-02 documents.
- `.planning/phases/54-kube-submit-watch-reconcile-cron/54-CONTEXT.md` — D-06 (Inadmissible → dashboard alert + WARNING; the surface D-05 reuses for the LocalQueue check); the kr8s submit/reconcile contract the RBAC verb list (D-01) must cover.
- `.planning/phases/53-s3-object-staging-leg/53-CONTEXT.md` — KSTAGE-05 S3 `_FILE`-secret config that KDEPLOY-02 documents; the bucket lifecycle TTL setup the runbook must mention.

### Config surface (KDEPLOY-02 — documentation target; already-shipped code)
- `src/phaze/config.py` — `ControlSettings`: `cloud_target` (`:406`), `kube_api_url`/`kube_namespace`/`kube_local_queue` (`:534`/`:539`/`:544`), `kube_workload_api_version` (`:564`), `s3_*` fields (`:466`–`:496`), `cloud_route_threshold_sec`/`cloud_max_in_flight`/`kube_submit_max_attempts`; the three validators `_enforce_s3_config_when_k8s` (`:600`), `_enforce_compute_scratch_dir_when_a1` (`:621`), `_enforce_kube_config_when_k8s` (`:642`); `_FILE`-secret machinery `SECRET_FILE_FIELDS` (`:348`) + `_resolve_secret_files` (`:90`). Source the configuration.md knob descriptions directly from these `Field(...)` descriptions.

### Net-new code integration points (KDEPLOY-04)
- `src/phaze/tasks/controller.py` — `startup(ctx)` (`:51`) is where the LocalQueue probe runs (D-06); the kr8s client + Phase 54 reconcile cron already live in this module.
- `src/phaze/tasks/reconcile_cloud_jobs.py` — the Inadmissible flag/alert pattern (`cloud_job.inadmissible`, `:235`; WARNING at `:240`) the startup-check surface (D-05) mirrors.
- `src/phaze/templates/pipeline/partials/inadmissible_card.html` (+ `analyzing_cloud_card.html`, `awaiting_cloud_card.html`) — the dashboard alert/cloud-state surface to reuse.
- `src/phaze/services/agent_liveness.py` — `classify(agent, now)` (`:68`) the 5-state status; D-07 ensures the k8s lane never produces a `dead` pill here.
- `src/phaze/routers/admin_agents.py` + `src/phaze/templates/admin/agents.html` — the Agents page where the D-07 ephemeral note lands.
- `src/phaze/constants.py` — `AGENT_HEARTBEAT_INTERVAL_SECONDS`/liveness thresholds (`:52`–`:78`) explaining why a non-heartbeating k8s lane would otherwise classify DEAD.

### Docs to write/update
- `docs/k8s-burst.md` — **NEW**: runbook (D-01 manifests) + homelab change-prompt (D-02) + deploy ordering + transport-agnostic endpoint notes + smoke test (D-03).
- `docs/configuration.md` (~36KB) — add the K8s/S3 knob table + `_FILE`-secret flags (D-04, KDEPLOY-02).
- `docs/deployment.md` (~34KB) — add the single-`cloud_target` revert section (KDEPLOY-05) + a pointer to `k8s-burst.md` (D-04).
- `docs/cloud-burst.md` — the A1 precedent to mirror for structure/tone (do NOT fold K8s into it — D-03).
- `docs/README.md` — docs index; add the `k8s-burst.md` entry.

### Cross-repo (homelab) deliverable
- `.planning/ROADMAP.md` §"Phase 36" (Step D) and v5.0 51 D-09/D-10 — the precedent format for the ready-to-paste homelab change-prompt (D-02).

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- The three per-target config validators + all `_FILE`-secret fields already exist (`config.py`) — KDEPLOY-02 is documentation, not new validators.
- `cloud_target` is already the single toggle (55 D-02) — KDEPLOY-05 revert is documentation, not new code.
- The Phase 54 Inadmissible surface (`cloud_job.inadmissible` flag + `inadmissible_card.html` + reconcile WARNING) is the ready template for the D-05 LocalQueue-unreachable alert.
- The kr8s client lives in `tasks/controller.py:startup` already — the LocalQueue GET reuses it (no new client wiring).
- `docs/cloud-burst.md` is the structural/tonal template for the new `docs/k8s-burst.md`.

### Established Patterns
- phaze emits a homelab change-prompt for infra-side work (Phase 36 Step D / v5.0 51 D-09) rather than reaching into the homelab repo — workspace boundary.
- Operator-facing config knobs are documented in `configuration.md` sourced from `config.py` `Field(...)` descriptions; feature runbook lives in a dedicated `docs/<feature>.md`; `deployment.md` carries pointers (v5.0 51 D-12/D-13).
- Agent liveness is computed by `classify()` over heartbeat recency; an entity with no heartbeats classifies `dead`/`never` — hence D-07 keeps the k8s lane out of the heartbeating-agent roster.

### Integration Points
- `tasks/controller.py:startup` — add the LocalQueue-reachability probe (D-05/D-06).
- The pipeline dashboard cloud-state partials — surface the LocalQueue-unreachable alert.
- `admin/agents.html` + `admin_agents.py` — add the ephemeral k8s-lane note (D-07).
- New `docs/k8s-burst.md` + `docs/configuration.md` + `docs/deployment.md` + `docs/README.md` — the doc surface.
- Homelab change-prompt — Kueue objects + RBAC + Secret apply steps + deploy ordering.

</code_context>

<specifics>
## Specific Ideas

- RBAC is **namespaced and least-privilege**: Role verbs `create/get/delete` on `jobs` (batch) +
  `get/watch/list` on `workloads.kueue.x-k8s.io`, scoped to exactly one namespace — no cluster-wide
  grants (criterion 1, KDEPLOY-01). The runbook YAML must reflect exactly this verb set.
- ClusterQueue quota is **single-CQ, no-preemption, CPU-only ResourceFlavor** — essentia analysis
  is CPU-bound; no GPU/Coral request (PROJECT.md Key Decisions / REQUIREMENTS Out of Scope).
- The bearer-token Secret carries the **compute-agent token** the pod uses to call back
  `/api/internal/agent/*` — the only authoritative result channel (54 D-01).
- Transport is **Tailscale OR WireGuard** — the runbook describes reachable-endpoint expectations
  only; zero mesh-specific code or assumptions (KDEPLOY-03).
- The LocalQueue check is a **live runtime probe**, deliberately non-fatal (warn + surface),
  distinct from the existing fail-fast config validators which stay fatal.

</specifics>

<deferred>
## Deferred Ideas

- **Full ephemeral "k8s burst" Agents card** with liveness derived from in-flight Kueue workloads —
  v7.0 **RECORD-03** (DAG-console redesign explicitly carries KDEPLOY-04's intent forward). v6.0
  ships the minimal note + DEAD-suppression only (D-07).
- **KDEPLOY-06** — ConfigMap-mounted internal CA so CA rotation doesn't require a Job-image rebuild
  (v6.0 bakes the CA into the image). Deferred per REQUIREMENTS.
- **KSUBMIT-07/08/09, KROUTE-05 cost/throughput routing** — future requirements, not this phase.

</deferred>

---

*Phase: 56-deployment-runbook-config-docs*
*Context gathered: 2026-06-28*
