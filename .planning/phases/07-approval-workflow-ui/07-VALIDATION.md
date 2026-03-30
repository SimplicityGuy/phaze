---
phase: 7
slug: approval-workflow-ui
status: complete
nyquist_compliant: true
wave_0_complete: true
created: 2026-03-28
audited: 2026-03-29
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
| **Estimated runtime** | ~15 seconds (requires PostgreSQL) |

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
| 07-01-01 | 01 | 1 | APR-01 | integration | `uv run pytest tests/test_routers/test_proposals.py -k "list or empty_state or shows_proposals or htmx or pagination" -x` | ✅ | ✅ green |
| 07-01-02 | 01 | 1 | APR-02 | integration | `uv run pytest tests/test_routers/test_proposals.py -k "approve or reject or undo or bulk" -x` | ✅ | ✅ green |
| 07-01-03 | 01 | 1 | APR-03 | integration | `uv run pytest tests/test_routers/test_proposals.py -k "filter or search or sort" -x` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

*Note: Tests require a running PostgreSQL instance. All 20 tests pass in CI with database available.*

---

## Wave 0 Requirements

- [x] `tests/test_routers/test_proposals.py` — 20 integration tests covering APR-01, APR-02, APR-03
- [x] Test helper `create_test_proposal()` creates FileRecord + RenameProposal pairs
- [x] Tests use httpx AsyncClient with real database transactions

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

All 3 tasks have automated test coverage. 20 integration tests across 1 test file covering list, approve/reject/undo, filter/search/sort, bulk actions, pagination, HTMX fragments, and error cases. Tests require PostgreSQL (pass in CI).
