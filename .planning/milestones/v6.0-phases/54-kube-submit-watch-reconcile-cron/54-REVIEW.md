---
phase: 54-kube-submit-watch-reconcile-cron
reviewed: 2026-06-28T00:00:00Z
depth: standard
files_reviewed: 18
files_reviewed_list:
  - src/phaze/config.py
  - src/phaze/models/cloud_job.py
  - alembic/versions/026_add_cloud_job_kube_columns.py
  - src/phaze/services/kube_staging.py
  - src/phaze/services/enqueue_router.py
  - src/phaze/services/pipeline.py
  - src/phaze/routers/pipeline.py
  - src/phaze/tasks/submit_cloud_job.py
  - src/phaze/tasks/reconcile_cloud_jobs.py
  - src/phaze/tasks/controller.py
  - src/phaze/tasks/_shared/deterministic_key.py
  - src/phaze/templates/pipeline/partials/inadmissible_card.html
  - src/phaze/templates/pipeline/partials/stats_bar.html
  - src/phaze/templates/pipeline/dashboard.html
  - tests/kube_fakes.py
  - tests/test_tasks/test_reconcile_cloud_jobs.py
  - tests/test_tasks/test_submit_cloud_job.py
  - tests/test_services/test_kube_staging.py
findings:
  critical: 1
  warning: 3
  info: 1
  total: 5
status: issues_found
---

# Phase 54: Code Review Report

**Reviewed:** 2026-06-28
**Depth:** standard
**Files Reviewed:** 18
**Status:** issues_found

## Summary

Phase 54 adds the kr8s `kube_staging` seam, the fast `submit_cloud_job` producer, the `*/5`
`reconcile_cloud_jobs` safety-net cron, the `cloud_job` kube columns + migration 026, the kube
`ControlSettings` surface, and the Inadmissible operator-alert card. The kube-access patterns are
sound (verified against kr8s 0.20.15: `.metadata`/`.status` return `box.Box`, so the
attribute-access in reconcile and the `.get()`-access in `submit_job` are both valid; a missing
`status` key resolves to `None` via `getattr` because `BoxKeyError` subclasses `AttributeError`).
Idempotency (deterministic Job name + 409→refresh, `ON CONFLICT (file_id)` upsert, deterministic
SAQ dedup key), the delete-after-record ordering, the bounded re-drive with race guard, and the
no-ledger-seed invariant are all correctly implemented and well-tested. No SQL/command/path
injection surface exists (Job names/labels/keys derive only from server-generated UUIDs and
config; all DB access is ORM/bound-param). Kube credentials are correctly confined to the control
plane via `SECRET_FILE_FIELDS` and are never logged.

One correctness defect is load-bearing: the **Inadmissible operator alert never clears**. Three
robustness gaps in the reconcile/submit error handling round out the findings.

## Critical Issues

### CR-01: Inadmissible alert flag is never cleared — the operator banner stays lit forever

**File:** `src/phaze/tasks/reconcile_cloud_jobs.py:217-243`, `src/phaze/services/pipeline.py:820-835`

**Issue:** `reconcile_cloud_jobs` is the **only** writer of `cloud_job.inadmissible`, and it only
ever sets it to `True` (lines 219-221). No code path ever resets it to `False`. The dashboard
counter `get_inadmissible_count` counts **every** row with `inadmissible IS True` regardless of
the row's `status`:

```python
select(func.count(CloudJob.id)).where(CloudJob.inadmissible.is_(True))
```

Consequences once a Workload is flagged Inadmissible even once:

- **Recovery does not clear it.** When the operator fixes the LocalQueue/ClusterQueue and Kueue
  admits the Job, `_reconcile_one` takes the Admitted branch (line 237-243) which sets
  `status = RUNNING` but does **not** touch `inadmissible`. The Pending branch (line 232-234) and
  the success path `_record_success` (line 124-135) likewise never clear it.
- **The flag survives to terminal states.** A row that was transiently Inadmissible then
  succeeded ends at `status = SUCCEEDED` with `inadmissible = True`. `cloud_job` rows are never
  deleted (the upsert keeps them; terminal reconcile only flips `status`), so the count is
  monotonic and grows with every transient admission failure.

This directly defeats the feature's stated contract — `inadmissible_card.html:5-7` and the
dashboard wiring promise "the alert is loud only when something is wrong / healthy Pending stays
invisible," and `docs/cloud-burst.md:342-345` frames Inadmissible as a current-misconfig signal.
After the misconfig is fixed the banner ("K8s Jobs not admitting — check LocalQueue config") stays
lit with a stale, ever-increasing count, so the operator can never trust it. No test exercises the
Inadmissible→recovered transition (`test_admission_to_success_sequence` starts from Pending;
`test_inadmissible_never_consumes_cap` holds it Inadmissible throughout), so the gap is untested.

**Fix:** Clear the flag whenever reconcile observes the Workload is no longer Inadmissible, and
scope the count to non-terminal rows. In `_reconcile_one`, in the Pending and Admitted branches
(and on success), reset the flag:

```python
# Pending / Admitted / success branches — the misconfig is resolved:
if cloud_job.inadmissible:
    cloud_job.inadmissible = False
    # (commit already happens in these branches)
```

And defensively narrow the dashboard query so a stale terminal row can never inflate the alert:

```python
select(func.count(CloudJob.id)).where(
    CloudJob.inadmissible.is_(True),
    CloudJob.status.in_([CloudJobStatus.SUBMITTED.value, CloudJobStatus.RUNNING.value]),
)
```

Add a reconcile test: Inadmissible tick → Admitted tick asserts `inadmissible is False` and
`get_inadmissible_count == 0`.

## Warnings

### WR-01: A vanished Job (404) on an in-flight row is swallowed as a transient error → row stuck forever

**File:** `src/phaze/tasks/reconcile_cloud_jobs.py:185-200, 272-283`

**Issue:** `_reconcile_one` calls `job = await kube_staging.get_job(name)` unguarded. `get_job`
does `job.refresh()`, which raises `kr8s.NotFoundError` on a 404 (only `delete_job` swallows
NotFound). For a SUBMITTED/RUNNING row whose Job has disappeared, that NotFoundError bubbles to the
per-row `except Exception` guard (line 279) and is treated as a generic transient error — rolled
back, logged, skipped — every tick, with no terminal handling and no operator alert. The row never
leaves the in-flight set. In the normal re-drive flow the Job is recreated by `submit_cloud_job`,
so it self-heals; but two real windows leave it permanently stuck:

1. `_handle_no_callback_terminal` increments `attempts` + commits (line 177-179) **before**
   `_enqueue_resubmit` (line 180). If the enqueue raises after that commit, the row is committed
   SUBMITTED with the prior Job already deleted+confirmed-gone — every later tick 404s and skips.
2. A Failed Job GC'd by `ttlSecondsAfterFinished` (900s) before reconcile reads it (the
   "never reconciled" orphan case) leaves a no-callback terminal that is never recorded.

**Fix:** Distinguish "Job gone" from "transient kube error". Catch `kr8s.NotFoundError` (or a
`None` from `get_job`) inside `_reconcile_one` and route it to `_handle_no_callback_terminal`
(re-drive under cap / ANALYSIS_FAILED at cap) rather than letting it fall through to the generic
per-row guard.

### WR-02: `_kube_config()` does not validate the manifest fields → opaque error when image/cpu/memory unset

**File:** `src/phaze/services/kube_staging.py:72-84, 103-150`

**Issue:** `_kube_config()` validates only `kube_api_url` / `kube_namespace` / `kube_local_queue`,
but `build_job_manifest` interpolates `cfg.kube_job_image`, `cfg.kube_job_cpu_request`, and
`cfg.kube_job_memory_request`, all of which are `Optional` with `default=None` in Phase 54
(`config.py:548-562`). `submit_cloud_job` is a registered, operator-enqueueable controller task
(`controller.py:217`). If it is enqueued with those three unset, the manifest carries
`"image": None` and `requests: {"cpu": None, "memory": None}`; the kube API rejects it with a
non-409 `ServerError`, surfacing as the opaque `KubeStagingError("failed to submit job for <id>")`
(line 169) rather than a clear "kube_job_image is required" startup/runtime signal.

**Fix:** Extend `_kube_config()` (or add a submit-time guard) to require `kube_job_image`,
`kube_job_cpu_request`, and `kube_job_memory_request` before building the manifest, raising a
`KubeStagingError` that names the missing variable — consistent with the fail-loud discipline the
module already applies to the three connection fields.

### WR-03: The SA-token credential path is unverified and has zero test coverage

**File:** `src/phaze/services/kube_staging.py:87-100`

**Issue:** `_api()` sets `api.auth.token = token` post-construction, which the docstring itself
flags as an unverified Phase-56 item. Every `_StubCfg` in `test_kube_staging.py` sets
`kube_sa_token=None`, so the `if token:` branch is never exercised by any test. If the kr8s
auth-application form is wrong, SA-token auth silently does not apply and the control plane gets
401s against a real cluster — and nothing catches it before live deploy.

**Fix:** Add a respx seam test that sets `kube_sa_token` and asserts the request carries
`Authorization: Bearer <token>` (or assert the kr8s auth object reflects the token), so the only
credential-application line is covered before Phase 55/56 wiring goes live.

## Info

### IN-01: `tally["reconciled"]` is incremented before the row-existence check

**File:** `src/phaze/tasks/reconcile_cloud_jobs.py:272-277`

**Issue:** `tally["reconciled"] += 1` runs before `cloud_job = await session.get(...)`; a row that
was concurrently deleted/terminalized (`session.get` → `None` → `continue`) still counts toward
`reconciled`. Purely cosmetic (the tally is a log-only summary), but the count can slightly
overstate the rows actually reconciled.

**Fix:** Move the increment after the `None` check, or count only rows that reach `_reconcile_one`.

---

_Reviewed: 2026-06-28_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
