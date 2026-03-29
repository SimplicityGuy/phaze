---
phase: 7
slug: approval-workflow-ui
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-28
---

# Phase 7 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.x with pytest-asyncio, httpx AsyncClient |
| **Config file** | `pyproject.toml` (pytest section exists) |
| **Quick run command** | `uv run pytest tests/test_routers/test_proposals.py -x -q` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_routers/test_proposals.py -x -q`
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 07-01-01 | 01 | 1 | APR-01 | integration | `uv run pytest tests/test_routers/test_proposals.py::test_proposals_list` | ❌ W0 | ⬜ pending |
| 07-01-02 | 01 | 1 | APR-02 | integration | `uv run pytest tests/test_routers/test_proposals.py::test_approve_reject` | ❌ W0 | ⬜ pending |
| 07-01-03 | 01 | 1 | APR-03 | integration | `uv run pytest tests/test_routers/test_proposals.py::test_filter_by_status` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_routers/test_proposals.py` — stubs for APR-01, APR-02, APR-03
- [ ] `tests/conftest.py` — update with proposal factory fixtures

*Existing test infrastructure (pytest, httpx, conftest) covers framework needs.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| HTMX partial page updates | SC-4 | Visual behavior requires browser | Load page, click filter tab, verify table swaps without full reload |
| Keyboard shortcuts (a/r/e/arrows) | D-08 | Alpine.js behavior requires browser | Press arrow keys, verify row focus moves; press 'a' on focused row |
| Toast undo within 5 seconds | D-05 | Time-based visual interaction | Approve a proposal, verify toast appears, click undo within 5s |
| Responsive table layout | UI-SPEC | Visual layout verification | Open in browser, verify table renders correctly |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
