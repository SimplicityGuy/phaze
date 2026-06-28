---
phase: 54-kube-submit-watch-reconcile-cron
plan: 03
subsystem: infra
tags: [kr8s, kubernetes, kueue, respx, pytest, seam, batch-job]

# Dependency graph
requires:
  - phase: 54-01
    provides: ControlSettings kube_* surface (kube_api_url/namespace/local_queue/job_image/cpu_request/memory_request/workload_api_version + kube_kubeconfig/kube_sa_token SecretStr via SECRET_FILE_FIELDS) and cloud_submit_max_attempts
  - phase: 53-s3-object-staging-leg
    provides: s3_staging.py seam pattern (pure SDK, file_id-keyed, idempotent-delete idiom) that kube_staging.py mirrors structurally
provides:
  - "kube_staging.py — the pure kr8s seam: build_job_manifest (suspended batch/v1 Job), submit_job (409-idempotent), get_job, list_inflight_jobs (deferred orphan-sweep), get_workload_for (job-uid label + owner-ref fallback), delete_job (404-idempotent), KubeStagingError, _kube_config gate, _api client factory"
  - "tests/kube_fakes.py — Layer-1 fake_workload/fake_job factories + PENDING/INADMISSIBLE/ADMITTED/EVICTED/QUOTA_RESERVED canned Kueue condition tuples"
  - "kube_respx conftest fixture — stubs kr8s discovery endpoints (/version,/api,/apis) for HTTP-level seam tests"
affects: [54-04, 54-05, 54-06, 55, 56, submit_cloud_job, reconcile_cloud_jobs]

# Tech tracking
tech-stack:
  added: [kr8s>=0.20.15 (control-plane kube client; installed Wave 1)]
  patterns:
    - "Pure kr8s seam mirroring s3_staging: __future__ annotations + TYPE_CHECKING guard + fail-loud custom error + _kube_config validation gate + async _api factory + idempotent-verb-per-function, NO ORM imports"
    - "Two-layer fake-kube testing: Layer-1 SimpleNamespace fakes (kube_fakes) for logic + Layer-2 respx discovery-stub fixture (kube_respx) for the seam's real kr8s HTTP calls"

key-files:
  created:
    - src/phaze/services/kube_staging.py
    - tests/kube_fakes.py
    - tests/test_services/test_kube_staging.py
  modified:
    - tests/conftest.py

key-decisions:
  - "Seam verbs pass kr8s CLASS objects (Job / new_class Workload) not string kinds, so kr8s never triggers /api,/apis,/api/v1 resource discovery — only GET /version is hit; the kube_respx fixture stubs all discovery endpoints defensively anyway (Pitfall 5)"
  - "kr8s appends a trailing slash to discovery endpoints (GET /version/) — kube_respx stubs use a trailing-slash-tolerant regex so the fixture is robust"
  - "get_workload_for falls back to an ownerReference.uid==job_uid scan when the kueue.x-k8s.io/job-uid label lookup misses (A2 de-risk); returns None only when BOTH miss"
  - "_api auth uses url + optional SA-token-on-auth.token; the exact kr8s auth/constructor form is deferred to Phase 56 live-cluster verification (RESEARCH Q3)"

patterns-established:
  - "kube_staging seam is the single home of every kr8s call (mirror s3_staging) — reconcile/submit tasks (Plans 05/06) monkeypatch it with kube_fakes for HTTP-free state-machine coverage"
  - "Import-boundary purity test reads the module source and asserts no sqlalchemy/phaze.models imports (mirror s3_staging)"

requirements-completed: [KSUBMIT-01, KSUBMIT-05, KSUBMIT-06]

# Metrics
duration: ~35min
completed: 2026-06-28
---

# Phase 54 Plan 03: Pure kr8s Kube-Staging Seam Summary

**A pure kr8s seam (no ORM) emitting the suspended batch/v1 Job manifest plus submit/list/get/get-workload/delete verbs with 409-idempotent create and 404-idempotent delete, backed by the shared fake-kube substrate (SimpleNamespace factories + a respx discovery-stub fixture).**

## Performance

- **Duration:** ~35 min
- **Completed:** 2026-06-28
- **Tasks:** 2
- **Files created:** 3
- **Files modified:** 1

## Accomplishments
- `build_job_manifest` produces the exact KSUBMIT-01/05 suspended Job: `suspend:true`, `parallelism/completions:1`, `backoffLimit:0`, `ttlSecondsAfterFinished:900` (module constant `JOB_TTL_SECONDS`), `restartPolicy:Never`, the `kueue.x-k8s.io/queue-name` label ON the Job, requests-only resources (NO limits), deterministic `phaze-analyze-<file_id>` name.
- `submit_job` is 409-idempotent (a duplicate submit refreshes the existing Job instead of raising); `delete_job` swallows 404/NotFound; non-409/non-404 errors surface as `KubeStagingError`.
- `get_workload_for` resolves the Kueue Workload via the job-uid label selector AND degrades to an owner-reference scan on a label miss (A2 de-risk) — tested across label-hit / owner-ref-fallback / both-miss.
- `list_inflight_jobs` is built + respx-tested but carries a docstring marking it the deliberately-deferred (uninvoked-this-phase) orphan-Job sweep, so the unused export isn't read as dead code.
- Shared fake-kube substrate (`kube_fakes` factories + `kube_respx` fixture) is in place for the Layer-1 logic tests in Plans 05/06.

## Task Commits

1. **Task 1: Shared fake-kube test substrate (kube_fakes + kube_respx fixture)** - `3c044c5` (test)
2. **Task 2: kube_staging.py pure kr8s seam** - `2b5488b` (feat — TDD RED→GREEN executed in-session; combined test+impl commit after a pre-commit ruff-format reflow)

## Files Created/Modified
- `src/phaze/services/kube_staging.py` (created) - The pure kr8s seam: manifest builder + idempotent submit/list/get/get-workload/delete verbs, `KubeStagingError`, `_kube_config` gate, `_api` factory. 234 lines, no ORM imports.
- `tests/kube_fakes.py` (created) - `fake_workload(*conditions, owner_uid=...)` / `fake_job(succeeded,failed,suspend)` factories + canned Kueue condition constants for Layer-1 logic tests.
- `tests/test_services/test_kube_staging.py` (created) - 17 tests: manifest spec, config gate, submit 201/409/non-409, get, list-by-label, deferred-docstring, get_workload_for 3 paths, delete 200/404, import-boundary purity.
- `tests/conftest.py` (modified, additive) - Appended the `kube_respx` fixture stubbing kr8s discovery endpoints (trailing-slash-tolerant regex) and `KUBE_TEST_API_URL`.

## Decisions Made
- **Class-object verbs over string kinds:** every seam call uses a kr8s class (`Job`, `new_class("Workload")`) so kr8s skips resource discovery — only `GET /version` is exercised at runtime; the fixture still stubs `/api`,`/apis`,`/api/v1` defensively per RESEARCH Pitfall 5.
- **Trailing-slash discovery stubs:** kr8s requests `GET /version/` (trailing slash); the fixture matches discovery endpoints with a `^…/version/?$`-style regex.
- **Owner-ref fallback for Workload resolution:** the exact `kueue.x-k8s.io/job-uid` label key is a Phase-56 verification item, so `get_workload_for` falls back to an `ownerReferences[*].uid == job_uid` scan (A2 de-risk), returning `None` only when both lookups miss.
- **Auth form deferred:** `_api` builds the client from `url` + namespace and sets an optional SA token on `api.auth.token`; the precise kr8s auth/constructor ergonomics are deferred to Phase 56 (RESEARCH Q3).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] mypy union-attr on the Workload owner-ref scan**
- **Found during:** Task 2 (kube_staging.py)
- **Issue:** `workload_cls.list(...)` is typed as yielding `APIObject | dict`, so `wl.metadata` failed mypy `union-attr` (a `dict` has no `.metadata`). At runtime `raw=False` always yields APIObject instances.
- **Fix:** Narrowed the loop variable with `cast("Any", wl)` before attribute access (consistent with the module's existing `cast`/`Any` usage and s3_staging's `cast("ControlSettings", …)` idiom).
- **Files modified:** src/phaze/services/kube_staging.py
- **Verification:** `uv run mypy src/phaze/services/kube_staging.py` → Success; 17 tests still green.
- **Committed in:** 2b5488b (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 blocking type error)
**Impact on plan:** Necessary for the mypy pre-commit gate. No behavioral change, no scope creep.

## Issues Encountered
- **Worktree base drift:** the worktree spawned at HEAD `82bdfc7` (Phase 53 merge) which predates the Phase 54 plan files; the `<worktree_branch_check>` correctly directed a `git reset --hard` to the intended base `76c3260` (a fast-forward, since `82bdfc7` is its ancestor), after which `54-03-PLAN.md` was present. Resolved before any task work.
- **Pre-commit ruff-format reflow:** the first Task-2 commit aborted because `ruff-format` reflowed two files; re-staged the formatted files and re-committed (no logic change).

## Threat Surface Scan
No new security-relevant surface beyond the plan's `<threat_model>`. The seam mitigates T-54-06 (deterministic UUID-derived Job name, no operator free-text), T-54-07 (`_api` builds creds from `_FILE`-resolved SecretStr and never logs the token), T-54-08 (`backoffLimit:0` in the manifest), and T-54-09 (404-idempotent `delete_job`).

## Known Stubs
None — `list_inflight_jobs` is intentionally uninvoked this phase (documented in-code as a reserved orphan-Job sweep per D-02) and is fully implemented + respx-tested, not a stub.

## User Setup Required
None - no external service configuration required (kube creds/RBAC wiring is Phase 56).

## Next Phase Readiness
- The seam + fake-kube substrate are ready for Plan 05 (`submit_cloud_job` task) and Plan 06 (`reconcile_cloud_jobs` cron), which monkeypatch `kube_staging` with `kube_fakes` for HTTP-free state-machine coverage.
- `get_workload_for`'s exact live label key and `_api`'s auth/constructor form remain explicit Phase-56 live-cluster verification items (A2 / Q3), de-risked here by the owner-ref fallback and the monkeypatched-seam test strategy.

## Self-Check: PASSED

- Files verified present: kube_staging.py, kube_fakes.py, test_kube_staging.py, 54-03-SUMMARY.md
- Commits verified: `3c044c5` (Task 1, test), `2b5488b` (Task 2, feat)

---
*Phase: 54-kube-submit-watch-reconcile-cron*
*Completed: 2026-06-28*
