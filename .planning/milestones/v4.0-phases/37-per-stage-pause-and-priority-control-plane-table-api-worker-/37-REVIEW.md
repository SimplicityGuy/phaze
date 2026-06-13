---
phase: 37-per-stage-pause-and-priority-control-plane-table-api-worker
reviewed: 2026-06-13
depth: standard
files_reviewed: 9
findings:
  critical: 0
  warning: 2
  info: 1
  total: 3
status: resolved
---

# Phase 37 Code Review

**Depth:** standard · **Files reviewed:** 9 · **Resolved:** 3/3 (commit `5da762e`)

## Files reviewed

- src/phaze/models/pipeline_stage_control.py
- alembic/versions/020_add_pipeline_stage_control.py
- src/phaze/tasks/_shared/stage_control.py
- src/phaze/services/stage_control.py
- src/phaze/routers/pipeline_stages.py
- src/phaze/schemas/pipeline_stages.py
- src/phaze/tasks/_shared/queue_factory.py
- src/phaze/main.py
- src/phaze/models/__init__.py

## Summary

Security surface is clean: `stage` is validated against the `STAGE_TO_FUNCTION` allowlist before
any `key LIKE` prefix is built, and every value reaches `saq_jobs` as a bound param (no injection).
Sentinel-guarded resume (`AND scheduled = :SENTINEL`) preserves retry backoffs (REQ-37-3).
`status='queued'` guards satisfy no-double-pickup (REQ-37-4). Agent import boundary respected
(`stage_control.py` carries no SQLAlchemy/`phaze.database` imports; `saq.Job` is `TYPE_CHECKING`-guarded).
Session factory uses `expire_on_commit=False`, so post-commit `_response(row)` accesses are safe.
CHECK constraint name matches between ORM (`priority_range`) and migration
(`ck_pipeline_stage_control_priority_range`).

## Findings

### WR-01 (Warning) — RESOLVED
`_read_stage_control` did not null-check `cursor.fetchone()`; a missing control row
(`pipeline_stage_control`) raised `TypeError` into `apply_stage_control`'s broad `except`,
logged as a misleading "read failed" and silently letting a job past a paused stage.
`src/phaze/tasks/_shared/stage_control.py:100`.
**Fix:** explicit `row is None` guard → log `"stage-control row missing"` and return
`(False, 50)` (unpaused/default), uncached so a freshly-seeded row is seen on the next enqueue.

### WR-02 (Warning) — RESOLVED
Priority-delta endpoint did a read-modify-write on `row.priority` without locking; two concurrent
`POST /pipeline/stages/{stage}/priority` requests both read the old value and one delta was
silently lost. `src/phaze/routers/pipeline_stages.py:88`.
**Fix:** `_load_control_row(..., lock=True)` fetches the row `FOR UPDATE` (via
`session.get(..., with_for_update=True)`), serializing concurrent deltas. No deadlock: the control
row is always acquired before the `saq_jobs` UPDATE, and `set_stage_priority` never locks `saq_jobs`.

### IN-01 (Info) — RESOLVED
`_FUNCTION_TO_STAGE` (private name) was exported from `__all__`; only consumed within its own module.
**Fix:** removed from `__all__`.

## Verification

Full suite green after fixes (`just integration-test` → 1739 passed); ruff + mypy clean.
