---
phase: 26-task-code-reorg-http-backed-agent-worker
plan: 09
subsystem: infra
tags: [python, saq, controller, task-queue, role-split, ops-banner]

# Dependency graph
requires:
  - phase: 26-task-code-reorg-http-backed-agent-worker (Plan 01)
    provides: ControlSettings, AgentSettings, get_settings() factory, Role enum (D-14)
  - phase: 25-internal-agent-http-api-bearer-auth
    provides: agent-internal HTTP routers (no direct usage in this plan, contextual)
provides:
  - "phaze.tasks.controller -- SAQ settings module for the control/application-server role"
  - "Module-level Queue named 'controller' constructed at import time for `saq <module>.settings` CLI"
  - "Fileless-only functions list: generate_proposals, match_tracklist_to_discogs, search_tracklist, scrape_and_store_tracklist"
  - "refresh_tracklists CronJob (1st of each month at 03:00)"
  - "Startup banner log line carrying role=control queue=controller (OPS-01 evidence)"
  - "W4 invariant: startup stashes ctx['queue'] for proposal/execution rate-limit readers"
  - "Plan 10's symmetric agent_worker has a clean control-side partner to mirror"
affects:
  - 26-10 (agent_worker.py mirrors this module's shape minus DB engine + LLM)
  - 26-11 (file-bound task rewrites must NOT be imported by controller.py)
  - 26-13 (docker-compose worker.command -> `uv run saq phaze.tasks.controller.settings`; deletes legacy worker.py)
  - 27 (watcher service enqueues via AgentTaskRouter, controller consumes nothing extra)
  - 29 (deployment hardening; agents admin page)

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "SAQ settings module per role (controller vs agent_worker) selected by PHAZE_ROLE"
    - "Module-level Queue.from_url(name=...) for CLI consumption (saq imports module + reads `settings`)"
    - "Startup banner with role+queue identifiers for ops-grep auditability (OPS-01)"
    - "ctx['queue'] = queue stash so per-task rate-limit cache readers find the Queue (W4)"
    - "monkeypatch + MagicMock for startup-hook tests; no Postgres/HTTP needed"

key-files:
  created:
    - "src/phaze/tasks/controller.py (117 lines) -- SAQ settings module, control role"
    - "tests/test_tasks/test_controller_startup_banner.py (49 lines) -- W2/OPS-01 banner coverage"
  modified: []

key-decisions:
  - "D-01..D-04 honored: module named 'controller' (no _worker suffix), fileless functions only, no cross-imports of file-bound modules"
  - "D-14 honored: imports from phaze.config.get_settings (factory); does NOT touch module-level `settings` singleton"
  - "W4 invariant landed: ctx['queue'] = queue is set in startup (read by proposal.py:66 and execution.py:33)"
  - "Legacy worker.py left in place this plan -- Plan 26-13 deletes it after docker-compose flip"

patterns-established:
  - "Role-specific SAQ settings module: each role declares its own queue name, function list, cron jobs, startup/shutdown hooks"
  - "Banner log convention: `phaze.<module> startup role=<role> queue=<queue_name> redis=<url>` -- greppable across operator logs"
  - "Startup-hook test: monkeypatch heavy constructors (engine/client/service), assert log+ctx-stash invariants via caplog"

requirements-completed: [TASK-01, OPS-01]

# Metrics
duration: 4m 5s
completed: 2026-05-12
---

# Phase 26 Plan 09: Controller SAQ Settings Module Summary

**SAQ settings module `phaze.tasks.controller` for the control/application-server role -- fileless tasks only (generate_proposals, match_tracklist_to_discogs, search_tracklist + scrape_and_store_tracklist, refresh_tracklists cron), greppable role banner, and a W2 startup-banner test.**

## Performance

- **Duration:** 4m 5s
- **Started:** 2026-05-12T22:10:10Z
- **Completed:** 2026-05-12T22:14:15Z
- **Tasks:** 2
- **Files modified:** 0
- **Files created:** 2

## Accomplishments

- `src/phaze/tasks/controller.py` exists with `settings: dict` consumable by `saq phaze.tasks.controller.settings`
- Module-level `queue = Queue.from_url(get_settings().redis_url, name="controller")` constructed at import time
- `settings["functions"]` is exactly the four fileless tasks (validated by smoke test + plan acceptance grep)
- `settings["cron_jobs"]` contains `CronJob(refresh_tracklists, cron="0 3 1 * *")`
- Async `startup`/`shutdown` hooks construct/dispose: engine pool (size=10, overflow=5), `async_sessionmaker`, `DiscogsographyClient`, `ProposalService`
- Startup hook stashes `ctx["queue"] = queue` so `phaze.tasks.proposal.generate_proposals` and `phaze.tasks.execution.execute_approved_batch` find the Queue handle for their Redis rate-limit/cache reads (W4 invariant)
- Startup emits the role banner: `phaze.controller startup role=control queue=controller redis=<url>` (OPS-01 evidence)
- ZERO imports of file-bound modules (verified by grep: 0 matches for `from phaze.tasks.(functions|execution|fingerprint|metadata_extraction|scan|pool) import`)
- ZERO imports of agent-side services (verified by grep: 0 matches for `from phaze.services.(fingerprint|agent_client) import`)
- New banner test `tests/test_tasks/test_controller_startup_banner.py` passes in <1.1s (1 passed, --no-cov)

## Task Commits

Each task was committed atomically:

1. **Task 1: Create phaze.tasks.controller -- SAQ settings for control role** -- `555a718` (feat)
2. **Task 2: Controller startup banner test (W2 / OPS-01 coverage)** -- `8cffb47` (test)

_Note: Plan 26-09 frontmatter marks Task 2 as `tdd="true"` but the implementation (Task 1) was committed before the test (Task 2). See "TDD Gate Compliance" below for the rationale._

## Files Created/Modified

- `src/phaze/tasks/controller.py` (117 lines, created) -- SAQ settings module for the control role
- `tests/test_tasks/test_controller_startup_banner.py` (49 lines, created) -- W2/OPS-01 banner coverage test

## Decisions Made

None of note beyond strict adherence to D-01..D-04, D-14, and W2/W4 from the plan. The module is a verbatim faithful subset of `phaze.tasks.worker.py` minus file-bound concerns; no deviations from the plan body were required.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Underscore-prefix unused lambda arguments in banner test**
- **Found during:** Task 2 (Controller startup banner test)
- **Issue:** Plan's verbatim test body uses `lambda *a, **kw: MagicMock()` inside `monkeypatch.setattr` calls. The project's ruff config enables `ARG` rules with no per-file ignore on `tests/` for `ARG005`, so the five lambda lines failed ruff (8 errors total: `Unused lambda argument: 'a'` / `'kw'`).
- **Fix:** Renamed the unused varargs to `_a` / `_kw` (the underscore prefix convention satisfies `ARG005`). Semantically identical: lambdas still accept any positional/keyword args and return a fresh `MagicMock()`.
- **Files modified:** `tests/test_tasks/test_controller_startup_banner.py`
- **Verification:** `uv run ruff check tests/test_tasks/test_controller_startup_banner.py` -> all checks passed; test still passes (1 passed, 0.99s)
- **Committed in:** `8cffb47` (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 blocking lint)
**Impact on plan:** Pure mechanical fix to satisfy project lint config. No semantic change to the test. No scope creep.

## TDD Gate Compliance

Task 2 was tagged `tdd="true"` in the plan frontmatter, but the implementation (controller.py, Task 1) was committed before the test (Task 2). Per the executor spec's fail-fast rule, a test that passes unexpectedly during RED is a halt condition -- however, in this plan the author explicitly placed the test *after* the implementation task (Task 2 only exists to add banner-coverage to the already-shipped Task 1). The Task 2 plan body verifies this with `uv run pytest ... -x -q --no-cov` expecting 1 green test, not a failing one. The git log shows:

1. `feat(26-09): create phaze.tasks.controller ...` (555a718) -- implementation
2. `test(26-09): controller startup banner test ...` (8cffb47) -- coverage

The gate-sequence check (test before feat) is **inverted by design** for this plan. The plan-level test-after-impl pattern is acceptable here because the test exercises an OPS-01 evidence requirement (the banner string), not a feature behavior contract -- the controller would still function without the test, and the test's value is regression-protection against future banner-string drift. The next plan in the sequence (Plan 26-10) follows standard RED-GREEN for the symmetric agent_worker.

## Issues Encountered

- The worktree was created from `main` before phase 26 wave 1/2 work landed; merging `gsd/phase-26-...` into the worktree was required as a setup step so that `phaze.config.get_settings()` and the rest of the Wave 1/2 outputs (`ControlSettings`, schemas, `PhazeAgentClient`) were available for `controller.py` to import. After the merge, all imports resolved cleanly.

## User Setup Required

None -- no external service configuration required. Plan 26-13 will update `docker-compose.yml` to point at the new `controller.settings`; this plan only ships the module.

## Next Phase Readiness

- **Plan 26-10 (agent_worker.py):** This plan's `controller.py` is the structural template -- Plan 10 mirrors it under `phaze.tasks.agent_worker`, swapping fileless task list for the five file-bound tasks, removing DB engine construction, and adding `PhazeAgentClient` + `/whoami` boot sequence.
- **Plan 26-13 (docker-compose flip + legacy worker.py delete):** `controller.settings` is ready to be the `worker` service's command target; the legacy `phaze.tasks.worker.py` remains untouched here per plan scope.
- **No blockers.** mypy + ruff + format + pre-commit all green; banner test green; smoke imports green.

## Self-Check: PASSED

Verified:
- `src/phaze/tasks/controller.py` exists (117 lines)
- `tests/test_tasks/test_controller_startup_banner.py` exists (49 lines)
- Commit `555a718` found in git log
- Commit `8cffb47` found in git log
- Module imports: `phaze.tasks.controller.settings["queue"].name == "controller"` -> True
- Functions list: `['generate_proposals', 'match_tracklist_to_discogs', 'search_tracklist', 'scrape_and_store_tracklist']` -> matches expected
- Banner test: 1 passed in 0.99-1.07s
- `uv run mypy .` -> Success: no issues found in 107 source files
- `uv run ruff check .` -> All checks passed
- `uv run ruff format --check .` -> 181 files already formatted
- `pre-commit run --all-files` -> all hooks Passed

---
*Phase: 26-task-code-reorg-http-backed-agent-worker*
*Completed: 2026-05-12*
