---
phase: 54
slug: kube-submit-watch-reconcile-cron
status: verified
threats_open: 0
asvs_level: 1
created: 2026-06-28
---

# Phase 54 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| Operator → `uv add kr8s` | A new third-party dependency enters the control-plane build | Package bytes (supply-chain risk) |
| Control plane → Kubernetes (Kueue) API | The control plane reaches an external kube API over Tailscale/WireGuard | SA bearer token / kubeconfig (SecretStr); suspended Job manifests |
| ControlSettings ↔ `_FILE` secret mounts | Kube credentials resolved from file-based secrets, never request bodies | `kube_kubeconfig` / `kube_sa_token` (SecretStr) |
| Reconcile cron ↔ `cloud_job` sidecar | The in-flight registry the cron iterates and mutates | Job lifecycle state (status, attempts, inadmissible) |
| Out-of-band callback (`/api/internal/agent/analysis/{file_id}`) | The SOLE authoritative analysis-result channel | Analysis result (reconcile never writes one) |
| Pipeline dashboard → operator | The Inadmissible alert card rendered to the admin UI | Integer count + static string (autoescaped, no PII) |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-54-SC | Tampering | `uv add kr8s` install | mitigate | Blocking-human kr8s legitimacy gate (operator pre-approved kr8s-org / BSD-3 / pure-Python); pinned `kr8s>=0.20.15` — 54-01-SUMMARY.md:80-87, pyproject.toml:27 | closed |
| T-54-01 | Information disclosure | kube creds on ControlSettings | mitigate | `kube_kubeconfig`/`kube_sa_token` SecretStr in SECRET_FILE_FIELDS; `_FILE`-resolved, never logged — config.py:570-579, 348-355 | closed |
| T-54-02 | Tampering | config field bounds | mitigate | `cloud_submit_max_attempts` gt=0/lt=20/default=3 — no unbounded retry storm — config.py:439-445 | closed |
| T-54-03 | Tampering | `cloud_job.status` values | mitigate | 6-member DB CHECK constraint is the authoritative gate — cloud_job.py:74-78, migration 026:45,57 | closed |
| T-54-04 | Denial of service | irreversible migration | mitigate | `downgrade()` reverses both columns + CHECK; round-trip test — 026:60-66, test_migration_026_kube_columns.py:83 | closed |
| T-54-05 | Tampering | accidental `saq_jobs` reference | mitigate | Banner + grep test assert the migration never touches the SAQ-owned table — 026:22-24, test:62-66 | closed |
| T-54-06 | Tampering | Job-name injection | mitigate | `phaze-analyze-<file_id>` (server UUID, DNS-1123 safe); no operator free-text — kube_staging.py:61-69 | closed |
| T-54-07 | Information disclosure | kube creds in the client factory | mitigate | Token via `SecretStr.get_secret_value()`, never logged; session rebuild applies the Bearer header (fix 280a2de), respx-covered — kube_staging.py:87-106 | closed |
| T-54-08 | Elevation of privilege | pod-level / infra retry | mitigate | `backoffLimit: 0` neutralizes Job retry; control plane owns the retry budget — kube_staging.py:154 | closed |
| T-54-09 | Denial of service | repeated delete on a missing Job | mitigate | `delete_job` swallows NotFound → idempotent — kube_staging.py:255-256 | closed |
| T-54-10 | Denial of service | hot 5s `/pipeline/stats` poll | mitigate | `get_inadmissible_count` degrade-safe (`_safe_count` → 0 on error) — pipeline.py:820-840 | closed |
| T-54-11 | Information disclosure | dashboard alert content | accept | Alert renders only an integer count + static string under Jinja autoescape; no operator free-text / PII — inadmissible_card.html:21,29 | closed |
| T-54-12 | Elevation of privilege | `process_file` ledger seed for a K8s file | mitigate | `submit_cloud_job` writes ONLY `cloud_job`; grep test asserts no SchedulingLedger import/write — submit_cloud_job.py, test:181 | closed |
| T-54-13 | Tampering | enqueue onto a consumer-less default queue | mitigate | `submit_cloud_job` in CONTROLLER_TASKS; `resolve_queue_for_task` fails loud on an unroutable task — enqueue_router.py:51,143,156,162 | closed |
| T-54-14 | Denial of service | duplicate Job submission | mitigate | Deterministic name + seam 409→refresh + `on_conflict_do_update(file_id)` upsert — submit_cloud_job.py:72,88-96 | closed |
| T-54-15 | Spoofing/Repudiation | trusting kube status as the analysis result | mitigate | Reconcile NEVER calls `put_analysis`/`report_analysis_failed`; grep test asserts zero result-writer calls — reconcile_cloud_jobs.py, test:422-429 | closed |
| T-54-16 | Tampering | TTL-vs-read race losing a terminal outcome | mitigate | D-04 delete-after-record ordering (record+commit → S3 delete → Job delete); TTL(900s) backstop only — reconcile_cloud_jobs.py:132-135,167-169 | closed |
| T-54-17 | Information disclosure | staged S3 object leak on eviction / lost pod | mitigate | D-05: `delete_staged_object(file_id)` precedes the Job delete on the no-callback terminal; bucket-lifecycle TTL backstop — reconcile_cloud_jobs.py:168-169 | closed |
| T-54-18 | Denial of service | unbounded re-drive loop | mitigate | D-08 bounded `cloud_submit_max_attempts` → ANALYSIS_FAILED; Inadmissible holds without consuming the cap — reconcile_cloud_jobs.py:163-172,232-243 | closed |
| T-54-19 | Elevation of privilege | `process_file` ledger seed via re-drive | mitigate | The re-drive enqueues `submit_cloud_job` (controller queue) only; reconcile reads `cloud_job`, never `recover_orphaned_work` — reconcile_cloud_jobs.py:111-121 | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-54-01 | T-54-11 | The Inadmissible dashboard alert interpolates only a server-computed integer count and a static English string through Jinja autoescape — no operator free-text, file paths, or PII reach the rendered card. XSS/disclosure surface is nil. | Robert Wlodarczyk | 2026-06-28 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-06-28 | 20 | 20 | 0 | gsd-security-auditor (opus) |

Notes: register authored at plan time (all 6 plans carry a `<threat_model>` block) — verified in verify-mitigations mode, no new-threat scan. Recovery-side Inadmissible-flag clearing (alert clears on success/Pending/Admitted) verified at reconcile_cloud_jobs.py per fix 280a2de. No implementation gaps; no unregistered trust boundaries.

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-06-28
