---
phase: 56
slug: deployment-runbook-config-docs
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-06-28
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

> Placeholder rows derived from the research test-map. The planner replaces `{plan}`/`{wave}`/task IDs with the actual plan task IDs and confirms each command.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| {N}-WW-TT | {plan} | 0 | KDEPLOY-01 | T-56-RBAC | Namespaced RBAC verb set ⊇ kr8s call graph (jobs create/get/delete; workloads list/get/watch; localqueues get) | unit | `uv run pytest tests/test_deployment/test_k8s_runbook.py -x` | ❌ W0 | ⬜ pending |
| {N}-WW-TT | {plan} | 0 | KDEPLOY-01 | — | Runbook manifests parse as valid YAML | unit | `uv run pytest tests/test_deployment/test_k8s_runbook.py -k yaml -x` | ❌ W0 | ⬜ pending |
| {N}-WW-TT | {plan} | 0 | KDEPLOY-04 | T-56-DOS | `get_local_queue()` success→no flag; 404→unreachable; transient→unreachable; never raises out of startup | unit | `uv run pytest tests/test_kube_staging.py -k local_queue -x` | ❌ W0 | ⬜ pending |
| {N}-WW-TT | {plan} | 0 | KDEPLOY-04 | T-56-DOS | Probe runs only when `cloud_target=="k8s"`; broad try/except never aborts boot | unit | `uv run pytest tests/test_controller_startup.py -k localqueue_probe -x` | ❌ W0 | ⬜ pending |
| {N}-WW-TT | {plan} | 0 | KDEPLOY-04 | — | Dashboard flag read is degrade-safe (missing/error redis → silent); alert empty when reachable | unit | `uv run pytest tests/test_pipeline_dashboard.py -k localqueue_alert -x` | ❌ W0 | ⬜ pending |
| {N}-WW-TT | {plan} | 0 | KDEPLOY-04 | — | Invariant: `classify(agent, now) != "dead"` when `last_seen_at is None` (always `never`) | unit | `uv run pytest tests/test_agent_liveness.py -k never -x` | ✅ extend | ⬜ pending |
| {N}-WW-TT | {plan} | 0 | KDEPLOY-04 | — | `job_runner` never imports/calls the heartbeat loop (ephemeral pod doesn't heartbeat) | unit | `uv run pytest tests/test_task_split.py -k heartbeat -x` | ✅ extend | ⬜ pending |
| {N}-WW-TT | {plan} | — | KDEPLOY-02/03/05 | — | Docs present/linked (k8s-burst.md, configuration.md table, deployment.md revert + pointer, README index) | smoke/doc | doc-checklist (or `tests/test_docs_links.py -x` if present) | manual/doc | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_deployment/test_k8s_runbook.py` — parse runbook manifests as YAML; assert the RBAC verb set covers the kr8s call graph (KDEPLOY-01).
- [ ] `tests/kube_fakes.py` — add a `fake_local_queue(...)` helper + 404/transient seam for `get_local_queue` (mirror `fake_job`).
- [ ] `tests/test_kube_staging.py` — `get_local_queue` success / not-found / transient cases.
- [ ] `tests/test_controller_startup.py` (new or extend) — probe gating (`cloud_target=="k8s"`) + boot-resilience.
- [ ] `tests/test_pipeline_dashboard.py` — alert OOB carrier + degrade-safe flag read.
- [ ] Extend `tests/test_agent_liveness.py` (the `never`-not-`dead` invariant) + `tests/test_task_split.py` (no heartbeat in `job_runner`).

*Pure-docs (not meaningfully samplable beyond YAML-validity + link checks): the homelab change-prompt, deploy-ordering prose, transport-agnostic endpoint notes, the configuration.md knob table.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Runbook manifests apply cleanly on a live Kueue cluster | KDEPLOY-01 | No live cluster in CI (operator-owned) | Doc-checklist smoke test in `docs/k8s-burst.md`: `kubectl apply -f` the manifests on the operator's cluster, confirm ResourceFlavor/ClusterQueue/LocalQueue admit and the SA can submit a test Job. |
| Single-toggle revert to all-local/A1 end-to-end | KDEPLOY-05 | Requires running control plane + redeploy | Set `PHAZE_CLOUD_TARGET=local` (or `a1`), redeploy, confirm long files route off the k8s path with no other change. |

---

## Validation Sign-Off

- [ ] All net-new-code tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive code tasks without automated verify
- [ ] Wave 0 covers all MISSING references (new test files above)
- [ ] No watch-mode flags
- [ ] Feedback latency < 120s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
