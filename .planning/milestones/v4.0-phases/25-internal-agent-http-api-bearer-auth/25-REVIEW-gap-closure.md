---
phase: 25-internal-agent-http-api-bearer-auth
scope: gap-closure (CR-01 + CR-02 fixes only)
reviewed: 2026-05-11T00:00:00Z
depth: standard
files_reviewed: 4
files_reviewed_list:
  - src/phaze/routers/agent_metadata.py
  - src/phaze/routers/agent_execution.py
  - tests/test_routers/test_agent_metadata.py
  - tests/test_routers/test_agent_execution.py
findings:
  critical: 0
  warning: 2
  info: 4
  total: 6
status: issues_found
---

# Phase 25 Gap-Closure: Code Review Report (CR-01 + CR-02)

**Reviewed:** 2026-05-11
**Depth:** standard
**Files Reviewed:** 4
**Scope:** Phase 25 gap-closure plans 25-07 (CR-01 metadata partial-PUT) and 25-08 (CR-02 terminal-state same-status PATCH). The phase-wide review lives at `25-REVIEW.md` and is preserved unchanged.
**Status:** issues_found

## Summary

Both gap-closure fixes implement what the original review (CR-01 / CR-02) and the verification report (`25-VERIFICATION.md`) prescribed. The router code is small, targeted, and well-commented. mypy / ruff / pytest are all reported green in the SUMMARY files. The two fixes correctly restore Success Criterion #3 (idempotent replay) at the two endpoints that were failing.

Specifically:

- **CR-01 (`agent_metadata.py`):** `body.model_dump(exclude_unset=True)` correctly distinguishes "the client did not mention this field" from "the client set this field to None"; the resulting `dumped` dict drives both the INSERT payload and the `on_conflict_do_update` SET clause, so unset fields no longer get nulled. The empty-dump branch falls back to `on_conflict_do_nothing(index_elements=["file_id"])`, which is the correct Postgres-side fix for the empty-SET-clause case.

- **CR-02 (`agent_execution.py`):** the terminal-state guard now reads `if cur in _TERMINAL and new != cur:`. Same-status PATCH against a terminal row passes both guards (terminal carve-out + strict-`<` regress comparator), so the canonical idempotent retry case returns 200. Terminal→other-terminal (e.g. COMPLETED→FAILED) still 409s because `new != cur` holds. Both `_TERMINAL` and `_STATUS_ORDER` are unchanged; the regress guard is unchanged; the docstring is updated to match.

No correctness defects, no security regressions, no convention drift severe enough to block. Two warnings worth fixing in a follow-up pass and four info-level notes — primarily edge cases the new tests don't cover.

## Critical Issues

(none)

## Warnings

### WR-01: Empty-body PUT against a *brand-new* `file_id` silently creates an all-NULL metadata row (untested, behavior unspecified)

**File:** `src/phaze/routers/agent_metadata.py:55-68`
**Issue:** The new code-flow for the empty-dump branch is:

```python
payload = {**dumped, "file_id": file_id, "id": uuid.uuid4()}  # dumped is {}
stmt = pg_insert(FileMetadata).values([payload])
# ...
stmt = stmt.on_conflict_do_nothing(index_elements=["file_id"])
```

When a client PUTs `{}` against a `file_id` that has **no existing metadata row**, this still INSERTs a row containing `{file_id, fresh_uuid_id}` plus NULLs for every other column. The handler returns 200 and a fresh metadata row is created with zero real metadata. The docstring at line 36-37 acknowledges this in passing ("New rows still get an INSERT with whatever fields were set") but the test suite never exercises this path — `test_metadata_empty_put_is_noop_for_existing_row` seeds a row first.

Two issues with this:

1. An agent that misroutes an empty body to a never-seen-before `file_id` permanently materializes a "ghost" metadata row that subsequent SELECTs will find. Downstream code that uses "metadata row exists" as a proxy for "we extracted metadata for this file" will incorrectly believe it has metadata.
2. If the supplied `file_id` doesn't correspond to a real `FileRecord`, the INSERT will raise a `ForeignKeyViolationError` (HTTP 500), because `FileMetadata.file_id` has `ForeignKey("files.id")`. The handler does not pre-validate FK existence. This is a behavioral surprise vs. the natural read of "empty PUT is a no-op."

Both behaviors are technically consistent with the gap-closure docstring, but neither is tested.

**Fix:** Pick one of:

- (a) Constrain the new-row INSERT to skip the empty case entirely:

```python
if not dumped:
    # Empty body & no existing row -> nothing to write; existing row -> untouched.
    # Either way, no state change. Skip the round-trip entirely.
    existing = await session.execute(
        select(FileMetadata.id).where(FileMetadata.file_id == file_id).limit(1)
    )
    if existing.scalar_one_or_none() is None:
        # No-op for new rows too. Don't create a ghost.
        return MetadataWriteResponse(agent_id=agent.id, file_id=file_id)
    # else: existing row, nothing to update.
    return MetadataWriteResponse(agent_id=agent.id, file_id=file_id)
```

- (b) Add a regression test that *documents and locks* the current behavior (ghost row created) so a future refactor doesn't silently change it.

Option (a) is the safer semantic — "empty PUT is truly a no-op" is what most readers will assume from the docstring.

### WR-02: `test_same_status_patch_terminal_failed_allowed` is missing the symmetric DB-side row-state assertion that its COMPLETED twin has

**File:** `tests/test_routers/test_agent_execution.py:319-348`
**Issue:** `test_same_status_patch_terminal_allowed` (COMPLETED twin, lines 277-315) does this after the PATCH:

```python
session.expire_all()
result = await session.execute(select(ExecutionLog).where(ExecutionLog.id == log_id))
row = result.scalar_one()
assert row.status == ExecutionStatus.COMPLETED
```

`test_same_status_patch_terminal_failed_allowed` does NOT do the equivalent `row.status == ExecutionStatus.FAILED` check after its PATCH. It only asserts the response status code + body. The docstring explicitly claims "Symmetry with COMPLETED -> COMPLETED" but the coverage is asymmetric: a future bug that quietly mutates the row's status on same-status FAILED PATCH (e.g., a bad `setattr` regression on line 127 of the router) would slip past this test but be caught by its COMPLETED twin.

**Fix:** Add a parallel DB-side post-condition:

```python
session.expire_all()
result = await session.execute(select(ExecutionLog).where(ExecutionLog.id == log_id))
row = result.scalar_one()
assert row.status == ExecutionStatus.FAILED
```

## Info

### IN-01: `test_terminal_completed_to_failed_still_rejected` does not assert the seed POST succeeded

**File:** `tests/test_routers/test_agent_execution.py:350-381`
**Issue:** Sibling tests (`test_same_status_patch_terminal_allowed`, `test_same_status_patch_terminal_failed_allowed`) assert `assert r_post.status_code == 200, r_post.text` after the seed POST. The boundary-rejection test simply does:

```python
await ac.post(...)  # no return value captured, no assertion
response = await ac.patch(...)
```

If the seed POST silently failed (e.g., a future schema change), the PATCH would 404, not 409, and the test would fail with a misleading message ("expected 409, got 404"). This wastes triage time. Trivial fix:

```python
r_post = await ac.post(
    "/api/internal/agent/execution-log",
    json=_make_create_body(proposal_id, log_id=log_id, status="completed"),
)
assert r_post.status_code == 200, r_post.text
```

### IN-02: Explicit `null` in metadata PUT body is silently treated as "clear this field" — undocumented & untested

**File:** `src/phaze/routers/agent_metadata.py:51-64`, `src/phaze/schemas/agent_metadata.py:17-25`
**Issue:** `model_dump(exclude_unset=True)` distinguishes "field not present in payload" from "field explicitly set to null." A client PUTting `{"artist": null}` produces `dumped == {"artist": None}`, which goes into the SET clause as `excluded.artist = NULL`, clearing the prior value.

This is correct PATCH/partial-PUT semantics in most APIs, but:

1. The docstring at line 29-37 talks about "fields the client explicitly set" without flagging the null-vs-unset distinction. A reader could reasonably assume `null` is treated the same as unset.
2. No test in the cohort exercises the explicit-null path. If a future schema migration adds a `NOT NULL` constraint to a column, an explicit-null PUT would 500 with no regression test catching it.

**Fix:** Either (a) document the null semantics explicitly in the docstring:

```
Note: explicit `null` in the request body IS a valid mutation -- it clears
the prior value of that field. Use "omit the field" (`{}`-style partial PUT)
to leave a field untouched.
```

or (b) add a quick regression test:

```python
async def test_metadata_explicit_null_clears_field(...):
    # PUT {artist: "X", title: "Y"} then PUT {title: null}
    # Assert row.title is None AND row.artist == "X"
```

Either is acceptable for v1; the current state is functional but ambiguous.

### IN-03: New router docstring narrates the *historical* bug ("Previously the dump call was invoked without `exclude_unset=True`...") — clutter that doesn't help future readers

**File:** `src/phaze/routers/agent_metadata.py:46-49`
**Issue:**

```python
Gap closure: CR-01 (25-VERIFICATION.md). Previously the dump call was
invoked without `exclude_unset=True`, so every Optional field with
default `None` was written to the SET clause, NULLing prior column
values on partial replays. Verified end-to-end in 25-VERIFICATION.md.
```

This is useful gap-traceability for the current sprint, but six months from now the docstring will narrate a bug that no longer exists in the codebase. The inline comment on line 51 (`# CR-01 fix: only fields the client explicitly set participate in the UPDATE.`) already records the gap reference. Consider trimming the historical narrative from the docstring once the verification report is closed and leaving the grep-able `CR-01` reference in the inline comment only.

**Fix (deferred — bookkeeping):** at next docstring pass, replace the historical paragraph with a single line: `Gap-closure: CR-01 (see git log + 25-VERIFICATION.md for the original bug).`

### IN-04: `agent_metadata.py` payload always stamps a fresh `uuid.uuid4()` even on UPDATE/no-op paths — wasteful but harmless

**File:** `src/phaze/routers/agent_metadata.py:55`
**Issue:** `payload = {**dumped, "file_id": file_id, "id": uuid.uuid4()}` is constructed unconditionally. On the `on_conflict_do_update` branch the freshly-generated `id` is never used (it's not in the SET clause). On the `on_conflict_do_nothing` branch it's also discarded when the row already exists. Only the cold-INSERT path consumes it. Two consequences:

1. A UUID4 is generated on every request even when no INSERT will happen. Cheap, but visible in CPU profiles if this endpoint becomes hot.
2. The reader of the code has to mentally verify that the unused UUID can't leak into the row via some forgotten code-path. The current implementation is correct; this is purely a style/readability comment.

**Fix (deferred — micro-optimization):**

```python
payload: dict[str, object] = {**dumped, "file_id": file_id}
if dumped:
    stmt = pg_insert(FileMetadata).values([{**payload, "id": uuid.uuid4()}])
    stmt = stmt.on_conflict_do_update(...)
else:
    stmt = pg_insert(FileMetadata).values([{**payload, "id": uuid.uuid4()}])
    stmt = stmt.on_conflict_do_nothing(...)
```

The duplication is awkward; the current layout is fine. Not worth changing unless this hot-path becomes a profile target.

---

## Notes Out of Scope (already accepted by prior review)

The phase-wide `25-REVIEW.md` already records WR-01 through WR-08 plus IN-01 through IN-04 for the broader Phase 25 surface. None of those findings are re-addressed by the gap-closure fixes, and none are made worse by them. In particular:

- WR-01 (cross-agent write authority on `file_id` / `proposal_id`) — still open, accepted as T-25-04-T deferred to Phase 29. The CR-01 fix preserves the existing acceptance (any authenticated agent can still PUT against any `file_id`).
- WR-04 (test DB uses `Base.metadata.create_all`, never runs migrations) — still applies to the new tests; they rely on the same fixtures and would also miss any migration-vs-model drift.

These remain valid concerns but are out of scope for the gap-closure review.

---

## Correctness Trace — CR-01 Partial-PUT SQL Pattern

The Postgres `ON CONFLICT DO UPDATE SET ...` pattern with `exclude_unset=True` was traced end-to-end:

| Scenario | `dumped` | Branch taken | SET clause | Expected outcome |
|---|---|---|---|---|
| First PUT of `{artist, title, year, album}` to a new `file_id` | 4 keys | `on_conflict_do_update` | n/a (no conflict, INSERT path) | New row with 4 fields populated, 5 NULL. ✓ |
| Second PUT of `{artist}` to same `file_id` | 1 key | `on_conflict_do_update` | `set_={artist: excluded.artist}` | `artist` overwritten, other 8 columns untouched. ✓ |
| PUT `{}` against existing row | empty | `on_conflict_do_nothing` | n/a (conflict → do nothing) | Row untouched. ✓ |
| PUT `{}` against new `file_id` | empty | `on_conflict_do_nothing` | n/a (no conflict, INSERT path) | INSERT proceeds with `{file_id, id}` + NULLs. **WR-01 above.** |
| PUT `{artist: null}` against existing row | 1 key (value=None) | `on_conflict_do_update` | `set_={artist: excluded.artist}` | `artist` set to NULL (clears prior value). **IN-02 above.** |

The SET clause is constructed from the `dumped` keys only (`for k in dumped`), which honors field-level last-write-wins. `excluded.K` always reads from the proposed INSERT row, so the previously-set columns on the existing row are not touched unless `K` is in the SET. Correct.

The `index_elements=["file_id"]` correctly selects the `UNIQUE` constraint on `FileMetadata.file_id` (verified in `models/metadata.py:18`). Conflict-on-PK (the row's `id`) cannot occur because `id` is freshly generated per request.

## Correctness Trace — CR-02 Terminal Carve-Out Truth Table

The seven-row truth table from `25-08-PLAN.md` was traced through the fixed code path:

| `cur` (DB) | `new` (body) | `cur in _TERMINAL` | `new != cur` | `_STATUS_ORDER[new] < _STATUS_ORDER[cur]` | Final outcome |
|---|---|---|---|---|---|
| PENDING | PENDING | F | (skipped) | F | **200** ✓ |
| IN_PROGRESS | IN_PROGRESS | F | (skipped) | F | **200** ✓ |
| COMPLETED | COMPLETED | T | F | (skipped) | **200** ✓ (CR-02 fix) |
| FAILED | FAILED | T | F | (skipped) | **200** ✓ (CR-02 fix) |
| COMPLETED | IN_PROGRESS | T | T | (skipped) | **409 "is terminal"** ✓ |
| COMPLETED | FAILED | T | T | (skipped) | **409 "is terminal"** ✓ |
| FAILED | COMPLETED | T | T | (skipped) | **409 "is terminal"** ✓ |
| IN_PROGRESS | PENDING | F | (skipped) | T | **409 "would regress"** ✓ |
| PENDING | COMPLETED | F | (skipped) | F | **200** ✓ (jump forward — allowed by ladder) |

All nine rows match the documented contract. The carve-out is exactly one operator wide; it doesn't widen the regress check.

The `for field, value in body.model_dump(exclude_unset=True).items()` apply-mutations loop also uses `exclude_unset=True`, so a same-status PATCH that supplies only `status` does not stomp on an existing `error_message`. (E.g., FAILED row with `error_message="ENOENT"` retried with `{"status": "failed"}` keeps its error message.) Verified.

---

_Reviewed: 2026-05-11_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
_Scope: Phase 25 gap-closure (CR-01 + CR-02) — 4 files only_
