---
phase: 31-windowed-time-series-audio-analysis
plan: 05
subsystem: agent-analysis-transport
tags: [saq, enqueue-policy, windows, import-boundary, restart-resilience]
requires: ["31-03", "31-04"]
provides:
  - "process_file forwards per-window time-series via AnalysisWritePayload.windows"
  - "process_file enqueue policy: timeout=14400 (4h bounded) + retries=2"
affects:
  - "src/phaze/tasks/functions.py"
  - "src/phaze/routers/pipeline.py"
  - "tests/_queue_fakes.py"
tech-stack:
  added: []
  patterns:
    - "Build wire payloads from plain analyze_file dicts (no ORM) to keep the D-25 import boundary"
    - "Explicit per-job SAQ control kwargs (timeout/retries) to escape apply_project_job_defaults clobber"
key-files:
  created: []
  modified:
    - "src/phaze/tasks/functions.py"
    - "src/phaze/routers/pipeline.py"
    - "tests/test_tasks/test_functions.py"
    - "tests/test_routers/test_pipeline.py"
    - "tests/_queue_fakes.py"
decisions:
  - "Amended plan timeout=0 -> timeout=14400 (4h bounded) for worker-restart resilience"
  - "Kept retries=2 (not 1) to stay in the locked 1-2 band and dodge the retries==1 -> 4 hook clobber"
  - "All process_file trigger endpoints funnel through one helper, so policy applied once covers every site"
metrics:
  duration: "~25m"
  completed: "2026-06-10"
  tasks: 2
  files: 5
requirements: [ANL-01]
---

# Phase 31 Plan 05: Wire Per-Window Output End-to-End + Bound Enqueue Churn Summary

`process_file` now transports the per-window time-series from `analyze_file` to the internal API via `AnalysisWritePayload.windows` (built from plain dicts, import boundary intact), and every `process_file` enqueue carries an explicit `timeout=14400` + `retries=2` so a single long/bad file no longer churns four full re-analyses.

## What Was Built

### Task 1 — windows payload forwarding (`tasks/functions.py`)
- `process_file` builds `windows = [AnalysisWindowPayload(**w) for w in analysis.get("windows", [])]` from `analyze_file`'s plain-dict return (Plan 04) and includes it in the `AnalysisWritePayload` PUT.
- When `analyze_file` omits the `windows` key, `windows` defaults to `[]`; aggregate fields (`bpm`/`musical_key`/`mood`/`style`/`danceability`/`energy`) are unchanged.
- Only a schema/pydantic import was added (`AnalysisWindowPayload`) — **no** `phaze.database` / `phaze.models` / `sqlalchemy` import. The D-25 import-boundary gate (`tests/test_task_split.py`) stays green.

### Task 2 — bounded enqueue policy (`routers/pipeline.py`)
- The single `_enqueue_analysis_jobs` helper (the funnel for both `/api/v1/analyze` and the HTMX `/pipeline/analyze`) now enqueues `process_file` with explicit `timeout=14400` and `retries=2`.
- `retries=2` is honored by `apply_project_job_defaults` (it only fills jobs still at the SAQ default `retries==1`, clobbering those to `worker_max_retries=4`). This kills the 4× re-analysis churn from the original long-file incident.

### Test infrastructure (`tests/_queue_fakes.py`)
- `FakeQueue` now mirrors `saq.Queue.enqueue`'s job-control key split (`k in Job.__dataclass_fields__`): the captured triple/`captured` holds only the task payload, and a new parallel `captured_policy` list holds the per-job control kwargs (`timeout`/`retries`). This keeps the existing `set(kwargs) == {five fields}` assertions valid while letting the new tests assert the enqueue policy.

## Deviations from Plan

### 1. [Orchestrator amendment] timeout=0 -> timeout=14400 (4h bounded)
- **Plan said:** enqueue `process_file` with `timeout=0` (unbounded), mirroring `pipeline_scans.py` (incident 260609-glv).
- **Amended to:** `timeout=14400` (4 hours, bounded), per the AUTHORITATIVE_PLAN_AMENDMENT driven by the Plan 31-01 spike numbers and an explicit restart-resilience requirement.
- **Why:** `timeout=0` (unbounded) means a worker that dies/restarts mid-file leaves an orphaned in-flight job SAQ can never reclaim (no timeout to trip). A bounded-generous 4h timeout lets SAQ reclaim a dead/restarted worker's job. The spike measured a 1.49h file at ~51min wall; a ~3h Coachella set runs well under 4h, so legitimate long files are not killed (the original `timeout=0` concern).
- **Files:** `src/phaze/routers/pipeline.py` (commit `9e04da7`); tests assert `timeout=14400` + `retries=2` (commit `1baf553`).
- **`retries=2` unchanged** from the plan (correctly avoids the `retries==1 -> 4` hook clobber).

### 2. [Plan grep amendment] "three enqueue sites" / `grep -c "timeout=0" >= 3`
- **Plan said:** the acceptance grep expects `timeout` at >= 3 `process_file` enqueue sites.
- **Reality:** there is exactly **one** real `queue.enqueue("process_file", ...)` call — in `_enqueue_analysis_jobs`. The plan's "l.67, l.92-96, l.221-225" are one enqueue plus two `resolve_queue_for_task` call points (which do not enqueue). All `process_file` trigger endpoints funnel through the single helper, so one policy change covers every enqueue site (the DRY, correct structure).
- **Resolution:** policy applied once at the funnel; `grep -c "timeout=14400"` is `1`. Both behavioral tests (`/api/v1/analyze` and `/pipeline/analyze`) independently assert the enqueued job carries `timeout=14400, retries=2`, proving "every site" is covered. The amendment explicitly authorized updating these greps.

### 3. [Test-double extension] FakeQueue job-control split
- Not in the plan, but required so the explicit `timeout`/`retries` kwargs (which real SAQ routes to the Job, not `job.kwargs`) are testable without breaking the existing flat-`kwargs` assertions. Mirrors real SAQ semantics. No production behavior change; shared across the six fake-queue consumers (only the policy capture is additive).

## Verification

- `uv run pytest tests/test_tasks/test_functions.py tests/test_task_split.py tests/test_routers/test_pipeline.py` — **52 passed**.
- `uv run mypy src/phaze/tasks/functions.py src/phaze/routers/pipeline.py` — clean.
- Import-boundary gate (`test_task_split.py`) — green (no ORM/database/sqlalchemy import in the worker graph).
- Coverage on changed source: `tasks/functions.py` 100%, `routers/pipeline.py` 88.3% (uncovered lines are pre-existing fingerprint endpoints, not this plan's changes); patch coverage of new lines is full.

## Threat Mitigations Applied

- **T-31-05-01 (DoS / retry churn):** `retries=2` (down from the hook's 4) + bounded `timeout=14400` stop the kill-then-restart-from-zero loop.
- **T-31-05-02 (import-boundary regression):** windows built from plain dicts, no ORM import; `test_task_split.py` gate stays green.
- **T-31-05-SC:** zero new packages.

## Deferred / Environmental

- `tests/test_services/test_agent_task_router.py` (7 tests) require a live Redis on `localhost:6379` and use real SAQ queues (not the `_queue_fakes` doubles). They fail in this sandbox with `redis.exceptions.ConnectionError` because Redis is not running. Pre-existing + environmental, **not** caused by this plan, **out of scope** — logged to `deferred-items.md`.

## TDD Gate Compliance

Both tasks followed RED -> GREEN:
- Task 1: `test(31-05)` `be4ad96` (RED) -> `feat(31-05)` `be269c0` (GREEN).
- Task 2: `test(31-05)` `1baf553` (RED) -> `feat(31-05)` `9e04da7` (GREEN).

## Commits

- `be4ad96` test(31-05): add failing tests for process_file windows forwarding
- `be269c0` feat(31-05): forward per-window time-series via AnalysisWritePayload.windows
- `1baf553` test(31-05): assert process_file enqueues with bounded timeout + retries=2
- `9e04da7` feat(31-05): bound process_file enqueue churn (timeout=14400, retries=2)
