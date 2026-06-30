---
phase: 53-s3-object-staging-leg
plan: 05
subsystem: cloud-burst
tags: [s3, object-staging, cleanup, agent-internal, fastapi, aioboto3, dist-01, kstage-04]

# Dependency graph
requires:
  - phase: 53-s3-object-staging-leg
    plan: 01
    provides: "CloudJob ORM model (per-file_id staging sidecar) — the existence guard for the inline delete"
  - phase: 53-s3-object-staging-leg
    plan: 02
    provides: "s3_staging.delete_staged_object(file_id) — idempotent control-plane S3 delete the callback invokes"
provides:
  - "inline staged-object delete hooked into BOTH analysis-result callbacks (success put_analysis + failure report_analysis_failed) — KSTAGE-04 / D-02"
  - "_delete_staged_object_if_cloud(session, file_id) helper: cloud_job existence guard → zero S3 calls on the all-local path (T-53-22)"
affects: [54-kueue-submit-reconcile]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "cloud_job existence guard short-circuits BEFORE any s3_staging call so the all-local path builds no client and needs no S3 config (T-53-22)"
    - "record-first discipline: result writes are staged in the txn, the delete runs before commit, and a delete error is log-and-swallowed so a cleanup blip never loses the result (T-53-21)"

key-files:
  created:
    - tests/test_routers/test_agent_analysis_inline_delete.py
  modified:
    - src/phaze/routers/agent_analysis.py

key-decisions:
  - "Inline delete placed AFTER clear_ledger_entry and BEFORE session.commit() on both paths (plan-mandated ordering) so the object is reaped exactly when the result is provably recorded"
  - "Imported s3_staging as a module (from phaze.services import s3_staging) so tests monkeypatch agent_analysis.s3_staging.delete_staged_object without touching a real S3 backend"
  - "Broad except Exception on the delete (no noqa needed — BLE not in the enabled ruff set) to log-and-swallow any cleanup blip; the lifecycle TTL is the backstop"

patterns-established:
  - "Control-plane result-callback cleanup hook guarded on a per-file_id sidecar row → no-op for files that were never staged"

requirements-completed: [KSTAGE-04]

# Metrics
duration: ~20min
completed: 2026-06-28
---

# Phase 53 Plan 05: Inline staged-object delete on the analysis-result callback Summary

**The D-02 cleanup leg: both the success (`put_analysis`) and failure (`report_analysis_failed`) analysis-result callbacks now delete the staged S3 object inline — the moment it is provably no longer needed — guarded on a `cloud_job` row so the all-local path makes zero S3 calls and a cleanup blip never loses the recorded result.**

## Performance

- **Duration:** ~20 min
- **Completed:** 2026-06-28
- **Tasks:** 1 (TDD)
- **Files created:** 1
- **Files modified:** 1

## Accomplishments
- `_delete_staged_object_if_cloud(session, file_id)` helper in `agent_analysis.py`: `select(CloudJob.id).where(CloudJob.file_id == file_id)`; if no row → return immediately (all-local no-op, **CRITICAL** so deploys with no S3 config never build a client or raise — T-53-22); otherwise `await s3_staging.delete_staged_object(file_id)` wrapped in a log-and-swallow guard.
- Hooked into `put_analysis` (success) immediately after `clear_ledger_entry(...)` and **before** `session.commit()` (D-02).
- Hooked identically into `report_analysis_failed` (failure) after its `clear_ledger_entry` and before commit — every terminal outcome the result callback represents now reaps the object (KSTAGE-04).
- `file_id` is the PATH value only on both paths (AUTH-01 / T-53-23) — a body field can never redirect the delete.
- 5 tests: success-with-cloud_job deletes once, failure-with-cloud_job deletes once, both all-local paths make ZERO S3 calls (no S3 config needed), and a delete error preserves the recorded result (state still ANALYZED, AnalysisResult row intact).

## Task Commits

TDD task committed RED → GREEN:

1. **Task 1 (RED)** — `2bbbd51` (test): 5 inline-delete contract tests
2. **Task 1 (GREEN)** — `a9710d8` (feat): helper + both call sites

_No REFACTOR commit — the implementation was minimal-and-clean at GREEN._

## Files Created/Modified
- `src/phaze/routers/agent_analysis.py` — added `select`/`CloudJob`/`s3_staging` imports, the `_delete_staged_object_if_cloud` helper, and the two call sites (success + failure, after ledger-clear, before commit).
- `tests/test_routers/test_agent_analysis_inline_delete.py` — 5 tests (smoke-app + seeded FileRecord/CloudJob, monkeypatched `s3_staging.delete_staged_object`).

## Decisions Made
- **Delete ordering:** placed after `clear_ledger_entry` and before `session.commit()` on both paths, exactly as the plan's `<interfaces>` directed — the result is staged in the transaction, the object is deleted, then everything commits together. A swallowed delete error still commits the recorded result.
- **Module-level `s3_staging` import:** `from phaze.services import s3_staging` (not a direct function import) so tests can `monkeypatch.setattr(agent_analysis.s3_staging, "delete_staged_object", ...)` and never touch a real S3 backend. Matches the existing `s3_staging`-confines-all-SDK-calls discipline (DIST-01) — `agent_analysis.py` is a control-plane router, so importing the control-plane S3 service does not cross the agent import boundary (verified: import-boundary suite still green).
- **Broad `except Exception`:** the delete is log-and-swallow so a transient S3 blip cannot lose the recorded analysis result (T-53-21); the bucket lifecycle TTL (Plan 02) reaps a missed object. `BLE` is not in the enabled ruff rule set, so no `noqa` is needed (and adding one would trip `RUF100`).

## Deviations from Plan

### Auto-fixed Issues

None affecting production code. One test-only fix during RED iteration:

**1. [Rule 1 - Bug] Used a valid `reason` literal in the all-local failure test**
- **Found during:** Task 1 (RED run)
- **Issue:** `AnalysisFailurePayload.reason` is a `Literal['timeout','crashed','error']`; the first draft of the all-local failure test sent `"crash"` → 422 instead of exercising the delete guard.
- **Fix:** Changed the payload to `{"reason": "crashed"}`.
- **Files modified:** tests/test_routers/test_agent_analysis_inline_delete.py (pre-RED-commit, never shipped broken)
- **Verification:** all 5 inline-delete tests pass.

---

**Total deviations:** 1 test-only fix during RED iteration; no production-code deviations beyond the planned surface; no scope creep.

## Threat Surface Coverage
- **T-53-20** (object leak after analysis): inline delete on BOTH success AND failure result paths (KSTAGE-04); lifecycle TTL backstops a missed callback.
- **T-53-21** (a delete error losing the result): result recorded first; the delete is log-and-swallow; TTL covers a missed delete — verified by `test_delete_error_does_not_corrupt_recorded_result` (200 + ANALYZED + AnalysisResult row intact after the delete raises).
- **T-53-22** (delete firing on all-local files): `cloud_job` existence guard short-circuits before any S3 call → zero client builds, no S3 config required — verified by the two all-local zero-S3-call tests.
- **T-53-23** (file_id from body redirecting the delete): `file_id` is the URL PATH value only on both handlers (AUTH-01).

## Issues Encountered
- Tests need ephemeral Postgres+Redis. Ran against the shared `phaze-test-db`/`phaze-test-redis` containers (ports 5433/6380) with `TEST_DATABASE_URL`/`MIGRATIONS_TEST_DATABASE_URL`/`PHAZE_REDIS_URL` exported — did NOT tear them down (a parallel wave agent shares them).

## Next Phase Readiness
- The inline-delete capability is now wired to every result-callback terminal outcome. Phase 54's reconcile may invoke the same `delete_staged_object` for the no-callback / eviction case (Kueue-evicted Jobs), with the bucket lifecycle TTL as the final backstop.
- No blockers.

## Self-Check: PASSED

- `tests/test_routers/test_agent_analysis_inline_delete.py` — present.
- Commits `2bbbd51` (test) and `a9710d8` (feat) — verified in git log.
- Acceptance: delete-ref grep = 4 (≥3), CloudJob grep = 2 (≥1).
- 5 new tests pass; 22 existing agent_analysis tests pass; import-boundary suite (10) green; project-wide ruff clean; mypy clean on the touched file.

---
*Phase: 53-s3-object-staging-leg*
*Completed: 2026-06-28*
