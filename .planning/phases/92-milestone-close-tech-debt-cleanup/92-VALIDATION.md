---
phase: 92
slug: milestone-close-tech-debt-cleanup
status: validated
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-13
validated: 2026-07-13
---

# Phase 92 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (via `uv run pytest`, pytest-asyncio) |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` + `tests/conftest.py` |
| **Quick run command** | `uv run pytest tests/<targeted>` (targeted per task) |
| **Full suite command** | `just test-bucket <bucket>` for every bucket in `tests/buckets.json` (per-bucket isolation — the CLEAN-02 acceptance surface, D-08) |
| **Estimated runtime** | ~3,411 tests across 9 buckets |

---

## Sampling Rate

- **After every task commit:** Run the targeted `uv run pytest` for the touched module/test.
- **After every plan wave:** Run affected bucket(s) via `just test-bucket <bucket>`.
- **Before `/gsd:verify-work`:** Full suite green under per-bucket isolation (D-08); both PERF-02 before/after latency numbers recorded in `92-VERIFICATION.md` (D-05); CLEAN-01/02/03 registered in REQUIREMENTS.md (DOCS-01 guard green).
- **Max feedback latency:** targeted run seconds; full per-bucket gate on wave close.

---

## Per-Task Verification Map

> Filled by the planner from PLAN.md tasks; statuses audited post-execution (2026-07-13). Each task maps to a candidate requirement (CLEAN-01..03) and an automated command.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 92-01-1 | 01 | 1 | CLEAN-03 | T-92-01-CMT | N/A (comment-only) | static/lint | `grep -c "thread THIS backend's KubeConfig" ...` + `uv run ruff check ...` | ✅ | ✅ green |
| 92-02-1 | 02 | 1 | CLEAN-01 | T-92-02-DoS | Perf-DB routing confirmed; before baseline | manual-bench | `PHAZE_DATABASE_URL=<perf> just perf-explain ITER=20` | ✅ (Phase 82) | ✅ green |
| 92-02-2 | 02 | 1 | CLEAN-01 | T-92-02-DoS/SKEW | Semaphore(4) + acquisition-degrade; fan-out seam patchable | integration | `uv run pytest tests/analyze/core/test_stage_progress.py tests/shared/routers/test_pipeline.py tests/integration/test_stage_progress_buckets.py -q` | ✅ | ✅ green |
| 92-02-3 | 02 | 1 | CLEAN-01 | T-92-02-SKEW | Before/after + DENORM-01 verdict (D-05) | manual-bench | `PHAZE_DATABASE_URL=<perf> just perf-explain ITER=20` → 92-VERIFICATION.md | ✅ | ✅ green |
| 92-03-1 | 03 | 2 | CLEAN-02 | T-92-03-ISO | create_savepoint isolation + verify fixture | infra | `uv run pytest tests/shared -q -k "conftest or dsn"` | ✅ (rewire) | ✅ green |
| 92-03-2 | 03 | 2 | CLEAN-02 | T-92-03-VIS | [BLOCKER-1 FIX] route get_stage_progress production fan-out → per-test connection (async_session monkeypatch + Semaphore(1)) | infra | `just test-bucket shared` (incl. seed-then-/pipeline/stats) | ✅ (conftest) | ✅ green |
| 92-03-3 | 03 | 2 | CLEAN-02 | T-92-03-ISO/VIS | Mutation-safe hermeticity proof: rollback + seed-then-get_stage_progress visibility (Wave 0) | infra | `uv run pytest tests/shared/test_conftest_hermeticity.py -q` | ✅ (created) | ✅ green |
| 92-04-1 | 04 | 3 | CLEAN-02 | T-92-04-VIS | analyze verify reads share outer-txn connection (9 files/12 sites) | infra | `just test-bucket analyze` | ✅ (migrated) | ✅ green |
| 92-04-2 | 04 | 3 | CLEAN-02 | T-92-04-VIS | review/agents/discovery migrated (4 files/9 sites) + integration exclusion grep | infra | `just test-bucket review agents discovery` | ✅ (migrated) | ✅ green |
| 92-05-1 | 05 | 4 | CLEAN-01/02/03 | — (bookkeeping) | Register CLEAN-01/02/03 in REQUIREMENTS.md; DOCS-01 guard green | static | `uv run pytest tests/shared/core/test_requirements_traceability.py -q` | ✅ | ✅ green |
| 92-05-2 | 05 | 4 | CLEAN-02 | T-92-05-GATE | [BLOCKING] D-08 full-suite per-bucket green | acceptance | `just test-bucket <all 9 buckets>` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

> **Post-execution audit note (2026-07-13):** CLEAN-02's blast radius exceeded the enumerated verify sites — the session-scoped-engine conversion (92-03) also broke 8 genuine cross-connection concurrency tests (relocated to `tests/integration/` on the new `committed_db` real-engine fixture, per the 92-04 Option-B checkpoint) and exposed 4 further hermeticity defects (`test_agents_add` committed-agent leak, `test_drain_double_dispatch`, `test_lifespan_orphan_task`, `test_stage_status_equivalence` ordering contamination) fixed in 92-05 via idempotent get-or-insert `test-fileserver` seeding. All are covered by the same D-08 per-bucket acceptance command (92-05-2).

---

## Wave 0 Requirements

- [x] **Fixture-contract test** (CLEAN-02) — `tests/shared/test_conftest_hermeticity.py`: mutation-safe, proves the create_savepoint fixture rolls back committed rows between tests (commit in test A, absent in test B) AND asserts seed-then-`get_stage_progress` visibility (guards the BLOCKER-1 production-fan-out regression). Mutation recipe exercised live (disabling the `async_session` patch flips it RED). (92-03 Task 3.)
- [x] **Production-fan-out routing** (CLEAN-01/02 boundary) — `_route_stats_fanout` in `tests/conftest.py` monkeypatches `phaze.database.async_session` onto the per-test `_db_connection` (create_savepoint) + sets `pipeline._STATS_FANOUT = Semaphore(1)`, so seed-then-read tests see in-test rows. Landed in 92-03 Task 2; the 8 previously-RED `test_stage_progress.py` cells (the wave-1→wave-2 collision) are now green. (92-03 Task 2.)
- [x] **Overlap/latency instrument** (CLEAN-01) — Phase-82 harness reused at 200K against the perf DSN. Recorded before/after in `92-VERIFICATION.md`: DIRECT p50 1468.9→860.6 ms, `GET /pipeline/stats` p50 1737.5→1072.2 ms → under budget → DENORM-01 killed/deferred (D-05).
- [x] Confirmed test-DB port (5433 via `just test-db`; Redis 6380) before running buckets in isolation.

*If none: "Existing infrastructure covers all phase requirements."*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions | Status |
|----------|-------------|------------|-------------------|--------|
| 200K-scale poll-latency win | CLEAN-01 | Requires seeded 200K synthetic corpus + dedicated perf DB; not part of the standard CI suite | Run the Phase-82 harness before/after; record both numbers in `92-VERIFICATION.md` (D-05) | ✅ executed & recorded (92-02) |

*If none: "All phase behaviors have automated verification."*

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (fixture-contract test + production-fan-out routing)
- [x] No watch-mode flags
- [x] Feedback latency acceptable
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** validated 2026-07-13 — all 11 task-map rows green; 0 automated gaps; the sole manual-only bench executed and recorded.

---

## Validation Audit 2026-07-13

| Metric | Count |
|--------|-------|
| Task-map rows | 11 |
| COVERED (green) | 11 |
| PARTIAL | 0 |
| MISSING (gaps) | 0 |
| Gaps resolved this audit | 0 |
| Escalated to manual-only | 0 (1 pre-existing manual bench, satisfied) |

**Result: NYQUIST-COMPLIANT.** Every phase requirement (CLEAN-01/02/03) has automated verification that runs green; the D-08 per-bucket acceptance gate (92-05-2) was independently re-run live by the verifier (3,411 tests, 0 failed across all 9 buckets). No test files generated — coverage was already complete. No implementation files modified.
