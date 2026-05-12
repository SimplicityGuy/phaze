---
phase: 26-task-code-reorg-http-backed-agent-worker
plan: 03
subsystem: schemas
tags: [python, pydantic, http-api, saq-payloads, http-boundary]

# Dependency graph
requires:
  - phase: 26-task-code-reorg-http-backed-agent-worker
    provides: "ProposalStatus.EXECUTED/FAILED + FileState.MOVED/UNCHANGED enum values (Plan 01) — agent_proposals Literal strings mirror these"
provides:
  - "AgentIdentity response model for GET /whoami (D-15)"
  - "AnalysisWritePayload + AnalysisWriteResponse for PUT /analysis/{file_id} (D-26)"
  - "TracklistTrackPayload + TracklistCreatePayload (with request_id idempotency + tracks max=2000 DoS cap) + TracklistCreateResponse for POST /tracklists (D-27, T-26-07-DoS)"
  - "ProposalStatePatch (with _require_path_when_moved validator) + ProposalStateResponse for PATCH /proposals/{id}/state (D-28)"
  - "5 SAQ-job payload models (ProcessFile, ExtractMetadata, FingerprintFile, ScanLiveSet, ExecuteApprovedBatch) + nested ExecuteBatchProposalItem (D-22..D-24)"
affects:
  - "Plan 02 (PhazeAgentClient — already imports these via TYPE_CHECKING; imports now resolve at runtime)"
  - "Plan 05/06/07/08 (Wave 2 routers — consume these as FastAPI body/response types)"
  - "Plan 11 (Wave 3 task rewrites — model_validate(ctx kwargs) at every task entry)"

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "ConfigDict(extra='forbid') on every request body AND every nested item (per-class, not inherited)"
    - "model_validator(mode='after') for cross-field invariants (current_path required when file_state='moved')"
    - "Literal[...] string types mirroring StrEnum string values for state-machine wire types"
    - "Bounded list fields: Field(min_length=1, max_length=N) for DoS hardening at the boundary"

key-files:
  created:
    - "src/phaze/schemas/agent_identity.py"
    - "src/phaze/schemas/agent_analysis.py"
    - "src/phaze/schemas/agent_tracklists.py"
    - "src/phaze/schemas/agent_proposals.py"
    - "src/phaze/schemas/agent_tasks.py"
    - "tests/test_schemas/__init__.py"
    - "tests/test_schemas/test_agent_identity.py"
    - "tests/test_schemas/test_agent_analysis.py"
    - "tests/test_schemas/test_agent_tracklists.py"
    - "tests/test_schemas/test_agent_proposals.py"
    - "tests/test_schemas/test_agent_tasks.py"
  modified: []

key-decisions:
  - "Removed unused `# noqa: TC003` directive on `from datetime import datetime` in agent_identity.py — Pydantic resolves the annotation at runtime, so the import is used and the suppression is unused. Ruff RUF100 enforced removal. (Rule 1 deviation; see below.)"
  - "Dropped `from __future__ import annotations` in agent_tasks.py — deferred annotations would trigger ruff TC003 on the runtime-needed `import uuid`. Mirrors the existing `agent_metadata.py` pattern. (Rule 1 deviation; see below.)"

patterns-established:
  - "Nested-item schema pattern: every list-item model (TracklistTrackPayload, ExecuteBatchProposalItem) sets its own ConfigDict(extra='forbid') because Pydantic per-class ConfigDict is NOT inherited from the parent schema."
  - "Response-only schemas (AgentIdentity, *Response models) deliberately omit `extra='forbid'` so the server can add fields non-breakingly; agent-side Pydantic discards unknown keys silently."
  - "model_validator(mode='after') for conditional-field invariants: ProposalStatePatch raises ValidationError when file_state='moved' without current_path, returning 422 at the boundary before any DB work."
  - "tests/test_schemas/ subdirectory established for module-focused unit tests (independent of router/database integration tests)."

requirements-completed:
  - TASK-02
  - TASK-03
  - DIST-03

# Metrics
duration: 10 min
completed: 2026-05-12
---

# Phase 26 Plan 03: Schema modules for HTTP-backed agent worker — Summary

**5 new Pydantic schema modules (4 HTTP-boundary + 1 SAQ-payload bundle) ship the wire-level types every Wave 2 router and Wave 3 task rewrite depends on, with `extra='forbid'` strictness, a conditional `_require_path_when_moved` validator, and bounded list caps for DoS hardening.**

- Start: 2026-05-12T21:20:32Z
- End:   2026-05-12T21:31:04Z
- Duration: 10 min
- Tasks: 2 / 2 complete (both TDD)
- Files created: 11 (5 source modules + 6 test files including __init__)
- Tests: 56 unit tests, all green; 582 non-integration tests pass overall
- Commits: 4 (1 RED + 1 GREEN per task)

## What shipped

### 4 HTTP-boundary schemas (Task 1)

| Module | Classes | Purpose |
|---|---|---|
| `agent_identity.py` | `AgentIdentity` | Response of `GET /whoami` (D-15). Response-only — loose schema. |
| `agent_analysis.py` | `AnalysisWritePayload`, `AnalysisWriteResponse` | `PUT /analysis/{file_id}` (D-26). All fields optional for partial-PUT; `bpm >= 0`; `danceability`/`energy` in `[0, 1]`. |
| `agent_tracklists.py` | `TracklistTrackPayload`, `TracklistCreatePayload`, `TracklistCreateResponse` | `POST /tracklists` (D-27). `request_id: uuid.UUID` for Stripe-style idempotency; `tracks: Field(min_length=1, max_length=2000)` per T-26-07-DoS. Nested item also `extra='forbid'`. |
| `agent_proposals.py` | `ProposalStatePatch`, `ProposalStateResponse` | `PATCH /proposals/{id}/state` (D-28). `Literal['executed', 'failed']` mirrors `ProposalStatus` enum string values; `_require_path_when_moved` model_validator enforces `current_path` is mandatory when `file_state='moved'`. |

### 5 SAQ-job payload models (Task 2)

`src/phaze/schemas/agent_tasks.py` ships 6 classes (5 task payloads + 1 nested item):

| Class | D-22..D-24 invariant |
|---|---|
| `ProcessFilePayload` | Only payload with `models_path` (essentia .pb files). |
| `ExtractMetadataPayload` | Mutagen tag extraction; no `models_path`, no `current_path`. |
| `FingerprintFilePayload` | Audfprint + panako sidecars; minimal 3-field payload. |
| `ScanLiveSetPayload` | Live-set fingerprint resolution; minimal 3-field payload. |
| `ExecuteBatchProposalItem` (nested) | Per-proposal: `proposal_id`, `file_id`, `original_path`, `proposed_path`, optional `sha256_hash`. **No `current_path`** (D-24). |
| `ExecuteApprovedBatchPayload` | `proposals: list[ExecuteBatchProposalItem]` capped `Field(min_length=1, max_length=500)`. B2 Option A — fully self-contained (D-23 invariant). |

## Decisions Made

- **D-15 / D-26 / D-27 / D-28 wire types finalized** as specified in CONTEXT.md — no semantic deviations.
- **B2 Option A confirmed for `ExecuteApprovedBatchPayload`**: the payload now carries full per-proposal `original_path` + `proposed_path` + optional `sha256_hash` so Plan 11 can implement `execute_approved_batch` fully without DB read-back. The old `proposal_ids: list[UUID]` shape is REMOVED.
- **T-26-07-DoS hardening**: `tracks` capped at 2000 (well above realistic ~200-300 live-set tracks); `proposals` capped at 500 per batch.
- **`extra='forbid'` everywhere except response models** — request bodies and nested items reject unknown keys with `extra_forbidden` (422 at the boundary); response schemas stay loose for forward-compat.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Removed unused `# noqa: TC003` in `agent_identity.py`**
- **Found during:** Task 1 ruff verification
- **Issue:** The plan's verbatim source contained `from datetime import datetime  # noqa: TC003 — Pydantic resolves Mapped-style annotations at runtime`. Ruff RUF100 flagged the `noqa: TC003` as unused because the import IS used at runtime (Pydantic resolves the annotation) and TC003 would not have fired without the suppression.
- **Fix:** Removed the unused suppression comment. Import stays, TC003 doesn't fire (no `from __future__ import annotations`), no further changes needed.
- **Files modified:** `src/phaze/schemas/agent_identity.py`
- **Commit:** 55fd5ea (folded into the Task 1 GREEN commit)
- **Verification:** `uv run ruff check src/phaze/schemas/agent_identity.py` exits 0.

**2. [Rule 1 - Bug] Dropped `from __future__ import annotations` in `agent_tasks.py`**
- **Found during:** Task 2 ruff verification
- **Issue:** The plan's verbatim source had `from __future__ import annotations` AND `import uuid`. Under deferred annotations, ruff TC003 demanded `uuid` be moved into a `TYPE_CHECKING` block — but Pydantic resolves `uuid.UUID` annotations at runtime and requires the import to be present at runtime. The existing Phase 25 `agent_metadata.py` solves this by omitting `from __future__ import annotations` entirely.
- **Fix:** Removed `from __future__ import annotations`. Python 3.13 native types (`list[X]`, `str | None`) work without it; uuid stays as a runtime import; TC003 doesn't fire.
- **Files modified:** `src/phaze/schemas/agent_tasks.py`
- **Commit:** fe6f382 (folded into the Task 2 GREEN commit)
- **Verification:** `uv run ruff check src/phaze/schemas/agent_tasks.py` exits 0; all 22 unit tests still pass.

**Total deviations:** 2 auto-fixed (both Rule 1 — fixing ruff lint violations introduced by the plan's verbatim source). **Impact:** zero behavioral change. The shipped modules match every other acceptance criterion exactly (extra='forbid' counts, class names, field names, max_length caps, validator wiring). Future planners should drop the `# noqa: TC003` directive and the `from __future__ import annotations` line from this schema template when copying it.

## Validation evidence

| Check | Command | Result |
|---|---|---|
| Module imports | `uv run python -c 'from phaze.schemas import agent_identity, agent_analysis, agent_tracklists, agent_proposals, agent_tasks'` | exit 0 |
| Mypy strict (5 files) | `uv run mypy src/phaze/schemas/agent_*.py` (the new 5) | `Success: no issues found in 5 source files` |
| Ruff (whole dir) | `uv run ruff check src/phaze/schemas/` | `All checks passed!` |
| Unit tests (Plan 26-03) | `uv run pytest tests/test_schemas/ --no-cov` | 56 passed, 0 failed |
| Full non-integration suite | `uv run pytest -m "not integration" --no-cov` | 582 passed, 0 failed |
| Pre-commit (all files) | `pre-commit run --all-files` | all hooks pass |

## Authentication Gates

None.

## Threat Flags

None — every new wire-level field was specified in the plan's `<threat_model>` register. The 4 boundary-mitigation controls (`extra='forbid'`, `request_id` typed UUID, `_require_path_when_moved`, `Field(min_length=1, max_length=N)` caps) are all in place and exercised by the unit tests.

## Known Stubs

None.

## TDD Gate Compliance

Both tasks followed RED → GREEN cleanly with separate commits per gate:

| Task | RED commit | GREEN commit |
|---|---|---|
| Task 1 (4 HTTP schemas) | `2540694 test(26-03): add failing tests for 4 HTTP-boundary schema modules` | `55fd5ea feat(26-03): add 4 HTTP-boundary schemas for agent endpoints` |
| Task 2 (agent_tasks)    | `aa33339 test(26-03): add failing tests for agent_tasks SAQ payload schemas` | `fe6f382 feat(26-03): add 5 SAQ-job payload schemas in agent_tasks.py` |

Each RED commit was verified to fail (`ModuleNotFoundError`); each GREEN commit was verified to pass all tests, mypy, ruff, and per-task acceptance-criteria grep checks. No REFACTOR commits needed — the auto-fix tweaks (removing unused noqa + `from __future__`) were folded into the GREEN commits since they were one-line lint corrections to the just-written files.

## Plan-level success criteria

- [x] 4 HTTP-boundary schemas exist (agent_identity, agent_analysis, agent_tracklists, agent_proposals)
- [x] 1 task-payload schema file exists (agent_tasks with 6 classes: 5 payloads + 1 nested item)
- [x] Every request schema has `model_config = ConfigDict(extra="forbid")`
- [x] Every nested item schema (TracklistTrackPayload, ExecuteBatchProposalItem) also has `extra="forbid"`
- [x] ProposalStatePatch has `_require_path_when_moved` model_validator
- [x] TracklistCreatePayload.tracks has `Field(min_length=1, max_length=2000)` cap (T-26-07-DoS)
- [x] ExecuteApprovedBatchPayload.proposals has `Field(min_length=1, max_length=500)` and carries full ExecuteBatchProposalItem with original_path + proposed_path (B2 Option A)
- [x] No agent_id field in any HTTP schema (auth dep derives it from token)
- [x] No current_path field in any agent_tasks payload (D-24)
- [x] mypy + ruff clean on every new file
- [x] All 5 modules importable
- [x] Git commit recorded; pre-commit hooks all green

## Self-Check: PASSED

All 5 source modules + 6 test files exist on disk; all 4 task commits (`2540694`, `55fd5ea`, `aa33339`, `fe6f382`) are reachable from HEAD via `git log --oneline`. All plan-level verifications pass.

## Next

Ready for Plan 26-04. Plan 02 (PhazeAgentClient, running in parallel) can now resolve its `TYPE_CHECKING` imports against these concrete modules.
