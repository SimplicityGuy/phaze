---
phase: 19
slug: discogs-cross-service-linking
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-04-02
---

# Phase 19 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio |
| **Config file** | `pyproject.toml` [tool.pytest.ini_options] |
| **Quick run command** | `uv run pytest tests/ -x --tb=short` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~30 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/ -x --tb=short`
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 30 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 19-01-01 | 01 | 1 | DISC-01, DISC-02 | unit | `uv run pytest tests/test_models/test_discogs_link.py -x` | ❌ W0 | ⬜ pending |
| 19-01-02 | 01 | 1 | DISC-01 | unit | `uv run pytest tests/test_services/test_discogs_matcher.py -x` | ❌ W0 | ⬜ pending |
| 19-01-03 | 01 | 1 | DISC-01 | unit | `uv run pytest tests/test_tasks/test_discogs.py -x` | ❌ W0 | ⬜ pending |
| 19-02-01 | 02 | 2 | DISC-02 | integration | `uv run pytest tests/test_routers/test_tracklists.py -x -k discogs` | ❌ W0 | ⬜ pending |
| 19-02-02 | 02 | 2 | DISC-04 | integration | `uv run pytest tests/test_routers/test_tracklists.py -x -k bulk_link` | ❌ W0 | ⬜ pending |
| 19-02-03 | 02 | 2 | DISC-03 | integration | `uv run pytest tests/test_routers/test_search.py -x -k discogs` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_models/test_discogs_link.py` — stubs for DiscogsLink model CRUD, constraints, indexes
- [ ] `tests/test_services/test_discogs_matcher.py` — API adapter (mocked httpx), confidence scoring, match orchestration
- [ ] `tests/test_tasks/test_discogs.py` — SAQ task execution with mocked adapter
- [ ] `tests/test_routers/test_tracklists.py` — extend with Discogs candidate endpoints, accept/dismiss, bulk-link
- [ ] `tests/test_routers/test_search.py` — extend with Discogs release search results
- [ ] `tests/test_services/test_search_queries.py` — extend with Discogs UNION ALL branch

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Inline candidate display on tracklist page | DISC-02 | Visual layout verification | Load tracklist detail, trigger match, verify candidate rows appear under tracks with accept/dismiss buttons |
| Purple pill badge for Discogs results in search | DISC-03 | Visual styling check | Search for a linked track, verify purple badge appears for Discogs release results |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 30s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
