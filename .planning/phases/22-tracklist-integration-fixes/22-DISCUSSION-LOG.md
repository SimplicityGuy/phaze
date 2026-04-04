# Phase 22: Tracklist Integration Fixes - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-04-03
**Phase:** 22-tracklist-integration-fixes
**Areas discussed:** Bulk-link button visibility, CUE badge after link ops, Testing approach

---

## Bulk-link Button Visibility

### Q1: When should has_candidates be computed?

| Option | Description | Selected |
|--------|-------------|----------|
| Every card render | Query DiscogsLink for 'candidate' status whenever tracklist_card.html is rendered (match_discogs, approve, accept, dismiss, and list views). Ensures button appears/disappears correctly in all flows. | ✓ |
| Only after match completes | Only compute has_candidates in the match_discogs endpoint response. Simpler but button won't appear if user navigates away and comes back. | |
| List + match endpoints only | Compute in _render_tracklist_list and match_discogs. Covers main list view and post-match, but not approve/accept/dismiss card re-renders. | |

**User's choice:** Every card render (Recommended)
**Notes:** Ensures consistent behavior across all user flows.

### Q2: What counts as 'has candidates'?

| Option | Description | Selected |
|--------|-------------|----------|
| Any 'candidate' status links exist | At least one DiscogsLink with status='candidate' for any track in the tracklist. Button disappears after bulk-link. | ✓ |
| Any non-dismissed links exist | At least one DiscogsLink with status != 'dismissed'. Button stays visible even after bulk-link. | |
| Any links exist at all | Show button whenever DiscogsLink rows exist regardless of status. | |

**User's choice:** Any 'candidate' status links exist
**Notes:** Button appears only when actionable, disappears after all candidates are accepted.

---

## CUE Badge After Link Ops

### Q1: How should _cue_version be handled consistently?

| Option | Description | Selected |
|--------|-------------|----------|
| Add to _render_tracklist_list | Mirror the same _cue_version computation from the main list view (line 99-109) into _render_tracklist_list (line 759). Targeted fix for the undo-link case. | ✓ |
| Extract shared helper | Create a helper function for both list render paths. DRYer but more refactoring. | |
| Compute in all card renders too | Add _cue_version to every endpoint rendering tracklist_card.html. Most thorough but touches many endpoints. | |

**User's choice:** Add to _render_tracklist_list (Recommended)
**Notes:** Targeted fix for the specific bug path. Other card renders already pass cue_version explicitly.

---

## Testing Approach

### Q1: What testing approach?

| Option | Description | Selected |
|--------|-------------|----------|
| Integration tests only | Test HTMX endpoints end-to-end: verify has_candidates in response context and _cue_version in list response. | ✓ |
| Unit + integration | Unit test the has_candidates query logic separately, plus integration tests. | |
| Extend existing test files | Add test cases to existing tracklist router test files. | |

**User's choice:** Integration tests only (Recommended)
**Notes:** Computation is trivial (EXISTS query), integration tests directly validate the bugs are fixed.

---

## Claude's Discretion

- Exact placement of has_candidates query within each endpoint
- Whether to use shared helper for has_candidates or inline
- Test fixture setup following existing patterns

## Deferred Ideas

None — discussion stayed within phase scope.
