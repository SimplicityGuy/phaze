---
phase: 32-pipeline-reboot-resilience-re-enqueue
plan: 01
subsystem: enqueue
tags: [saq, dedup, deterministic-key, process_file, wave-1]
requires:
  - "tests/_queue_fakes.py::FakeQueue (captured_policy splits key/timeout/retries)"
provides:
  - "src/phaze/services/analysis_enqueue.py::process_file_job_key — deterministic process_file:<file_id> key"
  - "src/phaze/services/analysis_enqueue.py::enqueue_process_file — FastAPI-free producer (key + 5-field payload + timeout=14400/retries=2), returns None on SAQ dedup"
affects:
  - "Wave 2 reboot re-enqueue task imports enqueue_process_file so both producers emit the identical key"
tech-stack:
  added: []
  patterns:
    - "Single-source-of-truth enqueue helper in a FastAPI-free module imported by both the HTTP router and the (Wave-2) reboot task"
    - "Deterministic SAQ job key drives per-queue incomplete-set dedup (no-op on a repeat in-flight enqueue)"
key-files:
  created:
    - src/phaze/services/analysis_enqueue.py
    - tests/test_services/test_analysis_enqueue.py
  modified:
    - src/phaze/routers/pipeline.py
    - tests/test_routers/test_pipeline.py
decisions:
  - "Helper lives in a NEW services/analysis_enqueue.py (FastAPI-free) so the Wave-2 reboot task can import it without pulling in the web layer (32-RESEARCH §Q4 import boundary)"
  - "uuid + FileRecord are annotation-only (TYPE_CHECKING); ProcessFilePayload is a real import because it is constructed"
  - "enqueue_process_file RETURNS the enqueue result (Job or None) so the Wave-2 loop can count a None as a dedup skip"
requirements: [RESIL-03, RESIL-05]
metrics:
  duration: ~12m
  completed: 2026-06-11
  tasks: 2
  files: 4
---

# Phase 32 Plan 01: Shared process_file Deterministic-Key Enqueue Seam Summary

Created the single FastAPI-free producer (`services/analysis_enqueue.py`) that owns the deterministic SAQ job key `process_file:<file_id>`, the complete 5-field `ProcessFilePayload`, and the job policy (`timeout=14400` / `retries=2`); refactored the dashboard "Run Analysis" path to delegate to it so the dashboard and the Wave-2 reboot re-enqueue path cannot drift.

## What Was Built

- **`process_file_job_key(file_id)`** (`src/phaze/services/analysis_enqueue.py`): returns exactly `f"process_file:{file_id}"` — the deterministic discriminator SAQ's per-queue `incomplete`-set dedup keys on (32-RESEARCH §Q4). `file_id` is a server-generated UUID, so no untrusted free-text enters the key (threat T-32-01).
- **`enqueue_process_file(queue, file, agent_id, models_path)`**: builds a COMPLETE `ProcessFilePayload` (the FileRecord's `id`/`original_path`/`file_type` + resolved `agent_id`/`models_path`), serializes via `model_dump(mode="json")`, and enqueues with `key=process_file_job_key(file.id)`, `timeout=14400`, `retries=2`, plus the 5 payload kwargs. Returns the enqueue result (a `saq.Job`, or `None` on SAQ dedup) so the Wave-2 reboot loop can count a `None` as a skip.
- **Import boundary held**: AST check confirms the module imports only `__future__`, `typing`, `uuid` (annotation-only), `phaze.models.file` (annotation-only), `phaze.schemas.agent_tasks` — neither `fastapi` nor `phaze.routers`.
- **`routers/pipeline.py::_enqueue_analysis_jobs`** now loops `await enqueue_process_file(queue, f, agent_id, models_path)`; the inline `ProcessFilePayload(...)` construction and inline `queue.enqueue("process_file", timeout=..., retries=...)` were removed (the policy lives once in the helper). The `ProcessFilePayload` import was dropped from the router (no longer constructed there). Function signature and both callers (`trigger_analysis`, `trigger_analysis_ui`) unchanged.

## Why It Matters

Today's dashboard enqueue passed NO `key` → SAQ minted a random `uuid1` → zero dedup, so a double-click of "Run Analysis" (or the Wave-2 reboot re-enqueue overlapping an in-flight run) enqueued duplicate `process_file` jobs. Centralizing the key + payload + policy in one FastAPI-free seam means BOTH producers emit the IDENTICAL `process_file:<file_id>`; the Wave-2 task imports the same helper, so the two paths are structurally incapable of drifting.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Test correctness] Updated two existing Phase-31 policy assertions to include the new key**
- **Found during:** Task 2
- **Issue:** `test_analyze_enqueues_bounded_timeout_and_retries` and `test_analyze_ui_enqueues_bounded_timeout_and_retries` asserted `captured_policy[0] == {"timeout": 14400, "retries": 2}` by exact-dict equality. The helper now also routes `key=` into `captured_policy` (a `saq.Job` dataclass field), so the exact-dict assertions had to acknowledge the key the plan deliberately adds.
- **Fix:** Each test now captures the seeded file's id and asserts `captured_policy[0] == {"key": f"process_file:{file_rec.id}", "timeout": 14400, "retries": 2}` — strengthening, not weakening, the assertion. This is the planned `-k key` assertion on the existing dashboard analyze test.
- **Files modified:** tests/test_routers/test_pipeline.py
- **Commit:** 17df533

## TDD Gate Compliance

- RED: `0e78f10 test(32-01): add failing test ...` — module absent, collection failed (`ModuleNotFoundError: phaze.services.analysis_enqueue`).
- GREEN: `a2061a9 feat(32-01): add FastAPI-free shared process_file enqueue helper` — 3 passed.
- No REFACTOR commit needed (helper was minimal as written).

## Verification

- `uv run pytest tests/test_services/test_analysis_enqueue.py tests/test_routers/test_pipeline.py -q` → 40 passed.
- `uv run mypy src/phaze/services/analysis_enqueue.py src/phaze/routers/pipeline.py` → clean.
- Import boundary (AST): `services/analysis_enqueue.py` imports neither `fastapi` nor `phaze.routers`.
- Regression smoke: `tests/test_queue_fakes_dedup.py` + `tests/test_services/test_enqueue_router.py` + `tests/test_no_default_queue_producers.py` → 22 passed (shared fakes unperturbed).
- Acceptance greps: `grep "ProcessFilePayload(" routers/pipeline.py` → no match inside `_enqueue_analysis_jobs`; `grep "enqueue_process_file" routers/pipeline.py` → import + delegation present.
- Pre-commit hooks (ruff, ruff-format, bandit, mypy, ...) passed on all three code commits.

## Threat Surface

No new trust boundary. T-32-01 (key from `file_id`) mitigated: the key interpolates a server-generated UUID, no untrusted free-text. T-32-05 (dropped policy → `apply_project_job_defaults` clobbers `retries`→4) mitigated: the helper always passes `timeout=14400, retries=2` and Task-1 + the two Phase-31 tests assert both on `captured_policy`. No package installs (T-32-SC accept).

## Commits

- `0e78f10` test(32-01): add failing test for shared process_file enqueue helper
- `a2061a9` feat(32-01): add FastAPI-free shared process_file enqueue helper
- `17df533` feat(32-01): delegate dashboard analyze enqueue to shared helper

## Self-Check: PASSED
- FOUND: src/phaze/services/analysis_enqueue.py
- FOUND: tests/test_services/test_analysis_enqueue.py
- FOUND: src/phaze/routers/pipeline.py (modified, delegates to helper)
- FOUND: tests/test_routers/test_pipeline.py (modified, key assertions added)
- FOUND: commit 0e78f10
- FOUND: commit a2061a9
- FOUND: commit 17df533
