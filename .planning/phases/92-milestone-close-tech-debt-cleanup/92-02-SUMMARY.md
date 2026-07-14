---
phase: 92-milestone-close-tech-debt-cleanup
plan: 02
subsystem: api
tags: [asyncio, sqlalchemy, asyncpg, concurrency, get_stage_progress, pipeline, perf, pool-headroom]

# Dependency graph
requires:
  - phase: 82-counts-pending-set-cutover
    provides: "the 200K perf harness (seed_perf_corpus.py / perf_explain.py / just perf-* targets) and the five-bucket get_stage_progress shape"
  - phase: 90-files-state-drop
    provides: "migration 039 (files.state drop) that the perf-DB seed must stage past"
provides:
  - "Parallelized get_stage_progress: independent reads fan out via asyncio.gather, each in its own AsyncSession, bounded by a per-poll asyncio.Semaphore(4)"
  - "_read_in_own_session helper + _stats_fanout / _STATS_FANOUT patchable seam (deferred phaze.database.async_session import) that 92-03 routes through the per-test connection"
  - "Before/after 200K /pipeline/stats + get_stage_progress DIRECT latency numbers and the DENORM-01 disposition in 92-VERIFICATION.md"
affects: [92-03-conftest-hermeticity, DENORM-01, pipeline-stats-perf]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Bounded per-task-session fan-out: asyncio.gather over independent AsyncSessions (one asyncpg connection each) with an acquisition-degrade belt so a pool_timeout degrades a single node, never the poll"
    - "Loop-safe module semaphore: build a fresh Semaphore per poll bound to the running loop (module-singleton binds to one loop and breaks under pytest per-test loops)"
    - "Fan-out routing seam: deferred phaze.database.async_session import + module _STATS_FANOUT override so tests can route the fan-out onto their own connection"

key-files:
  created:
    - ".planning/phases/92-milestone-close-tech-debt-cleanup/92-VERIFICATION.md"
  modified:
    - "src/phaze/services/pipeline.py"
    - "tests/analyze/core/test_stage_progress.py"
    - "tests/integration/test_stage_progress_buckets.py"
    - "tests/conftest.py"

key-decisions:
  - "CLEAN-01: get_stage_progress fans out ALL independent reads (not just the 3 enrich buckets) concurrently, each in its own AsyncSession, bounded by Semaphore(4); _safe_count/_safe_bucket_counts reused verbatim (D-04)"
  - "The incoming session param is kept for signature stability, documented unused-by-design (D-05 Open Question 2)"
  - "Semaphore is built FRESH per poll (via _stats_fanout) bound to the running loop, NOT a module-level singleton — a pre-constructed module Semaphore binds to one event loop and raises under pytest per-test loops"
  - "DENORM-01 stays DEFERRED/killed: get_stage_progress DIRECT p50 dropped 1468.9->860.6ms (under the <1s budget); residual endpoint overhead is outside the stage-count reads DENORM-01 would replace"

patterns-established:
  - "Per-poll fan-out: _read_in_own_session(fanout, fn, default) opens one degrade-safe read per gather task; acquisition wrapped so pool-timeout degrades to the node default"
  - "Test-side fan-out routing: monkeypatch phaze.database.async_session (onto the test's session) + phaze.services.pipeline._STATS_FANOUT (Semaphore(1)) so flush/commit-and-read tests still see their rows"

requirements-completed: [CLEAN-01]

# Metrics
duration: 45min
completed: 2026-07-13
---

# Phase 92 Plan 02: Parallelize get_stage_progress Summary

**get_stage_progress now fans its ~13 independent reads out concurrently via `asyncio.gather` over bounded per-task `AsyncSession`s (`Semaphore(4)`), cutting the 200K DIRECT poll core from 1468.9ms to 860.6ms p50 (under the <1s budget) and settling DENORM-01 as deferred.**

## Performance

- **Duration:** ~45 min
- **Started:** 2026-07-13T23:55Z (approx)
- **Completed:** 2026-07-14T00:39Z (approx)
- **Tasks:** 3
- **Files modified:** 5 (1 src, 3 test, 1 verification doc)

## Accomplishments
- Parallelized `get_stage_progress` (CLEAN-01/PERF-02): every independent read runs concurrently, each in its own `AsyncSession` from `phaze.database.async_session`, bounded by a fresh per-poll `asyncio.Semaphore(4)`; `_safe_count`/`_safe_bucket_counts` reused verbatim (D-04); 9-key dict + order + derived `done` buckets byte-identical on a quiescent DB.
- Acquisition-degrade belt (RESEARCH Pitfall 2): a pool `TimeoutError` during session checkout degrades that single node to its safe default rather than aborting the gather / 500ing the 5s poll.
- Measured before/after at 200K on the reused Phase-82 harness and recorded the numbers + DENORM-01 verdict in `92-VERIFICATION.md`: DIRECT 1468.9→860.6ms p50 (−41%), `/pipeline/stats` 1737.5→1072.2ms p50 (−38%).
- Preserved the fan-out seam (deferred `async_session` import + module `_STATS_FANOUT` override) so 92-03 Task 2 can route the fan-out through the per-test connection.

## Task Commits

1. **Task 1: BEFORE serial 200K baseline + perf-DB routing confirmation** - `80ea1f5c` (docs)
2. **Task 2: Parallelize get_stage_progress with bounded per-task-session fan-out** - `68d305b2` (feat)
3. **Task 3: AFTER 200K numbers + DENORM-01 verdict (D-05)** - `0626aa0f` (docs)

## Files Created/Modified
- `src/phaze/services/pipeline.py` - `get_stage_progress` rewritten to `asyncio.gather` over `_read_in_own_session`; added `_STATS_FANOUT` override + `_stats_fanout()` (fresh cap-4 semaphore per poll, loop-safe); PEP 695 generic helper.
- `tests/analyze/core/test_stage_progress.py` - degrade test retargeted onto the fan-out seam (monkeypatch `_safe_bucket_counts` to raise for FINGERPRINT) since the old passed-session monkeypatch no longer intercepts the reads.
- `tests/integration/test_stage_progress_buckets.py` - `db_session` fixture routes the fan-out back onto its own flush-and-rollback session (monkeypatch `async_session` → the shared session, `_STATS_FANOUT` → `Semaphore(1)`) so the uncommitted-row reads still work.
- `tests/conftest.py` - `async_engine` teardown now disposes the module-level `phaze.database.engine` so its asyncpg pool connections do not leak across pytest per-test loops.
- `.planning/phases/92-milestone-close-tech-debt-cleanup/92-VERIFICATION.md` - PERF-02 re-measurement: environment, staged migration recipe, routing proof, before/after numbers, SC1 verdict, DENORM-01 disposition, snapshot-skew caveat.

## Decisions Made
- Kept the `session` parameter (signature stability, documented unused-by-design) rather than removing it and touching ~2 callers (D-05 Open Question 2 — lowest blast radius).
- Built the semaphore fresh per poll instead of a module singleton — the literal `_STATS_FANOUT = asyncio.Semaphore(4)` from the plan is an event-loop-binding bug under pytest; the fresh-per-poll factory keeps the same cap-4 semantics and the patchable `_STATS_FANOUT` override.
- DENORM-01 stays deferred/killed: the parallelization brought the stage-progress DB core (its target) under budget; residual endpoint overhead is outside those reads.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Module-level `asyncio.Semaphore(4)` binds to a single event loop**
- **Found during:** Task 2 (running the targeted tests)
- **Issue:** The plan specified a module-level `_STATS_FANOUT = asyncio.Semaphore(4)`. An asyncio primitive binds to the loop of its first use, so under pytest-asyncio's per-test loops every test after the first raised `RuntimeError: bound to a different event loop`, degrading all reads to zero. This is a real bug (it would also bite any future test that re-enters get_stage_progress across loops), not just a test artifact.
- **Fix:** Made `_STATS_FANOUT` an optional override (default `None`) and added `_stats_fanout()` that returns a FRESH `asyncio.Semaphore(4)` bound to the running loop per poll (or the override when set). Same cap-4 semantics, same patchable seam name; the `Semaphore(4)` literal is an honest executable literal in the factory.
- **Files modified:** src/phaze/services/pipeline.py
- **Verification:** `tests/analyze/core/test_stage_progress.py` 10/10 green; mypy + ruff clean.
- **Committed in:** `68d305b2` (Task 2)

**2. [Rule 3 - Blocking] Module engine asyncpg pool leaks connections across pytest per-test loops**
- **Found during:** Task 2 (full-file run of test_stage_progress.py)
- **Issue:** The CLEAN-01 fan-out is the first code path in these tests to use the module-level `phaze.database.async_session`. Its asyncpg pool binds each connection to the loop that created it; pytest-asyncio's fresh-per-test loops made later tests reuse stale connections → `Event loop is closed` / `got Future attached to a different loop` → reads degraded to zero (3 tests failing intermittently on pool state).
- **Fix:** Disposed the module engine in the existing `async_engine` fixture teardown (same loop that created the connections) so the next test opens fresh connections. Superseded cleanly by 92-03's `async_session` routing.
- **Files modified:** tests/conftest.py
- **Verification:** test_stage_progress.py 10/10 + test_pipeline.py 110/110 green across the full files.
- **Committed in:** `68d305b2` (Task 2)

**3. [Rule 1 - Bug] Existing tests coupled to the passed session no longer route through the reads**
- **Found during:** Task 2
- **Issue:** `test_single_source_db_error_degrades_to_zero` monkeypatched the *passed* `session.execute`, and `test_stage_progress_buckets.py` seeded via `flush` (uncommitted) and read via the passed session. Post-parallelization the reads run in their own sessions, so neither mechanism intercepts/feeds the fan-out — the tests asserted stale behavior.
- **Fix:** Retargeted the degrade test onto the fan-out seam (monkeypatch `_safe_bucket_counts` to raise for FINGERPRINT — exercises the acquisition-degrade belt), and routed the integration fixture's fan-out back onto its own session via the `async_session`/`_STATS_FANOUT` seam (Semaphore(1)). Intent preserved (single node degrades, siblings intact; flush-and-read isolation still holds).
- **Files modified:** tests/analyze/core/test_stage_progress.py, tests/integration/test_stage_progress_buckets.py
- **Verification:** degrade test + all 4 integration bucket tests green.
- **Committed in:** `68d305b2` (Task 2)

**4. [Rule 3 - Blocking] Fresh perf DB cannot reach migration HEAD in one `alembic upgrade head`**
- **Found during:** Task 1 (seeding the 200K perf corpus)
- **Issue:** `just perf-seed` runs `alembic upgrade head` first, but migration 038 hard-aborts (no non-revoked fileserver to reattribute the legacy-owned seed files to) and migration 039 aborts on the seeded active `cloud_job` mid-flight rows; the single-transaction upgrade rolled back to empty.
- **Fix:** Staged the migration operationally (no code change): `upgrade 037` → seed (files.state still exists) → insert a non-revoked `perf-fileserver` → `upgrade 038` (reattribute + delete sentinel) → `-x force=1 upgrade head` (drop state on the throwaway perf DB). Full recipe recorded in 92-VERIFICATION.md for reproducibility.
- **Files modified:** none (operational; documented in 92-VERIFICATION.md)
- **Verification:** perf DB reached revision 039 with 200000 files; both bench instruments ran.
- **Committed in:** `80ea1f5c` (Task 1, documented)

---

**Total deviations:** 4 auto-fixed (2 Rule-1 bugs, 2 Rule-3 blocking)
**Impact on plan:** All four are corrections needed for the parallelization to be correct and testable; the loop-safety fix is a genuine improvement over the plan's literal spec. Two touched test files beyond the plan's declared `src/phaze/services/pipeline.py` — necessary consequences of the architecture change (and 92-03 supersedes the conftest/integration-fixture routing). No product-behavior change; the returned dict is byte-identical on a quiescent DB.

## Issues Encountered
- The endpoint `time_endpoint` instrument logs a Redis `mget` degrade (the ASGI test app has no lifespan-wired Redis client); `_read_pipeline_counters` catches it and returns `{}`, so `/pipeline/stats` still returns 200. Identical before/after, so it does not bias the delta. Not a regression.

## Threat Flags
None — no new endpoints, inputs, packages, secrets, or schema. The one trust boundary (fan-out vs lean asyncpg pool, T-92-02-DoS) is mitigated exactly as the threat register specifies (`Semaphore(4)` + acquisition-degrade), validated by the 200K measurement.

## Next Phase Readiness
- 92-03 (conftest hermeticity, wave 2) can now route the fan-out through the per-test connection via the preserved seam: monkeypatch `phaze.database.async_session` (per-test connection) + `phaze.services.pipeline._STATS_FANOUT` (`Semaphore(1)`). The conftest module-engine dispose + the two per-file test-routing shims here are superseded by that global rewrite.
- Perf DB container `phaze-perf-db` (port 5545) left running for reviewer reproduction; tear down with `just perf-db-down`.

## Self-Check: PASSED

All modified/created files exist on disk; all three task commits (`80ea1f5c`, `68d305b2`,
`0626aa0f`) are present in git history.

---
*Phase: 92-milestone-close-tech-debt-cleanup*
*Completed: 2026-07-13*
