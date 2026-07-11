---
status: complete
phase: 82-counts-pending-set-cutover
source: [82-01-SUMMARY.md, 82-02-SUMMARY.md, 82-03-SUMMARY.md, 82-04-SUMMARY.md]
started: 2026-07-10T00:00:00Z
updated: 2026-07-10T00:00:00Z
---

## Current Test

[testing complete]

## Tests

> Backend / data-layer phase (no interactive UI beyond the `stats_bar.html` partial). Tests were
> RUN by the agent — each user-observable deliverable was exercised against a live Postgres
> (isolated per-run DB) via the real service functions, and PASS/FAIL recorded from observed
> behavior (driver: `scratchpad/uat_82.py`). T5/T6 verified via the passing automated suite +
> recorded evidence.

### 1. Cross-stage independence — the deadlock dissolves (READ-01)
expected: A file with metadata DONE (fingerprint + analyze not done) is ABSENT from the metadata pending set but STILL PRESENT in the fingerprint AND analyze pending sets — each enrich stage surfaces its own not-done files independent of the others.
result: pass
observed: metadata-done file → in metadata_pending=False, in fingerprint_pending=True, in analyze_pending=True. The cross-stage deadlock is gone.

### 2. A1 cloud double-dispatch guard
expected: A file with an active `cloud_job` (status `awaiting`) is ABSENT from the local analyze pending set even though otherwise eligible; a file whose cloud_job FAILED is re-admitted to the local analyze set.
result: pass
observed: awaiting-cloud file in local analyze_pending=False; failed-cloud file in local analyze_pending=True. No double-dispatch.

### 3. Failed analyze is terminal; failed metadata/fingerprint stay eligible (ELIG-03 / ELIG-04)
expected: A file whose analyze FAILED is ABSENT from the analyze pending set (never auto-re-enqueued — the 44.5K over-enqueue guard); a failed-metadata file STAYS in the metadata pending set and a failed-fingerprint file STAYS in the fingerprint pending set (auto-retryable).
result: pass
observed: failed-analyze in analyze_pending=False; failed-metadata in metadata_pending=True; failed-fingerprint in fingerprint_pending=True.

### 4. Four-bucket per-stage counts sum to total, with a visible failed count (READ-02)
expected: `get_stage_progress`'s three enrich nodes each return `{not_started, in_flight, done, failed, total}`; on a healthy corpus the four buckets SUM to total, and a failed file shows in the `failed` bucket (previously invisible).
result: pass
observed: on an isolated 3-file corpus (1 done, 1 failed, 1 not-started) the analyze node = `{not_started:1, in_flight:0, done:1, failed:1, total:3}` — four_sum=3=total, visible failed=1; all three enrich stages carry the four-bucket shape.

### 5. Degrade-safe poll + dashboard renders off derived counts (READ-02, D-04)
expected: `/pipeline/stats` returns derived counts (`discovered` / `metadataExtracted` / `analyzed`) via `_derive_stats` (no linear `get_pipeline_stats`); a forced DB error on ONE stage source degrades that stage to 0 WITHOUT 500ing the poll, and the dashboard stats bar still renders.
result: pass
observed: verified via the passing automated suite — `tests/analyze/core/test_stage_progress.py::test_single_source_db_error_degrades_to_zero` (degrade-to-zero, no raise) and `tests/shared/routers/test_pipeline.py` + `test_pipeline_stats.py` dashboard/OOB-render tests all green in the 262-pass merged-tree run. `def get_pipeline_stats` is absent from `src/`; `_derive_stats` re-expresses the seven consumed keys in `routers/pipeline.py`.

### 6. PERF-02 200K measurement + DENORM-01 decision recorded (deliverable)
expected: `82-VERIFICATION.md` contains the real 200K-scale EXPLAIN ANALYZE plans, endpoint/direct p50/p95 timings, index-scan evidence, and an explicit DENORM-01 go/no-go call.
result: pass
observed: VERIFICATION records p50 ~1.4s endpoint / ~1.29s direct (over the D-07 `<~1s` soft budget), real `EXPLAIN (ANALYZE, BUFFERS)` plans, honest "3 of 5 named indexes used" evidence, and a reasoned **DENORM-01 NO-GO/deferred** call (try `asyncio.gather` parallelization first). The measurement — not a latency target — is the deliverable, and it was produced.

## Summary

total: 6
passed: 6
issues: 0
pending: 0
skipped: 0

## Gaps

[none — all tests passed]

## Notes

- Tests 1–4 were driven live against isolated Postgres DBs via the real `get_metadata_pending_files` /
  `get_fingerprint_pending_files` / `get_discovered_files_with_duration` / `get_stage_progress` functions.
- Two deployment-gated items remain outside UAT scope (tracked in `82-VALIDATION.md` Manual-Only):
  the D-02 live-prod `analyzed`-NULL invariant (read-only lux probe at rollout ≥036) and the PERF-02
  200K bench as a recurring measurement. Neither is a code defect.
