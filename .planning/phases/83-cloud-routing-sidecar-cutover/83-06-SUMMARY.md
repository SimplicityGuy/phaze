---
phase: 83-cloud-routing-sidecar-cutover
plan: 06
subsystem: api
tags: [cloud-routing, sqlalchemy, postgres, drain, cloud_job, sidecar, for-update, skip-locked]

# Dependency graph
requires:
  - phase: 83-01
    provides: hold_awaiting_cloud() shared awaiting writer (the go-forward cloud_job(status='awaiting') writer)
  - phase: 83-02
    provides: migration 034 backfilling the existing AWAITING_CLOUD corpus with sidecar rows
  - phase: 83-05
    provides: trigger_analysis hold path cutover to hold_awaiting_cloud (every hold carries a sidecar row)
  - phase: 78
    provides: inflight_clause / domain_completed_clause SQL clause builders (the derivation layer)
  - phase: 81
    provides: FAILURE_IS_TERMINAL[ANALYZE]=True + domain_completed (the terminal-failure exclusion)
provides:
  - get_cloud_staging_candidates cut over to the cloud_job sidecar (D-05 conjunct, D-06 lock, D-07 FIFO/clock)
  - select_backend staleness clock reads the awaiting cloud_job.updated_at (Phase-90-durable)
  - get_awaiting_cloud_count re-anchored on the same drain clause (card and drain cannot disagree)
  - SC#3 two-tick double-dispatch HARD GATE integration test
affects: [80-recovery-re-enqueue-cutover, 82-read-cutover, 90-filestate-column-drop]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Drain candidacy = cloud_job(status='awaiting') INNER join + ~inflight_clause(ANALYZE) + ~domain_completed_clause(ANALYZE), FOR UPDATE OF cloud_job SKIP LOCKED"
    - "Staleness clock passed explicitly into a pure policy function (lane_entered_at) so it survives a dual-write removal"
    - "Count card derives from the identical clause builder the drain uses so display and routing cannot diverge"

key-files:
  created:
    - tests/integration/test_drain_double_dispatch.py
  modified:
    - src/phaze/services/pipeline.py
    - src/phaze/services/backend_selection.py
    - src/phaze/tasks/release_awaiting_cloud.py
    - tests/analyze/services/test_backend_selection.py
    - tests/analyze/services/test_backends.py
    - tests/analyze/core/test_staging_cron.py
    - tests/analyze/core/test_dispatch_snapshot.py
    - tests/analyze/tasks/test_release_awaiting_cloud.py
    - tests/shared/routers/test_pipeline.py
    - tests/shared/services/test_pipeline.py

key-decisions:
  - "D-05 predicate conjunct (not row deletion): survives the rolled-back-tick double-dispatch because the committed ledger row re-excludes the file"
  - "D-06 with_for_update(of=CloudJob) over an INNER join: lock the candidacy table so EvalPlanQual re-checks cloud_job.status"
  - "D-07 FIFO stays on FileRecord.created_at; staleness clock moves to cloud_job.updated_at, surfaced from the candidate query and passed into select_backend as lane_entered_at"
  - "D-15 get_awaiting_cloud_count re-anchored on the drain clause; get_pushing_count/get_pushed_count left untouched (unowned Phase-90 blocker)"

patterns-established:
  - "Compose inflight_clause/domain_completed_clause verbatim (DERIV-04); a COUNT reusing them must INNER-join FileRecord so the correlated ~exists resolves"

requirements-completed: [SIDECAR-01]

# Metrics
duration: 175min
completed: 2026-07-09
---

# Phase 83 Plan 06: Cloud-Routing Sidecar Drain Cutover Summary

**Cut the AWAITING_CLOUD drain reader off `FileRecord.state` onto the `cloud_job` sidecar — `status='awaiting'` INNER join + `~inflight_clause(ANALYZE)` + `~domain_completed_clause(ANALYZE)` under `FOR UPDATE OF cloud_job SKIP LOCKED` — gated behind the SC#3 two-tick double-dispatch HARD GATE, with the staleness clock moved to `cloud_job.updated_at` and the count card re-anchored on the identical clause.**

## Performance

- **Duration:** ~175 min
- **Started:** 2026-07-09T20:40:00Z
- **Completed:** 2026-07-09T21:35:00Z
- **Tasks:** 3
- **Files created/modified:** 11 (1 created, 10 modified) + deferred-items.md

## Accomplishments
- SC#3 (D-08) HARD GATE: `tests/integration/test_drain_double_dispatch.py` proves each file is dispatched exactly once and never to a cloud backend after a local dispatch, across (a) local dispatch, (b) rolled-back tick with a committed ledger row, (c) terminally-failed local analyze. Written RED (cases b/c failed the state-based drain), turned GREEN by the cutover.
- `get_cloud_staging_candidates` cut over (SC#1): no `FileRecord.state` predicate remains; INNER join to `cloud_job` on `status='awaiting'`, `~inflight_clause(ANALYZE)`, `~domain_completed_clause(ANALYZE)`, FIFO on `FileRecord.created_at`, `with_for_update(of=CloudJob, skip_locked=True)`; surfaces `cloud_job.updated_at` per candidate.
- `select_backend` staleness clock reads the passed `lane_entered_at` (the awaiting `cloud_job.updated_at`) instead of `file.updated_at` (D-07, durable past Phase 90's state-write removal); FIFO unchanged.
- `get_awaiting_cloud_count` re-anchored on the same drain clause so the card and the drain can never disagree; `get_pushing_count`/`get_pushed_count` left untouched.

## Task Commits

Each task was committed atomically:

1. **Task 1: SC#3 two-tick HARD GATE (RED)** — `745cb4b2` (test)
2. **Task 2: drain query cutover + staleness clock (GREEN)** — `d00c164d` (feat)
3. **Task 3: re-anchor get_awaiting_cloud_count (D-15)** — `4bedaf04` (feat)

**Deferred-items log:** `4c6b4018` (docs)

## Files Created/Modified
- `tests/integration/test_drain_double_dispatch.py` — SC#3 two-tick double-dispatch HARD GATE (3 cases)
- `src/phaze/services/pipeline.py` — `get_cloud_staging_candidates` cutover (D-05/D-06/D-07) + `get_awaiting_cloud_count` re-anchor (D-15)
- `src/phaze/services/backend_selection.py` — `select_backend` staleness clock on `lane_entered_at` (D-07); `file` param replaced
- `src/phaze/tasks/release_awaiting_cloud.py` — drain loop unpacks `(file, lane_entered_at)` tuples and passes the clock
- `tests/analyze/services/test_backend_selection.py` — updated `select_backend` call sites + signature test to the new param list
- `tests/analyze/services/test_backends.py` — `test_local_dispatch_excluded_from_staging_candidates` now seeds the awaiting row + a ledger row and asserts the awaiting row is retained
- `tests/analyze/core/test_staging_cron.py` — seed `cloud_job(status='awaiting')` for held files; `_StubBackend` promote-not-insert; D-03 re-stamp (FAILED→awaiting); CR-01 spill test seeds a ledger row
- `tests/analyze/core/test_dispatch_snapshot.py` — seed awaiting rows; golden `cloud_job_count` updated to 3 across all cells
- `tests/analyze/tasks/test_release_awaiting_cloud.py` — seed awaiting rows; `_StubBackend` UPDATE-promotes; `== {}` assertions → `== {None}`
- `tests/shared/routers/test_pipeline.py` — D-15 unit test + count-card tests seed awaiting rows
- `tests/shared/services/test_pipeline.py` — `get_awaiting_cloud_count` happy-path seeds awaiting rows
- `.planning/phases/83-cloud-routing-sidecar-cutover/deferred-items.md` — logged the backfill/`~inflight_clause` interaction

## Decisions Made
- Surfaced `cloud_job.updated_at` as an extra returned column of `get_cloud_staging_candidates` (return type `list[tuple[FileRecord, datetime]]`) rather than a second per-candidate read — the query already joins `cloud_job`, so it costs no extra round trip and keeps `_cloud_attempts_for` intact (the CR-02 safety-net test still monkeypatches it).
- Replaced `select_backend`'s `file` parameter with `lane_entered_at` (the only thing it read off `file` was `updated_at`); updated all unit call sites and the signature-invariant test.
- `get_awaiting_cloud_count` must INNER-join `FileRecord` for the correlated `~exists(... file_id == FileRecord.id)` clause builders to resolve; without the join `_safe_count` silently degraded the card to 0.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] `get_awaiting_cloud_count` count query needed a `FileRecord` join to correlate the clause builders**
- **Found during:** Task 3
- **Issue:** `COUNT(cloud_job) WHERE ~inflight_clause(ANALYZE) ...` references `FileRecord.id` inside its correlated `~exists`, but the count query had no `FileRecord` in its FROM — so `_safe_count` swallowed the error and returned 0 (card always empty).
- **Fix:** `.select_from(CloudJob).join(FileRecord, FileRecord.id == CloudJob.file_id)` (1:1 via `uq_cloud_job_file_id`).
- **Files modified:** src/phaze/services/pipeline.py
- **Verification:** D-15 unit test asserts count==1 for a parked file + ==drain candidate set; existing count-card tests green.
- **Committed in:** 4bedaf04 (Task 3 commit)

**2. [Rule 1 - Bug] Existing drain/dispatch/count tests seeded bare-`state` AWAITING_CLOUD files (no longer drain candidates)**
- **Found during:** Task 2 and Task 3 (analyze + shared buckets)
- **Issue:** 22 analyze tests + 3 shared tests seeded `state=AWAITING_CLOUD` files with no `cloud_job` row; the sidecar drain's INNER join no longer selects them, so they failed. The `_StubBackend`/isolation stubs also INSERTed a fresh `cloud_job` row that now collides with the seeded awaiting row (`uq_cloud_job_file_id`).
- **Fix:** Seed `cloud_job(status='awaiting')` for held files (the post-83 representation); make dispatch stubs UPSERT/UPDATE-promote the awaiting row instead of INSERT; re-stamp the D-03 "budget-spent" fixture from FAILED→awaiting; seed `process_file:<id>` ledger rows where the real `before_enqueue` hook would (the fakes don't); update golden `cloud_job_count` and `backend_id` assertions accordingly.
- **Files modified:** the 6 test files listed above.
- **Verification:** `just test-bucket analyze` (523 passed), `just test-bucket integration` (164 passed), `just test-bucket shared` (998 passed), all in isolation.
- **Committed in:** d00c164d (Task 2) and 4bedaf04 (Task 3)

---

**Total deviations:** 2 auto-fixed (both Rule 1 — test/query correctness required by the cutover). No scope creep; `get_pushing_count`/`get_pushed_count` deliberately untouched.
**Impact on plan:** All fixes necessary for the cutover to be correct and the suite to stay green.

## Threat Flags

| Flag | File | Description |
|------|------|-------------|
| threat_flag: mis-routing | src/phaze/routers/pipeline.py | `trigger_backfill_cloud` seeds a `process_file:<id>` recovery-ledger row for **compute**-target held files; the new `~inflight_clause(ANALYZE)` drain conjunct now excludes them, so a backfill-held compute file falls to local analysis via `recover_orphaned_work` instead of the compute backend. NOT stranded, and NOT in this plan's scope (routers/pipeline.py backfill is unowned; D-05 is locked). Logged in deferred-items.md `## 83-06` for the backfill/recovery owner. |

## Issues Encountered
- `_cloud_job_status` test helper originally called `session.expire_all()`, which evicted the still-referenced fixture `FileRecord` and produced a `MissingGreenlet` on a later `file.id` access — removed the expire (a scalar-column select always round-trips anyway).
- Case (b) of the SC#3 gate needed a genuine whole-tick rollback with an independently-committed ledger row; modelled by a `commit()`-raising session wrapper + a separate hook session (AsyncSession permits instance attribute assignment).

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- SC#1 met in the drain and count card; SC#3 HARD GATE green in the `integration` bucket in isolation. DERIV-04 equivalence test still green (clauses composed, not re-spelled).
- Phase 90 (FileState column drop) can remove the dual-written `file.state` without breaking the spill staleness clock (now on `cloud_job.updated_at`) or the drain/count (now on `cloud_job.status` + derived in-flight).
- Open item for the backfill/recovery owner: the compute-backfill `process_file` ledger seed vs. the `~inflight_clause` drain conjunct (deferred-items.md `## 83-06`).

---
*Phase: 83-cloud-routing-sidecar-cutover*
*Completed: 2026-07-09*

## Self-Check: PASSED
- Created files verified on disk: tests/integration/test_drain_double_dispatch.py, 83-06-SUMMARY.md
- Task commits verified in git log: 745cb4b2, d00c164d, 4bedaf04, 4c6b4018
