# Pitfalls Research

**Domain:** Rewriting an existing server-rendered tabbed HTMX/Jinja admin UI (~10 tabs, 105 templates / 94 partials) into a DAG-centric three-column HTMX "hybrid console" — then retiring the old UI. phaze v7.0, phases 57–62.
**Researched:** 2026-06-29
**Confidence:** HIGH (grounded in the actual `src/phaze/templates`, `src/phaze/routers`, and `base.html`; HTMX history/OOB behavior verified against current HTMX docs via Context7)

> Scope note: this is **not** generic web-pitfall research. Every pitfall below is anchored to something that already exists in this codebase and will break (or silently regress) when the tab IA is collapsed into a rail-driven stage-swapping shell. File citations are load-bearing — they are where the existing pattern lives that the rewrite must preserve or replace.

---

## The existing UI in one paragraph (so the pitfalls make sense)

Today there is **no bare-`/` handler** — every page is a full-page template (`extends "base.html"`, 10 of them) served under a prefixed router: `/proposals/` (default-ish), `/pipeline/`, `/search/`, `/duplicates/`, `/tags/`, `/cue/`, `/tracklists/`, `/preview/`, `/audit/`, `/admin/agents`. `base.html` owns the brand (Jura/Inter, blue accent, wave SVG, `phaze-bg`/`phaze-panel` tokens), a flat `<nav>` of ~10 `<a href>` tabs highlighted by `current_page`, a hand-rolled **theme store** (`auto/dark/light` with a documented "Alpine doesn't process `:class` on `<html>`" landmine), and two global Alpine stores (`theme`, `pipeline`). The pipeline dashboard is the most sophisticated page and the template the rewrite most resembles: a single `#pipeline-stats` div polls `/pipeline/stats` `every 5s` with `hx-swap="innerHTML"`, and that one poll response carries **~10 `hx-swap-oob` fragments** to refresh cards that live *outside* the polled div, plus a `dag.items()` loop of hidden `x-init="$store.pipeline.<key> = N"` store-write paragraphs — all gated behind an `oob_counts` flag to avoid duplicate-id collisions at first paint. Bulk approval (`proposals/partials/bulk_actions.html`) serializes a client-side Alpine `selectedRows` Set into hidden `proposal_ids` inputs and PATCHes `/proposals/bulk`, which blindly applies the status change to whatever ids arrive. This is the machinery v7.0 must generalize to a rail.

---

## Critical Pitfalls

### Pitfall 1: The rail swap clobbers the shell — wrong `hx-target`/`hx-swap` eats the header, rail, or per-file pane

**What goes wrong:**
A rail node is wired like the existing tabs (`hx-get="/stage/analyze"`) but with a target that is too broad (`hx-target="body"`, `hx-target="#shell"`, or an implicit `outerHTML` on a wrapping element). The stage response then replaces the three-column shell itself — the rail, the ⌘K header, and the per-file pane vanish on the first click, leaving only the workspace fragment. The inverse also happens: the stage endpoint returns a **full** `extends "base.html"` page, so HTMX injects a second `<nav>`, a second theme toggle, and a second `$store` init into the middle column.

**Why it happens:**
The current app has *no* sub-page swapping — every nav item is a hard `<a href>` full-page load (`base.html` lines 191–239). There is no existing "swap just the center" pattern to copy; the closest analog is `#pipeline-stats` which swaps `innerHTML` of a *leaf* div, not a layout region. Developers reach for the pattern they know (full-page templates) and a blunt target.

**How to avoid:**
- Give the center column a single stable id (e.g. `#stage-workspace`) and make **every** rail node `hx-get="/console/stage/<name>" hx-target="#stage-workspace" hx-swap="innerHTML"`. Never target a parent of the rail/header.
- Stage endpoints return **fragments only** (no `extends "base.html"`). Establish a `_console/_stage_base.html` partial-include convention distinct from the full-page `base.html`, exactly as the codebase already separates `proposals/list.html` (full) from `proposals/partials/*.html` (fragments).
- The shell (`base.html` successor) renders once on `GET /`; the right per-file pane updates via an **OOB** fragment in the stage response (`hx-swap-oob` targeting `#file-pane`), never by widening the main target — the dashboard already proves this OOB-alongside-primary pattern (`proposals/partials/approve_response.html` swaps the row primary + `#stats-bar` OOB + toast OOB).

**Warning signs:**
The rail/header flicker or disappear on a stage click; "Skip to…" link count doubles; two theme toggles render; `$store.pipeline` resets to zeros mid-session (a second store init ran); browser devtools shows nested `<main>`/`<nav>`.

**Phase to address:** **57** (Shell & rail) — lock the `#stage-workspace` target contract and the fragment-vs-full-page template split before any stage is built.

---

### Pitfall 2: Lost Alpine state and dead event handlers after a swap (`x-data` re-init, keyboard nav, `selectedRows`)

**What goes wrong:**
The proposals table keyboard navigation and multi-select live entirely in an Alpine `x-data="proposalTable()"` component on `#proposal-list-container` with `@keydown.window`, a `focusedRow` cursor, and a `selectedRows` Set (`proposals/list.html` lines 17–20, 49–105). When that table becomes a *swapped* stage workspace, the new content's `x-data` is a fresh component instance — selection, focus cursor, and any open inline-edit state reset to empty on **every** swap and every 5s poll that re-renders the subtree. Worse, handlers bound with plain `<script>` or `htmx.onLoad` against ids that no longer exist after a swap silently stop firing.

**Why it happens:**
HTMX swaps raw HTML; Alpine *re-initializes* `x-data` on inserted nodes (good) but does **not** preserve the prior instance's runtime state (bad if you assumed persistence). The current code already had to defend this — note `@htmx:after-swap.window="$nextTick(() => { rowIds = …; focusedRow = -1; selectedRows = new Set(); })"` in `proposals/list.html`: it *deliberately resets* selection after a swap because state can't survive it. In a rail console, swaps are constant, so this reset-on-swap becomes a UX regression (operator loses their multi-select when a poll ticks).

**How to avoid:**
- Hoist any state that must outlive a swap into an **Alpine global store** (the codebase's established pattern — `$store.theme`, `$store.pipeline` in `base.html`), not into the swapped component's `x-data`. Selection-in-progress, the active stage, ⌘K open/closed, and the open per-file id should be store-level.
- Keep volatile, intentionally-ephemeral state (a row's inline edit buffer) in the swapped `x-data` — and **never** let the 5s poll re-render the subtree that holds an in-flight interaction. The dashboard already enforces this: the poll swaps `#pipeline-stats` only and pushes counts to `#pipeline-stages` buttons via **OOB hidden `x-init` store writes** so "the button subtree is never the swap target" (`stats_bar.html` lines 27–49). Replicate that split for every workspace with interactive rows.
- Prefer `@event.window` Alpine listeners (survive because they bind to `window`) over element-scoped `htmx.onLoad` re-binding.

**Warning signs:**
Multi-select clears when the live poll ticks; keyboard row-nav stops after the first stage swap; inline edit fields blank out under load; `x-cloak` flashes on every swap.

**Phase to address:** **57** (state-survival store contract) + revisited in **60** (Review & Apply multi-select must survive the live diff poll).

---

### Pitfall 3: Out-of-band swap collisions when one stage response updates rail counts + header strip + workspace at once

**What goes wrong:**
A rail console wants one click/poll to update three regions: the workspace (primary), the rail's live per-stage counts, and the header agent-status strip. Done naively with multiple `hx-swap-oob` fragments, two failure modes appear that this codebase has *already hit*: (a) OOB fragments emitted on the **initial full-page render** (not just the poll response) produce **duplicate ids** in the DOM and stray visible nodes; (b) an OOB fragment's id doesn't exactly match the live element, so the swap silently no-ops and a count goes stale.

**Why it happens:**
`hx-swap-oob` is only honored *during an HTMX swap* — at first paint it renders as ordinary (duplicate) markup. The dashboard guards this with an explicit `{% if oob_counts %}` gate and a documented "same-id contract" between the in-place seed and the OOB twin (`stats_bar.html` lines 36–45, 65–68; the `dag-seed-<key>` id convention). A new rail built without internalizing that gate will regress it.

**How to avoid:**
- Reuse the exact `oob_counts`-style flag: stage/poll endpoints set it `True`, the full-page shell include omits it, so OOB blocks fire *only* on swap responses. This is a proven phaze idiom — copy it, don't reinvent.
- Maintain a **single documented id registry** for OOB targets (rail counts `#rail-count-<stage>`, header dots `#agent-status-strip`, file pane `#file-pane`). The codebase already relies on id-contract discipline (`#straggler-failed-card`, `#admission-state-card`, etc., each re-pushed `{% with oob = True %}`); a console multiplies these, so the registry must be explicit or drift is guaranteed.
- Push **rail counts via hidden `x-init` store writes** (the `$store.pipeline` pattern) rather than re-rendering rail DOM, so a count refresh never clobbers rail focus/hover or an in-flight nav.

**Warning signs:**
Duplicate-id warnings in devtools; a rail count freezes while the workspace updates; a stray "12 files ready" text node appears at page top on first load (the classic un-gated-OOB symptom); the header status dots stop updating after the first poll.

**Phase to address:** **57** (rail count + header strip OOB contract) — and every later phase must conform to the id registry.

---

### Pitfall 4: Poll storms — many concurrent `hx-trigger="every Ns"` loops hammering endpoints, including in a backgrounded tab

**What goes wrong:**
The repo already has **15 `every Ns` polling triggers across 16 templates** (`base.html`, `pipeline/dashboard.html`, `admin/agents.html`, `tracklists/.../scan_progress.html`, and ~10 pipeline cards). A three-column console that shows the rail (per-stage counts), a workspace (lane cards + file queue), and a per-file pane *simultaneously* can easily mount **3–5 independent 5s polls at once** — and because each is its own request, they fan out to separate DB-querying endpoints (`/pipeline/stats` alone calls ~12 service functions per tick: `pipeline.py` lines 549–594). On ~200K files these become non-trivial; multiplied across regions and never paused when the tab is backgrounded, they are a self-inflicted load source.

**Why it happens:**
HTMX makes per-element polling trivial (`hx-trigger="every 5s"`), so each new live region "just adds a poll." There is no shared scheduler. The existing dashboard already consolidates *deliberately* — one `#pipeline-stats` poll fans out to ~10 OOB updates rather than 10 polls — but a rail rewrite that adds the rail and file pane as separate pollers loses that consolidation.

**How to avoid:**
- **One console-level poll.** Keep the single-poll/fan-out-via-OOB architecture the dashboard already uses (`stats_bar.html`): a single `every 5s` request returns the workspace delta **plus** OOB rail counts **plus** OOB header dots **plus** OOB file-pane facts. Do not give the rail, workspace, and pane independent `every Ns` triggers.
- Add a load-shedding visibility guard so polls don't fire on a hidden tab — an `every Ns` trigger keeps firing regardless of tab focus unless gated (verified current HTMX behavior). Gate via a `visibilitychange` listener that pauses/resumes (e.g. add/remove the polling attribute, or only `htmx.trigger` when `document.visibilityState === 'visible'`).
- Verify each poll endpoint stays **degrade-safe and cheap**: phaze's service layer already owns "never-500, return 0 on DB error" degrade (`get_queue_activity`, `get_stage_busy_counts`, etc., per `pipeline.py` comments) — keep new lane/rail counts on the same single poll context so they inherit that contract instead of adding fresh failure surfaces.

**Warning signs:**
Network tab shows N requests every 5s where N = number of live regions; Postgres connection pool pressure rises when the console is open; CPU on the app server climbs with the console idle in a background tab; `/pipeline/stats`-equivalent p95 latency grows with file count.

**Phase to address:** **58** (Enrich + Analyze workspaces own WORK-05 live refresh + the three lane cards — the densest live region; the consolidation decision lives here). Re-checked in **61** (per-file pane must ride the same poll, not add its own).

---

### Pitfall 5: URL/history breakage — deep-linking a stage, back/forward landing on a bare fragment, refresh on a swapped state, and SHELL-05 redirect loops

**What goes wrong:**
Four linked failures: (1) Clicking a rail node with `hx-push-url="true"` snapshots the *current DOM* into history (verified: "the current DOM is snapshotted and stored for restoration"). On **back**, HTMX restores that snapshot — which for an HTMX-swapped state may be a half-built DOM whose Alpine `x-data` does not re-run, so the restored page is visually right but **dead** (no reactivity). (2) A user who **refreshes** while deep-linked to `/?stage=analyze` hits a server route that must rebuild the *whole shell with Analyze selected* — if `GET /` ignores the stage param and always renders Analyze-default, the deep link silently resets. (3) A bookmarked **bare fragment** URL (the stage endpoint `/console/stage/analyze`) opened directly returns a headerless fragment (no shell). (4) SHELL-05 redirects (`/proposals` → shell Rename queue, etc.) collide with FastAPI's default `redirect_slashes=True`: `/proposals` already 307s to `/proposals/`, so a naive redirect rule can bounce `/proposals` → `/` → (stage) → … or break the existing trailing-slash bookmark.

**Why it happens:**
The current app never uses `hx-push-url` or history at all (hard `<a>` navigation handles it natively), so there is zero existing history-correctness code to lean on. And the legacy routes are *prefixed with trailing-slash semantics* (`prefix="/proposals"` + `get("/")` ⇒ canonical `/proposals/`), which the redesign's redirect layer must respect.

**How to avoid:**
- Make **stage selection a server-resolvable URL**: `GET /` accepts the selected stage (path `/` = Analyze default, plus `/stage/<name>` or `?stage=<name>`) and renders the **full shell** with that stage's workspace inlined. The rail's `hx-get` uses `hx-push-url` pointed at that *same* canonical URL — so back/forward/refresh/deep-link all resolve to a full-shell render, not a bare fragment.
- Serve **two shapes from one stage route** keyed on the `HX-Request` header: HTMX request → fragment (for the swap); full navigation/refresh → full shell with the stage inlined. This is the standard HTMX deep-link pattern and avoids "bookmark returns a headerless fragment."
- For SHELL-05, write **explicit redirect routes** for each legacy path (`/proposals/`, `/pipeline/`, `/search/`, `/duplicates/`, `/tags/`, `/cue/`, `/tracklists/`, `/preview/`, `/audit/`, `/admin/agents`) → the canonical shell URL, and **add a redirect-loop test** asserting each legacy path resolves to a 200 shell in ≤1 hop and never targets a path that itself redirects. Account for `redirect_slashes`: redirect the *canonical* (trailing-slash) form the routers actually expose.
- Handle `htmx:historyRestore`/`htmx:historyCacheMiss` (verified events) to re-trigger the live poll and re-init Alpine on a restored snapshot, so a back-navigated console isn't a dead DOM.

**Warning signs:**
Back button shows the right stage but buttons/counts are frozen (Alpine didn't re-init); refresh on a deep link drops you to Analyze; pasting a stage URL into a new tab returns un-styled fragment HTML; `curl -sI /proposals` shows a redirect chain >1 hop; existing `/proposals/` bookmarks 404 or loop.

**Phase to address:** **57** (SHELL-01 `/` route, SHELL-02 push-url, SHELL-05 legacy redirects — all live here; the redirect-loop test is a phase-57 must-have).

---

### Pitfall 6: Accessibility regressions baked in during the swap rewrite (focus management, ⌘K focus trap, rail keyboard nav, skip link, ARIA on the DAG)

**What goes wrong:**
HTMX swaps replace DOM without moving focus, so after a rail click focus stays on the (now-replaced) trigger or resets to `<body>` — keyboard users lose their place. The ⌘K palette (RECORD-02) opened/closed without an explicit focus trap + focus-restore traps or strands focus. The today-existing skip link is **wrong for a console**: `base.html` hard-codes `href="#proposals-table"` (overridden per page via a `{% block skip_link %}`), which points at a target that won't exist in most stages. ARIA on the DAG rail (CUT-01 requires it) is absent — today only the Agents nav link has `aria-current="page"` (a documented partial retrofit, `base.html` lines 228–239); the other 9 links never got it. A from-scratch rail with no `role`/`aria-current`/`aria-selected` ships *worse* a11y than the tabs it replaces.

**Why it happens:**
a11y is the named CUT-01 deliverable parked in the **final** phase (62), so the swap mechanics get built in 57–61 with no focus/ARIA discipline and 62 inherits a mountain of retrofits. Focus management is invisible in mouse testing, so it's not noticed until a keyboard pass.

**How to avoid:**
- Bake focus discipline into the **phase-57 swap contract**, not phase 62: after a stage swap, move focus to the workspace heading (`hx-on::after-swap` focusing an `[tabindex="-1"]` stage `<h1>`), and mark the active rail node `aria-current="page"`/`aria-selected="true"`. Treat "every swap manages focus" as a phase-57 acceptance criterion.
- Build ⌘K with a real focus trap from day one (Phase 61): on open, save `document.activeElement`, trap Tab within the palette, `Esc` closes and **restores** focus to the saved element. Alpine `x-trap` (or a tiny hand-rolled trap) is the lightweight fit for the no-build stack.
- Replace the hard-coded `#proposals-table` skip link with a console-correct `#stage-workspace` target in the shell's `{% block skip_link %}` default.
- Give the rail proper semantics: `role="navigation"` / a `role="list"` of stages with `aria-current` on the active one, `aria-label`s on the lane cards, and `focus-visible` rings preserved (Tailwind `focus-visible:` — verify they aren't stripped by the restyle).
- Run an **axe/keyboard pass after EACH phase** (57–61), not only in 62 — a per-phase a11y smoke check prevents the debt pile-up.

**Warning signs:**
Tabbing after a rail click jumps to `<body>` or the page top; ⌘K closes but focus is lost to the document; Esc doesn't restore focus; screen-reader announces nothing on stage change; the skip link jumps to a missing anchor; no visible focus ring on rail items.

**Phase to address:** Distributed — focus/ARIA contract **57**, ⌘K trap **61**, *full* CUT-01 audit + parity sign-off **62**. (Anti-pattern: deferring *all* of it to 62.)

---

### Pitfall 7: Cutover removes templates/routers/partials that something still references (CUT-02 dead-code deletion)

**What goes wrong:**
With **105 templates / 94 partials** and prefixed routers, "delete the old tab" in phase 62 removes a `*/list.html` or a partial that is still `{% include %}`d by a kept template, still rendered by a still-mounted endpoint, or still targeted by an `hx-get` string in a partial that survived. Jinja include errors throw at **request time**, not at deploy — so a missed reference 500s a live stage. Routers are registered by hand in `main.py` (lines 185–227); removing a router file without removing its `include_router` line breaks import; removing the line but leaving a template that `hx-post`s to its URL yields a 404 on click.

**Why it happens:**
The reuse strategy ("new templates over existing routers/services") means the new shell *keeps* most routers and *replaces* templates — so which templates/partials are truly orphaned is non-obvious. Includes and `hx-*` URLs are string references invisible to a Python import graph; a grep-free deletion misses them.

**How to avoid:**
- **Staged removal, reference-proven.** Before deleting any template/partial/router, grep the whole tree for: its filename in `{% include %}`/`{% extends %}`, its route path in `hx-get`/`hx-post`/`hx-patch`/`href`/`url_for`, and its symbol in `main.py`. Only delete when all three are empty. Encode this as a checklist in the phase-62 plan.
- Add a **dead-template test**: a unit test that walks `templates/` and asserts every non-fragment template is reachable from a mounted route, and every `{% include %}` target exists on disk (Jinja's `meta.find_referenced_templates` makes this mechanical). This catches an orphaned-include 500 before runtime.
- Delete **router-then-template in lockstep**, each in its own small PR (the repo's "one PR per feature, worktree-per-feature" rule, per CLAUDE.md), so a broken reference is bisectable and revertable.
- Keep the **SHELL-05 redirects** until after the legacy templates are gone — the redirect routes are the safety net proving no inbound bookmark hits a deleted page.

**Warning signs:**
A stage 500s with `TemplateNotFound` only when a specific row type renders; `grep -rn "old_partial.html" src` returns hits after you "removed" it; `main.py` import fails on boot; an `hx-post` button returns 404; coverage on a "removed" router file is suspiciously still >0.

**Phase to address:** **62** (CUT-02) — but seed the dead-template test in **57** so it's green-then-watched across the whole migration.

---

### Pitfall 8: before→after diff approval correctness — stale data after approve, bulk "approve all high-confidence" acting on changed rows, reversibility gaps (REVIEW)

**What goes wrong:**
Today's bulk approve serializes a **client-side** `selectedRows` Set into hidden `proposal_ids` inputs and PATCHes `/proposals/bulk`, which calls `bulk_update_status(uuids, APPROVED)` with **no re-validation** that those rows are still pending or still high-confidence (`bulk_actions.html`; `proposals.py` `bulk_action` lines 304–331). In a live-polling console where the diff list refreshes every 5s, the operator can approve a stale set: a row that changed confidence, was re-proposed, or was already executed by another action between render and submit. The unified REVIEW gate widens this from one queue (proposals) to **four** (Rename/Tag/Move/Dedupe), each with a "approve all high-confidence" bulk button — so a single stale-bulk bug now mis-applies renames, tag writes, and **file moves**. REVIEW-05 requires every applied change be audited and reversible; if the new diff UI bypasses the existing `ExecutionLog`/audit write or the copy-verify-delete protocol, reversibility silently breaks.

**Why it happens:**
The current bulk action was built for a *static* page (you load proposals, select, submit — little time passes). The console makes the list **live**, dramatically widening the render→submit staleness window, while the "approve all high-confidence" affordance encourages large, unreviewed batch actions. The threshold is also a fixed value (REVIEW-06 defers per-stage config), so "high-confidence" is evaluated *somewhere* — if client-side off possibly-stale rendered confidence, it acts on what the user *saw*, not current truth.

**How to avoid:**
- **Server is the source of truth for bulk scope.** "Approve all high-confidence" should send a *predicate* (action + threshold), not a client-built id list — the server re-queries pending rows above the threshold *at submit time* and applies to that fresh set, returning the actual count acted on. This eliminates the stale-id class entirely. (Per-file Approve/Edit/Skip can still send a single id, but the endpoint must re-check the row is still pending and 409/no-op if not.)
- **Optimistic-concurrency guard on per-file approve:** include the row's last-known state/version; the endpoint approves only if unchanged, else returns the refreshed diff fragment so the operator re-sees current truth (the dashboard already returns refreshed fragments — `approve_response.html` re-renders the row + OOB stats).
- **Preserve the audit + reversibility seam unchanged.** REVIEW is an IA rewrite over *existing* execution/tags/cue/duplicates routers (the milestone's explicit "no backend behavior change") — the new templates must POST to the **same** endpoints that already write `ExecutionLog` and honor copy-verify-delete; do not add a new direct-apply path that skips them. Add a test asserting every REVIEW apply action produces an audit row.
- **Don't let the 5s poll re-render an in-flight selection** (ties to Pitfall 2/4): the diff list's live refresh must OOB-update *counts* without clobbering the operator's checked rows mid-review.

**Warning signs:**
"Approve all high-confidence" count returned ≠ what the operator saw; a row that was just executed gets re-approved; an approved move has no `ExecutionLog`/audit entry; undo fails to find a reversal record; selection clears or shifts when the diff list polls; bulk acted on a row whose confidence dropped below threshold after render.

**Phase to address:** **60** (Review & Apply — REVIEW-01/02/03/05). The server-predicate bulk + audit-parity test are phase-60 must-haves.

---

### Pitfall 9: Theme-toggle and brand regressions during the shell rewrite (dark `phaze-bg`, light toggle, Jura/blue/wave)

**What goes wrong:**
`base.html` carries a **fragile, already-bitten** theme mechanism: a pre-Alpine inline script applies `.dark` to `<html>` to prevent FOUC, an OS `prefers-color-scheme` listener for auto mode, and an `$store.theme` whose `set()` calls `_applyTheme` directly — *because* a prior version that bound `:class` on `<html>` was "silently inert" (documented in the lines 42–53 comment). The `@theme` block defines the brand palette (`--color-blue-*`, `--color-phaze-bg/panel/border`) and Jura/Inter are loaded by `<link>`. A shell rewrite that regenerates `base.html` from the prototype can: re-introduce the inert `:class`-on-`<html>` bug, drop the pre-flash script (FOUC on every load), lose the `@custom-variant dark` registration (so all `dark:` utilities silently stop working), or hard-code Tailwind-default blue (losing the phaze accent). The Tailwind build is **self-hosted, not CDN** (lines 21–28, with a deliberate SRI rationale) — swapping to a CDN URL during the rewrite reintroduces the exact SRI-divergence bug that vendoring fixed.

**Why it happens:**
The prototype (`prototype.html`) is a standalone artifact that almost certainly inlines its own theme handling; "port the prototype" invites replacing the hard-won `base.html` head wholesale, discarding the embedded fixes and the vendored-Tailwind decision.

**How to avoid:**
- **Preserve `base.html`'s `<head>` as the source of truth** — port the prototype's *markup* into the existing head/theme scaffolding, do not replace the scaffolding. Keep `_applyTheme`, the pre-flash IIFE, the OS listener, `@custom-variant dark`, the `@theme` token block, the Jura/Inter `<link>`, and the **self-hosted** `/static/vendor/tailwindcss-browser-4.3.0.min.js` script (not CDN).
- Add a **theme smoke test/checklist**: load `/`, assert `.dark` is present when `localStorage['phaze-theme']='dark'`, assert `dark:bg-phaze-bg` actually paints, assert no FOUC (the IIFE runs before first paint), and cycle auto/dark/light.
- Keep brand tokens (`phaze-bg`/`phaze-panel`/blue accent) and the wave SVG logo verbatim from `base.html` (SHELL-04) — diff the new shell's computed colors against the old to confirm "evolve, don't reskin."

**Warning signs:**
A flash of light theme on load (pre-flash script lost); the toggle cycles `mode` but the page doesn't change (the inert `:class`-on-`<html>` regression is back); `dark:` utilities do nothing (custom-variant lost); accent renders as Tailwind default blue; SRI console error on the Tailwind script (CDN regression).

**Phase to address:** **57** (SHELL-04 — theme + brand preservation is a shell-phase acceptance gate).

---

### Pitfall 10: Template/partial duplication and drift between full-page and fragment responses

**What goes wrong:**
HTMX endpoints must return a **fragment** to a swap request and a **full page** to a direct navigation/refresh (see Pitfall 5). The repo's idiom is "full-page `*/list.html` includes the same `*/partials/*` the HTMX endpoint returns" — e.g. `proposals/list.html` includes `proposal_table.html` + `bulk_actions.html`, and the bulk endpoint returns `approve_response.html` which re-includes the row + stats partials. When a console adds a stage, it's easy to write the workspace markup **twice** (once inline in the shell for the full render, once in the fragment for the swap), and they drift — a column added to the fragment never appears on refresh, or vice versa.

**Why it happens:**
The "two shapes from one route" requirement (Pitfall 5) naturally tempts two code paths. With 94 partials already, the include graph is deep and a new contributor copies markup rather than extracting a shared partial.

**How to avoid:**
- **One partial, two callers.** Every stage workspace is a single `_console/stages/<name>.html` partial; the full-shell route `{% include %}`s it into the shell, and the HTMX stage route returns it bare. Neither path hand-writes the workspace markup. This is exactly how `proposals/list.html` and the proposals HTMX endpoints already share `proposals/partials/*`.
- A drift test: render each stage via the full route and via the HTMX route and assert the workspace subtree HTML is identical (or that the fragment is a literal substring of the full page).
- Watch for **orphaned partials** created during iteration (a `_v2` partial left behind) — fold this into the Pitfall-7 dead-template test.

**Warning signs:**
A field shows after a swap but vanishes on refresh (or vice versa); two near-identical partials differ by one column; `grep` finds the same table markup in both a `list.html` and a fragment.

**Phase to address:** **57** (establish the one-partial-two-callers convention) and enforced through **58–61** as stages are added.

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| Give the rail, workspace, and file-pane each their own `every 5s` poll | Trivial to wire; each region "just works" | 3–5× request fan-out on a 200K-file DB; load even when tab is backgrounded (Pitfall 4) | Never — keep the single-poll/OOB-fanout architecture the dashboard already proves |
| Stage endpoints return full `extends base.html` pages | Reuse existing full-page templates as-is | Nested shells, duplicate `$store`/nav/theme (Pitfall 1); no clean swap | Never for swap targets; the full-page shape is only for direct-nav/refresh |
| Client-built id list for "approve all high-confidence" | Reuses the existing `selectedRows`→hidden-inputs bulk pattern | Acts on stale rows under live polling; mis-applies moves/tags (Pitfall 8) | Never for bulk — send a server-evaluated predicate |
| Defer ALL of CUT-01 a11y to phase 62 | Faster stage builds in 57–61 | Mountain of focus/ARIA retrofits across every swap; likely ships worse than the tabs | Only the *final audit* belongs in 62; the focus/ARIA contract must be in 57 |
| Port the prototype's `<head>` wholesale | One copy-paste shell | Re-introduces the inert-`:class` theme bug, FOUC, lost `dark:` variant, CDN/SRI regression (Pitfall 9) | Never — port markup into the existing head scaffolding |
| Hand-write workspace markup in both the shell render and the fragment | Quick to see it working both ways | Full-page/fragment drift (Pitfall 10) | Never — one partial, two callers |
| Delete a legacy router/template by "it looks unused" | Removes clutter fast | Runtime `TemplateNotFound`/404 from a surviving include or `hx-*` URL (Pitfall 7) | Never without the three-way grep + dead-template test |

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| HTMX history (`hx-push-url`) | Assuming back/forward re-fetches; it restores a **DOM snapshot** (verified), leaving Alpine un-initialized | Point `hx-push-url` at a server-resolvable canonical URL; handle `htmx:historyRestore` to re-init Alpine + re-trigger the poll (Pitfall 5) |
| FastAPI `redirect_slashes` | Writing SHELL-05 redirects against `/proposals` while the router canonical is `/proposals/`, creating a 2-hop chain or loop | Redirect the canonical trailing-slash form the routers expose; assert ≤1 hop in a test |
| HTMX `hx-swap-oob` | Emitting OOB fragments on the initial full-page render → duplicate ids + stray nodes | Gate OOB blocks behind an `oob_counts`-style flag set only on swap responses (the existing `stats_bar.html` idiom) |
| Alpine `x-data` + HTMX swap | Expecting component state (selection/focus) to survive a swap | Hoist cross-swap state to `$store`; keep only ephemeral state in `x-data` (Pitfall 2) |
| Alpine `:class` on `<html>` | Binding theme `:class` on `<html>` — Alpine scans from `<body>` down, so it's silently inert (documented in `base.html`) | Drive `.dark` via `classList` from one `_applyTheme` function (preserve the existing fix) |
| Self-hosted Tailwind browser build | Swapping the vendored `/static/vendor/...min.js` for a CDN URL with SRI | Keep the vendored audited build; CDN minification diverges per-edge and breaks SRI (documented rationale) |
| Existing execution/tags/cue routers (REVIEW) | Adding a new direct-apply path from the diff UI that skips `ExecutionLog`/copy-verify-delete | POST to the same existing endpoints; assert an audit row per apply (REVIEW-05) |

## Performance Traps

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| Per-region polling fan-out | N requests/5s where N = live regions; rising PG pool pressure | Single console poll + OOB fan-out (Pitfall 4) | Noticeable now at ~200K files; worse with the 3-column always-live layout |
| Polls keep firing in a backgrounded tab | App-server CPU stays high with the console idle in another tab | Gate `every Ns` on `document.visibilityState` | Any long-lived admin session (this is a single-user tool left open) |
| Stage workspace re-renders the whole file queue every poll | Large innerHTML swaps; lost scroll/selection; jank | Poll updates counts via OOB store-writes; only swap rows when they actually change | Queues with thousands of pending rows (proposals/move on a big import) |
| `/pipeline/stats`-style endpoint grows more service calls per tick | p95 latency creeps as each phase adds a count | Keep new lane/rail counts on the one degrade-safe poll context; don't add fresh endpoints | As phases 58–61 each add "just one more live count" |

## Security Mistakes

| Mistake | Risk | Prevention |
|---------|------|------------|
| Trusting client-submitted bulk id lists for destructive applies (move/tag-write) | Operator approves a stale/changed set; irreversible file moves on wrong rows | Server re-evaluates the predicate at submit time; per-file approve re-checks state (Pitfall 8) |
| Removing SHELL-05 redirects before legacy templates are gone | Inbound bookmarks 404; or a deleted page leaks a stack trace | Keep redirects until cutover proven; never expose raw 500s (the codebase's "never raw 500" discipline) |
| ⌘K command palette running "quick commands" (scan, etc.) without the same guards as the buttons | A command bypasses an enqueue guard (e.g. the Phase-30 default-queue / no-active-agent guard) | ⌘K commands must funnel through the same router endpoints + `enqueue_router` guards, not a new path |

> Note: this is a single-user private-LAN tool (no public access, no multi-tenant auth) — so classic web-auth pitfalls are out of scope; the real "security" surface here is **destructive-action correctness** (moves/tag-writes are hard to reverse) and **not bypassing existing enqueue guards**.

## UX Pitfalls

| Pitfall | User Impact | Better Approach |
|---------|-------------|-----------------|
| Live poll clears the operator's in-progress multi-select | Loses a 50-row selection mid-review; rage | OOB-update counts only; never re-render the selection subtree (Pitfall 2/4/8) |
| Stage swap doesn't move focus | Keyboard user is stranded on a replaced trigger | Focus the stage heading after swap (Pitfall 6) |
| ⌘K doesn't restore focus on close | User loses their place in the rail/workspace | Save+restore `activeElement`; Esc closes and restores (Pitfall 6) |
| Rail counts go stale while workspace updates | Operator distrusts the whole console | Single poll updates rail + workspace together via OOB (Pitfall 3/4) |
| k8s lane shown as perpetually-DEAD agent | Operator thinks burst is broken | Model k8s as ephemeral Job-based identity (RECORD-03, carries v6.0 KDEPLOY-04 intent) |
| FOUC / theme flash on every navigation | Looks broken/cheap — the exact "v1-ish" complaint the redesign exists to kill | Preserve the pre-flash IIFE + `_applyTheme` (Pitfall 9) |

## "Looks Done But Isn't" Checklist

- [ ] **Rail swap:** Looks right on click — verify **refresh** on a deep-linked stage rebuilds the full shell with that stage selected (not Analyze-default), and **back/forward** restores a *live* (Alpine-initialized) DOM, not a frozen snapshot.
- [ ] **Legacy redirects (SHELL-05):** Each of `/proposals/ /pipeline/ /search/ /duplicates/ /tags/ /cue/ /tracklists/ /preview/ /audit/ /admin/agents` resolves to a 200 shell in ≤1 hop — `curl -sI` each, assert no loop.
- [ ] **Theme (SHELL-04):** dark/light/auto cycle works, `.dark` paints `phaze-bg`, no FOUC, accent is phaze-blue not Tailwind-blue, Tailwind script is the vendored file (not CDN).
- [ ] **Live polling:** Open the console, count requests/5s — verify it's **one**, not one-per-region; backgrounding the tab pauses/sheds the poll.
- [ ] **Bulk approve (REVIEW-02):** "Approve all high-confidence" returns a count derived from a **server** re-query at submit time; approving a row that changed since render no-ops/refreshes instead of mis-applying.
- [ ] **Reversibility (REVIEW-05):** every apply (rename/tag/move/dedupe) writes an `ExecutionLog`/audit row and is undoable — assert in a test, not by eye.
- [ ] **a11y (CUT-01):** keyboard-only pass of rail + ⌘K; focus moves on swap; Esc restores focus; skip link targets `#stage-workspace`; rail nodes carry `aria-current`; visible focus rings present.
- [ ] **Dead code (CUT-02):** grep proves no surviving `{% include %}`/`hx-*`/`main.py` reference to each removed template/router; dead-template test green.
- [ ] **Full/fragment parity:** each stage rendered via full route == via HTMX route (no drift).

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| Shell clobbered by over-broad swap target | LOW | Narrow `hx-target` to `#stage-workspace`; convert stage templates to fragments |
| Lost Alpine selection/focus on swap | LOW–MEDIUM | Hoist the lost state into `$store`; stop polling the interactive subtree |
| Poll storm in production | LOW | Collapse per-region polls into one console poll + OOB; add visibility gate |
| History/back returns a dead DOM | MEDIUM | Make `/` stage-resolvable; add `htmx:historyRestore` re-init; serve full-vs-fragment by `HX-Request` |
| Deleted template still referenced (runtime 500) | MEDIUM | Revert the deletion PR (one-PR-per-removal makes this clean); add the missing reference to the dead-template test; re-delete |
| Theme/brand regression | LOW | Restore `base.html`'s `<head>` scaffolding (it's in git history); re-run theme smoke test |
| Stale bulk approve mis-applied moves | HIGH | Use the existing audit log + undo/copy-verify-delete reversal to roll back; then switch bulk to server-predicate before re-enabling |

## Pitfall-to-Phase Mapping

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| 1 — Swap clobbers shell | 57 | Stage click leaves rail/header/pane intact; stage responses contain no `<nav>`/second `$store` |
| 2 — Lost Alpine state after swap | 57 (+60) | Selection/focus survive a poll tick; cross-swap state lives in `$store` |
| 3 — OOB collisions (rail + header + workspace) | 57 | No duplicate-id warnings; rail counts + header dots update on the same poll; OOB gated by `oob_counts`-style flag |
| 4 — Poll storms | 58 (+61) | One request/5s with the console open; poll pauses on hidden tab |
| 5 — URL/history + redirect loops | 57 | Deep-link refresh rebuilds full shell; back restores live DOM; each legacy path ≤1-hop to 200 |
| 6 — a11y regressions | 57 (focus/ARIA), 61 (⌘K trap), 62 (audit) | Per-phase keyboard/axe smoke; CUT-01 parity sign-off in 62 |
| 7 — Dead-code cutover | 62 (test seeded 57) | Three-way grep empty per removal; dead-template test green; boot succeeds |
| 8 — Diff/bulk approval correctness | 60 | Bulk count from server re-query; per-apply audit row asserted; stale per-file approve no-ops |
| 9 — Theme/brand regression | 57 | Theme smoke test; vendored Tailwind; phaze accent; no FOUC |
| 10 — Full/fragment drift | 57 (enforced 58–61) | Stage full-route HTML == HTMX-route HTML |

## Sources

- `src/phaze/templates/base.html` — theme store + documented `:class`-on-`<html>` inert bug, pre-flash `_applyTheme`, `@theme` brand tokens, `@custom-variant dark`, self-hosted Tailwind + SRI rationale, flat tab `<nav>`, skip-link block, `aria-current` partial-retrofit note (HIGH)
- `src/phaze/templates/pipeline/dashboard.html` + `pipeline/partials/stats_bar.html` — the single-poll/OOB-fanout architecture, `oob_counts` gate, `dag-seed-<key>` same-id contract, ~10 OOB cards (HIGH)
- `src/phaze/routers/pipeline.py` (lines 434–622) — `/pipeline/` full render vs `/pipeline/stats` partial; ~12 degrade-safe service calls per poll tick (HIGH)
- `src/phaze/templates/proposals/list.html` + `partials/bulk_actions.html` + `routers/proposals.py` `bulk_action` — `proposalTable()` `x-data`, `selectedRows` Set→hidden inputs, `@htmx:after-swap` selection reset, client-id bulk with no server re-validation (HIGH)
- `src/phaze/main.py` (lines 185–229) — hand-registered routers, no bare-`/` handler, router prefixes (`/proposals`, `/search`, `/duplicates`, `/tags`, `/cue`, `/tracklists`; `/preview/`, `/audit/`, `/admin/agents` path-based) (HIGH)
- Template inventory: 105 templates / 94 partials / 10 full-page (`extends base.html`); 15 `every Ns` polls across 16 templates (HIGH — direct counts)
- HTMX docs via Context7 (`/bigskysoftware/htmx`) — `hx-push-url` snapshots/restores the current DOM; `htmx:historyRestore`/`historyCacheMiss`; `hx-swap-oob` multi-target; combinable `hx-trigger` events; `every Ns` polling semantics (HIGH)
- `.planning/REQUIREMENTS.md` (v7.0, 25 reqs SHELL/WORK/IDENT/REVIEW/RECORD/CUT → phases 57–62) and `docs/superpowers/specs/2026-06-28-ui-redesign-dag-console-design.md` (locked design spine) (HIGH)

---
*Pitfalls research for: rewriting phaze's tabbed HTMX/Jinja admin UI into a DAG-centric hybrid console + retiring the legacy UI*
*Researched: 2026-06-29*
