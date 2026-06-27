---
phase: 48-compute-agent-type
plan: 03
subsystem: admin-ui
tags: [admin, agents, kind-badge, jinja2, tailwind, htmx, accessibility, import-boundary, compute-agent]
requires:
  - "agents.kind String(16) NOT NULL DEFAULT 'fileserver' column (Plan 01)"
  - "_status_pill.html LOCKED pill geometry (Phase 29)"
  - "agents_table.html HTMX self-replacing partial (Phase 29)"
provides:
  - "_kind_badge.html partial — COMPUTE (indigo) / FILE SERVER (slate) badge, defensive if/else fallback"
  - "Kind column in agents_table.html between Agent and Status (covers full-page + 5s poll render paths)"
  - "router-render tests covering both kinds across both render paths"
  - "reaffirmed compute-agent ORM/DB import boundary (CLOUDAGENT-02) in test_task_split.py"
affects:
  - src/phaze/templates/admin/partials/_kind_badge.html
  - src/phaze/templates/admin/partials/agents_table.html
  - tests/test_routers/test_admin_agents.py
  - tests/test_task_split.py
tech-stack:
  added: []
  patterns:
    - "Jinja2 {% include %} partial reused by both full-page and HTMX-poll render paths (single edit, no flicker)"
    - "defensive if/else badge fallback mirrors _status_pill.html — out-of-enum kind never blanks the cell"
    - "color-plus-label-plus-aria-label so a categorical attribute is never color-only (a11y)"
    - "subprocess import-boundary assertion proves a worker module's transitive import graph"
key-files:
  created:
    - src/phaze/templates/admin/partials/_kind_badge.html
  modified:
    - src/phaze/templates/admin/partials/agents_table.html
    - tests/test_routers/test_admin_agents.py
    - tests/test_task_split.py
decisions:
  - "compute=indigo / fileserver=slate: distinct from BOTH the status-pill ladder (green/amber/red/gray) AND the cyan brand accent, so kind never reads as status"
  - "both kinds render a badge (explicit over implicit) — an empty fileserver cell would read as missing data"
  - "router unchanged — _load_agents already SELECT Agent, so kind rides free on the loaded row"
  - "CLOUDAGENT-02 reaffirmation is comment-level: the compute agent runs the SAME agent_worker; no new test file, no essentia/file-read ban (media isolation is runtime, Phase 51)"
metrics:
  duration: ~20min
  tasks: 2
  files: 4
  tests_added: 4
  completed: 2026-06-25
---

# Phase 48 Plan 03: Agent Kind Badge + ORM Import-Boundary Reaffirmation Summary

Added a `_kind_badge.html` partial and a dedicated **Kind** column to the Agents admin table so the operator can count cloud capacity by running their eye down one column (CLOUDAGENT-03), and reaffirmed the compute agent's ORM/app-DB isolation invariant (CLOUDAGENT-02) in the existing subprocess import-boundary test. The badge renders `COMPUTE` (indigo) for `kind='compute'` rows and `FILE SERVER` (slate) for `kind='fileserver'` (and any out-of-enum) rows, honoring the APPROVED + LOCKED 48-UI-SPEC geometry, palette, labels, and aria-labels — on both the full-page load and the 5s HTMX poll partial.

## What was built

### Task 1 — Kind badge partial + Kind column (TDD)
- **RED:** Seeded the `smoke` fixture with one `kind='compute'` row (`alive-agent`) and four explicit `kind='fileserver'` rows, then added four failing router-render tests: `test_kind_badge_compute_renders`, `test_kind_badge_fileserver_renders`, `test_kind_badge_in_poll_partial`, `test_kind_column_header_present`.
- **GREEN:** Created `src/phaze/templates/admin/partials/_kind_badge.html` — an `if agent.kind == 'compute'` / `{% else %}` (neutral fileserver fallback) structure mirroring `_status_pill.html`'s defensive branching, with the LOCKED geometry `text-xs font-semibold px-2 py-0.5 rounded-full` copied verbatim, indigo/slate palettes, uppercase labels, and `aria-label="Kind: compute"` / `"Kind: file server"`. Inserted a `Kind` `<th>` between Agent and Status and a `{% include "admin/partials/_kind_badge.html" %}` `<td>` between the Agent cell and the status-pill cell — one edit site covers both full-page (`GET /admin/agents`) and poll (`GET /admin/agents/_table`) render paths.
- `src/phaze/routers/admin_agents.py` was **not** touched (`_load_agents` already does a bare `select(Agent)`; `kind` rides free).
- Commit `96dd8fe`.

### Task 2 — Reaffirm compute-agent ORM import boundary
- Expanded the docstring of `test_agent_worker_does_not_import_phaze_database` in `tests/test_task_split.py` to explicitly name the CLOUDAGENT-02 compute-agent invariant: a compute agent runs the SAME `phaze.tasks.agent_worker` module on the SAME `phaze-agent-<id>` queue and PUTs results to the SAME HTTP endpoint as a file-server agent, so its security guarantee IS the import boundary this test already enforces — only the SAQ Postgres broker (`saq.queue.postgres`, asserted present) + cache Redis + HTTP API are reachable; `phaze.database` / `phaze.tasks.session` / `sqlalchemy.ext.asyncio` stay out of `sys.modules` (asserted absent). Documented that the "no media filesystem" half is runtime-enforced (empty scan roots + no mount, Phase 51), not import-enforced — no essentia/file-read ban added, no duplicate test file created.
- Behavior unchanged: forbidden set + broker assertion intact. Commit `f131bee`.

## Verification

- `uv run pytest tests/test_routers/test_admin_agents.py tests/test_task_split.py -q` → **21 passed** on a pristine ephemeral DB (10 existing admin tests + 4 new kind tests + 7 import-boundary tests).
- `uv run pytest tests/test_routers/test_admin_agents.py -k kind tests/test_task_split.py -x -q` (plan verification command) → green.
- Full suite on a pristine DB (`just integration-test`): **2059 passed, 1 failed** — the single failure is the pre-existing wave-1 escapee documented below (not in this plan's files, not caused by any 48-03 change).
- UI-SPEC compliance: badge geometry/palette/labels/aria-labels match the LOCKED contract; status pill, queue depth, empty/error states, and 5s poll cadence untouched.

## Deviations from Plan

None affecting this plan's four files — both tasks executed exactly as written (Task 2 took the comment-level reaffirmation path the plan endorsed; no over-constraining assertion added).

## Deferred Issues

**DEF-48-01 — Stale agents-table column assertion (wave-1 escapee, OUT OF SCOPE)**
- **File:** `tests/test_migrations/test_012_upgrade.py::test_agents_table_columns` (NOT one of this plan's files).
- **Symptom:** `1 failed` in the full suite — `columns == expected` fails because the actual HEAD `agents` table now has a `kind` column that the test's hardcoded `expected` set omits.
- **Root cause:** Migration `024` (commit `9808d5e`, `feat(48-01)`, **wave 1**) added `agents.kind` but did not update this exact-inventory assertion. The test was **already failing at the wave-2 base commit `2e97705`**, before any 48-03 change.
- **Why not fixed here:** Executor SCOPE BOUNDARY — only issues directly caused by the current task's changes are auto-fixed; pre-existing failures in unrelated files are logged, not fixed. Logged to `.planning/phases/48-compute-agent-type/deferred-items.md` (DEF-48-01).
- **Fix:** one line — add `"kind"` to the `expected` set in `test_agents_table_columns`. Recommended for orchestrator post-merge cleanup or a `/gsd:quick` task. This is the only thing standing between the suite and fully green.

## Known Stubs

None — both kinds render real badges from the live `agent.kind` column; no placeholder/empty-data paths introduced.

## Threat Flags

None — no new network endpoints, auth paths, file-access patterns, or schema changes. T-48-01 (compute-agent EoP) and T-48-02 (kind-render tampering) from the plan's threat register are both mitigated: the import-boundary test reaffirms ORM isolation, and the badge's `{% else %}` fallback fails safe on any out-of-enum `kind` (Jinja2 autoescaping covers the static literals; no user text is interpolated).

## Self-Check: PASSED

- All created/modified files present: `_kind_badge.html`, `agents_table.html`, `test_admin_agents.py`, `test_task_split.py`, `48-03-SUMMARY.md`, `deferred-items.md`.
- Both task commits exist in git history: `96dd8fe` (Task 1), `f131bee` (Task 2).
