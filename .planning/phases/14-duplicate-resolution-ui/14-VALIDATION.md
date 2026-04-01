---
phase: 14
slug: duplicate-resolution-ui
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-31
---

# Phase 14 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.x |
| **Config file** | pyproject.toml |
| **Quick run command** | `uv run pytest tests/test_services/test_dedup.py tests/test_routers/test_duplicates.py -x -v` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing -x` |
| **Estimated runtime** | ~20 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_services/test_dedup.py tests/test_routers/test_duplicates.py -x -v`
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing -x`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 20 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 14-01-01 | 01 | 1 | DEDUP-04 | unit | `uv run pytest tests/test_services/test_dedup.py -x -k "score"` | ❌ W0 | ⬜ pending |
| 14-01-02 | 01 | 1 | DEDUP-01 | unit | `uv run pytest tests/test_services/test_dedup.py -x -k "enrich"` | ❌ W0 | ⬜ pending |
| 14-02-01 | 02 | 1 | DEDUP-01 | integration | `uv run pytest tests/test_routers/test_duplicates.py -x` | ❌ W0 | ⬜ pending |
| 14-02-02 | 02 | 1 | DEDUP-03 | integration | `uv run pytest tests/test_routers/test_duplicates.py -x -k "compare"` | ❌ W0 | ⬜ pending |
| 14-03-01 | 03 | 2 | DEDUP-02 | integration | `uv run pytest tests/test_routers/test_duplicates.py -x -k "resolve"` | ❌ W0 | ⬜ pending |
| 14-03-02 | 03 | 2 | DEDUP-02 | integration | `uv run pytest tests/test_routers/test_duplicates.py -x -k "bulk"` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_services/test_dedup.py` — extend with scoring and enriched group tests
- [ ] `tests/test_routers/test_duplicates.py` — new file for duplicates router tests

*Existing infrastructure covers test framework and fixtures.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Card layout renders correctly with comparison table | DEDUP-03 | Visual layout verification | Load /duplicates/, expand a group, verify comparison table columns and highlighting |
| Pre-selected canonical file has blue highlight | DEDUP-04 | Visual styling verification | Expand a group, verify pre-selected row has blue background and ring |
| Toast with undo appears on resolve | DEDUP-02 | Interaction timing verification | Click "Resolve Group", verify toast appears with 10-second undo window |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 20s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
