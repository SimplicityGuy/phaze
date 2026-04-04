# Phase 9: Pipeline Orchestration - Context

**Gathered:** 2026-03-30
**Status:** Ready for planning

<domain>
## Phase Boundary

Wire the automated pipeline so that file discovery triggers analysis, and analysis completion triggers proposal generation — making the core E2E flow (scan → analyze → propose → approve → execute) work without manual arq job injection. Includes a dashboard page showing pipeline status and trigger buttons, volume mount fix for execution, and session helper deduplication.

</domain>

<decisions>
## Implementation Decisions

### Trigger Architecture
- **D-01:** Explicit API endpoints for each pipeline transition. Add `POST /api/v1/analyze` (queries DISCOVERED files, enqueues one `process_file` arq job per file) and `POST /api/v1/proposals/generate` (queries ANALYZED files, chunks into batches, enqueues `generate_proposals` jobs).
- **D-02:** No auto-chaining — each step is manually triggered via API call or UI button. Matches the human-in-the-loop philosophy. Clear, debuggable.
- **D-03:** Dashboard page showing pipeline stages (discovered → analyzed → proposed) with trigger buttons and progress indicators. New page at `/dashboard/` or `/pipeline/`.

### Batch Strategy
- **D-04:** Analysis uses one arq job per file. `POST /api/v1/analyze` queries all DISCOVERED files and enqueues one `process_file(ctx, file_id)` per file. arq handles parallelism via `max_jobs=8`.
- **D-05:** Proposal generation uses fixed-size batches from `settings.llm_batch_size` (default 10). `POST /api/v1/proposals/generate` queries all ANALYZED files, chunks them into groups, enqueues one `generate_proposals(ctx, file_ids, batch_index)` per chunk.

### Volume Mount & Write Access
- **D-06:** Separate OUTPUT_PATH mount for file execution writes. Keep SCAN_PATH as `:ro` (source files stay read-only). Add `OUTPUT_PATH` volume mount (`:rw`) on worker for renamed file destination. Execution service copies to OUTPUT_PATH, original stays in SCAN_PATH.
- **D-07:** Worker gets both mounts: SCAN_PATH `:ro` for reading source files, OUTPUT_PATH `:rw` for writing destination files. API keeps only SCAN_PATH `:ro`.
- **D-08:** Add `output_path` setting to config.py (default `/data/output`). Execution service uses this as destination base directory.

### Session Deduplication
- **D-09:** Extract `_get_session` to `src/phaze/tasks/session.py`. All 3 task modules (`functions.py`, `proposal.py`, `execution.py`) import from there. Single place to change connection configuration.

### Claude's Discretion
- Dashboard page layout, styling, and exact component structure
- Error handling for enqueue failures (e.g., Redis down)
- Whether pipeline status uses SSE polling or HTMX periodic refresh

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Existing Pipeline Code
- `src/phaze/services/ingestion.py` — `run_scan` function that needs no modification (scan is working)
- `src/phaze/tasks/functions.py` — `process_file` arq job (analyze endpoint enqueues this)
- `src/phaze/tasks/proposal.py` — `generate_proposals` arq job (proposals endpoint enqueues this)
- `src/phaze/tasks/worker.py` — `WorkerSettings` with all registered task functions
- `src/phaze/main.py` — FastAPI lifespan with `arq_pool` on `app.state`
- `src/phaze/routers/scan.py` — Existing scan router pattern to follow for new endpoints

### Execution & Volume
- `src/phaze/services/execution.py` — `execute_single_file` that needs OUTPUT_PATH support
- `docker-compose.yml` — Volume mount configuration to update

### UI Patterns
- `src/phaze/templates/base.html` — Base template with nav bar to add dashboard link
- `src/phaze/templates/proposals/list.html` — Reference for HTMX page patterns
- `src/phaze/routers/proposals.py` — Reference for router + Jinja2 template pattern
- `src/phaze/routers/execution.py` — SSE pattern reference

### Config
- `src/phaze/config.py` — Settings class to add `output_path`

### Audit Report
- `.planning/v1.0-MILESTONE-AUDIT.md` — Detailed gap analysis driving this phase

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `app.state.arq_pool` — ArqRedis connection pool already available in all router handlers
- `arq_pool.enqueue_job(func_name, *args)` — Pattern used in `routers/execution.py` line 35
- `base.html` — Nav bar with `current_page` context variable for active state
- Jinja2 + HTMX partial pattern established in Phase 7 proposals UI
- SSE via `sse-starlette` established in Phase 8 execution progress

### Established Patterns
- Router → Service → arq enqueue pattern (execution.py does this already)
- HTMX partial rendering with `HX-Request` header detection
- Stats bar with aggregate counts (proposals page has this)
- `selectinload` for eager-loaded relationships in query functions

### Integration Points
- `main.py` — New router registration (`app.include_router(pipeline.router)`)
- `base.html` — Add "Dashboard" nav link
- `docker-compose.yml` — Add OUTPUT_PATH volume mount on worker
- `config.py` — Add `output_path` setting
- `FileState` enum — Files flow through DISCOVERED → ANALYZED → PROPOSAL_GENERATED → APPROVED → EXECUTED

</code_context>

<specifics>
## Specific Ideas

- Dashboard should show counts per pipeline stage (e.g., "1,234 discovered, 890 analyzed, 456 proposed, 123 approved, 45 executed")
- Trigger buttons should show confirmation with file count before enqueuing
- Follow the same HTMX + Tailwind + Alpine.js stack as the proposals page

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope

</deferred>

---

*Phase: 09-pipeline-orchestration*
*Context gathered: 2026-03-30*
