---
phase: 44-analyze-observability-ui-straggler-failed-count-sampled-badg
plan: 01
subsystem: analysis-enqueue
tags: [saq, payload, analysis, deepen, caps]
requires:
  - "ProcessFilePayload (existing schema, extra='forbid')"
  - "enqueue_process_file single-funnel producer (Phase 30/32/36)"
  - "analyze_file fine_cap/coarse_cap kwargs + _stride_to_cap no-op (Phase 43-04)"
provides:
  - "ProcessFilePayload.fine_cap / coarse_cap optional int|None fields (default None)"
  - "enqueue_process_file keyword-only fine_cap/coarse_cap pass-through into the serialized payload"
  - "process_file worker prefers payload cap override over AgentSettings (None falls back)"
affects:
  - "Plan 03 'deepen analysis' endpoint (will pull this backend lever via enqueue_router)"
tech-stack:
  added: []
  patterns:
    - "Same-file optional-default-under-extra='forbid' idiom (sub_batch_index / sha256_hash)"
    - "Keyword-only trailing params keep positional bulk caller unchanged"
    - "payload override with None-fallback to config"
key-files:
  created: []
  modified:
    - src/phaze/schemas/agent_tasks.py
    - src/phaze/services/analysis_enqueue.py
    - src/phaze/tasks/functions.py
    - tests/test_schemas/test_agent_tasks.py
    - tests/test_services/test_analysis_enqueue.py
    - tests/test_tasks/test_functions.py
decisions:
  - "Caps are keyword-only + trailing on enqueue_process_file so the positional bulk caller (_enqueue_analysis_jobs at pipeline.py:241) needs no change"
  - "Worker does NOT special-case cap<=0; it passes the override straight to analyze_file where _stride_to_cap (43-04) treats <=0 as the analyze-ALL-windows no-op"
  - "Default None on both schema fields preserves the five-field bulk producer under extra='forbid' (no migration, schema-only)"
metrics:
  duration: ~12 min
  completed: 2026-06-18
  tasks: 2
  files: 6
---

# Phase 44 Plan 01: Per-Job Cap Override End-to-End Summary

Added optional per-job `fine_cap`/`coarse_cap` to `ProcessFilePayload` and threaded them producer -> serialized payload -> agent worker -> `analyze_file`, giving the future "deepen analysis" endpoint a re-analyze lever (cap 0 = analyze ALL windows) while every existing bulk caller keeps the AgentSettings 60/30 defaults unchanged. No DB migration — pydantic schema field only.

## What Was Built

- **Task 1** — `ProcessFilePayload` gains `fine_cap: int | None = None` and `coarse_cap: int | None = None`, mirroring the same-file `sub_batch_index` / `sha256_hash` optional-default idiom. `extra="forbid"` unchanged; the five-field bulk build still validates. Tests cover default-None round-trip and explicit `0`/`0` round-trip as ints via `model_dump(mode="json")`.
- **Task 2** — `enqueue_process_file` extended with keyword-only trailing `fine_cap`/`coarse_cap`, threaded into the `ProcessFilePayload(...)` build. Single funnel preserved: deterministic key `process_file:<file_id>`, `timeout=7200`, `retries=2`, and `model_dump(mode="json")` serialization all unchanged. `process_file` now computes `fine_cap = payload.fine_cap if payload.fine_cap is not None else cfg.analysis_fine_cap` (same for coarse) and passes those to `run_in_process_pool`. `cap<=0` is NOT special-cased — it flows to `analyze_file`/`_stride_to_cap` (43-04) as the analyze-ALL no-op.

## Verification Results

- `uv run pytest tests/test_schemas/test_agent_tasks.py tests/test_services/test_analysis_enqueue.py tests/test_tasks/test_functions.py -q` — 59 passed
- `uv run mypy src/phaze/schemas/agent_tasks.py src/phaze/services/analysis_enqueue.py src/phaze/tasks/functions.py` — clean
- `uv run ruff check` on the three source files — All checks passed
- Full non-integration suite — 1187 passed, 0 regressions (pre-existing tracklist mock warnings only, out of scope)
- `grep -c "process_file_job_key" src/phaze/services/analysis_enqueue.py` = 2 (definition + single call site; single funnel intact)
- Positional bulk caller `enqueue_process_file(queue, f, agent_id, models_path)` at `routers/pipeline.py:241` unchanged and still valid

## Deviations from Plan

None — plan executed exactly as written. The plan-listed `extra="forbid"` rejection test was already present (`test_process_file_payload_rejects_unknown_field`) so no new one was added; the existing `complete_payload_and_policy` enqueue test was updated to assert the two new serialized cap fields (None when not overridden), which is the expected serialization change rather than a deviation.

## TDD Gate Compliance

Both tasks followed RED -> GREEN:
- Task 1: `74aae30` (test, RED) -> `90c5775` (feat, GREEN)
- Task 2: `e6be702` (test, RED) -> `3425d6b` (feat, GREEN)
RED runs confirmed the new tests failed before implementation (`extra_forbidden` on the caps, `60 != 0` on the override path).

## Known Stubs

None.

## Commits

- `74aae30` test(44-01): add failing tests for ProcessFilePayload fine_cap/coarse_cap
- `90c5775` feat(44-01): add optional fine_cap/coarse_cap to ProcessFilePayload
- `e6be702` test(44-01): add failing tests for cap threading producer->payload->worker
- `3425d6b` feat(44-01): thread cap override producer->payload->worker->analyze_file

## Self-Check: PASSED

- SUMMARY.md present at the plan path
- All four task commits (74aae30, 90c5775, e6be702, 3425d6b) exist in git history
