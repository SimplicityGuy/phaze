# Phase 14: Duplicate Resolution UI - Context

**Gathered:** 2026-03-31
**Status:** Ready for planning

<domain>
## Phase Boundary

Admin page for reviewing SHA256 duplicate groups, comparing file quality side-by-side, and resolving duplicates through a human-in-the-loop workflow. User selects the canonical file per group, non-canonical files are soft-deleted (state change only). Covers DEDUP-01 through DEDUP-04.

</domain>

<decisions>
## Implementation Decisions

### Group Display & Comparison Layout
- **D-01:** Card-per-group layout on the main page. Each card shows truncated SHA256 hash, file count, and pre-selected canonical file with scoring rationale. Cards expand inline (HTMX) to reveal a comparison table of all files in the group.
- **D-02:** Expanded comparison table columns: original path, file size (human-readable), file type, bitrate, duration, tag completeness badge (Full/Partial/None), and actual artist/title/album tag values.
- **D-03:** Best value per column is highlighted (green or bold). Highest bitrate, most complete tags, etc. are visually distinguished so the winner on each metric is instantly clear.

### Resolution Workflow & Actions
- **D-04:** Radio buttons per file in expanded group. One pre-selected (auto-scored best). User changes selection if needed, clicks "Resolve Group" to confirm. Single canonical selection per group.
- **D-05:** Soft delete via state change. Non-canonical files are marked DUPLICATE_RESOLVED in FileRecord.state. No filesystem operations — consistent with human-in-the-loop constraint. Actual file deletion is a future concern.
- **D-06:** Bulk resolution via "Accept All" button. Resolves all unresolved groups on the current page using auto-selected canonical files. Undo toast for 10 seconds. Essential for scale.
- **D-07:** Resolved groups disappear with undo toast (10 seconds). Page shows only unresolved groups. Consistent with approve/reject pattern from proposals page (Phase 7).

### Auto-Selection Scoring
- **D-08:** Bitrate-first ranking. Primary sort: highest bitrate wins. Tiebreaker 1: most complete tags. Tiebreaker 2: shortest path. Simple, predictable, and bitrate is the strongest quality indicator for audio files.
- **D-09:** Scoring rationale shown on card next to pre-selected file: "Best: highest bitrate (320kbps)" or "Best: most complete tags (5/6)". Builds trust in auto-selection without cluttering.

### Navigation & Integration
- **D-10:** Nav link position: Pipeline > Proposals > Preview > **Duplicates** > Audit Log. Groups file management tools together.
- **D-11:** Empty state: centered "No duplicates found" message with subtext "All files have unique content. Run a new scan to check again." Treats zero duplicates as a positive outcome.
- **D-12:** Summary stats header: "{N} duplicate groups - {M} total files - {X} MB recoverable". Recoverable = total size of non-canonical files across all groups.

### Claude's Discretion
- Pagination approach (page size, controls) — follow existing proposals page pattern
- HTMX swap targets and animation for card expand/collapse and resolve actions
- FileRecord state machine integration for DUPLICATE_RESOLVED state
- Toast/undo implementation details

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Existing Duplicate Detection
- `src/phaze/services/dedup.py` — `find_duplicate_groups()` and `count_duplicate_groups()` already implemented (Phase 3)
- `src/phaze/models/file.py` — FileRecord model with sha256_hash, original_path, file_size, file_type, state machine

### Data Sources for Comparison
- `src/phaze/models/metadata.py` — FileMetadata with artist, title, album, genre, bitrate, raw_tags for side-by-side comparison

### UI Patterns to Follow
- `src/phaze/templates/proposals/` — Established table/card patterns, expandable rows, HTMX partials
- `src/phaze/routers/proposals.py` — Router pattern with pagination, Jinja2Templates, Depends(get_session)
- `src/phaze/templates/base.html` — Navigation bar (add Duplicates link after Preview)
- `.planning/phases/07-approval-workflow-ui/07-CONTEXT.md` — UI decisions: expandable rows, instant action with undo toast, keyboard shortcuts

### Requirements
- `.planning/REQUIREMENTS.md` — DEDUP-01 through DEDUP-04 acceptance criteria

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `find_duplicate_groups()` in `dedup.py`: Already queries and groups files by sha256_hash with pagination. Returns grouped dicts with id, path, size, type. Needs extension for metadata join.
- `count_duplicate_groups()` in `dedup.py`: Already counts total groups. Reuse directly for stats header.
- Proposals router pattern: Jinja2Templates, APIRouter, pagination with limit/offset/page_size, HTMX partials for dynamic updates.
- Toast/undo pattern from proposals page (Phase 7).

### Established Patterns
- HTMX for dynamic updates (swap, trigger, out-of-band swaps)
- Tailwind CSS via CDN for styling
- Alpine.js for client-side interactivity (expand/collapse, radio selection)
- Jinja2 partials in `templates/{feature}/partials/` directory structure

### Integration Points
- `base.html` nav bar — add Duplicates link after Preview
- `main.py` — register new duplicates router
- `FileRecord.state` — needs DUPLICATE_RESOLVED state added to FileState enum
- `dedup.py` — extend to join FileMetadata for comparison data, add scoring logic, add resolve function

</code_context>

<specifics>
## Specific Ideas

No specific requirements — open to standard approaches following established UI patterns from Phase 7.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope

</deferred>

---

*Phase: 14-duplicate-resolution-ui*
*Context gathered: 2026-03-31*
