---
phase: 10
slug: ci-config-bug-fixes
status: approved
nyquist_compliant: true
wave_0_complete: true
created: 2026-03-30
---

# Phase 10 — Validation Strategy

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
| 10-01-01 | 01 | 1 | INF-03 | unit | `uv run pytest tests/test_models.py::test_file_record_has_batch_id -x -v` | yes | green |
| 10-01-01 | 01 | 1 | INF-03 | integration | `uv run pytest tests/test_services/test_ingestion.py -x -q` | yes | green |
| 10-01-01 | 01 | 1 | INF-03 | full | `uv run pytest tests/ -x -q` | yes | green |

*Status: pending / green / red / flaky*

---

## Wave 0 Requirements

- [x] `tests/test_models.py` — existing model tests covering FileRecord columns
- [x] `tests/test_services/test_ingestion.py` — existing integration tests for bulk upsert

*Existing infrastructure covers test framework and fixtures.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| `pre-commit run --all-files` passes | INF-03 | Requires local environment with all pre-commit hooks installed | Run `pre-commit run --all-files` and verify zero failures |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 10s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved
