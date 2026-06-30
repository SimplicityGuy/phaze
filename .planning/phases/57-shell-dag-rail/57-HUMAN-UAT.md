---
status: complete
phase: 57-shell-dag-rail
source: [57-VERIFICATION.md]
started: 2026-06-30T02:07:17Z
updated: 2026-06-30T02:21:57Z
verified_by: claude-driven (Playwright MCP, live app on ephemeral PG+Redis)
---

## Current Test

[testing complete]

## Tests

### 1. Three-column shell visual layout
expected: Visiting `/` renders the three-column Hybrid Console shell; the Analyze rail node shows the active highlight (blue tint + inset bar via the `aria-[current=page]` CSS variant).
result: pass
evidence: `/` → 200; left DAG rail + center Pipeline Graph workspace + header rendered; Analyze node carries `aria-current="page"` with blue tint + inset bar (screenshot uat-1-shell-default.png).

### 2. HTMX rail navigation (no full-page reload)
expected: Clicking a rail stage node swaps only the center `#stage-workspace` via HTMX — no full-page reload, URL updates via `hx-push-url`, browser back/forward restores the correct stage with the active node re-marked.
result: pass
evidence: Clicking Metadata pushed URL to `/s/metadata`, swapped center to the Metadata placeholder, moved `aria-current` to Metadata; a window sentinel survived the swap (no reload). Browser Back restored `/` with Analyze active + DAG present, sentinel still intact (htmx:historyRestore).

### 3. ⌘K modal open / close / focus contract
expected: ⌘K (or Ctrl+K) opens the command palette modal; input receives focus on open; ESC or backdrop click closes it and returns focus to the trigger; `/?palette=1` auto-opens it.
result: pass
evidence: Header trigger AND ⌘K keybind both open the `role=dialog aria-modal=true` palette with the search input focused (screenshots uat-3-cmdk-open.png, uat-3b-cmdk-keybind.png); ESC closes it and focus returns to `#cmdk-trigger`; `/?palette=1` auto-opens with input focused. Skeleton body present per D-04.

### 4. Theme toggle — no FOUC, dark mode applies
expected: Theme toggle switches light/dark with no flash-of-unstyled-content before Alpine boots; `dark:` Tailwind utilities apply correctly; preference persists across reloads.
result: pass
evidence: Inline `_applyTheme` script in `<head>` applies the theme before paint (no-FOUC mechanism). Toggling to dark set `phaze-theme=dark`, added `.dark` to `<html>`, body bg `rgb(10,12,18)`; full shell renders dark (screenshot uat-4-dark-mode.png). Reload persisted dark + Analyze default-active.

### 5. Legacy bookmark → shell redirect (end-to-end)
expected: Navigating directly to a legacy URL (e.g. `/proposals/`) 302-redirects in ≤1 hop into the shell (`/s/propose`) with the propose rail node active; in-page HTMX filter requests are NOT redirected.
result: pass
evidence: Browser nav to `/proposals/` resolved in 1 hop to `/s/propose` with the propose rail node active, rendered in shell. curl confirms: `/proposals/` (no HX) → 302 → `/s/propose`; `/proposals/` with `HX-Request: true` → 200 (no redirect).

## Summary

total: 5
passed: 5
issues: 0
pending: 0
skipped: 0
blocked: 0

## Gaps

[none — all tests passed]
