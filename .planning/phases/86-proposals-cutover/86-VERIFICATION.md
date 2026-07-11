---
phase: 86-proposals-cutover
verified: 2026-07-11T02:15:00Z
status: gaps_found
score: 7/9 must-haves verified
overrides_applied: 0
gaps:
  - truth: "Full test suite (review bucket) is green after this phase's deletions — no test in the repository asserts the removed FileRecord.state cascade"
    status: failed
    reason: "tests/review/services/test_proposal.py::TestStoreProposals::test_creates_rename_proposal_records asserts `file_record.state == \"proposal_generated\"` after calling store_proposals. This assertion tested the exact cascade write Plan 01 deleted (`store_proposals` no longer touches file.state). The test predates Phase 86 (last touched in Phase 63) and was never in any of the three plans' `files_modified` lists, so no plan updated or removed it. Reproduced deterministically both in the full `tests/review` bucket run (1 failed, 427 passed) and in isolation. All three phase SUMMARY.md files claim \"review bucket green\" but this claim rests on running only the specific proposal-scoped files (test_proposal_queries.py, test_agent_proposals.py), never the full `tests/review` bucket — the SUMMARY claims are not supported by an actual full-bucket run."
    artifacts:
      - path: "tests/review/services/test_proposal.py"
        issue: "TestStoreProposals::test_creates_rename_proposal_records (line 638-675) asserts file_record.state == 'proposal_generated', a behavior this phase intentionally removed. Currently FAILING."
    missing:
      - "Update or delete the stale assertion at tests/review/services/test_proposal.py:675 (and the docstring/inline comment above it claiming \"the file state advances\") to match the SIDECAR-03 cutover — no writer sets file.state from store_proposals anymore."
      - "Re-run the full `tests/review` bucket (not just the individually touched files) to confirm zero regressions before re-verifying."
  - truth: "The AST source-scan guard (tests/shared/test_proposals_cutover_source_scan.py) is mutation-verified for every syntactic form of the deleted cascade, per its own must-have text"
    status: partial
    reason: "Confirmed by direct reproduction (matches code-review WR-01): _state_reads/_state_writes only fire when the `.state` attribute hangs off a bare ast.Name that is bound via _filerecord_bound_names, which requires the name's DIRECT assignment RHS to textually contain \"FileRecord\". Two real evasions verified: (1) `proposal.file.state = \"approved\"` — the exact literal shape Plan 01 deleted from update_proposal_status — has an ast.Attribute base (`proposal.file`), not a Name, so no scanner fires; guard returns []. (2) The two-step ORM idiom the deleted store_proposals code itself used (`result = await session.execute(select(FileRecord)...); file_record = result.scalar_one_or_none(); file_record.state = \"moved\"`) also evades detection — `file_record`'s binding RHS is `result.scalar_one_or_none()`, which does not textually contain \"FileRecord\", so `file_record` is never added to `bound` and the write is invisible to the guard. The mutation suite (7 test_guard_flags_* cases) never exercises either shape, so the guard's own tests do not prove it catches the actual deleted-cascade forms — the exact 'mutate every syntactic form' gap this project's memory (feedback_mutation_test_guard_tests) calls out."
    artifacts:
      - path: "tests/shared/test_proposals_cutover_source_scan.py"
        issue: "_filerecord_bound_names / _state_reads / _state_writes require a bare-Name base bound via a direct 'FileRecord'-textual RHS; chained-attribute bases (proposal.file.state) and the two-step ORM-idiom binding are both blind spots. Confirmed via direct python repro, not just code-review narrative."
    missing:
      - "Broaden _state_reads/_state_writes to flag a `.state` attribute regardless of base kind (Name or Attribute chain), per code-review WR-01's suggested fix."
      - "Add test_guard_flags_chained_attr_string_write and a two-step-ORM-idiom mutation case to the permanently-encoded RED suite."
deferred: []
human_verification: []
---

# Phase 86: Proposals Cutover Verification Report

**Phase Goal:** Make `proposals.status` the sole authority for review decisions and apply outcomes, deleting the redundant, drift-prone `FileRecord.state` cascade (where the `store_proposals` MOVED-regression bug lives).
**Verified:** 2026-07-11T02:15:00Z
**Status:** gaps_found
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Review decisions/apply outcomes read from `proposals.status`; `_TERMINAL_FILE_STATES` cascade deleted from all 3 files (Roadmap SC1) | VERIFIED | `grep -c "FileState"` = 0 in `proposal.py`, `proposal_queries.py`, `agent_proposals.py`; direct source read confirms `store_proposals` no longer loads/writes `FileRecord`; `update_proposal_status`/`bulk_update_status` write only `proposals.status`; `patch_proposal_state` writes/reads zero `file_record.state`. `execution_log` join for `done(apply)` already lives in `stage_status.py` (Phase 85, untouched). |
| 2 | `store_proposals` MOVED-regression evaporates, proven by a test (Roadmap SC2) | VERIFIED | `tests/shared/core/test_proposals_upsert.py::test_stale_batch_does_not_disturb_executed_file` (lines 183-215) seeds an EXECUTED proposal, re-runs a stale `store_proposals` batch, and asserts from an independent post-commit read that the row is untouched and `is_applied()` stays True. Passes (`5 passed` in that file). |
| 3 | `proposal_queries.py` still writes `proposals.status` for APPROVED/REJECTED (86-01 must-have) | VERIFIED | `update_proposal_status`:163 `proposal.status = new_status.value`; `bulk_update_status`:178 `values(status=new_status.value)`; `tests/review/services/test_proposal_queries.py` 29/29 pass including `test_update_proposal_status_approve`/`_reject`, `test_bulk_update_status`. |
| 4 | Apply-PATCH echoes `body.file_state`, persists `current_path`, wire byte-identical on the mutation branch (86-02 must-have) | VERIFIED | `agent_proposals.py:107-119` — `response_file_state = body.file_state`; `current_path` write preserved; `test_executed_joint_update`/`test_failed_joint_update` assert `body["file_state"]`/`body["current_path"]` and a positive `f.state == FileState.APPROVED.value` (seed-unchanged) guard. All pass. |
| 5 | Same-state idempotent replay returns 200 without reading `file.state`, echoing `None` (86-02 must-have) | VERIFIED | `agent_proposals.py:80-92` no longer reads `file_record.state`; hard-codes `file_state=None`. `test_same_state_idempotent_no_op` asserts `r2.json()["file_state"] is None`. Passes. |
| 6 | Cross-tenant 403 guard + request schema unchanged (86-02 must-have) | VERIFIED | `agent_proposals.py:67-72` guard block byte-identical to pre-phase; `schemas/agent_proposals.py` not in `files_modified` for any plan; `test_proposal_cross_agent_403` passes. |
| 7 | AST source-scan guard proves clean absence across all 3 files, walks `Call.args`+`Call.keywords`, existence-asserted root (86-03 must-have) | VERIFIED | `tests/shared/test_proposals_cutover_source_scan.py` — 16/16 tests pass (independently re-run); `grep -c "\.keywords"` = 6; module-level `assert <target>.exists()` present for all 3 targets at `parents[2]`. |
| 8 | AST guard is mutation-verified for **every syntactic form** of the deleted cascade (86-03 must-have text) | **FAILED** | Reproduced directly: `_violations('proposal.file.state = "approved"\n')` returns `[]` (the exact literal shape deleted from `update_proposal_status`) and the two-step ORM idiom the deleted `store_proposals` code used also evades detection. See Gap 2 below — confirms code-review WR-01. |
| 9 | Full `tests/review` bucket is green — no test in the repo asserts the removed cascade (implicit from "deleting the redundant cascade" + plan's own verification requirement) | **FAILED** | `uv run pytest tests/review -q` → **1 failed, 427 passed**. `tests/review/services/test_proposal.py::TestStoreProposals::test_creates_rename_proposal_records` asserts `file_record.state == "proposal_generated"`, contradicting the phase's own deletion. Not in any plan's `files_modified`; reproduced deterministically in isolation too. See Gap 1 below. |

**Score:** 7/9 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/services/proposal.py` | `store_proposals` upsert with cascade block removed | VERIFIED | Zero `FileState`/`.state` occurrences; `pg_insert...on_conflict_do_update` intact; `ruff`+`mypy` clean |
| `src/phaze/services/proposal_queries.py` | `update_proposal_status`/`bulk_update_status` write only `proposals.status` | VERIFIED | Zero `FileState`/`.file.state`; `ruff`+`mypy` clean |
| `src/phaze/routers/agent_proposals.py` | `patch_proposal_state` echoes request, zero `file_record.state` reads/writes | VERIFIED | Confirmed via full file read; `ruff`+`mypy` clean |
| `tests/shared/core/test_proposals_upsert.py` | D-03 regression test | VERIFIED | `test_stale_batch_does_not_disturb_executed_file` present, passes |
| `tests/review/services/test_proposal_queries.py` | Status-only assertions | VERIFIED | 29/29 pass |
| `tests/review/routers/test_agent_proposals.py` | Echo/seed-unchanged/replay-None assertions | VERIFIED | 11/11 pass |
| `tests/shared/test_proposals_cutover_source_scan.py` | Mutation-verified AST guard | ⚠️ PARTIAL | 16/16 pass but demonstrable blind spot (chained-attribute base + two-step ORM idiom) — see Gap 2 |
| `tests/review/services/test_proposal.py` | (not a phase-declared artifact — pre-existing file) | ✗ REGRESSED | `test_creates_rename_proposal_records` FAILS after this phase's deletions; never updated by any plan |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|----|--------|---------|
| `test_proposals_upsert.py` | `services/stage_status.py` | `is_applied(session, file_id)` | WIRED | Imported and called in the D-03 test; passes |
| `proposal.py` | `proposals` table | `pg_insert(...).on_conflict_do_update(index_where=status=='pending')` | WIRED | Confirmed intact at lines 340-355 |
| `agent_proposals.py` | `ProposalStateResponse.file_state` | `response_file_state = body.file_state` | WIRED | Confirmed at line 114; response shape unchanged |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| D-03 regression test passes | `uv run pytest tests/shared/core/test_proposals_upsert.py -v` | 5 passed | ✓ PASS |
| proposal_queries tests pass | `uv run pytest tests/review/services/test_proposal_queries.py -v` | 29 passed | ✓ PASS |
| agent_proposals router tests pass | `uv run pytest tests/review/routers/test_agent_proposals.py -v` | 11 passed | ✓ PASS |
| AST source-scan guard passes | `uv run pytest tests/shared/test_proposals_cutover_source_scan.py -v` | 16 passed | ✓ PASS |
| Full `tests/shared` bucket green | `uv run pytest tests/shared -q` | 1057 passed | ✓ PASS |
| Full `tests/review` bucket green | `uv run pytest tests/review -q` | **1 failed, 427 passed** | ✗ FAIL |
| Lint/type clean on touched files | `uv run ruff check` + `uv run mypy` on the 3 source files | All checks passed / Success: no issues found | ✓ PASS |
| AST guard blind-spot repro (chained attr) | `_violations('proposal.file.state = "approved"\n')` | `[]` (should flag) | ✗ FAIL (confirms Gap 2) |
| AST guard blind-spot repro (two-step ORM idiom) | `_violations('result = ...; file_record = result.scalar_one_or_none(); file_record.state = "moved"\n')` | `[]` (should flag) | ✗ FAIL (confirms Gap 2) |

### Probe Execution

SKIPPED — no `scripts/*/tests/probe-*.sh` declared or found for this phase.

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|--------------|--------|----------|
| SIDECAR-03 | 86-01, 86-02, 86-03 | Review decisions/apply outcomes read from `proposals.status` + `execution_log`; `FileRecord.state` no longer a redundant mirror; `store_proposals` MOVED-regression fixed | ⚠️ MOSTLY SATISFIED | Core deletion and regression test verified (truths 1-7). Undermined by two concrete gaps: a stale test still asserting the removed cascade (now failing, truth 9) and a real, reproduced blind spot in the anti-drift guard the phase shipped as "insurance" (truth 8). No orphaned requirements — REQUIREMENTS.md line 151 maps SIDECAR-03 to Phase 86 only, matching all three plans' `requirements:` frontmatter. |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `tests/review/services/test_proposal.py` | 675 | Stale assertion on deleted behavior (`file_record.state == "proposal_generated"`) | 🛑 Blocker | Test suite is not green; contradicts the phase's own deletion; CI review-bucket job would fail |
| `tests/shared/test_proposals_cutover_source_scan.py` | 78-133 (guard helpers) | Attribute-scan blind spot: bare-Name-with-textual-"FileRecord"-RHS binding requirement misses chained-attribute bases and two-step ORM idioms | ⚠️ Warning | Anti-drift guard has real teeth for many forms but not the exact shape of the actually-deleted cascade; matches code-review WR-01 |
| `src/phaze/routers/agent_proposals.py` | 1-11 (docstring), 90 | Docstring claims response `file_state` is a "byte-for-byte echo" universally; the same-state replay branch (line 90) hard-codes `None` regardless of `body.file_state` | ℹ️ Info | Functional behavior was an explicit "Claude's Discretion" call sanctioned in 86-CONTEXT.md D-02 ("None vs echoing body.file_state — cosmetic, both honest") and disclosed transparently in the 86-02 SUMMARY; only the docstring's blanket wording overclaims. Matches code-review WR-02. |
| `src/phaze/services/proposal.py` | 306-321 | `file_index` bounds-checked only against `len(file_ids)`, then also indexes `files_context[idx]` — an `IndexError` is possible if the two parallel arrays ever differ in length | ℹ️ Info | Not introduced by this phase's edits (pre-existing WR-01 from code review, unrelated to the state cascade); noted for completeness, does not affect SIDECAR-03 goal |

### Human Verification Required

None — this phase is backend-only (service + router + test-only changes), no UI/visual/real-time surface introduced.

### Gaps Summary

Two gaps block a clean `passed` status:

1. **A pre-existing test outside the phase's declared scope is now failing.** `tests/review/services/test_proposal.py::TestStoreProposals::test_creates_rename_proposal_records` asserts the exact `file_record.state == "proposal_generated"` write that Plan 01 deleted from `store_proposals`. This file was never touched by any of the three plans (`files_modified` lists cover `test_proposal_queries.py`, `test_agent_proposals.py`, `test_proposals_upsert.py`, and the new source-scan guard — not `test_proposal.py`). Reproduced deterministically both in a full `tests/review` bucket run (1 failed, 427 passed) and in isolation. This directly contradicts every SUMMARY.md's "review bucket green" claim — none of the three executors actually ran the full bucket; each ran only the specific files their own plan touched.

2. **The AST anti-drift guard's own must-have claims mutation coverage it does not have.** 86-03's must-have text explicitly promises "mutation-verified... for every syntactic form." Direct reproduction confirms code-review finding WR-01 is real: the guard's `_filerecord_bound_names` requires a bound name's direct assignment RHS to textually contain `"FileRecord"`, so (a) the exact literal shape actually deleted from `update_proposal_status` (`proposal.file.state = "approved"`, a chained-attribute base) and (b) the two-step ORM idiom `store_proposals` itself used before deletion (`file_record = result.scalar_one_or_none()`) both evade every scanner in the guard. The guard still has real value against the forms it does test (bare-Name + `FileState` enum), but its central selling point — "cannot be fooled... unlike a line-grep" — does not hold for the actual deleted-code shapes.

Neither gap indicates the core application-code deletion is incomplete — direct source inspection independently confirms zero `FileState`/`.state` occurrences in all three production files, and 1057/1057 shared-bucket tests + 44/45 targeted proposal tests pass. The gaps are in test-suite completeness (a missed stale test) and in the guard's self-claimed mutation coverage — both fixable without touching the already-correct production code.

---

*Verified: 2026-07-11T02:15:00Z*
*Verifier: Claude (gsd-verifier)*
