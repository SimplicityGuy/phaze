---
phase: 8
slug: safe-file-execution-audit
status: complete
nyquist_compliant: true
wave_0_complete: true
created: 2026-03-29
audited: 2026-03-29
---

# Phase 8 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio |
| **Config file** | pyproject.toml `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/test_services/test_execution.py tests/test_tasks/test_execution.py -x` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~15 seconds (router tests require PostgreSQL) |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_services/test_execution.py tests/test_tasks/test_execution.py -x`
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 08-01-01 | 01 | 1 | EXE-01 | unit | `uv run pytest tests/test_services/test_execution.py::test_copy_verify_delete_success -x` | ✅ | ✅ green |
| 08-01-02 | 01 | 1 | EXE-01 | unit | `uv run pytest tests/test_services/test_execution.py::test_compute_sha256 -x` | ✅ | ✅ green |
| 08-01-03 | 01 | 1 | EXE-01 | unit | `uv run pytest tests/test_services/test_execution.py::test_hash_mismatch_cleanup -x` | ✅ | ✅ green |
| 08-01-04 | 01 | 1 | EXE-01 | unit | `uv run pytest tests/test_services/test_execution.py::test_destination_exists -x` | ✅ | ✅ green |
| 08-01-05 | 01 | 1 | EXE-01 | unit | `uv run pytest tests/test_services/test_execution.py::test_file_record_updated -x` | ✅ | ✅ green |
| 08-02-01 | 02 | 1 | EXE-02 | unit | `uv run pytest tests/test_services/test_execution.py::test_audit_log_created_before_operation -x` | ✅ | ✅ green |
| 08-02-02 | 02 | 1 | EXE-02 | unit | `uv run pytest tests/test_services/test_execution.py::test_log_operation_and_complete_operation -x` | ✅ | ✅ green |
| 08-02-03 | 02 | 2 | EXE-02 | integration | `uv run pytest tests/test_routers/test_execution.py -k "audit_log" -x` | ✅ | ✅ green |
| 08-03-01 | 03 | 2 | D-08 | integration | `uv run pytest tests/test_routers/test_execution.py::test_sse_progress -x` | ✅ | ✅ green |
| 08-03-02 | 03 | 2 | D-02 | integration | `uv run pytest tests/test_routers/test_execution.py::test_execute_approved -x` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

*Note: Service/task tests (15) run without external deps. Router tests (8) require PostgreSQL — pass in CI.*

---

## Wave 0 Requirements

- [x] `tests/test_services/test_execution.py` — 10 unit tests for EXE-01, EXE-02 service logic
- [x] `tests/test_tasks/test_execution.py` — 5 unit tests for arq batch job and Redis progress
- [x] `tests/test_routers/test_execution.py` — 8 integration tests for execution endpoints, SSE, audit page

*Existing infrastructure covers test framework and fixtures.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| SSE live progress in browser | D-08 | Requires browser with SSE connection | Open approval UI, click Execute, verify live counter updates in real-time |
| Execute button disabled during active execution | D-01 pitfall | HTMX button state depends on browser rendering | Click Execute, verify button is disabled/grayed during execution |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 15s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved

---

## Validation Audit 2026-03-29

| Metric | Count |
|--------|-------|
| Gaps found | 0 |
| Resolved | 0 |
| Escalated | 0 |

All 10 tasks have automated test coverage. 23 tests across 3 files (15 unit + 8 integration). No gaps detected.
