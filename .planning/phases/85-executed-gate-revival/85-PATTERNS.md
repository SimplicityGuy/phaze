# Phase 85: EXECUTED-Gate Revival - Pattern Map

**Mapped:** 2026-07-10
**Files analyzed:** 8 source files (1 new predicate pair + 5 cutover files + 1 template) + 2 test files
**Analogs found:** 8 / 8 (every new/modified file has an in-tree template)

This phase is a pure in-tree reader swap. There is **one genuinely new pair of functions**
(`applied_clause()` + `is_applied()`), and it has an exact sibling template already in the same
module (`dedup_resolved_clause()`). Everything else is an in-place predicate substitution at a known
line, so the "analog" for each cutover site is the *site's own current code* (shown as the excerpt to
transform) plus the shared predicate it must call.

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `services/stage_status.py` (add `applied_clause`/`is_applied`) | service (derived predicate) | transform (SQL fragment + scalar EXISTS) | `services/stage_status.py::dedup_resolved_clause` (same file, lines 91-112) | **exact** (file-level correlated `exists`, no `Stage` arg) |
| `services/review.py` (swap :109, :251 + bound) | service | CRUD read (list builders, degrade-safe) | `routers/tags.py::list_tags` (pagination idiom) + own current code | role-match / in-place |
| `routers/tags.py` (swap :44,:174,:179,:336,:422) | router | request-response + CRUD read | `services/stage_status.py` predicate consumers; `list_tags` already paginated | in-place |
| `routers/cue.py` (swap :48,:89,:251) | router | request-response + CRUD read | own current code + `applied_clause()`/`is_applied()` | in-place |
| `routers/tracklists.py` (swap :138,:600,:897) | router | request-response (per-record guard) | own current code + `is_applied()` | in-place |
| `services/tag_writer.py` (swap :185 guard) | service | file-I/O (write guard) | own current code + `is_applied()` | in-place |
| `templates/proposals/partials/proposal_row.html` (:46 badge) | template | request-response (SSR) | own current code — sibling badge branches `:48-53` already read `proposal.status` | **exact** |
| `tests/shared/.../test_applied_clause.py` (NEW) | test (unit) | transform | `tests/shared/test_stage_resolver.py` / `test_domain_completed_contract.py` (clause-shape unit tests) | role-match |
| `tests/review/services/test_tag_writer.py` (behavior-change, SC#2) | test (behavior) | file-I/O guard | `TestExecuteTagWrite` (same file, lines 183-243) — **must migrate off `MagicMock().state`** | in-place + reshape |

---

## Pattern Assignments

### `services/stage_status.py` — add `applied_clause()` + `is_applied()` (D-01, SC#1)

**Analog:** `services/stage_status.py::dedup_resolved_clause` (lines 91-112) — the exact file-level
correlated-`exists` template. It takes NO `stage` arg, correlates to `FileRecord` via `exists(...)`,
and is deliberately kept OUT of the `Stage` dispatch ladders (`done_clause`/`failed_clause`/
`stage_status_case`) so it does not perturb the DERIV-04 equivalence test
(`tests/integration/test_stage_status_equivalence.py`). Mirror all of this.

**Template body to copy** (lines 112):
```python
def dedup_resolved_clause() -> ColumnElement[bool]:
    return exists(select(DedupResolution.id).where(DedupResolution.file_id == FileRecord.id))
```

**Already-imported building blocks in this module** (lines 62, 70, 73 — nothing new to import):
```python
from sqlalchemy import ColumnElement, ..., exists, ..., select, ...
from phaze.models.file import FileRecord
from phaze.models.proposal import RenameProposal   # already imported (used by done_clause propose/review)
```
`ProposalStatus.EXECUTED.value == "executed"` (`models/proposal.py:20`). Use the string literal
`"executed"` to match how `shadow_compare` / `done_clause` spell status comparisons.

**New SQL-fragment form** (drop directly beneath `dedup_resolved_clause`, keep it a standalone
non-`Stage` function so it never touches the DERIV-04 ladder — this is an explicit anti-pattern per
RESEARCH §Anti-Patterns):
```python
def applied_clause() -> ColumnElement[bool]:
    """READ-05 / D-01: a file is 'applied' iff an executed proposal exists.

    File-level (no Stage arg); NEVER reads FileRecord.state; NEVER execution_log. Sibling of
    dedup_resolved_clause(); kept OUT of the Stage ladders so DERIV-04 is untouched.
    """
    return exists(
        select(RenameProposal.id).where(
            RenameProposal.file_id == FileRecord.id,
            RenameProposal.status == "executed",  # ProposalStatus.EXECUTED.value
        )
    )
```

**New per-record async helper** (for the write guards that hold a `file_id` + `session` but no
proposal — `proposal.file` is `lazy="raise"`, so never lazy-load). No exact per-record boolean
helper exists in `stage_status.py` today (the module is all `ColumnElement` builders), so this is the
one net-new *shape*; model it on the scalar-EXISTS idiom:
```python
async def is_applied(session: AsyncSession, file_id: uuid.UUID) -> bool:
    """READ-05 / D-01 per-record twin of applied_clause()."""
    stmt = select(
        exists(
            select(RenameProposal.id).where(
                RenameProposal.file_id == file_id,
                RenameProposal.status == "executed",
            )
        )
    )
    return bool(await session.scalar(stmt))
```
`AsyncSession` is already under `TYPE_CHECKING` (lines 79-80); add `import uuid` (or reuse an existing
uuid import — verify) for the `file_id` annotation.

**Relationship note (RESEARCH Pattern 2):** `proposals.file_id` is a plain FK; the
`uq_proposals_file_id_pending` partial-unique index enforces one PENDING proposal per file ONLY
(`models/proposal.py:53-59`). A file CAN have multiple non-pending proposals (FAILED then re-approved
EXECUTED). `exists(status=='executed')` is the correct authoritative test — if ANY proposal is
executed, the file is applied.

---

### `services/review.py` — swap :109 and :251, add bounding (D-03, sites #12/#13)

**Analog for the swap:** `applied_clause()` (above). **Analog for pagination:** `routers/tags.py::
list_tags` (lines 157-225, excerpted below).

**Current WHERE at `get_tagwrite_review_rows` (line 109) — the true unbounded site:**
```python
completed_subq = select(TagWriteLog.file_id).where(TagWriteLog.status == TagWriteStatus.COMPLETED)
stmt = (
    select(FileRecord)
    .options(selectinload(FileRecord.file_metadata))
    .where(FileRecord.state == FileState.EXECUTED, FileRecord.id.not_in(completed_subq))   # ← swap
    .order_by(FileRecord.original_filename)
)
```
Transform to: `.where(applied_clause(), FileRecord.id.not_in(completed_subq))` and append a bound.
**Preserve `completed_subq`** — that is the idempotency anti-join (D-02, Pitfall 5); do NOT replace it
with a state-based de-dupe.

**Current WHERE at `get_cue_review_cards` gated set (line 251):**
```python
gated_stmt = (
    select(Tracklist, FileRecord)
    .join(FileRecord, Tracklist.file_id == FileRecord.id)
    .where(
        Tracklist.status == "approved",
        Tracklist.file_id.is_not(None),
        FileRecord.state == FileState.EXECUTED,   # ← swap to applied_clause()
        Tracklist.id.not_in(has_timestamp_subq),
    )
    ...
)
```
Note this file also consumes the eligible set via `_get_eligible_tracklist_query` imported from
`routers.cue` (line 31) — that query's `state==EXECUTED` at `cue.py:48` is fixed in cue.py (site #6),
so review.py's eligible half is fixed transitively.

**Bounding pattern (D-03):** these builders currently take only `session`. Per RESEARCH recommendation
thread `page`/`page_size` through to match `list_tags`; a fixed `.limit(N)` cap is the acceptable
fallback (degrade-safe render helpers). Keep the `session.begin_nested()` SAVEPOINT + `return []`
degrade wrapper intact (lines 103-104 / 134-136) — do NOT add a router try/except.

**Import cleanup:** after the swap, verify whether `FileState` (imported line 28) is still used in
`review.py`; remove if dead (ruff F401).

---

### `routers/tags.py` — swap :44, :174, :179, :336, :422

**Analog:** `applied_clause()` for the WHERE/COUNT readers; `is_applied()` for the per-record guard.

**Site #1 — COUNT stat card (line 44):**
```python
executed_stmt = select(func.count(FileRecord.id)).where(FileRecord.state == FileState.EXECUTED)
# → .where(applied_clause())
```

**Sites #2/#3 — `list_tags` LIST + COUNT (lines 174, 179):** already fully paginated (Query params
lines 160-161, offset/limit line 185, `Pagination` line 213). **Swap the WHERE only; do NOT
re-paginate** (RESEARCH: pagination guard already satisfied here). This is the canonical pagination
idiom the review.py builders should copy:
```python
page: int = Query(1, ge=1),
page_size: int = Query(20, ge=10, le=100),
...
offset = (page - 1) * page_size
stmt = stmt.offset(offset).limit(page_size)
...
pagination = Pagination(page=page, page_size=page_size, total=total)   # from services.proposal_queries
```

**Site #4 — per-record write guard `write_file_tags` (line 336):**
```python
if file_record.state != FileState.EXECUTED:
    return HTMLResponse(content="Only executed files can have tags written", status_code=400)
# → if not await is_applied(session, file_id):
```

**Site #5 — `bulk_write_no_discrepancies` WHERE (line 422) — unbounded operator-triggered loop:**
```python
completed_subq = select(TagWriteLog.file_id).where(TagWriteLog.status == TagWriteStatus.COMPLETED)
stmt = (
    select(FileRecord).options(selectinload(FileRecord.file_metadata))
    .where(FileRecord.state == FileState.EXECUTED, FileRecord.id.not_in(completed_subq))   # ← swap + LIMIT
    .order_by(FileRecord.original_filename)
)
```
Swap WHERE to `applied_clause()`, preserve `completed_subq`, add a LIMIT/batch cap (D-03).

**Commit discipline (memory: get_session NEVER commits — VERIFIED already correct):**
`write_file_tags` commits at line 369; `bulk_write_no_discrepancies` at line 438. The swap is
read-only at the guard; the downstream commit is unchanged. Re-verify after edits (memory landmine),
add no new commit.

**Import cleanup:** `FileState` imported at line 16 — likely dead after all 5 swaps; remove if F401.

---

### `routers/cue.py` — swap :48, :89, :251

**Analog:** `applied_clause()` (WHERE readers #6/#7); `is_applied()` (per-record guard #8).

**Sites #6/#7 — eligible + missing-timestamp sets (lines 48, 89):** identical shape, both inside a
`.where(Tracklist.status == "approved", Tracklist.file_id.is_not(None), FileRecord.state ==
FileState.EXECUTED, ...)`. Swap the `FileRecord.state == FileState.EXECUTED` line to `applied_clause()`.

**Site #8 — per-record guard `generate_cue` (line 251):**
```python
if file_record is None or file_record.state != FileState.EXECUTED:
    toast_msg = "File must be executed before generating a CUE sheet. ..."
    return _render_error_toast(request, toast_msg)
# → if file_record is None or not await is_applied(session, file_record.id):
```
`generate_cue` writes a `.cue` to **disk only, no DB mutation** — VERIFIED no `session.commit()`
needed (RESEARCH commit table). Do not add one.

**Import cleanup:** verify `FileState` (imported cue.py) becomes dead; remove if F401.

---

### `routers/tracklists.py` — swap :138, :600, :897 (per-record cue-version guards)

**Analog:** `is_applied()`. All three are the identical `fr and fr.state == FileState.EXECUTED`
per-record shape after a `select(FileRecord).where(FileRecord.id == tl.file_id)` load:
```python
fr_result = await session.execute(select(FileRecord).where(FileRecord.id == tl.file_id))
fr = fr_result.scalar_one_or_none()
if fr and fr.state == FileState.EXECUTED:            # sites :138 / :600 / :897
    ... = _get_cue_version(fr.current_path)
```
Two options (Planner's discretion): (a) replace the bare-`fr.state` check with
`await is_applied(session, tl.file_id)` (drops the need to load `fr` for the guard, but `fr.current_path`
is still needed for `_get_cue_version`, so keep the load); or (b) since `tl.file_id` is already in
hand, `if fr and await is_applied(session, fr.id):`. Prefer (b) to reuse the loaded `current_path`.
Sites :138 and :897 are inside list loops (N per-record EXISTS queries — negligible, bounded lists).

**Import cleanup:** `tracklists.py` uses other `FileState`/model members elsewhere — verify before
removing the `FileState` import (may still be needed).

---

### `templates/proposals/partials/proposal_row.html` — badge (:46, D-04, site #15)

**Analog:** the sibling badge branches in the SAME `{% if/elif %}` chain (lines 48-53) already read
`proposal.status` directly. The `executed` branch is the only one reaching into the dead, `lazy="raise"`
`proposal.file.state`:
```jinja
{% if proposal.file.state == "executed" %}      {# line 46 — DEAD (always False; lazy="raise" trap) #}
    <span ...>Executed</span>
{% elif proposal.status == "approved" %}         {# lines 48+ — the correct in-scope idiom #}
```
Fix to `{% if proposal.status == "executed" %}` — trivial, no helper, no context change needed
(`proposal.status` is already in template scope). This deliberately removes the last stray
`proposal.file.state` reader so Phase 90 (drop `files.state`) does not trip over it (Pitfall 4). Badge
label wording "Executed"/"Applied" is cosmetic (Claude's discretion).

---

### `tests/shared/.../test_applied_clause.py` (NEW unit test, SC#1)

**Analog:** existing clause-contract unit tests under `tests/shared/` —
`test_stage_resolver.py`, `test_domain_completed_contract.py`, and the dedup source-scan test
`test_dedup_fingerprint_source_scan.py` (which asserts `~dedup_resolved_clause()` reader discipline).
Place `applied_clause`/`is_applied` tests in the **`shared`** bucket (stage_status is shared).

**Cases (RESEARCH Phase-Requirements→Test map):** executed→True; failed/approved/pending→False;
multi-proposal (failed + executed for same file)→True; and an assertion that the predicate **never
reads `FileRecord.state`** (a file with `state != 'executed'` but an executed proposal is still
applied — this is the whole point). Run: `just test-bucket shared` in isolation (bucket-isolation
memory).

---

### `tests/review/services/test_tag_writer.py::TestExecuteTagWrite` — behavior-change test (SC#2)

**Analog:** the existing `TestExecuteTagWrite` class (lines 183-243) — SAME file. **Critical reshape:**
the current guard test drives a `MagicMock` whose `.state` is set directly:
```python
def _make_file_record(self, state: str = FileState.EXECUTED, ...) -> MagicMock:
    fr = MagicMock(); fr.state = state; ...            # lines 186-192
...
async def test_rejects_non_executed_file(self) -> None:
    fr = self._make_file_record(state=FileState.APPROVED)
    session = AsyncMock()
    with pytest.raises(ValueError, match="executed"):
        await execute_tag_write(session, fr, {"artist": "Test"}, "tracklist")   # lines 194-200
```
After the swap, `execute_tag_write` gates on `await is_applied(session, file_record.id)` — a real DB
EXISTS query — NOT `file_record.state`. The `AsyncMock` session will no longer produce a truthful
boolean, so the mock-`state` fixtures no longer exercise the guard. Migrate the guard cases to:
- **seed a real file + `proposals.status='executed'`** (applied) → guard ADMITS → write proceeds; and
- a file with `proposals.status='failed'`/`'approved'` (no executed proposal) → guard RAISES
  `ValueError`.

**Mutation-check (memory `feedback_mutation_test_guard_tests`, Wave-0 gap):** with `applied()` reverted
to `state==EXECUTED`, the applied-proposal fixture (whose `file.state` is NOT `'executed'`) must FAIL
the guard (RED); restore → GREEN. A green guard proves nothing until you watch it go red.

**Commit-blindness caveat (memory `project_get_session_never_commits` / Pitfall 6):** `conftest.py`
overrides `get_session` with the test's own session. For any persistence assertion, read from an
**independent** session. (No new commit is introduced here, but the behavior test must assert the
write path actually runs against the applied fixture.)

**Also UPDATE these existing EXECUTED-seeded tests** (they seed `state='executed'`, which no prod
writer produces — migrate to seed `proposals.status='executed'`, optionally `file.state='moved'`):
`tests/review/routers/test_tags.py`, `tests/review/routers/test_cue.py`,
`tests/identify/routers/test_tracklists.py`, `tests/integration/test_review_audit.py`
(RESEARCH Wave-0 Gaps, `[VERIFIED: grep]`).

---

## Shared Patterns

### The `applied()` predicate pair (single-source — DERIV-01 discipline)
**Source:** NEW in `services/stage_status.py`, templated on `dedup_resolved_clause()` (line 112).
**Apply to:** every one of the 15 cutover sites. WHERE/COUNT readers consume `applied_clause()`
(`.where(applied_clause(), ...)`); per-record write guards consume `await is_applied(session, file_id)`.
Never inline a fresh subquery per call site (Don't-Hand-Roll table).
```python
def applied_clause() -> ColumnElement[bool]:
    return exists(select(RenameProposal.id).where(
        RenameProposal.file_id == FileRecord.id, RenameProposal.status == "executed"))

async def is_applied(session: AsyncSession, file_id: uuid.UUID) -> bool:
    return bool(await session.scalar(select(exists(select(RenameProposal.id).where(
        RenameProposal.file_id == file_id, RenameProposal.status == "executed")))))
```

### Idempotency anti-join (D-02 — PRESERVE, do not replace)
**Source:** `services/review.py:105`, `routers/tags.py:418` — `completed_subq`.
**Apply to:** the tag-write list builders/loops. Keep `FileRecord.id.not_in(completed_subq)` alongside
`applied_clause()`; do NOT re-introduce a state-based de-dupe (Pitfall 5).
```python
completed_subq = select(TagWriteLog.file_id).where(TagWriteLog.status == TagWriteStatus.COMPLETED)
.where(applied_clause(), FileRecord.id.not_in(completed_subq))
```

### Pagination (D-03)
**Source:** `services/proposal_queries.py::Pagination` (lines 23-58) + `routers/tags.py::list_tags`
(lines 157-225, the reference implementation).
**Apply to:** `services/review.py` `get_tagwrite_review_rows` / `get_cue_review_cards` (thread
`page`/`page_size`, or a fixed `.limit(N)` cap), and a LIMIT on `tags.py::bulk_write_no_discrepancies`.
`list_tags` is already paginated — leave it. Reuse the existing `Query(page_size, ge=10, le=100)`
bounds (V5 input-validation control, DoS guard).

### Degrade-safe read wrapper (PRESERVE)
**Source:** `services/review.py` `session.begin_nested()` SAVEPOINT + `return []` on exception (lines
60-76, 103-136, 219-269).
**Apply to:** keep intact around the review.py builders after adding pagination — no router try/except.

### Commit discipline (memory landmine — VERIFY, add nothing)
**Source:** `routers/tags.py:369/438/475` all `await session.commit()`; `routers/cue.py::generate_cue`
writes disk-only (no DB mutation → no commit). All mutating routers already commit correctly. Re-verify
after edits; introduce no new commit (the swap is read-only at every reader/guard).

---

## No Analog Found

None. Every new/modified file maps to an in-tree template — the predicate pair to
`dedup_resolved_clause()`, the pagination to `list_tags`, the badge to its own sibling branches, the
behavior test to the existing `TestExecuteTagWrite`. The single net-new *shape* is the per-record
async `is_applied()` boolean helper (the module is otherwise all `ColumnElement` builders); its body
is the standard scalar-`exists` idiom used throughout SQLAlchemy readers in this codebase.

---

## Metadata

**Analog search scope:** `src/phaze/services/`, `src/phaze/routers/`, `src/phaze/templates/proposals/`,
`src/phaze/models/`, `tests/review/`, `tests/shared/`.
**Files scanned:** stage_status.py, proposal_queries.py, review.py, tag_writer.py, tags.py, cue.py,
tracklists.py, proposal_row.html, test_tag_writer.py, tests/buckets.json (+ directory listings for
`tests/review/{services,routers}`, `tests/shared`).
**Pattern extraction date:** 2026-07-10
```
