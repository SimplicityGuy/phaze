---
phase: 12
slug: infrastructure-audio-tag-extraction
status: complete
nyquist_compliant: true
wave_0_complete: true
created: 2026-03-30
---

# Phase 12 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio |
| **Config file** | pyproject.toml `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/ -x --timeout=30` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~30 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/ -x --timeout=30`
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 30 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 12-01-01 | 01 | 1 | INFRA-01 | unit | `uv run pytest tests/test_tasks/test_session.py -x` | Needs rewrite | ⬜ pending |
| 12-01-02 | 01 | 1 | INFRA-02 | unit | `uv run pytest tests/test_services/test_pipeline.py -x` | Needs update | ⬜ pending |
| 12-02-01 | 02 | 1 | TAGS-01 | unit | `uv run pytest tests/test_services/test_metadata.py -x` | ❌ W0 | ⬜ pending |
| 12-02-02 | 02 | 1 | TAGS-02 | unit | `uv run pytest tests/test_services/test_metadata.py -x` | ❌ W0 | ⬜ pending |
| 12-02-03 | 02 | 1 | TAGS-03 | unit | `uv run pytest tests/test_services/test_metadata.py -x` | ❌ W0 | ⬜ pending |
| 12-02-04 | 02 | 1 | TAGS-04 | unit | `uv run pytest tests/test_services/test_metadata.py -x` | ❌ W0 | ⬜ pending |
| 12-03-01 | 03 | 2 | TAGS-05 | unit | `uv run pytest tests/test_services/test_proposal.py -x` | Needs update | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_services/test_metadata.py` — stubs for TAGS-01 through TAGS-04 (extract_tags, format mapping, serialization, edge cases)
- [ ] `tests/test_tasks/test_metadata_extraction.py` — stubs for TAGS-01 arq task (extract_file_metadata task function)
- [ ] Update `tests/test_tasks/test_session.py` — rewrite for shared engine pattern (INFRA-01)
- [ ] Update `tests/test_services/test_pipeline.py` — add METADATA_EXTRACTED to stage counts (INFRA-02)
- [ ] Update `tests/test_services/test_proposal.py` — test build_file_context with metadata param (TAGS-05)

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| No connection exhaustion under concurrent load | INFRA-01 | Requires real PostgreSQL under load | Run backfill against dev DB with max_jobs=8, monitor pg_stat_activity for connection count |
| Tag extraction from real music files | TAGS-01 | Unit tests use synthetic data | Run `extract_tags` against sample mp3/flac/ogg/m4a files from collection |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 30s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
