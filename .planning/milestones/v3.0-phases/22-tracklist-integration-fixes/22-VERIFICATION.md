---
phase: 22-tracklist-integration-fixes
verified: 2026-04-04T00:55:34Z
status: passed
score: 3/3 must-haves verified
---

# Phase 22: Tracklist Integration Fixes Verification Report

**Phase Goal:** Close audit gaps: DISC-04 bulk-link button unreachable and CUE version badge stale after link operations
**Verified:** 2026-04-04T00:55:34Z
**Status:** PASSED
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Bulk-link All button appears on tracklist card when candidate DiscogsLinks exist | ✓ VERIFIED | `_has_candidates()` helper at line 29; passed as `has_candidates` context var to `approve_tracklist` (L546), `reject_tracklist` (L568), `match_discogs` (L635); `_has_candidates` ORM attr computed in `list_tracklists` (L112-124) and `_render_tracklist_list` (L818-830) |
| 2 | Bulk-link All button disappears after all candidates are accepted/dismissed | ✓ VERIFIED | Template guard `{% set show_bulk = (has_candidates is defined and has_candidates) or (tracklist._has_candidates is defined and tracklist._has_candidates) %}` — re-evaluates `_has_candidates()` live query on each render; `reject_tracklist` hardcodes `has_candidates: False`; `test_approve_tracklist_no_candidates_no_bulk_button` covers absence case |
| 3 | CUE version badge persists in tracklist list after undo-link operation | ✓ VERIFIED | `_render_tracklist_list` computes `tl._cue_version = _get_cue_version(fr.current_path)` at L838 for approved+EXECUTED tracklists; `tracklist_card.html` L96 reads `tracklist._cue_version if tracklist._cue_version is defined else (cue_version if cue_version is defined else 0)`; `test_undo_link_preserves_cue_version` covers this path |

**Score:** 3/3 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/routers/tracklists.py` | `has_candidates` context var in all card-rendering endpoints; `_cue_version` in `_render_tracklist_list` | ✓ VERIFIED | `_has_candidates()` helper defined at L29-39; wired in `approve_tracklist` L546, `reject_tracklist` L568, `match_discogs` L635; `_has_candidates` ORM attr in `list_tracklists` L112-124 and `_render_tracklist_list` L818-830; `_cue_version` ORM attr in both list functions |
| `tests/test_routers/test_tracklists.py` | Integration tests verifying `has_candidates` and `_cue_version` wiring; must include `test_match_discogs_returns_has_candidates` | ✓ VERIFIED | All 5 required tests present at lines 985-1111; `patch` import added to `unittest.mock` import at L4; 56 total test functions |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|----|--------|---------|
| `routers/tracklists.py` | `templates/tracklists/partials/discogs_bulk_link.html` | `has_candidates` context variable | ✓ WIRED | Template L1: `{% set show_bulk = (has_candidates is defined and has_candidates) or (tracklist._has_candidates is defined and tracklist._has_candidates) %}`; both single-card and list-view forms handled |
| `routers/tracklists.py (_render_tracklist_list)` | `templates/tracklists/partials/tracklist_card.html` | `_cue_version` dynamic ORM attribute | ✓ WIRED | Router L838: `tl._cue_version = _get_cue_version(fr.current_path)`; template L96: `tracklist._cue_version if tracklist._cue_version is defined else (cue_version if cue_version is defined else 0)` |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|--------------------|--------|
| `discogs_bulk_link.html` | `show_bulk` | `_has_candidates()` async DB query — `func.count(DiscogsLink.id)` where `status == "candidate"` | Yes — live SQL count query, not hardcoded | ✓ FLOWING |
| `tracklist_card.html` (`_cue_version` badge) | `cv` via `tracklist._cue_version` | `_get_cue_version(fr.current_path)` — reads actual .cue file from filesystem | Yes — file-system read of real path, returns 0 if absent | ✓ FLOWING |

### Behavioral Spot-Checks

Tests require PostgreSQL and cannot run without the Docker service stack. The test suite runs via `just test-ci` in CI (GitHub Actions with a PostgreSQL service container). Local execution fails with `[Errno 61] Connect call failed` as expected.

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| `uv run ruff check src/phaze/routers/tracklists.py` | `ruff check` | All checks passed | ✓ PASS |
| `uv run mypy src/phaze/routers/tracklists.py` | `mypy` | Success: no issues found in 1 source file | ✓ PASS |
| `uv run ruff check tests/test_routers/test_tracklists.py` | `ruff check` | All checks passed | ✓ PASS |
| Test suite (`just test-ci`) | Requires Docker + PostgreSQL | Cannot run locally — no Postgres service | ? SKIP (needs CI) |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| DISC-04 | 22-01-PLAN.md | User can bulk-link an entire tracklist's tracks to Discogs releases in one action | ✓ SATISFIED | `has_candidates` wired to `discogs_bulk_link.html` button; bulk-link endpoint at L729 already existed; the gap (button never visible) is closed by wiring `has_candidates` context var in all card-rendering endpoints |

**Orphaned requirements check:** REQUIREMENTS.md maps only DISC-04 to Phase 22. No orphaned requirements.

**REQUIREMENTS.md checkbox:** Line 22 still shows `- [ ] **DISC-04**` as unchecked. The phase completed the implementation but did not update the checkbox. This is a documentation gap only — it does not affect code correctness. The mapping table at line 72 still shows "Pending".

### Anti-Patterns Found

Scanned `src/phaze/routers/tracklists.py` and `tests/test_routers/test_tracklists.py`:

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `routers/tracklists.py` | 359 | `rescrape_queued` endpoint passes `has_candidates` as absent (context dict has no `has_candidates` key) | ℹ️ Info | `rescrape_tracklist` renders `tracklist_card.html` without `has_candidates`; Jinja template guard `has_candidates is defined` handles this safely — button simply won't show. Not a regression since re-scrape is not a candidate-lifecycle endpoint. |

No blockers or warnings found.

### Human Verification Required

#### 1. Bulk-link button visibility after Discogs match job completes

**Test:** With a real test account, trigger match-discogs for a tracklist, wait for the SAQ worker to create candidate links, then reload the tracklist card.
**Expected:** "Bulk-link All" button appears without a page reload (or appears on next HTMX re-render of the card).
**Why human:** The `match_discogs` endpoint enqueues the job and returns `has_candidates: False` immediately (candidates don't exist yet at enqueue time — the worker creates them asynchronously). The button will only appear after the SAQ worker finishes and the user triggers another card render. This is correct behavior per the plan, but should be confirmed UX-acceptable.

#### 2. Button disappears after all candidates resolved

**Test:** Accept or dismiss all candidate DiscogsLinks for a tracklist, then trigger any endpoint that re-renders the card.
**Expected:** "Bulk-link All" button is absent.
**Why human:** Verifying the disappearance requires a real browser session with candidate lifecycle.

### Gaps Summary

No gaps. All three observable truths are verified with substantive, wired, and data-flowing implementations.

The single informational note (rescrape endpoint missing `has_candidates`) is not a gap — the template handles the undefined case safely and rescrape is not a candidate-management operation.

The REQUIREMENTS.md checkbox for DISC-04 remaining unchecked is a documentation-only item and does not affect phase goal achievement.

---

_Verified: 2026-04-04T00:55:34Z_
_Verifier: Claude (gsd-verifier)_
