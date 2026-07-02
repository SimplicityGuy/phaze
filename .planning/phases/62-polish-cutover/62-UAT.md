---
status: partial
phase: 62-polish-cutover
source: [62-01-SUMMARY.md, 62-02-SUMMARY.md, 62-03-SUMMARY.md, 62-04-SUMMARY.md]
started: 2026-07-02T15:54:38Z
updated: 2026-07-02T15:54:38Z
---

## Current Test

[testing paused — 2 human-perception items outstanding]

## Tests

### 1. Legacy tab bar removed from global chrome (CUT-02)
expected: The shared `base.html` chrome shows only the wave-logo home link + the theme toggle — the old 8-link tab bar (Search / Pipeline / Proposals / Duplicates / Tracklists / …) is gone. Legacy pages still extending base.html can navigate back to the shell via the logo.
result: pass
evidence: "base.html:161-208 — nav contains only the `/` wave-logo home link (aria-label=\"Phaze home\") + theme toggle; CUT-02/D-04a comment at :173 documents the tab-bar removal. Skip link retargeted to #main-content."

### 2. Legacy /pipeline/ URL redirects into the shell (CUT-02)
expected: Visiting `/pipeline/` no longer renders the old dashboard — it 302-redirects to the shell root `/` so old bookmarks resolve.
result: pass
evidence: "Runtime TestClient: GET /pipeline/ -> 302, Location: /. (pipeline.py:580-591 pure RedirectResponse, DB-free.)"

### 3. Legacy /preview/ URL redirects to the Move workspace (CUT-02)
expected: Visiting `/preview/` 302-redirects to the shell Move workspace `/s/move`.
result: pass
evidence: "Runtime TestClient: GET /preview/ -> 302, Location: /s/move. (preview.py:13-21 pure RedirectResponse.)"

### 4. Dead tab-era templates removed, no orphans (CUT-02)
expected: The 20 legacy wrapper/partial templates (proposals/list.html, pipeline/dashboard.html, pipeline/partials/dag_canvas.html, search/*, preview/tree.html, etc.) are deleted and nothing references them. The dead-template guard is green with an empty allowlist.
result: pass
evidence: "Confirmed gone on disk (9 spot-checked incl. dashboard.html, dag_canvas.html, search/page.html). test_dead_template_guard.py green with _ALLOWLIST=frozenset(); full suite 2565 passed / 96.89% cov per 62-04-SUMMARY."

### 5. ⌘K command palette has an accessible name (CUT-01)
expected: The ⌘K search input exposes an accessible name to assistive tech (not just a placeholder) — `aria-label="Search files and commands"` — and the palette carries combobox/listbox/dialog semantics.
result: pass
evidence: "cmdk_modal.html:54 aria-label=\"Search files and commands\" on role=combobox input; dialog role/aria-modal/aria-label + role=listbox present. test_a11y_guards.py green."

### 6. Dead detail-pane aside removed (CUT-01)
expected: The empty right-hand detail-pane `<aside aria-label="Detail pane">` (superseded by the Phase 61 record slide-in) is gone from the shell.
result: pass
evidence: "62-01-SUMMARY self-check GONE; SRI + dead-template guards green; a11y guard asserts record slide-in as the trapped modal dialog that supersedes it."

### 7. Docs describe the v7.0 DAG-centric console (CUT-03)
expected: README + docs/architecture.md + docs/project-structure.md describe the three-column DAG-rail console (/s/<stage> HTMX swaps, ⌘K, status strip, record slide-in), and docs/quick-start.md nav steps point at the shell instead of removed legacy pages.
result: pass
evidence: "test_docs_ia_current.py green (4 assertions: new-IA vocabulary present in each doc; quick-start no longer links host-qualified legacy visit URLs)."

### 8. Narrow-width rail collapses to a usable icon strip <1024px (CUT-04)
expected: Below 1024px the 280px DAG rail collapses to a 64px icon-only strip: per-stage glyphs visible, text labels visually hidden but still screen-reader readable, native tooltips + aria-labels intact, active-node tint/aria-current preserved, and the record slide-in + ⌘K overlays remain operable.
result: blocked
blocked_by: human-perception
reason: "Inherently a human-eye layout check at the breakpoint (per 62-VALIDATION 'Manual-Only Verifications'). Mechanism fully verified: test_rail_narrow_width.py green (max-lg:w-16 collapse, ≥15 aria-hidden glyphs, labels max-lg:sr-only NOT max-lg:hidden, per-node titles, aria-current+focus preserved) AND compiled app.css confirmed to contain the `@media …min-width:64rem` collapse query + .max-lg:w-16 / .max-lg:sr-only utilities. Remaining: eyeball the rendered result + overlay usability in a live browser <1024px."

### 9. Keyboard + screen-reader operability (CUT-01)
expected: Full keyboard navigation and screen-reader operability across the shell, rail, ⌘K palette, and record slide-in (focus rings, skip link, aria-current, trapped modal dialog, live labels).
result: blocked
blocked_by: human-perception
reason: "Inherently a human-perception check requiring an actual screen reader (per 62-VALIDATION 'Manual-Only Verifications'). Structural a11y tree fully guarded by test_a11y_guards.py (skip-link-first + target id, nav/aside landmarks with labels, aria-current idiom + focus-visible rings, ⌘K combobox/listbox/dialog semantics, record slide-in trapped modal). Remaining: live keyboard/SR walk-through."

## Summary

total: 9
passed: 7
issues: 0
pending: 0
skipped: 0
blocked: 2

## Gaps

[none — 0 issues. The 2 blocked items are inherently human-perception checks (not code defects); every automatable requirement is verified green.]
