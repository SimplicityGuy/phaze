# ADR 0009: Responsive and Accessibility Baseline

**Status:** Accepted

## Decision

The redesigned shell holds one responsive and accessibility baseline across every workspace. The
rules below are product-wide properties, not per-page choices, and each is enforced by a guard that
fails the build rather than by review vigilance.

## Breakpoints and what changes

| Width | Navigation | Tables | Layout |
| --- | --- | --- | --- |
| `lg` and up (≥1024px) | Static expanded 280px rail, all labels visible | Full column set | Header + rail + workspace |
| below `lg` | Off-canvas drawer opened from the header Menu button | Full column set, scrolling inside its own container | Header + workspace |

There is no icon-only navigation state at any width.

### Navigation

The rail collapsed to a 64px icon strip below `lg` until phaze-tzy6s.13. That failed precisely
where it was applied: `title=` never appears on touch, is unreliable for screen readers, and is
unreachable by keyboard, so on phones the fourteen destinations were labelled for assistive tech
only. Fourteen destinations across four groups also exceeds what an icon strip carries legibly.

The count is fourteen, not the sixteen this ADR originally recorded: phaze-tzy6s.11 had already
consolidated the three Rename / Path, Tag write and Move files nodes into the single Changes Review
destination before .13 landed, so the sixteen-node rail this argument was first written against
(phaze-tzy6s.3) no longer existed. The argument is unaffected — fourteen icon-only destinations
across four groups fails for exactly the reasons above — but the number is corrected to the rail
`templates/shell/partials/rail.html` actually renders.

The same `<aside>` is now the expanded rail at `lg`+ and an off-canvas drawer below it. One DOM
copy, moved by CSS — not a desktop rail plus a separate drawer, which would duplicate every heading
id, give the document two identically-named navigation landmarks, and let a destination exist on one
surface but not the other.

Closed means `visibility: hidden`, not merely translated off-screen. A transformed-but-visible
element keeps its tab stops, so a keyboard user would tab through fourteen off-screen destinations
before reaching the workspace.

### Tables

**Every visible table scrolls inside its own `overflow-x-auto` container; the page never scrolls
sideways.** The product deliberately keeps dense expert tables at every viewport rather than
dropping columns or collapsing to cards — the operator's job is comparison across columns, and a
card view destroys that. The cost of that choice is horizontal width on a phone, and the rule above
is what confines it: the table scrolls, the header and rail do not move.

A page that scrolls horizontally is the most common way a dense admin UI becomes unusable on touch,
and it is invisible in desktop review — which is why this is a guard and not a convention.

Exempt: `<table hidden>` carriers that render no visible rows. Matched structurally, so the
exemption cannot widen to a real data table.

### Motion

Every looping animation is gated behind `motion-safe:`. `prefers-reduced-motion` is a
vestibular-disorder accommodation, not a styling preference.

### Viewport units

Heights use `dvh`/`svh`, never bare `vh`. On mobile browsers `100vh` is the *large* viewport — the
height the page would have with the URL bar hidden — so sizing to it puts the bottom of the element
behind browser chrome and makes the last row or the primary button unreachable. Width units (`vw`)
are unaffected and remain correct for modal `max-w` guards.

### Controls

- Icon-only controls carry `aria-label`. Glyphs are `aria-hidden`, so without one the accessible
  name is empty and the control announces as "button".
- Touch targets on navigation controls are at least 44×44 (`h-11 w-11`).
- Status is never carried by colour alone: `status_badge` pairs every tone with a glyph and text.
- Disabled controls explain themselves in visible text wired via `aria-describedby`, never in a
  `title=` tooltip — unreachable by keyboard and absent on touch.

## Where each rule is enforced

| Rule | Guard |
| --- | --- |
| No icon-only rail; labels visible at every width | `tests/shared/core/test_rail_narrow_width.py` |
| Drawer traps focus, escapes, is untabbable when closed | `tests/shared/core/test_rail_narrow_width.py` |
| Exactly one navigation mount | `tests/shared/core/test_rail_narrow_width.py` |
| No static `vh` | `tests/shared/core/test_cross_workspace_responsive_a11y.py` |
| All animation `motion-safe:` gated | `tests/shared/core/test_cross_workspace_responsive_a11y.py` |
| Every visible table has an overflow-x container | `tests/shared/core/test_cross_workspace_responsive_a11y.py` |
| Icon-only controls have accessible names | `tests/shared/core/test_cross_workspace_responsive_a11y.py` |
| Disabled Execute states carry visible reason + next action | `tests/review/routers/test_execute_preflight.py` |

The sweep is filesystem-based and covers **all** templates, including partials that render only in
states a smoke test rarely reaches. Real-browser checks are complementary, not redundant: this lane
is exhaustive about markup properties, that lane is authoritative about rendered behaviour.

**What the browser lane actually covers, corrected (phaze-tzy6s.17 / CR-14-5).** This paragraph
previously stated that "computed contrast, focus order after HTMX swaps, axe — belong to
phaze-tzy6s.14", written as settled fact about landed code. Only one of the three is true.
`tests/browser/test_shell_contract.py` does assert **focus order after an HTMX swap**
(`test_focus_is_not_dropped_to_the_body_after_a_swap`), along with drawer behaviour at phone width,
closed-drawer tab stops, horizontal overflow, the command palette's focus trap and return, theme
persistence, the drawer's `aria-expanded`/`role=dialog` runtime state, and a per-workspace
console-error sweep. It ships **no computed-contrast assertion and no axe pass** — neither exists
anywhere in `tests/browser/`.

So contrast and axe are not covered by any lane today: this ADR's markup sweep cannot compute
rendered colour, and the browser suite does not try. That is a real gap in this baseline's
enforcement rather than a delegation, and it is tracked as such (`phaze-8p1uq`) instead of being
described here as already done. An ADR asserting a guard exists is worse than one admitting it does
not, because the claim is what the next reader checks against instead of the suite.

## The browser contract suite (phaze-tzy6s.14)

`tests/browser/` boots the real application — uvicorn, the real lifespan, real Alembic migrations,
real Postgres and Redis — and drives it with Playwright. Nothing is mocked.

```bash
just test-db                 # the shared Postgres (5433) + Redis (6380) harness
just test-db-for <seat>      # required if other agents are running; copy the exports it prints
just test-browser-install    # once per machine — downloads the Chromium build
just test-browser            # runs the suite (depends on `tailwind`, see below)
```

CI runs it as the **`Browser contract (non-blocking)`** job in `.github/workflows/tests.yml`, with
`continue-on-error: true`. A brand-new browser suite has no flake record, and gating merges on it
before that record exists trains everyone to re-run CI on red — which is how a real failure gets
waved through. Promote it to blocking after a clean run of 10 (tracked on `phaze-8p1uq`).

Three properties of the harness are load-bearing and easy to break:

- **`just test-browser` depends on `tailwind`.** Without the compiled `app.css` the app serves an
  unstyled page and every layout assertion passes vacuously — an unstyled document trivially does
  not overflow. The first run of this suite passed the horizontal-overflow test for exactly that
  reason, while the page was in fact broken.
- **The app gets its own database**, derived by appending `_browser` to the seat's. It must never be
  the unit suite's: that one drops its schema at session teardown, and a live uvicorn holding
  connections would corrupt both.
- **Session-scoped fixtures are synchronous.** Under `asyncio_mode = "auto"` each test gets a
  function-scoped event loop, so a session-scoped *async* fixture is created on the first test's
  loop and awaited from a dead one thereafter. The symptom is a hang, not an error.

What the suite asserts is chosen by one rule: **if an httpx test could prove it from the markup, it
does not belong there.** What remains is behaviour that exists only once a browser has run the
JavaScript — htmx swapping, history restoring, focus moving, storage persisting, layout overflowing.
Fixtures are synthetic; no private archive identifiers appear in the suite.

Note for anyone asserting on rendered text: `inner_text()` returns text as *rendered*, and the
eyebrow headings carry `uppercase`, so compare case-insensitively. And a rail swap uses
`hx-swap="innerHTML"` — the container's own `data-stage` never changes, so the fragment's
`data-document-title` marker is the signal a swap actually landed.

## Keyboard and screen-reader smoke pass

Run at each release touching the shell. Record the date and result in the PR.

1. **Keyboard only, no mouse.** Tab from page load: the drawer trigger (below `lg`) or the first
   rail destination (at `lg`+) must be reachable, and every destination must show a visible focus
   ring. At phone width, confirm the closed drawer contributes **no** tab stops.
2. **Drawer.** Open with Enter, confirm focus moves into it and is trapped, Escape closes it and
   returns focus to the trigger.
3. **Command palette.** ⌘K opens, Escape closes, focus returns to the trigger.
4. **Execute.** With approved work, the confirm dialog opens from the keyboard, is trapped, Escape
   cancels, and the manifest is readable in the dialog. With nothing approved, confirm the disabled
   reason and next action are announced — they are body text, not a tooltip.
5. **Tables.** Confirm a wide table scrolls within itself and the page does not move sideways.
6. **HTMX swaps.** Navigate between workspaces and confirm focus is not dropped to `<body>`.

## Consequences

- Desktop behaviour is unchanged; the expanded rail and full-density tables are preserved.
- Phone navigation costs one tap to open the drawer, accepted in exchange for legible labels.
- The guards are structural, so they run in milliseconds and cannot be satisfied by a mocked render.
- Adding a template with a bare `vh`, an ungated animation, an unwrapped table, or an unnamed icon
  button fails the suite immediately, naming the file and line.
