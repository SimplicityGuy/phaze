---
phase: 31-windowed-time-series-audio-analysis
plan: 03
subsystem: api
tags: [pydantic, sqlalchemy, fastapi, postgres, agent-wire-contract, idempotency]

# Dependency graph
requires:
  - phase: 31-windowed-time-series-audio-analysis (plan 02)
    provides: AnalysisWindow ORM model (analysis_window child table, migration 018)
provides:
  - AnalysisWindowPayload wire schema (tier Literal, ge guards, extra=forbid)
  - windows field on AnalysisWritePayload (bounded max_length=50000, partial-PUT None default)
  - put_analysis child-row replace (delete-by-file_id + bulk pg_insert, same transaction)
affects: [31-04 (process_file builds windows payload), 31-05/06 (review-UI timeline reads window rows)]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Idempotent child-row replace: delete-by-path-file_id then bulk pg_insert, guarded on body.windows is not None"
    - "Partial-PUT child semantics: None omits (preserve), [] clears, [..] replaces"

key-files:
  created: []
  modified:
    - src/phaze/schemas/agent_analysis.py
    - src/phaze/routers/agent_analysis.py
    - tests/test_schemas/test_agent_analysis.py
    - tests/test_routers/test_agent_analysis.py

key-decisions:
  - "windows popped from the aggregate model_dump BEFORE the overflow funnel so it never lands in features JSONB"
  - "body.windows read directly (not via exclude_unset dump) so an empty list [] is distinguishable from None"

patterns-established:
  - "Child-row replace runs in the SAME transaction as the aggregate upsert (single commit covers both)"
  - "DELETE + each inserted row use the PATH file_id only (cross-file-deletion mitigation, AUTH-01)"

requirements-completed: [ANL-01]

# Metrics
duration: ~20min
completed: 2026-06-10
---

# Phase 31 Plan 03: Windowed Agent Wire Contract + Idempotent Child Replace Summary

**Agents can now PUT per-window time-series alongside aggregates; put_analysis idempotently REPLACES a file's analysis_window rows (delete-by-file_id + bulk insert) in one transaction while preserving aggregate partial-PUT semantics.**

## Performance

- **Duration:** ~20 min
- **Started:** 2026-06-10T21:20:00Z (approx)
- **Completed:** 2026-06-10T21:40:00Z (approx)
- **Tasks:** 2 (both TDD: RED → GREEN)
- **Files modified:** 4

## Accomplishments
- `AnalysisWindowPayload` wire schema: `tier: Literal["fine","coarse"]`, `window_index`/`start_sec`/`end_sec` `ge` guards, fine fields (`bpm`/`musical_key`), coarse fields (`mood`/`style`/`danceability`/`features`), `extra="forbid"`.
- `AnalysisWritePayload.windows: list[AnalysisWindowPayload] | None = Field(default=None, max_length=50000)` — `| None` preserves partial-PUT; `max_length` bounds the DoS-via-huge-bulk-insert threat (T-31-03-02).
- `put_analysis` appends an idempotent child-replace after the aggregate upsert: `delete(AnalysisWindow).where(file_id == path file_id)` then bulk `pg_insert`, guarded on `body.windows is not None`, in the same transaction as the aggregate upsert.
- 100% line coverage on both changed source files; full schema + router suites green (28 tests).

## Task Commits

Each task committed atomically (TDD RED → GREEN):

1. **Task 1: AnalysisWindowPayload + windows field** — `79f051c` (test, RED) → `32c721b` (feat, GREEN)
2. **Task 2: put_analysis idempotent child replace** — `cc52e18` (test, RED) → `aade084` (feat, GREEN)

**Plan metadata:** committed separately with this SUMMARY.

## Files Created/Modified
- `src/phaze/schemas/agent_analysis.py` - Added `AnalysisWindowPayload`; added bounded `windows` field to `AnalysisWritePayload`.
- `src/phaze/routers/agent_analysis.py` - `put_analysis` now replaces window child rows (delete + bulk insert), popping `windows` from the aggregate dump before the overflow funnel; imports `delete` + `AnalysisWindow`.
- `tests/test_schemas/test_agent_analysis.py` - 9 window round-trip/rejection/partial-PUT/oversize cases.
- `tests/test_routers/test_agent_analysis.py` - 4 idempotency cases (replace-no-duplicates, partial-PUT preserves, `[]` clears, path-scoped delete).

## Decisions Made
- **Pop `windows` before the overflow funnel:** the existing aggregate path funnels any non-column field into `features` JSONB. `windows` is a child relationship, not an aggregate column, so it is popped out of `dumped` immediately after `model_dump(exclude_unset=True)` to keep it out of `features`.
- **Read `body.windows` directly for the child block:** an empty list is falsy, so the partial-PUT guard uses `body.windows is not None` (off the model, not the dump) to distinguish "omit/preserve" (None) from "clear" (`[]`).

## Deviations from Plan
None - plan executed exactly as written. The two implementation notes above are within the plan's stated action (the action explicitly anticipated the `is not None` guard and the path-`file_id`-only delete); popping `windows` from the aggregate dump is the natural consequence of the existing overflow funnel and required no structural change.

## Issues Encountered
None. RED phases failed as expected (ImportError for the schema; 0 inserted rows for the router), GREEN phases passed on first implementation. ruff-format reformatted the RED test file once on first commit attempt; re-staging resolved it.

## Threat Surface
No new surface beyond the plan's `<threat_model>`. Mitigations applied as specified:
- T-31-03-01 (cross-file deletion): DELETE + insert use PATH `file_id` only; asserted by `test_analysis_window_idempotent_delete_scoped_to_path_file_id`.
- T-31-03-02 (oversized bulk insert): `max_length=50000` + per-field `ge` guards + `extra="forbid"`; asserted by `test_analysis_write_payload_rejects_oversized_windows`.
- T-31-03-SC: zero new packages added.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Wire contract + persistence path ready for Plan 04 (`process_file` builds the `windows=[...]` payload from `analyze_file`'s per-window output).
- Window rows are queryable for Plans 05/06 (review-UI timeline). No blockers.

---
*Phase: 31-windowed-time-series-audio-analysis*
*Completed: 2026-06-10*
