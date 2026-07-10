---
phase: 84-dedup-fingerprint-progress-cutover
plan: 04
subsystem: services
tags: [sqlalchemy, fingerprint, progress, derive-dont-store, dedup-marker, agent-worker-boundary]

# Dependency graph
requires:
  - phase: 84-02-shared-predicate
    provides: services/stage_status.dedup_resolved_clause() — file-level correlated exists(marker)
  - phase: 78-derivation-layer
    provides: services/stage_status.done_clause/failed_clause(Stage.FINGERPRINT) — DERIV-05 aggregation
provides:
  - "get_fingerprint_progress derives total/completed/failed from MUSIC_VIDEO_TYPES + the dedup marker + done/failed_clause(Stage.FINGERPRINT); 3-key contract + shared denominator preserved; all DB imports function-local"
  - "tests/integration/test_fingerprint_progress.py — mutation-tested real-DB replacement for the toothless mock stub"
affects: [phase-87-per-engine-breakdown, phase-90-filestate-drop]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Shared-denominator counts: a `denom` tuple splatted into every `.where(*denom, ...)` guarantees completed ⊆ total and failed ⊆ total (progress bar can never exceed 100%)"
    - "Agent-worker import boundary: every phaze.models/database/services.pipeline/services.stage_status dependency imported INSIDE the function body (D-00e), asserted by a sys.modules leak spot-check"

key-files:
  created:
    - tests/integration/test_fingerprint_progress.py
  modified:
    - src/phaze/services/fingerprint.py
    - tests/fingerprint/services/test_fingerprint.py

key-decisions:
  - "D-09: 3-key {total, completed, failed} contract preserved — each body redefined over derived predicates; zero API break for docs/api.md:35 and justfile:500"
  - "D-10: total = count(files) where file_type IN MUSIC_VIDEO_TYPES AND ~dedup_resolved_clause() — no FileRecord.state read"
  - "D-11: completed and failed become FILE counts (done_clause/failed_clause(Stage.FINGERPRINT)); failed stops being a fingerprint_results ROW count"
  - "D-12: no per-engine breakdown — done_clause(FINGERPRINT) is already the per-engine coverage predicate; GROUP BY engine is Phase 87"
  - "D-17: all three keys share total's denominator, so completed ⊆ total and failed ⊆ total"
  - "D-00e: MUSIC_VIDEO_TYPES, done_clause/failed_clause/dedup_resolved_clause, Stage all imported function-locally; FileState and FingerprintResult function-local imports dropped (no writer here)"
  - "D-15: the side_effect assert-your-own-dict stub replaced with a real-DB integration test, mutation-tested RED on both regressions"

patterns-established:
  - "A green guard proves nothing: the new test was mutation-tested (completed→state==FINGERPRINTED gives 0≠2 RED; failed→row count gives 3≠1 RED) then restored to GREEN"

requirements-completed: [READ-04]

# Metrics
duration: 25min
completed: 2026-07-09
---

# Phase 84 Plan 04: get_fingerprint_progress Derivation Cutover Summary

**Rewrote `get_fingerprint_progress` off `FileRecord.state` and onto the derived predicates — `total` = `file_type IN MUSIC_VIDEO_TYPES AND ~dedup_resolved_clause()` (D-10), `completed`/`failed` = FILE counts over `done_clause`/`failed_clause(Stage.FINGERPRINT)` sharing that denominator (D-11/D-17) — while preserving the 3-key `{total, completed, failed}` contract (D-09) and the function-local agent-worker import boundary (D-00e), and replaced the toothless mock stub with a mutation-tested real-DB integration test (D-15).**

## Performance
- **Duration:** ~25 min
- **Tasks:** 2
- **Files created:** 1 · **Files modified:** 2

## The `completed` jump / `failed` drop is THE FIX (not a regression)
This is the point of the plan and must be stated plainly for whoever watches the dashboard move after deploy:

- **`completed` VISIBLY JUMPS.** It previously read `state == FileState.FINGERPRINTED`, whose sole writer is `retry_analysis_failed` — so in practice it counted ~nothing. It now counts files whose fingerprint stage is genuinely done (`done_clause(Stage.FINGERPRINT)`: any engine row `status IN ('success','completed')`, riding `ix_fprint_success`).
- **`failed` VISIBLY DROPS.** It previously counted `fingerprint_results` **ROWS** with `status='failed'` — so a file failing two engines counted **twice**, and a file with one success + one failure was miscounted as failed. It now counts **files** whose stage is failed (`failed_clause(Stage.FINGERPRINT)`: no engine succeeded AND ≥1 failed, DERIV-05). A one-success/one-failure file is now correctly `completed`, not `failed`; a two-engine-failure file counts **1**, not 2.

Both movements are the derivation correcting long-standing miscounts, per D-11.

## Accomplishments
- **`get_fingerprint_progress` rewrite (D-09..D-17):** built a shared `denom = (FileRecord.file_type.in_(MUSIC_VIDEO_TYPES), ~dedup_resolved_clause())` tuple splatted into all three `.where(*denom, ...)` clauses, so `completed`/`failed` are strict subsets of `total`. `completed` uses `done_clause(Stage.FINGERPRINT)` unchanged (Pitfall 6 — no re-spelled status set); `failed` uses `failed_clause(Stage.FINGERPRINT)` (no `count(FingerprintResult.id)` row count). Docstring rewritten to describe the derived contract and flag the number changes.
- **Agent-worker boundary intact (D-00e / Pitfall 5):** `MUSIC_VIDEO_TYPES`, `done_clause`/`failed_clause`/`dedup_resolved_clause`, and `Stage` are all imported **inside** the function; the now-unused `FileState` and `FingerprintResult` function-local imports were dropped. `grep "^from phaze" src/phaze/services/fingerprint.py` shows **no** module-level `phaze.models`/`phaze.database`/`phaze.services.pipeline`/`phaze.services.stage_status` import, and a `sys.modules` leak spot-check confirms importing the module drags none of those four into the graph. Zero `FileState.FINGERPRINTED` attribute access remains (the only textual `FINGERPRINTED` is a docstring word — invisible to the 84-05 AST guard, which matches `ast.Attribute` nodes).
- **Mock stub replaced (D-15):** deleted `TestGetFingerprintProgress.test_get_progress_returns_counts` (the `session.execute` `side_effect` list that asserted its own dict) plus the now-unused `get_fingerprint_progress` import; left a breadcrumb comment pointing to the new test. Created `tests/integration/test_fingerprint_progress.py` on the `test_shadow_compare.py` real-PG `db_session` idiom (no SAQ fixture). Corpus of 7 files pins the denominator, units, aggregation, and subset in one assertion `{"total": 5, "completed": 2, "failed": 1}`.

## Corpus (the marker-not-state proof)
| # | File | Backing | Effect |
|---|------|---------|--------|
| 1 | music `mp3` | fingerprint `success` | total + completed |
| 2 | video `mp4` | none | total only |
| 3 | non-audio `txt` | fingerprint `success` | **excluded from total** (D-10 — even a success can't pull a non-MV file in) |
| 4 | music `mp3` + **marker** | — | **excluded from total** (D-10 marker exclusion) |
| 5 | music `mp3`, `state='duplicate_resolved'`, **no marker** | none | **INCLUDED in total** — a state read would exclude it; the derivation includes it (marker is authority) |
| 6 | music `mp3` | one `success` + one `failed` | completed, NOT failed (DERIV-05) |
| 7 | music `mp3` | two `failed` | failed, counted **1** (a FILE, not a ROW) |

## Task Commits
1. **Task 1 — rewrite `get_fingerprint_progress` over the derived predicates** — `15bd5749` (feat)
2. **Task 2 — replace the mock stub with a real-DB integration test** — `b7ed1a9c` (test)

## Mutation-Check Evidence (both proven RED then restored GREEN)
Both mutations were applied to `src/phaze/services/fingerprint.py`, run against the new integration test on port 5433, then restored via `git checkout` (source verified byte-identical to the Task-1 commit afterward).

- **MUTATION #1 — `completed` reverted to `state == FileState.FINGERPRINTED`.** Observed RED:
  ```
  >       assert progress == {"total": 5, "completed": 2, "failed": 1}
  E       AssertionError: ...
  E         {'completed': 0} != {'completed': 2}
  ```
  None of the seeded files sits at `state='fingerprinted'` (they carry `success` rows instead), so the state read collapses `completed` from 2 to **0**. Restored → 1 passed (GREEN).
- **MUTATION #2 — `failed` reverted to `count(FingerprintResult.id) where status=='failed'` (a ROW count).** Observed RED:
  ```
  >       assert progress == {"total": 5, "completed": 2, "failed": 1}
  E       AssertionError: ...
  E         {'failed': 3} != {'failed': 1}
  ```
  The row count sums file #6's one `failed` row + file #7's two `failed` rows = **3**, exposing BOTH the two-engine double-count and the one-success/one-failure misclassification in a single number. Restored → 1 passed (GREEN).

## Decisions Made
- **Deleted the whole `TestGetFingerprintProgress` class**, not just the method — it held only the one stub, and `get_fingerprint_progress` was imported solely for it (import removed too; `AsyncMock`/`MagicMock` stay, used by the orchestrator mocks). Left a comment pointing to the integration test so the deletion is not later mistaken for lost coverage.
- **Self-contained the integration test's `db_session` fixture** (copied the `test_shadow_compare.py` real-PG idiom verbatim) rather than importing across test packages — matches the established integration-test pattern and keeps the `_test`-database destructive-safety guard local.
- **No `tests/buckets.json` change needed** — the new file lives under `tests/integration/`, already the `integration` bucket; the fingerprint mock file stays in the `fingerprint` bucket (35 passed, unaffected).

## Deviations from Plan
None — plan executed exactly as written.

## Threat Flags
None — no new network endpoints, auth paths, or schema surface. The parameterless read-only endpoint (`routers/pipeline.py:1339`) is unchanged; the sole trust-boundary threat (T-84-04-01, agent-worker import crash from a module-level DB import) is mitigated by the function-local imports, verified by the `sys.modules` leak spot-check.

## Issues Encountered
- `just test-bucket` does not export `TEST_DATABASE_URL`/`MIGRATIONS_TEST_DATABASE_URL`/`PHAZE_QUEUE_URL`, which default to port **5432** in the test harness while the ephemeral `phaze-test-db` runs on **5433** (known footgun). Exported all three against 5433 for every run.

## Verification
- `uv run ruff check .` and `uv run mypy .` both exit 0 (206 source files).
- `grep "^from phaze" src/phaze/services/fingerprint.py` → no module-level model/database/pipeline/stage_status import; `sys.modules` spot-check → none of the four leaked at import time.
- No `FileState.FINGERPRINTED` attribute access in `services/fingerprint.py` (only a docstring word).
- `tests/integration/test_fingerprint_progress.py` → 1 passed in isolation on 5433; `tests/fingerprint/services/test_fingerprint.py` → 35 passed (mocks unaffected); stub gone (`grep -c "side_effect=[mock_result_total" → 0`).
- Both mutation checks observed RED (`completed 2→0`; `failed 1→3`) then restored GREEN.

## Next Phase Readiness
- Plan 84-05 ships the AST guard asserting zero `FileState.FINGERPRINTED` in `services/fingerprint.py` — satisfied here (the surviving textual occurrence is a docstring word, not an `ast.Attribute`).
- Phase 87 (per-engine breakdown) can add a `GROUP BY engine` view alongside `done_clause(FINGERPRINT)` without touching this 3-key contract (D-12).

## Self-Check: PASSED
- FOUND: src/phaze/services/fingerprint.py
- FOUND: tests/integration/test_fingerprint_progress.py
- FOUND: tests/fingerprint/services/test_fingerprint.py
- FOUND commit: 15bd5749 (Task 1)
- FOUND commit: b7ed1a9c (Task 2)

---
*Phase: 84-dedup-fingerprint-progress-cutover*
*Completed: 2026-07-09*
