---
phase: 80-recovery-re-enqueue-cutover
plan: 05
subsystem: recovery-guards
tags: [ast-guard, source-scan, mutation-testing, recovery, reconcile, D-11, READ-03]
requires:
  - "tasks/reenqueue.py at clean absence of FileRecord.state (Plan 80-04)"
  - "tasks/reconcile_cloud_jobs.py at clean absence of FileRecord.state (Plan 80-03)"
provides:
  - "tests/shared/test_reenqueue_reconcile_source_scan.py — mutation-proven clean-absence AST guard"
  - "D-11 rejected-option rationale documented at the DERIV-04 equivalence-test boundary"
affects:
  - "tests/shared/"
  - "tests/integration/test_stage_status_equivalence.py (comment-only)"
tech-stack:
  added: []
  patterns:
    - "clean-absence AST source scan (no allow-list) — strictly stronger than the Phase-84 dedup guard"
    - "crafted-string mutation self-tests + real-file inject→RED→restore mutation discipline"
key-files:
  created:
    - "tests/shared/test_reenqueue_reconcile_source_scan.py"
  modified:
    - "tests/integration/test_stage_status_equivalence.py"
decisions:
  - "The guard is clean-absence (zero occurrences) over BOTH target files — no allow-list, because neither reenqueue.py nor reconcile_cloud_jobs.py retains any FileRecord.state writer after the cutover."
  - "The whole-tree .state-read scan already catches positional .where reads (ast.walk descends into Call.args); a dedicated _where_family_arg_violations walker + two unit tests make the Call.args+Call.keywords coverage explicit and independently testable (the Phase-83 blind-spot closure)."
  - "D-11 trap documented at the equivalence-test SCOPE boundary; the recovery-layer regression (Plan 80-04) is the real lock, not this ledger-agnostic equivalence test."
metrics:
  duration: "~35m"
  completed: "2026-07-10"
  tasks: 2
  commits: 3
  files_created: 1
  files_modified: 1
---

# Phase 80 Plan 05: Mutation-Proven "Zero FileRecord.state Reads" Guard + D-11 Boundary Doc Summary

A hermetic, mutation-proven AST source guard that locks the phase's core guarantee — **zero
`FileRecord.state` reads** in both cutover targets (`tasks/reenqueue.py`, `tasks/reconcile_cloud_jobs.py`)
— plus the D-11 `~inflight_clause`-trap rationale documented at the DERIV-04 equivalence-test boundary.

## What Was Built

### Task 1 — `tests/shared/test_reenqueue_reconcile_source_scan.py` (commit `0e7cf4b7`)

A clean-absence AST scanner over both target files. Because both files end the phase with **zero**
`FileRecord.state` / `FileState.<member>` references (recovery derives "done" from the Phase-78/81
`done_clause` / `domain_completed_clause` builders; reconcile's at-cap spill re-stamps the `cloud_job`
sidecar via `hold_awaiting_cloud`), the guard needs **no allow-list** — the invariant is simply "zero
occurrences," which is strictly stronger than the Phase-84 dedup guard (which allows exactly one surviving
`FileState.DUPLICATE_RESOLVED` dual-writer) and cannot false-positive on a legitimate writer.

Three whole-tree scans compose the violation set:
- `_state_reads` — `ast.Attribute` `.attr == "state"`, ctx `Load`, base resolves to `FileRecord` (the model
  name or any local bound from a `FileRecord` expression, e.g. `file = session.get(FileRecord, …)`).
- `_filestate_occurrences` — any `FileState.<member>` attribute access (the removed `.values(state=…)` write).
- `_getattr_state_calls` — `getattr(_, "state")`.

A supplementary `_where_family_arg_violations` walker inspects **both** `Call.args` and `Call.keywords` of
the `.where`/`.filter`/`.filter_by`/`.having` funcs — the exact Phase-83 blind spot — with two dedicated
unit tests proving the positional-arg and keyword-arg coverage.

Real-file guards (`test_reenqueue_has_zero_state_reads`, `test_reconcile_cloud_jobs_has_zero_state_reads`)
assert an empty violation list. Crafted-string mutation self-tests encode forms #1–#6 (each asserted RED),
and three GREEN false-positive checks confirm `cloud_job.status` (`.attr=="status"`), `FileRecord.id`, and
a docstring mention of `FileRecord.state` are **not** flagged.

### Task 2 — `tests/integration/test_stage_status_equivalence.py` (commit `ed0cdb36`)

Comment-only amendment (17 inserted lines) to the DERIV-04 SCOPE block (`:415-427`): the D-11
rejected-option rationale — `~inflight_clause(stage)` must NEVER enter `domain_completed_clause` because
every recovery candidate is a ledger row, so the disjunct would return `domain_completed=False` for all of
them and silently disable the secondary over-enqueue net (the 44.5K incident class). The trap is a silent
no-op for the drain, the count card, and these `*_inflight`-excluded cells, so the equivalence test stays
GREEN under it — the real lock is the Plan 80-04 recovery-layer regression (Cell B goes RED). `DOMAIN_COMPLETED_CASES`
and all assertion structure are unchanged; the `*_inflight` seed exclusion is kept.

## Mutation-Test Discipline (SC-1, project rule `feedback_mutation_test_guard_tests`)

A guard never seen RED is worthless. Every one of RESEARCH §(b) forms #1–#6 was injected into the **real**
target file and observed to flip the classifier from 0 → ≥1 violations (RED), then restored:

| Form | Mutation | Target file | Violations 0→N | Verdict |
|------|----------|-------------|----------------|---------|
| #1 | `select(FileRecord.id).where(FileRecord.state.in_([…]))` | reenqueue.py | 0→1 | RED |
| #2 | `.where(FileRecord.state == FileState.ANALYZED)` | reenqueue.py | 0→2 | RED |
| #3 | `update(CloudJob).values(state=FileState.AWAITING_CLOUD)` | reconcile_cloud_jobs.py | 0→1 | RED |
| #4 | `file.state` (FileRecord-bound local) | reconcile_cloud_jobs.py | 0→1 | RED |
| #5 | `getattr(_, "state")` | reenqueue.py | 0→1 | RED |
| #6 | positional `.where(a, b, FileRecord.state != …)` | reenqueue.py | 0→2 | RED |

Additionally, one **on-disk + real-pytest + git-restore** round-trip was performed against
`tasks/reenqueue.py` (inject form #1 → `test_reenqueue_has_zero_state_reads` FAILED → `git checkout` →
re-run PASSED), confirming the guard has teeth at the pytest layer and leaves the real file clean.

## Deviations from Plan

None affecting behavior. One tooling deviation:

**[Rule 3 — Blocking issue] Ruff SIM102 (nested-if) auto-fix + manual flatten.** The pre-commit `ruff`
hook flagged two nested `if` statements in the scanner helpers; one was auto-fixed by the hook and the
second (an `isinstance`-narrowing guard in `_where_family_arg_violations`) was flattened by hand into a
single `if` with an `and`-joined boolean (logically identical). Re-verified: `ruff check` clean, 13/13
guard tests pass.

## Verification

- `just test-bucket shared` (with the ephemeral test DB up): **1028 passed**. The guard's 13 tests are
  hermetic (DB-free); the shared bucket also contains DB-fixture service tests (`test_pipeline*`) that
  ERROR only when the test DB is down — unrelated to this plan.
- `just test-bucket integration` equivalence file: `tests/integration/test_stage_status_equivalence.py`
  **36 passed** (green after the D-11 comment amendment).
- Real target files (`reenqueue.py`, `reconcile_cloud_jobs.py`) unmodified — `git diff --stat` empty.

## Known Stubs

None.

## Threat Flags

None. This plan is hermetic test code + a comment amendment with no runtime input surface. T-80-14
(silent `FileRecord.state` drift) is now mitigated by the mutation-proven guard; T-80-15 (the
`~inflight_clause` trap) is documented (accept) + mitigated by the Plan 80-04 recovery regression.

## Commits

- `0e7cf4b7` — test(80-05): mutation-proven AST guard for zero FileRecord.state reads
- `ed0cdb36` — docs(80-05): amend DERIV-04 SCOPE comment with the D-11 rejected-option rationale
