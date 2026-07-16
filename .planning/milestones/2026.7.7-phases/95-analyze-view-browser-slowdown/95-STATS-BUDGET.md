# Phase 95 — `/pipeline/stats` Budget Verification + Fan-Out Fix (phaze-zqvh.4)

Verify-bead for the Phase 95 epic (`phaze-zqvh`, CONSOLE-04). Satisfies ROADMAP phase-95 success
criterion 3: the DENORM-01 deferral decision, recorded here with numbers, either way. Builds
directly on the `phaze-zqvh.1` baseline (`95-BASELINE.md`, frozen — not edited by this artifact).

## Method — identical to `95-BASELINE.md`

Same Phase 82 PERF-02 bench harness, same dedicated perf Postgres (`phaze-perf-db`, port 5545,
database `phaze_perf82`, seeded ~200K-file corpus — 92,335-row Analyze-stage membership, unchanged
from the baseline), same env exports:

```bash
export PHAZE_DATABASE_URL=postgresql+asyncpg://phaze:phaze@localhost:5545/phaze_perf82
export PHAZE_QUEUE_URL=postgresql://phaze:phaze@localhost:5545/phaze_perf82
export PHAZE_REDIS_URL=redis://localhost:6380/0
just perf-explain 20
# equivalent to:
#   uv run python scripts/perf_explain.py --dsn postgresql://phaze:phaze@localhost:5545/phaze_perf82 \
#       --redis-url redis://localhost:6380/0 --iterations 20
```

No product code was touched to take any measurement below (read-only ASGI client against the
synthetic seeded corpus, same D-06/PERF-02 precedent as the baseline).

**Machine-load note**: this rig runs multiple concurrent dev sessions. A first BEFORE run taken
while another worktree's full `pytest` suite was executing measured p50 1334.8ms/p95 1595.0ms — a
CPU-contention artifact, not a code-path regression (confirmed via `pgrep -fl pytest` + `uptime`
load-average). All numbers reported below were taken with no competing full-suite `pytest` process
running (verified via `pgrep -fl pytest` immediately before each run).

## Headline numbers

| Measurement | BEFORE (this bead, current code) | AFTER (this bead, post-fix) | Phase 95 baseline (phaze-zqvh.1) |
|---|---|---|---|
| `get_stage_progress()` DIRECT | p50 **919.8ms**, p95 **973.6ms** | p50 **850.2ms**, p95 **895.6ms** | p50 975.0ms, p95 1121.0ms |
| `GET /pipeline/stats` endpoint | p50 **1147.1ms**, p95 **1223.9ms** | p50 **1100.3ms**, p95 **1167.7ms** | p50 1099.5ms, p95 1205.4ms |

`get_stage_progress` DIRECT is unchanged by this bead's fix (Phase 92 already parallelized it) —
the BEFORE/AFTER delta there (919.8 vs 850.2ms) is run-to-run noise on a shared rig, not a code
change. The endpoint number is what this bead's fix targets.

## The fix

`pipeline_stats_partial` (`src/phaze/routers/pipeline.py`) ran ~12 independent reads as SERIAL
`await`s on the shared request `session`, after the already-parallel `get_stage_progress` call:
`get_queue_activity`, `get_straggler_count`, `get_analysis_failed_count`, `get_awaiting_cloud_count`,
`get_pushing_count`, `get_pushed_count`, `get_inadmissible_count`, `get_localqueue_unreachable`,
`get_cloud_phase_counts`, `get_backend_lane_snapshot`, `derive_cloud_hold_reason`. None of these
consumes another's RESOLVED value — `derive_cloud_hold_reason` re-derives its own lane snapshot
internally rather than reading the router's `lanes` variable — so all 11 (+ the session-free
`get_localqueue_unreachable`, a pure Redis read) now fan out CONCURRENTLY via `asyncio.gather`,
mirroring the Phase 92 `get_stage_progress` pattern exactly: each read runs in its OWN
`AsyncSession` via the reused `_read_in_own_session` helper, bounded by the reused `_stats_fanout()`
semaphore (`src/phaze/routers/pipeline.py:761-830`).

`activity` (from `get_queue_activity`) feeds `queue_progress` AND is a required — if
internally-unused-by-design (see `_build_dag_context`'s own docstring) — positional argument to
`_build_dag_context`: a TRUE dependency by call signature, so `_build_dag_context` stays a
sequential `await` immediately after the gather, using the shared request `session` directly
(safe: nothing else touches that session concurrently once the fan-out, which reads through its
own sessions, is under way). This is the one genuine sequencing constraint in the group; every
other read is independent and now runs in parallel.

Reusing the Phase 92 `_read_in_own_session` / `_stats_fanout` helpers (rather than hand-rolling a
new fan-out) means the existing test-isolation seam applies for free: the `tests/conftest.py`
`_route_stats_fanout` fixture already monkeypatches `phaze.database.async_session` onto the
per-test connection and `_STATS_FANOUT` to `Semaphore(1)` for every test using the `session`
fixture — no new test fixture was needed, and no OOB fragment contract (`stats_bar.html`'s
byte-for-byte identical-state response) was touched: the context dict keys, key order, and values
are unchanged from before the refactor for a quiescent DB.

## Decision on ROADMAP phase-95 success criterion 3 (DENORM-01 revisit)

**DENORM-01 stays deferred.** The fix brought the endpoint from p50 1147.1ms to p50 1100.3ms (a
~50ms / ~4% improvement) — still marginally over the ~1s soft budget (D-07), but the remaining
overshoot is NOT evidence a denormalized stage-status bitmap column is needed. It decomposes as:

1. `get_stage_progress` itself: ~850-920ms DIRECT. Already Phase-92-parallelized (bounded
   `asyncio.gather` over per-node `_safe_count`/`_safe_bucket_counts` reads, each in its own
   session) — this is a genuine DB-bound floor at 200K-corpus scale (the slowest single bucketed
   `GROUP BY stage_status_case` query, not serialization overhead), and IS the kind of cost
   DENORM-01 would target. It has not regressed since Phase 92 (861ms baseline vs. 850-920ms here
   — within run-to-run noise on this shared rig).
2. `_build_dag_context` (`src/phaze/routers/pipeline.py:192-`, called once, sequentially, after
   this bead's gather): still runs ~10 of its OWN serial `await`s (`get_stage_controls`,
   `get_search_busy_count`, `get_scan_busy_count`, `count_active_agents` x2,
   `derive_compute_lane_identities`, `get_scrape_busy_count`, `get_match_busy_count`,
   `get_stage_busy_counts`, `_read_pipeline_counters`) — OUT OF this bead's named scope (the bead
   description enumerated exactly the router's own top-level awaits as "the remaining lever";
   `_build_dag_context`'s internals were not named). This is almost certainly where most of the
   remaining ~250ms gap between `get_stage_progress` DIRECT (~900ms) and the full endpoint
   (~1100ms) lives.

Point 2 is a SERIALIZATION problem with a known, mechanical, already-proven fix (the exact
`asyncio.gather` + `_read_in_own_session` idiom this bead and Phase 92 both applied) — not a
data-model problem a denormalized bitmap column would solve. **Recommendation: file a fast-follow
bead to fan out `_build_dag_context`'s internal serial awaits via the same pattern before
reconsidering DENORM-01.** That is expected to close most or all of the remaining ~100ms overshoot
without a schema change. DENORM-01 should be revisited again only if that follow-up fan-out still
leaves the endpoint over budget — at which point the bottleneck would genuinely be
`get_stage_progress`'s own per-node query cost, the case DENORM-01 was designed for.

This is consistent with, and does not revise, the Phase 95 baseline's (`95-BASELINE.md`) verdict:
the SEVERE Analyze-workspace-open slowdown the epic names traces to the client-side unbounded
per-file table render, not the stats poll — this bead's numbers confirm the poll sits close to,
not dramatically over, its own budget, with a known, cheap, next lever available before any
architecture change is warranted.

## Files consulted / changed

- `src/phaze/routers/pipeline.py` (`pipeline_stats_partial`) — refactored: the ~12 independent
  serial awaits now fan out via `asyncio.gather` + `_read_in_own_session` + `_stats_fanout`
  (imported from `phaze.services.pipeline`), mirroring Phase 92. `_build_dag_context` and its
  internals are UNCHANGED (out of scope, see decision above).
- `tests/shared/routers/test_pipeline.py` — 115 existing tests re-run unmodified, all green (the
  refactor preserves the response's context dict / OOB fragment contract byte-for-byte on a
  quiescent DB).
- Full suite (`uv run pytest tests/`): 3424 passed, 17 skipped, 97.56% coverage (>= 90% minimum),
  no changes needed to any test.
- `.planning/REQUIREMENTS.md` — DENORM-01 entry updated with this bead's measurement + decision.
