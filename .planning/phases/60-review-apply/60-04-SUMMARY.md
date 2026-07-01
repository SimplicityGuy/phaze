---
phase: 60-review-apply
plan: 04
subsystem: ui
tags: [htmx, jinja2, alpine, review-apply, dedupe, cue, shell]

# Dependency graph
requires:
  - phase: 60-review-apply
    plan: 01
    provides: "Wave-0 test scaffold (test_review_apply_workspaces.py) + seed factories (seed_duplicate_group, seed_cue_set); integration audit test proving dedupe resolve/undo file_states round-trip"
  - phase: 60-review-apply
    plan: 03
    provides: "services/review.py degrade-safe read-helper pattern + shell.py _render_stage propose/rename/tagwrite/move branches to mirror"
  - phase: 57-shell-dag-rail
    provides: "shell.py _render_stage fork + STAGE_PARTIALS whitelist + single /pipeline/stats poll (R-2); dead-template AST guard"
  - phase: 58-enrich-analyze-workspaces
    provides: "_workspace_scaffold.html; degrade-safe service-helper pattern"
provides:
  - "pipeline/partials/dedupe_workspace.html + _dupe_group.html — the D-07 keeper-select workspace: a radio keeper posts POST /duplicates/{sha256_hash}/resolve with Form canonical_id (NOT group_id/keeper_id), page-scoped AUTO-KEEP posts /duplicates/resolve-all, resolve/undo file_states round-trip via the existing resolve_response.html OOB toast (REVIEW-03/REVIEW-05)"
  - "pipeline/partials/cue_workspace.html + _cue_preview.html — the D-08 cue preview workspace: eligible cards render the IN-MEMORY .cue preview + APPROVE->POST /cue/{id}/generate (generate IS approve; no /approve route), gated cards render opacity-60 'awaiting tracklist match…' with no approve control (REVIEW-04)"
  - "services/review.get_dedupe_groups — SAVEPOINT-wrapped scored duplicate groups (keeper = score_group's canonical_id), plain dicts, [] on error"
  - "services/review.get_cue_review_cards — SAVEPOINT-wrapped eligible+gated cue cards; eligible .cue text built via generate_cue_content ONLY (NO write_cue_file / no disk write), [] on error"
  - "shell.py /s/dedupe + /s/cue wiring (static-literal STAGE_PARTIALS, T-57-01) — the LAST two placeholders superseded; all six Review workspaces now live"
affects: [61-record-slidein, 62-polish-cutover]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Read-time reuse of a WRITE helper without the write: get_cue_review_cards calls cue.py's _build_cue_tracks + cue_generator.generate_cue_content to build the .cue preview text purely in memory, deliberately NOT calling write_cue_file — the render never mutates disk; the write happens only on explicit APPROVE->/cue/{id}/generate (T-60-CUE)"
    - "Ride an existing stateful-undo endpoint verbatim: the dedupe keeper radio posts the existing /duplicates/{hash}/resolve, whose resolve_response.html OOB toast already carries resolved_file_states + the UNDO form — the workspace needs no new undo template; a #toast-container landing host is the only addition"
    - "Third + fourth degrade-safe review reads mirror Wave 2/3: session.begin_nested() SAVEPOINT, ORM->plain-dict, [] on error, no router try/except; both new stages ride the fork with oob_counts=False"

key-files:
  created:
    - src/phaze/templates/pipeline/partials/dedupe_workspace.html
    - src/phaze/templates/pipeline/partials/_dupe_group.html
    - src/phaze/templates/pipeline/partials/cue_workspace.html
    - src/phaze/templates/pipeline/partials/_cue_preview.html
  modified:
    - src/phaze/services/review.py
    - src/phaze/routers/shell.py
    - tests/test_review_apply_workspaces.py

key-decisions:
  - "Wired against the VERIFIED endpoints, NOT the UI-SPEC sketch: dedupe resolve uses Form canonical_id + the sha256_hash group key (the sketch's group_id/keeper_id are wrong); cue APPROVE posts /cue/{id}/generate (there is no /cue/{id}/approve route — generate IS the write). The converted tests assert canonical_id present AND group_id/keeper_id absent, and /approve absent."
  - "Dedupe UNDO is carried by the existing resolve_response.html OOB toast (which already holds resolved_file_states + the /duplicates/{hash}/undo form), not a new resolved-card template — zero backend change. Added a #toast-container to the dedupe (and cue) workspace as the OOB toast landing host."
  - "get_cue_review_cards builds the .cue preview via generate_cue_content only (no write_cue_file) so render never writes disk (T-60-CUE); the gated set reuses cue._get_cue_stats's missing-timestamp query shape (approved + EXECUTED + NOT IN has_timestamp_subq)."
  - "set_name = the audio file stem (Path(current_path).stem) so the card title matches the .cue file that /cue/{id}/generate actually writes."
  - "_STAGE_PLACEHOLDER kept as a module constant in shell.py even though no stage now uses it — it keeps the string literal shell/partials/_stage_placeholder.html in router source so the dead-template guard keeps that partial reachable; both are removed together by CUT-02 (Phase 62)."

requirements-completed: [REVIEW-03, REVIEW-04, REVIEW-05]

# Metrics
duration: 35min
completed: 2026-07-01
---

# Phase 60 Plan 04: Dedupe Keeper-Select + Cue Preview Workspaces Summary

**The final two Review & Apply placeholders superseded over existing endpoints with zero backend change: the D-07 Dedupe keeper-select workspace (a radio keeper posting the VERIFIED `/duplicates/{sha256_hash}/resolve` `canonical_id` contract with a page-scoped AUTO-KEEP and the stateful `file_states` undo round-trip) and the D-08 Cue preview workspace (in-memory `.cue` previews built with `generate_cue_content` — no disk write — with APPROVE wired to `/cue/{id}/generate` as the write and visibly gated ineligible cards), completing the Review & Apply gate with all six workspaces live.**

## Performance

- **Duration:** ~35 min
- **Started:** 2026-07-01
- **Completed:** 2026-07-01
- **Tasks:** 3
- **Files created/modified:** 7

## Accomplishments
- **REVIEW-03/REVIEW-05 Dedupe (D-07):** `dedupe_workspace.html` + `_dupe_group.html` render each scored duplicate group as a keeper-select card. The keeper radio POSTs the **VERIFIED** `/duplicates/{sha256_hash}/resolve` with Form `canonical_id` via `hx-vals` (NOT the UI-SPEC sketch's `group_id`/`keeper_id`), swapping the card (`outerHTML`) to the existing `resolve_response.html` resolved state whose OOB toast carries `resolved_file_states` + the `/duplicates/{hash}/undo` form — so UNDO round-trips the JSON blob (REVIEW-05, undo reconstructs prior state FROM the blob). The header **AUTO-KEEP HIGHEST QUALITY** POSTs the page-scoped `/duplicates/resolve-all` with the R-4 guard (`hx-confirm` naming the count + `hx-disabled-elt`). KEEP/archive are text tags, never hue-only (WCAG 1.4.1).
- **REVIEW-04 Cue (D-08):** `cue_workspace.html` + `_cue_preview.html` render eligible + gated cards in a two-column grid. Eligible cards show the **in-memory** `.cue` preview `<pre>` (built at render via `_build_cue_tracks` + `generate_cue_content`, **no `write_cue_file`, no disk write** — T-60-CUE) and an emerald **APPROVE** that POSTs `/cue/{id}/generate` (generate IS the approve/write — there is no `/approve` route). Gated cards render `opacity-60` with "awaiting tracklist match…" and no approve control. No bulk header (REVIEW-04 requires none; the prototype's EXPORT APPROVED maps to no existing endpoint — omitted).
- **Two new degrade-safe reads:** `get_dedupe_groups` (SAVEPOINT-wrapped; `find_duplicate_groups_with_metadata` + `score_group` per group, keeper = `canonical_id`, plain dicts, `[]` on error) and `get_cue_review_cards` (SAVEPOINT-wrapped; eligible via `_get_eligible_tracklist_query` with in-memory `.cue` text, gated via the approved+EXECUTED+no-timestamp query, `[]` on error).
- **Wiring (T-57-01):** `STAGE_PARTIALS["dedupe"]`/`["cue"]` are now static string literals; `_render_stage` gained `dedupe`/`cue` branches (`oob_counts` stays False); the review import extended with the two helpers. This is the **last** of the six Review workspaces — every `_STAGE_PLACEHOLDER` value is superseded.
- **Tests converted:** `test_dedupe_keeper_resolve_wiring` (seeds a group, asserts the `canonical_id` resolve wiring, KEEP/archive text tags, one keeper, and the resolve response's `file_states` undo round-trip) and `test_cue_gate_and_preview` (seeds one eligible + one gated set, asserts the `<pre>` preview + generate-as-approve on the eligible card and the `opacity-60` no-approve gate on the ineligible) — both moved from `xfail` stubs to real assertions. No Phase-60 xfail remains.

## Task Commits

Each task committed atomically:

1. **Task 1: dedupe_workspace.html + _dupe_group.html + get_dedupe_groups** — `f0428d8` (feat)
2. **Task 2: cue_workspace.html + _cue_preview.html + get_cue_review_cards** — `1a43c2c` (feat)
3. **Task 3: shell.py /s/dedupe + /s/cue wiring + xfail conversion** — `a3b31eb` (feat)

## Files Created/Modified
- `src/phaze/templates/pipeline/partials/dedupe_workspace.html` (created) — D-07 keeper-select workspace + page-scoped AUTO-KEEP.
- `src/phaze/templates/pipeline/partials/_dupe_group.html` (created) — one keeper-group card (canonical_id resolve, KEEP/archive text tags).
- `src/phaze/templates/pipeline/partials/cue_workspace.html` (created) — D-08 two-column cue preview grid, no bulk header.
- `src/phaze/templates/pipeline/partials/_cue_preview.html` (created) — eligible in-memory `.cue` preview + generate-as-approve; gated opacity-60 card.
- `src/phaze/services/review.py` (modified) — `get_dedupe_groups` + `get_cue_review_cards` + `_format_size`/`_format_quality` helpers; docstring updated.
- `src/phaze/routers/shell.py` (modified) — static-literal `STAGE_PARTIALS` dedupe/cue + `_render_stage` branches + extended review import.
- `tests/test_review_apply_workspaces.py` (modified) — converted the two dedupe/cue xfail stubs to real behavior assertions.

## Decisions Made
- **Wired to the verified endpoints, not the sketch.** The UI-SPEC Pattern 4/5 markup shows `group_id`/`keeper_id` and `/cue/{id}/approve` — all wrong. Dedupe uses `canonical_id` + the `sha256_hash` group key; cue APPROVE posts `/cue/{id}/generate` (generate IS the write). The tests assert the wrong forms are absent.
- **Dedupe UNDO rides the existing `resolve_response.html`.** The resolve endpoint already returns the OOB toast carrying `resolved_file_states` + the undo form, so no new resolved-card template was needed — the workspace only adds a `#toast-container` landing host (mirrored on cue for its generate toast).
- **Cue preview is a read-time reuse of the write helpers minus the write.** `generate_cue_content` builds the `<pre>` text in memory; `write_cue_file` is deliberately never called at render (T-60-CUE). `set_name` uses the audio file stem so the card title matches the `.cue` that generate actually writes.
- **`_STAGE_PLACEHOLDER` retained.** No stage uses it after this plan, but keeping the constant keeps the `_stage_placeholder.html` literal in router source so the dead-template guard keeps it reachable; both go together in CUT-02 (Phase 62).

## Deviations from Plan

None functionally — the plan executed as written. The only interpretation choices (dedupe UNDO carried by the existing OOB toast rather than a new resolved-card template; `set_name` = file stem; retaining `_STAGE_PLACEHOLDER`) are documented above under Decisions; each stays within the sanctioned surface (no new endpoints, no schema change). `just tailwind` was re-run to compile the new `accent-emerald-500` / grid utility classes (build artifact, not committed).

## Known Stubs
None — both workspaces render live data through wired reads (`get_dedupe_groups`, `get_cue_review_cards`). No Phase-60 xfail remains; all six Review & Apply placeholders are superseded.

## Threat Flags
None — `STAGE_PARTIALS` dedupe/cue are static literals (T-57-01); every DB+filesystem-derived cell (dupe file names/quality, the cue `<pre>`) autoescapes with no `| safe` (T-60-XSS); the cue preview never writes disk (T-60-CUE); both read helpers are SAVEPOINT-degrade-safe with `oob_counts=False` (T-60-DOS); the dedupe `file_states` undo round-trips the existing resolve response (T-60-03). No new network/auth/schema surface — dedupe/cue routes predate this plan.

## Issues Encountered
- The full suite reports **2577 passed, 96.80% coverage** with `TEST_DATABASE_URL`/`MIGRATIONS_TEST_DATABASE_URL` set. The 6 failures + 9 errors are all SAQ-Postgres queue-infra tests (`test_agent_task_router`, `test_ledger_backfill`, `test_recovery`, and the `test_pg_*`/`test_stage_*` integration modules) and are **environmental, not a regression**: they feed `PHAZE_QUEUE_URL` to psycopg as a Postgres DSN, but the plan's review-workspace test-env note sets it to `redis://localhost:6380/0`. Re-running those modules with `PHAZE_QUEUE_URL=postgresql://…:5433/phaze_test` turns `test_agent_task_router`/`ledger_backfill`/`recovery` green; the remaining `test_pg_*`/`test_stage_*` need the SAQ-queue schema that only `just integration-test`'s self-contained harness provisions (Wave-1 SUMMARY confirmed the entire suite is green under that harness). Zero overlap with the changed files (review.py dedupe/cue helpers, shell.py, templates); the review-workspace + dead-template + shell-route guards all pass.

## Self-Check: PASSED
- Created files exist: `dedupe_workspace.html`, `_dupe_group.html`, `cue_workspace.html`, `_cue_preview.html` — all present.
- Commits exist: `f0428d8`, `1a43c2c`, `a3b31eb` — all in git log.
- Verification green: `tests/test_review_apply_workspaces.py tests/test_dead_template_guard.py tests/test_shell_routes.py` → 19 passed, 0 xfailed (both dedupe/cue stubs converted); `mypy src/phaze/routers/shell.py src/phaze/services/review.py` clean; `ruff` clean; `just tailwind` compiled the new utility classes; full suite 96.80% coverage (the 6+9 queue-infra failures are environmental, documented under Issues Encountered).

---
*Phase: 60-review-apply*
*Completed: 2026-07-01*
