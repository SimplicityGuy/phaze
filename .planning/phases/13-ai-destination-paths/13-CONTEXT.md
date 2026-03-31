# Phase 13: AI Destination Paths - Context

**Gathered:** 2026-03-31
**Status:** Ready for planning

<domain>
## Phase Boundary

LLM-generated destination paths alongside filenames, with batch collision detection before execution and a dedicated directory tree preview page in the admin UI. Extends the existing proposal pipeline (prompt template, Pydantic response model, approval UI) rather than building new infrastructure.

</domain>

<decisions>
## Implementation Decisions

### Path Generation Strategy
- **D-01:** Extend the existing `naming.md` prompt template with path generation rules. Single LLM call produces both `proposed_filename` and `proposed_path`. Keep rules in the markdown file for easy editing.
- **D-02:** Template-guided LLM approach — provide directory convention templates in the prompt, LLM picks the best template and fills values from available metadata.
- **D-03:** Path logic is a 3-step decision tree for the LLM:
  1. Figure out which category under `performances/` the file belongs in
  2. Determine which artist/festival/concert/radioshow
  3. For festivals/concerts, figure out the year and correct nested structure
- **D-04:** Album tracks go under a separate `music/{Artist}/{Album}/` tree — keeps studio releases separate from live performances.
- **D-05:** When the LLM can't determine a good path (too little metadata), leave `proposed_path` null and flag for manual review. Same behavior as v1 (file stays in place if no path proposed).
- **D-06:** Add `proposed_path` to the Pydantic structured output response model alongside `proposed_filename`. No separate LLM call needed.

### Collision Detection & Handling
- **D-07:** Batch collision check runs before execution — scan all approved proposals for duplicate destination paths (`proposed_path + proposed_filename`).
- **D-08:** Collisions block execution — affected proposals get a collision warning in the UI. User must resolve (reject one, or paths need to differ) before execution proceeds.
- **D-09:** No auto-suffixing or auto-resolution. Human-in-the-loop constraint applies to collisions too.

### Path Display in Approval UI
- **D-10:** New "Destination" column in the proposal table — shows the proposed path, truncated with tooltip for long paths. Visible at a glance without expanding the row.
- **D-11:** Null paths (no path proposed) display as a subtle gray "No path" badge in the Destination column. Makes it clear extraction ran but couldn't determine a path.

### Directory Tree Preview
- **D-12:** Dedicated `/preview` page showing the full directory tree of all approved proposals. Collapsible folders with file counts per directory. Linked from the approval page.
- **D-13:** Scope is approved proposals only — this is the "what will happen when I execute" view.

### Claude's Discretion
- Prompt template wording for path generation rules and examples
- Pydantic response model field additions (proposed_path type, validation)
- Directory tree rendering approach (server-side HTML vs Alpine.js collapsible)
- Tree page pagination/virtualization strategy for large approval sets
- Collision detection query design (SQL grouping vs application logic)
- How collision warnings display in the proposal table (badge, icon, row highlight)
- Truncation length for destination column and tooltip implementation
- Navigation link placement from approval page to tree preview

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project Configuration
- `CLAUDE.md` — Development setup, code quality rules, HTMX + Jinja2 + Tailwind stack
- `.planning/REQUIREMENTS.md` — PATH-01, PATH-02, PATH-03, PATH-04
- `.planning/PROJECT.md` — Naming format constraint, directory conventions, tech stack

### Existing Code (MUST READ)
- `src/phaze/prompts/naming.md` — Current LLM prompt template (extend with path generation rules)
- `src/phaze/services/proposal.py` — `build_file_context()` and `generate_batch()` (add proposed_path to response model)
- `src/phaze/models/proposal.py` — RenameProposal model (proposed_path column already exists as nullable Text)
- `src/phaze/services/execution.py` — Already uses `proposed_path` when present (line 159-165)
- `src/phaze/templates/proposals/list.html` — Approval page (add Destination column)
- `src/phaze/templates/proposals/partials/proposal_table.html` — Table partial (add column)
- `src/phaze/templates/base.html` — Base template (add nav link to tree preview)
- `src/phaze/routers/` — Router directory (add tree preview route, collision check endpoint)

### Prior Phase Context
- `.planning/phases/06-ai-proposal-generation/06-CONTEXT.md` — Naming format, prompt design, batch strategy, directory conventions in specifics section
- `.planning/phases/07-approval-workflow-ui/07-CONTEXT.md` — Table layout, expandable rows, HTMX patterns, keyboard shortcuts
- `.planning/phases/12-infrastructure-audio-tag-extraction/12-CONTEXT.md` — Tag-to-LLM integration (tags key in context), no prompt template changes for tags

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `RenameProposal.proposed_path` column — already exists, nullable Text, already wired into execution service
- `naming.md` prompt template — markdown file loaded at runtime, easy to extend with path rules
- `build_file_context()` — assembles per-file context including tags, analysis, companions
- Approval UI table with HTMX partials — established pattern for adding columns
- Execution service path routing — already constructs `output_path / proposed_path / proposed_filename`

### Established Patterns
- HTMX partial swaps for table updates (`hx-get`, `hx-swap`, `hx-target`)
- Jinja2 templates with `{% include %}` partials
- Tailwind CSS via CDN for styling
- Alpine.js for client-side interactivity (keyboard shortcuts, toggles)
- FastAPI APIRouter with `Depends(get_session)` for new routes
- Pydantic structured output from LLM responses

### Integration Points
- Extend `naming.md` with path generation rules and directory convention templates
- Add `proposed_path` to Pydantic LLM response model in proposal service
- Add Destination column to `proposals/partials/proposal_table.html`
- New `/preview` route and template for directory tree page
- New collision check logic in execution service (pre-execution batch scan)
- Collision warning display in proposal table (new partial or row annotation)

</code_context>

<specifics>
## Specific Ideas

- Directory conventions from Phase 6 specifics: `performances/artists/{Artist Name}/`, `performances/festivals/{Festival Name} {Year}/`, `performances/concerts/{Concert Name} {Year}/`, `performances/radioshows/{Radioshow Name}/`, `performances/raid party/{Date}/`
- Album tracks use a separate tree: `music/{Artist}/{Album}/`
- The `context_used` JSONB from Phase 6 already contains extracted metadata (event_name, source_type, venue, year) that the LLM can reference for path decisions
- Path generation is additive — existing filename generation stays identical, path is a new field alongside it

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 13-ai-destination-paths*
*Context gathered: 2026-03-31*
