# Phase 14: Duplicate Resolution UI - Research

**Researched:** 2026-03-31
**Domain:** HTMX/Jinja2 admin UI for duplicate file resolution workflow
**Confidence:** HIGH

## Summary

Phase 14 is a UI-focused phase that adds a duplicate resolution page to the existing admin interface. The core backend logic (`find_duplicate_groups`, `count_duplicate_groups`) already exists in `src/phaze/services/dedup.py` from Phase 3. The phase requires extending these queries to join FileMetadata for comparison data, adding auto-selection scoring logic, a new `DUPLICATE_RESOLVED` FileState, a new router, and a set of Jinja2/HTMX templates following the established proposals page pattern.

The existing codebase provides a strong reference implementation. The proposals router (`routers/proposals.py`) demonstrates the exact pattern needed: paginated list view, HTMX partial responses, OOB stat updates, toast/undo flow, and bulk actions. The templates in `templates/proposals/partials/` provide concrete examples of every UI pattern required (cards, stats bars, toasts, OOB swaps). This phase is primarily about replicating established patterns with domain-specific data.

**Primary recommendation:** Follow the proposals page implementation as a 1:1 template. Extend `dedup.py` with metadata-joined queries and scoring, add `DUPLICATE_RESOLVED` to FileState enum, create a new `duplicates` router and template directory, and write an Alembic migration for the new state value.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- D-01: Card-per-group layout. Cards expand inline (HTMX) to reveal comparison table.
- D-02: Comparison table columns: original path, file size, file type, bitrate, duration, tag completeness badge, artist/title/album.
- D-03: Best value per column highlighted (green/bold).
- D-04: Radio buttons per file. One pre-selected (auto-scored best). "Resolve Group" to confirm.
- D-05: Soft delete via state change. Non-canonical files marked DUPLICATE_RESOLVED. No filesystem ops.
- D-06: Bulk "Accept All" button. Resolves all unresolved groups on current page. Undo toast 10 seconds.
- D-07: Resolved groups disappear with undo toast (10 seconds). Page shows only unresolved groups.
- D-08: Bitrate-first scoring. Tiebreaker 1: most complete tags. Tiebreaker 2: shortest path.
- D-09: Scoring rationale shown on card next to pre-selected file.
- D-10: Nav link: Pipeline > Proposals > Preview > Duplicates > Audit Log.
- D-11: Empty state with positive messaging.
- D-12: Summary stats header: groups count, total files, recoverable space.

### Claude's Discretion
- Pagination approach (page size, controls) -- follow existing proposals page pattern
- HTMX swap targets and animation for card expand/collapse and resolve actions
- FileRecord state machine integration for DUPLICATE_RESOLVED state
- Toast/undo implementation details

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| DEDUP-01 | Admin UI page displays SHA256 duplicate groups with file details, paginated | Proposals router pattern provides exact pagination/HTMX partial model. `find_duplicate_groups()` already does paginated grouping. |
| DEDUP-02 | User can select canonical file per group and mark others for deletion | Radio button form in comparison table, `hx-post` to resolve endpoint, bulk state update to DUPLICATE_RESOLVED. Proposals approve/reject flow is the reference. |
| DEDUP-03 | User can compare duplicates side-by-side (path, size, bitrate, tags, analysis) | Extend `find_duplicate_groups()` to join FileMetadata. Comparison table partial loaded via HTMX expand. |
| DEDUP-04 | System pre-selects best duplicate based on bitrate, tag completeness, path length | New scoring function in dedup.py. Pure Python sort with D-08 criteria. Pre-selected ID passed to template. |
</phase_requirements>

## Project Constraints (from CLAUDE.md)

- Python 3.13 exclusively
- Package manager: `uv` only -- `uv run` prefix for all commands
- Pre-commit hooks must pass before commits
- Ruff line length: 150
- Mypy strict mode (excluding tests)
- 85% minimum code coverage
- Double quotes for strings
- Type hints on all functions

## Standard Stack

No new dependencies needed. This phase uses only existing project libraries.

### Core (Already Installed)
| Library | Purpose | Notes |
|---------|---------|-------|
| FastAPI | Router/endpoint definitions | Existing `APIRouter` pattern |
| Jinja2 | Server-side templating | Existing `Jinja2Templates` setup |
| SQLAlchemy | Database queries | Extend existing dedup queries with joins |
| HTMX 2.0.7 | Dynamic UI interactions | CDN, already in base.html |
| Alpine.js 3.15.9 | Client-side interactivity | CDN, already in base.html |
| Tailwind CSS 4.x | Styling | CDN, already in base.html |

### Testing (Already Installed)
| Library | Purpose |
|---------|---------|
| pytest + pytest-asyncio | Async test runner |
| httpx (AsyncClient) | HTTP endpoint testing |

**Installation:** None required. All dependencies are already in the project.

## Architecture Patterns

### Project Structure (New Files)
```
src/phaze/
  routers/
    duplicates.py           # New router (follow proposals.py pattern)
  services/
    dedup.py                # Extend with metadata join, scoring, resolve, undo
  models/
    file.py                 # Add DUPLICATE_RESOLVED to FileState enum
  templates/
    duplicates/
      list.html             # Main page (extends base.html)
      partials/
        stats_header.html   # Stats bar with group/file/recoverable counts
        group_card.html     # Collapsed card per duplicate group
        comparison_table.html  # Expanded comparison with radio buttons
        resolve_response.html  # OOB response: empty card + stats + toast
        toast.html           # Undo toast (10-second auto-dismiss)
    base.html               # Add Duplicates nav link after Preview
  main.py                   # Register duplicates router
alembic/versions/
    006_add_duplicate_resolved_state.py  # Migration (if needed for enum)
tests/
  test_routers/
    test_duplicates.py      # Router integration tests
  test_services/
    test_dedup.py            # Extend with scoring, resolve, undo tests
```

### Pattern 1: Router Following Proposals Pattern
**What:** New `duplicates.py` router mirroring the proposals router structure.
**When to use:** Every page in this admin UI follows this pattern.
**Example:**
```python
# Source: src/phaze/routers/proposals.py (existing pattern)
router = APIRouter(prefix="/duplicates", tags=["duplicates"])

@router.get("/", response_class=HTMLResponse)
async def list_duplicates(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=10, le=100),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    groups = await find_duplicate_groups_with_metadata(session, limit=page_size, offset=(page - 1) * page_size)
    stats = await get_duplicate_stats(session)
    total = await count_duplicate_groups(session)
    pagination = Pagination(page=page, page_size=page_size, total=total)
    # Score each group, identify canonical
    for group in groups:
        score_group(group)
    context = {"request": request, "groups": groups, "stats": stats, "pagination": pagination, "current_page": "duplicates"}
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(request=request, name="duplicates/partials/group_list.html", context=context)
    return templates.TemplateResponse(request=request, name="duplicates/list.html", context=context)
```

### Pattern 2: OOB Swap for Stats + Toast
**What:** Resolve action returns empty card (removes it) plus OOB-swapped stats and toast.
**When to use:** Every resolve/undo action.
**Example:**
```html
{# Source: proposals/partials/approve_response.html (existing pattern) #}

{# Primary: empty the card slot to remove it #}

{# OOB: update stats header #}
<div id="stats-header" hx-swap-oob="true">
    {% include "duplicates/partials/stats_header.html" %}
</div>

{# OOB: inject toast #}
<div hx-swap-oob="beforeend:#toast-container">
    {% include "duplicates/partials/toast.html" %}
</div>
```

### Pattern 3: Auto-Selection Scoring
**What:** Pure Python scoring function that ranks files within a group.
**When to use:** Before rendering any group card (collapsed or expanded).
**Example:**
```python
def score_group(group: dict) -> None:
    """Score files in a group and mark the best as canonical (D-08).

    Criteria: highest bitrate > most complete tags > shortest path.
    """
    TAG_FIELDS = ["artist", "title", "album", "year", "genre", "track_number"]

    def sort_key(file: dict) -> tuple[int, int, int]:
        bitrate = file.get("bitrate") or 0
        tag_count = sum(1 for f in TAG_FIELDS if file.get(f))
        path_len = -len(file.get("original_path", ""))  # negative = shorter wins
        return (bitrate, tag_count, path_len)

    files = group["files"]
    files.sort(key=sort_key, reverse=True)
    best = files[0]
    group["canonical_id"] = best["id"]
    # Generate rationale (D-09)
    if best.get("bitrate"):
        group["rationale"] = f"highest bitrate ({best['bitrate']}kbps)"
    elif sum(1 for f in TAG_FIELDS if best.get(f)) > 0:
        count = sum(1 for f in TAG_FIELDS if best.get(f))
        group["rationale"] = f"most complete tags ({count}/{len(TAG_FIELDS)})"
    else:
        group["rationale"] = "shortest path"
```

### Pattern 4: Extend find_duplicate_groups with Metadata Join
**What:** Join FileMetadata to include bitrate, duration, artist, title, album in group results.
**When to use:** The comparison table needs metadata columns.
**Example:**
```python
from sqlalchemy.orm import selectinload

# In the main query, eager-load metadata
stmt = (
    select(FileRecord)
    .options(selectinload(FileRecord.metadata))  # Requires relationship on model
    .where(FileRecord.sha256_hash.in_(select(dup_hashes.c.sha256_hash)))
    .order_by(FileRecord.sha256_hash, FileRecord.original_path)
)
```
**Note:** FileRecord currently has no `metadata` relationship defined. A `relationship()` to FileMetadata must be added, or use a manual join. The manual join approach avoids changing the model but is more verbose. Adding a relationship is cleaner.

### Anti-Patterns to Avoid
- **Filesystem operations in resolve:** D-05 explicitly states soft delete only (state change). Never touch the filesystem.
- **New SQLAlchemy models for scoring:** Scoring is pure Python on already-fetched data, not a database operation.
- **Separate toast templates per action:** Reuse a single parameterized toast template (existing proposals pattern).
- **Client-side scoring:** Keep scoring server-side for consistency. The template just renders the pre-computed canonical selection.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Pagination | Custom pagination logic | Reuse `Pagination` dataclass from `proposal_queries.py` | Already handles page/total/has_prev/has_next |
| Human-readable file sizes | Custom size formatter | Jinja2 filter or inline `{size / 1024 / 1024:.1f} MB` | Simple division, no library needed |
| Toast dismiss timing | Custom JS timer | Alpine.js `setTimeout` pattern from existing toast.html | Already proven, 3 lines |
| Radio group state | Complex JS state management | HTML `<input type="radio" name="canonical-{hash}">` | Native HTML radio behavior handles mutual exclusion |
| Card expand/collapse | Custom JS toggling | Alpine.js `x-show` + HTMX lazy load | Existing pattern in proposals row_detail |

## Common Pitfalls

### Pitfall 1: FileState Enum Migration
**What goes wrong:** Adding `DUPLICATE_RESOLVED` to the Python `FileState` StrEnum but forgetting the database. PostgreSQL stores state as `String(30)`, not a native ENUM type, so no migration is strictly needed -- but the state column max length (30 characters) must accommodate the new value.
**Why it happens:** `DUPLICATE_RESOLVED` is 19 characters, within the 30-char limit. No schema migration needed.
**How to avoid:** Verify `String(30)` is sufficient. Add the value to the Python enum only. Tests using the conftest auto-create tables from models so they pick up changes automatically.
**Warning signs:** If someone had used a PostgreSQL ENUM type instead of String, a migration would be required.

### Pitfall 2: Missing FileRecord-FileMetadata Relationship
**What goes wrong:** `find_duplicate_groups` currently returns dicts without metadata. The comparison table needs bitrate, duration, artist, title, album from FileMetadata.
**Why it happens:** Phase 3 only needed basic file info for dedup detection, not comparison.
**How to avoid:** Either add a SQLAlchemy `relationship()` on FileRecord pointing to FileMetadata (requires updating the model), or do a manual outerjoin in the query. The relationship approach is cleaner and enables `selectinload`.
**Warning signs:** N+1 query if you fetch metadata per-file in a loop instead of eager loading.

### Pitfall 3: Bulk Undo Requires Tracking Resolved IDs
**What goes wrong:** "Accept All" resolves all groups on current page. "Undo All" needs to know which file IDs were just resolved to reverse them.
**Why it happens:** Unlike single-group undo where you know the group hash, bulk undo must remember the entire set.
**How to avoid:** Return the list of resolved file IDs in the bulk response (hidden inputs or data attributes), so the "Undo All" HTMX call can send them back. Or store a batch ID for the bulk operation.
**Warning signs:** "Undo All" reverting more or fewer files than expected.

### Pitfall 4: Filtering Out Already-Resolved Files
**What goes wrong:** `find_duplicate_groups` currently groups ALL files by hash, including those already marked DUPLICATE_RESOLVED. Resolved groups keep showing up.
**Why it happens:** The original query doesn't filter by state.
**How to avoid:** Add a WHERE clause excluding files with state = DUPLICATE_RESOLVED. Only count files NOT in resolved state for grouping.
**Warning signs:** Groups reappearing after resolution, incorrect group counts.

### Pitfall 5: Concurrent Resolution Race Conditions
**What goes wrong:** Two browser tabs could resolve the same group simultaneously.
**Why it happens:** Single-user app makes this unlikely but not impossible (e.g., Accept All + manual resolve).
**How to avoid:** Use optimistic concurrency or simply check if files are already resolved before updating. Idempotent resolution (setting state to DUPLICATE_RESOLVED when it's already DUPLICATE_RESOLVED is a no-op).
**Warning signs:** Errors on resolve, undo not working because state was already changed.

### Pitfall 6: Stats Calculation for Recoverable Space
**What goes wrong:** "Recoverable" in stats header is total size of non-canonical files across all groups. This requires knowing which file is canonical in every group -- but canonical selection is a Python-side scoring decision, not stored in DB.
**Why it happens:** Scoring is ephemeral (computed at render time), but stats need aggregate data.
**How to avoid:** Calculate recoverable as (total duplicate file size - size of one file per group). For stats purposes, use a SQL query that sums `file_size` of all files in duplicate groups, minus the max-bitrate file per group (approximation) or compute in Python after fetching groups.
**Warning signs:** Recoverable space being wildly wrong, or slow stats query.

## Code Examples

### Endpoint: Resolve Group
```python
# Source: Derived from proposals approve/undo pattern
@router.post("/{group_hash}/resolve", response_class=HTMLResponse)
async def resolve_group(
    request: Request,
    group_hash: str,
    canonical_id: uuid.UUID = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Mark non-canonical files in a group as DUPLICATE_RESOLVED."""
    # Get all files with this hash except the canonical one
    stmt = (
        select(FileRecord)
        .where(FileRecord.sha256_hash == group_hash)
        .where(FileRecord.id != canonical_id)
        .where(FileRecord.state != FileState.DUPLICATE_RESOLVED)
    )
    result = await session.execute(stmt)
    files = result.scalars().all()
    resolved_count = len(files)
    resolved_ids = [str(f.id) for f in files]
    for f in files:
        f.state = FileState.DUPLICATE_RESOLVED
    await session.commit()

    stats = await get_duplicate_stats(session)
    return templates.TemplateResponse(
        request=request,
        name="duplicates/partials/resolve_response.html",
        context={
            "request": request,
            "stats": stats,
            "group_hash": group_hash,
            "resolved_count": resolved_count,
            "resolved_ids": resolved_ids,
            "canonical_id": str(canonical_id),
        },
    )
```

### Endpoint: Undo Resolution
```python
@router.post("/{group_hash}/undo", response_class=HTMLResponse)
async def undo_resolve(
    request: Request,
    group_hash: str,
    resolved_ids: list[str] = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Revert DUPLICATE_RESOLVED state for specified files."""
    uuids = [uuid.UUID(fid) for fid in resolved_ids]
    stmt = (
        select(FileRecord)
        .where(FileRecord.id.in_(uuids))
    )
    result = await session.execute(stmt)
    files = result.scalars().all()
    for f in files:
        # Revert to their previous state -- since we only resolve from non-DUPLICATE_RESOLVED,
        # reverting to their original state requires knowing it. Simplest: store previous state
        # or always revert to a known state like DISCOVERED/METADATA_EXTRACTED.
        f.state = FileState.METADATA_EXTRACTED  # or track previous state
    await session.commit()
    # Return the group card back
    # ... rebuild group data and return card partial
```

**Undo state concern:** When undoing, we need to know the previous state. Options:
1. Store previous state before resolving (add a `previous_state` column or track in response data)
2. Always revert to a known state (e.g., the state they were in before -- but we don't know it)
3. Use the state that makes sense in the pipeline (METADATA_EXTRACTED or ANALYZED depending on where they are)

**Recommendation:** Track the previous states in the resolve response as hidden form data. The undo POST sends them back. This avoids schema changes and is consistent with the stateless HTMX pattern.

### Tag Completeness Badge
```python
TAG_FIELDS = ["artist", "title", "album", "year", "genre", "track_number"]

def tag_completeness(metadata: dict | None) -> tuple[str, int, int]:
    """Return (label, filled_count, total_count) for tag badge."""
    if metadata is None:
        return ("None", 0, len(TAG_FIELDS))
    filled = sum(1 for f in TAG_FIELDS if metadata.get(f) is not None)
    if filled == len(TAG_FIELDS):
        return ("Full", filled, len(TAG_FIELDS))
    if filled > 0:
        return ("Partial", filled, len(TAG_FIELDS))
    return ("None", 0, len(TAG_FIELDS))
```

### Human-Readable File Size (Jinja2 Filter)
```python
def filesizeformat(value: int) -> str:
    """Convert bytes to human-readable format."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(value) < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"

# Register in router setup:
templates.env.filters["filesizeformat"] = filesizeformat
```

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio |
| Config file | pyproject.toml |
| Quick run command | `uv run pytest tests/test_services/test_dedup.py tests/test_routers/test_duplicates.py -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` |

### Phase Requirements to Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| DEDUP-01 | Paginated duplicate groups page | integration | `uv run pytest tests/test_routers/test_duplicates.py::test_list_duplicates_returns_html -x` | Wave 0 |
| DEDUP-01 | Empty state when no duplicates | integration | `uv run pytest tests/test_routers/test_duplicates.py::test_empty_state -x` | Wave 0 |
| DEDUP-02 | Resolve group marks non-canonical as DUPLICATE_RESOLVED | integration | `uv run pytest tests/test_routers/test_duplicates.py::test_resolve_group -x` | Wave 0 |
| DEDUP-02 | Undo restores resolved files | integration | `uv run pytest tests/test_routers/test_duplicates.py::test_undo_resolve -x` | Wave 0 |
| DEDUP-03 | Comparison endpoint returns metadata columns | integration | `uv run pytest tests/test_routers/test_duplicates.py::test_compare_endpoint -x` | Wave 0 |
| DEDUP-04 | Scoring selects highest bitrate as canonical | unit | `uv run pytest tests/test_services/test_dedup.py::test_score_group_bitrate_wins -x` | Wave 0 |
| DEDUP-04 | Scoring tiebreaker: tag completeness | unit | `uv run pytest tests/test_services/test_dedup.py::test_score_group_tag_tiebreak -x` | Wave 0 |
| DEDUP-04 | Scoring tiebreaker: shortest path | unit | `uv run pytest tests/test_services/test_dedup.py::test_score_group_path_tiebreak -x` | Wave 0 |
| D-06 | Bulk resolve all groups on page | integration | `uv run pytest tests/test_routers/test_duplicates.py::test_bulk_resolve -x` | Wave 0 |
| D-06 | Bulk undo reverses all resolutions | integration | `uv run pytest tests/test_routers/test_duplicates.py::test_bulk_undo -x` | Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_services/test_dedup.py tests/test_routers/test_duplicates.py -x`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_routers/test_duplicates.py` -- covers DEDUP-01, DEDUP-02, DEDUP-03, D-06
- [ ] Extend `tests/test_services/test_dedup.py` -- covers DEDUP-04 (scoring tests)
- [ ] No new fixtures needed -- existing `session` and `client` fixtures from conftest.py are sufficient

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Custom JS frameworks for CRUD UIs | HTMX + Alpine.js server-rendered | 2023+ | Existing project pattern, no change needed |
| Separate SPA for admin tools | Jinja2 + HTMX hypermedia | Established in Phase 7 | All admin pages follow this, Phase 14 continues it |

## Open Questions

1. **Undo state tracking**
   - What we know: Resolve changes state to DUPLICATE_RESOLVED. Undo must revert to previous state.
   - What's unclear: What was the file's state before resolution? Could be DISCOVERED, METADATA_EXTRACTED, ANALYZED, or PROPOSAL_GENERATED.
   - Recommendation: Pass previous states as hidden form data in the resolve response. The undo POST sends them back. Zero schema changes, stateless pattern.

2. **FileRecord relationship to FileMetadata**
   - What we know: FileMetadata has `file_id` FK to FileRecord. No `relationship()` is defined on FileRecord.
   - What's unclear: Whether adding a relationship would break existing code.
   - Recommendation: Add `metadata: Mapped[FileMetadata | None] = relationship(...)` on FileRecord. Safe addition -- existing queries that don't use it are unaffected. Enables `selectinload` for the comparison query.

3. **Recoverable space calculation efficiency**
   - What we know: Stats header needs total recoverable space (sum of non-canonical file sizes).
   - What's unclear: Whether to compute in SQL or Python.
   - Recommendation: Compute in SQL for accuracy across all groups (not just current page). Query: total file size in duplicate groups minus the max file size per group.

## Sources

### Primary (HIGH confidence)
- `src/phaze/routers/proposals.py` -- established router pattern with pagination, HTMX, OOB swaps
- `src/phaze/services/dedup.py` -- existing duplicate detection queries
- `src/phaze/models/file.py` -- FileState enum, FileRecord model
- `src/phaze/models/metadata.py` -- FileMetadata model with bitrate, duration, artist, title, album
- `src/phaze/templates/proposals/partials/` -- toast, stats_bar, approve_response OOB patterns
- `src/phaze/templates/base.html` -- navigation bar structure, CDN dependencies
- `tests/conftest.py` -- test fixture pattern (async engine, session, client)
- `tests/test_services/test_dedup.py` -- existing dedup test patterns
- `.planning/phases/14-duplicate-resolution-ui/14-CONTEXT.md` -- all locked decisions D-01 through D-12
- `.planning/phases/14-duplicate-resolution-ui/14-UI-SPEC.md` -- complete visual/interaction contract

### Secondary (MEDIUM confidence)
- None needed -- all patterns are established in-codebase

### Tertiary (LOW confidence)
- None

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- no new dependencies, all libraries already in use
- Architecture: HIGH -- direct replication of established proposals page pattern
- Pitfalls: HIGH -- identified from reading actual codebase (state enum sizing, missing relationship, undo state tracking)

**Research date:** 2026-03-31
**Valid until:** 2026-04-30 (stable -- no external dependencies or fast-moving libraries)
