---
phase: 38-pipeline-dag-pause-priority-ui-and-rescan-button-removal
plan: 03
subsystem: ui
tags: [htmx, alpinejs, jinja2, fastapi, sqlalchemy, dag-canvas, pipeline, saq]

# Dependency graph
requires:
  - phase: 37-per-stage-pause-and-priority-control-plane
    provides: "PipelineStageControl model (pipeline_stage_control table) + the /pipeline/stages/* control endpoints + STAGE_TO_FUNCTION constants"
  - phase: 35-pipeline-determinism-idempotency-observability
    provides: "the store-driven DAG canvas, the 5s /pipeline/stats OOB poll, _build_dag_context, the dag.items() seed loop, and the all-ints x-init invariant (T-35-11)"
provides:
  - "get_stage_controls(session): degrade-safe reader of the 3 pipeline_stage_control rows (returns paused=False/priority=50 defaults on any failure, never raises into the 5s poll)"
  - "_build_dag_context extended with 6 int keys: metadata/analyze/fingerprint Paused (0/1) + Priority"
  - "$store.pipeline seeded with the 6 new keys (0) so no control binding reads undefined pre-poll"
  - "GET /pipeline/stats emits a dag-seed-<key> OOB paragraph per new key via the existing dag.items() loop (zero stats_bar.html edit)"
affects: [38-02-dag-canvas-controls, pipeline-dashboard, dag-canvas]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Degrade-safe hot-poll reader: try -> warn -> guarded rollback -> defaults (mirrors _safe_count / get_queue_activity, T-38-DEGRADE)"
    - "Server-computed int-only x-init: paused coerced to int(0/1), never a Python bool (T-35-11 / Pitfall 3)"
    - "Single _NEW_STORE_KEYS tuple edit drives store-literal + int-key + OOB-seed tests"

key-files:
  created:
    - .planning/milestones/v4.0-phases/38-pipeline-dag-pause-priority-ui-and-rescan-button-removal/38-03-SUMMARY.md
  modified:
    - src/phaze/services/pipeline.py
    - src/phaze/routers/pipeline.py
    - src/phaze/templates/base.html
    - tests/test_pipeline_dag_context.py
    - README.md

key-decisions:
  - "get_stage_controls degrades to paused=False/priority=50 on any failure (mirrors _safe_count); _build_dag_context coerces paused to int 0/1 to hold the all-ints x-init invariant (T-35-11)"
  - "The 6 stage-control keys ride the existing dag.items() OOB loop with zero stats_bar.html edit; one _NEW_STORE_KEYS edit drives all three parametrized tests"

patterns-established:
  - "Never-500 control read in the 5s poll path: degrade owned by the reader, not by _build_dag_context (no try/except at the call site)"
  - "Prove threat-register mitigations directly: a fake-session test forces the except/rollback branch the empty-table happy path can't reach"

requirements-completed: [REQ-38-4]

# Metrics
duration: ~14min
completed: 2026-06-13
---

# Phase 38 Plan 03: Per-Stage Pause/Priority Live-State Substrate Summary

**Plumbed degrade-safe per-stage {paused, priority} into the existing 5s /pipeline/stats OOB poll: `get_stage_controls` reader + 6 int keys in `_build_dag_context` + 6 seeded `$store.pipeline` keys, all riding the existing `dag.items()` loop with zero template edits.**

## Performance

- **Duration:** ~14 min
- **Started:** 2026-06-13T20:45:10Z
- **Completed:** 2026-06-13T20:59:18Z
- **Tasks:** 3 (TDD on Task 1)
- **Files modified:** 5 (+1 SUMMARY created)

## Accomplishments
- `get_stage_controls(session)` — a never-500 reader of the 3 `pipeline_stage_control` rows that degrades to `{paused: False, priority: 50}` for all three agent stages on any failure (missing table, DB hiccup), mirroring the repo-wide `_safe_count` / `get_queue_activity` discipline (T-38-DEGRADE).
- `_build_dag_context` now overlays six integer keys — `metadata/analyze/fingerprint` `Paused` (int 0/1) and `Priority` — preserving the canvas's "every dag value is a server-computed int safe to interpolate into `x-init`" invariant (T-35-11 / Pitfall 3). No try/except at the call site: the degrade lives in the reader.
- `$store.pipeline` (base.html) seeded the six new keys to `0` so no control binding reads `undefined` before the first poll tick.
- The six keys propagate to `GET /pipeline/stats` as `dag-seed-<key>` OOB paragraphs through the existing `dag.items()` loop — **zero `stats_bar.html` edit** (verified by the OOB-seed test).
- README documents the new DAG pause/resume toggle + priority stepper, the authoritative-only after-request store write, the degrade-safe defaults, and the removed Rescan anchor.

## Task Commits

Each task was committed atomically:

1. **Task 1 (TDD): degrade-safe get_stage_controls reader**
   - `f526d21` (test) — RED: failing degrade test (KeyError on `metadataPaused`)
   - `2b842a7` (feat) — GREEN: `get_stage_controls` + wired 6 dag keys into `_build_dag_context`
2. **Task 2: extend _build_dag_context + base.html store literal** - `0243984` (feat) — the 6 `$store.pipeline` keys (router half already landed in Task 1 GREEN; see Deviations)
3. **Task 3: extend _NEW_STORE_KEYS + assert OOB propagation + README** - `e10a7e4` (test/docs)

**Follow-up test hardening:** `b6f2ba3` (test) — fake-session except-branch + row-overlay tests (see Deviations)

## Files Created/Modified
- `src/phaze/services/pipeline.py` - Added `_DEFAULT_CONTROLS` + the degrade-safe `get_stage_controls` reader.
- `src/phaze/routers/pipeline.py` - Imported `get_stage_controls`; `_build_dag_context` overlays the 6 int stage-control keys.
- `src/phaze/templates/base.html` - Seeded the 6 new keys (0) into the `Alpine.store('pipeline', {...})` literal.
- `tests/test_pipeline_dag_context.py` - Extended `_NEW_STORE_KEYS` (+6); added the `_build_dag_context` degrade test, a fake-session except-branch test, and a row-overlay test.
- `README.md` - Documented the DAG controls + Rescan-anchor removal.

## Decisions Made
- **Degrade owned by the reader, not the call site:** `get_stage_controls` is the single try/except; `_build_dag_context` adds none. Keeps the never-500 contract in one place.
- **`paused` emitted as int 0/1:** coerced via `int(...)` so Jinja never interpolates a Python `True`/`False` (capital-T, invalid JS) into `x-init` — preserves the T-35-11 invariant.
- **Loop variable renamed `stage_name`:** avoided shadowing the existing `stage = await get_stage_progress(...)` binding (mypy assignment error).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Router wiring landed in Task 1's GREEN commit (not Task 2)**
- **Found during:** Task 1 (TDD GREEN)
- **Issue:** The plan places `get_stage_controls` (Task 1) and the `_build_dag_context` wiring (Task 2) in separate tasks, but Task 1's verify (`uv run pytest tests/test_pipeline_dag_context.py -x`) exercises the degrade test which calls `_build_dag_context` and asserts `dag["metadataPaused"] == 0` / `dag["metadataPriority"] == 50`. Those keys do not exist until the router is wired, so Task 1 could not go GREEN without the wiring.
- **Fix:** Added the import + the 6-key overlay loop in `routers/pipeline.py` as part of Task 1 GREEN (the minimal code to pass the test). Task 2 therefore carried only the `base.html` store-literal edit. mypy-clean and verify-green at every step.
- **Files modified:** src/phaze/routers/pipeline.py
- **Verification:** `uv run mypy` clean on both modules; degrade test passes.
- **Committed in:** `2b842a7` (Task 1 GREEN)

**2. [Rule 1 - Bug] mypy variable-shadowing error on the overlay loop**
- **Found during:** Task 1 (GREEN verify)
- **Issue:** The overlay loop initially used `for stage in (...)`, shadowing the earlier `stage = await get_stage_progress(session)` (a dict) — mypy: "Incompatible types in assignment".
- **Fix:** Renamed the loop variable to `stage_name`.
- **Files modified:** src/phaze/routers/pipeline.py
- **Verification:** `uv run mypy src/phaze/routers/pipeline.py src/phaze/services/pipeline.py` → Success.
- **Committed in:** `2b842a7` (Task 1 GREEN)

**3. [Rule 2 - Missing Critical] Added tests that actually prove the T-38-DEGRADE mitigation + overlay behavior**
- **Found during:** Task 3 (coverage review)
- **Issue:** The plan's interfaces note assumed the test DB lacks `pipeline_stage_control`, so the `_build_dag_context` degrade test would hit the except branch. In this repo's fixture the table EXISTS but is empty, so that test only exercised the zero-rows happy path — leaving the actual T-38-DEGRADE except/rollback branch and the row-overlay loop (both mandated by Task 1's `<behavior>`) untested.
- **Fix:** Added `test_get_stage_controls_degrades_on_db_error` (fake session whose `execute` raises → asserts defaults returned + rollback called + no raise) and `test_get_stage_controls_overlays_present_rows` (a seeded `analyze` row overlays; absent stages keep defaults).
- **Files modified:** tests/test_pipeline_dag_context.py
- **Verification:** Full suite — `routers/pipeline.py` 100%, `services/pipeline.py` 97.62% (only the rollback-itself-fails edge at 309-310 uncovered, in parity with the existing `_safe_count` idiom).
- **Committed in:** `b6f2ba3`

---

**Total deviations:** 3 auto-fixed (1 blocking, 1 bug, 1 missing-critical-test)
**Impact on plan:** All necessary for a green, mypy-clean, behavior-proven result. No scope creep — `stats_bar.html` was confirmed to need no edit (the OOB loop propagates the keys for free), exactly as the plan predicted.

## Issues Encountered
- The unit/integration tests require Postgres; the local dev DB on 5432 was down. Started the project's ephemeral test DB via `just test-db` (ports 5433/6380), ran all verification against it, and tore it down with `just test-db-down`.

## Verification
- `uv run pytest tests/test_pipeline_dag_context.py -x` → 15 passed.
- `uv run mypy src/phaze/routers/pipeline.py src/phaze/services/pipeline.py` → Success, no issues.
- Full suite: **1743 passed**, 0 failed; touched-module coverage `routers/pipeline.py` 100%, `services/pipeline.py` 97.62%, TOTAL 97.55% (≥85%).
- Pre-commit hooks (ruff, ruff-format, bandit, mypy, file hygiene) passed on every commit — no `--no-verify`.

## User Setup Required
None - no external service configuration required (no new package, `uv.lock` unchanged).

## Next Phase Readiness
- The read-only data substrate the DAG pause/priority controls bind to is live: the 6 store keys, the OOB seeds, and the degrade-safe reader are all in place.
- Plan 38-02 (the DAG-canvas control macro + Rescan-anchor markup, `NODE_LAYOUT` recompute) binds to these `$store.pipeline.<stage>Paused/Priority` keys — they are now seeded and poll-refreshed.
- Functional dependency: the controls only mutate state once Phase 37's endpoints + table are live (merged on this branch). The reader degrades harmlessly if the table is ever unreadable.

## Self-Check: PASSED
- `src/phaze/services/pipeline.py` (get_stage_controls) — FOUND
- `src/phaze/routers/pipeline.py` (6-key overlay) — FOUND
- `src/phaze/templates/base.html` (6 seeded keys) — FOUND
- Commits `f526d21`, `2b842a7`, `0243984`, `e10a7e4`, `b6f2ba3` — all present in `git log`

---
*Phase: 38-pipeline-dag-pause-priority-ui-and-rescan-button-removal*
*Completed: 2026-06-13*
