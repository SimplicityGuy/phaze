# Phase 7: Approval Workflow UI - Context

**Gathered:** 2026-03-28
**Status:** Ready for planning

<domain>
## Phase Boundary

Admin web interface for reviewing, approving, and rejecting AI-generated filename proposals. Paginated table view with status filtering, bulk actions, and keyboard shortcuts. This is the first UI phase -- no existing templates, static files, or Jinja2 setup exists yet. Uses HTMX + Jinja2 + Tailwind CSS (CDN) + Alpine.js per CLAUDE.md stack decisions.

</domain>

<decisions>
## Implementation Decisions

### Page Layout & Proposal Display
- **D-01:** Table rows layout -- dense table with columns for original filename, proposed filename, confidence, status, and action buttons. Optimized for scanning large numbers of proposals.
- **D-02:** Essential columns only in the table view: original filename, proposed filename, confidence score (color-coded), status badge, approve/reject buttons. No extracted metadata columns in the default view.
- **D-03:** Rows are expandable via click -- inline detail panel shows LLM reasoning, extracted metadata from context_used JSONB, confidence breakdown, original path. HTMX lazy-loads the detail content.
- **D-04:** Confidence scores displayed as color-coded numbers: green (high), yellow (medium), red (low). Simple and immediately highlights items needing attention.

### Approve/Reject Interaction
- **D-05:** Instant action with undo -- clicking approve/reject applies immediately via HTMX. A brief toast notification appears with an "Undo" button (5-second window). Optimized for reviewing thousands of proposals quickly.
- **D-06:** Bulk actions supported -- checkboxes on each row, "Select all on page" checkbox in header, bulk approve/reject buttons appear when items are selected. Essential for 200K file scale.
- **D-07:** After approve/reject, row stays in place with updated status badge (green for approved, red for rejected). Buttons update accordingly. Table remains stable during review.
- **D-08:** Keyboard shortcuts for power-reviewing: arrow keys to navigate rows, 'a' to approve, 'r' to reject, 'e' to expand details. Implemented via Alpine.js.

### Filtering & Navigation
- **D-09:** Tab bar for status filtering -- horizontal tabs: All | Pending | Approved | Rejected. Each tab shows count badge. HTMX reloads the table on tab click. Default view: Pending.
- **D-10:** Text search box that filters proposals by original or proposed filename. HTMX-powered with debounce. Essential for finding specific files among 200K.
- **D-11:** Numbered page pagination at bottom: previous/next + page numbers. Configurable page size (25/50/100). Stable URLs for bookmarking.
- **D-12:** Sort options: confidence ascending (low first -- items needing most review) as default. Also sortable by original filename and proposed filename via column header clicks.

### Empty & Edge States
- **D-13:** No proposals state: centered helpful message -- "No proposals yet. Run the AI proposal generation pipeline to get started." Guides user to next action.
- **D-14:** All reviewed state: celebration message -- "All caught up! No pending proposals." with link to view approved/rejected. Positive feedback for completing the review queue.
- **D-15:** Summary stats bar at top of page: Total proposals, Pending count, Approved count, Rejected count, average confidence. Quick overview before diving into the table.

### Claude's Discretion
- Jinja2 template structure and organization (base template, partials, etc.)
- Tailwind CSS styling choices and color palette
- HTMX patterns for partial page swaps (hx-get, hx-swap, hx-target)
- Alpine.js keyboard shortcut implementation details
- Toast notification implementation (Alpine.js component or HTMX OOB swap)
- Confidence color thresholds (what counts as high/medium/low)
- Page size default (25, 50, or 100)
- Search debounce timing
- Undo mechanism implementation (delayed DB write vs immediate write + rollback)
- Static file serving strategy (FastAPI StaticFiles mount vs CDN-only)

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project Configuration
- `CLAUDE.md` -- Development setup, code quality rules, HTMX + Jinja2 + Tailwind CSS + Alpine.js stack decision
- `.planning/PROJECT.md` -- Project vision, constraints, human-in-the-loop approval requirement
- `.planning/REQUIREMENTS.md` -- APR-01 (paginated list), APR-02 (approve/reject), APR-03 (filter by status)

### Existing Code
- `src/phaze/models/proposal.py` -- RenameProposal model (proposed_filename, proposed_path, confidence, context_used JSONB, reason, status), ProposalStatus enum (PENDING/APPROVED/REJECTED), ix_proposals_status index
- `src/phaze/models/file.py` -- FileRecord model (original_path, original_filename, file_type)
- `src/phaze/models/analysis.py` -- AnalysisResult model (bpm, musical_key, mood, style, features JSONB)
- `src/phaze/main.py` -- FastAPI app factory with lifespan, router registration pattern
- `src/phaze/config.py` -- Settings with pydantic-settings (env vars, .env file)
- `src/phaze/database.py` -- Async SQLAlchemy engine and get_session dependency
- `src/phaze/routers/health.py` -- Example router pattern (APIRouter, Depends)

### Prior Phase Context
- `.planning/phases/06-ai-proposal-generation/06-CONTEXT.md` -- AI proposal decisions (naming format, LLM provider, batch strategy, metadata extraction into context_used JSONB)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `RenameProposal` model with ProposalStatus enum and status index -- ready for querying by status
- `context_used` JSONB column stores extracted metadata (artist, event, venue, date, source_type) -- available for expanded row details
- `get_session` dependency for async DB access in routers
- FastAPI app factory pattern with `include_router` -- new approval router plugs in directly
- pydantic-settings `Settings` class for any new config (e.g., default page size)

### Established Patterns
- APIRouter with tags for endpoint grouping
- Async SQLAlchemy 2.0 queries with `select()` and `session.execute()`
- FastAPI dependency injection via `Depends(get_session)`
- Lifespan context manager for startup/shutdown

### Integration Points
- New `src/phaze/routers/proposals.py` for approval UI endpoints (HTML responses)
- New `src/phaze/templates/` directory for Jinja2 templates
- New `src/phaze/static/` directory (if needed for local CSS/JS, though CDN preferred)
- Add Jinja2 dependency to pyproject.toml
- Configure Jinja2Templates in FastAPI app
- Register proposal router in `main.py` app factory
- Mount static files directory if needed

</code_context>

<specifics>
## Specific Ideas

- Default sort by confidence ascending surfaces low-confidence proposals first -- these are the ones most likely to need human correction or rejection.
- Keyboard shortcuts (a/r/e + arrow keys) modeled after email triage workflows -- fast power-reviewing for working through thousands of proposals.
- Tab bar with count badges gives instant visibility into review progress without a separate dashboard page.
- Summary stats bar at top provides at-a-glance overview of the entire collection's review state.
- Undo on approve/reject reduces anxiety about misclicks when rapidly reviewing.

</specifics>

<deferred>
## Deferred Ideas

- **APR-04 (Batch approval with smart grouping):** Group proposals by artist/album/event for batch review. Deferred to v2 per REQUIREMENTS.md. The bulk select + approve on current page covers basic batch needs for v1.
- **APR-05 (Inline editing of proposals):** Edit proposed filenames before approval. Deferred to v2 per REQUIREMENTS.md.
- **EXE-05 (Progress tracking / job status visibility):** Show AI generation pipeline status in the UI. Deferred to v2.

None -- discussion stayed within phase scope.

</deferred>

---

*Phase: 07-approval-workflow-ui*
*Context gathered: 2026-03-28*
