---
phase: 83-cloud-routing-sidecar-cutover
reviewed: 2026-07-09T00:00:00Z
depth: standard
files_reviewed: 7
files_reviewed_list:
  - src/phaze/services/backends.py
  - src/phaze/routers/agent_s3.py
  - src/phaze/routers/agent_push.py
  - tests/analyze/services/test_backends.py
  - tests/analyze/services/test_single_awaiting_writer.py
  - tests/agents/routers/test_agent_s3.py
  - tests/agents/routers/test_agent_push.py
findings:
  critical: 0
  warning: 1
  info: 2
  total: 3
status: issues_found
---

# Phase 83 (plan 83-07): Code Review Report

**Reviewed:** 2026-07-09
**Depth:** standard
**Files Reviewed:** 7
**Status:** issues_found

## Summary

83-07 consolidates the two hand-written over-cap spill CAS re-stamps
(`report_upload_failed`, `report_push_mismatch`) into a dual-mode
`hold_awaiting_cloud`, closing the LOCKED **D-02** "one awaiting writer" constraint.
I reviewed the refactor adversarially against every cited invariant (SC#2 /
T-83-PUSH-CLOBBER, D-00c, D-03, D-11, D-12, Landmine L1) and traced both spill
callers, the hold caller (`pipeline.trigger_analysis`), and all four test modules.

**The refactor is behavior-preserving and correct on every load-bearing invariant:**

- **CAS guard survived (SC#2 / T-83-PUSH-CLOBBER).** Spill mode is a pure
  rowcount-guarded `UPDATE ... WHERE file_id=? AND status IN (...)` returning
  `rowcount > 0`. Both callers gate 100% of side effects (FileRecord write, multipart
  abort, `delete_staged_object`, `clear_ledger_entry`) behind `cleared`. The
  0-row → FULL no-op is intact; an already-advanced (RUNNING/SUCCEEDED/reaped) row
  cannot be re-stamped to `AWAITING_CLOUD`. Verified byte-equivalent to the shipped
  inline predicates (s3: `IN(uploading,uploaded)` + `cloud_phase=None`; push:
  `==submitted`, cloud_phase untouched).
- **Short-circuit null-guard (point 2) is equivalent to a CAS miss.** `file is not
  None and await hold_awaiting_cloud(...)` short-circuits to `cleared=False` when the
  FileRecord is absent. Because `cloud_job.file_id` FKs `files.id`, an absent file
  implies an absent row (CAS would also match 0), so both paths reach the identical
  FULL no-op. No partial write precedes the guard in either handler.
- **Transaction discipline (Landmine L1) holds.** The helper NEVER commits in either
  mode; the `pg_advisory_xact_lock` (D-11, s3 line 186 / push line 249) survives to the
  caller's own commit. No mid-branch rollback.
- **D-12 / D-00c preserved.** The `values` dict adds `cloud_phase=None` only when
  `clear_cloud_phase` (s3 only); the push spill never touches `cloud_phase`. The
  `file.state=AWAITING_CLOUD` dual-write stays owned by the callers (gated behind the
  bool) in spill mode, and by the helper only in hold mode.
- **rowcount semantics sound.** `uq_cloud_job_file_id` makes the CAS match exactly 0 or
  1 rows, so `rowcount > 0` is a correct boolean; `cast("CursorResult[Any]", ...)` is
  the same idiom already used in `report_uploaded`/`report_pushed`. `CursorResult` is
  now a runtime import in `backends.py`.
- **Hold caller unaffected.** `trigger_analysis` ignores the new `-> bool` return; the
  `None -> bool` signature change is backward compatible.
- **Test discrimination confirmed.** `test_hold_awaiting_cloud_spill_cas_miss_is_full_noop`
  is genuinely RED-on-regression: it seeds `SUCCEEDED`, expects `SUBMITTED`, and asserts
  status/attempts UNCHANGED + `False` — replacing the CAS with an unconditional upsert
  would clobber the row and flip these assertions. The null-guard and clobber contract
  tests in both router suites cover the SC#2 paths end-to-end.

No BLOCKER-level defect was found. The findings below are one WARNING on the durability
of the anti-drift guard and two INFO items.

## Structural Findings (fallow)

No `<structural_findings>` block was supplied with this review; none to normalize.

## Narrative Findings (AI reviewer)

## Warnings

### WR-01: The D-02 anti-drift AST guard overclaims — a `.values(**dict)` spill writer evades it

**File:** `tests/analyze/services/test_single_awaiting_writer.py:43-56`
**Issue:** The guard's docstring claims it "goes RED the moment ANY module under
`src/phaze/` re-introduces an inline awaiting WRITE." That is only true for the
*literal-keyword* form `.values(status=CloudJobStatus.AWAITING.value, ...)`.
`_status_value_writes_awaiting` inspects `.values(...)` call sites for a literal
`status=` keyword whose subtree references `AWAITING`/`"awaiting"`. But the sole allowed
writer's own spill branch does NOT use that form — `backends.py:144-149` builds a dict
and splats it:

```python
values: dict[str, Any] = {"status": CloudJobStatus.AWAITING.value, ...}
res = await session.execute(update(CloudJob).where(...).values(**values))
```

The `**values` splat has no `status=` keyword in the AST, so this exact idiom is
invisible to the scanner. If a future edit reintroduced an inline spill CAS by
copy-pasting that (idiomatic, in-repo) pattern into `agent_s3.py` / `agent_push.py`, the
guard would stay GREEN and the D-02 "single writer" invariant would silently regress.
The straight revert of 83-07 (which re-adds the *literal-keyword* form) IS still caught —
so the guard is useful — but the "ANY inline awaiting WRITE" claim is stronger than what
is enforced. Given the guard exists specifically to protect the SC#2 clobber-safety
property, the blind spot is worth closing.

**Fix:** Broaden detection to also flag a `.values(**name)` splat whose bound dict
literal contains a `"status"` key mapping to an awaiting value, e.g. resolve simple
`dict` assignments in the same function and inspect their keys, or (simpler and
robust) additionally flag any `update(CloudJob)`/`pg_insert(CloudJob)` statement in a
non-allowed module that also references `CloudJobStatus.AWAITING`/`"awaiting"` anywhere
in its expression, not just as a `.values` keyword. At minimum, soften the docstring so
it does not claim coverage the scan does not provide.

## Info

### IN-01: Duplicated comment block in `KueueBackend.reconcile` (pre-existing, outside the 83-07 diff)

**File:** `src/phaze/services/backends.py:563-566`
**Issue:** The `# MKUE-01/D-04: thread THIS backend's KubeConfig ...` comment is
duplicated verbatim on two consecutive line pairs. Confirmed NOT part of the 83-07 diff
(pre-existing from an earlier phase), so it is out of scope for this gap-closure, but it
sits in a reviewed file and is trivially removable.
**Fix:** Delete the duplicate two-line comment (keep one copy).

### IN-02: `hold_awaiting_cloud` hold-mode `on_conflict_do_update` resets `attempts` on re-stamp (pre-existing behavior, retained)

**File:** `src/phaze/services/backends.py:122-138`
**Issue:** In hold mode, an `on_conflict` re-stamp of an existing row sets
`attempts = <arg>` (default 0). If the hold path (`trigger_analysis`,
`attempts=0`) were ever invoked on a file that already carries a budget-spent
`cloud_job` row (`attempts = cloud_submit_max_attempts` from a prior spill), this would
reset the spent-budget marker to 0 and re-open the file to cloud routing via
`select_backend`. This is identical to the pre-83-07 helper (the old code had the same
`set_={"status": ..., "attempts": ...}`), so it is NOT a regression introduced by this
plan and may be unreachable given how `trigger_analysis` selects files — but the
re-stamp/budget interaction is worth an explicit note/test if a re-trigger path exists.
**Fix:** If a re-trigger of an already-spilled file is reachable, guard hold-mode
`attempts` on conflict (e.g. `GREATEST(existing, excluded)` or omit `attempts` from the
`set_` so a re-stamp preserves the spent budget). Otherwise document the invariant that
`trigger_analysis` never re-holds a spent-budget file.

---

_Reviewed: 2026-07-09_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
