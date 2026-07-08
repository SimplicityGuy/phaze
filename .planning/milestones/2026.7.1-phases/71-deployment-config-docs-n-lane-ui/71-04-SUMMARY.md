---
phase: 71-deployment-config-docs-n-lane-ui
plan: 04
subsystem: routing-control-ui
tags: [beui-02, force-local, thin-endpoint, header-pill, htmx, incident-response]
requires:
  - "route_control 'global' row + get_route_control(session) reader (Plan 71-02)"
  - "v7.0 shell header ml-auto status-strip cluster (Phase 57)"
provides:
  - "POST /pipeline/routing/force-local thin write endpoint"
  - "shell/partials/_force_local_pill.html header master-toggle pill"
  - "force_local seeded on every shell page via shell.py _render_stage base context"
affects:
  - "src/phaze/routers/shell.py base shell context (all /s/{stage} + / renders)"
  - "src/phaze/templates/shell/partials/header.html"
tech-stack:
  added: []
  patterns:
    - "thin write endpoint mirroring pipeline_stages pause/resume (load-or-create row, mutate, commit, return swapped partial)"
    - "authoritative server-driven pill state (no optimistic mutation) — hx-target this + outerHTML"
    - "OOB polite aria-live toast appended to base #toast-container (does not steal focus)"
key-files:
  created:
    - src/phaze/routers/routing.py
    - src/phaze/templates/shell/partials/_force_local_pill.html
  modified:
    - src/phaze/main.py
    - src/phaze/routers/shell.py
    - src/phaze/templates/shell/partials/header.html
    - tests/shared/routers/test_routing.py
decisions:
  - "The _force_local_pill.html partial is created in Task 1 (not Task 2) because the Task-1 endpoint response renders it; Task 2 wires it into header.html + the shell seed (sequencing adjustment, Rule 3)."
  - "Pill placed as the FIRST child of the ml-auto cluster (left of the agent dot + Agents pill) — leftmost global status indicator, satisfying 'immediately left of the Agents pill' while keeping the dot paired with its Agents label."
metrics:
  tasks: 2
  files_created: 2
  files_modified: 4
  completed: 2026-07-05
---

# Phase 71 Plan 04: Force-Local Control Surface Summary

A persistent header pill (`role="switch"`) on every shell page that reverts all analysis routing to local in one click by writing the durable `route_control` `'global'` row via a thin endpoint — reversible, instant-on both directions, with authoritative server-driven state and internal-realm-only boolean-coerced writes (BEUI-02).

## What Was Built

**Task 1 — thin force-local write endpoint (`3289b139`)**
- `src/phaze/routers/routing.py`: `POST /pipeline/routing/force-local` taking `engage: Annotated[bool, Form()]` (V5 boolean coercion, T-71-07; no app-layer auth per T-37-04). Loads-or-defensively-creates the `'global'` `RouteControl` row, sets `force_local=engage`, commits in one transaction, and returns the `_force_local_pill.html` partial reflecting the JUST-COMMITTED state (authoritative, never optimistic — T-71-10) plus an OOB polite-aria-live toast carrying the engage/revert copy.
- `src/phaze/templates/shell/partials/_force_local_pill.html`: the two server-rendered states from `force_local` — engaged = loud amber incident pill + warning glyph + `FORCED LOCAL` + `aria-checked="false"`; normal = neutral pill + `CLOUD ROUTING` + `aria-checked="true"`. `hx-post`s the opposite `engage` via `hx-vals`, swaps itself in place (`hx-target="this"`, `hx-swap="outerHTML"`). Word-labelled state (never hue-only, WCAG 1.4.1).
- Registered `routing.router` in `main.py`.

**Task 2 — header pill + shell-context seed (`399c29c9`)**
- `src/phaze/templates/shell/partials/header.html`: `{% include %}` the pill in the `ml-auto` status-strip cluster, left of the Agents pill.
- `src/phaze/routers/shell.py`: seed `"force_local": await get_route_control(session)` in the base `_render_stage` context (NOT the Analyze-only `build_dashboard_context`), so the global incident control shows correct state on EVERY page. `get_route_control` is degrade-safe (returns `False` on any DB error, never raises).

## Verification

- `uv run pytest tests/shared/routers/test_routing.py` — 7 passed (endpoint round-trip persists the row and reflects state in the returned pill; a NON-Analyze shell page `/s/discover` seeds the pill from the persisted row both engaged and reverted).
- `tests/shared/core/test_shell_routes.py` — 15 passed (header include did not break full-page shell rendering).
- `uv run ruff check` + `uv run mypy` — clean on all new/modified files.

## TDD Gate Compliance

Task 1 followed RED → GREEN: the round-trip test was written first and observed failing (404, endpoint absent), then the endpoint + partial + registration made it pass. Both tests were committed with the Task-1 implementation commit (`3289b139`); the Task-2 wiring commit (`399c29c9`) is the GREEN for `test_force_local_pill_seeded_on_shell_page`. No separate `test(...)`-only commit was cut (single per-task commit protocol for this parallel executor).

## Deviations from Plan

**1. [Rule 3 - Blocking] `_force_local_pill.html` created in Task 1 rather than Task 2**
- **Found during:** Task 1
- **Issue:** The Task-1 endpoint returns `templates.TemplateResponse(..., "_force_local_pill.html", ...)`, so the partial must exist for the Task-1 round-trip test to pass. The plan's artifact table assigned the partial to Task 2.
- **Fix:** Created the partial in the Task-1 commit; Task 2 only adds the `header.html` include + the `shell.py` seed.
- **Files:** `src/phaze/templates/shell/partials/_force_local_pill.html`
- **Commit:** `3289b139`

## Must-Haves Coverage

- **D-08** (live one-click reversible force-local revert writing the Plan-02 row, no redeploy): endpoint writes `route_control.force_local`; both directions covered by the round-trip test.
- **D-09** (thin endpoint mirroring pause/resume; no app-layer auth; boolean-coerced form input): `engage: Annotated[bool, Form()]`, internal realm, mirrors `pipeline_stages` load-or-create + commit + return-partial.
- **D-10** (role=switch pill in header ml-auto cluster left of Agents; amber loud FORCED LOCAL engaged / neutral CLOUD ROUTING normal; instant-on both directions, no confirm; authoritative from write response): implemented in `_force_local_pill.html` + `header.html`.
- **Seed on every page**: `get_route_control(session)` in `shell.py` `_render_stage` base context; verified on the non-Analyze `/s/discover` page.

## Self-Check: PASSED

- `src/phaze/routers/routing.py` — FOUND
- `src/phaze/templates/shell/partials/_force_local_pill.html` — FOUND
- Commit `3289b139` — FOUND
- Commit `399c29c9` — FOUND
