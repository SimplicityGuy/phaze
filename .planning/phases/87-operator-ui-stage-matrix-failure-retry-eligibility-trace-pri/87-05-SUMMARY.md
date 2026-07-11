---
phase: 87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri
plan: 05
subsystem: operator-ui
tags: [status-filter, failure-visibility, url-carried-lens, raw-enum-retirement, grep-guard, ui-01, ui-02, d-03]
requires:
  - "GET /pipeline/files with validated stage+bucket params + files_table_view.html (Plan 04)"
  - "_stage_pill.html / _stage_matrix.html derived pill matrix (Plan 04)"
  - "Status enum values done/in_flight/not_started/failed/skipped (Plan 02)"
provides:
  - "_status_filter_bar.html — URL-carried per-stage bucket filter (stage x bucket selects, hx-push-url, failure-inclusive); ONE canonical surface, no separate failures page"
  - "files_table_view.html filter-aware empty states (unfiltered / failed-filter / other-filter copy per the Copywriting Contract)"
  - "test_no_raw_state_render.py — mutation-tested grep guard forbidding {{ f.state }} / {'text': f.state} renders across pipeline/partials + record templates"
  - "metadata_workspace.html: raw-enum State column RETIRED (the one genuine raw-internal-status render site)"
  - "analyze_workspace.html: derived window-progress column renamed State -> Progress (never a raw-enum render)"
affects:
  - "Plan 06 (pipeline.py retry affordance) renders per-row retry into the same failed-filter lens"
tech-stack:
  added: []
  patterns:
    - "URL-carried filter lens: a form hx-gets /pipeline/files with stage+bucket + hx-push-url so the filter survives the record slide-in and browser back/forward (the rail nav idiom)"
    - "empty-state copy is filter-aware: failed-filter reassurance ('No failed files in {stage}') vs unfiltered onboarding, single-sourced from the Copywriting Contract"
    - "mutation-tested anti-feature grep guard: strips Jinja {# #} comments before scanning (prose mentions never false-positive), matches only the two render forms, filters out f.state comparisons/sets"
key-files:
  created:
    - src/phaze/templates/pipeline/partials/_status_filter_bar.html
    - tests/integration/test_files_filter.py
    - tests/shared/test_no_raw_state_render.py
  modified:
    - src/phaze/templates/pipeline/partials/files_table_view.html
    - src/phaze/templates/pipeline/partials/metadata_workspace.html
    - src/phaze/templates/pipeline/partials/analyze_workspace.html
decisions:
  - "The raw-enum State render existed in exactly ONE place — metadata_workspace.html's {'text': f.state} cell. analyze_workspace's 'State' column renders DERIVED window-progress text (window N/M · running · pending (quota) · complete), never a raw enum; its f.state uses are pure comparisons. So the substantive retirement is the metadata cell; analyze was renamed State->Progress for honesty and its comparisons left intact (they carry the load-bearing WORK-04 mid-flight signal)."
  - "The retired State cell is DROPPED (not replaced by an in-cell pill): _file_table.html's cell contract is text-only ({{ cell.text }}) and CANNOT host the _stage_pill COMPONENT (the exact Plan-04 finding), and _file_table.html is a shared partial outside this plan's declared files. Per-stage status is 'replaced by the pill matrix' at its ONE canonical home — the files table — not duplicated into the single-stage queue workspaces."
  - "Filter bar is a form GET (stage + bucket selects) rather than per-bucket buttons: a GET form serializes both axes into the query string for free, naturally resets to page 1 on change, and round-trips selected state on re-render — the minimal URL-carried surface D-03 asks for."
  - "Added a third empty-state branch ('No files match this filter') for a non-failed active filter that matched nothing: the Copywriting Contract specifies only unfiltered + failed-filter, but a done/skipped filter returning zero must not show the misleading 'No files yet' onboarding copy. Points the operator at Clear filter."
metrics:
  duration: ~40m
  completed: 2026-07-11
  tasks: 2
  files: 6
---

# Phase 87 Plan 05: Failure/status filter lens + raw-enum State retirement Summary

Added the failure/status filter as another LENS over the single paginated files table (UI-02 failure
visibility is a filter, not a third page — D-03), and completed the UI-01 cutover by retiring the last
raw-enum `State` render and locking it shut behind a mutation-tested grep guard. Templates + tests only:
the `GET /pipeline/files` route already validated and plumbed `stage`+`bucket` (Plan 04).

## What Was Built

- **`_status_filter_bar.html` (Task 1)**: a URL-carried per-stage bucket filter — two selects expressing
  "Show files where `{stage}` = `{bucket}`", with the `failed` bucket first-class (UI-02). Wired
  `hx-get="/pipeline/files"` → `#files-table-view`, `hx-push-url="true"` (survives the record slide-in +
  browser back/forward, the rail-nav idiom), `hx-trigger="change"` (user-initiated, no self-poll). An
  empty option clears each axis; the route's allowlist validation degrades unknown/empty to an unfiltered
  page (T-87-16). A `Clear filter` link appears only when a filter is active. Select tokens mirror
  `scan_path_picker.html` verbatim (grid-aligned, dark: pairs, `focus:ring-blue-500`).
- **`files_table_view.html` (Task 1)**: includes the filter bar ABOVE the table and OUTSIDE the rows
  guard (so a filter that returned nothing can always be cleared). The empty state is now filter-aware
  with three branches per the Copywriting Contract: failed-filter ("No failed files in {stage}" /
  "Nothing is stuck in {stage} right now."), any-other-active-filter ("No files match this filter"), and
  unfiltered ("No files yet" / "Discovered files will appear here with their per-stage status." — the
  unfiltered body was also corrected to the UI-SPEC's exact wording). `{stage}` renders a human stage
  name (Metadata/Fingerprint/Analyze/Propose/Approve/Execute).
- **`metadata_workspace.html` (Task 2)**: RETIRED the raw-enum `State` column — the `{'text': f.state}`
  cell was the one genuine "raw internal status string" render (it emitted `metadata_extracted` /
  `fingerprinted` / `analyzed` / `discovered`). Removed the column, the cell, and the now-dead
  `extracted`/`state_color` derivation. Per-stage status now lives only at its canonical home: the
  derived `_stage_matrix` pill row on the files table.
- **`analyze_workspace.html` (Task 2)**: this "State" column renders DERIVED window-progress text
  (`window N/M` · `running · 14/41 windows` · `pending (quota)` · `complete` · `failed`), NEVER a raw
  `f.state`. Renamed the header `State` → `Progress` to make explicit that the retired raw-status surface
  does not live here; the `f.state == '...'` lines are pure comparisons driving friendly words (the
  load-bearing WORK-04 mid-flight signal) and were left intact.
- **`test_no_raw_state_render.py` (Task 2)**: scans `pipeline/partials/` + `record/` templates for the
  two raw-enum render forms — `{{ f.state }}` (optionally filtered) and `{'text': f.state}` — and asserts
  NONE remain. It strips `{# … #}` Jinja comments before scanning (so a partial can DOCUMENT the
  retirement without tripping the guard) and matches only render forms (comparisons/sets are filtered
  out). A companion self-test proves both regexes actually match (guard is not a vacuous no-op) and that
  comparisons/comments do not.
- **`test_files_filter.py` (Task 1)**: seeds mixed metadata buckets (failed / done / not-started) and
  asserts `?stage=metadata&bucket=failed` renders ONLY the failed row; that an empty failed filter renders
  the failed-filter copy (not "No files yet"); and that the filter bar carries URL state (`hx-push-url`,
  selected options round-trip).

## How to Verify

With the test DB up (port 5433, DB `phaze_test`):
```
export TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test"
export PHAZE_QUEUE_URL="postgresql://phaze:phaze@localhost:5433/phaze_test"
uv run pytest tests/integration/test_files_filter.py tests/shared/test_no_raw_state_render.py -q      # 3 + 2 passed
uv run pytest tests/shared/core/test_enrich_analyze_workspaces.py tests/shared/core/test_identify_workspaces.py -q  # 25 passed (no workspace regression)
```
- Combined plan + neighboring suites: `test_files_filter` (3) + `test_files_page` (5) +
  `test_no_raw_state_render` (2) + `test_stage_pill_render` (10) + the two workspace suites (25) →
  **45 passed**.
- `uv run ruff check .` + `uv run mypy .` ran green via pre-commit on both task commits.

### Grep-guard mutation observation (recorded per the project mutation-test rule)

Re-added a real `{'text': f.state, 'color': 'text-gray-500'}` cell to `metadata_workspace.html`'s
`rows.append` (a genuine render site, NOT a comment) → `test_no_raw_enum_state_render_in_operator_templates`
went **RED** (1 failed, flagging `metadata_workspace.html:…`). Removed the planted cell → **GREEN** (2
passed). The guard has teeth: the retired render cannot silently return. (Note: the comment-stripping means
the explanatory `{'text': f.state}` mention inside the retirement comment on line 41 does NOT false-positive.)

### Counts will look different, not broken (UI-SPEC operator note)

As the `failed`/`skipped` buckets and simultaneous per-stage eligibility become filterable, the numbers an
operator sees will shift relative to the old serially-gated `State` view. This is the intended effect of the
UI-01 cutover, not a regression.

## Deviations from Plan

**1. [Rule 3 — Design] Retired State cell is DROPPED, not replaced by an in-cell pill**
- **Found during:** Task 2.
- **Issue:** The plan says "replace the `{'text': f.state}` cell with a `_stage_pill` cell." But these
  workspaces feed the shared `_file_table.html`, whose cell contract is text-only (`{{ cell.text }}`,
  autoescaped) and CANNOT host the `_stage_pill` COMPONENT (the exact Plan-04 finding). `_file_table.html`
  is a shared partial (discover/fingerprint use it too) and is NOT in this plan's declared files, so
  editing it to host a component is out of scope and would risk parallel work.
- **Fix:** Dropped the raw-enum State column from `metadata_workspace.html`. Per-stage status is "replaced
  by the pill matrix" at its ONE canonical home — the files-table `_stage_matrix` (UI-01) — rather than
  duplicated into a single-stage pending queue. No signal lost: the metadata workspace is the
  metadata-PENDING queue; a per-row raw enum there was the anti-feature itself.
- **Files:** `metadata_workspace.html`. **Commit:** 880f4b33.

**2. [Rule 1 — Correctness] analyze_workspace had NO raw-enum render to retire; renamed header instead**
- **Found during:** Task 2.
- **Issue:** The plan/UI-SPEC name `analyze_workspace.html:81-86` as a raw-enum State cell, but on
  inspection that column renders DERIVED window-progress text and its `f.state` uses are COMPARISONS
  (`f.state == 'awaiting_cloud'`), never a `{{ f.state }}` render. It already satisfies the anti-feature.
- **Fix:** Renamed the column header `State` → `Progress` (honest labelling of the retired raw-status
  surface) and preserved the derived window signal + comparisons intact (WORK-04 is asserted by
  `test_analyze_file_table_lane_and_windows`, still green). The grep guard now covers this partial so a
  future raw render here would fail.
- **Files:** `analyze_workspace.html`. **Commit:** 880f4b33.

**3. [Rule 2 — Correctness] Added a third empty-state branch for non-failed filters**
- **Found during:** Task 1.
- **Issue:** The Copywriting Contract specifies only unfiltered + failed-filter empty copy. A non-failed
  active filter (e.g. `bucket=done`) that matches nothing would otherwise render the misleading
  unfiltered "No files yet / Discovered files will appear here" onboarding copy.
- **Fix:** Added a middle branch — "No files match this filter" / "Try a different stage or status, or
  clear the filter above." — for any active filter that isn't the failed case. Does not contradict the
  contract; fills the gap it left.
- **Files:** `files_table_view.html`. **Commit:** 3d9aebdd.

No auth gates, no architectural (Rule 4) escalations, no package installs. No out-of-scope issues found
during this plan; `deferred-items.md` not appended (the pre-existing FAIL-04 xfail entry there is from an
earlier wave and is untouched).

## Threat Register Coverage

- **T-87-15** (Information Disclosure — rendering raw internal status strings): mitigated — the one raw
  `{'text': f.state}` render is removed and a mutation-tested grep guard forbids `{{ f.state }}` /
  `{'text': f.state}` renders across the operator surface templates permanently.
- **T-87-16** (Tampering — filter param injection): mitigated at the route (Plan 04) — `stage`/`bucket`
  validated against the `Stage`/`Status` allowlists (unknown/empty → unfiltered); the filter bar template
  only READS the validated `active_stage`/`active_bucket` and renders them as autoescaped option values.
- **T-87-17** (DoS — filter widening the poll to a whole-corpus scan): accepted/unchanged — the filter
  rides the SAME bounded `get_files_page` (LIMIT+1 sentinel, no COUNT); this plan adds no new query.

No new threat surface introduced (templates + tests only; no route/service/schema change).

## Self-Check: PASSED

All 3 created files + 3 modified files present on disk; both task commits (3d9aebdd, 880f4b33) in git
history. (Verified below at write time.)
