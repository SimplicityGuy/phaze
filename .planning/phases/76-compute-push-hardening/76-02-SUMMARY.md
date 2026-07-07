---
phase: 76-compute-push-hardening
plan: 02
subsystem: control-plane/agent-push
tags: [HARD-02, concurrency, advisory-lock, push, ledger]
requires:
  - "routers/agent_push.py::report_push_mismatch (Phase 50/69/73 push callback)"
  - "SchedulingLedger model (key + payload JSONB)"
  - "config.push_max_attempts (gt=0, lt=20)"
provides:
  - "Advisory-locked push_attempt RMW: concurrent /mismatch cannot lose an increment, without deadlocking the before_enqueue hook"
affects:
  - "src/phaze/routers/agent_push.py"
  - "tests/agents/routers/test_agent_push.py"
tech-stack:
  added: []
  patterns:
    - "pg_advisory_xact_lock(hashtext(key)) to serialize a JSONB counter RMW without a row lock (avoids deadlocking a nested before_enqueue upsert on the same row)"
    - "Real-Postgres concurrent-transaction regression test via two AsyncSessions + a parking FakeQueue"
    - "Regression test that drives the REAL apply_deterministic_key before_enqueue hook to prove no deadlock (RED-verified: hangs on a row lock, passes on the advisory lock)"
key-files:
  created: []
  modified:
    - "src/phaze/routers/agent_push.py"
    - "tests/agents/routers/test_agent_push.py"
decisions:
  - "D-05 (as-shipped, superseded): initial fix was `.with_for_update()` on the ledger SELECT — self-deadlocked against the before_enqueue hook (see CR-01 correction below); auth gate + CR-01 CAS guard byte-unchanged throughout"
  - "CR-01 correction (operator-approved 2026-07-06): replaced the row lock with pg_advisory_xact_lock(hashtext(key)); same serialize-the-RMW intent, different lock space so the hook's row upsert never deadlocks"
  - "D-06: concurrent no-lost-update test uses genuine row contention (parking queue holds the lock while a second request blocks)"
metrics:
  duration: "~9 min"
  completed: "2026-07-06"
  tasks: 2
  files_changed: 2
requirements: [HARD-02]
---

# Phase 76 Plan 02: push_attempt Ledger RMW Atomicity Summary

Row-locked the `push_attempt` read-modify-write in `report_push_mismatch` with `.with_for_update()` so two concurrent `/mismatch` callbacks for one file can no longer lose an increment (AR-73-02 / T-73-13 / WR-04), while the bounded `push_max_attempts` cap still trips at the exact boundary and the D-06/D-07 reporter-auth gate and CR-01 PUSHING-only spill guard remain untouched.

## What Was Built

### Task 1 — Row-lock the ledger RMW SELECT + concurrent regression tests (commit `944c8408`)
- **Production change (one line):** appended `.with_for_update()` to the `SchedulingLedger` SELECT at `agent_push.py:231` that reads `push_attempt` for the read→+1→write-back. This serializes the RMW at the Postgres row level: a second concurrent `/mismatch` blocks on the lock until the first commits, then reads the committed value and applies its own increment.
- **Test 1 — `test_mismatch_concurrent_no_lost_update`:** exercises genuine row contention against the real port-5433 test DB. Each request runs in its own `AsyncSession`/transaction (from an `async_sessionmaker` over the shared `async_engine`). Request A parks mid-transaction (a `_GatedQueue.connect()` blocks *after* the row-locked SELECT but *before* the write-back), holding the lock while request B is launched and blocks on it. After A commits and drops the lock, B reads `1` and writes `2` → persisted `push_attempt == 2`. Confirmed RED: with `.with_for_update()` removed both requests read `0` and write `1` (final `1`), and the test fails.
- **Test 2 — `test_mismatch_cap_trips_exactly_at_boundary`:** pins the cap boundary on both sides with `push_max_attempts=3` — `push_attempt=2` (next 3, not `> 3`) re-drives and stays PUSHING; `push_attempt=3` (next 4, `> 3`) spills to `AWAITING_CLOUD` with the ledger cleared. Demonstrates the lock change does not shift the boundary.

### Task 2 — Quality gate (no code changes)
- `uv run ruff check` and `uv run ruff format --check` on both files: clean.
- `uv run mypy src/phaze/routers/agent_push.py`: `Success: no issues found`.
- `just docs-drift`: green (10 passed).
- `pyproject.toml` / `uv.lock`: unchanged (D-10 — no new dependencies).
- The gate surfaced no lint/type findings, so there was nothing to fix and no separate commit was needed.

## Verification Results
- `tests/agents/routers/test_agent_push.py`: **17 passed** against the real port-5433 test DB (`just test-db` → run → `just test-db-down`).
- RED discrimination confirmed: removing `.with_for_update()` makes `test_mismatch_concurrent_no_lost_update` fail (lost update → final `1`); restoring it → final `2`.
- `grep -n "with_for_update"` returns the RMW SELECT at `agent_push.py:231`.
- `git diff` on production code shows only the `.with_for_update()` addition (plus its explanatory comment); the reporter-auth gate and CR-01 CAS guard are byte-unchanged.

## Deviations from Plan
None — plan executed exactly as written. The only adjustment was internal to the new tests: the fixture `agent` object is expired by the `_ledger_row`/`_file_row` helpers' `session.expire_all()`, so `agent.id` is captured into a local before those helpers run (the same early-capture pattern the existing tests use for `fileserver_id`). This is test-only and introduced no production change.

## Post-Review Correction — CR-01 (commit after code review)

The standard code-review gate (`76-REVIEW.md`) found that the as-shipped `.with_for_update()` on the ledger SELECT (`agent_push.py:231`) **self-deadlocks on the common under-cap re-drive path**: `report_push_mismatch` re-enqueues `push_file` while its transaction is still open, and `push_file` is a registered `_KEY_BUILDERS` entry, so the `apply_deterministic_key` before_enqueue WRITE hook opens its own session on the same pool (`ledger_sessionmaker=async_session`) and upserts the **same** `push_file:<id>` ledger row. That nested upsert blocks on the row lock the uncommitted request holds, and the request can't commit to release it until `enqueue()` returns — a hang with no `statement_timeout`/`lock_timeout` anywhere and no Postgres deadlock cycle to detect. The original `FakeQueue`-based tests missed it because they never run the real hook.

**Fix (operator chose "advisory xact lock"):** replaced the row lock with a transaction-scoped `pg_advisory_xact_lock(func.hashtext(ledger_key))` acquired before a plain SELECT. It still serializes concurrent `/mismatch` for the same key (no lost increment; cap still trips) but lives in a different lock space than the hook's row upsert, so the hook proceeds and there is no deadlock. Added `test_mismatch_real_enqueue_hook_does_not_deadlock`, which drives the **real** `apply_deterministic_key` hook and is RED-verified (`TimeoutError` at 15s on the row-lock version, passes on the advisory-lock version). This supersedes D-05's specific `.with_for_update()` mechanism while preserving its intent.

Final module state: **18 passed** against the port-5433 test DB; ruff/format/mypy/docs-drift green; `pyproject.toml`/`uv.lock` unchanged.

## Self-Check: PASSED
- Modified files present:
  - `src/phaze/routers/agent_push.py` — FOUND
  - `tests/agents/routers/test_agent_push.py` — FOUND
- Commit present:
  - `944c8408` — FOUND
