---
phase: 15
slug: 1001tracklists-integration
status: complete
nyquist_compliant: true
wave_0_complete: true
created: 2026-04-01
---

# Phase 15 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x |
| **Config file** | pyproject.toml |
| **Quick run command** | `uv run pytest tests/test_tracklist*.py -x -q` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_tracklist*.py -x -q`
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 15-01-01 | 01 | 1 | TL-01, TL-02 | unit | `uv run pytest tests/test_tracklist_models.py -x -q` | ❌ W0 | ⬜ pending |
| 15-01-02 | 01 | 1 | TL-01 | unit | `uv run pytest tests/test_tracklist_scraper.py -x -q` | ❌ W0 | ⬜ pending |
| 15-02-01 | 02 | 2 | TL-03 | unit | `uv run pytest tests/test_tracklist_matcher.py -x -q` | ❌ W0 | ⬜ pending |
| 15-02-02 | 02 | 2 | TL-04 | unit | `uv run pytest tests/test_tracklist_refresh.py -x -q` | ❌ W0 | ⬜ pending |
| 15-03-01 | 03 | 3 | TL-03 | integration | `uv run pytest tests/test_tracklist_router.py -x -q` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_tracklist_models.py` — stubs for Tracklist and TracklistTrack model tests
- [ ] `tests/test_tracklist_scraper.py` — stubs for search and scrape service tests
- [ ] `tests/test_tracklist_matcher.py` — stubs for fuzzy matching logic tests
- [ ] `tests/test_tracklist_refresh.py` — stubs for periodic refresh task tests
- [ ] `tests/test_tracklist_router.py` — stubs for tracklist UI endpoint tests

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| 1001tracklists.com scraping returns valid data | TL-01 | External service dependency, rate limiting | Trigger manual search from UI, verify results appear |
| Tracklist card expand/collapse in admin UI | TL-03 | Visual HTMX interaction | Navigate to Tracklists page, click card, verify track list expands |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
