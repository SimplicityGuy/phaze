---
phase: 85-executed-gate-revival
reviewed: 2026-07-10T00:00:00Z
depth: standard
files_reviewed: 16
files_reviewed_list:
  - src/phaze/services/stage_status.py
  - src/phaze/routers/tags.py
  - src/phaze/services/tag_writer.py
  - src/phaze/routers/cue.py
  - src/phaze/routers/tracklists.py
  - src/phaze/services/review.py
  - src/phaze/templates/proposals/partials/proposal_row.html
  - tests/shared/test_applied_clause.py
  - tests/review/services/test_tag_writer.py
  - tests/review/routers/test_tags.py
  - tests/review/routers/test_cue.py
  - tests/identify/routers/test_tracklists.py
  - tests/review/services/test_review_degrade.py
  - tests/integration/test_review_audit.py
  - tests/review/routers/test_proposals.py
findings:
  critical: 0
  warning: 4
  info: 2
  total: 6
status: issues_found
---

# Phase 85: Code Review Report

**Reviewed:** 2026-07-10
**Depth:** standard
**Files Reviewed:** 16
**Status:** issues_found

## Summary

Phase 85 revives READ-05's dead `state == EXECUTED` gates by routing every gate through a single-source
`applied()` predicate pair (`applied_clause()` SQLAlchemy clause + `is_applied()` row helper) in
`services/stage_status.py`, gating on `exists(proposals WHERE file_id==FileRecord.id AND
status=='executed')`.

The core migration is **correct and well-guarded**:

- `applied_clause()` / `is_applied()` express the correlated `exists` subquery correctly, compose into
  `.where(...)` at every call site (each enclosing query carries `FileRecord`), and correctly never read
  `files.state` or touch `execution_log`. The multi-proposal case (a file with both a `failed` and an
  `executed` proposal is applied) is right.
- The six revived guards all invert correctly (`not is_applied` rejects non-applied files).
- The `completed_subq` idempotency anti-join is preserved in both `bulk_write_no_discrepancies` and
  `get_tagwrite_review_rows`, and the D-01 admit / D-02 exclude cases are mutation-checked in tests.
- The degrade wrappers (`begin_nested()` + `except -> []` + named warning) are intact and behavior-tested.
- The `proposal_row.html` badge fix (`proposal.file.state` -> `proposal.status`) is correct and also
  removes a latent `lazy="raise"` lazy-load risk on `proposal.file`.

The findings below are all correctness/robustness degradations of the D-03 bounding logic and one latent
counting bug that the applied() revival newly exposes. None are blockers, but WR-01 undermines the
contract of the two bulk operator tools at the 200K scale this phase explicitly targets.

## Structural Findings (fallow)

No `<structural_findings>` block was provided with this review; no structural pre-pass to normalize.

## Narrative Findings (AI reviewer)

## Warnings

### WR-01: `.limit()` is applied BEFORE the Python qualification filter — qualifying rows are silently truncated / permanently starved

**File:** `src/phaze/services/review.py:117-134` and `src/phaze/routers/tags.py:421-441`

**Issue:** Both operator builders cap the candidate set with a SQL `.limit(_MAX_REVIEW_ROWS)` /
`.limit(_MAX_BULK_TAG_WRITE)` on a query ordered by `FileRecord.original_filename`, and *only then*
filter in Python for "something to write":

```python
# review.py get_tagwrite_review_rows
.where(applied_clause(), FileRecord.id.not_in(completed_subq))
.order_by(FileRecord.original_filename)
.limit(_MAX_REVIEW_ROWS)          # <-- SQL cap first
...
for fr in file_records:
    ...
    if changed_count < 1:          # <-- qualification filter applied AFTER the cap
        continue
```

The query cannot express "has >= 1 change" (that is computed in Python from `compute_proposed_tags`), so
zero-change applied files fully consume the capped window. Critically, a zero-change applied file **never
qualifies, so it never receives a COMPLETED `TagWriteLog`, so `completed_subq` never excludes it** — it
occupies the same alphabetical slot on every submit. Consequences at the 200K applied backlog this phase
targets:

- **`get_tagwrite_review_rows`:** if >`_MAX_REVIEW_ROWS` zero-change applied files sort alphabetically
  before a qualifying file, the Tag-write workspace renders *empty or undercounted* even though qualifying
  files exist — a silent false-empty, not the "empty queue is CORRECT" case the docstring claims.
- **`bulk_write_no_discrepancies`:** re-submitting does **not** make progress. Because the ordering is
  deterministic and non-qualifying occupants never leave the candidate set, the operator can click the
  bulk button repeatedly and never reach a qualifying file trapped behind a wall of >2000 zero-change
  files. The tool's contract ("write every qualifying applied file, re-submit for the next batch") is
  unsatisfiable for those files via the bulk path.

**Fix:** Exclude non-writable files at the SQL level so the cap counts only real candidates, or cap after
qualification. The cleanest is to page through candidates (keyset on `original_filename`) accumulating
*qualifying* rows until `_MAX_*` is reached, rather than SQL-limiting raw candidates then dropping most of
them. Minimally, order by a column that changes when a file is processed (or track "inspected, no change"
so those files leave the window) so repeated submits make forward progress:

```python
# Accumulate qualifying rows up to the cap instead of capping raw candidates:
qualifying: list[...] = []
last_name = ""
while len(qualifying) < _MAX_REVIEW_ROWS:
    batch = await session.execute(
        select(FileRecord).options(selectinload(FileRecord.file_metadata))
        .where(applied_clause(), FileRecord.id.not_in(completed_subq),
               FileRecord.original_filename > last_name)
        .order_by(FileRecord.original_filename).limit(500)
    )
    rows = list(batch.scalars().all())
    if not rows:
        break
    last_name = rows[-1].original_filename
    for fr in rows:
        # ... compute comparison; append only if it qualifies ...
```

### WR-02: `_get_tag_stats` double-subtracts files carrying both a COMPLETED and a DISCREPANCY log

**File:** `src/phaze/routers/tags.py:47-66`

**Issue:** `pending = total_executed - completed - discrepancies`, where `completed` and `discrepancies`
are *independent* `COUNT(DISTINCT file_id)` tallies over `TagWriteLog`. A single file that has both a
`DISCREPANCY` write and a later `COMPLETED` write (a normal re-write sequence) is counted in **both**
subtrahends, so it is subtracted twice from `total_executed`, under-reporting `pending`. This was latent
pre-Phase-85 (the old `state == FileState.EXECUTED` count was always 0, so `pending` floored to 0
regardless), but the applied() revival makes `total_executed` real and thus surfaces the miscount. The
`max(pending, 0)` guard hides negatives but not the general under-count.

**Fix:** Compute the "already handled" set once so each file is counted at most once:

```python
handled_stmt = select(func.count(func.distinct(TagWriteLog.file_id))).where(
    TagWriteLog.status.in_((TagWriteStatus.COMPLETED, TagWriteStatus.DISCREPANCY))
)
handled = (await session.execute(handled_stmt)).scalar() or 0
pending = max(total_executed - handled, 0)
```

(Keep the separate `completed` / `discrepancies` tallies only for their own display cells.)

### WR-03: `get_cue_review_cards` eligible half is not SQL-bounded — the D-03 memory bound is only partial

**File:** `src/phaze/services/review.py:214-252` (via `src/phaze/routers/cue.py:32-61`
`_get_eligible_tracklist_query`)

**Issue:** The gated half of `get_cue_review_cards` carries a real `.limit(_MAX_REVIEW_ROWS)`
(review.py:271), but the eligible half is bounded only by a Python `if len(cards) >= _MAX_REVIEW_ROWS:
break` (review.py:238). The break sits on top of `_get_eligible_tracklist_query`, which does
`return list(result.tuples().all())` — it materializes **every** approved + applied + timestamped
`(Tracklist, FileRecord)` pair into memory first, then the loop caps card *building*. So the loop-break
saves the per-row `_build_cue_tracks` DB work but does not bound the initial result-set load. At the 200K
scale the `_MAX_REVIEW_ROWS` cap is justified by, the unbounded eligible query is the exact blow-up the
bound was meant to prevent. (The same unbounded query also backs `cue.list_cue` / `generate_batch`, which
paginate in Python — pre-existing, but the review.py consumer is new.)

**Fix:** Push a real `.limit(_MAX_REVIEW_ROWS)` into the eligible query for the review-card path (either a
`limit=` parameter on `_get_eligible_tracklist_query`, or a locally-bounded copy of the statement) so the
DB never returns more than the cap:

```python
eligible = await _get_eligible_tracklist_query(session, limit=_MAX_REVIEW_ROWS)
```

### WR-04: `get_cue_review_cards` total card count can reach 2×`_MAX_REVIEW_ROWS`

**File:** `src/phaze/services/review.py:238,271`

**Issue:** The eligible loop caps at `_MAX_REVIEW_ROWS` cards and the gated query independently
`.limit(_MAX_REVIEW_ROWS)`s, so the returned list can hold up to `2 * _MAX_REVIEW_ROWS` (4000) cards. The
docstring says "each set bounded by `_MAX_REVIEW_ROWS`", so this is intentional per-set, but the module
constant reads as a single render budget and a downstream consumer expecting `_MAX_REVIEW_ROWS` total rows
gets double. Combined with WR-03 this is the practical render-size ceiling.

**Fix:** If the intent is a single render budget, share the cap across both halves (decrement the
remaining budget after the eligible loop and pass it to the gated `.limit`); otherwise rename/annotate the
constant to make the per-set semantics explicit at the call sites.

## Info

### IN-01: Redundant `is_applied()` query on the single-file write path

**File:** `src/phaze/routers/tags.py:337` and `src/phaze/services/tag_writer.py:185`

**Issue:** `write_file_tags` guards with `if not await is_applied(session, file_id)` (returning a friendly
400), and then `execute_tag_write` immediately re-runs `is_applied(session, file_record.id)` for the same
file. The inner check is correct defense-in-depth, but it means two identical `EXISTS` round-trips per
single write. Harmless for a single write; benign duplication worth noting. No change required — the inner
guard is the load-bearing one (it also protects the `undo` and bulk paths); the outer one exists only for
UX. Consider dropping the outer guard and mapping the inner `ValueError` to the 400 in `write_file_tags`
if the duplication bothers you.

### IN-02: `bulk_write_no_discrepancies` cap budget is consumed by non-qualifying files

**File:** `src/phaze/routers/tags.py:429-441`

**Issue:** Sub-point of WR-01, called out separately because the reported `written` count and the
"Nothing matched" toast can both be misleading: with a page of mostly non-qualifying files inside the
`_MAX_BULK_TAG_WRITE` window, `written` can be near-zero while a large qualifying backlog remains just
past the cap. The toast tells the operator "no executed files qualify … right now," which is false. If
WR-01 is fixed by accumulating qualifying rows, this message becomes accurate for free; otherwise consider
distinguishing "cap reached, more may qualify" from "genuinely nothing qualifies."

---

_Reviewed: 2026-07-10_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
