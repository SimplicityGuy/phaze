# Phase 22: Tracklist Integration Fixes - Context

**Gathered:** 2026-04-03
**Status:** Ready for planning

<domain>
## Phase Boundary

Close two audit gaps from v3.0 milestone: (1) the "Bulk-link All" button on tracklist cards is unreachable because `has_candidates` is never passed to the template context, and (2) the CUE version badge disappears after undo-link because `_render_tracklist_list` doesn't compute `_cue_version`. Both are wiring bugs in the tracklist router — no new features, no new models, no new templates.

</domain>

<decisions>
## Implementation Decisions

### Bulk-link Button Visibility
- **D-01:** Compute `has_candidates` on every tracklist card render — not just after match_discogs, but also in approve, accept, dismiss, and list view endpoints. Ensures the button appears/disappears correctly in all user flows.
- **D-02:** `has_candidates` is true when at least one DiscogsLink with `status='candidate'` exists for any track in the tracklist. Button disappears after bulk-link (all candidates become 'accepted') and reappears if user re-matches.
- **D-03:** Query pattern: EXISTS subquery on DiscogsLink where track_id IN (tracklist's track IDs) AND status='candidate'.

### CUE Badge After Link Operations
- **D-04:** Add `_cue_version` computation to `_render_tracklist_list` (the helper function at line 759), mirroring the same logic from the main tracklist list view (lines 99-109). This is the targeted fix for the undo-link case.
- **D-05:** Other card-level renders (approve, match_discogs, etc.) already pass `cue_version` explicitly and are working — no changes needed there.

### Testing Approach
- **D-06:** Integration tests only — test the HTMX endpoints end-to-end. Verify `has_candidates` appears in response context after match completion, and `_cue_version` persists in list response after undo-link.
- **D-07:** Add test cases to existing tracklist router test files rather than creating new test files.

### Claude's Discretion
- Exact placement of the has_candidates query within each endpoint (before or after other queries)
- Whether to use a shared helper for has_candidates or inline it per endpoint
- Test fixture setup (existing test patterns should be followed)

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Bug Location — Tracklist Router
- `src/phaze/routers/tracklists.py` — All affected endpoints: match_discogs (L585), approve_tracklist (L495), accept_discogs_link (L626), dismiss_discogs_link (L666), bulk_link_discogs (L696), undo_link (L407), _render_tracklist_list (L759)

### Template — Bulk-link Button
- `src/phaze/templates/tracklists/partials/discogs_bulk_link.html` — Checks `has_candidates` variable (the unreachable guard)
- `src/phaze/templates/tracklists/partials/tracklist_card.html` — Includes bulk-link partial (L94), CUE version badge (L96-109)

### Data Models
- `src/phaze/models/discogs_link.py` — DiscogsLink model with status field ('candidate', 'accepted', 'dismissed')
- `src/phaze/models/tracklist.py` — Tracklist, TracklistTrack models
- `src/phaze/models/file.py` — FileRecord model with FileState enum, current_path field

### CUE Version Helper
- `src/phaze/routers/cue.py` — `_get_cue_version()` function (L99), already imported by tracklists.py

### Existing Tests
- `tests/` — Existing tracklist router test files (follow patterns for new integration tests)

### Requirements
- `.planning/REQUIREMENTS.md` — DISC-04 (bulk-link entire tracklist)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `_get_cue_version(file_path)`: Already imported in tracklists.py from cue router — reuse for _render_tracklist_list fix
- `DiscogsLink` model: Already imported in tracklists.py — query for has_candidates

### Established Patterns
- Dynamic ORM attributes: `tl._cue_version` and `tl._track_count` set via `# type: ignore[attr-defined]` pattern
- Template context: `cue_version` passed as explicit context var for card renders, `_cue_version` as ORM attr for list renders
- HTMX partials: All card renders return `tracklist_card.html`, list renders return `tracklist_list.html`

### Integration Points
- Main list view (L99-109): Already computes `_cue_version` — pattern to mirror in `_render_tracklist_list`
- Every endpoint returning `tracklist_card.html`: Needs `has_candidates` added to context dict
- `_render_tracklist_list`: Needs both `_cue_version` computation added and `has_candidates` per-tracklist

</code_context>

<specifics>
## Specific Ideas

No specific requirements — straightforward wiring fixes following existing patterns.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 22-tracklist-integration-fixes*
*Context gathered: 2026-04-03*
