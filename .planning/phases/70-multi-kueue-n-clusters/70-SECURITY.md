---
phase: 70
slug: multi-kueue-n-clusters
status: verified
threats_open: 0
asvs_level: 1
created: 2026-07-04
---

# Phase 70 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.
> Register authored at plan time (all 5 plans carry `<threat_model>` blocks); auditor VERIFIED each mitigation exists in the implementation with file:line evidence. 22/22 threats closed.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| operator config → control plane | `KubeConfig` (incl. new `context`) + `BucketConfig` supplied via `backends.toml`; secrets are `SecretStr` | kube context names (non-secret), kubeconfig/SA tokens + S3 creds (secret) |
| control plane → N kube API servers | per-backend kr8s client from an operator-supplied `KubeConfig`; creds control-plane-only (DIST-01) | kube API auth, Job/Workload manifests |
| control plane → S3 (per bucket) | every presign/upload/delete minted control-side from a per-bucket `BucketConfig` | presigned URLs, bucket creds (control-only) |
| pod/agent → S3 | pods/agents receive only presigned, file_id-scoped, TTL-bounded URLs; never bucket creds | presigned object URLs |
| pod → control plane | pods are kube-credential-free; call back over the mounted CA (`report_uploaded`) | upload-complete callbacks |
| N kube clusters → drain tick | a flaky/timing-out cluster is untrusted input to the once-per-tick snapshot loop | cluster availability / in-flight counts |
| reconcile txn ↔ concurrent drain tick | both take `pg_advisory_xact_lock(5_000_504)`; spillover-cleanup correctness lives in the lock/txn boundary | staged-object deletes, state flips |
| control plane → Postgres | additive DDL migration 030 runs control-side only | schema (cloud_job only) |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation (verified evidence) | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-70-01-01 | Information Disclosure | new `KubeConfig.context` field | accept | `config_backends.py:144-160` — `context: str \| None` plain non-secret ("NOT a secret"); kubeconfig/sa_token remain `SecretStr` | closed |
| T-70-04-01 | Tampering | migration 030 DDL | mitigate | `alembic/versions/030_*.py:39,44` additive nullable `staging_bucket`, clean-drop downgrade; `test_migration_never_references_saq_jobs` (`tests/integration/test_migrations/test_migration_030_staging_bucket.py:77-81`) | closed |
| T-70-02-01 | Tampering | `pick_bucket` determinism | mitigate | `s3_staging.py:69-88` — `hashlib.sha256(file_id.bytes)` (not salted `hash()`), `sorted()` stable order | closed |
| T-70-SC | Tampering | PyYAML dependency declaration | accept | `pyproject.toml:53` `PyYAML>=6.0.3` explicit + mypy stub override `:115-117`; already transitive via kr8s, cooldown respected | closed |
| T-70-02 | Information Disclosure | staged object on Internet-reachable ("public") bucket | mitigate | `s3_staging.py:217-238` `presign_get` short-TTL (`s3_presign_get_ttl_sec`); creds confined to `_client` (`:108-124`); pods get only presigned URLs (DIST-01) | closed |
| T-70-02-02 | Tampering | presign/delete choosing the wrong bucket | mitigate | all call sites read recorded `cloud_job.staging_bucket` via `resolve_bucket_config` (`agent_s3.py:94,199`, `agent_analysis.py:127`, `agent_files.py:187`); zero `pick_bucket` in routers | closed |
| T-70-02-03 | Tampering | SSRF via malicious `endpoint_url` | mitigate | `config_backends.py:208-220` `_validate_endpoint_url` rejects non-http(s)/host-less; `file_id` is a server UUID (`s3_staging.py:60-66`) | closed |
| T-70-02-04 | Information Disclosure | bucket creds in logs (N buckets) | mitigate | `config_backends.py:192-193` creds `SecretStr`; `s3_staging.py:116-117` `get_secret_value()` only in client build; module ORM-free, logs no `BucketConfig` | closed |
| T-70-02-05 | Tampering | ORM import creeping into `s3_staging` | mitigate | `s3_staging.py:33-37` — only `TYPE_CHECKING` config imports, no `phaze.models`; purity grep-confirmed | closed |
| T-70-01 | Information Disclosure | kubeconfig/SA token leaked in logs (N clusters) | mitigate | `kube_staging.py:94-136` synthesized kubeconfig dict in-memory only, never logged; secrets via `get_secret_value()` | closed |
| T-70-01-02 | Spoofing / EoP | wrong-cluster dispatch via no-arg `kr8s.api()` cache fallback | mitigate | `kube_staging.py:136` `_api` always passes explicit `kubeconfig=`/`context=`; "NEVER call kr8s.asyncio.api() with no args" (`:128-129`); distinct dicts → distinct cached clients | closed |
| T-70-01-03 | Cryptography | TLS to a second cluster | accept (deployment-gated) | no `insecure-skip-tls-verify`/`verify=False` anywhere in `src/phaze/` (grep-confirmed); TLS left to kr8s/httpx. Live TLS verification is a rollout-time E2E item | closed |
| T-70-01-04 | Denial of Service | `/pushed` 500 under ≥2 non-local backends | mitigate | `config.py:469-489` `active_compute_scratch_dir` reduces over ≤1 `kind=="compute"` (not ≤1-non-local); regression-tested | closed |
| T-70-01-05 | Denial of Service | `report_uploaded`/`build_dashboard_context`/backfill 500 under ≥2 Kueue backends | mitigate | `backends.py:468-498` `resolved_non_local_kind` returns `"kueue"` when any non-local is kueue; fail-fast only for >1-compute-only; regression-tested | closed |
| T-70-03-01 | Denial of Service | one unreachable cluster aborting controller boot | mitigate | `controller.py:181-191` per-cluster probe + Redis write each in own `try/except`; "control plane boots regardless (D-05)" | closed |
| T-70-03 | Denial of Service | one flaky cluster poisoning the whole drain tick | mitigate | `release_awaiting_cloud.py:219-240` per-candidate `try/except` around `dispatch` (D-07); healthy backends + local proceed | closed |
| T-70-03-02 | Information Disclosure | error logging in the snapshot loop | mitigate | `release_awaiting_cloud.py:238` logs `backend_id=target.id` only, no `exc_info`/payload | closed |
| T-70-03-03 | Tampering | mid-loop commit re-opening the over-stage window | accept/mitigate | `release_awaiting_cloud.py:249` single post-loop `session.commit()`; no in-loop commit; advisory lock unchanged | closed |
| T-70-04 | Tampering / DoS | cross-bucket object destruction on spillover | mitigate | `reconcile_cloud_jobs.py:203-213` at-cap delete of recorded old object UNDER held `pg_advisory_xact_lock(5_000_504)` (`:205-207`) BEFORE the AWAITING_CLOUD `commit` (`:213`) | closed |
| T-70-04-02 | Denial of Service | a slow/failed S3 delete pinning the per-row lock | mitigate | `reconcile_cloud_jobs.py:205` `contextlib.suppress(Exception)` around idempotent `delete_staged_object`; per-bucket TTL backstop (`s3_staging.py:260-281`); `*/5` cron | closed |
| T-70-04-03 | Tampering | whole-tick lock breaking per-row commit granularity | mitigate | `backends.py:424` `pg_advisory_xact_lock` acquired per-row; `_reconcile_one` commits per row; delete runs inside existing per-row unit | closed |
| T-70-04-04 | Information Disclosure | recorded-bucket mis-resolution | mitigate | `reconcile_cloud_jobs.py:203` `old_bucket_id` captured pre-mutation, `:204` `resolve_bucket_config` (never re-derive), `:211` `staging_bucket = None` after cleanup | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-70-01 | T-70-01-01 | `KubeConfig.context` is a plain kubeconfig context name, not a secret — never `SecretStr`, never logged as sensitive | operator (plan-time disposition) | 2026-07-04 |
| AR-70-02 | T-70-SC | PyYAML is already resolved transitively via pinned kr8s (mature, high-download); explicit declaration is hygiene, no new install, `exclude-newer` cooldown respected | operator (plan-time disposition) | 2026-07-04 |
| AR-70-03 | T-70-01-03 | TLS to Kueue clusters is left to kr8s/httpx defaults (no `insecure-skip-tls-verify`); live per-cluster TLS verification is deferred to a rollout-time E2E item | operator (plan-time disposition) | 2026-07-04 |
| AR-70-04 | T-70-03-03 | The over-stage window is guarded by the single post-loop commit + tick-wide advisory lock (5_000_504), unchanged by the failure-isolation work — no mid-loop commit introduced | operator (plan-time disposition) | 2026-07-04 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-07-04 | 22 | 22 | 0 | gsd-security-auditor (verify-mitigations mode; register authored at plan time) |

**Cross-check:** the 2 code-review blockers + 3 warnings (CR-01 gate-before-flip, CR-02 outer safety-net rollback, WR-01 per-row commits, WR-02 `ClientError`→`S3StagingError`, WR-03 duplicate-bucket-id fail-fast) were confirmed present in code during the audit; none open a threat.

**Non-blocking observation:** the CR-02 tick-level safety net (`release_awaiting_cloud.py:255`) logs with `exc_info=True`, sitting outside the T-70-03-02-scoped id-only snapshot loop. It is the poisoned-transaction guard; exception messages here are `file_id`-scoped and any `SecretStr` renders as `**********`, so no credential can leak. Noted for awareness only.

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-07-04
