---
phase: 88-lane-agent-drill-in
plan: 01
subsystem: ui
tags: [htmx, alpinejs, jinja2, fastapi, a11y, poll-survival, drill-in]

# Dependency graph
requires:
  - phase: 71-backend-lane-ui
    provides: get_backend_lane_snapshot + _lane_card.html N-lane grid the lane trigger extends
  - phase: 29-admin-agents
    provides: agents_table.html self-poll + admin_agents page/table_partial the agent trigger extends
  - phase: 61-record-slide-in
    provides: record_host.html / cmdk_modal.html Esc + heading-focus discipline (borrowed non-modal)
provides:
  - Shared NON-modal _detail_pane.html shell (role=region swap target, D-09 keyboard/focus/dismiss, ?param clear, own-tick + degrade caption slots)
  - Keyboard-accessible role=button drill-in triggers on lane cards (lane-trigger-{id}) and agent rows (agent-trigger-{id})
  - ?lane=/?agent= poll-survival wiring — both polls carry the pushed param via hx-vals and re-emit the selected highlight (aria-current + ring) every 5s swap
  - Server-side selected_lane/selected_agent seeding (reload re-opens the highlight)
affects: [88-02-lane-detail-body, 88-03-agent-activity-body]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Non-modal side pane: role=region (NOT dialog), no aria-modal/x-trap/backdrop; borrow Esc + heading-focus, drop the trap"
    - "Poll-survival: pushed ?param re-read on the existing poll via hx-vals='js:{...URLSearchParams...}' (htmx 2.0.10 does not auto-append) + lookup-in-known-set server resolution"
    - "Focus-return-by-STABLE-ID (not captured node) for a trigger the 5s poll replaces in the DOM"

key-files:
  created:
    - src/phaze/templates/pipeline/partials/_detail_pane.html
    - tests/analyze/routers/test_lane_drill_survival.py
    - tests/agents/routers/test_agent_drill_survival.py
  modified:
    - src/phaze/templates/pipeline/partials/_lane_card.html
    - src/phaze/templates/admin/partials/agents_table.html
    - src/phaze/templates/pipeline/partials/analyze_workspace.html
    - src/phaze/templates/admin/agents.html
    - src/phaze/templates/shell/shell.html
    - src/phaze/routers/pipeline.py
    - src/phaze/routers/admin_agents.py
    - src/phaze/routers/shell.py
    - tests/shared/core/test_enrich_analyze_workspaces.py

key-decisions:
  - "The D-03 own-tick 'Last refreshed' setInterval countdown lives in the wave-2 body, NOT the shell — the /s/analyze fragment is a load-bearing single-poll, setInterval-free surface (WORK-05/R-2). The shell defines the placeholder + static degrade/refresh-error caption slots."
  - "Esc guard detects a visible [role=dialog] (offsetParent != null) instead of [aria-modal='true'] literal — keeps the modal-deference behaviour while satisfying the 'no aria-modal in the pane' invariant."
  - "Pane hosted as a right-side column on lg+ / stacked below on <lg using ONLY core Tailwind utilities (lg:grid lg:grid-cols-3 lg:col-span-2/1) — no arbitrary values (self-hosted, no build step)."

patterns-established:
  - "Shared drill-in shell contract single-sourced (D-08): #detail-pane target id, lane-trigger-{id}/agent-trigger-{id} stable ids, ?lane=/?agent= protocol, own-tick placeholder — plans 02/03 implement bodies against it."

requirements-completed: [DRILL-03]

# Metrics
duration: 35min
completed: 2026-07-11
---

# Phase 88 Plan 01: Lane / Agent Drill-In Foundation Summary

**Shared NON-modal `_detail_pane.html` shell + keyboard-accessible `role=button` drill-in triggers on lane cards and agent rows, with `?lane=`/`?agent=` poll-survival re-emitting the selected highlight on every 5s swap.**

## Performance

- **Duration:** ~35 min
- **Started:** 2026-07-11T15:52Z (approx)
- **Completed:** 2026-07-11T16:22Z (approx)
- **Tasks:** 3
- **Files modified:** 13 (3 created, 10 modified)

## Accomplishments
- Built the interface-first `_detail_pane.html` shell: `role="region"` non-modal swap target hosted OUTSIDE both polled regions, with stable-id focus-return, `history.replaceState` `?param` clear, guarded Esc + visible ✕ Close, D-09 heading-focus-on-open, and static degrade/refresh-error caption slots.
- Made lane cards (`lane-trigger-{id}`) and agent rows (`agent-trigger-{id}`) keyboard-accessible `role=button` triggers (Enter via htmx `keyup[key=='Enter']` + the required inline `onkeydown` Space handler, visible `focus-visible` ring, `hx-target="#detail-pane"`, `hx-push-url` `?param`).
- Wired both DRILL-03 escape hatches together (D-02): the persistent `#pipeline-stats` poll and the `#agents-table-section` self-poll each carry the pushed `?param` via `hx-vals`, and the endpoints re-emit `aria-current` + the `ring-2 ring-blue-500` selected ring for the matching card/row; an unknown/absent param highlights nothing and never 500s; a reload seeds the highlight server-side.
- Added two RED-first survival test modules (7 tests) plus full `analyze` (566) and `agents` (455) bucket runs — all green.

## Task Commits

1. **Task 1: shared _detail_pane.html shell + survival test scaffolds** - `e6010b9f` (feat)
2. **Task 2: keyboard-accessible drill-in triggers + host #detail-pane** - `45c1a94d` (feat)
3. **Task 3: poll-survival — carry ?lane=/?agent= + re-emit selection** - `13a1d864` (feat)

## Files Created/Modified
- `src/phaze/templates/pipeline/partials/_detail_pane.html` - NEW shared non-modal pane shell (D-08/D-09/D-03)
- `src/phaze/templates/pipeline/partials/_lane_card.html` - card root is the lane drill-in trigger (frozen box model untouched)
- `src/phaze/templates/admin/partials/agents_table.html` - `<tr>` drill-in trigger + `#agents-table-section` `?agent=` hx-vals + `selected_agent` default
- `src/phaze/templates/pipeline/partials/analyze_workspace.html` - hosts `#detail-pane` outside `#analyze-lanes` (lg right column / stacked)
- `src/phaze/templates/admin/agents.html` - hosts `#detail-pane` outside `#agents-table-section`
- `src/phaze/templates/shell/shell.html` - `#pipeline-stats` poll carries `?lane=` via hx-vals (RESEARCH OQ-2)
- `src/phaze/routers/pipeline.py` - `pipeline_stats_partial` accepts `lane` Query, threads lookup-resolved `selected_lane`
- `src/phaze/routers/admin_agents.py` - `page`/`table_partial` accept `agent` Query, thread `selected_agent` via `_resolve_selected_agent`
- `src/phaze/routers/shell.py` - `/s/analyze` reload seeds `selected_lane` from `?lane=`
- `tests/analyze/routers/test_lane_drill_survival.py` - NEW lane trigger markup + poll-highlight survival tests
- `tests/agents/routers/test_agent_drill_survival.py` - NEW agent-row trigger markup + poll-highlight survival tests
- `tests/shared/core/test_enrich_analyze_workspaces.py` - updated scaffold-focus-target invariant (see Deviations)

## Decisions Made
- See `key-decisions` frontmatter. The load-bearing one: the own-tick "Last refreshed" countdown was moved out of the shell into the wave-2 body to preserve the pervasive WORK-05/R-2 "no setInterval in the /s/analyze fragment" invariant, consistent with the plan's own `<interfaces>` (own-tick = wave-2 body's responsibility).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Removed the shell setInterval countdown to preserve the single-poll fragment invariant**
- **Found during:** Task 2 (hosting `#detail-pane` in `analyze_workspace.html`)
- **Issue:** The plan's Task 1 action mandated a "Last refreshed Ns ago" `setInterval` countdown in the shell. Hosting the pane inside the `/s/analyze` HX fragment then put a `setInterval` in that fragment, which is forbidden by a pervasive family of WORK-05/R-2 invariant tests ("no second poll loop / no setInterval in the fragment", enforced across `test_enrich_analyze_workspaces.py`, `test_shell_routes.py`, `test_record_palette_agents.py`, `test_review_apply_workspaces.py`, `test_identify_workspaces.py`).
- **Fix:** Moved the own-tick + "Last refreshed" countdown to the wave-2 body (aligns with the plan's `<interfaces>`: "Own-tick placeholder: wave-2 body carries hx-get={endpoint} hx-trigger='every 5s'"). The shell keeps the own-tick placeholder comment + static (no-setInterval) degrade and `role="alert"` refresh-error caption slots.
- **Files modified:** `src/phaze/templates/pipeline/partials/_detail_pane.html`
- **Verification:** `analyze` + `agents` buckets green; all `setInterval not in fragment` assertions pass.
- **Committed in:** `45c1a94d` (Task 2 commit)

**2. [Rule 1 - Bug] Updated the analyze-fragment scaffold-focus-target invariant (1 → 2)**
- **Found during:** Task 2 (hosting `#detail-pane` in the analyze fragment)
- **Issue:** `test_lane_cards_states` asserts `body.count('tabindex="-1"') == 1` (exactly one scaffold focus target). The pane's D-09-required header `<h2 id="detail-pane-heading" tabindex="-1">` (focus-on-open, Pitfall 1) is an intentional SECOND focus target, so the fragment now legitimately has 2.
- **Fix:** Updated the assertion to `== 2` with an explanatory comment and added `assert 'id="detail-pane-heading"' in body` to keep the check specific.
- **Files modified:** `tests/shared/core/test_enrich_analyze_workspaces.py`
- **Verification:** `test_enrich_analyze_workspaces.py` green.
- **Committed in:** `45c1a94d` (Task 2 commit)

**3. [Rule 3 - Blocking] Esc guard uses visible-dialog detection instead of the `aria-modal` literal**
- **Found during:** Task 1 (writing the pane Esc handler)
- **Issue:** The 88-PATTERNS excerpt guarded Esc with `document.querySelector('[aria-modal="true"]')`, but Task 1's acceptance criterion requires `_detail_pane.html` to NOT contain `aria-modal`.
- **Fix:** Guard on a visible `[role="dialog"]` (`offsetParent !== null`) instead — same "defer to an open modal" behaviour (record slide-in / ⌘K palette both carry `role="dialog"`), no `aria-modal`/`x-trap` literal in the pane.
- **Files modified:** `src/phaze/templates/pipeline/partials/_detail_pane.html`
- **Verification:** Template render-smoke asserts `aria-modal`/`x-trap` absent, `role="region"` present.
- **Committed in:** `e6010b9f` (Task 1 commit)

---

**Total deviations:** 3 auto-fixed (2 bug, 1 blocking)
**Impact on plan:** All three reconcile the plan's guidance with pre-existing load-bearing invariants and its own acceptance criteria. No scope creep — the delivered contract (D-01/D-02/D-08/D-09) is intact, with the own-tick correctly assigned to the wave-2 body per the plan's `<interfaces>`.

## Known Stubs

Intentional wave-1 placeholders resolved by plans 02/03 (documented in the pane + tracked here):
- `_detail_pane.html` renders a **resting empty state** ("No lane/agent selected") — the wave-2 bodies (`_lane_detail.html`, `_agent_activity.html`) are swapped into `#detail-pane` by the trigger `hx-get`. The endpoints `/pipeline/lanes/{id}` and `/admin/agents/{id}/_activity` do NOT exist yet (wave 2) — a drill-in click currently has no body to load. This is the interface-first split (D-08); the shell contract is deliberately body-agnostic.
- The **own-tick "Last refreshed" countdown** is a placeholder comment in the shell; the wave-2 body carries the actual `hx-trigger="every 5s"` self-refresh + countdown.

No stubs block DRILL-03 (the shell + triggers + poll-survival highlight are fully wired and tested).

## Issues Encountered
- None beyond the deviations above. Both `analyze` (566) and `agents` (455) buckets pass in isolation; `ruff`/`ruff format`/`mypy` clean on all changed sources.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- The DRILL-03 shell contract is single-sourced and green: `#detail-pane` target id, `lane-trigger-{id}`/`agent-trigger-{id}` stable ids, `?lane=`/`?agent=` protocol, and the own-tick placeholder are all defined.
- Plan 88-02 (lane detail body) and 88-03 (agent activity body) implement the `GET /pipeline/lanes/{id}` and `GET /admin/agents/{id}/_activity` endpoints + their content slots against this contract, and carry the own-tick self-refresh.

---
*Phase: 88-lane-agent-drill-in*
*Completed: 2026-07-11*
