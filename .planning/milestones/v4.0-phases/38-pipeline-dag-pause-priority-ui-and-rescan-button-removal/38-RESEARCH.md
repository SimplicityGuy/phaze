<!-- GSD:RESEARCH -->
# Phase 38: Pipeline DAG Pause/Priority UI and Rescan Button Removal - Research

**Researched:** 2026-06-12
**Domain:** Server-rendered HTMX 2.x + Alpine.js store-driven DAG UI; extend the existing 5s `/pipeline/stats` OOB poll with per-stage `{paused, priority}`; wire pause/resume + priority-stepper controls to the Phase 37 endpoints; remove a dead anchor.
**Confidence:** HIGH (every claim below is read directly from the installed templates/routers/tests in this repo; HTMX 2.x OOB/polling/`hx-swap=none` semantics confirmed against HTMX docs via Context7)

## Summary

Phase 38 is a pure front-end + thin-router phase layered on a mature, well-understood machine. The pipeline dashboard already runs a single 5-second `GET /pipeline/stats` poll (`dashboard.html:21`) whose HTML response carries **only hidden `hx-swap-oob` `x-init` store-write paragraphs** — it re-pushes server-computed ints into a single Alpine `$store.pipeline` (`base.html:106-117`) and **never re-renders the interactive DAG canvas or its buttons**. The DAG canvas (`dag_canvas.html`) binds every visible value (`x-text`, `:disabled`, `:class`, `:style`) to that store. This "poll writes the store; markup reads the store; buttons are never the swap target" architecture (Phase 34/35) is exactly the clobber-safe substrate Phase 38 needs, and the correct way to add per-stage pause/priority is to **extend it, not invent a new mechanism**.

Three concrete changes: (1) **delete the dead "Rescan Files" anchor** at `dag_canvas.html:202-203` — it is a plain `<a href="#trigger-scan-heading">` that only scrolls to the existing Trigger Scan card (`trigger_scan_card.html:12` / `<form hx-post="/pipeline/scans">`); removal is safe because the scan affordance lives entirely in that card, not the anchor. (2) **Add a Pause/Resume toggle + a priority stepper** (▲ Higher = delta −10, ▼ Lower = delta +10, "lower runs first" hint) to each of the three agent nodes (`node-metadata`, `node-analyze`, `node-fingerprint`), reading `$store.pipeline.<stage>Paused` / `.<stage>Priority` and POSTing to the Phase 37 endpoints. (3) **Extend `_build_dag_context`** (`routers/pipeline.py:106`) to read the three `pipeline_stage_control` rows and add six new store keys to the `dag` dict so the existing OOB poll reflects live pause/priority state.

**The single most important design decision (flagged in Open Questions Q1): how the control POSTs update the UI.** Phase 37 ships its endpoints returning **JSON** `{stage, priority, paused}` (`37-04-PLAN.md` Task 1, and its tests assert that JSON shape). HTMX swaps HTML, not JSON. The recommended, lowest-risk pattern that keeps the Phase 37 JSON contract (and its tests) untouched is **`hx-swap="none"` + an Alpine `@htmx:after-request` handler that parses `$event.detail.xhr.response` and writes the authoritative `priority`/`paused` into `$store.pipeline`** — identical in spirit to the existing `enqueue_button` macro's `@htmx:after-request` error handling (`dag_canvas.html:99-111`). The 5s poll then reconciles via the new OOB store seeds. This is faithful to the store-driven canvas and requires zero change to Phase 37.

**Primary recommendation:** Keep the Phase 37 JSON contract; drive the controls store-side (`hx-swap="none"` + after-request JSON parse + optimistic store write), reflect live state through six new int store keys re-pushed on the existing 5s OOB poll, delete the dead anchor, and **recompute the `NODE_LAYOUT` y-coordinates + canvas height** because taller agent chips will otherwise overlap (a real, test-enforced layout consequence — `test_topology_column_one_chips_do_not_overlap`).

## User Constraints

No `38-CONTEXT.md` exists yet (this research is standalone, ahead of discuss-phase). Constraints are drawn from ROADMAP Phase 38 (`.planning/ROADMAP.md:304-323`), the Phase 37 contract (`37-RESEARCH.md`, `37-04-PLAN.md`), STATE.md accumulated context, the approved 2026-06-12 inline design (auto-memory `project_stage_pause_priority_design`), and project CLAUDE.md. The planner MUST treat these as locked inputs.

### Locked Decisions (from ROADMAP Phase 38 + approved inline design)
- **Remove the "Rescan Files" anchor** on the Discovery node (`dag_canvas.html:202-203`). It is a duplicate of the Trigger Scan card's "Start Scan" → both target `POST /pipeline/scans`. Confirmed safe: the anchor only scrolls (`href="#trigger-scan-heading"`); it carries no `hx-post` and no behavior beyond navigation.
- **Per-stage controls on the 3 agent nodes only** (`metadata` / `analyze` / `fingerprint`): a **Pause/Resume toggle** + a **priority stepper** showing the raw number. Buttons labeled by intent: **"▲ Higher priority"** decrements the number (delta −10, higher priority = sooner), **"▼ Lower priority"** increments (delta +10), plus a **"lower runs first"** hint.
- **HTMX-post to the Phase 37 endpoints** (exact contracts below): `POST /pipeline/stages/{stage}/priority` body `{delta: int}` (default UI step ±10, clamp `[0,100]`), `POST /pipeline/stages/{stage}/pause`, `POST /pipeline/stages/{stage}/resume`. All return `{stage, priority, paused}` JSON.
- **Extend `/pipeline/stats`** to return each stage's `{paused, priority}` so controls reflect live state across the 5s refresh.
- **The existing `agentBusy`-based trigger-button disabling stays as-is** — explicitly OUT OF SCOPE; a separate concern. The new pause/priority controls MUST NOT be gated by `agentBusy` (pausing a busy stage is the entire point — drain semantics).
- Priority semantics (inherited from Phase 37, LOCKED): integer `0–100`, default `50`, **LOWER = higher priority = dequeues sooner**, maps directly to SAQ `priority` with no inversion. Pause = drain (active jobs finish, queued backlog parks). Resume = un-park only.

### Project Constraints (from CLAUDE.md)
- Python 3.14 exclusively; `uv` only — never bare `pip`/`python`/`pytest`/`mypy`, always `uv run`.
- Server-rendered HTMX 2.x + Jinja2 + Tailwind (self-hosted, NOT CDN — `base.html:28`) + Alpine.js 3.x. **NO React/SPA, no build step.**
- FastAPI + SQLAlchemy 2.0 async ORM; mypy strict (excludes tests/); ruff line length 150, `target-version = py313`.
- 85% min coverage, Codecov with service flags. Pre-commit must pass (frozen SHAs); never `--no-verify`.
- PR per phase, worktree branch, no direct main commits. Update affected READMEs + `scripts/update-project.sh`.
- Commit frequently during execution, not batched at the end.
- Dark mode is mandatory: every node chip carries a `dark:` class (enforced by `test_render_every_node_has_dark_class`).

### Deferred / Out of Scope
- Anything touching the Phase 37 control plane internals (`pipeline_stage_control` table, the `saq_jobs` UPDATE helpers, the enqueue hook) — Phase 37 owns those.
- Changing the `agentBusy` trigger gating, the enqueue trigger buttons, or the scan card.
- Non-agent stages (proposals/execute/tracklist) get no pause/priority controls.
- Homelab Step D consolidation (ROADMAP says "final consolidation here if any new env/UI config emerges" — but this phase introduces **no new env var**, so Step D has nothing to add from Phase 38).

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| REQ-38-1 | Operator can pause/resume each agent stage from the DAG | Pause/Resume toggle per agent node → `hx-post` to `/pipeline/stages/{stage}/pause|resume`; state from `$store.pipeline.<stage>Paused` (UI Notes §Components, §Interaction Flow) |
| REQ-38-2 | Operator can raise/lower priority per agent stage from the DAG | ▲/▼ stepper → `hx-post` to `/pipeline/stages/{stage}/priority` `{delta}`; raw number from `$store.pipeline.<stage>Priority` (UI Notes §Components) |
| REQ-38-3 | Rescan button gone | Delete `dag_canvas.html:202-203`; safe because it only scrolls to the Trigger Scan card (Pattern 1, Pitfall 4) |
| REQ-38-4 | Live state reflected across the 5s refresh | Extend `_build_dag_context` with six `<stage>Paused`/`<stage>Priority` int keys; OOB store seeds in `stats_bar.html` re-push them on each poll (Pattern 2, Pattern 3) |

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Render pause/priority controls + read live state | Browser (Alpine bindings on the DAG canvas) | — | Pure presentation; binds to `$store.pipeline`, mirrors the existing canvas |
| Issue pause/resume/priority mutations | Browser → API (`hx-post` to Phase 37 endpoints) | — | Controls are thin transport; all logic lives in the Phase 37 endpoints |
| Reflect authoritative result of a click | Browser (Alpine `@htmx:after-request` parses JSON → store) | — | HTMX `hx-swap=none`; no DOM swap, store updated from the JSON body |
| Supply live per-stage `{paused, priority}` | Frontend server (`_build_dag_context` reads control rows) | DB (`pipeline_stage_control` SELECT) | Same router seam that already feeds the DAG; degrade-safe read |
| Re-push live state every 5s | Frontend server (`stats_bar.html` OOB seeds) | — | Reuses the single existing poll; no new loop, no SSE |
| Persist pause/priority intent + mutate backlog | API + DB (Phase 37) | — | OUT OF SCOPE here — owned by Phase 37 |

## Standard Stack

No new packages. This phase uses only what is already installed and wired.

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| HTMX | 2.0.7 (self-referenced via unpkg, SRI-pinned `base.html:34`) | `hx-post` controls, `hx-swap="none"`, the existing 5s `hx-trigger="every 5s"` poll, `hx-swap-oob` store seeds | Already the project's hypermedia layer; the entire DAG poll is built on it |
| Alpine.js | 3.15.9 (CDN, SRI-pinned `base.html:40`) | `$store.pipeline` single source of truth; `x-text`/`:disabled`/`:class` bindings; `@htmx:after-request` JSON-parse handler | Already drives every live value on the canvas |
| Jinja2 | (FastAPI-bundled) | Render the new control fragment + OOB seed paragraphs | Existing template engine; partials live under `templates/pipeline/partials/` |
| Tailwind | 4.3.0 self-hosted (`static/vendor/tailwindcss-browser-4.3.0.min.js`) | Styling the controls (utility classes, `dark:` variants) | Self-hosted build is the locked project decision (quick task 260606-qgu) — do NOT reintroduce a CDN URL |
| FastAPI + SQLAlchemy 2.0 async | existing | `_build_dag_context` SELECT of the 3 control rows | The `/pipeline/stats` + dashboard router already uses this session |

**Installation:** none. `uv sync` unchanged. No `## Package Legitimacy Audit` is required (no external package is installed in this phase).

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `hx-swap="none"` + Alpine after-request JSON parse | Phase 37 endpoints return an HTML OOB partial | Cleaner HTMX-native instant reflection, BUT requires changing Phase 37's return type from JSON → HTMLResponse and rewriting its endpoint tests. Phase 37 isn't executed yet, so this is *possible* but couples the two phases. See Open Q1. |
| Reuse the existing 5s `/pipeline/stats` poll | A dedicated per-control poll or SSE | The project already rejected new polls/SSE for the DAG (`dag_canvas.html:5-8`); one poll, OOB store writes, is the locked pattern |
| Store the raw priority int in `$store.pipeline` | Re-render the stepper fragment on each poll via OOB swap | OOB-swapping the buttons would clobber an in-flight click and revert optimistic state — the exact failure the canvas comment warns against (`stats_bar.html:27-32`) |

## Architecture Patterns

### System flow (live-state without clobbering user input)

```
                       ┌─────────────────── 5s poll (existing) ───────────────────┐
                       │ GET /pipeline/stats                                       │
                       ▼                                                           │
OPERATOR clicks ▲/▼/Pause            routers/pipeline.py:_build_dag_context        │
  │ hx-post /pipeline/stages/{s}/…   reads pipeline_stage_control (3 rows)         │
  │ hx-swap="none"                   → dag["metadataPaused"/"metadataPriority"/…]  │
  ▼                                            │                                   │
Phase 37 endpoint (JSON {stage,priority,paused})                                   │
  │                                            ▼                                   │
  │ @htmx:after-request                 stats_bar.html (oob_counts):               │
  │   JSON.parse(xhr.response)          <p id="dag-seed-metadataPaused"            │
  │   $store.pipeline.<s>Paused = …       hx-swap-oob="true"                       │
  │   $store.pipeline.<s>Priority = …     x-init="$store.pipeline.metadataPaused   │
  ▼                                                = 1"> …                         │
$store.pipeline  ◄───── Alpine writes ──────────────┘                              │
  │  (single source of truth — base.html:106)                                      │
  ▼                                                                                │
DAG canvas controls bind: x-text=$store.pipeline.<s>Priority,                      │
   :class on Pause toggle from $store.pipeline.<s>Paused  ◄───── never OOB-swapped ┘
```

The buttons are **read-only consumers** of the store; the poll only ever writes hidden seed paragraphs. An in-flight click and a racing poll both converge on the store; the after-request handler lands the authoritative value. This is the identical contract the Phase 34/35 canvas already relies on.

### Recommended file touch-list
```
src/phaze/templates/pipeline/partials/dag_canvas.html   # delete anchor; add control fragment to 3 agent nodes; recompute NODE_LAYOUT
src/phaze/templates/pipeline/partials/stats_bar.html    # add 6 OOB store-seed paragraphs (inside the oob_counts gate)
src/phaze/templates/base.html                           # add 6 store keys seeded to 0
src/phaze/routers/pipeline.py                            # extend _build_dag_context to read control rows → 6 dag keys
src/phaze/services/pipeline.py                           # (recommended) add get_stage_controls(session) degrade-safe reader
tests/test_dag_canvas_render.py                          # update the EXACT-4-hx-post assertion; add control + anchor-removed tests
tests/test_pipeline_dag_context.py                       # extend _NEW_STORE_KEYS + store test with the 6 new keys
```

### Pattern 1: Delete the dead Rescan anchor (REQ-38-3)
**What:** Remove `dag_canvas.html:202-203` (the `<a href="#trigger-scan-heading" … >Rescan Files</a>`). Also update the stale comments that reference it: `:152` ("Discovery's Rescan stays enabled regardless") and `:191` ("root; Rescan links to the existing Trigger Scan card"). The Discovery node then renders header + count + bar only (no action) — which is correct: scanning is initiated solely from the Trigger Scan card above (`dashboard.html:14`).
**Why safe:** The anchor has no `hx-post`, no Alpine handler, no behavior beyond an in-page scroll to `#trigger-scan-heading` (`trigger_scan_card.html:12`). Nothing reads or depends on it. Grep confirms no test asserts the string "Rescan" (verify during planning; add a negative assertion that it is gone).

### Pattern 2: Per-stage controls on the agent nodes (REQ-38-1, REQ-38-2)
**What:** A reusable Jinja macro `stage_controls(stage)` inserted after `enqueue_button(...)` inside `node-metadata` (`:218`), `node-analyze` (`:233`), `node-fingerprint` (`:248`). It renders a Pause/Resume toggle and a ▲/▼ priority stepper, all bound to `$store.pipeline.<stage>Paused` / `.<stage>Priority`.
**Key constraints:**
- The stepper number is `x-text="$store.pipeline.metadataPriority"`.
- ▲ Higher posts `{delta: -10}`; disable when `$store.pipeline.metadataPriority <= 0`.
- ▼ Lower posts `{delta: +10}`; disable when `$store.pipeline.metadataPriority >= 100`.
- Pause toggle: when `!paused` show "Pause" → `hx-post=".../pause"`; when `paused` show "Resume" → `hx-post=".../resume"`. (Either a single label-flipping button or two `x-show`-gated buttons; single button is cleaner.)
- `hx-swap="none"` on every control (the JSON response is consumed by Alpine, not swapped).
- `hx-disabled-elt="this"` (HTMX 2.x) to prevent a double-click while the POST is in flight.
- An `@htmx:after-request` handler parses the JSON and writes the store (see Code Examples).
- The controls are **independent of `agentBusy`** — never add the `nodes.<node>.blocked` gate to them.

### Pattern 3: Extend the poll payload + store (REQ-38-4)
**What (router):** In `_build_dag_context` (`routers/pipeline.py:106-148`) add a degrade-safe read of the three control rows and six int keys to the `dag` dict:
```python
controls = await get_stage_controls(session)   # degrade-safe; defaults paused=False, priority=50
dag.update({
    "metadataPaused": int(controls["metadata"]["paused"]),
    "metadataPriority": controls["metadata"]["priority"],
    "analyzePaused": int(controls["analyze"]["paused"]),
    "analyzePriority": controls["analyze"]["priority"],
    "fingerprintPaused": int(controls["fingerprint"]["paused"]),
    "fingerprintPriority": controls["fingerprint"]["priority"],
})
```
`paused` is emitted as `0`/`1` (int) to preserve the template's load-bearing invariant — *every* `dag` value is a server-computed int safe to interpolate into `x-init` numeric store writes (`dag_canvas.html:19-21`, T-35-11). The store reads `<stage>Paused` for truthiness (`0`/`1` works in JS conditionals).
**What (stats_bar.html):** The existing `{% for key, value in dag.items() %}` loop (`:66-68`) already emits one OOB `dag-seed-<key>` paragraph per dag key — **the six new keys flow through automatically** with no new template code, as long as they are in the `dag` dict. (Verify the loop covers them; it does by construction.)
**What (base.html):** Add the six keys to the `Alpine.store('pipeline', {...})` literal (`:106-117`), each seeded to `0`, so no binding reads `undefined` before the first poll. The DAG canvas's in-place seeds (`dag_canvas.html:161-163`) also flow these through automatically (same `dag.items()` loop).

### Pattern 4: Degrade-safe control read (mirror `get_queue_activity` / `_safe_count`)
**What:** `get_stage_controls(session)` must **never raise into the 5s poll**. The `pipeline_stage_control` table may not exist yet (before migration 020 in a partially-migrated env), or a DB hiccup may occur. Wrap the SELECT in try/except and degrade to defaults `{paused: False, priority: 50}` for all three stages on any failure — identical discipline to `get_queue_activity` (`services/pipeline.py:63-120`) and `_safe_count` (`:141-158`). On degrade, the controls render at default state and the poll still returns 200.

### Anti-Patterns to Avoid
- **OOB-swapping the control buttons** on the poll — clobbers in-flight clicks (the exact warning at `stats_bar.html:27-32`). Only OOB-swap the hidden store-seed paragraphs.
- **Gating pause/priority on `agentBusy`** — you must be able to pause a *running* stage (drain). The `agentBusy` gate is for the enqueue triggers only.
- **Changing Phase 37's JSON return type** without also rewriting its endpoint tests — couples two phases; prefer the store-side `hx-swap=none` pattern (Open Q1).
- **Interpolating `paused` as a JS `true`/`false` literal** into `x-init` — breaks the "all dag values are ints" invariant; emit `0`/`1`.
- **Adding controls without recomputing `NODE_LAYOUT`** — taller chips overlap; `test_topology_column_one_chips_do_not_overlap` will fail (Pitfall 1).
- **Reintroducing a Tailwind/HTMX CDN URL** — Tailwind is deliberately self-hosted (`base.html:21-28`).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Live-state refresh of the controls | A new per-control poll, SSE, or websocket | The existing 5s `/pipeline/stats` OOB store seeds | One poll already exists and was deliberately kept singular (`dag_canvas.html:5-8`) |
| Reflecting a click's result | Re-render the whole node via OOB swap | `hx-swap="none"` + Alpine `@htmx:after-request` JSON→store write | No DOM clobber; mirrors the enqueue_button error pattern (`:99-111`) |
| Double-click protection | Custom disabled-flag bookkeeping | `hx-disabled-elt="this"` (HTMX 2.x) | Native; HTMX disables the element for the request duration |
| Per-stage state plumbing | A new context object / new template | Add ints to the existing `dag` dict — the `dag.items()` loops in both `stats_bar.html` and `dag_canvas.html` propagate them for free | Zero new seed-paragraph code |
| Never-500 degrade | Bare SELECT in the hot poll path | `get_stage_controls` with try/except → defaults | The poll must never 500 (T-35-09 discipline already in `services/pipeline.py`) |

**Key insight:** Phase 38 adds *zero new infrastructure*. It is "six more int keys in the `dag` dict + one control macro + a JSON-parse handler + a layout recompute + one deleted anchor." Every hard problem (clobber-safety, degrade, polling) is already solved by the Phase 34/35 store pattern.

## Runtime State Inventory

This is a UI + read-only-router phase. No rename/migration, but the "what state changes" categories are answered explicitly.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| **Stored data** | None written by this phase. The `pipeline_stage_control` rows are READ here; they are written only by the Phase 37 endpoints this UI POSTs to. | None — read-only in `_build_dag_context`. |
| **Live service config** | The Alpine `$store.pipeline` gains 6 keys; the `/pipeline/stats` HTML response gains 6 OOB seed paragraphs. Both are derived state, not persisted config. | Add keys to `base.html` store literal; extend `_build_dag_context`. |
| **OS-registered state** | None. | None — verified by scope (no systemd/cron/env touched). |
| **Secrets/env vars** | None new. ROADMAP's "Step D consolidation if new env emerges" has **nothing to add from Phase 38** — no new env var is introduced. | None. |
| **Build artifacts / installed packages** | None — no new dependency; `uv.lock` unchanged. | None. No Package Legitimacy Audit needed. |

**The canonical question:** after this phase ships, what changes at runtime? Only the rendered DAG (new controls) and the `/pipeline/stats` payload (6 more hidden seeds). All durable state is owned by Phase 37.

## Common Pitfalls

### Pitfall 1: Taller agent chips overlap the chips below them (test-enforced)
**What goes wrong:** The three agent nodes are absolutely positioned at fixed `top:` offsets via `NODE_LAYOUT` (`dag_canvas.html:31-41`): metadata `y:24`, analyze `y:206`, fingerprint `y:388`, scan_search `y:570` — a uniform 182px vertical gutter. Each agent chip currently renders ~154px (header+count+bar+button). Adding a pause toggle + a stepper row (~70-100px) pushes each agent chip to ~240-260px, so the 182px gutter **overlaps** the chip below.
**Why it happens:** Node chips are content-height (`style` sets only `left/top/width`); `NODE_LAYOUT.h` only feeds edge anchors. Increasing content height without increasing the `y` deltas causes paint-over — the exact regression `test_topology_column_one_chips_do_not_overlap` (`test_dag_canvas_render.py:146-169`) was written to catch.
**How to avoid:** Recompute the col-1 `y` positions to ≥ ~260px gutters (e.g. metadata `24`, analyze `~300`, fingerprint `~576`, scan_search `~852`), grow the canvas wrapper height (`dag_canvas.html:177` `height: 720px` and the `<svg>` height/viewBox `:178`) accordingly, and re-balance the col-2/col-3 node `y` positions (proposals/scrape/execute/match) so edges still land cleanly. Update `test_topology_column_one_chips_do_not_overlap`'s `min_chip_height` to the new measured agent-chip height. **Treat the layout recompute as a first-class task, not an afterthought.**
**Warning signs:** Visual overlap in the rendered SVG; the overlap regression test failing.

### Pitfall 2: The "exactly 4 hx-post" test breaks
**What goes wrong:** `test_gating_triggers_post_only_to_existing_endpoints` (`test_dag_canvas_render.py:240-251`) asserts the canvas contains **exactly** four `hx-post="/pipeline/...` calls and a specific sorted list. Adding pause/resume/priority controls adds new `hx-post` targets (`/pipeline/stages/...`), breaking this test.
**How to avoid:** Update that test deliberately: either scope its regex to the enqueue-trigger targets only, or extend the expected set to include the new `/pipeline/stages/{stage}/{priority,pause,resume}` targets (9 new posts: 3 stages × 3 actions, or fewer if pause/resume share a button). Document the change so it is an intentional contract update, not an accidental loosening.
**Warning signs:** CI fails on the count assertion after adding controls.

### Pitfall 3: `paused` emitted as a JS boolean literal corrupts the seed write
**What goes wrong:** Writing `x-init="$store.pipeline.metadataPaused = {{ paused }}"` where `paused` is a Python `True` interpolates the literal `True` (capital T) — invalid JS, silently breaking the seed.
**Why it happens:** Jinja renders Python `True`/`False` with capitals; the template's whole safety model assumes `dag` values are ints (`dag_canvas.html:19-21`).
**How to avoid:** Coerce in the router: `int(controls[stage]["paused"])` → `0`/`1`. The store reads it as truthy/falsy. Never pass a Python bool through to the `x-init` interpolation.
**Warning signs:** A console JS error on poll; the pause toggle never reflecting live state.

### Pitfall 4: Removing the anchor but leaving the empty-state hint stale
**What goes wrong:** `dag_canvas.html:152-156` renders an empty-state hint whose comment says "Discovery's Rescan stays enabled regardless." After the anchor is gone, that comment is stale and could mislead a future editor; the hint copy itself ("Trigger a scan to populate the pipeline") is still correct because the Trigger Scan card remains.
**How to avoid:** Update the comments at `:152` and `:191`; keep the empty-state hint (it points the operator at the still-present Trigger Scan card).
**Warning signs:** Reviewer confusion; a future PR re-adding a Rescan affordance.

### Pitfall 5: Optimistic store write racing the poll
**What goes wrong:** Operator clicks ▲ (optimistic `priority -= 10`); a 5s poll fires mid-flight and re-pushes the *old* priority before the endpoint commits, making the number flicker back then forward.
**Why it happens:** The poll and the click are independent; the poll seed reflects whatever the control row held at poll time.
**How to avoid:** Prefer the **authoritative** path: do NOT optimistically mutate on click; instead let `@htmx:after-request` write the JSON `priority`/`paused` (the post commits before responding, so the returned value is authoritative). The brief delay (one request RTT, ~tens of ms on a LAN) is invisible. If an optimistic feel is wanted, write optimistically on `@click` AND authoritatively on after-request — the after-request value wins, and a racing poll only briefly reverts. For a single-user admin tool the authoritative-only path is simplest and flicker-free. Document the choice.
**Warning signs:** The stepper number bouncing after a click.

## Code Examples

### The `stage_controls` macro (control fragment) — store-driven, hx-swap=none
```jinja
{# Source: new macro in dag_canvas.html, modeled on enqueue_button (:98-112) +
   the @htmx:after-request handler pattern. `stage` ∈ {metadata, analyze, fingerprint}.
   All values read $store.pipeline.<stage>Paused / .<stage>Priority. NOT gated by agentBusy. #}
{% macro stage_controls(stage) %}
<div class="mt-2 pt-2 border-t border-gray-100 dark:border-phaze-border"
     x-data
     @htmx:after-request="
       if ($event.detail.successful) {
         const r = JSON.parse($event.detail.xhr.response);
         $store.pipeline.{{ stage }}Priority = r.priority;
         $store.pipeline.{{ stage }}Paused = r.paused ? 1 : 0;
       }">
  <div class="flex items-center justify-between gap-2">
    {# Pause/Resume toggle — single button, label + target flip on paused state #}
    <button type="button" hx-swap="none" hx-disabled-elt="this"
            :hx-post="$store.pipeline.{{ stage }}Paused
                        ? '/pipeline/stages/{{ stage }}/resume'
                        : '/pipeline/stages/{{ stage }}/pause'"
            :aria-label="($store.pipeline.{{ stage }}Paused ? 'Resume' : 'Pause') + ' {{ stage }} stage'"
            class="px-2 py-1 text-xs font-semibold rounded-md text-white
                   focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
            :class="$store.pipeline.{{ stage }}Paused
                      ? 'bg-green-600 dark:bg-green-700 hover:bg-green-700'
                      : 'bg-amber-600 dark:bg-amber-700 hover:bg-amber-700'"
            x-text="$store.pipeline.{{ stage }}Paused ? 'Resume' : 'Pause'"></button>
    {# Priority stepper — ▲ Higher = delta -10, ▼ Lower = delta +10 #}
    <div class="inline-flex items-center gap-1">
      <button type="button" hx-post="/pipeline/stages/{{ stage }}/priority"
              hx-vals='{"delta": -10}' hx-swap="none" hx-disabled-elt="this"
              :disabled="$store.pipeline.{{ stage }}Priority <= 0"
              aria-label="Higher priority for {{ stage }} (runs sooner)"
              class="px-1 py-1 min-h-[28px] text-xs rounded border border-gray-300 dark:border-phaze-border disabled:opacity-40">▲ Higher</button>
      <span class="text-sm font-semibold tabular-nums w-7 text-center"
            x-text="$store.pipeline.{{ stage }}Priority"
            :aria-label="'priority ' + $store.pipeline.{{ stage }}Priority"></span>
      <button type="button" hx-post="/pipeline/stages/{{ stage }}/priority"
              hx-vals='{"delta": 10}' hx-swap="none" hx-disabled-elt="this"
              :disabled="$store.pipeline.{{ stage }}Priority >= 100"
              aria-label="Lower priority for {{ stage }} (runs later)"
              class="px-1 py-1 min-h-[28px] text-xs rounded border border-gray-300 dark:border-phaze-border disabled:opacity-40">▼ Lower</button>
    </div>
  </div>
  <p class="mt-1 text-[11px] text-gray-500 dark:text-gray-400">lower number runs first</p>
</div>
{% endmacro %}
```
*Note:* `:hx-post` is an Alpine-bound attribute; confirm HTMX picks up the bound value (Alpine sets the literal `hx-post` attribute before HTMX processes the click — works because HTMX reads the attribute at request time, but **verify in a render+behavior test**; the simplest robust alternative is two `x-show`-gated buttons each with a static `hx-post`, which sidesteps any bound-attribute timing question — recommend that variant if the bound `:hx-post` proves flaky).

### Extending the router (`_build_dag_context`) + degrade-safe reader
```python
# Source: new helper in services/pipeline.py, mirroring get_queue_activity degrade style.
_DEFAULT_CONTROLS = {s: {"paused": False, "priority": 50} for s in ("metadata", "analyze", "fingerprint")}

async def get_stage_controls(session: AsyncSession) -> dict[str, dict[str, int | bool]]:
    """Read the 3 pipeline_stage_control rows; degrade to defaults so the 5s poll never 500s."""
    try:
        rows = (await session.execute(select(PipelineStageControl))).scalars().all()
        out = {s: dict(v) for s, v in _DEFAULT_CONTROLS.items()}
        for r in rows:
            if r.stage in out:
                out[r.stage] = {"paused": r.paused, "priority": r.priority}
        return out
    except Exception:
        logger.warning("stage_controls_degraded", exc_info=True)
        try:
            await session.rollback()
        except Exception:
            logger.warning("stage_controls_rollback_failed", exc_info=True)
        return {s: dict(v) for s, v in _DEFAULT_CONTROLS.items()}

# In routers/pipeline.py _build_dag_context, after the dag dict is built:
controls = await get_stage_controls(session)
for stage in ("metadata", "analyze", "fingerprint"):
    dag[f"{stage}Paused"] = int(controls[stage]["paused"])
    dag[f"{stage}Priority"] = int(controls[stage]["priority"])
```

### base.html store extension
```javascript
// Source: base.html:106-117 Alpine.store('pipeline', {...}) — add 6 keys seeded to 0.
metadataPaused: 0, metadataPriority: 0,
analyzePaused: 0, analyzePriority: 0,
fingerprintPaused: 0, fingerprintPriority: 0
```
*(Seeded to 0; the real value lands on the first poll / full-page seed. Priority seeded to 0 is acceptable as a pre-poll placeholder — the page always seeds the real value in-place at render via the `dag.items()` loop, so there is no flash.)*

## UI Notes

> For the downstream `/gsd:ui-phase` UI-SPEC. This is an extension of an existing, locked DAG canvas — the UI-SPEC should treat the established canvas chrome (chip layout, state pills, dark mode, the `<ol>` text equivalent) as fixed and specify only the new controls + the deleted anchor.

### Component inventory (new)
| Component | Location | Bound to | Action |
|-----------|----------|----------|--------|
| **Pause/Resume toggle** | Each agent node chip (metadata, analyze, fingerprint), below the enqueue trigger button | `$store.pipeline.<stage>Paused` (label + color flip) | `hx-post` `/pause` (when running) or `/resume` (when paused), `hx-swap="none"` |
| **Priority stepper — ▲ Higher** | Same control row, left of the number | disabled when `<stage>Priority <= 0` | `hx-post` `/pipeline/stages/<stage>/priority` `{delta: -10}` |
| **Priority value** | Center of the stepper | `x-text=$store.pipeline.<stage>Priority` (raw int, `tabular-nums`) | read-only |
| **Priority stepper — ▼ Lower** | Right of the number | disabled when `<stage>Priority >= 100` | `hx-post` `.../priority` `{delta: 10}` |
| **"lower number runs first" hint** | Below the control row | static | none |
| **(removed) Rescan Files anchor** | Discovery node | — | DELETED (`dag_canvas.html:202-203`) |

### States per control
- **Pause toggle:** `running` (amber "Pause") ↔ `paused` (green "Resume"). In-flight: `hx-disabled-elt` greys it for the request.
- **Priority stepper:** normal · `at floor` (▲ disabled, priority 0 = max urgency) · `at ceiling` (▼ disabled, priority 100 = least urgent) · in-flight (clicked button disabled).
- **Degrade state:** if the control table is unreadable, controls render at defaults (running, priority 50) — never an error or a broken control.
- **Pre-poll state:** on full-page load the in-place seeds set the real values immediately (no flash); the store's `0` default only applies for the sub-millisecond before Alpine `x-init` runs.

### Interaction flow
1. Operator views the DAG; each agent node shows its live priority number and pause state (from the store, refreshed every 5s).
2. Operator clicks **▲ Higher** on Analyze → `hx-post .../analyze/priority {delta:-10}`, `hx-swap=none`. HTMX disables the button for the request. The endpoint updates the control row + reorders the queued backlog, returns `{stage:"analyze", priority:40, paused:false}`. Alpine `@htmx:after-request` writes `$store.pipeline.analyzePriority = 40`. The number updates; ▲ re-enables (still > 0).
3. Operator clicks **Pause** on Metadata → `hx-post .../metadata/pause`. Returns `{…, paused:true}`. The toggle flips to green "Resume". Live backlog parks (Phase 37); the Metadata `count("queued")` may drop (Phase 37 Pitfall 1 — a separate dashboard count, not this control).
4. The 5s poll continues to re-push all six keys, reconciling state if anything changed out-of-band (e.g. another tab, or a pause that re-parked re-enqueued jobs).

### Accessibility
- Each control has an `:aria-label` that names the action and the current value/state (e.g. "Resume metadata stage", "priority 40", "Higher priority for analyze (runs sooner)").
- The stacked `<ol>` text-equivalent (`dag_canvas.html:339-360`) is the screen-reader fallback for the SVG canvas. **Decide (Open Q3)** whether to surface paused/priority in the `<ol>` `<li>` for the three agent stages so the text equivalent stays complete; recommended yes (append " — paused" / " — priority N").
- Color is never the only signal (project rule): the Pause toggle flips its **text label**, not just color.

### Layout note (critical)
Adding the control row makes the three agent chips materially taller. The UI-SPEC MUST account for recomputed `NODE_LAYOUT` y-positions and a taller canvas (Pitfall 1). The Scan/Search node (no controls) and the col-2/col-3 nodes shift accordingly so edges remain clean.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Phase-34 stage cards + processing card | Single DAG canvas, store-driven, one 5s OOB poll | Phase 35 | The substrate Phase 38 extends; no new poll/SSE |
| Discovery node carried a "Rescan Files" scroll anchor | Discovery node is display-only (scan lives in Trigger Scan card) | Phase 38 | Removes a confusing duplicate affordance |
| No per-stage operator controls on the DAG | Pause/Resume + priority stepper on the 3 agent nodes | Phase 38 (this) | Operator drives Phase 37 control plane from the graph |

**Deprecated/outdated:** none. HTMX 2.0.7 + Alpine 3.15.9 are current and pinned. `hx-disabled-elt` and `hx-swap="none"` are stable HTMX 2.x attributes (confirmed via Context7).

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | Phase 37 endpoints return JSON `{stage, priority, paused}` (not an HTML partial) | Summary, Pattern 2 | If Phase 37 is changed to return HTML, the `@htmx:after-request` JSON parse must become a normal OOB swap instead. Confirmed against `37-04-PLAN.md`; re-verify after Phase 37 executes. |
| A2 | The Phase 37 priority endpoint accepts `{delta: int}` and the default UI step is ±10 | UI Notes, Pattern 2 | If the step or body shape differs, the `hx-vals` must change. Locked in `37-04-PLAN.md` (delta, step ±10, clamp 0–100). |
| A3 | `pipeline_stage_control` rows exist (migration 020 seeds metadata/analyze/fingerprint) by the time this UI ships | Pattern 3, Pattern 4 | Mitigated by the degrade-safe reader (defaults paused=False/priority=50); UI still renders if the table is absent. |
| A4 | Alpine-bound `:hx-post` is honored by HTMX at click time | Code Examples | If the bound attribute races HTMX processing, fall back to two static-`hx-post` buttons gated by `x-show` (noted inline). Verify in a behavior test. |
| A5 | No test currently asserts the literal string "Rescan" such that deletion silently passes | Pattern 1 | Low — grep for "Rescan" in tests during planning; add a negative assertion. |
| A6 | Emitting `paused` as int `0`/`1` keeps the template's "all dag values are ints" invariant intact | Pattern 3, Pitfall 3 | None if followed; breaking it injects invalid JS into x-init. |
| A7 | The existing `dag.items()` loops auto-propagate the 6 new keys (no new seed-paragraph code) | Pattern 3 | Verified by reading `stats_bar.html:66-68` and `dag_canvas.html:161-163`; both iterate `dag`. |

## Open Questions

1. **How does a control POST update the UI — store-side JSON parse vs HTML OOB partial?** *(the central design decision)*
   - What we know: Phase 37 ships JSON `{stage, priority, paused}` with tests asserting that shape; Phase 37 is not yet executed.
   - What's unclear: whether to (a) keep JSON and consume it via `hx-swap="none"` + Alpine `@htmx:after-request` (zero Phase 37 change), or (b) have Phase 37 (or a Phase 38 wrapper) return an HTML OOB store-seed fragment (HTMX-native, instant, but couples the phases and rewrites Phase 37 tests).
   - **Recommendation:** (a) — keep the Phase 37 JSON contract untouched; drive the controls store-side. It is the lowest-coupling option and is a faithful extension of the existing store-driven canvas. Revisit only if the bound-attribute approach (A4) proves flaky, in which case still keep JSON and use static-`hx-post` buttons.

2. **Optimistic store write on click, or authoritative-only on after-request?**
   - Recommendation: authoritative-only (write the store from the JSON response). Flicker-free, simplest, and the request RTT is negligible on a LAN single-user tool. (Pitfall 5.)

3. **Surface paused/priority in the `<ol>` text equivalent for the 3 agent stages?**
   - Recommendation: yes — append " — paused" / " — priority N" to the metadata/analyze/fingerprint `<li>` so the screen-reader fallback stays complete. Cheap; preserves the canvas's text-equivalence guarantee.

4. **Single label-flipping Pause/Resume button vs two `x-show`-gated buttons?**
   - Recommendation: single button (cleaner DOM). If A4 (bound `:hx-post`) is undesirable, two static-`hx-post` buttons gated by `x-show` on `<stage>Paused` is the robust fallback and also resolves Q1's bound-attribute concern.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| HTMX | controls + poll | ✓ (`base.html:34`) | 2.0.7 | — |
| Alpine.js | store bindings + after-request handler | ✓ (`base.html:40`) | 3.15.9 | — |
| Jinja2 / FastAPI / SQLAlchemy async | render + router read | ✓ | existing | — |
| Tailwind (self-hosted) | control styling | ✓ (`static/vendor/...`) | 4.3.0 | — |
| `pipeline_stage_control` table + Phase 37 endpoints | live state + mutations | ✗ until Phase 37 executes | — | Degrade-safe reader renders defaults; controls POST to endpoints that 404 until Phase 37 lands |

**Missing dependencies with no fallback:** none for *rendering* (degrade-safe). **Functional** dependency: the controls do nothing useful until Phase 37's endpoints + table exist. **Phase 38 must land after Phase 37** (ROADMAP "Depends on: Phase 37"). Plan the UI to render harmlessly even if Phase 37 hasn't shipped (defaults), but treat Phase 37 as a hard predecessor for the feature to function.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (`uv run pytest`) |
| Config file | pyproject.toml `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/test_dag_canvas_render.py tests/test_pipeline_dag_context.py -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` |

The DAG UI is tested two ways already (mirror these exactly): **pure-Jinja render tests** (`test_dag_canvas_render.py` — render the partial with a fake context, assert markup/topology/copy) and **DB-backed integration tests** via the shared `client` fixture (GET `/pipeline/` and `/pipeline/stats`, assert OOB seeds). Store-literal text assertions live in `test_pipeline_dag_context.py`.

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| REQ-38-3 | The "Rescan Files" anchor is gone; no `href="#trigger-scan-heading"` Rescan link in the canvas | pure render | `uv run pytest tests/test_dag_canvas_render.py -x` | ⚠️ add to existing file |
| REQ-38-1 | Each agent node renders a Pause/Resume toggle posting to `/pipeline/stages/{stage}/pause|resume` with `hx-swap="none"` | pure render | `uv run pytest tests/test_dag_canvas_render.py -x` | ⚠️ add |
| REQ-38-2 | Each agent node renders ▲/▼ steppers posting `/pipeline/stages/{stage}/priority` with `{delta:-10}`/`{delta:10}`, value bound to `<stage>Priority` | pure render | `uv run pytest tests/test_dag_canvas_render.py -x` | ⚠️ add |
| REQ-38-2 | ▲ disabled at priority 0, ▼ disabled at priority 100 (clamp mirror) | pure render | `uv run pytest tests/test_dag_canvas_render.py -x` | ⚠️ add |
| REQ-38-1/2 | Controls are NOT gated by `agentBusy` (no `nodes.<node>.blocked` on the control markup) | pure render | `uv run pytest tests/test_dag_canvas_render.py -x` | ⚠️ add |
| REQ-38-4 | `_build_dag_context` returns the 6 `<stage>Paused`/`<stage>Priority` int keys | DB-backed | `uv run pytest tests/test_pipeline_dag_context.py -x` | ⚠️ extend `_NEW_STORE_KEYS` |
| REQ-38-4 | `GET /pipeline/stats` emits an OOB `dag-seed-<key>` paragraph for each of the 6 new keys | integration | `uv run pytest tests/test_pipeline_dag_context.py -x` | ⚠️ extend |
| REQ-38-4 | `base.html` store literal seeds all 6 new keys to 0 (no undefined) | store-text | `uv run pytest tests/test_pipeline_dag_context.py -x` | ⚠️ extend `_NEW_STORE_KEYS` |
| REQ-38-4 | The poll degrades to 200 (defaults) when the control table is unreadable | integration | `uv run pytest tests/test_pipeline_dag_context.py -x` | ⚠️ add (mirror `test_stats_poll_degrades_to_200_without_counter_source`) |
| guard | The overlap regression still passes with recomputed `NODE_LAYOUT` + taller chips | pure render | `uv run pytest tests/test_dag_canvas_render.py::test_topology_column_one_chips_do_not_overlap -x` | ⚠️ update `min_chip_height` |
| guard (update) | The "exactly 4 hx-post" test is updated to include the new stage-control posts | pure render | `uv run pytest tests/test_dag_canvas_render.py -x` | ⚠️ update existing |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_dag_canvas_render.py tests/test_pipeline_dag_context.py -x`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing`
- **Phase gate:** full suite green + ≥85% coverage on touched modules before `/gsd:verify-work`.

### Wave 0 Gaps
- [ ] Extend `tests/test_dag_canvas_render.py`: Rescan-removed assertion; control-fragment render (toggle + steppers per agent node, hx targets, `hx-swap=none`, disabled bounds, not-`agentBusy`-gated); update the exact-4-hx-post test; update the overlap test's `min_chip_height`.
- [ ] Extend `tests/test_pipeline_dag_context.py`: add the 6 keys to `_NEW_STORE_KEYS`; assert `_build_dag_context` returns them as ints; assert OOB seeds for them; add a control-table-unreadable degrade test.
- [ ] `services/pipeline.py::get_stage_controls` degrade-safe reader (depends on Phase 37's `PipelineStageControl` model — import lands when Phase 37 ships).
- [ ] No new test *file* needed — extend the two existing DAG test files (mirror their structure).

## Security Domain

`security_enforcement` is not configured in `.planning/config.json` (treat as enabled).

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | No new auth surface; the dashboard + controls sit behind the same reverse-proxy internal-realm auth as the rest of `phaze-api` (LOCKED, consistent with `/pipeline/*` and `/saq`) |
| V3 Session Management | no | — |
| V4 Access Control | yes (infra) | Operator-only via the reverse proxy; no app-layer auth added (this phase only adds front-end controls + a read-only router extension) |
| V5 Input Validation | yes | This phase sends only a fixed `{delta: ±10}` and a path `stage` from a hardcoded allowlist of 3 nodes; the **Phase 37 endpoints** own the authoritative `stage`-allowlist + clamp validation (T-37-01/02). The UI must not be the only guard — it isn't. |
| V6 Cryptography | no | No secrets handled |

### Known Threat Patterns for this UI extension
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| XSS via interpolating server values into `x-init`/`x-text` | Tampering (injection) | Only server-computed **ints** are interpolated into `x-init` (the existing T-35-11 invariant; `paused` coerced to `0`/`1`); Jinja autoescape on for `x-text` content |
| A forged/oversized `delta` driving priority out of range | DoS (pipeline stall) | The UI sends a fixed ±10; the Phase 37 endpoint clamps `[0,100]` + DB CHECK — the UI is never the sole guard (T-37-02) |
| Poll 500 if the control table is missing/unreadable | Availability | `get_stage_controls` degrades to defaults, never raises into the 5s poll (mirrors `get_queue_activity` / `_safe_count`) |
| OOB swap clobbering an in-flight control click | Tampering (state corruption / UX) | Only hidden store-seed paragraphs are OOB-swapped; the buttons are never the swap target (the locked Phase 34/35 contract) |

## Sources

### Primary (HIGH confidence)
- Project templates (read in full this session): `src/phaze/templates/pipeline/partials/dag_canvas.html` (the canvas, the Rescan anchor `:202-203`, `NODE_LAYOUT` `:31-41`, `enqueue_button` macro `:98-112`, the `dag.items()` in-place seeds `:161-163`, the `<ol>` text equivalent `:339-360`), `stats_bar.html` (the OOB poll mechanism + `dag.items()` loop `:66-68`, the clobber-safety comment `:27-32`), `dashboard.html` (the single 5s poll `:21`, includes), `base.html` (`$store.pipeline` literal `:106-117`, HTMX/Alpine/Tailwind pins `:28/34/40`), `trigger_scan_card.html` (`#trigger-scan-heading` `:12`, `hx-post="/pipeline/scans"` `:16`), `trigger_response.html`
- Project routers/services: `src/phaze/routers/pipeline.py` (`dashboard` `:269`, `pipeline_stats_partial` `:324`, `_build_dag_context` `:106-148`, the HTMX trigger endpoints), `src/phaze/services/pipeline.py` (`get_pipeline_stats` `:50`, `get_queue_activity` degrade pattern `:63-120`, `get_stage_progress` + `_safe_count` `:141-270`)
- Project tests: `tests/test_dag_canvas_render.py` (render/topology/gating/integration layers; the exact-4-hx-post test `:240-251`, the overlap test `:146-169`), `tests/test_pipeline_dag_context.py` (store-literal + `_build_dag_context` + OOB-seed + degrade tests)
- Phase 37 artifacts: `37-RESEARCH.md` (endpoint contracts, priority semantics, Pitfall 1 the count-vanishes interaction), `37-04-PLAN.md` (the exact endpoint shapes, JSON return `{stage, priority, paused}`, delta/step ±10/clamp 0–100)
- `.planning/ROADMAP.md:304-323` (Phase 38 scope), `.planning/STATE.md` (Phases 36/37/38 accumulated context), CLAUDE.md (stack + constraints), auto-memory `project_stage_pause_priority_design`
- HTMX docs via Context7 (`/bigskysoftware/htmx`): `hx-swap-oob` (multi-element OOB), `hx-trigger` polling (`every`), `hx-disabled-elt` (disable during request) — confirms the 2.x attribute semantics this phase relies on

### Secondary (MEDIUM confidence)
- Alpine.js `@htmx:after-request` + `$event.detail.xhr.response` JSON-parse pattern — modeled on the existing `enqueue_button` `@htmx:after-request` error handler (`dag_canvas.html:99-111`); the JSON-parse extension is a straightforward application but should be proven by a behavior test (A4).

### Tertiary (LOW confidence)
- None — every load-bearing claim is read directly from this repo or confirmed against HTMX docs.

## Metadata

**Confidence breakdown:**
- DAG poll / store / OOB clobber-safety substrate: HIGH — read directly from the templates + tests
- Rescan anchor removal safety: HIGH — the anchor is a plain scroll link; scan lives in the Trigger Scan card
- `_build_dag_context` extension + degrade pattern: HIGH — mirrors existing `get_queue_activity`/`_safe_count`
- HTMX 2.x `hx-swap=none` / `hx-disabled-elt` / OOB semantics: HIGH — confirmed via Context7
- The JSON-vs-partial control-update decision + Alpine `:hx-post` binding: MEDIUM — recommended path is sound but the bound-attribute detail (A4) needs a behavior test; flagged as Open Q1/Q4
- Layout recompute magnitude: MEDIUM — direction is certain (chips get taller, gutters must grow), exact new y-values are a render-and-measure task

**Research date:** 2026-06-12
**Valid until:** 2026-07-12 (stable; re-verify after Phase 37 executes — confirm its endpoints still return JSON `{stage, priority, paused}` and the `PipelineStageControl` model path)
