---
quick_id: 260609-on2
slug: poll-wire-recent-scans-table-and-stage-c
status: complete
---

# Quick Task Summary: Poll-wire Recent Scans table + stage-card "files ready" counts

Two live dashboard values that were rendered once at page load and never refreshed now
tick every 5s in lockstep with the existing stats poll, without re-rendering any
interactive controls.

## What changed

### A. Recent Scans table self-polls (new GET endpoint)

- Added `GET /pipeline/scans/recent` to `src/phaze/routers/pipeline_scans.py`
  (`recent_scans_partial`). It reuses the existing `build_recent_scans(session)` helper
  (which attaches `_agent_name`, `_elapsed_seconds`, `_seconds_since_progress`,
  `_is_stalled`) and returns the `pipeline/partials/recent_scans_table.html` partial.
- The route is registered **before** `GET /pipeline/scans/{batch_id}` so the literal
  `/recent` path is matched by this handler instead of being captured as a `batch_id`
  UUID path param. `build_recent_scans` is module-level and resolved at call time, so
  placing the decorator above its definition is safe.
- `recent_scans_table.html`'s root `<section id="recent-scans">` is now self-arming:
  `hx-get="/pipeline/scans/recent" hx-trigger="every 5s" hx-swap="outerHTML"`. Each
  swapped-in copy re-arms the poll (same pattern as `scan_progress_card.html`). The
  existing delete button already swaps `#recent-scans` via `outerHTML` returning this
  same partial, so deletes keep the poll armed.

### B. Stage-card "files ready" counts refresh via OOB on the existing stats poll

- `stats_bar.html` (returned by the existing 5s `GET /pipeline/stats` poll) now appends
  two out-of-band paragraphs: `id="analyze-files-ready"` (`{{ stats.discovered }} files
  ready`) and `id="proposals-files-ready"` (`{{ stats.analyzed }} files ready`), both
  `hx-swap-oob="true"` with classes identical to the in-place paragraphs.
- `stage_cards.html` gained the matching `id="analyze-files-ready"` and
  `id="proposals-files-ready"` on its two existing count paragraphs.
- This avoids adding a blunt poll to `#pipeline-stages`, which holds `hx-post` buttons,
  Alpine `x-data` loading state, and the `#analyze-response`/`#proposals-response` divs
  that carry enqueue result messages -- re-rendering them every 5s would clobber an
  in-flight click.

### OOB gated to the poll response only (initial-load defect fix)

`stats_bar.html` is `{% include %}`d at full-page load, so the OOB paragraphs would
render into the page at load time -- but `hx-swap-oob` is only honored during an HTMX
swap, so at load they appeared as stray visible text AND duplicated the `id`s already
present in `stage_cards.html` (invalid duplicate-id DOM). Fixed by wrapping the OOB block
in `{% if oob_counts %}` and setting `oob_counts=True` ONLY in the `/pipeline/stats` poll
handler (`pipeline_stats_partial`). The dashboard full-page include omits the flag, so the
OOB paragraphs are emitted exclusively on the 5s poll response.

## Files changed

- `src/phaze/routers/pipeline_scans.py` -- new `GET /pipeline/scans/recent` route.
- `src/phaze/routers/pipeline.py` -- `pipeline_stats_partial` passes `oob_counts=True`.
- `src/phaze/templates/pipeline/partials/recent_scans_table.html` -- self-arming poll on root `<section>`.
- `src/phaze/templates/pipeline/partials/stats_bar.html` -- two OOB count paragraphs, gated by `{% if oob_counts %}`.
- `src/phaze/templates/pipeline/partials/stage_cards.html` -- matching ids on count paragraphs.
- `tests/test_routers/test_pipeline_scans.py` -- new `/recent` route tests + OOB poll test +
  full-page-omits-OOB / no-duplicate-id test; `test_router_registered_in_main_app` now also
  asserts `/pipeline/scans/recent`.

## Verification

- Affected suites (`test_pipeline_scans.py`, `test_pipeline.py`, `test_pipeline_fingerprint.py`):
  77 passed (includes the new `test_dashboard_full_page_omits_oob_counts`).
- Full suite (`uv run pytest`): 1470 passed, 9 failed, 42 errors.
  - All 9 failures + 42 errors are the PRE-EXISTING redis `ConnectionError` to
    `localhost:6379` (suites: `test_agent_tracklists`, `test_agent_task_router`,
    `test_execution_dispatch`, `test_agent_exec_batches`) -- a sandbox limitation, not a
    regression. No new failures.
- `uv run ruff check` / `ruff format --check` / `uv run mypy` -- clean.
- `pre-commit run --all-files` -- all hooks Passed.

## Known follow-up (out of scope)

The `:disabled="loading || {{ stats.discovered }} === 0"` Alpine binding on the action
buttons is server-rendered once. If a scan starts from exactly 0 discovered and files
appear, the button stays disabled until a full reload. This is an edge case and is not
the reported issue.

## Self-Check: PASSED

- New route present: `GET /pipeline/scans/recent` in `pipeline_scans.py`.
- Templates updated and verified by passing tests.
- All new tests green; pre-commit clean.
