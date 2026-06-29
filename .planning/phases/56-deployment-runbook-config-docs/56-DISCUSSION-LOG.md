# Phase 56: Deployment, runbook, config & docs - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-06-28
**Phase:** 56-deployment-runbook-config-docs
**Areas discussed:** Runbook scope & format, Doc layout / home, LocalQueue startup check, Ephemeral Agents-UI identity

---

## Runbook scope & format (KDEPLOY-01)

### Detail level

| Option | Description | Selected |
|--------|-------------|----------|
| Copy-paste-ready YAML | Complete apply-ready manifests for ResourceFlavor/ClusterQueue/LocalQueue + RBAC Role/SA/RoleBinding + bearer-token Secret, with kubectl apply steps. Mirrors v5.0 "exact ACL JSON + PG role SQL". | ✓ |
| Prose + key snippets | Describe what each object must contain with a few illustrative snippets; operator authors full YAML. Lighter, higher misconfig risk. | |

**User's choice:** Copy-paste-ready YAML

### Cluster setup delivery

| Option | Description | Selected |
|--------|-------------|----------|
| Homelab change-prompt + phaze spec | phaze ships authoritative runbook/manifests as SPEC AND emits a ready-to-paste homelab change-prompt; phaze = spec, homelab = live infra (v5.0 D-09/D-10). | ✓ |
| Pure phaze runbook only | phaze-repo runbook the operator follows with kubectl; no homelab change-prompt. | |

**User's choice:** Homelab change-prompt + phaze spec

---

## Doc layout / home (KDEPLOY-03, KDEPLOY-05)

| Option | Description | Selected |
|--------|-------------|----------|
| New docs/k8s-burst.md | One new doc holds the whole K8s feature (runbook + change-prompt + deploy ordering + transport notes + smoke test); mirrors v5.0 cloud-burst.md. configuration.md gets the knob table; deployment.md + README get pointers. | ✓ |
| Extend docs/cloud-burst.md | Fold K8s into the existing A1 cloud-burst.md. One doc for all cloud targets, but large mixed file. | |
| Section in deployment.md | Put the K8s runbook directly in deployment.md. Most discoverable but bloats it; v5.0 avoided this. | |

**User's choice:** New docs/k8s-burst.md
**Notes:** KDEPLOY-05's success criterion names `docs/deployment.md` specifically, so deployment.md still carries the single-toggle revert section + a pointer into k8s-burst.md (which holds the full cluster/bucket/secret setup) — literal criterion satisfied.

---

## LocalQueue startup check (KDEPLOY-04 — primary net-new code)

### Behavior on failure

| Option | Description | Selected |
|--------|-------------|----------|
| Warn + surface in UI | Log WARNING + surface on the dashboard (reuse Phase 54 Inadmissible surface); app still starts. Transient kube/mesh blip shouldn't take down the control plane. | ✓ |
| Fail-fast refuse-to-start | Abort startup if LocalQueue unreachable, like the config validators. Strongest signal but couples whole-app availability to cluster reachability. | |

**User's choice:** Warn + surface in UI

### Probe site

| Option | Description | Selected |
|--------|-------------|----------|
| Controller worker startup | Probe at the controller SAQ worker startup (owns kube submission + creds), GET the LocalQueue via kr8s. Surface via a dashboard flag. | ✓ |
| API lifespan startup | Probe in the FastAPI lifespan where UI renders; but api doesn't submit Jobs and may lack kube creds. | |
| Both / periodic | Probe at startup AND let the 5-min reconcile cron re-check for liveness after boot. | |

**User's choice:** Controller worker startup

---

## Ephemeral Agents-UI identity (KDEPLOY-04)

| Option | Description | Selected |
|--------|-------------|----------|
| Minimal note + suppress DEAD | No k8s agent registers as heartbeating (no perpetual-DEAD pill) + a static informational note; full ephemeral card deferred to v7.0 RECORD-03. Matches roadmap "note" wording. | ✓ |
| Full synthetic card now | Real "k8s burst" card with liveness from in-flight Kueue workloads. More complete but duplicates v7.0 RECORD-03 work. | |

**User's choice:** Minimal note + suppress DEAD

---

## Claude's Discretion

- Exact RBAC verb/resource refinement against the live kr8s submit/reconcile call graph.
- Smoke test as doc checklist vs scripted check.
- kr8s mechanism for the LocalQueue GET + exact dashboard flag plumbing (mirror Phase 54 `inadmissible`).
- Wording/structure of the homelab change-prompt.
- Precise deployment.md vs k8s-burst.md split wording.
- Whether transport-agnostic (KDEPLOY-03) gets its own subsection or folds into the endpoint table.

## Deferred Ideas

- Full ephemeral "k8s burst" Agents card with workload-derived liveness → v7.0 RECORD-03.
- KDEPLOY-06 (ConfigMap-mounted internal CA, skip Job-image rebuild on CA rotation) → deferred.
- KSUBMIT-07/08/09, KROUTE-05 cost/throughput routing → future requirements.
