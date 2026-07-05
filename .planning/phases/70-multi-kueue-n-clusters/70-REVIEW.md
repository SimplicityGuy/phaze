---
phase: 70-multi-kueue-n-clusters
reviewed: 2026-07-04T00:00:00Z
depth: standard
files_reviewed: 20
files_reviewed_list:
  - alembic/versions/030_add_cloud_job_staging_bucket.py
  - src/phaze/config.py
  - src/phaze/config_backends.py
  - src/phaze/models/cloud_job.py
  - src/phaze/routers/agent_analysis.py
  - src/phaze/routers/agent_files.py
  - src/phaze/routers/agent_s3.py
  - src/phaze/services/backends.py
  - src/phaze/services/cloud_staging.py
  - src/phaze/services/kube_staging.py
  - src/phaze/services/s3_staging.py
  - src/phaze/tasks/controller.py
  - src/phaze/tasks/reconcile_cloud_jobs.py
  - src/phaze/tasks/release_awaiting_cloud.py
  - src/phaze/tasks/submit_cloud_job.py
  - pyproject.toml
  - tests/analyze/services/test_backends.py
  - tests/analyze/services/test_kube_staging.py
  - tests/analyze/services/test_s3_staging.py
  - tests/analyze/tasks/test_reconcile_cloud_jobs.py
  - tests/analyze/tasks/test_release_awaiting_cloud.py
  - tests/analyze/tasks/test_submit_cloud_job.py
findings:
  critical: 2
  warning: 3
  info: 1
  total: 6
status: issues_found
---

# Phase 70: Code Review Report

**Reviewed:** 2026-07-04
**Depth:** standard
**Files Reviewed:** 22 (20 required-reading files; pyproject.toml and 6 test files included as supporting evidence)
**Status:** issues_found

## Summary

Phase 70 re-homes single-cluster Kueue staging/submission/reconcile onto an N-cluster registry:
per-file deterministic bucket picking (`s3_staging.pick_bucket` / `resolve_bucket_config`),
per-backend `KubeConfig`-threaded `kube_staging` calls, and per-backend failure isolation in the
drain (`stage_cloud_window`) and reconcile (`KueueBackend.reconcile`) loops. The deterministic
bucket-recording design (D-06: record at stage time, never re-derive at presign/cleanup time) is
implemented correctly and consistently across `s3_staging.py`, `cloud_staging.py`,
`reconcile_cloud_jobs.py`, and the `agent_s3.py` / `agent_files.py` / `agent_analysis.py`
callbacks — every presign/delete call site resolves the *recorded* `cloud_job.staging_bucket`,
never `pick_bucket` again. The `KueueBackend._kube()` / `kube_staging._api()` per-cluster
kr8s-client construction (constructor-time auth from a synthesized in-memory kubeconfig dict) is
also sound and well covered by `test_kube_staging.py`.

However, two BLOCKER-level defects were found in the drain's dispatch/failure-isolation path
(`services/backends.py` `KueueBackend.dispatch` + `tasks/release_awaiting_cloud.py`
`stage_cloud_window`) that can strand a `FileRecord` in `PUSHING` with no corresponding
`cloud_job` row — precisely the "limbo row" scenario the module docstrings claim is structurally
impossible (the "Pitfall 4 limbo guard" / D-03 invariant). Three WARNING-level design/consistency
gaps were also found in the reconcile advisory-lock granularity, S3 error-wrapping consistency,
and bucket-id uniqueness validation.

## Critical Issues

### CR-01: `KueueBackend.dispatch` mutates `FileState` before gating on fileserver-agent availability, unlike its sibling backends — can strand a file in PUSHING with no `cloud_job` row

**File:** `src/phaze/services/backends.py:352-361` (`KueueBackend.dispatch`), `src/phaze/services/cloud_staging.py:96-104` (`_stage_file_to_s3`'s fileserver gate)

**Issue:** Both `LocalBackend.dispatch` (backends.py:213-227) and `ComputeAgentBackend.dispatch`
(backends.py:268-271) correctly resolve/gate on the fileserver agent **before** touching
`file.state`, so a `NoActiveAgentError` leaves the file completely untouched (the module docstring
calls this "the Pitfall 4 limbo guard": no committed in-flight `FileState` can ever exist without a
live `cloud_job` row). `KueueBackend.dispatch` breaks this ordering:

```python
async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: AgentTaskRouter) -> bool:
    cfg = cast("ControlSettings", get_settings())
    # D-03: the drain flips PUSHING before the per-kind fork; own that flip here so dispatch is atomic.
    file.state = FileState.PUSHING          # <-- mutated FIRST, unconditionally
    bucket_ids = list(getattr(self.config, "buckets", []) or [])
    bucket_id = s3_staging.pick_bucket(file.id, bucket_ids)
    bucket = s3_staging.resolve_bucket_config(cfg, bucket_id)
    if bucket is None:
        raise s3_staging.S3StagingError(...)
    await _stage_file_to_s3(session, file, task_router, bucket)   # <-- fileserver gate happens INSIDE here
    ...
```

`_stage_file_to_s3` (`cloud_staging.py:100`) is the function that actually gates on the fileserver
agent (`agent = await select_active_agent(session, kind="fileserver")`), and that call is a plain
`select(Agent)` executed via `session.execute(...)`. SQLAlchemy's `AsyncSession` defaults to
`autoflush=True`, so the pending `file.state = PUSHING` change set one line earlier in
`KueueBackend.dispatch` is **flushed to the open (uncommitted) transaction as a side effect of that
SELECT**, before `select_active_agent` ever raises `NoActiveAgentError`.

The caller, `stage_cloud_window` (`release_awaiting_cloud.py:207-213`), catches
`NoActiveAgentError` and does `break` with **no rollback**, then falls through to the tick's single
`await session.commit()` (`release_awaiting_cloud.py:234`). The result: the flushed
`FileState.PUSHING` update is **committed**, even though `_stage_file_to_s3` raised before writing
any `cloud_job` row and before enqueuing anything. The file is now permanently stuck: it is no
longer `AWAITING_CLOUD` (so `get_cloud_staging_candidates` never selects it again), and it has no
`cloud_job` row (so `reconcile_cloud_jobs` never looks at it either). This directly contradicts the
`release_awaiting_cloud.py` docstring's own claim (lines 202-206): *"dispatch resolves the
fileserver BEFORE any mutation, so the raising path touches nothing."* That claim is true for
`LocalBackend`/`ComputeAgentBackend` but false for `KueueBackend`.

The same ordering bug also means a **generic** exception from anywhere inside `_stage_file_to_s3`
after the state flip (e.g. an unwrapped `ClientError` from `s3_staging.create_multipart_upload`,
see WR-02 below) produces the identical committed-limbo outcome via the `except Exception:` branch
(`release_awaiting_cloud.py:214-225`), which also does not roll back.

This is not covered by the existing `test_backends.py` / `test_release_awaiting_cloud.py` isolation
suite: the isolation tests use a `_StubBackend` whose `dispatch` is documented to "Fail (if
configured) BEFORE any mutation" — i.e. the test doubles model the *correct* ordering, not the
*actual* `KueueBackend` ordering, so the bug is not exercised.

**Fix:** Reorder `KueueBackend.dispatch` to resolve/gate the fileserver agent before mutating
`file.state`, mirroring `ComputeAgentBackend`/`LocalBackend`. The cleanest fix is to hoist the
fileserver-agent gate out of `_stage_file_to_s3` (or add a cheap pre-check) so `KueueBackend.dispatch`
can gate first:

```python
async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: AgentTaskRouter) -> bool:
    cfg = cast("ControlSettings", get_settings())
    # Gate BEFORE mutating anything (mirrors Local/Compute): an absent fileserver must leave
    # the file untouched.
    await select_active_agent(session, kind="fileserver")  # NoActiveAgentError propagates untouched
    file.state = FileState.PUSHING
    bucket_ids = list(getattr(self.config, "buckets", []) or [])
    bucket_id = s3_staging.pick_bucket(file.id, bucket_ids)
    bucket = s3_staging.resolve_bucket_config(cfg, bucket_id)
    if bucket is None:
        raise s3_staging.S3StagingError(...)
    await _stage_file_to_s3(session, file, task_router, bucket)
    await session.execute(update(CloudJob).where(CloudJob.file_id == file.id).values(backend_id=self.id, staging_bucket=bucket_id))
    return True
```
Add a regression test mirroring `test_stage_cloud_window_isolation_dispatch_noactiveagent_holds_all_and_breaks`
but against the **real** `KueueBackend` (not a stub) with no fileserver agent seeded, asserting the
file stays `AWAITING_CLOUD` and no `cloud_job` row is written.

---

### CR-02: `stage_cloud_window`'s per-candidate exception handlers never roll back the session, so a partial write from a failed `dispatch()` is silently committed

**File:** `src/phaze/tasks/release_awaiting_cloud.py:207-225`

**Issue:** The per-candidate dispatch loop has two exception handlers:

```python
try:
    dispatched = await target.dispatch(file, session, task_router)
except NoActiveAgentError:
    remaining = len(candidates) - index
    logger.info(...)
    tally["skipped"] += remaining
    break
except Exception:
    logger.warning(...)
    tally["skipped"] += 1
    continue
```

Neither branch calls `await session.rollback()`. The design intent, per the `services/backends.py`
module docstring ("D-03 ... NEVER after a separate commit (Pitfall 4 limbo guard)") and the
`ComputeAgentBackend.dispatch` docstring ("a rollback leaves no limbo row"), is that an exception
mid-`dispatch()` is safe *because a rollback will undo the partial write*. But no rollback ever
happens here — the tick instead proceeds to the single post-loop `await session.commit()`
(`release_awaiting_cloud.py:234`), which commits **everything accumulated in the transaction so
far**, including any partial write a raising `dispatch()` already made before it raised (e.g.
`ComputeAgentBackend.dispatch` upserts the `cloud_job` row and flips `FileState` *before* calling
`_enqueue_push_file`; if the enqueue call itself raises — a Postgres/Redis broker hiccup — the
`cloud_job`/`FileState` write persists with no push ever actually enqueued: a "phantom dispatch"
that will never progress, since nothing else watches for it).

Beyond the phantom-dispatch data-integrity problem, this is also a robustness bug: if the
exception that propagates out of `dispatch()` is itself a genuine Postgres-level statement failure
(e.g. a deadlock, serialization failure, or constraint violation surfaced from one of the
`session.execute()` calls inside `dispatch`), Postgres marks the transaction as
aborted-until-rollback. Every subsequent statement on that same session — including the next
candidate's `dispatch()` calls in this same loop and the tick's final `await session.commit()` —
will then raise (asyncpg's `current transaction is aborted, commands ignored until end of
transaction block` / SQLAlchemy `PendingRollbackError`). The `except Exception:` branch would catch
and swallow that for subsequent candidates (silently converting every remaining candidate into a
misleading "skipped" instead of the true cause), but the final `await session.commit()` sits
**outside** any try/except and would raise straight out of `stage_cloud_window` — violating the
module's own documented "NEVER raises" cron discipline (module docstring, and explicitly restated
in the function docstring: "Every early-return path ... is a clean no-op ... NEVER raises").

**Fix:** Roll back on both branches before continuing/breaking, and stop after a rollback since the
whole tick's transaction (including the advisory lock and any earlier genuinely-good dispatches) is
now gone:

```python
except NoActiveAgentError:
    remaining = len(candidates) - index
    logger.info("stage_cloud_window hold: fileserver agent vanished mid-tick", held=remaining)
    tally["skipped"] += remaining
    await session.rollback()
    return {"staged": 0, "skipped": len(candidates)}  # discard partial work from this poisoned txn
except Exception:
    logger.warning("stage_cloud_window: backend dispatch failed -> holding this candidate", backend_id=target.id)
    await session.rollback()
    tally["skipped"] += 1
    continue
```
(If preserving already-staged work within the same tick is important, this instead argues for
committing per-candidate rather than once per tick — but that is a bigger design change than a
"fix the missing rollback" patch and should be considered against the advisory-lock/cap-overshoot
invariant this module is built around.) At minimum, add a test that makes a `dispatch()` call raise
**after** performing a real DB write (not before, as every existing stub does) and assert the
write is not observable after the tick completes.

## Warnings

### WR-01: `KueueBackend.reconcile`'s advisory-lock/commit-per-row design is violated by several no-op branches in `_reconcile_one`

**File:** `src/phaze/services/backends.py:367-424` (`KueueBackend.reconcile`), `src/phaze/tasks/reconcile_cloud_jobs.py:268-330` (`_reconcile_one`)

**Issue:** `KueueBackend.reconcile`'s docstring states the per-row `pg_advisory_xact_lock` design
requires "`_reconcile_one` commits per row, which auto-releases the xact lock -- that per-row
granularity is REQUIRED (Pitfall 2: a whole-tick lock would break the load-bearing
delete-after-record ordering)". In practice, several `_reconcile_one` branches return **without**
calling `session.commit()`:
- the "Admission state unreadable this tick" branch (`reconcile_cloud_jobs.py:271-274`),
- the already-`inadmissible` branch when nothing changed (`reconcile_cloud_jobs.py:284-295`, the
  `if not cloud_job.inadmissible:` guard skips the commit when it's already `True`),
- the healthy-Pending branch when `dirty` stays `False` (`reconcile_cloud_jobs.py:298-309`),
- the Admitted/Running branch when the row is already in the target state
  (`reconcile_cloud_jobs.py:311-327`),
- the trailing "Unknown in-flight condition set" fallthrough (`reconcile_cloud_jobs.py:329`).

Because the same `AsyncSession`/transaction spans the entire `for cloud_job_id in cloud_job_ids:`
loop (and, in `reconcile_cloud_jobs()`, across every backend's `reconcile()` call), a
no-commit outcome leaves the transaction — and therefore the `pg_advisory_xact_lock` acquired at
the top of that iteration — open into the *next* row's iteration (whose own
`pg_advisory_xact_lock` acquisition becomes a same-transaction no-op re-acquire, not a fresh
acquire/release cycle). This does not break correctness (the lock is still held, so mutual
exclusion with `stage_cloud_window` is preserved), but it silently defeats the documented
per-row granularity and can turn into exactly the "whole-tick lock" the docstring calls out as the
thing to avoid (Pitfall 2) whenever a backend has many in-flight rows that are all in a steady,
unchanged state (e.g. a burst of healthy `Pending` rows) — needlessly starving `stage_cloud_window`
of the lock for the whole reconcile tick instead of releasing it between rows as designed.

**Fix:** Either commit unconditionally at the end of each `_reconcile_one` call (a cheap no-op
commit when nothing changed) so the per-row lock-release guarantee actually holds, or explicitly
document/accept that the lock may span multiple no-op rows and adjust the Pitfall-2 comment
accordingly so the code and the docstring agree.

### WR-02: Inconsistent S3 error wrapping in `s3_staging.py` — some verbs never raise `S3StagingError`

**File:** `src/phaze/services/s3_staging.py:127-153` (`create_multipart_upload`, `presign_upload_parts`), `:202-217` (`presign_get`) vs. `:156-179`, `:182-199`, `:220-236` (`complete_multipart_upload`, `abort_multipart_upload`, `delete_staged_object`)

**Issue:** The module docstring positions `s3_staging.py` as "the single home of every S3 SDK call
in the system" with a "fail-loud custom error" (`S3StagingError`) discipline. `complete_multipart_upload`,
`abort_multipart_upload`, and `delete_staged_object` all catch `ClientError` and either swallow
known-absent codes or re-raise as `S3StagingError`. `create_multipart_upload`, `presign_upload_parts`,
and `presign_get` have no such handling at all — a `ClientError` (bad credentials, missing bucket,
network failure) propagates as a raw `botocore.exceptions.ClientError`. Every caller of these three
functions (`_stage_file_to_s3` in `cloud_staging.py`, `presign_download` in `agent_files.py`) is
therefore exposed to a different exception type than the rest of the module promises, which will
silently defeat any current or future `except S3StagingError:` handling and makes the module's
error surface inconsistent for a caller trying to distinguish "S3 misconfiguration" from other
failure classes.

**Fix:** Wrap the three unguarded verbs' `ClientError` in `S3StagingError` for consistency with
their siblings, e.g.:
```python
async def create_multipart_upload(file_id: uuid.UUID, bucket: BucketConfig) -> str:
    key = staged_object_key(file_id)
    try:
        async with _client(bucket) as client:
            resp = await client.create_multipart_upload(Bucket=bucket.bucket, Key=key)
    except ClientError as exc:
        raise S3StagingError(f"failed to create multipart upload for {file_id}") from exc
    return resp["UploadId"]
```

### WR-03: No uniqueness validation on `[[buckets]]` ids — a duplicate id in `backends.toml` silently mis-resolves instead of failing fast

**File:** `src/phaze/config.py:413-447` (`ControlSettings._validate_registry`), `src/phaze/services/s3_staging.py:91-105` (`resolve_bucket_config`)

**Issue:** `_validate_registry` enforces several bucket-registry invariants (unknown bucket-id
references, empty resolved bucket sets, and the `cluster-specific` single-referencing-backend
cardinality rule) but never checks that `ControlSettings.buckets` itself contains no duplicate
`id` values. `resolve_bucket_config` builds its lookup via
`{bucket.id: bucket for bucket in cfg.buckets}.get(bucket_id)`, which silently collapses duplicate
ids to whichever `BucketConfig` appears last in the TOML-parsed list — with distinct
`endpoint_url`/credentials per entry, this means a copy-paste typo in an operator's `backends.toml`
(two `[[buckets]]` blocks sharing an `id`) does not fail fast at boot like every other registry
invariant in this file; it instead non-deterministically (list-order-dependent) redirects every
presign/cleanup call for that bucket id to the wrong endpoint/credentials.

**Fix:** Add a duplicate-id check to `_validate_registry` alongside the existing bucket checks:
```python
bucket_id_counts = Counter(b.id for b in self.buckets)
dupes = [bid for bid, count in bucket_id_counts.items() if count > 1]
if dupes:
    raise ValueError(f"duplicate bucket ids in registry: {dupes} (each [[buckets]] id must be unique)")
```

## Info

### IN-01: Redundant `staging_bucket` write in `KueueBackend.dispatch`

**File:** `src/phaze/services/backends.py:364`, `src/phaze/services/cloud_staging.py:108-132`

**Issue:** `_stage_file_to_s3`'s `pg_insert(CloudJob)...on_conflict_do_update` already sets
`staging_bucket=bucket.id` in both the INSERT values and the `ON CONFLICT` `set_` clause
(`cloud_staging.py:120,130`). Immediately after `_stage_file_to_s3` returns,
`KueueBackend.dispatch` issues a second, separate `UPDATE cloud_job ... SET staging_bucket = bucket_id`
(`backends.py:364`) with the identical value. This is harmless today (both writes agree) but is a
"two sources of truth for the same column" code smell: a future edit to either write site that
changes the bucket-selection logic in only one place would silently desynchronize them.

**Fix:** Drop `staging_bucket` from the follow-up `UPDATE` in `backends.py:364` (keep only
`backend_id`, which genuinely needs the second write since `_stage_file_to_s3` never sets it), or
have `_stage_file_to_s3` accept and stamp `backend_id` directly so there is a single write site for
both columns.

---

_Reviewed: 2026-07-04_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
