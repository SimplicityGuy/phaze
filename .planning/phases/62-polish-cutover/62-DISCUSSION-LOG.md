# Phase 62: Polish & cutover - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-01
**Phase:** 62-polish-cutover
**Areas discussed:** a11y bar & testing (CUT-01), Cutover aggressiveness (CUT-02), Docs & README scope (CUT-03), Narrow-width rail behavior (CUT-04)

---

## a11y bar & testing (CUT-01)

| Option | Description | Selected |
|--------|-------------|----------|
| pytest structural guards @ WCAG 2.1 AA | Target WCAG 2.1 AA; prove with rendered-HTML assertion tests in the existing guard-test style (skip-link, DAG nav ARIA, ⌘K combobox/listbox, slide-in dialog, focus order). No new runtime deps. | ✓ |
| axe-core / pa11y browser audit | Headless-browser a11y audit in CI; catches computed contrast/focus but adds a Node/browser CI dependency + flake. | |
| Manual audit only, "parity with today" | Hand-audit + fix, add ARIA, no new automated tests. Lightest; nothing prevents regressions. | |

**User's choice:** pytest structural guards @ WCAG 2.1 AA
**Notes:** Matches the repo's guard-test culture (dead-template guard, SRI test, x-data quote guard). Parity-or-better is the floor; the four named surfaces (rail keyboard nav, ⌘K, focus states, DAG ARIA + skip link) are non-negotiable.

---

## Cutover aggressiveness (CUT-02)

| Option | Description | Selected |
|--------|-------------|----------|
| Full purge — supersede legacy pages then delete | Convert still-rendered legacy full-page GET routes to shell redirects (like /search), then delete dead list.html wrappers + orphaned partials/JS. Empty the allowlist; guard green. | ✓ |
| Minimal — only guard-proven-dead | Delete just the 7 allowlisted templates + empty the allowlist; leave the still-rendered legacy pages. Lower risk, leaves old-UI code reachable. | |
| You decide (inventory-driven) | Researcher inventories superseded vs still-serving; planner picks the safe cut line. | |

**User's choice:** Full purge — supersede legacy pages then delete
**Notes:** Realizes CUT-02's "no orphaned dead code." Supersession must be verified per legacy page before deletion (D-05); `/audit/` and `/admin/agents` are kept, not superseded (D-04).

---

## Docs & README scope (CUT-03)

| Option | Description | Selected |
|--------|-------------|----------|
| README + architecture.md + project-structure.md, no screenshots | Refresh the IA-describing docs for the DAG-centric shell; skip screenshots (rot fast). | ✓ |
| README only | Update just the top-level README; leave docs/ as-is. | |
| Full sweep incl. quick-start.md + screenshots | All docs + walkthrough + screenshots/GIFs. Most complete, most maintenance. | |

**User's choice:** README + architecture.md + project-structure.md, no screenshots
**Notes:** quick-start.md gets inline nav corrections only if it contains now-wrong steps; no full walkthrough rewrite.

---

## Narrow-width rail behavior (CUT-04)

| Option | Description | Selected |
|--------|-------------|----------|
| Auto-collapse to icon-rail via CSS breakpoint | Below ~lg/1024px the 280px rail collapses to an icon-only strip, pure CSS, no persistence. Requires adding real per-stage icons. Slide-in/⌘K overlay, unaffected. | ✓ |
| Manual toggle + persisted state | Collapse button + localStorage. More control, needs JS + the icon work. | |
| Off-canvas drawer (hamburger) | Rail slides off-screen behind a hamburger. Mobile-nav pattern, heavier for a desktop-primary tool. | |

**User's choice:** Auto-collapse to icon-rail via CSS breakpoint
**Notes:** Requires adding a per-stage icon set — chosen as inline SVG, no new dep (D-08), consistent with the inline wave logo. Collapsed rail must stay screen-reader-navigable via aria-labels.

---

## Claude's Discretion

- Exact CSS breakpoint value + collapsed-rail width (CUT-04).
- The specific inline-SVG glyph per stage (match the prototype/design language).
- Redirect status codes + whether legacy HX branches are removed or left as thin redirects (CUT-02).
- The exact pytest assertion/role set for the CUT-01 guard, so long as the four named surfaces are covered at WCAG 2.1 AA.
- Whether docs/quick-start.md needs inline nav corrections (CUT-03).

## Deferred Ideas

- Touch-input / tablet support (SHELL-06) — v7.x; no phone UI ever.
- Full first-class C3 light theme (RECORD-05) — dark stays primary.
- Per-stage configurable confidence thresholds + override UI (REVIEW-06).
- axe-core/pa11y browser a11y audit — rejected for CUT-01 (dependency/flake); revisit if pytest guards prove insufficient.
- Screenshots/GIFs in docs — rejected for CUT-03 (maintenance rot).
- Manual rail-collapse toggle with persisted state — rejected for CUT-04 (auto CSS chosen).
