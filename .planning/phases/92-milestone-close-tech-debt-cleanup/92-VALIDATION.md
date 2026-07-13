---
phase: 92
slug: milestone-close-tech-debt-cleanup
status: planned
nyquist_compliant: true
wave_0_complete: false
created: 2026-07-13
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
| **Estimated runtime** | ~full ~1750 tests across 9 buckets |

---

## Sampling Rate

- **After every task commit:** Run the targeted `uv run pytest` for the touched module/test.
- **After every plan wave:** Run affected bucket(s) via `just test-bucket <bucket>`.
- **Before `/gsd:verify-work`:** Full suite green under per-bucket isolation (D-08); both PERF-02 before/after latency numbers recorded in `92-VERIFICATION.md` (D-05).
- **Max feedback latency:** targeted run seconds; full per-bucket gate on wave close.

---

## Per-Task Verification Map

> Filled by the planner from PLAN.md tasks. Each task maps to a candidate requirement (CLEAN-01..03) and an automated command.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 92-01-1 | 01 | 1 | CLEAN-03 | T-92-01-CMT | N/A (comment-only) | static/lint | `grep -c "thread THIS backend's KubeConfig" ...` + `uv run ruff check ...` | ✅ | ⬜ pending |
| 92-02-1 | 02 | 1 | CLEAN-01 | T-92-02-DoS | Perf-DB routing confirmed; before baseline | manual-bench | `PHAZE_DATABASE_URL=<perf> just perf-explain ITER=20` | ✅ (Phase 82) | ⬜ pending |
| 92-02-2 | 02 | 1 | CLEAN-01 | T-92-02-DoS/SKEW | Semaphore(4) + acquisition-degrade | integration | `uv run pytest tests/analyze/core/test_stage_progress.py tests/shared/routers/test_pipeline.py tests/integration/test_stage_progress_buckets.py -q` | ✅ | ⬜ pending |
| 92-02-3 | 02 | 1 | CLEAN-01 | T-92-02-SKEW | Before/after + DENORM-01 verdict (D-05) | manual-bench | `PHAZE_DATABASE_URL=<perf> just perf-explain ITER=20` → 92-VERIFICATION.md | ✅ | ⬜ pending |
| 92-03-1 | 03 | 2 | CLEAN-02 | T-92-03-ISO | create_savepoint isolation + verify fixture | infra | `just test-bucket shared` | ✅ (rewire) | ⬜ pending |
| 92-03-2 | 03 | 2 | CLEAN-02 | T-92-03-ISO | Mutation-safe hermeticity proof (Wave 0) | infra | `uv run pytest tests/shared/test_conftest_hermeticity.py -q` | ❌ NEW (Wave 0) | ⬜ pending |
| 92-04-1 | 04 | 3 | CLEAN-02 | T-92-04-VIS | verify reads share outer-txn connection | infra | `just test-bucket analyze` | ✅ (migrate) | ⬜ pending |
| 92-04-2 | 04 | 3 | CLEAN-02 | T-92-04-VIS | review/agents/discovery migrated + integration exclusion grep | infra | `just test-bucket review agents discovery` | ✅ (migrate) | ⬜ pending |
| 92-05-1 | 05 | 4 | CLEAN-02 | T-92-05-GATE | [BLOCKING] D-08 full-suite per-bucket green | acceptance | `just test-bucket <all 9 buckets>` | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] **Fixture-contract test** (CLEAN-02) — a mutation-safe test that PROVES the new transactional
      fixture rolls back committed rows between tests (a green suite alone does not prove hermeticity
      for an intermittent flake — research landmine). Deliberately commit a row in test A, assert it is
      absent in test B.
- [ ] **Overlap/latency instrument** (CLEAN-01) — reuse `time_stage_progress()` + the Phase-82
      harness (`scripts/seed_perf_corpus.py`, `just perf-db-up / perf-seed / perf-explain`) to capture
      before/after `/pipeline/stats` poll latency at 200K scale. Confirm the bench points at the perf DSN
      (`PHAZE_DATABASE_URL`), not the app default (research Open Question 1).
- [ ] Confirm test-DB port (5432 vs 5433 footgun — MEMORY `reference_migrations_test_db_port`) before running buckets in isolation.

*If none: "Existing infrastructure covers all phase requirements."*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| 200K-scale poll-latency win | CLEAN-01 | Requires seeded 200K synthetic corpus + perf DB; not part of the standard CI suite | Run the Phase-82 harness before/after; record both numbers in `92-VERIFICATION.md` (D-05) |

*If none: "All phase behaviors have automated verification."*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency acceptable
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
