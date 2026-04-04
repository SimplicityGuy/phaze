---
phase: 23-v3-polish-wiring-fixes
verified: 2026-04-03T10:00:00Z
status: passed
score: 2/2 must-haves verified
re_verification: false
---

# Phase 23: v3 Polish Wiring Fixes Verification Report

**Phase Goal:** Fix rescrape_tracklist has_candidates omission and enrich tag proposals with accepted Discogs metadata
**Verified:** 2026-04-03
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| #   | Truth                                                                                         | Status     | Evidence                                                                                                                                             |
| --- | --------------------------------------------------------------------------------------------- | ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Bulk-link All button visible on tracklist card after re-scrape completes                      | ✓ VERIFIED | `rescrape_tracklist` passes `has_candidates` in context (tracklists.py:361); `tracklist_card.html` includes `discogs_bulk_link.html` which guards the button on that variable |
| 2   | Tag proposals include Discogs-verified artist and title from accepted DiscogsLinks            | ✓ VERIFIED | `compute_proposed_tags` accepts `discogs_link` param (tag_proposal.py:61); Layer 4 block (lines 99-106) applies `discogs_artist`, `discogs_title`, `discogs_year`; all 4 call sites in tags.py pass the resolved link |

**Score:** 2/2 truths verified

### Required Artifacts

| Artifact                                        | Expected                                                | Status     | Details                                                                                                        |
| ----------------------------------------------- | ------------------------------------------------------- | ---------- | -------------------------------------------------------------------------------------------------------------- |
| `src/phaze/routers/tracklists.py`               | rescrape_tracklist endpoint with has_candidates in context | ✓ VERIFIED | `has_candidates = await _has_candidates(session, tracklist) if tracklist else False` at line 356; included in TemplateResponse context at line 361 |
| `src/phaze/services/tag_proposal.py`            | Tag proposal cascade consulting DiscogsLink accepted metadata | ✓ VERIFIED | `discogs_link: DiscogsLink | None = None` param added; Layer 4 block at lines 99-106; module docstring updated to reflect new priority order |
| `tests/test_services/test_tag_proposal.py`      | Tests for Discogs-enriched tag proposals                | ✓ VERIFIED | 5 new tests: `test_discogs_link_overrides_tracklist`, `test_discogs_link_year_overrides`, `test_discogs_link_none_fields_no_override`, `test_discogs_link_without_other_sources`, `test_no_discogs_link_unchanged_behavior` — all pass |

### Key Link Verification

| From                                       | To                          | Via                                                                 | Status     | Details                                                                                                |
| ------------------------------------------ | --------------------------- | ------------------------------------------------------------------- | ---------- | ------------------------------------------------------------------------------------------------------ |
| `src/phaze/routers/tracklists.py`          | `_has_candidates` helper    | `await _has_candidates(session, tracklist)` in rescrape_tracklist   | ✓ WIRED    | Helper defined at lines 29-39; called at line 356 inside `rescrape_tracklist`                         |
| `src/phaze/services/tag_proposal.py`       | `DiscogsLink` model         | `discogs_link` parameter in `compute_proposed_tags`                 | ✓ WIRED    | TYPE_CHECKING import at line 15; parameter at line 61; accessed at lines 101-106                      |
| `src/phaze/routers/tags.py`                | `src/phaze/services/tag_proposal.py` | `compute_proposed_tags` called with `discogs_link=discogs_link` | ✓ WIRED    | `_get_accepted_discogs_link` helper at lines 77-92; called at all 4 sites: list_tags (172-173), compare_tags (219-220), edit_tag_field (252-253), write_file_tags (336-342) |

### Data-Flow Trace (Level 4)

| Artifact                              | Data Variable    | Source                                                          | Produces Real Data | Status      |
| ------------------------------------- | ---------------- | --------------------------------------------------------------- | ------------------ | ----------- |
| `src/phaze/services/tag_proposal.py`  | `discogs_link`   | `_get_accepted_discogs_link` queries `discogs_links` table with status="accepted" filter | Yes — SQLAlchemy query with `.where(DiscogsLink.status == "accepted").order_by(DiscogsLink.confidence.desc()).limit(1)` | ✓ FLOWING |
| `src/phaze/routers/tracklists.py`     | `has_candidates` | `_has_candidates` queries `discogs_links` table with status="candidate" count | Yes — `select(func.count(DiscogsLink.id)).where(...)` returns real count | ✓ FLOWING |

### Behavioral Spot-Checks

| Behavior                                         | Command                                                                        | Result      | Status  |
| ------------------------------------------------ | ------------------------------------------------------------------------------ | ----------- | ------- |
| Tag proposal Discogs layer overrides tracklist   | `uv run pytest tests/test_services/test_tag_proposal.py -k discogs -v`        | 5 passed    | ✓ PASS  |
| Backward compat: discogs_link=None unchanged     | `uv run pytest tests/test_services/test_tag_proposal.py::TestComputeProposedTags::test_no_discogs_link_unchanged_behavior` | passed | ✓ PASS |
| Full tag proposal suite (no regressions)         | `uv run pytest tests/test_services/test_tag_proposal.py`                      | 20/20 passed | ✓ PASS |
| Router tests (rescrape with candidates)          | `uv run pytest tests/test_routers/test_tracklists.py -k rescrape`             | Requires PostgreSQL — not available in this environment | ? SKIP |
| Mypy on all modified files                        | `uv run mypy src/phaze/services/tag_proposal.py src/phaze/routers/tags.py src/phaze/routers/tracklists.py` | Success: no issues found in 3 source files | ✓ PASS |
| Ruff lint on modified files                       | `uv run ruff check ...`                                                       | All checks passed | ✓ PASS |

### Requirements Coverage

No formal requirement IDs were assigned to this phase (polish/gap-closure phase). The phase addresses the DISC-04 gap documented in `.planning/v3.0-MILESTONE-AUDIT.md` — the `has_candidates` context omission in `rescrape_tracklist`.

### Anti-Patterns Found

No blockers or warnings detected.

| File                                          | Pattern Checked                        | Result    |
| --------------------------------------------- | -------------------------------------- | --------- |
| `src/phaze/routers/tracklists.py`             | Empty return / TODO / placeholder      | None found |
| `src/phaze/services/tag_proposal.py`          | Hardcoded empty returns / stub         | None found |
| `src/phaze/routers/tags.py`                   | discogs_link passed as `[]` or `None` at call sites | None — all 4 sites call `_get_accepted_discogs_link` and pass the real result |
| `tests/test_services/test_tag_proposal.py`    | Mock disconnected from real assertions | None — mock attributes match actual DiscogsLink fields |

### Human Verification Required

#### 1. Bulk-link button visibility after re-scrape (UI)

**Test:** Open the tracklists admin page, find a tracklist with at least one candidate DiscogsLink, click Re-scrape, and observe whether the "Bulk-link All" button appears in the refreshed card without a page reload.
**Expected:** The Bulk-link All button appears immediately in the HTMX-swapped card HTML when candidates exist.
**Why human:** Requires a running application with PostgreSQL, seeded candidate DiscogsLinks, and browser verification of the HTMX swap result.

#### 2. Tag write uses Discogs data end-to-end (UI)

**Test:** Accept a Discogs link for a track, navigate to the Tags review page for the associated file, and verify that the proposed artist/title/year reflect the accepted Discogs values rather than the file metadata.
**Expected:** The comparison panel shows DiscogsLink-sourced artist, title, and/or year in the "Proposed" column.
**Why human:** Requires a running application with PostgreSQL, a file with an accepted DiscogsLink, and visual inspection of the tag comparison panel.

### Gaps Summary

No gaps. All must-haves verified.

- Truth 1 (Bulk-link button after re-scrape): The `_has_candidates` helper was created and wired into `rescrape_tracklist`. The `discogs_bulk_link.html` template reads the `has_candidates` context variable via `{% set show_bulk = (has_candidates is defined and has_candidates) ... %}`.
- Truth 2 (Discogs metadata in tag proposals): The four-layer cascade is fully implemented. All 4 call sites in `tags.py` fetch the accepted link via `_get_accepted_discogs_link` and pass it as `discogs_link=discogs_link`. The service-level unit tests confirm the cascade priority is correct and backward-compatible.
- The only items deferred to human verification are UI/visual checks that require a live environment with PostgreSQL — these are expected and do not constitute gaps.

---

_Verified: 2026-04-03_
_Verifier: Claude (gsd-verifier)_
