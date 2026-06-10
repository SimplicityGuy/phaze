---
quick_id: 260609-on2
slug: poll-wire-recent-scans-table-and-stage-c
status: planned
---

# Quick Task: Poll-wire Recent Scans table + stage-card "files ready" counts

## Problem

On the Pipeline Dashboard (`templates/pipeline/dashboard.html`) only the stats bar
(`#pipeline-stats`) is wrapped in an HTMX poll (`hx-get="/pipeline/stats" hx-trigger="every 5s"`).
Two other live values are rendered once at page load and **never refresh**:

1. **Recent Scans "N / Z"** (`recent_scans_table.html:64`, `ScanBatch.processed_files / total_files`)
   — the `<section id="recent-scans">` is `{% include %}`d statically at `dashboard.html:14`, outside
   any polling container. During a RUNNING scan the row stays frozen (e.g. "5500 / —") while the
   Discovered badge climbs.
2. **Stage-card "files ready"** (`stage_cards.html:9,31`, `{{ stats.discovered }}` / `{{ stats.analyzed }}`)
   — rendered inside `#pipeline-stages` (`dashboard.html:22`) which has NO `hx-trigger`. Frozen at the
   page-load value even though it reads the SAME `stats` dict the polling stats bar already refreshes.

## Fix

### A. Recent Scans table self-polls (new GET endpoint)

1. Add a GET endpoint to `routers/pipeline_scans.py` (router prefix is `/pipeline/scans`):
   ```python
   @router.get("/recent", response_class=HTMLResponse)
   async def recent_scans_partial(request, session) -> HTMLResponse:
       rows = await build_recent_scans(session)
       return templates.TemplateResponse(request=request,
           name="pipeline/partials/recent_scans_table.html",
           context={"request": request, "recent_scans": rows})
   ```
   Reuse the existing `build_recent_scans` helper (already attaches `_agent_name`, `_elapsed_seconds`,
   `_seconds_since_progress`, `_is_stalled`). Resolves to `GET /pipeline/scans/recent`.
   IMPORTANT: place this route BEFORE the `@router.delete("/{batch_id}")` / any `/{batch_id}` route so
   `/recent` is not captured as a `batch_id` path param. (GET vs DELETE differ by method so collision is
   unlikely, but if a `GET /{batch_id}` exists, order matters — verify.)

2. In `recent_scans_table.html`, make the root `<section id="recent-scans">` self-arming:
   add `hx-get="/pipeline/scans/recent" hx-trigger="every 5s" hx-swap="outerHTML"`. Because the partial's
   own root carries these attrs, each swapped-in copy re-arms the poll (same self-referential pattern as
   `scan_progress_card.html`). The existing delete button already swaps `#recent-scans` via `outerHTML`
   and returns this same partial, so deletes keep the poll armed too.

### B. Stage-card "files ready" counts refresh via OOB on the existing stats poll

Do NOT add a blunt poll to `#pipeline-stages` — it contains `hx-post` buttons + Alpine `x-data` loading
state and `#analyze-response`/`#proposals-response` divs that hold enqueue result messages. Re-rendering
them every 5s would clobber an in-flight click / revert the "Enqueued N files" message. Instead use HTMX
out-of-band swaps piggybacked on the stats poll that already fires every 5s:

1. In `stats_bar.html` (returned by `GET /pipeline/stats`), append two OOB elements that mirror the
   stage-card counts — same tag/classes as the originals so the swap is visually identical:
   ```jinja
   {# OOB: refresh Pipeline Actions "files ready" counts on the same 5s tick WITHOUT
      re-rendering the interactive buttons (avoids clobbering in-flight enqueue responses). #}
   <p id="analyze-files-ready" hx-swap-oob="true" class="text-sm text-gray-400 dark:text-gray-500 mt-1">{{ stats.discovered }} files ready</p>
   <p id="proposals-files-ready" hx-swap-oob="true" class="text-sm text-gray-400 dark:text-gray-500 mt-1">{{ stats.analyzed }} files ready</p>
   ```
2. In `stage_cards.html`, add the matching ids to the existing count paragraphs:
   - line 9 `<p ...>{{ stats.discovered }} files ready</p>` → add `id="analyze-files-ready"`.
   - line 31 `<p ...>{{ stats.analyzed }} files ready</p>` → add `id="proposals-files-ready"`.

   Keep the class lists identical between the OOB element and the in-place element so a swap is seamless.

### Known caveat (out of scope, note only)

The button `:disabled="loading || {{ stats.discovered }} === 0"` binding is server-rendered once; if a
scan starts from exactly 0 discovered and files appear, the button stays disabled until a full reload.
This is an edge case (the dashboard is normally viewed with discovered > 0 during a scan) and is NOT the
reported issue — leave it; mention in the PR description as a known follow-up.

## Files

- `src/phaze/routers/pipeline_scans.py` — add `GET /recent` endpoint.
- `src/phaze/templates/pipeline/partials/recent_scans_table.html` — self-arming poll on root `<section>`.
- `src/phaze/templates/pipeline/partials/stats_bar.html` — two OOB count paragraphs.
- `src/phaze/templates/pipeline/partials/stage_cards.html` — add matching ids to count paragraphs.
- `tests/` — add a router test for `GET /pipeline/scans/recent` (200 + renders agent/path/files);
  if there are template/dashboard assertion tests, update them. Keep existing delete-endpoint tests green.

## Verification

- `uv run pytest` full suite passes (note: redis-dependent tests in `test_services/test_agent_task_router.py`,
  `test_execution_dispatch`, `test_agent_tracklists` fail in sandboxes without Redis on :6379 — that is a
  pre-existing environment limitation, NOT a regression; confirm any NEW failures are real).
- Coverage ≥ 85%.
- `pre-commit run --all-files` clean (ruff, mypy, yamllint, actionlint, bandit, etc.).
- Manual reasoning: during a RUNNING scan the Recent Scans "N / Z" and both "files ready" counts now
  tick every 5s in lockstep with the Discovered badge; clicking Run Analysis / Generate Proposals still
  shows its enqueue response without being reverted by the poll.
