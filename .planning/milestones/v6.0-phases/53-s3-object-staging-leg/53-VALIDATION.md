---
phase: 53
slug: s3-object-staging-leg
status: complete
nyquist_compliant: true
wave_0_complete: true
created: 2026-06-28
---

# Phase 53 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Reconstructed retroactively (State B) from the 5 plan/summary artifacts after execution. Every KSTAGE requirement maps to a dedicated, green automated test in the project suite.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x + pytest-asyncio (async), moto[server] (`ThreadedMotoServer`) for S3 round-trips |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| **Quick run command** | `just test-db` once, then `TEST_DATABASE_URL=… MIGRATIONS_TEST_DATABASE_URL=… PHAZE_REDIS_URL=… uv run pytest <file>` (env per `justfile` `integration-test` recipe) |
| **Full suite command** | `just integration-test` (spins ephemeral Postgres:5433 + Redis:6380, runs pytest, tears down) |
| **Estimated runtime** | ~315 seconds (full suite, 2352 tests) |

> NOTE: a bare `uv run pytest` fails with connection errors to port 5432 — the integration suite requires the ephemeral test DB/Redis on 5433/6380.

---

## Sampling Rate

- **After every task commit:** Run the affected test file via the `just test-db` + env path.
- **After every plan wave:** Run `just integration-test` (full suite).
- **Before `/gsd:verify-work`:** Full suite must be green.
- **Max feedback latency:** ~315 seconds (full suite); single-file runs are seconds.

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 53-01-01 | 01 | 1 | KSTAGE-02, KSTAGE-05 | T-53-01/02/03 | S3 config on ControlSettings only; `_FILE`+SecretStr creds; SSRF endpoint validator; bounded int fields | unit | `pytest tests/test_config/test_s3_settings.py` | ✅ | ✅ green |
| 53-01-02 | 01 | 1 | KSTAGE-04 | T-53-SC | `CloudJob` per-`file_id` sidecar + reversible migration 025 | unit/integration | `pytest tests/test_models/test_cloud_job.py tests/test_migrations/test_migration_025_cloud_job.py` | ✅ | ✅ green |
| 53-02-01 | 02 | 2 | KSTAGE-01 | T-53-05/09/10 | aioboto3 `s3_staging` ops (multipart/presign/complete/abort/delete/lifecycle); SDK confined here | unit | `pytest tests/test_services/test_s3_staging.py` | ✅ | ✅ green |
| 53-02-02 | 02 | 2 | KSTAGE-03 | T-53-06/07/08 | just-in-time presigned GET route; auth-gated; sha from FileRecord; path-only file_id; readiness guard | integration | `pytest tests/test_routers/test_agent_presign_download.py` | ✅ | ✅ green |
| 53-03-01 | 03 | 2 | KSTAGE-02 | T-53-13/14 | `agent_s3` schemas (`extra="forbid"`, validators, no identity) + agent_client callbacks | unit | `pytest tests/test_schemas/test_agent_s3.py tests/test_services/test_agent_client_upload.py` | ✅ | ✅ green |
| 53-03-02 | 03 | 2 | KSTAGE-02 | T-53-11/12 | `s3_upload` httpx-PUT task (no SDK/creds, bounded memory/snippet/timeout); DIST-01 import ban | unit | `pytest tests/test_tasks/test_s3_upload.py tests/test_task_split.py` | ✅ | ✅ green |
| 53-04-01 | 04 | 3 | KSTAGE-01 | T-53-16/17 | `cloud_staging` producer (multipart init + presign + idempotent cloud_job upsert + enqueue); re-drive cap | unit | `pytest tests/test_services/test_cloud_staging.py` | ✅ | ✅ green |
| 53-04-02 | 04 | 3 | KSTAGE-01, KSTAGE-04 | T-53-15/17/18/19 | `agent_s3` callbacks (rowcount-guarded flip, terminal abort+delete, identity rejection, route mounted) | integration | `pytest tests/test_routers/test_agent_s3.py` | ✅ | ✅ green |
| 53-05-01 | 05 | 3 | KSTAGE-04 | T-53-20/21/22/23 | inline staged-object delete on success AND failure paths; all-local guard; record-first; path-only file_id | integration | `pytest tests/test_routers/test_agent_analysis_inline_delete.py` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

Existing infrastructure covers all phase requirements. No new framework/config installs were needed — phase 53 reused the project pytest + `just integration-test` harness and added `moto[server]` (dev) for aioboto3 round-trip tests.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live S3-compatible backend round-trip (real MinIO/S3, not moto) | KSTAGE-05 | moto models the API but a real endpoint exercises actual creds/addressing/lifecycle behavior | Deploy with operator S3 config (`S3_ENDPOINT_URL`, bucket, `_FILE` creds), stage a real file, confirm multipart upload + presigned GET + inline delete + lifecycle TTL against the live bucket |

> The producer (`cloud_staging.stage_file_to_s3`) is built + unit-tested but intentionally NOT wired into the live routing seam (Phase 55 owns that), so end-to-end live staging is deployment-/Phase-55-gated.

---

## Validation Sign-Off

- [x] All tasks have automated verify coverage (no Wave 0 dependencies outstanding)
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (none — all COVERED)
- [x] No watch-mode flags
- [x] Feedback latency acceptable (~315s full suite; per-file runs in seconds)
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-06-28
