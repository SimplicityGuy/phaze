---
phase: 92-milestone-close-tech-debt-cleanup
reviewed: 2026-07-13T00:00:00Z
depth: standard
files_reviewed: 32
files_reviewed_list:
  - src/phaze/services/pipeline.py
  - src/phaze/routers/agent_files.py
  - src/phaze/services/backends.py
  - tests/conftest.py
  - tests/integration/conftest.py
  - tests/agents/cli/test_agents_add.py
  - tests/agents/routers/test_agent_push.py
  - tests/agents/routers/test_agent_s3.py
  - tests/analyze/core/test_dispatch_snapshot.py
  - tests/analyze/core/test_reenqueue.py
  - tests/analyze/core/test_stage_progress.py
  - tests/analyze/core/test_staging_cron.py
  - tests/analyze/tasks/test_reconcile_cloud_jobs.py
  - tests/analyze/tasks/test_recovery.py
  - tests/analyze/tasks/test_release_awaiting_cloud.py
  - tests/analyze/tasks/test_submit_cloud_job.py
  - tests/analyze/test_force_skip_writer.py
  - tests/analyze/test_retry_affordances.py
  - tests/discovery/tasks/test_scan_reaper.py
  - tests/integration/test_agent_push_concurrency.py
  - tests/integration/test_agent_s3_concurrency.py
  - tests/integration/test_dedup_resolve_undo_shadow.py
  - tests/integration/test_drain_double_dispatch.py
  - tests/integration/test_files_page.py
  - tests/integration/test_fingerprint_progress.py
  - tests/integration/test_lifespan_orphan_task.py
  - tests/integration/test_reconcile_concurrency.py
  - tests/integration/test_stage_progress_buckets.py
  - tests/integration/test_stage_status_equivalence.py
  - tests/integration/test_staging_cron_concurrency.py
  - tests/review/routers/test_duplicates.py
  - tests/shared/test_conftest_hermeticity.py
findings:
  critical: 0
  warning: 4
  info: 2
  total: 6
status: issues_found
---

# Phase 92: Code Review Report

**Reviewed:** 2026-07-13
**Depth:** standard
**Files Reviewed:** 32
**Status:** issues_found

## Summary

Reviewed the CLEAN-01 production fan-out in `services/pipeline.py`, the two comment-only
edits (`agent_files.py`, `backends.py`), and the CLEAN-02/CLEAN-03 test-suite hermeticity
rewrite (`conftest.py`, `integration/conftest.py`, and 28 migrated/added test files).

The core `get_stage_progress` parallelization is carefully engineered and defensible:
statement construction is pure and pre-built; gather-order matches the unpacking order
1:1; the 13-tuple `type_cast` shape is correct; degrade discipline is layered
(`_safe_count`/`_safe_bucket_counts` never raise, and `_read_in_own_session` catches the
out-of-`fn` pool-acquire `TimeoutError`); `asyncio.gather` cannot propagate because every
task swallows `Exception`; the shared `bucket_default` dict is only spread, never mutated;
the per-poll `Semaphore` correctly avoids the event-loop-binding trap; and the lambdas bind
distinct statements (no late-binding closure bug). The `create_savepoint` conftest rewrite
is sound — the single-connection funnel makes flushed-but-uncommitted AND
committed-via-savepoint-release rows visible to the routed fan-out and to the `verify`
sibling, and the per-test outer-transaction rollback guarantees hermeticity. The
`committed_db` fixture correctly isolates the genuine cross-connection concurrency tests and
re-seeds the FK parent after its TRUNCATE.

No BLOCKER-class defects were found: the one behavior that could crash the hot poll (pool
saturation) is degrade-safe by construction. The findings below are robustness,
consistency, and maintainability concerns — the most material being the increased peak pool
checkout per dashboard request against the deliberately-lean post-PgBouncer-incident pool,
and the loss of the single-snapshot consistency the serial reader used to provide.

No structural pre-pass (`<structural_findings>`) was provided.

## Warnings

### WR-01: Fan-out raises peak pool checkout per dashboard poll from ~1 to ~5 against a lean 10-conn pool

**File:** `src/phaze/services/pipeline.py:490-543, 631-672`
**Issue:** Before CLEAN-01, `get_stage_progress` ran all ~13 reads on the caller's single
request session (1 connection). It now opens up to `Semaphore(4)` additional independent
sessions from `phaze.database.async_session`, on top of the request's own `get_session`
connection — a peak of ~5 concurrent checkouts per poll. The pool is deliberately lean
(`pool_size=5 + max_overflow=5 = 10`, `pool_timeout=10`) precisely because of the prior
PgBouncer session-mode exhaustion incident (see `database.py` header comment and project
memory `project_pgbouncer_pool_exhaustion`). The "leaves >=6 free" reasoning in the comment
assumes exactly ONE in-flight poll; two overlapping 5s polls (a slow prior poll not yet
returned when the next fires, plus the background orphan refresher) reach 2×5 = 10 and
saturate the worker pool. The failure is degrade-safe (each `_read_in_own_session` returns
its `default` after the 10s `pool_timeout`), but the visible symptom is the dashboard
flashing zero counts and a poll that blocks up to `pool_timeout` before degrading rather
than degrading fast.
**Fix:** Consider lowering the cap (e.g. `Semaphore(3)`) to preserve strictly more headroom,
and/or make the acquire non-blocking under contention (attempt with a short timeout and fall
back to `default` immediately) so a saturated pool degrades in milliseconds instead of
waiting the full `pool_timeout`. At minimum, add an operational note that overlapping polls
can transiently zero the dashboard so it is not mistaken for a data bug.

### WR-02: Independent-per-read transactions can transiently break the sum-to-total and done<=total invariants

**File:** `src/phaze/services/pipeline.py:674-704`
**Issue:** Each gathered read now runs in its OWN transaction/MVCC snapshot. The enrich
nodes are assembled as `{**metadata_b, "total": music_video_total}` where the five buckets
(`metadata_b`) and the `total` (`music_video_total`) come from two DIFFERENT snapshots. The
serial predecessor read every count in ONE transaction, so the five buckets always summed to
`music_video_total` and `done <= total` held on any healthy query. Under concurrent writes a
file added between the total read and the bucket read (or vice-versa) can make the buckets
sum to a different number than `total`, or make `done > total`, which the DAG bar can render
as >100%. The docstring acknowledges this ("NOT strict identity under live writes") and for
a 5s single-user poll it is self-correcting, but it is a genuine regression from the prior
consistency guarantee and worth an explicit accept.
**Fix:** Acceptable as documented for the dashboard use case. If cross-node consistency is
ever required, clamp at the render layer (`done = min(done, total)`) or read the
mutually-dependent counts (buckets + their `total`) within a single shared read rather than
splitting them across snapshots.

### WR-03: `verify` fixture shares the per-test connection but does not depend on `session`, so correctness relies on call-site parameter order

**File:** `tests/conftest.py:313-332`
**Issue:** `verify` depends only on `_db_connection`, not on `session`. Its
`create_savepoint` session must join the outer transaction that `session` begins
(`_db_connection.begin()`), and at teardown its session must be closed BEFORE
`session`'s `await outer.rollback()`. Both hold today only because every consuming test lists
`session`/`client` before (or as a dependency ahead of) `verify`, so `session` sets up first
and tears down last. A future test that requests `verify` ahead of `session` as direct
params would invert teardown order and call `s.close()` on a session whose outer transaction
was already rolled back — an avoidable, order-dependent failure.
**Fix:** Make the ordering explicit by having `verify` depend on `session`
(`async def verify(session: AsyncSession, _db_connection: AsyncConnection)`), which both
guarantees the outer transaction exists before `verify` executes and pins teardown to run
`verify` before `session`.

### WR-04: Dead/misleading `async_engine` parameter retained across several helpers after the routing rewrite

**File:** `tests/analyze/test_force_skip_writer.py:62` (and its 8 call sites); `tests/analyze/core/test_dispatch_snapshot.py:134`; `tests/analyze/core/test_reenqueue.py:76`; `tests/analyze/tasks/test_reconcile_cloud_jobs.py:187`
**Issue:** `_read_skip(async_engine, ...)` no longer uses `async_engine` — it reads via the
monkeypatched `phaze.database.async_session`. Likewise the three `_make_ctx(async_engine, ...)`
helpers now source `async_session` from `phaze.database` and ignore the passed engine (the
`session` fixture already transitively instantiates `async_engine` via `_db_connection`). The
retained parameter is dead and actively misleading: it signals "this read/ctx uses that
engine" when it no longer does, exactly the kind of stale signal the CLEAN-03 comment scrub
targeted elsewhere.
**Fix:** Drop the unused `async_engine` parameter from these helpers (and the corresponding
positional arguments at call sites), or, if the intent is to force the `async_engine` fixture
to be requested, take it as a named fixture on the test function rather than threading it
through a helper that never touches it.

## Info

### IN-01: `get_stage_progress` accepts a `session` it ignores — a silent contract divergence

**File:** `src/phaze/services/pipeline.py:546`
**Issue:** The signature still takes `session: AsyncSession` (now `# noqa: ARG001`) and every
caller passes its request session, but the function no longer reads it — all reads run in
independent sessions. All current callers are read-only dashboard paths, so there is no live
bug, but a future caller that writes in its request transaction and then calls
`get_stage_progress` expecting to see those uncommitted writes will silently read stale/empty
data with no error.
**Fix:** Keep the parameter for signature stability (as intended) but make the divergence
loud: the docstring already notes "UNUSED-BY-DESIGN"; consider renaming to `_session` or
adding a short assertion/comment at each call site that the reads are snapshot-independent so
the contract is discoverable without reading the implementation.

### IN-02: Two parallel fan-out routing mechanisms now coexist; the per-file one lacks savepoint isolation

**File:** `tests/integration/test_stage_progress_buckets.py:84-127` vs `tests/conftest.py:259-287`
**Issue:** The global suite routes the fan-out via `_route_stats_fanout` using a fresh
`create_savepoint` session per read (each read's error rolls back only its own savepoint). The
`test_stage_progress_buckets.py` `db_session` fixture instead monkeypatches
`phaze.database.async_session` to a `_yield_shared_session` that hands the SAME single session
to every fan-out read. On that shared session a read error in `_safe_count`/`_safe_bucket_counts`
calls `session.rollback()`, which would discard the flushed-but-uncommitted seed for all
subsequent reads in the same poll. It works today only because these tests never trigger a
read error, but it is a latent, less-robust variant of the mechanism the global conftest
already generalizes.
**Fix:** Migrate `test_stage_progress_buckets.py` to the global `session`/`_route_stats_fanout`
funnel (or a local `create_savepoint`-per-read factory) so a spurious read error cannot poison
the shared seed, and to keep one routing mechanism in the suite.

---

_Reviewed: 2026-07-13_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
