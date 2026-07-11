---
phase: 86-proposals-cutover
reviewed: 2026-07-10T00:00:00Z
depth: standard
files_reviewed: 7
files_reviewed_list:
  - src/phaze/routers/agent_proposals.py
  - src/phaze/services/proposal.py
  - src/phaze/services/proposal_queries.py
  - tests/review/routers/test_agent_proposals.py
  - tests/review/services/test_proposal_queries.py
  - tests/shared/core/test_proposals_upsert.py
  - tests/shared/test_proposals_cutover_source_scan.py
findings:
  critical: 0
  warning: 3
  info: 1
  total: 4
status: issues_found
---

# Phase 86: Code Review Report

**Reviewed:** 2026-07-10
**Depth:** standard
**Files Reviewed:** 7
**Status:** issues_found

## Summary

SIDECAR-03 removes the proposal -> `FileRecord.state` cascade from three sites: `store_proposals`
(the `_TERMINAL_FILE_STATES` guard + `PROPOSAL_GENERATED` write), `update_proposal_status` /
`bulk_update_status` (the `APPROVED`/`REJECTED` `file.state` limbs), and the agent apply-PATCH
handler (the `MOVED`/`UNCHANGED` mirror write + the idempotent-replay read). `proposals.status` is
intended to become the sole review-decision authority.

**Cutover completeness — verified sound.** I traced every downstream consumer of the retired
`FileRecord.state` values. The apply/execute path selects work by
`RenameProposal.status == ProposalStatus.APPROVED` (`services/execution_dispatch.py:72,106`,
`services/collision.py:37,59`); stage eligibility and apply-outcome derive from `proposals.status`
(`services/stage_status.py:121-161`); pipeline stats were already migrated off state-grouping to
output-table counts (`services/pipeline.py:83-92`); and the `shadow_compare` apply-outcome
invariants already assert `proposals.status` as the derived side. No live reader was left depending
on a now-unwritten `FileRecord.state`, so the cutover does not strand approved/executed files. I
also confirmed the removed `FileState` imports and the `_FILE_FOLLOW` map left no dangling
references.

**Wire contract — mutation path is intact, replay path silently changed.** On the main mutation
branch the response `file_state` echo is genuinely byte-identical (the schema constrains
`body.file_state` to `Literal["moved","unchanged"] | None`, which are also the FileState `.value`s
the old `FileState(body.file_state).value` produced). The same-state idempotent-replay branch,
however, now hard-codes `file_state=None` regardless of the request body — see WR-02.

**AST guard — real teeth, one demonstrable evasion.** The source-scan guard is a genuine
`ast.walk` scan keyed on the exact `.state` attribute / `FileState.<member>` node, not a line grep,
and its false-positive GREEN checks are legitimate. But its binding model does not cover the exact
syntactic shape of the deleted `proposal_queries` cascade (`proposal.file.state = ...`) when
reintroduced with a string literal — see WR-01.

No BLOCKER-class correctness, security, or data-loss defect was found. Three robustness/contract
warnings and one defense-in-depth note follow.

## Warnings

### WR-01: Source-scan guard cannot catch the exact deleted `proposal_queries` cascade shape when reintroduced with a string literal

**File:** `tests/shared/test_proposals_cutover_source_scan.py:78-133` (with `src/phaze/services/proposal_queries.py:163-166` as the protected site)

**Issue:** The guard's `_state_reads` / `_state_writes` require `isinstance(node.value, ast.Name)`
— i.e. the `.state` attribute must hang off a *bare Name*. The cascade Plan 01 actually deleted from
`update_proposal_status` was `proposal.file.state = FileState.APPROVED.value`, whose assignment
target is `Attribute(value=Attribute(value=Name('proposal'), attr='file'), attr='state')`. Here
`node.value` is an `ast.Attribute` (`proposal.file`), **not** an `ast.Name`, so neither
`_state_writes` nor `_state_reads` flags it. Coverage of that site therefore rests *entirely* on the
`FileState.<member>` occurrence scan. If a future dev reintroduces the cascade with the string-value
form the codebase uses elsewhere — `proposal.file.state = "approved"` — there is **no** `FileState`
node, `node.value` is not a Name, and all four scanners stay silent. The guard reports clean absence
while the cascade is back.

Two compounding factors:
1. `_filerecord_bound_names` (line 89-92) only binds a local whose *direct* assignment RHS textually
   contains `FileRecord`. It does not follow transitive bindings, so a FileRecord instance obtained
   via the two-step ORM idiom the deleted `store_proposals` code itself used
   (`result = await session.execute(select(FileRecord)...)` then
   `file_record = result.scalar_one_or_none()`) is **not** in `bound` — its RHS references `result`,
   not `FileRecord`. A reintroduced `file_record.state = "moved"` off that local also evades
   `_state_writes`.
2. The mutation tests (`test_guard_flags_*`) only exercise bare-Name-base + `FileState`-enum forms
   (`file_record.state = FileState.MOVED`). They never mutate the chained-attribute base
   (`proposal.file.state`) nor the string-literal value form, so the mutation discipline that is
   supposed to prove teeth never touches the shape that evades the guard — precisely the
   "mutate *every* syntactic form" lesson in `feedback_mutation_test_guard_tests`.

**Fix:** Broaden the attribute scanners to match a `.state` attribute regardless of base kind (Name
or Attribute chain) and add mutation tests for the missed shapes:
```python
def _state_reads(tree: ast.AST) -> list[ast.Attribute]:
    bound = _filerecord_bound_names(tree)
    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and node.attr == "state" and isinstance(node.ctx, ast.Load)):
            continue
        base = node.value
        # bare FileRecord-bound name  OR  any chained `<...>.file.state` / `<...>.state` off ORM rows
        if (isinstance(base, ast.Name) and base.id in bound) or isinstance(base, ast.Attribute):
            hits.append(node)
    return hits
# + likewise for _state_writes, then add:
def test_guard_flags_chained_attr_string_write():
    assert _violations('proposal.file.state = "approved"\n') != []  # currently GREEN -> bug
```
(Matching *any* chained `.state` may need a small allow-list if unrelated `.state` chains exist in
the three files; today they do not, so the broadened scan stays false-positive-free.)

### WR-02: Same-state idempotent replay hard-codes `file_state=None`, contradicting the documented "byte-for-byte echo" contract

**File:** `src/phaze/routers/agent_proposals.py:80-92` (contract prose at lines 8-11)

**Issue:** The module docstring states the response `file_state` "is now a byte-for-byte echo of the
request's `body.file_state`." That holds on the mutation branch (line 114) but is **false** on the
same-state branch: when `cur == new` the handler returns `file_state=None` unconditionally (line 90),
ignoring `body.file_state`. A SAQ retry that replays the *full* successful body
(`{"proposal_state": "executed", "file_state": "moved", "current_path": "/x"}`) after the first
PATCH already moved the proposal to EXECUTED will hit this branch and receive `file_state=None`,
diverging from the `file_state="moved"` the first (non-replay) call returned. The only test that
covers this path (`test_same_state_idempotent_no_op`) sends a body *without* `file_state`, so the
`None` echo coincidentally matches and the divergence is never exercised. The "the only caller
discards it" justification is asserted in prose but not proven by any test in scope.

**Fix:** Make the replay branch honor the same echo rule as the mutation branch (echo
`body.file_state`, which may be `None`), so the documented contract is uniform:
```python
if cur == new:
    current_path_str = file_record.current_path if file_record is not None else None
    return ProposalStateResponse(
        proposal_id=proposal_id,
        proposal_state=cur.value,
        file_state=body.file_state,   # uniform echo (was: hard-coded None)
        current_path=current_path_str,
    )
```
If `None`-always is genuinely intended, tighten the docstring to scope the byte-for-byte claim to
the transition path only, and add a test that replays a full body and asserts the `None` response.

### WR-03: `store_proposals` bounds-checks `file_index` against `file_ids` but then indexes `files_context` with the same index

**File:** `src/phaze/services/proposal.py:306-321`

**Issue:** The WR-01 guard validates `0 <= idx < len(file_ids)` (line 307) and then uses `idx` for
**both** `file_ids[idx]` (line 310) and `files_context[idx]` (line 321). The bound is derived from
`len(file_ids)` only. If a caller ever passes `file_ids` and `files_context` of different lengths
(they are parallel arrays assembled separately by the caller), a `file_index` valid for `file_ids`
but `>= len(files_context)` raises `IndexError` and crashes the entire batch — the same
whole-batch-crash failure mode the guard was explicitly added to prevent, just via the other array.

**Fix:** Guard against both lengths (or assert the invariant once at entry):
```python
if not (0 <= idx < len(file_ids) and idx < len(files_context)):
    logger.warning("proposal file_index out of range — skipping", file_index=idx,
                   batch_size=len(file_ids), ctx_size=len(files_context))
    continue
```

## Info

### IN-01: LLM-proposed path normalization does not strip `..`; traversal safety rests entirely on one downstream guard

**File:** `src/phaze/services/proposal.py:323-327`

**Issue:** `path_raw.strip("/")` + `//`-collapse leaves `..` segments intact in the persisted
`proposed_path`, which originates from the (prompt-injectable) LLM output. This is **not** currently
exploitable: `tasks/execution.py:78-80,163-167` resolves the destination and enforces containment
within `scan_roots` (T-26-11-S1) before any move, neutralizing traversal. Flagged only as a
defense-in-depth observation — the archive's move safety depends on that single downstream check
never being bypassed by a future apply path.

**Fix (optional):** Reject or sanitize `..` segments at storage time so an out-of-root proposal is
never persisted in the first place, keeping the containment check as a second layer rather than the
sole one.

---

_Reviewed: 2026-07-10_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
