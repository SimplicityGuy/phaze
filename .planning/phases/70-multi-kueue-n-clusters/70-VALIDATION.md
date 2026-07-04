---
phase: 70
slug: multi-kueue-n-clusters
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-07-04
---

# Phase 70 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio (`asyncio_mode = "auto"`); respx for the kr8s httpx seam; moto/fake S3 for aioboto3 |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]`, `testpaths = ["tests"]` |
| **Quick run command** | `uv run pytest tests/analyze/services/test_backends.py tests/analyze/services/test_kube_staging.py tests/analyze/services/test_s3_staging.py -x` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~30s quick / full suite per CLAUDE.md (85% min) |

---

## Sampling Rate

- **After every task commit:** Run the Quick run command (affected service tests, < 30s)
- **After every plan wave:** Run `uv run pytest tests/analyze tests/agents tests/discovery -q` (backends, kube/s3 staging, reconcile, agent routers)
- **Before `/gsd:verify-work`:** Full suite must be green + 85% coverage
- **Max feedback latency:** 30 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 70-01-* | 01 | 1 | MKUE-01 | T-70-01 | Distinct kr8s client per backend; token-hack retired; no shared-global auth mutation across N clusters | unit + seam | `uv run pytest tests/analyze/services/test_kube_staging.py -k "auth or client or multi" -x` | ✅ extend | ⬜ pending |
| 70-02-* | 02 | 1 | MKUE-02 | T-70-02 | Deterministic per-file bucket; presign/delete read recorded `staging_bucket`, never re-derive; objects never world-readable (presigned TTL only) | unit | `uv run pytest tests/analyze/services/test_s3_staging.py -k "pick_bucket or staging_bucket" -x` | ❌ W0 | ⬜ pending |
| 70-03-* | 03 | 2 | MKUE-03 | T-70-03 | One backend raising on snapshot/dispatch is isolated (0 slots, logged); healthy backends + local still get work | unit | `uv run pytest tests/analyze/tasks/ -k "stage_cloud_window and isolation" -x` | ❌ W0 | ⬜ pending |
| 70-04-* | 04 | 2 | MKUE-04 | T-70-04 | Clean-before-flip: old `(backend_id, staging_bucket)` object deleted BEFORE the `AWAITING_CLOUD` commit, under the per-row advisory lock; same-bucket re-dispatch never destroys the new owner's object; TTL is backstop only | unit + concurrency | `uv run pytest tests/analyze/tasks/test_reconcile_cloud_jobs.py -k "clean_before_flip or spillover" -x` | ❌ W0 | ⬜ pending |
| 70-04-* | 04 | 2 | MKUE-04 | — | migration 030 upgrade/downgrade round-trips; `staging_bucket` nullable, no backfill | integration | `uv run pytest tests/integration/test_migrations/ -k "030 or staging_bucket" -x` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/analyze/services/test_s3_staging.py` — add `pick_bucket` determinism + stability-across-restart + empty-set-raises cases; per-bucket `BucketConfig`-param cases for presign/delete (MKUE-02)
- [ ] `tests/analyze/services/test_kube_staging.py` — synthesized-kubeconfig-dict auth cases (both `kubeconfig+context` and `api_url+sa_token` forms) + distinct-client-per-backend; assert no `_create_session` usage (MKUE-01)
- [ ] `tests/analyze/tasks/test_release_awaiting_cloud*.py` — N≥2 backend fixture where one backend raises on `is_available`/`in_flight_count`/`dispatch`; assert the tick survives and healthy backends get work (MKUE-03)
- [ ] `tests/analyze/tasks/test_reconcile_cloud_jobs.py` — clean-before-flip ordering test + same-bucket re-dispatch preservation test + drain↔reconcile concurrency test (no file in two backends; no object the new pod needs is deleted) (MKUE-04 / Pitfall 9)
- [ ] `tests/integration/test_migrations/test_030_staging_bucket.py` — upgrade/downgrade round-trip (mirror the 029 migration test)
- [ ] Import-boundary guard: keep `s3_staging` and `kube_staging` ORM-free after parameterization (extend existing purity tests)

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live second-cluster kr8s auth over the mesh (private CA / TLS behavior per distinct kubeconfig/context) | MKUE-01 | No real 2nd Kueue cluster in-session; Phase-56-carryover live-E2E item | At rollout, declare a 2nd `KueueBackend`, run a real drain tick, confirm both clusters reachable + reconcile scoped by `backend_id` |
| End-to-end spillover cleanup against real S3 endpoints for two distinct buckets | MKUE-04 | Requires two live bucket endpoints + a real re-dispatch | At rollout, force a spillover across two buckets, confirm the old object is gone and the new pod's object survives |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 30s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
