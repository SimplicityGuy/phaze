---
phase: 60-review-apply
plan: 02
subsystem: ui
tags: [htmx, jinja2, alpine, review-apply, diff, proposals, shell]

# Dependency graph
requires:
  - phase: 60-review-apply
    plan: 01
    provides: "PATCH /proposals/bulk-approve-high-confidence (D-02) + /proposals/{id}/edit (D-05) + approve/reject/undo routes; Wave-0 test scaffold + seed factories"
  - phase: 57-shell-dag-rail
    provides: "shell.py _render_stage fork + STAGE_PARTIALS whitelist + single /pipeline/stats poll (R-2)"
  - phase: 58-enrich-analyze-workspaces
    provides: "_workspace_scaffold.html + _file_table.html + _workspace_poll_seeds.html; degrade-safe service-helper pattern"
provides:
  - "pipeline/partials/_diff_row.html — ONE shared before→after diff row (facet-parameterized: filename/path/tag), verified PATCH verbs, Alpine LOCAL inline-edit island (R-6) [D-06]"
  - "pipeline/partials/rename_workspace.html + move_workspace.html — the two Review diff workspaces over one RenameProposal source (filename vs proposed_path facet)"
  - "services/review.get_pending_proposal_rows — SAVEPOINT-wrapped degrade-safe read mapping pending proposals to plain dicts"
  - "shell.py /s/rename + /s/move wiring (static-literal STAGE_PARTIALS, T-57-01)"
affects: [60-03-tagwrite-dedupe, 60-04-cue, 61-record-slidein]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "ONE facet-parameterized Jinja partial: caller passes before/after/urls/edit_facet + a templated approve verb (hx-{{ approve_method|default('patch') }}) so a later facet can pass POST without editing the literal"
    - "Alpine LOCAL x-data island per diff row; SAVE EDIT hx-includes + hx-targets ONLY its own row id with outerHTML (R-6) — the counts-only poll never clobbers an in-progress edit"
    - "Degrade-safe review read helper: get_proposals_page wrapped in session.begin_nested(), plain dicts out, [] on error (mirrors get_analyze_stage_files)"

key-files:
  created:
    - src/phaze/templates/pipeline/partials/_diff_row.html
    - src/phaze/templates/pipeline/partials/rename_workspace.html
    - src/phaze/templates/pipeline/partials/move_workspace.html
    - src/phaze/services/review.py
  modified:
    - src/phaze/routers/shell.py
    - tests/test_review_apply_workspaces.py

key-decisions:
  - "Approve verb is templated (hx-{{ approve_method|default('patch') }}); skip/undo/edit stay literal hx-patch — rename/move keep PATCH, Plan 60-03 tag facet can pass approve_method='post' without editing _diff_row.html"
  - "_diff_row.html carries the applied/approved UNDO branch (row_state != 'pending') in source for the Pattern-6 lifecycle; get_pending_proposal_rows returns only PENDING rows so the workspaces render the pending branch (Approve/Edit/Skip)"
  - "SAVE EDIT uses hx-include=\"#{row_id_prefix}-{pid}\" (the whole row) not \"closest div\" — the name=proposed input and the hidden facet field live in different child divs; the row-id selector pulls both"
  - "Bulk R-4 busy-gate binds $store.pipeline.controllerBusy (a real base.html store key; proposals are controller-side work) alongside hx-disabled-elt; no invented store key"

requirements-completed: [REVIEW-01, REVIEW-02]

# Metrics
duration: 20min
completed: 2026-07-01
---

# Phase 60 Plan 02: Rename & Move Review Workspaces Summary

**The ONE shared before→after `_diff_row.html` partial (D-06) plus the Rename/Path and Move-files review diff workspaces — per-file Approve/Edit/Skip over verified PATCH routes and the id-less server-predicate bulk-approve header — wired at `/s/rename` and `/s/move` over a degrade-safe pending-proposal read.**

## Performance

- **Duration:** ~20 min
- **Started:** 2026-07-01
- **Completed:** 2026-07-01
- **Tasks:** 3
- **Files created/modified:** 6

## Accomplishments
- **D-06 shared diff row:** `_diff_row.html` is ONE partial parameterized by facet vars (`row_id_prefix`, `before`/`after`, `approve_url`/`skip_url`/`undo_url`/`edit_url`, `edit_facet`). Rose+`line-through` BEFORE, emerald AFTER, fixed `grid-cols-[1fr_auto_1fr]`, every cell autoescaped (never raw). The APPROVE verb is templated (`hx-{{ approve_method|default('patch') }}`) so the 60-03 Tag facet can pass a POST verb without touching the file; rename/move stay PATCH.
- **REVIEW-01 inline edit (R-6):** the AFTER cell swaps (Alpine `x-if`) for a `name="proposed"` input inside a per-row LOCAL `x-data` island; SAVE EDIT `hx-patch`es `/proposals/{id}/edit`, `hx-include`s the row's own `proposed`+`facet` inputs, and `hx-target`s ONLY its own row id with `hx-swap="outerHTML"`. DISCARD sends no request.
- **REVIEW-02 bulk approve (D-02):** each workspace header renders exactly one amber-attention **APPROVE ALL ≥90% CONFIDENCE** button posting the id-less/threshold-less `PATCH /proposals/bulk-approve-high-confidence` with `hx-confirm` (predicate + live count) + `hx-disabled-elt` + a `controllerBusy` busy-gate (R-4). No client `proposal_ids` markup — the counts-only poll (R-2) can't corrupt a mass-approve.
- **Two-node split (D-06 LOCKED):** `rename_workspace.html` (filename facet: before=original_filename, after=proposed_filename) and `move_workspace.html` (path facet: before=current_path, after=proposed_path, `facet=path`) are siblings over the one `RenameProposal` source, with per-UI-SPEC titles/sub-counts/empty states.
- **Degrade-safe read:** `services/review.get_pending_proposal_rows` wraps `get_proposals_page(status="pending")` in a SAVEPOINT and returns plain dicts (`[]` on DB error); the `_render_stage` rename/move branches use it with no router try/except and `oob_counts` unchanged (False).
- **Wiring (T-57-01):** `STAGE_PARTIALS["rename"]`/`["move"]` are static string literals; `/s/rename` and `/s/move` return bare fragments (one `<h1 tabindex="-1">`, no `<html>`/`<head>`); dead-template + shell-route guards stay green.

## Task Commits

Each task committed atomically:

1. **Task 1: shared `_diff_row.html` partial (D-06)** — `11fb24f` (feat)
2. **Task 2: rename + move workspaces + `review.py` helper** — `26e2b01` (feat)
3. **Task 3: shell.py `/s/rename` + `/s/move` wiring + test conversion** — `286a3c0` (feat)

## Files Created/Modified
- `src/phaze/templates/pipeline/partials/_diff_row.html` (created) — the shared before→after diff row.
- `src/phaze/templates/pipeline/partials/rename_workspace.html` (created) — Rename/Path diff workspace + amber bulk header.
- `src/phaze/templates/pipeline/partials/move_workspace.html` (created) — Move-files diff workspace (proposed_path facet).
- `src/phaze/services/review.py` (created) — `get_pending_proposal_rows` degrade-safe read helper.
- `src/phaze/routers/shell.py` (modified) — static-literal `STAGE_PARTIALS` rename/move + `_render_stage` branches + import.
- `tests/test_review_apply_workspaces.py` (modified) — converted `test_diff_row_before_after` (both facets over `/s/rename` + `/s/move`); added fragment assertions to the bulk-predicate + own-row-edit tests.

## Decisions Made
- **`hx-include="#{row_id_prefix}-{pid}"` over the UI-SPEC's `closest div`.** The `name="proposed"` input (in the body grid, inside an Alpine `x-if` template) and the hidden `facet` field (row-level) live in different child divs, so `closest div` from the SAVE EDIT button would miss one. Targeting the row id includes both descendant inputs — unambiguous and R-6-safe.
- **UNDO branch shipped in the partial source, not rendered for pending rows.** `_diff_row.html` carries the Pattern-6 `approved`/`applied ✓` + neutral UNDO branch under `{% if row_state != 'pending' %}`; the workspaces pass `row_state="pending"` (and `get_pending_proposal_rows` only returns pending), so the pending Approve/Edit/Skip cluster renders while the lifecycle branch stays ready for a later state-carrying caller.
- **Bulk busy-gate binds `controllerBusy`.** Proposals are controller-side work; `controllerBusy` is a real `base.html` `$store.pipeline` key (seeded in `_workspace_poll_seeds.html`), so the `:disabled` gate is safe and correct — no invented store key.

## Deviations from Plan

None — plan executed as written. The only interpretation choices (documented above under Decisions) are the `hx-include` selector and the `controllerBusy` busy-gate key; both are executor-discretion details the plan left open, and neither changes the sanctioned surface.

## Known Stubs
None — both workspaces render live pending proposals through the wired read helper. The dedupe/cue behavior tests remain `xfail` by design (owned by Plans 60-03/60-04).

## Threat Flags
None — the two `STAGE_PARTIALS` values are static literals (T-57-01), the bulk button is id-less/threshold-less (T-60-01), all diff cells autoescape (T-60-XSS), SAVE EDIT scopes to its own row (T-60-R6), and the read helper is SAVEPOINT-degrade-safe (T-60-DOS). No new network/auth/schema surface — proposals.py was not modified this plan.

## Self-Check: PASSED
- Created files exist: `_diff_row.html`, `rename_workspace.html`, `move_workspace.html`, `services/review.py` — all present.
- Commits exist: `11fb24f`, `26e2b01`, `286a3c0` — all in git log.
- Verification green: `tests/test_review_apply_workspaces.py tests/test_dead_template_guard.py tests/test_shell_routes.py` → 14 passed, 2 xfailed (dedupe/cue stubs for later plans); `mypy src/phaze/routers/shell.py src/phaze/services/review.py` clean; `ruff` clean.

---
*Phase: 60-review-apply*
*Completed: 2026-07-01*
