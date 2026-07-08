---
task: 260707-s44
title: Hide revoked agents (legacy-application-server) from /admin/agents
status: complete
requirements: [QUICK-260707-s44]
base_commit: dc8fbda662a13122298849b2abd86715b761f90a
commits:
  - 8c85c8ba fix(admin_agents): filter revoked agents out of /admin/agents panel
  - 72dc6a2f test(admin_agents): lock revoked-agent absence + reconcile 5-state/sort tests
files_modified:
  - src/phaze/routers/admin_agents.py
  - tests/agents/routers/test_admin_agents.py
---

# Summary — 260707-s44

## Objective
The permanently-revoked `legacy-application-server` FK-placeholder row (and any
revoked agent) was leaking into the `/admin/agents` operator panel because
`_load_agents()` ran `select(Agent)` with no `revoked_at` filter. Add the same
`Agent.revoked_at.is_(None)` filter every other agent query in the codebase
(main.py / shell.py / pipeline.py) already uses. Display-only fix — the legacy
Agent row stays in the DB as the FK default owner.

## What changed

### Task 1 — Filter revoked agents out of `_load_agents` (commit 8c85c8ba)
- `src/phaze/routers/admin_agents.py`: changed
  `select(Agent)` → `select(Agent).where(Agent.revoked_at.is_(None))`.
  This single query change covers BOTH render paths (`page()` full-page +
  HX-Request partial, and `table_partial()` /_table poll) since both call
  `_load_agents`.
- Updated the `_load_agents` docstring and the module docstring so they no
  longer claim revoked agents "land last" in this panel (they no longer
  appear); documented the shared `revoked_at IS NULL` convention. Left
  `classify` / `sort_key` revoked handling in `agent_liveness.py` untouched
  (still reachable for callers elsewhere; noted as effectively unreachable via
  this panel). No DELETE/UPDATE against the Agent row anywhere.

### Task 2 — Regression test + reconcile existing tests (commit 72dc6a2f)
- `tests/agents/routers/test_admin_agents.py`:
  - Added `test_revoked_agent_absent`: asserts `RevokedBox` and
    `aria-label="Status: revoked"` are absent from BOTH `/admin/agents/_table`
    and `/admin/agents`, while the non-revoked control `AliveBox` is present
    (proves the filter drops only revoked rows, not the whole table). Uses the
    `smoke` fixture's explicitly-revoked `RevokedBox` (id `revoked-agent`,
    `revoked_at=now`) — NOT the conftest legacy row, which is seeded WITHOUT
    `revoked_at` and is therefore a NEVER agent in the test DB.
  - Renamed `test_status_pills_render_all_5_states` →
    `test_status_pills_render_4_visible_states`; removed the `"REVOKED" in body`
    assertion; kept the `bg-gray-100 dark:bg-gray-800` (NEVER) assertion.
  - `test_sort_order`: removed `"revoked"` from the `pos` dict and the final
    chained assertion (now `alive < stale < dead < never`); updated docstring.
  - Updated the module docstring bullets to reflect the 4 visible states and
    that revoked agents are filtered out.

## Verification
- `grep -q "revoked_at.is_(None)" src/phaze/routers/admin_agents.py` — present.
- `uv run mypy src/phaze/routers/admin_agents.py` — Success, no issues.
- `uv run ruff check` on both files — All checks passed.
- `uv run pytest tests/agents/routers/test_admin_agents.py -q` — **18 passed**
  (against the ephemeral test Postgres on port 5433 via `just test-db`; the
  suite requires the DB fixture — an initial run against the default 5432 dev DB
  errored with OSError connection-refused, which is an infra/env issue, not a
  test failure).
- Pre-commit hooks ran on both commits — all hooks Passed (ruff, ruff-format,
  bandit, mypy, etc.).

## Notes
- Worktree base was corrected: HEAD started at `c559ab74` (release bump); the
  plan commit is `dc8fbda6`. Hard-reset to `dc8fbda6` per the branch-check
  protocol before starting.
- No new dependencies. No schema/migration changes. Legacy Agent row untouched
  in the DB — display-only suppression.
