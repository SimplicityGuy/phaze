# Phase 13: AI Destination Paths - Research

**Researched:** 2026-03-31
**Domain:** LLM prompt extension, HTMX UI additions, SQL collision detection
**Confidence:** HIGH

## Summary

Phase 13 extends the existing proposal pipeline to generate destination paths alongside filenames. The infrastructure is already in place: `RenameProposal.proposed_path` column exists as nullable Text, the execution service already routes files using `proposed_path` when present (lines 159-165 of `execution.py`), and the `context_used` JSONB already captures event metadata the LLM can reference for path decisions.

The work is additive across four areas: (1) extend the `naming.md` prompt template with path generation rules and add `proposed_path` to the Pydantic LLM response model, (2) add a Destination column to the proposal table UI, (3) implement batch collision detection as a SQL query before execution, and (4) build a `/preview` page with a collapsible directory tree of approved proposals.

**Primary recommendation:** This phase requires no new dependencies, no new database columns, and no schema migrations. All changes are prompt edits, Pydantic model updates, service logic, template additions, and one new route. Keep the collision check in SQL (GROUP BY on `proposed_path || '/' || proposed_filename` WHERE status = approved HAVING count > 1) for efficiency at 200K scale.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Extend the existing `naming.md` prompt template with path generation rules. Single LLM call produces both `proposed_filename` and `proposed_path`. Keep rules in the markdown file for easy editing.
- **D-02:** Template-guided LLM approach -- provide directory convention templates in the prompt, LLM picks the best template and fills values from available metadata.
- **D-03:** Path logic is a 3-step decision tree for the LLM: (1) figure out which category under `performances/` the file belongs in, (2) determine which artist/festival/concert/radioshow, (3) for festivals/concerts, figure out the year and correct nested structure.
- **D-04:** Album tracks go under a separate `music/{Artist}/{Album}/` tree -- keeps studio releases separate from live performances.
- **D-05:** When the LLM can't determine a good path (too little metadata), leave `proposed_path` null and flag for manual review. Same behavior as v1 (file stays in place if no path proposed).
- **D-06:** Add `proposed_path` to the Pydantic structured output response model alongside `proposed_filename`. No separate LLM call needed.
- **D-07:** Batch collision check runs before execution -- scan all approved proposals for duplicate destination paths (`proposed_path + proposed_filename`).
- **D-08:** Collisions block execution -- affected proposals get a collision warning in the UI. User must resolve (reject one, or paths need to differ) before execution proceeds.
- **D-09:** No auto-suffixing or auto-resolution. Human-in-the-loop constraint applies to collisions too.
- **D-10:** New "Destination" column in the proposal table -- shows the proposed path, truncated with tooltip for long paths. Visible at a glance without expanding the row.
- **D-11:** Null paths (no path proposed) display as a subtle gray "No path" badge in the Destination column.
- **D-12:** Dedicated `/preview` page showing the full directory tree of all approved proposals. Collapsible folders with file counts per directory. Linked from the approval page.
- **D-13:** Scope is approved proposals only -- this is the "what will happen when I execute" view.

### Claude's Discretion
- Prompt template wording for path generation rules and examples
- Pydantic response model field additions (proposed_path type, validation)
- Directory tree rendering approach (server-side HTML vs Alpine.js collapsible)
- Tree page pagination/virtualization strategy for large approval sets
- Collision detection query design (SQL grouping vs application logic)
- How collision warnings display in the proposal table (badge, icon, row highlight)
- Truncation length for destination column and tooltip implementation
- Navigation link placement from approval page to tree preview

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| PATH-01 | LLM prompt generates proposed_path alongside proposed_filename using v1.0 naming format | Extend `naming.md` with path rules; add `proposed_path` to `FileProposalResponse` Pydantic model; update `store_proposals()` to persist path |
| PATH-02 | Proposed destination path displayed in approval UI alongside filename | New Destination column in `proposal_table.html` and `proposal_row.html`; truncation + tooltip pattern |
| PATH-03 | Path collisions detected and flagged when two files would land at the same destination | SQL GROUP BY query on approved proposals; collision badge/highlight in table rows; block execution button when collisions exist |
| PATH-04 | User can view a directory tree preview of where approved files will land | New `/preview` route + template; build tree dict in Python, render with nested HTML + Alpine.js collapse |
</phase_requirements>

## Project Constraints (from CLAUDE.md)

- **Python 3.13 exclusively**, `uv` only for package management
- **Pre-commit hooks** must pass before commits (ruff, mypy, bandit, etc.)
- **Mypy strict mode** on all non-test code: `disallow_untyped_defs`, `disallow_incomplete_defs`, etc.
- **Ruff** line length 150, double quotes, specific rule sets enabled
- **85% minimum code coverage**
- **HTMX + Jinja2 + Tailwind CSS via CDN** for all UI -- no SPA, no build step
- **Alpine.js** for client-side interactivity
- Every feature gets its own git worktree and PR
- Docker Compose deployment, PostgreSQL database
- `uv run pytest`, `uv run mypy .`, `uv run ruff check .` for all quality gates

## Standard Stack

No new dependencies required. This phase uses only existing project libraries.

### Core (already installed)
| Library | Version | Purpose | Role in Phase 13 |
|---------|---------|---------|-------------------|
| FastAPI | >=0.135.2 | Web framework | New `/preview` route, collision check endpoint |
| SQLAlchemy | >=2.0.48 | ORM | Collision detection query, tree data query |
| Jinja2 | >=3.1 | Templates | Destination column, tree preview page |
| HTMX | 2.0.7 (CDN) | Dynamic UI | Collision warning swaps, tree page link |
| Alpine.js | 3.15.9 (CDN) | Client interactions | Collapsible tree folders |
| Tailwind CSS | 4.x (CDN) | Styling | Destination column, tree page, collision badges |
| Pydantic | >=2.10 | Validation | Extended LLM response model with proposed_path |
| litellm | >=1.82.6,<1.82.7 | LLM API | Same call, richer prompt, richer response model |

### No New Packages Needed
This phase is purely additive: prompt edits, model updates, service logic, templates, routes. No `uv add` required.

## Architecture Patterns

### Files to Modify (existing)

```
src/phaze/
├── prompts/
│   └── naming.md              # Add path generation rules section
├── services/
│   └── proposal.py            # Add proposed_path to FileProposalResponse + store_proposals
├── routers/
│   └── proposals.py           # (optional) add collision warning context
│   └── execution.py           # Add collision check before start_execution
├── templates/
│   ├── base.html              # Add "Preview" nav link
│   └── proposals/
│       └── partials/
│           ├── proposal_table.html  # Add Destination column header
│           └── proposal_row.html    # Add Destination cell
```

### Files to Create (new)

```
src/phaze/
├── services/
│   └── collision.py           # Collision detection query + tree builder
├── routers/
│   └── preview.py             # /preview route (tree page)
├── templates/
│   └── preview/
│       ├── tree.html          # Tree preview full page
│       └── partials/
│           └── tree_node.html # Recursive tree node partial
tests/
├── test_services/
│   └── test_collision.py      # Collision detection + tree builder tests
├── test_routers/
│   └── test_preview.py        # Preview route tests
```

### Pattern 1: Prompt Template Extension

**What:** Add a `## Directory Path Rules` section to `naming.md` with the 3-step decision tree and directory convention templates.
**When to use:** PATH-01 -- single LLM call generates both filename and path.

```markdown
## Directory Path Rules

For each file, also propose a destination directory path. Use a 3-step decision tree:

### Step 1: Determine Category
- Album tracks -> `music/`
- DJ sets, live performances, festival recordings -> `performances/`
- Radio shows -> `performances/`

### Step 2: Determine Subcategory and Artist/Event

For `performances/`:
- Artist DJ sets/live sets -> `performances/artists/{Artist Name}/`
- Festival recordings -> `performances/festivals/{Festival Name} {Year}/`
- Concert recordings -> `performances/concerts/{Concert Name} {Year}/`
- Radio shows -> `performances/radioshows/{Radioshow Name}/`

For `music/`:
- Album tracks -> `music/{Artist}/{Album}/`

### Step 3: Nest by Year (festivals/concerts only)
- If year is known, include in the directory name: `performances/festivals/Coachella 2024/`
- If year is unknown, omit: `performances/festivals/Coachella/`

### Path Confidence
- If you cannot determine a reasonable path, set `proposed_path` to null.
- A null path means the file stays in place during execution.
```

The `{files_json}` placeholder and output instructions section must also be updated to include `proposed_path` in the output format.

### Pattern 2: Extended Pydantic Response Model

**What:** Add `proposed_path: str | None = None` to `FileProposalResponse`.

```python
class FileProposalResponse(BaseModel):
    file_index: int
    proposed_filename: str
    proposed_path: str | None = None  # NEW: destination directory path
    confidence: float
    artist: str | None = None
    # ... rest unchanged
```

### Pattern 3: SQL Collision Detection

**What:** GROUP BY query on approved proposals to find duplicate full destination paths.
**When to use:** PATH-03 -- before execution, and for UI collision badges.

```python
from sqlalchemy import func, select

async def detect_collisions(session: AsyncSession) -> list[tuple[str, int]]:
    """Find approved proposals that would collide at the same destination.

    Returns list of (full_path, count) tuples where count > 1.
    """
    full_path = func.concat(
        RenameProposal.proposed_path, "/", RenameProposal.proposed_filename
    )
    stmt = (
        select(full_path.label("dest"), func.count().label("cnt"))
        .where(
            RenameProposal.status == ProposalStatus.APPROVED,
            RenameProposal.proposed_path.isnot(None),
        )
        .group_by(full_path)
        .having(func.count() > 1)
    )
    result = await session.execute(stmt)
    return [(row.dest, row.cnt) for row in result.all()]
```

**Why SQL, not application logic:** With 200K files, loading all proposals into Python to check collisions is wasteful. The database does GROUP BY efficiently with an index.

### Pattern 4: Directory Tree Builder

**What:** Build a nested dict from approved proposals' paths, render as collapsible HTML.
**When to use:** PATH-04 -- the `/preview` page.

```python
from collections import defaultdict
from dataclasses import dataclass, field

@dataclass
class TreeNode:
    name: str
    children: dict[str, "TreeNode"] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    file_count: int = 0

def build_tree(proposals: list[RenameProposal]) -> TreeNode:
    """Build a directory tree from approved proposals."""
    root = TreeNode(name="output")
    for p in proposals:
        if p.proposed_path is None:
            root.files.append(p.proposed_filename)
            continue
        parts = p.proposed_path.strip("/").split("/")
        node = root
        for part in parts:
            if part not in node.children:
                node.children[part] = TreeNode(name=part)
            node = node.children[part]
        node.files.append(p.proposed_filename)
    # Compute recursive file counts
    _count_files(root)
    return root

def _count_files(node: TreeNode) -> int:
    count = len(node.files)
    for child in node.children.values():
        count += _count_files(child)
    node.file_count = count
    return count
```

### Pattern 5: Collapsible Tree with Alpine.js

**What:** Render tree nodes as nested `<details>` or Alpine.js `x-data` with toggle.
**Recommendation:** Use native HTML `<details>/<summary>` elements -- they are collapsible without JS, progressively enhanced, and work with screen readers. Alpine.js adds bulk expand/collapse controls.

```html
<!-- tree_node.html (recursive Jinja2 macro) -->
{% macro render_node(node, depth=0) %}
<details {% if depth < 2 %}open{% endif %} class="ml-4">
    <summary class="cursor-pointer hover:bg-gray-50 py-1 flex items-center gap-2">
        <span class="text-gray-400">&#128193;</span>
        <span class="font-medium">{{ node.name }}</span>
        <span class="text-xs text-gray-400">({{ node.file_count }} files)</span>
    </summary>
    <div class="ml-4">
        {% for child_name, child in node.children.items()|sort %}
            {{ render_node(child, depth + 1) }}
        {% endfor %}
        {% for filename in node.files|sort %}
            <div class="py-0.5 text-sm text-gray-600 flex items-center gap-2">
                <span class="text-gray-300">&#128196;</span>
                {{ filename }}
            </div>
        {% endfor %}
    </div>
</details>
{% endmacro %}
```

### Pattern 6: Execution Gate with Collision Block

**What:** Before starting execution, check for collisions. If any exist, return an error HTML fragment instead of starting the batch.

```python
@router.post("/execution/start", response_class=HTMLResponse)
async def start_execution(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    collisions = await detect_collisions(session)
    if collisions:
        return templates.TemplateResponse(
            request=request,
            name="execution/partials/collision_block.html",
            context={"request": request, "collisions": collisions},
        )
    # ... existing execution logic
```

### Anti-Patterns to Avoid
- **Loading all proposals into Python for collision detection:** Use SQL GROUP BY. At 200K files, in-memory grouping is wasteful.
- **Separate LLM call for paths:** D-01 is explicit -- single call for both filename and path. Extend the existing prompt, not add a second one.
- **Creating directories during proposal generation:** Out of scope (see REQUIREMENTS.md). Directories are created only during execution (already handled by `execution.py` line 163).
- **Auto-resolving collisions:** D-09 explicitly forbids auto-suffixing. Collisions must be human-resolved.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Collapsible tree UI | Custom JS tree component | Native `<details>/<summary>` + Alpine.js | Built into HTML5, accessible, no dependencies |
| Path collision detection | In-memory set comparison | SQL GROUP BY + HAVING | Database handles 200K rows efficiently with index |
| Path string joining | Manual string concatenation | `func.concat()` in SQLAlchemy or Python `pathlib` | Edge cases with leading/trailing slashes |
| Recursive file counting | Multiple SQL queries per node | Single query + Python tree build | One query fetches all approved proposals, tree is built in-memory from that bounded set |

## Common Pitfalls

### Pitfall 1: Trailing/Leading Slashes in proposed_path
**What goes wrong:** Inconsistent path formatting from LLM (some with trailing slash, some without) causes collision detection to miss duplicates.
**Why it happens:** LLM output is unpredictable for formatting details.
**How to avoid:** Normalize `proposed_path` in `store_proposals()` -- strip leading/trailing slashes, collapse double slashes. Add a validator or post-processing step.
**Warning signs:** Collision detection returns 0 collisions when visual inspection shows duplicates.

### Pitfall 2: Collision Check Race Condition
**What goes wrong:** User approves proposals after collision check passes but before execution completes.
**Why it happens:** No locking between collision check and execution.
**How to avoid:** Run collision check inside the execution task itself (in arq), not just in the HTTP handler. The execution service already checks `destination.exists()` as a final guard (line 171-173).
**Warning signs:** Two files land at the same path despite collision check passing.

### Pitfall 3: Large Tree Rendering Performance
**What goes wrong:** `/preview` page takes seconds to render with 200K approved proposals generating deep tree HTML.
**Why it happens:** Rendering thousands of nested HTML elements in a single response.
**How to avoid:** In practice, not all 200K files will be approved simultaneously. But add a note in the template showing count, and consider lazy-loading subtrees via HTMX if the tree exceeds ~1000 nodes. For v1, server-rendered with `<details>` (collapsed by default below depth 2) is sufficient.
**Warning signs:** Preview page load time exceeds 3 seconds.

### Pitfall 4: Prompt Token Budget
**What goes wrong:** Adding path rules significantly increases prompt size, pushing batch token usage over model limits.
**Why it happens:** Path rules add ~500 tokens to the system prompt; multiplied by nothing (it's once per batch, not per file).
**How to avoid:** The path rules are in the system prompt, not duplicated per file. Current batch size is 10 files. Verify total token count stays under model context window after adding rules. The increase is minimal (~500 tokens added to a ~2000 token prompt).
**Warning signs:** LLM returns truncated or malformed responses.

### Pitfall 5: Null proposed_path in Collision Detection
**What goes wrong:** Files with null proposed_path get grouped together in collision detection.
**Why it happens:** SQL `concat(NULL, '/', filename)` returns NULL in PostgreSQL; GROUP BY NULL groups all nulls together.
**How to avoid:** Filter `WHERE proposed_path IS NOT NULL` in the collision query (already shown in Pattern 3 above).
**Warning signs:** Collision detection reports false positives for files without paths.

## Code Examples

### store_proposals Update (add proposed_path persistence)

```python
# In store_proposals(), after creating the RenameProposal record:
path_raw = proposal.proposed_path
# Normalize: strip slashes, collapse doubles
if path_raw:
    path_raw = path_raw.strip("/")
    while "//" in path_raw:
        path_raw = path_raw.replace("//", "/")

record = RenameProposal(
    file_id=uuid.UUID(fid),
    proposed_filename=proposal.proposed_filename,
    proposed_path=path_raw,  # NEW: persist normalized path
    confidence=confidence,
    status=ProposalStatus.PENDING,
    context_used=context_used,
    reason=proposal.reasoning,
)
```

### Destination Column in proposal_row.html

```html
<!-- After proposed_filename cell, before confidence cell -->
<td class="px-3 py-3 max-w-48">
    {% if proposal.proposed_path %}
        <span class="text-sm text-gray-700 truncate block" title="{{ proposal.proposed_path }}">
            {{ proposal.proposed_path|truncate(40) }}
        </span>
    {% else %}
        <span class="text-xs text-gray-400 bg-gray-100 px-2 py-0.5 rounded">No path</span>
    {% endif %}
</td>
```

### Collision Warning Badge in proposal_row.html

```html
<!-- Inside the destination cell, when collision_ids is passed in context -->
{% if proposal.id|string in collision_ids %}
    <span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-orange-100 text-orange-700 ml-1"
          title="Another approved file targets this same destination">
        Collision
    </span>
{% endif %}
```

### Navigation Link in base.html

```html
<!-- Add to nav bar after "Proposals" link -->
<a href="/preview/"
   class="text-sm font-semibold px-3 py-2 {% if current_page == 'preview' %}text-blue-600{% else %}text-gray-600 hover:text-gray-900{% endif %}">
    Preview
</a>
```

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/test_services/test_collision.py -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| PATH-01 | FileProposalResponse accepts proposed_path, prompt includes path rules, store_proposals persists path | unit | `uv run pytest tests/test_services/test_proposal.py -x -k "path"` | Partially (model exists, path tests needed) |
| PATH-02 | Destination column renders in proposal table, null displays "No path" badge | integration | `uv run pytest tests/test_routers/test_proposals.py -x -k "destination"` | No - Wave 0 |
| PATH-03 | Collision detection query finds duplicates, blocks execution, displays warnings | unit + integration | `uv run pytest tests/test_services/test_collision.py -x` | No - Wave 0 |
| PATH-04 | Tree builder produces correct structure, preview route renders page | unit + integration | `uv run pytest tests/test_services/test_collision.py tests/test_routers/test_preview.py -x` | No - Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_services/test_collision.py tests/test_routers/test_preview.py -x`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_services/test_collision.py` -- covers PATH-03, PATH-04 (collision detection + tree builder)
- [ ] `tests/test_routers/test_preview.py` -- covers PATH-04 (preview route rendering)
- [ ] Add path-related test cases to existing `tests/test_services/test_proposal.py` -- covers PATH-01
- [ ] Add destination column assertions to existing `tests/test_routers/test_proposals.py` -- covers PATH-02

## Open Questions

1. **Collision badge data flow**
   - What we know: Collision detection is a SQL query. The proposal list page needs to know which rows have collisions.
   - What's unclear: Should collision IDs be computed on every page load (adds a query per request) or cached/computed on approval action?
   - Recommendation: Compute on page load -- it's a single GROUP BY query, fast with an index. No caching complexity needed for a single-user app.

2. **Tree preview scale**
   - What we know: 200K files is the collection size, but not all will be approved at once. Typical batch might be hundreds to low thousands.
   - What's unclear: What is the realistic maximum number of approved proposals at any one time?
   - Recommendation: Build for thousands. Server-render with collapsed `<details>` below depth 2. Add HTMX lazy-load subtrees only if profiling shows a problem.

## Sources

### Primary (HIGH confidence)
- `src/phaze/models/proposal.py` -- confirmed `proposed_path` column exists as `mapped_column(Text, nullable=True)`
- `src/phaze/services/execution.py` lines 159-165 -- confirmed execution already uses `proposed_path` when present
- `src/phaze/services/proposal.py` -- confirmed `FileProposalResponse` model, `store_proposals()` function, prompt loading
- `src/phaze/prompts/naming.md` -- confirmed current prompt structure with `{files_json}` placeholder
- `src/phaze/templates/proposals/partials/proposal_table.html` -- confirmed 5-column table structure
- `src/phaze/templates/proposals/partials/proposal_row.html` -- confirmed row template with truncation + tooltip pattern
- `src/phaze/routers/execution.py` -- confirmed `start_execution` handler using arq
- `src/phaze/routers/proposals.py` -- confirmed router pattern with Depends(get_session), Jinja2Templates
- `src/phaze/main.py` -- confirmed router registration pattern via `app.include_router()`
- `.planning/phases/06-ai-proposal-generation/06-CONTEXT.md` -- confirmed directory conventions from Phase 6

### Secondary (MEDIUM confidence)
- PostgreSQL `func.concat` behavior with NULL -- standard SQL behavior, verified by PostgreSQL documentation
- HTML `<details>/<summary>` accessibility -- well-established web standard, no verification needed

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- no new dependencies, all libraries already in use
- Architecture: HIGH -- extending existing patterns (prompt, model, template, route), all code inspected
- Pitfalls: HIGH -- identified from direct code inspection of collision edge cases and LLM output normalization

**Research date:** 2026-03-31
**Valid until:** 2026-04-30 (stable -- no dependency changes, all internal code)
