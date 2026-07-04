---
phase: 68
slug: backend-protocol-3-implementations
status: verified
threats_open: 0
asvs_level: 1
created: 2026-07-03
---

# Phase 68 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.
> Behavior-preserving refactor — `if/elif cloud_target` dispatch switch replaced by a `Backend`
> protocol (Local/ComputeAgent/Kueue), additive nullable `cloud_job.backend_id`, uniform per-backend
> in-flight accounting. Every declared mitigation verified against implementation code (not intent).

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| control-plane → compute-agent | Heartbeat/agent gate (GATE-1), read-only; `is_available` probes `select_active_agent(kind="compute")` | Agent liveness (no secrets) |
| control-plane → Kueue cluster | `KueueBackend.is_available` LocalQueue probe + kr8s submit/reconcile; creds control-side only (DIST-01) | Kube API calls; SA token stays control-side, never logged |
| compute-agent → control-plane `/pushed`,`/mismatch` | Token-authed internal callbacks (`agent_push.py`); `file_id` on URL path, agent from token (AUTH-01) | Push outcome; `expected_sha256` read control-side (D-11), never agent-supplied |
| Alembic DDL → cloud_job | Migration 029, control-plane DB only, additive nullable columns | Schema change (cloud_job only, never saq_jobs) |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-68-01 | Info Disclosure | test fixtures logging secrets | accept | Tests construct no real SecretStr/`*_file` values; snapshot serializes only `{id,kind,rank,cap}` projections + call-arg names | closed |
| T-68-02 | Tampering | migration touching saq_jobs (SAQ-owned) | mitigate | Migration 029 `upgrade`/`downgrade` reference only `"cloud_job"` (029:39-46); saq_jobs appears only in the CRITICAL docstring banner (029:14-16); grep-assert test green | closed |
| T-68-03 | DoS | s3_key nullability weakening integrity | accept | `s3_key` NOT NULL was S3-lifecycle only; compute rows carry no object (nullable — model cloud_job.py:76, migration 029:40); kueue/S3 rows still stamp it | closed |
| T-68-04 | Info Disclosure | backend structlog leaking kube SA token / S3 creds | mitigate | `backends.py` logs only `file_id`/`backend_id`/tally fields (200,297,357); no SecretStr/`*_file`; SA token read via re-homed body, never logged (D-05) | closed |
| T-68-05 | DoS | is_available/dispatch/reconcile raising out of the cron | mitigate | compute `is_available` catches `NoActiveAgentError`→False (backends.py:227-231); kueue catches broad `Exception`→False (294-298); reconcile per-row rollback guard (354-357) | closed |
| T-68-06 | EoP | DIST-01 media-plane boundary | mitigate | Control plane stays sole S3 importer/presigner: `KueueBackend.dispatch` calls single-cluster `_stage_file_to_s3` verbatim (313); `report_pushed` reads `sha256_hash` control-side (agent_push.py:137), builds scratch_path from control settings (131); pods/agents credential-free | closed |
| T-68-07 | Tampering | dispatch committing mid-body, releasing advisory lock | mitigate | No `session.commit()` in any `dispatch` body (backends.py:233-314); drain owns the single post-loop commit (release_awaiting_cloud.py:173, Landmine L1) | closed |
| T-68-08 | Tampering | duplicate/late /pushed callback double-terminalizing | mitigate | `report_pushed` gates the state flip on `state == PUSHING` and returns early when `rowcount == 0` (agent_push.py:107-118); `cloud_job` SUCCEEDED write (127) only reached after the guard | closed |
| T-68-09 | Tampering | dispatch committing mid-loop, releasing advisory lock | mitigate | Drain keeps single post-loop commit (release_awaiting_cloud.py:173); dispatch never commits; snapshot + staging_cron tests assert tick shape | closed |
| T-68-10 | DoS | is_available/dispatch raising out of the cron | mitigate | `cloud_enabled` early no-op (release_awaiting_cloud.py:95); GATE-1 hold (123-125); GATE-2 `NoActiveAgentError`→hold (140-144); per-file `NoActiveAgentError`→clean hold (162-168) | closed |
| T-68-11 | Info Disclosure | reader rewire leaking backend identity/creds to dashboard | accept | `resolved_non_local_kind` returns only a kind string (backends.py:387-406); consumed by dashboard/callback readers (pipeline.py:576,811; agent_s3.py:114) — no secret/creds surfaced | closed |
| T-68-12 | Info Disclosure | log_effective_registry leaking SecretStr / `*_file` paths | mitigate | Projection logs only `{id,kind,rank,cap}` + `cloud_enabled` (config.py:527-528); never a whole model/SecretStr/`*_file` path | closed |
| T-68-13 | DoS | removing the >1-non-local raise leaving no fail-fast guard | mitigate | Boot guard in `resolve_backends` (backends.py:379-383); `resolved_non_local_kind` ALSO raises on >1 (401-405, WR-01); `_single_non_local` raise retained in config as defense-in-depth (config.py:475-479); controller wraps `resolve_backends` at boot (controller.py:184-193) | closed |
| T-68-SC | Tampering | npm/pip/cargo installs | accept (N/A) | Zero package installs this phase — all five SUMMARY `tech-stack.added: []` (zero-new-deps lock) | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

### Code-review fixes that strengthen mitigations (verified present)

| Fix | Reinforces | Evidence |
|-----|-----------|----------|
| CR-01 (`report_push_mismatch` cap-reached branch) | Accounting integrity (D-02) | Terminalizes compute `cloud_job`→FAILED in the same txn as the ANALYSIS_FAILED flip (agent_push.py:193); idempotent no-op for non-compute (0 rows) — safe and idempotent (commit af7756e) |
| WR-02 (`stage_cloud_window` drain loop) | T-68-05 / T-68-10 (never raise out of cron) | Per-file `backend.dispatch` wrapped in `try/except NoActiveAgentError` → clean hold of remaining candidates, break (release_awaiting_cloud.py:162-168); dispatch gates the fileserver before any mutation (backends.py:245), so the raising file is untouched (commit 0ccf6a3) |
| WR-01 (`resolved_non_local_kind`) | T-68-13 | Raises `ValueError` naming offending ids on `len(non_local) > 1` (backends.py:401-405); all-local + single-non-local paths byte-identical (commit 1671732) |

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-68-01 | T-68-01 | Test fixtures never construct real `SecretStr`/`*_file` values; the D-01 golden snapshot serializes only `{id,kind,rank,cap}`-level projections and call-arg names — no secret material can reach a test log or snapshot artifact. Test-only surface, no production exposure. | Phase 68 plan (register-authored) | 2026-07-03 |
| AR-68-03 | T-68-03 | `s3_key` NOT NULL was an S3-lifecycle-only constraint; a compute burst rsync-pushes over Tailscale and legitimately carries no S3 object, so its `cloud_job` row leaves `s3_key` NULL (model cloud_job.py:76). Kueue/S3-staged rows still stamp it — no integrity loss on the S3 path. | Phase 68 plan (D-08) | 2026-07-03 |
| AR-68-11 | T-68-11 | `resolved_non_local_kind` surfaces only a kind literal (`"local"`/`"compute"`/`"kueue"`) to the dashboard/callback readers; no backend identity secret, bucket cred, or SA token crosses the reader boundary. | Phase 68 plan (D-09) | 2026-07-03 |
| AR-68-SC | T-68-SC | Zero package installs this phase (behavior-preserving refactor over existing deps); all five plan SUMMARYs record `tech-stack.added: []` — no supply-chain surface introduced. | Phase 68 plan (zero-new-deps lock) | 2026-07-03 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-07-04 | 13 (+T-68-SC) | 13 | 0 | gsd-security-auditor (Claude) |

Unregistered flags: none. All five plan SUMMARYs (`## Threat Flags`) report **None** — no new network endpoint, auth path, or trust-boundary surface introduced (every dispatch/staging/submit/reconcile body is a verbatim re-home).

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-07-04
