# Phase 9: Pipeline Orchestration - Research

**Researched:** 2026-03-29
**Domain:** FastAPI API endpoints, arq job enqueuing, HTMX dashboard UI, Docker volume configuration
**Confidence:** HIGH

## Summary

Phase 9 closes the critical integration gaps identified in the v1.0 milestone audit: scan completion does not trigger analysis, and analysis completion does not trigger proposal generation. The entire downstream pipeline (analysis, proposals, approval, execution) is unreachable without manual arq job injection.

This phase is primarily a wiring/integration phase, not a greenfield feature phase. All the individual pieces exist and are tested: `process_file` arq job, `generate_proposals` arq job, `execute_approved_batch` arq job, `WorkerSettings` with all functions registered, and `app.state.arq_pool` available in all routers. The work is (1) adding API endpoints that query files by state and enqueue the appropriate arq jobs, (2) building a dashboard UI page with trigger buttons, (3) fixing the Docker volume mounts for execution write access, (4) deduplicating the `_get_session` helper, and (5) adding an `output_path` config setting.

**Primary recommendation:** Follow the established Router -> Service -> arq enqueue pattern (already used in `routers/execution.py`). Each new endpoint queries files by `FileState`, enqueues jobs via `app.state.arq_pool.enqueue_job()`, and returns a response. The dashboard page follows the Jinja2 + HTMX + Tailwind pattern established in proposals and audit pages.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Explicit API endpoints for each pipeline transition. Add `POST /api/v1/analyze` (queries DISCOVERED files, enqueues one `process_file` arq job per file) and `POST /api/v1/proposals/generate` (queries ANALYZED files, chunks into batches, enqueues `generate_proposals` jobs).
- **D-02:** No auto-chaining -- each step is manually triggered via API call or UI button. Matches the human-in-the-loop philosophy. Clear, debuggable.
- **D-03:** Dashboard page showing pipeline stages (discovered, analyzed, proposed) with trigger buttons and progress indicators. New page at `/dashboard/` or `/pipeline/`.
- **D-04:** Analysis uses one arq job per file. `POST /api/v1/analyze` queries all DISCOVERED files and enqueues one `process_file(ctx, file_id)` per file. arq handles parallelism via `max_jobs=8`.
- **D-05:** Proposal generation uses fixed-size batches from `settings.llm_batch_size` (default 10). `POST /api/v1/proposals/generate` queries all ANALYZED files, chunks them into groups, enqueues one `generate_proposals(ctx, file_ids, batch_index)` per chunk.
- **D-06:** Separate OUTPUT_PATH mount for file execution writes. Keep SCAN_PATH as `:ro` (source files stay read-only). Add `OUTPUT_PATH` volume mount (`:rw`) on worker for renamed file destination. Execution service copies to OUTPUT_PATH, original stays in SCAN_PATH.
- **D-07:** Worker gets both mounts: SCAN_PATH `:ro` for reading source files, OUTPUT_PATH `:rw` for writing destination files. API keeps only SCAN_PATH `:ro`.
- **D-08:** Add `output_path` setting to config.py (default `/data/output`). Execution service uses this as destination base directory.
- **D-09:** Extract `_get_session` to `src/phaze/tasks/session.py`. All 3 task modules (`functions.py`, `proposal.py`, `execution.py`) import from there. Single place to change connection configuration.

### Claude's Discretion
- Dashboard page layout, styling, and exact component structure
- Error handling for enqueue failures (e.g., Redis down)
- Whether pipeline status uses SSE polling or HTMX periodic refresh

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| ANL-01 | System detects BPM for music files using librosa/existing prototypes | Analysis logic exists in `process_file` arq job. This phase provides the `POST /api/v1/analyze` trigger endpoint that enqueues `process_file` for DISCOVERED files. |
| ANL-02 | System classifies mood and style for music files using existing prototypes | Same as ANL-01 -- mood/style classification is part of `process_file`. This phase makes it reachable. |
| AIP-01 | System uses LLM to propose a new filename for each file based on available metadata, analysis results, and companion file content | Proposal logic exists in `generate_proposals` arq job. This phase provides the `POST /api/v1/proposals/generate` trigger endpoint that batches ANALYZED files and enqueues `generate_proposals` jobs. |
</phase_requirements>

## Project Constraints (from CLAUDE.md)

- **Python 3.13 exclusively** -- all code must target 3.13
- **`uv` only** -- never bare `pip`, `python`, `pytest`, or `mypy`; always `uv run` prefix
- **Pre-commit hooks** must pass -- frozen SHAs, ruff, mypy, bandit, yamllint, etc.
- **85% minimum code coverage** required
- **Type hints on all functions** -- mypy strict mode (excluding tests)
- **150-character line length**, double quotes, PEP 8
- **Every feature gets its own git worktree and PR**
- **Ruff rules** as specified in CLAUDE.md (ARG, B, C4, E, F, I, PLC, PTH, RUF, S, SIM, T20, TCH, UP, W)
- **Commit frequently** during execution, not batched at the end
- **README per service** -- keep docs updated alongside code
- **Justfile as command runner** -- keep updated with new services
- **GitHub Actions must delegate to just commands**

## Architecture Patterns

### Established Patterns to Follow

**1. Router -> arq enqueue pattern (from `routers/execution.py`):**
```python
# Access arq pool from app state
arq_pool = request.app.state.arq_pool
# Enqueue a job by function name string
await arq_pool.enqueue_job("execute_approved_batch", batch_id)
```

**2. HTMX partial vs full page pattern (from `routers/proposals.py`):**
```python
# HX-Request header detection for partial rendering
if request.headers.get("HX-Request") == "true":
    return templates.TemplateResponse(request=request, name="partial.html", context=context)
return templates.TemplateResponse(request=request, name="full_page.html", context=context)
```

**3. Template structure:**
- Full pages extend `base.html`
- Partials live in `{feature}/partials/` subdirectory
- `current_page` context variable for nav active state
- Stats bar pattern for aggregate counts (proposals page)

**4. Database query pattern (from services):**
```python
stmt = select(FileRecord).where(FileRecord.state == FileState.DISCOVERED)
result = await session.execute(stmt)
files = list(result.scalars().all())
```

**5. Task session pattern (to be deduplicated per D-09):**
```python
# Current: duplicated in functions.py, proposal.py, execution.py
# Target: single module at src/phaze/tasks/session.py
async def get_task_session() -> AsyncSession:
    engine = create_async_engine(settings.database_url)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return async_session()
```

### Recommended Project Structure for New Files

```
src/phaze/
  config.py            # Add output_path setting (D-08)
  main.py              # Register pipeline router
  tasks/
    session.py         # NEW: shared _get_session (D-09)
    functions.py       # Update import
    proposal.py        # Update import
    execution.py       # Update import
  routers/
    pipeline.py        # NEW: POST /api/v1/analyze, POST /api/v1/proposals/generate
  services/
    pipeline.py        # NEW: query functions for pipeline stage counts, file batching
  templates/
    pipeline/
      dashboard.html         # NEW: full dashboard page
      partials/
        stats_bar.html       # NEW: pipeline stage counts
        stage_card.html      # NEW: individual stage card with trigger button
        trigger_response.html # NEW: response after trigger click
```

### API Endpoint Design

**`POST /api/v1/analyze`:**
- Query: `SELECT * FROM files WHERE state = 'discovered'`
- For each file: `await arq_pool.enqueue_job("process_file", str(file.id))`
- Return: `{"enqueued": count, "message": "..."}`

**`POST /api/v1/proposals/generate`:**
- Query: `SELECT * FROM files WHERE state = 'analyzed'`
- Chunk into batches of `settings.llm_batch_size` (default 10)
- For each batch: `await arq_pool.enqueue_job("generate_proposals", [str(f.id) for f in batch], batch_index)`
- Return: `{"enqueued_batches": count, "total_files": total, "message": "..."}`

### Dashboard Design (Claude's Discretion)

Recommendation: Use HTMX polling (`hx-trigger="every 5s"`) for pipeline status rather than SSE. Rationale: the dashboard is a status overview, not a real-time progress stream. SSE is overkill for periodic count updates. HTMX polling via `hx-get` is simpler and matches the existing HTMX patterns better than adding another SSE endpoint.

The dashboard should show a pipeline flow visualization with stage cards showing:
- Stage name and file count
- Trigger button (enabled only when files are available)
- Visual arrow/flow between stages

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Job enqueuing | Custom Redis pubsub | `arq_pool.enqueue_job()` | Already available on `app.state.arq_pool` |
| Batch chunking | Manual list slicing | Python itertools-style batching | Use a simple helper or list comprehension to chunk file IDs |
| Session management | New engine per endpoint | FastAPI `Depends(get_session)` | Already established in all routers |
| Task session | 3 duplicate `_get_session` | Single `tasks/session.py` module | D-09 decision |
| Real-time updates | WebSocket implementation | HTMX `hx-trigger="every Ns"` polling | Sufficient for dashboard count refreshes |

## Common Pitfalls

### Pitfall 1: Engine Creation Per Task Call
**What goes wrong:** The current `_get_session` creates a new `create_async_engine` on every task invocation. At 200K files this means 200K engine instances.
**Why it happens:** Tasks run in a separate worker process without access to FastAPI's shared engine.
**How to avoid:** When deduplicating to `tasks/session.py`, consider using a module-level engine singleton or passing the engine via arq's startup context. However, the current approach works because SQLAlchemy reuses connections internally. Leave as-is for now unless performance issues arise. The dedup (D-09) focuses on DRY, not optimization.
**Warning signs:** Connection pool exhaustion errors, "too many connections" from PostgreSQL.

### Pitfall 2: Enqueuing 200K Jobs Synchronously
**What goes wrong:** `POST /api/v1/analyze` loops over all DISCOVERED files and calls `enqueue_job` one at a time. With 200K files, this could take minutes and timeout the HTTP request.
**Why it happens:** Each `enqueue_job` is an async Redis call, but 200K sequential calls is still slow.
**How to avoid:** Use `asyncio.gather` with batched enqueue calls, or use Redis pipeline mode. Alternatively, enqueue in chunks and return immediately after the first batch, with a background task handling the rest. A pragmatic approach: run the enqueue loop in a background `asyncio.Task` (like `run_scan` does) and return immediately with the expected count.
**Warning signs:** HTTP 504 timeout on analyze endpoint, slow Redis response.

### Pitfall 3: Docker Volume Mount Permissions
**What goes wrong:** Adding OUTPUT_PATH volume mount but forgetting to ensure the destination directory exists or has correct permissions inside the container.
**Why it happens:** Docker creates the mount point but the application user may not have write permissions.
**How to avoid:** Ensure the Dockerfile creates the output directory, or use Docker Compose `mkdir` in the entrypoint. Test with `docker compose exec worker ls -la /data/output`.
**Warning signs:** `PermissionError` or `FileNotFoundError` on file execution.

### Pitfall 4: Race Condition on State Queries
**What goes wrong:** Two simultaneous calls to `POST /api/v1/analyze` could enqueue the same files twice.
**Why it happens:** Both calls query DISCOVERED files before either updates the state.
**How to avoid:** This is acceptable for a single-user tool. Arq deduplicates jobs by function+args by default. If needed, add a simple Redis lock key. The trigger buttons should show a loading state to prevent double-clicks.
**Warning signs:** Duplicate analysis results (arq's built-in dedup should prevent this).

### Pitfall 5: Background Task Garbage Collection
**What goes wrong:** `asyncio.create_task()` tasks can be garbage collected before completion.
**Why it happens:** No reference is kept to the task object.
**How to avoid:** Follow the existing pattern in `routers/scan.py` with `_background_tasks` set and `task.add_done_callback(_background_tasks.discard)`.
**Warning signs:** Tasks silently disappearing, incomplete enqueuing.

## Code Examples

### New API Endpoint Pattern (POST /api/v1/analyze)
```python
# Source: established pattern from routers/scan.py + routers/execution.py
@router.post("/api/v1/analyze")
async def trigger_analysis(request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Enqueue process_file jobs for all DISCOVERED files."""
    result = await session.execute(
        select(FileRecord).where(FileRecord.state == FileState.DISCOVERED)
    )
    files = list(result.scalars().all())
    if not files:
        return {"enqueued": 0, "message": "No files in DISCOVERED state"}

    arq_pool = request.app.state.arq_pool
    for file in files:
        await arq_pool.enqueue_job("process_file", str(file.id))

    return {"enqueued": len(files), "message": f"Enqueued {len(files)} files for analysis"}
```

### Batch Chunking for Proposals
```python
# Source: D-05 batching decision
def chunk_list(items: list[str], size: int) -> list[list[str]]:
    """Split a list into fixed-size chunks."""
    return [items[i : i + size] for i in range(0, len(items), size)]
```

### Session Deduplication (tasks/session.py)
```python
# Source: D-09 decision, extracted from tasks/functions.py
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from phaze.config import settings


async def get_task_session() -> AsyncSession:
    """Create a one-off async session for arq task use.

    Workers don't share the FastAPI app's engine. Each task creates
    its own lightweight session.
    """
    engine = create_async_engine(settings.database_url)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)  # type: ignore[call-overload]
    session: AsyncSession = async_session()
    return session
```

### Dashboard Page Template Pattern
```html
<!-- Source: established pattern from proposals/list.html -->
{% extends "base.html" %}
{% block title %}Pipeline Dashboard - Phaze{% endblock %}
{% block content %}
<div class="space-y-6">
    <h1 class="text-2xl font-semibold leading-tight">Pipeline Dashboard</h1>
    <!-- Stats bar with stage counts -->
    <div id="pipeline-stats" hx-get="/pipeline/stats" hx-trigger="every 5s" hx-swap="innerHTML">
        {% include "pipeline/partials/stats_bar.html" %}
    </div>
    <!-- Stage cards with trigger buttons -->
    {% include "pipeline/partials/stage_cards.html" %}
</div>
{% endblock %}
```

### Docker Volume Mount Update
```yaml
# Source: D-06, D-07 decisions
worker:
  volumes:
    - "${SCAN_PATH:-/data/music}:/data/music:ro"
    - "${MODELS_PATH:-./models}:/models:ro"
    - "${OUTPUT_PATH:-/data/output}:/data/output:rw"  # NEW: write access for execution
```

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio |
| Config file | `pyproject.toml` [tool.pytest.ini_options] |
| Quick run command | `uv run pytest tests/ -x --no-header -q` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| ANL-01 | POST /api/v1/analyze enqueues process_file for DISCOVERED files | unit | `uv run pytest tests/test_routers/test_pipeline.py::test_analyze_enqueues_discovered -x` | Wave 0 |
| ANL-02 | Same endpoint covers mood/style (process_file handles both) | unit | `uv run pytest tests/test_routers/test_pipeline.py::test_analyze_enqueues_discovered -x` | Wave 0 |
| AIP-01 | POST /api/v1/proposals/generate enqueues generate_proposals for ANALYZED files in batches | unit | `uv run pytest tests/test_routers/test_pipeline.py::test_proposals_generate_batches -x` | Wave 0 |

### Additional Test Coverage
| Feature | Behavior | Test Type | Automated Command | File Exists? |
|---------|----------|-----------|-------------------|-------------|
| Session dedup | tasks/session.py get_task_session returns AsyncSession | unit | `uv run pytest tests/test_tasks/test_session.py -x` | Wave 0 |
| Dashboard page | GET /pipeline/ returns 200 with HTML | unit | `uv run pytest tests/test_routers/test_pipeline.py::test_dashboard_page -x` | Wave 0 |
| Config output_path | Settings.output_path defaults to /data/output | unit | `uv run pytest tests/test_config_worker.py -x` (extend) | Exists |
| Empty state | POST /api/v1/analyze with no DISCOVERED files returns 0 | unit | `uv run pytest tests/test_routers/test_pipeline.py::test_analyze_no_files -x` | Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/ -x --no-header -q`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_routers/test_pipeline.py` -- covers ANL-01, ANL-02, AIP-01, dashboard rendering
- [ ] `tests/test_tasks/test_session.py` -- covers session dedup (D-09)
- [ ] `tests/test_services/test_pipeline.py` -- covers pipeline query functions (stage counts, file batching)

## Environment Availability

Step 2.6: SKIPPED (no external dependencies identified). This phase is purely code/config changes using already-installed technologies (FastAPI, arq, SQLAlchemy, Jinja2, HTMX). Docker Compose changes are configuration-only.

## Sources

### Primary (HIGH confidence)
- **Existing codebase** -- all patterns derived from actual project files:
  - `src/phaze/routers/execution.py` -- arq enqueue pattern
  - `src/phaze/routers/proposals.py` -- HTMX partial rendering pattern
  - `src/phaze/routers/scan.py` -- background task pattern with GC protection
  - `src/phaze/tasks/functions.py` -- process_file job signature
  - `src/phaze/tasks/proposal.py` -- generate_proposals job signature
  - `src/phaze/tasks/execution.py` -- _get_session duplication source
  - `src/phaze/config.py` -- settings pattern for new output_path
  - `src/phaze/models/file.py` -- FileState enum for queries
  - `src/phaze/templates/base.html` -- nav bar pattern with current_page
  - `docker-compose.yml` -- current volume mount configuration

### Secondary (MEDIUM confidence)
- `.planning/v1.0-MILESTONE-AUDIT.md` -- gap analysis driving this phase, verified against code

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- no new libraries needed, all existing
- Architecture: HIGH -- all patterns derived from existing codebase, well-established
- Pitfalls: HIGH -- based on direct code review (200K scale concerns, GC pattern, engine per task)

**Research date:** 2026-03-29
**Valid until:** 2026-04-28 (stable -- no library changes, purely integration work)
