---
phase: 45-scheduling-ledger-for-orphan-recovery
plan: 02
subsystem: agent-callbacks
tags: [saq, ledger, recovery, agent-stage, fastapi, callbacks, scan, analyze, metadata, fingerprint]

# Dependency graph
requires:
  - phase: 45-scheduling-ledger-for-orphan-recovery
    plan: 01
    provides: "control-only clear_ledger_entry(session, key) + deterministic ledger keys + SchedulingLedger model"
provides:
  - "agent-stage ledger clears wired into the existing control-side callback handlers (analyze success + terminal-failure, metadata, fingerprint)"
  - "scan_live_set ledger clear on every terminal outcome: match via create_tracklist owner-path; no-match/failure via a NEW POST /tracklists/{file_id}/scanned terminal-ack endpoint"
  - "report_scan_terminal agent-client method (httpx-only, agent-import-safe)"
  - "ScanTerminalAckResponse schema"
affects: [45-03-recovery-rewrite]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "agent-stage terminal outcome becomes control-visible only at the HTTP callback -- the ledger clear rides the existing result-write transaction"
    - "terminal-ack endpoint pattern for a stage whose no-match COMPLETE posts no domain row (scan_live_set)"
    - "not job.retryable guard (mirrors functions.py) so only the retries-exhausted attempt acks"

key-files:
  created: []
  modified:
    - src/phaze/routers/agent_analysis.py
    - src/phaze/routers/agent_metadata.py
    - src/phaze/routers/agent_fingerprint.py
    - src/phaze/routers/agent_tracklists.py
    - src/phaze/schemas/agent_tracklists.py
    - src/phaze/services/agent_client.py
    - src/phaze/tasks/scan.py
    - tests/test_routers/test_agent_analysis.py
    - tests/test_routers/test_agent_metadata.py
    - tests/test_routers/test_agent_fingerprint.py
    - tests/test_routers/test_agent_tracklists.py
    - tests/test_tasks/test_scan.py

key-decisions:
  - "Clear keys are reconstructed control-side from the fixed per-endpoint function name + a trusted natural id (PATH file_id, or body.file_id on create_tracklist), with agent identity bound from the auth token -- never an attacker-chosen field (T-45-05)."
  - "scan_live_set's no-match COMPLETE and retries-exhausted failure ack via a dedicated terminal-ack endpoint (Option a) rather than a FileState-derived predicate (Option b) -- a dedicated ack is unambiguous and self-clearing."
  - "The match-path failure ack fires ONLY on the terminal (not job.retryable) attempt, so a retryable attempt leaves the ledger row intact for the real retry (T-45-06)."

patterns-established:
  - "Per-agent-stage ledger clear lives in the control-side callback, NOT in the agent after_process (the agent worker is Postgres-free)."
  - "A stage whose no-result outcome posts no domain row gets a terminal-ack endpoint so recovery can never loop on a legitimate no-result."

requirements-completed: ["L-02 (agent half)", "L-05"]

# Metrics
duration: ~35min
completed: 2026-06-19
---

# Phase 45 Plan 02: Agent-Stage Ledger Clear Summary

**Agent-stage scheduling-ledger rows are now cleared CONTROL-side, in the HTTP callbacks the agent already invokes (plus one new terminal-ack endpoint for scan's no-match/failure hole), so recovery never re-runs finished agent work and the analyze poison case no longer re-queues.**

## Performance

- **Duration:** ~35 min
- **Completed:** 2026-06-19
- **Tasks:** 2/2
- **Files modified:** 12 (0 created, 12 modified)

## Accomplishments

### Task 1 — analyze + metadata + fingerprint callback clears (commit 817f819)
- `agent_analysis.put_analysis`: clears `process_file:<file_id>` BEFORE the existing `session.commit()`, riding the same transaction as the `ANALYZED` state write.
- `agent_analysis.report_analysis_failed`: clears `process_file:<file_id>` in the same transaction as the `ANALYSIS_FAILED` write — **the locked decision #1 poison-case clear** (essentia timeout/crash/terminal-error no longer recovery-re-queues).
- `agent_metadata.put_metadata`: clears `extract_file_metadata:<file_id>` before commit.
- `agent_fingerprint.put_fingerprint`: clears `fingerprint_file:<file_id>` — a SINGLE key per file (NOT per engine), so a second-engine PUT is a clean no-op.
- All keys use the PATH `file_id` exclusively (AUTH-01 / T-45-05). The clear rides the existing commit — no extra commit added.
- Tests: success-clears (all three), failed-clears (analyze poison case), absent-no-op, body/path-only-key no-redirect, second-engine no-op (fingerprint). 36 router tests pass.

### Task 2 — scan_live_set clear: match path + terminal-ack endpoint (commit 130987d)
- `agent_tracklists.create_tracklist`: clears `scan_live_set:<body.file_id>` inside the OWNER-path transaction before the single commit (the MATCH outcome). The fast-path/cached return does NO clear (no DB work).
- **NEW** `POST /api/internal/agent/tracklists/{file_id}/scanned` (`ack_scan_terminal`): clears `scan_live_set:<file_id>` from the PATH file_id only; auth via `get_authenticated_agent`; absent-key clear is a no-op (still 200). Returns `ScanTerminalAckResponse(file_id, cleared=True)`.
- `agent_client.report_scan_terminal(file_id)`: authenticated POST to the new endpoint; httpx-only, NO DB import (agent stays Postgres-free; `test_task_split.py` green).
- `tasks/scan.scan_live_set`: acks on the `no_matches` early return; on a match-path terminal exception acks ONLY when `not job.retryable`, then re-raises. The match success clears via `create_tracklist` — no double-ack.
- Net invariant: EVERY scan run clears `scan_live_set:<file_id>` exactly once (match → create_tracklist; no-match/failure → ack). Blocker 2 / T-45-16 closed.
- `agent_scan_batches.py` untouched (Blocker 1). Tests: owner-path clears, cached-replay does NOT clear, ack clears + absent-no-op + path-only no-redirect + auth-required; scan-task no-match-acks-once, match-no-double-ack, terminal-failure-acks-then-raises, retryable-does-not-ack, no-job-in-ctx-does-not-ack. 30 tests pass.

## Deviations from Plan

None — plan executed exactly as written. The pre-existing `agent_scan_batches` import in `scan.py` (`ScanBatchPatch`, used by the unrelated `scan_directory` task) is left untouched; the Blocker 1 intent (do NOT wire the scan clear to scan_batches) is honored — no `agent_scan_batches` reference was added by this plan and `agent_tracklists.py` has zero references.

## Threat Mitigations Applied

- **T-45-05 (Spoofing — clear-key redirect):** every clear key is built from the PATH `file_id` (analyze/metadata/fingerprint, ack endpoint) or `body.file_id` (create_tracklist, the trusted tracklist target) + the fixed function name; agent identity from the auth dep. Tested by `*_clear_uses_path_file_id_not_redirected` (another file's row survives).
- **T-45-06 (premature clear of a still-running scan):** the match-path ack fires only on `not job.retryable`; `test_scan_match_retryable_failure_does_not_ack` proves a retryable attempt leaves the row.
- **T-45-07 (DoS — clear failing the callback):** the clear is a plain DELETE riding the existing transaction (or the sole DB op of the ack endpoint); an absent-row clear is a no-op (tested).
- **T-45-16 (recovery loop on a legitimate no-match):** the terminal-ack closes the no-match hole; `test_scan_no_matches_returns_no_matches` asserts the single ack.
- **T-45-SC (supply chain):** no new packages.

## Verification

- `uv run pytest tests/test_routers/test_agent_analysis.py tests/test_routers/test_agent_metadata.py tests/test_routers/test_agent_fingerprint.py tests/test_routers/test_agent_tracklists.py tests/test_tasks/test_scan.py tests/test_task_split.py -q` → **66 passed** (against the ephemeral test DB on :5433 + Redis on :6380).
- `uv run mypy <all 7 source files>` → clean.
- `uv run ruff check .` → All checks passed.
- Pre-commit hooks passed on both task commits (no `--no-verify`).

## Residual Gap (documented, accepted — from the plan)

A metadata/fingerprint job that exhausts retries WITHOUT any callback leaves its ledger row uncleared and is re-attempted on the next recovery. This degrades to "recoverable" (benign transient I/O, not the poison auth/connect case) and is the secondary safety net in Plan 03's domain-completed exclusion. Decision #1 is honored for every poison case (analyze /failed clears; controller proposal/auth-connect cleared via Plan 01's after_process).

## Known Stubs

None — every clear is fully wired.

## Notes for Downstream Plans

- **Plan 03 (recovery rewrite):** with agent-stage clears in place, `recover_orphaned_work` can drive off `get_ledger_rows(session)` minus `get_live_job_keys(session)`; the metadata/fingerprint residual gap should be covered by the domain-completed exclusion as planned.

## Self-Check: PASSED

Both task commits present on the worktree branch (817f819 feat 45-02 analyze/metadata/fingerprint; 130987d feat 45-02 scan terminal-ack). All 12 modified files confirmed via `git show --stat`. 66/66 tests green; mypy + ruff clean.
