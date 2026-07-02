# Phase 61: Full record + ⌘K + Agents - Pattern Map

**Mapped:** 2026-07-01
**Files analyzed:** 14 (7 new · 7 modified)
**Analogs found:** 14 / 14 (every seam has an in-repo analog — this is a composition phase)

> This is a v7.0 IA/presentation phase. Four new surfaces COMPOSE existing partials/endpoints; the only
> sanctioned backend touches are **two read-only queries** (`distinct_artists`, `classify_compute_lanes`).
> Every analog below was read against the working tree on 2026-07-01. Line numbers verified.

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/phaze/routers/record.py` (NEW) | route | request-response (read-only fragment) | `src/phaze/routers/shell.py` `_render_stage` + `proposals.py:259` `proposal_timeline` | exact (fragment fork + file_id-scoped read) |
| `src/phaze/templates/shell/partials/record_host.html` (NEW) | component (persistent chrome host) | event-driven (Alpine open/close) | `shell/partials/cmdk_modal.html` | exact (persistent-host + x-trap precedent) |
| `src/phaze/templates/record/…record body partials` (NEW) | component | transform (compose reads → HTML) | `proposals/partials/analysis_timeline.html` + `pipeline/partials/_diff_row.html` | exact (reused verbatim) |
| `src/phaze/templates/shell/partials/cmdk_modal.html` (MODIFY) | component | request-response (grouped HX results) | itself (Phase 57 skeleton) + `search.py:39-82` HX fork | exact (extend in place) |
| `src/phaze/routers/search.py` (MODIFY — add grouped branch) | route | CRUD-read | itself `search_page` HX branch | exact |
| `src/phaze/services/search_queries.py` (MODIFY — add `distinct_artists`) | service | CRUD-read (SELECT DISTINCT) | `search_queries.py:167` `get_summary_counts` | exact (same file, same shape) |
| `src/phaze/routers/admin_agents.py` (MODIFY — 2nd section) | route | request-response + self-poll | itself `_load_agents`/`page`/`table_partial` | exact |
| `src/phaze/services/agent_liveness.py` OR `services/pipeline.py` (MODIFY — add `classify_compute_lanes`) | service | CRUD-read (aggregation) | `services/pipeline.py:1116` `get_inadmissible_count` + `:1159` `get_cloud_phase_counts` | exact (degrade-safe count precedent) |
| `src/phaze/templates/admin/agents.html` (MODIFY — 2nd section) | component | transform | itself (static k8s note, Phase 56) | role-match |
| `src/phaze/templates/…/empty_state.html` (NEW) | component | transform (count==0 branch) | `pipeline/partials/discover_workspace.html` + `scan_path_picker.html` | role-match (reuse scan form) |
| `src/phaze/routers/shell.py` (MODIFY — count==0 branch + record_host include) | route | request-response | itself `_render_stage` (analyze branch) | exact |
| `src/phaze/templates/shell/shell.html` (MODIFY — focus plugin + record_host include) | config/component | — | itself `:32-39` head + `:172` cmdk include | exact |
| `src/phaze/templates/base.html` (MODIFY — focus plugin) | config | — | `shell.html:32-39` (identical block) | exact |
| `tests/test_record_palette_agents.py` (NEW) | test | request-response assertion | `tests/test_shell_routes.py` | exact |
| `tests/test_base_html_sri.py` (MODIFY — scan shell.html too) | test | — | itself `_extract_cdn_scripts:57` | exact (parametrize over both templates) |

---

## Shared Patterns

### SP-1 — Fragment-vs-full fork (bare HTMX fragment, `oob_counts=False`)
**Source:** `src/phaze/routers/shell.py:122-238` (`_render_stage`)
**Apply to:** the new record route, the empty-state render, and (implicitly) the grouped ⌘K results.
The fork mirrors `search.py:73-77` verbatim: an `HX-Request: true` swap returns a content-only fragment that
NEVER extends `base.html`; a direct nav returns full chrome. Every new fragment render keeps `oob_counts=False`
(Pitfall 5 — duplicate-id OOB collision with the DAG seeds).
```python
# shell.py:236-238 — the exact fork to copy
if request.headers.get("HX-Request") == "true":
    return templates.TemplateResponse(request=request, name="shell/_stage_fragment.html", context=context)
return templates.TemplateResponse(request=request, name="shell/shell.html", context=context)
```

### SP-2 — file_id-scoped read (broken-access-control mitigation, T-31-06-02)
**Source:** `src/phaze/routers/proposals.py:259-308` (`proposal_timeline`)
**Apply to:** the record route (RECORD-01). Take a typed UUID path param, resolve/scope every read strictly by
that `file_id`, 404 on miss (friendly fragment). The timeline assembly is reusable verbatim — re-scope by
`file_id` instead of `proposal → file_id`:
```python
# proposals.py:276-290 — the reads the record route re-scopes by file_id directly
stmt = select(AnalysisWindow).where(AnalysisWindow.file_id == file_id).order_by(AnalysisWindow.tier, AnalysisWindow.window_index)
windows = list((await session.execute(stmt)).scalars().all())
fine = [w for w in windows if w.tier == "fine"]
coarse = [w for w in windows if w.tier == "coarse"]
# 1:1 AnalysisResult for the "Sampled" badge; scalar_one_or_none() → None renders nothing (never errors)
analysis = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one_or_none()
```
Renders `proposals/partials/analysis_timeline.html` — the ready-made multi-lane SVG (BPM/Key/Energy over
fine/coarse windows + sampled badge + deepen button). Do NOT hand-roll a new timeline.

### SP-3 — Degrade-safe read aggregation (`_safe_count` / try-except → default)
**Source:** `src/phaze/services/pipeline.py:1116-1136` (`get_inadmissible_count`) + `:1159-1184` (`get_cloud_phase_counts`)
**Apply to:** `classify_compute_lanes` (D-07). The CloudJob liveness aggregation is a near-identical count read;
degrade to `("IDLE", 0)` — NEVER a DEAD/red state (KDEPLOY-04). The exact in-flight predicate already exists:
```python
# pipeline.py:1131-1134 — the running/inflight predicate to reuse for ACTIVE / WAITING
select(func.count(CloudJob.id)).where(
    CloudJob.inadmissible.is_(True),
    CloudJob.status.in_([CloudJobStatus.SUBMITTED.value, CloudJobStatus.RUNNING.value]),
)
```
Status members verified at `models/cloud_job.py:38-46` (`RUNNING="running"`, `SUBMITTED="submitted"`,
`inadmissible` bool at `:88`). ACTIVE = ≥1 `status=running`; WAITING = ≥1 `submitted` + `inadmissible=true`; IDLE = none.

### SP-4 — Server-side classify → inject on the row (transient attr)
**Source:** `src/phaze/services/agent_liveness.py:68-114` (`classify`/`sort_key`) + `admin_agents.py:59-77` (`_load_agents`)
**Apply to:** Agents Section 1 (reuse verbatim) AND the mental model for Section 2 (classify server-side, never
client-side). `_load_agents` injects `a._status = classify(a, now)` on a transient attr then sorts — copy this
shape for the compute lane (compute a `("ACTIVE"|"WAITING"|"IDLE", n)` tuple server-side, render it).

### SP-5 — XSS: autoescape + `|tojson` in Alpine JS contexts (NOT `|e`)
**Source:** `src/phaze/templates/pipeline/partials/_diff_row.html:32,64,71,74`
**Apply to:** record header/facts/history + palette rows. Every DB-sourced cell (filename, path, artist, tag) is
autoescaped; in `x-data`/`:aria-label` JS-attribute contexts use `|tojson` (the Phase 60 apostrophe-filename XSS
class — "Guns N' Roses" broke out of `|e`). `_diff_row.html` already models it: `x-data='{ editing:false, val:{{ after|tojson }} }'`.

### SP-6 — Single `/pipeline/stats` 5s poll, snapshot body, counts-only OOB (D-02 / R-2)
**Source:** `src/phaze/templates/shell/shell.html:187-191` (`#pipeline-stats`) + the `_render_stage` `oob_counts=False` discipline
**Apply to:** record + palette + agents. Add NO new loop, NO `hx-trigger="every"` inside the record, NO
`setInterval`. Any live badge binds to a GLOBAL `$store.pipeline` key. NEVER put `hx-swap-oob` on an
approval-row subtree — an in-progress inline edit must survive the poll.

---

## Pattern Assignments

### `src/phaze/routers/record.py` (NEW — route, read-only fragment) — RECORD-01

**Analogs:** `shell.py:122-256` (fragment fork + whitelist route) · `proposals.py:242-308` (`row_detail` + `proposal_timeline`, file_id-scoping)

**Route shape (Research OQ-3 recommendation):** `GET /record/{file_id}` with a typed `uuid.UUID` path param
(FastAPI-validated — closes template-path/BAC surface). 404 → friendly fragment (UI-SPEC copy), close/focus
contract still applies.

**Imports pattern** (copy from `proposals.py` header + `shell.py:25-31`):
```python
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from phaze.database import get_session
from phaze.models.analysis import AnalysisResult, AnalysisWindow
```

**Compose these existing reads (all read-only, scope by `file_id`):**
- Timeline: SP-2 assembly above → `proposals/partials/analysis_timeline.html`.
- Metadata diff + pending approvals: `_diff_row.html` (below) + the SAME Phase 60 approve/edit/undo routes
  (`proposals.py:168/193/218` PATCH; `proposals.py:/{id}/edit` PATCH; `tags.py:309` `POST /tags/{id}/write`).
- History: `ExecutionLog` (`models/execution.py`) + `TagWriteLog` (`models/tag_write_log.py`) direct read, or
  `/audit/` scoped to the file (Discretion — pick cleanest read-only).

**Fragment discipline:** SP-1 (`oob_counts=False`, bare fragment — no `extends base.html`).

---

### `src/phaze/templates/shell/partials/record_host.html` (NEW — persistent chrome host) — RECORD-01

**Analog:** `shell/partials/cmdk_modal.html` (the persistent-host + Alpine open/close + focus-return precedent)

The record host is a NEW sibling `{% include %}` in `shell.html` (next to `cmdk_modal.html` at `:172`), OUTSIDE
`#stage-workspace`, so `x-trap` initializes at page load and survives rail swaps. Copy `cmdk_modal.html`'s
`x-data` open/close + `$nextTick` focus + Esc-return shape; add `x-trap.inert.noscroll="open"` on the panel and a
record-body container that re-inits Alpine after the HTMX body swap (Pitfall 3):

```html
{# record body swap target — _diff_row.html islands need initTree after swap (Pitfall 3) #}
<div id="record-body" hx-on::after-swap="if(window.Alpine) Alpine.initTree(this)"></div>
```
A file row opens it: `hx-get="/record/{{ file_id }}" hx-target="#record-body" hx-swap="innerHTML" @click="$dispatch('record:open', {el: $el})"`.
Panel geometry (UI-SPEC): `absolute inset-y-4 right-4 w-[760px] max-w-[94vw]`, backdrop `bg-black/60`,
`role="dialog" aria-modal="true"`, `aria-label` bound to the file name via `|tojson`.

**Close/focus-return contract** (from `cmdk_modal.html:22-26`): record the opener element; on Esc/backdrop/✕
return focus to it (fallback `#stage-workspace`).

---

### Record body sections (NEW partials) — reuse verbatim, do NOT restyle internals

| Section | Reused analog | Note |
|---------|---------------|------|
| Windowed timeline | `proposals/partials/analysis_timeline.html` | SP-2; re-scoped by `file_id` |
| Metadata diff + pending approvals | `pipeline/partials/_diff_row.html` | pass `approve_url`/`skip_url`/`undo_url`/`edit_url` + `approve_method`; `row_id_prefix="record-row"` (R-6 self-target) |
| Identity | `proposals/partials/row_detail.html` (via `get_proposal_with_file`) | Discretion: reuse vs restyle |

**`_diff_row.html` contract** (`:7-19`): caller supplies `row_id_prefix`, `pid`, `file`, `before`, `after`,
`approve_url`/`skip_url`/`undo_url`/`edit_url`, `approve_method`, `edit_facet`, `row_state`. Every control
`hx-target`s `#{row_id_prefix}-{pid}` only (R-6). The row carries `x-data='{editing:false,...}'` → **the record
body MUST run `Alpine.initTree` after swap** (Pitfall 3, wired in `record_host.html` above).

---

### `src/phaze/templates/shell/partials/cmdk_modal.html` (MODIFY) — RECORD-02

**Analog:** itself (Phase 57 skeleton, read above). Keep the open/close/`$nextTick`-focus/`?palette=1`/
Esc→`#cmdk-trigger` contract (`:16-32`). ADD:
- `x-trap.inert.noscroll="open"` on the panel (`:34-39`) — replaces the skeleton's "basic focus management".
- `x-data` roving state `{ q, results, activeIndex, items[] }`; debounced `x-on:input` → `hx-get` grouped
  results endpoint swapping ONLY the results body (Research Pattern 2).
- ARIA listbox: input `role="combobox" aria-activedescendant`; results `role="listbox"`; rows `role="option"`
  `:aria-selected`; group headers `role="presentation"`.
- `↑/↓` roving index over the flat ordered list (skip headers), `Enter` activates `items[activeIndex]`.

Results rows are **static `option` markup** (no Alpine islands) → **no `initTree` needed** for the palette
(contrast the record body).

---

### `src/phaze/routers/search.py` + `services/search_queries.py` (MODIFY) — RECORD-02 / D-05

**Analog for the grouped branch:** `search.py:39-84` (existing HX fork; renders flat `results_content.html`).
Add a grouped-results template + a distinct-artist read. The `artist=` filter param already exists
(`search.py:23` → `search(artist=…)` at `search_queries.py:75`).

**Analog for `distinct_artists` (NEW read, D-05 — the one sanctioned additive query):** `search_queries.py:167`
`get_summary_counts` (same file, same async count-read shape). Columns verified: `FileMetadata.artist`
(`models/metadata.py:19`, `Text` nullable, **UNINDEXED**) + `Tracklist.artist` (`models/tracklist.py:35`).
```python
async def distinct_artists(session: AsyncSession, query: str, *, limit: int = 20) -> list[str]:
    like = f"%{query}%"
    fm = select(FileMetadata.artist).where(FileMetadata.artist.is_not(None), FileMetadata.artist.ilike(like))
    tl = select(Tracklist.artist).where(Tracklist.artist.is_not(None), Tracklist.artist.ilike(like))
    rows = await session.execute(select(union_all(fm, tl).subquery().c.artist).distinct().limit(limit))
    return [a for (a,) in rows if a]
```
**Pitfall 4:** both columns are UNINDEXED — debounce (≥150-250ms), `LIMIT`, gate on `len(query) >= 2`; defer any
index (schema change = out of presentation scope).

**Commands wiring (D-03):** Scan → `POST /pipeline/scan-live-sets` (parameterless, `pipeline.py:1187`) — CORRECT
for ⌘K. Jump-to-stage/review-queue → HTMX nav to `/s/<stage>` (`shell.py:247` whitelist). Open Agents →
`/admin/agents`.

---

### `src/phaze/routers/admin_agents.py` + `services/agent_liveness.py` (MODIFY) — RECORD-03

**Section 1 (heartbeating) — reuse VERBATIM:** `_load_agents` (`admin_agents.py:59`) + `classify`/`sort_key`
(`agent_liveness.py:68/89`). Keep the existing `/admin/agents/_table` 5s self-poll (`:108`).

**Section 2 (compute lanes) — NEW read `classify_compute_lanes`:** SP-3 above (mirror `pipeline.py:1116/1159`).
Render a distinct amber "Compute / burst lanes · ephemeral" section from `CloudJob` counts — Active/Waiting/Idle,
**never DEAD**. Live refresh rides the existing `_table` poll (Research OQ-1 recommendation: restyle standalone
`/admin/agents` in place — lowest risk; do NOT promote into the shell this phase).

**Placement decision (OQ-1):** the standalone `/admin/agents` page (extends `base.html`) — so this touches
`admin/agents.html` (add Section 2) not the shell chrome.

---

### Empty-state + `shell.py` count==0 branch (NEW) — RECORD-04

**Analog:** `pipeline/partials/discover_workspace.html:39` (the reused Trigger Scan form) + `scan_path_picker.html`
(`pipeline_scans.py:150` `agent-roots` swap). Branch on file-count==0 in the Analyze workspace render
(`shell.py` analyze branch, `:144`).

**⚠ Scan endpoint correction (Pitfall 2):** the empty-state "Scan {agent}" posts the **DISCOVERY** scan
`POST /pipeline/scans` (`pipeline_scans.py:305` — requires `agent_id` + `scan_root` Form fields, validated
against `agent.scan_roots`), **NOT** the parameterless `scan-live-sets` (with 0 files there is nothing to
fingerprint). List each `Agent.scan_roots` (`models/agent.py:29`, JSONB); render one button per (agent, root) OR
post `scan_roots[0]`. Zero new input surface (D-08 — no free-text path, no directory browser). Live progress
rides the existing `/pipeline/stats` poll (SP-6).

---

### `shell.html` + `base.html` (MODIFY) + `tests/test_base_html_sri.py` (MODIFY) — the one new dep

**Analog:** the `<head>` script block at `shell.html:32-39` and the IDENTICAL block at `base.html:32-39`. Insert
`@alpinejs/focus@3.15.12` as `<script defer>` **immediately before** the Alpine core line (`shell.html:39`) in
**BOTH** files (Pitfall 1 — the shell runs on `shell.html`, not `base.html`):
```html
<script defer src="https://cdn.jsdelivr.net/npm/@alpinejs/focus@3.15.12/dist/cdn.min.js" integrity="sha384-ysJcnHb6oCzqAGKdoTm+IqKqmPKgxHT+ApZCawkyWOJfMq15WvzW3RRmHl7tWpEY" crossorigin="anonymous"></script>
```
Version MUST equal Alpine core (both files pin `alpinejs@3.15.12` — verified). SRI computed in RESEARCH
(26,051 bytes, `Accept-Encoding: identity`); recompute recipe if it drifts is in RESEARCH §CDN Audit.

**SRI test extension (Pitfall 1):** `test_base_html_sri.py:44,57` — `_BASE_HTML` and `_extract_cdn_scripts`
currently read ONLY `base.html`. Parametrize `_extract_cdn_scripts` over BOTH templates (add a `_SHELL_HTML`
path), so the two assertions (`test_every_cdn_script_pins_a_specific_version` + the network SRI check) cover
`shell.html` where the traps actually run.

---

### `tests/test_record_palette_agents.py` (NEW) — RECORD-01..04 + fragment guard

**Analog:** `tests/test_shell_routes.py` (route+template assertion precedent — bare-fragment check at `:67-79`).
Copy the fragment-bareness assertions:
```python
r = await client.get(f"/record/{file_id}", headers={"HX-Request": "true"})
assert "<html" not in r.text and "<head" not in r.text   # R-5 bare fragment (test_shell_routes:73-74)
```
**Fixture extensions (`tests/conftest.py`):** the existing async factory pattern lives at `:351` (`make_file`),
`:380` (`seed_pending_proposal`), `:412` (`seed_executed_file_with_metadata` — already sets `artist`), `:445`
(`seed_duplicate_group`), `:459` (`seed_cue_set`). Add factories for: a file with `AnalysisResult` +
`AnalysisWindow` (fine+coarse) rows; `FileMetadata`/`Tracklist` rows with distinct artists (for `distinct_artists`);
`CloudJob` rows in running / submitted+inadmissible / none states (for `classify_compute_lanes`); the empty-DB
file_count==0 case. Reuse `make_file` as the base (all higher factories depend on it).

---

## No Analog Found

None. Every seam composes an existing partial, route, model, or service. The only genuinely new code is
(a) two read-only query functions (`distinct_artists`, `classify_compute_lanes`) — both with direct in-file
precedents, (b) the `record_host.html` overlay — a direct extension of `cmdk_modal.html`, and (c) the roving-nav
Alpine state in the palette — a client-side extension of the existing skeleton.

---

## Metadata

**Analog search scope:** `src/phaze/routers/{shell,proposals,search,admin_agents,pipeline_scans}.py`,
`src/phaze/services/{search_queries,agent_liveness,pipeline}.py`, `src/phaze/models/{cloud_job,analysis,agent,metadata,tracklist}.py`,
`src/phaze/templates/{shell/shell.html,shell/partials/cmdk_modal.html,pipeline/partials/_diff_row.html,proposals/partials/analysis_timeline.html}`,
`tests/{test_shell_routes,test_base_html_sri,conftest}.py`
**Files scanned:** ~20 (targeted, verified against RESEARCH line numbers)
**Pattern extraction date:** 2026-07-01
