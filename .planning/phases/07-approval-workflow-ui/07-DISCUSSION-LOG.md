# Phase 7: Approval Workflow UI - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md -- this log preserves the alternatives considered.

**Date:** 2026-03-28
**Phase:** 07-approval-workflow-ui
**Areas discussed:** Page layout & proposal display, Approve/reject interaction, Filtering & navigation, Empty & edge states

---

## Page Layout & Proposal Display

### How should proposals be displayed?

| Option | Description | Selected |
|--------|-------------|----------|
| Table rows | Dense table with columns for original filename, proposed filename, confidence, status, actions. Efficient for scanning 200K proposals. | ✓ |
| Cards | Each proposal in its own card showing original to proposed with metadata details. More visual but lower density. | |
| Compact list | Minimal rows showing original to proposed with inline approve/reject. Maximum density, minimal metadata. | |

**User's choice:** Table rows
**Notes:** Good fit for scanning a large collection.

### What metadata should be visible in the table?

| Option | Description | Selected |
|--------|-------------|----------|
| Essential only | Original filename, proposed filename, confidence score, status, approve/reject buttons. Details on row expand. | ✓ |
| With extracted metadata | Add columns for artist, event/venue, date from context_used JSONB. More info at a glance. | |
| With file context | Add original path and file type columns too. Maximum info. | |

**User's choice:** Essential only
**Notes:** None.

### Should rows be expandable?

| Option | Description | Selected |
|--------|-------------|----------|
| Click to expand | Clicking expands inline detail panel with LLM reasoning, metadata, original path. HTMX lazy-loads. | ✓ |
| Hover tooltip | Hovering shows popover with key details. | |
| No expansion | Table columns sufficient. | |

**User's choice:** Click to expand
**Notes:** None.

### How should confidence scores be displayed?

| Option | Description | Selected |
|--------|-------------|----------|
| Color-coded number | 0.92 in green, 0.55 in yellow, 0.31 in red. Simple, scannable. | ✓ |
| Progress bar + number | Small horizontal bar with number beside it. | |
| Just the number | Plain numeric display. | |

**User's choice:** Color-coded number
**Notes:** None.

---

## Approve/Reject Interaction

### Should actions require confirmation?

| Option | Description | Selected |
|--------|-------------|----------|
| Instant with undo | Click and it happens via HTMX. Toast with "Undo" button (5-second window). | ✓ |
| Instant, no undo | One click, done. Status changeable later. | |
| Confirm dialog | Modal or inline confirmation before each action. | |

**User's choice:** Instant with undo
**Notes:** None.

### Should there be bulk approve/reject?

| Option | Description | Selected |
|--------|-------------|----------|
| Select + bulk action | Checkboxes on rows, "Select all" in header, bulk approve/reject buttons. | ✓ |
| No bulk actions in v1 | Individual only. APR-04 deferred to v2. | |
| Approve all on page | Single button for everything visible. | |

**User's choice:** Select + bulk action
**Notes:** Essential for 200K files.

### Row behavior after action?

| Option | Description | Selected |
|--------|-------------|----------|
| Stay with updated status | Row stays, status badge changes color. Table remains stable. | ✓ |
| Fade out and remove | Row fades, next fills in. Keeps view showing pending only. | |
| You decide | Claude picks best approach. | |

**User's choice:** Stay with updated status
**Notes:** None.

### Keyboard shortcuts?

| Option | Description | Selected |
|--------|-------------|----------|
| Yes, basic shortcuts | Arrow keys navigate, 'a' approve, 'r' reject, 'e' expand. | ✓ |
| No shortcuts in v1 | Mouse/touch only. | |
| You decide | Claude decides. | |

**User's choice:** Yes, basic shortcuts
**Notes:** None.

---

## Filtering & Navigation

### Status filtering style?

| Option | Description | Selected |
|--------|-------------|----------|
| Tab bar | Horizontal tabs: All, Pending, Approved, Rejected with count badges. Default: Pending. | ✓ |
| Dropdown select | Dropdown menu to pick status. | |
| Toggle buttons | Three toggles that can combine. | |

**User's choice:** Tab bar
**Notes:** None.

### Text search?

| Option | Description | Selected |
|--------|-------------|----------|
| Yes, search box | Text input filtering by original or proposed filename. HTMX with debounce. | ✓ |
| No search in v1 | Pagination and status filters only. | |
| You decide | Claude decides. | |

**User's choice:** Yes, search box
**Notes:** Essential for finding specific files among 200K.

### Pagination style?

| Option | Description | Selected |
|--------|-------------|----------|
| Numbered pages | Classic page numbers, configurable page size (25/50/100), stable URLs. | ✓ |
| Infinite scroll | Rows load on scroll. No page breaks. | |
| Load more button | Manual trigger to load next batch. | |

**User's choice:** Numbered pages
**Notes:** None.

### Sort options?

| Option | Description | Selected |
|--------|-------------|----------|
| Confidence + filename | Default: confidence ascending (low first). Also sortable by filename. Column header toggle. | ✓ |
| Multiple columns | All columns sortable. | |
| You decide | Claude decides. | |

**User's choice:** Confidence + filename
**Notes:** Low confidence first surfaces items needing most review.

---

## Empty & Edge States

### No proposals state?

| Option | Description | Selected |
|--------|-------------|----------|
| Helpful empty state | Centered message guiding user to run AI pipeline. | ✓ |
| Empty table | Table headers with no rows. | |
| You decide | Claude decides. | |

**User's choice:** Helpful empty state
**Notes:** None.

### All reviewed state?

| Option | Description | Selected |
|--------|-------------|----------|
| Celebration state | "All caught up!" with link to view approved/rejected. | ✓ |
| Same as no-proposals | Generic empty message. | |
| You decide | Claude decides. | |

**User's choice:** Celebration state
**Notes:** None.

### Summary stats bar?

| Option | Description | Selected |
|--------|-------------|----------|
| Yes, summary bar | Top of page: Total, Pending, Approved, Rejected counts + avg confidence. | ✓ |
| No stats | Tab counts sufficient. | |
| You decide | Claude decides. | |

**User's choice:** Yes, summary bar
**Notes:** None.

---

## Claude's Discretion

- Jinja2 template structure and organization
- Tailwind CSS styling choices and color palette
- HTMX patterns for partial page swaps
- Alpine.js keyboard shortcut implementation details
- Toast notification implementation
- Confidence color thresholds
- Page size default
- Search debounce timing
- Undo mechanism implementation
- Static file serving strategy

## Deferred Ideas

- APR-04: Batch approval with smart grouping (v2)
- APR-05: Inline editing of proposals (v2)
- EXE-05: Progress tracking / job status visibility in UI (v2)
