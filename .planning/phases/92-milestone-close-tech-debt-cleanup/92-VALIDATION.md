---
phase: 92
slug: milestone-close-tech-debt-cleanup
status: draft
nyquist_compliant: false
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
| 92-XX-XX | XX | X | CLEAN-0X | — | N/A (behavior-preserving cleanup) | unit/integration | `uv run pytest ...` | ⬜ per plan | ⬜ pending |

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
