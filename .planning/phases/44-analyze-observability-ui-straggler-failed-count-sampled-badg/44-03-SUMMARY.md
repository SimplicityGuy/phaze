---
phase: 44-analyze-observability-ui-straggler-failed-count-sampled-badg
plan: 03
subsystem: pipeline-router
tags: [fastapi, htmx, saq, deepen, analysis, caps, enqueue-routing]
requires:
  - "44-01 enqueue_process_file(..., fine_cap=, coarse_cap=) cap pass-through"
  - "ProcessFilePayload.fine_cap / coarse_cap optional int|None fields"
  - "enqueue_router.resolve_queue_for_task('process_file', ...) + NoActiveAgentError"
  - "analysis._stride_to_cap (cap<=0 -> analyze ALL windows no-op, Phase 43-04)"
provides:
  - "POST /pipeline/files/{file_id}/deepen control-plane re-analyze endpoint (HTMX fragment)"
  - "deepen_response.html fragment (queued / no-active-agent / not-found states)"
affects:
  - "Plan 04 sampled-file UI (will wire a deepen button hx-post to this endpoint)"
tech-stack:
  added: []
  patterns:
    - "Single-resource path-param re-trigger (load by uuid -> resolve queue -> enqueue -> fragment), analog to tracklists.rescrape_tracklist"
    - "process_file routing + NoActiveAgentError + cast('str', routed.agent_id) idiom (analog to trigger_analysis_ui)"
    - "Funnel through enqueue_process_file for full payload + deterministic dedup key"
    - "# noqa: TC003 on a runtime-resolved FastAPI path-param annotation import (PEP 649 / get_type_hints)"
key-files:
  created:
    - src/phaze/templates/pipeline/partials/deepen_response.html
  modified:
    - src/phaze/routers/pipeline.py
    - tests/test_routers/test_pipeline.py
decisions:
  - "fine_cap=0/coarse_cap=0 (not a separate 'deepen' flag) is the unbounded sentinel — reaches _stride_to_cap as the analyze-ALL-windows no-op (D-04)"
  - "Endpoint loads the FileRecord directly (no state filter) so an already-ANALYZED sampled file is re-deepenable; in-flight repeats dedup via the process_file:<file_id> key (D-05)"
  - "uuid imported at runtime with # noqa: TC003 because FastAPI resolves the file_id: uuid.UUID path-param annotation at runtime; sibling routers avoid the flag only because they also use uuid in runtime positions"
metrics:
  duration: ~25 min
  completed: 2026-06-18
  tasks: 2
  files: 3
---

# Phase 44 Plan 03: Deepen-Analysis Control-Plane Endpoint Summary

Added `POST /pipeline/files/{file_id}/deepen` — a single-file re-analyze re-trigger that loads the `FileRecord`, resolves the per-agent `process_file` queue via `enqueue_router`, and re-enqueues through the Plan-01-extended `enqueue_process_file(..., fine_cap=0, coarse_cap=0)`. The `0/0` sentinel reaches `_stride_to_cap` (43-04) as the analyze-ALL-windows no-op, so a Phase-43-sampled file gets re-analyzed at full window budget on demand. The single funnel inherits the Phase-30 per-agent routing (never the consumer-less default queue), the v4.0.8 full `ProcessFilePayload`, and the deterministic `process_file:<file_id>` dedup key.

## What Was Built

- **Task 1** — `deepen_analysis` endpoint in `routers/pipeline.py`. Loads `FileRecord` by typed `uuid.UUID` path param (`scalar_one_or_none`); a well-formed but unknown id -> not-found fragment (200, never 500), a malformed id -> FastAPI 422. Resolves the `process_file` queue inside `try/except enqueue_router.NoActiveAgentError`; on `NoActiveAgentError` it sets a flag and returns a fragment WITHOUT enqueuing (no fall-through to the default queue). On success, `agent_id = cast("str", routed.agent_id)` then `await enqueue_process_file(routed.queue, file, agent_id, settings.models_path, fine_cap=0, coarse_cap=0)`. New `deepen_response.html` HTMX fragment renders the queued / no-active-agent / not-found states.
- **Task 2** — six `test_deepen_*` tests in `tests/test_routers/test_pipeline.py` mirroring the existing `/pipeline` trigger-test harness (`seed_active_agent` + the `FakeQueue`/`DedupFakeQueue` capture doubles): (1) elevated-cap enqueue on the per-agent queue (`fine_cap==0`/`coarse_cap==0`, queue name `phaze-agent-nox`, never `default`); (2) COMPLETE `ProcessFilePayload` (all five required fields + the two cap overrides, validates via `model_validate`); (3) deterministic `process_file:<file_id>` key dedups an in-flight repeat to a no-op via `DedupFakeQueue`, with a fresh re-enqueue after `finish`; (4) `NoActiveAgentError` returns a fragment and does NOT enqueue; (5) unknown id -> not-found fragment, no enqueue; (6) malformed id -> 422.

## Verification Results

- `uv run ruff check src/phaze/routers/pipeline.py tests/test_routers/test_pipeline.py` — All checks passed
- `uv run mypy src/phaze/routers/pipeline.py` — Success: no issues found
- `uv run pytest tests/test_routers/test_pipeline.py -q -k "deepen"` — 6 passed
- `uv run pytest tests/test_routers/test_pipeline.py -q` — 64 passed
- `uv run pytest tests/test_routers/ tests/test_services/test_analysis_enqueue.py -q` (Postgres + Redis up) — 485 passed, 0 regressions
- Acceptance greps all present: `files/{file_id}/deepen`, `fine_cap=0`, `resolve_queue_for_task("process_file"`, `NoActiveAgentError` in the deepen handler

Test infrastructure note: these router tests require a live PostgreSQL (`localhost:5432/phaze_test`) and Redis (`localhost:6379`). I started ephemeral `postgres:18-alpine` + `redis:7-alpine` containers to run them (matching `just test-db`). The 39 errors/failures seen before Redis was started were all Redis-connection errors in unrelated `test_agent_*` / `test_execution_dispatch` modules — they vanished once Redis was up, confirming none were caused by this plan.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Three pre-existing `test_pipeline.py` payload-set assertions broken by Plan 01**
- **Found during:** Task 2 (running the full `test_pipeline.py` file, a stated plan verification: "uv run pytest tests/test_routers/test_pipeline.py -q passes").
- **Issue:** Plan 01 added `fine_cap`/`coarse_cap` to `ProcessFilePayload`, so `enqueue_process_file`'s `model_dump(mode="json")` now serializes both fields (as `None` on the bulk path). Three existing tests (`test_analyze_enqueues_complete_process_file_payload`, `test_analyze_enqueues_bounded_timeout_and_retries`, `test_enqueue_analysis_background`) still asserted `set(kwargs) == {five required fields}` and failed with "Extra items: fine_cap, coarse_cap". Plan 01 only updated `tests/test_services/test_analysis_enqueue.py`, not these three in `test_pipeline.py`.
- **Fix:** Updated the three assertions to the correct seven-key set (`{...five..., "fine_cap", "coarse_cap"}`) and added `is None` assertions documenting that the bulk "Run Analysis" path carries no cap override (deepen is the only elevated-cap caller).
- **Files modified:** tests/test_routers/test_pipeline.py
- **Commit:** 88f0eac

**2. [Rule 3 - Blocking] `# noqa: TC003` on the `uuid` import**
- **Found during:** Task 1 (`uv run ruff check` failed with TC003 "Move standard library import `uuid` into a type-checking block").
- **Issue:** `uuid` is only used in annotation position (`file_id: uuid.UUID`), so ruff's flake8-type-checking wants it under `TYPE_CHECKING`. But with `from __future__ import annotations`, FastAPI resolves the path-param annotation at runtime via `get_type_hints`, which needs `uuid` importable at runtime — moving it would break the route at import/request time. This is the exact PEP 649 / runtime-annotation hazard CLAUDE.md documents. Sibling routers (e.g. `tracklists.py`) escape the flag only because they also use `uuid` in runtime positions.
- **Fix:** Added a targeted `# noqa: TC003` with an explaining comment, keeping the runtime import.
- **Files modified:** src/phaze/routers/pipeline.py
- **Commit:** b3977e9

## TDD Gate Compliance

Plan frontmatter `type: execute` (not `tdd`); tasks are `type="auto"` with no `tdd="true"` attribute, so the RED/GREEN gate sequence does not apply. Task 1 (implementation) and Task 2 (tests) were committed separately as planned.

## Known Stubs

None. The endpoint is fully wired to `enqueue_process_file`; the Plan 04 UI button that POSTs to it is intentionally out of scope for this plan (control-plane endpoint half only, per the plan objective).

## Commits

- `b3977e9` feat(44-03): add POST /pipeline/files/{file_id}/deepen re-analyze endpoint
- `88f0eac` test(44-03): cover deepen endpoint (cap, routing, payload, dedup, no-agent, 404)

## Self-Check: PASSED

- SUMMARY.md present at the plan path
- src/phaze/routers/pipeline.py and src/phaze/templates/pipeline/partials/deepen_response.html exist
- Both task commits (b3977e9, 88f0eac) exist in git history
