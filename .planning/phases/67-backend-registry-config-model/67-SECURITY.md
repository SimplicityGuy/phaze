---
phase: 67
slug: backend-registry-config-model
status: verified
threats_open: 0
asvs_level: 2
created: 2026-07-04
---

# Phase 67 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.
> Verification method: each declared mitigation confirmed by locating the actual control in
> implemented code (not documentation/intent). Threat register authored at plan time across all
> 6 plans; this audit VERIFIED each mitigation against current `src/` — no scan for new threats.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| Operator → control plane | `backends.toml` + `PHAZE_BACKENDS_CONFIG_FILE` env pointer + inline `*_file` secret paths | Backend registry config, S3/kube credentials (SecretStr) |
| Control plane → S3 backend | `s3_staging.py` aioboto3 client built from `active_bucket` | Bucket creds, presigned URLs, endpoint_url (SSRF surface) |
| Control plane → Kueue cluster | `kube_staging.py` kr8s client built from `active_kube` | kubeconfig / SA token (SecretStr), Job manifests |
| Startup logs | `log_effective_registry` projection + probe WARNINGs | id/kind/rank/cap ONLY — never secrets/DSNs/mount paths |
| Agent → control internal API | `agent_s3.py` / `agent_push.py` token-authed callbacks | file_id (PATH), part etags; identity from token, not body |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation (verified location) | Status |
|-----------|----------|-----------|-------------|-------------------------------|--------|
| T-67-01-01 | Tampering | `BucketConfig.endpoint_url` SSRF | mitigate | `config_backends.py:205-217` `_validate_endpoint_url` field_validator rejects non-http(s) scheme / empty netloc | closed |
| T-67-01-02 | Info disclosure | resolved kube/S3 secrets | mitigate | `config_backends.py:156-157` (`kubeconfig`/`sa_token`), `:189-190` (`access_key_id`/`secret_access_key`) typed `SecretStr` | closed |
| T-67-01-03 | Tampering/Info disc. | inline `*_file` path read | mitigate | `config_backends.py:28-42` `_read_secret_file` raises `ValueError` naming path only (never file contents) on `OSError` | closed |
| T-67-01-04 | DoS | out-of-range rank/cap | mitigate | `config_backends.py:74-76,83-84,112-113` `rank: Field(ge=0, lt=1000)`, `cap: Field(gt=0, lt=1000)` on all 3 variants | closed |
| T-67-01-SC | Tampering | package installs | accept | Zero new deps — `git diff main` shows no `pyproject.toml`/`uv.lock` change | closed |
| T-67-02-01 | DoS | present-but-empty registry | mitigate | `config.py:430-431` `_validate_registry` fails fast on empty; `:405-407` absent-file → `_default_local_registry` implicit-local | closed |
| T-67-02-02 | Spoofing/Tampering | cluster-specific bucket, wrong cluster | mitigate | `config.py:443-450` cluster-specific bucket referenced by >1 kueue backend raises (D-09) | closed |
| T-67-02-03 | Info disclosure | secret leak via startup log | mitigate | `config.py:525-533` `log_effective_registry` projects `{id,kind,rank,cap}` only | closed |
| T-67-02-04 | Tampering | two config sources | mitigate | `config.py:382-383` `backends`/`buckets` have NO `validation_alias` (not env-exposed); `:385-415` sourced only from TOML | closed |
| T-67-02-SC | Tampering | package installs | accept | Zero new deps (confirmed) | closed |
| T-67-03-01 | EoP/scope creep | dispatch logic leaking | mitigate | grep: NO `Backend`-protocol/dispatch import in `pipeline.py` or `release_awaiting_cloud.py` (reads-only accessors) | closed |
| T-67-03-02 | DoS | staging cron no-op lost | mitigate | `release_awaiting_cloud.py:127-128` `if not cfg.cloud_enabled: return {"staged":0,"skipped":0}`; GATE 1/2 no-ops preserved `:149-169` | closed |
| T-67-03-03 | Tampering | template render break → 500 | mitigate | `pipeline.py:575` neutral `cloud_lane_kind` key; grep: NO `cloud_target` literal in `pipeline.py` or the 3 templates | closed |
| T-67-03-SC | Tampering | package installs | accept | Zero new deps (confirmed) | closed |
| T-67-04-01 | EoP/scope creep | Backend-protocol dispatch leaking | mitigate | `s3_staging.py:36` imports `BucketConfig` (config model), `kube_staging.py:36` `KubeConfig` — no dispatch protocol; reads `active_bucket`/`active_kube` | closed |
| T-67-04-02 | Spoofing/Tampering | wrong-cluster bucket reachability | mitigate | `s3_staging.py:68-83` `_staging_config` uses `active_bucket`; `config.py:506-523` resolves single kueue backend's bucket + D-09 | closed |
| T-67-04-03 | Info disclosure | S3/kube creds in logs | mitigate | `s3_staging.py:94-96` + `kube_staging.py:102-109` `.get_secret_value()` only at client construction; no secret logged | closed |
| T-67-04-04 | DoS | multi-target/bucket ambiguity, silent wrong pick | mitigate | `config.py:472-477` `_single_non_local` RAISES on >1 non-local; `:518-522` `active_bucket` RAISES on >1 bucket (never silently picks) | closed |
| T-67-04-SC | Tampering | package installs | accept | Zero new deps (confirmed) | closed |
| T-67-05-01 | DoS | kube/Redis blip aborting boot | mitigate | `controller.py:178-185` `active_cloud_kind` read wrapped in try/except (fix 7ce7fef); `:192-217` probe + flag persist each own try/except; regression test `test_multi_backend_registry_does_not_abort_boot` present | closed |
| T-67-05-02 | Info disclosure | secret in startup registry log | mitigate | `controller.py:79` secret-free projection; WARNINGs `:181-184,196-199` name only `PHAZE_KUBE_LOCAL_QUEUE`, never token/DSN | closed |
| T-67-05-03 | EoP/scope creep | Backend-protocol dispatch leaking | mitigate | `agent_s3.py:113` reads `active_cloud_kind`, `agent_push.py:122` reads `active_compute_scratch_dir` — no Backend type introduced | closed |
| T-67-05-SC | Tampering | package installs | accept | Zero new deps (confirmed) | closed |
| T-67-06-01 | Repudiation | dead flat vars silently ignored | mitigate | `.env.example:169-179` ">>> BREAKING REMOVAL IN 2026.7.1 <<<" callout enumerates every removed flat var | closed |
| T-67-06-02 | Tampering | D-15 knob / control-secret removed from SECRET_FILE_FIELDS | mitigate | `config.py:97` base set {database_url,redis_url,queue_url}; `:369-372` + openai/anthropic; D-15 knobs `:613-640` (s3 presign/lifecycle/part-size) + `:567,583,596` retained | closed |
| T-67-06-03 | DoS | missed flat-field reader breaks at runtime | mitigate | grep: NO `settings.s3_*`/`settings.kube_*`/`cloud_max_in_flight` readers remain in `src/`; REVIEW.md confirms mypy clean + no lingering flat refs | closed |
| T-67-06-SC | Tampering | package installs | accept | Zero new deps (confirmed) | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| SC-67 | T-67-0{1..6}-SC | Zero new runtime dependencies added — `git diff main -- pyproject.toml uv.lock` is empty. No new supply-chain surface. | phase plan | 2026-07-04 |

*Accepted risks do not resurface in future audit runs.*

---

## Informational — documented robustness deferral (NOT a threat gap)

**D-67-CR-01b** (`deferred-items.md`): A registry with >1 non-local backend is schema-valid
(multi-cluster is the milestone goal; D-09 exists to police it) but the ≤1-non-local transitional
accessors RAISE on read (single-selection dispatch lands in Phase 69). The **boot-fatal** case was
FIXED in this phase (T-67-05-01 — `controller.startup` read now guarded; regression test present,
commit 7ce7fef). The remaining unguarded reads (`pipeline.py` `build_dashboard_context` ~575 and
~810, `agent_s3.py` ~113, `release_awaiting_cloud.py` ~131/145/180) still raise a page/cron/route
error rather than degrading cleanly under that premature-multi-cluster config, and are deferred to
Phase 69 (SCHED).

This does NOT reopen any declared threat: the security-relevant mitigations for T-67-04-04 /
T-67-03-02 are "RAISE, never silently pick the wrong target" and "preserve the cloud_enabled no-op
early-return" — both verified present. The deferral is an availability/graceful-degradation
robustness item under an operator-misconfiguration-only path, explicitly tracked and NOT boot-fatal.

## Unregistered Flags

None. No `## Threat Flags` section present in any 67-*-SUMMARY.md; no new attack surface appeared
during implementation without a mapped threat ID.

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-07-04 | 27 | 27 | 0 | gsd-security-auditor |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-07-04
