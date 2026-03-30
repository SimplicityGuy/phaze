---
phase: 8
slug: safe-file-execution-audit
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-29
---

# Phase 8 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio |
| **Config file** | pyproject.toml `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/test_services/test_execution.py tests/test_routers/test_execution.py -x` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_services/test_execution.py tests/test_routers/test_execution.py -x`
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 08-01-01 | 01 | 1 | EXE-01 | unit | `uv run pytest tests/test_services/test_execution.py::test_copy_verify_delete_success -x` | ❌ W0 | ⬜ pending |
| 08-01-02 | 01 | 1 | EXE-01 | unit | `uv run pytest tests/test_services/test_execution.py::test_hash_verification -x` | ❌ W0 | ⬜ pending |
| 08-01-03 | 01 | 1 | EXE-01 | unit | `uv run pytest tests/test_services/test_execution.py::test_hash_mismatch_cleanup -x` | ❌ W0 | ⬜ pending |
| 08-01-04 | 01 | 1 | EXE-01 | unit | `uv run pytest tests/test_services/test_execution.py::test_destination_exists -x` | ❌ W0 | ⬜ pending |
| 08-01-05 | 01 | 1 | EXE-01 | unit | `uv run pytest tests/test_services/test_execution.py::test_file_record_updated -x` | ❌ W0 | ⬜ pending |
| 08-02-01 | 02 | 1 | EXE-02 | unit | `uv run pytest tests/test_services/test_execution.py::test_audit_log_created -x` | ❌ W0 | ⬜ pending |
| 08-02-02 | 02 | 1 | EXE-02 | unit | `uv run pytest tests/test_services/test_execution.py::test_audit_log_status_update -x` | ❌ W0 | ⬜ pending |
| 08-02-03 | 02 | 2 | EXE-02 | integration | `uv run pytest tests/test_routers/test_execution.py::test_audit_log_page -x` | ❌ W0 | ⬜ pending |
| 08-03-01 | 03 | 2 | D-08 | integration | `uv run pytest tests/test_routers/test_execution.py::test_sse_progress -x` | ❌ W0 | ⬜ pending |
| 08-03-02 | 03 | 2 | D-02 | integration | `uv run pytest tests/test_routers/test_execution.py::test_execute_approved -x` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_services/test_execution.py` — stubs for EXE-01, EXE-02 service logic
- [ ] `tests/test_routers/test_execution.py` — stubs for execution endpoints, SSE, audit page
- [ ] `tests/test_tasks/test_execution.py` — stubs for arq batch job function

*Existing infrastructure covers test framework and fixtures.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| SSE live progress in browser | D-08 | Requires browser with SSE connection | Open approval UI, click Execute, verify live counter updates in real-time |
| Execute button disabled during active execution | D-01 pitfall | HTMX button state depends on browser rendering | Click Execute, verify button is disabled/grayed during execution |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
