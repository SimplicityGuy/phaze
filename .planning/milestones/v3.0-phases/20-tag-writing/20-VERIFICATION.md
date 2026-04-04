---
phase: 20-tag-writing
verified: 2026-04-03T19:10:00Z
status: verified
score: 4/4 must-haves verified
re_verification:
  previous_status: gaps_found
  previous_score: 3/4
  gaps_closed:
    - "Write Tags button in collapsed table row now computes proposed tags server-side (router fallback when form data is empty)"
    - "Post-write row update now targets main file row by stable ID (#row-{file_id}) instead of broken 'closest tr'"
  gaps_remaining: []
  regressions: []
human_verification:
  - test: "Confirm Write Tags button from the collapsed table row POSTs with correct tag values and the UI updates"
    expected: "Clicking Write Tags on a pending file in the table (without expanding) should write the computed proposed tags, show 'Done' in the action column, and update the status badge to 'completed'"
    why_human: "Automated tests mock execute_tag_write at service level. Browser interaction required to confirm the HTMX POST fires, the server fallback triggers, the response replaces the correct row (outerHTML swap on #row-{file_id}), and the detail row is cleared via OOB"
  - test: "Confirm that after clicking Write Tags from the expanded comparison panel, the main file row and detail row both update correctly"
    expected: "The main row should show 'completed' status badge and 'Done' in the action column; the expanded comparison panel should collapse/clear via OOB swap"
    why_human: "HTMX OOB swap behavior and Alpine.js x-data state interaction when the main tr is replaced require browser DOM inspection to verify correct rendering"
---

# Phase 20: Tag Writing Verification Report

**Phase Goal:** Users can push corrected metadata from Postgres into destination file tags with full review and audit trail
**Verified:** 2026-04-03T19:10:00Z
**Status:** human_needed
**Re-verification:** Yes — after gap closure (plan 20-03)

## Re-verification Summary

Previous verification (2026-04-03T18:45:00Z) found 2 gaps:

1. **Gap 1 (Blocker):** `hx-include="[name^='{{ file.id }}']"` in `tag_list.html` — broken CSS attribute selector, never matched any inputs. Write Tags button in collapsed row POSTed empty form data, silently writing nothing.
2. **Gap 2 (Warning):** `hx-target="closest tr"` in `tag_comparison.html` — targeted detail row, not main file row. Post-write update left the main row showing stale "pending" state.

Plan 20-03 was executed to close both gaps. Both fixes are verified in the actual codebase.

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | User can view proposed vs current tags side-by-side on a review page before approving a tag write | VERIFIED | `compare_tags` endpoint + `tag_comparison.html` renders 3-column Field/Current/Proposed table for all 6 CORE_FIELDS with inline edit support |
| 2 | User can write corrected tags to destination copies across all supported formats (MP3, M4A, OGG, OPUS, FLAC) and the system verifies correctness by re-reading the file | VERIFIED | `tag_writer.py` implements format-aware write + verify-after-write. Router fallback ensures both collapsed-row button and comparison panel write real tags. `hx-include` removed; router computes proposed tags when form data is empty. |
| 3 | All tag writes appear in an append-only audit log with before/after snapshots | VERIFIED | `TagWriteLog` model with JSONB `before_tags`/`after_tags`; `execute_tag_write` always creates a log entry regardless of success/failure |
| 4 | Tag writes are blocked on non-EXECUTED files (only destination copies are writable) | VERIFIED | Double-gated: `execute_tag_write` raises `ValueError` if `file_record.state != FileState.EXECUTED`; router also returns 400 at line 301 before calling the service |

**Score:** 4/4 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/models/tag_write_log.py` | TagWriteLog audit model and TagWriteStatus enum | VERIFIED | `TagWriteStatus(StrEnum)` with COMPLETED/FAILED/DISCREPANCY; JSONB before_tags/after_tags, file_id FK; append-only |
| `alembic/versions/011_add_tag_write_log.py` | Migration creating tag_write_log table | VERIFIED | revision="011", down_revision="010"; table + two indexes; includes downgrade |
| `src/phaze/services/tag_proposal.py` | compute_proposed_tags cascade merge and parse_filename | VERIFIED | 94 lines; CORE_FIELDS tuple; 3-layer cascade (filename < FileMetadata < tracklist) |
| `src/phaze/services/tag_writer.py` | write_tags, verify_write, execute_tag_write functions | VERIFIED | 216 lines; format-aware write maps for ID3/Vorbis/MP4; NFC normalization in verify_write; EXECUTED state gate |
| `src/phaze/routers/tags.py` | Tag review page endpoints with fallback for empty form data | VERIFIED | 366 lines; 5 endpoints; server-side fallback at lines 320-324 computes proposed tags when form data is empty |
| `src/phaze/templates/tags/list.html` | Full tag review page extending base.html | VERIFIED | Extends base.html; 3-stat header; includes tag_list and pagination partials |
| `src/phaze/templates/tags/partials/tag_list.html` | Main file rows with stable IDs, no broken hx-include | VERIFIED | Line 13: `id="row-{{ file.id }}"` on main tr; Write Tags button has `hx-target="#row-{{ file.id }}"` and `hx-swap="outerHTML"`; no hx-include present |
| `src/phaze/templates/tags/partials/tag_comparison.html` | Write Tags form targeting main row by stable ID | VERIFIED | Line 35: `hx-target="#row-{{ file.id }}"` — targets main row by ID, not "closest tr" |
| `src/phaze/templates/tags/partials/tag_row.html` | Post-write response with matching row ID and OOB detail row clear | VERIFIED | Line 1: `id="row-{{ file.id }}"` on main tr; line 50-52: `<tr id="detail-{{ file.id }}" hx-swap-oob="outerHTML">` clears detail row |
| `src/phaze/templates/tags/partials/inline_edit.html` | Editable input for proposed tag field | VERIFIED | `hx-put` on blur/enter; `hx-target="closest td"` |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `src/phaze/services/tag_writer.py` | `src/phaze/services/metadata.py` | `extract_tags` for verify-after-write | WIRED | Line 20: import; used at lines 134 and 156 |
| `src/phaze/services/tag_writer.py` | `src/phaze/models/tag_write_log.py` | creates TagWriteLog entries | WIRED | Line 19: import; `TagWriteLog(...)` at line 204 |
| `src/phaze/services/tag_proposal.py` | `src/phaze/models/metadata.py` | reads FileMetadata fields | WIRED | TYPE_CHECKING import; `getattr(file_metadata, field, None)` iterates CORE_FIELDS |
| `src/phaze/routers/tags.py` | `src/phaze/services/tag_writer.py` | `execute_tag_write` on POST /tags/{file_id}/write | WIRED | Line 20: import; called at line 332 |
| `src/phaze/routers/tags.py` | `src/phaze/services/tag_proposal.py` | `compute_proposed_tags` fallback when form data empty | WIRED | Lines 322-323: `computed = compute_proposed_tags(...)`; `tags = {k: v for k, v in computed.items() if v is not None}` |
| `src/phaze/templates/base.html` | `/tags/` | Tags nav tab link | WIRED | `<a href="/tags/"` with active-page class |
| `src/phaze/main.py` | `src/phaze/routers/tags.py` | `app.include_router` | WIRED | Line 12: import; line 41: `app.include_router(tags.router)` |
| `src/phaze/templates/tags/partials/tag_list.html` | `POST /tags/{file_id}/write` | Write Tags button in collapsed row | WIRED | Lines 52-56: `hx-post="/tags/{{ file.id }}/write"`, `hx-target="#row-{{ file.id }}"`, `hx-swap="outerHTML"` — no hx-include |
| `src/phaze/templates/tags/partials/tag_comparison.html` | `#row-{{ file.id }}` | Write Tags form targets main row by ID | WIRED | Line 35: `hx-target="#row-{{ file.id }}"` — fixes previous "closest tr" bug |
| `src/phaze/templates/tags/partials/tag_row.html` | `#detail-{{ file.id }}` | OOB swap clears comparison panel after write | WIRED | Line 50: `<tr id="detail-{{ file.id }}" hx-swap-oob="outerHTML">` |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|--------------------|--------|
| `src/phaze/routers/tags.py` | `file_records` | `select(FileRecord).where(FileRecord.state == FileState.EXECUTED)` | Yes — SQLAlchemy query against files table | FLOWING |
| `src/phaze/routers/tags.py` | `tags` (fallback path) | `compute_proposed_tags(file_record.file_metadata, tracklist, ...)` | Yes — reads DB-loaded FileMetadata; no longer falls through as empty dict | FLOWING |
| `src/phaze/services/tag_writer.py` | `before_tags` | `extract_tags(file_path)` | Yes — mutagen reads real file tags | FLOWING |
| `src/phaze/services/tag_writer.py` | `discrepancies` | `verify_write(file_path, proposed_tags)` | Yes — real disk re-read with NFC normalization | FLOWING |

### Behavioral Spot-Checks

Step 7b: SKIPPED — requires PostgreSQL `phaze_test` database not available outside Docker/CI. Regression tests in `tests/test_routers/test_tags.py` cover the gap-closure behaviors (lines 200-242: `test_write_tags_empty_body_uses_fallback` and `test_write_tags_response_has_row_id`); these will run in CI.

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| TAGW-01 | 20-01, 20-02, 20-03 | User can write corrected tags to destination file copies (never originals) with format-aware encoding | SATISFIED | `write_tags` uses `_WRITE_ID3_MAP`, `_WRITE_VORBIS_MAP`, `_WRITE_MP4_MAP`; `execute_tag_write` uses `file_record.current_path`; EXECUTED gate blocks non-destination files; collapsed-row button now writes real tags via server-side fallback |
| TAGW-02 | 20-01 | Tag writes are verified by re-reading the file after write, with discrepancies flagged | SATISFIED | `verify_write` re-reads with `extract_tags`, NFC normalization, returns discrepancy dict; `execute_tag_write` sets `TagWriteStatus.DISCREPANCY` when non-empty |
| TAGW-03 | 20-01 | All tag writes logged in append-only TagWriteLog audit table | SATISFIED | `TagWriteLog` model; `execute_tag_write` always creates a log entry including on failure; migration 011 creates the table |
| TAGW-04 | 20-02, 20-03 | Tag review page shows proposed vs current tags side-by-side before user approves the write | SATISFIED | `/tags/{file_id}/compare` renders `tag_comparison.html` with three-column Field/Current/Proposed view; write path in both collapsed and expanded states now correct |

All 4 TAGW requirements satisfied. No orphaned requirements — plans 20-01, 20-02, and 20-03 together declare all four IDs; all four appear in REQUIREMENTS.md mapped to Phase 20.

### Anti-Patterns Found

No blockers or warnings. Previous blockers from gap closure confirmed resolved:

| File | Previous Issue | Resolution | Status |
|------|---------------|------------|--------|
| `src/phaze/templates/tags/partials/tag_list.html` | `hx-include="[name^='{{ file.id }}']"` — selector never matched | `hx-include` attribute removed entirely; router computes fallback server-side | RESOLVED |
| `src/phaze/templates/tags/partials/tag_comparison.html` | `hx-target="closest tr"` — targeted detail row, not main row | Changed to `hx-target="#row-{{ file.id }}"` | RESOLVED |

No TODO/FIXME/placeholder comments found in any phase 20 source files. No stub return patterns.

### Human Verification Required

#### 1. Write Tags button in collapsed table row

**Test:** Load `/tags/` page with at least one EXECUTED file showing pending status. Do NOT expand the row. Click the "Write Tags" button directly from the action column.
**Expected:** Tags are computed server-side from the file's FileMetadata and tracklist, written to the file, the main row replaces itself (outerHTML swap on `#row-{file_id}`) showing "completed" status badge and "Done" in the action column, and a toast notification appears.
**Why human:** Automated tests mock `execute_tag_write` at service level and verify the fallback is triggered but cannot confirm the HTMX DOM swap happens correctly, that the detail row is cleared via OOB, and that Alpine.js x-data state on the replaced row initializes correctly.

#### 2. Write Tags from expanded comparison panel

**Test:** Load `/tags/`, expand a file row to show the comparison panel, then click "Write Tags" from the comparison panel.
**Expected:** The main row replaces itself via `#row-{file_id}` outerHTML swap showing "completed" status; the detail row is cleared via OOB swap (comparison panel collapses); a toast notification appears.
**Why human:** HTMX OOB swap with Alpine.js x-data on the newly inserted main row requires browser rendering to confirm correct DOM state — specifically that the `x-data="{ expanded: false }"` initializes properly on the swapped row.

### Gaps Summary

No gaps remaining. All four observable truths are verified. Both previous gaps are closed and confirmed by direct file inspection:

- Gap 1 closed: `hx-include` removed from `tag_list.html`; router `write_file_tags` has explicit fallback at lines 320-324 that computes proposed tags when `tags` dict is empty after form parsing.
- Gap 2 closed: `tag_comparison.html` line 35 uses `hx-target="#row-{{ file.id }}"`. `tag_list.html` line 13 adds `id="row-{{ file.id }}"` to the main tr. `tag_row.html` line 1 carries the matching `id="row-{{ file.id }}"` in the response. `tag_row.html` line 50 clears the detail row via `hx-swap-oob="outerHTML"`.

Two items remain for human verification (browser behavior of HTMX OOB swaps with Alpine.js). These are not blockers — the code is correctly wired. Human verification confirms the end-user experience.

---

_Verified: 2026-04-03T19:10:00Z_
_Verifier: Claude (gsd-verifier)_
