---
phase: 83-cloud-routing-sidecar-cutover
plan: 04
subsystem: cloud-routing
tags: [agent_s3, agent_push, cloud_job, CAS, advisory-lock, callbacks, sidecar]
requires:
  - "hold_awaiting_cloud() shared awaiting writer (83-01) — informs the D-03 re-stamp shape"
  - "cloud_job sidecar (Phase 77 D-04: 'awaiting'/'submitted' status members)"
provides:
  - "report_upload_failed CAS-guarded on cloud_job.status IN ('uploading','uploaded') + full no-op + advisory lock (D-09/D-10/D-11/D-03)"
  - "report_pushed CAS anchored on cloud_job.status=='submitted' (SC#1/D-12)"
  - "report_push_mismatch over-cap spill CAS anchored on cloud_job.status=='submitted' + re-stamp to awaiting (SC#1/D-12/D-03)"
affects:
  - "Phase 90 (all four cloud-routing callback guards now read/write the sidecar, not FileRecord.state — the state write can be dropped safely)"
tech-stack:
  added: []
  patterns:
    - "rowcount-guarded idempotent CAS on cloud_job.status via cast('CursorResult[Any]', ...)"
    - "pg_advisory_xact_lock(hashtext(ledger_key)) serializes an attempt RMW without a self-deadlocking row lock"
    - "FULL no-op on rowcount==0 (no dual-write, no S3 cleanup, no ledger clear)"
key-files:
  created: []
  modified:
    - "src/phaze/routers/agent_s3.py"
    - "src/phaze/routers/agent_push.py"
    - "tests/agents/routers/test_agent_s3.py"
    - "tests/agents/routers/test_agent_push.py"
decisions:
  - "D-09: report_upload_failed CAS anchors on cloud_job.status IN ('uploading','uploaded'), NOT FileRecord.state; the FileRecord dual-write is gated behind the rowcount"
  - "D-10: rowcount==0 is a FULL no-op (no cloud_job write, no FileRecord write, no multipart abort, no delete_staged_object, no ledger clear); commit + return cleared=False"
  - "D-11: pg_advisory_xact_lock(hashtext('s3_upload:'+file_id)) precedes the attempt RMW read (copied from agent_push.py:240) — no self-deadlock vs the before_enqueue hook's session"
  - "D-12: all four callback CAS guards anchor on their own kind's cloud_job.status (compute -> 'submitted', kueue -> 'uploading'/'uploaded'); no universal PUSHING/PUSHED predicate; enums/stage.py untouched"
  - "D-03: both spill paths re-stamp submitted/uploading -> awaiting (was FAILED), retaining attempts=cloud_submit_max_attempts, so AWAITING_CLOUD => status='awaiting' holds"
  - "SC#1: report_pushed + report_push_mismatch spill CAS anchors swapped off FileRecord.state==PUSHING onto cloud_job.status=='submitted' — removing the last cloud-routing FileRecord.state routing reads"
metrics:
  duration: "~35m"
  completed: "2026-07-09"
  tasks: 4
  files: 4
---

# Phase 83 Plan 04: Push/Upload Callback CAS Cutover Summary

Collapsed all four push/upload callback CAS guards onto the `cloud_job` sidecar as the single CAS
domain, removing every `FileRecord.state` routing read from the callbacks (SC#1) and closing the
unguarded `agent_s3.py:195` clobber bug (SC#2) plus the unserialized `s3_upload_attempt` RMW (T-83-02).

## What Was Built

- **`report_upload_failed` rewrite** (`routers/agent_s3.py`, D-09/D-10/D-11/D-03):
  - D-11: a `pg_advisory_xact_lock(hashtext('s3_upload:'+file_id))` now precedes the attempt RMW read
    (copied verbatim from `agent_push.py:240`), serializing two concurrent `/failed` without a
    self-deadlocking row lock against `stage_file_to_s3`'s `before_enqueue` hook session.
  - D-09: the over-cap spill is a CAS `update(CloudJob).where(status IN ('uploading','uploaded'))` —
    anchored on the sidecar, not `FileRecord.state`. An already-advanced file (`cloud_job` at
    `running`/`succeeded`) matches 0 rows and cannot be clobbered back to `AWAITING_CLOUD`.
  - D-03: the CAS re-stamps to `status='awaiting'` (was `FAILED`), `cloud_phase=None`,
    `attempts=cloud_submit_max_attempts`.
  - D-10: `rowcount==0` is a FULL no-op — no FileRecord write, no multipart abort, no
    `delete_staged_object` (a live Kueue job may be mid-download), no ledger clear; commit +
    `cleared=False`. The FileRecord dual-write, S3 cleanup, and ledger clear all moved inside the
    `rowcount!=0` branch.

- **`report_push_mismatch` over-cap spill anchor swap** (`routers/agent_push.py`, SC#1/D-12/D-03):
  the spill CAS now keys on `cloud_job.status == 'submitted'` (compute's single in-flight status)
  instead of `FileRecord.state == PUSHING`, and re-stamps `submitted -> awaiting` in the SAME CAS
  (the now-redundant separate `FAILED` UPDATE was deleted). The FileRecord dual-write (a plain write,
  not a `state==PUSHING` predicate) + ledger clear are gated behind the rowcount. The `:240` advisory
  lock and the D-07 reporter-auth 403 are unchanged and still run before the CAS; the under-cap
  re-drive path is byte-unchanged.

- **`report_pushed` anchor swap** (`routers/agent_push.py`, SC#1/D-12): the CAS now keys on
  `cloud_job.status == 'submitted' -> succeeded`, replacing BOTH the old `FileRecord PUSHING->PUSHED`
  guard AND the unconditional `cloud_job SUCCEEDED` write. The FileRecord `PUSHED` dual-write, ledger
  clear, and `process_file` enqueue are gated behind the rowcount. Anchoring on the `submitted`
  literal is safe despite kueue also transiting `SUBMITTED` because a kueue file returns via the
  `:107` no-attributed-backend 200 hold before the CAS — no backend-kind check and no
  `reporter==agent_ref` gate were added (D-12).

- **Five regression tests** (RED in Task 1 → GREEN across Tasks 2–4):
  - `test_upload_failed_cas_noop_on_advanced_cloud_job` (SC#2/T-83-01): over-cap `/upload-failed` on a
    `running`/`succeeded` cloud_job is a full no-op (row unchanged, file not clobbered, no abort/delete,
    ledger retained, `cleared=False`).
  - `test_failed_concurrent_under_cap_no_lost_update` (D-11/T-83-02): two concurrent under-cap
    `/upload-failed` against the real port-5433 engine increment `s3_upload_attempt` to exactly 2
    (RED proved the lost update → 1).
  - `test_pushed_does_not_clobber_when_cloud_job_not_submitted` (SC#1): late `/pushed` on a `succeeded`
    cloud_job (FileRecord still lagging PUSHING) is an idempotent no-op.
  - `test_push_mismatch_over_cap_spill_restamps_cloud_job_to_awaiting` (D-03): over-cap spill leaves the
    row at `awaiting`, not `failed`.
  - `test_push_mismatch_over_cap_does_not_clobber_when_cloud_job_not_submitted` (SC#1): late over-cap
    `/mismatch` on a `succeeded` cloud_job is a full no-op even when the reporter passes D-07.

## Verification

- `just test-bucket agents` IN ISOLATION (`-p no:randomly`): **450 passed, 0 failed**.
- `uv run mypy` + `uv run ruff check` clean on both routers and both test files.
- Grep audit (SC#1): neither `report_pushed` nor `report_push_mismatch` contains
  `FileRecord.state == FileState.PUSHING`; both compute CAS guards anchor on
  `CloudJobStatus.SUBMITTED.value` (agent_push.py:135, :277).
- Grep audit (SC#2/D-03/D-11): `pg_advisory_xact_lock` present in `report_upload_failed`
  (agent_s3.py:186); no `CloudJobStatus.FAILED.value` in either router; the agent_s3 CAS anchors on
  `UPLOADING`/`UPLOADED` and re-stamps to `AWAITING`.

## Deviations from Plan

None — plan executed exactly as written. No Rules 1–4 deviations were required.

Coupling note (not a deviation): three existing tests were updated in the task that changed the
behavior they assert (as the plan's Task 3/4 `<action>` instructs, and required for a green bucket):
`test_upload_failed_at_cap_spills...` (FAILED→AWAITING, Task 1 test commit),
`test_push_mismatch_over_cap_spills...` and `..._compute_spill_marks_cloud_budget_spent`
(seed a `submitted` cloud_job + assert the `awaiting` re-stamp, Task 3), and
`test_pushed_duplicate_callback_is_idempotent_noop` (seed `succeeded` instead of `submitted`, Task 4).
All are within this plan's declared `files_modified`.

## Threat Surface

No new network endpoints, auth paths, file-access patterns, or schema changes were introduced — the
plan modified the bodies of four existing token-authed callback handlers only. AUTH-01 (path-only
`file_id`, token identity, `extra='forbid'` bodies) and the D-07 reporter authorization are preserved
unchanged. No threat flags.

## Notes for Downstream Plans

- **Phase 90:** all four cloud-routing callback guards (`report_uploaded` already, plus the three this
  plan swapped) now read/write `cloud_job.status`; dropping `FileRecord.state` will not un-guard them.
  `report_uploaded`'s redundant `FileRecord.state == PUSHING` belt-and-braces guard (`agent_s3.py:128`)
  was intentionally NOT retired here (Deferred; belt-and-braces on a column Phase 90 removes).

## Self-Check: PASSED

- `src/phaze/routers/agent_s3.py` — FOUND
- `src/phaze/routers/agent_push.py` — FOUND
- `tests/agents/routers/test_agent_s3.py` — FOUND
- `tests/agents/routers/test_agent_push.py` — FOUND
- Commit `868fe462` (test, RED) — FOUND
- Commit `33cd3f37` (feat, agent_s3) — FOUND
- Commit `5233ded9` (feat, agent_push mismatch) — FOUND
- Commit `ecb8790b` (feat, agent_push pushed) — FOUND
