---
phase: 2
slug: file-discovery-ingestion
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-28
---

# Phase 2 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio |
| **Config file** | pyproject.toml [tool.pytest.ini_options] |
| **Quick run command** | `uv run pytest tests/ -x --no-header -q` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~10 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_services/test_ingestion.py tests/test_routers/test_scan.py -x --no-header -q`
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 02-01-01 | 01 | 0 | ING-01 | unit | `uv run pytest tests/test_services/test_ingestion.py::test_discover_files -x` | W0 | pending |
| 02-01-02 | 01 | 0 | ING-02 | unit | `uv run pytest tests/test_services/test_ingestion.py::test_compute_sha256 -x` | W0 | pending |
| 02-01-03 | 01 | 0 | ING-05 | unit | `uv run pytest tests/test_services/test_ingestion.py::test_classify_file -x` | W0 | pending |
| 02-02-01 | 02 | 1 | ING-03 | integration | `uv run pytest tests/test_services/test_ingestion.py::test_bulk_insert_stores_paths -x` | W0 | pending |
| 02-02-02 | 02 | 1 | -- | integration | `uv run pytest tests/test_routers/test_scan.py::test_trigger_scan -x` | W0 | pending |

*Status: pending / green / red / flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_services/__init__.py` — package init
- [ ] `tests/test_services/test_ingestion.py` — ingestion service tests
- [ ] `tests/test_routers/__init__.py` — package init
- [ ] `tests/test_routers/test_scan.py` — scan router tests

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Docker volume mount works | ING-01 | Requires Docker runtime + mounted dir | Mount a test dir, run `docker compose up`, trigger scan via API |

---

## Validation Sign-Off

- [ ] All tasks have automated verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
