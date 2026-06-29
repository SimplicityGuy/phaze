---
phase: 56-deployment-runbook-config-docs
plan: 01
subsystem: infra
tags: [kubernetes, kueue, kr8s, redis, saq, reachability-probe, observability]

# Dependency graph
requires:
  - phase: 56-00
    provides: RED tests for kube_staging get_local_queue, controller-startup probe, and the degrade-safe pipeline read
  - phase: 54-kube-submit-watch-reconcile-cron
    provides: kube_staging.py kr8s seam (_kube_config, _api, get_job, get_workload_for/new_class)
  - phase: 55-routing-state-ledger-integration-the-live-seam
    provides: cloud_target=="k8s" fail-fast config validators distinct from this runtime probe
provides:
  - get_local_queue() kr8s GET of the configured Kueue LocalQueue (raises on 404/transient)
  - Non-fatal LocalQueue reachability probe in controller.startup gated on cloud_target=="k8s" (D-05/D-06)
  - Cross-process flag phaze:k8s:localqueue_unreachable written by the controller process
  - Degrade-safe get_localqueue_unreachable(redis) reader the dashboard consumes
affects: [56-02]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Runtime reachability probe distinct from fail-fast config validators: gated + own broad try/except that never re-raises (boot resilience)"
    - "Cross-process control->api signalling via a Redis flag key, read degrade-safe (returns False, never 500s the hot 5s poll)"

key-files:
  created: []
  modified:
    - src/phaze/services/kube_staging.py
    - src/phaze/tasks/controller.py
    - src/phaze/services/pipeline.py

key-decisions:
  - "get_local_queue() raises (never swallows); the non-fatal catch lives only in the controller.startup caller (anti-pattern per 56-RESEARCH to fail-fast in the service)"
  - "Probe WARNING is a static message naming PHAZE_KUBE_LOCAL_QUEUE only — never interpolates the SA token or kube DSN (T-56-LOG / T-54-07)"
  - "cfg.cloud_target read carries # type: ignore[attr-defined] consistent with the existing ControlSettings-specific reads in controller.startup (cfg.llm_model)"

patterns-established:
  - "LocalQueue reuses kube_workload_api_version via new_class(kind=LocalQueue) — same kueue.x-k8s.io group as Workload, no new import"
  - "Degrade-safe bool reader mirrors get_inadmissible_count discipline but returns False (not {}) on None/error"

requirements-completed: [KDEPLOY-04]

# Metrics
duration: 8min
completed: 2026-06-28
---

# Phase 56 Plan 01: LocalQueue Reachability Probe Summary

**Non-fatal Kueue LocalQueue reachability probe at controller startup (kr8s GET) that writes a cross-process Redis flag, plus the degrade-safe dashboard reader — turning the 56-00 RED tests green.**

## Performance

- **Duration:** ~8 min
- **Started:** 2026-06-29T03:04:00Z
- **Completed:** 2026-06-29T03:07:00Z
- **Tasks:** 3
- **Files modified:** 3

## Accomplishments
- `get_local_queue()` in kube_staging.py: mirrors `get_job` (new_class + construct-by-name + refresh()), raises `kr8s.NotFoundError` on 404 and `kr8s.ServerError` on transient — no swallow.
- Non-fatal LocalQueue probe wired into `controller.startup`, gated on `cloud_target == "k8s"`, in its own broad try/except that never re-raises; sets `phaze:k8s:localqueue_unreachable` on failure, deletes on success.
- `get_localqueue_unreachable(redis)` degrade-safe reader in services/pipeline.py: returns False on a None handle and on any Redis error (never 500s the hot 5s `/pipeline/stats` poll).

## Task Commits

Each task was committed atomically:

1. **Task 1: get_local_queue() in kube_staging.py** - `5c179a5` (feat)
2. **Task 2: degrade-safe get_localqueue_unreachable() reader** - `54f7d65` (feat)
3. **Task 3: non-fatal LocalQueue probe in controller.startup** - `032ab80` (feat)

_TDD note: the RED tests were authored in wave 0 (56-00); this plan implemented the production code to turn them green (GREEN phase), so each task is a single feat commit._

## Files Created/Modified
- `src/phaze/services/kube_staging.py` - Added `async def get_local_queue()` after `get_job`; GETs the configured LocalQueue by name, raises on 404/transient.
- `src/phaze/tasks/controller.py` - Added `from phaze.services import kube_staging` import and the gated, non-fatal probe block after the recovery block in `startup`.
- `src/phaze/services/pipeline.py` - Added `async def get_localqueue_unreachable(redis)` near `get_inadmissible_count`; degrade-safe Redis flag read.

## Decisions Made
- Kept `get_local_queue()` swallow-free per 56-RESEARCH; the controller owns the non-fatal catch (D-05/D-06).
- Static probe WARNING names `PHAZE_KUBE_LOCAL_QUEUE`/cluster connectivity only; never interpolates token/DSN (T-56-LOG).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] mypy attr-defined on cfg.cloud_target**
- **Found during:** Task 3 (controller.startup probe)
- **Issue:** `get_settings()` is typed as `BaseSettings`, which has no `cloud_target` attribute (lives on the `ControlSettings` subclass), so `uv run mypy` failed with `[attr-defined]`.
- **Fix:** Added `# type: ignore[attr-defined]` on the gate line, consistent with the existing ControlSettings-specific reads in the same function (e.g. `cfg.llm_model` at controller.py:96).
- **Files modified:** src/phaze/tasks/controller.py
- **Verification:** `uv run mypy src/phaze/services/kube_staging.py src/phaze/tasks/controller.py src/phaze/services/pipeline.py` → Success, no issues.
- **Committed in:** 032ab80 (Task 3 commit)

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** Minimal — the type-ignore matches the established discipline for ControlSettings-typed reads. No scope creep.

## Issues Encountered
None — planned work proceeded as written.

## User Setup Required
None — no external service configuration required.

## Next Phase Readiness
- The read service `get_localqueue_unreachable()` is in place; **56-02** will wire it into both `/pipeline/` render paths and the new `localqueue_card.html` partial (the three render/OOB cases in `tests/test_routers/test_pipeline_localqueue.py` remain RED by design until then).
- mypy clean on all three modified modules; pre-commit hooks passed on every task commit.

## Verification
- `uv run pytest tests/test_services/test_kube_staging.py tests/test_tasks/test_controller_startup_localqueue.py` → 27 passed.
- `uv run pytest tests/test_routers/test_pipeline_localqueue.py -k degrades_to_false` → 1 passed.
- The render/OOB cases (`alert_empty_when_reachable`, `alert_renders_when_flagged`, `alert_oob_on_stats`) stay RED — owned by 56-02 (router wiring + template), exactly as the plan's `<verification>` states.
- `uv run mypy` clean on kube_staging.py, controller.py, pipeline.py.

## Self-Check: PASSED

---
*Phase: 56-deployment-runbook-config-docs*
*Completed: 2026-06-28*
