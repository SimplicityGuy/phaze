# Phase 7: Approval Workflow UI - Research

**Researched:** 2026-03-28
**Domain:** Server-rendered web UI with HTMX, Jinja2, Tailwind CSS, Alpine.js
**Confidence:** HIGH

## Summary

Phase 7 is the first UI phase for phaze. No templates, static files, or Jinja2 configuration exists yet -- everything must be built from scratch. The stack is fully decided: FastAPI + Jinja2 server-side rendering, HTMX for dynamic interactions (partial page swaps, OOB updates), Tailwind CSS via CDN for styling, and Alpine.js for client-side state (keyboard shortcuts, checkboxes, toast auto-dismiss).

The core challenge is building a high-throughput review interface for up to 200K proposals. The UI must support paginated browsing, status filtering, text search, bulk actions, keyboard shortcuts, and undo-able approve/reject -- all without full page reloads. The approved UI-SPEC (`07-UI-SPEC.md`) provides complete design contracts including colors, typography, spacing, component inventory, and copywriting.

**Primary recommendation:** Build a layered template architecture (base.html -> page template -> partials) where each HTMX-swappable region is its own Jinja2 partial. Endpoints return full pages for initial load and HTML fragments for HTMX requests (detected via `HX-Request` header). Use OOB swaps to update stats bar and inject toast notifications alongside primary table updates.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- D-01: Dense table layout with columns: original filename, proposed filename, confidence, status, action buttons
- D-02: Essential columns only in default view -- no extracted metadata columns
- D-03: Expandable rows via click -- inline detail panel lazy-loaded via HTMX
- D-04: Confidence scores color-coded: green (high), yellow (medium), red (low)
- D-05: Instant approve/reject with undo toast (5-second window)
- D-06: Bulk actions -- checkboxes, select-all, bulk approve/reject buttons
- D-07: Row stays in place after approve/reject with updated status badge
- D-08: Keyboard shortcuts via Alpine.js: arrows, 'a' approve, 'r' reject, 'e' expand
- D-09: Tab bar for status filtering (All/Pending/Approved/Rejected) with count badges, default Pending
- D-10: Text search by filename with HTMX debounce
- D-11: Numbered page pagination with configurable page size (25/50/100)
- D-12: Default sort by confidence ascending (low first); sortable columns
- D-13: No proposals empty state with guidance message
- D-14: All reviewed celebration state
- D-15: Summary stats bar at top

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

### Deferred Ideas (OUT OF SCOPE)
- APR-04 (Batch approval with smart grouping) -- v2
- APR-05 (Inline editing of proposals) -- v2
- EXE-05 (Progress tracking / job status visibility) -- v2
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| APR-01 | Admin can view paginated list of all proposed renames in a web UI | Jinja2Templates + SQLAlchemy offset/limit pagination + HTMX partial swaps for page navigation |
| APR-02 | Admin can approve or reject individual proposals | HTMX hx-patch on action buttons -> update ProposalStatus in DB -> OOB swap for stats + toast |
| APR-03 | Admin can filter proposals by status (pending, approved, rejected) | Tab bar with hx-get that passes status query param, reloads table partial |
</phase_requirements>

## Standard Stack

### Core (already installed)
| Library | Version | Purpose | Notes |
|---------|---------|---------|-------|
| FastAPI | >=0.135.2 | Web framework | Already in pyproject.toml |
| Jinja2 | 3.1.6 | Server-side templating | Transitive dep via Starlette -- already importable, no pyproject.toml change needed |
| SQLAlchemy | >=2.0.48 | Database queries | Already in pyproject.toml. Use `select()` with `.offset()/.limit()` for pagination |
| Pydantic | >=2.10 | Request/response validation | Already a FastAPI dep. Use for query parameter validation |

### CDN Dependencies (no Python packages)
| Library | Version | CDN URL | Purpose |
|---------|---------|---------|---------|
| HTMX | 2.0.7 | `https://unpkg.com/htmx.org@2.0.7/dist/htmx.min.js` | Dynamic DOM updates without JS |
| Alpine.js | 3.15.9 | `https://cdn.jsdelivr.net/npm/alpinejs@3.15.9/dist/cdn.min.js` | Keyboard shortcuts, checkbox state, toast dismiss |
| Tailwind CSS | 4.x | `https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4` | Utility-first CSS styling |
| Inter font | - | `https://fonts.googleapis.com/css2?family=Inter:wght@400;600` | Typography per UI-SPEC |

**Important:** Pin exact CDN versions in templates (not `@latest`). The UI-SPEC specifies Heroicons 2.1.5 for icons via inline SVG.

**Note on Tailwind v4 vs v3:** The UI-SPEC was written referencing Tailwind v3 class names (e.g., `text-sm`, `bg-gray-50`). Tailwind v4 CDN (`@tailwindcss/browser@4`) is the current CDN offering and supports all v3 utility classes. The v3 Play CDN (`cdn.tailwindcss.com`) remains available as fallback. Use v4 unless class compatibility issues arise.

### New Python Dependencies Needed
| Library | Version | Purpose |
|---------|---------|---------|
| python-multipart | >=0.0.20 | Required by FastAPI for form data handling (bulk action forms) |

**Verification:** `Jinja2Templates` and `StaticFiles` are already importable from the current environment. No new template-related Python packages needed.

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Raw Jinja2 + HTMX | fastapi-htmx library | Adds dependency for minimal benefit. Raw HTMX attributes are simple enough. |
| OOB swaps for toast | HX-Trigger header + Alpine listener | HX-Trigger is cleaner for some patterns but OOB is more explicit and easier to test. |
| Offset pagination | Keyset pagination | Keyset is faster for deep pages (>1000 offset) but adds complexity. Offset is fine for 200K/50-per-page = 4000 pages max, and users rarely paginate deeply. |

## Architecture Patterns

### Recommended Template Structure
```
src/phaze/
  templates/
    base.html                         # Full page shell: <html>, CDN links, nav, toast container
    proposals/
      list.html                       # Full page: extends base.html, includes all partials
      partials/
        stats_bar.html                # Summary counts (OOB swap target)
        filter_tabs.html              # Status tabs with count badges
        search_box.html               # Search input with hx-trigger debounce
        proposal_table.html           # Table wrapper (primary swap target)
        proposal_row.html             # Single table row (for individual row updates)
        row_detail.html               # Expanded detail panel (lazy loaded)
        pagination.html               # Page navigation controls
        bulk_actions.html             # Bulk approve/reject bar
        toast.html                    # Toast notification (OOB swap)
  static/                             # Only if local assets needed (likely empty -- CDN-only)
  routers/
    proposals.py                      # All UI endpoints for proposal review
  services/
    proposal_queries.py               # Database query functions (pagination, filtering, counts)
```

### Pattern 1: Full Page vs HTMX Fragment Detection
**What:** Same endpoint returns full page for browser navigation and HTML fragment for HTMX requests.
**When to use:** Every endpoint that serves the proposal table.
**Example:**
```python
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/proposals", tags=["proposals"])
templates = Jinja2Templates(directory="src/phaze/templates")

@router.get("/", response_class=HTMLResponse)
async def list_proposals(
    request: Request,
    status: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
    sort: str = "confidence",
    order: str = "asc",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    # ... fetch proposals, counts ...
    context = {"request": request, "proposals": proposals, "pagination": pagination, "stats": stats}

    if request.headers.get("HX-Request"):
        # HTMX request: return just the table partial + OOB stats update
        return templates.TemplateResponse("proposals/partials/proposal_table.html", context)
    # Full browser request: return complete page
    return templates.TemplateResponse("proposals/list.html", context)
```

### Pattern 2: HTMX OOB Swap for Stats + Toast
**What:** When approve/reject happens, response includes both the updated row AND out-of-band swaps for the stats bar and toast notification.
**When to use:** Every approve/reject action (single or bulk).
**Example template response combining OOB swaps:**
```html
{# Primary response: updated row #}
<tr id="proposal-{{ proposal.id }}" ...>
  ... updated row content ...
</tr>

{# OOB: update stats bar #}
<div id="stats-bar" hx-swap-oob="true">
  {% include "proposals/partials/stats_bar.html" %}
</div>

{# OOB: inject toast #}
<div hx-swap-oob="beforeend:#toast-container">
  {% include "proposals/partials/toast.html" %}
</div>
```

### Pattern 3: Alpine.js Keyboard Navigation
**What:** Alpine.js manages focused-row state and keyboard shortcuts within the table.
**When to use:** The proposal table component.
**Example:**
```html
<div x-data="proposalTable()" @keydown.window="handleKeydown($event)">
  <table>
    <tbody>
      {% for proposal in proposals %}
      <tr :class="{ 'bg-blue-100 ring-2 ring-blue-500': focusedRow === {{ loop.index0 }} }"
          @click="focusedRow = {{ loop.index0 }}">
        ...
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>

<script>
function proposalTable() {
  return {
    focusedRow: -1,
    selectedRows: new Set(),
    handleKeydown(e) {
      if (e.target.tagName === 'INPUT') return; // skip when typing in search
      switch(e.key) {
        case 'ArrowDown': this.focusedRow = Math.min(this.focusedRow + 1, rowCount - 1); break;
        case 'ArrowUp': this.focusedRow = Math.max(this.focusedRow - 1, 0); break;
        case 'a': /* trigger approve on focused row */ break;
        case 'r': /* trigger reject on focused row */ break;
        case 'e': /* toggle expand on focused row */ break;
      }
    }
  }
}
</script>
```

### Pattern 4: Pagination Query
**What:** Async SQLAlchemy offset/limit pagination with count.
**When to use:** The proposal listing query.
**Example:**
```python
from sqlalchemy import func, select
from phaze.models.proposal import RenameProposal, ProposalStatus

async def get_proposals_page(
    session: AsyncSession,
    status: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 50,
    sort_by: str = "confidence",
    sort_order: str = "asc",
) -> tuple[list[RenameProposal], int]:
    """Return (proposals, total_count) for a page."""
    base = select(RenameProposal)
    count_q = select(func.count()).select_from(RenameProposal)

    if status and status != "all":
        base = base.where(RenameProposal.status == status)
        count_q = count_q.where(RenameProposal.status == status)

    if search:
        like_pattern = f"%{search}%"
        search_filter = RenameProposal.proposed_filename.ilike(like_pattern)
        base = base.where(search_filter)
        count_q = count_q.where(search_filter)

    # Sort
    sort_col = getattr(RenameProposal, sort_by, RenameProposal.confidence)
    base = base.order_by(sort_col.asc() if sort_order == "asc" else sort_col.desc())

    # Paginate
    offset = (page - 1) * page_size
    base = base.offset(offset).limit(page_size)

    result = await session.execute(base)
    total = await session.scalar(count_q)

    return list(result.scalars().all()), total or 0
```

### Anti-Patterns to Avoid
- **Loading all 200K proposals at once:** Always paginate server-side. Never return more than `page_size` rows.
- **Full page reload on filter/search/paginate:** Use HTMX to swap only the table region. Full page reloads break the UX.
- **Separate API + frontend:** Do NOT build JSON APIs consumed by JavaScript. Return rendered HTML fragments directly. This is the HTMX way.
- **Complex Alpine.js state management:** Alpine.js should manage only UI state (focused row, selected checkboxes, toast visibility). All data mutations go through HTMX to the server.
- **N+1 queries:** When displaying proposals, join-load the related FileRecord in one query to get `original_filename` and `original_path`. Do not lazy-load per row.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Template rendering | Custom string interpolation | Jinja2Templates (built into Starlette) | Template inheritance, auto-escaping, URL generation |
| Partial page updates | Custom JavaScript fetch+DOM | HTMX attributes (hx-get, hx-swap, hx-target) | Declarative, no JS to write/debug |
| Toast auto-dismiss | Custom setTimeout + DOM | Alpine.js `x-init="setTimeout(() => show = false, 5000)"` | Reactive, handles edge cases |
| Pagination math | Manual offset calculation | Write a small `Pagination` dataclass | Reusable, testable, prevents off-by-one bugs |
| Form data parsing | Manual request body parsing | FastAPI Form() parameters or Pydantic model | Validation built-in |

**Key insight:** The entire UI stack (HTMX + Jinja2 + Alpine.js) is designed so you write almost zero JavaScript. Resist the urge to add custom JS -- if you need JS, check if HTMX or Alpine.js already handles it.

## Common Pitfalls

### Pitfall 1: Jinja2Templates Directory Path
**What goes wrong:** `Jinja2Templates(directory="templates")` fails because the path is relative to the working directory, not the package.
**Why it happens:** In Docker or when running from a different directory, the relative path doesn't resolve.
**How to avoid:** Use `pathlib.Path(__file__).parent / "templates"` to anchor the template directory to the package location.
**Warning signs:** `TemplateNotFoundError` in tests or Docker.

### Pitfall 2: Missing `request` in Template Context
**What goes wrong:** Jinja2 templates crash with `UndefinedError: 'request' is undefined`.
**Why it happens:** FastAPI's `TemplateResponse` requires `request` in the context dict.
**How to avoid:** Always pass `request=request` in the context. The `TemplateResponse(request=request, name=..., context=...)` signature handles this.
**Warning signs:** Any template that calls `url_for()` will fail without request.

### Pitfall 3: HTMX Swap Target Mismatch
**What goes wrong:** HTMX replaces the wrong element or the entire page.
**Why it happens:** `hx-target` and `hx-swap` not matching the fragment returned by the server.
**How to avoid:** Each swappable region needs a stable `id`. HTMX fragment endpoints must return HTML with the same `id` as the target. Use `hx-swap="innerHTML"` on containers, `hx-swap="outerHTML"` on individual rows.
**Warning signs:** Page flashes, content appears in wrong location, duplicate elements.

### Pitfall 4: Alpine.js and HTMX Interaction After Swap
**What goes wrong:** Alpine.js components stop working after HTMX swaps in new content.
**Why it happens:** Alpine.js initializes on page load. Newly swapped DOM elements haven't been initialized.
**How to avoid:** HTMX 2.x dispatches `htmx:afterSwap` events. Alpine.js 3.x handles this automatically when scripts are loaded via `defer`. But if you use `x-data` on swapped-in elements, ensure Alpine reinitializes. The simplest fix: keep Alpine `x-data` on a parent element that is NOT swapped (wrap the swap target).
**Warning signs:** Checkboxes stop working, keyboard shortcuts fail after pagination.

### Pitfall 5: N+1 Query on FileRecord Join
**What goes wrong:** Displaying `original_filename` from the related `FileRecord` triggers a separate query per row.
**Why it happens:** SQLAlchemy lazy-loads relationships by default. Async sessions don't support implicit lazy loading.
**How to avoid:** Use `selectinload(RenameProposal.file)` or join-load in the query. Note: RenameProposal currently has no relationship defined -- you'll need to add one (or use a manual join).
**Warning signs:** `MissingGreenlet` error in async context, or slow page loads.

### Pitfall 6: Bulk Actions with Large Selection
**What goes wrong:** Selecting "all on page" and bulk-approving sends 50+ IDs in one request.
**Why it happens:** Checkbox state needs to be collected and sent as a list.
**How to avoid:** Use Alpine.js to maintain `selectedRows` set, serialize as hidden form inputs or JSON. Server endpoint accepts list of UUIDs. Use a single `UPDATE ... WHERE id IN (...)` query, not individual updates.
**Warning signs:** Timeout on bulk operations, partial updates.

### Pitfall 7: Test Client Template Path
**What goes wrong:** Tests using `AsyncClient` with `ASGITransport` fail to find templates.
**Why it happens:** The test client doesn't change the working directory. Template directory path resolution differs.
**How to avoid:** Use `pathlib.Path` for template directory (Pitfall 1 fix). Alternatively, configure template directory in Settings and override in tests.
**Warning signs:** `TemplateNotFoundError` only in tests, works fine when running the app.

## Code Examples

### FastAPI App Factory with Jinja2
```python
# src/phaze/main.py additions
from pathlib import Path
from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

def create_app() -> FastAPI:
    app = FastAPI(title="Phaze", version="0.1.0", lifespan=lifespan)
    # Store templates on app.state for access in routers
    app.state.templates = templates
    app.include_router(health.router)
    app.include_router(scan.router)
    app.include_router(companion.router)
    app.include_router(proposals.router)
    return app
```

### HTMX Request Detection
```python
def is_htmx_request(request: Request) -> bool:
    """Check if the request was made by HTMX."""
    return request.headers.get("HX-Request") == "true"
```

### Approve/Reject Endpoint with OOB Updates
```python
@router.patch("/{proposal_id}/approve", response_class=HTMLResponse)
async def approve_proposal(
    request: Request,
    proposal_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    proposal = await session.get(RenameProposal, proposal_id)
    if not proposal:
        raise HTTPException(status_code=404)
    proposal.status = ProposalStatus.APPROVED
    await session.commit()

    # Re-fetch stats for OOB update
    stats = await get_proposal_stats(session)

    context = {"request": request, "proposal": proposal, "stats": stats}
    # Return updated row + OOB stats + OOB toast
    return templates.TemplateResponse(
        "proposals/partials/approve_response.html", context
    )
```

### Undo Endpoint
```python
@router.patch("/{proposal_id}/undo", response_class=HTMLResponse)
async def undo_proposal(
    request: Request,
    proposal_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    proposal = await session.get(RenameProposal, proposal_id)
    if not proposal:
        raise HTTPException(status_code=404)
    proposal.status = ProposalStatus.PENDING
    await session.commit()

    stats = await get_proposal_stats(session)
    context = {"request": request, "proposal": proposal, "stats": stats}
    return templates.TemplateResponse(
        "proposals/partials/undo_response.html", context
    )
```

### Testing HTML Endpoints
```python
async def test_proposals_list_returns_html(client: AsyncClient) -> None:
    """Proposal list endpoint returns HTML page."""
    response = await client.get("/proposals/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Proposal Review" in response.text

async def test_proposals_htmx_returns_fragment(client: AsyncClient) -> None:
    """HTMX request returns table fragment, not full page."""
    response = await client.get(
        "/proposals/",
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert "<html" not in response.text  # Fragment, not full page
    assert "<table" in response.text or "<tbody" in response.text
```

### Pagination Dataclass
```python
from dataclasses import dataclass

@dataclass
class Pagination:
    page: int
    page_size: int
    total: int

    @property
    def total_pages(self) -> int:
        return max(1, (self.total + self.page_size - 1) // self.page_size)

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages

    @property
    def start(self) -> int:
        return (self.page - 1) * self.page_size + 1

    @property
    def end(self) -> int:
        return min(self.page * self.page_size, self.total)
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| HTMX 1.x `hx-swap-oob` | HTMX 2.x `hx-swap-oob` (same API) | June 2024 | No breaking change for OOB swaps |
| Tailwind v3 Play CDN | Tailwind v4 `@tailwindcss/browser` | 2025 | New CDN URL, same utility classes work |
| Alpine.js 2.x | Alpine.js 3.x | 2021 | Different init pattern (`Alpine.start()` not needed with `defer`) |
| FastAPI `TemplateResponse(name, {"request": req})` | `TemplateResponse(request=req, name=..., context=...)` | FastAPI 0.100+ | Named params preferred |

**Deprecated/outdated:**
- `cdn.tailwindcss.com` (v3 Play CDN): Still works but v4 CDN via jsDelivr is current
- HTMX 4.0: In development (fetch-based internals) -- not yet released, use 2.0.7

## Open Questions

1. **RenameProposal -> FileRecord Relationship**
   - What we know: `RenameProposal` has `file_id` FK but no SQLAlchemy `relationship()` defined
   - What's unclear: Whether to add a relationship or use manual joins
   - Recommendation: Add `file: Mapped["FileRecord"] = relationship()` to RenameProposal for cleaner query patterns. This is a small model change with no migration impact (relationship is ORM-only, not a schema change).

2. **Template Location in Package**
   - What we know: Templates should live at `src/phaze/templates/`
   - What's unclear: Whether `hatch` wheel build includes non-Python files
   - Recommendation: For Docker deployment (running from source), this is not an issue. If wheel packaging is needed later, add `[tool.hatch.build.targets.wheel.shared-data]` config. Not a concern for this phase.

3. **python-multipart Dependency**
   - What we know: Form data handling (for bulk action checkboxes) requires `python-multipart`
   - What's unclear: Whether it's already a transitive dependency
   - Recommendation: Check if already installed; if not, add to pyproject.toml

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/test_routers/test_proposals.py -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| APR-01 | Paginated proposal list renders HTML | integration | `uv run pytest tests/test_routers/test_proposals.py::test_proposals_list -x` | Wave 0 |
| APR-01 | Pagination returns correct page | integration | `uv run pytest tests/test_routers/test_proposals.py::test_proposals_pagination -x` | Wave 0 |
| APR-02 | Approve individual proposal | integration | `uv run pytest tests/test_routers/test_proposals.py::test_approve_proposal -x` | Wave 0 |
| APR-02 | Reject individual proposal | integration | `uv run pytest tests/test_routers/test_proposals.py::test_reject_proposal -x` | Wave 0 |
| APR-02 | Undo approve/reject | integration | `uv run pytest tests/test_routers/test_proposals.py::test_undo_proposal -x` | Wave 0 |
| APR-03 | Filter by status returns filtered results | integration | `uv run pytest tests/test_routers/test_proposals.py::test_filter_by_status -x` | Wave 0 |
| APR-03 | Search by filename | integration | `uv run pytest tests/test_routers/test_proposals.py::test_search_proposals -x` | Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_routers/test_proposals.py -x`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_routers/test_proposals.py` -- covers APR-01, APR-02, APR-03
- [ ] Test fixtures for creating RenameProposal + FileRecord test data in conftest.py or local fixtures
- [ ] Template directory must be resolvable from test working directory

## Project Constraints (from CLAUDE.md)

- **Python 3.13 exclusively** -- all code must target 3.13
- **uv only** -- never bare `pip`, `python`, `pytest`, `mypy`
- **Pre-commit hooks must pass** before commits
- **85% code coverage minimum**
- **Type hints on all functions** (`disallow_untyped_defs = true` in mypy)
- **150-character line length**
- **Double quotes for strings**
- **Ruff for linting and formatting** -- run `uv run ruff check .` and `uv run ruff format .`
- **Commit frequently** during execution
- **README per service** -- keep updated
- **PR per phase** -- use worktree branches
- **GitHub Actions use just commands** -- not inline shell
- **Tests exclude mypy strict decorators** (`disallow_untyped_decorators = false` for tests)

## Sources

### Primary (HIGH confidence)
- [FastAPI Templates docs](https://fastapi.tiangolo.com/advanced/templates/) -- Jinja2Templates setup, TemplateResponse pattern
- [HTMX hx-swap-oob docs](https://htmx.org/attributes/hx-swap-oob/) -- OOB swap syntax and patterns
- [HTMX releases](https://github.com/bigskysoftware/htmx/releases) -- v2.0.7 verified as latest stable
- [Alpine.js releases](https://github.com/alpinejs/alpine/releases) -- v3.15.9 verified as latest stable
- [Tailwind CSS CDN docs](https://tailwindcss.com/docs/installation/play-cdn) -- v4 CDN via `@tailwindcss/browser@4`
- Local verification: `Jinja2Templates` and `StaticFiles` importable from current environment (Jinja2 3.1.6, Starlette 1.0.0)

### Secondary (MEDIUM confidence)
- [HTMX + FastAPI patterns (TestDriven.io)](https://testdriven.io/blog/fastapi-htmx/) -- integration patterns and testing
- [FastAPI Hypermedia with HTMX (Medium)](https://medium.com/@strasbourgwebsolutions/fastapi-as-a-hypermedia-driven-application-w-htmx-jinja2templates-644c3bfa51d1) -- full page vs fragment pattern
- [HTMX OOB toast pattern (mostlylucid.net)](https://www.mostlylucid.net/blog/showingtoastandswappingwithhtmx) -- toast notification implementation

### Tertiary (LOW confidence)
- None -- all findings verified with official docs

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- all libraries already installed or available via CDN, versions verified
- Architecture: HIGH -- HTMX + Jinja2 + FastAPI is a well-documented pattern with many production examples
- Pitfalls: HIGH -- common issues documented in official docs and community resources
- Testing: HIGH -- existing test infrastructure (conftest.py, AsyncClient pattern) directly applicable

**Research date:** 2026-03-28
**Valid until:** 2026-04-28 (stable stack, no fast-moving dependencies)
