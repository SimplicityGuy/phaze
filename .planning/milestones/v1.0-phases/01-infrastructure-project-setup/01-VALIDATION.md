---
phase: 1
slug: infrastructure-project-setup
status: complete
nyquist_compliant: true
wave_0_complete: true
created: 2026-03-27
audited: 2026-03-28
---

# Phase 1 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.2 + pytest-asyncio 1.3.0 |
| **Config file** | pyproject.toml (`asyncio_mode = "auto"`) |
| **Quick run command** | `uv run pytest tests/ -x -q` |
| **Full suite command** | `uv run pytest --cov=phaze --cov-report=term-missing` |
| **Estimated runtime** | ~5 seconds (non-DB), ~30 seconds (with DB) |

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
| 01-01-01 | 01 | 0 | INF-01 | unit | `uv run pytest tests/test_health.py -x` | ✅ | ✅ green |
| 01-01-02 | 01 | 0 | INF-03 | unit | `uv run pytest tests/test_models.py -x` | ✅ | ✅ green |
| 01-02-01 | 02 | 1 | INF-01 | integration | `docker compose up -d && docker compose ps` | N/A manual | ⬜ manual-only |
| 01-02-02 | 02 | 1 | INF-03 | integration | `uv run alembic upgrade head` | N/A manual | ⬜ manual-only |
| gap-01 | 01/02 | audit | INF-01/INF-03 | unit | `uv run pytest tests/test_phase01_gaps.py -x` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Gap-Fill Audit (2026-03-28)

Gaps identified and resolved by nyquist-auditor:

| # | Gap | Test Added | Result |
|---|-----|------------|--------|
| 1 | Core Settings defaults (database_url, redis_url, debug, api_port) not tested | `test_settings_database_url_default`, `test_settings_redis_url_default`, `test_settings_debug_default_is_false`, `test_settings_api_port_default`, `test_settings_openai_api_key_default_is_none` | green |
| 2 | App factory structure (title, version, /health route) not tested | `test_create_app_returns_fastapi_instance`, `test_create_app_title_is_phaze`, `test_create_app_has_health_route` | green |
| 3 | `get_session` session factory not unit-tested | `test_get_session_is_async_generator_function` | green |
| 4 | Alembic initial migration structural verification missing | `test_initial_migration_creates_five_tables`, `test_initial_migration_has_downgrade`, `test_initial_migration_has_down_revision_none` | green |

**Test file:** `tests/test_phase01_gaps.py` (12 tests, all green)

---

## Wave 0 Requirements

- [x] `pyproject.toml` — project configuration with all tool settings
- [x] `.pre-commit-config.yaml` — pre-commit hook configuration
- [x] `tests/conftest.py` — async database fixtures, test client
- [x] `tests/test_health.py` — health endpoint test
- [x] `tests/test_models.py` — verify model definitions and table creation
- [x] pytest-asyncio configuration in pyproject.toml (`asyncio_mode = "auto"`)

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Docker Compose starts all services | INF-01 | Requires Docker runtime | Run `docker compose up -d`, verify all containers healthy via `docker compose ps` |
| Alembic `upgrade head` applies cleanly | INF-03 | Requires running PostgreSQL | Run `docker compose up -d postgres && uv run alembic upgrade head` |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 10s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** complete — all automated gaps resolved 2026-03-28
