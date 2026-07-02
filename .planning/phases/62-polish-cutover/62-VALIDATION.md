---
phase: 62
slug: polish-cutover
status: draft
nyquist_compliant: true
wave_0_complete: false
created: 2026-07-01
---

# Phase 62 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x (pytest-asyncio) |
| **Config file** | pyproject.toml (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest tests/test_dead_template_guard.py tests/test_base_html_sri.py -q` |
| **Full suite command** | `uv run pytest -q` |
| **Estimated runtime** | ~90–180 seconds (full suite ~1900 tests; DB-heavy subsets may flake under colima — re-run isolated) |

---

## Sampling Rate

- **After every task commit:** Run the quick run command (guard tests are pure-filesystem, sub-second)
- **After every plan wave:** Run `uv run pytest -q` plus `uv run ruff check .` + `uv run mypy .`
- **Before `/gsd:verify-work`:** Full suite green + `pre-commit run --all-files` clean
- **Max feedback latency:** ~180 seconds

---

## Per-Task Verification Map

> Task IDs are provisional — the planner assigns final IDs. This maps each CUT requirement to its automated proof.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 62-01-01 | 01 | 1 | CUT-01 | — | Skip link present + first focusable; rail exposes nav landmark + aria-current; visible focus states asserted | structural (rendered/source) | `uv run pytest tests/test_a11y_guards.py -q` | ❌ W0 | ⬜ pending |
| 62-01-02 | 01 | 1 | CUT-01 | — | ⌘K combobox input has accessible name (`aria-label`); combobox/listbox/option + aria-activedescendant/aria-expanded present | structural | `uv run pytest tests/test_a11y_guards.py -q` | ❌ W0 | ⬜ pending |
| 62-01-03 | 01 | 1 | CUT-01 | — | Record slide-in is `role=dialog aria-modal=true` with accessible name + x-trap | structural | `uv run pytest tests/test_a11y_guards.py -q` | ❌ W0 | ⬜ pending |
| 62-02-01 | 02 | 1 | CUT-04 | — | rail labels use `max-lg:sr-only` (readable, not hidden); per-stage inline-SVG icons present with aria-hidden + node accessible label; rail collapses <lg | structural | `uv run pytest tests/test_rail_narrow_width.py -q` | ❌ W0 | ⬜ pending |
| 62-03-01 | 03 | 1 | CUT-03 | — | README + docs/architecture.md + docs/project-structure.md contain the new-IA sections (shell/rail/⌘K/stages); no stale tab-UI nav references | source assertion | `uv run pytest tests/test_docs_ia_current.py -q` | ❌ W0 | ⬜ pending |
| 62-04-01 | 04 (LAST) | 2 | CUT-02 | — | 8 wrapper templates + 6 orphaned partials deleted; `_ALLOWLIST` empty; closure logic untouched; the 5 content routers' live HX branch KEPT | guard | `uv run pytest tests/test_dead_template_guard.py -q` | ✅ | ⬜ pending |
| 62-04-02 | 04 (LAST) | 2 | CUT-02 | — | legacy non-HX GET still 302-redirects into shell (SHELL-05 bookmark preservation) after wrapper deletion | behavior | `uv run pytest tests/test_shell_routes.py -q` | ✅ (extend) | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_a11y_guards.py` — CUT-01 skip-link / rail landmark / ⌘K combobox / record dialog structural guards
- [ ] `tests/test_rail_narrow_width.py` — CUT-04 max-lg collapse + inline-SVG icon stubs
- [ ] `tests/test_docs_ia_current.py` — CUT-03 docs-contain-new-IA stubs

*Existing infrastructure (pytest, `tests/conftest.py`, the `test_dead_template_guard.py` + `test_base_html_sri.py` + `test_shell_routes.py` guards) covers CUT-02. New a11y/rail/docs guard files are the only Wave 0 additions — they are authored inline as the first (TDD) task of plans 01–03, so `wave_0_complete` stays false at planning time even though every plan task already carries a concrete `<automated>` verify. Follow the pure-filesystem/rendered-HTML assertion idiom of the existing guards — no browser/axe dependency.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Full keyboard-only walkthrough (Tab through rail → ⌘K → record slide-in, no pointer) with visible focus at every stop | CUT-01 | Real focus-order/visibility perception isn't fully assertable via structural tests | Load `/`, Tab through the rail nodes, open ⌘K (⌘K/`/`), arrow-nav results, open a record, Esc — confirm focus is always visible and lands sanely |
| Narrow-width visual: rail collapses to a usable icon strip; slide-in + ⌘K overlays still usable <1024px | CUT-04 | Visual/layout regression needs a human eye at the breakpoint | Resize viewport <1024px; confirm rail shows icons only (labels hidden but SR-readable), tooltips/aria-labels intact, record + palette overlays still operable |

*Screen-reader spot-check (VoiceOver) of the collapsed rail + ⌘K is optional but recommended.*

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies — every plan task carries a concrete inline `<automated>` command; no MISSING placeholders remain
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references — the 3 new guard test files are authored inline as the first task of plans 01–03
- [x] No watch-mode flags
- [x] Feedback latency < 180s — 62-04 Task 3 leads with the sub-second fast-lane filesystem guards, full suite is the secondary final gate
- [x] `nyquist_compliant: true` set in frontmatter

*Note: `wave_0_complete` remains false because the CUT-01/03/04 guard test files are created inline (TDD) during execution, not pre-authored. This is intentional — every task nonetheless ships a real automated command, so the phase is Nyquist-compliant.*

**Approval:** approved — all tasks carry concrete automated commands; 62-04 fast-lane guards give sub-second feedback with the full suite as the final gate.
