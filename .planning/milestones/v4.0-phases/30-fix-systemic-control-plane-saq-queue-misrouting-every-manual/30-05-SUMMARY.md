---
phase: 30-fix-systemic-control-plane-saq-queue-misrouting-every-manual
plan: 05
subsystem: ci-guard
tags: [saq, redis, task-queue, routing, regression-guard, ast, docs]

# Dependency graph
requires:
  - phase: 30-01 (routing foundation)
    provides: "resolve_queue_for_task, CONTROLLER_TASKS, AGENT_TASKS, controller_queue, AgentTaskRouter.queue_for"
  - phase: 30-02..30-04 (per-site fixes)
    provides: "Zero app.state.queue references remain across pipeline.py / tracklists.py / scan.py / ingestion.py"
provides:
  - "tests/test_no_default_queue_producers.py: AST static guard (no *.state.queue / no unnamed Queue.from_url in routers+services) + runtime routing assertions"
  - "README 'Task Queue Routing' subsection documenting controller vs per-agent queues, active-agent selection, fail-loud, and the guard"
affects: []

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "AST-based static guard: parse each routers/services .py with ast and flag *.state.queue attribute access + unnamed Queue.from_url calls (no false positives from docstring/comment prose)"
    - "Meta-tests prove the guard visitor is not vacuously green (crafted sample asserts both offence classes are caught; a named Queue.from_url passes)"

key-files:
  created:
    - tests/test_no_default_queue_producers.py
  modified:
    - README.md

key-decisions:
  - "Used ast parsing instead of the plan's literal line/regex text scan to avoid a false positive on agent_task_router.py's module-docstring reference to Queue.from_url(...) (and to ignore comment prose); strictly more correct, same coverage"
  - "Static guard scope = src/phaze/routers + src/phaze/services only (main.py's named controller queue and the per-agent named Queue in AgentTaskRouter._queue_for are intentionally allowed)"

requirements-completed: [QR-01, QR-03]

# Metrics
duration: ~25min
completed: 2026-06-09
---

# Phase 30 Plan 05: Default-queue producer guard + routing docs Summary

**A committed static guard (`tests/test_no_default_queue_producers.py`) AST-scans `routers/` + `services/` and fails loudly with `file:line` if anyone reintroduces a `*.state.queue` reference or an unnamed `Queue.from_url(...)`, plus runtime assertions that every routable task resolves to a named queue and unknown tasks raise — and the README now documents the controller/per-agent routing model so the v4.0.6 silent-misrouting bug class cannot recur unnoticed.**

## Performance

- **Duration:** ~25 min
- **Started:** 2026-06-09
- **Completed:** 2026-06-09
- **Tasks:** 2
- **Files modified:** 2 (1 created, 1 modified)

## Accomplishments
- New guard test with three static checks: (1) a real-tree scan asserting zero default-queue producers across `routers/` + `services/`; (2) a meta-test proving the AST visitor flags both `request.app.state.queue` and an unnamed `Queue.from_url(url)` on a crafted sample (so the green scan is not vacuous); (3) a positive test asserting a *named* `Queue.from_url(..., name=...)` is allowed.
- Runtime guard: parametrized over the live `CONTROLLER_TASKS`/`AGENT_TASKS` sets — every controller task routes to `controller_queue` with `agent_id is None`, every agent task routes to `phaze-agent-<id>` for the seeded active agent, and `definitely_not_a_task` raises `ValueError("unroutable task ...")`.
- Verified the guard *would* catch a regression: a temporary `request.app.state.queue` added to `pipeline.py` made the scan fail with `src/phaze/routers/pipeline.py:417: *.state.queue attribute access`; reverted and re-confirmed green.
- README gained a "Task Queue Routing" subsection (under Architecture Overview) covering: control-plane never produces onto an unnamed default queue; the controller-bound task list -> `controller` queue (`phaze-worker`); the per-agent task list -> `phaze-agent-<id>` via `AgentTaskRouter` (`phaze-agent-worker`) with active-agent selection; fail-loud on unknown tasks / no active agent; the guard test; and an operational note that clearing stranded `saq:job:default:*` jobs is a one-time deploy step. Line-1 `generated-by` marker untouched.

## Task Commits

1. **Task 1: static + runtime guard against default-queue producers** — `26d0614` (test)
2. **Task 2: document the queue-routing model + guard in README** — `4b752c2` (docs)

_Task 1 is the TDD artifact, but it is test-only (no production source in `<files>`), so the RED/GREEN cycle collapses to a single `test(...)` commit: the guard passes immediately against the already-fixed tree (Plans 01-04), and its negative behavior was verified by a temporary edit + revert rather than a failing-then-passing source change._

## Files Created/Modified
- `tests/test_no_default_queue_producers.py` (created) — `_ProducerVisitor` (AST), `_scan_source_files`, the real-tree static guard, two meta-tests, and three runtime routing tests (controller / per-agent / unknown).
- `README.md` (modified) — new "📬 Task Queue Routing" subsection after the File Processing Pipeline diagram.

## Decisions Made
- **AST over literal text/regex scan (deviation, see below):** the plan specified a per-line comment-stripped regex (`app\.state\.queue(?![_a-zA-Z])`) plus a `Queue.from_url(` substring check. A pure-text `Queue.from_url(` check produces a false positive on `agent_task_router.py`'s module docstring, which references `` ``Queue.from_url(...)`` `` in prose (extracting between its parens yields `...`, with no `name=`). Parsing with `ast` and inspecting `Call`/`Attribute` nodes ignores all docstring/comment prose, correctly allows the multi-line *named* `Queue.from_url(self._redis_url, name=f"phaze-agent-{agent_id}")` in `_queue_for`, and still flags any genuinely reintroduced producer with its `file:line`. Strictly more correct, identical intent.
- **Scope limited to `routers/` + `services/`** per the plan: the lifespan's named `controller` queue (`main.py`) and the per-agent named queue (`AgentTaskRouter._queue_for`) are the only legitimate constructions and live outside the scanned trees / pass the `name=` check.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] AST-based static scan instead of literal text/regex scan**
- **Found during:** Task 1
- **Issue:** The plan's literal `Queue.from_url(` substring scan would flag `src/phaze/services/agent_task_router.py:3`, where the module docstring mentions `` ``Queue.from_url(...)`` `` as prose — a false positive that would make the guard fail against the correct post-fix tree.
- **Fix:** Implemented the scan with `ast.parse` + a `NodeVisitor` that inspects only real `Call`/`Attribute` nodes (`Queue.from_url` lacking a `name=` keyword; `*.state.queue` attribute access). Comments and docstrings are never code nodes, so they cannot false-positive. Same offence classes, same `file:line` reporting.
- **Files modified:** tests/test_no_default_queue_producers.py
- **Verification:** `uv run pytest tests/test_no_default_queue_producers.py -q` -> 6 passed; temporary `request.app.state.queue` regression in `pipeline.py` correctly failed the scan, then reverted.
- **Committed in:** `26d0614` (Task 1 commit)

**Total deviations:** 1 (implementation technique, not scope). No new packages, no architectural changes, no auth gates.

## Threat Register Outcomes
- **T-30-01 (silent reintroduction of a default-queue producer):** mitigated — the committed static guard fails CI on any new `*.state.queue` / unnamed `Queue.from_url` in routers/services; the runtime test asserts unknown task names raise. Verified the guard fails on a planted regression.
- **T-30-SC (package installs):** accept — no new packages introduced.

## Issues Encountered
- **Local Redis unavailable (environmental, not a code issue):** the full `uv run pytest --cov` run reported 9 failed + 42 errors, **all** `redis.asyncio.connection.Connection(host=localhost,port=6379)` failures in Redis-dependent suites this plan does not touch (`test_services/test_agent_task_router.py`, `test_routers/test_agent_exec_batches.py`, `test_routers/test_agent_tracklists.py`, `test_routers/test_execution_dispatch.py`). No Redis server runs in this worktree (only Postgres). This is identical to the environmental failure documented in Plan 01's summary and is out of scope per the scope-boundary rule (pre-existing, unrelated, not caused by this plan's changes — docs + a Redis-free AST test). These suites pass in CI where Redis runs. Because the Redis suites error out, their lines are not counted, so a local `>=85%` total-coverage number could not be confirmed in this Redis-less environment; the threshold is a CI concern where the full suite (including Redis) runs.
- **`1497 passed`** across the rest of the suite, including the 6 new guard tests.

## Verification
- `uv run pytest tests/test_no_default_queue_producers.py -q` -> **6 passed**.
- Negative check: planted `request.app.state.queue` in `pipeline.py` -> scan failed `src/phaze/routers/pipeline.py:417: *.state.queue attribute access`; reverted -> green.
- `uv run ruff check .` -> All checks passed. `uv run ruff format --check .` -> 277 files already formatted.
- `uv run mypy .` -> Success (142 source files).
- `grep -n "controller" README.md` and `grep -n "phaze-agent" README.md` -> routing subsection present; README line 1 (`<!-- generated-by: gsd-doc-writer -->`) unchanged.
- Full suite: 1497 passed; 9 failed + 42 errors, all environmental Redis `ConnectionError` in untouched suites (see Issues).

## Self-Check: PASSED

- `tests/test_no_default_queue_producers.py` exists on disk.
- `README.md` carries the "Task Queue Routing" subsection (lines ~110-116).
- Commits `26d0614` (test) and `4b752c2` (docs) are present in git history on the worktree branch.

---
*Phase: 30-fix-systemic-control-plane-saq-queue-misrouting-every-manual*
*Completed: 2026-06-09*
