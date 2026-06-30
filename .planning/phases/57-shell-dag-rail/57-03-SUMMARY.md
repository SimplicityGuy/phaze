---
phase: 57-shell-dag-rail
plan: 03
subsystem: ui
tags: [htmx, alpinejs, jinja2, shell, dag-rail, command-palette, status-strip, a11y, testing]

# Dependency graph
requires:
  - phase: 57-02
    provides: "shell.html (three-column frame, #stage-workspace swap target, $store.pipeline + theme machinery lifted, syncRailSelection stub, history/afterSwap handlers), shell.py STAGE_PARTIALS whitelist + `stage` context var"
  - phase: 57-01
    provides: "htmx 2.0.10 / Alpine 3.15.12 SRI-pinned base.html, dead-template AST guard, collectible test_shell_routes.py stub"
provides:
  - "shell/partials/rail.html — the DAG nav spine: 12 prototype-order nodes, each hx-get=/s/<id> hx-target=#stage-workspace hx-swap=innerHTML hx-push-url=true; aria-current drives the active visual via the aria-[current=page] variant; x-text live counts bound to EXISTING $store.pipeline keys only; +Scan CTA; amber Review & Apply group; below-the-line audit/agents links"
  - "shell/partials/header.html — wave logo + theme toggle (lifted verbatim), the ⌘K affordance button (id=cmdk-trigger, $dispatch cmdk:open), the D-05 agent status strip (dot + Agents·{n} bound to $store.pipeline.agentOnline, riding the existing /pipeline/stats poll)"
  - "shell/partials/cmdk_modal.html — Alpine skeleton command palette: role=dialog/aria-modal, ⌘K/Ctrl+K keybind, ESC + backdrop close, focus input on open, return focus to #cmdk-trigger on close, ?palette=1 auto-open hook; empty skeleton body (D-04), core Alpine only"
  - "shell.html include wiring (header/rail/cmdk) + completed syncRailSelection(path) re-marking the active rail node on history-restore AND afterSwap"
affects: [57-04 legacy-redirects (the /search → /?palette=1 target the cmdk auto-open consumes), 58 analyze-workspace (rail Analyze lane sub-list), 61 cmdk-functional + status-strip-rich + focus-trap, 62 cutover CUT-02 (legacy tab-bar dead-code removal)]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Rail node = focusable <button> carrying the four HTMX swap attrs + data-rail-stage=<id>; the active visual is driven ENTIRELY from aria-current=\"page\" via the Tailwind aria-[current=page] arbitrary variant, so JS only toggles the attribute (no class bookkeeping)"
    - "syncRailSelection re-marks the active rail node from the URL on BOTH htmx:historyRestore and htmx:afterSwap of #stage-workspace — the rail is not re-rendered on the innerHTML swap, so the active marker is re-synced client-side from location.pathname (/ → analyze, /s/<id> → <id>)"
    - "data-rail-stage on rail nodes is distinct from #stage-workspace's own data-stage marker so the query selector never mis-selects the workspace div"
    - "Cross-partial open: header button $dispatch('cmdk:open') bubbles to window; the modal listens @cmdk:open.window — no shared Alpine store, no new $store key"
    - "Skeleton modal open/close/focus contract on core Alpine only (no @alpinejs/focus): show()/hide() methods, $nextTick input focus, getElementById('cmdk-trigger').focus() on close; ?palette=1 auto-open via x-init"
    - "Live counts: x-text bound to EXISTING $store.pipeline keys (discovered/metadataDone/fingerprintDone/analyzeActive/tracklistDone/proposalsDone); trackid + the 5 amber items render no count (no new store keys)"

key-files:
  created:
    - src/phaze/templates/shell/partials/rail.html
    - src/phaze/templates/shell/partials/header.html
    - src/phaze/templates/shell/partials/cmdk_modal.html
  modified:
    - src/phaze/templates/shell/shell.html
    - tests/test_shell_routes.py

key-decisions:
  - "The active-node visual is driven from aria-current=\"page\" via the aria-[current=page] Tailwind variant rather than a separate `.sel` class — so the server-rendered initial state AND the JS re-sync both reduce to toggling ONE attribute (no class drift between the two paths)"
  - "syncRailSelection is also called from htmx:afterSwap (not only historyRestore): a rail swap replaces ONLY #stage-workspace, so the rail's server-rendered aria-current would otherwise stay on the previous node; re-syncing from the just-pushed location.pathname keeps the marker correct after every click"
  - "The header ⌘K button → modal open uses a bubbling $dispatch('cmdk:open') event (caught by @cmdk:open.window on the modal root) instead of a shared $store.cmdk — honors the must_have 'no new store keys' and keeps the two partials decoupled"
  - "The ?palette=1 auto-open hook (A3) lives on the cmdk_modal root's x-init (the only scope that owns `open`); shell.html wires it in via the {% include %} — functionally 'on shell load' while keeping the open-state encapsulated in the modal"
  - "test_tabbar_removed_header_present asserts absence of the base.html nav landmark (aria-label=\"Main navigation\") + the legacy /search/ tab href, NOT /proposals/ — the bridged Analyze dashboard content (dag_canvas.html) legitimately links to /proposals/, so it is not a valid tab-bar marker (auto-fixed during Task 3)"

requirements-completed: [SHELL-02, SHELL-03]

# Metrics
duration: ~40min
completed: 2026-06-29
---

# Phase 57 Plan 03: Shell Navigation Chrome (DAG Rail · Header · ⌘K) Summary

**The navigation chrome that makes the v7.0 shell usable: a DAG rail nav spine (12 prototype-order nodes, each HTMX-swapping only `#stage-workspace` with `hx-push-url`, `aria-current` active state, and live counts bound to existing `$store.pipeline` keys), a header carrying the wave logo + ⌘K affordance + D-05 agent status strip, and an Alpine ⌘K skeleton modal — all wired into the Plan-02 shell with `syncRailSelection` completed and SHELL-02/03 proven by tests.**

## Performance
- **Duration:** ~40 min
- **Completed:** 2026-06-29
- **Tasks:** 3
- **Files:** 3 created, 2 modified

## Accomplishments
- **DAG rail (`rail.html`, SHELL-02):** the `<aside w-[280px]>` nav spine with the "Pipeline" eyebrow + primary "+ Scan" CTA, then the 12 nodes in VERBATIM prototype order (discover · Enrich[metadata, fingerprint, analyze] · Identify[trackid, tracklist] · propose · amber Review & Apply[rename, tagwrite, move, dedupe, cue]) plus the below-the-line audit/agents plain links. Every navigable node is a focusable `<button>` carrying all four HTMX attrs (`hx-get="/s/<id>"`, `hx-target="#stage-workspace"`, `hx-swap="innerHTML"`, `hx-push-url="true"`) + `data-rail-stage="<id>"`. The active node carries `aria-current="page"` and the active visual (blue tint + inset bar) follows from it via the `aria-[current=page]` Tailwind variant. Live `x-text` counts bind to the EXISTING `$store.pipeline` keys only (discovered/metadataDone/fingerprintDone/analyzeActive/tracklistDone/proposalsDone); trackid + the 5 amber items render no count.
- **Header (`header.html`, SHELL-03 + D-05):** lifts the wave logo (links to `/`) and the auto/dark/light theme toggle verbatim; adds the ⌘K search affordance (`id="cmdk-trigger"`, dispatches `cmdk:open`) and the minimal agent status strip — a dot bound to `$store.pipeline.agentOnline` + an "Agents · {n}" link to `/admin/agents`, refreshed by the EXISTING `/pipeline/stats` OOB poll with no second timer.
- **⌘K modal (`cmdk_modal.html`, D-04):** a core-Alpine skeleton command palette with the full open/close/focus contract — opens on the header button (`@cmdk:open.window`), the `⌘K`/`Ctrl+K` keybinding, or `?palette=1` on load; closes on ESC + backdrop; focuses the input on open and returns focus to `#cmdk-trigger` on close. `role="dialog" aria-modal="true"`, empty skeleton body, no `@alpinejs/focus`.
- **Wiring (`shell.html`):** replaced the Plan-02 placeholder header + rail with the `header.html`/`rail.html` includes, added the `cmdk_modal.html` include; completed `syncRailSelection(path)` (maps `/`→analyze, `/s/<id>`→`<id>`; toggles `aria-current` on `[data-rail-stage]` nodes) and now also calls it from `htmx:afterSwap` so a rail click re-marks the active node (the rail is not re-rendered on the `#stage-workspace` swap).
- **Tests (`test_shell_routes.py`):** filled the two remaining Plan-03 stubs — `test_rail_nodes_wired` (all 12 nodes wired + analyze `aria-current` on `/`) and `test_tabbar_removed_header_present` (legacy nav landmark gone; ⌘K button + status dots + Agents link present).

## Task Commits
1. **Task 1: DAG rail partial** — `8b4d610` (feat)
2. **Task 2: header + ⌘K skeleton modal** — `f95142e` (feat)
3. **Task 3: wire into shell.html + complete syncRailSelection + SHELL-02/03 tests** — `24cb86d` (feat)

## Files Created/Modified
- `src/phaze/templates/shell/partials/rail.html` *(new)* — the DAG nav spine (see Accomplishments). Carries working `dark:` variants on every element.
- `src/phaze/templates/shell/partials/header.html` *(new)* — wave logo + theme toggle (lifted) + ⌘K affordance + D-05 status strip.
- `src/phaze/templates/shell/partials/cmdk_modal.html` *(new)* — Alpine skeleton command palette (D-04).
- `src/phaze/templates/shell/shell.html` — header/rail/cmdk includes wired in; `syncRailSelection` completed; `afterSwap` re-syncs the active rail node.
- `tests/test_shell_routes.py` — filled `test_rail_nodes_wired` + `test_tabbar_removed_header_present` (replaced the two body-less Plan-03 stubs; added `_RAIL_STAGES` + `re` import).

## Decisions Made
See frontmatter `key-decisions`. Headlines: (1) the active visual is driven from `aria-current="page"` via the `aria-[current=page]` variant so server-render and JS re-sync both reduce to toggling one attribute; (2) `syncRailSelection` runs on `afterSwap` too, not only `historyRestore`, because a rail swap never re-renders the rail; (3) the header→modal open uses a bubbling `$dispatch('cmdk:open')` event rather than a new `$store` key; (4) the `?palette=1` auto-open hook lives on the modal root's `x-init` (the scope that owns `open`), wired into the shell via the include.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] `test_tabbar_removed_header_present` must not assert absence of the `/proposals/` href**
- **Found during:** Task 3 (test fill + run).
- **Issue:** The bridged Analyze default (`/`) embeds the existing pipeline-dashboard content (`dag_canvas.html`), which legitimately contains `href="/proposals/"`. An initial assertion `'href="/proposals/"' not in body` (intended as a legacy-tab-bar marker) failed against that legitimate content.
- **Fix:** Dropped the `/proposals/` assertion; kept the two markers that are unique to the retired tab-bar — `aria-label="Main navigation"` (the base.html nav landmark) and `href="/search/"` (only base.html renders it). These robustly prove the legacy tab-bar is gone without false-flagging bridged content.
- **Files modified:** `tests/test_shell_routes.py`
- **Verification:** `test_tabbar_removed_header_present` passes; the full target set is 10/10 green.
- **Committed in:** `24cb86d` (Task 3 commit).

**2. [Rule 3 - Blocking] `syncRailSelection` extended to `htmx:afterSwap` (not only `historyRestore`)**
- **Found during:** Task 3 (completing the stub).
- **Issue:** The Plan-02 history handler called `syncRailSelection` only on `htmx:historyRestore`. A forward rail click swaps only `#stage-workspace`, so the rail's server-rendered `aria-current` would remain on the previously-active node — the active marker would not follow a normal click, only a back/forward restore.
- **Fix:** Added a `syncRailSelection(location.pathname)` call inside the existing `htmx:afterSwap` `#stage-workspace` branch (alongside the existing focus-to-heading), so every rail click re-marks the active node from the just-pushed URL.
- **Files modified:** `src/phaze/templates/shell/shell.html`
- **Committed in:** `24cb86d` (Task 3 commit).

### Enhancement
- Comments in `header.html`/`cmdk_modal.html` were phrased to avoid the literal forbidden tokens (`setInterval`, `@alpinejs/focus`) so the source files stay clean against any grep-style verification (the rendered body never contains Jinja comments anyway).

---

**Total deviations:** 2 auto-fixed (1 bug, 1 blocking) + 1 enhancement. No architectural changes; no new dependencies; no new `$store.pipeline` keys.

## Known Stubs
- The ⌘K modal body is an intentional **skeleton** (D-04) — a single muted placeholder line, no search/command wiring. Functional palette contents are **Phase 61**. This is a planned, contract-locked stub, not a defect.
- The right detail pane in `shell.html` remains a Phase-57 placeholder `<aside>` (bridged content is a later phase) — unchanged by this plan and explicitly out of scope (per 57-UI-SPEC Scope Boundary).
- The rail Analyze node renders the node only (no lane sub-list) — the lane sub-list is **Phase 58** (per 57-UI-SPEC).

## Threat Flags
None — this plan introduces no new network endpoint, auth path, or trust boundary. Per the plan's `<threat_model>`: rail counts + status dots bind `x-text` to server-computed `int` `$store.pipeline` keys only (no `| safe`, no user-influenced interpolation — T-57-04 mitigated); the ⌘K modal is a client-only Alpine skeleton with an empty body and no server input (T-57-05 accept); the status strip rides the single existing `/pipeline/stats` poll with no new loop (T-57-06 accept); no package installs (T-57-SC accept).

## Verification Evidence
- `uv run pytest tests/test_shell_routes.py tests/test_dead_template_guard.py tests/test_base_html_sri.py` → **10 passed** (incl. the two newly-filled SHELL-02/03 tests; dead-template guard still GREEN — rail/header/cmdk reachable via shell.html includes).
- `uv run pytest tests/test_dag_canvas_render.py tests/test_pipeline_dag_context.py tests/test_routers/test_pipeline.py` → **154 passed** (bridged Analyze content + dashboard routes unaffected).
- `uv run ruff check .` → all checks passed; `uv run mypy .` → no issues in 183 source files.
- Jinja parse-checks on all three new partials + `shell.html` → parse-ok.
- Full suite (single shared ephemeral Postgres+Redis): **2417 passed, 90 errors**. The 90 are the SAME pre-existing cross-test DB-contamination (shared single Postgres, no per-test DB isolation) documented in the 57-01 and 57-02 summaries — every erroring file is unrelated to this plan (none are shell/dead-template/SRI), this plan changed only templates + one test file, and all of this plan's tests pass in isolation. CI provisions a clean DB per job.

## Self-Check: PASSED
- Files: all 3 created + 2 modified present on disk (verified).
- Commits: `8b4d610`, `f95142e`, `24cb86d` all present in `git log` (verified).

---
*Phase: 57-shell-dag-rail*
*Completed: 2026-06-29*
