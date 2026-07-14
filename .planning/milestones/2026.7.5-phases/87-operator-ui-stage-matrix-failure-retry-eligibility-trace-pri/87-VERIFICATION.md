---
phase: 87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri
verified: 2026-07-11T09:15:00Z
status: passed
score: 5/5 must-haves verified
overrides_applied: 0
re_verification:
  previous_status: gaps_found
  previous_score: 3/5
  gaps_closed:
    - "The file-row 'State' display is a derived per-file stage matrix (paginated, NEVER a whole-corpus scan per poll), replacing the raw-enum State column, AND is reachable from the shell."
    - "The operator can see failed files per enrich stage and trigger a retry from the console (fingerprint/metadata + manual analyze), reachable from the shell."
  gaps_remaining: []
  regressions: []
human_verification: []
---

# Phase 87: Operator UI — Stage Matrix, Failure Retry, Eligibility Trace, Priority Verification Report

**Phase Goal:** Replace the raw-enum "State" column with a derived per-file stage matrix and give the
operator failure visibility + retry, a "why not eligible?" trace, a force-done/skip affordance, an
orphaned-work count, and the restored per-stage priority control.
**Verified:** 2026-07-11T09:15:00Z
**Status:** passed
**Re-verification:** Yes — after gap closure (Plan 87-09, commit 33b34890)

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Derived per-file stage matrix replaces raw-enum State column, paginated, never a whole-corpus scan, AND reachable from the shell | ✓ VERIFIED | Backend/template unchanged and still correct (`get_files_page` LIMIT+1 no-COUNT, SAVEPOINT degrade). **Gap closed**: `routers/shell.py` now maps `STAGE_PARTIALS["files"]` to `files_table_view.html` and `_render_stage`'s `elif stage == "files":` branch builds the identical `get_files_page` context; `rail.html` carries a native, keyboard-accessible "Files" `<button data-rail-stage="files" hx-get="/s/files">` right after Summary. Live test run confirms: `GET /s/files` direct nav → 200, full shell chrome (`id="stage-workspace"`, `data-stage="files"`), renders `id="files-table-view"` + a rendered `_stage_pill` token; `GET /s/files` with `HX-Request: true` → bare fragment (`id="files-table-view"`, no `<html>`/`<head>`). `test_rail_nodes_wired` now enumerates 14 nodes including "files". |
| 2 | Operator sees failed files per enrich stage and can trigger retry (fingerprint auto / metadata / analyze), reachable from the console | ✓ VERIFIED | Metadata bulk retry (`POST /pipeline/metadata-failed/retry`) and both per-file scoped retry variants (`/pipeline/files/{id}/analysis-failed/retry`, `/pipeline/files/{id}/metadata-failed/retry`) live in `files_table_view.html`'s per-row/bulk buttons (unchanged, still correctly wired and tested). **Gap closed**: that page is now reachable at `/s/files` via the new rail node, so an operator can reach and use the metadata retry and per-file retry affordances from the shipped console. Fingerprint correctly has no manual control (self-retries via the pending set, matches RESEARCH); analyze bulk retry remains reachable via the pre-existing `straggler_failed_card.html` surface. |
| 3 | For a file not in a stage's pending set, operator sees WHY (eligibility trace over `eligible()` conjuncts) | ✓ VERIFIED | Unchanged from initial verification. `GET /pipeline/files/{id}/trace/{stage}` + `_eligibility_trace.html` render 4 named conjuncts + blocker, reading the REAL `eligible()`. Reachable via the record right pane (⌘K search, Analyze-workspace row clicks). Regression run: `tests/shared/test_eligibility_trace.py` — all pass. |
| 4 | Operator can force-skip a stage per file; orphaned/stuck-work count surfaced | ✓ VERIFIED | Unchanged. `POST /pipeline/files/{id}/skip/{stage}` (enrich-only, sanitized AFTER validation, additive, committed, `on_conflict_do_nothing(index_elements=["file_id","stage"])` for idempotency) + `_force_skip_dialog.html`, reachable via the record right pane. Orphan badge (`get_stage_orphan_counts`) on the DAG rail, reachable on every page. Regression run: `tests/analyze/test_force_skip_writer.py` — all pass. |
| 5 | Per-stage priority stepper + pause/resume re-wired to `POST /pipeline/stages/{stage}/priority` (+pause/resume) | ✓ VERIFIED | Unchanged. Rail posts `±10` deltas + pause/resume to the live `routers/pipeline_stages.py` endpoints, reachable on every page. Regression run: `tests/shared/test_rail_priority_controls.py` — all pass. |

**Score:** 5/5 truths verified

### Additional Behaviors Re-Confirmed (Live)

| Behavior | Command | Result | Status |
|---|---|---|---|
| Behavior-5: force-SKIPPED fingerprint excluded from recovery | `src/phaze/tasks/reenqueue.py:263-277` (`fingerprint_done = {... or_(done_clause(Stage.FINGERPRINT), skipped_clause(Stage.FINGERPRINT)) ...}`) + live `pytest tests/analyze/tasks/test_recovery.py -q` | 54 passed | ✓ VERIFIED |
| Force-skip writer idempotent on duplicate (file,stage) | `src/phaze/routers/pipeline.py:1334` — `pg_insert(StageSkip).values(...).on_conflict_do_nothing(index_elements=["file_id", "stage"])` | present in source; no 500 on duplicate | ✓ VERIFIED |
| Force-skip reason validated AFTER sanitize | `src/phaze/routers/pipeline.py:1322-1323` — `clean_reason = sanitize_pg_text(reason).strip()` then `if not clean_reason: ...` (D-09 check runs on the sanitized value, never the raw NUL-bearing input) | present in source | ✓ VERIFIED |

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/routers/shell.py` | `STAGE_PARTIALS["files"]` + `_render_stage` "files" branch | ✓ VERIFIED | Static literal at line 86; `elif stage == "files":` branch (lines 204-218) mirrors `pipeline_files()` context exactly (`get_files_page(session, page=1, page_size=25, stage=None, bucket=None)`), re-asserts `stage`/`stage_partial`/`oob_counts` defensively |
| `src/phaze/templates/shell/partials/rail.html` | "Files" nav node wired to `/s/files` | ✓ VERIFIED | Native `<button data-rail-stage="files">`, `hx-get="/s/files" hx-target="#stage-workspace" hx-swap="innerHTML" hx-push-url="true"`, aria-hidden glyph, `max-lg:sr-only` label (never `max-lg:hidden`), `title="Files"`, `aria-current` binding — placed right after Summary |
| `tests/shared/core/test_shell_routes.py` | 14-node rail enumeration + fork test + reachability/a11y test | ✓ VERIFIED | `_RAIL_STAGES` now 14 entries including "files"; `test_files_stage_route_and_fragment` (fork) + `test_files_rail_node_is_reachable_and_accessible` (a11y) both pass live |
| `src/phaze/templates/pipeline/partials/files_table_view.html` | Files table w/ filter + retry | ✓ VERIFIED (no longer ORPHANED) | Same content as before, now reachable via `/s/files` |
| `src/phaze/templates/pipeline/partials/_status_filter_bar.html` | URL-carried status filter | ✓ VERIFIED (no longer ORPHANED) | Reachable inside the now-reachable `/s/files` page |
| `src/phaze/services/pipeline.py:get_files_page` | Paginated, no-COUNT, degrade-safe | ✓ VERIFIED | Unchanged; confirmed via live `tests/integration/test_files_page.py` |
| `src/phaze/tasks/reenqueue.py` | fingerprint_done includes skipped_clause | ✓ VERIFIED | Unchanged; confirmed via live `test_skipped_fingerprint_row_is_excluded_from_recovery` |
| All other Phase-87 artifacts (stage_skip model, migration 037, stage.py enums, stage_status.py, `_stage_pill.html`, `_stage_matrix.html`, `_eligibility_trace.html`, `_force_skip_dialog.html`, `record_body.html`) | Per prior verification | ✓ VERIFIED (regression-checked) | All corresponding test files re-run live in this pass; no regressions |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|----|--------|---------|
| **`shell` rail navigation** | **`GET /s/files`** | **rail node `hx-get="/s/files"`** | **✓ WIRED** | **Gap closed** — confirmed live: rail button present, `/s/files` resolves via `STAGE_PARTIALS`, fork inherited from `_render_stage` |
| `stage_status.py:eligible_clause` | `skipped_clause` | `not_(skipped_clause(stage))` conjunct | ✓ WIRED | Unchanged, confirmed in source |
| `tasks/reenqueue.py:_build_done_sets` | `skipped_clause(FINGERPRINT)` | `or_(done_clause, skipped_clause)` | ✓ WIRED | Unchanged, confirmed in source + live test pass |
| `pipeline.py:get_files_page` | `stage_status_case` | correlated per-page CASE columns | ✓ WIRED | Unchanged, confirmed in source |
| `_force_skip_dialog.html` | `POST /pipeline/files/{id}/skip/{stage}` | Alpine x-trap confirm dialog | ✓ WIRED | Unchanged, confirmed |
| `rail.html` priority/pause controls | `POST /pipeline/stages/{stage}/{priority,pause,resume}` | hx-post + hx-vals | ✓ WIRED | Unchanged, confirmed live endpoints exist |
| `⌘K palette` / Analyze-workspace rows | `record_body.html` (eligibility trace + force-skip) | `/record/{file_id}` | ✓ WIRED | Unchanged, confirmed |

### Behavioral Spot-Checks / Live Test Runs (this re-verification pass)

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Reachability gap-closure suite (rail + fork + a11y + dead-template guard + priority + files-page + filter + routing) | `pytest tests/shared/core/test_shell_routes.py tests/shared/core/test_rail_narrow_width.py tests/shared/core/test_a11y_guards.py tests/shared/core/test_dead_template_guard.py tests/shared/test_rail_priority_controls.py tests/integration/test_files_page.py tests/integration/test_files_filter.py tests/shared/routers/test_routing.py -q` | `67 passed` | ✓ PASS |
| Recovery suite regression (behavior-5) | `pytest tests/analyze/tasks/test_recovery.py -q` | `54 passed` | ✓ PASS |
| DERIV-04 equivalence + migration + files-page + filter + orphan + shadow-compare regression | `pytest tests/integration/test_stage_status_equivalence.py tests/integration/test_migrations/test_037_stage_skip.py tests/integration/test_files_page.py tests/integration/test_files_filter.py tests/integration/test_orphan_count.py tests/integration/test_shadow_compare_skipped.py -q` | `80 passed` | ✓ PASS |
| Force-skip writer, eligibility trace, retry affordances, rail controls, pill render, no-raw-state guard, stage resolver regression | `pytest tests/analyze/test_force_skip_writer.py tests/shared/test_eligibility_trace.py tests/analyze/test_retry_affordances.py tests/metadata/test_retry_affordances.py tests/shared/test_rail_priority_controls.py tests/shared/test_stage_pill_render.py tests/shared/test_no_raw_state_render.py tests/shared/test_stage_resolver.py -q` | `108 passed` | ✓ PASS |
| Model registry + dead-template guard + pipeline router regression | `pytest tests/shared/models/test_core_models.py tests/shared/core/test_dead_template_guard.py tests/shared/routers/test_pipeline.py -q` | `122 passed` | ✓ PASS |
| Rail node enumeration (confirms "files" nav node NOW exists) | `grep -n "STAGE_PARTIALS" routers/shell.py` + `test_rail_nodes_wired` | 14 nodes enumerated, including "files" | ✓ GAP CLOSED |
| Lint / type-check on gap-closure files | `ruff check src/phaze/routers/shell.py tests/shared/core/test_shell_routes.py` / `mypy src/phaze/routers/shell.py` | clean / `Success: no issues found in 1 source file` | ✓ PASS |
| Commit verification | `git show --stat 33b34890` | Present: modifies `shell.py` (+24), `rail.html` (+19), `test_shell_routes.py`, adds `87-09-SUMMARY.md` | ✓ PASS |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|---|---|---|---|---|
| UI-01 | 87-04, 87-05, 87-09 | Derived per-stage matrix replaces raw-enum State, paginated, no whole-corpus scan, reachable | ✓ SATISFIED | Implemented, tested, AND now reachable via `/s/files` rail node |
| UI-02 | 87-05, 87-07, 87-09 | Failure visibility + retry (fingerprint/metadata/analyze), reachable | ✓ SATISFIED | Metadata + per-file retry affordances now reachable via `/s/files`; analyze bulk retry reachable via pre-existing straggler card; fingerprint correctly self-retries with no manual control |
| UI-03 | 87-06 | Eligibility trace over `eligible()` conjuncts | ✓ SATISFIED | Reachable via record right pane, regression-confirmed |
| UI-04 | 87-01, 87-02, 87-03, 87-06 | Force-skip / stage_skip marker + derivation threading | ✓ SATISFIED | Marker, derivation, writer, dialog all correct and reachable, regression-confirmed |
| UI-05 | 87-08 | Orphaned/stuck-work count on the rail | ✓ SATISFIED | Rail badge, definitional recovery parity, reachable, regression-confirmed |
| PRIO-01 | 87-08 | Priority stepper + pause/resume re-wire | ✓ SATISFIED | Rail controls, live endpoints, reachable, regression-confirmed |

No orphaned requirements — every ID declared in ROADMAP/REQUIREMENTS.md for Phase 87 (UI-01..05, PRIO-01)
is claimed by at least one plan and is now SATISFIED.

**Note (non-blocking, documentation-only):** `.planning/REQUIREMENTS.md` still shows `[ ]` unchecked
checkboxes and a "Pending" status for UI-01..05 and PRIO-01 in its coverage table. This is a planning-doc
bookkeeping lag (typically updated at milestone close), not a code gap — every one of these requirement
IDs is now functionally SATISFIED per the evidence above. Flagging for the milestone-close step to update
REQUIREMENTS.md's checkboxes/status column; this does not block Phase 87.

### Anti-Patterns Found

None. No unresolved `TBD`/`FIXME`/`XXX`/`TODO`/`HACK` markers in any Phase-87-touched source file
(`shell.py`, `rail.html`, `test_shell_routes.py`). One incidental string match of the word "PLACEHOLDER"
in a `rail.html` comment refers to the Summary node's intentional static/no-DB-read design (shipped
Phase 57 architecture, not incomplete work) — not a stub marker. No stub renders, no empty handlers found.

### Human Verification Required

None. The prior gap (unreachable navigation) was code-verifiable and has been closed with a passing,
mutation-tested regression suite; no visual/UX judgment call remains outstanding for this phase.

### Gaps Summary

Both previously-FAILED/PARTIAL truths are now closed by Plan 87-09 (commit `33b34890`, merged):

1. **UI-01 reachability** — `routers/shell.py` gained a `STAGE_PARTIALS["files"]` static literal and a
   `_render_stage` branch that builds the identical `get_files_page` context the standalone route used;
   `rail.html` gained a keyboard-accessible "Files" nav node wired to `/s/files`. The
   fragment-vs-full-page fork is inherited from the existing `_render_stage` machinery (no bespoke fork
   needed on `pipeline_files()` itself) — direct/bookmark navigation to `/s/files` now renders full shell
   chrome; an HX rail swap renders the bare content fragment. Live-tested: `test_files_stage_route_and_fragment`
   and `test_files_rail_node_is_reachable_and_accessible` both pass, and a mutation check (label
   `max-lg:sr-only` → `max-lg:hidden`) proved the a11y guard has teeth (went RED, restored to green).

2. **UI-02 reachability** — Since the metadata bulk retry and both per-file retry variants live inside
   `files_table_view.html` (which is now rendered at `/s/files`), they inherit the same reachability fix
   with zero additional wiring changes needed.

All 5 phase truths are now VERIFIED against the current codebase with live, passing test evidence
(regression suites for the previously-passing truths 3/4/5 re-run clean with no drift). The phase goal —
"Replace the raw-enum 'State' column with a derived per-file stage matrix and give the operator failure
visibility + retry, a 'why not eligible?' trace, a force-done/skip affordance, an orphaned-work count,
and the restored per-stage priority control" — is observably true and reachable in the running
application.

---

*Verified: 2026-07-11T09:15:00Z*
*Verifier: Claude (gsd-verifier)*
