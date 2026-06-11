---
phase: 34-pipeline-queue-depth-status-double-enqueue-guard
plan: "04"
subsystem: pipeline-ui
tags: [alpine-store, htmx, double-enqueue-guard, queue-depth, jinja2]
requires:
  - phase: 34-02
    provides: "agent-busy-seed / controller-busy-seed OOB store-write nodes + $store.pipeline.agentBusy/controllerBusy populated by the 5s poll"
provides:
  - "Four pipeline-action buttons render: Run Analysis, Extract Metadata, Fingerprint, Generate Proposals"
  - "Coarse double-enqueue guard: Run Analysis/Fingerprint/Extract-Metadata gate on $store.pipeline.agentBusy > 0; Generate Proposals on controllerBusy > 0"
  - "Alpine store defaults agentBusy:0, controllerBusy:0, metadataExtracted:0 so every :disabled binding has a defined value before the first poll"
  - "In-place agent-busy-seed / controller-busy-seed x-init anchors seed the busy store at initial page load"
affects: []
tech-stack:
  added: []
  patterns:
    - "Mirror the existing Run Analysis card (x-data loading, hx-post, own response div, trigger_response.html, spinner) to surface new pipeline actions with zero new endpoints"
    - "Coarse client-side enqueue guard: a single store key (agentBusy) disables all three agent-task buttons because they contend for one serial worker queue"
    - "In-place (non-OOB) x-init seed anchors share ids with the poll's OOB nodes so the same store key is correct at load AND refreshed every 5s"
key-files:
  created:
    - tests/test_template_helpers/test_stage_cards_partial.py
  modified:
    - src/phaze/templates/base.html
    - src/phaze/templates/pipeline/partials/stage_cards.html
    - tests/test_routers/test_pipeline_scans.py
key-decisions:
  - "Card order: Run Analysis, Extract Metadata, Fingerprint, Generate Proposals — the two new cards slot between the existing pair in rough pipeline order (Claude's Discretion per CONTEXT)"
  - "Extract Metadata uses a static 'Backfill all music/video files' label and gates only on loading || agentBusy>0 (no per-state ready count — CONTEXT-locked)"
  - "Fingerprint ready-count source = stats.metadata_extracted, seeded into $store.pipeline.metadataExtracted (CONTEXT-locked)"
patterns-established:
  - "Pattern: extend an existing reactive :disabled with ' || $store.pipeline.<busy> > 0' to add a double-enqueue guard without touching the server enqueue path"
requirements-completed: [Q34-5]
duration: 18min
completed: 2026-06-11
---

# Phase 34 Plan 04: Four Pipeline Buttons + Double-Enqueue Guard Summary

**Surfaced all four pipeline actions (added Fingerprint + Extract Metadata) and closed the double-enqueue hole by gating the three agent-task buttons on live `$store.pipeline.agentBusy` and Generate Proposals on `controllerBusy`, seeded from the server render so the guard is correct before the first poll.**

## Performance

- **Duration:** ~18 min
- **Completed:** 2026-06-11
- **Tasks:** 3 (+ 1 in-scope deviation fix)
- **Files modified:** 3 (+ 1 created)

## Accomplishments

- **base.html store defaults (Task 1):** Extended `Alpine.store('pipeline', {...})` from `{ discovered: 0, analyzed: 0 }` to also define `metadataExtracted: 0, agentBusy: 0, controllerBusy: 0`, and rewrote the adjacent comment to document the four-button gating contract. Every `:disabled` binding now has a defined default before the first poll (never reads `undefined`).
- **stage_cards.html — two new buttons + gating + seed (Task 2):**
  - Added a **Fingerprint** card (`hx-post="/pipeline/fingerprint"` → `#fingerprint-response`, `#fingerprint-spinner`) mirroring Run Analysis exactly, with `:disabled="loading || $store.pipeline.metadataExtracted === 0 || $store.pipeline.agentBusy > 0"` and an in-place `id="fingerprint-files-ready"` `x-init` anchor seeding `metadataExtracted` from `stats.metadata_extracted`.
  - Added an **Extract Metadata** card (`hx-post="/pipeline/extract-metadata"` → `#extract-metadata-response`) with a static "Backfill all music/video files" label and `:disabled="loading || $store.pipeline.agentBusy > 0"` (no per-state ready count).
  - Extended Run Analysis `:disabled` with `|| $store.pipeline.agentBusy > 0` and Generate Proposals with `|| $store.pipeline.controllerBusy > 0`.
  - Added two hidden in-place seed anchors `id="agent-busy-seed"` / `id="controller-busy-seed"` carrying the `x-init` store-writes (same ids the Plan 02 OOB poll nodes re-seed every 5s).
- **Render tests (Task 3):** New `tests/test_template_helpers/test_stage_cards_partial.py` (mirrors `test_progress_partial.py`) with four tests — all four `hx-post` endpoints render; the three agent buttons gate on `agentBusy`; Generate Proposals gates on `controllerBusy` (and NOT `agentBusy`); the in-place busy-seed anchors render the correct `x-init` store-writes (`agentBusy = 5`, `controllerBusy = 2`).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated three `test_pipeline_scans.py` assertions that encoded the pre-change store/binding literals**
- **Found during:** Phase quality gate (full suite run).
- **Issue:** Three existing tests asserted the exact strings the plan intentionally changed: the frozen `Alpine.store('pipeline', { discovered: 0, analyzed: 0 })` literal and the two un-extended `:disabled` bindings. They are directly downstream of this plan's Task 1/Task 2 edits (in scope).
- **Fix:** `test_dashboard_seeds_pipeline_store_from_server_count` now asserts the five-key store literal; `test_button_disabled_binds_to_store_not_frozen_literal` now asserts the agentBusy/controllerBusy-extended bindings.
- **Files modified:** `tests/test_routers/test_pipeline_scans.py`
- **Commit:** `ccf9b1f`

**2. [Rule 1 - Bug] Reworded the stage_cards seed-anchor HTML comment to avoid the literal token `hx-swap-oob`**
- **Found during:** Phase quality gate.
- **Issue:** `test_dashboard_full_page_omits_oob_counts` asserts `"hx-swap-oob" not in response.text`; my explanatory comment contained that exact token as a substring, producing a false positive.
- **Fix:** Reworded the comment to say "out-of-band swap attribute" / "out-of-band nodes" instead of the literal attribute name. No behavior change.
- **Files modified:** `src/phaze/templates/pipeline/partials/stage_cards.html`
- **Commit:** `ccf9b1f`

## Verification

- `uv run pytest tests/test_template_helpers/test_stage_cards_partial.py -q` → 4 passed.
- `uv run pytest tests/test_routers/test_pipeline_scans.py -q` → 59 passed (the three previously-frozen assertions now track the extended store/bindings).
- Phase quality gate (full suite minus the four Redis-dependent integration files — see Deferred Issues): **1581 passed, coverage 96.06%** (≥85% required).
- `uv run ruff check .` → All checks passed. `uv run ruff format --check .` → 286 files already formatted. `uv run mypy .` → Success, no issues in 143 source files.
- Pre-commit hooks (ruff, ruff-format, bandit, mypy) passed on every commit.

## Deferred Issues

- **Redis-dependent integration tests cannot run in this sandbox (no `localhost:6379`):** `tests/test_services/test_agent_task_router.py`, `tests/test_routers/test_agent_tracklists.py`, `tests/test_routers/test_agent_exec_batches.py`, `tests/test_routers/test_execution_dispatch.py` fail/error purely on Redis connection refused. These files are entirely unrelated to this plan's template changes (they exercise SAQ queue routing) and import nothing this plan touched — verified all failures are `localhost:6379` connection errors. Out of scope per the SCOPE BOUNDARY rule (pre-existing, environmental). They run green in CI where the Redis service is provisioned.

## Commits

- `ed41575` feat(34-04): add agentBusy/controllerBusy/metadataExtracted store defaults
- `126efa9` feat(34-04): add Fingerprint + Extract Metadata buttons, coarse busy gating
- `13dbb34` test(34-04): stage_cards render tests for four buttons + busy gating
- `ccf9b1f` fix(34-04): update store/binding assertions for the four-button gating

## Self-Check: PASSED

- FOUND: src/phaze/templates/base.html (all five store keys at 0)
- FOUND: src/phaze/templates/pipeline/partials/stage_cards.html (four buttons + seed anchors + agentBusy/controllerBusy bindings)
- FOUND: tests/test_template_helpers/test_stage_cards_partial.py (4 tests)
- FOUND: commit ed41575
- FOUND: commit 126efa9
- FOUND: commit 13dbb34
- FOUND: commit ccf9b1f
