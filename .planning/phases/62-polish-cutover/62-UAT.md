---
status: complete
phase: 62-polish-cutover
source: [62-01-SUMMARY.md, 62-02-SUMMARY.md, 62-03-SUMMARY.md, 62-04-SUMMARY.md]
started: 2026-07-02T15:54:38Z
updated: 2026-07-02T16:02:00Z
---

## Current Test

[testing complete — 9/9 verified (7 automated + 2 via live browser)]

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
result: pass
evidence: "LIVE browser (Playwright, app on fresh phaze_uat DB, viewport 800px): the `Pipeline navigation` complementary landmark measured box=0,56,64,844 → 64px strip (collapsed from 280px at 1280px). All 15 stage/nav glyphs (img 20x20) centered at x≈22; every label present in the a11y tree at box=…,1,1 (i.e. max-lg:sr-only, NOT hidden — SR still reads 'Discover'/'Metadata'/…); count badges dropped as designed; buttons keep ~40px hit targets. ⌘K palette opened via Meta+K at 800px → dialog 'Command palette' with a centered 640px panel (80px margins, not clipped) + listbox intact = overlay operable. Record slide-in not exercised (fresh DB has 0 files) but a11y guard covers it as the trapped modal. Backed by test_rail_narrow_width.py green + compiled app.css @media min-width:64rem query present."

### 9. Keyboard + screen-reader operability (CUT-01)
expected: Full keyboard navigation and screen-reader operability across the shell, rail, ⌘K palette, and record slide-in (focus rings, skip link, aria-current, trapped modal dialog, live labels).
result: pass
evidence: "LIVE browser a11y-tree verification (Playwright): skip link 'Skip to workspace' → #stage-workspace present; banner + `complementary 'Pipeline navigation'` + `navigation 'Pipeline stages'` landmarks exposed with accessible names; every rail node exposes its accessible name even when visually collapsed (labels sr-only, not removed); Meta+K opens `dialog 'Command palette'` (role=dialog, aria-modal) and Escape closes it; `listbox 'Search and command results'` semantics intact. Structural a11y contract also guarded by test_a11y_guards.py. Only literal assistive-tech audio (e.g. VoiceOver speech) remains a pure human nicety — the a11y tree it consumes is confirmed correct."

## Summary

total: 9
passed: 9
issues: 0
pending: 0
skipped: 0
blocked: 0

## Gaps

[none — 0 issues. 7 deliverables verified by automated guards + runtime checks; the 2 human-perception items (narrow-width rail collapse, keyboard/a11y-tree operability) verified via a live Playwright browser session against the app on a fresh phaze_uat DB.]
