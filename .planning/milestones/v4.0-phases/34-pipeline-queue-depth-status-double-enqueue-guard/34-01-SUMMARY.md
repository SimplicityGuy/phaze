---
phase: 34-pipeline-queue-depth-status-double-enqueue-guard
plan: "01"
subsystem: pipeline-service
tags: [saq, queue-depth, failure-isolation, read-only-service]
requires:
  - "FakeQueue.count + set_counts / fail_count (Plan 00)"
  - "FakeTaskRouter.set_counts (Plan 00)"
provides:
  - "get_queue_activity(app_state, session) -- live SAQ queue-depth read with split per-source failure isolation"
affects:
  - "Plans 02-04 (router context seeding + processing-card partial + button disable consume the six-key dict)"
tech-stack:
  added: []
  patterns:
    - "Read-only service over a flaky external system (Redis) on a hot poll path: split try/except per source, degrade each to 0, never raise"
    - "Reuse the dashboard() revoked_at IS NULL predicate to enumerate agents (NOT select_active_agent)"
key-files:
  created: []
  modified:
    - src/phaze/services/pipeline.py
    - tests/test_services/test_pipeline.py
decisions:
  - "Broad except Exception (no BLE001 noqa -- BLE is not an enabled ruff rule set in this project) catches both AttributeError (test lifespan-skip) and Redis errors; justified inline + structlog.warning emitted so silent zeros are observable"
  - "app_state typed Any (matches enqueue_router precedent) so the missing-attr degrade path type-checks under strict mypy"
  - "Only queued+active kinds read; the scheduled-inclusive kind is never referenced (verified by grep)"
metrics:
  duration: "~12 min"
  completed: "2026-06-11"
  tasks: 2
  files: 2
---

# Phase 34 Plan 01: get_queue_activity Queue-Depth Service Summary

Added the read-only `get_queue_activity(app_state, session)` service to `src/phaze/services/pipeline.py` -- the authoritative "is anything in flight" signal the DB cannot provide. It sums `count("queued") + count("active")` across every non-revoked agent's per-agent queue plus the controller queue, and degrades each source independently to all-zero on BOTH a missing `app.state` attribute (the test lifespan-skip) AND any Redis error, so a hiccup never 500s the 5s dashboard poll. This is the single backend primitive Plans 02-04 will surface in the UI.

## What Was Built

- **`get_queue_activity(app_state: Any, session: AsyncSession) -> dict[str, int]`** in `services/pipeline.py`:
  - Initialises four counters to 0.
  - **First try block:** executes the exact `dashboard()` predicate `select(Agent).where(Agent.revoked_at.is_(None))`, iterates `.scalars().all()`, and for each agent does `q = app_state.task_router.queue_for(agent.id)` then `agent_queued += await q.count("queued")` / `agent_active += await q.count("active")`.
  - **Second, independent try block:** reads `controller_queued`/`controller_active` from `app_state.controller_queue.count(...)`.
  - Each `except Exception` resets only that source's two counters to 0 and emits `logger.warning("queue_activity_degraded", source=..., exc_info=True)` (structlog, no `print`).
  - Returns the six-key dict: `agent_queued`, `agent_active`, `controller_queued`, `controller_active`, `agent_busy` (= agent_queued+agent_active), `controller_busy` (= controller_queued+controller_active).
  - Added imports: `Any` (typing), `import structlog`, `from phaze.models.agent import Agent`, plus a module-level `logger = structlog.get_logger(__name__)`.
- **Five `@pytest.mark.asyncio` tests** in `tests/test_services/test_pipeline.py` using the Plan-00 harness (`FakeQueue`, `FakeTaskRouter`, `seed_active_agent`):
  1. `test_get_queue_activity_sums_across_agents` -- two non-revoked agents (3/2 + 4/1) -> agent_queued=7, agent_active=3, agent_busy=10; controller 5/0 -> controller_busy=5.
  2. `test_get_queue_activity_excludes_scheduled` -- a queue reporting `incomplete=999` does not change `agent_busy`/`controller_busy`.
  3. `test_get_queue_activity_degrades_on_redis_error` -- `fail_count()` on agent + controller -> all six values 0, no raise.
  4. `test_get_queue_activity_degrades_on_missing_app_state` -- `SimpleNamespace()` -> all six values 0.
  5. `test_get_queue_activity_controller_independent_of_agents` -- controller `fail_count()` but agents healthy -> agent counts intact, controller counts 0.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Dropped the `# noqa: BLE001` directives the plan prescribed**
- **Found during:** Task 1 (ruff check)
- **Issue:** The plan's action text said to annotate each broad `except` with `# noqa: BLE001`. But `BLE` is not in this project's enabled ruff rule sets (CLAUDE.md: `ARG B C4 E F I PLC PTH RUF S SIM T20 TCH UP W W191`), so the `noqa` was flagged as an unused directive by `RUF100` and blocked the commit.
- **Fix:** Removed the `noqa` and replaced it with a plain inline comment justifying the broad catch (degrade-to-0 on missing app.state attr or Redis hiccup, never 500 the poll). The broad `except Exception` itself is lint-clean because `BLE001` is not enforced here. Behaviour is identical to the planned design.
- **Files modified:** src/phaze/services/pipeline.py
- **Commit:** c452a85

**2. [Rule 3 - Blocking] Reworded the docstring to avoid the literal "incomplete"**
- **Found during:** Task 1 (acceptance grep `grep -v '^#' ... | grep -c 'incomplete'` must return 0)
- **Issue:** The docstring originally explained the design by naming the `"incomplete"` kind, which the acceptance criterion's grep (which only strips lines starting with `#`, not docstring lines) would have counted as a reference.
- **Fix:** Reworded the docstring to say "the scheduled-inclusive kind is never read" instead of naming the literal. The function body never read `"incomplete"` to begin with; grep now returns 0.
- **Files modified:** src/phaze/services/pipeline.py
- **Commit:** c452a85

No architectural deviations. No auth gates.

## Verification

- `uv run mypy src/phaze/services/pipeline.py` -- Success, no issues.
- `uv run ruff check src/phaze/services/pipeline.py` -- All checks passed.
- `grep -v '^#' src/phaze/services/pipeline.py | grep -c 'incomplete'` -- 0 (the scheduled-inclusive kind is never referenced).
- Two independent `try`/`except` blocks present (`grep -c 'except Exception:'` -> 2).
- `uv run pytest tests/test_services/test_pipeline.py -q` -- 9 passed.
- `-k queue_activity` -> 5 passed; `-k scheduled` -> 1 passed; `-k degrade` -> 2 passed.
- Pre-commit hooks (ruff, ruff-format, bandit, mypy `uv run mypy .`) passed on both commits.

## Commits

- `c452a85` feat(34-01): add get_queue_activity read-only queue-depth service
- `1990b73` test(34-01): cover get_queue_activity sum, scheduled-exclusion, degrade modes

## Self-Check: PASSED

- FOUND: src/phaze/services/pipeline.py (`async def get_queue_activity` present)
- FOUND: tests/test_services/test_pipeline.py (5 `get_queue_activity` tests present)
- FOUND: commit c452a85
- FOUND: commit 1990b73
