# Phase 86: Proposals Cutover - Pattern Map

**Mapped:** 2026-07-10
**Files analyzed:** 7 (3 writer-site deletions, 1 new test, 3 test adaptations)
**Analogs found:** 7 / 7 (all analogs are in-tree — this is a deletion/refactor phase, every pattern already exists locally)

> **Nature of this phase:** writer-DELETION. Most "files to modify" are *removals* of a `proposal → FileRecord.state` cascade write from existing files. The only genuinely NEW code is two test artifacts (the D-03 regression test and the AST source-scan guard). There is no new production module. Do NOT touch `services/shadow_compare.py` (D-04). Copy patterns from the analogs below verbatim so the deletions land cleanly and the new tests match in-tree idiom.

## File Classification

| File | Role | Data Flow | Closest Analog | Match Quality |
|------|------|-----------|----------------|---------------|
| `src/phaze/services/proposal.py` | writer-site deletion | CRUD (upsert) | self (`store_proposals` upsert stays; only the state-cascade block goes) | exact |
| `src/phaze/services/proposal_queries.py` | writer-site deletion | CRUD (status update) | self (keep `proposals.status` write, drop `.file.state` limbs) | exact |
| `src/phaze/routers/agent_proposals.py` | writer-site deletion + contract rework | request-response (PATCH) | self (`patch_proposal_state`; echo request, drop `file.state`) | exact |
| `tests/shared/core/test_proposals_upsert.py` | test-new (D-03) + test-adapt | integration | self: `test_rerun_never_touches_approved_row` (`:104-139`) | exact |
| `tests/shared/test_proposals_cutover_source_scan.py` | test-new (AST guard) | source-scan (DB-free) | `tests/shared/test_reenqueue_reconcile_source_scan.py` | exact |
| `tests/review/services/test_proposal_queries.py` | test-adapt | integration | self: `test_update_proposal_status_approve` (`:250-255`) | exact |
| `tests/review/routers/test_agent_proposals.py` | test-adapt | integration | self: `test_executed_joint_update` (`:72-93`) | exact |

## Shared Patterns

### Independent-session assertion (the load-bearing test pattern)
**Source:** `tests/shared/core/test_proposals_upsert.py:104-139` (`test_rerun_never_touches_approved_row`)
**Apply to:** the new D-03 test, and any assertion that a row was/was-not written.

The `conftest` `get_session` override makes the handler and the test share a session, so the router must `await session.commit()` itself and the test asserts via a **fresh `select(...)`** after commit (project memory `project_get_session_never_commits`). The existing analog already does exactly this — a `store_proposals` call, `await session.commit()`, then a re-`select` of the untouched row:

```python
# Re-run proposal generation for the same file.
await store_proposals(session, [str(file_id)], _batch("regenerated.mp3", reasoning="regen"), [{"ctx": "regen"}])
await session.commit()

# The APPROVED row is byte-for-byte untouched.
approved = (await session.execute(select(RenameProposal).where(RenameProposal.id == approved_id))).scalar_one()
assert approved.status == ProposalStatus.APPROVED
```

For the router tests the same pattern appears as `await session.commit(); session.expire_all()` before the verifying `select` (`test_agent_proposals.py:87-90`). Keep that idiom.

### `is_applied()` reuse (never read `file.state`)
**Source:** `src/phaze/services/stage_status.py:148-166`
**Apply to:** the D-03 regression assertions.
Signature: `async def is_applied(session: AsyncSession, file_id: uuid.UUID) -> bool`. Single scalar `EXISTS` over `proposals.status == 'executed'`. The D-03 test asserts `await is_applied(indep_session, file_id) is True` **instead of** reading `file.state` — this is the Phase-85 predicate kept deliberately stable so Phase 86 needs no rework.

### Mutation-verified guard discipline
**Source:** project memory `feedback_mutation_test_guard_tests` + `tests/shared/test_reenqueue_reconcile_source_scan.py:220-357`
**Apply to:** the new source-scan guard.
A GREEN guard proves nothing until it has gone RED. The analog encodes the mutation directions permanently as `test_guard_flags_*` (RED on crafted source strings) and `test_guard_ignores_*` (GREEN false-positive checks). Copy that structure. Line-grep guards are toothless against multi-line SQLAlchemy and `.values(state=...)` splats — use the AST walk.

---

## Pattern Assignments

### `src/phaze/services/proposal.py` (writer-site deletion, CRUD)

**Analog:** self. Delete sites 1a/1b/1c; the `pg_insert` upsert (`:348-364`) STAYS.

**Import to trim** (`:18`) — drop `FileState` only, keep `FileRecord`:
```python
from phaze.models.file import FileRecord, FileState   # -> from phaze.models.file import FileRecord
```
`FileRecord` is still used (`build_file_context` typing, `load_companion_contents`). `FileState` becomes unused after 1a+1b → ruff `F401` will fail if left.

**Frozenset to delete entirely** (`:35-39`, D-04 "fully delete, no repurpose"):
```python
_TERMINAL_FILE_STATES: frozenset[FileState] = frozenset({FileState.APPROVED, FileState.REJECTED, FileState.EXECUTED, FileState.DUPLICATE_RESOLVED})
```

**Core deletion — the whole file-load-and-guard block** (`:366-373`), this is where the MOVED-regression bug lives. Delete all of it (the `select(FileRecord)` load exists ONLY to write state; `file_record` is used nowhere else in the loop — Claude's-discretion resolved to "remove the dead load"):
```python
# Update file state -- forward-only (WR-04). ...
result = await session.execute(select(FileRecord).where(FileRecord.id == uuid.UUID(fid)))
file_record = result.scalar_one_or_none()
if file_record is not None and file_record.state not in _TERMINAL_FILE_STATES:
    file_record.state = FileState.PROPOSAL_GENERATED

count += 1   # <- KEEP this line
return count # <- KEEP
```
**KEEP:** `select` import (`:14`) — still used elsewhere in the module. Verify with `ruff check` (Pitfall 4).

---

### `src/phaze/services/proposal_queries.py` (writer-site deletion, CRUD)

**Analog:** self. Two limbs go; the `proposals.status` writes STAY.

**Site 2 — `update_proposal_status`** (`:164-168`), delete the `.file.state` block, keep `proposal.status = new_status.value` (`:163`) and the re-select tail (`:170-174`):
```python
proposal.status = new_status.value            # KEEP
# Transition FileRecord.state alongside proposal status (APR-02)   <- DELETE from here
if new_status == ProposalStatus.APPROVED:
    proposal.file.state = FileState.APPROVED.value
elif new_status == ProposalStatus.REJECTED:
    proposal.file.state = FileState.REJECTED.value               # <- to here
await session.commit()                        # KEEP
```

**Site 3 — `bulk_update_status`** (`:185-189`), delete the `file_state` derivation + the `select(RenameProposal.file_id)` subquery + the `update(FileRecord).values(state=...)`, keep the `update(RenameProposal).values(status=...)` (`:183`) and `return int(cursor_result.rowcount)`:
```python
cursor_result: Any = await session.execute(stmt)   # KEEP
# Transition FileRecord.state for all affected files (APR-02)    <- DELETE from here
file_state = FileState.APPROVED.value if new_status == ProposalStatus.APPROVED else FileState.REJECTED.value
file_ids_stmt = select(RenameProposal.file_id).where(RenameProposal.id.in_(proposal_ids))
file_update = update(FileRecord).where(FileRecord.id.in_(file_ids_stmt)).values(state=file_state)
await session.execute(file_update)                              # <- to here
await session.commit()                             # KEEP
return int(cursor_result.rowcount)                 # KEEP
```
`approve_pending_above_confidence` (`:194`) reuses `bulk_update_status` and needs no other change. Check whether `FileState` / `FileRecord` imports go unused after both limbs are gone (ruff `F401`).

---

### `src/phaze/routers/agent_proposals.py` (writer-site deletion + contract rework, request-response)

**Analog:** self. Rework `patch_proposal_state`; keep the wire response byte-identical (D-02).

**Import to trim** (`:31`) — drop `FileState`, keep `FileRecord` (used at `:62`,`:71`):
```python
from phaze.models.file import FileRecord, FileState   # -> keep only FileRecord
```

**`_FILE_FOLLOW` map — now dead, delete** (`:45-50`) — it was the only other `FileState` consumer:
```python
_FILE_FOLLOW: dict[ProposalStatus, FileState] = {
    ProposalStatus.EXECUTED: FileState.MOVED,
    ProposalStatus.FAILED: FileState.UNCHANGED,
}
```

**Site 5 — idempotent same-state branch** (`:84-95`), stop reading `file_record.state` at `:88`. Per discretion, echo `None` on replay (recommended — no outcome was requested on a pure replay). `current_path` echo (`:89`) is NOT part of the state cascade — may stay:
```python
if cur == new:
    file_state_str: str | None = None
    current_path_str: str | None = None
    if file_record is not None:
        file_state_str = file_record.state          # <- DELETE this read (site 5)
        current_path_str = file_record.current_path  # <- MAY keep (real path, not cascade)
    return ProposalStateResponse(
        proposal_id=proposal_id,
        proposal_state=cur.value,
        file_state=file_state_str,                   # -> becomes None (or body.file_state)
        current_path=current_path_str,
    )
```

**Site 4 — apply-outcome limb** (`:110-121`), delete `file_record.state =` ONLY; KEEP the `current_path` write; source the response `file_state` from `body.file_state` (the request echo, D-02) instead of the written `new_file_state.value` (Pitfall 3 — do NOT drop `current_path`):
```python
if body.file_state is not None and file_record is not None:
    new_file_state = FileState(body.file_state)   # <- DELETE (no longer need the enum)
    file_record.state = new_file_state.value      # <- DELETE (the cascade write, site 4)
    response_file_state = new_file_state.value     # -> response_file_state = body.file_state
    if body.current_path is not None:              # <- KEEP this whole block
        file_record.current_path = body.current_path
        response_current_path = body.current_path
    else:
        response_current_path = file_record.current_path
```
The final `ProposalStateResponse(...)` (`:126-131`) is unchanged in shape — `file_state` / `current_path` still present (byte-identical wire contract). Only caller `tasks/execution.py:205,269` discards the response → zero risk.

---

### `tests/shared/core/test_proposals_upsert.py` (test-new D-03 + test-adapt, integration — `shared` bucket)

**Analog:** self, `test_rerun_never_touches_approved_row` (`:104-139`) — the seed-an-existing-proposal + re-run + assert-row-untouched shape is exactly the D-03 shape, swapping `APPROVED` → `EXECUTED` and adding an `is_applied` assertion.

**ADAPT `test_fresh_insert_stamps_pk`** — delete the state assertion (`:157-159`), keep PK/status/path:
```python
# DELETE these three lines (no writer sets PROPOSAL_GENERATED anymore):
file_record = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one()
assert file_record.state == FileState.PROPOSAL_GENERATED
```

**REPLACE `test_rerun_does_not_regress_terminal_file_state`** (`:185-203`) with the D-03 test. Its premise (state forward-only guard) is being deleted, so the whole test goes. New test seeds an **executed proposal** (not a `file.state`), runs a stale `store_proposals` batch, and asserts from an independent read that the executed proposal row is untouched and `is_applied()` is True. Reuse the file's `_seed_file` / `_batch` / `_count` helpers (`:39-75`). New imports needed: `from phaze.services.stage_status import is_applied`. Skeleton (copy the analog's commit-then-reselect idiom):
```python
@pytest.mark.asyncio
async def test_stale_batch_does_not_disturb_executed_file(session: AsyncSession) -> None:
    """D-03: a stale store_proposals batch on an already-applied file leaves the executed proposal
    row untouched and is_applied() True (the MOVED-regression bug is gone)."""
    file_id = await _seed_file(session)
    executed_id = uuid.uuid4()
    session.add(RenameProposal(id=executed_id, file_id=file_id, proposed_filename="done.mp3",
                               proposed_path="done/path", confidence=1.0,
                               status=ProposalStatus.EXECUTED, context_used={}, reason="applied"))
    await session.commit()

    await store_proposals(session, [str(file_id)], _batch("regen.mp3", reasoning="regen"), [{"ctx": "regen"}])
    await session.commit()

    executed = (await session.execute(select(RenameProposal).where(RenameProposal.id == executed_id))).scalar_one()
    assert executed.status == ProposalStatus.EXECUTED   # untouched
    assert await is_applied(session, file_id) is True    # reads proposals, never file.state
```
> Note: `test_rerun_never_touches_approved_row` (`:104-139`) STAYS — it asserts the PROPOSAL ROW via the partial index, which is the real safety guarantee and is not affected by the cutover. Only the `FileState`-import usage must be reviewed if every remaining reference is a fixture seed (`state=FileState.ANALYZED` at `:51` keeps `FileState` imported).

---

### `tests/shared/test_proposals_cutover_source_scan.py` (test-new AST guard, source-scan — `shared` bucket)

**Analog:** `tests/shared/test_reenqueue_reconcile_source_scan.py` (the named template). Copy its helper scaffold verbatim, retarget the three source files, and keep the mutation + false-positive test suites.

**Path scaffold** (`:58-66`) — this file lives at `tests/shared/`, so `parents[1]` is repo root (NOT `parents[2]` — the template is one dir deeper). Verify the depth when copying:
```python
_SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "phaze"   # tests/shared/ -> parents[1]
_PROPOSAL = _SRC_ROOT / "services" / "proposal.py"
_PROPOSAL_QUERIES = _SRC_ROOT / "services" / "proposal_queries.py"
_AGENT_PROPOSALS = _SRC_ROOT / "routers" / "agent_proposals.py"
```

**Reusable walkers — copy verbatim** (`:68-174`): `_filerecord_bound_names`, `_state_reads`, `_filestate_occurrences`, `_getattr_state_calls`, `_where_family_arg_violations`, `_violations`, `_lines`. These key on `ast.Attribute` with `.attr == "state"` in `Load` context and `FileState.<member>` occurrences — walking BOTH `Call.args` and `Call.keywords` (the Phase-83 blind-spot closure). Do NOT reimplement as a grep (Pitfall 1).

**Real-source guards** — one per target file, asserting `_violations(...) == []`:
```python
def test_proposal_py_has_zero_state_writes() -> None:
    violations = _violations(_PROPOSAL.read_text(encoding="utf-8"))
    assert violations == [], f"services/proposal.py reintroduced a FileRecord.state / FileState occurrence at lines {_lines(violations)}"
```
Repeat for `proposal_queries.py` and `agent_proposals.py`.

> **Caveat for the planner:** the template asserts `== []` (clean absence). Confirm each target file has ZERO surviving `.state` Store/Load and `FileState.<member>` occurrences after the deletions before asserting empty — the router keeps `FileRecord` and `current_path` (both fine: `.attr` is `current_path`/`agent_id`/`id`, never `state`). If a file legitimately retains a `FileState` reference, the guard needs an allow-list (the template discusses the dedup-scanner allow-list variant at `:8-15`); the research says clean absence holds for all three, so `== []` should be correct.

**Mutation + false-positive suites — copy and keep** (`:225-357`): the `test_guard_flags_*` (RED) cases for forms `f.state = X`, `.values(state=X)`, `getattr(f,"state")`, positional `.where(...)`, and the `test_guard_ignores_*` (GREEN) cases for `.status` reads, `.id` reads, and docstring mentions. This is what makes the guard mutation-verifiable (D-03 requirement).

---

### `tests/review/services/test_proposal_queries.py` (test-adapt, integration — `review` bucket)

**Analog:** self. Drop the `.file.state` / `.state` assertions; keep the `proposals.status` assertions.

- `test_update_proposal_status_approve_sets_file_state` (`:258-264`): delete `assert result.file.state == FileState.APPROVED` (`:264`) — either delete the whole test (premise gone) or narrow it to the status assertion. Drop the "transitions FileRecord.state (APR-02)" framing.
- `test_update_proposal_status_reject_sets_file_state` (`:267-273`): same — delete `assert result.file.state == FileState.REJECTED` (`:273`).
- `test_bulk_update_status_sets_file_state` (`:300-316`): delete the file-refresh + `for f in files: assert f.state == FileState.APPROVED` block (`:308-316`); keep the status coverage in `test_bulk_update_status` (`:287-297`).

The non-state tests (`test_update_proposal_status_approve` `:250-255`, `test_bulk_update_status` `:287-297`) STAY as the retained `proposals.status` coverage. Check `FileState` import usage after removals.

---

### `tests/review/routers/test_agent_proposals.py` (test-adapt, integration — `review` bucket)

**Analog:** self. The response-echo assertions STAY; the `f.state ==` DB assertions BREAK.

**`test_executed_joint_update`** (`:72-93`):
- KEEP `assert body["file_state"] == "moved"` (`:84`) and `assert body["current_path"] == "/new/proposed.mp3"` (`:85`) — response echo (D-02).
- DELETE `assert f.state == FileState.MOVED.value` (`:92`).
- KEEP `assert f.current_path == "/new/proposed.mp3"` (`:93`) — the real path survives (Pitfall 3).
- ADD a **positive** guard: seed with a known `file_state` (helper default `FileState.APPROVED`, `:42`) and assert `f.state == FileState.APPROVED.value` (UNCHANGED from seed) — proves the cascade write is gone, not merely absent.

**`test_failed_joint_update`** (`:96-114`):
- KEEP `assert body["file_state"] == "unchanged"` (`:108`).
- DELETE `assert f.state == FileState.UNCHANGED.value` (`:114`); add the "state unchanged from seed" positive guard.

**`test_same_state_idempotent_no_op`** (`:117-136`): status-code asserts (`:130-131`) and the `p.status == EXECUTED` check (`:135-136`) are unchanged. If any body assertion on `file_state` is added on the replay leg, use the new request-derived / `None` echo (discretion — recommend `None`). The replay call at `:126-129` sends no `file_state`, so `body["file_state"]` should be `None` post-cutover.

All other tests in this file (`:139-247`: 409/404/422/403/401 guards) are untouched — they assert status codes and error text, never `f.state`.

## No Analog Found

None. Every target is either an in-place deletion from an existing file or a new test with a direct in-tree template. No production file is created; no external pattern from RESEARCH.md is needed.

## Metadata

**Analog search scope:** `src/phaze/services/`, `src/phaze/routers/`, `tests/shared/`, `tests/review/`
**Files scanned:** 7 (3 source + 4 test), plus `stage_status.py` and `buckets.json` for signatures/placement
**Key patterns identified:**
- Independent-session assertion after `await session.commit()` (conftest override reads uncommitted rows)
- `is_applied(session, file_id)` reused as the apply-outcome predicate in place of any `file.state` read
- AST `ast.walk` source-scan guard (args + keywords) with permanently-encoded RED mutation + GREEN false-positive tests — never a line-grep
- Response-echo of `body.file_state` keeps the PATCH wire contract byte-identical while the state-mirror side effect is removed
**Pattern extraction date:** 2026-07-10
