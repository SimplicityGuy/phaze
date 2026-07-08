---
phase: 73-per-agent-dispatch-liveness-scratch-failure-isolation
plan: 03
subsystem: cloud-compute-dispatch
tags: [push-callbacks, backend-attribution, reporter-authorization, per-agent-dispatch]
requires:
  - resolve_compute_backend (Plan 01, D-06 authoritative backend_id -> ComputeBackend)
  - PushFilePayload.dest_host / dest_scratch_dir / dest_ssh_user (Plan 01, validated per-file destination)
  - ComputeBackend.push_host / scratch_dir / agent_ref / ssh_user (Plan 01)
provides:
  - backend_id-scoped /pushed (D-06 record-don't-rederive scratch + compute-queue routing)
  - reporter-validated /mismatch (D-07 agent.id == backend.agent_ref gate, 403 reject-don't-terminalize)
  - destination-re-stamped /mismatch re-drive (Landmine 1, never a destination-less push)
affects:
  - Plan 04 (deletes the now-unused active_compute_scratch_dir accessor read from /pushed)
tech-stack:
  added: []
  patterns:
    - record-don't-rederive: route/terminalize/scratch off the recorded cloud_job.backend_id, never select_active_agent
    - reject-don't-terminalize: a wrong reporter 403s BEFORE any mutation; never re-stamp backend_id from the token
    - union-narrowing cast for validator-guaranteed Optional ComputeBackend fields (mirrors backends._destination)
key-files:
  created: []
  modified:
    - src/phaze/routers/agent_push.py
    - tests/agents/routers/test_agent_push.py
decisions:
  - "An unattributed under-cap /mismatch (backend None -> no destination) HOLDS the file PUSHING rather than enqueuing a destination-less re-drive, because 'never a destination-less payload' is the governing must-have (Landmine 1). The over-cap SPILL path is destination-free and runs regardless of backend."
  - "The D-07 reporter==agent_ref gate is on /mismatch ONLY (its reporter is the compute agent). /pushed's reporter is the fileserver, so its MCOMP-06 no-cross-attribution is delivered purely by D-06 record-don't-rederive routing (no reporter gate)."
metrics:
  tasks: 2
  source-files-modified: 1
  test-files-modified: 1
  completed: 2026-07-05
---

# Phase 73 Plan 03: Per-Agent Push-Callback Attribution Summary

Re-keyed both push callbacks off the RECORDED `cloud_job.backend_id` so a file's terminalization, scratch dir, and compute-queue routing attribute to the agent it was dispatched to (MCOMP-06 no-cross-attribution) — plus a D-07 reporter-identity gate on `/mismatch` and the Landmine-1 destination re-stamp on its re-drive.

## What Was Built

**Task 1 — `/pushed` resolves scratch + compute queue from the recorded backend_id (D-06).** `report_pushed` now loads the file's `cloud_job` and resolves its `backend_id` via `resolve_compute_backend`, replacing the `select_active_agent(kind="compute")` gate. `process_file` routes to `backend.agent_ref`'s queue with `scratch_path` built from `backend.scratch_dir` — never `settings.active_compute_scratch_dir` (now unused here; its deletion is Plan 04). A file with no `cloud_job`, or an operator-removed / unresolvable `backend_id`, is a clean 200 hold (no mutation, no enqueue), mirroring the old no-compute-agent hold. The WR-02 rowcount==0 idempotent guard, the `CloudJob` SUCCEEDED terminalization gated behind it, the ledger clear, and the single commit are byte-identical. No reporter gate here — the `/pushed` reporter is the fileserver, so MCOMP-06 is delivered by routing to the RECORDED backend.

**Task 2 — `/mismatch` reporter validation (D-07) + destination re-stamp (Landmine 1).** `report_push_mismatch` resolves the file's backend early (before any mutation) and adds the D-07 gate: `if backend is not None and agent.id != backend.agent_ref → HTTP 403` with a `{file_id, reporter, expected}` structlog warning — reject-don't-terminalize (never re-stamp `backend_id` from the token). The under-cap re-drive stamps `dest_host` / `dest_scratch_dir` / `dest_ssh_user` from the recorded backend onto the rebuilt `PushFilePayload` (Landmine 1) — never a destination-less payload. An unattributed under-cap file (backend None) HOLDS the file PUSHING rather than enqueuing a destination-less push. The over-cap SPILL to `AWAITING_CLOUD` + `CloudJob` FAILED + budget-spent + ledger clear, and the `push_attempt` JSONB increment, stay byte-identical.

## Deviations from Plan

None — plan executed as written. Both tasks followed the specified RED → GREEN TDD flow. One planned-but-ambiguous point was resolved explicitly (documented as a decision, not a deviation): the plan's Task 2 behavior text ("no backend recorded → attempt-cap logic unchanged") and its action text ("backend None → re-drive cannot proceed → hold") conflicted for the unattributed under-cap case. The governing must-have — "`/mismatch` re-drives … never a destination-less payload" — decides it: an unattributed under-cap file HOLDS (no destination to stamp). The destination-free over-cap SPILL path still runs regardless of backend.

## Threat Model Coverage

| Threat ID | Disposition | Realized |
|-----------|-------------|----------|
| T-73-07 (spoofing: /mismatch reporter mis-attributing another agent's file) | mitigate | D-07 gate `agent.id != backend.agent_ref` → 403 + no terminalize; `backend_id` never re-stamped from the token (Task 2) |
| T-73-08 (wrong-agent process_file routing → retry storm) | mitigate | D-06: /pushed routes process_file to the RECORDED `backend.agent_ref`, not select_active_agent (Task 1) |
| T-73-09 (/mismatch re-drive to a null/empty destination) | mitigate | Landmine 1: re-driven payload stamps `dest_*` from the recorded backend; unattributed → hold, never destination-less (Task 2) |
| T-73-10 (backend_id / agent identity in logs) | mitigate | logs project only `{file_id, reporter, expected, backend_id, agent_id}` ids — no SecretStr / token |
| T-73-SC (dependency installs) | accept | zero new dependencies; pyproject untouched |

No new security surface beyond the plan's threat register. No threat flags.

## Verification

- `uv run pytest tests/agents/routers/test_agent_push.py` → **14 passed** (7 /pushed, 7 /mismatch).
- `uv run pytest tests/agents/routers/` (broader regression, serial) → **108 passed**.
- Agent-client + push-pipeline + process_file-scratch regression → **61 passed**.
- Whole-tree `uv run ruff check .` → clean; `uv run mypy .` → **Success: no issues found in 196 source files**.
- Acceptance greps: `resolve_compute_backend` present in `report_pushed`; `select_active_agent(session, kind="compute")` GONE from the router; `HTTP_403_FORBIDDEN` present in `report_push_mismatch`; `dest_host` / `dest_scratch_dir` stamped on the re-driven payload.

## Known Stubs

None — every read/route/stamp resolves off a real recorded backend value.

## Self-Check: PASSED

- `src/phaze/routers/agent_push.py` and `tests/agents/routers/test_agent_push.py` exist on disk (modified).
- All four task commits (2 RED + 2 GREEN) exist in git history: `0f3889d3` (RED T1), `32849540` (GREEN T1), `cf135eaa` (RED T2), `241c8c09` (GREEN T2).
- Key symbols present: `resolve_compute_backend`, `HTTP_403_FORBIDDEN`, `dest_scratch_dir`.
