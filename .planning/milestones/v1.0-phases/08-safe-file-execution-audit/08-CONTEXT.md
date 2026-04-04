# Phase 8: Safe File Execution & Audit - Context

**Gathered:** 2026-03-29
**Status:** Ready for planning

<domain>
## Phase Boundary

Execute approved renames using copy-verify-delete protocol with every operation logged to an append-only audit trail. Adds an "Execute approved" button to the existing approval UI, a live SSE progress counter, and a paginated audit log page. Files are renamed in-place (same directory, new filename). No tag writing, no directory reorganization (v2).

</domain>

<decisions>
## Implementation Decisions

### Execution Trigger
- **D-01:** Admin button in the existing approval UI — "Execute approved" button triggers a batch job for all approved proposals. Fits the human-in-the-loop pattern: review first, then explicit action.
- **D-02:** Executes ALL approved proposals at once. No selection UI — one click processes everything. arq workers handle the batch in parallel.

### Destination Paths
- **D-03:** Rename in-place — file stays in its current directory, gets the proposed_filename. Destination = current_path directory + proposed_filename. Simplest and safest for v1 since directory path proposals (AIP-03) are deferred to v2.
- **D-04:** Leave empty directories alone after renames. No cleanup of directory structure. User can handle manually or in a future phase.

### Failure & Partial State
- **D-05:** If SHA256 verification fails after copy, delete the bad copy at destination. Original file remains untouched. This phase does NOT modify file contents (no tag writing — explicitly out of scope per REQUIREMENTS.md), so SHA256 must match exactly on byte-for-byte copy.
- **D-06:** No automatic retry on failure. Mark as failed, move to next file. User can re-trigger execution later for failed files.
- **D-07:** Continue on failure — each file is independent. One failure does not affect others. Log all failures, report results at the end.

### Execution Visibility
- **D-08:** Live progress counter via SSE (server-sent events). FastAPI streams updates to the browser using HTMX's native SSE support (hx-ext='sse'). Shows real-time count of files processed during batch execution.
- **D-09:** Separate audit log page — paginated table of ExecutionLog rows: operation, source path, destination path, sha256_verified, status, timestamp. Filterable by status. Consistent with Phase 7's table pattern.
- **D-10:** After execution completes, approval UI shows "Executed" badge on executed proposals. Uses existing FileState.EXECUTED on the file record. Minimal change to Phase 7 UI.

### Claude's Discretion
- SSE endpoint implementation details (EventSourceResponse pattern)
- HTMX SSE integration specifics (hx-ext, event names, swap strategy)
- Batch size for arq jobs (how many files per job vs one job per file)
- ExecutionLog write timing (before vs after each operation step)
- Audit log page URL and navigation placement
- "Execute approved" button placement and styling in approval UI
- How to update FileRecord.current_path after successful rename
- Alembic migration strategy if ExecutionLog table needs changes (model already exists but may need migration)

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project Configuration
- `CLAUDE.md` — Development setup, code quality rules, technology stack
- `.planning/PROJECT.md` — Project vision, constraints, human-in-the-loop requirement
- `.planning/REQUIREMENTS.md` — EXE-01 (copy-verify-delete), EXE-02 (append-only audit log)

### Existing Models
- `src/phaze/models/execution.py` — ExecutionLog model (proposal_id, operation, source_path, destination_path, sha256_verified, status, error_message, executed_at), ExecutionStatus enum (PENDING/IN_PROGRESS/COMPLETED/FAILED)
- `src/phaze/models/proposal.py` — RenameProposal model (proposed_filename, proposed_path, confidence, context_used, status), ProposalStatus enum
- `src/phaze/models/file.py` — FileRecord model (current_path, original_path, sha256_hash, state), FileState enum with EXECUTED and FAILED states

### Existing Services & Patterns
- `src/phaze/tasks/functions.py` — arq task pattern (_get_session, process_file with retry/backoff, session management)
- `src/phaze/tasks/pool.py` — run_in_process_pool helper (execution is I/O-bound, may not need this)
- `src/phaze/tasks/worker.py` — WorkerSettings with on_startup/on_shutdown hooks
- `src/phaze/services/proposal_queries.py` — Proposal query patterns for the approval UI
- `src/phaze/routers/proposals.py` — Approval UI router (HTMX patterns, template rendering)
- `src/phaze/config.py` — Settings with pydantic-settings

### UI Patterns (from Phase 7)
- `src/phaze/templates/` — Jinja2 templates with HTMX partials pattern
- `src/phaze/routers/proposals.py` — HTMX fragment detection (HX-Request header), Alpine.js integration

### Prior Phase Context
- `.planning/phases/04-task-queue-worker-infrastructure/04-CONTEXT.md` — arq worker decisions, retry/backoff patterns
- `.planning/phases/07-approval-workflow-ui/07-CONTEXT.md` — UI decisions (table layout, HTMX patterns, bulk actions, keyboard shortcuts)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `ExecutionLog` model — already defined with all needed columns (proposal_id, operation, source/dest paths, sha256_verified, status, error_message)
- `ExecutionStatus` enum — PENDING/IN_PROGRESS/COMPLETED/FAILED already defined
- `FileState.EXECUTED` and `FileState.FAILED` — state transitions already in the file state machine
- `FileRecord.sha256_hash` — original hash available for verification after copy
- `FileRecord.current_path` — tracks current file location, needs updating after successful rename
- arq task infrastructure — _get_session pattern, retry/backoff, worker settings
- Phase 7 HTMX/Jinja2 templates — table rendering, pagination, filtering, partials pattern

### Established Patterns
- arq job functions with try/finally session management
- SQLAlchemy 2.0 async queries with select() and session.execute()
- HTMX partial page swaps (HX-Request header detection)
- Alpine.js for client-side interactivity
- Pydantic-settings for configuration (env vars)
- Template partials directory structure for composable HTMX fragments

### Integration Points
- New `src/phaze/services/execution.py` for copy-verify-delete logic
- New arq job function for file execution (in tasks/functions.py or separate file)
- New SSE endpoint for live progress streaming
- New audit log page (templates + router)
- "Execute approved" button added to existing approval UI template
- "Executed" status badge added to proposal row display
- New Alembic migration if execution_log table doesn't exist in DB yet

</code_context>

<specifics>
## Specific Ideas

- Copy-verify-delete is critical safety for an irreplaceable 200K file collection — never use direct rename/move which could lose data on failure.
- Live SSE counter provides confidence during long-running batch operations — user knows the system is working.
- Audit log page serves as a permanent record and debugging tool — if something goes wrong, every operation is traceable.
- No tag writing in this phase (explicitly out of scope) means SHA256 verification is a clean byte-for-byte comparison.
- Rename in-place is the right v1 choice — directory organization (AIP-03) in v2 will use the metadata already stored in context_used JSONB.

</specifics>

<deferred>
## Deferred Ideas

- **EXE-03 (Full undo/rollback via audit trail):** Use the audit log to reverse executed renames. Deferred to v2 per REQUIREMENTS.md.
- **EXE-04 (Acoustic duplicate detection):** Chromaprint fingerprint similarity for finding non-exact duplicates. Deferred to v2.
- **EXE-05 (Full progress tracking / job status visibility):** Detailed progress UI with per-file status, ETA, etc. v1 covers basic SSE counter and audit log. Full tracking deferred to v2.
- **AIP-03 (Directory path proposals):** LLM proposes destination folder paths. Deferred to v2. v1 renames in-place.
- **Empty directory cleanup:** Automatically removing directories that become empty after renames. Deferred — user handles manually.

</deferred>

---

*Phase: 08-safe-file-execution-audit*
*Context gathered: 2026-03-29*
