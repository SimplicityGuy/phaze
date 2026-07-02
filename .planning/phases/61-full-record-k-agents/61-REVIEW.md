---
phase: 61-full-record-k-agents
reviewed: 2026-07-01T23:35:06Z
depth: standard
files_reviewed: 24
files_reviewed_list:
  - src/phaze/main.py
  - src/phaze/routers/admin_agents.py
  - src/phaze/routers/record.py
  - src/phaze/routers/search.py
  - src/phaze/routers/shell.py
  - src/phaze/services/agent_liveness.py
  - src/phaze/services/pipeline.py
  - src/phaze/services/search_queries.py
  - src/phaze/templates/admin/agents.html
  - src/phaze/templates/admin/partials/agents_table.html
  - src/phaze/templates/admin/partials/compute_lanes.html
  - src/phaze/templates/pipeline/partials/_file_table.html
  - src/phaze/templates/pipeline/partials/analyze_workspace.html
  - src/phaze/templates/pipeline/partials/empty_state.html
  - src/phaze/templates/record/record_body.html
  - src/phaze/templates/record/record_not_found.html
  - src/phaze/templates/search/partials/palette_results.html
  - src/phaze/templates/shell/partials/cmdk_modal.html
  - src/phaze/templates/shell/partials/record_host.html
  - src/phaze/templates/shell/shell.html
  - tests/test_dead_template_guard.py
  - tests/test_enrich_analyze_workspaces.py
  - tests/test_routers/test_search.py
  - tests/test_shell_routes.py
findings:
  critical: 1
  warning: 5
  info: 2
  total: 8
status: issues_found
---

# Phase 61: Code Review Report

**Reviewed:** 2026-07-01T23:35:06Z
**Depth:** standard
**Files Reviewed:** 24
**Status:** issues_found

## Summary

Phase 61 layers three UI features (per-file full-record slide-in, ⌘K palette, two-section
Agents page) over existing routers/services with a single sanctioned additive query
(`distinct_artists`). The security-sensitive surfaces the phase called out are clean:

- **SQL injection** — `distinct_artists()` and every `search()` filter use bound ORM
  params / `.ilike(f"%{query}%")` (the pattern is a bound value, never string-interpolated
  into SQL text). No injection surface introduced.
- **Template-path injection / BAC** — `GET /record/{file_id}` is a typed `uuid.UUID` param;
  every read is strictly `file_id`-scoped; the template name is a static literal. `/s/{stage}`
  is whitelisted against `STAGE_PARTIALS`. No path-splicing.
- **XSS** — DB-sourced cells autoescape through Jinja; the reused `_diff_row.html` uses
  `|tojson` in the Alpine `x-data` JS context (the prior-incident pattern is preserved);
  artist names in `hx-get`/`href` are `| urlencode`d. No new `|e`-in-JS-context regressions.
- **Write paths** — the router reads are read-only; no accidental mutation introduced in the
  Phase-61 routers.

However, the primary RECORD-01 interaction is broken on the default view, and several
correctness/quality defects exist in the new templates and helpers. Details below.

## Critical Issues

### CR-01: Analyze file-row "open record" click is inert on the default `/` view (record never opens)

**File:** `src/phaze/templates/pipeline/partials/analyze_workspace.html:38`, `src/phaze/templates/pipeline/partials/_file_table.html:48`, `src/phaze/templates/pipeline/partials/_workspace_scaffold.html:24-25`

**Issue:** The Analyze file-table rows carry the record-opener
`@click="$dispatch('record:open', { el: $el })"` (`_file_table.html:48`). Alpine v3 only
processes directives inside an `x-data` scope — `Alpine.start()` initializes **only** `[x-data]`
roots and their descendants. The scaffold `<section>` gets `x-data` **only when the caller
passes `x_data`** (`_workspace_scaffold.html:25`), and `analyze_workspace.html:38` calls
`ws.workspace(title="ANALYZE", subcount=subcount, actions=actions, cloud_cards=true)` **without
`x_data`**. `#stage-workspace` and all its ancestors (`<main>`, `<body>`) have no `x-data`
(`shell.html:150,164,165`). Therefore, on a direct/bookmark `GET /` (the default Analyze
landing view — the most common entry point), the file-row `@click` is never wired: clicking a
row fires the `hx-get="/record/{id}"` (HTMX is independent of Alpine) into the **hidden**
`#record-body`, but the `record:open` event is never dispatched, so `record_host.html`'s
`open` state stays `false` and the slide-in never becomes visible. The record simply does not
open.

This is confirmed by internal precedent — every sibling element that uses `@click`/`$dispatch`
or `$store` bindings explicitly adds an `x-data` scope for exactly this reason:
`discover_workspace.html:38` passes `x_data="{ scanOpen: false }"`, `empty_state.html:22` adds
a bare `x-data`, and the header's ⌘K trigger button carries a bare `x-data` next to its
`@click="$dispatch('cmdk:open')"` (`header.html:28`). Only the Analyze workspace omits it.
(The same omission also silently breaks the `x-text` subcount at `_workspace_scaffold.html:32`
on initial load.) The behavior is inconsistent: after an HX rail-swap to `/s/analyze` Alpine's
mutation observer re-`initTree`s the subtree and the click works — but the default `/` render
does not, so the feature appears intermittently broken.

The tests do not catch this: `test_analyze_file_table_lane_and_windows` only asserts the string
`"record:open"` is present in the HTML, not that the binding is functional.

**Fix:** Give the Analyze workspace an Alpine root, mirroring the other workspaces:

```jinja
{# analyze_workspace.html #}
{% call ws.workspace(title="ANALYZE", subcount=subcount, actions=actions, cloud_cards=true, x_data="{}") %}
```

or wrap the file table in a bare `x-data` div. Verify in a browser that clicking an Analyze
row on a fresh `GET /` opens the slide-in (not just after a rail navigation).

## Warnings

### WR-01: Not-found record returns 404, so HTMX will not swap the friendly "file no longer exists" fragment

**File:** `src/phaze/routers/record.py:55-61`, `src/phaze/templates/record/record_not_found.html`

**Issue:** For a missing/de-duplicated file the route returns `record_not_found.html` with
`status_code=404`. HTMX 2.x default `responseHandling` treats `4xx` responses as errors and
does **not** swap them (`{code:"[45]..", swap:false, error:true}`). The `#record-body` swap
target is loaded via `hx-get`, so the friendly fragment is rendered server-side but never
displayed — the panel keeps its previous (stale) content or stays empty while `htmx:responseError`
fires. The T-61-05 "friendly 404, never a 500/stack trace" intent is defeated at the client. (The
palette Files rows exercise this path since they do have a working Alpine scope.)

**Fix:** Return the not-found fragment with `status_code=200` (it is still a semantically valid
HTML fragment for the swap target), or add `hx-swap` error handling / an `htmx.config`
`responseHandling` rule so 404 bodies are swapped. Returning 200 for the fragment is the
smallest change and matches how HTMX consumers expect swap targets to behave.

### WR-02: Empty-state scan button shows the agent UUID instead of the agent name

**File:** `src/phaze/templates/pipeline/partials/empty_state.html:69`

**Issue:** The visible button label is `Scan {{ agent.id }}` (a raw UUID), while the card
header (`:47`) and the button's own `aria-label` (`:68`) correctly use `{{ agent.name }}`. The
partial's own comment describes the control as `"Scan {agent}"`. Operators see `Scan
3f2a...-uuid` instead of `Scan nox`. Clear copy/paste defect.

**Fix:**

```jinja
<button type="submit" class="{{ _scan_btn }}"
        aria-label="Scan {{ root }} on {{ agent.name }}">
    Scan {{ agent.name }}
</button>
```

### WR-03: ⌘K Artists group is non-functional — the artist row omits the `q` param, returning an empty palette

**File:** `src/phaze/templates/search/partials/palette_results.html:76-77`, `src/phaze/routers/search.py:47-65`

**Issue:** Each Artists row activates `hx-get="/search/?artist={{ a | urlencode }}"` targeting
`#cmdk-results`, but the request carries **only** `artist=` — no `q`. In `search_page`,
`results` is gated on `if q:` and `artists` on `if q and len(q) >= 2:`, so with `q is None`
both are empty. Clicking an artist therefore replaces the results with just the static Commands
group (the "No results found" block is also suppressed because it requires `query` truthy).
The Artists facet (RECORD-02 / D-05) does nothing useful.

**Fix:** Include the current query in the artist request (e.g. `hx-include` the palette input,
or embed `q={{ query | urlencode }}` in the URL), or repoint the artist row at a
route/branch that filters files by artist without requiring `q`. Confirm the resulting palette
shows that artist's files.

### WR-04: Record history is not chronologically ordered — two DESC lists are concatenated, not merged

**File:** `src/phaze/routers/record.py:93-104`

**Issue:** `exec_logs` (ordered `executed_at DESC`) and `tag_logs` (ordered `written_at DESC`)
are each sorted independently, then combined by list concatenation (`[... for e in exec_logs] +
[... for t in tag_logs]`). The merged `history` places **all** execution events before **all**
tag-write events regardless of their actual timestamps, so a tag write that happened after an
execution renders below it. The section is presented as a chronological per-file event list, so
the ordering is wrong whenever both event types exist.

**Fix:** Merge then sort the combined list by the `when` key:

```python
history.sort(key=lambda h: h["when"] or datetime.min.replace(tzinfo=UTC), reverse=True)
```

### WR-05: `_analyze_file_count` swallows exceptions without rolling back the session

**File:** `src/phaze/routers/shell.py:137-141`

**Issue:** On any error the helper returns the sentinel `1` but does **not** roll back the
session, diverging from the codebase-wide degrade discipline (every `pipeline.py` helper does
`except: log → guarded rollback → sentinel`, e.g. `_safe_count`, `get_stage_controls`). A failed
`COUNT(*)` leaves the async transaction in an aborted state for the remainder of the request; it
happens to be harmless here only because `build_dashboard_context` already materialized its reads
and the analyze render performs no further DB access — a fragile coincidence, not a guarantee.

**Fix:** Mirror the established pattern:

```python
    except Exception:
        logger.warning("analyze_file_count_degraded", exc_info=True)
        try:
            await session.rollback()
        except Exception:
            logger.warning("analyze_file_count_rollback_failed", exc_info=True)
        return 1
```

## Info

### IN-01: Record "Lane" tile hardcodes "local" for every file

**File:** `src/phaze/templates/record/record_body.html:48-51`

**Issue:** The facts-grid Lane tile always renders `🖥️ local`, so a file analyzed on the A1 or
k8s lane is mislabeled. `get_analyze_stage_files` already derives the real lane
(`pipeline.py:818-824`); the record route does not surface it. Low impact (cosmetic) but a
factual inaccuracy in a per-file detail view.

**Fix:** Pass the derived lane (or the file's `cloud_job` state) into the record context and
render it, or drop the tile until it can show real data.

### IN-02: "read-only" record embeds a write control via the reused timeline partial

**File:** `src/phaze/templates/record/record_body.html:59`, `src/phaze/templates/proposals/partials/analysis_timeline.html:9-15`

**Issue:** The phase describes the record body as a read-only snapshot, but the reused
`analysis_timeline.html` includes a "Deepen analysis" button that `hx-post`s to
`/pipeline/files/{file_id}/deepen` whenever `analysis.sampled` is truthy. This is an existing,
legitimate per-file action rather than a new write path, but it does surface a mutation control
inside the "read-only" surface — worth a conscious note given the phase's read-only framing.

**Fix:** None required if intended; otherwise gate the deepen control out of the record context.

---

_Reviewed: 2026-07-01T23:35:06Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
