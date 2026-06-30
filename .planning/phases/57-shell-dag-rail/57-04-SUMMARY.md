---
phase: 57-shell-dag-rail
plan: 04
subsystem: ui
tags: [fastapi, htmx, routing, redirect, shell, dag-rail, legacy-bookmarks, testing]

# Dependency graph
requires:
  - phase: 57-02
    provides: "GET / shell root + GET /s/{stage} with STAGE_PARTIALS whitelist; /pipeline/→/ rename already landed here"
  - phase: 57-03
    provides: "rail/canonical stage ids (the /s/<id> targets the redirects point at); base.html tab-bar retired; ?palette=1 ⌘K auto-open hook"
provides:
  - "Conditional 302 redirects on the 7 legacy render-in-shell / rename GET handlers: a plain (non-HX) GET resolves into the shell in ≤1 hop; the existing HX-filter branch is left intact (D-01)"
  - "Canonical legacy→shell map: /proposals/→/s/propose, /tracklists/→/s/tracklist, /tags/→/s/tagwrite, /cue/→/s/cue, /duplicates/→/s/dedupe, /preview/→/s/move, /search/→/?palette=1 (⌘K)"
  - "tests/test_redirect_resolution.py — 8-route ≤1-hop redirect proof + HX-filter-not-redirected, enumerating routes via _route_introspection"
affects: [62 cutover CUT-02 (removes the now-unreachable full-page else branches + dead legacy templates)]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Conditional legacy-route redirect: `if request.headers.get('HX-Request') != 'true': return RedirectResponse(url='<static target>', 302)` as the FIRST handler statement, above the existing `== 'true'` filter branch — plain GET/bookmark moves to the shell, the in-page HX filter keystroke is never hijacked (D-01, Pitfall 2)"
    - "Static internal redirect targets only (`/s/<stage>`, `/?palette=1`) — never derived from a query param or user input, so there is no open-redirect surface (T-57-RD)"
    - "Return type widened HTMLResponse→Response on the redirected handlers (RedirectResponse is a Response, not an HTMLResponse) — mirrors pipeline.py's /pipeline→/ rename from Plan 02"
    - "Legacy-route content tests migrated to the shell reality: row-data tests send HX-Request (exercise the now-canonical filter partial); full-page-chrome tests (heading/stats/empty-state/nav-tabs/filter-panel) assert the 302 into the shell (that content moved to the placeholder workspace nodes / ⌘K)"

key-files:
  created:
    - tests/test_redirect_resolution.py
  modified:
    - src/phaze/routers/proposals.py
    - src/phaze/routers/tracklists.py
    - src/phaze/routers/tags.py
    - src/phaze/routers/cue.py
    - src/phaze/routers/duplicates.py
    - src/phaze/routers/preview.py
    - src/phaze/routers/search.py
    - tests/test_routers/test_proposals.py
    - tests/test_routers/test_tracklists.py
    - tests/test_routers/test_tags.py
    - tests/test_routers/test_cue.py
    - tests/test_routers/test_duplicates.py
    - tests/test_routers/test_preview.py
    - tests/test_routers/test_search.py
    - tests/test_routers/test_execution.py

key-decisions:
  - "proposals redirects to /s/propose, NOT /s/proposals as the plan's interface table stated — the canonical shell stage id for the proposals workspace is `propose` (the rail node id); /s/proposals is not in STAGE_PARTIALS and 404s, which would violate SHELL-05's ≤1-hop-to-200 criterion. Rule-1 fix applied to both the router and the test map."
  - "The redirect broke 58 pre-existing list-page tests across 8 router test files (plain GET now 302s) — the same class of breakage Plan 02's /pipeline→/ redirect caused (42 tests). Migrated them (Rule 3): row-data assertions get HX-Request; full-page-chrome assertions are repointed to assert the 302 into the shell, because that chrome (page headings, stats headers, empty-states, the retired nav tab-bar, the search filter panel) now lives on the placeholder workspace nodes (Phases 58-61) or the ⌘K palette."
  - "The now-unreachable `else: return <full page>` lines in each handler are LEFT in place (the plan scopes this wave to inserting the redirect, not removing the full-page branch; the literal template names keep the dead-template guard green; Phase 62 CUT-02 removes them)."

requirements-completed: [SHELL-05]

# Metrics
duration: ~35min
completed: 2026-06-29
---

# Phase 57 Plan 04: Legacy Bookmark Redirects into the Shell Summary

**Every legacy tab URL now resolves into the v7.0 shell in ≤1 hop: a conditional 302 at the top of each of the 7 legacy GET handlers (`HX-Request != "true"` → static `/s/<stage>` or `/?palette=1`) moves plain navigations/bookmarks to the canonical shell URL while leaving each handler's in-page HX-filter branch untouched (D-01), proven by an 8-route ≤1-hop redirect-resolution test plus an HX-filter-not-redirected guard.**

## Performance
- **Duration:** ~35 min
- **Completed:** 2026-06-29
- **Tasks:** 2
- **Files:** 1 created, 15 modified

## Accomplishments
- **Conditional redirects on 7 routers (Task 1):** inserted the `if request.headers.get("HX-Request") != "true": return RedirectResponse(url="<target>", status_code=302)` guard as the FIRST statement of `list_proposals`, `list_tracklists`, `list_tags`, `list_cue`, `list_duplicates`, `tree_preview`, and `search_page`. Added `RedirectResponse`/`Response` to each `from fastapi.responses import …` line and widened the handler return type `HTMLResponse → Response`. All targets are static internal string literals (no open-redirect, T-57-RD). The existing `== "true"` filter branches were left byte-for-byte intact (D-01); `preview.py` had no HX-branch so the conditional form is used purely to bypass the redirect for uniformity.
- **Redirect-resolution test (Task 2):** `tests/test_redirect_resolution.py` parametrizes the 8 canonical trailing-slash routes → their shell targets, asserts a single-hop 302 to the exact target (`follow_redirects=False`, `location.split("?")[0] == target`) and a 200 on `follow_redirects=True`; `test_hx_filter_not_redirected` proves an `HX-Request: true` GET to `/proposals/` returns its filter partial (NOT a 302); `test_legacy_routes_registered` enumerates the routes via `tests/_route_introspection.effective_route_paths` (never `app.routes`).
- **Verified green:** the plan's target set (`test_redirect_resolution` + `test_shell_routes` + `test_dead_template_guard` + `test_base_html_sri`) = 20 passed; the full suite = **2517 passed, 0 failed**; coverage **97.24%** (gate 85%); `ruff check .` + `mypy .` clean.

## Task Commits
1. **Task 1: conditional redirects on the 7 legacy routers** — `b0f7300` (feat)
2. **Task 2: redirect-resolution test + migrate legacy-route tests to shell behavior** — `81c6135` (test; includes the Rule-1 `/s/propose` fix)

## Files Created/Modified
- `tests/test_redirect_resolution.py` *(new)* — the SHELL-05 proof (8-route ≤1-hop, HX-not-redirected, route-registration via introspection).
- `src/phaze/routers/{proposals,tracklists,tags,cue,duplicates,preview,search}.py` — conditional redirect guard + import + return-type widening.
- `tests/test_routers/test_{proposals,tracklists,tags,cue,duplicates,preview,search,execution}.py` — migrated 58 list-page tests to the shell reality (see Deviations).

## Decisions Made
See frontmatter `key-decisions`. Headlines: (1) proposals → `/s/propose` (the canonical rail-node id; the plan's `/s/proposals` 404s); (2) the redirect broke 58 legacy list-page tests — migrated them exactly as Plan 02 migrated the 42 dashboard tests; (3) the now-dead full-page `else` returns are left for Phase 62 CUT-02.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] proposals redirect target `/s/proposals` 404s — corrected to `/s/propose`**
- **Found during:** Task 2 (the redirect-resolution test's `follow_redirects=True` landed on a 404).
- **Issue:** The plan's interface table specified `/proposals → /s/proposals`, but the canonical shell stage id for the proposals workspace is `propose` (the rail-node id, per `STAGE_PARTIALS` / `_RAIL_STAGES`). `/s/proposals` is not whitelisted → 404, violating SHELL-05's "resolves in ≤1 hop to a 200 with the matching rail node" criterion.
- **Fix:** Changed the `proposals.py` redirect target and the test `CANONICAL` map to `/s/propose`.
- **Files modified:** `src/phaze/routers/proposals.py`, `tests/test_redirect_resolution.py`.
- **Committed in:** `81c6135`.

**2. [Rule 3 - Blocking] The conditional redirects broke 58 pre-existing list-page tests in 8 router test files**
- **Found during:** Task 2 (running the suite after Task 1).
- **Issue:** 58 tests across `test_routers/test_{proposals,tracklists,tags,cue,duplicates,preview,search,execution}.py` do a plain (non-HX) `client.get("/<route>/")` and assert a 200 + page content. With the new conditional 302 they now receive a redirect. None of these files are in the plan's `files_modified`. This is the same class of breakage Plan 02's `/pipeline→/` redirect caused (42 dashboard tests).
- **Fix (mirrors Plan 02):**
  - **Row-data tests** (assert content that lives in the HX filter partial — table rows, filenames, badges, pagination, per-row CUE versions): send `headers={"HX-Request": "true"}` so they exercise the now-canonical filter partial. (`preview.py` has no HX-branch, so its HX request renders the full `tree.html` — all preview assertions survive unchanged.)
  - **Full-page-chrome tests** (assert page headings, stats headers, empty-states, the retired base.html nav tab-bar, the search filter panel — none of which are in the HX partials): repointed to assert the `302` into the shell (`follow_redirects=False`, exact `location`). That chrome moved to the placeholder workspace nodes (Phases 58-61) or, for `/search/`, the ⌘K palette (D-04). Docstrings updated to record the Phase-57 reality.
- **Files modified:** the 8 `test_routers/*` files above.
- **Verification:** all 8 files + `test_redirect_resolution` + `test_shell_routes` = 187 passed; full suite 2517 passed; ruff/mypy clean.
- **Committed in:** `81c6135`.

---

**Total deviations:** 2 auto-fixed (1 Rule-1 bug, 1 Rule-3 blocking). No architectural changes; no new dependencies. `RedirectResponse` is stdlib (starlette, already a dep).

## Known Stubs
None introduced. The redirected legacy routes still contain their now-unreachable `else: return <full page>` branches (the plan deliberately defers their removal to Phase 62 CUT-02); the literal template names keep the dead-template guard green and the full-page templates referenced.

## Threat Flags
None — no new network endpoint or trust boundary. All 7 redirect targets are static internal string literals (`/s/<stage>`, `/?palette=1`), never derived from query params or user input, so there is no open-redirect (`next=`-style) surface (T-57-RD mitigated; the test asserts the exact canonical target). The redirect fires ONLY when `HX-Request` is absent, so the in-page filter is never hijacked into a shell swap (T-57-FH mitigated; `test_hx_filter_not_redirected` enforces it). No package installs (T-57-SC).

## Verification Evidence
- `uv run pytest tests/test_redirect_resolution.py tests/test_shell_routes.py tests/test_dead_template_guard.py tests/test_base_html_sri.py` → **20 passed**.
- The 8 migrated router test files + `test_redirect_resolution` + `test_shell_routes` → **187 passed**.
- Full suite (ephemeral Postgres 18 + Redis 7 via `just test-db`) → **2517 passed, 0 failed** in 324s.
- `uv run pytest --cov` → **TOTAL 97.24%** (Required 85.0% reached). Touched routers: preview 100%, proposals 98.4%, cue 96.4%, duplicates 95.2%, tracklists 92.8%, search 91.7%, tags 87.5%.
- `uv run ruff check .` → all checks passed; `uv run mypy .` → no issues in 183 source files.

## Self-Check: PASSED

---
*Phase: 57-shell-dag-rail*
*Completed: 2026-06-29*
