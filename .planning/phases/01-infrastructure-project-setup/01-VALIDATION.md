---
phase: 1
slug: infrastructure-project-setup
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-27
---

# Phase 1 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.2 + pytest-asyncio 1.3.0 |
| **Config file** | pyproject.toml (to be created in Wave 0) |
| **Quick run command** | `uv run pytest tests/ -x -q` |
| **Full suite command** | `uv run pytest --cov=phaze --cov-report=term-missing` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/ -x -q`
- **After every plan wave:** Run `uv run pytest --cov=phaze --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 10 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 01-01-01 | 01 | 0 | INF-01 | unit | `uv run pytest tests/test_health.py -x` | ❌ W0 | ⬜ pending |
| 01-01-02 | 01 | 0 | INF-03 | unit | `uv run pytest tests/test_models.py -x` | ❌ W0 | ⬜ pending |
| 01-02-01 | 02 | 1 | INF-01 | integration | `docker compose up -d && docker compose ps` | N/A manual | ⬜ pending |
| 01-02-02 | 02 | 1 | INF-03 | integration | `uv run alembic upgrade head` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `pyproject.toml` — project configuration with all tool settings
- [ ] `.pre-commit-config.yaml` — pre-commit hook configuration
- [ ] `tests/conftest.py` — async database fixtures, test client
- [ ] `tests/test_health.py` — health endpoint test
- [ ] `tests/test_models.py` — verify model definitions and table creation
- [ ] pytest-asyncio configuration in pyproject.toml (`asyncio_mode = "auto"`)

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Docker Compose starts all services | INF-01 | Requires Docker runtime | Run `docker compose up -d`, verify all containers healthy via `docker compose ps` |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
