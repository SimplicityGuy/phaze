# Phase 9: Pipeline Orchestration - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-03-30
**Phase:** 09-pipeline-orchestration
**Areas discussed:** Trigger architecture, Batch strategy, Volume mount & write access, Session deduplication

---

## Trigger Architecture

### Q1: How should the scan→analyze pipeline be triggered?

| Option | Description | Selected |
|--------|-------------|----------|
| Explicit API endpoints | Add POST /api/v1/analyze that queries DISCOVERED files and enqueues process_file jobs. Clear, debuggable. | ✓ |
| Auto-chain from scan | run_scan automatically enqueues process_file jobs after discovery. Fully automatic but tighter coupling. | |
| Hybrid | Auto-enqueue after scan + manual trigger endpoint. Most flexible but more code. | |

**User's choice:** Explicit API endpoints
**Notes:** Matches human-in-the-loop philosophy

### Q2: How should the analyze→propose pipeline be triggered?

| Option | Description | Selected |
|--------|-------------|----------|
| Explicit API endpoint | Add POST /api/v1/proposals/generate. Consistent with scan→analyze pattern. | ✓ |
| Auto-chain from process_file | Auto-check and enqueue when batch ready. Tighter coupling. | |

**User's choice:** Explicit API endpoint

### Q3: Should the UI have pipeline trigger buttons?

| Option | Description | Selected |
|--------|-------------|----------|
| UI buttons on proposals page | Add trigger buttons to existing page | |
| API-only endpoints | REST only, no UI | |
| Dashboard page with pipeline status | New page with pipeline stages, triggers, and progress | ✓ |

**User's choice:** Dashboard page with pipeline status

---

## Batch Strategy

### Q1: How should files be enqueued for analysis?

| Option | Description | Selected |
|--------|-------------|----------|
| One arq job per file | Matches existing process_file signature. arq handles parallelism. | ✓ |
| Batch jobs (N files per job) | Group files into chunks. More complex, requires signature change. | |

**User's choice:** One arq job per file

### Q2: How should ANALYZED files be batched for proposals?

| Option | Description | Selected |
|--------|-------------|----------|
| Fixed-size batches using llm_batch_size | Chunks of 10 (default). Matches existing generate_proposals signature. | ✓ |
| By scan batch | Group by scan_batch. Variable sizes. | |
| All at once | One call with all IDs. Risks overflow. | |

**User's choice:** Fixed-size batches using llm_batch_size

---

## Volume Mount & Write Access

### Q1: How should file write access work?

| Option | Description | Selected |
|--------|-------------|----------|
| Separate OUTPUT_PATH mount | SCAN_PATH stays :ro. New OUTPUT_PATH :rw for writes. Cleanest separation. | ✓ |
| Change SCAN_PATH to :rw on worker | Simple, minimal change. Files stay in same tree. | |
| Both :rw everywhere | Simplest but gives API unnecessary write access. | |

**User's choice:** Separate OUTPUT_PATH mount

---

## Session Deduplication

### Q1: How should _get_session be handled?

| Option | Description | Selected |
|--------|-------------|----------|
| Extract to tasks/session.py | Single shared module. All 3 task files import from there. | ✓ |
| Keep duplicated | Maximum isolation. 3 copies to maintain. | |
| You decide | Claude's discretion. | |

**User's choice:** Extract to tasks/session.py

---

## Claude's Discretion

- Dashboard page layout, styling, and component structure
- Error handling for enqueue failures
- Pipeline status polling mechanism (SSE vs HTMX refresh)

## Deferred Ideas

None
