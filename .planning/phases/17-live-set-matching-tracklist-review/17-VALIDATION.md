---
phase: 17
slug: live-set-matching-tracklist-review
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-04-01
---

# Phase 17 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x |
| **Config file** | pyproject.toml |
| **Quick run command** | `uv run pytest tests/test_tasks/test_scan*.py tests/test_routers/test_tracklist*.py -x -q` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~20 seconds |

---

## Sampling Rate

- **After every task commit:** Run quick command
- **After every plan wave:** Run full suite
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 20 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 17-01-01 | 01 | 1 | FPRINT-03 | unit | `uv run pytest tests/test_tasks/test_scan.py -x -q` | ❌ W0 | ⬜ pending |
| 17-01-02 | 01 | 1 | FPRINT-03, FPRINT-04 | unit | `uv run pytest tests/test_models/test_tracklist.py -x -q` | ✅ | ⬜ pending |
| 17-02-01 | 02 | 2 | FPRINT-04 | unit | `uv run pytest tests/test_routers/test_tracklists.py -x -q` | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_tasks/test_scan.py` — stubs for scan task tests

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Scan tab file selection and batch trigger | FPRINT-03 | Visual HTMX interaction | Navigate to Tracklists > Scan tab, select files, trigger scan |
| Inline track editing (click-to-edit, save on blur) | FPRINT-04 | Visual HTMX interaction | Expand a fingerprint tracklist card, click artist/title, edit, verify save |
| Per-track confidence color badges | FPRINT-04 | Visual rendering | Expand fingerprint tracklist, verify green/yellow/red badges per track |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 20s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
