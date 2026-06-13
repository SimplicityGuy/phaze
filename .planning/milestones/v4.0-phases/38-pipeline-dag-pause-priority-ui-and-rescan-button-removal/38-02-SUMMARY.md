---
phase: 38-pipeline-dag-pause-priority-ui-and-rescan-button-removal
plan: 02
subsystem: ui
tags: [htmx, alpinejs, jinja2, dag-canvas, pipeline, saq, operator-controls]

# Dependency graph
requires:
  - phase: 37-per-stage-pause-and-priority-control-plane
    provides: "POST /pipeline/stages/{stage}/{priority,pause,resume} endpoints returning {stage, priority, paused}"
  - phase: 38-pipeline-dag-pause-priority-ui-and-rescan-button-removal (38-01)
    provides: "Rescan anchor removed from the Discovery node + the dag_canvas render-test harness"
  - phase: 38-pipeline-dag-pause-priority-ui-and-rescan-button-removal (38-03)
    provides: "the 6 $store.pipeline keys {metadata,analyze,fingerprint}{Paused,Priority}, the degrade-safe get_stage_controls reader, and the 5s /pipeline/stats OOB poll that refreshes them"
provides:
  - "stage_controls(stage) Jinja macro on the 3 agent chips: 2 x-show-gated static-hx-post pause/resume buttons + ▲/▼ priority steppers, authoritative-only store write"
  - "recomputed NODE_LAYOUT (col-1 gutter 276px, agent h:250) + grown canvas/SVG (1000px) so the taller chips clear"
  - "extended <ol> text equivalent surfacing ' — paused' / ' — priority N' for the 3 agent stages"
  - "endpoint-surface guard split into enqueue-trigger + stage-control assertions; overlap guard min_chip_height bumped to 240"
affects: [pipeline-dashboard, dag-canvas]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Operator control macro: x-show-gated TWO-button static-hx-post toggle (not a bound :hx-post) keeps each endpoint statically testable (RESEARCH A4)"
    - "Authoritative-only store write: @htmx:after-request JSON.parse → store, no optimistic mutation, so a racing 5s poll can't revert a half-applied click (T-38-OOB)"
    - "Fragment-scoped render assertions via a stable id=stage-controls-<stage> slicer (mirrors the node-<id> idiom)"
    - "Edge-anchor-midpoint layout: col-2/col-3 node y derived from incoming-edge source midpoints after the col-1 recompute"

key-files:
  created:
    - .planning/milestones/v4.0-phases/38-pipeline-dag-pause-priority-ui-and-rescan-button-removal/38-02-SUMMARY.md
  modified:
    - src/phaze/templates/pipeline/partials/dag_canvas.html
    - tests/test_dag_canvas_render.py

key-decisions:
  - "stage_controls is a reusable macro keyed off the hardcoded allowlist value {{ stage }} only; pause/resume are TWO x-show-gated static-hx-post buttons, updates authoritative-only via @htmx:after-request (no optimistic mutation, T-38-OOB)"
  - "agent-chip NODE_LAYOUT gutter widened 182->276px (h 154->250) for the control row; col-0/col-2/col-3 nodes re-balanced to incoming-edge midpoints; canvas/SVG grown 720->1000; overlap guard min_chip_height bumped 150->240"
  - "the endpoint-surface guard rewrite landed in Task 1 (forced: the macro breaks the exactly-4 assertion); Task 3 carried only the overlap min_chip_height bump (which depends on Task 2's layout) — keeps every commit's suite green"

patterns-established:
  - "Add a stable id to a reusable control macro container so render tests can slice the fragment and assert negative space (no .blocked / no agentBusy / no px-1.5)"

requirements-completed: [REQ-38-1, REQ-38-2]

# Metrics
duration: ~8min
completed: 2026-06-13
---

# Phase 38 Plan 02: DAG Per-Stage Pause/Priority Controls Summary

**Wired the three agent DAG chips (metadata/analyze/fingerprint) to the Phase-37 control plane via a reusable `stage_controls` macro — pause/resume toggle + ▲/▼ priority stepper, store-driven and authoritative-only — and recomputed the layout the taller chips force, with the `<ol>` text equivalent and both guard tests updated as intentional contract changes.**

## Performance

- **Duration:** ~8 min
- **Started:** 2026-06-13T21:04:58Z
- **Completed:** 2026-06-13T21:13:00Z
- **Tasks:** 3 (TDD on Task 1: RED → GREEN)
- **Files modified:** 2 (+1 SUMMARY created)

## Accomplishments

- **`stage_controls(stage)` macro** attached after the `enqueue_button(...)` inside `node-metadata`, `node-analyze`, `node-fingerprint`:
  - Pause/Resume = **TWO `x-show`-gated buttons, each with a STATIC `hx-post`** (`.../pause` amber, shown while not paused; `.../resume` green, shown while paused) — the label flips so color is never the only signal (RESEARCH A4 — sidesteps the bound-`:hx-post` timing question).
  - **Priority stepper**: ▲ Higher (`hx-vals='{"delta": -10}'`, disabled at floor `<= 0`) DECREMENTS the raw number (runs sooner); ▼ Lower (`hx-vals='{"delta": 10}'`, disabled at ceiling `>= 100`) INCREMENTS it. The value span binds `x-text="$store.pipeline.<stage>Priority"` with `tabular-nums`.
  - Every control is `hx-swap="none"` + `hx-disabled-elt="this"`; the container `@htmx:after-request` does `JSON.parse(...)` and writes the store **authoritatively** (`r.paused ? 1 : 0`) — no optimistic mutation, so a racing 5s poll cannot revert a half-applied click (T-38-OOB).
  - Reads **only** `$store.pipeline.<stage>Paused/Priority` — **never** `nodes.<stage>.blocked` / `agentBusy`: pausing a busy stage is the entire point (drain semantics).
  - Static `lower number runs first` hint + the LOCKED rose `Couldn't update. Retry.` error reveal; steppers use grid-aligned `px-1 py-1 min-h-[28px]` (never `px-1.5`); every element carries a `dark:` variant.
- **Layout recompute** (the taller chips force it): col-1 gutter widened 182→**276px** (≥ the ~250px agent-chip height + 16px), agent `h` 154→250; `metadata` y:24, `analyze` y:300, `fingerprint` y:576, `scan_search` y:852 (no controls, h:126). `discovery`/`proposals`/`scrape`/`execute`/`match` y re-balanced to their incoming-edge source midpoints so the 9 curved edges still land cleanly. Canvas wrapper + `<svg>` height/viewBox grown 720→**1000**.
- **`<ol>` text equivalent** extended for the 3 agent stages only: appends `<template x-if="$store.pipeline.<stage>Paused"><span> — paused</span></template>` and `<span x-text="' — priority ' + $store.pipeline.<stage>Priority">`. SVG stays `aria-hidden`.
- **Guard tests** updated as intentional contract changes: `test_gating_triggers_post_only_to_existing_endpoints` split into a pinned 4-endpoint enqueue assertion + a 12-post stage-control assertion (4 per agent stage); `test_topology_column_one_chips_do_not_overlap` `min_chip_height` bumped 150→240 so it now actually guards the taller chips (the old 182px gutters would fail it).

## Task Commits

Each task committed atomically:

1. **Task 1 (TDD): stage_controls macro + attach to 3 agent nodes**
   - `4fc17ac` (test) — RED: 7 control-render tests + `_stage_control_fragment` slicer + extended `_DAG_KEYS` (+6) + rewritten endpoint-surface guard (8 failing)
   - `04361e9` (feat) — GREEN: the `stage_controls` macro + 3 attach calls
2. **Task 2: recompute NODE_LAYOUT + grow canvas/SVG + extend the `<ol>` mirror** — `eecfc70` (feat)
3. **Task 3: bump overlap guard min_chip_height 150→240** — `f78d7c2` (test)

## Files Created/Modified

- `src/phaze/templates/pipeline/partials/dag_canvas.html` — added the `stage_controls` macro + 3 attach calls; recomputed `NODE_LAYOUT` (gutter/h) + canvas/SVG dims; extended the `<ol>` mirror for the 3 agent stages; updated the layout comment block.
- `tests/test_dag_canvas_render.py` — extended `_DAG_KEYS` (+6), added `_AGENT_STAGES` + `_stage_control_fragment`, 7 new control-render tests, rewrote the endpoint-surface guard, bumped the overlap `min_chip_height`.

## Decisions Made

- **Pause/Resume as two static-`hx-post` buttons, not one bound `:hx-post`:** keeps each endpoint statically testable and sidesteps the Alpine-bound-attribute timing question (38-UI-SPEC LOCKED / RESEARCH A4).
- **Authoritative-only store write:** the store is written solely from the server JSON in `@htmx:after-request`; no optimistic mutation, so the 5s poll and an in-flight click never race into a half-applied state (T-38-OOB).
- **Stable `id="stage-controls-<stage>"` on the macro container:** additive, non-visual, mirrors the existing `node-<id>` idiom; lets the render tests slice the exact fragment to assert negative space (`.blocked`/`agentBusy`/`px-1.5` absent). Not a change to the locked visual/interaction contract.
- **col-2/col-3 node y derived from incoming-edge midpoints:** after widening the col-1 gutters, `proposals`/`execute` center on the metadata+analyze / proposals midpoints and `scrape`/`match` on the scan_search midpoint, so the bézier edges still land cleanly.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Endpoint-surface guard rewrite moved into Task 1 (not Task 3)**
- **Found during:** Task 1 (GREEN)
- **Issue:** The plan defers BOTH guard-test updates to Task 3, but attaching the `stage_controls` macro in Task 1 adds 12 new `hx-post="/pipeline/stages/..."` calls, immediately breaking the existing `test_gating_triggers_post_only_to_existing_endpoints` "exactly 4 hx-post" assertion. The suite could not go green at the Task-1 commit without rewriting that guard then.
- **Fix:** Rewrote the endpoint-surface guard into its final documented split form (pinned 4-endpoint enqueue assertion + a separate 12-post stage-control assertion) as part of Task 1's test commit. Task 3 then carried only the overlap `min_chip_height` bump — which legitimately depends on Task 2's layout recompute (bumping it before Task 2 would fail). Net effect: each task commit leaves the full render suite green; both guard updates are still documented as intentional Phase-38 contract changes, exactly as the plan intended.
- **Files modified:** tests/test_dag_canvas_render.py
- **Verification:** `uv run pytest tests/test_dag_canvas_render.py` → 31 passed at every task commit.
- **Committed in:** `4fc17ac` (Task 1 RED), `f78d7c2` (Task 3)

**Total deviations:** 1 auto-fixed (blocking — task-ordering of a forced guard-test update). No scope change.

## Issues Encountered

- The 4 DB-backed integration tests need Postgres; the local dev DB on 5432 was down. Ran the project's ephemeral test DB via `just test-db` (ports 5433/6380) and pointed the suite at it with `TEST_DATABASE_URL=...:5433/phaze_test` — all 31 tests pass (the 23-key OOB-seed integration test now also covers the 6 Phase-38 stage-control keys). The dev DB env-dependency is pre-existing, not a regression.

## Verification

- `uv run pytest tests/test_dag_canvas_render.py` → **31 passed** (27 pure-Jinja + 4 DB integration against the ephemeral test DB).
- Rendered-output spot check: 3 `' — priority '` spans + 3 paused templates in the `<ol>`; canvas/SVG at 1000px; 9 anchor-derived bézier edges intact.
- Pre-commit (ruff, ruff-format, bandit, mypy, file hygiene) passed on every commit — no `--no-verify`.

## User Setup Required

None — no new package, `uv.lock` unchanged. The controls only mutate state once Phase 37's endpoints + table are live (merged on this branch); they render and bind harmlessly otherwise (38-03's degrade-safe reader supplies running/priority-50 defaults).

## Known Stubs

None. The controls are wired to live Phase-37 endpoints and the 38-03 store keys; no placeholder data.

## Threat Flags

None — no new network/auth/file/schema surface introduced. The macro posts only to the existing Phase-37 `/pipeline/stages/*` endpoints (allowlist-validated server-side, `[0,100]` clamp + DB CHECK there), interpolates only the hardcoded allowlist `{{ stage }}` value, and binds ints into `x-text` (T-38-XSS / T-38-DELTA handled at the boundary).

## TDD Gate Compliance

RED gate (`test` commit `4fc17ac`, 8 failing) precedes GREEN gate (`feat` commit `04361e9`); RED verified failing before the macro existed, GREEN verified passing after. No REFACTOR commit needed.

## Self-Check: PASSED

- `src/phaze/templates/pipeline/partials/dag_canvas.html` (stage_controls macro) — FOUND
- `tests/test_dag_canvas_render.py` (control-render tests) — FOUND
- `.planning/.../38-02-SUMMARY.md` — FOUND
- Commits `4fc17ac`, `04361e9`, `eecfc70`, `f78d7c2` — all present in `git log`

---
*Phase: 38-pipeline-dag-pause-priority-ui-and-rescan-button-removal*
*Completed: 2026-06-13*
