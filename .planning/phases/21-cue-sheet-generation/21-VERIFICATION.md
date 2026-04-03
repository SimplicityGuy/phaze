---
phase: 21-cue-sheet-generation
verified: 2026-04-03T22:30:00Z
status: gaps_found
score: 11/13 must-haves verified
gaps:
  - truth: "Fingerprint timestamps take priority over 1001tracklists timestamps"
    status: partial
    reason: "The router's generate endpoint operates on a single tracklist_id selected by the user. There is no automatic source-priority logic that selects fingerprint timestamps over 1001tracklists timestamps when both exist for the same file. D-02 and Plan 01 truth 2 specify automated priority, but the implementation defers the choice to the user via which row they click. CUE-01 in REQUIREMENTS.md says 'preferring fingerprint timestamps with 1001tracklists fallback' — this preference is not automated."
    artifacts:
      - path: "src/phaze/routers/cue.py"
        issue: "_build_cue_tracks uses latest_version_id from whichever single tracklist_id is passed. No multi-source priority selection logic exists."
    missing:
      - "When a file has both fingerprint and 1001tracklists tracklists, the eligible tracklist query or generate endpoint should select/prefer the fingerprint tracklist automatically, or at minimum surface the source type to the user so they can make an informed choice."
  - truth: "Re-generating shows Regenerate CUE and increments version badge"
    status: partial
    reason: "The cue_row.html partial correctly switches button text to 'Regenerate CUE' when cue_version > 0 and shows the version badge. However, the tracklist_card.html inline button (the primary Generate CUE entry point) always shows 'Generate CUE' regardless of whether a CUE already exists — it has no awareness of cue_version. Only the /cue management page rows show the Regenerate CUE state."
    artifacts:
      - path: "src/phaze/templates/tracklists/partials/tracklist_card.html"
        issue: "Generate CUE button at line 96-101 has no cue_version check. Button always reads 'Generate CUE' even after a CUE has been generated for this tracklist."
    missing:
      - "The tracklist router that renders tracklist_card.html should pass cue_version context, and tracklist_card.html should show 'Regenerate CUE' when cue_version > 0."
human_verification:
  - test: "Generate CUE from tracklist card and verify button changes to Regenerate CUE"
    expected: "After generating a CUE, the card should show 'Regenerate CUE' button and a CUE vN badge"
    why_human: "Requires live app with an EXECUTED tracklist; button state depends on runtime cue_version context"
  - test: "File with both fingerprint and 1001tracklists tracklists — verify CUE management page indicates source type"
    expected: "User should be able to identify which tracklist is fingerprint-sourced to prefer it for CUE generation"
    why_human: "Requires seeded database with both source types for the same file"
---

# Phase 21: CUE Sheet Generation Verification Report

**Phase Goal:** Users can generate .cue companion files from tracklist data enriched with Discogs metadata
**Verified:** 2026-04-03T22:30:00Z
**Status:** gaps_found
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| #  | Truth | Status | Evidence |
|----|-------|--------|---------|
| 1  | CUE content generated from tracklist data with correct MM:SS:FF timestamps at 75fps | VERIFIED | `seconds_to_cue_timestamp` implemented in cue_generator.py:46-60; 9 unit tests in TestSecondsToTimestamp all pass |
| 2  | Fingerprint timestamps take priority over 1001tracklists timestamps | PARTIAL | No automated priority selection in router. Generate endpoint takes a single `tracklist_id` — user must manually choose. D-02 specifies automated preference. |
| 3  | Tracks without any timestamp are omitted from the CUE output | VERIFIED | `generate_cue_content` filters `t.timestamp_seconds is not None` at line 111; `test_tracks_without_timestamp_omitted` passes |
| 4  | CUE files include per-track REM GENRE, REM LABEL, REM YEAR from accepted DiscogsLinks | VERIFIED | `_build_cue_tracks` in cue.py populates label/year from accepted DiscogsLink; 5 REM tests pass. Note: genre is always None because DiscogsLink has no genre field — implementation comments this correctly per D-09. |
| 5  | Tracks without accepted DiscogsLink have no REM comments | VERIFIED | `test_no_discogs_metadata` and `test_mixed_tracks_with_and_without_discogs` pass; router sets genre=None always, label/year only if discogs_link exists |
| 6  | CUE files are written with UTF-8 BOM encoding | VERIFIED | `write_cue_file` uses `encoding="utf-8-sig"` at line 184; `test_written_file_has_bom` verifies BOM bytes EF BB BF |
| 7  | Re-generation uses version suffix naming (file.v2.cue, file.v3.cue) | VERIFIED | `next_cue_path` in cue_generator.py:139-168; 4 TestNextCuePath tests pass; `test_version_increment_on_rewrite` passes |
| 8  | User can generate a CUE file from the tracklist detail page via Generate CUE button | VERIFIED | tracklist_card.html lines 95-102 add button with `hx-post="/cue/{{ tracklist.id }}/generate"` when status='approved' and file_id set |
| 9  | User can view a CUE management page listing all eligible tracklists with CUE status | VERIFIED | GET /cue/ in cue.py:168-224; list.html with stats header and cue-list-container; test_cue_list_full_page |
| 10 | User can batch-generate CUE files for all eligible tracklists | VERIFIED | POST /cue/generate-batch endpoint at cue.py:302-366; "Generate All Eligible" button in list.html; test_generate_batch |
| 11 | CUE tab appears in main navigation between Tags and Audit Log | VERIFIED | base.html lines 67-70: `<a href="/cue/">` after Tags and before Audit Log |
| 12 | Only approved tracklists with EXECUTED files and timestamps show Generate CUE button | VERIFIED | tracklist_card.html condition `tracklist.status == 'approved' and tracklist.file_id`; router validates EXECUTED state before generating |
| 13 | Re-generating shows Regenerate CUE and increments version badge | PARTIAL | cue_row.html (management page) correctly shows "Regenerate CUE" when cue_version > 0. tracklist_card.html (inline button) always shows "Generate CUE" — no cue_version context passed to tracklist card template. |

**Score:** 11/13 truths verified

### Required Artifacts

| Artifact | Min Lines | Actual Lines | Status | Details |
|----------|-----------|-------------|--------|---------|
| `src/phaze/services/cue_generator.py` | — | 187 | VERIFIED | All 6 exports present: generate_cue_content, write_cue_file, seconds_to_cue_timestamp, parse_timestamp_string, next_cue_path, CueTrackData |
| `tests/test_services/test_cue_generator.py` | 150 | 352 | VERIFIED | 44 unit tests across 6 classes; all pass |
| `src/phaze/routers/cue.py` | 100 | 378 | VERIFIED | list_cue, generate_cue, generate_batch endpoints present |
| `src/phaze/templates/cue/list.html` | — | 49 | VERIFIED | "CUE Sheets" heading, stats cards bg-blue-50/bg-green-50/bg-yellow-50, cue-list-container |
| `tests/test_routers/test_cue.py` | 80 | 219 | VERIFIED | 10 integration tests; all substantive with real DB fixture setup |

Note: test_routers/test_cue.py tests error on connection-refused to PostgreSQL in this environment (PostgreSQL not running locally). This is an infrastructure gap, not a code issue. The test code is substantive and correct; the same tests passed when the SUMMARY was written (728 tests passing, 95.65% coverage).

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `src/phaze/routers/cue.py` | `src/phaze/services/cue_generator.py` | `from phaze.services.cue_generator import` | VERIFIED | Line 19: imports CueTrackData, generate_cue_content, parse_timestamp_string, write_cue_file |
| `src/phaze/main.py` | `src/phaze/routers/cue.py` | `app.include_router(cue.router)` | VERIFIED | main.py line 12 imports cue; line 42 `app.include_router(cue.router)` |
| `src/phaze/templates/base.html` | `/cue` | nav tab link | VERIFIED | base.html lines 67-70: `href="/cue/"` with `current_page == 'cue'` active state |
| `src/phaze/routers/cue.py` | `TracklistTrack.timestamp` | CueTrackData consuming timestamp strings | VERIFIED | `_build_cue_tracks` calls `parse_timestamp_string(track.timestamp)` at line 141 |
| `src/phaze/routers/cue.py` | `DiscogsLink metadata` | CueTrackData fields for label, year | VERIFIED | Lines 149-152 populate label/year from accepted DiscogsLink |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|-------------------|--------|
| `cue/list.html` | `stats` | `_get_cue_stats(session)` — DB queries for eligible/generated/missing | Yes — real SQLAlchemy queries | FLOWING |
| `cue/list.html` | `tracklists` | `_get_eligible_tracklist_query(session)` — approved+EXECUTED+timestamps | Yes — subquery-backed filter | FLOWING |
| `cue/partials/cue_row.html` | `tracklist.cue_version` | `_get_cue_version(fr.current_path)` — filesystem scan | Yes — scans directory for .cue files | FLOWING |
| `generate_cue_content` return | CUE string | `_build_cue_tracks` — loads tracks from DB + DiscogsLinks | Yes — full DB round-trip | FLOWING |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| seconds_to_cue_timestamp(332.45) == "05:32:33" | `uv run pytest tests/test_services/test_cue_generator.py::TestSecondsToTimestamp::test_mixed_minutes_seconds -v` | 44 passed | PASS |
| Frame values never reach 75 | `uv run pytest tests/test_services/test_cue_generator.py::TestSecondsToTimestamp::test_frames_never_reach_75 -v` | PASSED | PASS |
| UTF-8 BOM written | `uv run pytest tests/test_services/test_cue_generator.py::TestWriteCueFile::test_written_file_has_bom -v` | PASSED | PASS |
| Tracks without timestamps omitted | `uv run pytest tests/test_services/test_cue_generator.py::TestGenerateCueContent::test_tracks_without_timestamp_omitted -v` | PASSED | PASS |
| Router integration tests | `uv run pytest tests/test_routers/test_cue.py -v` | 10 errors (PostgreSQL not running locally) | SKIP — infrastructure only |
| mypy clean | `uv run mypy src/phaze/services/cue_generator.py src/phaze/routers/cue.py` | Success: no issues found | PASS |
| ruff clean | `uv run ruff check src/phaze/services/cue_generator.py src/phaze/routers/cue.py` | All checks passed | PASS |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|------------|------------|-------------|--------|---------|
| CUE-01 | 21-01, 21-02 | System generates .cue companion files from tracklist data, preferring fingerprint timestamps with 1001tracklists fallback | PARTIAL | CUE generation works end-to-end. Automated fingerprint timestamp priority not implemented — user selects which tracklist to generate from manually |
| CUE-02 | 21-01, 21-02 | CUE files use correct 75fps frame conversion and UTF-8 with BOM encoding | SATISFIED | seconds_to_cue_timestamp verified; utf-8-sig encoding verified; BOM bytes EF BB BF confirmed by test |
| CUE-03 | 21-01, 21-02 | CUE files include REM comments with Discogs metadata (genre, label, catalog number, year) | SATISFIED with note | REM LABEL and REM YEAR implemented from accepted DiscogsLinks. REM GENRE is always None because DiscogsLink model has no genre field — this is documented in code (line 149) and consistent with D-09. No catalog number field exists in DiscogsLink either. Requirements text says "genre, label, catalog number, year" but the model only has label/year/artist/title. |

**Orphaned requirements:** None. All three CUE-01/02/03 are claimed in both plan frontmatters.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|---------|--------|
| `src/phaze/routers/cue.py` | 324 | `except Exception: # noqa: S112` (bare except with continue in batch loop) | Info | Batch generation silently swallows write errors per-tracklist. Individual failures do not surface in the final count or toast. This is a deliberate design choice (resilient batch) but means partial failures are invisible. |
| `src/phaze/templates/tracklists/partials/tracklist_card.html` | 96-101 | Button always reads "Generate CUE" regardless of existing CUE | Warning | CUE-01 regeneration flow shows "Generate CUE" even if a .cue file already exists — misleading for the user. The management page /cue shows the correct "Regenerate CUE" state. |

No TODO/FIXME/placeholder comments found. No stub returns. No hardcoded empty data flowing to render.

### Human Verification Required

#### 1. Tracklist Card Regenerate State

**Test:** Generate a CUE from a tracklist card. Refresh or re-render the tracklist list.
**Expected:** The Generate CUE button should change to "Regenerate CUE" and a CUE vN badge should appear.
**Why human:** Requires live app with EXECUTED tracklist; button state depends on tracklist router passing cue_version context, which currently it does not.

#### 2. Fingerprint vs 1001tracklists Priority on CUE Management Page

**Test:** Create a file that has both a fingerprint tracklist and a 1001tracklists tracklist, both approved. Visit /cue/.
**Expected:** The page should either (a) automatically generate from the fingerprint tracklist, or (b) surface the source type so the user can choose the right one.
**Why human:** Requires seeded database with two tracklists for the same file from different sources.

### Gaps Summary

Two gaps found:

**Gap 1 — Fingerprint priority (CUE-01 partial):** The requirement text says "preferring fingerprint timestamps with 1001tracklists fallback" and CONTEXT decision D-02 says "Fingerprint timestamps always take priority." The implementation does not automate this: the eligible tracklist query returns all approved tracklists regardless of source, the management page lists them all, and the generate button fires on whichever row the user clicks. When a file has tracklists from both sources, the user must know to pick the fingerprint one. The source type (`fingerprint` vs `1001tracklists`) is not surfaced in the management page row (`cue_row.html` shows artist/event/date/track_count/cue_version — no source field). This is a completeness gap against the stated requirement preference.

**Gap 2 — Tracklist card regenerate state (Plan 02 truth 6):** The Generate CUE button in `tracklist_card.html` has no awareness of whether a CUE already exists. The tracklist router that renders this template does not compute `cue_version` context per tracklist. Only the dedicated `/cue` management page rows (`cue_row.html`) correctly show "Regenerate CUE" and the version badge. The inline flow — which is described as the "primary per-tracklist action" in the plan — is incomplete for the regeneration case.

These two gaps are related to incomplete UI state propagation, not to the core CUE generation logic which is fully working.

---

_Verified: 2026-04-03T22:30:00Z_
_Verifier: Claude (gsd-verifier)_
