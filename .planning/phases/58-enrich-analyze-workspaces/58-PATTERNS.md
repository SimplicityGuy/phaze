# Phase 58: Enrich + Analyze workspaces - Pattern Map

**Mapped:** 2026-06-30
**Files analyzed:** 12 (8 new, 4 modified)
**Analogs found:** 12 / 12

> Scope reminder: Phase 58 is **presentation-only (NO backend behavior change)**. Every
> file below is either a new Jinja2 fragment, a read-only context/seed addition, or a test.
> The trigger endpoints, cloud-count services, and the `/pipeline/stats` poll are reused
> **verbatim** — do not author new endpoints, payloads, or stage semantics (D-01/D-02).

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `templates/pipeline/partials/_workspace_scaffold.html` (NEW) | component (partial) | request-response (fragment) | `templates/shell/partials/_stage_placeholder.html` | role-match (h1 focus-target header) |
| `templates/pipeline/partials/_file_table.html` (NEW) | component (partial) | request-response (table render) | `templates/pipeline/partials/recent_scans_table.html` | exact (table shell + empty state) |
| `templates/pipeline/partials/discover_workspace.html` (NEW) | component (partial) | request-response | `recent_scans_table.html` + scaffold | exact (reuse + restyle, WORK-01) |
| `templates/pipeline/partials/metadata_workspace.html` (NEW) | component (partial) | request-response + trigger | `dag_canvas.html` trigger button + `trigger_response.html` | role-match (queue + ALL button) |
| `templates/pipeline/partials/fingerprint_workspace.html` (NEW) | component (partial) | request-response + trigger | metadata_workspace (sibling) | exact (same shape, diff endpoint) |
| `templates/pipeline/partials/analyze_workspace.html` (NEW) | component (partial) | request-response | lane cards + cloud-card includes + file table | role-match (composite, WORK-03/04) |
| `templates/pipeline/partials/_lane_card.html` (NEW) | component (partial) | request-response | `awaiting_cloud_card.html` / `staged_pushing_card.html` | role-match (section + colored numeral + bar) |
| `src/phaze/routers/shell.py` (MOD) | route | request-response | existing `_render_stage` analyze branch | exact (extend STAGE_PARTIALS + per-stage ctx) |
| `src/phaze/routers/pipeline.py` (MOD) | route/context-builder | CRUD (read-only) | `_build_dag_context` + `build_dashboard_context` | exact (add derived seed keys) |
| `src/phaze/services/pipeline.py` (MOD) | service | CRUD (read-only SELECT) | `get_files_by_state` (`:727`) | role-match (multi-state + cloud_job join) |
| `templates/pipeline/partials/stats_bar.html` (MOD) | component (partial) | event-driven (OOB fanout) | existing `dag-seed-<key>` loop + `oob_counts` gate | exact (add gated seeds) |
| `tests/test_enrich_analyze_workspaces.py` (NEW) | test | request-response asserts | `tests/test_shell_routes.py` | exact (fragment/render seam) |

> **Directory note:** existing `STAGE_PARTIALS` values point at `pipeline/partials/*.html`
> (e.g. `pipeline/partials/dag_canvas.html`). Keep the new workspace partials under
> `pipeline/partials/` for consistency with that whitelist (a `pipeline/workspaces/`
> subdir is acceptable but then the STAGE_PARTIALS literals must match). Fragment roots are
> content-only — NEVER `{% extends "base.html" %}`, no `<html>`/`<head>`/second skip-link
> (R-5; the Phase-57 dead-template AST guard stays green).

---

## Pattern Assignments

### `templates/pipeline/partials/_workspace_scaffold.html` (component, fragment)

**Analog:** `templates/shell/partials/_stage_placeholder.html`

**Header / focus-target pattern** (`_stage_placeholder.html:8-10`) — the `<h1 tabindex="-1">`
is the landing target the shell's `htmx:afterSwap` handler focuses after a rail swap; reuse
its exact Jura token string:
```html
<section class="px-6 py-6">
    <h1 tabindex="-1" class="font-jura text-lg font-medium uppercase tracking-[0.15em] text-gray-900 dark:text-gray-100">{{ stage }}</h1>
    <p class="mt-3 text-sm text-gray-600 dark:text-gray-400">...</p>
</section>
```
Scaffold adds (per UI-SPEC Pattern 1): `px-6 py-4 border-b phaze-border` header bar, the
live sub-count `<p>` (Inter 14 `text-gray-500`, `x-text` on `$store.pipeline`), and a
right-aligned `flex gap-2` action-button row. Stage-action buttons use the Phase-57
**secondary** style: `h-9 px-3 rounded-lg bg phaze-panel border phaze-border hover:bg-white/5 text-gray-300`, Jura uppercase `tracking-wider` (NOT blue primary).

---

### `templates/pipeline/partials/_file_table.html` (component, table render)

**Analog:** `templates/pipeline/partials/recent_scans_table.html`

**Table shell + empty state** (`recent_scans_table.html:22-43`) — copy the empty-state block
and the thead/tbody structure, retokened to C3 (UI-SPEC Pattern 2: thead Jura
`text-[11px] uppercase tracking-[0.2em] text-gray-500`; tbody `divide-y phaze-border`; row
`hover:bg-white/5`; cells `px-6 py-3`):
```html
{% if not rows %}
<div class="text-center py-8">
    <p class="text-sm font-semibold ...">{HEADING}</p>
    <p class="text-sm text-gray-500 dark:text-gray-400">{BODY}</p>
</div>
{% else %}
<div class="overflow-x-auto">
    <table class="w-full text-sm text-left">
        <thead ...><tr>...</tr></thead>
        <tbody class="divide-y ...">{% for row in rows %}<tr class="hover:...">...</tr>{% endfor %}</tbody>
    </table>
</div>
{% endif %}
```

**Path/text safety** (`recent_scans_table.html:47`) — render file paths in mono + truncate +
`title=`, never `| safe` (Jinja autoescape; XSS mitigation V5):
```html
<td class="px-4 py-3 font-mono text-xs ... truncate max-w-md" title="{{ batch.scan_path }}">{{ batch.scan_path }}</td>
```

**Row affordance (D-06 inert rows):** add `cursor-pointer` + a stable row markup/target id per
R-1, but DO NOT bind the click — no `hx-get`, no selected-state, no pane fetch (that is Phase 61).

**Status/state cell:** colored text span, never hue-only — always a word (`complete`/`pending`/`running`), per UI-SPEC "State / status words" table.

---

### `templates/pipeline/partials/discover_workspace.html` (component, WORK-01)

**Analog:** `templates/pipeline/partials/recent_scans_table.html` (reuse as the data source)

- Reuse the existing recent-scans data (`recent_scans` rows from `build_recent_scans`,
  already in `build_dashboard_context`). **CRITICAL (RESEARCH Pitfall 4):** the existing
  `recent_scans_table.html` self-polls — `recent_scans_table.html:15-20` carries
  `hx-get="/pipeline/scans/recent" hx-trigger="every 5s"`. Reusing it verbatim inside the
  Discover workspace would add a SECOND poll loop, violating R-2 single-poll discipline.
  **Strip the `hx-get`/`hx-trigger`/`hx-swap` self-poll** when embedding (A3); refresh via
  the single `/pipeline/stats` OOB fanout or accept ≤5s staleness.
- Sub-count copy: "{discovered} files · last scan {when} · {not_yet_enriched} not yet enriched".
  `discovered` exists in `$store.pipeline`; `not_yet_enriched` does NOT — derive
  `discovered − metadataExtracted` client-side via `x-text`, or add one derived seed (see pipeline.py below).
- Actions: `SCAN` · `RECOVER` wire to the existing scan endpoints (already exist; no change).

---

### `templates/pipeline/partials/metadata_workspace.html` (component, WORK-02)

**Analog:** trigger button pattern in `dag_canvas.html` + `trigger_response.html`

**Trigger wiring (D-01, verbatim endpoint):** the `EXTRACT ALL` button POSTs to the existing
`POST /pipeline/extract-metadata` (`pipeline.py:959`), which returns
`pipeline/partials/trigger_response.html`. Target a response div in the workspace:
```html
<button type="button"
        hx-post="/pipeline/extract-metadata"
        hx-target="#metadata-trigger-response"
        :disabled="$store.pipeline.metadataBusy > 0"
        hx-confirm="Enqueue metadata extraction for all pending files?"  {# R-4 bulk-enqueue guard #}
        class="{secondary-button-tokens}">EXTRACT ALL</button>
<div id="metadata-trigger-response"></div>
```
`trigger_response.html` branches consumed: `no_active_agent` (amber held copy) / `count > 0`
("Enqueued N files") / else ("No files ready"). See `trigger_response.html:1-25`.

**D-02:** DROP `EXTRACT SELECTED` and ALL row-checkbox/selection state — it needs a new
subset-enqueue endpoint (backend change). Add the one-line reconciliation note to
`58-UI-SPEC.md` recording the deferral.

**Queue table:** include `_file_table.html` fed by `get_files_by_state(... METADATA_EXTRACTED)`
or the pending helper; columns File · Format · Size · Existing tags · State (UI-SPEC).

---

### `templates/pipeline/partials/fingerprint_workspace.html` (component, WORK-02)

**Analog:** `metadata_workspace.html` (sibling) — identical shape, different endpoint.

`FINGERPRINT ALL` POSTs verbatim to `POST /pipeline/fingerprint` (`pipeline.py:1045`),
returns `trigger_response.html` with `action="fingerprinting"`. Columns: File · Duration ·
Chromaprint · AcoustID. Same R-4 confirm + `:disabled="$store.pipeline.fingerprintBusy > 0"`.

---

### `templates/pipeline/partials/analyze_workspace.html` (component, WORK-03/04)

**Analog (composite):** 3 lane cards = `_lane_card.html` (new, below) ×3; cloud sub-state
detail = reuse existing cloud-card partials verbatim; file table = `_file_table.html`.

**Reuse cloud cards verbatim under the lane grid** (UI-SPEC Pattern 3 fault overlay; RESEARCH
Pattern 2). These already ride the OOB poll and encode the quota-wait-vs-Inadmissible
distinction — place, do not restyle:
```html
{% include "pipeline/partials/admission_state_card.html" %}   {# k8s healthy progression — NO role=alert #}
{% include "pipeline/partials/inadmissible_card.html" %}      {# k8s fault — role="alert" amber banner #}
{% include "pipeline/partials/localqueue_card.html" %}        {# k8s fault — role="alert" amber banner #}
{% include "pipeline/partials/awaiting_cloud_card.html" %}    {# A1 — held, no compute agent (sky) #}
{% include "pipeline/partials/staged_pushing_card.html" %}    {# A1 — push in progress (amber) #}
{% include "pipeline/partials/analyzing_cloud_card.html" %}   {# A1/cloud in-analysis (violet) #}
```
The load-bearing contrast (UI-SPEC Color): `admission_state_card.html:25-29` has NO alert role
(healthy); `inadmissible_card.html:19-24` and `localqueue_card.html:19-24` use
`role="alert"` + ⚠ + `border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-950`.
Keep amber for both Kueue states; separate by alert semantics + copy, NOT by escalating to red.

**Per-file lane badge + windowed progress (Pattern 4, D-03/D-04):** one table of ALL in-stage
Analyze files (queued · running · awaiting-cloud · done). Columns File · Duration · **Lane** ·
State. Lane is a derivation (no `cloud_target` file column):
- no `cloud_job` row → `🖥️ local` (`text-emerald-300`)
- `cloud_job` with `cloud_phase IS NULL` → `☁️ A1` (`text-blue-300`)
- `cloud_job` with `cloud_phase` set → `⎈ k8s` (`text-amber-300`)
Window progress reads the `analysis` aggregate columns `fine_windows_analyzed/fine_windows_total`
(`models/analysis.py:28-29`), rendered as `window {done}/{total}` mono. **Post-57.1 (PR #184, MERGED):**
`fine_windows_analyzed` increments DURING flight, so in-flight rows MUST show `running` + a live
`{fine_windows_analyzed}/{fine_windows_total} windows` indicator (NOT a bare `running` — that is the
superseded pre-57.1 behavior); completed (ANALYZED) rows show full coverage. Phase 58 simply *reads*
this mid-flight signal (D-04); no new query/schema here.

---

### `templates/pipeline/partials/_lane_card.html` (component, WORK-03)

**Analog:** `awaiting_cloud_card.html` / `staged_pushing_card.html` (section + colored numeral)

Mirror the cloud-card section structure but per UI-SPEC Pattern 3 (lane border + `h-1.5`
capacity bar + `font-medium` Inter-500 numeral — NOT `font-semibold`, to stay two-weight):
```html
<div class="rounded-xl bg phaze-panel border {lane-border} p-4">
  <div class="flex justify-between">
    <span class="text-gray-200">{🖥️/☁️/⎈ LANE · NODE}</span>
    <span class="font-mono text-sm font-medium {capacity-color}" x-text="...">{N / M or 'n pending'}</span>
  </div>
  <div class="mt-3 h-1.5 rounded-full bg-phaze-border"><div class="h-full rounded-full {lane-bar}" :style="..."></div></div>
  <div class="text-xs text-gray-500 mt-2">{SUB-LABEL}</div>
</div>
```
Lane identity colors (UI-SPEC): local `emerald-400`, A1 `blue-400`, k8s `amber-400`
(border `border-amber-500/30`). **D-05: always render all three; label the unavailable state**
(`offline` = configured-but-down; `not configured` = `cloud_target != lane`) with 0 capacity —
do NOT hide down/unconfigured lanes. Bind to existing `$store.pipeline` keys
(`agentOnline`/`analyzeBusy`/`analyzeActive`); add a derived seed only if a numeral genuinely
needs a binding (Pitfall 2 — cloud counts are NOT store keys, they arrive as OOB card swaps).

---

### `src/phaze/routers/shell.py` (route, MOD)

**Analog:** existing `_render_stage` analyze branch (`shell.py:67-97`)

Replace the four `_STAGE_PLACEHOLDER` / `dag_canvas.html` map values (`shell.py:50-64`) with
the new workspace partials. Extend `_render_stage` to load per-stage DB context for
discover/metadata/fingerprint (they have NO context today — Pitfall 5), mirroring the existing
analyze branch which calls `build_dashboard_context`:
```python
if stage == "analyze":
    context.update(await build_dashboard_context(request.app.state, session))
    context["stage"] = stage          # re-assert AFTER merge so bridge ctx can't shadow
    context["stage_partial"] = STAGE_PARTIALS[stage]
    context["oob_counts"] = False
```
Keep the static-whitelist invariant (`stage` never spliced into a template path — T-57-01);
keep `oob_counts=False` on the stage render (Pitfall 3 — OOB seeds collide on first render).
Keep degrade-safe service reads (no try/except in the route; services own the never-500 degrade).

---

### `src/phaze/routers/pipeline.py` (context-builder, MOD, read-only)

**Analog:** `_build_dag_context` (`pipeline.py:131-217`) + `build_dashboard_context` (`:434-543`)

Add any NEW reactive numeral as a derived int key in the `dag` dict — it then rides the
existing `dag.items()` OOB seed loop + `base.html` store default with ZERO `stats_bar.html`
edit (the established idiom at `pipeline.py:189-215`):
```python
dag["metadataBusy"] = int(busy["metadata"])   # existing pattern to copy
# Phase 58 candidates (read-only, only if a binding needs them):
#   dag["notYetEnriched"] = max(stats["discovered"] - stats["metadata_extracted"], 0)
#   dag["computeOnline"]  = int(await count_active_agents(session, kind="compute"))  # see enqueue_router.py:96 liveness seam
```
Every dag value MUST be a server-computed `int` (autoescaped, safe for `x-init`). For the
Analyze "all in-stage files" table (D-03), add the multi-state file list to
`build_dashboard_context`'s return dict (it already returns `recent_scans`, the cloud counts,
`**dag_ctx` at `:521-543`) — call the new service read (below).

> Do NOT add store keys when a card partial already carries the data (RESEARCH anti-pattern):
> cloud counts are delivered as whole-partial OOB swaps, not `dag` writes.

---

### `src/phaze/services/pipeline.py` (service, MOD, read-only SELECT)

**Analog:** `get_files_by_state` (`pipeline.py:727-739`)

The D-03 "all in-stage Analyze files" table spans multiple states (ANALYZING/AWAITING_CLOUD/
PUSHING/PUSHED/ANALYZED) and needs the per-file lane (`cloud_job`) + window aggregate
(`analysis`). Add ONE read-only multi-state SELECT (a pure read — no behavior change),
modeled on `get_files_by_state` but `WHERE state IN (...)` with `LEFT JOIN cloud_job` /
`LEFT JOIN analysis`:
```python
stmt = select(FileRecord).where(FileRecord.state == state)   # existing single-state shape to extend
result = await session.execute(stmt)
return list(result.scalars().all())
```
Keep it degrade-safe in the same style as the existing `get_*_count` reads (never-500). Confirm
the `cloud_job` lifecycle vs `FileState` (A1 assumption) by reading `release_awaiting_cloud.py`/
`cloud_staging.py` during planning so a transient `cloud_job` row cannot mislabel a local file.

---

### `templates/pipeline/partials/stats_bar.html` (OOB fanout, MOD)

**Analog:** existing `dag-seed-<key>` loop + `oob_counts` gate (`stats_bar.html:46-68`)

Any new seed MUST be inside `{% if oob_counts %}` and mirror the hidden-paragraph idiom so it
writes to `$store.pipeline` without re-rendering button subtrees (Pitfall 3 + anti-pattern):
```html
{% if oob_counts %}
{% for key, value in dag.items() %}
<p id="dag-seed-{{ key }}" hx-swap-oob="true" x-init="$store.pipeline.{{ key }} = {{ value }}" class="hidden"></p>
{% endfor %}
...existing cloud-card OOB includes ({% with oob = True %}{% include ... %})...
{% endif %}
```
If a new derived key is added to the `dag` dict (pipeline.py above), it is seeded automatically
by the existing `dag.items()` loop — no new line needed here. New cloud-card includes only if a
NEW card is introduced (none required — reuse the six existing ones). NO `hx-trigger="every"`,
no second poll (R-2).

---

### `tests/test_enrich_analyze_workspaces.py` (test, NEW)

**Analog:** `tests/test_shell_routes.py`

Copy the fixture/seam conventions verbatim (`@pytest.mark.asyncio`, `client: AsyncClient`,
`response.text` substring asserts):
```python
@pytest.mark.asyncio
async def test_stage_fragment_is_bare(client: AsyncClient) -> None:
    hx = await client.get("/s/discover", headers={"HX-Request": "true"})
    assert hx.status_code == 200
    assert "<html" not in hx.text and "<head" not in hx.text
```
Per RESEARCH "Phase Requirements → Test Map", cover:
- `test_discover_workspace` (WORK-01) — recent-scans table + sub-count + SCAN/RECOVER present
- `test_metadata_trigger_all_wired` (WORK-02) — `hx-post="/pipeline/extract-metadata"` present; no `EXTRACT SELECTED` / checkbox (D-02)
- `test_lane_cards_states` (WORK-03) — all 3 lanes always render; `not configured` vs `offline`; Inadmissible has `role="alert"`, admission card does NOT
- `test_analyze_file_table_lane_and_windows` (WORK-04) — lane badge derivation + `window {a}/{total}` for completed; `running` for in-flight
- `test_single_poll_discipline` (WORK-05) — assert NO `hx-trigger="every"` / `setInterval` in any workspace fragment (structural grep)
- Reuse the Phase-57 fragment-bareness + dead-template AST guard for R-5.

Seed `cloud_job` (cloud_phase variants) + `analysis` aggregate rows in fixtures for lane/window
asserts (confirm `conftest.py` can seed these — RESEARCH Wave-0 gap).

---

## Shared Patterns

### Fragment-only stage render (R-5, inherited Phase 57)
**Source:** `templates/shell/_stage_fragment.html` (`{% include stage_partial %}`) + `_render_stage` (`shell.py:67-97`)
**Apply to:** all 6 new workspace/scaffold/table partials
Content-only; NEVER `{% extends "base.html" %}`; no `<html>`/`<head>`/second skip-link. Exactly
one `<h1 tabindex="-1">` per workspace (the focus landing target). Keeps the dead-template AST
guard green by superseding-in-place (CUT-02/Phase 62 owns deletion).

### Single-poll OOB fanout + `oob_counts` gate (R-2, WORK-05)
**Source:** `stats_bar.html:46-104` (gated seeds + cloud-card OOB includes) + `pipeline_stats_partial` (`pipeline.py:571`, `oob_counts=True` at `:628`)
**Apply to:** every live value in all four workspaces
One `/pipeline/stats` request per 5s for the whole shell; the poll element lives in persistent
shell chrome, never inside a swappable fragment. All workspace live values ride this poll via
`hx-swap-oob` (store-key seeds OR whole-partial card swaps). No second loop, no `setInterval`.

### Carrier-always / body-conditional cloud cards
**Source:** `awaiting_cloud_card.html`, `admission_state_card.html:25-29`, `inadmissible_card.html:19-32`, `localqueue_card.html:19-32`, `staged_pushing_card.html`, `analyzing_cloud_card.html`
**Apply to:** the Analyze workspace cloud sub-state grid + the new `_lane_card.html`
Outer `<section id=...>` always renders (stable OOB target; cleared state collapses to empty);
body renders only when count > 0. `{% if oob %}hx-swap-oob="true"{% endif %}` flips the partial
for the poll re-push. Reuse the six existing cards verbatim — they already satisfy the C3 dark
contract; do not restyle.

### Degrade-safe service reads (never-500)
**Source:** `build_dashboard_context` (`pipeline.py:434-543` — no try/except; services own the degrade)
**Apply to:** the new multi-state Analyze file read + any derived count
Every read degrades to 0/empty at the service layer so the hot 5s poll can never 500. Mirror the
existing `get_*_count` / `_build_dag_context` wiring idiom — NO router-level try/except.

### Trigger button → `trigger_response.html` (D-01, WORK-02)
**Source:** `POST /pipeline/extract-metadata` (`pipeline.py:959`), `POST /pipeline/fingerprint` (`:1045`), `trigger_response.html:1-25`
**Apply to:** Metadata `EXTRACT ALL`, Fingerprint `FINGERPRINT ALL`
ALL-only, wired verbatim to existing endpoints; `:disabled` bound to the relevant
`$store.pipeline.*Busy` count + `hx-confirm` per R-4 bulk-enqueue guard. No subset/selected
endpoint (D-02).

### Output-encoding / XSS (V5)
**Source:** `recent_scans_table.html:47` (mono + truncate + `title=`, no `| safe`)
**Apply to:** all file-path / tag cells in `_file_table.html` and the Analyze table
All interpolated values are server-computed ints or autoescaped strings; `stage` stays
whitelisted in `STAGE_PARTIALS` (never a template path, T-57-01).

---

## No Analog Found

None. Every Phase-58 file maps to an existing in-repo analog (the phase is plumbing +
presentation over existing routers/services/partials).

## Metadata

**Analog search scope:** `src/phaze/routers/`, `src/phaze/templates/pipeline/partials/`,
`src/phaze/templates/shell/`, `src/phaze/services/pipeline.py`, `src/phaze/models/`, `tests/`
**Files scanned:** ~20 (read this session)
**Pattern extraction date:** 2026-06-30
