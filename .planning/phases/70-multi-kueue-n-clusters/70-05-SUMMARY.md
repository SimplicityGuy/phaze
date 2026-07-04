---
phase: 70-multi-kueue-n-clusters
plan: 05
subsystem: infra
tags: [reconcile, spillover, cleanup, advisory-lock, s3-staging, kueue, mkue-04, pitfall-9]

# Dependency graph
requires:
  - phase: 70-multi-kueue-n-clusters
    plan: 02
    provides: "cloud_job.staging_bucket recorded + delete_staged_object(file_id, bucket) + resolve_bucket_config + the interim bucket-aware at-cap delete"
  - phase: 70-multi-kueue-n-clusters
    plan: 03
    provides: "reconcile threads the file's backend KubeConfig (kube: KubeConfig) through delete_job"
provides:
  - "clean-before-flip at-cap spill-back: the old (backend_id, staging_bucket) staged object is deleted UNDER the held per-row pg_advisory_xact_lock(5_000_504), BEFORE the AWAITING_CLOUD flip commit (D-01/MKUE-04)"
  - "the delete reads the RECORDED staging_bucket captured pre-mutation (never re-derived); the row's staging_bucket is cleared to None after cleanup (T-70-04-04)"
  - "best-effort/idempotent delete via contextlib.suppress(Exception) so a slow/failed/absent S3 delete never blocks re-dispatch nor pins the lock; the per-bucket TTL stays the backstop (D-03)"
  - "the Job delete stays post-commit; cleanup stays in reconcile, out of the hot dispatch path (D-04)"
affects: [multi-kueue, spillover, reconcile, s3-staging, drain]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Clean-before-flip: delete the old resource UNDER the still-held advisory lock, BEFORE the commit that flips state + releases the lock -- so a concurrent claimant cannot race a re-stage of the same file_id-scoped key (Pitfall 9)"
    - "Capture-old-identity-into-locals before an in-place row mutation, so the trailing cleanup targets the RECORDED (authoritative) resource, never a post-mutation re-derive"
    - "Best-effort cleanup under a lock: contextlib.suppress(Exception) bounds the lock hold to one network timeout; the lifecycle TTL is the correctness backstop"

key-files:
  created: []
  modified:
    - src/phaze/tasks/reconcile_cloud_jobs.py
    - tests/analyze/tasks/test_reconcile_cloud_jobs.py

key-decisions:
  - "The advisory lock is the load-bearing boundary: proved directly with a real second-connection pg_try_advisory_xact_lock probe from inside the delete seam (returns False during the delete = lock held; True after commit) -- this is what distinguishes the new ordering from the old commit-then-delete and closes Pitfall 9."
  - "staging_bucket is cleared to None on the terminal row (beyond the plan's minimum) so a pre-repurpose reader can never resolve a stale bucket for an object that is already gone (T-70-04-04)."
  - "The under-cap re-drive branch is untouched -- it PRESERVES the staged object because the re-submitted Job still needs it; only the genuinely-terminal at-cap path deletes."

requirements-completed: [MKUE-04]

# Metrics
duration: ~25min
completed: 2026-07-04
---

# Phase 70 Plan 05: Clean-Before-Flip Spillover Cleanup Summary

**The at-cap spill-back in `reconcile_cloud_jobs._handle_no_callback_terminal` now deletes the old `(backend_id, staging_bucket)` staged object WHILE the per-row `pg_advisory_xact_lock(5_000_504)` is still held — BEFORE the commit that flips the file to `AWAITING_CLOUD` and releases the lock — closing Pitfall 9 (a concurrent drain re-dispatching + re-staging a new object under the same `file_id`-scoped key can no longer be destroyed by the trailing delete). The delete reads the RECORDED bucket captured pre-mutation, is best-effort/idempotent, clears `staging_bucket` to None, and the Job delete stays post-commit.**

## Performance

- **Duration:** ~25 min
- **Tasks:** 1 (TDD)
- **Files modified:** 2 (1 source, 1 test)

## Accomplishments

- **Task 1 (D-01/D-03, MKUE-04, the crux):** Rewrote ONLY the at-cap branch of `_handle_no_callback_terminal` to the clean-before-flip ordering. It now:
  1. Captures `old_bucket_id = cloud_job.staging_bucket` into a local BEFORE any mutation (the authoritative old identity — never a post-mutation re-derive, Pitfall 4/T-70-04-04).
  2. Resolves `bucket = s3_staging.resolve_bucket_config(cfg, old_bucket_id)` and deletes the old object UNDER the still-held per-row advisory lock, inside `contextlib.suppress(Exception)` (D-03 best-effort/idempotent — a slow/failed/absent delete never blocks the spill nor pins the lock beyond one timeout; the per-bucket TTL is the backstop). A bucketless row resolves to `None` and skips the delete cleanly.
  3. Sets `status=FAILED`, `inadmissible=False`, `cloud_phase=None`, and additionally `staging_bucket=None` (clears the recorded bucket so no pre-repurpose reader is misled), flips the `FileRecord` to `AWAITING_CLOUD`, then `session.commit()` — which releases the lock, by which point the old object is ALREADY gone.
  4. Keeps `kube_staging.delete_job(name, kube)` POST-commit (D-04 status-read-vs-GC; Job delete only).
  - Added `import contextlib`. The under-cap re-drive branch is untouched (it PRESERVES the staged object — the re-submitted Job still needs it). The lock stays PER-ROW (no whole-tick lock — Pitfall 2).

## Task Commits

1. **Task 1 (RED):** `b16146a` (test — 5 failing clean-before-flip/spillover/concurrency/best-effort tests, verified failing against the old commit-then-delete code)
2. **Task 1 (GREEN):** `4f7fa94` (feat — the clean-before-flip reorder + `import contextlib` + docstring update)

_TDD note: the 5 new tests landed RED first (`b16146a`), all failing against the old ordering, then GREEN in the source commit (`4f7fa94`)._

## Files Created/Modified

**Source:**
- `src/phaze/tasks/reconcile_cloud_jobs.py` — `import contextlib`; the at-cap branch of `_handle_no_callback_terminal` reordered to clean-before-flip (capture old identity → delete under the lock in `contextlib.suppress` → set FAILED + clear `staging_bucket` + flip + commit → post-commit Job delete); function docstring updated to describe the new load-bearing sequence.

**Tests:**
- `tests/analyze/tasks/test_reconcile_cloud_jobs.py` — added `text` import, a `_patch_commit_marker` helper (records a `"commit"` marker into the shared events list on every `AsyncSession.commit`), and 5 tests:
  - `test_clean_before_flip_ordering_delete_precedes_commit_precedes_job` — `events == ["s3_delete", "commit", "delete_job"]`.
  - `test_clean_before_flip_deletes_recorded_bucket_and_clears_it` — delete targets `_STAGING_BUCKET_ID`; row `staging_bucket` ends `None`.
  - `test_spillover_same_bucket_redispatch_preserves_new_object` — models an object store; delete runs before commit, so a post-commit re-stage of a new object on the same key survives (Pitfall 9).
  - `test_drain_reconcile_concurrency_delete_runs_under_advisory_lock` — a real second-connection `pg_try_advisory_xact_lock(5_000_504)` fails DURING the delete (lock held by reconcile) and succeeds AFTER commit.
  - `test_clean_before_flip_delete_is_best_effort` — a raising `delete_staged_object` is swallowed; the spill still commits and the Job is still deleted post-commit.

## Deviations from Plan

None — plan executed exactly as written. `staging_bucket = None` clearing (called out as "additionally" in the plan's `<action>`) was included as specified.

## Authentication Gates

None.

## Known Stubs

None — the change is a pure reordering + best-effort delete of real staged objects; no placeholder or empty-data path introduced.

## Threat Flags

None — no new network endpoints, auth paths, or trust-boundary schema beyond the plan's `<threat_model>`. The register mitigations are honored:
- **T-70-04** (cross-bucket object destruction): the delete runs UNDER the held `pg_advisory_xact_lock(5_000_504)`, BEFORE the AWAITING_CLOUD commit — proved by the second-connection try-lock probe; the drain cannot claim the file until reconcile commits.
- **T-70-04-02** (slow/failed S3 delete pinning the lock): `contextlib.suppress(Exception)` + the idempotent delete bound the hold to one timeout; TTL is the backstop; reconcile is the `*/5` cron, not the dispatch path.
- **T-70-04-03** (whole-tick lock): the lock stays PER-ROW — the delete runs inside the existing per-row unit, not a widened lock.
- **T-70-04-04** (recorded-bucket mis-resolution): the RECORDED `staging_bucket` is captured pre-mutation and cleared to `None` after cleanup — never re-derived.

## Verification

- `uv run pytest tests/analyze/tasks/test_reconcile_cloud_jobs.py -x` → **28 passed** (the 5 new + 23 existing; existing ordering/at-cap tests unaffected).
- Acceptance: in the at-cap branch `delete_staged_object` (L207) is textually BEFORE `session.commit()` (L213) and `delete_job` (L214) is AFTER it; `old_bucket_id = cloud_job.staging_bucket` captured pre-mutation; `cloud_job.staging_bucket = None` set; the delete is wrapped in `contextlib.suppress(Exception)`. `-k "clean_before_flip or spillover or concurrency"` → all pass.
- `uv run pytest tests/analyze` (isolation) → **444 passed**; the errored `tests/agents` subset re-ran in isolation → **40 passed** (the combined `tests/analyze tests/agents` run's 2 failed + 55 errors were the known local colima full-suite DB-connection flake — all in unrelated `tests/agents/routers|services` setup, none in the reconcile area; they pass in isolation).
- `uv run ruff check .` → All checks passed. `uv run mypy .` → Passed (the pre-commit mypy hook runs whole-project with `pass_filenames: false`). Both task commits passed the full pre-commit hook suite with no `--no-verify`.

## Self-Check: PASSED

`70-05-SUMMARY.md` exists on disk; both commits (`b16146a` test-RED, `4f7fa94` feat-GREEN) are present in git history.

---
*Phase: 70-multi-kueue-n-clusters*
*Completed: 2026-07-04*
