---
phase: 27-watcher-service-user-initiated-scan
plan: 02
subsystem: schemas-wire-contracts
tags:
  - schemas
  - contracts
  - wire-format
  - pydantic
requires:
  - phaze.schemas.agent_files.FileUpsertChunk (Phase 25 D-16)
  - phaze.schemas.agent_execution.ExecutionLogPatch + ExecutionLogPatchResponse (Phase 25 D-13/D-15 — byte-level analog for the new PATCH schemas)
  - phaze.schemas.agent_tasks.ScanLiveSetPayload (Phase 26 D-22 — byte-level analog for ScanDirectoryPayload)
  - phaze.config.AgentSettings.scan_chunk_size (Phase 27 Plan 01 — referenced by future consumers, not directly imported here)
provides:
  - FileUpsertChunk.batch_id (uuid.UUID | None, default None) — Phase 27 D-09 wire-format extension
  - ScanBatchPatch — PATCH /api/internal/agent/scan-batches/{batch_id} body (D-10); Literal["running","completed","failed"] excludes the watcher's LIVE sentinel
  - ScanBatchPatchResponse — full-row echo response (D-Discretion §4)
  - ScanDirectoryPayload — SAQ payload for scan_directory task (D-14)
  - TriggerScanForm — POST /pipeline/scans form body (D-06)
affects:
  - tests/test_schemas/test_agent_tasks.py — extended `test_no_current_path_field_anywhere` and `test_only_process_file_payload_has_models_path` to cover the new ScanDirectoryPayload class
tech_stack:
  added: []
  patterns:
    - Optional UUID wire field via `uuid.UUID | None = None` (Pydantic v2 — runtime `import uuid` required for validator construction even with PEP 604 syntax)
    - Schema-layer terminal-state guard via `Literal[...]`-without-`live` on PATCH body (D-10)
    - Loose `status: str` on response classes (mirrors `ExecutionLogPatchResponse.status: ExecutionStatus`) — server-built objects extend non-breakingly
key_files:
  created:
    - src/phaze/schemas/agent_scan_batches.py
    - src/phaze/schemas/pipeline_scans.py
    - tests/test_schemas/test_agent_files.py
    - tests/test_schemas/test_agent_scan_batches.py
    - tests/test_schemas/test_pipeline_scans.py
  modified:
    - src/phaze/schemas/agent_files.py (FileUpsertChunk +1 optional field; dropped unused `from __future__ import annotations`)
    - src/phaze/schemas/agent_tasks.py (ScanDirectoryPayload class appended after ScanLiveSetPayload)
    - tests/test_schemas/test_agent_tasks.py (5 new tests + invariant test updates)
decisions:
  - "Dropped `from __future__ import annotations` from agent_files.py rather than gate the runtime `import uuid` with `if TYPE_CHECKING:`. Pydantic v2 needs `uuid` in module globals at class creation time to build the UUID validator; ruff's TC003 cannot know this and emits a false positive. Removing the stringized-annotations import is consistent with every other schema module in `src/phaze/schemas/` (none of them use `from __future__`) and matches the Phase 25/26 precedent."
  - "Placed ScanDirectoryPayload immediately AFTER ScanLiveSetPayload in agent_tasks.py (not at the end of the file). Two reasons: (a) all scan-family payloads cluster together for read order, and (b) the ExecuteApprovedBatchPayload + ExecuteBatchProposalItem pair at the end of the file is its own logical unit (Phase 26 D-23 dispatch) that should not have a non-related payload inserted between them."
  - "Tightened the spec's 8 behavior tests for ScanBatchPatch to 9 by adding a JSON-schema assertion (`test_scan_batch_patch_status_json_schema_excludes_live`). The plan text mentioned 'verify model_json_schema()[\"properties\"][\"status\"][\"anyOf\"] contains the three Literal values plus null' as part of the action block — promoted that to a standalone test for explicit grep-able coverage of the D-10 schema-layer LIVE guard."
metrics:
  duration_minutes: 12
  completed_date: 2026-05-13
  tasks_completed: 3
  commits: 3
  tests_added: 19
  tests_passing: 56
  files_created: 5
  files_modified: 3
---

# Phase 27 Plan 02: Wire-Format Schemas Summary

Define every Pydantic wire contract Phase 27 introduces — `FileUpsertChunk.batch_id`, `ScanBatchPatch{,Response}`, `ScanDirectoryPayload`, and `TriggerScanForm` — as a single contracts-first plan so Waves 2-3 (routers, tasks, UI) import names and stop, with zero contract-renegotiation churn.

## What Was Built

**Three atomic commits, one per task:**

| Commit  | Task | Description |
| ------- | ---- | ----------- |
| d93f496 | 1    | Extend `FileUpsertChunk` with `batch_id: uuid.UUID | None = None` (D-09). 5 tests cover default-None, explicit-UUID, non-UUID rejection, preserved `extra="forbid"` for unknown fields, JSON-schema exposes uuid+null. Dropped unused `from __future__ import annotations` so the runtime `uuid` import passes ruff TC003 cleanly. |
| 1ec37e2 | 2    | New `phaze.schemas.agent_scan_batches` module with `ScanBatchPatch` (PATCH body; `extra="forbid"`; 4 optional fields; status restricted to `Literal["running", "completed", "failed"]` excluding the watcher-owned `"live"` sentinel) + `ScanBatchPatchResponse` (full-row echo per D-Discretion §4). 9 tests cover acceptance/rejection of each status value, optional progress counts, no-`ge=` constraint, extra-forbid, empty-body validity, row-echo response, and the JSON-schema Literal-alternatives invariant. |
| 0f0b6bc | 3    | Appended `ScanDirectoryPayload` to `phaze.schemas.agent_tasks` (after `ScanLiveSetPayload`); new module `phaze.schemas.pipeline_scans` with `TriggerScanForm`. 5 ScanDirectoryPayload tests (minimal-valid, non-UUID rejection, extra-forbid, field-set, no-models/current-path) + 4 TriggerScanForm tests (default empty subpath, explicit subpath, extra-forbid, required-fields). Existing invariant tests extended. |

## Verification

The plan's `<verification>` block in full:

- `uv run pytest tests/test_schemas/test_agent_files.py tests/test_schemas/test_agent_scan_batches.py tests/test_schemas/test_agent_tasks.py tests/test_schemas/test_pipeline_scans.py -x -q` → **45 passed in 0.03s**
- `uv run pytest tests/test_routers/test_agent_files.py -x -q` → **11 passed in 2.04s** (no Phase 25/26 regression)
- `uv run ruff check src/phaze/schemas/agent_files.py src/phaze/schemas/agent_scan_batches.py src/phaze/schemas/agent_tasks.py src/phaze/schemas/pipeline_scans.py` → **All checks passed**
- `uv run ruff format --check src/phaze/schemas/` → **15 files already formatted**
- `uv run mypy src/phaze/schemas/agent_files.py src/phaze/schemas/agent_scan_batches.py src/phaze/schemas/agent_tasks.py src/phaze/schemas/pipeline_scans.py` → **Success: no issues found in 4 source files**
- pre-commit hooks ran on every commit (no `--no-verify`); bandit clean

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Ruff TC003 false positive on `import uuid` in agent_files.py**
- **Found during:** Task 1 (post-implementation `ruff check`)
- **Issue:** Phase 25's `agent_files.py` declared `from __future__ import annotations`. After adding `batch_id: uuid.UUID | None = None`, ruff's TC003 (`typing-only-third-party-import`) suggested moving `import uuid` into an `if TYPE_CHECKING:` block. That suggestion is incorrect for pydantic-validated models: pydantic v2 resolves stringized annotations via `get_type_hints()` at class-creation time, which requires `uuid` to be present in module globals at runtime — not just at type-check time. A `TYPE_CHECKING`-gated import would break model construction at import time.
- **Fix:** Dropped `from __future__ import annotations` from `agent_files.py` (it was the only schema module in `src/phaze/schemas/` that had it; the runtime PEP 604 `|` syntax has been supported natively since Python 3.10 and is the project's `target-version = "py313"`). Result: ruff TC003 no longer fires; runtime behavior unchanged; consistent with the other 10 schema modules.
- **Files modified:** `src/phaze/schemas/agent_files.py`
- **Commit:** d93f496

**2. [Rule 2 - Critical functionality] Promoted JSON-schema LIVE-exclusion check to a standalone test**
- **Found during:** Task 2 (drafting tests)
- **Issue:** The plan's `<action>` block for Task 2 said "Verify that `model_json_schema()["properties"]["status"]["anyOf"]` contains the three Literal values plus null." but did not list this as one of the 8 behavior tests. Schema-layer regression on the D-10 LIVE-guard invariant deserves an explicit grep-able test name.
- **Fix:** Added `test_scan_batch_patch_status_json_schema_excludes_live` as a 9th test. It asserts that the set of Literal alternatives in the rendered JSON schema is exactly `{"running", "completed", "failed"}` — so any future "helpful" widening of the Literal to include `"live"` fails CI loudly.
- **Files modified:** `tests/test_schemas/test_agent_scan_batches.py`
- **Commit:** 1ec37e2

**3. [Rule 1 - Bug] Docstring word collision with acceptance-criterion grep**
- **Found during:** Task 2 (acceptance-criterion verification)
- **Issue:** The plan's acceptance criterion `grep -c "extra=\"forbid\"" src/phaze/schemas/agent_scan_batches.py returns 1 (only on the PATCH body class)` failed because the module docstring contained the literal phrase `extra="forbid"` as part of an explanatory sentence ("The PATCH body class sets `extra=\"forbid\"` per..."). Grep counted that as a second match.
- **Fix:** Reworded the docstring to say "forbids extras" instead of repeating the literal config-key spelling. The actual `model_config = ConfigDict(extra="forbid")` line is now the only literal occurrence, satisfying the acceptance grep count of exactly 1.
- **Files modified:** `src/phaze/schemas/agent_scan_batches.py`
- **Commit:** 1ec37e2

**4. [Rule 1 - Bug] Ruff I001 — import block ordering**
- **Found during:** Task 2 (post-`ruff format` check)
- **Issue:** Initial import block was `import uuid` then `from typing import Literal` then blank then pydantic. Project isort config (`force-sort-within-sections = true`) wants alphabetical sort across the stdlib group, putting `from typing` before `import uuid`.
- **Fix:** `uv run ruff check --fix` reordered to `from typing import Literal\nimport uuid` (alphabetical within the stdlib section). One auto-fix, no behavior change.
- **Files modified:** `src/phaze/schemas/agent_scan_batches.py`
- **Commit:** 1ec37e2

### Out-of-scope discoveries

None. No `deferred-items.md` entries written.

## Output Asks Resolved

Plan `<output>` asked four specific questions:

1. **Whether `tests/test_schemas/` already existed or was newly created** → it pre-existed (Phase 26 created it; contains `test_agent_analysis.py`, `test_agent_identity.py`, `test_agent_proposals.py`, `test_agent_tasks.py`, `test_agent_tracklists.py` already). This plan added 3 new test files into the existing package; no `__init__.py` or directory creation needed.

2. **Exact location chosen for ScanDirectoryPayload in agent_tasks.py** → immediately after `ScanLiveSetPayload`, before the `ExecuteBatchProposalItem` + `ExecuteApprovedBatchPayload` pair. Decision rationale recorded above: keep scan-family payloads adjacent; do not insert non-related classes between the existing `ExecuteApprovedBatchPayload`/`ExecuteBatchProposalItem` unit.

3. **Pydantic v2 quirks encountered** → one significant quirk: **`from __future__ import annotations` is mutually incompatible with ruff's TC003 rule when pydantic validates UUID fields.** Pydantic v2 reads the annotation as a string and calls `typing.get_type_hints()` to resolve it at class-creation time, which requires the symbol to be in module globals at runtime — but ruff TC003 sees the syntactic-level usage as "only inside an annotation" and recommends `TYPE_CHECKING`-gating. Resolution: drop `from __future__` (Python 3.13 already supports PEP 604 `X | Y` natively; project `target-version = "py313"`). All other schema modules in the project already follow this pattern. This is a project-wide convention worth recording for future schema work. — Secondary quirk: Pydantic v2 renders `Literal["a","b","c"] | None` as `anyOf` with one entry containing `enum: ["a","b","c"]` plus an entry with `type: "null"` (NOT three separate `const:` entries). The new JSON-schema test handles both shapes (`enum` set extraction + `const` extraction) for forward compat across pydantic minor versions.

4. **Confirmation that no test failures were observed in Phase 25/26 contract tests after the FileUpsertChunk extension** → confirmed. `uv run pytest tests/test_routers/test_agent_files.py -x -q` → 11 passed (baseline) → 11 passed (after Task 1 commit) → 11 passed (after the full plan). All Phase 25/26 callers omit `batch_id`; the additive optional field with default `None` is fully backwards-compatible.

## TDD Gate Compliance

All three tasks marked `tdd="true"`. RED gate was confirmed explicitly for each task before implementation:

- **Task 1 RED:** `pytest tests/test_schemas/test_agent_files.py -x -q` failed with `AttributeError: 'FileUpsertChunk' object has no attribute 'batch_id'` (test file written first, implementation second).
- **Task 2 RED:** `pytest tests/test_schemas/test_agent_scan_batches.py -x -q` failed with `ModuleNotFoundError: No module named 'phaze.schemas.agent_scan_batches'`.
- **Task 3 RED:** `pytest tests/test_schemas/test_agent_tasks.py tests/test_schemas/test_pipeline_scans.py -x -q` failed with `ImportError: cannot import name 'ScanDirectoryPayload' from 'phaze.schemas.agent_tasks'`.

Following the Phase 25/26/27-01 project precedent, RED-then-GREEN landed in the same commit per task (no separate `test(...)` then `feat(...)` commit pair). Each commit message documents the RED-state evidence in its narrative. No REFACTOR commits were needed — the schemas are simple data containers.

## Known Stubs

None. Every schema class is a fully-realized contract that downstream Plans 03/04/06 will import directly without modification. The schemas accept and produce fully-typed Pydantic models with no placeholder fields.

## Threat Flags

None new beyond the plan's `<threat_model>`. The four documented mitigations are all in place:

- **T-27-02 (cross-tenant batch_id tampering on `FileUpsertChunk`)** — disposition `mitigate (router-layer)` confirmed deferred to Plan 03. Schema-side accepts any UUID; the 403-before-state-machine guard lands at the router.
- **Schema-level: `ScanBatchPatch.status` LIVE-exclusion** — mitigated; `test_scan_batch_patch_rejects_live_status` + `test_scan_batch_patch_status_json_schema_excludes_live` cover both runtime + JSON-schema invariants.
- **Schema-level: all four new schemas reject unknown fields** — mitigated; one `extra="forbid"` test per schema (5 negative tests total).
- **T-27-03 (`TriggerScanForm.subpath` traversal)** — disposition `accept (schema-level)`, deferred to Plan 06 router. No regex-based pre-rejection at the schema layer; this is by design.

## Self-Check: PASSED

**Files exist:**
- FOUND: src/phaze/schemas/agent_scan_batches.py
- FOUND: src/phaze/schemas/pipeline_scans.py
- FOUND: tests/test_schemas/test_agent_files.py
- FOUND: tests/test_schemas/test_agent_scan_batches.py
- FOUND: tests/test_schemas/test_pipeline_scans.py

**Files modified (verified via `git diff --name-only`):**
- FOUND: src/phaze/schemas/agent_files.py
- FOUND: src/phaze/schemas/agent_tasks.py
- FOUND: tests/test_schemas/test_agent_tasks.py

**Commits exist (on `worktree-agent-a15ef8d44376a3635`):**
- FOUND: d93f496 — feat(27-02): add optional batch_id to FileUpsertChunk (D-09)
- FOUND: 1ec37e2 — feat(27-02): add ScanBatchPatch + ScanBatchPatchResponse schemas (D-10)
- FOUND: 0f0b6bc — feat(27-02): add ScanDirectoryPayload + TriggerScanForm schemas (D-14, D-06)
