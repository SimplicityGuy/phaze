---
slug: run-analysis-payload-invalid
status: resolved
trigger: "Run analysis" enqueues process_file jobs that fail ProcessFilePayload validation on the agent worker; all 11,428 files stuck in 'discovered', 0 analysis rows.
created: 2026-06-10
updated: 2026-06-10
---

# Debug Session: run-analysis-payload-invalid

## Symptoms

- **Expected behavior:** Clicking "Run analysis" enqueues one `process_file` SAQ job per DISCOVERED file; the agent worker analyzes each file (essentia) and writes an `analysis` row, advancing the file out of `discovered`.
- **Actual behavior:** Nothing visibly happens. All 11,428 files remain in `discovered`; `analysis` table has 0 rows. Jobs are enqueued and DO reach the agent worker (queue `phaze-agent-nox`), but each immediately fails validation, retries 4×, and dead-letters.
- **Error messages:** `pydantic_core.ValidationError: 4 validation errors for ProcessFilePayload` — `original_path`, `file_type`, `agent_id`, `models_path` all `Field required`; `input_value={'file_id': '...'}`. 73+ occurrences in `phaze-agent-worker@nox` logs (`src/phaze/tasks/functions.py:116` `ProcessFilePayload.model_validate(kwargs)`).
- **Timeline:** Surfaced after the v4.0.7 deploy (Phase 30 queue-misrouting fix, 10h ago). Latent bug — before Phase 30, `process_file` jobs were stranded on the consumer-less default queue and never reached a worker to fail validation. Now routing works, so the incomplete payload fails loudly.
- **Reproduction:** POST `/api/v1/analyze` (the "Run analysis" button) with files in DISCOVERED state and an active agent. Observe ProcessFilePayload validation errors on the agent worker.

## Live environment evidence (gathered pre-session)

- Both nox + lux on `ghcr.io/simplicityguy/phaze:v4.0.7` (deploy is correct; not a deploy regression).
- DB `phaze.files`: 11,428 rows, all `state = discovered`. `phaze.analysis`: 0 rows.
- Agent worker queue `phaze-agent-nox` is consuming jobs (Phase 30 routing fix confirmed working) — the failure is payload shape, not routing.

## Root Cause (pre-located, confirmed by debugger)

`src/phaze/routers/pipeline.py:43` `_enqueue_analysis_jobs`:
```python
for fid in file_ids:
    await queue.enqueue("process_file", file_id=fid)   # only file_id
```
`ProcessFilePayload` (`src/phaze/schemas/agent_tasks.py:28`, `extra="forbid"`) requires `file_id`, `original_path`, `file_type`, `agent_id`, `models_path`. The enqueue passes only `file_id`.

Working reference: `src/phaze/routers/agent_files.py:143` builds a full `ExtractMetadataPayload(file_id, original_path, file_type, agent_id)` via `task_router.enqueue_for_agent`.

`trigger_analysis` already has everything needed:
- `routed.agent_id` (from `enqueue_router.resolve_queue_for_task`)
- full `FileRecord` objects with `.id`, `.original_path`, `.file_type` (from `get_files_by_state`) — currently reduced to `[str(f.id)]`
- `settings.models_path` (`src/phaze/config.py:187`, default `/models`)

## Current Focus

- hypothesis: `_enqueue_analysis_jobs` sends an incomplete `process_file` payload (only `file_id`); fix = build a complete `ProcessFilePayload` per file from the already-available FileRecord + routed.agent_id + settings.models_path.
- test: regression test asserting `/api/v1/analyze` enqueues `process_file` with all required `ProcessFilePayload` fields (and that the payload validates against `ProcessFilePayload`).
- expecting: enqueued kwargs validate cleanly; jobs no longer dead-letter; files advance out of `discovered`.
- next_action: DONE — root cause confirmed, fix applied, regression tests added, full gate green.
- reasoning_checkpoint:
- tdd_checkpoint:

## Evidence

- timestamp 2026-06-10: agent-worker@nox logs show 73+ `ProcessFilePayload` validation errors, all on queue `phaze-agent-nox`, input_value contains only `file_id`.
- timestamp 2026-06-10: DB confirms 11,428 files all `discovered`, 0 analysis rows.
- timestamp 2026-06-10: both hosts on v4.0.7; queue routing (Phase 30) confirmed working.
- timestamp 2026-06-10: codebase confirmation — `pipeline.py` `_enqueue_analysis_jobs` enqueued only `file_id`; both `trigger_analysis` (`/api/v1/analyze`) and `trigger_analysis_ui` (`/pipeline/analyze`) reduced full FileRecord objects to `[str(f.id)]` before enqueue. `ProcessFilePayload` is `extra="forbid"` with 5 required fields. `agent_files.py:143` is the working ExtractMetadataPayload reference.

## Eliminated

- hypothesis: queue misrouting (Phase 30 / default-queue) — ELIMINATED: jobs reach `phaze-agent-nox` and fail at validation, not at routing.
- hypothesis: deploy/version skew — ELIMINATED: both hosts confirmed on v4.0.7.
- hypothesis: no active agent — ELIMINATED: agent worker is up and consuming jobs.

## Resolution

- root_cause: `_enqueue_analysis_jobs` in `src/phaze/routers/pipeline.py` enqueued each `process_file` SAQ job with only `file_id`, but the agent worker validates kwargs against `ProcessFilePayload` (`extra="forbid"`, 5 required fields), so every job failed validation, retried 4×, and dead-lettered — stranding all 11,428 files in DISCOVERED. Latent since inception; only surfaced once Phase 30 (v4.0.7) routed the jobs to a real consumer instead of the consumer-less default queue.
- fix: `_enqueue_analysis_jobs` now builds a complete `ProcessFilePayload(file_id, original_path, file_type, agent_id, models_path)` per file and enqueues `payload.model_dump(mode="json")` (UUID → str round-trips and validates). Both call sites (`trigger_analysis` for `/api/v1/analyze` and `trigger_analysis_ui` for `/pipeline/analyze`) now pass full `FileRecord` objects plus `routed.agent_id` and `settings.models_path` into the helper, mirroring the working `agent_files.py` ExtractMetadataPayload pattern.
- verification: new regression test `test_analyze_enqueues_complete_process_file_payload` asserts all five fields are present, carry the FileRecord/agent/models_path values, and that the captured kwargs validate against `ProcessFilePayload`; `test_enqueue_analysis_background` updated to assert the full payload; UI path asserts every enqueued job validates. Full gate green on a branch worktree: `ruff check` + `ruff format` clean, `mypy` clean (142 files), 1535 tests passed, coverage 97.46% (≥85%).
- files_changed: src/phaze/routers/pipeline.py, tests/test_routers/test_pipeline.py
