# Phase 14: Duplicate Resolution UI - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-03-31
**Phase:** 14-duplicate-resolution-ui
**Areas discussed:** Group display & comparison layout, Resolution workflow & actions, Auto-selection scoring logic, Navigation & integration

---

## Group Display & Comparison Layout

### Group layout

| Option | Description | Selected |
|--------|-------------|----------|
| Card per group | Each group is a card showing hash, file count, pre-selected canonical. Click to expand comparison table. | ✓ |
| Table rows grouped by hash | Dense table with alternating backgrounds and group headers. | |
| Dedicated comparison page per group | List page with summaries, click through to full-page comparison. | |

**User's choice:** Card per group
**Notes:** Consistent with expandable-row pattern from proposals table.

### Comparison columns

| Option | Description | Selected |
|--------|-------------|----------|
| Path, size, format | Original path, file size, file type from FileRecord. | ✓ |
| Bitrate & duration | Audio quality indicators from FileMetadata. | ✓ |
| Tag completeness | Full/Partial/None badge based on populated tag fields. | ✓ |
| Artist/title/album tags | Actual tag values for accuracy comparison. | ✓ |

**User's choice:** All four columns selected
**Notes:** Rich comparison view with all available data.

### Difference highlighting

| Option | Description | Selected |
|--------|-------------|----------|
| Best value highlighted | Highest bitrate, most complete tags get green/bold treatment. | ✓ |
| Differences only marked | Cells that differ get subtle background color. | |
| No highlighting | Plain table, manual comparison. | |

**User's choice:** Best value highlighted
**Notes:** None

---

## Resolution Workflow & Actions

### Keep action

| Option | Description | Selected |
|--------|-------------|----------|
| Radio buttons per file | Radio button per file, one pre-selected, "Resolve Group" button. | ✓ |
| Keep/Delete toggles per file | Toggle per file, allows keeping multiple. | |
| Click-to-select the canonical | Click row to select, then confirm. | |

**User's choice:** Radio buttons per file
**Notes:** None

### Delete mode

| Option | Description | Selected |
|--------|-------------|----------|
| Soft delete with state change | Mark as DUPLICATE_RESOLVED in FileRecord. No filesystem operations. | ✓ |
| Move to trash directory | Immediately move to .trash/ directory. | |
| Queue for deletion | Add to deletion queue, process separately. | |

**User's choice:** Soft delete with state change
**Notes:** Consistent with human-in-the-loop constraint.

### Bulk resolution

| Option | Description | Selected |
|--------|-------------|----------|
| Accept all auto-selections | "Accept All" button resolves all groups on page using auto-selections. Undo toast. | ✓ |
| Checkbox + bulk resolve | Checkboxes per group, select multiple, then bulk resolve. | |
| One at a time only | Individual resolution only. | |

**User's choice:** Accept all auto-selections
**Notes:** Essential for scale with potentially thousands of groups.

### After resolve

| Option | Description | Selected |
|--------|-------------|----------|
| Disappear with undo toast | Group fades out, 10-second undo toast. Shows only unresolved. | ✓ |
| Stay with resolved badge | Group stays with dimmed "Resolved" badge. | |
| Move to Resolved tab | Tab bar separating unresolved and resolved. | |

**User's choice:** Disappear with undo toast
**Notes:** Consistent with approve/reject pattern from proposals.

---

## Auto-Selection Scoring Logic

### Scoring weight

| Option | Description | Selected |
|--------|-------------|----------|
| Bitrate-first ranking | Primary: highest bitrate. Tiebreaker 1: most tags. Tiebreaker 2: shortest path. | ✓ |
| Weighted composite score | Numeric score: 50% bitrate + 30% tags + 20% path. | |
| Tag completeness first | Primary: most tags populated. Then bitrate, then path. | |

**User's choice:** Bitrate-first ranking
**Notes:** Simple, predictable. Bitrate is strongest quality indicator.

### Rationale visibility

| Option | Description | Selected |
|--------|-------------|----------|
| Show why on card | Brief reason next to pre-selected file on the card. | ✓ |
| Show in expanded comparison only | Rationale only visible when expanded. | |
| No rationale shown | Just star/checkmark, no explanation. | |

**User's choice:** Show why on card
**Notes:** Builds trust in auto-selection.

---

## Navigation & Integration

### Nav position

| Option | Description | Selected |
|--------|-------------|----------|
| After Preview | Pipeline > Proposals > Preview > Duplicates > Audit Log | ✓ |
| After Proposals | Pipeline > Proposals > Duplicates > Preview > Audit Log | |
| Before Audit Log (last) | Pipeline > Proposals > Preview > Audit Log > Duplicates | |

**User's choice:** After Preview
**Notes:** Groups file management tools together.

### Empty state

| Option | Description | Selected |
|--------|-------------|----------|
| Success message | "No duplicates found" with positive framing. | ✓ |
| Prompt to scan | Action-oriented message linking to Pipeline. | |
| Claude decides | Let Claude pick based on patterns. | |

**User's choice:** Success message
**Notes:** None

### Summary stats

| Option | Description | Selected |
|--------|-------------|----------|
| Group count + file count | "{N} groups - {M} files - {X} MB recoverable" | ✓ |
| Group count only | Just "{N} duplicate groups remaining" | |
| Claude decides | Let Claude determine useful stats. | |

**User's choice:** Group count + file count
**Notes:** None

---

## Claude's Discretion

- Pagination approach (follow proposals page pattern)
- HTMX swap targets and animation details
- FileRecord state machine integration for DUPLICATE_RESOLVED
- Toast/undo implementation specifics

## Deferred Ideas

None — discussion stayed within phase scope
