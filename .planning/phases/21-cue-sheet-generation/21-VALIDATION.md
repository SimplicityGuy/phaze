---
phase: 21
slug: cue-sheet-generation
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-04-03
---

# Phase 21 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.x |
| **Config file** | pyproject.toml |
| **Quick run command** | `uv run pytest tests/test_cue_generator.py -x -q` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_cue_generator.py -x -q`
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 21-01-01 | 01 | 1 | CUE-01 | unit | `uv run pytest tests/test_cue_generator.py -x -q` | ❌ W0 | ⬜ pending |
| 21-01-02 | 01 | 1 | CUE-02 | unit | `uv run pytest tests/test_cue_generator.py -x -q` | ❌ W0 | ⬜ pending |
| 21-01-03 | 01 | 1 | CUE-03 | unit | `uv run pytest tests/test_cue_generator.py -x -q` | ❌ W0 | ⬜ pending |
| 21-02-01 | 02 | 2 | CUE-01 | integration | `uv run pytest tests/test_cue_router.py -x -q` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_cue_generator.py` — stubs for CUE-01, CUE-02, CUE-03
- [ ] `tests/test_cue_router.py` — stubs for CUE management page and tracklist inline action

*Existing conftest.py and test infrastructure covers shared fixtures.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| CUE file plays correctly in media player | CUE-01 | Requires audio playback software | Open generated .cue in foobar2000 or VLC, verify track seeking works |
| UTF-8 BOM visible in hex editor | CUE-02 | Encoding verification beyond unit test | `xxd file.cue | head -1` should show `efbb bf` BOM bytes |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
