---
phase: 62-polish-cutover
plan: 03
subsystem: docs
tags: [documentation, ui, information-architecture, htmx, shell, guard-test]

# Dependency graph
requires:
  - phase: 57-shell-dag-rail
    provides: the three-column shell, GET / (Analyze default), GET /s/<stage> routing, DAG rail, ⌘K, header status strip
  - phase: 61-full-record-k-agents
    provides: the per-file record slide-in and Agents page the docs describe
provides:
  - README + docs/architecture.md + docs/project-structure.md describe the v7.0 DAG-centric console IA
  - docs/quick-start.md nav steps point at the shell (/ + DAG rail + ⌘K) instead of removed legacy pages
  - tests/test_docs_ia_current.py — a pure-filesystem docs-currency guard locking the new-IA vocabulary
affects: [62-polish-cutover CUT-02 dead-code cutover, future docs edits]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Filesystem-only docs-currency guard (read_text substring checks, no DB/client fixture — fast lane)"

key-files:
  created:
    - tests/test_docs_ia_current.py
  modified:
    - README.md
    - docs/architecture.md
    - docs/project-structure.md
    - docs/quick-start.md

key-decisions:
  - "Stale-nav guard targets host-qualified legacy-page visit URLs (localhost:8000/{pipeline,proposals,duplicates,tracklists}/) so it never trips on the still-live POST /pipeline/* API endpoints the walkthrough keeps"
  - "Added the shell.py router + templates/shell tree + templates/record to project-structure.md for accuracy (docs currency), beyond the minimal /s/<stage> mapping"

patterns-established:
  - "Docs-currency guard: pytest structural assertions over doc text mirror test_dead_template_guard.py / test_base_html_sri.py idioms"

requirements-completed: [CUT-03]

# Metrics
duration: ~12min
completed: 2026-07-02
---

# Phase 62 Plan 03: CUT-03 Docs Refresh Summary

**Refreshed README + docs/architecture.md + docs/project-structure.md + docs/quick-start.md to describe the v7.0 DAG-centric three-column console (rail-as-nav, /s/<stage> HTMX stage swaps, ⌘K command palette, header status strip, per-file record slide-in), locked by a new pure-filesystem docs-currency guard.**

## Performance

- **Duration:** ~12 min
- **Completed:** 2026-07-02
- **Tasks:** 2
- **Files modified:** 5 (1 created, 4 modified)

## Accomplishments
- New `tests/test_docs_ia_current.py` — a fast-lane structural guard proving each doc carries the new-IA vocabulary and that quick-start no longer points at removed legacy pages (RED before the doc edits, GREEN after).
- README: new "The Console (v7.0 DAG-Centric Shell)" subsection + a Key Features bullet + updated the System Architecture mermaid UI node label from the old tab list to "DAG rail nav · stage workspaces · ⌘K palette".
- `docs/architecture.md`: added a "User Interface / Information Architecture (v7.0)" section (shell layout, the rail-as-nav swap contract — single `#stage-workspace` target, `GET /s/<stage>`, `STAGE_PARTIALS` whitelist, single `/pipeline/stats` poll — ⌘K, status strip, record slide-in, Review & Apply gate), with an explicit "IA/presentation only, no backend behavior change" statement.
- `docs/project-structure.md`: added `shell.py` to the routers listing, added the `templates/shell/` + `templates/record/` tree entries, and a "Shell templates & /s/<stage> routing" section with a router-to-workspace-partial mapping table (verified against `shell.py`'s `STAGE_PARTIALS`).
- `docs/quick-start.md`: corrected the two now-wrong nav steps inline — "open the console" at `/` (Analyze default, DAG rail, ⌘K) and review via the Propose/Rename/Dedupe/Tracklist rail stages — without a full walkthrough rewrite.

## Task Commits

1. **Task 1: Write the CUT-03 docs-currency guard** - `106f2f0` (test)
2. **Task 2: Rewrite README + docs UI/IA sections and fix quick-start nav** - `721db69` (docs)

## Files Created/Modified
- `tests/test_docs_ia_current.py` - Pure-filesystem docs-currency guard (4 assertion functions; no DB/client fixture)
- `README.md` - DAG-centric console subsection + Key Features bullet + updated mermaid UI node
- `docs/architecture.md` - New UI/IA section describing the shell, /s/<stage> swap contract, and record slide-in
- `docs/project-structure.md` - shell.py router + templates/shell tree + /s/<stage> workspace mapping section
- `docs/quick-start.md` - Nav steps repointed at the shell (/, DAG rail, ⌘K)

## Decisions Made
- Kept the stale-nav check targeted at host-qualified legacy-page visit URLs so the guard is robust and does not flag the still-live `POST /pipeline/*` API endpoints the walkthrough legitimately references.
- Went slightly beyond the minimal doc scope in `project-structure.md` (added the `shell.py` router row and `templates/record/` entry) so the codebase-layout doc is accurate, not just partially updated — consistent with the project's docs-currency expectation.

## Deviations from Plan

None - plan executed exactly as written. (Both docs describe existing capabilities under the new IA only; no backend/logic change, no screenshots, no new dependency — consistent with D-06 and the presentation-only phase constraint.)

## Issues Encountered
None. Verified `templates/record/` and each `pipeline/partials/<stage>_workspace.html` exist before documenting them.

## Verification
- `uv run pytest tests/test_docs_ia_current.py -q` — 4 passed (was 4 failed before Task 2, confirming RED→GREEN).
- `uv run ruff check .` — clean.
- `uv run mypy .` — clean (186 source files; the "unused section(s)" note is pre-existing config noise).
- Regression: `tests/test_dead_template_guard.py` + `tests/test_base_html_sri.py` static checks pass (no template/SRI drift — shell.html/base.html untouched).

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- CUT-03 complete. The remaining Phase 62 work (CUT-01 a11y, CUT-02 dead-code cutover, CUT-04 narrow-width rail) is independent of this docs plan.
- Note for CUT-02: when the legacy wrappers/routers are deleted, the redirect-into-shell behavior the docs now describe must remain — the docs-currency guard does not assert redirects (that is the dead-template guard's and router tests' job).

---
*Phase: 62-polish-cutover*
*Completed: 2026-07-02*
