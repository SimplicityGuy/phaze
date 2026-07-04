---
phase: 70-multi-kueue-n-clusters
plan: 03
subsystem: infra
tags: [kr8s, kubeconfig, kueue, multi-cluster, backends, reconcile, controller, mkue-01]

# Dependency graph
requires:
  - phase: 70-multi-kueue-n-clusters
    plan: 01
    provides: "KubeConfig.context field + PyYAML declared (the kubeconfig+context auth path)"
  - phase: 70-multi-kueue-n-clusters
    plan: 02
    provides: "bucket-parameterized s3_staging + active_bucket retired (the sibling module-global lift)"
  - phase: 67-backend-registry
    provides: "KubeConfig / KueueBackend discriminated-union submodels in config_backends.py"
provides:
  - "per-backend kr8s client from a synthesized in-memory kubeconfig dict (both auth forms); the api.auth.token/_create_session hack is retired (D-04)"
  - "kube_staging verbs are kube: KubeConfig parameterized; the module-global active_kube read (_kube_config) is gone (D-04)"
  - "submit_cloud_job + reconcile resolve THIS file's backend kube via cloud_job.backend_id -> the registry KueueBackend.kube (MKUE-01)"
  - "controller startup probes EACH configured Kueue cluster's LocalQueue (flag set iff ANY unreachable); boot never aborts (MKUE-03/D-05)"
  - "resolved_non_local_kind is N-Kueue-safe (any-kueue -> \"kueue\"); active_compute_scratch_dir re-based on single-compute so local + N-Kueue + 1-compute no longer 500s /pushed or the S3-upload-complete callback (Pitfall 1 + sibling)"
affects: [70-04, 70-05, multi-kueue, kube-staging, reconcile, backends, controller, agent-callbacks]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Lift-a-global-to-a-parameter refactor: a module-global config read (active_kube) becomes an explicit per-call kube: KubeConfig, resolved authoritatively from the file's recorded backend_id"
    - "Constructor-time kube auth from a synthesized in-memory kubeconfig dict (both kubeconfig+context and api_url+sa_token forms); distinct dicts key distinct cached kr8s clients (no post-construction token mutation)"
    - "Single-<kind> reduction with fail-fast-on->1: active_compute_scratch_dir (compute-only) and resolved_non_local_kind (any-kueue wins, compute-only >1 raises) mirror each other so a 2nd non-local backend never 500s an unguarded call site"

key-files:
  created: []
  modified:
    - src/phaze/services/kube_staging.py
    - src/phaze/services/backends.py
    - src/phaze/tasks/submit_cloud_job.py
    - src/phaze/tasks/reconcile_cloud_jobs.py
    - src/phaze/tasks/controller.py
    - src/phaze/config.py
    - pyproject.toml
    - tests/analyze/services/test_kube_staging.py
    - tests/analyze/services/test_backends.py
    - tests/analyze/tasks/test_submit_cloud_job.py
    - tests/analyze/tasks/test_reconcile_cloud_jobs.py
    - tests/agents/routers/test_agent_s3.py
    - tests/shared/routers/test_pipeline.py
    - tests/agents/routers/test_agent_push.py
    - tests/shared/config/test_bucket_registry.py
    - tests/shared/tasks/test_controller_startup_localqueue.py
    - tests/analyze/core/test_dispatch_snapshot.py
    - tests/analyze/core/test_staging_cron.py

key-decisions:
  - "The kube-threading (Tasks 1+2) + active_kube retirement (Task 3) form ONE mutual-arity mypy unit (changing a kube_staging signature breaks every caller); source landed in one atomic feat commit, tests split by task. The Task-1 kube-auth RED test committed first for TDD."
  - "The guard moved off the retired _kube_config into _require_kube: auth needs EITHER kubeconfig OR api_url; namespace + local_queue are phaze-level (used in every manifest/probe regardless of auth form)."
  - "resolved_non_local_kind generalized to any-kueue -> \"kueue\" rather than adding per-site try/except at report_uploaded / build_dashboard_context / backfill — the shared-helper fix is the single correct seam (scattering guards would mask a genuinely-ambiguous config)."
  - "The reconcile at-cap commit-then-delete ordering is UNCHANGED (only kube-threaded); the clean-before-flip reorder is Plan 05 per the 70-02 handoff."

patterns-established:
  - "kube_staging is the single home of the per-backend kr8s client factory (_kubeconfig_dict_from + _api(kube)); the caller resolves the KubeConfig, the module stays ORM-free"
  - "A submit/reconcile resolves its target cluster from the authoritative cloud_job.backend_id (stamped at dispatch), never a module-global — a submit with no owning kueue backend fails loud (KubeStagingError)"

requirements-completed: [MKUE-01]

# Metrics
duration: 39min
completed: 2026-07-04
---

# Phase 70 Plan 03: N Concurrently-Dispatched Kueue Clusters Summary

**One control plane dispatching to N Kueue clusters: each backend now builds a DISTINCT constructor-time-authed kr8s client from its own `KubeConfig` (threaded through every `kube_staging` verb), `submit_cloud_job`/reconcile resolve the file's cluster via `cloud_job.backend_id`, the controller probes every cluster, and the two single-non-local companion reductions (`resolved_non_local_kind`, `active_compute_scratch_dir`) are made N-Kueue-safe — the fragile `api.auth.token`/`_create_session` hack and the `active_kube` module-global are gone.**

## Performance

- **Duration:** ~39 min
- **Started:** 2026-07-04T19:41:00Z
- **Completed:** 2026-07-04T20:20:00Z
- **Tasks:** 3
- **Files modified:** 18 (7 source, 11 test) + pyproject.toml

## Accomplishments

- **Task 1 (D-04, MKUE-01):** retired `_kube_config()`/`active_kube` and the post-construction `api.auth.token = token; await api._create_session()` hack. Added `_kubeconfig_dict_from` (parse inline kubeconfig YAML for the `kubeconfig`+`context` form, or synthesize a minimal dict from `api_url`+`sa_token`+`namespace`) and a constructor-time `_api(kube)` calling `kr8s.asyncio.api(kubeconfig=<dict>, namespace=, context=)` (verified live against kr8s 0.20.15: `KubeAuth` loads server+token+namespace from the dict with no network, and distinct dicts key distinct cached clients). Every verb (`submit_job`/`get_job`/`get_local_queue`/`list_inflight_jobs`/`get_workload_for`/`delete_job`) takes `kube: KubeConfig`; the fail-loud guard moved to `_require_kube`. Module stays ORM-free.
- **Task 2 (MKUE-01/03):** `KueueBackend._kube()` resolves `self.config.kube`; `is_available` + `reconcile` thread it into `get_local_queue` / `_reconcile_one` (→ `_record_success`/`_handle_no_callback_terminal`/`_job_gone`). `submit_cloud_job` reads `cloud_job.backend_id`, resolves the matching registry `KueueBackend.kube`, and threads it into the POST (fail-loud `KubeStagingError` when no owning backend resolves). `controller.startup` probes EACH configured Kueue cluster's LocalQueue (flag set iff ANY unreachable), each probe + Redis write individually guarded (D-05 boot invariant preserved). `resolved_non_local_kind` generalized to return `"kueue"` when any non-local backend is kueue — so `report_uploaded`, `build_dashboard_context`, and the backfill route no longer 500 the moment a 2nd Kueue backend is declared (the literal MKUE-01 scenario), keeping the fail-fast only for the ambiguous compute-only `>1` case.
- **Task 3 (Pitfall 1):** retired `active_kube` + `_single_non_local`; re-based `active_compute_scratch_dir` on a single-COMPUTE reduction — so a `local + N-Kueue + 1-compute` registry resolves the compute scratch_dir cleanly instead of routing through the raising `_single_non_local` and 500ing the `/pushed` callback.

## Task Commits

1. **Task 1 (RED):** `8b4a8f2` (test — per-backend kube auth coverage, verified failing pre-impl)
2. **Tasks 1–3 source (GREEN):** `6f57287` (feat — the mutual-arity kube-threading + active_kube/token-hack retirement + N-kueue-safe helper + scratch_dir re-base)
3. **Tasks 2–3 tests:** `60a0eee` (test — N-Kueue threading + companion-reduction coverage)
4. **Docstring cleanup:** `1611967` (docs — drop the stale `_kube_config` token so the acceptance grep is exactly 0)

_TDD note: the Task-1 kube-auth test landed RED first (`8b4a8f2`), then GREEN in the source commit (`6f57287`)._

## Files Created/Modified

**Source:**
- `src/phaze/services/kube_staging.py` — `_require_kube` + `_kubeconfig_dict_from` + constructor-time `_api(kube)`; every verb `kube: KubeConfig`-parameterized; token hack + `_kube_config` gone; ORM-free preserved.
- `src/phaze/services/backends.py` — `KueueBackend._kube()`; `is_available`/`reconcile` thread it; `resolved_non_local_kind` generalized (any-kueue → `"kueue"`, compute-only `>1` fail-fast retained).
- `src/phaze/tasks/submit_cloud_job.py` — `_resolve_backend_kube(settings, backend_id)`; reads `cloud_job.backend_id` before the POST, threads the resolved `KubeConfig` into `submit_job` (reordered: DB read precedes the POST).
- `src/phaze/tasks/reconcile_cloud_jobs.py` — `kube` threaded through `_reconcile_one`/`_record_success`/`_handle_no_callback_terminal`/`_job_gone` (commit-then-delete ordering unchanged; reorder is Plan 05).
- `src/phaze/tasks/controller.py` — per-cluster LocalQueue probe loop over `control_cfg.backends` filtered to `kind == "kueue"` (was a single global `resolved_non_local_kind`-gated probe); dropped the now-unused `resolved_non_local_kind` import.
- `src/phaze/config.py` — `active_kube` + `_single_non_local` removed; `active_compute_scratch_dir` on a single-compute reduction; dropped the now-unused `TYPE_CHECKING`/`KubeConfig` import.
- `pyproject.toml` — `yaml` mypy override (PyYAML ships no bundled stubs).

**Tests:** `test_kube_staging.py` (rewritten: pass `KubeConfig` directly, both auth forms, distinct clients, no-token-hack), `test_backends.py` (`_kueue` carries `[kube]`; any-kueue + compute-only-raise + scratch_dir cases), `test_submit_cloud_job.py` (spy takes `kube`, seed `backend_id` + settings stub, resolver cases), `test_reconcile_cloud_jobs.py` (seam spies accept `kube`; backend stubs carry `.kube`), `test_agent_s3.py` (2-kueue `report_uploaded`), `test_pipeline.py` (2-kueue `cloud_lane_kind`), `test_agent_push.py` (`/pushed` under local + 2 kueue + 1 compute), `test_bucket_registry.py`, `test_controller_startup_localqueue.py`, `test_dispatch_snapshot.py`, `test_staging_cron.py`.

## Deviations from Plan

### Structural

**1. Tasks 1–3 source landed as ONE atomic feat commit (mutual-arity mypy unit).**
Changing a `kube_staging` verb signature (Task 1) breaks every caller in `backends.py`/`submit_cloud_job.py`/`reconcile_cloud_jobs.py`/`controller.py` (Task 2) and `config.py`'s `active_kube` removal (Task 3) at `uv run mypy .` time. The global mypy pre-commit hook rejects any intermediate committed source state, and CLAUDE.md forbids `--no-verify`, so per-task source splitting is infeasible. Mirrors Plan 02's precedent. Tests (mypy-excluded) split per task; the Task-1 kube-auth RED test committed first to honor TDD.

### Rule 3 — auto-fixed blocking issues (test files outside the plan's list broke on the signature/behavior change)

**2. [Rule 3 - Blocking] `test_controller_startup_localqueue.py` drove the OLD single-cluster probe.**
Not in the plan's `files_modified`, but the rewired per-cluster probe changed its behavior: the kueue stub needed a `.kube`, and `test_multi_backend_registry_does_not_abort_boot` (which asserted the probe was SKIPPED on a >1-non-local registry via the old accessor raise) was rewritten to `test_multi_kueue_registry_probes_every_cluster` (both clusters probed, `await_count == 2`, no abort).

**3. [Rule 3 - Blocking] `test_dispatch_snapshot.py` + `test_staging_cron.py` kueue stubs lacked `.kube`.**
`KueueBackend.is_available` now threads `self.config.kube`; the `_StubCfg` kueue backend entries needed a minimal `kube` so `is_available` reaches the (stubbed) `get_local_queue` instead of degrading to False. Caught by the full-suite run.

**4. [Rule 3 - Blocking] `test_bucket_registry.py` referenced the retired `active_kube`.**
Updated the two `settings.active_kube` reads to resolve the kueue backend's `kube` off `settings.backends`; `test_multiple_non_local_backends_accessor_raises` (2 compute → raise) renamed and its `match` updated from `Phase 69` to `PROV-01` (the re-based `active_compute_scratch_dir` message).

**5. [Rule 3 - Blocking] Added a `/pushed` regression to `test_agent_push.py` (not in the plan's file list).**
The Task-3 verify command runs `tests/agents/routers/test_agent_push.py`; added `test_pushed_scratch_path_resolves_under_local_2kueue_1compute` to prove the Pitfall-1 fix end-to-end (no 500).

### Tooling

**6. [Rule 3 - Blocking] `yaml` had no type stubs.**
`kube_staging` now imports `yaml` (parse inline kubeconfig). Rather than install `types-PyYAML` (a package install — excluded from auto-fix, and PyYAML ships no bundled stubs), added a `[[tool.mypy.overrides]]` `ignore_missing_imports` for `yaml`, matching the existing aioboto3/essentia/mutagen pattern. Also cast the in-memory dict past kr8s's narrow `kubeconfig: str | None` stub (kr8s accepts a dict at runtime).

## Authentication Gates

None.

## Known Stubs

None — every kube call resolves the file's own backend `KubeConfig`; no placeholder/global-fallback path remains in the runtime.

## Threat Flags

None — no new network endpoints, auth paths, or trust-boundary schema beyond the plan's `<threat_model>`. The register mitigations are honored: the synthesized kubeconfig dict is in-memory only and never logged (`SecretStr` throughout, T-70-01); `_api` always passes an explicit `kubeconfig=`/`context=` and never the no-arg cache fallback (T-70-01-02); TLS is left to kr8s/httpx with no `insecure-skip-tls-verify` (T-70-01-03, deployment-gated); `active_compute_scratch_dir` re-based closes the `/pushed` DoS (T-70-01-04); `resolved_non_local_kind` generalized closes the `report_uploaded`/dashboard/backfill DoS under ≥2 Kueue backends (T-70-01-05); each per-cluster probe + Redis write stays in its own try/except so one unreachable cluster never aborts boot (T-70-03-01).

## Verification

- `uv run pytest tests/analyze tests/agents tests/shared -q` → **1642 passed** (after the Rule-3 fixes; the initial full run surfaced 5 kube-stub gaps + 1 environmental migration-DB miss, all resolved). Test DB on `localhost:5433`; migration tests need `MIGRATIONS_TEST_DATABASE_URL` (the one non-code miss).
- Task acceptance greps: `_create_session`/`api.auth.token` count `== 0`; non-comment `_kube_config` count `== 0`; `kube: KubeConfig` + `kr8s.asyncio.api(kubeconfig=` present; `def active_kube`/`def _single_non_local` count `== 0`; `active_compute_scratch_dir` reduces over `"compute"`; `is_available` threads `self._kube()`; submit reads `backend_id`.
- Purity: `grep -L "import phaze.models" src/phaze/services/kube_staging.py` matches → ORM-free.
- `uv run ruff check .` → All checks passed; `uv run mypy .` → no issues in 192 source files. Every commit passed the full pre-commit hook suite (ruff, ruff-format, bandit, mypy) with no `--no-verify`.

## Self-Check: PASSED

`70-03-SUMMARY.md` exists on disk; all four commits (`8b4a8f2` test-RED, `6f57287` feat, `60a0eee` test, `1611967` docs) are present in git history.

---
*Phase: 70-multi-kueue-n-clusters*
*Completed: 2026-07-04*
