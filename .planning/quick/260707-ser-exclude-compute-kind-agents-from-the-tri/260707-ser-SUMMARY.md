# Quick Task 260707-ser — Summary

**Task:** Exclude `kind="compute"` agents (Kueue/burst backends like `k8s-vox`) from the
operator "Trigger Scan" agent-picker dropdown. Compute agents are media-less and cannot be
scan targets, so listing them is a bug.

**Requirement:** SER-01
**Status:** COMPLETE
**Base:** `e46e21e4` (reset from `c559ab74` per worktree base-correctness step)

## What changed

### Task 1 — filter all three scan-picker queries (`93c6205e`)
All three `agents_stmt` queries that feed `trigger_scan_card.html` now filter
`Agent.kind == "fileserver"` in addition to the existing `Agent.revoked_at.is_(None)`:

- `src/phaze/routers/pipeline.py:484` — `build_dashboard_context`
- `src/phaze/routers/shell.py:186` — Analyze empty-state branch
- `src/phaze/routers/shell.py:196` — Discover workspace branch

Each got an explanatory SER-01 comment. `src/phaze/routers/admin_agents.py` `_load_agents`
was intentionally left untouched (its liveness table shows all kinds by design). No changes to
the template, the `Agent` model, or the compute-registration path.

### Task 2 — regression test (`b7de115e`)
Added `test_dashboard_context_excludes_compute_agents_from_scan_picker` in
`tests/shared/routers/test_pipeline.py` (next to `test_dashboard_context_binds_lanes`). It seeds
one `kind="fileserver"` (`nox`) and one `kind="compute"` (`k8s-vox`) agent, calls
`build_dashboard_context`, and asserts on agent **ids**: `nox` present, `k8s-vox` absent.

**Harness note:** `build_dashboard_context` runs degrade-safe reads (`_safe_count`) that call
`session.rollback()` when the SAQ `saq_jobs` broker table is absent (it is not part of
`Base.metadata`, so the `session` fixture DB has no such table). That rollback expires the
returned ORM rows, so a lazy `agent.id` access raises `MissingGreenlet`. The test reads each PK
from the SQLAlchemy identity map via `inspect(agent).identity[0]` (IO-free), faithfully
asserting on what the function returned. This mirrors how the existing lanes test tolerates the
same degraded path.

## Verification

- `uv run ruff check` on all three files — **clean**.
- `uv run ruff format --check` — **clean** (already formatted).
- `uv run mypy src/phaze/routers/pipeline.py src/phaze/routers/shell.py` — **Success, no issues**.
  (Tests are excluded from the mypy gate per CLAUDE.md `exclude = "^(tests/...)"`.)
- Grep gate: exactly **3** occurrences of `Agent.kind == "fileserver"` across the two routers.
- New test **passes** against the ephemeral test DB (`just test-db`, Postgres 5433).
- Full `tests/shared/routers/test_pipeline.py` — **108 passed**.
- `tests/shared/core/test_shell_routes.py` — **6 passed** (shell.py edits, no regression).
- Pre-commit hooks ran on **both** commits — all hooks **Passed** (incl. ruff, ruff-format,
  bandit, mypy).

## Commits

- `93c6205e` fix(pipeline): exclude compute-kind agents from Trigger Scan picker
- `b7de115e` test(pipeline): assert compute agents excluded from scan-picker context

## Notes for the orchestrator

- Worktree base was reset from `c559ab74` to `e46e21e4` (required base) at start.
- Docs artifacts (this SUMMARY, STATE, PLAN) intentionally **not** committed — per constraints.
- ROADMAP.md not touched.
