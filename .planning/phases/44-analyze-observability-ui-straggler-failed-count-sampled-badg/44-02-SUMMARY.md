---
phase: 44-analyze-observability-ui-straggler-failed-count-sampled-badg
plan: 02
subsystem: services/pipeline
tags: [observability, saq, degrade-safe, straggler, analysis-failed]
requires:
  - FileState.ANALYSIS_FAILED (Phase 43)
  - saq_jobs Postgres broker table (Phase 36)
  - _safe_count / get_stage_busy_counts SAVEPOINT degrade idioms (pre-existing)
provides:
  - get_straggler_count(session, threshold_sec) -> int
  - get_analysis_failed_count(session) -> int
  - get_analysis_failed_files(session) -> list[FileRecord]
  - _job_started_ms(blob) helper (parses default-json SAQ job blob)
  - settings.straggler_threshold_sec config knob
affects:
  - Plan 04 (router wiring + dashboard templates consume these reads)
tech-stack:
  added: []
  patterns:
    - "Shared Pattern A — never-500 SAVEPOINT (begin_nested) degrade for the hot 5s /pipeline/stats poll"
    - "Shared Pattern B — static text() SQL, no interpolated operator input; threshold compared post-deserialize as a Python int"
    - "Python-side blob deserialization for job age (saq_jobs has no started SQL column)"
key-files:
  created: []
  modified:
    - src/phaze/config.py
    - src/phaze/services/pipeline.py
    - tests/test_services/test_pipeline.py
decisions:
  - "Straggler age read in Python from the deserialized job blob's started (epoch ms), NOT via SQL — saq_jobs has no started/touched column (PATTERNS.md banner / D-01)"
  - "_job_started_ms parses the default-json blob dict directly instead of constructing a saq.Job — Queue.deserialize needs the live queue and raises on queue-name mismatch; only started is needed"
  - "straggler_threshold_sec default 6600 (tied to analysis_inner_timeout_sec), lt=86400 (one-day cap)"
  - "ANALYSIS_FAILED kept OUT of PIPELINE_STAGES (D-02) — its own bucket, not a linear stage; adding it would double-count in the bar"
  - "Straggler SQL filters status='active' only (running), distinct from the busy gates that count queued+active"
metrics:
  duration: ~10 min
  completed: 2026-06-18
  tasks: 2
  files: 3
  tests_added: 11
---

# Phase 44 Plan 02: Straggler + ANALYSIS_FAILED Service Reads Summary

Degrade-safe data-layer reads that surface Phase 43's analysis outcomes for the pipeline dashboard:
a STRAGGLER count (long-running in-flight `process_file` jobs from `saq_jobs`, age computed in Python)
and an ANALYSIS_FAILED count + list (from the indexed `files.state`). Both feed the hot 5s
`/pipeline/stats` poll and follow the never-500 SAVEPOINT degrade discipline. Router wiring and
templates land in Plan 04.

## What Was Built

**Task 1 — ANALYSIS_FAILED reads + config knob (commit 75f02c5)**
- `config.py`: `straggler_threshold_sec` Field (default 6600, `gt=0`, `lt=86400`, `PHAZE_STRAGGLER_THRESHOLD_SEC`), mirroring the `analysis_inner_timeout_sec` Field shape.
- `pipeline.py`: `get_analysis_failed_files` (one-liner reuse of `get_files_by_state`) and `get_analysis_failed_count` (poll-safe `func.count` wrapped in `_safe_count`, mirroring the ANALYZED-count precedent).
- `ANALYSIS_FAILED` deliberately kept out of `PIPELINE_STAGES` (D-02).
- Tests: failed-count happy path, failed-list returns those rows, count degrades to 0 on a forced read error, guard that `ANALYSIS_FAILED not in PIPELINE_STAGES`.

**Task 2 — straggler count read from saq_jobs (commit 9f2d45e + test commit e481073)**
- `pipeline.py`: `get_straggler_count(session, threshold_sec)` — static SQL (`_STRAGGLER_ACTIVE_SQL`) selects ONLY the bounded active `process_file` BYTEA set (`status = 'active' AND split_part(key, ':', 1) = 'process_file'`), then `_job_started_ms` reads `started` (epoch ms) from each default-json blob; counts jobs where `(now_ms - started)/1000 > threshold_sec`. Wrapped in `session.begin_nested()` SAVEPOINT, returns 0 / logs `straggler_degraded` on any error, never raises.
- A missing / None / non-positive / non-int `started` is treated as not-yet-old (not counted).
- Tests: over/under-threshold + no-started happy path, zero-when-empty, degrade-on-error, no-poison-session, plus a dedicated `_job_started_ms` malformed-blob unit test (invalid JSON, non-dict, missing/0/non-int started, non-blob input, already-a-dict input).

## How It Works

`saq_jobs` (Postgres broker, Phase 36) has NO `started`/`touched` SQL column — SAQ stores `started`
(epoch milliseconds, `saq.utils.now()`) inside the serialized `job` BYTEA blob. A SQL
`WHERE now() - started > threshold` filter is therefore impossible. The straggler reader instead
selects only the small active `process_file` blob set, deserializes each in Python the same way SAQ
does on its default `json` serializer (the project passes no custom `dump`/`load` to
`build_pipeline_queue`), reads the top-level `started` int, and applies the threshold as a plain
Python comparison. The threshold is never interpolated into SQL (T-44-05).

Both new `saq_jobs`/`files` reads use the SAVEPOINT (`begin_nested`) degrade rather than
`session.rollback()` so a DB hiccup recovers the aborted Postgres transaction without expiring the
dashboard's already-loaded ORM objects (T-44-04) — the never-500 discipline for the 5s poll.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Robustness] Added `_job_started_ms` malformed-blob unit test (commit e481073)**
- **Found during:** Task 2 coverage check.
- **Issue:** The defensive branches in `_job_started_ms` (non-JSON, non-dict, missing/non-positive `started`) were uncovered. `saq_jobs` is an external broker table; a corrupt/legacy blob must degrade to "not countable" rather than crash the hot poll.
- **Fix:** Added a focused unit test exercising every defensive branch.
- **Files modified:** `tests/test_services/test_pipeline.py`
- **Commit:** e481073

No other deviations — plan executed as written.

## Verification

- `uv run pytest tests/test_services/test_pipeline.py -q` → 51 passed
- `uv run pytest -k "straggler"` → 5 passed; `-k "failed or analysis_failed"` → 6 passed
- `uv run mypy src/phaze/services/pipeline.py src/phaze/config.py` → clean
- `uv run ruff check` (both modules + tests) → clean
- Coverage on `src/phaze/services/pipeline.py`: 89.61% (≥85% threshold); the new straggler code is fully covered, remaining misses are pre-existing untouched lines.
- All pre-commit hooks passed on every commit (no `--no-verify`).

Note: the DB-backed tests require Postgres; run them with the ephemeral test DB
(`just test-db`, then `TEST_DATABASE_URL=postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test`).

## Authentication Gates

None.

## Known Stubs

None — these are real data-layer reads wired to `saq_jobs` and the indexed `files.state`. Consumption
(router context + dashboard templates) is intentionally deferred to Plan 04 per the plan's "data layer only" scope.

## Self-Check: PASSED

- src/phaze/config.py — FOUND (straggler_threshold_sec present)
- src/phaze/services/pipeline.py — FOUND (get_straggler_count, get_analysis_failed_count, get_analysis_failed_files present)
- tests/test_services/test_pipeline.py — FOUND
- Commit 75f02c5 — FOUND
- Commit 9f2d45e — FOUND
- Commit e481073 — FOUND
