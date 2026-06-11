---
phase: 34-pipeline-queue-depth-status-double-enqueue-guard
plan: "02"
subsystem: pipeline-ui
tags: [htmx-oob, alpine-store, queue-depth, router-context, degrade-to-200]
requires:
  - "get_queue_activity(app_state, session) six-key dict (Plan 01)"
  - "FakeQueue.count + set_counts / FakeTaskRouter.set_counts (Plan 00)"
  - "stats_bar.html OOB + x-init $store.pipeline pattern (existing)"
provides:
  - "queue_progress_percent(analyzed, agent_busy) pure helper -- single source of truth for the DB-derived Processing bar"
  - "dashboard() + pipeline_stats_partial() both carry agent_busy/controller_busy/the four sub-counts/queue_progress_percent"
  - "stats_bar.html agent-busy-seed / controller-busy-seed OOB store-write nodes (poll-only, gated by oob_counts)"
affects:
  - "Plan 03 (processing card consumes queue_progress_percent + the six counts)"
  - "Plan 04 (button :disabled bindings consume $store.pipeline.agentBusy/controllerBusy seeded by these OOB nodes)"
tech-stack:
  added: []
  patterns:
    - "Pure module-level helper extracted from an inline ratio so the formula is unit-testable from raw ints (reversed-denominator guard)"
    - "Mirror the existing hx-swap-oob + x-init $store.pipeline store-write pattern for new live counts; gate to poll responses via oob_counts"
    - "Spread the service dict into the template context (**activity) so all six keys flow without per-key restatement"
key-files:
  created: []
  modified:
    - src/phaze/routers/pipeline.py
    - src/phaze/services/pipeline.py
    - src/phaze/templates/pipeline/partials/stats_bar.html
    - tests/test_routers/test_pipeline.py
decisions:
  - "queue_progress_percent placed in services/pipeline.py (next to get_queue_activity) as a pure helper -- closes plan-check W1 (an inline router expression could harbor a reversed denominator no echo-only test would catch)"
  - "No new try/except in either endpoint -- get_queue_activity already isolates failures and returns zeros, so the degrade-to-200 guarantee is inherited"
  - "The two busy-seed nodes render class='hidden' with no visible text -- their sole job is the x-init store write; ids are the same-id contract with Plan 04's stage_cards in-place anchors"
metrics:
  duration: "~10 min"
  completed: "2026-06-11"
  tasks: 3
  files: 4
---

# Phase 34 Plan 02: Surface Queue Activity Through the Existing Poll + Initial Render Summary

Wired the Plan-01 `get_queue_activity` queue-depth read into BOTH the initial `/pipeline/` full-page render and the existing 5s `/pipeline/stats` poll -- no new loop -- and extracted the DB-derived progress formula into a pure, unit-testable `queue_progress_percent(analyzed, agent_busy)` helper. Added two hidden `hx-swap-oob` store-write nodes to `stats_bar.html` that re-push `$store.pipeline.agentBusy`/`controllerBusy` on each tick, mirroring the established `discovered`/`analyzed` OOB pattern exactly. The live queue depth is now available to the Plan 03 card and the Plan 04 button gating, and router tests prove the poll cannot 500.

## What Was Built

- **`queue_progress_percent(analyzed: int, agent_busy: int) -> int`** in `services/pipeline.py` (alongside `get_queue_activity`): `round(analyzed / denom * 100) if (denom := analyzed + agent_busy) else 0`. The single source of truth for the operator-chosen progress formula (`done` = DB `analyzed`, denominator = `analyzed + agent_busy`), extracted as a pure helper so it is testable from raw inputs and divide-by-zero guarded.
- **`routers/pipeline.py`**: extended the `get_files_by_state, get_pipeline_stats` import to also bring in `get_queue_activity` and `queue_progress_percent`. In `dashboard()` and `pipeline_stats_partial()`, after computing `stats`, both call `activity = await get_queue_activity(request.app.state, session)`, compute `queue_progress = queue_progress_percent(stats["analyzed"], activity["agent_busy"])`, and merge `**activity` (the six keys) plus `queue_progress_percent=queue_progress` into their template contexts. No new try/except added (the service self-isolates); `agents`/`recent_scans`/`oob_counts` left intact; `HTMLResponse` return types unchanged.
- **`stats_bar.html`**: inside the existing `{% if oob_counts %}` gate, added `<p id="agent-busy-seed" hx-swap-oob="true" x-init="$store.pipeline.agentBusy = {{ agent_busy }}" class="hidden">` and the `controller-busy-seed` twin for `controllerBusy`. Hidden, no visible text; a Jinja comment documents the same-id contract with `stage_cards.html`. Kept inside the gate so the initial full-page include omits them (htmx honors `hx-swap-oob` only during a swap; the initial seed lives in `stage_cards.html` via Plan 04).
- **Four tests** appended to `tests/test_routers/test_pipeline.py`:
  1. `test_pipeline_stats_degrades_without_queues` -- no fakes wired → GET `/pipeline/stats` AND `/pipeline/` both 200 (AttributeError degrade keeps both alive).
  2. `test_pipeline_stats_surfaces_agent_busy` -- agent depth 4+1=5, controller 2+0=2 → body contains `"$store.pipeline.agentBusy = 5"` and `"$store.pipeline.controllerBusy = 2"`.
  3. `test_dashboard_seeds_busy_on_first_load` -- wired queues → initial `/pipeline/` render 200 (no 500 on first load).
  4. `test_queue_progress_percent_formula` -- imports the helper, asserts `(30,10)==75`, `(0,0)==0`, `(11428,0)==100`.

## Deviations from Plan

None - plan executed exactly as written.

## Verification

- `uv run mypy src/phaze/routers/pipeline.py src/phaze/services/pipeline.py` -- Success, no issues.
- `uv run ruff check src/phaze/routers/pipeline.py src/phaze/services/pipeline.py tests/test_routers/test_pipeline.py` -- All checks passed.
- `grep -c get_queue_activity src/phaze/routers/pipeline.py` → 5 (≥3); `grep -c queue_progress_percent` → 7 (≥3).
- `grep -c agent-busy-seed` / `controller-busy-seed` in stats_bar.html → 2 each; Jinja `get_template` parses without error.
- `uv run pytest tests/test_routers/test_pipeline.py -k "busy or degrade or progress_percent" -q` → 4 passed.
- `uv run pytest tests/test_routers/test_pipeline.py -q` → 35 passed (the previously-green `test_dashboard_page` / `test_pipeline_stats_partial` / `test_dashboard_includes_settings_batch_size` remain green -- degrade proven, no regression).
- Pre-commit hooks (ruff, ruff-format, bandit, mypy `uv run mypy .`) passed on all three code commits.

## Commits

- `0ba94ad` feat(34-02): wire queue activity + guarded percent into both pipeline contexts
- `096fc03` feat(34-02): add agentBusy/controllerBusy OOB store-write nodes to stats_bar
- `4a623fa` test(34-02): degrade-to-200 + wired busy counts surfaced + percent formula

## Self-Check: PASSED

- FOUND: src/phaze/services/pipeline.py (`def queue_progress_percent` present)
- FOUND: src/phaze/routers/pipeline.py (`get_queue_activity` import + both call sites)
- FOUND: src/phaze/templates/pipeline/partials/stats_bar.html (agent-busy-seed / controller-busy-seed nodes)
- FOUND: tests/test_routers/test_pipeline.py (4 new tests)
- FOUND: commit 0ba94ad
- FOUND: commit 096fc03
- FOUND: commit 4a623fa
