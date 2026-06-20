---
phase: 45-scheduling-ledger-for-orphan-recovery
plan: 06
subsystem: agent-boundary / scheduling-ledger
tags: [ledger, recovery, terminal-failure, agent-boundary, CR-02, L-02]
requires:
  - "scheduling_ledger.clear_ledger_entry (control-side DELETE-by-key)"
  - "agent terminal-attempt guard precedent (process_file functions.py:179-189)"
  - "analyze /failed precedent (agent_analysis.py report_analysis_failed)"
provides:
  - "POST /api/internal/agent/metadata/{file_id}/failed -> clears extract_file_metadata:<file_id>"
  - "POST /api/internal/agent/fingerprints/{file_id}/failed -> clears fingerprint_file:<file_id> (single per-file)"
  - "agent_client.report_metadata_failed + report_fingerprint_failed (httpx-only)"
  - "metadata/fingerprint agent-worker terminal-failure acks"
affects:
  - "recover_orphaned_work (CR-02 loop closed: terminally-failed metadata/fingerprint rows are cleared, never re-enqueued)"
tech-stack:
  added: []
  patterns:
    - "Terminal-ack callback per agent stage (NEW /failed endpoint + client method + worker guard) mirroring analyze/scan precedent"
    - "Clear key reconstructed control-side from fixed function name + PATH file_id ONLY (AUTH-01 / T-45-05)"
key-files:
  created: []
  modified:
    - src/phaze/schemas/agent_metadata.py
    - src/phaze/schemas/agent_fingerprint.py
    - src/phaze/routers/agent_metadata.py
    - src/phaze/routers/agent_fingerprint.py
    - src/phaze/services/agent_client.py
    - src/phaze/tasks/metadata_extraction.py
    - src/phaze/tasks/fingerprint.py
    - tests/test_routers/test_agent_metadata.py
    - tests/test_routers/test_agent_fingerprint.py
    - tests/test_tasks/test_metadata_extraction.py
    - tests/test_tasks/test_fingerprint.py
    - tests/test_tasks/test_recovery.py
decisions:
  - "Approach A (analyze precedent): per-stage terminal-ack callback over reworking is_domain_completed -- deterministic, self-contained per file, keeps the agent boundary clean"
  - "No request body on the metadata/fingerprint /failed endpoints (no terminal state persisted) -- minimal *FailureResponse echo with cleared:bool, matching the scan-terminal ack shape"
  - "Single per-file fingerprint clear key (NOT per engine); the /failed path takes no engine"
metrics:
  duration: ~25 min
  completed: 2026-06-19
---

# Phase 45 Plan 06: Metadata + Fingerprint Terminal-Failure Ledger Clear (CR-02) Summary

Gave `extract_file_metadata` and `fingerprint_file` a terminal-failure scheduling-ledger clear via two new control-side `POST /{file_id}/failed` endpoints, mirroring the shipped analyze precedent, closing the CR-02 unbounded recovery re-enqueue loop.

## What Was Built

**Task 1 — `/failed` endpoints + client methods (commit 79e9964):**
- `MetadataFailureResponse` / `FingerprintFailureResponse` echo schemas (`agent_id`, `file_id`, `cleared: bool`).
- `POST /api/internal/agent/metadata/{file_id}/failed` clears `extract_file_metadata:<file_id>`; `POST /api/internal/agent/fingerprints/{file_id}/failed` clears the single-per-file `fingerprint_file:<file_id>`. Auth via `get_authenticated_agent`; clear key from the PATH `file_id` only; absent-row is a clean 200 no-op; the endpoints write no FileState (clearing the ledger row is the sole control-side effect).
- `agent_client.report_metadata_failed(file_id)` + `report_fingerprint_failed(file_id)` — authenticated bodyless POSTs, function-local response import, httpx-only (no DB import).
- 6 handler tests (clear / absent-no-op / no-redirect for each stage).

**Task 2 — agent-worker guards + recovery regression (commit effc15e):**
- Wrapped the failure-prone region of each task in `try/except` mirroring `process_file` (functions.py:179-189): ack via `report_*_failed` ONLY on `job is not None and not job.retryable`, then re-raise. Retryable / job-absent attempts re-raise without acking so the row survives for the real retry (T-45-06).
- 9 task tests (terminal-acks-once / retryable-no-ack / job-absent-no-ack / success-no-ack; fingerprint also covers a failure mid per-engine PUT loop).
- 2 recovery regression tests proving a CLEARED metadata/fingerprint ledger row is NOT re-enqueued by `recover_orphaned_work` even though its file remains in the pending set (so the broken `is_domain_completed` predicate can never fire) — demonstrating the CLEAR, not the predicate, closes the loop.

## How CR-02 Is Closed

Before this plan the only ledger clear for either stage was on the success PUT; there was no `/failed` callback. Because `get_metadata_pending_files` returns ALL music/video files and `get_fingerprint_pending_files` returns METADATA_EXTRACTED + `FingerprintResult(status="failed")` rows, a terminally-failed file stayed in the pending set forever → `is_domain_completed` never fired → `recover_orphaned_work` re-enqueued it on every pass. The new terminal-ack removes the ledger row on the retries-exhausted attempt, so recovery never sees the row again — independent of the predicate, which becomes belt-and-suspenders.

## Deviations from Plan

None — plan executed exactly as written. The success-clear handler tests and the metadata/fingerprint pending/done recovery tests already existed from Plan 02; this plan added the `/failed`-specific handler tests, the worker-guard task tests, and the explicit cleared-row CR-02 regression tests.

## Verification

- `uv run pytest tests/test_routers/test_agent_metadata.py tests/test_routers/test_agent_fingerprint.py tests/test_tasks/test_metadata_extraction.py tests/test_tasks/test_fingerprint.py tests/test_tasks/test_recovery.py -q` — 68 passed (20 router + 48 task/recovery).
- `uv run pytest tests/test_task_split.py -q` — 7 passed (agent worker + agent_client stay Postgres-free; L-05 held).
- `uv run mypy` on all 7 source files — clean.
- `uv run pytest --cov` — 1988 passed, total coverage 97.49% (>=85%); all 7 modified source files at 100% except none below threshold.
- `uv run ruff check` on all changed files — clean.
- pre-commit ran on both commits (no `--no-verify`); all hooks passed.

Acceptance-criteria greps all confirmed: `/{file_id}/failed` + `report_*_failed` present in both routers and the client; `extract_file_metadata:` / `fingerprint_file:` clear keys present; `not job.retryable` guard count >=1 in each task; no `phaze.database`/`phaze.models`/`sqlalchemy` imports in either task or `agent_client`.

## Threat Coverage

- T-45-18 (CR-02 recovery loop) — mitigated: the cleared-row recovery regression tests prove a terminally-failed metadata/fingerprint job is never re-enqueued.
- T-45-05 (spoofing the clear key) — mitigated: clear key from PATH file_id only; no-redirect handler tests.
- T-45-02 (boundary violation) — mitigated: ack is httpx-only; `test_task_split.py` gate green.
- T-45-06 (premature ack) — accepted-by-design and tested: retryable/job-absent attempts do not ack.

## Self-Check: PASSED
- Commit 79e9964 found in git log.
- Commit effc15e found in git log.
- All 12 modified files exist on disk.
