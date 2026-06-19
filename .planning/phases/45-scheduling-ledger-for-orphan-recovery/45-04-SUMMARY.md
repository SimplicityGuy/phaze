---
phase: 45-scheduling-ledger-for-orphan-recovery
plan: 04
subsystem: recovery
tags: [saq, postgres, scheduling-ledger, backfill, startup, idempotency, control-only]

# Dependency graph
requires:
  - phase: 45-01
    provides: "insert_ledger_if_absent (ON CONFLICT DO NOTHING) + routing_for_function + SchedulingLedger model"
  - phase: 45-03
    provides: "recover_orphaned_work (ledger-driven) + reenqueue.py control-only banner this builds on"
  - phase: 35-deterministic-keys
    provides: "_KEY_BUILDERS (the 8 keyed-function allowlist) + <function>:<natural_id> key shape"
  - phase: 36-postgres-broker
    provides: "saq_jobs table (PostgresQueue, default json.dumps serializer -> JSON blob with function/kwargs/key)"
provides:
  - "backfill_ledger_from_saq_jobs(session) -- one-time idempotent startup reconcile seeding the ledger from live queued/active saq_jobs blobs"
  - "_parse_job_blob(blob) -- tolerant json.loads blob deserializer (mirrors pipeline._job_started_ms)"
  - "controller.startup wiring: backfill runs BEFORE recover_orphaned_work in its own try/except (boot never aborts)"
affects: []

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "control-side runtime reconcile of the SAQ-owned saq_jobs table (read-only SAVEPOINT probe), NEVER an Alembic data step"
    - "backfill consumes the Plan-01 insert_ledger_if_absent DO-NOTHING primitive verbatim -- adds no new ledger contract, edits no Plan-01 test"
    - "inserted tally is the count of keyed INSERT-if-absent CALLS (upper bound); idempotency is asserted on the durable ROW COUNT, not the tally"

key-files:
  created:
    - tests/test_tasks/test_ledger_backfill.py
  modified:
    - src/phaze/tasks/reenqueue.py
    - src/phaze/tasks/controller.py
    - tests/test_tasks/test_recovery.py
    - tests/test_tasks/test_controller_reenqueue.py

key-decisions:
  - "backfill_ledger_from_saq_jobs reads ONLY (job, key) from saq_jobs inside a begin_nested() SAVEPOINT and degrades to an empty tally on any error (missing table / DB hiccup), cloning the get_live_job_keys / get_straggler_count isolation -- a pre-migration boot cannot abort."
  - "function classification trusts the blob's top-level `function` field but falls back to the saq_jobs key prefix (split on ':') so a row missing the field is still routed correctly; a non-keyed function (not in _KEY_BUILDERS) is skipped."
  - "the `inserted` return field counts keyed INSERT-if-absent CALLS (an upper bound, since a DO-NOTHING no-op against a pre-existing hook row still counts the call); the integration test therefore asserts the durable ledger ROW COUNT for idempotency + no-overwrite, not the tally. Documented in the function docstring."

patterns-established:
  - "Startup ordering: backfill (seed) BEFORE recovery (replay), each in an independent try/except, so the in-flight cohort already in saq_jobs is recoverable on first boot and neither step can abort controller boot."

requirements-completed: [L-04, L-05]

# Metrics
duration: ~55min
completed: 2026-06-19
---

# Phase 45 Plan 04: Startup Ledger Backfill Summary

**`backfill_ledger_from_saq_jobs` seeds the durable scheduling_ledger from the live queued/active `saq_jobs` rows once at boot (idempotent ON CONFLICT DO NOTHING, keyed-functions only, never overwriting a hook-written row, tolerant of bad blobs + a missing table) and runs in controller startup BEFORE recovery in its own try/except — closing the blind window between the 022 migration landing and the before_enqueue WRITE hook populating the ledger, with no risk of aborting boot.**

## Performance

- **Duration:** ~55 min
- **Completed:** 2026-06-19
- **Tasks:** 1/1 (TDD: RED -> GREEN, single task)
- **Files modified:** 5 (1 created, 4 modified)

## Accomplishments

### Task 1 — backfill_ledger_from_saq_jobs (idempotent) + startup wiring

- **`backfill_ledger_from_saq_jobs(session) -> dict[str, int]`** (`reenqueue.py`): reads `(job, key)` from `saq_jobs` WHERE status IN ('queued','active') inside a `begin_nested()` SAVEPOINT (degrade-to-empty on a missing table / DB error, cloning the `get_live_job_keys` discipline). For each row it deserializes the blob via the new `_parse_job_blob` (the SAME tolerant `json.loads` idiom as `pipeline._job_started_ms`; no `saq.Job` construction). A row whose function is a keyed pipeline function (in `_KEY_BUILDERS`, with a key-prefix fallback when the blob omits `function`) is seeded with `insert_ledger_if_absent` (the Plan-01 ON CONFLICT DO NOTHING primitive); a random-key / non-keyed / unparseable / fieldless row is skipped. Returns `{"inserted": N, "skipped": M}`. This plan added NO new ledger contract and edited NO Plan-01 test — it only consumes `insert_ledger_if_absent`.
- **`_parse_job_blob(blob)`** (`reenqueue.py`): `json.loads` a str/bytes blob, pass a pre-decoded dict through, return None for anything that is not a JSON dict — so one malformed/malicious blob skips ALONE (T-45-12).
- **`controller.startup`**: a NEW try/except block opens `async with ctx["async_session"]() as session: await backfill_ledger_from_saq_jobs(session); await session.commit()` BEFORE the existing `recover_orphaned_work(ctx)` call. A backfill failure logs (`ledger backfill on startup failed`) and never aborts boot or blocks the subsequent recovery (independent try/except blocks).

### Tests

- **`tests/test_tasks/test_ledger_backfill.py`** (new):
  - unit: `_RaisingSession` proves a missing `saq_jobs` table degrades to `{"inserted": 0, "skipped": 0}` (T-45-14);
  - unit: parametrized `_parse_job_blob` tolerance (bytes/str/dict in, dict-or-None out);
  - unit: `_SeededSession` drives the loop over keyed + key-prefix-fallback + random-key + bad-blob + fieldless rows and asserts the tally + which keys reached the INSERT (every classification branch, no DB);
  - unit: a keyed row with non-dict `kwargs` is seeded with empty kwargs (defensive);
  - integration (`@pytest.mark.integration`, real `saq_jobs`): seeds keyed-queued + keyed-active + keyed-complete + random-key + bad-blob rows plus a pre-existing hook-written sentinel row, runs backfill twice, and asserts keyed-queued/active present, complete/random/bad absent, the sentinel payload preserved (no overwrite), and the durable ROW COUNT unchanged on the second run (idempotent).
- **`tests/test_tasks/test_recovery.py`**: added `test_startup_backfills_ledger_before_recovery` (spy on both controller-side names with a shared call-order list; asserts `["backfill", "recover"]`, each awaited once) and `test_startup_survives_raising_backfill` (a raising backfill never aborts boot and recovery still runs).
- **`tests/test_tasks/test_controller_reenqueue.py`**: updated `_patch_startup_constructors` so the patched `async_sessionmaker` returns a callable yielding an async-context-manager session (the new startup backfill opens `async with ctx["async_session"]()`), eliminating a `coroutine never awaited` warning the new wiring would otherwise raise in the existing startup tests.

## Deviations from Plan

### Tooling adjustments

**1. [Rule 3 - Blocking issue] Missing-table degrade test uses a fake session, not a SQLite engine**
- **Found during:** GREEN.
- **Issue:** The first draft of `test_missing_saq_jobs_table_degrades_to_no_op` used an in-memory `sqlite+aiosqlite` engine to model a DB with no `saq_jobs` table, but `aiosqlite` is not a project dependency (`ModuleNotFoundError`). Adding a package is explicitly NOT auto-fixable (slopsquat guard), and a new test-only dep is unwarranted.
- **Fix:** Replaced it with a `_RaisingSession` stand-in whose SAVEPOINT `execute` raises, exercising the same degrade path with no DB and no new dependency. Also added the `_parse_job_blob` parametrized unit test + `_SeededSession` loop test so the full backfill body is covered without the integration DB.
- **Files modified:** tests/test_tasks/test_ledger_backfill.py
- **Commit:** e57bc3d

**2. [Rule 1 - Bug] Pre-existing startup tests emitted a `coroutine never awaited` warning under the new wiring**
- **Found during:** GREEN.
- **Issue:** `test_controller_reenqueue.py`'s startup tests patched `async_sessionmaker` to a bare `MagicMock()`; the new `async with ctx["async_session"]() as session` backfill call made `session.begin_nested()` return a non-awaited coroutine mock, raising a RuntimeWarning (the backfill swallowed the error so the tests still passed, but noisily).
- **Fix:** Patched `async_sessionmaker` to return a callable yielding a real `@asynccontextmanager` session stub. In-scope because the warning was introduced by THIS task's wiring change.
- **Files modified:** tests/test_tasks/test_controller_reenqueue.py
- **Commit:** e57bc3d

Note (not a deviation): the integration test asserts `first["inserted"] == 2` (both keyed rows had INSERT-if-absent CALLS issued) and proves idempotency/no-overwrite via the durable ROW COUNT + sentinel payload, exactly as the function docstring sanctions — `inserted` is an upper bound on rows written, not a count of rows written.

## Threat Mitigations Applied

- **T-45-12 (tampering — unparseable/malicious blob):** each blob parse is isolated in `_parse_job_blob`; a non-JSON / non-dict / fieldless row skips alone (asserted by the bad-blob integration row + the parametrized unit test). One bad row never aborts the batch.
- **T-45-13 (correctness — overwriting a fresher hook row):** backfill uses `insert_ledger_if_absent` (ON CONFLICT DO NOTHING); the integration test seeds a hook-written sentinel payload and asserts it survives.
- **T-45-14 (availability — backfill aborting boot):** wrapped in a startup try/except (mirrors the recover_orphaned_work guard) + an internal SAVEPOINT degrade-to-empty on a missing `saq_jobs` table (asserted by the `_RaisingSession` unit test + `test_startup_survives_raising_backfill`).
- **T-45-15 (coupling — reading saq_jobs from Alembic):** the backfill is a runtime startup reconcile in `reenqueue.py` (control-only banner preserved); no Alembic step touches `saq_jobs`.
- **T-45-SC:** no new packages this plan (the aiosqlite mis-step was reverted before commit).

## Verification

- `uv run pytest tests/test_tasks/test_ledger_backfill.py tests/test_tasks/test_recovery.py tests/test_task_split.py -q -m "not integration"` -> **27 passed** (the plan's automated verify gate).
- Regression: `tests/test_tasks/ tests/test_task_split.py tests/test_main_lifespan.py tests/test_services/test_scheduling_ledger.py -m "not integration"` -> **193 passed**, 0 failures.
- Integration: `test_backfill_seeds_keyed_skips_random_is_idempotent_and_no_overwrite` -> **1 passed** against the ephemeral broker (`just test-db` on :5433); seeds real `saq_jobs` blobs, runs backfill twice, asserts keyed-seeded / random-skipped / complete-skipped / bad-blob-skipped / hook-row-preserved / row-count-stable.
- `uv run mypy src/phaze/tasks/reenqueue.py src/phaze/tasks/controller.py` -> clean (also clean over the full tree via the pre-commit mypy hook).
- `uv run ruff check .` -> clean. Pre-commit hooks ran on the commit (no `--no-verify`); mypy passed.
- Acceptance greps: `def backfill_ledger_from_saq_jobs`, `json.loads` (in `_parse_job_blob`), `insert_ledger_if_absent`, and `_BACKFILL_SAQ_JOBS_SQL` all present in `reenqueue.py`; `backfill_ledger_from_saq_jobs` imported + called in `controller.py` startup before `recover_orphaned_work`.

Note: the pre-existing Plan-03 integration test `test_count_inflight_jobs_reads_real_saq_jobs` fails when run OUTSIDE the full `just integration-test` harness (it needs Redis on :6380 + applied migrations for `pipeline_stage_control`; a bare `just test-db` + wrong Redis port produces a `ConnectionError`/`UndefinedTable`). This is an environmental harness dependency unrelated to Plan 04 (out of scope — logged here for the verifier, not fixed).

## Known Stubs

None — the backfill is fully wired (function + startup call + tests). On a worker whose `saq_jobs` table is absent (pre-migration) or empty, the backfill is a degrade-safe no-op by design, not a stub.

## Notes for Downstream Plans

Phase 45 is complete with this plan: the ledger is WRITTEN at the before_enqueue chokepoint (01), CLEARED on every terminal outcome (01 controller + 02 agent), DRIVES recovery (03), and is BACKFILLED from the live broker at first boot (04). The backfill becomes a cheap no-op once the transition cohort drains; it stays safe to run on every boot (DO NOTHING). No further ledger work is required for the orphan-recovery incident fix.

## Self-Check: PASSED

- `src/phaze/tasks/reenqueue.py` exists; `src/phaze/tasks/controller.py` exists; `tests/test_tasks/test_ledger_backfill.py` exists; `tests/test_tasks/test_recovery.py` exists.
- Commit `e57bc3d` (feat 45-04) present in the worktree branch history (`git log` HEAD).
