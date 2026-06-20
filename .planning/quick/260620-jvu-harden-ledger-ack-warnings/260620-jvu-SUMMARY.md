---
phase: quick-260620-jvu
plan: 01
subsystem: agent-worker-tasks / agent-schemas
tags: [phase-45, ledger, terminal-ack, exception-masking, pydantic-literal, WR-01, WR-02]
requires:
  - Phase 45 ledger terminal-ack endpoints + agent-worker guards (plans 45-05, 45-06)
provides:
  - Exception-masking-free terminal-ack handling in all three agent task failure paths
  - Machine-enforced cleared=Literal[True] invariant on both failure-response schemas
affects:
  - src/phaze/tasks/scan.py
  - src/phaze/tasks/metadata_extraction.py
  - src/phaze/tasks/fingerprint.py
  - src/phaze/schemas/agent_metadata.py
  - src/phaze/schemas/agent_fingerprint.py
tech-stack:
  added: []
  patterns:
    - "Nested try/except around best-effort terminal ack: swallow+log E2, always re-raise original E1"
    - "Pydantic Literal[True] to machine-enforce an always-True response invariant"
key-files:
  created: []
  modified:
    - src/phaze/tasks/scan.py
    - src/phaze/tasks/metadata_extraction.py
    - src/phaze/tasks/fingerprint.py
    - src/phaze/schemas/agent_metadata.py
    - src/phaze/schemas/agent_fingerprint.py
    - tests/test_tasks/test_scan.py
    - tests/test_tasks/test_metadata_extraction.py
    - tests/test_tasks/test_fingerprint.py
    - tests/test_routers/test_agent_metadata.py
    - tests/test_routers/test_agent_fingerprint.py
    - .planning/phases/45-scheduling-ledger-for-orphan-recovery/45-REVIEW.md
decisions:
  - "Swallow+log the terminal-ack failure (E2) and always re-raise the original task error (E1); a one-time ledger-row leak is reconciled by the recovery sweep, whereas masking E1 corrupts SAQ's failure record"
  - "Place WR-02 schema-rejection tests in the existing router test files (no DB fixture used) so the Task 2 verify command finds them"
metrics:
  duration: ~12 min
  completed: 2026-06-20
---

# Quick Task 260620-jvu: Harden Ledger Terminal-Ack Warnings Summary

Closed the two advisory warnings from Phase 45's 45-REVIEW.md: removed exception-masking in the three terminal-ack failure handlers (WR-01) and made the failure-response `cleared` field a `Literal[True]` invariant (WR-02).

## What Was Done

### Task 1 (WR-01) — guard the three terminal-ack except blocks (commit d9123af)
Wrapped the terminal ack call in `scan.py` (match-failure path), `metadata_extraction.py`, and `fingerprint.py` in a nested `try/except` that swallows + logs on the terminal attempt, mirroring the already-shipped no-match guard at `scan.py:106-113`. The trailing bare `raise` now always re-raises the ORIGINAL task error (E1) instead of being masked when the ack also raises (E2). Added a terminal-ack-failure test to each of the three task test files asserting the original error type/message propagates via `pytest.raises`.

Behavioral note (as specified): unlike the no-match path (which RETURNS the `no_matches` COMPLETE after swallowing), the three failure paths ALWAYS re-raise E1 after swallowing the ack failure — SAQ must record the real task failure.

### Task 2 (WR-02) — cleared: Literal[True] invariant (commit d992f84)
Added `from typing import Literal` and changed `cleared: bool` -> `cleared: Literal[True]` in `MetadataFailureResponse` and `FingerprintFailureResponse`. Pydantic now rejects `cleared=False` with a `ValidationError`. Added four pure-construction tests (cleared=True accepted, cleared=False rejected) to the two router test files; these need no DB.

### Task 3 — mark resolved in 45-REVIEW.md (commit e80d2c1)
Frontmatter `findings.warning` 2->0, `total` 3->1, `status` `issues_found`->`resolved`. Added explicit resolved markers under both warning headings and a closing note in the Summary, referencing this quick pass and the fix commits. WR-01/WR-02 bodies preserved; IN-01 (info) left untouched as out of scope.

## Deviations from Plan

None — plan executed exactly as written.

## Verification

- `uv run pytest tests/test_tasks/test_scan.py tests/test_tasks/test_metadata_extraction.py tests/test_tasks/test_fingerprint.py tests/test_task_split.py -q` — 41 passed (includes 3 new WR-01 tests; agent-worker Postgres-free boundary intact).
- `uv run pytest tests/test_routers/test_agent_metadata.py tests/test_routers/test_agent_fingerprint.py -q -k "cleared or Literal or failure"` — 4 passed (no DB).
- Full DB-backed router suites (ephemeral Postgres+Redis via `just test-db`) — 24 passed; existing `cleared is True` assertions still green.
- `uv run ruff check` + `ruff format --check` + `uv run mypy` on all touched src files — clean.
- All grep acceptance criteria for Tasks 1-3 satisfied.

## Self-Check: PASSED

- src/phaze/tasks/scan.py — FOUND
- src/phaze/tasks/metadata_extraction.py — FOUND
- src/phaze/tasks/fingerprint.py — FOUND
- src/phaze/schemas/agent_metadata.py — FOUND
- src/phaze/schemas/agent_fingerprint.py — FOUND
- .planning/phases/45-scheduling-ledger-for-orphan-recovery/45-REVIEW.md — FOUND
- Commit d9123af — FOUND
- Commit d992f84 — FOUND
- Commit e80d2c1 — FOUND
