# Phase 88: Lane / Agent Drill-In - Research

**Researched:** 2026-07-11
**Domain:** Server-rendered HTMX drill-in detail panes over a derived-status aggregate layer (FastAPI + Jinja2 + async SQLAlchemy)
**Confidence:** HIGH (every surface below was verified by reading source in this worktree; exact file:line citations throughout)

## Summary

Phase 88 adds two read-only HTMX drill-in detail views — a **lane detail** (`GET /pipeline/lanes/{backend_id}`) and an **agent activity** view (`GET /admin/agents/{agent_id}/_activity`) — that render into a shared sibling pane sitting OUTSIDE the two existing 5s `outerHTML` poll regions (`#analyze-lanes`, `#agents-table-section`). Every data source these endpoints need already exists in the service layer and is degrade-safe: `get_backend_lane_snapshot()` (backends.py:756) yields the per-lane dict D-06 reads, `_safe_bucket_counts()` (pipeline.py:324) is the exact template for the per-agent × per-stage COUNT aggregate D-04 needs, `get_queue_activity`'s per-lane `all_lane_queues(agent_id)` read (pipeline.py:256) supplies queue depths, and `CloudJob` (backend_id + status + updated_at) supplies lane recent-completions. `stage_status_case` (stage_status.py:392) is the single derivation the agent matrix aggregates over — filtered to `agent_id`, never forked.

This is a **pure additive, read-only** phase: **no Alembic migration**, **no new Python packages**, **no new JS/CSS**. HTMX 2.0.10 + Alpine.js 3.15.12 + @alpinejs/focus are already SRI-pinned in `base.html`. Every token, pill, badge, and color is reused from an existing partial per the approved UI-SPEC.

**Primary recommendation:** Build three seams exactly as the CONTEXT D-08 discretion lays out — (a) shared `_detail_pane.html` shell + trigger a11y wiring + `?param`/`hx-push-url` poll-survival mechanism; (b) lane endpoint + `_lane_detail.html` body reading a backend-id-scoped slice of `get_backend_lane_snapshot()`; (c) agent endpoint + `_agent_activity.html` body whose stage matrix is `_safe_bucket_counts()` cloned with a `FileRecord.agent_id == agent_id` conjunct added to the inner subquery. Copy the `_safe_count`/`begin_nested()` degrade discipline verbatim into both endpoints AND both self-refresh ticks.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-00a:** `stage_status_case(stage)` (`services/stage_status.py`) is the single derivation. The agent detail's stage grouping reads it — no second status-derivation path.
- **D-00b:** Never render a whole-corpus scan per poll. Every derived read is bounded and `_safe_count`/SAVEPOINT-degrade-safe (degrade to 0/None, never 500 the poll). Applies to both detail endpoints AND their live-refresh ticks.
- **D-01:** Detail renders in a SIBLING pane that is a separate swap target OUTSIDE the polled list region. The lane cards (`#analyze-lanes`) and agent rows (`#agents-table-section`) keep swapping on their 5s poll; the pane is a distinct element the poll does not touch. (Rejected: modal/dialog; dedicated full page.)
- **D-02:** Selection carried via a URL param — `?lane={backend_id}` / `?agent={agent_id}` set with `hx-push-url` on the trigger. Both DRILL-03 escape hatches used together: pane lives outside the polled region AND the param lets the polled list re-render the selected-highlight after each swap and lets a reload restore the open detail.
- **D-03:** The open detail live-refreshes on its OWN bounded `hx-trigger="every 5s"` scoped to that lane/agent; the tick is a single bounded read (D-00b) and STOPS when dismissed (no orphan loop). (Rejected: snapshot-on-click.)
- **D-04:** Owned files grouped as a per-agent 6-stage matrix of COUNTS. Reuse the Phase-87 stage model — 6 stages (Meta/FP/Analyze/Propose/Approve/Execute) × the `stage_status_case` buckets — as aggregate counts filtered to `agent_id`. MUST be a SQL `GROUP BY` aggregate over `stage_status_case`, NOT row-by-row materialization. (Rejected: expandable per-bucket file lists [deferred]; single 4-bucket roll-up.)
- **D-05:** Agent pane = stacked vertical sections. Order: liveness header (last-seen/status badge, reuse `_kind_badge.html`) → D-04 stage grouping → per-lane queue depths → recent scan batches (last N). Everything visible on open, scroll for depth. (Rejected: tabbed sections.)
- **D-06:** Kind-adaptive field set. Common fields for every lane (rank, in-flight/cap, availability, recent completions); kind-specific only where they exist — quota-waiting + Inadmissible for kueue only, short/long-set caption for local/compute. No fabricated zero/"n/a" quota rows on non-kueue lanes. (Rejected: uniform field set with n/a fillers.)
- **D-07:** Recent completions bounded by last-N (newest-first). A fixed small count (planning picks exact N, e.g. ~20) rather than a time window. (Rejected: trailing time window.)
- **D-08:** One shared detail-pane shell (proposed `_detail_pane.html`) owns the swap-target container, `?param`/`hx-push-url` wiring, open/dismiss, and the keyboard/a11y contract; lane body (`_lane_detail.html`) and agent body (`_agent_activity.html`) are parameterized content slots. (Rejected: page-native each.)
- **D-09:** Full keyboard loop — `role=button` triggers + Esc dismiss + focus restoration. Cards/rows get `role=button`, `tabindex=0`, Enter/Space activation, visible focus ring. Open pane is focusable, has a visible Close control AND Esc-to-dismiss, and dismissing clears the `?param` and returns focus to the originating card/row. (Rejected: close-only without Esc/focus-return.)

### Claude's Discretion
- Exact N for lane recent completions (D-07) and precise ordering/labels within the agent stacked sections (D-05) — constrained by bounded/derived reads.
- Where the sibling pane physically sits per host page (right column vs below the grid) and its responsive collapse on narrow screens — constrained by D-01.
- Whether per-lane queue depths in the agent pane reuse an existing lane snapshot read or a scoped per-agent variant — constrained by D-00b.
- Plan/PR decomposition — natural seams (a) shell+triggers, (b) lane body, (c) agent body. Small blast-radius per PR is the milestone's standing rule.

### Deferred Ideas (OUT OF SCOPE)
- Expandable per-bucket file lists in the agent pane (clicking a stage-bucket count → paginated file list). Considered under D-04; deferred to keep per-agent reads to bounded aggregates.
- Legacy `legacy-application-server` sentinel retire → Phase 89 (LEGACY-01..03).
- `files.state` column drop + `FileState` enum deletion + remaining `.state=` writers → Phase 90.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| DRILL-01 | Clicking a backend-lane card opens `GET /pipeline/lanes/{backend_id}` showing that lane's queues / in-flight / waiting / quota / recent completions | Lane dict from `get_backend_lane_snapshot()` (backends.py:756 — id/kind/rank/cap/in_flight/available/quota_wait/inadmissible); recent completions from `CloudJob` (backend_id + status='succeeded' + updated_at DESC LIMIT N); queue depths from `all_lane_queues(agent_id)` (see §Lane detail data sources) |
| DRILL-02 | Clicking an agent row opens `GET /admin/agents/{agent_id}/_activity` showing owned files grouped by derived `stage_status`, recent scan batches, per-lane queue depths, liveness | Per-agent 6-stage COUNT aggregate = `_safe_bucket_counts()` (pipeline.py:324) + `FileRecord.agent_id == agent_id`; recent scans from `ScanBatch` (ix_scan_batches_agent_id); queue depths from `all_lane_queues(agent_id)`; liveness from `classify()` + `_kind_badge.html`/`_status_pill.html` + `humanize_relative_time` |
| DRILL-03 | Drill-in survives the 5s poll swap (URL param + outside polled region) and is keyboard-accessible (role=button, Enter/Space, focus ring) | Pane is a distinct `#detail-pane` swap target outside the polled regions; `?lane=`/`?agent=` via `hx-push-url`; polled endpoints re-read param to re-apply highlight; focus-return-by-stable-id (record_host.html/cmdk_modal.html Esc+focus precedent, NON-modal) |
</phase_requirements>

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Lane detail render | API/Backend (`routers/pipeline.py`) | Jinja template | New `GET /pipeline/lanes/{backend_id}` returns an HTML fragment; the analyze workspace is a shell workspace, not a SPA route |
| Agent activity render | API/Backend (`routers/admin_agents.py`) | Jinja template | New `GET /admin/agents/{agent_id}/_activity`; landing in the existing admin_agents router keeps the `/admin/agents` prefix + `_load_agents`/`classify` helpers in reach |
| Per-agent stage COUNT aggregate | Database (one `GROUP BY` per stage over `stage_status_case`) | — | Bounded aggregate; MUST NOT materialize the agent's file rows (D-04, D-00b) |
| Lane snapshot / admission / in-flight | Database + service (`backends.py`) | — | Already derived by `get_backend_lane_snapshot()`; consumed read-only |
| Per-lane queue depth | Redis/SAQ broker via `app.state.task_router` | Service (`get_queue_activity` idiom) | Live queue depth is a broker read, degrade-safe |
| Poll-survival + selection state | Browser (URL param + HTMX attrs) | Frontend server (re-renders highlight from param) | `?param` is the single source of "what's selected"; server re-applies highlight each swap so state survives the client-side swap |
| Keyboard/focus loop | Browser (Alpine + inline handlers) | — | Non-modal pane; focus-return by stable element id after the poll replaces the trigger node |

## Standard Stack

**No new packages.** This phase uses only what is already installed and loaded. Verified in `base.html`:

| Library | Version | Purpose | Status |
|---------|---------|---------|--------|
| HTMX | 2.0.10 (SRI-pinned CDN, base.html:33) | `hx-get`/`hx-target`/`hx-swap`/`hx-push-url`/`hx-trigger="every 5s"` | [VERIFIED: base.html:33] |
| Alpine.js | 3.15.12 (SRI-pinned, base.html:44) | pane open/dismiss state, `setInterval` "last refreshed" countdown | [VERIFIED: base.html:44] |
| @alpinejs/focus | 3.15.12 (base.html:41) | `x-trap` exists but is DELIBERATELY NOT USED here (non-modal pane, D-09) | [VERIFIED: base.html:41] |
| FastAPI / Jinja2 / async SQLAlchemy / asyncpg | per CLAUDE.md stack | endpoints + templates + aggregate queries | [VERIFIED: existing routers] |

**Installation:** none. No `uv add`. No `package.json`. No CDN script addition (UI-SPEC §Registry Safety: "zero new CDN scripts").

## Package Legitimacy Audit

**Not applicable — this phase installs zero external packages.** No `uv add`, no npm, no new CDN `<script>`. The slopcheck / registry-verification gate is only required when a phase installs external packages; this one does not. All runtime dependencies (HTMX, Alpine, @alpinejs/focus, FastAPI, SQLAlchemy, asyncpg) are already present and SRI-pinned. [VERIFIED: base.html + pyproject.toml unchanged by this phase]

## Architecture Patterns

### System Architecture Diagram

```
                        ┌──────────────── Browser (analyze OR admin page) ────────────────┐
                        │                                                                  │
  operator clicks/⏎ ──▶ │  [lane card / agent row]  ──hx-get──▶  #detail-pane             │
   a trigger            │   role=button tabindex=0                (innerHTML swap)          │
                        │   hx-push-url ?lane=/?agent=            OUTSIDE polled region     │
                        │        │                                     │                    │
                        │        │ (focus moves to pane <h2>)          │ hx-trigger every 5s│
                        │        ▼                                     ▼ (own bounded tick) │
                        │  URL ?param  ◀── re-read each poll ──  [pane body: lane/agent]    │
                        │        │                                                          │
                        │  #analyze-lanes / #agents-table-section  ──hx-trigger every 5s──▶ │
                        │   (self outerHTML poll; re-applies selected ring from ?param)     │
                        └───────────────────────────────┬──────────────────────────────────┘
                                                         │ HTTP GET
                    ┌────────────────────────────────────┼─────────────────────────────────┐
                    ▼                                     ▼                                  ▼
   GET /pipeline/lanes/{backend_id}      GET /admin/agents/{agent_id}/_activity   (existing 5s polls)
        │                                      │                                   /pipeline/stats
        ▼                                      ▼                                   /admin/agents/_table
  get_backend_lane_snapshot()          per-agent aggregate (D-04):                     │
   → pick lane by backend_id           6× GROUP BY stage_status_case                   │ (read ?param,
  CloudJob succeeded LIMIT N            filtered agent_id  (clone of                    │  re-emit highlight)
  all_lane_queues(agent_id).count()      _safe_bucket_counts)                          ▼
        │                              ScanBatch DESC LIMIT N                    stage_status.py
        ▼                              all_lane_queues(agent_id).count()         (single derivation,
   all reads _safe_count /             classify() liveness                       D-00a — read only)
   begin_nested() degrade-safe                │
        └──────────────────────────────────────┴──────▶ Postgres (indexes: ix_files_state,
                                                          uq_files_agent_id_original_path,
                                                          ix_scan_batches_agent_id,
                                                          Phase-77 partial done/failed indexes)
```

### Component Responsibilities

| File (new/edit) | Responsibility |
|-----------------|----------------|
| `src/phaze/templates/pipeline/partials/_detail_pane.html` (NEW) | Shared shell: `#detail-pane` swap-target container, header + `✕ Close`, D-09 keyboard/dismiss/focus-return, `?param`/`hx-push-url` wiring, D-03 own-tick, degrade/error surface. Included by BOTH host pages. |
| `src/phaze/templates/pipeline/partials/_lane_detail.html` (NEW) | Lane body slot (DRILL-01, D-06/D-07). Reuses `_lane_card.html` kind glyph/color tokens. |
| `src/phaze/templates/admin/partials/_agent_activity.html` (NEW) | Agent body slot (DRILL-02, D-04/D-05). Reuses `_stage_matrix.html` stage order + `_stage_pill.html` tokens as a numeral grid, `_kind_badge.html`/`_status_pill.html` liveness. |
| `src/phaze/templates/pipeline/partials/_lane_card.html` (EDIT) | Add `id="lane-trigger-{{ lane.id }}"`, `role="button"`, `tabindex="0"`, `aria-label`, `hx-get`/`hx-target`/`hx-push-url`, focus/selected classes. Do NOT touch the frozen box model. |
| `src/phaze/templates/admin/partials/agents_table.html` (EDIT) | Add the same trigger wiring to the `<tr>` (`id="agent-trigger-{{ agent.id }}"`). |
| `src/phaze/routers/pipeline.py` (EDIT) | New `GET /pipeline/lanes/{backend_id}` (`router = APIRouter(tags=["pipeline"])`, pipeline.py:311). Also: the existing `pipeline_stats_partial` (pipeline.py:686) + analyze workspace render must accept `?lane=` and pass the selected id into the lane grid for highlight re-render. |
| `src/phaze/routers/admin_agents.py` (EDIT) | New `GET /admin/agents/{agent_id}/_activity` (`router = APIRouter(prefix="/admin/agents", ...)`, admin_agents.py:50). `page`/`table_partial` must accept `?agent=` and pass the selected id for highlight. |
| `src/phaze/templates/pipeline/partials/analyze_workspace.html` (EDIT) | Host the `#detail-pane` as a sibling of `#analyze-lanes` (inside the workspace but outside the polled grid). |
| `src/phaze/templates/admin/agents.html` (EDIT) | Host the `#detail-pane` outside `#agents-table-section`. |

### Pattern 1: The bounded per-agent stage-count aggregate (D-04) — clone `_safe_bucket_counts`
**What:** One `GROUP BY stage_status_case(stage)` per stage, filtered to the agent, degrade-safe. There is an EXACT existing template.
**When to use:** The agent-activity stage matrix (6 stages).
**Source pattern** (`services/pipeline.py:324-361`, `_safe_bucket_counts`) — the per-agent variant adds ONE conjunct to the inner subquery:
```python
# Source: services/pipeline.py:342-361 (verbatim shape) + the D-04 agent filter.
# CRITICAL Postgres gotcha (documented at pipeline.py:343-348): you CANNOT
# GROUP BY stage_status_case(stage) directly — the CASE ladder embeds correlated
# exists(... == FileRecord.id) subqueries, and a top-level GROUP BY re-projects the
# ungrouped files.id ("subquery uses ungrouped column" GroupingError). Materialize the
# per-row label in an inner subquery FIRST, then GROUP BY the scalar label.
out: dict[str, int] = {s.value: 0 for s in Status}
status_subq = (
    select(stage_status_case(stage).label("status"))
    .where(FileRecord.file_type.in_(MUSIC_VIDEO_TYPES))
    .where(FileRecord.agent_id == agent_id)          # <-- the only D-04 addition
    .subquery()
)
stmt = select(status_subq.c.status, func.count()).group_by(status_subq.c.status)
try:
    async with session.begin_nested():               # SAVEPOINT-isolate (D-00b)
        for status_label, n in (await session.execute(stmt)).all():
            if status_label in out:
                out[status_label] = int(n)
except Exception:
    logger.warning("agent_stage_bucket_degraded", stage=stage.value, exc_info=True)
    # (guarded rollback per _safe_bucket_counts) -> return all-zero
return out
```
The matrix needs all SIX stages: `Stage.METADATA`, `Stage.FINGERPRINT`, `Stage.ANALYZE`, `Stage.PROPOSE`, `Stage.REVIEW`, `Stage.APPLY`. `stage_status_case` works for all seven Stage members (raises only on a genuinely unknown stage). The three enrich stages produce the 5-way bucket set (incl. `skipped`/`in_flight`); the three downstream stages (propose/review/apply) produce only `done`/`failed`/`not_started` because `inflight_clause` returns `false()` and `skipped_clause` is not composed for them — this is correct and matches the `_stage_matrix.html` Appr=REVIEW / Exec=APPLY remap (see Pitfall 3).

### Pattern 2: Lane detail = a backend-id-scoped slice of the existing snapshot (DRILL-01, D-06)
**What:** `get_backend_lane_snapshot()` (backends.py:756) already returns the full `{id, kind, rank, cap, in_flight, available, quota_wait, inadmissible}` dict list, degrade-safe to `[]`. The lane endpoint calls it and selects the entry whose `id == backend_id` (or renders the "Lane offline" empty state if absent). Kueue-only fields (`quota_wait`/`inadmissible`) are already in the dict, so D-06 kind-adaptivity is a template `{% if lane.kind == 'kueue' %}` branch, not new derivation — mirroring `_lane_card.html:74-85`.
**Recent completions (D-07):** `CloudJob` carries `backend_id` (stamped at dispatch, Phase 68), `status`, and `updated_at` (TimestampMixin). Last-N succeeded for a lane:
```python
# Source: models/cloud_job.py (backend_id String, status 'succeeded', updated_at from TimestampMixin)
stmt = (
    select(CloudJob)
    .where(CloudJob.backend_id == backend_id, CloudJob.status == CloudJobStatus.SUCCEEDED.value)
    .order_by(CloudJob.updated_at.desc())
    .limit(RECENT_N)   # D-07: fixed small N; recommend 20
)
```
NOTE — a `LocalBackend` writes NO `cloud_job` rows (backends.py:276, `in_flight_count` always 0), so a local lane has no `cloud_job`-sourced completions. See Open Question 1 for the local-lane completions source.

### Pattern 3: Poll-survival — pane outside the region + `?param` re-read (DRILL-03, D-01/D-02)
**What:** The trigger sets `hx-get` → `#detail-pane` (`hx-swap="innerHTML"`) AND `hx-push-url` to `?lane=`/`?agent=`. The polled list endpoints (`pipeline_stats_partial`, `admin_agents.table_partial`) read the same param from the query string and re-emit the `ring-2 ring-blue-500 … aria-current="true"` highlight on the matching card/row every swap. A reload with `?param` present renders the pane populated server-side.
```html
<!-- Trigger (edit into _lane_card.html / agents_table.html <tr>). HTMX 2.0.10 attrs. -->
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
The `onkeydown` Space handler is REQUIRED (UI-SPEC §Keyboard): `keyup[key=='Enter']` covers Enter but Space must `preventDefault()` the page-scroll and re-fire activation (a `role=button` div does not get native Space activation).

### Pattern 4: The own-tick live refresh, stopped on dismiss (D-03)
**What:** The pane body carries its OWN `hx-trigger="every 5s"` re-fetching the same endpoint with the current id. Because the tick element is INSIDE `#detail-pane`, dismissing the pane (Close/Esc → swap `#detail-pane` back to the resting empty state) REMOVES the polling element — HTMX has nothing left to fire, so no orphan loop. This mirrors how `#analyze-lanes`/`#agents-table-section` re-emit their own `hx-trigger` each swap (agents_table.html:20-23) and how dismissing simply swaps the element away.
```html
<!-- Inside the loaded pane body -->
<div hx-get="/pipeline/lanes/{{ lane.id }}" hx-trigger="every 5s"
     hx-target="#detail-pane" hx-swap="innerHTML">…</div>
```

### Anti-Patterns to Avoid
- **Materializing the agent's files** to count buckets — D-04 forbids it; an agent may own a large share of the 200K corpus. Use the `GROUP BY` aggregate (Pattern 1).
- **A fresh `CASE` ladder** for the agent matrix — D-00a: compose `stage_status_case` verbatim, never re-spell the predicates (the DERIV-04 equivalence lock only protects the single source).
- **`x-trap` / `aria-modal` on the pane** — D-09 + UI-SPEC §Keyboard: the pane is NON-modal (sits beside the list the operator keeps glancing at). Borrow record_host.html/cmdk_modal.html's Esc+focus-return DISCIPLINE, not their focus-trap/backdrop.
- **Capturing the trigger element handle for focus-return** — the 5s poll replaces the trigger node in the DOM; a captured handle points at a detached node. Return focus by STABLE ID: `document.getElementById('lane-trigger-'+id)?.focus()` (see Pitfall 1).
- **Grouping directly by `stage_status_case(stage)`** — Postgres `GroupingError` (Pitfall 2).
- **`response_class=JSONResponse` / raising HTTPException into the poll** — these are HTML-fragment endpoints; a missing lane/agent renders a friendly empty fragment (record.py:55-61 precedent returns a 404 HTML fragment, never JSON/stack trace).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Per-agent per-stage bucket counts | A new grouped query with inline CASE | Clone `_safe_bucket_counts` (pipeline.py:324) + `agent_id` filter | Reuses the DERIV-04-locked `stage_status_case`; avoids the documented `GroupingError` (pipeline.py:343) |
| Lane fields (rank/cap/in-flight/avail/quota/inadmissible) | New per-backend probes | `get_backend_lane_snapshot()` (backends.py:756) | Already degrade-safe, secret-free, kind-adaptive fields present |
| Per-lane queue depth | New Redis/SAQ counting | `app.state.task_router.all_lane_queues(agent_id)` + `.count("queued"/"active")` (pipeline.py:256) | Already the authoritative per-lane depth read; degrade-safe |
| Degrade-on-error for a poll read | A new try/except shape | `_safe_count` / `begin_nested()` SAVEPOINT (pipeline.py:304, stage_status.py:429) | Rolls back the aborted txn so one bad source doesn't poison later COUNTs, never 500s the poll |
| Liveness header | New last-seen math | `classify(agent, now)` + `_status_pill.html` + `_kind_badge.html` + `humanize_relative_time` (admin_agents.py:38-48, 85) | Already the admin page's liveness idiom |
| Detail fragment endpoint + 404 | New error handling | `GET /record/{file_id}` shape (record.py:41-61): typed path param, HTML fragment, friendly 404 partial | Closes the template-path/BAC surface (typed param) and never 500s |
| Focus-return + Esc discipline | New JS | `record_host.html` / `cmdk_modal.html` `$nextTick` focus + `@keydown.escape.window="if(open)hide()"` | Proven; only difference is stable-id return + NO trap |

**Key insight:** Every data source and every degrade pattern this phase needs already exists and is battle-tested on the 5s poll. The phase is 90% template wiring + 10% two thin read-only endpoints that compose existing service calls. The single genuinely new query is the agent stage aggregate, and even that is a one-line diff on `_safe_bucket_counts`.

## Common Pitfalls

### Pitfall 1: Focus stranded on a swapped-away trigger node
**What goes wrong:** On dismiss you `focus()` a captured element handle; the operator's focus lands nowhere (or on `<body>`) because a 5s poll already replaced that DOM node.
**Why it happens:** Both triggers live INSIDE the polled `outerHTML` regions (`_lane_card.html` in `#analyze-lanes`; `<tr>` in `#agents-table-section`). Any poll between open and dismiss detaches the original node.
**How to avoid:** Two load-bearing moves (UI-SPEC §Keyboard). (1) On OPEN, move focus to the pane header (`<h2 tabindex="-1">`) — this parks focus OFF the doomed trigger. (2) On DISMISS, return focus by STABLE ID: `document.getElementById('lane-trigger-'+id)?.focus()` / `'agent-trigger-'+id`. The re-rendered trigger keeps the same id, so the id resolves even after N swaps.
**Warning signs:** Focus test passes when you don't wait for a poll but fails after a simulated swap. Write the a11y test to swap the list once between open and dismiss.

### Pitfall 2: `GroupingError` when grouping by the derived status
**What goes wrong:** `select(stage_status_case(stage), func.count()).group_by(stage_status_case(stage))` throws "subquery uses ungrouped column files.id".
**Why it happens:** `stage_status_case` embeds correlated `exists(... == FileRecord.id)` subqueries; a top-level GROUP BY re-projects the ungrouped `files.id`.
**How to avoid:** Materialize the label in an inner subquery first, then GROUP BY the scalar label — exactly as `_safe_bucket_counts` (pipeline.py:349-350) does. Copy that form; do not "simplify" it.
**Warning signs:** Works against SQLite in a unit test but fails on Postgres. Test the aggregate against the real Postgres test DB.

### Pitfall 3: The 7-stage → 6-pill Appr/Exec remap landmine
**What goes wrong:** The agent matrix mislabels every row because `Appr`/`Exec` are wired to the wrong Stage.
**Why it happens:** phaze has SEVEN `Stage` members but the matrix shows SIX pills: `tracklist` is OMITTED and the last two are RE-LABELLED — `Appr = Stage.REVIEW`, `Exec = Stage.APPLY` (`_stage_matrix.html:29-36` reads `buckets.review` for Appr and `buckets.apply` for Exec ON PURPOSE).
**How to avoid:** When building the per-agent `buckets` dict, key it by Stage VALUE exactly as `_stage_matrix.html` expects (`metadata`, `fingerprint`, `analyze`, `propose`, `review`, `apply`) and reuse `_stage_matrix.html`'s stage order. Do NOT invent your own labels.
**Warning signs:** Counts look plausible but "Approve"/"Execute" columns are swapped vs the file-surface matrix.

### Pitfall 4: The lane pane host lives at a different structural level on each page
**What goes wrong:** You put the pane inside `#analyze-lanes` (clobbered by the poll) or forget the admin page is a standalone page, not a shell workspace.
**Why it happens:** The analyze workspace is served as `#stage-workspace` content THROUGH the shell (shell.html:165; `record_host.html`/`cmdk_modal.html` sit OUTSIDE `#stage-workspace` at shell.html:174/181). The admin agents page (`admin/agents.html`) is a standalone full page whose poll root is `#agents-table-section` (agents_table.html:20).
**How to avoid:** Lane pane → sibling of `#analyze-lanes` inside `analyze_workspace.html` (inside the workspace, outside the polled grid). Agent pane → outside `#agents-table-section` in `agents.html`. The shared `_detail_pane.html` is the same partial; only its include site differs. (Leaving the analyze page via a rail swap closes the lane pane — acceptable; the `?lane=` param re-opens on return.)
**Warning signs:** The pane vanishes every 5s (it's inside the poll region) or duplicates (two swap roots).

### Pitfall 5: Reintroducing a `FileRecord.state` read
**What goes wrong:** Sourcing agent "activity" from `files.state` — forbidden; Phase 90 deletes that column and the milestone forbids raw-state renders.
**Why it happens:** `state` is still on the model (file.py:86) and tempting.
**How to avoid:** The agent matrix reads ONLY the derived `stage_status_case`. There is a guard test (`test_no_raw_state_render.py`) that filters raw-enum renders; the new templates must not render `f.state`. All agent grouping is derived (D-00a/D-04).
**Warning signs:** `test_no_raw_state_render` (or `just docs-drift`) flags the new template.

### Pitfall 6: `get_session` never commits — tests must assert from an independent session
**What goes wrong:** A test seeds rows via the request session, the endpoint reads its own session, and the test asserts against uncommitted state (or vice-versa).
**Why it happens:** Project invariant (MEMORY): routers never commit; `conftest.py` overrides `get_session` so the test client shares one session. These endpoints are READ-ONLY (no writes, no commit needed), so this is lower-risk here, but seeding + assertion must still use the shared/independent session correctly (conftest.py:232 overrides `get_session` with the test `session`).
**How to avoid:** Seed via the conftest factories (they commit — conftest.py:436 etc.), drive the endpoint through the `client` fixture, assert on the returned HTML fragment. For any independent-session assertion, follow the tags.py:369 / conftest.py:216 pattern noted in MEMORY.

## Code Examples

### The lane endpoint (thin, read-only, degrade-safe) — DRILL-01
```python
# Source: mirrors record.py:41-61 (typed param + HTML fragment + friendly empty state)
#         and pipeline.py:686 pipeline_stats_partial (get_backend_lane_snapshot on the poll).
@router.get("/pipeline/lanes/{backend_id}", response_class=HTMLResponse)
async def lane_detail(
    request: Request,
    backend_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    lanes = await get_backend_lane_snapshot(session)          # degrade-safe -> [] (backends.py:756)
    lane = next((l for l in lanes if l["id"] == backend_id), None)
    # recent completions (D-07) + queue depths sourced per §Lane detail data sources; all _safe_count-wrapped
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/_detail_pane.html",         # shell wrapping _lane_detail.html slot
        context={"request": request, "lane": lane, "backend_id": backend_id, ...},
    )
```
Note: `backend_id` is operator-declared free text and MUST stay Jinja-autoescaped (T-71-05). Prefer a lookup-in-known-set (the snapshot ids) over trusting the path param — an unknown id renders the "Lane offline / not found" empty state, never a 500 (UI-SPEC §Copywriting).

### The agent endpoint — DRILL-02
```python
# Lands in admin_agents.py (prefix="/admin/agents"). agent_id validated against a loaded Agent row.
@router.get("/{agent_id}/_activity", response_class=HTMLResponse)
async def agent_activity(
    request: Request,
    agent_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    agent = await session.get(Agent, agent_id)                # None -> friendly empty fragment
    now = datetime.now(UTC)
    status = classify(agent, now) if agent else None          # liveness (agent_liveness.py:85)
    buckets = {stage.value: await _agent_stage_buckets(session, agent_id, stage)
               for stage in (Stage.METADATA, Stage.FINGERPRINT, Stage.ANALYZE,
                             Stage.PROPOSE, Stage.REVIEW, Stage.APPLY)}   # D-04
    recent_scans = ... # ScanBatch WHERE agent_id ORDER BY created_at DESC LIMIT N (ix_scan_batches_agent_id)
    queue_depths = ... # all_lane_queues(agent_id).count(...) per lane, degrade-safe
    return templates.TemplateResponse(request=request, name="admin/partials/_detail_pane.html",
                                      context={...})
```

## Lane detail data sources (DRILL-01 / D-06 / D-07)

| Field | Source | Kind scope | Degrade |
|-------|--------|-----------|---------|
| rank, cap, in_flight, available | `get_backend_lane_snapshot()` entry (backends.py:781-791) | all | `[]` → "Lane offline" empty state |
| quota_wait, inadmissible | same snapshot dict (kueue-only populated; `_lane_card.html:74-85` branch) | **kueue only** (D-06 — no n/a on others) | 0 defaults in `_ZERO_ADMISSION` |
| queues / in-flight / waiting | `app.state.task_router.all_lane_queues(agent_id)` + `.count("queued"/"active")` (pipeline.py:252-258) — for compute/local lanes backed by an agent; kueue "waiting" = `quota_wait` | kind-adaptive | per-source try/except → 0 (get_queue_activity idiom) |
| recent completions (last-N) | `CloudJob` WHERE `backend_id==id` AND `status='succeeded'` ORDER BY `updated_at` DESC LIMIT N | compute/kueue (local writes no cloud_job) | `_safe_count`/wrapped; empty → "No completions in the last {N}" |

**Local-lane completions** — see Open Question 1.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `FileRecord.state` single enum for pipeline status | Derived `stage_status_case` per stage (parallel DAG) | Phases 78/82/87 | Agent matrix MUST derive, never read `state` (Pitfall 5) |
| Modal record slide-in (`record_host.html`, `aria-modal` + `x-trap`) | NON-modal side-by-side pane for this phase | Phase 88 (D-01) | Borrow Esc/focus discipline, NOT the trap (Pitfall 1) |
| SAQ on Redis | SAQ on Postgres broker | Phase 36 (MEMORY) | `all_lane_queues().count()` still works; queue reads still degrade-safe |

**Deprecated/outdated here:** nothing being removed. This phase is purely additive.

## Runtime State Inventory

**Not applicable — this is a greenfield/additive UI phase, not a rename/refactor/migration.** No stored data keys, service config, OS registrations, secrets, or build artifacts are renamed or migrated. No Alembic migration (37 existing migrations, latest unchanged). [VERIFIED: alembic/versions count unchanged; CONTEXT §Out of scope explicitly defers all state changes to Phases 89/90]

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `CloudJob.updated_at` (TimestampMixin) advances on the transition to `succeeded`, so "recent completions newest-first" ordering is meaningful | Lane data sources / Pattern 2 | If `updated_at` doesn't move on completion, "recent" ordering is by last-touch not completion time — cosmetically wrong ordering, not a correctness bug; planner should confirm the completion writer touches the row |
| A2 | Recommended N=20 for lane recent completions (D-07) | Pattern 2 / D-07 | Wrong N is a one-line change; D-07 explicitly leaves N to planning |
| A3 | Local-lane "recent completions" is best sourced from recently-completed `AnalysisResult` for files owned by the local fileserver agent, OR omitted per D-06 kind-adaptivity | Open Question 1 | A wrong choice shows an empty/odd completions list on the local lane; flag for user/planner |

## Open Questions (RESOLVED)

> Both resolved during planning (Phase 88 plans committed). Resolutions locked below.

1. **Local-lane recent completions source (D-06/D-07).** → **RESOLVED (omit).** Plan 88-02 Task 1: local lanes write no `CloudJob` rows, so the lane body renders the "No completions in the last 20" empty state rather than fabricating a source (per D-06 kind-adaptivity). Option (a) chosen.
   - What we know: compute/kueue lanes have `CloudJob` rows (backend_id + status='succeeded'); a `LocalBackend` writes NO `cloud_job` row (backends.py:276 in_flight_count always 0).
   - What's unclear: where a local lane's "recent completions" come from. Candidate: last-N `AnalysisResult` (order by `analysis_completed_at` DESC) for files whose owning agent is the local fileserver — but `AnalysisResult` carries no `backend_id`, so "which lane completed it" is not directly attributable.
   - Recommendation: per D-06 kind-adaptivity, either (a) omit the recent-completions section on the local lane entirely (show "No completions in the last {N}" or hide it), or (b) source it from `AnalysisResult.analysis_completed_at` DESC. Prefer (a) for a clean first cut; flag to the user in discuss/plan.

2. **Where the poll reads `?lane=` for highlight re-render.** → **RESOLVED (explicit `hx-vals`).** The `/pipeline/stats` poll is a single persistent chrome-level element in `shell/shell.html:196-198` (NOT in `_analyze_lanes.html`, which has no `hx-get` of its own). Plan 88-01 Task 3 adds `hx-vals='js:{lane: new URLSearchParams(location.search).get("lane")}'` to that `#pipeline-stats` element, and the agent-param `hx-vals` to the `#agents-table-section` self-poll in `agents_table.html`, so the pushed `?lane=`/`?agent=` reaches the poll endpoint for the D-02 highlight re-render. HTMX does NOT auto-append the pushed URL's query — this wiring is mandatory, not optional.

## Environment Availability

**Skipped — no new external dependencies.** All runtime deps (Postgres, Redis/SAQ-on-Postgres broker, HTMX/Alpine CDN scripts, FastAPI/SQLAlchemy) are already provisioned and used by the surfaces this phase extends. Test DB footgun noted below (Validation Architecture).

## Validation Architecture

`workflow.nyquist_validation` is `true` (.planning/config.json:20) — this section is REQUIRED.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio + httpx `AsyncClient` (per CLAUDE.md; `conftest.py:229` `client` fixture) |
| Config file | `pyproject.toml` (`[tool.pytest...]`); buckets in `tests/buckets.json` |
| Quick run command | `uv run pytest tests/integration/test_lane_detail.py -x` (per new file) |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` (90% floor) |
| Bucket isolation | `just test-bucket <bucket>` — new tests must pass in isolation (MEMORY: get_settings lru_cache leak / saq stub poison). Relevant buckets: `analyze` (lane detail on pipeline), `agents` (agent activity). |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| DRILL-01 | `GET /pipeline/lanes/{id}` returns lane fields for a known backend | integration | `uv run pytest tests/integration/test_lane_detail.py::test_known_lane_renders -x` | ❌ Wave 0 |
| DRILL-01 | Unknown/offline backend_id → friendly empty fragment, not 500 | integration | `...::test_unknown_lane_empty_state -x` | ❌ Wave 0 |
| DRILL-01 | kueue lane shows quota_wait/inadmissible; non-kueue shows NO n/a quota row (D-06) | integration | `...::test_lane_kind_adaptive_fields -x` | ❌ Wave 0 |
| DRILL-01 | recent completions bounded to N, newest-first (D-07) | integration | `...::test_recent_completions_bounded_newest_first -x` | ❌ Wave 0 |
| DRILL-02 | `GET /admin/agents/{id}/_activity` per-agent 6-stage COUNT matrix matches derived truth | integration | `uv run pytest tests/integration/test_agent_activity.py::test_stage_counts_match_derivation -x` | ❌ Wave 0 |
| DRILL-02 | Aggregate is `GROUP BY` (no per-file materialization); runs against Postgres without `GroupingError` (Pitfall 2) | integration | `...::test_agent_aggregate_postgres_groupby -x` | ❌ Wave 0 |
| DRILL-02 | Appr=REVIEW / Exec=APPLY remap correct (Pitfall 3) | integration | `...::test_appr_exec_remap -x` | ❌ Wave 0 |
| DRILL-02 | agent owns 0 files → "This agent owns no files yet" empty state | integration | `...::test_agent_zero_files_empty -x` | ❌ Wave 0 |
| DRILL-02 | recent scan batches + queue depths render, degrade-safe | integration | `...::test_agent_scans_and_queues -x` | ❌ Wave 0 |
| DRILL-03 | trigger markup: `role=button`, `tabindex=0`, `aria-label`, focus-ring class, stable id present | integration (HTML assert) | `...::test_trigger_a11y_markup -x` | ❌ Wave 0 |
| DRILL-03 | polled list re-applies `aria-current`/ring from `?lane=`/`?agent=` after a swap (D-02) | integration | `...::test_selected_highlight_survives_poll -x` | ❌ Wave 0 |
| DRILL-03 | reload with `?param` renders pane populated | integration | `...::test_param_reopens_pane -x` | ❌ Wave 0 |
| DRILL-03 (Pitfall 1) | focus-return targets a STABLE id, not a captured node; survives an intervening swap | integration (assert `getElementById(...)` wiring in markup) OR a JS/dom test | `...::test_focus_return_by_stable_id -x` | ❌ Wave 0 |
| D-00b | every read degrades to 0/None, never 500 (simulate a failing source) | integration | `...::test_degrade_never_500 -x` | ❌ Wave 0 |

**Correctness assertion for DRILL-02 (load-bearing):** the per-agent bucket counts test should seed a known fixture (e.g. an agent owning M files with known per-stage states) and assert the aggregate returns the exact bucket counts — AND that the five enrich buckets sum to the agent's music/video file count on a healthy query (the same invariant `_safe_bucket_counts` documents, pipeline.py:338, as a healthy-query property only, never a runtime assertion). Assert against the derived truth (`resolve_status`/`stage_status_case`), not a hand-count, so it stays drift-locked.

### Sampling Rate
- **Per task commit:** `uv run pytest tests/integration/test_lane_detail.py tests/integration/test_agent_activity.py -x`
- **Per wave merge:** `just test-bucket analyze` + `just test-bucket agents`
- **Phase gate:** full suite green (`uv run pytest --cov`, 90% floor) before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/integration/test_lane_detail.py` — DRILL-01 (covers lane fields, kind-adaptive, recent completions, empty/degrade)
- [ ] `tests/integration/test_agent_activity.py` — DRILL-02 (aggregate correctness, remap, empty, scans/queues)
- [ ] `tests/integration/test_drill_poll_survival.py` — DRILL-03 (highlight-from-param, reload-reopens, trigger a11y markup, focus-return-by-id)
- [ ] Reuse existing conftest factories (`make_file`, agent/scan_batch/cloud_job fixtures) — verify a `cloud_job`-with-`backend_id` factory exists; if not, add one for the lane recent-completions test.
- [ ] **Test-DB footgun (MEMORY):** export BOTH `TEST_DATABASE_URL` (5433) and `MIGRATIONS_TEST_DATABASE_URL` — `just test-bucket` doesn't export the migration URL by default. Run the Postgres `GROUP BY` test against the real test DB (5433), not SQLite, or Pitfall 2 won't be caught.

## Security Domain

`security_enforcement` is not set to `false` in config → treated as enabled. Both endpoints are READ-ONLY on the private LAN (no auth dependency, consistent with the existing pipeline/admin operator pages — admin_agents.py:16-19).

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | operator pages are open on the private LAN (documented posture, admin_agents.py:16) |
| V3 Session Management | no | no sessions introduced |
| V4 Access Control | partial | typed/validated path params scope every read; `agent_id`/`backend_id` looked up against known rows, unknown → friendly empty fragment (not 500) — closes the BAC/template-path surface (record.py:10-13 T-61-03 precedent) |
| V5 Input Validation | yes | `backend_id` (str) and `agent_id` (str) are operator-declared; stay Jinja-autoescaped (T-71-05); prefer lookup-in-known-set over trusting the raw param |
| V6 Cryptography | no | none |

### Known Threat Patterns for FastAPI + Jinja HTMX fragments
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Reflected XSS via operator-declared id/name/kind | Tampering/Info-disclosure | Jinja autoescape (default on); never `|safe`/`|tojson` on these values (T-71-05) |
| Path/BAC via unvalidated path param | Elevation/Info-disclosure | Look up `backend_id` in the snapshot set / `agent_id` via `session.get(Agent, …)`; unknown → empty fragment, never 500 |
| DoS via unbounded read on the 5s tick | DoS | Every read bounded (LIMIT N / `GROUP BY` aggregate) + `_safe_count`/SAVEPOINT degrade (D-00b/PERF-01) — no whole-corpus scan per poll |
| Secret leakage in lane detail | Info-disclosure | `get_backend_lane_snapshot()` is already secret-free (no config/SecretStr/kube token, backends.py:765 T-71-01) — do not add raw `backend.config` to the detail body |

## Project Constraints (from CLAUDE.md)
- **`uv` only** — all commands `uv run …`; never bare `pip`/`python`/`pytest`/`mypy`.
- **Python 3.14**; ruff line length 150, `target-version = py313` (deferred-annotation reason); mypy strict (excludes `tests/`, `services/`... note: `mypy` excludes `^(tests/|prototype/|services/)` — but the NEW aggregate helper landing in `services/` is therefore mypy-excluded; keep type hints anyway per project style).
- **90% coverage floor**, Codecov per-service flags; per-bucket test isolation (`tests/buckets.json`).
- **Pre-commit frozen SHAs**, never `--no-verify`; bandit `-s B608`; T20 (no `print`) except CLI/tests.
- **PR per phase** on a worktree branch; no direct main commits; each seam (a/b/c) is a natural small-blast-radius PR (D-08 discretion).
- **`get_session` never commits in routers** — these endpoints are read-only (no commit); tests assert via the shared/independent session (MEMORY).
- **No raw `FileRecord.state` render** — guard test `test_no_raw_state_render.py` / `just docs-drift` (Pitfall 5).

## Sources

### Primary (HIGH confidence) — read in this worktree
- `src/phaze/services/stage_status.py` — `stage_status_case`/`done_clause`/`failed_clause`/`inflight_clause` (single derivation, D-00a) + `saq_detail` SAVEPOINT degrade
- `src/phaze/services/pipeline.py:180-361` — `get_agent_reconciliations` (GROUP BY agent_id precedent), `get_queue_activity` (all_lane_queues per-lane depth), `_safe_count`, `_safe_bucket_counts` (the D-04 template + GroupingError note), `get_stage_progress`
- `src/phaze/services/backends.py:700-801` — `get_backend_lane_snapshot`, `in_flight_count`, `_kind_of`, secret-free lane dict contract
- `src/phaze/routers/admin_agents.py` — `page`/`table_partial`/`_load_agents`/`classify` (agent endpoint host + liveness idiom)
- `src/phaze/routers/pipeline.py:311,686,740` — router prefix, `pipeline_stats_partial` (lane grid on poll), `get_backend_lane_snapshot` call site
- `src/phaze/routers/record.py:41-61` — typed-param HTML-fragment + friendly-404 endpoint precedent
- `src/phaze/templates/pipeline/partials/_lane_card.html`, `_analyze_lanes.html`, `analyze_workspace.html`, `_stage_matrix.html`, `_stage_pill.html` — token/box-model/remap contracts
- `src/phaze/templates/admin/partials/agents_table.html`, `admin/agents.html` — `#agents-table-section` poll root + error-banner listener
- `src/phaze/templates/shell/partials/record_host.html`, `cmdk_modal.html`, `src/phaze/templates/shell/shell.html:165-181` — Esc/focus-return precedent (modal — borrow discipline, not trap) + persistent-host placement
- `src/phaze/models/cloud_job.py`, `models/file.py:84-100`, `models/scan_batch.py:29-61` — CloudJob(backend_id/status/updated_at), FileRecord indexes (`ix_files_state`, `uq_files_agent_id_original_path`), ScanBatch(`ix_scan_batches_agent_id`)
- `src/phaze/templates/base.html:33-44` — HTMX 2.0.10 / Alpine 3.15.12 / @alpinejs/focus SRI pins
- `tests/conftest.py:229-232` — `client` fixture + `get_session` override
- `.planning/config.json:20` — `nyquist_validation: true`

### Secondary (MEDIUM confidence)
- HTMX 2.0.10 attribute semantics (`hx-push-url`, `hx-trigger="every 5s"`, `hx-vals`/`hx-include` for carrying `?param` onto the poll) — well-established and already used in-codebase; Open Question 2 flags the one non-obvious wiring (poll does not auto-append the pushed URL's query). Verify via Context7 (`/bigskysoftware/htmx`) if the planner wants exact `hx-vals`/`hx-include` confirmation.

### Tertiary (LOW confidence)
- None — every claim is codebase-grounded.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — no new packages; all deps verified in base.html/pyproject
- Data sources & aggregate shape: HIGH — exact templates read (`_safe_bucket_counts`, `get_backend_lane_snapshot`, `all_lane_queues`), incl. the GroupingError gotcha
- Poll-survival & a11y wiring: HIGH on the pattern (record_host/cmdk_modal precedents), MEDIUM on the single HTMX detail of carrying `?param` onto the existing poll request (Open Question 2)
- Lane recent-completions for the LOCAL kind: MEDIUM — genuinely ambiguous (Open Question 1)

**Research date:** 2026-07-11
**Valid until:** 2026-08-10 (stable — additive UI over a mature, locked derivation layer; no fast-moving external deps)
