---
phase: 85-executed-gate-revival
verified: 2026-07-10T21:52:26Z
status: human_needed
score: 5/5 must-haves verified
overrides_applied: 0
human_verification:
  - test: "Confirm the WR-01 review finding (SQL .limit() applied BEFORE the Python qualification filter in get_tagwrite_review_rows / bulk_write_no_discrepancies) is an acceptable known-debt item for this phase, or requires a follow-up plan before shipping at the 200K-scale the phase explicitly targets."
    expected: "A decision: accept as documented follow-up work, or open a closure plan to fix the ordering so qualifying rows are never starved behind a wall of non-qualifying applied files."
    why_human: "This is a design/priority tradeoff already surfaced in 85-REVIEW.md (WR-01..WR-04, status: issues_found, 0 critical/4 warnings). It does not block the phase's stated goal (predicate revival + mutation-verified behavior change), but it does undermine the D-03 bound's practical guarantee at scale. Needs a product/priority call, not a code check."
---

# Phase 85: EXECUTED-Gate Revival Verification Report

**Phase Goal:** Revive the permanently-dead `state == EXECUTED` gates against the real apply-outcome source so tag writing, review, and tags/cue/tracklists guards fire for *actually-applied* files — turning on tag/CUE writing for the first time. The one behavior-reviving, filesystem-mutating change in the milestone.
**Verified:** 2026-07-10T21:52:26Z
**Status:** human_needed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | An `applied(f)` predicate replaces every dead `state == EXECUTED` gate in `tag_writer.py`, `review.py`, `tags.py`, `cue.py`, `tracklists.py` | VERIFIED | `grep -rn "FileState.EXECUTED\|state == FileState.EXECUTED"` across `src/phaze/` returns ZERO hits in the 5 target files. The only remaining `FileState.EXECUTED` references are the documented out-of-scope sites: `services/pipeline.py:57` (terminal-state list), `services/proposal.py:39` (`_TERMINAL_FILE_STATES` frozenset), `services/shadow_compare.py:139` (invariant registry entry), and a docstring mention in `services/stage_status.py:124`. `applied_clause()`/`is_applied()` are defined in `services/stage_status.py:117-166`, expressed purely as `exists(proposals WHERE file_id==FileRecord.id AND status=='executed')`, and are imported/consumed at every swapped site (verified by grep + direct file read of all 6 files). |
| 2 | A test asserts the behavior change explicitly — an actually-applied file now passes the tag/CUE guards that previously always failed (mutation-checked RED→GREEN) | VERIFIED | `tests/review/services/test_tag_writer.py::TestExecuteTagWrite::test_applied_file_passes_guard` seeds a real `FileRecord(state='moved')` + `RenameProposal(status='executed')` and asserts `execute_tag_write` proceeds to `COMPLETED`. **I independently re-ran the mutation check** (not trusting the SUMMARY's claim): reverted `tag_writer.py:185` to the dead `file_record.state != FileState.EXECUTED` guard (re-adding a `FileState` import) — the test went **RED** (`ValueError: Only executed files can have tags written`, `NameError` transiently while wiring the mutation). Restored the file via `git checkout` (confirmed `git diff` clean) — the test went **GREEN** (19/19 passed in `test_tag_writer.py`). Parallel SC#2 tests exist for CUE (`test_cue.py::test_generate_cue_admits_applied_file_not_executed_state`) and were run and passed (23/23). |

**Score:** 2/2 truths verified (both roadmap success criteria satisfied)

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/services/stage_status.py` | `applied_clause()` + `is_applied()` predicate pair | VERIFIED | Both functions present (lines 117, 148), correctly correlated, kept out of the `done_clause`/`failed_clause`/`stage_status_case` Stage ladders (confirmed by reading the full function bodies). |
| `src/phaze/services/tag_writer.py` | `is_applied`-gated `execute_tag_write` | VERIFIED | Line 185: `if not await is_applied(session, file_record.id):`. Zero `FileState.EXECUTED` references. mypy/ruff clean. |
| `src/phaze/routers/tags.py` | `applied_clause()` readers (stat card, list, count, bulk) + `is_applied` per-record guard | VERIFIED | 4× `applied_clause()` (lines 50, 177, 180, 425) + 1× `await is_applied(session, file_id)` (line 337). `_MAX_BULK_TAG_WRITE = 2000` + `.limit(...)` present (D-03). `completed_subq` (`TagWriteLog.status == COMPLETED`) preserved (D-02). |
| `src/phaze/routers/cue.py` | `applied_clause()` readers + `is_applied` guard | VERIFIED | 2× `applied_clause()` (lines 49, 90) + `not await is_applied(session, file_record.id)` (line 252). No `session.commit()` added (disk-only write, confirmed correct). |
| `src/phaze/routers/tracklists.py` | 3× `is_applied` cue-version guards | VERIFIED | Lines 139, 601, 898 all read `await is_applied(session, fr.id)`. Zero `state == FileState.EXECUTED` remnants. |
| `src/phaze/services/review.py` | `applied_clause()` readers + bounded builders | VERIFIED | `applied_clause()` in both `get_tagwrite_review_rows` (line 120) and `get_cue_review_cards` gated_stmt (line 267). `_MAX_REVIEW_ROWS = 2000` + `.limit(...)` on both; `begin_nested()` degrade wrappers unchanged; `completed_subq` preserved. |
| `src/phaze/templates/proposals/partials/proposal_row.html` | Badge derived from `proposal.status` | VERIFIED | Line 46: `{% if proposal.status == "executed" %}`. Zero `proposal.file.state` readers remain in `src/phaze/templates/`. |
| `tests/shared/test_applied_clause.py` | SC#1 unit contract | VERIFIED | 7 cases, all pass in isolation (`7 passed`), including the load-bearing `state='moved'`+executed→True case. |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `tag_writer.py::execute_tag_write` | `stage_status.is_applied` | `await is_applied(session, file_record.id)` guard | WIRED | Confirmed by direct read + independently re-run mutation test. |
| `tags.py` (5 sites) | `stage_status.applied_clause`/`is_applied` | `.where(applied_clause())` / `await is_applied(...)` | WIRED | Confirmed by grep + read; `test_tags.py` (13 tests) and `test_review_apply_workspaces.py` bulk/tagwrite tests (4 tests) pass. |
| `cue.py::generate_cue` | `stage_status.is_applied` | `await is_applied(session, file_record.id)` guard before `write_cue_file` | WIRED | Confirmed; `test_cue.py` (23 tests) pass. |
| `cue.py::_get_eligible_tracklist_query` | `stage_status.applied_clause` | `.where(applied_clause())` | WIRED | Confirmed; transitively fixes `review.py`'s eligible half (Plan 03→04 dependency verified in source). |
| `tracklists.py` (3 sites) | `stage_status.is_applied` | `await is_applied(session, fr.id)` | WIRED | Confirmed; `test_tracklists.py` (67 tests) pass. |
| `review.py::get_tagwrite_review_rows` | `stage_status.applied_clause` | `.where(applied_clause(), FileRecord.id.not_in(completed_subq))` | WIRED | Confirmed; `test_review_degrade.py` (8 tests) pass. |
| `review.py::get_cue_review_cards` | `stage_status.applied_clause` | gated_stmt conjunct | WIRED | Confirmed by direct read. |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|---------------------|--------|
| `applied_clause()` | correlated `exists()` subquery | `RenameProposal.status == "executed"` (real DB column, transactionally coupled to the apply-path copy→verify→delete + PATCH) | Yes | FLOWING |
| `is_applied()` | scalar `EXISTS` query | Same source, per-record | Yes | FLOWING |
| `_get_tag_stats` pending count | `total_executed - completed - discrepancies` | Real applied/`TagWriteLog` counts | Yes, but see WR-02 below — a file with BOTH a `DISCREPANCY` and later `COMPLETED` log is double-subtracted, undercounting `pending` (latent, newly surfaced by this phase making `total_executed` non-zero). Not a blocker for goal achievement (predicate revival); noted as a real, pre-existing-review-flagged bug. | FLOWING (with a known counting bug, see anti-patterns) |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| SC#2 mutation check (tag-write guard) | Manually reverted `tag_writer.py:185` to `file_record.state != FileState.EXECUTED`, ran `test_applied_file_passes_guard` | RED (`ValueError`), then restored file, ran again | PASS (RED→GREEN independently reproduced) |
| SC#1 predicate contract | `uv run pytest tests/shared/test_applied_clause.py -q` | `7 passed` | PASS |
| Tag-writer guard tests | `uv run pytest tests/review/services/test_tag_writer.py -q` | `19 passed` | PASS |
| tags.py router tests | `uv run pytest tests/review/routers/test_tags.py -q` | `13 passed` | PASS |
| cue.py router tests | `uv run pytest tests/review/routers/test_cue.py -q` | `23 passed` | PASS |
| tracklists.py router tests | `uv run pytest tests/identify/routers/test_tracklists.py -q` | `67 passed` | PASS |
| review.py degrade tests | `uv run pytest tests/review/services/test_review_degrade.py -q` | `8 passed` | PASS |
| review-audit integration tests | `uv run pytest tests/integration/test_review_audit.py -q` | `4 passed` | PASS |
| proposal-row badge tests | `uv run pytest tests/review/routers/test_proposals.py -q` | `39 passed` | PASS |
| Post-merge regression file (flagged in verification notes) | `uv run pytest tests/shared/core/test_review_apply_workspaces.py -q` | `12 passed` (all 4 tagwrite/cue tests: `test_tag_bulk_no_discrepancy_predicate`, `test_review_audit_one_row`, `test_tagwrite_workspace_apply_and_bulk_wiring`, `test_cue_gate_and_preview`) | PASS |
| Partition guard | `uv run pytest tests/shared/test_partition_guard.py -q` | `3 passed` | PASS |
| mypy/ruff on all 6 touched source files | `uv run ruff check` + `uv run mypy` | `All checks passed!` / `Success: no issues found in 6 source files` | PASS |

Tests run against the ephemeral DB on port 5433 per the provided env vars. One environment flake was hit and resolved by hand (schema-creation race under colima VM pressure, matching documented memory `reference_local_fullsuite_colima_flake` / `reference_ci_bucket_isolation`) — resolved by dropping/recreating the `public` schema and re-running; not a code defect.

### Probe Execution

N/A — no `scripts/*/tests/probe-*.sh` probes declared or discovered for this phase.

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|-------------|--------|----------|
| READ-05 | 85-01, 85-02, 85-03, 85-04 | The dead `state == EXECUTED` gates are revived against the real apply-outcome source — tag writing, review, tags/cue/tracklists guards fire for actually-applied files | SATISFIED | All 15 dead gate sites across `stage_status.py`, `tags.py`, `tag_writer.py`, `cue.py`, `tracklists.py`, `review.py`, `proposal_row.html` now read `applied_clause()`/`is_applied()`, confirmed by direct source read + independent mutation-check re-run. **Note:** `.planning/REQUIREMENTS.md` line 46 still shows READ-05 as `[ ]` unchecked and the tracking table (line 148) still shows "Pending" — this is a documentation-sync gap (the checkbox/table were not flipped as part of phase execution), not a code gap. Flagging for housekeeping. |

No orphaned requirements found — REQUIREMENTS.md maps exactly READ-05 to Phase 85, and all 4 plans declare `requirements: [READ-05]`.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `src/phaze/services/review.py` | 117-134 | `.limit()` applied to the SQL candidate set BEFORE the Python "has >= 1 change" qualification filter | WARNING (pre-existing 85-REVIEW.md WR-01) | A large wall of zero-change applied files sorted alphabetically before a qualifying file can make `get_tagwrite_review_rows` render empty/undercounted even though qualifying files exist, at the 200K scale this phase explicitly targets. Not a blocker for the phase's stated goal (predicate revival), but undermines the D-03 bound's practical guarantee. Routed to human verification below. |
| `src/phaze/routers/tags.py` | 421-441 | Same pattern in `bulk_write_no_discrepancies` | WARNING (pre-existing 85-REVIEW.md WR-01/IN-02) | Repeated bulk-submit does not guarantee forward progress past a wall of non-qualifying applied files within the cap window. |
| `src/phaze/routers/tags.py` | 47-66 | `_get_tag_stats` double-subtracts files with both a `DISCREPANCY` and a later `COMPLETED` log | WARNING (pre-existing 85-REVIEW.md WR-02) | Latent bug now surfaced because `total_executed` is finally non-zero; under-reports `pending`. Cosmetic (stat-card display), not a write-path correctness issue. |
| `src/phaze/services/review.py` | 214-271 | `get_cue_review_cards` eligible half is bounded only by an in-memory `break`, not a SQL `.limit()` — the underlying query still materializes all approved+applied+timestamped pairs | WARNING (pre-existing 85-REVIEW.md WR-03/WR-04) | D-03 bound is partial for the CUE review path at 200K scale; combined with WR-04, total returned cards can reach 2×`_MAX_REVIEW_ROWS`. |

All four warnings were already identified and documented by the phase's own code review (`85-REVIEW.md`, status: `issues_found`, 0 critical / 4 warnings / 2 info) — this verification independently confirmed they are real (by reading the flagged code) and unresolved as of the current HEAD. No `TBD`/`FIXME`/`XXX`/`TODO`/`HACK`/`PLACEHOLDER` debt markers found in any of the 15 touched files.

### Human Verification Required

### 1. WR-01 disposition (D-03 bound ordering bug)

**Test:** Review `85-REVIEW.md` WR-01 (SQL `.limit()` before Python qualification filter in `get_tagwrite_review_rows` and `bulk_write_no_discrepancies`) and decide whether to accept it as documented follow-up debt or require a closure plan before the tag-write/review UI is used at scale.
**Expected:** A recorded decision — either an override/acceptance note, or a new phase/plan targeting the fix in the RESEARCH-suggested form (accumulate qualifying rows via keyset pagination instead of capping raw candidates).
**Why human:** This is a scale/priority tradeoff already surfaced by the phase's own code review with 0 critical findings; it does not block the phase's stated success criteria (predicate revival + mutation-verified behavior change) but does affect the real-world usability of the bulk operator tools this phase turns on for the first time. A verifier grep/test cannot adjudicate whether this is acceptable for the current milestone stage.

## Gaps Summary

No gaps against the phase's stated success criteria. Both roadmap truths are VERIFIED with independently-reproduced evidence (not just SUMMARY claims):

1. Every one of the 15 dead `FileRecord.state == FileState.EXECUTED` gate-reader sites across the 5 target files (+ the template badge) now reads the single-source `applied_clause()`/`is_applied()` predicate pair, which is expressed purely over `proposals.status == 'executed'` and never touches `FileRecord.state` or `execution_log`.
2. The milestone's one behavior-reviving, filesystem-mutating claim — "an actually-applied file now passes a guard that previously always failed" — was independently re-verified by this verifier reverting the guard, observing the test go RED, and restoring it to GREEN (not merely trusting the SUMMARY's narrated mutation check).

The phase is escalated to `human_needed` (not `passed`) solely because the phase's own code review left 4 open, unresolved WARNING-severity findings (WR-01 through WR-04) concerning the D-03 bound's correctness at the 200K scale this phase explicitly targets. These findings are real (independently confirmed by reading the flagged code) but do not falsify the phase's stated goal — they are a quality/priority question for the developer to accept or schedule follow-up work for.

Additionally, `.planning/REQUIREMENTS.md` has not been updated to mark READ-05 complete (still shows `[ ]` / "Pending") — a documentation-sync housekeeping item, not a code gap.

---

_Verified: 2026-07-10T21:52:26Z_
_Verifier: Claude (gsd-verifier)_
