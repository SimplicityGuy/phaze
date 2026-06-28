# SECURITY.md — Phase 53: S3 Object-Staging Leg

**Verdict:** SECURED
**Threats Closed:** 24/24 (T-53-01..23 + T-53-SC)
**ASVS Level:** L1
**block_on:** high — no high/blocker gaps found
**Audited:** 2026-06-27
**Method:** FORCE stance — each declared mitigation verified by grep/read against the cited implementation file. Documentation/intent not accepted as evidence.

> Single-user system: agents are trusted, interchangeable workers; cross-agent file access is BY DESIGN. The presign-download / callback / inline-delete routes intentionally carry NO per-agent ownership predicate (file_id is path-only, AUTH-01). Not treated as an IDOR/authz gap (prior automated scanner finding assessed N/A — a per-agent predicate would break the cross-agent hand-off this phase implements).

## Threat Verification (all `mitigate`)

| Threat ID | Category | Evidence (file:line) |
|-----------|----------|----------------------|
| T-53-01 | InfoDisclosure — S3 creds in config | `config.py:345-350` SECRET_FILE_FIELDS incl. s3_access_key_id/s3_secret_access_key (_FILE convention); `config.py:467-476` SecretStr types; all s3_ fields between 345-542 (ControlSettings 338-565), AgentSettings starts 566 — zero s3_ fields on AgentSettings |
| T-53-02 | Spoofing/SSRF — s3_endpoint_url | `config.py:508-524` `_validate_s3_endpoint_url` field_validator rejects scheme not in (http,https) and missing netloc (file://, scheme-less rejected) |
| T-53-03 | DoS — unbounded TTL/part-size | `config.py:479-506` gt=0/lt=86400 on both presign TTLs, gt=0/lt=30 on lifecycle days, ge=5242880/lt=5368709120 on part size (startup fail-fast) |
| T-53-04 / T-53-SC | Tampering/supply-chain | `pyproject.toml:15` aioboto3>=15.5.0 (pinned floor), `pyproject.toml:212` moto[server]>=5.1.0 (dev), `pyproject.toml:192` exclude-newer cooldown present |
| T-53-05 | EoP — presign scope+TTL | `s3_staging.py:58-64` file_id-scoped key; `s3_staging.py:176-191` presign_get uses s3_presign_get_ttl_sec (short); `s3_staging.py:108-125` presign_upload_parts bounded by s3_presign_put_ttl_sec |
| T-53-06 | InfoDisclosure — sha from body | `agent_files.py:179` expected_sha256=file.sha256_hash (server-side); `agent_files.py:137-142` route accepts no body |
| T-53-07 | Spoofing — unauth presign | `agent_files.py:140` `Depends(get_authenticated_agent)` on presign-download |
| T-53-08 | Tampering — file_id in body | `agent_files.py:139` file_id is path param; no body parameter on handler |
| T-53-09 | InfoDisclosure — creds in logs | aioboto3/aiobotocore/botocore imports confined to `s3_staging.py:25-27` ONLY (grep across src/ confirms); `s3_staging.py:87-88` get_secret_value, no logging of secret values in module |
| T-53-10 | DoS — orphaned objects | `s3_staging.py:213-234` ensure_bucket_lifecycle_ttl + `s3_staging.py:194-210` delete_staged_object |
| T-53-11 | EoP — SDK/creds on agent | `tests/test_task_split.py:85` (agent_worker graph) + `:171` (s3_upload task) forbidden tuple includes "aioboto3","botocore"; `s3_upload.py` imports only httpx/asyncio/pathlib/config/schemas |
| T-53-12 | DoS — runaway snippet/memory | `s3_upload.py:45` _BODY_SNIPPET_MAX=500 (applied :101); `s3_upload.py:94` one-chunk-at-a-time read (bounded memory); `s3_upload.py:50-58,126-128` inner<outer<SAQ timeout layering |
| T-53-13 | Tampering — malicious URL/path | `schemas/agent_s3.py:46` extra=forbid; `:54-60` original_path absolute validator; `:62-70` part_urls http(s) validator; `s3_upload.py:99` shell-free httpx PUT |
| T-53-14 | Spoofing — forged identity | `schemas/agent_s3.py:85-94` UploadedRequest carries only `parts`, no identity; extra=forbid; file_id on path (`agent_s3.py:60`) |
| T-53-15 | Tampering — duplicate callback | `agent_s3.py:86-97` rowcount-guarded UPLOADING→UPLOADED flip; `:76-78` pre-check idempotent 200 no re-complete |
| T-53-16 | DoS — runaway re-drive | `agent_s3.py:128-138` s3_upload_attempt ledger counter capped at push_max_attempts → terminal FAILED |
| T-53-17 | DoS — orphaned multipart/object | `agent_s3.py:142-143` terminal path abort_multipart_upload + delete_staged_object; lifecycle TTL backstop (s3_staging.py:213) |
| T-53-18 | Spoofing — identity in body | `agent_s3.py:60-62` file_id path + identity from token; `schemas/agent_s3.py:92` extra=forbid rejects identity in body |
| T-53-19 | Repudiation — unmounted/silent 500 | `main.py:28,211` agent_s3 imported + include_router; `agent_s3.py:165-169` NoActiveAgentError → clean 200 hold |
| T-53-20 | DoS — leak after analysis | `agent_analysis.py:231` (success) + `:263` (failure) inline delete; both before commit |
| T-53-21 | InfoDisclosure — delete error loses result | `agent_analysis.py:116-119` log-and-swallow (except Exception) AFTER result recorded (record-first); TTL backstop |
| T-53-22 | Tampering — delete on all-local | `agent_analysis.py:112-115` CloudJob existence guard → return before any s3_staging call (zero S3 calls, no client build) |
| T-53-23 | Spoofing — file_id from body | `agent_analysis.py:96` helper takes path file_id; call sites `:231/:263` pass the PATH file_id (handler params `:239` path-only) |

## Unregistered Flags

None. No `## Threat Flags` section is present in any of 53-01..05-SUMMARY.md, so the executor declared no new attack surface beyond the registered threats.

## Informational (non-blocking)

- `pyproject.toml:192` uses `exclude-newer = "7 days"` (relative). The cooldown directive required by T-53-04/SC is present and the dep floors are pinned, so the threat is CLOSED. Separately, project memory notes a relative value can break `uv lock` resolution (a build-resolution concern, not a security mitigation gap) — out of scope for this audit, flagged for awareness only.
