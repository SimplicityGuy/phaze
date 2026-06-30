---
phase: 57-shell-dag-rail
plan: 01
subsystem: ui
tags: [htmx, alpinejs, tailwind, sri, jinja2, dead-template-guard, testing]

# Dependency graph
requires:
  - phase: 27-ui-hardening
    provides: tests/test_base_html_sri.py (the SRI static-pin + live-CDN recompute guard this plan re-validates against)
provides:
  - htmx 2.0.10 + Alpine 3.15.12 SRI-pinned to recomputed SHA-384 hashes in base.html
  - Tailwind 4.3.2 vendored (static/vendor) with the stale 4.3.0 file deleted
  - tests/test_dead_template_guard.py — jinja2.meta orphan-template AST guard, seeded GREEN
  - tests/test_shell_routes.py — collectible Wave-1 stub with the six SHELL-01..04 test names
affects: [57-02 shell-router, 57-03 dag-rail, 57-04 legacy-redirects, 62 cutover CUT-02]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Dead-template AST guard via jinja2.meta.find_referenced_templates transitive closure from a router-derived entry set"
    - "Collectible body-less test stub seeded one wave ahead so later -k/::node-id verify commands resolve immediately"

key-files:
  created:
    - src/phaze/static/vendor/tailwindcss-browser-4.3.2.min.js
    - tests/test_dead_template_guard.py
    - tests/test_shell_routes.py
  modified:
    - src/phaze/templates/base.html

key-decisions:
  - "Used the live-CDN-recomputed Alpine 3.15.12 SHA-384 (pb6hrQvo…) instead of the RESEARCH-supplied hash (LUONAH…), which was stale and would have silently blocked Alpine"
  - "Broadened the dead-template guard entry set from name=\"x.html\" literals to all \"*.html\" string literals in router source, because routers also render via _render_partial(positional) and ternary-assigned template vars (RESEARCH caveat A4 was inaccurate)"
  - "Allowlisted exactly one genuinely-dead partial (tracklists/partials/toast.html) for Phase 62 / CUT-02 removal rather than weakening the guard or deleting templates out of this plan's scope"

patterns-established:
  - "Dead-template guard: entry set = all .html string literals in src/phaze/routers/*.py; reachable = jinja2.meta closure over extends/include/import; orphan = templates/**/*.html reached by nobody; minimal justified allowlist"
  - "SRI bumps are validated by the existing test_base_html_sri.py live-CDN recompute — never trust a hand-supplied hash without recomputing"

requirements-completed: [SHELL-04, SHELL-05]

# Metrics
duration: ~12min
completed: 2026-06-29
---

# Phase 57 Plan 01: Stack Bumps + Dead-Template Guard Foundation Summary

**htmx 2.0.10 / Alpine 3.15.12 SRI-repinned (live-CDN-verified) + Tailwind 4.3.2 vendored, with a jinja2.meta dead-template AST guard and a collectible SHELL-01..04 test stub both seeded green.**

## Performance

- **Duration:** ~12 min
- **Started:** 2026-06-29T16:36Z (worktree spawn)
- **Completed:** 2026-06-29T16:48Z
- **Tasks:** 3
- **Files modified:** 4 (1 modified, 3 created; 1 deleted)

## Accomplishments
- Bumped the three browser-delivered libs to the ROADMAP-locked versions with recomputed SRI; the existing live-CDN SRI test is green against all three.
- Caught and corrected a stale Alpine SRI hash in the plan's own interface block — the exact silent-script-block landmine this plan exists to de-risk.
- Seeded `tests/test_dead_template_guard.py` GREEN with a correct, honest entry-set extraction and a one-entry justified allowlist.
- Seeded `tests/test_shell_routes.py` as a collectible green stub so every later `-k`/`::node-id` verify command in Plans 02/03 resolves the moment it runs.

## Task Commits

Each task was committed atomically:

1. **Task 1: Bump htmx/Alpine SRI + swap vendored Tailwind to 4.3.2** - `20f8d5c` (feat)
2. **Task 2: Seed the dead-template AST guard test (green)** - `6916fda` (test)
3. **Task 3: Seed the shell-route test stub (collectible, green)** - `7228f11` (test)

## Files Created/Modified
- `src/phaze/templates/base.html` - htmx 2.0.7→2.0.10 and Alpine 3.15.9→3.15.12 src+integrity bumped; Tailwind script + comment block bumped 4.3.0→4.3.2; htmx-ext-sse@2.2.4 left untouched.
- `src/phaze/static/vendor/tailwindcss-browser-4.3.2.min.js` - vendored Tailwind v4.3.2 browser build (SHA-384 matched the RESEARCH reference hash exactly on download).
- `src/phaze/static/vendor/tailwindcss-browser-4.3.0.min.js` - **deleted** (stale, replaced by 4.3.2).
- `tests/test_dead_template_guard.py` - orphan-template AST guard (jinja2.meta closure), seeded green.
- `tests/test_shell_routes.py` - six body-less SHELL-01..04 stubs, filled by Plans 02/03.

## Decisions Made
- **Alpine SRI hash source:** the RESEARCH/interface hash for Alpine 3.15.12 (`LUONAH…`) did not match the live CDN; the recomputed value (`pb6hrQvo…`) was stable across 3 independent fetches and is what shipped. htmx's RESEARCH hash (`H5Srcfyg…`) matched the live CDN and was used as-is.
- **Dead-template entry set scope:** broadened to all `.html` string literals in router source (not only `name="..."`), the minimum change that correctly classifies templates rendered via `_render_partial(request, "x.html", …)` and the `admin_agents` ternary-assigned `name=template`.
- **Static path correction:** the plan frontmatter listed the vendored asset under `static/vendor/`; the actual mount is `src/phaze/static/vendor/` (`main.py:228`), so files landed there.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Corrected the Alpine 3.15.12 SRI hash to the live-CDN value**
- **Found during:** Task 1 (SRI bump)
- **Issue:** The plan's `<interfaces>` block and 57-RESEARCH.md specified Alpine integrity `sha384-LUONAH/vnlbGK96OtMBbN0l0Fcsr7dW3BK7NOImE4oHZAZ/IwIEvvpxyajWxvpaD`. `tests/test_base_html_sri.py`'s live-CDN recompute rejected it: jsDelivr serves `sha384-pb6hrQvo4s23cEUFtj0CZkzGE3jyK3pj26RIupXXxhSrrcUA/Cn0lZgcCrGH0t6L`. A stale hash silently blocks Alpine (theme/store dead) — exactly the failure mode this plan targets.
- **Fix:** Recomputed independently (3 stable fetches via `curl … | openssl dgst -sha384`), used the served value.
- **Files modified:** src/phaze/templates/base.html
- **Verification:** `uv run pytest tests/test_base_html_sri.py` — 3 passed incl. the live-CDN recompute.
- **Committed in:** `20f8d5c` (Task 1 commit)

**2. [Rule 1 - Bug] Broadened the dead-template guard entry set beyond `name=` literals**
- **Found during:** Task 2 (guard seeding)
- **Issue:** The plan-specified entry set (`name="...html"` literals only) produced 3 false orphans — `admin/agents.html` (rendered via a ternary-assigned `template` var → `name=template`) and `execution/partials/{dispatch_summary_inline,progress_row_inline}.html` (rendered via the `_render_partial(request, "<tpl>.html", …)` positional helper). RESEARCH caveat A4 ("all renders use `name=` literals") was inaccurate.
- **Fix:** Entry set now extracts every quoted `"*.html"` literal from router source (still computed from routers, not hardcoded). This is an entry-set accuracy fix, not a closure-logic weakening — stray non-template literals are harmless (no on-disk target, not under templates/).
- **Files modified:** tests/test_dead_template_guard.py
- **Verification:** `uv run pytest tests/test_dead_template_guard.py` green; the 3 false orphans are now correctly reachable.
- **Committed in:** `6916fda` (Task 2 commit)

**3. [Rule 2 - Missing Critical] Allowlisted one genuinely-dead template (the guard's intended signal)**
- **Found during:** Task 2 (guard seeding)
- **Issue:** After fixing the entry set, `tracklists/partials/toast.html` remained a true orphan — zero references in any router, template `{% include %}`, or JS (the duplicates/proposals/cue toast partials are the live ones). RESEARCH claimed zero orphans; this one is real.
- **Fix:** Added it to `_ALLOWLIST` with an inline comment citing it as pre-existing dead, slated for removal by CUT-02 (Phase 62). The plan explicitly defers dead-template *removal* to Phase 62, so allowlisting (per the plan's own instruction) keeps the guard green without deleting templates out of scope.
- **Files modified:** tests/test_dead_template_guard.py
- **Verification:** `uv run pytest tests/test_dead_template_guard.py` green; allowlist has exactly one justified entry.
- **Committed in:** `6916fda` (Task 2 commit)

---

**Total deviations:** 3 auto-fixed (2 bugs, 1 missing-critical)
**Impact on plan:** All three are correctness fixes the plan's own verification (the SRI test) and the guard's purpose demanded. No scope creep — no templates deleted, no shell code added.

## Issues Encountered
- Worktree spawned at the old v6.0 tip (`d7420ba`) rather than the phase-57 base; the startup base-correction step `git reset --hard 5526653` brought in the phase-57 plan files before execution.
- PostgreSQL is not running in this sandbox, so the DB-backed portion of the full suite was not executed here. This plan changed no Python source (only `base.html` content, a vendored JS asset, and two self-contained test files), so there is no regression risk to DB-backed tests; full-suite collection is clean (2507 tests collected, no errors) and CI runs the suite against a real DB.

## Known Stubs
- `tests/test_shell_routes.py` — six intentionally body-less (`...`) test functions. This is a planned Wave-1 collectible stub (plan Task 3); bodies are filled by Plan 57-02 (Task 3) and Plan 57-03 (Task 3), which replace — not redeclare — the functions. Documented in the plan's must_haves; not a defect.

## Threat Flags
None — no new network endpoint, auth path, file-access pattern, or trust-boundary surface was introduced. The only CDN→browser surface (htmx/Alpine `<script integrity=>`) is the in-scope T-57-SRI mitigation and is enforced by `tests/test_base_html_sri.py`.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- The SRI hashes are correct and live-CDN-validated, so Plans 02–04 can build the shell/rail without the silent-script-block risk.
- The dead-template guard is green and will be re-run in every subsequent plan's quick run; it is structured to stay green as shell templates are added (dynamic `{% include stage_partial %}` resolves to None and is dropped, not flagged).
- `tests/test_shell_routes.py` exists with the six SHELL-01..04 names, so Plan 02/03 `-k` selectors and `::node-id` verify commands resolve immediately.

## Self-Check: PASSED

---
*Phase: 57-shell-dag-rail*
*Completed: 2026-06-29*
