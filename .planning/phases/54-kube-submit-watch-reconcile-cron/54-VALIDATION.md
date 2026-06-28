---
phase: 54
slug: kube-submit-watch-reconcile-cron
status: verified
nyquist_compliant: true
wave_0_complete: true
created: 2026-06-27
updated: 2026-06-28
---

# Phase 54 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Reconciled to the actual test filenames the plans create (54-01..54-06).

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x (pytest-asyncio, `asyncio_mode = "auto"`) |
| **Config file** | pyproject.toml `[tool.pytest.ini_options]` (`testpaths = ["tests"]`) |
| **Quick run command** | `uv run pytest tests/test_tasks/test_submit_cloud_job.py tests/test_tasks/test_reconcile_cloud_jobs.py tests/test_services/test_kube_staging.py -x` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~60 seconds |

---

## Sampling Rate

- **After every task commit:** Run the quick run command above (seam + submit + reconcile — the highest-value fake-kube coverage).
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite green + ≥85% coverage + `pre-commit run --all-files` (ruff/mypy/bandit).
- **Max feedback latency:** 60 seconds

---

## Per-Task Verification Map

> Populated from RESEARCH.md "## Validation Architecture" (the critical-transition tests mapped to KSUBMIT-01..06 / D-01..D-09) against the actual plan tasks. Every row's test file is created by the named plan (Wave-0-first within each `tdd` plan).

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 54-01-T2 | 54-01 | 1 | KSUBMIT-01, KSUBMIT-05 (D-08) | T-54-07 | `cloud_submit_max_attempts` knob + optional kube_* config; kube creds as `_FILE` SecretStr (never logged) | unit | `uv run pytest tests/test_config/test_kube_settings.py -x` | ✅ | ✅ green |
| 54-02-T1 | 54-02 | 1 | KSUBMIT-04, KSUBMIT-05, KSUBMIT-06 (D-09) | — | CloudJobStatus gains SUBMITTED/RUNNING/SUCCEEDED; cloud_job gains kueue_workload/attempts/inadmissible | unit | `uv run pytest tests/test_models/test_cloud_job.py -x` | ✅ | ✅ green |
| 54-02-T2 | 54-02 | 1 | D-09 | — | Migration 026 additive + reversible; CHECK membership includes new status members; touches only cloud_job | unit | `uv run pytest tests/test_migrations/test_migration_026_kube_columns.py -x` | ✅ | ✅ green |
| 54-03-T1 | 54-03 | 2 | KSUBMIT-01..06 (substrate) | — | fake_workload/fake_job factories (incl. ownerReferences for the owner-ref fallback) + kube_respx discovery fixture | unit | `uv run python -c "from tests.kube_fakes import fake_workload, fake_job, PENDING, INADMISSIBLE, ADMITTED, EVICTED"` | ✅ | ✅ green |
| 54-03-T2 | 54-03 | 2 | KSUBMIT-01, KSUBMIT-05, KSUBMIT-06 | T-54-06/07/08/09 | suspended-Job manifest (requests-only, backoffLimit 0, queue-name label, ttl 900); 409-idempotent create; 404-idempotent delete; get_workload_for label-hit + owner-ref fallback + None; no-ORM purity | unit (respx) | `uv run pytest tests/test_services/test_kube_staging.py -x` | ✅ | ✅ green |
| 54-04-T1 | 54-04 | 2 | KSUBMIT-04 (D-06) | — | degrade-safe get_inadmissible_count reader (DB hiccup → 0, never 500) | unit | `uv run pytest tests/test_services/test_pipeline_counts.py -x` | ✅ | ✅ green |
| 54-04-T2 | 54-04 | 2 | KSUBMIT-04 (D-06) | — | OOB Inadmissible warning card shown only when count > 0; healthy Pending invisible | unit | `uv run pytest tests/test_routers/test_pipeline_inadmissible.py -x` | ✅ | ✅ green |
| 54-05-T1 | 54-05 | 3 | KSUBMIT-01, KSUBMIT-02, KSUBMIT-06 | T-54-19 | fast submit (one POST), 409-idempotent, writes ONLY cloud_job (no process_file ledger seed) | unit | `uv run pytest tests/test_tasks/test_submit_cloud_job.py -x` | ✅ | ✅ green |
| 54-05-T2 | 54-05 | 3 | KSUBMIT-02 | — | submit_cloud_job routable in enqueue_router.CONTROLLER_TASKS + controller functions; control-only | unit | `uv run pytest tests/test_services/test_enqueue_router.py tests/test_task_split.py -x` | ✅ | ✅ green |
| 54-06-T1 | 54-06 | 4 | KSUBMIT-02..06 (D-01,02,04,05,06,07,08) | T-54-15..19 | full status→outcome state machine; delete-after-record ordering; S3 delete on no-callback terminal; bounded re-drive + re-drive-race guard; Inadmissible no-cap; never writes a result | unit | `uv run pytest tests/test_tasks/test_reconcile_cloud_jobs.py -x` | ✅ | ✅ green |
| 54-06-T2 | 54-06 | 4 | D-01, D-03 | — | reconcile registered as fixed `*/5` CronJob on the controller, control-only (not routable) | unit | `uv run pytest tests/test_task_split.py -x` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Validation Audit 2026-06-28

| Metric | Count |
|--------|-------|
| Requirements mapped | 11 tasks (KSUBMIT-01..06 / D-01..09) |
| COVERED | 11 |
| PARTIAL | 0 |
| MISSING | 0 |
| Gaps found | 0 |
| Resolved | 0 (none needed) |
| Escalated | 0 |

Audited in State A (planning-time strategy reconciled to executed reality). All 10 mapped test modules exist and run green — 115 tests passed against the ephemeral test DB (Postgres :5433 / Redis :6380); the full suite is 2432 passing. Path correction: `test_enqueue_router.py` lives under `tests/test_services/`. No test generation required — every in-scope behavior already has automated verification. Run by: orchestrator (no gsd-nyquist-auditor spawn needed — zero gaps).

---

## Wave 0 Requirements

> Test scaffolds each `tdd`/test-bearing plan created first (RED before GREEN). All present and green post-execution:

- [x] `tests/test_config/test_kube_settings.py` — `cloud_submit_max_attempts` + kube_* config + `_FILE` creds (Plan 54-01)
- [x] `tests/test_models/test_cloud_job.py` — extended CloudJobStatus + new columns (Plan 54-02)
- [x] `tests/test_migrations/test_migration_026_kube_columns.py` — migration 026 upgrade/downgrade + CHECK membership (Plan 54-02)
- [x] `tests/kube_fakes.py` — Layer-1 `fake_workload(*conditions, owner_uid=None)` / `fake_job(...)` factories + PENDING/INADMISSIBLE/ADMITTED/EVICTED/QUOTA_RESERVED constants (Plan 54-03)
- [x] `tests/conftest.py` — `kube_respx` fixture stubbing kr8s API-discovery endpoints (Plan 54-03)
- [x] `tests/test_services/test_kube_staging.py` — seam (respx): create/201, create/409, get, list-by-label, delete/200, delete/404, get_workload_for label-hit + owner-ref fallback + None, manifest spec, import-boundary purity (Plan 54-03)
- [x] `tests/test_services/test_pipeline_counts.py` — degrade-safe inadmissible count reader (Plan 54-04)
- [x] `tests/test_routers/test_pipeline_inadmissible.py` — OOB inadmissible card render (Plan 54-04)
- [x] `tests/test_tasks/test_submit_cloud_job.py` — submit spec, idempotency, fast-return, no-ledger-seed (Plan 54-05)
- [x] `tests/test_tasks/test_reconcile_cloud_jobs.py` — the full condition→outcome state machine incl. the re-drive-race guard test (Plan 54-06)
- [x] `tests/test_services/test_enqueue_router.py` + `tests/test_task_split.py` — control-only registration assertions for submit_cloud_job + reconcile_cloud_jobs (Plans 54-05/54-06)

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live Kueue admission against a real cluster | KSUBMIT-04 | Phase is testable against a fake kube API; live admission/RBAC is Phase 56 | Deferred to Phase 56 deploy/runbook |
| Exact `kueue.x-k8s.io/job-uid` Workload-link label (A2) | KSUBMIT-04 | The precise live label key can only be confirmed against the deployed Kueue version; Phase 54 ships the owner-reference fallback so a wrong key degrades gracefully | Phase 56: confirm `get_workload_for` resolves via the label on the live cluster (fallback covers the miss) |
| kr8s 0.20.15 async constructor form (Q3) | KSUBMIT-01 | Exact constructor signature verified against the live client; Phase 54 logic is independent via the monkeypatched seam | Phase 56: confirm `Job(dict, api=api)` + `await job.create()` against the live API |

*All in-scope Phase 54 behaviors have automated verification against the fake kube API.*

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (reconciled to real filenames)
- [x] No watch-mode flags
- [x] Feedback latency < 60s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** verified 2026-06-28 — nyquist-compliant, all 11 mapped tasks COVERED (0 gaps)
