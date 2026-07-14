# Phase 88: Lane / Agent Drill-In - Pattern Map

**Mapped:** 2026-07-11
**Files analyzed:** 12 (5 new, 7 modified — plus 3 new test files)
**Analogs found:** 15 / 15 (every new/modified file has a verified in-repo analog)

> Pure additive, read-only Python/FastAPI + Jinja2/HTMX phase. NO Alembic migration, NO new
> packages, NO new CDN scripts. Every analog below was re-read against source in this worktree;
> file:line citations are current as of HEAD (`ac395311`).

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `routers/pipeline.py` — new `GET /pipeline/lanes/{backend_id}` (EDIT) | route/controller | request-response (HTML fragment) | `routers/record.py:41-61` `file_record` + `pipeline.py:685` `pipeline_stats_partial` | exact |
| `routers/admin_agents.py` — new `GET /admin/agents/{agent_id}/_activity` (EDIT) | route/controller | request-response (HTML fragment) | `routers/record.py:41-61` (typed-param + 404 fragment) + `admin_agents.py:90-149` (`page`/`table_partial` liveness idiom) | exact |
| `services/pipeline.py` — new `_agent_stage_buckets` helper (EDIT) | service | CRUD aggregate (GROUP BY) | `services/pipeline.py:324-361` `_safe_bucket_counts` | exact (one-conjunct clone) |
| `services/pipeline.py` OR `services/backends.py` — per-lane queue-depth read for agent pane (EDIT, optional) | service | pub-sub / broker read | `services/pipeline.py:222-283` `get_queue_activity` (`all_lane_queues` loop) | exact |
| `templates/pipeline/partials/_detail_pane.html` (NEW) | component (shared shell) | request-response + self-poll | `templates/shell/partials/record_host.html` (Esc+focus discipline, NON-modal) + `cmdk_modal.html` (focus-return-by-id) | role-match (borrow discipline, drop the trap) |
| `templates/pipeline/partials/_lane_detail.html` (NEW) | component (body slot) | request-response | `templates/pipeline/partials/_lane_card.html` (kind glyph/color tokens, D-06 branch) | exact (token reuse) |
| `templates/admin/partials/_agent_activity.html` (NEW) | component (body slot) | request-response | `templates/pipeline/partials/_stage_matrix.html` + `_stage_pill.html` + `admin/partials/_kind_badge.html`/`_status_pill.html` | exact (token reuse) |
| `templates/pipeline/partials/_lane_card.html` (EDIT — trigger wiring) | component (trigger) | event-driven (click/key → hx-get) | self (existing box model, frozen) + Pattern 3 trigger markup (RESEARCH §Pattern 3) | exact |
| `templates/admin/partials/agents_table.html` (EDIT — `<tr>` trigger) | component (trigger) | event-driven | self (existing `<tr>` at :55) + Pattern 3 trigger markup | exact |
| `templates/pipeline/partials/analyze_workspace.html` (EDIT — host pane) | component (host page) | layout | self (:42-120 workspace scaffold; add `#detail-pane` sibling of `#analyze-lanes`) | exact |
| `templates/admin/agents.html` (EDIT — host pane) | component (host page) | layout | self (:6-19; add `#detail-pane` outside `#agents-table-section`) | exact |
| `tests/integration/test_lane_detail.py` (NEW) | test | integration (httpx) | `tests/integration/test_stage_progress_buckets.py` (real-PG GROUP BY) + `tests/agents/routers/test_admin_agents.py` (smoke-app + client) | role-match |
| `tests/integration/test_agent_activity.py` (NEW) | test | integration | same two analogs | role-match |
| `tests/integration/test_drill_poll_survival.py` (NEW) | test | integration (HTML assert) | `tests/agents/routers/test_admin_agents.py` (partial/full render, HX-Request) | role-match |

---

## Pattern Assignments

### `services/pipeline.py` — `_agent_stage_buckets` (service, CRUD aggregate) — DRILL-02 / D-04

**Analog:** `services/pipeline.py:324-361` `_safe_bucket_counts` — this is a **verbatim clone plus ONE
`.where()` conjunct**. Do NOT re-spell the CASE ladder (D-00a/DERIV-04 lock).

**The GroupingError-safe aggregate shape** (`pipeline.py:342-361`, re-read against source):
```python
out: dict[str, int] = {s.value: 0 for s in Status}
# Materialize the per-row status label in an inner subquery FIRST, then GROUP BY the label.
# Grouping directly by stage_status_case(stage) fails on Postgres — the CASE ladder embeds
# correlated exists(... == FileRecord.id) subqueries; a top-level GROUP BY re-projects the
# ungrouped files.id ("subquery uses ungrouped column" GroupingError).
status_subq = select(stage_status_case(stage).label("status")).where(FileRecord.file_type.in_(MUSIC_VIDEO_TYPES)).subquery()
stmt = select(status_subq.c.status, func.count()).group_by(status_subq.c.status)
try:
    for status_label, n in (await session.execute(stmt)).all():
        if status_label in out:
            out[status_label] = int(n)
except Exception:
    logger.warning("stage_bucket_degraded", stage=stage.value, exc_info=True)
    try:
        await session.rollback()
    except Exception:
        logger.warning("stage_bucket_rollback_failed", stage=stage.value, exc_info=True)
return out
```

**The ONLY D-04 change** — add `FileRecord.agent_id == agent_id` to the inner subquery `.where()`:
```python
status_subq = (
    select(stage_status_case(stage).label("status"))
    .where(FileRecord.file_type.in_(MUSIC_VIDEO_TYPES))
    .where(FileRecord.agent_id == agent_id)   # <-- the only D-04 addition
    .subquery()
)
```

**6-stage call set** (Pitfall 3 — 7 Stage members, 6 pills; TRACKLIST omitted):
`Stage.METADATA, Stage.FINGERPRINT, Stage.ANALYZE, Stage.PROPOSE, Stage.REVIEW, Stage.APPLY`
(verified `enums/stage.py:36-42`). `stage_status_case` (`services/stage_status.py:392`) accepts all six;
the three downstream stages (propose/review/apply) yield only `done`/`failed`/`not_started` — correct.

**GROUP-BY-agent precedent** (that `agent_id` is a real, indexed FileRecord column filtered elsewhere):
`services/pipeline.py:204` — `select(FileRecord.agent_id, func.count(FileRecord.id)).group_by(FileRecord.agent_id)`.

---

### `routers/pipeline.py` — `GET /pipeline/lanes/{backend_id}` (route, request-response) — DRILL-01

**Analog A — endpoint shape:** `routers/record.py:41-61` (typed path param, HTML-fragment response,
friendly 404 fragment, NEVER JSON/500). Router is `APIRouter(tags=["pipeline"])` at `pipeline.py:311`.

**Analog A excerpt** (`record.py:41-61`, the friendly-empty-fragment discipline to copy):
```python
@router.get("/{file_id}", response_class=HTMLResponse)
async def file_record(request: Request, file_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    file = await session.get(FileRecord, file_id)
    if file is None:
        return templates.TemplateResponse(request=request, name="record/record_not_found.html",
                                          context={"request": request}, status_code=404)
    ...
```
Adapt: `backend_id: str` (operator-declared, stays Jinja-autoescaped, T-71-05); look it up in the
snapshot's known-id set rather than trusting the raw param; unknown → the "Lane offline" empty fragment.

**Analog B — the lane data source (already degrade-safe):** `services/backends.py:756-800`
`get_backend_lane_snapshot(session)` → `list[{id, kind, rank, cap, in_flight, available, quota_wait,
inadmissible}]`, degrades to `[]`. Kueue-only `quota_wait`/`inadmissible` already populated → D-06
kind-adaptivity is a template `{% if lane.kind == 'kueue' %}` branch, not new derivation.
```python
lanes = await get_backend_lane_snapshot(session)   # degrade-safe -> [] (backends.py:793-799)
lane = next((l for l in lanes if l["id"] == backend_id), None)   # None -> "Lane offline" empty state
```

**Analog C — how the poll already seeds lanes** (mirror the call site, don't re-derive):
`pipeline.py:740` `lanes = await get_backend_lane_snapshot(session)` inside `pipeline_stats_partial`.

**Recent completions (D-07):** `CloudJob` (verified `models/cloud_job.py`: `backend_id` String(255)
nullable :102, `status` String(16) :82, `SUCCEEDED = "succeeded"` :46, `updated_at` from TimestampMixin):
```python
stmt = (
    select(CloudJob)
    .where(CloudJob.backend_id == backend_id, CloudJob.status == CloudJobStatus.SUCCEEDED.value)
    .order_by(CloudJob.updated_at.desc())
    .limit(RECENT_N)   # D-07 fixed small N; RESEARCH recommends 20
)
```
NOTE: a `LocalBackend` writes NO `cloud_job` rows (backends.py:711 short-circuit) → local lane has no
cloud_job completions. RESEARCH Open Question 1: prefer omitting the section on local (show
"No completions in the last {N}") for a clean first cut.

**Also EDIT `pipeline_stats_partial` (pipeline.py:685)** to accept `?lane=` and pass the selected id
into the `_analyze_lanes.html` include so the poll re-emits the selected-ring (D-02). See Shared
Pattern "Poll-survival" + RESEARCH Open Question 2 (the poll `hx-get` does NOT auto-carry the pushed
`?lane=` — wire it explicitly via `hx-vals`/`hx-include`).

---

### `routers/admin_agents.py` — `GET /admin/agents/{agent_id}/_activity` (route) — DRILL-02

**Analog:** the SAME `admin_agents.py` file (`prefix="/admin/agents"`, :50) hosts it. Reuse its
liveness idiom verbatim — `classify(agent, now)` (imported :38), `_kind_badge.html`/`_status_pill.html`,
`humanize_relative_time` (registered as a template global at `admin_agents.py:48`).

**Liveness header wiring** (mirror `_load_agents` transient-attr pattern, `admin_agents.py:80-86`):
```python
agent = await session.get(Agent, agent_id)          # None -> friendly empty fragment (record.py idiom)
now = datetime.now(UTC)
if agent is not None:
    agent._status = classify(agent, now)            # transient attr; _status_pill.html reads it
```
`_status_pill.html:9-18` renders alive/stale/dead/revoked/never off `agent._status`; `_kind_badge.html:11-14`
renders COMPUTE/FILE SERVER off `agent.kind`; both are `{% include %}` with `agent` in context.

**Stage matrix context** — build a `buckets` dict keyed by Stage VALUE for `_stage_matrix.html`:
```python
buckets = {stage.value: await _agent_stage_buckets(session, agent_id, stage)
           for stage in (Stage.METADATA, Stage.FINGERPRINT, Stage.ANALYZE, Stage.PROPOSE, Stage.REVIEW, Stage.APPLY)}
```
CRITICAL (Pitfall 3): `_stage_matrix.html:29-36` reads `buckets.review` for the **Appr** pill and
`buckets.apply` for the **Exec** pill on purpose. Key exactly `metadata/fingerprint/analyze/propose/review/apply`.
But note: `_stage_matrix.html` expects each `buckets.<stage>` to be a **single bucket string**; the
per-agent surface needs a **stage × bucket numeral grid** (counts), so the agent body renders its OWN
numeral grid reusing `_stage_pill.html`'s color tokens per cell — it does NOT pass count-dicts into
`_stage_matrix.html` unchanged (which pill-renders one bucket). Reuse the stage ORDER + tokens, not the
pill-include as-is (see `_agent_activity.html` below).

**Recent scan batches (D-05):** `ScanBatch` has `ix_scan_batches_agent_id` (verified
`models/scan_batch.py:55`). `select(ScanBatch).where(ScanBatch.agent_id == agent_id).order_by(ScanBatch.created_at.desc()).limit(N)`.
Precedent for the ranked per-agent batch read: `pipeline.py:193-202` (`get_agent_reconciliations`).

**Also EDIT `page`/`table_partial` (admin_agents.py:90/125)** to accept `?agent=` and thread the
selected id into `agents_table.html` for the highlight re-render (D-02).

---

### `services/pipeline.py` — per-lane queue depth for the agent pane (service, broker read) — D-05

**Analog:** `services/pipeline.py:222-283` `get_queue_activity`. The authoritative per-lane depth loop
(`pipeline.py:256-258`):
```python
for q in (*app_state.task_router.all_lane_queues(agent.id), app_state.task_router.legacy_base_queue(agent.id)):
    agent_queued += await q.count("queued")
    agent_active += await q.count("active")
```
Copy the **per-source `try/except → 0`** isolation (`pipeline.py:259-263`) — a missing `app.state`
attr (test lifespan-skip) or a broker hiccup degrades to 0, never 500 the tick. Discretion (CONTEXT):
reuse this read or a scoped per-agent variant; either way keep it bounded + degrade-safe (D-00b).

---

### `templates/pipeline/partials/_detail_pane.html` (NEW, shared shell) — D-08 / D-09 / DRILL-03

**Analog:** `templates/shell/partials/record_host.html` + `cmdk_modal.html` — **borrow the Esc + focus
discipline, DROP the modal parts** (D-01 chose a NON-modal side pane; RESEARCH Anti-Pattern: no
`x-trap`/`aria-modal`/backdrop — that would inert the list the operator keeps glancing at).

**Focus-on-open (park focus off the doomed trigger — Pitfall 1)** from `record_host.html:31-35`:
```javascript
show(el) {
    this.opener = el || null;
    this.open = true;
    this.$nextTick(() => {
        const panel = this.$refs.panel;
        const heading = panel && panel.querySelector('h2');
        if (heading) heading.focus();     // <h2 tabindex="-1"> in the pane header
    });
}
```

**Esc-to-dismiss (guarded)** from `record_host.html:50` / `cmdk_modal.html:27`:
```html
@keydown.escape.window="if (open) hide()"
```
Guard so it fires only when the pane is open AND no `[aria-modal="true"]` layer is above it (record/cmdk
own Esc when THEY are open).

**Focus-return-by-STABLE-ID (Pitfall 1 — the key deviation from the modal precedent).** The modal
precedents capture an element handle (`this.opener`); here the 5s poll REPLACES the trigger node, so a
captured handle is detached. Return by id instead:
```javascript
hide() {
    this.open = false;
    const id = this.selectedId;   // 'lane-trigger-'+backend_id  OR  'agent-trigger-'+agent_id
    this.$nextTick(() => { document.getElementById(id)?.focus(); });   // re-rendered node keeps the id
}
```
(`cmdk_modal.html:101-110` shows the `$nextTick` + `getElementById(...).focus()` deferral pattern to
copy; the difference is the id is DERIVED, not captured.)

**Pane ARIA** (UI-SPEC §Keyboard): `role="region"` `aria-labelledby="{pane-header-id}"`
`aria-live="polite"`; visible `✕ Close` (`aria-label="Close detail"`).

**Own-tick live refresh (D-03)** — the loaded body carries its own bounded tick, removed with the pane
on dismiss (no orphan loop); mirrors how `#analyze-lanes`/`#agents-table-section` re-emit their own
`hx-trigger` each swap:
```html
<div hx-get="/pipeline/lanes/{{ lane.id }}" hx-trigger="every 5s" hx-target="#detail-pane" hx-swap="innerHTML">…</div>
```

**Alpine re-init after swap** (if the body has x-data islands): `record_host.html:71` shows the
`hx-on::after-swap="if (window.Alpine) Alpine.initTree(this)"` idiom.

**"Last refreshed Ns ago" caption** — reuse the `agents_table.html:82-86` Alpine `x-init setInterval`
countdown verbatim. Refresh-error red `role="alert"` caption — reuse `agents_table.html:94-109` pattern.

---

### `templates/pipeline/partials/_lane_detail.html` (NEW, lane body) — DRILL-01 / D-06 / D-07

**Analog:** `templates/pipeline/partials/_lane_card.html` (re-read in full). Reuse its kind→token
mapping verbatim (`_lane_card.html:25-50`): local=`🖥️`/`text-emerald-600 dark:text-emerald-300`/`bg-emerald-400`;
compute=`☁️`/`text-blue-600 dark:text-blue-300`/`bg-blue-400`; kueue=`⎈`/`text-amber-600 dark:text-amber-300`/
`border-amber-500/30`/`bg-amber-400`. RANK micro-label markup (`_lane_card.html:58`):
```html
<span class="ml-1 font-jura text-xs font-medium uppercase tracking-wider text-gray-400 dark:text-gray-500">RANK {{ lane.rank }}</span>
```

**D-06 kueue-only admission branch** — copy `_lane_card.html:74-85` verbatim (word-labelled, amber +
`role="alert"` when `inadmissible > 0`; NO fabricated n/a rows on non-kueue):
```html
{% elif _kind == 'kueue' %}
    <span>{{ lane.quota_wait }} waiting</span> ·
    {% if lane.inadmissible > 0 %}
    <span role="alert" class="text-amber-600 dark:text-amber-400">{{ lane.inadmissible }} inadmissible</span>
    {% else %}
    <span>{{ lane.inadmissible }} inadmissible</span>
    {% endif %}
{% endif %}
```

**Capacity bar** — reuse the frozen `h-1.5` bar (`_lane_card.html:66-70`). **Offline empty state**:
reuse the `opacity-60` + word `offline` idiom (`_lane_card.html:54,73`).

---

### `templates/admin/partials/_agent_activity.html` (NEW, agent body) — DRILL-02 / D-04 / D-05

**Analogs:** `_stage_matrix.html` (stage ORDER + legend), `_stage_pill.html` (bucket color tokens),
`_kind_badge.html`/`_status_pill.html` (liveness header), `agents_table.html` (`overflow-x-auto` +
countdown/error footers).

**Stacked section order (D-05):** (1) liveness header — `{% include "admin/partials/_kind_badge.html" %}`
+ `{% include "admin/partials/_status_pill.html" %}` + `{{ humanize_relative_time(agent.last_seen_at, now=now) }}`
(cell pattern from `agents_table.html:60-69`); (2) the 6-stage COUNT grid; (3) per-lane queue depths
(`analyze 12 · io 3`); (4) recent scan batches.

**Stage-order source** (`_stage_matrix.html:29-36`) — reuse EXACTLY, incl. the Appr=review/Exec=apply remap:
```jinja
{% set _matrix_stages = [
    ('Meta', buckets.metadata),  ('FP', buckets.fingerprint), ('Analyze', buckets.analyze),
    ('Prop', buckets.propose),   ('Appr', buckets.review),    ('Exec', buckets.apply),
] %}
```

**Bucket color tokens per COUNT cell** (from `_stage_pill.html:19-28` — the pill geometry
`text-xs font-semibold px-2 py-0.5 rounded-full`; `py-0.5` is the LOCKED project-wide pill spacing
exception): done=`bg-green-100 dark:bg-green-950 text-green-700 dark:text-green-400`;
in_flight=`bg-blue-100 dark:bg-blue-950 text-blue-700 dark:text-blue-400 animate-pulse`;
not_started=`bg-gray-100 dark:bg-gray-800/60 text-gray-500 dark:text-gray-400`;
failed=`bg-red-100 dark:bg-red-950 text-red-700 dark:text-red-400`;
skipped=`bg-violet-100 dark:bg-violet-950 text-violet-700 dark:text-violet-300 ring-1 ring-dashed ring-violet-400/60`.
Each cell keeps WORD+GLYPH+`aria-label` (WCAG 1.4.1). Legend: reuse `_stage_matrix.html:45-51` verbatim.

**Reuse the 5-bucket legend + `overflow-x-auto` narrow-scroll** (`agents_table.html:39`).
**Empty states:** "This agent owns no files yet." / reuse the `agents_table.html:31-37`
"No agents registered yet" state verbatim if no agents.

---

### Trigger edits — `_lane_card.html` + `agents_table.html <tr>` (event-driven) — DRILL-03 / D-09

**Analog:** RESEARCH §Pattern 3 markup (verified against HTMX 2.0.10 pins in `base.html:33`). Add to
the card root (`_lane_card.html:54`) / the `<tr>` (`agents_table.html:55`) WITHOUT touching the frozen
box model:
```html
<div id="lane-trigger-{{ lane.id }}" role="button" tabindex="0"
     aria-label="Open lane {{ lane.kind }} {{ lane.id }} detail"
     {% if selected_lane == lane.id %}aria-current="true"{% endif %}
     class="… cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500
            ring-offset-2 dark:ring-offset-phaze-bg
            {% if selected_lane == lane.id %}ring-2 ring-blue-500{% endif %}"
     hx-get="/pipeline/lanes/{{ lane.id }}"
     hx-target="#detail-pane" hx-swap="innerHTML"
     hx-push-url="/s/analyze?lane={{ lane.id }}"
     hx-trigger="click, keyup[key=='Enter']"
     onkeydown="if(event.key===' '){event.preventDefault();this.dispatchEvent(new Event('click'));}">
```
The `onkeydown` Space handler is REQUIRED (a `role=button` div gets no native Space activation and must
`preventDefault()` the page scroll). Focus-ring token `focus-visible:ring-2 focus-visible:ring-blue-500`
is the console's established token (from `analyze_workspace.html`; `_btn` at :35 uses `focus:ring-2
focus:ring-blue-500`). Agent row uses `id="agent-trigger-{{ agent.id }}"` and keeps its existing
`hover:bg-gray-50 dark:hover:bg-phaze-panel` (agents_table.html:55).

---

### Host-page edits — `analyze_workspace.html` + `agents.html` (layout) — D-01 / Pitfall 4

**`analyze_workspace.html`** (:42-120): place `#detail-pane` as a sibling of `#analyze-lanes` INSIDE the
`ws.workspace` call but OUTSIDE the polled grid (the grid `#analyze-lanes` OOB-swaps as a unit,
`_analyze_lanes.html:18-20`). Recommended: right-side column on `lg+`, stacked below on `< lg`.

**`agents.html`** (:6-19): place `#detail-pane` OUTSIDE `#agents-table-section` (the `outerHTML` poll
root, `agents_table.html:20-27`). The admin page is a STANDALONE full page (not a shell workspace), so
the pane sits directly in `{% block content %}` beside the `{% include "admin/partials/agents_table.html" %}`.

Same `_detail_pane.html` partial included at both sites; only the include location differs.

---

## Shared Patterns

### Degrade-safe read (`_safe_count` / `begin_nested()` SAVEPOINT) — apply to BOTH endpoints AND both ticks
**Source:** `services/pipeline.py:304-321` (`_safe_count`) and `:351-360` (`_safe_bucket_counts`
try/except → guarded rollback → zero). Also `services/stage_status.py:429` (`saq_detail`
`begin_nested()` SAVEPOINT form for reads that must not expire loaded ORM objects).
```python
try:
    return int((await session.execute(stmt)).scalar() or 0)
except Exception:
    logger.warning("stage_progress_degraded", node=node, exc_info=True)
    try:
        await session.rollback()
    except Exception:
        logger.warning("stage_progress_rollback_failed", node=node, exc_info=True)
    return 0
```
Every derived read in this phase (lane snapshot, agent aggregate, queue depth, recent completions,
recent scans) degrades to 0/None/`[]` — NEVER a 500 into the 5s poll (D-00b / PERF-01).

### Friendly HTML-fragment 404 (never JSON/stack trace) — apply to BOTH new endpoints
**Source:** `routers/record.py:54-61`. Unknown `backend_id`/`agent_id` → a friendly empty fragment with
(optionally) a 404 status, rendered through `templates.TemplateResponse`, never `HTTPException`/JSON.

### Free-text autoescape (T-71-05) — apply to every rendered id/kind/name
**Source:** `_lane_card.html:24` header comment; Jinja autoescape is default-on. Operator-declared
`backend_id`/`agent_id`/`agent.name`/`lane.kind` stay autoescaped; NEVER `|safe`/`|tojson` on them.
`get_backend_lane_snapshot` is already secret-free (`backends.py:765` — no config/SecretStr/kube token).

### Poll-survival: `?param` re-read on the existing poll — apply to `pipeline_stats_partial` + `table_partial`
**Sources:** the two OOB self-polls — `_analyze_lanes.html:18-20` (`#analyze-lanes` OOB grid) and
`agents_table.html:20-27` (`#agents-table-section` outerHTML). Both re-emit their own `hx-trigger` each
swap. To re-apply the selected ring (D-02) each poll must RECEIVE the selected id. **RESEARCH Open
Question 2 (load-bearing):** HTMX 2.0.10 does NOT auto-append the pushed URL's `?lane=`/`?agent=` to the
poll `hx-get` — the planner MUST wire it explicitly via `hx-vals='js:{lane: new URLSearchParams(location.search).get("lane")}'`
or `hx-include`. Verify against Context7 `/bigskysoftware/htmx` if exact semantics are needed.

### "Last refreshed Ns ago" + refresh-error banner — apply to the pane's own-tick (D-03)
**Source:** `agents_table.html:82-109` — the Alpine `x-init="setInterval(...)"` countdown (happy path)
and the `role="alert"` red `phaze:agents:*` localStorage error banner. The pane reuses the countdown
verbatim; refresh-error caption is "Detail refresh failed — retrying every 5s." (UI-SPEC §Copywriting).

---

## No Analog Found

None. Every new/modified file maps to a verified in-repo analog. The two genuinely-new pieces —
the per-agent stage aggregate and the shared non-modal pane — are both **one-diff clones** of existing
code (`_safe_bucket_counts` + one conjunct; `record_host.html`/`cmdk_modal.html` minus the trap).

---

## Test Analogs

| New test file | Analog | What to copy |
|---------------|--------|--------------|
| `tests/integration/test_lane_detail.py` | `tests/agents/routers/test_admin_agents.py` (smoke-app + `client` fixture, HX-Request assertions) | `_make_smoke_app(session)` mounting the router + `get_session` override; HTML-fragment assertions |
| `tests/integration/test_agent_activity.py` | `tests/integration/test_stage_progress_buckets.py` (real-PG `GROUP BY stage_status_case` over a seeded corpus; sum-to-total invariant on a healthy query only) | real-PG `db_session` + `_file` seed helper + `*_test` DB guard; assert bucket counts against DERIVED truth (`stage_status_case`/`resolve_status`), not a hand-count (drift-lock) |
| `tests/integration/test_drill_poll_survival.py` | `tests/agents/routers/test_admin_agents.py` (partial vs full render, HX-Request branch) | HTML-attribute assertions on trigger markup (`role=button`, `tabindex=0`, stable id, `aria-current` from `?param`) |

**Test-DB footgun (MEMORY / RESEARCH Wave 0):** export BOTH `TEST_DATABASE_URL` (5433) and
`MIGRATIONS_TEST_DATABASE_URL` — `just test-bucket` doesn't export the migration URL by default. Run the
Postgres `GROUP BY` test against real PG (5433), NOT SQLite, or Pitfall 2 (`GroupingError`) won't be
caught. Relevant buckets: `analyze` (lane detail), `agents` (agent activity). `get_session` never
commits (MEMORY) — these endpoints are read-only; seed via conftest factories, assert on returned HTML.

---

## Metadata

**Analog search scope:** `src/phaze/routers/` (pipeline, admin_agents, record), `src/phaze/services/`
(pipeline, backends, stage_status), `src/phaze/templates/{pipeline,admin,shell}/partials/`,
`src/phaze/models/` (cloud_job, scan_batch, file), `src/phaze/enums/stage.py`, `tests/integration/`,
`tests/agents/routers/`.
**Files scanned (read against source):** 18.
**Pattern extraction date:** 2026-07-11.
**Confidence:** HIGH — every excerpt above was re-read in this worktree at HEAD `ac395311`; line
numbers current.
