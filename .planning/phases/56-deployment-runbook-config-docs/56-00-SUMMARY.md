---
phase: 56-deployment-runbook-config-docs
plan: "00"
subsystem: testing
tags: [nyquist-red, kube, kueue, localqueue-probe, rbac, runbook, dashboard-alert]
requires:
  - "phaze.services.kube_staging (existing kr8s seam)"
  - "tests/conftest.py kube_respx fixture + KUBE_TEST_API_URL"
  - "tests/kube_fakes.py fake_job/fake_workload factories"
provides:
  - "tests/kube_fakes.py::fake_local_queue (LocalQueue SimpleNamespace factory)"
  - "tests/test_services/test_kube_staging.py get_local_queue success/not-found/transient RED cases"
  - "tests/test_tasks/test_controller_startup_localqueue.py probe-gating + boot-resilience RED tests"
  - "tests/test_routers/test_pipeline_localqueue.py dashboard-alert + degrade-safe-read RED tests"
  - "tests/test_deployment/test_k8s_runbook.py runbook YAML-validity + RBAC-covers-call-graph RED test"
affects:
  - "56-01 (must implement kube_staging.get_local_queue + controller probe + pipeline.get_localqueue_unreachable)"
  - "56-02 (must wire flag into pipeline router + add localqueue_card.html partial)"
  - "56-04 (must write docs/k8s-burst.md with the documented Kueue/RBAC manifests)"
tech-stack:
  added: []
  patterns:
    - "Nyquist RED-first: contracts pinned as executable tests before implementation"
    - "kube_respx httpx-seam mocking for kr8s GET (localqueues path)"
    - "raising=False monkeypatch on not-yet-existing seam functions (GREEN-compatible patch targets)"
    - "redis_async.Redis.from_url override to assert on ctx['redis'] set/delete"
    - "yaml.safe_load_all over fenced ```yaml blocks for runbook manifest assertions"
key-files:
  created:
    - "tests/test_tasks/test_controller_startup_localqueue.py"
    - "tests/test_routers/test_pipeline_localqueue.py"
    - "tests/test_deployment/test_k8s_runbook.py"
  modified:
    - "tests/kube_fakes.py"
    - "tests/test_services/test_kube_staging.py"
decisions:
  - "Transient case asserts kr8s.ServerError (500) and not-found asserts kr8s.NotFoundError — both distinct from the RED-state AttributeError, so the bodies stay meaningfully RED until 56-01"
  - "Dashboard alert flag driven by patching phaze.routers.pipeline.get_localqueue_unreachable (raising=False) — the exact name the 56-02 router will import, so the same tests flip GREEN on implementation"
  - "REQUIRED_RBAC is a subset gate (Role MAY grant more, e.g. workloads get/watch) — guards the floor without over-pinning the conservative D-01 spec"
metrics:
  tasks_completed: 3
  files_changed: 5
  tests_added: 13
  completed: 2026-06-28
---

# Phase 56 Plan 00: Wave 0 RED Test Scaffolding Summary

Nyquist RED-first test scaffolding that locks every Phase 56 net-new-code contract (KDEPLOY-04 LocalQueue probe + dashboard alert) and the runbook (KDEPLOY-01) as executable assertions before any implementation exists — five test files, all RED/erroring by design until 56-01/56-02/56-04 land.

## What Was Built

**Task 1 — `fake_local_queue` helper + `get_local_queue` RED unit tests** (commit 7d84689)
- Added `fake_local_queue(name="phaze-lq", namespace="phaze")` to `tests/kube_fakes.py`, a `SimpleNamespace` factory mirroring `fake_job`/`fake_workload` (exposes `metadata.name`/`metadata.namespace` — the only fields the probe touches).
- Added three cases to `tests/test_services/test_kube_staging.py` driven by the shared `kube_respx` + `stub_cfg` fixtures against the localqueues GET path (`/apis/kueue.x-k8s.io/v1beta1/namespaces/phaze/localqueues/phaze-lq`):
  - `test_get_local_queue_success` (200 → returns the refreshed object, `lq.name == "phaze-lq"`)
  - `test_get_local_queue_not_found` (404 → `kr8s.NotFoundError`)
  - `test_get_local_queue_transient` (500 → `kr8s.ServerError`)

**Task 2 — controller-startup probe + dashboard-alert RED tests** (commit 12fb0e3)
- `tests/test_tasks/test_controller_startup_localqueue.py`: clones the banner-test monkeypatch recipe (stub heavy constructors + `get_settings`) and overrides `redis_async.Redis.from_url` so `ctx["redis"]` is an assertable `AsyncMock`:
  - `test_localqueue_probe_skipped_when_not_k8s` (`cloud_target="local"` ⇒ `get_local_queue` never called)
  - `test_localqueue_probe_sets_flag_on_failure` (probe raises ⇒ `redis.set("phaze:k8s:localqueue_unreachable", ...)` AND `startup` returns without raising — boot resilience)
  - `test_localqueue_probe_clears_flag_on_success` (probe returns ⇒ `redis.delete(<flag key>)`)
- `tests/test_routers/test_pipeline_localqueue.py`: clones the inadmissible-alert recipe; drives the flag by patching `phaze.routers.pipeline.get_localqueue_unreachable` (`raising=False`):
  - empty-when-reachable, locked-copy-when-flagged (`"K8s LocalQueue unreachable"`), stable `id="localqueue-card"` + `hx-swap-oob="true"` on the `/pipeline/stats` OOB poll
  - `test_get_localqueue_unreachable_degrades_to_false` (None handle → False; raising redis → False)

**Task 3 — runbook YAML-validity + RBAC-covers-call-graph test** (commit 9e2a066)
- `tests/test_deployment/test_k8s_runbook.py`: reads `docs/k8s-burst.md` via `pathlib`, extracts fenced ```yaml blocks, parses with `yaml.safe_load_all`:
  - `test_runbook_manifests_are_valid_yaml` (every fence parses)
  - `test_runbook_has_required_kinds` (ResourceFlavor, ClusterQueue, LocalQueue, ServiceAccount, Role, RoleBinding, Secret)
  - `test_rbac_covers_call_graph` (module-level `REQUIRED_RBAC` floor: `batch/jobs` {create,get,delete}, `kueue.x-k8s.io/workloads` ⊇ {list}, `kueue.x-k8s.io/localqueues` {get})

## Expected RED State (intended Wave 0 outcome)

| Test group | RED reason | Goes GREEN in |
|------------|-----------|---------------|
| `get_local_queue` (3) | `kube_staging.get_local_queue` not written | 56-01 |
| controller probe set/clear flag (2) | probe block not wired into `startup` | 56-01 |
| `get_localqueue_unreachable` degrade (1) | service function not written | 56-01 |
| dashboard alert render/OOB | router wiring + `localqueue_card.html` absent | 56-01/56-02 |
| runbook YAML/kinds/RBAC (3) | `docs/k8s-burst.md` not written | 56-04 |

The `skipped_when_not_k8s` and `empty_when_reachable` cases pass trivially in the RED state (vacuous — they assert absence) but pin the contract and stay GREEN through implementation.

## Verification

- `uv run pytest <all five paths> --co -q` → **34 tests collected, zero import errors** (the plan's gate).
- The RBAC parser was proven GREEN-compatible against the documented 56-RESEARCH manifest (required kinds satisfied, RBAC floor satisfied) via an inline simulation.
- `get_local_queue` cases run RED as expected (3 failed / 1 unrelated pass).
- Controller probe set/clear + degrade tests run RED (3 failed); `skipped_when_not_k8s` passes (vacuous).
- Runbook tests run RED (3 failed — `docs/k8s-burst.md` absent).
- `pre-commit` (ruff + ruff-format + bandit + mypy) passed on every commit.

## Deviations from Plan

None — plan executed exactly as written.

## Environment Notes (not deviations)

The three DB-backed dashboard-alert tests (`test_localqueue_alert_*`) ERROR at fixture setup in this sandbox because the `client` fixture needs a live Postgres (asyncpg connection refused). This is an environment limitation, not a code defect: the existing analog `test_pipeline_inadmissible.py::test_dashboard_renders_on_all_zero_path` errors identically here. Collection (the plan's verification gate) succeeds, and the tests run RED against the missing card in a DB-backed CI run.

## Known Stubs

None — these are test files only; the RED tests are the intended deliverable, not stubs.

## Notes for Implementers (56-01 / 56-02 / 56-04)

- **56-01:** `get_local_queue()` must `refresh()` so a 404 surfaces `kr8s.NotFoundError` and a 500 surfaces `kr8s.ServerError`; the controller probe must gate on `cfg.cloud_target == "k8s"`, write/clear `phaze:k8s:localqueue_unreachable` via `ctx["redis"]`, and live inside a broad try/except that never re-raises; `pipeline.get_localqueue_unreachable(redis)` must return `False` on `None`/error.
- **56-02:** the router must `from phaze.services.pipeline import get_localqueue_unreachable` and seed `localqueue_unreachable` into BOTH render contexts; the new `localqueue_card.html` partial needs a stable `id="localqueue-card"`, the locked copy `K8s LocalQueue unreachable`, amber classes, and `hx-swap-oob="true"` on the OOB re-push.
- **56-04:** `docs/k8s-burst.md` must ship the seven documented manifest kinds as fenced ```yaml blocks, and the RBAC Role must grant at least the `REQUIRED_RBAC` floor.

## Self-Check: PASSED

All five test files and the SUMMARY exist on disk; all three task commits (7d84689, 12fb0e3, 9e2a066) are present in the git history.
