---
phase: 56
slug: deployment-runbook-config-docs
status: verified
nyquist_compliant: true
wave_0_complete: true
created: 2026-06-28
updated: 2026-06-28
---

# Phase 56 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Seeded from `56-RESEARCH.md` §Validation Architecture. Per-task map finalized by the planner.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio + pytest-cov (uv-managed) |
| **Config file** | `pyproject.toml` (`[tool.pytest...]` / `[tool.coverage]`) |
| **Quick run command** | `uv run pytest tests/test_kube_staging.py tests/test_agent_liveness.py -x` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` (≥85% required) |
| **Estimated runtime** | ~60–120 seconds (full suite) |
| **Existing kube seams** | `tests/kube_fakes.py` (fake_job/fake_workload + canned conditions), `tests/test_deployment/` (compose/job-image invariants) |

---

## Sampling Rate

- **After every task commit:** Run the quick command above (kube_staging + agent_liveness).
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing` (≥85%).
- **Before `/gsd:verify-work`:** Full suite green + `pre-commit run --all-files`.
- **Max feedback latency:** ~120 seconds.

---

## Per-Task Verification Map

> Finalized after execution against the actual test files created by the phase. Wave-0 scaffolding
> (56-00, 56-03) authored the RED tests; the implementation plans (56-01, 56-02, 56-06) turned them
> green. The CR-01/WR-01 boot-resilience cases were added during the execute-phase code-review gate.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|--------|
| 56-00 → 56-04 | 56-00/56-04 | 0→1 | KDEPLOY-01 | T-56-RBAC | Namespaced RBAC verb set ⊇ kr8s call graph (jobs create/get/delete; workloads get/watch/list; localqueues get) | unit | `uv run pytest tests/test_deployment/test_k8s_runbook.py::test_rbac_covers_call_graph -x` | ✅ green |
| 56-00 → 56-04 | 56-00/56-04 | 0→1 | KDEPLOY-01 | — | Runbook manifests parse as valid YAML + all required kinds present | unit | `uv run pytest tests/test_deployment/test_k8s_runbook.py -k "yaml or required_kinds" -x` | ✅ green |
| 56-00 → 56-01 | 56-00/56-01 | 0→1 | KDEPLOY-04 | T-56-DOS | `get_local_queue()` success / 404→raise / transient→raise | unit | `uv run pytest tests/test_services/test_kube_staging.py -k get_local_queue -x` | ✅ green |
| 56-00 → 56-01 | 56-00/56-01 | 0→1 | KDEPLOY-04 | T-56-DOS | Probe gated on `cloud_target=="k8s"`; kube/Redis blips never abort boot (CR-01); stale flag cleared off-k8s (WR-01) | unit | `uv run pytest tests/test_tasks/test_controller_startup_localqueue.py -x` | ✅ green |
| 56-00 → 56-02 | 56-00/56-01/56-02 | 0→2 | KDEPLOY-04 | T-56-POLL | `get_localqueue_unreachable` degrade-safe (missing/error redis → False); alert empty when reachable; renders + OOB on stats when flagged | unit | `uv run pytest tests/test_routers/test_pipeline_localqueue.py -x` | ✅ green¹ |
| 56-03 | 56-03 | 0 | KDEPLOY-04 | T-56-DEAD | Invariant: `classify(agent, now) != "dead"` when `last_seen_at is None` (always `never`) | unit | `uv run pytest tests/test_services/test_agent_liveness.py -k never_not_dead -x` | ✅ green |
| 56-03 | 56-03 | 0 | KDEPLOY-04 | — | `job_runner` never runs the heartbeat loop (ephemeral pod doesn't heartbeat) | unit | `uv run pytest tests/test_task_split.py -k "heartbeat or job_runner" -x` | ✅ green |
| 56-04/05 | 56-04/56-05 | 1 | KDEPLOY-02/03/05 | — | Docs present/linked (k8s-burst.md, configuration.md knob table, deployment.md revert + pointer, README index) | doc/manual | doc-checklist (no automated link test) — see Manual-Only | 📋 manual-only |

*Status: ✅ green · ⬜ pending · ❌ red · ⚠️ flaky · 📋 manual-only*

> ¹ `test_pipeline_localqueue.py` is auto-marked `integration` (consumes a DB-backed fixture) and runs
> green under `just integration-test` / CI with the ephemeral Postgres+Redis; it ECONNREFUSEs on a bare
> local run with no DB (same as the sibling `test_pipeline_inadmissible` suite). Code wiring is verified.

---

## Wave 0 Requirements

- [x] `tests/test_deployment/test_k8s_runbook.py` — parses runbook manifests as YAML; asserts required kinds + the RBAC verb set covers the kr8s call graph (KDEPLOY-01). **3/3 green.**
- [x] `tests/kube_fakes.py` — `fake_local_queue(...)` helper + 404/transient seam for `get_local_queue` (mirrors `fake_job`).
- [x] `tests/test_services/test_kube_staging.py` — `get_local_queue` success / not-found / transient cases. **green.**
- [x] `tests/test_tasks/test_controller_startup_localqueue.py` — probe gating (`cloud_target=="k8s"`) + boot-resilience; extended with CR-01 (Redis-down) and WR-01 (stale-flag clear) cases. **7/7 green.**
- [x] `tests/test_routers/test_pipeline_localqueue.py` — alert OOB carrier + degrade-safe flag read + empty-when-reachable. **4/4 green under integration DB.**
- [x] `tests/test_services/test_agent_liveness.py` (`never`-not-`dead` invariant) + `tests/test_task_split.py` (`test_job_runner_does_not_run_heartbeat_loop`). **green.**

*Pure-docs (not meaningfully samplable beyond YAML-validity + link checks): the homelab change-prompt, deploy-ordering prose, transport-agnostic endpoint notes, the configuration.md knob table — see Manual-Only.*

---

## Validation Audit 2026-06-28

| Metric | Count |
|--------|-------|
| Net-new-code behaviors | 7 |
| Covered (automated) | 7 |
| Missing (automated gaps) | 0 |
| Manual-only (inherently un-automatable) | 2 — live-cluster apply, e2e revert |

No automated gaps found — every samplable net-new behavior has a green test (no auditor spawn needed).
Full suite **2499 passed** against the ephemeral Postgres+Redis (`just integration-test`). The two
manual-only items require an operator-owned Kueue cluster / a running deployment and cannot run in CI.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Runbook manifests apply cleanly on a live Kueue cluster | KDEPLOY-01 | No live cluster in CI (operator-owned) | Doc-checklist smoke test in `docs/k8s-burst.md`: `kubectl apply -f` the manifests on the operator's cluster, confirm ResourceFlavor/ClusterQueue/LocalQueue admit and the SA can submit a test Job. |
| Single-toggle revert to all-local/A1 end-to-end | KDEPLOY-05 | Requires running control plane + redeploy | Set `PHAZE_CLOUD_TARGET=local` (or `a1`), redeploy, confirm long files route off the k8s path with no other change. |

---

## Validation Sign-Off

- [x] All net-new-code tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive code tasks without automated verify
- [x] Wave 0 covers all MISSING references (new test files above)
- [x] No watch-mode flags
- [x] Feedback latency < 120s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** verified 2026-06-28 — all 7 net-new-code behaviors automated; 2 inherently-manual items remain in the Manual-Only table.
