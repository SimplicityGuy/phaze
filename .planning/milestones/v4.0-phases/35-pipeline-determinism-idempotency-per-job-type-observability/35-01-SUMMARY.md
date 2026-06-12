---
phase: 35-pipeline-determinism-idempotency-per-job-type-observability
plan: 01
subsystem: infra
tags: [saq, redis, deterministic-key, idempotency, before_enqueue, after_process, counters, dedup]

# Dependency graph
requires:
  - phase: 32-reboot-resilience
    provides: process_file deterministic-key fix (process_file:<file_id>) + analysis_enqueue.process_file_job_key template
  - phase: 30-queue-routing
    provides: enqueue_router CONTROLLER_TASKS / AGENT_TASKS routable-task universe
provides:
  - Central apply_deterministic_key before_enqueue hook with 8-entry _KEY_BUILDERS registry (keys every routable task <function>:<natural_id>)
  - increment_completed after_process hook wired into both worker settings dicts
  - pipeline_counters service (durable Redis INCR enqueued/completed counters + read_counters MGET helper)
  - Loud drift-guard test enforcing a deterministic key (or documented exemption) for every routable task
  - D-06 removal of both auto extract_file_metadata paths (agent file-upsert + legacy ingestion scan)
affects: [35-02-idempotent-upsert, 35-03-stage-progress-reconcile, 35-04-observability-ui]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Central before_enqueue chokepoint: key construction + enqueued-counter INCR folded into one hook so no call site can drift to a random-uuid key"
    - "after_process Worker-kwarg hook for terminal-outcome counters (not a register_* call)"
    - "Drift-guard test over CONTROLLER_TASKS u AGENT_TASKS with a documented _UNKEYED_TASKS allow-list"
    - "Durable (no-EXPIRE) Redis counters as a fast cache; DB reconcile is the rendering authority (D-03)"

key-files:
  created:
    - src/phaze/tasks/_shared/deterministic_key.py
    - src/phaze/services/pipeline_counters.py
    - tests/test_deterministic_key.py
    - tests/test_pipeline_counters.py
    - tests/test_no_auto_metadata_enqueue.py
  modified:
    - src/phaze/main.py
    - src/phaze/services/agent_task_router.py
    - src/phaze/tasks/controller.py
    - src/phaze/tasks/agent_worker.py
    - src/phaze/routers/agent_files.py
    - src/phaze/services/ingestion.py
    - tests/_queue_fakes.py
    - tests/test_main_lifespan.py
    - tests/test_routers/test_agent_files.py
    - tests/test_routers/test_agent_files_batch_id.py
    - tests/test_tasks/test_metadata_extraction.py

key-decisions:
  - "process_file builder computes the IDENTICAL process_file:<file_id> string as the Phase-32 template so the existing keyed path stays a dedup no-op-equivalent"
  - "generate_proposals keyed by order-independent sha256 batch-hash of sorted file_ids (per-file idempotency owned by 35-02, not the key)"
  - "enqueued counter increment folded into the key hook (PRE-dedup) -- accepted monotonic upward drift; enqueued is a non-authoritative soft hint only (W3)"
  - "completed counter is a deliberate reconcile/backstop cache satisfying D-02; no node renders it directly (DB-truth renders done per D-03) (W4)"
  - "ingestion.run_scan retains the now-unused queue param (noqa ARG001) for caller/signature stability rather than touching routers/scan.py (minimal blast)"
  - "agent_files FileUpsertResponse.enqueued field retained for schema stability, always 0"

patterns-established:
  - "Deterministic-key drift guard: any future routable task without a _KEY_BUILDERS entry or _UNKEYED_TASKS exemption fails a test loud"
  - "Best-effort counter hooks: a Redis hiccup during INCR is swallowed (logged), never blocks enqueue or job teardown"

requirements-completed: [SCHED, MANUAL-META, OBSERV]

# Metrics
duration: 28min
completed: 2026-06-12
---

# Phase 35 Plan 01: Pipeline-Wide Deterministic Keys, Maintained Counters & Manual-Only Metadata Summary

**Central before_enqueue deterministic-key hook (8-function registry, batch-hash for generate_proposals) wired on all four SAQ seams, maintained Redis enqueued/completed counters via a folded INCR + after_process hook, a loud drift-guard test, and removal of both auto extract_file_metadata paths (D-06).**

## Performance

- **Duration:** 28 min
- **Started:** 2026-06-12T01:36:43Z
- **Completed:** 2026-06-12T02:04:36Z
- **Tasks:** 3
- **Files modified:** 16 (5 created, 11 modified)

## Accomplishments
- Generalized the Phase-32 `process_file` deterministic-key fix to the whole pipeline, enforced CENTRALLY at the single `before_enqueue` chokepoint so no call site can drift back to a random-uuid key (D-05). Re-enqueue of a live `(function, natural_id)` now dedups to a no-op.
- Maintained Redis counters (`phaze:pipeline:enqueued:<fn>` / `phaze:pipeline:completed:<fn>`) backing the per-job-type progress UI (D-02), reconcilable against DB-truth on read (D-03). `enqueued` folded into the key hook; `completed` bumped by an `after_process` hook wired into both worker settings dicts.
- Drift-guard test over `CONTROLLER_TASKS u AGENT_TASKS` with a documented `_UNKEYED_TASKS` allow-list (refresh_tracklists, scan_directory, execute_approved_batch) — a new routable task missing a key builder fails loud.
- Removed BOTH auto `extract_file_metadata` enqueue paths (agent file-upsert + legacy ingestion scan) — metadata extraction is now operator-triggered only (D-06 / MANUAL-META), with regression guards.

## Task Commits

1. **Task 1: Central key hook + counters service + completion hook** - `5dc2816` (feat)
2. **Task 2: Wire on 4 seams + after_process + drift-guard test** - `97083d4` (test, RED) → `d45ba4b` (feat, GREEN)
3. **Task 3: Remove both auto metadata-extraction paths (D-06)** - `dffd13f` (feat)
4. **Coverage hardening: best-effort counter-failure swallow** - `a0e4914` (test)

## Files Created/Modified
- `src/phaze/tasks/_shared/deterministic_key.py` (created) - `apply_deterministic_key` before_enqueue hook, 8-entry `_KEY_BUILDERS`, `_hash_ids` batch helper, `increment_completed` after_process hook
- `src/phaze/services/pipeline_counters.py` (created) - durable `incr_enqueued`/`incr_completed` + `read_counters` MGET over the 8 known functions
- `tests/test_deterministic_key.py` (created) - per-function key + batch-hash + override + best-effort + drift-guard tests
- `tests/test_pipeline_counters.py` (created) - counter namespace + read_counters merge tests
- `tests/test_no_auto_metadata_enqueue.py` (created) - MANUAL-META regression guards for both removed paths
- `src/phaze/main.py`, `services/agent_task_router.py`, `tasks/controller.py`, `tasks/agent_worker.py` (modified) - register `apply_deterministic_key` (4 seams) + `after_process=increment_completed` (2 workers)
- `src/phaze/routers/agent_files.py` (modified) - deleted auto-enqueue block + dead imports; `enqueued` always 0
- `src/phaze/services/ingestion.py` (modified) - deleted D-09 auto-enqueue loop; `queue` param retained unused
- `tests/_queue_fakes.py` (modified) - additive `FakeRedis` async double
- `tests/test_main_lifespan.py`, `test_routers/test_agent_files.py`, `test_routers/test_agent_files_batch_id.py`, `test_tasks/test_metadata_extraction.py` (modified) - updated to the two-hook + no-enqueue contract

## Decisions Made
- `process_file` builder emits the identical `process_file:<file_id>` string as the Phase-32 template, keeping the existing keyed path a dedup no-op-equivalent.
- `generate_proposals` keyed by order-independent sha256 of sorted `file_ids`.
- `enqueued` counter drift (PRE-dedup INCR) is ACCEPTED as documented — non-authoritative soft hint only (W3).
- `completed` counter is a deliberate reconcile/backstop cache (W4), satisfying D-02 while DB owns rendering (D-03).
- `ingestion.run_scan` keeps the now-unused `queue` param (noqa ARG001) for caller/signature stability instead of expanding the change into `routers/scan.py`.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated lifespan + agent_files tests to the new contract**
- **Found during:** Task 2 (wiring) and Task 3 (D-06 removal)
- **Issue:** `test_main_lifespan` asserted the controller queue registers exactly ONE before_enqueue hook (now two); several DB-integration tests in `test_agent_files.py` / `test_agent_files_batch_id.py` and the `test_run_scan_auto_enqueues_extraction` test asserted the OLD auto-enqueue contract removed by D-06.
- **Fix:** Updated the lifespan assertion to verify both hooks are registered; inverted/renamed the auto-enqueue tests to assert NO enqueue + `enqueued == 0`; deleted the obsolete `test_run_scan_auto_enqueues_extraction` (its inverse now lives in `test_no_auto_metadata_enqueue.py`).
- **Files modified:** tests/test_main_lifespan.py, tests/test_routers/test_agent_files.py, tests/test_routers/test_agent_files_batch_id.py, tests/test_tasks/test_metadata_extraction.py
- **Verification:** `uv run pytest -m "not integration"` green (1097 passed); edited integration files collect cleanly.
- **Committed in:** d45ba4b (lifespan) and dffd13f (agent_files / batch_id / metadata-extraction)

**2. [Rule 2 - Missing Critical] Added best-effort counter-failure tests**
- **Found during:** Task 2 verification (coverage review)
- **Issue:** The documented "never block the enqueue / job teardown on a counter hiccup" contract was untested.
- **Fix:** Added tests asserting `apply_deterministic_key` and `increment_completed` swallow a Redis `incr` exception (key still set / no raise). Module coverage rose to 97%.
- **Files modified:** tests/test_deterministic_key.py
- **Verification:** `uv run pytest tests/test_deterministic_key.py` green; coverage 97%.
- **Committed in:** a0e4914

---

**Total deviations:** 2 auto-fixed (1 bug — contract-aligned test updates, 1 missing-critical test).
**Impact on plan:** Test updates were mandatory consequences of the planned source changes (two-hook registration + D-06 removal). No scope creep into production behavior; `routers/scan.py` deliberately left untouched per minimal-blast.

## Issues Encountered
- No Redis/Postgres in the execution sandbox: `@pytest.mark.integration` tests (auto-marked when they need external services) are deselected here (579 deselected). The non-integration suite (1097 tests) is fully green, `uv run mypy .` and `uv run ruff check .` are clean, and the edited integration files collect without error. The integration assertions were updated to the new contract for CI (which provides the services).

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- `read_counters` + the maintained counters are ready for 35-03/35-04 reconcile-on-read consumption.
- The drift-guard guarantees any new routable task added in later plans is forced to declare a key (or an explicit exemption).
- 35-02 owns per-file idempotency in the proposals/file upsert (the `generate_proposals` batch-hash key intentionally does NOT provide per-file idempotency).

## Self-Check: PASSED

- Created files all exist on disk (deterministic_key.py, pipeline_counters.py, test_deterministic_key.py, test_pipeline_counters.py, test_no_auto_metadata_enqueue.py).
- All task commits present in git log: 5dc2816, 97083d4, d45ba4b, dffd13f, a0e4914.
- Acceptance greps satisfied: `grep -c extract_file_metadata src/phaze/routers/agent_files.py` == 0; ingestion non-comment enqueue == 0; 4 seams register `apply_deterministic_key` (1 each); `after_process` present in both workers.

---
*Phase: 35-pipeline-determinism-idempotency-per-job-type-observability*
*Completed: 2026-06-12*
