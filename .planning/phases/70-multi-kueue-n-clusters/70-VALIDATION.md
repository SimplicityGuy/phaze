---
phase: 70
slug: multi-kueue-n-clusters
status: verified
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-04
validated: 2026-07-04
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
| 70-01-* | 01 | 1 | MKUE-01 | T-70-01 | Distinct kr8s client per backend; token-hack retired; no shared-global auth mutation across N clusters | unit + seam | `uv run pytest tests/analyze/services/test_kube_staging.py -k "kubeconfig or api or distinct or token or bearer"` | ✅ | ✅ green (8) |
| 70-02-* | 02 | 1 | MKUE-02 | T-70-02 | Deterministic per-file bucket; presign/delete read recorded `staging_bucket`, never re-derive; objects never world-readable (presigned TTL only) | unit | `uv run pytest tests/analyze/services/test_s3_staging.py -k "pick_bucket or resolve_bucket_config or _acts_on_the_called_bucket"` | ✅ | ✅ green (9) |
| 70-03-* | 03 | 2 (exec W4) | MKUE-03 | T-70-03 | One backend raising on snapshot/dispatch is isolated (0 slots, logged); healthy backends + local still get work | unit | `uv run pytest tests/analyze/tasks/test_release_awaiting_cloud.py -k "isolation or unexpected_error"` | ✅ | ✅ green (5) |
| 70-04-* | 04 | 2 (exec W4) | MKUE-04 | T-70-04 | Clean-before-flip: old `(backend_id, staging_bucket)` object deleted BEFORE the `AWAITING_CLOUD` commit, under the per-row advisory lock; same-bucket re-dispatch never destroys the new owner's object; TTL is backstop only | unit + concurrency | `uv run pytest tests/analyze/tasks/test_reconcile_cloud_jobs.py -k "clean_before_flip or spill or delete_after_record or concurrency"` | ✅ | ✅ green (8) |
| 70-04-* | 04 | 2 (exec W4) | MKUE-04 | — | migration 030 upgrade/downgrade round-trips; `staging_bucket` nullable, no backfill; never references saq_jobs | integration | `uv run pytest tests/integration/test_migrations/test_migration_030_staging_bucket.py` | ✅ | ✅ green (3) |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

> **Note:** the plan-time `-k` filters were keyword guesses that deselected all tests; the commands above are corrected to the real test names. Actual wave layout differed from the plan-time estimate (MKUE-03/04 landed in exec Wave 4 as plans 70-04/70-05, not Wave 2). All coverage verified green 2026-07-04 against the ephemeral Postgres test DB (localhost:5433).

---

## Wave 0 Requirements

- [x] `tests/analyze/services/test_s3_staging.py` — `pick_bucket` determinism (`test_pick_bucket_matches_stable_sha256_formula_not_salted_hash`) + order-independence + empty-set-raises + always-member; per-bucket `resolve_bucket_config` + `_acts_on_the_called_bucket` presign/delete cases (MKUE-02) ✅
- [x] `tests/analyze/services/test_kube_staging.py` — synthesized-kubeconfig-dict auth cases (both `kubeconfig+context` via `test_kubeconfig_dict_from_parses_inline_kubeconfig_yaml`/`test_api_passes_dict_kubeconfig_and_context` and `api_url+sa_token` via `test_kubeconfig_dict_from_synthesizes_from_api_url_and_token`/`test_sa_token_applied_as_bearer`) + distinct-client-per-backend (`test_distinct_kubeconfigs_yield_distinct_clients`) + no token-hack (`test_source_has_no_token_hack`) (MKUE-01) ✅
- [x] `tests/analyze/tasks/test_release_awaiting_cloud.py` — one backend raises on `is_available`/`in_flight_count`/`dispatch` and the tick survives + healthy backends get work (`test_stage_cloud_window_isolation_*`), plus the CR-02 poisoned-txn guard `test_stage_cloud_window_unexpected_error_rolls_back_and_never_raises` (MKUE-03) ✅
- [x] `tests/analyze/tasks/test_reconcile_cloud_jobs.py` — clean-before-flip ordering (`test_clean_before_flip_ordering_delete_precedes_commit_precedes_job`) + same-bucket re-dispatch preservation (`test_spillover_same_bucket_redispatch_preserves_new_object`) + drain↔reconcile concurrency under the advisory lock (`test_drain_reconcile_concurrency_delete_runs_under_advisory_lock`) + best-effort delete (MKUE-04 / Pitfall 9) ✅
- [x] `tests/integration/test_migrations/test_migration_030_staging_bucket.py` — upgrade/downgrade round-trip + `test_migration_never_references_saq_jobs` (mirror the 029 migration test) ✅
- [x] Import-boundary guard: `s3_staging` and `kube_staging` stay ORM-free (`test_kube_staging.py:480` "NO sqlalchemy / phaze.models imports"; s3_staging purity retained) ✅

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live second-cluster kr8s auth over the mesh (private CA / TLS behavior per distinct kubeconfig/context) | MKUE-01 | No real 2nd Kueue cluster in-session; Phase-56-carryover live-E2E item | At rollout, declare a 2nd `KueueBackend`, run a real drain tick, confirm both clusters reachable + reconcile scoped by `backend_id` |
| End-to-end spillover cleanup against real S3 endpoints for two distinct buckets | MKUE-04 | Requires two live bucket endpoints + a real re-dispatch | At rollout, force a spillover across two buckets, confirm the old object is gone and the new pod's object survives |

---

## Validation Audit 2026-07-04

| Metric | Count |
|--------|-------|
| Requirements audited | 5 (MKUE-01..04 + migration 030) |
| Covered (green) | 5 |
| Partial | 0 |
| Missing | 0 |
| Gaps found | 0 |
| Resolved | 0 (no gaps — all coverage authored TDD in-phase) |
| Escalated to manual-only | 2 (pre-existing: live 2nd-cluster kr8s auth, real-S3 cross-bucket spillover — deployment-gated) |

State A audit: every plan-time Per-Task Map row resolves to real, green tests (33 targeted tests: 8+9+5+8+3). No gsd-nyquist-auditor spawn needed — zero gaps. The only guessed `-k` filters were corrected to real test names. Manual-only rows remain deployment-gated (no 2nd live Kueue cluster / dual live buckets in-session), consistent with the Phase-56/68/69 live-E2E carryover.

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (all Wave 0 items shipped + green)
- [x] No watch-mode flags
- [x] Feedback latency < 30s (targeted per-requirement runs ≤ 3s)
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** verified 2026-07-04 (2 deployment-gated manual-only items remain, tracked)
