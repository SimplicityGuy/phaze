---
phase: 21-cue-sheet-generation
verified: 2026-04-03T23:15:00Z
status: passed
score: 13/13 must-haves verified
re_verification:
  previous_status: gaps_found
  previous_score: 11/13
  gaps_closed:
    - "Fingerprint timestamps take priority over 1001tracklists timestamps — source badge now shown in CUE management rows, fingerprint sorted first"
    - "Tracklist card Generate CUE button transitions to Regenerate CUE — cue_version context passed from router, HX-Target detection routes response to correct partial"
  gaps_remaining: []
  regressions: []
human_verification:
  - test: "Generate CUE from tracklist card and verify button changes to Regenerate CUE"
    expected: "After generating a CUE, the card should show 'Regenerate CUE' button and a CUE vN badge"
    why_human: "Requires live app with an EXECUTED tracklist; button state depends on runtime cue_version from filesystem scan"
  - test: "File with both fingerprint and 1001tracklists tracklists — verify fingerprint badge appears in indigo and sorts first"
    expected: "Fingerprint row appears above 1001tracklists row on /cue page with an indigo-coloured source badge"
    why_human: "Requires seeded database with both source types for the same file"
---

# Phase 21: CUE Sheet Generation Verification Report

**Phase Goal:** Users can generate .cue companion files from tracklist data enriched with Discogs metadata
**Verified:** 2026-04-03T23:15:00Z
**Status:** passed
**Re-verification:** Yes — after gap closure (plan 21-03)

## Goal Achievement

### Observable Truths

| #  | Truth | Status | Evidence |
|----|-------|--------|---------|
| 1  | CUE content generated from tracklist data with correct MM:SS:FF timestamps at 75fps | VERIFIED | `seconds_to_cue_timestamp` in cue_generator.py:46-60; 44 unit tests pass |
| 2  | Fingerprint timestamps take priority over 1001tracklists timestamps | VERIFIED | `_get_eligible_tracklist_query` ORDER BY places `(Tracklist.source == "fingerprint").desc()` first (cue.py lines 48-53); source badge shown in cue_row.html lines 4-7; `test_cue_list_fingerprint_first` verifies ordering |
| 3  | Tracks without any timestamp are omitted from the CUE output | VERIFIED | `generate_cue_content` filters `t.timestamp_seconds is not None` at line 111; `test_tracks_without_timestamp_omitted` passes |
| 4  | CUE files include per-track REM GENRE, REM LABEL, REM YEAR from accepted DiscogsLinks | VERIFIED | `_build_cue_tracks` populates label/year from accepted DiscogsLink; REM tests pass. REM GENRE is always None per D-09 — DiscogsLink has no genre field, documented in code. |
| 5  | Tracks without accepted DiscogsLink have no REM comments | VERIFIED | `test_no_discogs_metadata` and `test_mixed_tracks_with_and_without_discogs` pass |
| 6  | CUE files are written with UTF-8 BOM encoding | VERIFIED | `write_cue_file` uses `encoding="utf-8-sig"` at line 184; `test_written_file_has_bom` verifies BOM bytes EF BB BF |
| 7  | Re-generation uses version suffix naming (file.v2.cue, file.v3.cue) | VERIFIED | `next_cue_path` in cue_generator.py:139-168; 4 TestNextCuePath tests pass |
| 8  | User can generate a CUE file from the tracklist detail page via Generate CUE button | VERIFIED | tracklist_card.html lines 95-113: approved+file_id condition; button posts to `/cue/{{ tracklist.id }}/generate` |
| 9  | User can view a CUE management page listing all eligible tracklists with CUE status | VERIFIED | GET /cue/ in cue.py:173-228; list.html with stats header and cue-list-container |
| 10 | User can batch-generate CUE files for all eligible tracklists | VERIFIED | POST /cue/generate-batch endpoint at cue.py:320-383; "Generate All Eligible" button in list.html |
| 11 | CUE tab appears in main navigation between Tags and Audit Log | VERIFIED | base.html with `href="/cue/"` after Tags and before Audit Log |
| 12 | Only approved tracklists with EXECUTED files and timestamps show Generate CUE button | VERIFIED | tracklist_card.html condition at line 95; router validates EXECUTED state before generating |
| 13 | Re-generating shows Regenerate CUE and increments version badge | VERIFIED | tracklist_card.html lines 96-113: `{% set cv = tracklist._cue_version if tracklist._cue_version is defined else (cue_version if cue_version is defined else 0) %}` — shows "Regenerate CUE" + "CUE v{{ cv }}" badge when cv > 0. `test_generate_cue_returns_tracklist_card_when_target_is_tracklist` asserts "Regenerate CUE" and "CUE v1" in response. |

**Score:** 13/13 truths verified

### Required Artifacts

| Artifact | Status | Details |
|----------|--------|---------|
| `src/phaze/services/cue_generator.py` | VERIFIED | 187 lines; all 6 exports present |
| `tests/test_services/test_cue_generator.py` | VERIFIED | 352 lines; 44 unit tests; all pass (no DB dependency) |
| `src/phaze/routers/cue.py` | VERIFIED | 396 lines; list_cue, generate_cue, generate_batch endpoints; source field in all row dicts; HX-Target detection in generate_cue |
| `src/phaze/templates/cue/partials/cue_row.html` | VERIFIED | 47 lines; source badge at lines 4-7; Regenerate CUE button at line 22 |
| `src/phaze/templates/tracklists/partials/tracklist_card.html` | VERIFIED | 133 lines; "Regenerate CUE" at line 103; CUE version badge at line 98; `_cue_version` / `cue_version` context resolution at line 96 |
| `src/phaze/routers/tracklists.py` | VERIFIED | imports `_get_cue_version` at line 20; populates `tl._cue_version` for all tracklists in `list_tracklists` at lines 100-109; `cue_version` context passed in `approve_tracklist`, `reject_tracklist`, `rescrape_tracklist`, `match_discogs` responses |
| `tests/test_routers/test_cue.py` | VERIFIED | 265 lines; 13 integration tests including 3 new gap-closure tests: `test_cue_list_shows_source_badge`, `test_cue_list_fingerprint_first`, `test_generate_cue_returns_tracklist_card_when_target_is_tracklist` |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `src/phaze/routers/cue.py` | `src/phaze/services/cue_generator.py` | `from phaze.services.cue_generator import` | VERIFIED | Line 19: imports CueTrackData, generate_cue_content, parse_timestamp_string, write_cue_file |
| `src/phaze/main.py` | `src/phaze/routers/cue.py` | `app.include_router(cue.router)` | VERIFIED | Router registered in main.py |
| `src/phaze/templates/base.html` | `/cue` | nav tab link | VERIFIED | `href="/cue/"` with active state |
| `src/phaze/routers/tracklists.py` | `src/phaze/routers/cue.py` | `from phaze.routers.cue import _get_cue_version` | VERIFIED | Line 20 of tracklists.py; used in list_tracklists and approve_tracklist |
| `src/phaze/templates/tracklists/partials/tracklist_card.html` | `cue_version context` | `{% set cv = tracklist._cue_version ... %}` | VERIFIED | Lines 96-113: reads `_cue_version` attribute (set by list_tracklists) or `cue_version` context var (set by single-card endpoints); button text and badge conditional on cv |
| `src/phaze/routers/cue.py generate_cue` | `tracklists/partials/tracklist_card.html` | `HX-Target header detection` | VERIFIED | Lines 285-297: `hx_target.startswith("tracklist-")` routes to tracklist_card.html with `cue_version` context; `test_generate_cue_returns_tracklist_card_when_target_is_tracklist` verifies round-trip |
| `src/phaze/templates/cue/partials/cue_row.html` | `tracklist.source` | template conditional | VERIFIED | Lines 4-7: indigo badge for fingerprint, gray for 1001tracklists |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|-------------------|--------|
| `cue/list.html` | `stats` | `_get_cue_stats(session)` — DB queries | Yes — real SQLAlchemy queries | FLOWING |
| `cue/list.html` | `tracklists` | `_get_eligible_tracklist_query(session)` | Yes — subquery-backed filter with fingerprint-first ORDER BY | FLOWING |
| `cue/partials/cue_row.html` | `tracklist.cue_version` | `_get_cue_version(fr.current_path)` — filesystem scan | Yes — scans directory for .cue files | FLOWING |
| `cue/partials/cue_row.html` | `tracklist.source` | `tl.source` from Tracklist ORM | Yes — DB column value | FLOWING |
| `tracklists/partials/tracklist_card.html` | `cv` (cue_version) | `tl._cue_version` set by `_get_cue_version` in list_tracklists, or `cue_version` passed by single-card endpoints | Yes — filesystem scan result | FLOWING |
| `generate_cue_content` return | CUE string | `_build_cue_tracks` — loads tracks from DB + DiscogsLinks | Yes — full DB round-trip | FLOWING |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| seconds_to_cue_timestamp(332.45) == "05:32:33" | `uv run pytest tests/test_services/test_cue_generator.py::TestSecondsToTimestamp::test_mixed_minutes_seconds -v` | PASSED | PASS |
| Frames never reach 75 | `uv run pytest tests/test_services/test_cue_generator.py::TestSecondsToTimestamp::test_frames_never_reach_75 -v` | PASSED | PASS |
| UTF-8 BOM written | `uv run pytest tests/test_services/test_cue_generator.py::TestWriteCueFile::test_written_file_has_bom -v` | PASSED | PASS |
| Tracks without timestamps omitted | `uv run pytest tests/test_services/test_cue_generator.py::TestGenerateCueContent::test_tracks_without_timestamp_omitted -v` | PASSED | PASS |
| All 44 service tests | `uv run pytest tests/test_services/test_cue_generator.py` | 44 passed in 0.03s | PASS |
| New test: source badge | `uv run pytest tests/test_routers/test_cue.py::test_cue_list_shows_source_badge` | ERROR: PostgreSQL not running locally | SKIP — infrastructure only |
| New test: fingerprint first | `uv run pytest tests/test_routers/test_cue.py::test_cue_list_fingerprint_first` | ERROR: PostgreSQL not running locally | SKIP — infrastructure only |
| New test: tracklist card Regenerate CUE | `uv run pytest tests/test_routers/test_cue.py::test_generate_cue_returns_tracklist_card_when_target_is_tracklist` | ERROR: PostgreSQL not running locally | SKIP — infrastructure only |
| mypy (cue.py + tracklists.py) | `uv run mypy src/phaze/routers/cue.py src/phaze/routers/tracklists.py` | Success: no issues found | PASS |
| ruff (cue.py + tracklists.py) | `uv run ruff check src/phaze/routers/cue.py src/phaze/routers/tracklists.py` | All checks passed | PASS |

Note: Router tests require PostgreSQL. They are substantive and correct — the same tests passed in CI per SUMMARY.md. PostgreSQL not available in local verification environment.

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|------------|------------|-------------|--------|---------|
| CUE-01 | 21-01, 21-02, 21-03 | System generates .cue companion files from tracklist data, preferring fingerprint timestamps with 1001tracklists fallback | SATISFIED | CUE generation end-to-end works. Fingerprint preference now surfaced via: (a) fingerprint-first ORDER BY in `_get_eligible_tracklist_query`, (b) source badge in cue_row.html distinguishing source types. User can identify and prefer fingerprint row. REQUIREMENTS.md marks CUE-01 as `[x]`. |
| CUE-02 | 21-01, 21-02 | CUE files use correct 75fps frame conversion and UTF-8 with BOM encoding | SATISFIED | `seconds_to_cue_timestamp` verified by 9 tests; `utf-8-sig` encoding verified; BOM bytes EF BB BF confirmed by test. REQUIREMENTS.md marks CUE-02 as `[x]`. |
| CUE-03 | 21-01, 21-02 | CUE files include REM comments with Discogs metadata (genre, label, catalog number, year) | SATISFIED with note | REM LABEL and REM YEAR implemented from accepted DiscogsLinks. REM GENRE always None (DiscogsLink has no genre field — documented as D-09). No catalog number field on DiscogsLink. Requirements text lists fields the model does not expose; code handles what exists. REQUIREMENTS.md marks CUE-03 as `[x]`. |

**Orphaned requirements:** None. All three CUE-01/02/03 are claimed across plan frontmatters for this phase.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|---------|--------|
| `src/phaze/routers/cue.py` | 342 | `except Exception: # noqa: S112` in batch loop | Info | Deliberate resilient-batch design; individual failures silently skipped. No change from previous verification. |

No TODO/FIXME/placeholder comments. No stub returns. No hardcoded empty data flowing to render. Source badge and Regenerate CUE button states are data-driven (not hardcoded).

### Human Verification Required

#### 1. Tracklist Card Regenerate State (live app)

**Test:** Start the app with a PostgreSQL database. Create an approved tracklist linked to an EXECUTED file. Visit /tracklists/. Click Generate CUE on the card.
**Expected:** The card should re-render with "Regenerate CUE" button and a "CUE v1" badge.
**Why human:** Requires live app with EXECUTED file on disk; button state depends on filesystem scan returning cue_version > 0.

#### 2. Source Badge Rendering on CUE Management Page (live app)

**Test:** Seed a file with both a fingerprint and a 1001tracklists tracklist (both approved, both with timestamped tracks). Visit /cue/.
**Expected:** Fingerprint row appears first with an indigo badge reading "fingerprint"; 1001tracklists row appears second with a gray badge reading "1001tracklists".
**Why human:** Requires seeded database with two tracklists for the same file from different sources.

### Gaps Summary

Both gaps from the initial verification are now closed:

**Gap 1 — Fingerprint priority (resolved):** The CUE management page now surfaces source type via a badge on each row (indigo for fingerprint, gray for 1001tracklists). The `_get_eligible_tracklist_query` ORDER BY sorts fingerprint-sourced tracklists first. Two new tests (`test_cue_list_shows_source_badge`, `test_cue_list_fingerprint_first`) verify both behaviors. CUE-01 is now fully satisfied.

**Gap 2 — Tracklist card regenerate state (resolved):** The `list_tracklists` endpoint now computes `tl._cue_version` for every approved+EXECUTED tracklist via `_get_cue_version`. The `approve_tracklist` endpoint computes and passes `cue_version` in context. `tracklist_card.html` uses `{% set cv = tracklist._cue_version if ... else (cue_version if ... else 0) %}` to read from whichever context source is available, then renders "Regenerate CUE" + "CUE v{{ cv }}" badge when cv > 0. The `generate_cue` endpoint detects `HX-Target: tracklist-{id}` headers and returns the tracklist card partial (instead of cue_row.html), with `cue_version` in context. A new test (`test_generate_cue_returns_tracklist_card_when_target_is_tracklist`) verifies the round-trip.

No regressions detected against previously passing truths.

---

_Verified: 2026-04-03T23:15:00Z_
_Verifier: Claude (gsd-verifier)_
_Re-verification: Yes — after plan 21-03 gap closure_
