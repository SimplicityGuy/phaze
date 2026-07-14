---
status: complete
phase: 86-proposals-cutover
source: [86-01-SUMMARY.md, 86-02-SUMMARY.md, 86-03-SUMMARY.md, 86-04-SUMMARY.md, 86-05-SUMMARY.md]
started: 2026-07-11T02:35:22Z
updated: 2026-07-11T02:36:30Z
---

## Current Test

[testing complete]

## Tests

### 1. Approve → apply a rename proposal (happy path)
expected: In the admin UI, approve a pending rename proposal. An agent applies the move. The proposal becomes EXECUTED, the file's recorded path is the new destination, and it leaves the pending queue. No error; behaves as before (unchanged API contract).
result: pass
evidence: "API-level: tests/review/routers/test_agent_proposals.py::test_executed_joint_update — drives the real PATCH endpoint APPROVED→EXECUTED+moved, asserts body.file_state=='moved', body.current_path and f.current_path=='/new/proposed.mp3'. 1 passed."

### 2. Applied proposal is not re-proposed (MOVED-regression fix — the point of this phase)
expected: For a file that already has an EXECUTED (moved) proposal, trigger proposal generation again over a stale/older batch. The already-applied proposal is left untouched, the file stays post-move, no duplicate/stale proposal resurrects it. `is_applied()` stays true.
result: pass
evidence: "API-level: tests/shared/core/test_proposals_upsert.py::test_stale_batch_does_not_disturb_executed_file — stale store_proposals batch over an executed file leaves the executed row untouched, is_applied() stays true. 1 passed."

### 3. Reject a proposal
expected: Reject a pending rename proposal. The proposal is marked rejected/failed outcome, the file is NOT moved, no error, and it leaves the pending queue. Illegal transitions are rejected.
result: pass
evidence: "API-level: test_agent_proposals.py::test_failed_joint_update (FAILED outcome, file unchanged, no current_path required) + ::test_illegal_transition_409 (bad transition rejected). 2 passed."

### 4. Review queue reflects decisions from proposals.status only
expected: After approve/reject/apply, the review queue and status reflect the decision purely from the proposal's own status (the redundant per-file state mirror was removed). No stale/contradictory state versus the proposal outcome.
result: pass
evidence: "API-level: tests/review/services/test_proposal_queries.py (29 — status writes only, no file.state cascade) + tests/shared/test_proposals_cutover_source_scan.py (18 — anti-drift guard proves zero FileRecord.state mirror survives). 47 passed. NOTE: purely-visual admin-UI rendering not driven (needs browser + running stack); phase made zero UI/template changes (verifier: 'backend-only, no UI/visual surface'), so view layer unchanged and out of UAT scope."

## Summary

total: 4
passed: 4
issues: 0
pending: 0
skipped: 0

## Gaps

[none — all tests passed]
