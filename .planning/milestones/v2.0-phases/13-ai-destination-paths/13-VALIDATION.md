---
phase: 13
slug: ai-destination-paths
status: complete
nyquist_compliant: true
wave_0_complete: true
created: 2026-03-31
---

# Phase 13 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/test_services/test_collision.py tests/test_routers/test_preview.py -x` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_services/test_collision.py tests/test_routers/test_preview.py -x`
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 13-01-01 | 01 | 1 | PATH-01 | unit | `uv run pytest tests/test_services/test_proposal.py -x -k "path"` | Partially | ⬜ pending |
| 13-02-01 | 02 | 1 | PATH-02 | integration | `uv run pytest tests/test_routers/test_proposals.py -x -k "destination"` | ❌ W0 | ⬜ pending |
| 13-03-01 | 03 | 2 | PATH-03 | unit + integration | `uv run pytest tests/test_services/test_collision.py -x` | ❌ W0 | ⬜ pending |
| 13-04-01 | 04 | 2 | PATH-04 | unit + integration | `uv run pytest tests/test_services/test_collision.py tests/test_routers/test_preview.py -x` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_services/test_collision.py` — stubs for PATH-03, PATH-04 (collision detection + tree builder)
- [ ] `tests/test_routers/test_preview.py` — stubs for PATH-04 (preview route rendering)
- [ ] Add path-related test cases to existing `tests/test_services/test_proposal.py` — covers PATH-01
- [ ] Add destination column assertions to existing `tests/test_routers/test_proposals.py` — covers PATH-02

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Visual tree rendering | PATH-04 | Visual layout, collapsible interaction | Open /preview, verify folders collapse/expand, file counts match |
| Collision warning display | PATH-03 | Visual badge rendering | Approve two files with same destination, verify orange warning badge |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
