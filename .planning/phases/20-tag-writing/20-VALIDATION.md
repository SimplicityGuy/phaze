---
phase: 20
slug: tag-writing
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-04-03
---

# Phase 20 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio |
| **Config file** | `pyproject.toml` [tool.pytest.ini_options] |
| **Quick run command** | `uv run pytest tests/test_services/test_tag_writer.py tests/test_services/test_tag_proposal.py -x` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~30 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_services/test_tag_writer.py tests/test_services/test_tag_proposal.py tests/test_routers/test_tags.py -x`
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 30 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 20-01-01 | 01 | 1 | TAGW-01, TAGW-03 | unit | `uv run pytest tests/test_models/test_tag_write_log.py tests/test_services/test_tag_writer.py -x` | ❌ W0 | ⬜ pending |
| 20-01-02 | 01 | 1 | TAGW-01, TAGW-02 | unit | `uv run pytest tests/test_services/test_tag_writer.py tests/test_services/test_tag_proposal.py -x` | ❌ W0 | ⬜ pending |
| 20-02-01 | 02 | 2 | TAGW-04 | integration | `uv run pytest tests/test_routers/test_tags.py -x` | ❌ W0 | ⬜ pending |
| 20-02-02 | 02 | 2 | TAGW-04 | integration | `uv run pytest tests/test_routers/test_tags.py -x` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_models/test_tag_write_log.py` — TagWriteLog model CRUD, JSONB snapshots, append-only constraint
- [ ] `tests/test_services/test_tag_writer.py` — Write + verify per format (MP3, M4A, OGG, OPUS, FLAC), EXECUTED gate, discrepancy flagging
- [ ] `tests/test_services/test_tag_proposal.py` — Cascade merge logic, priority resolution
- [ ] `tests/test_routers/test_tags.py` — Review page, inline edit, approve, side-by-side comparison
- [ ] Test audio fixtures: small valid MP3, M4A, OGG, OPUS, FLAC files for write tests

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Side-by-side visual layout | TAGW-04 | Visual layout verification | Load tag review page, verify two-column table with Field/Current/Proposed columns, changed fields highlighted |
| Inline edit interaction | TAGW-04 | HTMX interaction check | Click proposed field, edit value, verify it saves via HTMX swap |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 30s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
