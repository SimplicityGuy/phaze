---
phase: 50-push-pipeline
plan: 05
subsystem: control-plane internal-API push callbacks
tags: [push-pipeline, internal-api, scheduling-ledger, cloud-burst, D-11, D-12]
requires:
  - "schemas.agent_push.PushedResponse / PushMismatchResponse (50-01)"
  - "ControlSettings.compute_scratch_dir / push_max_attempts (50-01)"
  - "schemas.agent_tasks.PushFilePayload (50-02)"
  - "_KEY_BUILDERS['push_file'] deterministic key + recovery classification (50-02)"
  - "enqueue_process_file(expected_sha256=, scratch_path=) (50-04)"
provides:
  - "POST /api/internal/agent/push/{file_id}/pushed — PUSHING->PUSHED + ledger clear + process_file enqueue"
  - "POST /api/internal/agent/push/{file_id}/mismatch — attempt-capped re-drive or ANALYSIS_FAILED"
affects:
  - "src/phaze/main.py (router registration)"
tech-stack:
  added: []
  patterns:
    - "Control-side internal-API callback for the Postgres-free agent boundary (mirror agent_analysis.py)"
    - "push_attempt counter rides the push_file ledger payload JSONB (migration-free, Pitfall 4)"
key-files:
  created:
    - "src/phaze/routers/agent_push.py"
    - "tests/test_routers/test_agent_push.py"
  modified:
    - "src/phaze/main.py"
decisions:
  - "D-01 intent realized control-side: push-success goes through a token-authed callback, not an agent-side enqueue (RESEARCH Critical Finding 1)"
  - "D-11: expected_sha256 read from FileRecord.sha256_hash control-side; the untrusted agent never supplies it"
  - "D-12 / Open-Q1: under the cap the file keeps its PUSHING slot on re-drive; the counter lives in the ledger payload"
  - "No-compute / no-fileserver -> a clean 200 hold (never a 500); the staging cron / recovery re-drives"
metrics:
  duration: ~35m
  completed: 2026-06-26
  tasks: 2
  files_changed: 3
---

# Phase 50 Plan 05: Push-pipeline control callbacks Summary

Added the two control-side internal-API callbacks the file-server/compute agents invoke to report `push_file` outcomes within the Postgres-free agent boundary: a `pushed` handler that flips `PUSHING -> PUSHED`, clears the `push_file:<id>` ledger row, and enqueues exactly one `process_file` job on the compute queue with the ORM-pinned `expected_sha256` (D-11) and a `compute_scratch_dir`-rooted `scratch_path`; and a `mismatch` handler that increments a `push_attempt` counter in the ledger payload JSONB and either re-drives `push_file` on the fileserver queue (keeping the PUSHING slot) or, past `push_max_attempts`, sets `ANALYSIS_FAILED` and clears the ledger — each in a single committed transaction.

## What was built

### Task 1 — `pushed` callback (TDD: 421fb19 RED → bf8eadc GREEN)
`POST /api/internal/agent/push/{file_id}/pushed` (`report_pushed`). In one transaction: load the FileRecord (for `sha256_hash` + `file_type`), gate on an online compute agent (`select_active_agent(kind="compute")`), `update(FileRecord).values(state=PUSHED)`, `clear_ledger_entry("push_file:<id>")`, resolve the compute queue via `request.app.state.task_router.queue_for(...)`, and `enqueue_process_file(..., expected_sha256=file.sha256_hash, scratch_path=f"{settings.compute_scratch_dir}/{file_id}.{file.file_type}")`, then `commit()`. No compute agent online → a clean 200 hold (no state change, no enqueue, no 500). Registered the router in `main.py` beside the other internal-API routers.

### Task 2 — `mismatch` callback (TDD: 12eb286 RED → b7ae5f9 GREEN)
`POST /api/internal/agent/push/{file_id}/mismatch` (`report_push_mismatch`). Reads `push_attempt` from the `push_file:<id>` ledger payload (default 0) and increments. Over `push_max_attempts` → `state=ANALYSIS_FAILED` + `clear_ledger_entry` in one transaction (mirrors `report_analysis_failed`). Under the cap → re-enqueue `push_file` on the fileserver queue with the deterministic `push_file:<id>` key (keeping the file `PUSHING`, Open-Q1) and stamp the incremented `push_attempt` back onto the ledger row. No fileserver online → clean 200 hold.

## Verification
- `uv run pytest tests/test_routers -q -k push` — 8 passed (7 new + 1 pre-existing)
- `uv run pytest tests/test_task_split.py` — 8 passed (Postgres-free boundary intact)
- `uv run ruff check .` — All checks passed
- `uv run mypy .` — Success, 168 source files
- Acceptance greps: `clear_ledger_entry`, `enqueue_process_file`, `compute_scratch_dir`, `push_max_attempts`, `ANALYSIS_FAILED` all present in `agent_push.py`; `agent_push` registered in `main.py`.

## Deviations from Plan
None — plan executed exactly as written. The two "clean hold" paths (no compute / no fileserver agent) were already implied by the plan's behavior notes ("clean, non-500 response/skip"); they were implemented as explicit `NoActiveAgentError` guards and covered by tests.

Note on `push_attempt` persistence: the counter is stamped onto the ledger row via an explicit `update(SchedulingLedger).values(payload=...)` AFTER the `push_file` re-enqueue. In production the control-side `before_enqueue` ledger-write hook upserts the row (with the fresh `PushFilePayload` kwargs, no `push_attempt`) in its own short-lived session, so the explicit post-enqueue UPDATE is the source of truth for the counter. The router test uses the `FakeQueue` (no hook), so the same UPDATE is what the test asserts against — behavior is identical either way.

## Threat surface
No new surface beyond the plan's `<threat_model>`. Both endpoints are token-authed via `get_authenticated_agent`; `file_id` is read from the PATH only and identity from the token dependency, never the body (AUTH-01); `expected_sha256` is pinned control-side from the ORM (D-11); the re-push loop is bounded by `push_max_attempts` (T-50-loop). No new packages.

## TDD Gate Compliance
Both tasks followed RED → GREEN: `test(50-05)` commit precedes each `feat(50-05)` commit (421fb19→bf8eadc, 12eb286→b7ae5f9). No REFACTOR commits were needed.

## Self-Check: PASSED
- FOUND: src/phaze/routers/agent_push.py
- FOUND: tests/test_routers/test_agent_push.py
- FOUND: src/phaze/main.py (agent_push registered)
- FOUND commit 421fb19 (test pushed RED)
- FOUND commit bf8eadc (feat pushed GREEN)
- FOUND commit 12eb286 (test mismatch RED)
- FOUND commit b7ae5f9 (feat mismatch GREEN)
