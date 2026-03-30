# Phase 8: Safe File Execution & Audit - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-03-29
**Phase:** 08-safe-file-execution-audit
**Areas discussed:** Execution trigger, Destination paths, Failure & partial state, Execution visibility

---

## Execution Trigger

### How should execution be triggered?

| Option | Description | Selected |
|--------|-------------|----------|
| Admin button in UI | Add an 'Execute approved' button to the approval UI. Enqueues arq batch job. | ✓ |
| API endpoint only | POST /api/execute endpoint, triggered via curl/script. | |
| Both UI + API | Button in UI that calls the API, API also callable directly. | |

**User's choice:** Admin button in UI
**Notes:** Fits the human-in-the-loop pattern — explicit action after reviewing.

### Should execution process all approved or user-selected?

| Option | Description | Selected |
|--------|-------------|----------|
| All approved at once | One click executes every approved proposal. | ✓ |
| User selects which | Checkboxes to pick specific approved proposals. | |
| Both — default all, option to select | Default all, optional selection for partial execution. | |

**User's choice:** All approved at once
**Notes:** Simple, matches 'review then execute' workflow.

---

## Destination Paths

### How should the destination path be determined?

| Option | Description | Selected |
|--------|-------------|----------|
| Rename in-place | File stays in current directory, gets new filename. | ✓ |
| Configurable output root | All renamed files move to a configured base directory. | |
| Output root with subdirectories | Files move to base directory with auto-created subdirectories. | |

**User's choice:** Rename in-place
**Notes:** Simplest and safest for v1 since directory path proposals don't exist yet.

### Should empty directories be cleaned up?

| Option | Description | Selected |
|--------|-------------|----------|
| Leave empty dirs alone | Don't touch directory structure. | ✓ |
| Clean up empty dirs after | Remove directories that became empty after execution. | |

**User's choice:** Leave empty dirs alone
**Notes:** Safe, predictable. User can clean up manually or in a future phase.

---

## Failure & Partial State

### What happens if SHA256 verification fails after copy?

| Option | Description | Selected |
|--------|-------------|----------|
| Delete the bad copy | Remove corrupted destination file. Original stays untouched. | ✓ |
| Keep for inspection | Leave bad copy at destination, mark as failed. | |
| Quarantine directory | Move bad copy to quarantine folder for review. | |

**User's choice:** Delete the bad copy
**Notes:** User asked about MP3 tag writing affecting SHA256. Confirmed this phase does NOT modify file contents (tag writing explicitly out of scope per REQUIREMENTS.md), so SHA256 must match exactly on byte-for-byte copy.

### Should failed operations be automatically retried?

| Option | Description | Selected |
|--------|-------------|----------|
| No auto-retry | Mark as failed, move on. User re-triggers later. | ✓ |
| Retry with backoff | Use arq retry/backoff. | |
| Retry once, then fail | Single retry attempt after short delay. | |

**User's choice:** No auto-retry
**Notes:** Avoids hammering on persistent errors (disk full, permissions).

### If batch encounters multiple failures, continue or stop?

| Option | Description | Selected |
|--------|-------------|----------|
| Continue on failure | Each file independent. Log failures, keep going. | ✓ |
| Stop after N failures | Halt after configurable threshold. | |
| Stop on first failure | Any failure halts everything. | |

**User's choice:** Continue on failure
**Notes:** Report results at the end.

---

## Execution Visibility

### What level of visibility during batch execution?

| Option | Description | Selected |
|--------|-------------|----------|
| Summary after completion | Status banner with results after done. | |
| Live counter via SSE/polling | Real-time progress counter. | ✓ |
| Execution log page | Separate page showing audit log table. | ✓ |

**User's choice:** Both live counter AND audit log page
**Notes:** User explicitly wanted both options — live counter for real-time feedback and audit log for permanent queryable record.

### Live counter approach?

| Option | Description | Selected |
|--------|-------------|----------|
| SSE (server-sent events) | FastAPI streams updates. HTMX native SSE support. | ✓ |
| HTMX polling | Polls endpoint every few seconds. | |

**User's choice:** SSE
**Notes:** One-way, lightweight, no polling overhead.

### Audit log page detail level?

| Option | Description | Selected |
|--------|-------------|----------|
| Paginated table | Table of ExecutionLog rows, filterable by status. | ✓ |
| Grouped by execution batch | Group by batch run, expandable. | |

**User's choice:** Paginated table
**Notes:** Consistent with Phase 7's table pattern.

### Should approval UI update after execution?

| Option | Description | Selected |
|--------|-------------|----------|
| Update proposal status display | Show 'Executed' badge on executed proposals. | ✓ |
| Hide executed proposals | Filter out executed from default view. | |
| Both — badge + optional hide | Badge with filter tab for executed. | |

**User's choice:** Update proposal status display
**Notes:** Uses existing FileState.EXECUTED. Minimal change to Phase 7 UI.

---

## Claude's Discretion

- SSE endpoint implementation details (EventSourceResponse pattern)
- HTMX SSE integration specifics (hx-ext, event names, swap strategy)
- Batch size for arq jobs
- ExecutionLog write timing
- Audit log page URL and navigation
- Button placement and styling
- FileRecord.current_path update logic
- Alembic migration strategy

## Deferred Ideas

- EXE-03: Full undo/rollback via audit trail (v2)
- EXE-04: Acoustic duplicate detection (v2)
- EXE-05: Full progress tracking / job status visibility (v2)
- AIP-03: Directory path proposals (v2)
- Empty directory cleanup after renames
