---
phase: 56-deployment-runbook-config-docs
plan: 04
subsystem: docs
tags: [kubernetes, kueue, rbac, runbook, deployment, k8s-burst]
requires:
  - "tests/test_deployment/test_k8s_runbook.py (Wave 0): YAML-validity + RBAC-covers-call-graph guards"
  - "config.py: cloud_target / kube_* / s3_* knobs (Phases 53/54/55, already shipped)"
provides:
  - "docs/k8s-burst.md: authoritative cluster-admin runbook (Kueue manifests + namespaced RBAC + Secret) + transport notes + deploy ordering + smoke test"
  - "56-HOMELAB-CHANGE-PROMPT.md: ready-to-paste homelab apply steps + deploy ordering (datum@nox/datum@lux)"
  - "docs/cloud-burst.md: pointer to k8s-burst.md (now A1-specific only)"
affects:
  - "Operators standing up the v6.0 K8s (Kueue) burst target"
tech-stack:
  added: []
  patterns:
    - "docs-as-spec / homelab-as-infra (workspace boundary, no live kubectl in the phaze repo)"
    - "apiVersion lockstep rule (manifest == cluster == PHAZE_KUBE_WORKLOAD_API_VERSION)"
key-files:
  created:
    - "docs/k8s-burst.md"
    - ".planning/phases/56-deployment-runbook-config-docs/56-HOMELAB-CHANGE-PROMPT.md"
  modified:
    - "docs/cloud-burst.md"
decisions:
  - "k8s-burst.md OWNS the K8s feature (D-03): moved the Phase-54 submit/reconcile lifecycle prose out of cloud-burst.md into k8s-burst.md, leaving cloud-burst.md A1-specific with a pointer"
  - "RBAC verb floor documented exactly as the kr8s call graph requires: jobs create/get/delete, workloads get/watch/list, localqueues get"
metrics:
  duration: "~12 min"
  tasks: 2
  files: 3
  completed: "2026-06-29"
---

# Phase 56 Plan 04: K8s cluster-admin runbook + homelab change-prompt Summary

Authored `docs/k8s-burst.md` — the apply-ready cluster-admin runbook (verified `v1beta1`
Kueue manifests, namespaced least-privilege RBAC, bearer-token Secret, apiVersion-lockstep
rule, transport-agnostic endpoint notes, deploy ordering, smoke test) — plus the ready-to-paste
`56-HOMELAB-CHANGE-PROMPT.md`, and pointed `docs/cloud-burst.md` at the new runbook so it stays
A1-specific. The Wave-0 runbook test (YAML-validity + RBAC-covers-call-graph) is now green.

## What was built

### Task 1 — `docs/k8s-burst.md` (cluster-admin runbook) — commit `10e4ed4`
- Copy-paste-ready, apply-ready fenced `yaml` manifests at `kueue.x-k8s.io/v1beta1` for every
  operator-owned object phaze does NOT create:
  - **CPU-only ResourceFlavor** (empty spec = any node; commented `nodeLabels` pin option)
  - **single-CQ no-preemption ClusterQueue** (`reclaimWithinCohort: Never` +
    `withinClusterQueue: Never`; `resourceGroups` covering `cpu`+`memory` `nominalQuota`; no
    `pods`, no `limits` — matches the requests-only Job manifest)
  - **LocalQueue** (`metadata.name == PHAZE_KUBE_LOCAL_QUEUE`, namespace ==
    `PHAZE_KUBE_NAMESPACE`)
  - **namespaced least-privilege RBAC** (ServiceAccount + Role + RoleBinding) with the exact
    verb floor: `batch/jobs` create/get/delete, `kueue.x-k8s.io/workloads` get/watch/list,
    `kueue.x-k8s.io/localqueues` get (the Phase 56 startup probe verb) — no cluster-wide grants
  - **bearer-token Secret** (core/v1 Opaque, `stringData.PHAZE_AGENT_TOKEN`, minted via
    `phaze agents add --kind compute`, consumed via `PHAZE_AGENT_TOKEN_FILE`)
- Each manifest preceded by its `kubectl apply` step; a namespace-create step first.
- **apiVersion lockstep** callout (the ONE rule: manifest == cluster == `PHAZE_KUBE_WORKLOAD_API_VERSION`)
  + a v1beta2 upgrade note (what to change, what's unused by phaze).
- **Transport-agnostic connectivity** section (Tailscale OR WireGuard; reachable-endpoint table only).
- Deploy ordering + pointer to the homelab change-prompt + a smoke-test checklist.
- DNS-1123-safe placeholder names (`phaze-cpu` / `phaze-cq` / `phaze-lq` / `phaze` /
  `phaze-submitter` / `phaze-agent-token`).
- `uv run pytest tests/test_deployment/test_k8s_runbook.py -x` → **3 passed**.

### Task 2 — homelab change-prompt + cloud-burst.md pointer — commit `965d8ba`
- New `56-HOMELAB-CHANGE-PROMPT.md` mirroring the v5.0 51 headings: title → **Context for the
  homelab agent** (workspace boundary: phaze = spec, homelab = live infra, no live kubectl) →
  numbered apply steps (§1 Kueue objects, §2 RBAC, §3 token Secret, §4 control-plane env) →
  **Deploy ordering** via `datum@nox` / `datum@lux` → **Done-when checklist**. References
  applying the manifests from `docs/k8s-burst.md` (no live kubectl authored in the phaze repo).
- `docs/cloud-burst.md`: replaced the two transitional inline k8s sections (the "Selecting the
  k8s target — required knobs" block and the "Kubernetes burst — submit/reconcile lifecycle"
  section) with a short pointer to `k8s-burst.md`; updated the 3 inline "see Kubernetes burst
  below" references to link the new doc; added a `k8s-burst.md` row to "See also". cloud-burst.md
  is now A1-specific (D-03).
- The submit → reconcile lifecycle prose was **moved** into `k8s-burst.md` (so the K8s feature
  is fully owned there) rather than deleted.
- `uv run pytest tests/test_deployment/test_k8s_runbook.py -k yaml -x` → **1 passed** (no
  manifest regressions; full file still 3 passed).

## Verification
- `uv run pytest tests/test_deployment/test_k8s_runbook.py` → **3 passed** (valid YAML +
  required kinds {ResourceFlavor, ClusterQueue, LocalQueue, ServiceAccount, Role, RoleBinding,
  Secret} + RBAC verb floor ⊇ the kr8s call graph).
- Acceptance strings confirmed present: homelab prompt carries "Context for the homelab agent",
  "Deploy ordering", "datum@nox", "datum@lux", "Done-when checklist"; cloud-burst.md has 5
  references to `k8s-burst.md` and no residual `## Kubernetes burst` inline runbook heading.
- Pre-commit hooks ran clean on both commits (no `--no-verify`).

## Deviations from Plan
**1. [Doc-structure] Moved the Phase-54 submit/reconcile lifecycle prose from cloud-burst.md into k8s-burst.md.**
- **Found during:** Task 2 (removing the transitional inline k8s sections from cloud-burst.md).
- **Why:** The plan's Task 1 referenced the lifecycle section as living in cloud-burst.md, but
  Task 2 removes that section (D-03 wants cloud-burst.md A1-specific). To avoid a broken
  cross-reference and to make k8s-burst.md the single authoritative home of the K8s feature
  (D-03), the prose was relocated into k8s-burst.md rather than dropped. No information lost; the
  forward-reference in k8s-burst.md became a real in-doc section.
- **Files modified:** docs/k8s-burst.md, docs/cloud-burst.md
- **Commit:** 965d8ba

Otherwise the plan executed as written.

## Known Stubs
None — both docs are complete, apply-ready content; no placeholder/TODO sections that block the
plan goal. (Manifest values like `nominalQuota: "8"` and the token string are intentional
operator-edited placeholders, documented as such.)

## Self-Check: PASSED
- FOUND: docs/k8s-burst.md
- FOUND: docs/cloud-burst.md
- FOUND: .planning/phases/56-deployment-runbook-config-docs/56-HOMELAB-CHANGE-PROMPT.md
- FOUND commit: 10e4ed4
- FOUND commit: 965d8ba
