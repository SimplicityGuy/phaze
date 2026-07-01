---
phase: 60-review-apply
plan: 01
subsystem: api
tags: [fastapi, sqlalchemy, htmx, jinja2, review-apply, tag-write, proposals, audit]

# Dependency graph
requires:
  - phase: 57-shell-dag-rail
    provides: "shell.py _render_stage fork + STAGE_PARTIALS whitelist + single /pipeline/stats poll (R-2/R-5 contract)"
  - phase: 59-identify-workspaces
    provides: "Wave-0 scaffold precedent (test_identify_workspaces.py) + degrade-safe service-helper pattern"
provides:
  - "PATCH /proposals/bulk-approve-high-confidence — server-predicate bulk approve (confidence>=0.9, no client id-list) [D-02/REVIEW-02]"
  - "PATCH /proposals/{id}/edit — validated inline edit of proposed_filename/proposed_path, stays PENDING, no LLM re-run [D-05/REVIEW-01]"
  - "POST /tags/bulk-write-no-discrepancies — server-predicate no-discrepancy bulk tag write [D-03/OQ-1/REVIEW-02]"
  - "POST /tags/{id}/undo — tag reversibility restoring TagWriteLog.before_tags via existing execute_tag_write [REVIEW-05]"
  - "proposal_queries.approve_pending_above_confidence + update_proposal_fields helpers"
  - "tags._qualifies_for_bulk_write pure predicate (LOCKED D-03/OQ-1)"
  - "tests/test_review_apply_workspaces.py Wave-0 scaffold (R-2/R-5 foundation green + xfail behavior stubs) + conftest Review seed factories"
  - "tests/integration/test_review_audit.py — one-audit-row-per-apply + reversibility"
affects: [60-02-rename-move-workspaces, 60-03-tagwrite-dedupe, 60-04-cue, 61-record-slidein]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Server-predicate mutation: re-query the fixed predicate at submit (mirror tracklists.reject_low_confidence); NEVER accept a client id-list"
    - "Thin route over unchanged apply logic: new endpoints reuse bulk_update_status / execute_tag_write verbatim, inventing no apply/audit/undo logic"
    - "Pure, directly-unit-testable predicate function (_qualifies_for_bulk_write) when an end-to-end path can't construct a branch"

key-files:
  created:
    - tests/test_review_apply_workspaces.py
    - tests/integration/test_review_audit.py
    - src/phaze/templates/tags/partials/bulk_write_response.html
  modified:
    - src/phaze/services/proposal_queries.py
    - src/phaze/routers/proposals.py
    - src/phaze/routers/tags.py
    - tests/conftest.py

key-decisions:
  - "D-02 bulk-approve is id-less + threshold-less: server re-queries PENDING confidence>=0.9 at submit; NULL confidence excluded by SQL (Pitfall 2, no COALESCE)"
  - "D-05 edit validates proposed value (reject empty/'..'/control/NUL; filename facet rejects '/'; path facet strip('/')+collapse '//' mirroring store_proposals) before persisting; row stays PENDING"
  - "D-03/OQ-1 predicate LOCKED: >=1 changed field AND no field would blank an existing tag; blank-guard is defensive (compute_proposed_tags never blanks) and asserted directly via _qualifies_for_bulk_write"
  - "Scope refined 2->4 thin routes: tag-bulk + tag-undo have no existing endpoint and REVIEW-02/05 demand them; both live in tags.py (tags are computed, not RenameProposal rows)"

patterns-established:
  - "Wave-0 Review scaffold: two R-2/R-5 foundation tests green against placeholders + xfail behavior stubs each later plan converts"
  - "Review seed factories live in conftest (make_file, seed_pending_proposal incl. NULL confidence, seed_executed_file_with_metadata, seed_duplicate_group, seed_cue_set)"

requirements-completed: [REVIEW-01, REVIEW-02, REVIEW-05]

# Metrics
duration: 45min
completed: 2026-07-01
---

# Phase 60 Plan 01: Review & Apply backend additions Summary

**Four thin routes over unchanged apply/generation logic — D-02 server-predicate bulk-approve, D-05 validated inline-edit, D-03/OQ-1 no-discrepancy tag-bulk, and REVIEW-05 tag-undo — plus the Wave-0 test scaffold + seed factories that gate every later Review workspace plan.**

## Performance

- **Duration:** ~45 min
- **Started:** 2026-07-01
- **Completed:** 2026-07-01
- **Tasks:** 3
- **Files created/modified:** 7

## Accomplishments
- **REVIEW-02 stale-bulk fix (D-02):** `PATCH /proposals/bulk-approve-high-confidence` re-queries pending `confidence >= 0.9` rows server-side at submit and drives the result; a client-sent `proposal_ids` form field has **no effect** (asserted). NULL-confidence rows are excluded by the SQL predicate (Pitfall 2), never approved.
- **REVIEW-01 inline edit (D-05):** `PATCH /proposals/{id}/edit` persists `proposed_filename`/`proposed_path`, validates the edited value against traversal/control/`/`, leaves the row PENDING, does not re-run the LLM, and returns only the row (R-6).
- **REVIEW-02 tag-bulk (D-03/OQ-1):** `POST /tags/bulk-write-no-discrepancies` server-re-queries EXECUTED files with no COMPLETED `TagWriteLog`, applies the LOCKED predicate (`>=1` change AND never blank a tag), and writes via the existing `execute_tag_write`. Predicate documented verbatim in the route docstring.
- **REVIEW-05 reversibility:** `POST /tags/{id}/undo` restores `TagWriteLog.before_tags` through the existing `execute_tag_write` mutagen path (no new logic); integration test proves exactly one audit row per apply and round-trips tag-undo + dedupe resolve/undo.
- **Wave-0 gate:** foundation R-2/R-5 tests green against the six placeholders; seven xfail behavior stubs collected; five reusable seed factories in conftest (including the 0.95/0.50/NULL confidence set).

## Task Commits

Each task committed atomically:

1. **Task 1: Wave 0 scaffold + seed factories** - `c621528` (test)
2. **Task 2: D-02 bulk-approve + D-05 inline-edit** - `d2f117d` (feat)
3. **Task 3: D-03 tag-bulk + REVIEW-05 tag-undo + audit integration** - `89b509c` (feat)

_Note: Tasks 2 and 3 were TDD (RED via converted xfail stubs -> GREEN implementation) but landed as single feat commits since the RED stubs were established in Task 1's scaffold commit._

## Files Created/Modified
- `tests/test_review_apply_workspaces.py` (created) - Wave-0 scaffold: 2 foundation tests + 7 behavior tests (4 converted here, 3 xfail for later plans).
- `tests/integration/test_review_audit.py` (created) - REVIEW-05: one TagWriteLog per write, tag-undo re-applies before_tags, dedupe resolve one resolution + undo round-trips file_states.
- `src/phaze/templates/tags/partials/bulk_write_response.html` (created) - count-bearing OOB toast for the tag-bulk response (reuses existing inline-toast markup).
- `src/phaze/services/proposal_queries.py` (modified) - `approve_pending_above_confidence` + `update_proposal_fields` helpers.
- `src/phaze/routers/proposals.py` (modified) - D-02 + D-05 PATCH routes + `_validate_proposed_value`.
- `src/phaze/routers/tags.py` (modified) - `_qualifies_for_bulk_write` + `bulk_write_no_discrepancies` + `undo_tag_write`.
- `tests/conftest.py` (modified) - five Review seed factories.

## Decisions Made
- **Blank-guard is defensive, tested directly.** `compute_proposed_tags` copies every non-None metadata field into the proposal, so a server-computed comparison can never satisfy `current is not None and proposed is None` — the "blank an existing tag" branch is structurally unreachable through the real endpoint. Rather than fake it, the predicate was factored into the pure `_qualifies_for_bulk_write(comparison)` and its blank-guard clause is asserted directly on a hand-built comparison, while the endpoint tests cover clean-change-written + zero-change-untouched.
- **Scope refined 2 -> 4 thin routes** (stated for the PR): CONTEXT sanctioned two thin endpoints (D-02, D-05); this plan adds two more — tag-bulk (D-03) and tag-undo (REVIEW-05) — because neither has an existing endpoint and REVIEW-02/REVIEW-05 require them. Both live in `tags.py` (tags are computed, not `RenameProposal` rows) and reuse `execute_tag_write` verbatim.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] mypy invariance on the bulk-write tags dict**
- **Found during:** Task 3 (tag-bulk endpoint)
- **Issue:** `tags = {k: v for k, v in proposed.items() if v is not None}` narrowed to `dict[str, str | int]`, incompatible with `execute_tag_write`'s invariant `dict[str, str | int | None]` param.
- **Fix:** Annotated the local as `tags: dict[str, str | int | None] = {...}` (mirrors the existing `write_file_tags` pattern).
- **Files modified:** src/phaze/routers/tags.py
- **Verification:** `uv run mypy src/phaze/routers/tags.py` clean.
- **Committed in:** `89b509c` (Task 3 commit)

**2. [Rule 3 - Blocking] No existing tags bulk-response partial**
- **Found during:** Task 3 (tag-bulk endpoint)
- **Issue:** The plan said to reuse an existing tags response partial for the count toast, but `tags/partials/` has no bulk/toast-only partial (tag_row.html requires a single `file`).
- **Fix:** Created a minimal `tags/partials/bulk_write_response.html` that reuses the exact existing inline-toast markup (from tag_row.html) — no new toast style invented. Tag-undo still reuses `tag_row.html` verbatim.
- **Files modified:** src/phaze/templates/tags/partials/bulk_write_response.html
- **Verification:** endpoint returns 200; `test_tag_bulk_no_discrepancy_predicate` green.
- **Committed in:** `89b509c` (Task 3 commit)

---

**Total deviations:** 2 auto-fixed (both Rule 3 - blocking).
**Impact on plan:** Both minor and necessary to complete the task; no scope creep, no change to the sanctioned four-route surface.

## Issues Encountered
- The integration harness (`tests/integration/conftest.py`) targets a raw `PostgresQueue`, not an HTTP client. `test_review_audit.py` instead uses the DB-backed `client`/`session` fixtures from the top-level `conftest.py` (auto-marked `integration` by the path rule) — a real Postgres session that fits the audit assertions. Mutagen writes are patched (no audio files on disk) so the DB audit trail is exercised end-to-end.
- Full-suite migration tests require `MIGRATIONS_TEST_DATABASE_URL` (separate `phaze_migrations_test` DB). With it set, the entire suite is green: **2586 passed, 3 xfailed, 97.05% coverage** (the 3 xfailed are the later-plan behavior stubs).

## Threat Flags
None — the four routes are server-predicate/validated writes over unchanged apply logic; all threat-register mitigations (T-60-01 server re-query, T-60-02 edit validation, T-60-03 one-row audit) are implemented and tested. No new network/auth/schema surface introduced.

## Next Phase Readiness
- Backend correctness core is landed and tested; Plans 60-02/03/04 build the six workspace templates and convert the remaining three xfail stubs (`test_diff_row_before_after`, `test_dedupe_keeper_resolve_wiring`, `test_cue_gate_and_preview`).
- Plan 60-02 re-points `edit_proposal` at the shared `pipeline/partials/_diff_row.html` once it exists (the endpoint currently returns `proposals/partials/proposal_row.html`).
- The seed factories + Wave-0 foundation guards are ready for reuse by all later Review plans.

## Self-Check: PASSED
- Created files exist: tests/test_review_apply_workspaces.py, tests/integration/test_review_audit.py, src/phaze/templates/tags/partials/bulk_write_response.html — all present.
- Commits exist: c621528, d2f117d, 89b509c — all in git log.

---
*Phase: 60-review-apply*
*Completed: 2026-07-01*
