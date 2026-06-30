---
phase: 54-kube-submit-watch-reconcile-cron
verified: 2026-06-28T00:00:00Z
status: passed
score: 5/5 must-haves verified
overrides_applied: 0
---

# Phase 54: Kube Submit / Watch + Reconcile Cron Verification Report

**Phase Goal:** The control plane submits a suspended per-file Kueue Job, a fast submit task never blocks a worker, and a periodic reconcile cron owns Workload lifecycle, cleanup, and re-drive — with the out-of-band `/api/internal/agent/*` callback as the only authoritative result channel.
**Verified:** 2026-06-28
**Status:** passed
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths (ROADMAP Success Criteria)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Suspended batch/v1 Job submitted idempotently via deterministic `file_id`-keyed name; `kueue.x-k8s.io/queue-name` label, `restartPolicy: Never`, `parallelism: 1`, cpu/memory requests only | ✓ VERIFIED | `kube_staging.py:114-150` — `suspend: True`, `parallelism: 1`, `completions: 1`, `backoffLimit: 0`, `ttlSecondsAfterFinished: 900`, label on Job metadata, requests-only; `job_name()` at line 61 returns `phaze-analyze-{file_id}`; 409→refresh at line 166 |
| 2 | Submit task returns within seconds; periodic reconcile cron owns Workload status, cleanup, re-drive | ✓ VERIFIED | `submit_cloud_job.py:55-101` — ONE kube POST then returns; `controller.py:251` — `CronJob(reconcile_cloud_jobs, cron="*/5 * * * *")`; `submit_cloud_job` in `CONTROLLER_TASKS` (enqueue_router.py:51) |
| 3 | Analysis result authoritative ONLY via out-of-band callback; no kube watch | ✓ VERIFIED | `reconcile_cloud_jobs.py` — zero calls to `put_analysis` or `report_analysis_failed` (grep returns 0); module docstring line 6: "This cron NEVER writes an analysis result. It is a cron-only POLL (D-01): every tick it re-reads the in-flight Jobs/Workloads; there is NO live kube watch stream" |
| 4 | Reconcile distinguishes healthy Pending (silent) from Inadmissible (operator alert surfaced); detects success/failure/eviction | ✓ VERIFIED | `reconcile_cloud_jobs.py:232-233` — Pending silently tallied, no flag change; lines 218-229 — Inadmissible sets `cloud_job.inadmissible=True` + WARNING log, never touches `attempts`; `pipeline.py:820-835` `get_inadmissible_count` degrade-safe reader wired into router at lines 498/564; `inadmissible_card.html` with `{% if inadmissible_count %}` gate; OOB push in `stats_bar.html:90` |
| 5 | Bounded re-drive → ANALYSIS_FAILED at cap; no TTL-vs-read race (delete-after-record); no `process_file` ledger seed | ✓ VERIFIED | `reconcile_cloud_jobs.py:162-167` — at cap: `session.commit()` → `s3_staging.delete_staged_object` → `kube_staging.delete_job`; under cap race guard: `_job_gone()` check at line 174 before re-enqueue; `submit_cloud_job.py` — zero `SchedulingLedger` references; `backoffLimit: 0` in manifest neutralizes pod-level retry |

**Score:** 5/5 truths verified

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/config.py` | `cloud_submit_max_attempts` + kube_* fields + SecretStr creds in SECRET_FILE_FIELDS | ✓ VERIFIED | Lines 439-444: `cloud_submit_max_attempts` Field(default=3, gt=0, lt=20); lines 533-578: 7 kube fields (kube_api_url, kube_namespace, kube_local_queue, kube_job_image, kube_job_cpu_request, kube_job_memory_request, kube_workload_api_version="kueue.x-k8s.io/v1beta1"); lines 353-354: kube_kubeconfig + kube_sa_token as SecretStr in SECRET_FILE_FIELDS; no Phase-55 kube→cloud_burst model validator |
| `pyproject.toml` | kr8s control-plane dependency | ✓ VERIFIED | Line 27: `"kr8s>=0.20.15"` |
| `src/phaze/models/cloud_job.py` | SUBMITTED/RUNNING/SUCCEEDED enum members + kueue_workload/attempts/inadmissible columns + 6-member CHECK | ✓ VERIFIED | Lines 44-46: three new StrEnum members; lines 66-72: three new columns with correct types and defaults; line 76: 6-member CHECK `status IN ('uploading', 'uploaded', 'submitted', 'running', 'succeeded', 'failed')`; `cloud_phase` absent (grep confirms 1 count is comment-only) |
| `alembic/versions/026_add_cloud_job_kube_columns.py` | Additive reversible migration, down_revision="025", no saq_jobs | ✓ VERIFIED | Line 40: `down_revision = "025"`; upgrade() adds 3 columns + swaps CHECK; downgrade() reverses; no `saq_jobs` in op.* calls |
| `src/phaze/services/kube_staging.py` | build_job_manifest, submit_job (409-idempotent), list_inflight_jobs (deferred), get_job, get_workload_for (label+owner-ref fallback), delete_job (404-idempotent); NO ORM imports | ✓ VERIFIED | 234 lines; all 6 functions present; grep for `sqlalchemy`/`phaze.models` returns 0; `list_inflight_jobs` docstring at line 183 marks it deferred; `get_workload_for` label-hit path at line 207, owner-ref fallback at lines 211-215, returns None only when both miss |
| `tests/kube_fakes.py` | `fake_workload`/`fake_job` factories + PENDING/INADMISSIBLE/ADMITTED/EVICTED constants | ✓ VERIFIED | Lines 21-59: all factories and constants present with correct `(type, status, reason)` tuples |
| `tests/conftest.py` | `kube_respx` fixture | ✓ VERIFIED | Line 307: `def kube_respx(monkeypatch)` |
| `src/phaze/tasks/submit_cloud_job.py` | Fast controller-queue task; upsert cloud_job SUBMITTED+kueue_workload; no SchedulingLedger; `submit_cloud_job_key` helper | ✓ VERIFIED | 102 lines; function at line 55; upsert at lines 79-97; `SchedulingLedger` grep returns 0; `submit_cloud_job_key` at line 44 |
| `src/phaze/services/enqueue_router.py` | `submit_cloud_job` in CONTROLLER_TASKS; `reconcile_cloud_jobs` absent (cron-only) | ✓ VERIFIED | Line 51: `"submit_cloud_job"` in CONTROLLER_TASKS; grep for `reconcile_cloud_jobs` returns 0 |
| `src/phaze/tasks/controller.py` | `reconcile_cloud_jobs` imported + in functions + CronJob `*/5`; `submit_cloud_job` imported + in functions; no CronJob for submit | ✓ VERIFIED | Line 40: import reconcile; line 44: import submit; line 217: submit in functions; line 220: reconcile in functions; line 251: `CronJob(reconcile_cloud_jobs, cron="*/5 * * * *")`; no CronJob for submit_cloud_job |
| `src/phaze/tasks/reconcile_cloud_jobs.py` | Cron-only poll; SUBMITTED/RUNNING iteration; full state machine; delete-after-record; no result writers; re-drive race guard | ✓ VERIFIED | 287 lines; `status.in_([SUBMITTED, RUNNING])` at line 263; all 8 outcome branches implemented; D-04 ordering in `_handle_no_callback_terminal` at lines 162-167; `_job_gone` race guard at lines 95-108; `put_analysis`/`report_analysis_failed` grep returns 0 |
| `src/phaze/services/pipeline.py` | `get_inadmissible_count` degrade-safe reader | ✓ VERIFIED | Lines 820-835: `_safe_count` on `CloudJob.inadmissible.is_(True)` |
| `src/phaze/routers/pipeline.py` | `inadmissible_count` in both dashboard and stats contexts | ✓ VERIFIED | Line 34: import; line 498: dashboard context; line 564: stats poll context |
| `src/phaze/templates/pipeline/partials/inadmissible_card.html` | `id="inadmissible-card"`, OOB-capable, `{% if inadmissible_count %}` gate | ✓ VERIFIED | Lines 19-33: section with id, oob conditional, warning content gated behind `{% if inadmissible_count %}` |
| `src/phaze/templates/pipeline/dashboard.html` | Includes inadmissible_card outside #pipeline-stats | ✓ VERIFIED | Line 35: `{% include "pipeline/partials/inadmissible_card.html" %}` |
| `src/phaze/templates/pipeline/partials/stats_bar.html` | OOB re-push of inadmissible card on 5s poll | ✓ VERIFIED | Line 90: `{% with oob = True %}{% include "pipeline/partials/inadmissible_card.html" %}{% endwith %}` inside `{% if oob_counts %}` block |

---

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `config.py` `ControlSettings.SECRET_FILE_FIELDS` | `_resolve_secret_files` before-validator | `kube_kubeconfig`/`kube_sa_token` membership | ✓ WIRED | Lines 353-354 add both to the frozenset; inherited validator auto-resolves `_FILE` siblings |
| `kube_staging.py` `build_job_manifest` | `ControlSettings` kube surface | `cfg.kube_api_url`, `cfg.kube_namespace`, `cfg.kube_local_queue`, `cfg.kube_job_image`, cpu/memory request fields | ✓ WIRED | Lines 119, 121, 141-142 |
| `submit_cloud_job.py` | `kube_staging.submit_job` | Single kube POST returning `(name, uid)` recorded as `kueue_workload` | ✓ WIRED | Line 72: `name, _uid = await kube_staging.submit_job(fid)`; line 87: `kueue_workload=name` in upsert |
| `enqueue_router.py CONTROLLER_TASKS` | `controller.settings["functions"]` | `submit_cloud_job` present in both | ✓ WIRED | enqueue_router.py:51; controller.py:217 |
| `reconcile_cloud_jobs.py` | `cloud_job` sidecar SUBMITTED/RUNNING | `SELECT cloud_job WHERE status IN (SUBMITTED, RUNNING)` | ✓ WIRED | Line 263: `CloudJob.status.in_([CloudJobStatus.SUBMITTED.value, CloudJobStatus.RUNNING.value])` |
| `reconcile_cloud_jobs.py` terminal ordering | `s3_staging.delete_staged_object` + `kube_staging.delete_job` | record+commit → S3 delete → Job delete (D-04/D-05) | ✓ WIRED | Lines 162-167 in `_handle_no_callback_terminal` |
| `controller.py` cron_jobs | `reconcile_cloud_jobs` | `CronJob(reconcile_cloud_jobs, cron="*/5 * * * *")` | ✓ WIRED | Line 251 |
| `reconcile_cloud_jobs.py` re-drive path | `kube_staging.delete_job` + `_job_gone` + `submit_cloud_job` enqueue | Delete prior Job + confirm gone before fresh submit | ✓ WIRED | Lines 173-180 in `_handle_no_callback_terminal` under cap |
| `pipeline.py get_inadmissible_count` | `routers/pipeline.py` both render paths | `inadmissible_count` in dashboard() + /pipeline/stats contexts | ✓ WIRED | pipeline.py:820-835; routers/pipeline.py:498, 564 |
| `routers/pipeline.py` `inadmissible_count` | `inadmissible_card.html` `{% if inadmissible_count %}` gate | Template context → conditional render | ✓ WIRED | Card visible only when count > 0; OOB re-push in stats_bar.html |

---

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|--------------------|--------|
| `inadmissible_card.html` | `inadmissible_count` | `get_inadmissible_count()` → `_safe_count(session, COUNT(CloudJob.inadmissible IS True))` | Yes — real DB aggregate count | ✓ FLOWING |
| `reconcile_cloud_jobs` tally | `cloud_job` rows | `SELECT cloud_job WHERE status IN (SUBMITTED, RUNNING)` | Yes — live ORM query | ✓ FLOWING |

---

### Behavioral Spot-Checks

Step 7b: SKIPPED — the core deliverables (kube seam, submit task, reconcile cron) require a live Kubernetes API cluster not available without running services. The test suite covers all behaviors via monkeypatched seam (2422 tests passing as reported). No runnable entry-point tests are feasible without the cluster.

---

### Probe Execution

No probe scripts declared or found for this phase.

---

### Requirements Coverage

| Requirement | Source Plans | Description | Status | Evidence |
|-------------|-------------|-------------|--------|----------|
| KSUBMIT-01 | 54-01, 54-03, 54-05 | Suspended batch/v1 Job, idempotent via deterministic `file_id`-keyed name | ✓ SATISFIED | `kube_staging.py:103-150`; `submit_cloud_job.py` upsert |
| KSUBMIT-02 | 54-05, 54-06 | Submit returns within seconds; reconcile cron owns lifecycle | ✓ SATISFIED | `submit_cloud_job.py` returns after ONE POST; `controller.py:251` `*/5` CronJob |
| KSUBMIT-03 | 54-06 | Analysis result authoritative ONLY via out-of-band callback | ✓ SATISFIED | `reconcile_cloud_jobs.py` — zero `put_analysis`/`report_analysis_failed` calls (grep=0) |
| KSUBMIT-04 | 54-04, 54-06 | Reconcile distinguishes Pending from Inadmissible; detects success/failure/eviction | ✓ SATISFIED | State machine in `reconcile_cloud_jobs.py:232-238`; Inadmissible alert via `get_inadmissible_count` + `inadmissible_card.html` |
| KSUBMIT-05 | 54-03, 54-06 | Bounded re-drive cap → ANALYSIS_FAILED; `backoffLimit: 0` neutralizes pod retry | ✓ SATISFIED | `kube_staging.py:130` `backoffLimit: 0`; `reconcile_cloud_jobs.py:162-169` cap→ANALYSIS_FAILED |
| KSUBMIT-06 | 54-05, 54-06 | No `process_file` ledger seed; delete-after-record ordering | ✓ SATISFIED | `submit_cloud_job.py` grep=0 for SchedulingLedger; D-04 ordering in `_handle_no_callback_terminal` |

All 6 KSUBMIT requirements satisfied. No orphaned requirements found.

---

### Anti-Patterns Found

No blockers or warnings found. Scanned: `kube_staging.py`, `submit_cloud_job.py`, `reconcile_cloud_jobs.py`, `cloud_job.py`, `026_add_cloud_job_kube_columns.py`, `config.py`, `pipeline.py`, `routers/pipeline.py`, `controller.py`.

- Zero TBD/FIXME/XXX markers in any modified file
- Zero stub returns (no `return null`/`return []`/`return {}` in live paths)
- Zero placeholder templates
- `list_inflight_jobs` is intentionally deferred with documented docstring — not a stub; exercised by tests

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| (none) | — | — | — | — |

---

### Human Verification Required

None. All observable truths are verifiable from code inspection and test structure:
- The reconcile state machine logic is covered by 12 monkeypatched transition tests in `test_reconcile_cloud_jobs.py` (lines 206-519)
- The inadmissible alert rendering is covered by `test_pipeline_inadmissible.py` (4 test functions including warning copy presence/absence)
- The kube seam purity (no ORM imports) is asserted by `test_kube_staging_has_no_orm_imports` at line 304

No visual or UX items require human testing beyond what the automated test suite covers.

---

### Gaps Summary

No gaps. All 5 ROADMAP success criteria are VERIFIED against the actual codebase. All 6 KSUBMIT requirement IDs are satisfied. All 16 expected artifacts exist and are substantive and wired. No unresolved debt markers.

---

_Verified: 2026-06-28_
_Verifier: Claude (gsd-verifier)_
