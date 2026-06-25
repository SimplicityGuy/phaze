# Phase 48 — Deferred Items

Out-of-scope discoveries logged during execution (per executor SCOPE BOUNDARY). These are
NOT fixed by the discovering plan; they are surfaced for the orchestrator / verifier / a
follow-up quick task.

## DEF-48-01 — Stale agents-table column assertion (wave-1 escapee) — ✅ RESOLVED

- **Resolution:** Fixed during execute-phase post-merge cleanup — added `"kind"` to the
  `expected` set and updated the docstring (commit on branch `gsd/phase-48-compute-agent-type`).
  Full integration suite now green: **2071 passed, 0 failed**.
- **Discovered during:** Plan 48-03 execution (wave 2), full-suite verification run.
- **File:** `tests/test_migrations/test_012_upgrade.py::test_agents_table_columns`
- **Status:** FAILING on a pristine DB (and already failing at the wave-2 base commit `2e97705`,
  i.e. before any 48-03 change).
- **Root cause:** Migration `024` (commit `9808d5e`, `feat(48-01)`, wave 1) added the
  `agents.kind` column. `test_agents_table_columns` asserts the **exact** HEAD column inventory
  of the `agents` table via `columns == expected`, but its `expected` set was never updated to
  include `kind`. So after head upgrade the actual set has one extra column (`kind`) and the
  equality fails.
- **Why out of scope for 48-03:** Plan 03 only modifies
  `src/phaze/templates/admin/partials/_kind_badge.html`,
  `src/phaze/templates/admin/partials/agents_table.html`,
  `tests/test_routers/test_admin_agents.py`, and `tests/test_task_split.py`. The failure is in
  `tests/test_migrations/`, a file outside this plan, and was introduced by wave 1 (Plan 01),
  not by any 48-03 change. The executor SCOPE BOUNDARY forbids fixing pre-existing failures in
  unrelated files.
- **Fix (trivial, one line):** add `"kind"` to the `expected` set in `test_agents_table_columns`
  (lines ~47-57). Optionally update the docstring to note `kind` was added by migration 024
  (phase 48). No production code change needed — the column and its CHECK already exist and are
  covered by Plan 01's own migration tests.
- **Impact if unfixed:** the full suite stays red by exactly 1 test (`1 failed, 2059 passed`),
  which blocks the phase-level "full suite green / coverage ≥85%" verification gate.
- **Suggested owner:** orchestrator post-merge cleanup or a `/gsd:quick` one-liner.
