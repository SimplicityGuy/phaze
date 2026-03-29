---
phase: 2
slug: file-discovery-ingestion
status: complete
nyquist_compliant: true
wave_0_complete: true
created: 2026-03-28
audited: 2026-03-28
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
| 02-01-01 | 01 | 0 | ING-01 | unit | `uv run pytest tests/test_services/test_ingestion.py::test_discover_files_recursive -x` | yes | green |
| 02-01-02 | 01 | 0 | ING-02 | unit | `uv run pytest tests/test_services/test_ingestion.py::test_compute_sha256_known_content -x` | yes | green |
| 02-01-03 | 01 | 0 | ING-05 | unit | `uv run pytest tests/test_services/test_ingestion.py::test_classify_file_music -x` | yes | green |
| 02-02-01 | 02 | 1 | ING-03 | integration | `uv run pytest tests/test_services/test_ingestion.py::test_bulk_upsert_stores_paths -x` | yes | green |
| 02-02-02 | 02 | 1 | -- | integration | `uv run pytest tests/test_routers/test_scan.py::test_trigger_scan_returns_batch_id -x` | yes | green |
| 02-GAP-01 | 02 | 1 | ING-01,ING-02,ING-03 | unit | `uv run pytest tests/test_phase02_gaps.py::test_run_scan_creates_batch_and_completes -x` | yes | green |
| 02-GAP-02 | 02 | 1 | ING-01,ING-02,ING-03 | unit | `uv run pytest tests/test_phase02_gaps.py::test_run_scan_marks_failed_on_exception -x` | yes | green |
| 02-GAP-03 | 01 | 0 | -- | unit | `uv run pytest tests/test_phase02_gaps.py::test_scan_batch_tablename -x` | yes | green |
| 02-GAP-04 | 01 | 0 | -- | unit | `uv run pytest tests/test_phase02_gaps.py::test_scan_status_has_three_values -x` | yes | green |

*Status: pending / green / red / flaky*

---

## Wave 0 Requirements

- [x] `tests/test_services/__init__.py` — package init
- [x] `tests/test_services/test_ingestion.py` — ingestion service tests (20 tests)
- [x] `tests/test_routers/__init__.py` — package init
- [x] `tests/test_routers/test_scan.py` — scan router tests (6 tests)
- [x] `tests/test_constants.py` — constants and enum tests (9 tests)
- [x] `tests/test_phase02_gaps.py` — gap-filling tests (10 tests, added by Nyquist audit)

---

## Coverage Summary (post-audit)

| Module | Coverage |
|--------|----------|
| constants.py | 100% |
| models/scan_batch.py | 100% |
| schemas/scan.py | 100% |
| services/ingestion.py | 100% |

**Total phase 2 tests: 45 passed**

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Docker volume mount works | ING-01 | Requires Docker runtime + mounted dir | Mount a test dir, run `docker compose up`, trigger scan via API |

---

## Validation Sign-Off

- [x] All tasks have automated verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 15s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** complete (Nyquist audit 2026-03-28)
