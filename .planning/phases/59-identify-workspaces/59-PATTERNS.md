# Phase 59: Identify workspaces - Pattern Map

**Mapped:** 2026-06-30
**Files analyzed:** 5 (2 new templates, 1 new test, 2 modified source files; +1 optional service-helper modification)
**Analogs found:** 5 / 5 (every new file has an exact Phase-58 sibling)

> **Phase character:** presentation-only IA/template rewrite. Every analog is a Phase-58 deliverable.
> The planner's job is near-mechanical replication of the Phase-58 `metadata`/`fingerprint`/`analyze`
> wiring for the two `trackid`/`tracklist` placeholder stages. **No backend behavior change.**

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/phaze/routers/shell.py` (MODIFY: `STAGE_PARTIALS` + `_render_stage`) | router | request-response (read) | `shell.py` `metadata`/`fingerprint` branches (`:62-63`, `:116-129`) | exact (same file) |
| `src/phaze/services/pipeline.py` (MODIFY: 2 new read-only assembly helpers — A1/Open-Q1, optional) | service | CRUD (read-only SELECT) | `get_analyze_stage_files` (`:768-835`) | exact |
| `src/phaze/templates/pipeline/partials/trackid_workspace.html` (CREATE) | component (Jinja fragment) | request-response (read-only render) | `fingerprint_workspace.html` (no-trigger variant) + `analyze_workspace.html` file-table block (`:95-143`) | exact |
| `src/phaze/templates/pipeline/partials/tracklist_workspace.html` (CREATE) | component (Jinja fragment) | request-response + event-driven (bulk triggers) | `analyze_workspace.html` (lane-card grid `:40-81` + file table) + `fingerprint_workspace.html` (trigger button `:25-32`) | exact |
| `tests/test_identify_workspaces.py` (CREATE) | test | request-response assertions | `tests/test_enrich_analyze_workspaces.py` | exact |

**Existing endpoints/helpers wired VERBATIM (no new code, reference only):**
`POST /pipeline/search-tracklists` (`pipeline.py:1130`), `POST /pipeline/scrape-tracklists` (`:1258`), `POST /pipeline/match-tracklists` (`:1289`); `get_untracked_files` (`services/pipeline.py:1183`), `get_scrape_pending_tracklists` (`:668`), `get_match_pending_tracklists` (`:682`), `get_stage_progress` (`:294`), `_get_tracklist_stats` (`tracklists.py:51`).

---

## Pattern Assignments

### `src/phaze/routers/shell.py` (router, request-response) — MODIFY

**Analog:** the `metadata`/`fingerprint` entries + branches in the SAME file (added in Phase 58).

**STAGE_PARTIALS map** — replace two `_STAGE_PLACEHOLDER` values with static-literal partial paths (T-57-01: `stage` NEVER spliced into a template path). Current state (`shell.py:62-70`):
```python
    "metadata": "pipeline/partials/metadata_workspace.html",
    "fingerprint": "pipeline/partials/fingerprint_workspace.html",
    "analyze": "pipeline/partials/analyze_workspace.html",
    "trackid": _STAGE_PLACEHOLDER,      # ← replace with "pipeline/partials/trackid_workspace.html"
    "tracklist": _STAGE_PLACEHOLDER,    # ← replace with "pipeline/partials/tracklist_workspace.html"
```
Keep the same Phase-58 comment convention (cite phase + WORK id + T-57-01 + supersede-in-place).

**`_render_stage` DB-context branch** — append two `elif` branches mirroring `metadata`/`fingerprint` (`shell.py:116-129`). `oob_counts` stays `False` (Pitfall 5/Pitfall 3); reads are degrade-safe at the service layer (no router try/except). The exact pattern to copy:
```python
    elif stage == "metadata":
        context["metadata_files"] = await get_metadata_pending_files(session)
    elif stage == "fingerprint":
        context["fingerprint_files"] = await get_fingerprint_pending_files(session)
    # Phase 59 adds (read-only assembly — no enqueue, no commit):
    #   elif stage == "trackid":
    #       context["trackid_files"] = await get_trackid_stage_files(session)
    #   elif stage == "tracklist":
    #       context["tracklist_steps"] = await get_stage_progress(session)   # + pending counts
    #       context["tracklist_sets"] = await get_tracklist_set_rows(session)
```
**Imports to extend** (`shell.py:33`): add the two new helper names to the existing
`from phaze.services.pipeline import get_fingerprint_pending_files, get_metadata_pending_files` line (combine-as-imports, force-sort-within-sections).

**Fragment-vs-full fork** (`shell.py:131-133`) — UNCHANGED; both new fragments ride it automatically (HX → `_stage_fragment.html` bare; direct → `shell.html`).

---

### `src/phaze/services/pipeline.py` (service, read-only CRUD) — MODIFY (A1/Open-Q1: helper vs inline)

**Analog:** `get_analyze_stage_files` (`services/pipeline.py:768-835`) — the Phase-58 precedent for a NEW read-only assembly helper that returns a list of presentation dicts. Replicate its conventions VERBATIM:

**Degrade-safe SAVEPOINT wrapper** (`:790-813`) — the load-bearing pattern; copy exactly so a DB hiccup on a hot read returns `[]`, never 500s the page/poll:
```python
    try:
        async with session.begin_nested():
            stmt = (
                select(...)
                .select_from(FileRecord)
                .outerjoin(...)            # LEFT JOIN the per-engine / per-tracklist sidecars
                .order_by(...)
            )
            rows = (await session.execute(stmt)).all()
    except Exception:
        logger.warning("trackid_stage_files_degraded", exc_info=True)   # rename per helper
        return []
```

**Dict-shaping loop** (`:815-835`) — derive presentation fields from the joined columns and append plain dicts (NOT ORM objects), so the template reads `f["..."]`-style keys. For `get_trackid_stage_files` the derived per-row shape is: `audfprint` status + `panako` status + tracklist match-state + confidence.

**Module-level state-list constant** convention (`:759-765`, `_ANALYZE_STAGE_STATES`) — if a query needs an explicit state/engine set, declare it as a module constant with a comment, same as Phase 58.

**Engine-key + status mapping (Pitfall 1 — CRITICAL, RESEARCH §1):** query keys are lowercase `"audfprint"`/`"panako"`; done ⟺ `status == "success"` (tolerate `"completed"` defensively), failed ⟺ `"failed"`, pending ⟺ **no row** for `(file_id, engine)` (LEFT-join / dict-lookup, default "pending"). Do NOT key "done" on `"completed"` and do NOT derive from `get_stage_progress`.

**Best-candidate ordering (D-04):** reuse `Tracklist.match_confidence.desc().nulls_last()` — the exact ordering `list_tracklists` already uses (`tracklists.py:101`). Linked ⟺ `Tracklist.file_id IS NOT NULL` → "matched" + its `match_confidence`; else highest-candidate `match_confidence` → "candidate"; else "no match".

**Per-set coverage (D-07):** `N/M tracks confident` = count of `TracklistTrack.confidence` (over the linked tracklist's tracks) above the existing confidence threshold / `M` total. Read-only over `TracklistVersion` → `TracklistTrack`.

> **A1 note:** inline composition in `_render_stage` is equally valid (read-only either way). Recommend the helper for unit-testability (matches Phase 58 + the Validation Architecture). Planner decides; behavior identical.

---

### `src/phaze/templates/pipeline/partials/trackid_workspace.html` (component) — CREATE

**Analog:** `fingerprint_workspace.html` (scaffold + single table) — but with the **`actions` slot EMPTY** (Track-ID is a read-only consolidated view; the prototype's `IDENTIFY PENDING` button is the dropped AcoustID flow, IDENT-03/deferred).

**Scaffold composition** (copy `fingerprint_workspace.html:20,34,54`):
```jinja
{% import "pipeline/partials/_workspace_scaffold.html" as ws %}
{% set subcount = '`${$store.pipeline.fingerprintDone} fingerprinted · ${$store.pipeline.tracklistDone} with a tracklist match`' %}
{% call ws.workspace(title="TRACK-ID", subcount=subcount) %}   {# NO actions= arg — empty slot #}
    <div class="p-6">
        ... _file_table.html include ...
    </div>
{% endcall %}
```

**Combined table** (copy the `_file_table.html` feed loop from `fingerprint_workspace.html:37-52`, re-columned per D-03):
```jinja
{% set columns = ["File", "audfprint", "Panako", "Tracklist", "Confidence"] %}
{% set ns = namespace(rows=[]) %}
{% for f in trackid_files %}
    {% set _ = ns.rows.append([
        {'text': f.filename, 'mono': True, 'title': f.path},   {# helper row keys: filename / path (match Plan 01 dict shape) #}
        {'text': af_word, 'color': af_color},      {# done/failed/pending — engine colors #}
        {'text': pk_word, 'color': pk_color},
        {'text': match_word, 'color': match_color},
        {'text': conf_text, 'mono': True, 'color': conf_color},
    ]) %}
{% endfor %}
{% set rows = ns.rows %}
{% set empty_heading = "Nothing to identify" %}
{% set empty_body = "No discovered files carry a fingerprint or tracklist signal yet. Run Fingerprint or Tracklist Search first." %}
{% set row_id_prefix = "trackid-row" %}
{% include "pipeline/partials/_file_table.html" %}
```

**Cell contract** (`_file_table.html:9-23`): every cell is `{text, mono?, title?, color?}`; `text` is ALWAYS autoescaped (NEVER `| safe`) — DB→render trust boundary (ASVS V5). Status cells carry a **word + color**, never hue-only (WCAG 1.4.1). Color classes per UI-SPEC: done `text-emerald-600 dark:text-emerald-300`, failed `text-rose-600 dark:text-rose-400`, pending `text-gray-500 dark:text-gray-400`; confidence tiers ≥90 emerald / 70-89 amber / <70 gray.

**Inert rows:** `row_id_prefix` gives `cursor-pointer` + stable id, click UNBOUND (D-06/R-1) — the partial already provides this; add NO `hx-get`.

---

### `src/phaze/templates/pipeline/partials/tracklist_workspace.html` (component) — CREATE

**Analog:** `analyze_workspace.html` — the closest structural sibling: a `grid grid-cols-3 gap-4 p-6` card row on top (lane cards → step cards) + a `_file_table.html` below.

**Three step cards (Pattern B)** — follow the `_lane_card.html` visual shape but extend it with a per-step trigger. The grid wrapper + per-card context-set idiom from `analyze_workspace.html:40-81`:
```jinja
<div id="tracklist-steps" class="grid grid-cols-3 gap-4 p-6">
  {# card shell per _lane_card.html: rounded-xl border phaze-border bg p-4, title row + mono numeral #}
  {# title "N · 🔎 SEARCH" (Inter 400) · server-rendered done/total (Search → "done / —") #}
  {# busy pill: <span x-show="$store.pipeline.searchBusy > 0" class="text-blue-300">· running…</span> #}
  {# + ALL trigger button (see below) #}
</div>
```
Step counts are **server-rendered at fragment render time** (like the Phase-58 k8s `capacity_value`, `_lane_card.html:38`) from `get_stage_progress` done/total + the `get_*_pending_*` helpers. Busy pills bind to **existing** store keys `searchBusy`/`scrapeBusy`/`matchBusy` (no new key — Pitfall 3).

**Per-step ALL trigger button (D-06)** — copy the R-4-guarded button VERBATIM from `fingerprint_workspace.html:22-32`, swapping endpoint/target/copy per card. The `_btn` class string is identical across all Phase-58 workspaces; copy it verbatim:
```jinja
{% set _btn = 'inline-flex items-center justify-center h-9 px-3 rounded-lg border border-gray-300 dark:border-phaze-border bg-white dark:bg-phaze-panel text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-white/5 font-jura text-xs font-medium uppercase tracking-wider focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50 disabled:cursor-not-allowed' %}
<button type="button"
        hx-post="/pipeline/scrape-tracklists" hx-target="#scrape-trigger-response" hx-swap="innerHTML"
        hx-confirm="Enqueue scraping for all pending tracklists?"
        :disabled="$store.pipeline.scrapeBusy > 0"
        class="{{ _btn }}">SCRAPE ALL</button>
<div id="scrape-trigger-response" class="px-6 pt-4 empty:hidden"></div>
```

**Trigger → endpoint → response-partial map (Pitfall 4 — asymmetry):**

| Card | `hx-post` | Busy gate | Response partial (endpoint-chosen) | Sink id |
|------|-----------|-----------|-------------------------------------|---------|
| 🔎 SEARCH ALL | `/pipeline/search-tracklists` | `searchBusy` | `trigger_response.html` (action `"tracklist search"`, HAS `no_active_agent` branch) | `#search-trigger-response` |
| 📄 SCRAPE ALL | `/pipeline/scrape-tracklists` | `scrapeBusy` | `trigger_tracklist_response.html` (action `"scraping"`, NO agent branch) | `#scrape-trigger-response` |
| 🔗 MATCH ALL | `/pipeline/match-tracklists` | `matchBusy` | `trigger_tracklist_response.html` (action `"matching"`) | `#match-trigger-response` |

The endpoint picks its own partial; the fragment only supplies a distinct `*-trigger-response` sink per card. **No single "run chain" button** (D-06). **Do NOT** build the prototype breadcrumb stepper (UI-SPEC Pattern B note).

**Per-set table (Pattern C)** — below the cards (`analyze_workspace.html:100-143` is the `border-t … p-6` + `_file_table.html` block to copy):
```jinja
<div class="border-t border-gray-200 p-6 dark:border-phaze-border">
    {% set columns = ["Set", "Tracklist", "Tracks", "Matched to file"] %}
    ... feed ns.rows from tracklist_sets (mono N/M coverage cell, D-07) ...
    {% set empty_heading = "No tracklists yet" %}
    {% set empty_body = "Run SEARCH ALL to discover 1001Tracklists matches for your live sets." %}
    {% set row_id_prefix = "tracklist-row" %}
    {% set table_id = "tracklist-set-table" %}
    {% include "pipeline/partials/_file_table.html" %}
</div>
```

**ANTI-PATTERN (UI-SPEC Typography + RESEARCH):** do NOT reuse `tracklists/partials/confidence_badge.html` / `status_badge.html` — they render `font-semibold` green/yellow/red pills, violating the two-weight + C3 emerald/amber/blue/gray contract. Render status as colored words / mono numerals through `_file_table.html` cells.

---

### `tests/test_identify_workspaces.py` (test) — CREATE

**Analog:** `tests/test_enrich_analyze_workspaces.py` — copy its whole shape.

**Module structure to mirror:**
- `from __future__ import annotations`; async httpx `client` fixture + Postgres `session` fixture (already in conftest).
- Module-level `_seed_*` ORM helpers (inserts only, `session.add` + `await session.commit()` + `await session.refresh()`, no backend change) — copy `_seed_file` (`:56-82`) verbatim; add `_seed_fingerprint_result(file_id, engine, status)`, `_seed_tracklist(file_id, match_confidence, …)`, `_seed_tracklist_version(tracklist_id)`, `_seed_tracklist_track(version_id, confidence)`.
- A `_WORKSPACE_STAGES = ["trackid", "tracklist"]` constant (analog `:45`).

**Test functions to replicate (RESEARCH Test Map → Wave 0 gaps):**

| Test | Mirrors | Key assertions |
|------|---------|----------------|
| `test_identify_fragments_are_bare` | `test_stage_fragment_is_bare` (`:139-152`) | `<html`/`<head` absent for both `/s/trackid` & `/s/tracklist` HX |
| `test_identify_single_poll_discipline` | `test_single_poll_discipline` (`:154-177`) | shell fires exactly one `hx-get="/pipeline/stats"`; neither fragment has `hx-trigger="every` / `setInterval` |
| `test_trackid_table_signals` | `test_analyze_file_table_lane_and_windows` (`:387-441`) | one combined table; audfprint/Panako status words; tracklist match/confidence; inert rows (`hx-get` absent) |
| `test_trackid_success_renders_done` (neg) | (Pitfall 1 guard) | seed `FingerprintResult(status="success")` → row renders "done", NOT "pending" |
| `test_tracklist_step_cards_and_triggers` | `test_metadata_trigger_all_wired` (`:280-326`) | 3 cards; `hx-post` to all three endpoints; R-4 `hx-confirm` + `:disabled` on each `*Busy`; no chain button |
| `test_tracklist_per_set_coverage` | `test_analyze_file_table…` | per-set table renders N/M from `TracklistTrack.confidence`; inert rows |

**Scoped-substring assertion idiom** (copy from `:416-417`): `tbl = body[body.index('id="tracklist-set-table"'):]` then assert within `tbl`.

**Existing guards (already pass once `STAGE_PARTIALS` points at the new fragments — do NOT modify):** `tests/test_dead_template_guard.py::test_no_orphan_templates`, `tests/test_shell_routes.py::test_rail_nodes_wired`.

---

## Shared Patterns

### Workspace scaffold (the spine every workspace composes)
**Source:** `src/phaze/templates/pipeline/partials/_workspace_scaffold.html:24-42`
**Apply to:** both new templates.
- `{% import "pipeline/partials/_workspace_scaffold.html" as ws %}` then `{% call ws.workspace(title=…, subcount=…, actions=…) %}`.
- Emits EXACTLY ONE `<h1 tabindex="-1">` focus target (R-5 / the test's `body.count('tabindex="-1"') == 1`).
- Auto-includes `_workspace_poll_seeds.html` (the OOB seed host — all needed targets already exist; add none).
- `subcount` is a JS expression string bound via `x-text` against `$store.pipeline`.
- `actions` is a block-set (`{% set actions %}…{% endset %}`); **omit it for Track-ID** (empty slot).

### Generic file table (autoescape trust boundary + inert rows + empty state)
**Source:** `src/phaze/templates/pipeline/partials/_file_table.html:9-51`
**Apply to:** both new templates (Track-ID combined table; Tracklist per-set table).
- Context: `columns` (list[str]), `rows` (list of cell-dict lists), `empty_heading`/`empty_body`, `row_id_prefix`, optional `table_id`.
- `text` ALWAYS autoescaped, NEVER `| safe` (XSS V5). `color` = caller class string. `mono` → `font-mono text-xs` + truncate.
- Rows: `cursor-pointer` + stable id, click UNBOUND (D-06/R-1).

### R-4 bulk-enqueue guard (Tracklist triggers only)
**Source:** `fingerprint_workspace.html:25-32` (button) + RESEARCH Code Examples
**Apply to:** all three Tracklist ALL triggers.
- `hx-confirm="…"` + `:disabled="$store.pipeline.{searchBusy|scrapeBusy|matchBusy} > 0"`.
- Guards the over-enqueue / Phase-34 doubling incident; endpoints are also deterministic-keyed + idempotent.
- Track-ID has no trigger → no guard there.

### Trigger endpoints (wired verbatim, zero new backend)
**Source:** `src/phaze/routers/pipeline.py:1130` / `:1258` / `:1289`
**Apply to:** the three Tracklist step-card buttons. Each is already routed via `enqueue_router.resolve_queue_for_task` (Phase-30), background-enqueued, deterministic-keyed. The fragment only supplies `hx-post` + a sink id; the endpoint chooses its response partial (Pitfall 4 asymmetry — see map above).

### Fingerprint status vocabulary (Pitfall 1 — load-bearing)
**Source:** `src/phaze/models/fingerprint.py:14-25` + RESEARCH §1
**Apply to:** the Track-ID assembly helper/branch.
- `FingerprintResult` = `file_id` + `engine` + `status` + `error_message` only — **no score column**.
- engine values lowercase `"audfprint"`/`"panako"`; status values `"success"`/`"failed"` (NEVER `"completed"`).
- done ⟺ `"success"` (tolerate `"completed"`), failed ⟺ `"failed"`, pending ⟺ no `(file_id, engine)` row (unique `ix_fprint_file_engine`).

### Degrade-safe read helper (if helper route chosen)
**Source:** `src/phaze/services/pipeline.py:768-835` (`get_analyze_stage_files`)
**Apply to:** `get_trackid_stage_files` / `get_tracklist_set_rows`.
- `async with session.begin_nested():` SAVEPOINT; `except Exception: logger.warning(...); return []`.
- Return list of plain dicts, not ORM objects. No `session.commit`, no enqueue, no schema change.

---

## No Analog Found

None. Every new file maps to an exact Phase-58 sibling. The only genuinely new logic is the two read-only row-assembly shapes (Track-ID per-file; Tracklist per-set), and even those follow the `get_analyze_stage_files` precedent verbatim.

---

## Metadata

**Analog search scope:** `src/phaze/routers/{shell,pipeline,tracklists}.py`, `src/phaze/services/pipeline.py`, `src/phaze/models/{fingerprint,tracklist}.py`, `src/phaze/templates/pipeline/partials/` (all 4 reusable partials + 4 Phase-58 workspaces + 2 trigger responses), `tests/test_enrich_analyze_workspaces.py`.
**Files scanned:** 13 source/template/test files (all line numbers verified this session).
**Pattern extraction date:** 2026-06-30
