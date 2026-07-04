---
phase: 69
slug: tiered-drain-scheduler
status: approved
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-04
audited: 2026-07-04
---

# Phase 69 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Source: `69-RESEARCH.md` § Validation Architecture.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.1.1 + pytest-asyncio, `uv run` prefix mandatory (CLAUDE.md) |
| **Config file** | `pyproject.toml` (`[tool.pytest]`) + `tests/conftest.py` |
| **Quick run command** | `uv run pytest tests/analyze/services/test_backend_selection.py tests/analyze/core/test_staging_cron.py -x` |
| **Bucket run command** | `just test-bucket analyze` (drain/reconcile/backends live in the **analyze** bucket) |
| **Full suite command** | `just integration-test` (ephemeral PG 5433 + Redis 6380; baseline 2566 passed, ~96.9% cov) |
| **Estimated runtime** | ~15 s quick · ~bucket · full suite minutes |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/analyze/services/test_backend_selection.py tests/analyze/core/test_staging_cron.py -x`
- **After every plan wave:** Run `just test-bucket analyze`
- **Before `/gsd:verify-work`:** `just integration-test` full suite green (2566+ baseline, no test lost), 85% coverage floor
- **Max feedback latency:** ~15 seconds (quick), bucket per wave

---

## Per-Task Verification Map

> Requirement → test mapping from RESEARCH § "Phase Requirements → Test Map". Task IDs assigned by the planner; this table binds each SCHED requirement to its automated command and Wave-0 status. The planner MUST attach `<automated>` verify to each task and reconcile Task IDs here.

| Requirement | Behavior | Test Type | Automated Command | File Exists | Status |
|-------------|----------|-----------|-------------------|-------------|--------|
| SCHED-01 | Rank-first eligible per-candidate; full top rank spills to next rank | unit | `uv run pytest tests/analyze/services/test_backend_selection.py -x` | ✅ (13 tests) | ✅ green |
| SCHED-01 | Drain dispatches across N backends in one tick | integration | `uv run pytest tests/analyze/core/test_staging_cron.py -k "multi_backend or spill" -x` | ✅ (2 tests) | ✅ green |
| SCHED-02 | Overlapping drain ticks never overshoot per-backend cap | integration | `uv run pytest tests/analyze/core/test_staging_cron.py -k overshoot -x` | ✅ (1 test) | ✅ green |
| SCHED-02 | Reconcile decrement concurrent with drain snapshot stays cap-safe | integration | `uv run pytest tests/analyze/tasks/test_reconcile_cloud_jobs.py -k cap_safe -x` | ✅ (1 test) | ✅ green |
| SCHED-03 | Cloud-failed file returns to AWAITING_CLOUD (not ANALYSIS_FAILED) under ceiling | integration | `uv run pytest tests/analyze/tasks/test_reconcile_cloud_jobs.py -k spill_back -x` | ✅ (2 tests) | ✅ green |
| SCHED-03 | Attempt-exhausted file falls to local; no A↔B thrash | unit + integration | `uv run pytest tests/analyze/services/test_backend_selection.py -k attempt -x` | ✅ (2 tests) | ✅ green |
| SCHED-03 | Staleness: full→local gated by threshold; offline→local immediate | unit | `uv run pytest tests/analyze/services/test_backend_selection.py -k stale -x` | ✅ (2 tests) | ✅ green |
| SCHED-04 | Equal-rank tie-break by utilization then stable id | unit | `uv run pytest tests/analyze/services/test_backend_selection.py -k tiebreak -x` | ✅ (2 tests) | ✅ green |
| SCHED-05 | Compute file with in-flight cloud_job recovered by exactly one path | integration | `uv run pytest tests/analyze/tasks/test_recovery.py -k "single_owner or in_flight" -x` | ✅ (4 tests) | ✅ green |
| SCHED-05 | Reconcile is backend_id-scoped; compute rows untouched by kueue reconcile | integration | `uv run pytest tests/analyze/services/test_backends.py -k reconcile_scope -x` | ✅ (1 test) | ✅ green |
| — (guard) | No non-drain writer touches AWAITING_CLOUD rows (protects `updated_at` staleness signal) | integration | `uv run pytest tests/analyze/core/test_staging_cron.py -k awaiting_untouched -x` | ✅ (1 test) | ✅ green |
| SCHED-01/03 (CR-01 gap 69-05) | Locally-spilled file removed from candidate set; NOT re-dispatched to cloud when a slot frees next tick | integration | `uv run pytest tests/analyze/core/test_staging_cron.py -k local_spill_not_redispatched -x` | ✅ (1 test, 69-05) | ✅ green |
| SCHED-01/03 (CR-01 gap 69-05) | `LocalBackend.dispatch` flips to `LOCAL_ANALYZING` + excluded from `get_cloud_staging_candidates`; honest WR-01 return | unit | `uv run pytest tests/analyze/services/test_backends.py -k "local_analyzing or dispatch" -x` | ✅ (7 tests, 69-05) | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*
*All rows re-verified green during the phase-completion verifier run (146 phase-touched tests passed against the live `phaze-test-db`/`phaze-test-redis` containers) and re-confirmed by collection audit 2026-07-04.*

---

## Wave 0 Requirements

- [x] `tests/analyze/services/test_backend_selection.py` — new pure `select_backend` unit suite (rank-first, staleness full/offline, attempt-exclusion, tie-break) — covers SCHED-01/03/04 — **landed (13 tests, 69-01)**
- [x] `tests/analyze/core/test_staging_cron.py` — add multi-backend drain + per-backend overshoot + `awaiting_untouched` cases (extend existing) — **landed (69-02; +CR-01 two-tick re-dispatch case, 69-05)**
- [x] `tests/analyze/tasks/test_reconcile_cloud_jobs.py` — add cap-safe-under-concurrent-drain + spill-back-not-ANALYSIS_FAILED (modify existing at-cap assertion) — **landed (69-03)**
- [x] `tests/analyze/tasks/test_recovery.py` — single-owner assertion for a compute file with an in-flight cloud_job (extend) — **landed (69-04)**
- [x] Config field `cloud_spill_to_local_after_seconds` default/bounds test in `tests/shared/config/` (mirror `cloud_route_threshold_sec`) — **landed (`test_cloud_spill_to_local.py`, 4 tests, 69-01)**

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live multi-backend E2E (Kueue + compute + local running simultaneously) | SCHED-01..05 | No live Kueue cluster in CI; faked via `tests/kube_fakes`. Live E2E is deployment-gated (matches Phase-68 precedent). | Deferred to Phase 70 rollout; unit + integration fakes (`DedupFakeQueue`, `fake_local_queue`, `seed_active_agent`) cover the logic. |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (scaffolds authored inside the plans that create the code they cover)
- [x] No watch-mode flags
- [x] Feedback latency < 15s (quick)
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-07-04 (plan-checker: 0 blockers on nyquist coverage). `wave_0_complete` flipped to true — all Wave-0 scaffolds landed and every mapped requirement is green.

---

## Validation Audit 2026-07-04

Post-execution audit (State A). Every SCHED-01..05 requirement + the staleness guard + the CR-01 gap-closure (69-05) rows were cross-referenced against the test suite by collection audit; all selectors bind and all were re-verified green in the phase-completion verifier run (146 phase-touched tests passed on the live test DB).

| Metric | Count |
|--------|-------|
| Requirements/behaviors mapped | 13 |
| COVERED (green) | 13 |
| PARTIAL / MISSING | 0 |
| Gaps found | 0 |
| Tests generated this audit | 0 (all coverage already landed during execution + gap closure) |

**Manual-only carry-forward:** Live simultaneous multi-backend E2E (Kueue + compute + local) remains deployment-gated to Phase 70 rollout — covered in logic by unit + integration fakes, consistent with the Phase-68 precedent.

`nyquist_compliant: true` confirmed — no test generation required.
