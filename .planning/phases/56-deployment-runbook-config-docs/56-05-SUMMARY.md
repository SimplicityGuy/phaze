---
phase: 56-deployment-runbook-config-docs
plan: 05
subsystem: docs
tags: [config, deployment, k8s, s3, kueue, cloud-burst, runbook]
requires:
  - "config.py ControlSettings K8s/S3 knobs + per-target validators (Phases 53/54/55, already shipped)"
provides:
  - "docs/configuration.md: complete K8s/S3 knob reference with _FILE flags + fail-fast-vs-probe note"
  - "docs/deployment.md: single-cloud_target revert procedure + k8s-burst.md pointer"
  - "docs/README.md: k8s-burst.md index row under Operations"
affects:
  - docs/configuration.md
  - docs/deployment.md
  - docs/README.md
tech-stack:
  added: []
  patterns:
    - "Doc knob tables sourced verbatim from config.py Field(...) descriptions"
    - "_FILE secret-flag column for control-plane credentials"
key-files:
  created: []
  modified:
    - docs/configuration.md
    - docs/deployment.md
    - docs/README.md
decisions:
  - "Documented the two distinct k8s guard layers separately: startup fail-fast model validators (missing config → crash) vs the non-fatal runtime LocalQueue admission probe (present-but-unadmittable → live Inadmissible dashboard card)"
  - "Revert is a single PHAZE_CLOUD_TARGET=local flip + restart with no teardown of K8s/S3/A1 objects; in-flight work drains"
metrics:
  duration: ~10 min
  completed: 2026-06-29
  tasks: 2
  files: 3
  commits: 2
---

# Phase 56 Plan 05: K8s/S3 Config & Revert Docs Summary

Documentation-only plan: completed the K8s/S3 config-knob reference in `configuration.md` (KDEPLOY-02) and added the single-`cloud_target` revert procedure + `k8s-burst.md` pointer in `deployment.md` (KDEPLOY-05), with a `k8s-burst.md` index row in `docs/README.md`. No code, no validators, no new dependencies.

## What Was Built

### Task 1 — Complete the K8s/S3 knob table in configuration.md (KDEPLOY-02) — `ca67d02`

The existing `## Cloud-burst settings` section already documented `cloud_target`, the rsync/A1 knobs, and the Phase-54 Kube submit/reconcile knobs. The gap was the **S3 object-staging leg** (Phase 53) and a clear statement of the validator semantics. Added:

- **New `### S3 object-staging settings (Phase 53, v6.0)` subsection** with a full knob table sourced verbatim from the `config.py` `Field(...)` descriptions: `s3_endpoint_url`, `s3_bucket`, `s3_region`, `s3_addressing_style`, `s3_access_key_id`, `s3_secret_access_key`, `s3_presign_put_ttl_sec`, `s3_presign_get_ttl_sec`, `s3_lifecycle_ttl_days`, `s3_multipart_part_size_bytes` — each with env-var alias (`PHAZE_*`), role (Control), default, bounds, and a `_FILE` column flagging the two credential fields.
- **Central `_FILE` convention table extended** with the four control-plane credential rows that were missing: `s3_access_key_id`, `s3_secret_access_key`, `kube_kubeconfig`, `kube_sa_token`.
- **New `### Fail-fast startup validators vs. the non-fatal runtime LocalQueue probe` note** distinguishing the two guard layers that protect the `k8s` path:
  - Startup fail-fast (`_enforce_s3_config_when_k8s`, `_enforce_kube_config_when_k8s`, plus the `a1`-only `_enforce_compute_scratch_dir_when_a1`) — *missing* K8s/S3 config crashes the controller at construction. Kept as three separate per-target validators (verified against `config.py:601/643/621`).
  - Runtime non-fatal LocalQueue admission probe — a *present-but-unadmittable* LocalQueue/ClusterQueue surfaces as a warning log + an **Inadmissible** operator-alert card via the `*/5` `reconcile_cloud_jobs` cron (verified against `reconcile_cloud_jobs.py:240` / `pipeline.py:825`), never a crash.

Verify command prints `OK` (`PHAZE_KUBE_WORKLOAD_API_VERSION` + `PHAZE_S3_ENDPOINT_URL` + `PHAZE_KUBE_LOCAL_QUEUE` all present).

### Task 2 — deployment.md revert section + pointer; README index row (KDEPLOY-05) — `e234c3a`

- **New `### Revert / single-toggle (disable cloud offload)` subsection** in `deployment.md`: setting `PHAZE_CLOUD_TARGET=local` (or removing it — `local` is the default) and restarting `worker` + `api` reverts the entire offload to all-local with **no other change**. Documents that the flip is a startup-read (restart required), that in-flight `PUSHING`/`PUSHED` work drains, and that the K8s/S3/A1 objects can be left inert (no teardown needed to revert; re-enable is the reverse flip). Also documents switching *between* targets (`a1` vs `k8s`) with their respective fail-fast requirements.
- **Explicit `[k8s-burst.md](k8s-burst.md)` pointers** (two) for the full cluster/bucket/secret/transport setup — so `deployment.md` literally documents both the revert AND a reachable path to the full setup (KDEPLOY-05 criterion 5).
- **`docs/README.md`**: added a `Kubernetes Burst` row under `## 🚀 Operations` mirroring the Cloud Burst row format (☸️ + one-line description: Kueue Job-runner runbook, RBAC, `_FILE` Secret, S3 staging, master toggle).

Verify command prints `OK` (`k8s-burst.md` in both `deployment.md` and `README.md`; `PHAZE_CLOUD_TARGET=local` in `deployment.md`).

## Source Accuracy

All documented knob names, defaults, bounds, aliases, and the `cloud_target` Literal toggle were verified against the actual `src/phaze/config.py` source (the `ControlSettings` `Field(...)` entries and the three `@model_validator` per-target guards) rather than from training knowledge, per the plan's parallel-execution note. The runtime LocalQueue probe behavior was verified against `reconcile_cloud_jobs.py` and `services/pipeline.py`.

## Note on the k8s-burst.md pointer target

`docs/k8s-burst.md` did not exist in this worktree at execution time; it is produced by a sibling plan in this phase (the runbook plan). The pointers added here intentionally reference it by its canonical path so they resolve once the sibling plan's file lands on merge. This is a documentation cross-reference, not a missing dependency for this plan's deliverables.

## Deviations from Plan

**1. [Rule 2 - Missing critical doc completeness] Extended the central `_FILE` convention table.**
- **Found during:** Task 1
- **Issue:** The central `## Secrets via files (_FILE convention)` table (the canonical secret-field index) omitted all four k8s/S3 control-plane credentials, even though the per-feature knob tables flagged them. D-04 / T-56-SECRETS require every credential field flagged with its `_FILE` sibling consistently.
- **Fix:** Added `s3_access_key_id`, `s3_secret_access_key`, `kube_kubeconfig`, `kube_sa_token` rows to the central table, matching `SECRET_FILE_FIELDS` in `config.py:348`.
- **Files modified:** docs/configuration.md
- **Commit:** ca67d02

## Verification

- Task 1 grep: `PHAZE_KUBE_WORKLOAD_API_VERSION` + `PHAZE_S3_ENDPOINT_URL` + `PHAZE_KUBE_LOCAL_QUEUE` → `OK`
- Task 2 grep: `k8s-burst.md` (deployment.md + README.md) + `PHAZE_CLOUD_TARGET=local` (deployment.md) → `OK`
- Pre-commit hooks ran on both commits (markdown files; large-files / EOF / trailing-whitespace / private-key / aws-creds all passed; code hooks skipped — no code files).
- Manual KDEPLOY-05 end-to-end (set `PHAZE_CLOUD_TARGET=local`, redeploy, confirm long files route off the k8s path) remains manual-only — no CI cluster — as the plan states.

## Self-Check: PASSED

- FOUND: docs/configuration.md (modified, committed ca67d02)
- FOUND: docs/deployment.md (modified, committed e234c3a)
- FOUND: docs/README.md (modified, committed e234c3a)
- FOUND commit: ca67d02
- FOUND commit: e234c3a
</content>
</invoke>
