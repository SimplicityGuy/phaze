---
phase: 57-shell-dag-rail
plan: 02
subsystem: ui
tags: [fastapi, htmx, alpinejs, jinja2, shell, dag-rail, routing, redirect, testing]

# Dependency graph
requires:
  - phase: 57-01
    provides: htmx 2.0.10 / Alpine 3.15.12 SRI-pinned base.html, dead-template AST guard, collectible test_shell_routes.py stub
provides:
  - "GET / shell root (Analyze default) + GET /s/{stage} with the search.py HX fragment/full fork"
  - "STAGE_PARTIALS whitelist (12 rail-node ids) — per-stage 404 validation, no template-path injection (T-57-01)"
  - "shell/shell.html — the load-bearing three-column frame with the base.html theme/$store.pipeline machinery lifted verbatim; #stage-workspace single swap target; skip-link → #stage-workspace; htmx:historyRestore + afterSwap re-init/focus handlers; data-stage marker"
  - "shell/_stage_fragment.html — bare {% include stage_partial %} (content-only HX swap body)"
  - "pipeline.build_dashboard_context — shared dashboard-context builder consumed by /pipeline/ and the shell Analyze default"
  - "/pipeline/ → / 302 rename-redirect for plain (non-HX) navigations"
affects: [57-03 dag-rail (fills header/rail/cmdk into shell.html + the 2 remaining tests), 57-04 legacy-redirects, 62 cutover CUT-02]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Prefix-less shell router owning / and /s/{stage}; one private _render_stage helper holds the HX fragment-vs-full fork (mirrors search.py:73-77)"
    - "Static STAGE_PARTIALS dict as the stage whitelist — `stage` is matched against keys, never spliced into a template path (ASVS V5 / T-57-01); the literals double as dead-template-guard entry roots"
    - "Shared render-context builder factored out of a page handler so a legacy page and a new shell node render identical content from one source (no drift)"
    - "Conditional rename-redirect (`HX-Request != 'true'` → 302) preserves the HX render path while bookmarks/nav move to the new canonical URL"
    - "data-stage attribute on the swap target so the active stage is readable from the DOM (test marker + Plan-03 syncRailSelection source)"

key-files:
  created:
    - src/phaze/routers/shell.py
    - src/phaze/templates/shell/shell.html
    - src/phaze/templates/shell/_stage_fragment.html
    - src/phaze/templates/shell/partials/_stage_placeholder.html
  modified:
    - src/phaze/routers/pipeline.py
    - src/phaze/main.py
    - tests/test_shell_routes.py
    - tests/test_pipeline_dag_context.py
    - tests/test_dag_canvas_render.py
    - tests/test_routers/test_pipeline.py
    - tests/test_routers/test_pipeline_scans.py
    - tests/test_routers/test_pipeline_inadmissible.py
    - tests/test_routers/test_pipeline_localqueue.py

key-decisions:
  - "The 11 non-Analyze rail nodes map to ONE minimal placeholder partial (shell/partials/_stage_placeholder.html), not per-route legacy content — real content needs per-stage context explicitly deferred to Phases 58-61; the acceptance criteria only require the 12 ids + analyze→dag_canvas"
  - "The /pipeline/→/ redirect (an explicit must_have) broke 42 dashboard test call-sites in 6 files not listed in the plan; auto-fixed (Rule 3) by sending HX-Request so they keep rendering the identical dashboard.html content"
  - "Used a relative import `from .pipeline import build_dashboard_context` to satisfy the plan's key_link pattern (`from .pipeline import|import pipeline`); valid intra-package import, ruff/mypy clean"
  - "Mirrored pipeline.py's import idiom (AsyncSession under TYPE_CHECKING + `from __future__ import annotations`); empirically resolves at app build (create_app succeeds), and keeps ruff TCH happy"

requirements-completed: [SHELL-01, SHELL-04]

# Metrics
duration: ~55min
completed: 2026-06-29
---

# Phase 57 Plan 02: Shell Router + Structural Shell Summary

**The v7.0 shell spine: a prefix-less `GET /` (Analyze default) + `GET /s/{stage}` router with the HX fragment/full fork and a 12-id stage whitelist, a three-column `shell.html` that lifts the base.html theme/`$store.pipeline` machinery verbatim around a single `#stage-workspace` swap target, a shared `build_dashboard_context` bridging the Analyze node to the existing DAG canvas, and the `/pipeline/`→`/` rename-redirect.**

## Performance

- **Duration:** ~55 min
- **Completed:** 2026-06-29
- **Tasks:** 3
- **Files:** 4 created, 9 modified

## Accomplishments
- Stood up `shell.py` with the load-bearing contracts locked: the single `_render_stage` HX fork (mirrored verbatim from `search.py:73-77`), the static `STAGE_PARTIALS` whitelist (12 rail-node ids, T-57-01 template-path-injection mitigation), and per-stage 404 validation owned in the handler (D-02).
- Built `shell.html` as the `h-screen overflow-hidden` three-column frame and lifted the entire base.html `<head>` theme/brand machinery byte-for-byte (no-FOUC `_applyTheme`, `Alpine.store('theme')`, the single `Alpine.store('pipeline')` seed consumed-not-redefined, `@theme` tokens, Jura/Inter links, vendored Tailwind 4.3.2 + htmx/Alpine SRI scripts). Retargeted the skip-link to `#stage-workspace`, carried the wave logo + theme toggle, and wired the `htmx:historyRestore`/`htmx:afterSwap` Alpine re-init + focus-to-heading handlers.
- Factored `build_dashboard_context` out of `dashboard()` so the legacy `/pipeline/` page and the shell `/` Analyze default render the same DAG content from one source (D-01 / RESEARCH Open-Q2), and renamed `/pipeline/`→`/` via a conditional 302.
- Filled the SHELL-01 / SHELL-02-fragment / SHELL-04 behavioral tests (green against a real Postgres); left the two Plan-03 stubs body-less.
- Verified the whole change green: shell routes (6), the 6 redirect-impacted dashboard files (217), the dead-template guard, the SRI guard, and `ruff check .` + `mypy .` all clean.

## Task Commits
1. **Task 1: shell router + dashboard-context factoring + /pipeline→/ redirect + main wiring** — `2e0c8e1` (feat)
2. **Task 2: structural shell.html + bare _stage_fragment.html + placeholder partial** — `c095081` (feat)
3. **Task 3: fill SHELL-01/02-fragment/04 route tests (+ data-stage marker)** — `0d902ea` (test)

## Files Created/Modified
- `src/phaze/routers/shell.py` *(new)* — `router = APIRouter(tags=["shell"])`; `STAGE_PARTIALS` (12 ids; `analyze`→`pipeline/partials/dag_canvas.html`, the rest→the shared placeholder); `_render_stage` HX fork; `GET /` (Analyze) + `GET /s/{stage}` (404 on unknown).
- `src/phaze/templates/shell/shell.html` *(new)* — full chrome; lifted `<head>`; `#stage-workspace` (carries `data-stage`); placeholder header (wave + theme toggle) / rail / pane; history/focus handlers.
- `src/phaze/templates/shell/_stage_fragment.html` *(new)* — bare `{% include stage_partial %}`, no extends, no document wrapper.
- `src/phaze/templates/shell/partials/_stage_placeholder.html` *(new)* — inert per-stage panel with a focus `<h1>`.
- `src/phaze/routers/pipeline.py` — new `build_dashboard_context`; `dashboard()` now 302-redirects plain GETs to `/` and renders via the shared builder; `RedirectResponse`/`Response` imports; return type widened to `Response`.
- `src/phaze/main.py` — `from phaze.routers import (… shell …)` + `app.include_router(shell.router)` (prefix-less).
- `tests/test_shell_routes.py` — 4 filled behavioral tests; 2 Plan-03 stubs preserved.
- `tests/test_pipeline_dag_context.py`, `tests/test_dag_canvas_render.py`, `tests/test_routers/test_pipeline*.py` (4 files) — dashboard GETs now send `HX-Request: true` (42 call-sites + `_capture_context`) so they keep rendering `dashboard.html` under the new redirect.

## Decisions Made
- **Non-Analyze stage content:** all 11 non-Analyze nodes share one minimal placeholder partial in Phase 57. Wiring real per-route content requires per-stage context that the plan explicitly defers to Phases 58-61; the acceptance criteria only require the 12 ids present + `analyze` bridged to the dashboard content. The static literal in `STAGE_PARTIALS` keeps the placeholder reachable for the dead-template guard.
- **Relative import for the key_link:** `from .pipeline import build_dashboard_context` (satisfies the plan's `from .pipeline import|import pipeline` link pattern; intra-package, ruff/mypy clean).
- **`data-stage` marker:** added to `#stage-workspace` so SHELL-01 can robustly assert the active stage and Plan 03's `syncRailSelection` can read it from the DOM (rather than depending solely on embedded-content markers).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] The /pipeline→/ redirect broke 42 dashboard test call-sites in 6 unlisted files**
- **Found during:** Task 1 (adding the `dashboard()` redirect — an explicit must_have/action).
- **Issue:** With the conditional 302, a plain `GET /pipeline/` returns 302; 42 existing assertions across `test_dag_canvas_render.py`, `test_pipeline_dag_context.py`, and `test_routers/test_pipeline{,_scans,_inadmissible,_localqueue}.py` expect a 200 dashboard render. None of these files are in the plan's `files_modified`.
- **Fix:** Sent `headers={"HX-Request": "true"}` on every affected `client.get("/pipeline/")` (and in the shared `_capture_context` helper) so `dashboard()` still renders the identical `dashboard.html` content for them. Directly caused by the in-scope redirect → in-scope Rule-3 auto-fix.
- **Files modified:** the 6 test files above.
- **Verification:** the 6 files (217 tests) pass against a real Postgres; `ruff` clean.
- **Committed in:** `2e0c8e1` (Task 1 commit).

**2. [Rule 3 - Scoping] Non-Analyze stages bridged to a shared placeholder, not legacy content**
- **Found during:** Task 1 (`STAGE_PARTIALS` design).
- **Issue:** The plan's literal "map each rail node id to the EXISTING content partial that bridges it" is not satisfiable for the 11 non-Analyze nodes without wiring their per-route context — which the plan/UI-SPEC explicitly defer to Phases 58-61.
- **Fix:** Map the 11 to one minimal `shell/partials/_stage_placeholder.html`; keep `analyze`→`dag_canvas.html` as the only live bridge (its context comes from the shared `build_dashboard_context`). Within the Task-1 acceptance criteria (12 ids present; `analyze`→dashboard content).
- **Committed in:** `2e0c8e1` (router) + `c095081` (placeholder template).

### Enhancement
- **`data-stage` on `#stage-workspace`** (Task 3) — small markup addition for testability + Plan-03 `syncRailSelection`; committed in `0d902ea`.

---

**Total deviations:** 2 auto-fixed (both Rule 3) + 1 enhancement. No architectural changes; no new dependencies.

## Known Stubs
- `tests/test_shell_routes.py` — `test_rail_nodes_wired` and `test_tabbar_removed_header_present` remain intentionally body-less. They are Plan 57-03 (Task 3) territory (the rail + header partials do not exist yet) and are filled — not redeclared — by that plan. Documented in the plan; not a defect.
- `shell.html`'s `syncRailSelection(path)` is a deliberate no-op stub — the history handler is wired now; Plan 03 completes the rail-selection sync. The header/rail/⌘K placeholders are inert markup (Plan 03 replaces them via `{% include %}`).

## Threat Flags
None — no new network endpoint introduces unmitigated surface. `GET /s/{stage}` is the only new untrusted-input path and it is whitelisted (`stage in STAGE_PARTIALS` → 404; `stage` never interpolated into a template path), satisfying the plan's T-57-01 `mitigate` disposition. The `/pipeline/`→`/` redirect target is the static constant `/` (no open-redirect surface, T-57-02). New templates keep Jinja autoescape on; no `| safe` on user-influenced values (T-57-03).

## Verification Evidence
- `uv run pytest tests/test_shell_routes.py tests/test_dead_template_guard.py tests/test_base_html_sri.py` → 10 passed.
- The 6 redirect-impacted dashboard files → 217 passed.
- `uv run ruff check .` → all checks passed; `uv run mypy .` → no issues in 183 source files.
- Full suite (local single Postgres+Redis) showed only non-deterministic `IntegrityError` cross-test-contamination failures in files this plan never touched; every such file passes in isolation (e.g. 140 passed across the run-2 failing files). CI provisions a clean DB per job.

## Self-Check: PASSED

---
*Phase: 57-shell-dag-rail*
*Completed: 2026-06-29*
