---
phase: 87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri
reviewed: 2026-07-11T00:00:00Z
depth: standard
files_reviewed: 20
files_reviewed_list:
  - src/phaze/enums/stage.py
  - src/phaze/models/__init__.py
  - src/phaze/models/stage_skip.py
  - src/phaze/routers/pipeline.py
  - src/phaze/services/pipeline.py
  - src/phaze/services/stage_status.py
  - src/phaze/tasks/reenqueue.py
  - src/phaze/templates/base.html
  - src/phaze/templates/pipeline/partials/_eligibility_trace.html
  - src/phaze/templates/pipeline/partials/_force_skip_dialog.html
  - src/phaze/templates/pipeline/partials/_stage_matrix.html
  - src/phaze/templates/pipeline/partials/_stage_pill.html
  - src/phaze/templates/pipeline/partials/_status_filter_bar.html
  - src/phaze/templates/pipeline/partials/analyze_workspace.html
  - src/phaze/templates/pipeline/partials/files_table_view.html
  - src/phaze/templates/pipeline/partials/metadata_workspace.html
  - src/phaze/templates/record/record_body.html
  - src/phaze/templates/shell/partials/rail.html
  - assets/src/app.css
findings:
  critical: 1
  warning: 2
  info: 2
  total: 5
status: issues_found
---

# Phase 87: Code Review Report

**Reviewed:** 2026-07-11
**Depth:** standard
**Files Reviewed:** 20
**Status:** issues_found

## Summary

Reviewed the Phase 87 operator-UI surface: the `stage_skip` sidecar model, the `skipped` marker
threaded through the single-source stage-status derivation (`enums/stage.py` ⇄
`services/stage_status.py` twins), the recovery consumer (`tasks/reenqueue.py`), and the new router
endpoints + Jinja/HTMX/Alpine templates (stage-matrix pills, status filter, eligibility trace,
per-file/bulk retry, force-skip writer, DAG-rail orphan badge + priority/pause controls).

The predicate-twin work is careful and correct: `skipped_clause` is enrich-only guarded, the
precedence ladder `in_flight ≻ done ≻ skipped ≻ failed ≻ not_started` is mirrored identically in
both twins (`stage_status_case`, `eligible_clause`, `domain_completed_clause` vs their Python
counterparts), and the recovery `fingerprint_done` correctly excludes force-skipped rows without
coupling to the terminality axis. The force-skip writer follows most of its stated discipline
(enrich-only allowlist via `STAGE_TO_FUNCTION`, `sanitize_pg_text`, explicit `commit`, reason not
echoed → no XSS), and the priority/pause rail rewire targets the real `pipeline_stages.py` endpoints
with a matching `hx-vals`→`Form()` contract.

The one blocker is an **unhandled `IntegrityError` (HTTP 500) on the flagship mutating endpoint**:
the `stage_skip` UNIQUE(file_id, stage) constraint guarantees that a second force-skip of the same
(file, stage) — a reliably reachable operator action, since the "Force complete / skip" button is
rendered unconditionally per enrich stage and is not hidden after a successful skip — aborts the
commit with no conflict handling. Two secondary issues: the required-reason invariant is bypassable
by ordering (blank-check before sanitize), and doc/comment drift now that the "four-bucket" enrich
counts are five.

## Critical Issues

### CR-01: Force-skip writer 500s on a duplicate (or bad file_id) — unhandled IntegrityError

**File:** `src/phaze/routers/pipeline.py:1325-1326`
**Issue:** `force_skip_stage` does a bare `session.add(StageSkip(...))` + `await session.commit()`
with no conflict handling. The `stage_skip` table declares `UniqueConstraint("file_id", "stage",
name="uq_stage_skip_file_stage")` (`models/stage_skip.py:54`) and a FK to `files.id`
(`models/stage_skip.py:46`). Therefore:
- **Duplicate skip → 500.** Re-force-skipping an already-skipped (file, stage) raises a unique-
  violation `IntegrityError` on commit, which propagates as an unhandled 500. This is a normal,
  reachable path: `_force_skip_dialog.html` is included **unconditionally** for every enrich stage
  in `record_body.html:74-79` — it is *not* gated on the current bucket and is *not* hidden after a
  successful skip — so the "Force complete / skip" button stays clickable. After one skip (the pill
  only flips on the next 5s poll), a second confirm on the same still-open/re-opened record submits
  again and 500s.
- **Stale / unknown file_id → 500.** A `file_id` with no `files` row raises a FK-violation
  `IntegrityError` on the same commit.

The endpoint is described as "the correctness-sensitive mutating endpoint of this phase," yet it has
no idempotency. Confirmed no `on_conflict`, no `IntegrityError` catch, and no pre-insert existence
check anywhere around the `StageSkip` insert.

**Fix:** Make the write idempotent (mirror the `insert_ledger_if_absent` ON CONFLICT DO NOTHING
precedent) or catch the violation and return the success/validation fragment instead of 500-ing:
```python
from sqlalchemy.dialects.postgresql import insert as pg_insert

stmt = (
    pg_insert(StageSkip)
    .values(file_id=file_id, stage=stage, reason=clean_reason)
    .on_conflict_do_nothing(constraint="uq_stage_skip_file_stage")
)
await session.execute(stmt)
await session.commit()
```
(FK violations for an unknown `file_id` should be handled explicitly — e.g. verify the file exists
first, or catch `IntegrityError` and return a 404/validation fragment — rather than surfacing a 500.)

## Warnings

### WR-01: "Reason required" (D-09) is bypassable — blank-check runs before sanitize

**File:** `src/phaze/routers/pipeline.py:1319-1325`
**Issue:** The required-reason gate is `if not reason.strip():` on the **raw** input, and only then is
`clean_reason = sanitize_pg_text(reason)` computed and persisted. `str.strip()` removes whitespace
but **not** NUL / control chars / lone surrogates, while `sanitize_pg_text` strips exactly those. A
reason consisting solely of stripped-away characters (e.g. `"\x00"`) passes the non-blank check, then
sanitizes to `""`, and is stored as an empty-string reason. The `reason` column is `nullable=False`
(`models/stage_skip.py:50`), but an empty string satisfies NOT NULL — so the D-09 "a reason is
required" invariant is defeated. This is the same sanitize-ordering class flagged in project memory
(sanitize before enforcing constraints on the free text). Practical risk is low for a single-user
admin tool (a browser textarea cannot type NUL; it needs a crafted request), but it is a real
ordering defect on the endpoint whose whole point is a deliberate, justified operator action.

**Fix:** Validate emptiness on the sanitized value:
```python
clean_reason = sanitize_pg_text(reason)
if not clean_reason.strip():
    return HTMLResponse('<p ... role="alert">A reason is required.</p>', status_code=422)
```

### WR-02: Hot 5s poll now runs the full recovery-orphan derivation over the entire ledger

**File:** `src/phaze/services/pipeline.py:589-645` (`get_stage_orphan_counts`), called from
`src/phaze/routers/pipeline.py:247` (`_build_dag_context`) on every `/pipeline/stats` tick.
**Issue:** For definitional parity with recovery, `get_stage_orphan_counts` reuses recovery's own
machinery on the hot poll path: `get_ledger_rows(session)` (the **entire** `scheduling_ledger` — a
table that reached ~44.5K rows in the 2026-06-18 incident), `get_live_job_keys`, `_build_done_sets`
(several ledger-scoped probes), and `_in_flight_cloud_job_ids`, all every 5 seconds per connected
dashboard. It is SAVEPOINT-degrade-safe (returns all-zeros on error), so it can't 500 the poll, but a
slow derivation on a large ledger will still *block* the hot poll rather than degrade. This borders
on availability rather than pure performance; flagging so the ledger-size cost on the 5s path is a
conscious, tested decision (e.g. a cheap cache/TTL or a periodic recompute) rather than an accident.
(Pure algorithmic performance is out of v1 scope; noted here only for the availability edge.)

**Fix:** Consider decoupling the orphan-count derivation from the per-tick poll (compute on a slower
cadence / cache the last value in `app.state` or Redis), or bound `get_ledger_rows` for this read.

## Info

### IN-01: "Four-bucket" docs/comments are now stale — the enrich shape is five buckets

**File:** `src/phaze/services/pipeline.py:324-360` (`_safe_bucket_counts`), `435-442`
(`get_stage_progress`)
**Issue:** With `Status.SKIPPED` added, `_safe_bucket_counts` initializes `out = {s.value: 0 for s in
Status}` (five keys) and `stage_status_case` emits a `skipped` branch for enrich stages, so the enrich
nodes now carry `{not_started, in_flight, done, skipped, failed}` (+ `total`). The docstrings and
inline comments still say "four-bucket" / "four counts SUM to `music_video_total`" (e.g.
`services/pipeline.py:324`, `330`, `337-339`, `435-439`). The code is correct (the sum-to-total
invariant still holds across five buckets, and `done` correctly excludes skipped); only the prose is
out of date and could mislead the next maintainer.

**Fix:** Update the "four-bucket"/"four counts" wording to five throughout `_safe_bucket_counts` and
`get_stage_progress`.

### IN-02: Status filter offers `skipped` for downstream stages, which can never match

**File:** `src/phaze/templates/pipeline/partials/_status_filter_bar.html:30-36` +
`src/phaze/routers/pipeline.py:799` / `services/pipeline.py:1797-1798`
**Issue:** The filter bar lists `skipped` as a bucket option for all six stage columns, but
`stage_status_case` only emits a `skipped` branch for the three enrich stages
(`stage_status.py:417-418`). Selecting e.g. "Prop = skipped" / "Appr = skipped" / "Exec = skipped"
runs `stage_status_case(<downstream>) == 'skipped'`, which matches zero rows and always renders the
"No files match this filter" empty state. Harmless (no error, degrade-safe), but a slightly
misleading control — a downstream + skipped combination is structurally impossible.

**Fix:** Either restrict the `skipped` bucket option to enrich stages in the filter UI, or accept the
always-empty result as intended (document it). Low priority.

---

_Reviewed: 2026-07-11_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
