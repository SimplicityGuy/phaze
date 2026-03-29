---
phase: 4
slug: task-queue-worker-infrastructure
status: complete
nyquist_compliant: true
wave_0_complete: true
created: 2026-03-28
audited: 2026-03-28
---

# Phase 4 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.2 with pytest-asyncio 1.3.0 |
| **Config file** | pyproject.toml |
| **Quick run command** | `uv run pytest tests/ -x -q` |
| **Full suite command** | `uv run pytest tests/ --cov --cov-report=term-missing` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/ -x -q`
- **After every plan wave:** Run `uv run pytest tests/ --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 20 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 04-01-01 | 01 | 1 | INF-02 | unit | `uv run pytest tests/test_tasks/ tests/test_config_worker.py -x -q` | ✅ | ✅ green |
| 04-01-02 | 01 | 1 | ANL-03 | unit | `uv run pytest tests/test_tasks/ -x -q` | ✅ | ✅ green |
| 04-02-01 | 02 | 2 | INF-02 | integration | `uv run pytest tests/test_phase04_gaps.py -x -q` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [x] `tests/test_tasks/` — test directory for worker/task tests
- [x] `tests/test_tasks/conftest.py` — not needed; fixtures in each test file
- [x] `tests/conftest.py` — no Redis fixtures needed (ASGITransport does not invoke lifespan; lifespan tested directly)

*Existing test infrastructure from prior phases covers framework setup.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Worker connects to Redis in Docker | INF-02 | Requires running Redis container | `docker compose up redis worker` and check logs |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 20s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** audited 2026-03-28 by gsd-nyquist-auditor
