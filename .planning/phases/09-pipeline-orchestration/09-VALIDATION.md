---
phase: 09
slug: pipeline-orchestration
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-30
---

# Phase 09 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x with pytest-asyncio |
| **Config file** | `pyproject.toml` |
| **Quick run command** | `uv run pytest tests/ -x -q --ignore=tests/test_routers` |
| **Full suite command** | `uv run pytest tests/ -x -q` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/ -x -q --ignore=tests/test_routers`
- **After every plan wave:** Run `uv run pytest tests/ -x -q`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 10 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 09-01-01 | 01 | 1 | ANL-01 | unit | `uv run pytest tests/test_routers/test_pipeline.py -x -q` | ❌ W0 | ⬜ pending |
| 09-01-02 | 01 | 1 | AIP-01 | unit | `uv run pytest tests/test_routers/test_pipeline.py -x -q` | ❌ W0 | ⬜ pending |
| 09-02-01 | 02 | 2 | ANL-01 | unit | `uv run pytest tests/test_tasks/test_session.py -x -q` | ❌ W0 | ⬜ pending |
| 09-03-01 | 03 | 3 | - | config | `docker compose config --quiet` | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_routers/test_pipeline.py` — stubs for pipeline trigger endpoints
- [ ] `tests/test_tasks/test_session.py` — stubs for shared session module

*Existing infrastructure covers test framework and fixtures.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Dashboard renders with pipeline stats | ANL-01 | Requires running Docker + PostgreSQL + browser | Visit /dashboard/, verify stage counts display |
| E2E pipeline flow | ANL-01, ANL-02, AIP-01 | Requires full Docker stack + real files + LLM key | POST /api/v1/scan, then /api/v1/analyze, then /api/v1/proposals/generate — verify files progress through states |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
