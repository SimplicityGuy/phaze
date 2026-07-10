---
phase: 85-executed-gate-revival
plan: 03
subsystem: api
tags: [sqlalchemy, predicate, proposals, stage_status, cue, tracklists, router]

# Dependency graph
requires:
  - phase: 85-executed-gate-revival
    plan: 01
    provides: "applied_clause() + is_applied() predicate pair (services/stage_status.py)"
provides:
  - "cue.py eligible/gated readers + generate_cue guard read applied() -- CUE generation admits actually-applied files"
  - "the three tracklists cue-version guards read is_applied() (per-record)"
  - "_get_eligible_tracklist_query cut to applied_clause() (transitively fixes review.py's eligible half for Plan 04)"
affects: [85-04, 90-drop-files-state]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "WHERE/COUNT readers consume applied_clause() in .where(); per-record write/badge guards consume await is_applied(session, file_id)"
    - "EXECUTED-seeded router fixtures migrated to proposals.status='executed' + file.state='moved' (mutation-sensitive: a reverted state==EXECUTED guard rejects them)"

key-files:
  created: []
  modified:
    - src/phaze/routers/cue.py
    - src/phaze/routers/tracklists.py
    - tests/review/routers/test_cue.py
    - tests/identify/routers/test_tracklists.py

key-decisions:
  - "generate_cue gets NO session.commit() -- it writes a .cue to disk only, no DB mutation (VERIFIED, RESEARCH commit table)"
  - "tracklists guards use option (b): keep the loaded fr (fr.current_path is still needed for _get_cue_version) and gate on is_applied(session, fr.id)"
  - "removed FileState from BOTH cue.py and tracklists.py imports -- it became fully dead after the swaps (F401); FileRecord stays (still used for the fr loads / joins)"

patterns-established:
  - "Router cue-version/CUE-write gates derive applied-ness from proposals.status via the single-source applied() pair, never files.state"

requirements-completed: [READ-05]

# Metrics
duration: 40min
completed: 2026-07-10
---

# Phase 85 Plan 03: CUE-write + tracklists cue-version gates → applied() Summary

**The CUE-write path (eligible/gated readers + the `generate_cue` per-record guard) and the three `tracklists.py` cue-version per-record guards now read the D-01 `applied()` predicate (`proposals.status == 'executed'`), reviving CUE generation for actually-applied files and removing the last `FileRecord.state == FileState.EXECUTED` readers outside `review.py`.**

## Performance

- **Duration:** ~40 min
- **Completed:** 2026-07-10
- **Tasks:** 2
- **Files modified:** 4 (0 created, 4 modified)

## Accomplishments

- **`cue.py` readers** — `_get_eligible_tracklist_query` (:48) and `_get_cue_stats`' missing-timestamp COUNT (:89) now `.where(applied_clause(), ...)` instead of `FileRecord.state == FileState.EXECUTED`. Because `services/review.py` imports `_get_eligible_tracklist_query` from `cue.py`, this transitively fixes review.py's eligible half (Plan 04 depends on it landing first).
- **`cue.py` write guard** — `generate_cue` (:251) gates on `not await is_applied(session, file_record.id)` (keeping the existing "must be executed" error toast). No `session.commit()` added — `generate_cue` writes a `.cue` to disk only (VERIFIED).
- **`tracklists.py` cue-version guards** — all three per-record sites (:139 list loop, :601 approve handler, :898 list loop) gate on `await is_applied(session, fr.id)` instead of `fr.state == FileState.EXECUTED`; `fr` stays loaded so `fr.current_path` remains available for `_get_cue_version`.
- **Dead-reader removal (D-04-adjacent)** — zero `FileRecord.state == FileState.EXECUTED` / `state == FileState.EXECUTED` readers survive in either router; the now-dead `FileState` import was dropped from both (F401).
- **SC#2 behavior (CUE-admit)** — `test_cue.py` gains an explicit admit test proving a `state='moved'` file with an executed proposal IS admitted (`test_generate_cue_admits_applied_file_not_executed_state`) plus a non-applied reject test (`test_generate_cue_file_not_applied`). Fixtures migrated from `state='executed'` seeds to `proposals.status='executed'` + `file.state='moved'`.

## Task Commits

Each task was committed atomically:

1. **Task 1: Swap cue.py readers + generate_cue guard to applied(); assert CUE admit (SC#2)** — `5b5745c2` (feat)
2. **Task 2: Swap the three tracklists cue-version guards to is_applied; migrate fixtures** — `7412490a` (feat)

## Files Created/Modified

- `src/phaze/routers/cue.py` — `applied_clause()` in the eligible + missing-timestamp `.where()`s; `not await is_applied(session, file_record.id)` in the `generate_cue` guard; `FileState` import removed.
- `src/phaze/routers/tracklists.py` — three cue-version guards read `await is_applied(session, fr.id)`; `FileState` import removed; `is_applied` imported from `phaze.services.stage_status`.
- `tests/review/routers/test_cue.py` — helper reshaped (`applied: bool` param seeding `RenameProposal.status='executed'`/`'approved'`, `file.state='moved'`); two inline EXECUTED-seeded tests given executed proposals; SC#2 admit + non-applied reject tests added.
- `tests/identify/routers/test_tracklists.py` — added `_make_executed_proposal` helper; migrated the two CUE-badge-present fixtures (`test_undo_link_preserves_cue_version`, `test_list_tracklists_cue_version_executed`) to `proposals.status='executed'` + `file.state='moved'`.

## Decisions Made

- **No `session.commit()` in `generate_cue`** — confirmed disk-only (RESEARCH commit table); adding one would be an unnecessary mutation. Left absent.
- **Option (b) for tracklists guards** — `fr` is already loaded (needed for `fr.current_path`), so gate on `is_applied(session, fr.id)` rather than re-deriving from `tl.file_id`.
- **`file.state='moved'` in migrated fixtures** — makes the fixtures mutation-sensitive: a guard reverted to `state == FileState.EXECUTED` would reject a `state='moved'` file, so the CUE-admit/badge tests go RED on a revert. This directly proves the gates read `proposals.status`, not `files.state`.

## Deviations from Plan

None — plan executed exactly as written. The `FileState` import was removed from BOTH routers (the plan flagged this as "verify before removing"; in both files `FileState` was used ONLY at the swapped guard sites, so ruff would flag F401 — removal is correct and mypy/ruff confirm both files clean).

## Issues Encountered

- **Local test-infra flakes (colima VM pressure), not code:** running whole buckets/files together intermittently produces `asyncpg UniqueViolationError: pg_type_typname_nsp_index` (concurrent enum `create_all`) and Redis `ConnectionError`/`OSError` setup errors — a documented local flake (`reference_local_fullsuite_colima_flake` / `reference_ci_bucket_isolation`). The affected tests are unrelated (`test_execution*`, `test_proposal_queries`, `test_bulk_link_discogs`, etc.) and each passes in isolation; the set differs run-to-run. The touched-file tests are deterministically green:
  - `tests/review/routers/test_cue.py`: **23/23 passed** on a fresh DB in isolation.
  - `tests/identify/routers/test_tracklists.py` cue-version group (all three swapped sites): **5/5 passed** deterministically.

## Verification

- `uv run ruff check` + `uv run mypy` clean for `cue.py` and `tracklists.py` (pre-commit hooks — ruff, ruff-format, bandit, project mypy — passed on both commits).
- `grep -rn "FileState.EXECUTED" src/phaze/routers/cue.py src/phaze/routers/tracklists.py` → nothing.
- `cue.py`: 2× `applied_clause()`, 1× `await is_applied(session, file_record.id)`; `tracklists.py`: 3× `await is_applied(session, fr.id)`; 0× `session.commit` in `generate_cue`.

## Threat Flags

None — no new security surface. T-85-02 (stale/deleted-path CUE write) is *mitigated exactly as planned*: the CUE gate now reads `proposals.status=='executed'` (transactionally coupled to `current_path` via the apply path), not `execution_log`. T-85-05 (write_cue_file path containment) is untouched — no new write surface; the writer was already wired. Zero new packages (T-85-SC).

## Next Phase Readiness

- Plan 04 (services/review.py) can proceed: `_get_eligible_tracklist_query` now reads `applied_clause()`, so review.py's eligible half is already cut over via the shared import.
- The only surviving `FileRecord.state == FileState.EXECUTED` readers are now confined to `services/review.py` (Plan 04's target) — clearing another Phase-90 `files.state`-drop trap for the CUE/tracklists surface.

## Self-Check: PASSED

All 4 touched files exist on disk; both task commits (`5b5745c2`, `7412490a`) are present in the branch log.

---
*Phase: 85-executed-gate-revival*
*Completed: 2026-07-10*
