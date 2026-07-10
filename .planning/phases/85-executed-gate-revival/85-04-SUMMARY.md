---
phase: 85-executed-gate-revival
plan: 04
subsystem: api
tags: [sqlalchemy, applied-predicate, review, degrade-safe, pagination, dos-bound]

# Dependency graph
requires:
  - phase: 85-executed-gate-revival
    plan: 01
    provides: "applied_clause() predicate reading proposals.status=='executed' (services/stage_status.py)"
  - phase: 85-executed-gate-revival
    plan: 02
    provides: "tags.py helpers (_build_comparison/_count_changes/_get_tracklist_for_file/_get_accepted_discogs_link) review.py imports at module level"
  - phase: 85-executed-gate-revival
    plan: 03
    provides: "_get_eligible_tracklist_query cut to applied_clause() — review.py's eligible half fixed transitively"
provides:
  - "get_tagwrite_review_rows + get_cue_review_cards gated set read applied_clause() (the last two READ-05 EXECUTED readers)"
  - "_MAX_REVIEW_ROWS=2000 cap on both builders (D-03) — the now-populating applied backlog cannot blow up the render at 200K scale"
  - "review-audit integration fixtures migrated to proposals.status='executed' + file.state='moved' (mutation-sensitive)"
affects: [90-drop-files-state]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Degrade-safe render list builders bound by a fixed module .limit(N) cap (stronger DoS control than Query(le=100) since there is no operator-supplied page_size); begin_nested() SAVEPOINT + return [] wrapper untouched"
    - "D-03 bound tests patch the module cap constant (monkeypatch _MAX_REVIEW_ROWS=3) so the .limit() wiring is proven without seeding thousands of rows"

key-files:
  created: []
  modified:
    - src/phaze/services/review.py
    - tests/review/services/test_review_degrade.py
    - tests/integration/test_review_audit.py

key-decisions:
  - "_MAX_REVIEW_ROWS=2000 fixed cap (matches Plan 02's _MAX_BULK_TAG_WRITE) chosen over threading page/page_size through the router callers — keeps the edit self-contained to review.py + minimizes blast radius (plan's preferred idiom for degrade-safe render helpers)"
  - "get_cue_review_cards bounds BOTH halves: gated_stmt gets .limit(_MAX_REVIEW_ROWS); the eligible loop (fed by cue.py's _get_eligible_tracklist_query, which review.py cannot .limit() here) is capped in-memory via a break at the same cap"
  - "review-audit _executed_file migrated to state=MOVED + executed proposal (not just adding a proposal to an EXECUTED file) so the fixture is mutation-sensitive: a guard reverted to state==EXECUTED rejects a MOVED file"

patterns-established:
  - "The two genuinely-unbounded review list builders are the only READ-05 sites needing an explicit .limit() bound (the paginated list_tags and the capped bulk builders were handled in-place in Plans 02/03)"

requirements-completed: [READ-05]

# Metrics
duration: 45min
completed: 2026-07-10
---

# Phase 85 Plan 04: Review Builders applied() Cutover + D-03 Bound Summary

**The two genuinely-unbounded operator list builders in `services/review.py` (`get_tagwrite_review_rows` and the `get_cue_review_cards` gated set) cut over from the permanently-dead `FileRecord.state == FileState.EXECUTED` reader to the D-01 `applied_clause()` predicate and bounded by a fixed `_MAX_REVIEW_ROWS=2000` cap (D-03), with the `completed_subq` idempotency anti-join (D-02) and the `begin_nested()` degrade wrappers preserved verbatim — the last cutover seam of the READ-05 revival.**

## Performance

- **Duration:** ~45 min
- **Completed:** 2026-07-10
- **Tasks:** 2
- **Files modified:** 3 (0 created, 3 modified)

## Accomplishments

- **`get_tagwrite_review_rows` (review.py:109)** — WHERE swapped to `.where(applied_clause(), FileRecord.id.not_in(completed_subq))` + `.limit(_MAX_REVIEW_ROWS)`. The `completed_subq` (`TagWriteLog.status == COMPLETED`) anti-join is preserved verbatim (D-02); no state-based de-dupe reintroduced. The previously-empty Tag-write queue now populates for actually-applied files.
- **`get_cue_review_cards` gated set (review.py:251)** — the `FileRecord.state == FileState.EXECUTED` conjunct swapped to `applied_clause()`; the sibling `Tracklist.status == "approved"`, `Tracklist.file_id.is_not(None)`, and `has_timestamp_subq` conjuncts left intact. `gated_stmt` gains `.limit(_MAX_REVIEW_ROWS)`; the eligible half (fed by `_get_eligible_tracklist_query`, already cut to `applied_clause()` in Plan 03) is capped in-memory via a `break` at the same bound so total cards never exceed the cap.
- **D-03 bound:** `_MAX_REVIEW_ROWS = 2000` module constant (matches Plan 02's `_MAX_BULK_TAG_WRITE`) — a fixed cap is a stronger DoS control than a `Query(le=100)` bound here because these render helpers take no operator-supplied `page_size` (T-85-01 mitigation).
- **Degrade wrappers untouched:** all `async with session.begin_nested():` SAVEPOINT + `return []` wrappers are byte-for-byte preserved (9 `begin_nested` occurrences unchanged); no router try/except added.
- **Dead-reader removal:** zero `FileState.EXECUTED` readers survive in `review.py`; the now-dead `FileState` import was dropped (ruff F401); docstrings reworded to the `applied()` invariant.
- **Tests (Task 2):** a D-03 cap-bound test (patched `_MAX_REVIEW_ROWS=3`, 5 qualifying applied files → builder returns exactly 3) and a D-01/D-02 admit test (an applied `state='moved'` file with no COMPLETED log IS offered; one WITH a COMPLETED log is NOT). The `test_review_audit.py` `_executed_file` fixture migrated to seed a `proposals.status='executed'` + `state='moved'` file (mutation-sensitive).

## Task Commits

Each task was committed atomically:

1. **Task 1: gate review builders on applied() + bound both (D-03)** — `1dd53f84` (feat)
2. **Task 2: D-03 bound + D-01/D-02 applied-admit for review builders** — `9ba9cf85` (test)

## Files Created/Modified

- `src/phaze/services/review.py` — `applied_clause()` in both builders; `_MAX_REVIEW_ROWS=2000` module constant + `.limit(...)` on `get_tagwrite_review_rows.stmt` and `get_cue_review_cards.gated_stmt` + an eligible-loop `break` cap; `FileState` import dropped; three docstrings reworded to the applied() invariant. `completed_subq` (D-02) + `begin_nested()` wrappers preserved.
- `tests/review/services/test_review_degrade.py` — added `_seed_applied_tagwrite_file` helper + two DB-backed tests: `test_get_tagwrite_review_rows_bounded_by_cap` (D-03) and `test_get_tagwrite_review_rows_admits_applied_excludes_completed` (D-01 admit + D-02 idempotency). Existing degrade/formatter tests intact.
- `tests/integration/test_review_audit.py` — `_executed_file` now seeds `state=FileState.MOVED` + an `executed` `RenameProposal` (so the real applied() guard admits it, and the fixture is mutation-sensitive); the dedupe undo test's `previous_state` blob + final assertion track `FileState.MOVED`. Added `ProposalStatus`/`RenameProposal` imports.

## Decisions Made

- **Fixed cap over threaded pagination** — the plan gave discretion; chose the self-contained `.limit(_MAX_REVIEW_ROWS)` fixed cap (minimizing blast radius to `review.py`) over threading `page`/`page_size` through the router callers. `2000` matches the in-tree page bounds and Plan 02's `_MAX_BULK_TAG_WRITE`.
- **Cap the cue eligible half in-memory** — `_get_eligible_tracklist_query` (in `cue.py`) returns already-executed results, not a statement, so `review.py` cannot `.limit()` it directly; a `break` at `_MAX_REVIEW_ROWS` bounds the total card count so `get_cue_review_cards` is genuinely bounded (not just its gated `.limit()`).
- **Audit fixture set to MOVED, not EXECUTED+proposal** — makes the audit exercise the real predicate and go RED on a reverted `state==EXECUTED` guard; the dedupe undo assertions were updated to `MOVED` accordingly (the resolve/undo path is state-restoration, independent of the apply gate).

## Deviations from Plan

None — plan executed as written.

Note: the `test_review_audit.py` tag-write/undo tests were latently RED on the branch before this plan (Plan 02 changed the `execute_tag_write` guard to `is_applied`, but that plan did not migrate the audit fixtures, which seeded a bare `state=EXECUTED` file with no proposal). Task 2's fixture migration is exactly the intended fix — the acceptance criterion "migrate the `state='executed'`-seeded fixtures" covers it. This is documented here as a note rather than a deviation because it is the planned Task 2 scope.

## Issues Encountered

- **Test DB wiring (environment, not code):** `just test-bucket review` reported `257 passed, 172 errors` — every error was a DB-backed test failing at setup with `OSError: Connect call failed ('127.0.0.1', 5432)`. The `test-bucket` recipe does not export `TEST_DATABASE_URL`, and the running ephemeral Postgres (`phaze-test-db`) is on host port **5433**, not the conftest default 5432. Re-running with `TEST_DATABASE_URL=…localhost:5433/phaze_test` (and `MIGRATIONS_TEST_DATABASE_URL` for the integration bucket) is deterministically green (see Verification). This matches memory `reference_migrations_test_db_port` / `reference_local_fullsuite_colima_flake`.
- One unrelated flake in the full review-bucket run (`test_execution.py::test_collision_gate_blocks_execution` — a collision-gate test, no `review.py`/`applied()` involvement) errored at setup once and **passed in isolation** on re-run (documented colima VM-pressure flake).

## Verification

- `uv run ruff check src/phaze/services/review.py` + `uv run mypy src/phaze/services/review.py` — clean (no dead `FileState` import; F401-free).
- `grep -c "FileState.EXECUTED" src/phaze/services/review.py` == **0**.
- `review.py` contains `applied_clause()` in both builders; `get_tagwrite_review_rows` retains `completed_subq` (`TagWriteStatus.COMPLETED`); both builders contain `.limit(`; all `begin_nested()` wrappers unchanged.
- **Touched-file tests (isolation, live 5433 DB):** `tests/review/services/test_review_degrade.py` **8 passed**; `tests/integration/test_review_audit.py` **4 passed**; `tests/review/services/test_tag_writer.py` (Plan 02 regression) **19 passed**.
- **Full buckets (live 5433 DB):** review **428 passed** (1 unrelated flake, green in isolation); integration **187 passed**, 0 failures.
- **Phase-wide close-out:** `grep -rn "FileState.EXECUTED\|proposal.file.state" src/phaze/services src/phaze/routers src/phaze/templates` shows only the OUT-OF-SCOPE sites (`pipeline.py:57`, `proposal.py:39`, `shadow_compare.py:139`), a docstring in `stage_status.py:124`, and the proposal approve/reject **writers** in `proposal_queries.py:166/168` — **none of the 15 revived READ-05 EXECUTED readers remain**.

## Threat Flags

None — no new security surface. T-85-01 (DoS on the newly-visible applied backlog) is mitigated exactly as planned by the `.limit(_MAX_REVIEW_ROWS)` cap on both builders. T-85-02 (predicate source) is mitigated — `applied_clause()` reads `proposals.status=='executed'`, never `execution_log` or `files.state`. Zero new packages (T-85-SC).

## Next Phase Readiness

- READ-05 is fully revived: all 15 dead `state == EXECUTED` reader sites (across `stage_status.py`, `tags.py`, `tag_writer.py`, `cue.py`, `tracklists.py`, `review.py`, `proposal_row.html`) now read the single-source `applied()` predicate.
- The only surviving `FileState.EXECUTED` references in the READ surface are OUT-OF-SCOPE writers/enum-members (pipeline terminal-state set, proposal terminal-state set, shadow_compare invariant) — clearing the last `review.py` trap for the Phase-90 `files.state` drop.

## Self-Check: PASSED

All 3 touched files exist on disk; both task commits (`1dd53f84`, `9ba9cf85`) are present in the branch log.

---
*Phase: 85-executed-gate-revival*
*Completed: 2026-07-10*
