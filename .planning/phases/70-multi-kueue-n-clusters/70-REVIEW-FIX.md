---
phase: 70-multi-kueue-n-clusters
fixed_at: 2026-07-04T00:00:00Z
review_path: .planning/phases/70-multi-kueue-n-clusters/70-REVIEW.md
iteration: 1
findings_in_scope: 5
fixed: 5
skipped: 0
status: all_fixed
---

# Phase 70: Code Review Fix Report

**Fixed at:** 2026-07-04
**Source review:** .planning/phases/70-multi-kueue-n-clusters/70-REVIEW.md
**Iteration:** 1

**Summary:**
- Findings in scope: 5 (CR-01, CR-02, WR-01, WR-02, WR-03; the Info finding IN-01 was out of scope)
- Fixed: 5
- Skipped: 0

All fixes are transaction-semantics / logic changes. Each is covered by the existing suite plus (for
the two blockers) a new regression test, and all four affected suites pass green against the ephemeral
test DB (81 passed). Because CR-01 and CR-02 turn on subtle SQLAlchemy autoflush / poisoned-transaction
behavior, they are flagged **requires human verification** — the tests confirm the observable behavior,
but a human should confirm the transaction-ordering reasoning holds under production concurrency.

## Fixed Issues

### CR-01: `KueueBackend.dispatch` mutated `FileState` before gating on the fileserver agent

**Files modified:** `src/phaze/services/backends.py`, `tests/analyze/services/test_backends.py`
**Commit:** 43dcafa
**Status:** fixed: requires human verification (transaction-ordering / autoflush semantics)

**Applied fix:** Reordered `KueueBackend.dispatch` so the deterministic bucket pick/resolve and the
`_stage_file_to_s3(...)` call (which resolves the fileserver agent FIRST via
`select_active_agent(kind="fileserver")` and reads nothing from `file.state`) run BEFORE
`file.state = FileState.PUSHING`. This mirrors `LocalBackend`/`ComputeAgentBackend`'s gate-before-mutate
ordering, so a `NoActiveAgentError` (or any pre-upsert S3 raise) is mutation-free: SQLAlchemy autoflush
can no longer flush a pending PUSHING flip as a side effect of the gate's `SELECT`, so the drain's single
post-loop commit can never persist a PUSHING file with no `cloud_job` row (the Pitfall-4 limbo). Did NOT
introduce a mid-loop commit (Landmine L1 preserved). Added regression test
`test_kueue_dispatch_no_fileserver_agent_leaves_file_untouched` exercising the REAL `KueueBackend` (not a
stub) with no fileserver agent: it asserts the file stays `AWAITING_CLOUD` and no `cloud_job` row is
written even after emulating the drain's post-loop commit.

### CR-02: `stage_cloud_window` could commit a partial write / let a poisoned transaction raise out of the cron

**Files modified:** `src/phaze/tasks/release_awaiting_cloud.py`, `tests/analyze/tasks/test_release_awaiting_cloud.py`
**Commit:** ee552ad
**Status:** fixed: requires human verification (poisoned-transaction / rollback semantics)

**Applied fix:** Wrapped the per-candidate dispatch loop AND the single post-loop `session.commit()` in one
outer safety-net `try/except`. On any unexpected raise — e.g. a Postgres serialization/deadlock surfaced
from a `session.execute` in the loop body that sits OUTSIDE the per-candidate `try` (such as
`_cloud_attempts_for`), which poisons the transaction so every later statement including the final commit
would raise — the guard rolls back the WHOLE tick (discarding any uncommitted partial write, so no phantom
dispatch is ever committed) and returns a clean `{"staged": 0, "skipped": len(candidates)}`. The held
candidates stay `AWAITING_CLOUD` and re-stage next tick.

Deliberately did NOT add a per-candidate `await session.rollback()` in the `except NoActiveAgentError` /
`except Exception` branches: a mid-loop rollback would end the transaction and release
`pg_advisory_xact_lock`, re-opening the over-stage class (Landmine L1), and would forfeit already-staged
candidates in the same tick. The existing per-backend failure-isolation tests
(`..._generic_dispatch_raise_holds_candidate_and_continues`, `..._dispatch_noactiveagent_holds_all_and_breaks`)
lock in the `continue`/`break` (no mid-loop rollback) semantics, which this fix preserves — the safety net
is the ONLY rollback and it only fires at tick boundary. With CR-01 in place, the common per-candidate raise
(NoActiveAgentError / pre-upsert S3 error) is mutation-free, so `continue`/`break` leave nothing partial;
the outer guard covers the residual poisoned-transaction path. Added regression test
`test_stage_cloud_window_unexpected_error_rolls_back_and_never_raises` (patches `_cloud_attempts_for` to
raise): the cron returns a clean hold, attempts no dispatch, and leaves every file `AWAITING_CLOUD` with no
`cloud_job` row.

**Known residual (documented, not a limbo):** a raise from the SAQ enqueue (a separate DB connection, so it
does NOT poison the session txn) AFTER the `cloud_job` upsert within a dispatch is caught by the generic
`continue` branch and its partial `cloud_job` row commits. On the Kueue path this self-heals — the file
stays `AWAITING_CLOUD`, so the next tick re-dispatches and the idempotent `on_conflict(file_id)` upsert
refreshes the row. Making dispatch strictly all-or-nothing across the Postgres+queue boundary would require
reordering the enqueue ahead of the upsert inside the shared `_stage_file_to_s3` (also used by the
committing `redrive_upload`), which is a larger design change than a review fix and carries its own
orphaned-enqueue tradeoff; it was intentionally left out of scope.

### WR-01: reconcile per-row advisory-lock granularity defeated by no-op branches

**Files modified:** `src/phaze/tasks/reconcile_cloud_jobs.py`
**Commit:** 399fa6a
**Status:** fixed

**Applied fix:** Added an unconditional `await session.commit()` to each steady-state no-op return branch of
`_reconcile_one` (missing-`kueue_workload`, admission-unreadable `workload is None`, already-inadmissible,
healthy-Pending-unchanged, already-RUNNING, and the trailing unknown-condition fallthrough). Each is a clean
no-op commit when the row was unchanged, so the `pg_advisory_xact_lock` acquired per row in
`KueueBackend.reconcile` now always releases at a per-row commit rather than spanning into the next row's
iteration (Pitfall 2). Chose this surgical per-branch placement over a single caller-level unconditional
commit specifically because a caller-level commit adds a redundant trailing commit AFTER the terminal
branches that already commit (e.g. the at-cap `delete → commit → delete_job` path), which broke the strict
ordering assertion in `test_clean_before_flip_ordering_delete_precedes_commit_precedes_job`. The terminal
handlers (`_record_success` / `_handle_no_callback_terminal`) keep their single existing commit untouched.
Also removed the now-dead `dirty` flag from the healthy-Pending branch (its only reader was the removed
conditional commit).

### WR-02: inconsistent S3 error wrapping — three verbs leaked raw `ClientError`

**Files modified:** `src/phaze/services/s3_staging.py`
**Commit:** 02dc699
**Status:** fixed

**Applied fix:** Wrapped `ClientError` in `S3StagingError` for `create_multipart_upload`,
`presign_upload_parts`, and `presign_get`, matching the module's existing fail-loud discipline in
`complete_multipart_upload` / `abort_multipart_upload` / `delete_staged_object`. Now every caller that
distinguishes S3 misconfiguration via `except S3StagingError` sees a uniform error surface instead of a
leaked `botocore.exceptions.ClientError`.

### WR-03: no uniqueness validation on `[[buckets]]` ids

**Files modified:** `src/phaze/config.py`
**Commit:** 1162af5
**Status:** fixed

**Applied fix:** Added a duplicate-`id` check to `ControlSettings._validate_registry` (using
`collections.Counter`), raising `ValueError` at boot when two `[[buckets]]` blocks share an `id`. This makes
a copy-paste id typo fail fast like every other registry invariant in the validator, instead of silently
collapsing to whichever entry appears last in the TOML list (the behavior of the `{b.id: b}` lookup that
`resolve_bucket_config` builds), which would non-deterministically redirect presign/cleanup to the wrong
endpoint/credentials.

## Verification

- Static: `ruff check`, `ruff format --check`, and `mypy .` (192 source files) all pass on every changed file.
- Tests (ephemeral DB on localhost:5433, `TEST_DATABASE_URL` / `MIGRATIONS_TEST_DATABASE_URL`):
  `tests/analyze/tasks/test_release_awaiting_cloud.py`, `tests/analyze/tasks/test_reconcile_cloud_jobs.py`,
  `tests/analyze/services/test_backends.py`, `tests/analyze/services/test_s3_staging.py` — **81 passed**
  (79 pre-existing + 2 new regression tests).

---

_Fixed: 2026-07-04_
_Fixer: Claude (gsd-code-fixer)_
_Iteration: 1_
