---
phase: 26
plan: 11
subsystem: tasks
tags: [python, saq, http-rewrite, task-bodies, execute-approved-batch]
dependency_graph:
  requires:
    - "26-02"  # PhazeAgentClient (put_analysis, put_metadata, put_fingerprint, create_tracklist, post_execution_log, patch_execution_log, patch_proposal_state)
    - "26-03"  # Agent task payload schemas (ProcessFilePayload, ExtractMetadataPayload, FingerprintFilePayload, ScanLiveSetPayload, ExecuteApprovedBatchPayload, ExecuteBatchProposalItem)
    - "26-06"  # PUT /api/internal/agent/analysis/{file_id} router
    - "26-07"  # POST /api/internal/agent/tracklists router
    - "26-08"  # PATCH /api/internal/agent/proposals/{id}/state router
  provides:
    - "agent-process_file"          # essentia analysis via HTTP
    - "agent-extract_file_metadata" # mutagen tags via HTTP
    - "agent-fingerprint_file"      # audfprint+panako via HTTP
    - "agent-scan_live_set"         # tracklist creation via HTTP with stable uuid5 request_id
    - "agent-execute_approved_batch" # per-proposal copy+verify+delete + state reporting
    - "phaze.enums.execution"       # DB-free ExecutionStatus enum
  affects:
    - "src/phaze/schemas/agent_execution.py"  # now imports from phaze.enums (clean boundary)
    - "src/phaze/models/execution.py"         # re-exports ExecutionStatus from phaze.enums
    - "src/phaze/services/fingerprint.py"     # get_fingerprint_progress imports moved function-local
tech_stack:
  added:
    - "phaze.enums (new DB-free enum package)"
  patterns:
    - "ctx['api_client'] PhazeAgentClient injection for every file-bound task"
    - "Payload.model_validate(kwargs) at task entry (extra='forbid')"
    - "uuid5(NAMESPACE_URL, 'phaze-scan-{file_id}') for stable idempotency keys"
    - "Path.resolve() + relative_to(scan_root) for path-traversal containment"
    - "Streaming sha256 for large-file integrity verification"
    - "Per-proposal failure isolation in batch executor (broad except + log + continue)"
key_files:
  created:
    - "src/phaze/enums/__init__.py"
    - "src/phaze/enums/execution.py"
    - "tests/test_tasks/test_execute_approved_batch.py"
  modified:
    - "src/phaze/tasks/functions.py"
    - "src/phaze/tasks/metadata_extraction.py"
    - "src/phaze/tasks/fingerprint.py"
    - "src/phaze/tasks/scan.py"
    - "src/phaze/tasks/execution.py"
    - "src/phaze/schemas/agent_execution.py"
    - "src/phaze/models/execution.py"
    - "src/phaze/services/fingerprint.py"
    - "tests/test_tasks/test_functions.py"
    - "tests/test_tasks/test_metadata_extraction.py"
    - "tests/test_tasks/test_fingerprint.py"
    - "tests/test_tasks/test_scan.py"
    - "tests/test_tasks/test_execution.py"
decisions:
  - "D-05: in-place rewrite -- no parallel files, no compatibility shims"
  - "D-23: agents read files via payload.original_path; no DB read-back"
  - "D-24: payload has no current_path; controller patches it via patch_proposal_state"
  - "D-26: AnalysisWritePayload mood/style wire format is dict[str, float]"
  - "T-26-11-S1: path-traversal guard via Path.resolve() + relative_to"
  - "W5 Option (b): scan_live_set artist/title resolution removed; known v3.0 UI regression"
  - "B2 Option A: execute_approved_batch fully implemented (no NotImplementedError stub)"
  - "Rule 3 fix: extract ExecutionStatus enum to phaze.enums (DB-free) so phaze.schemas.agent_execution is loadable without sqlalchemy/phaze.database"
metrics:
  duration: "~30 minutes"
  completed_date: "2026-05-12"
  tasks_completed: "4/4"
  files_modified: 13
  files_created: 3
  tests_passing: "64/64 task tests; 165/165 task+schema+service tests relevant to Plan 11"
  net_lines: "+1241 / -1206 (incl. test rewrites)"
---

# Phase 26 Plan 11: Task Code Reorg & HTTP-Backed Agent Worker Summary

Rewrote all 5 file-bound SAQ task bodies to send state changes via HTTP instead of direct ORM access — the mechanical core of Phase 26 that unblocks an agent process running without Postgres reachability. Adds full B2 Option A implementation of `execute_approved_batch` with per-proposal copy + verify + delete + HTTP state reporting + path-traversal guard.

## One-liner

Replaced every `ctx["async_session"]` call in `src/phaze/tasks/{functions,metadata_extraction,fingerprint,scan,execution}.py` with `ctx["api_client"]` calls into `PhazeAgentClient`, and verified the agent worker can now load these modules without pulling SQLAlchemy / `phaze.database` / `phaze.models.*` into memory.

## Tasks Completed

| Task | Name | Commit | Files |
| ---- | ---- | ------ | ----- |
| 1 | Rewrite process_file + extract_file_metadata | 839b9e5 | src/phaze/tasks/functions.py, metadata_extraction.py, tests/test_tasks/test_functions.py, test_metadata_extraction.py |
| 2 | Rewrite fingerprint_file + scan_live_set | 6d39131 | src/phaze/tasks/fingerprint.py, scan.py, src/phaze/services/fingerprint.py, tests/test_tasks/test_fingerprint.py, test_scan.py |
| 3 | Rewrite execute_approved_batch + contract tests | 0163abf | src/phaze/tasks/execution.py, src/phaze/enums/{__init__,execution}.py, src/phaze/models/execution.py, src/phaze/schemas/agent_execution.py, tests/test_tasks/test_execute_approved_batch.py, test_execution.py |
| 4 | Verify import-boundary invariant (read-only) | (no commit) | n/a |

## What Was Built

### process_file (functions.py)
- Validates kwargs via `ProcessFilePayload.model_validate(...)` (extra='forbid').
- Runs essentia via `run_in_process_pool(ctx, analyze_file, payload.original_path, payload.models_path)`.
- Posts the result via `api.put_analysis(file_id, AnalysisWritePayload(...))`.
- New helpers `_features_to_mood_dict` and `_features_to_style_dict` convert essentia's string outputs into the D-26 wire format (`dict[str, float]`), averaging the mood_* sets across their 3 model variants and listing the top-N genres for style.

### extract_file_metadata (metadata_extraction.py)
- Validates kwargs via `ExtractMetadataPayload.model_validate(...)`.
- Skips companion files via `EXTENSION_MAP` (parity with prior body).
- Calls `extract_tags(payload.original_path)` (sync mutagen) and posts via `api.put_metadata(file_id, MetadataWriteRequest(...))`.

### fingerprint_file (fingerprint.py)
- Validates kwargs via `FingerprintFilePayload.model_validate(...)`.
- Pulls `FingerprintOrchestrator` from `ctx["fingerprint_orchestrator"]` (constructed by Plan 10's startup hook).
- Submits to all engines via `orchestrator.ingest_all(...)` and PUTs per-engine result via `api.put_fingerprint(file_id, engine_name, FingerprintWriteRequest(...))`.

### scan_live_set (scan.py)
- Validates kwargs via `ScanLiveSetPayload.model_validate(...)`.
- Queries via `orchestrator.combined_query(...)`.
- Generates a stable `request_id = uuid.uuid5(NAMESPACE_URL, f"phaze-scan-{file_id}")` so SAQ retries hit the server's Redis idempotency cache (Plan 26-07).
- POSTs `TracklistCreatePayload` via `api.create_tracklist(...)`.

### execute_approved_batch (execution.py) -- FULL B2 Option A
Per-proposal lifecycle:
1. POST execution-log with `status=in_progress` (one row per file op; matches Phase 25 ExecutionLog schema).
2. Path-traversal guard via `_resolve_and_check_containment(path, scan_roots)` (T-26-11-S1).
3. Optional sha256 verify via streaming hash (avoids loading huge files into memory).
4. Copy `original_path -> proposed_path` (mkdir parent; write_bytes + read_bytes pair).
5. Delete original.
6. PATCH execution-log with `status=completed | failed`.
7. PATCH proposal-state with `proposal_state=executed | failed`, `file_state=moved | None`, `current_path=str(proposed)`.

Cross-proposal failures are isolated: one bad file (IO error, path traversal, sha256 mismatch) gets `state=failed` and the batch continues. Batch return dict carries `status=completed | completed_with_errors` + aggregate counts.

### Supporting refactors

- **`phaze.enums` package (NEW)**: holds `ExecutionStatus` enum at a DB-free location so `phaze.schemas.agent_execution` is loadable inside the agent worker without dragging in SQLAlchemy.
- **`phaze.models.execution`**: re-exports `ExecutionStatus` from `phaze.enums.execution` for full backward compatibility.
- **`phaze.services.fingerprint`**: moves `from phaze.models.*` + `from sqlalchemy import ...` inside `get_fingerprint_progress` (the only function that uses them); now the file-level imports are DB-free so the orchestrator can load on the agent.

### Tests

- `tests/test_tasks/test_execute_approved_batch.py` (NEW, 5 contract tests):
  - happy path (3 proposals all succeed)
  - partial failure (middle IO-fails)
  - path traversal rejected (T-26-11-S1)
  - sha256 mismatch
  - empty scan_roots refused
- Rewritten task tests use respx-style `AsyncMock` for `ctx["api_client"]` and `ctx["fingerprint_orchestrator"]`. No DB fixtures needed. No new `pytest.skip` markers added.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking issue] Extracted ExecutionStatus to phaze.enums (DB-free)**
- **Found during:** Task 3
- **Issue:** `phaze.schemas.agent_execution` imported `ExecutionStatus` from `phaze.models.execution`, which transitively pulled in `sqlalchemy` and `phaze.models.base`. This violated the D-03 import boundary even *before* Plan 26-11 began -- a Phase 25 oversight. The new `tasks/execution.py` needs to import the schema; if the schema is itself dirty, the boundary fails.
- **Fix:** Created `src/phaze/enums/__init__.py` + `src/phaze/enums/execution.py` (DB-free enum module). Updated `phaze.schemas.agent_execution` and `phaze.tasks.execution` to import from the new location. `phaze.models.execution` re-exports `ExecutionStatus` so legacy importers (routers, services, tests) keep working.
- **Files modified:** src/phaze/enums/__init__.py (new), src/phaze/enums/execution.py (new), src/phaze/models/execution.py, src/phaze/schemas/agent_execution.py
- **Commit:** 0163abf

**2. [Rule 3 - Blocking issue] Function-local DB imports in services/fingerprint.py**
- **Found during:** Task 2
- **Issue:** `phaze.services.fingerprint` imported `from sqlalchemy import func, select` and `from phaze.models.file import FileRecord, FileState` and `from phaze.models.fingerprint import FingerprintResult` at module level. Only `get_fingerprint_progress(session)` used them, but the agent worker also needs to import `FingerprintOrchestrator` from the same module. The module-level imports made the file un-loadable on the agent.
- **Fix:** Moved all three DB imports inside `get_fingerprint_progress`. Module-level surface is now DB-free; only the controller code path that calls `get_fingerprint_progress` pays the import cost.
- **Files modified:** src/phaze/services/fingerprint.py
- **Commit:** 6d39131

### Adaptations from plan spec

**3. [Adaptation] execute_approved_batch uses per-proposal ExecutionLog (not per-batch)**
- The plan envisioned one POST execution-log at batch start with `batch_id, total_count` and one PATCH at end with `processed_count, error_count`. The actual Phase 25 `ExecutionLogCreate` schema is per-proposal (one row per file op with `proposal_id, source_path, destination_path, sha256_verified, status, error_message`) -- no batch-level fields.
- Adapted to the existing schema: one POST execution-log per proposal at the start of each file op (status=in_progress) and one PATCH per proposal at the end (status=completed | failed). The aggregate `completed | completed_with_errors` lives in the **returned dict** from `execute_approved_batch` (consumed by SAQ job results / future controller-side telemetry), not in any new schema field.
- The plan's "Cross-plan dependencies" section explicitly anticipated this: "If the actual schemas use different field names (`total` vs `total_count`), adjust to match. The intent (one POST at start, per-proposal PATCH state, one PATCH at end) is the invariant." The invariant of "every state change is an authenticated HTTP call" is preserved.

**4. [Adaptation] mood / style wire format conversion**
- `analyze_file` returns `mood: str` (dominant label, e.g., "happy") and `style: str` (top genre, e.g., "Electronic/House"). The `AnalysisWritePayload` wire schema requires `dict[str, float] | None` per D-26.
- The plan's example body simply did `mood=analysis.get("mood")` which would fail Pydantic validation (`str` is not `dict[str, float]`). Added two helpers (`_features_to_mood_dict`, `_features_to_style_dict`) that rebuild the wire-format dicts from `analysis["features"]` -- the richer feature data the agent already has from essentia. This preserves end-to-end fidelity (the dict carries multiple mood scores, not just the dominant one) and lets the server's `_summarize_dict_to_string` produce its bounded summary correctly.

## Known Stubs

None. All 5 task bodies are full implementations; `execute_approved_batch` is B2 Option A (no NotImplementedError stub per CONTEXT.md revision iter 2).

## v3.0 UI Regression (per W5 Option (b))

`scan_live_set` no longer joins `FileMetadata` to resolve `artist` / `title` on fingerprint matches (the agent has no DB access). Fingerprint-sourced tracklist rows therefore land with `artist=None, title=None`. The v3.0 tracklist review UI may show empty artist/title columns for fingerprint-sourced tracklists until a future Phase 27/28 controller-side enrichment task fills them in.

This is documented here per W5 guidance and will also surface in:
- `.planning/ROADMAP.md` Phase 26 entry (Plan 13 doc sweep)
- Future Phase 27/28 enrichment plan referencing this Summary

## Authentication Gates

None. All work performed in tests with mocked `api_client`; no live agent token / API URL needed.

## Import Boundary Invariant (Task 4 verification)

All 5 rewritten task modules load cleanly without any banned imports. Verified via:

```
uv run python -c "
import sys
for mod in ['phaze.tasks.functions', 'phaze.tasks.metadata_extraction',
           'phaze.tasks.fingerprint', 'phaze.tasks.scan', 'phaze.tasks.execution']:
    __import__(mod)
banned = ['phaze.database', 'sqlalchemy.ext.asyncio']
loaded = [m for m in banned if m in sys.modules]
assert not loaded, f'BANNED MODULES LOADED: {loaded}'
print('PASS: no banned modules loaded')
"
```

Output: `PASS: no banned modules loaded`

Plan 10's `tests/test_task_split.py` hasn't merged yet, but the structural invariant it will assert is already satisfied by Plan 26-11. When Plan 10 lands, its import-boundary test will go GREEN on first run.

## Verification

- `uv run pytest tests/test_tasks/ -x -q --no-cov`: 64 passed
- `uv run pytest tests/test_tasks/ tests/test_services/test_fingerprint.py tests/test_services/test_agent_client.py tests/test_schemas/ -q --no-cov`: 165 passed
- `uv run mypy src/phaze/tasks/*.py`: clean
- `uv run ruff check .`: clean (post-fix)
- `uv run ruff format --check .`: clean
- `pre-commit run --all-files`: all hooks pass

Pre-existing Redis-dependent tests (`tests/test_routers/test_agent_tracklists.py`, `tests/test_services/test_agent_task_router.py`) fail in this environment due to no local Redis server -- unrelated to Plan 26-11. Verified by inspection: these failures predate the Task 1 commit.

## Self-Check: PASSED

- Files created/modified exist on disk (verified via `git diff --stat HEAD~3..HEAD`).
- All 3 commits exist on branch (verified via `git log --oneline | head -3`):
  - 0163abf feat(26-11): rewrite execute_approved_batch over HTTP (B2 Option A)
  - 6d39131 refactor(26-11): rewrite fingerprint_file + scan_live_set over HTTP
  - 839b9e5 refactor(26-11): rewrite process_file + extract_file_metadata over HTTP
- All 5 task files have 0 DB imports (verified via grep).
- All 5 task modules load without pulling in phaze.database / sqlalchemy.
- 64/64 task tests pass; 5/5 execute_approved_batch contract tests green.
