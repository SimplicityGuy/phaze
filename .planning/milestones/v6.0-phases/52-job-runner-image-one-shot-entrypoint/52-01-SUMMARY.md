---
phase: 52-job-runner-image-one-shot-entrypoint
plan: 01
subsystem: agent-http-client / analysis-wire
tags: [contracts, presign, analysis_wire, tdd, KJOB-02]
requires:
  - phaze.services.agent_client.PhazeAgentClient._request (existing retry funnel)
  - phaze.tasks.functions.process_file (existing converter call sites)
provides:
  - phaze.services.analysis_wire (_features_to_mood_dict / _features_to_style_dict, Postgres-free)
  - phaze.services.agent_client.PhazeAgentClient.request_download_url
  - phaze.schemas.agent_analysis.PresignDownloadResponse
affects:
  - Plan 52-02 (job_runner imports analysis_wire converters + calls request_download_url)
  - Phase 53 (server side of the presign-download endpoint)
tech-stack:
  added: []
  patterns:
    - "one-method-per-endpoint client method routed through the shared _request funnel (D-10/D-11)"
    - "expected_sha256 pinned server-side from FileRecord.sha256_hash (v5.0 ProcessFilePayload precedent)"
key-files:
  created:
    - src/phaze/services/analysis_wire.py
  modified:
    - src/phaze/tasks/functions.py
    - src/phaze/services/agent_client.py
    - src/phaze/schemas/agent_analysis.py
    - tests/test_services/test_agent_client.py
decisions:
  - "Kept the original underscore names (_features_to_mood_dict / _features_to_style_dict) and re-exported them via import in functions.py so existing callers and tests/test_tasks/test_functions.py resolve unchanged (pure relocation, no rename churn)."
  - "Reworded the analysis_wire banner to avoid the literal dotted tokens phaze.database/phaze.models/sqlalchemy so the plan's boundary grep asserts against real imports, not docstring prose."
metrics:
  duration: ~10m
  completed: 2026-06-27
  tasks: 2
  files: 5
requirements: [KJOB-02]
---

# Phase 52 Plan 01: One-Shot Entrypoint Contracts Summary

Defined the two interface contracts the Plan 02 one-shot `job_runner` consumes: a shared, Postgres-free `analysis_wire` module holding the mood/style feature-to-dict converters, and a `request_download_url(file_id) -> (url, expected_sha256)` client method plus the `PresignDownloadResponse` schema it deserializes.

## What Was Built

### Task 1 — Shared `analysis_wire` module (relocation only)
- Created `src/phaze/services/analysis_wire.py` holding `_MOOD_SET_NAMES`, `_features_to_mood_dict`, and `_features_to_style_dict` moved verbatim from `phaze.tasks.functions` (no behavior change).
- The module is stdlib + `typing` only — no database/ORM/SQLAlchemy import — so both the SAQ `process_file` path and the DB-less one-shot pod can import it without crossing the agent import boundary. Enforced green by `tests/test_task_split.py`.
- `functions.py` now imports the two converters from `analysis_wire` (re-exported), so `process_file` usage and `tests/test_tasks/test_functions.py` resolve unchanged.

### Task 2 — `request_download_url` client method + `PresignDownloadResponse` (TDD)
- Added `PresignDownloadResponse(BaseModel)` with `extra="forbid"`, fields `download_url: str` and required `expected_sha256: str` (server-sourced from `FileRecord.sha256_hash`, mirroring v5.0 `ProcessFilePayload.expected_sha256`). The required hash is the only integrity check a Postgres-free pod can perform (Pitfall 3).
- Added `async def request_download_url(self, file_id) -> tuple[str, str]` to `PhazeAgentClient`, mirroring `put_analysis`: lazily imports the response schema, POSTs through the shared `self._request` funnel (5xx retried, 4xx fail-fast — no bespoke retry loop, D-02), validates the JSON, and returns `(download_url, expected_sha256)`.
- Server side (the presign endpoint) is explicitly deferred to Phase 53 (KSTAGE-03); this defines and unit-tests the client only.

## TDD Gate Compliance
- RED gate: `adba14c` — `test(52-01)` adds 5 failing presign tests (`AttributeError: no attribute 'request_download_url'`).
- GREEN gate: `4976dff` — `feat(52-01)` implements the schema + method; all 5 pass.
- No REFACTOR commit needed — the method mirrors `put_analysis` and was clean on first green.

## Verification
- `uv run pytest tests/test_services/test_agent_client.py` — 20 passed (5 new presign cases: happy-path tuple + auth header, 4xx no-retry, 401 auth-error, 5xx retried×3, token absent from WARNING logs).
- `uv run pytest tests/test_tasks/test_functions.py` — 24 passed (converters unchanged post-relocation).
- `uv run pytest tests/test_task_split.py` — 8 passed (import-boundary invariant holds).
- `uv run ruff check .` — clean; `uv run ruff format --check .` — 374 files already formatted.
- `uv run mypy src/phaze/services/agent_client.py src/phaze/schemas/agent_analysis.py` — no issues.

## Acceptance Criteria
- `grep "from phaze.services.analysis_wire import" src/phaze/tasks/functions.py` — match.
- `grep -c "def _features_to_mood_dict" functions.py` = 0; in `analysis_wire.py` = 1.
- Boundary grep (`phaze.database|phaze.models|sqlalchemy`) on `analysis_wire.py` = 0.
- `grep "async def request_download_url" agent_client.py` — match; body contains `self._request(`.
- `grep "class PresignDownloadResponse" agent_analysis.py` — match; declares `expected_sha256`.

## Threat Model Disposition
- T-52-04 (token info disclosure) — mitigated: token stays header-only via `_request`; a test asserts it is absent from WARNING logs.
- T-52-03a (integrity hash provenance) — mitigated: `expected_sha256` is a required field on the `extra="forbid"` response schema.
- T-52-05 (presign replay) — accepted as documented; short-TTL minting + replay defense is the Phase 53 server contract. The client requests a fresh presign per call.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Boundary banner reworded to satisfy the import-only grep**
- **Found during:** Task 1 acceptance check.
- **Issue:** The plan's boundary assertion `grep -Ev '^#' analysis_wire.py | grep -Ec "phaze\.database|phaze\.models|sqlalchemy"` filters only `#`-comment lines, not `"""` docstring lines; the banner's literal dotted tokens (describing the forbidden imports) tripped it (returned 2).
- **Fix:** Reworded the banner to "no database, ORM-model, or SQLAlchemy imports" so the grep asserts against real imports (now returns 0). No code/behavior change.
- **Files modified:** src/phaze/services/analysis_wire.py
- **Commit:** 6dcafa3

### Notes
- The `process_file or functions or analysis_wire` keyword selection also matches `tests/test_reenqueue.py`, which requires a live Postgres (port 5432); it ERRORed on setup in this sandbox (no DB). This is environmental and unrelated to the relocation — the converter unit tests and the import-boundary test both pass.

## Commits
- `6dcafa3` refactor(52-01): relocate mood/style converters to shared analysis_wire module
- `adba14c` test(52-01): add failing tests for request_download_url presign client method (RED)
- `4976dff` feat(52-01): add request_download_url client method + PresignDownloadResponse schema (GREEN)

## Self-Check: PASSED
- FOUND: src/phaze/services/analysis_wire.py
- FOUND: commit 6dcafa3, adba14c, 4976dff
