---
phase: 60-review-apply
plan: 03
subsystem: ui
tags: [htmx, jinja2, alpine, review-apply, propose, tag-write, diff, shell]

# Dependency graph
requires:
  - phase: 60-review-apply
    plan: 01
    provides: "POST /tags/{id}/write + /tags/{id}/undo + /tags/bulk-write-no-discrepancies (D-03) routes; Wave-0 test scaffold + seed factories"
  - phase: 60-review-apply
    plan: 02
    provides: "shared _diff_row.html (facet-parameterized diff row) + services/review.get_pending_proposal_rows + shell.py _render_stage rename/move branches"
  - phase: 57-shell-dag-rail
    provides: "shell.py _render_stage fork + STAGE_PARTIALS whitelist + single /pipeline/stats poll (R-2)"
  - phase: 58-enrich-analyze-workspaces
    provides: "_workspace_scaffold.html + _file_table.html; degrade-safe service-helper pattern"
provides:
  - "pipeline/partials/propose_workspace.html — the D-01 thin GENERATION view (pending RenameProposal list + Model + Conf + GENERATE ALL over the existing /pipeline/proposals trigger); NOT a diff"
  - "pipeline/partials/tagwrite_workspace.html — the Tag-write review diff workspace over the shared _diff_row.html (tag facet: APPROVE->POST /tags/{id}/write, UNDO->POST /tags/{id}/undo, bulk->POST /tags/bulk-write-no-discrepancies)"
  - "services/review.get_tagwrite_review_rows — SAVEPOINT-wrapped degrade-safe read of EXECUTED files w/o a COMPLETED TagWriteLog (Pitfall 3), >=1-change tag comparisons mapped to plain dicts"
  - "_diff_row.html backward-compatible show_edit/show_skip (default true) + show_undo (default false) + undo_method flags — the tag facet suppresses SAVE-EDIT/SKIP and surfaces a POST UNDO without touching rename/move"
  - "shell.py /s/propose + /s/tagwrite wiring (static-literal STAGE_PARTIALS, T-57-01; propose sets configured llm_model, A1)"
affects: [60-04-cue, 61-record-slidein]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Backward-compatible facet flags on a shared partial: `show_edit`/`show_skip` default true, `show_undo` default false, `undo_method` templated — a new facet opts out/in without editing existing callers (rename/move output byte-identical)"
    - "Propose is a GENERATION view, not a diff: reuse _file_table.html (not _diff_row.html) over the SAME get_pending_proposal_rows read; the Model column is the configured settings.llm_model (A1), not a per-row field"
    - "Second degrade-safe review read helper (get_tagwrite_review_rows): mirror tags.list_tags EXECUTED-only query inside session.begin_nested(), compute compute_proposed_tags + _build_comparison, plain dicts out, [] on error"

key-files:
  created:
    - src/phaze/templates/pipeline/partials/propose_workspace.html
    - src/phaze/templates/pipeline/partials/tagwrite_workspace.html
  modified:
    - src/phaze/templates/pipeline/partials/_diff_row.html
    - src/phaze/services/review.py
    - src/phaze/routers/shell.py
    - tests/test_review_apply_workspaces.py

key-decisions:
  - "Tag SKIP is omitted (show_skip=false): the plan's VERIFIED interface endpoint list carries NO tag-skip route (tags are computed, not stored RenameProposal rows), so wiring a SKIP would target a non-existent endpoint. APPROVE (write) + UNDO cover the tag lifecycle; documented as an interface reconciliation, not new surface."
  - "Tag UNDO renders in the PENDING cluster (show_undo=true) rather than only the row_state!='pending' lifecycle branch — the tag queue is all-pending (EXECUTED files awaiting a write), so a lifecycle-only UNDO would never render; a tag write is reversible via /tags/{id}/undo (before_tags restore, REVIEW-05)."
  - "get_tagwrite_review_rows filters to changed_count >= 1 — a zero-change file has nothing to write; matches the 'No tag changes pending / Every file's tags already match' empty state."
  - "Model column reads the module-level ControlSettings singleton (settings.llm_model) — a plain str, one model per run (A1); no DB, no per-row field. shell.py imports `settings` directly (mypy-clean ControlSettings type), mirroring pipeline.py."
  - "review.py imports the pure tags.py query/compare helpers (_build_comparison/_count_changes/_get_tracklist_for_file/_get_accepted_discogs_link) at module top — no cycle (routers.tags never imports services.review); PLC0415 forbids the inline-import alternative."

requirements-completed: [REVIEW-01, REVIEW-02]

# Metrics
duration: 25min
completed: 2026-07-01
---

# Phase 60 Plan 03: Propose Generation + Tag-write Review Workspaces Summary

**The D-01 Propose generation view (pending-proposal list + configured Model + Conf + GENERATE ALL over the existing `/pipeline/proposals` trigger — NOT a diff) plus the Tag-write review diff workspace that reuses the ONE shared `_diff_row.html` over the computed tag comparison, applying via `/tags/{id}/write` + `/tags/{id}/undo` and the id-less D-03 `/tags/bulk-write-no-discrepancies` bulk header, over a new EXECUTED-only degrade-safe read.**

## Performance

- **Duration:** ~25 min
- **Started:** 2026-07-01
- **Completed:** 2026-07-01
- **Tasks:** 3
- **Files created/modified:** 6

## Accomplishments
- **D-01 Propose (generation view, not a diff):** `propose_workspace.html` reuses `_file_table.html` (columns File · Proposed name · Proposed path · Model · Conf) over the SAME `get_pending_proposal_rows` read as Rename/Move. The Model cell renders the CONFIGURED `settings.llm_model` (A1 — one model per run, not a per-row field); the Conf cell reuses the Phase-59 tier palette (≥90 emerald / 70–89 amber / <70 gray). ONE **GENERATE ALL** secondary button posts the EXISTING `POST /pipeline/proposals` batch trigger with an `hx-confirm` + a `proposalsBusy` `:disabled` busy-gate (R-4). No per-row approve — approval lives on Rename/Move.
- **REVIEW-01/REVIEW-02 Tag-write:** `tagwrite_workspace.html` reuses the shared `_diff_row.html` over the tag facet. Per-row **APPROVE** POSTs `/tags/{id}/write` (the write IS the apply — no tag pending→approved status), per-row **UNDO** POSTs `/tags/{id}/undo` (before_tags restore, Plan 60-01), and the header **APPROVE ALL WITH NO DISCREPANCIES** POSTs the id-less server-predicate `/tags/bulk-write-no-discrepancies` (D-03) with `hx-confirm` + `hx-disabled-elt` (R-4). Tag rows carry NO SAVE-EDIT (tag inline-edit out of cut) and NO proposals-facet PATCH.
- **Shared partial extended, backward-compatible:** `_diff_row.html` gained `show_edit`/`show_skip` (default `true`), `show_undo` (default `false`), and a templated `undo_method` — the tag facet suppresses EDIT+SKIP and surfaces a POST UNDO, while rename/move (which pass none of these) render byte-identically (still `hx-patch`, Approve/Edit/Skip). The JS-context-safe `|tojson` inline-edit island is preserved (the Wave-2 apostrophe/XSS fix).
- **New degrade-safe read (Pitfall 3):** `get_tagwrite_review_rows` wraps a `session.begin_nested()` query of EXECUTED files without a COMPLETED `TagWriteLog`, computes `compute_proposed_tags` + `_build_comparison`, keeps only ≥1-change rows, and maps each to a plain dict (`file_id`/`filename`/`before_summary`/`after_summary`/`changed_count`/`has_blanking`); returns `[]` on any error. An empty queue while files await a move is CORRECT.
- **Wiring (T-57-01):** `STAGE_PARTIALS["propose"]`/`["tagwrite"]` are static string literals; `/s/propose` sets `propose_proposals` + `llm_model`, `/s/tagwrite` sets `tagwrite_files`; both return bare fragments; `oob_counts` stays False; dead-template + shell-route guards stay green.

## Task Commits

Each task committed atomically:

1. **Task 1: propose_workspace.html (D-01 generation view)** — `38b5d6f` (feat)
2. **Task 2: tagwrite_workspace.html + get_tagwrite_review_rows + _diff_row.html flags** — `24c2e0d` (feat)
3. **Task 3: shell.py /s/propose + /s/tagwrite wiring + test conversion** — `71c133f` (feat)

## Files Created/Modified
- `src/phaze/templates/pipeline/partials/propose_workspace.html` (created) — D-01 generation view over `_file_table.html`.
- `src/phaze/templates/pipeline/partials/tagwrite_workspace.html` (created) — tag-facet diff workspace + D-03 bulk header.
- `src/phaze/templates/pipeline/partials/_diff_row.html` (modified) — backward-compatible `show_edit`/`show_skip`/`show_undo`/`undo_method` flags.
- `src/phaze/services/review.py` (modified) — `get_tagwrite_review_rows` + `_summarize_tags` helper; module docstring updated for the two helpers.
- `src/phaze/routers/shell.py` (modified) — static-literal `STAGE_PARTIALS` propose/tagwrite + `_render_stage` branches + `settings`/`get_tagwrite_review_rows` imports.
- `tests/test_review_apply_workspaces.py` (modified) — added `test_tagwrite_workspace_apply_and_bulk_wiring` + `test_propose_workspace_generate_and_model`.

## Decisions Made
- **Tag SKIP omitted; UNDO in the pending cluster.** The interface's verified endpoint list has no tag-skip route (tags are computed, not stored rows), so SKIP is suppressed (`show_skip=false`) rather than wired to a non-existent endpoint. Because the tag queue is all-pending (EXECUTED files awaiting a write), the reversibility UNDO is surfaced in the pending cluster (`show_undo=true`) instead of the `row_state!='pending'` lifecycle branch it would otherwise never reach.
- **Propose confirm/sub-count use the pending-proposal length.** The plan reuses `get_pending_proposal_rows` and adds no separate pending-to-generate count, so both the sub-count (`{n} proposals ready`) and the GENERATE ALL confirm numeral use `propose_proposals | length` — the best available count in context.
- **`settings.llm_model` read off the module-level ControlSettings singleton** (mypy-clean, mirrors `pipeline.py`), not `get_settings()` casting.

## Deviations from Plan

None functionally — plan executed as written. The only interpretation choices (Tag SKIP suppression, UNDO in the pending cluster, Propose count source) are documented above under Decisions; each stays within the sanctioned surface (no new endpoints, no schema change). Ruff auto-fixed import sorting in `review.py` and `ruff-format` reflowed the test file on commit — cosmetic only.

## Known Stubs
None — both workspaces render live data through wired reads (`get_pending_proposal_rows` for Propose, `get_tagwrite_review_rows` for Tag-write). The dedupe/cue behavior tests remain `xfail` by design (owned by Plan 60-04).

## Threat Flags
None — `STAGE_PARTIALS` propose/tagwrite are static literals (T-57-01); the tag bulk button is id-less/threshold-less (T-60-01); every diff/table cell autoescapes with no `| safe` (T-60-XSS); both read helpers are SAVEPOINT-degrade-safe (T-60-DOS). No new network/auth/schema surface — the tag routes were added in Plan 60-01, not here.

## Self-Check: PASSED
- Created files exist: `propose_workspace.html`, `tagwrite_workspace.html` — both present.
- Commits exist: `38b5d6f`, `24c2e0d`, `71c133f` — all in git log.
- Verification green: `tests/test_review_apply_workspaces.py tests/test_dead_template_guard.py tests/test_shell_routes.py` → 17 passed, 2 xfailed (dedupe/cue stubs for Plan 60-04); `mypy src/phaze/routers/shell.py src/phaze/services/review.py` clean; `ruff` clean; `just tailwind` produced no CSS diff (all classes reused).

---
*Phase: 60-review-apply*
*Completed: 2026-07-01*
