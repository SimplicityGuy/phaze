---
phase: 3
slug: companion-files-deduplication
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-27
---

# Phase 3 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x |
| **Config file** | pyproject.toml |
| **Quick run command** | `uv run pytest tests/ -x -q` |
| **Full suite command** | `uv run pytest tests/ --cov --cov-report=term-missing` |
| **Estimated runtime** | ~10 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/ -x -q`
- **After every plan wave:** Run `uv run pytest tests/ --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 03-01-01 | 01 | 1 | ING-06 | unit | `uv run pytest tests/test_companion.py -x -q` | ❌ W0 | ⬜ pending |
| 03-01-02 | 01 | 1 | ING-06 | integration | `uv run pytest tests/test_companion.py -x -q` | ❌ W0 | ⬜ pending |
| 03-02-01 | 02 | 1 | ING-04 | unit | `uv run pytest tests/test_dedup.py -x -q` | ❌ W0 | ⬜ pending |
| 03-02-02 | 02 | 1 | ING-04 | integration | `uv run pytest tests/test_dedup.py -x -q` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_companion.py` — stubs for ING-06 (companion association)
- [ ] `tests/test_dedup.py` — stubs for ING-04 (duplicate detection)
- [ ] `tests/conftest.py` — shared fixtures (exists from Phase 2, may need extension)

*Existing test infrastructure from Phase 2 covers framework setup.*

---

## Manual-Only Verifications

*All phase behaviors have automated verification.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
