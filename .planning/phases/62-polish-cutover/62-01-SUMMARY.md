---
phase: 62-polish-cutover
plan: 01
subsystem: ui
tags: [a11y, wcag, aria, htmx, alpine, jinja2, pytest, structural-guard]

# Dependency graph
requires:
  - phase: 57-shell-dag-rail
    provides: skip link, rail nav/aside landmarks, aria-current, focus-visible rings, dead-template + SRI guard idiom
  - phase: 61-full-record-k-agents
    provides: ⌘K combobox/listbox palette, record slide-in role=dialog aria-modal x-trap
provides:
  - "tests/test_a11y_guards.py — pure-filesystem CUT-01 structural a11y guard (no browser, no new dep)"
  - "⌘K combobox input accessible name (aria-label=\"Search files and commands\")"
  - "Removal of the dead empty right detail-pane <aside> (superseded by the Phase 61 record slide-in)"
affects: [62-polish-cutover, cutover, accessibility, regression-guards]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Filesystem-only pytest structural a11y guard (read_text + substring/regex over template SOURCE, fast lane, no DB fixture)"

key-files:
  created:
    - tests/test_a11y_guards.py
  modified:
    - src/phaze/templates/shell/partials/cmdk_modal.html
    - src/phaze/templates/shell/shell.html

key-decisions:
  - "CUT-01 executed as audit-and-close-gaps, not an ARIA rebuild (D-01a) — one real fix (⌘K accessible name) + one dead-element removal"
  - "A11y proven by repo-style pytest structural guards over HTML source, no axe/pa11y/browser dependency (D-01)"

patterns-established:
  - "Strip Jinja {# comments #} before splitting template source on an attribute token, so a comment that documents the attribute by name does not create a spurious node chunk"

requirements-completed: [CUT-01]

# Metrics
duration: 5min
completed: 2026-07-02
---

# Phase 62 Plan 01: CUT-01 Accessibility Baseline Summary

**Locked the WCAG-2.1-AA CUT-01 baseline with a browser-free pytest structural guard, closed the one real gap (⌘K combobox accessible name), and removed the dead detail-pane aside — no new dependency, no logic change.**

## Performance

- **Duration:** ~5 min
- **Started:** 2026-07-02T05:13:41Z
- **Completed:** 2026-07-02T05:18:24Z
- **Tasks:** 2
- **Files modified:** 3 (1 created, 2 modified)

## Accomplishments
- New `tests/test_a11y_guards.py` codifies the whole CUT-01 baseline as filesystem structural assertions: skip-link-first-focusable + target id, rail `nav`/`aside` landmarks with non-empty labels, every rail node's `aria-current="page"` idiom + a `focus-visible:` ring, ⌘K combobox/listbox/dialog semantics, and the record slide-in as a trapped modal dialog. Runs in the fast lane (no DB fixture ⇒ not `integration`-marked).
- Closed the one confirmed CUT-01 gap: added `aria-label="Search files and commands"` to the ⌘K combobox input (a placeholder is not an accessible name per WAI-ARIA APG).
- Removed the dead empty right detail-pane `<aside aria-label="Detail pane">` from `shell.html` (superseded by the Phase 61 record slide-in; removal deferred from Phase 61) and refreshed the stale record-host include comment that referenced it.
- No SRI drift (`test_base_html_sri.py` green), dead-template guard still green.

## Task Commits

Each task was committed atomically (TDD RED → GREEN):

1. **Task 1: Write the CUT-01 filesystem a11y structural guard** - `2cf5bde` (test)
2. **Task 2: Close the CUT-01 gaps — ⌘K accessible name + remove dead aside** - `1ef73be` (feat)

_TDD: Task 1 is the RED gate (7 present-baseline assertions green, 2 gap assertions red); Task 2 is the GREEN gate (all 9 green)._

## Files Created/Modified
- `tests/test_a11y_guards.py` (created) - Pure-filesystem CUT-01 structural a11y guard, 9 test functions mirroring the `test_dead_template_guard.py` / `test_base_html_sri.py` idiom.
- `src/phaze/templates/shell/partials/cmdk_modal.html` (modified) - Added `aria-label="Search files and commands"` to the combobox input.
- `src/phaze/templates/shell/shell.html` (modified) - Removed the dead detail-pane `<aside>`; refreshed the record-host include comment.

## Decisions Made
None beyond the locked plan decisions (D-01, D-01a, D-02) — followed the plan as specified.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Rail-node split matched `data-rail-stage` inside a Jinja comment**
- **Found during:** Task 1 (writing the rail per-node assertion)
- **Issue:** `rail.html` documents the `data-rail-stage` hook inside a `{# ... #}` comment. Splitting the raw source on the attribute name produced a spurious leading chunk that carried no `aria-current`, so `test_rail_nodes_carry_aria_current_and_focus_visible` failed against known-good markup (false negative).
- **Fix:** Strip Jinja comments (`re.sub(r"\{#.*?#\}", "", ...)`) before splitting, since comments are not rendered markup.
- **Files modified:** tests/test_a11y_guards.py
- **Verification:** Subset re-run — 6/6 target functions green; full file then shows exactly the 2 intended RED gap assertions.
- **Committed in:** `2cf5bde` (Task 1 commit)

---

**Total deviations:** 1 auto-fixed (1 bug, in the new test only)
**Impact on plan:** Correctness fix to the guard's own parsing; no scope creep, no source/logic change.

## Issues Encountered
- The first Task-1 commit was aborted by the `ruff-format` pre-commit hook (it normalized quote style on assert-message strings containing embedded double-quotes). Re-staged the auto-formatted file and re-committed — hooks then passed. No `--no-verify` used.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- CUT-01 is complete and locked by an automated, browser-free guard that is green.
- Remaining Phase 62 work (CUT-02 dead-code cutover, CUT-03 docs, CUT-04 narrow-width rail collapse + icons) is untouched by this plan and can proceed. The a11y guard's rail assertions are collapse-safe scaffolding for CUT-04 (they assert on source class strings, so `max-lg:*` additions won't break them).

## Self-Check: PASSED

- FOUND: tests/test_a11y_guards.py
- FOUND: .planning/phases/62-polish-cutover/62-01-SUMMARY.md
- FOUND commit: 2cf5bde (test — RED gate)
- FOUND commit: 1ef73be (feat — GREEN gate)
- FOUND: cmdk `aria-label="Search files and commands"`
- GONE: dead `aria-label="Detail pane"` aside

---
*Phase: 62-polish-cutover*
*Completed: 2026-07-02*
