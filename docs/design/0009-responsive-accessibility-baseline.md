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
| Every workspace holds the layout contract at 3 widths × 2 themes × 5 states | `tests/browser/test_responsive_matrix.py` (phaze-fk1ww) |
| The dark theme actually paints, and is not just a class name | `tests/browser/test_responsive_matrix.py` |
| The six-step keyboard/screen-reader script, at every width | `tests/browser/test_keyboard_screen_reader.py` (phaze-fk1ww) |
| Rendered ARIA/labelling violations, per workspace | `tests/browser/test_accessibility.py` + `tests/browser/axe.py` |
| Computed WCAG AA contrast, both themes | `tests/browser/test_accessibility.py` — **currently a strict xfail; see ADR-0010** |

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

**Superseded 2026-08-18.** Both now exist. `tests/browser/axe.py` runs axe with an explicit rule
list, and `tests/browser/test_accessibility.py` drives it per workspace — so the axe half of the gap
above is closed, and the paragraph before this one is now itself out of date and kept only as the
record of what was true on 2026-08-17.

The contrast half is closed differently: the check **runs on every invocation and fails**, recorded
as a strict xfail across 10 parametrised cells rather than disabled, because the palette itself is
non-conformant. See **ADR-0010**, which decides the repaint and its target ratio; `phaze-qvid8`
implements it and folds `color-contrast` into `axe.py`'s blocking `RULES`. A guard that runs and is
red is a different state from one that does not exist, and this ADR should not be read as claiming
either that contrast is unchecked or that it passes.

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

Run at each release touching the shell. **Record the date and result below** — see "Validation
record" at the end of this document. This line previously said "in the PR", which is where the
result of the first pass went missing: nothing in the epic's PRs recorded one, and the ADR read as
though the procedure landing were the same thing as the procedure having been run (phaze-fk1ww).
The record lives with the decision now, where the next reader checks.

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

## Validation record

### 2026-08-17 — multi-viewport, dark-theme and screen-reader pass (phaze-fk1ww)

**Result: NOT green.** Two defects found, both filed and neither fixed here: **phaze-mrg1c** (P1,
horizontal document scroll) and **phaze-bdeih** (P2, focus dropped to `<body>`). Everything else in
this baseline held across the whole matrix. The pass is recorded as red because it was red; a
"passed" here that omitted the two would be the same failure mode as the missing record it replaces.

Run against `refactor/code-quality-decomposition`, Chromium via Playwright, driving the real
application (uvicorn, real lifespan, real Alembic migrations, real Postgres and Redis) with the
compiled Tailwind `app.css` — an unstyled page satisfies every layout assertion vacuously, so a run
without `just tailwind` proves nothing. Harness: `tests/browser/test_responsive_matrix.py` and
`tests/browser/test_keyboard_screen_reader.py`; state seeding in `tests/browser/_seed.py` (folded
into `tests/browser/seed.py` by `phaze-jimgu`, which reconciled the two seeding modules the suite
had accumulated). Whole browser suite: **74 passed, 1 skipped, 4 xfailed, 97s**.

#### What was exercised

| Axis | Values |
| --- | --- |
| Widths | desktop **1440×900**, tablet **768×1024** (`md`, below `lg`), phone **390×844** |
| Themes | light and dark, both pinned before first paint via `localStorage['phaze-theme']` |
| States | empty, populated, degraded (terminal metadata + analysis failures → the `danger` attention cards), loading (a workspace fetch held 900 ms), error (the Compute pane's `role=alert` refresh-failure banner) |
| Workspaces | all 14 rail destinations: summary, files, discover, metadata, analyze, tracklist, propose, rename, dedupe, cue, apply, operations, audit, agents |

**252 workspace visits** in the layout sweep (14 workspaces × 3 states × 3 widths × 2 themes),
42 per cell. Per visit: horizontal document overflow (both the scrolling *area* and whether
`scrollX` actually moves), every visible table's computed scroll container, every visible icon-only
control's accessible name, the painted theme, and the navigation branch for the width. Console and
`pageerror` output was captured on every visit.

Counts observed: **15–16 visible tables** per cell at desktop and tablet, **14** at phone (`/s/files`
renders a card view below `md`); **42 / 84 / 126** icon-only controls per cell at desktop / tablet /
phone respectively — the two extra per visit below `lg` are the drawer affordances. Body background
resolved to `rgb(255, 255, 255)` in every light cell and `rgb(10, 12, 18)` in every dark cell, with
body text `oklch(0.21 0.034 264.665)` vs `oklch(0.928 0.006 264.531)`, identical at all three widths.

#### What held, at every width and in both themes

- **The dark theme actually paints.** Not merely `html.dark` present: the two themes were compared
  across separate cold loads and differ in body background, body text colour and header background.
  This is the check that stops "dark validated" from being satisfied by a light render.
- **No icon-only control anywhere lacks a runtime accessible name** — 0 of the 42/84/126 per cell.
- **Every visible table sits in a container that computes `overflow-x: auto|scroll`** and really
  scrolls: measured 517 px of internal scroll at desktop, 323 px at tablet on `/s/files`.
- **No navigation regression in the `md` band.** At 768 px the rail is off-canvas (`visibility:
  hidden`, 0 of 14 destinations focusable while closed) and the drawer trigger is present; at 1440 px
  the rail is the expanded 280 px strip with all 14 labels visibly rendered. There is no icon-only
  state at any width.
- **No console or page errors** on any of the 252 visits, in any state.
- **The layout survives the loading and error states**: no overflow while a workspace fetch is
  outstanding, no overflow with the `role=alert` banner up, and the theme is not dropped mid-swap.
- **Keyboard steps 1–5 pass at all three widths in both themes.** Every tab stop carries a visible
  focus ring and an accessible name; the closed drawer contributes no tab stops at 768 px as well as
  at 390 px; the drawer opens on Enter, moves focus inside, holds it through 25 tabs, and returns it
  to the trigger on Escape; the command palette opens on ⌘K and returns focus at every width; the
  disabled Execute control carries no `title=` and does have `aria-describedby` body text.
- **The accessibility tree exposes one navigation landmark, a main landmark, and no unnamed
  control**, and the open drawer is exposed as a modal dialog.

#### What was found

1. **phaze-mrg1c (P1) — populated `/s/files` and `/s/audit` scroll the whole document sideways.**
   The table is wrapped correctly and the wrapper does scroll; the document scrolls anyway.
   Measured: `/s/files` 429 px at desktop 1440 (644 px in the degraded state) and 229 px at tablet
   768; `/s/audit` 386 px at tablet 768 and a 756 px scrolling area at phone 390. Identical in both
   themes, so it is layout and not colour. At desktop the rail and header leave the viewport
   entirely. Two distinct shapes: `/s/files` is content-width driven (clean with one short-named
   row), `/s/audit` at phone still overflows by 325 px on a single minimal row. The phone `/s/audit`
   case is the weakest of the five — the area is there but a programmatic scroll did not move an
   `is_mobile` context — and is recorded as such.
2. **phaze-bdeih (P2) — below `lg`, activating a rail destination from the drawer drops focus to
   `<body>`.** ADR-0009 step 6, on the path that only exists below `lg`. Intermittent: 8 of 12
   below-`lg` cell-runs over three consecutive runs, 0 of 6 at desktop, with `activeElement` polled
   for 2 s after the swap marker lands so it is not a read-too-early artefact. The intermittency is
   part of the finding — something is racing the drawer's close transition.

#### Why neither was caught before, and what now catches them

Both gaps are the same gap: **an empty database and two hardcoded widths**. The markup sweep
(`test_cross_workspace_responsive_a11y.py`) is exhaustive about classes and cannot compute a rendered
layout, so a correctly-classed wrapper satisfies it. The browser suite
(`test_shell_contract.py`) computes real layout but ran at 1440×900 and 390×844 only, in light only,
against no rows — and an empty table has no width to overflow with, so
`test_the_page_never_scrolls_sideways_on_a_phone` was passing vacuously for precisely the two
workspaces that break. Step 6 was asserted at 1440 px, where the drawer does not exist.

The two new modules close it. `tests/browser/conftest.py` now carries a named `VIEWPORTS` map and a
`page_at(viewport=…, theme=…)` factory rather than two hardcoded fixtures, so a width or a theme is
written down once and every test claiming to have checked "tablet" checked the same 768 px.
`KNOWN_OVERFLOW` in the matrix names the four failing (workspace, width) pairs against phaze-mrg1c;
it fails on a *new* overflow anywhere else and also fails when a listed pair stops reproducing, so
the entries have to be deleted with the fix instead of surviving as stale text.

#### What this pass does NOT establish

- **No automated accessibility checker ran, and none exists in this repo.** The screen-reader half
  of this pass inspected Chromium's own accessibility tree — the tree VoiceOver and NVDA consume —
  not the speech those programs synthesize from it. No VoiceOver or NVDA session was run.
- **No computed-contrast assertion ran.** Colour values are recorded above; nothing computed a
  contrast ratio against them. Both this and an axe integration remain deferred to phaze-8p1uq, as
  the corrected paragraph above already states.
  *(Superseded 2026-08-18: both landed. The computed-contrast check now runs and FAILS — 15 failing
  light-theme colours across 508 class-string uses, 3 dark-theme across 62, worst case 2.36:1. The
  repaint that fixes it is ADR-0010, implemented by `phaze-qvid8`. This 2026-08-17 record stands as
  written; it is not amended, only annotated.)*
- **Step 4's populated half is not covered**: the Execute confirm dialog *with approved work* was
  not driven from the keyboard. Only the disabled/empty branch was.
- The seeded data is synthetic and deliberately smaller than the real archive. Real filenames are
  longer than the ones used here, so phaze-mrg1c is worse in production, not better.

### 2026-08-20 — cold vs warm boot timing audit (phaze-doku9)

**Status: complete.** Audit, measurement, and the AC4 harness-vs-product verdict are all below.

`phaze-39eiy` measured a variable nobody had tested: a fresh pytest process per attempt (fresh
uvicorn boot, fresh Alembic migration from an empty database, fresh browser launch) reproduced a
focus-timing flake once in 14 attempts, at roughly 8.5s per iteration, where ~88 attempts across
four *warm* shapes (single process, repeats, CPU-throttled, whole-file loop) came up clean. This
entry is the general follow-up `phaze-doku9` asked for: which OTHER browser tests carry timing
assumptions that a warm app satisfies and a cold one may not, and what the cold/warm gap actually
is. It does not fix any one test — see the sibling bead for that — and it does not re-run the
cold-boot measurement; it audits `tests/browser/` for the shape of exposure and records what the
code says about where the gap comes from.

**Correction to the framing `phaze-39eiy` shipped with.** `tests/browser/conftest.py`'s
`live_server` fixture is `scope="session"` — one uvicorn boot, one migration run, per pytest
*session*, not per test. CI's `Browser contract (non-blocking)` job runs the whole suite as one
session, so it pays the true boot+migration cost once, for its first test(s), and runs *warm*
against an already-booted server for the remaining ~170. The fresh-process-per-attempt shape that
found the flake is a **more extreme cold reproduction than CI's own steady state** — a good
instrument for finding this class of bug (it forces the server-boot and per-code-path
cache-warming variables to their worst case on every attempt), but its 1-in-14 rate is not a valid
estimate of how often CI itself hits the same race. Read the finding below as "this class of
assumption is real and present in N other tests," not as "CI fails at 1-in-14."

#### Audit method

Every file in `tests/browser/` (21 test modules, plus `conftest.py`, `helpers.py`, `axe.py`) was
read for: fixed sleeps and fixed-timeout waits, `wait_for_function` calls with an implicit
assumption about response latency, any assertion that something did **not** happen within a
window, and settle helpers whose stability window could be satisfied by the wrong state. The
`FLAKE_RECORD.md` section "What to watch when the CI runs start" was the starting list, not the
whole one — it was written as speculation before this bead had evidence, and three of the six
findings below are not in it.

#### The dangerous shape: asserting a negative inside a fixed window

A test that waits a fixed duration and then asserts something did **not** happen is only sound if
the window is longer than the *slowest* correct run could ever take. A cold app that is slower for
reasons unrelated to the property under test (server boot amortization, a not-yet-warm
SQLAlchemy statement cache, a not-yet-warm Jinja2 template cache — see "harness or product?"
below) can push the real event past the window, and the test then reports "confirmed absent"
for an event that was actually still coming. This is a silent wrong-pass, not a red build, which
is what makes it worth an audit rather than waiting for it to fail.

Three tests carry this shape:

1. **`test_execute_dispatch.py::test_the_progress_stream_opens_once_and_stops_reconnecting_after_close`**
   — `await asyncio.sleep(_RECONNECT_WINDOW_SEC)` (6.0s, "Chromium's ~3s reconnect delay with
   generous margin") then `assert len(stream_requests) == 1`. Already named in `FLAKE_RECORD.md`.
2. **`test_execute_dispatch.py::test_an_execution_that_finishes_with_failures_reports_them_and_still_closes`**
   — the same 6.0s pattern, for the `complete_with_errors` terminal status. Also already named.
3. **`test_analyze_lane_detail.py::test_dismissing_the_lane_detail_stops_its_poll_and_returns_focus`**
   — **not previously recorded anywhere.** `await page.wait_for_timeout(7000)` (one 5s own-tick
   poll interval plus slack) then `assert after_dismiss == []`, to prove the lane detail pane's
   self-poll was cancelled by its own dismissal. Same shape as the two SSE tests above, found by
   reading the suite rather than by a red build — which is the case for running this audit at all
   rather than waiting for the next flake to name the next instance.

#### The milder shape: asserting a positive inside a fixed window

Cold slowness here produces a visible failure, not a wrong pass — annoying, but not the dangerous
kind. Two instances:

4. **`test_analyze_lane_detail.py::test_the_open_lane_detail_refreshes_itself_without_stealing_focus_back`**
   — the same 7s wait as #3, but asserting the poll's fetch list is non-empty (the tick DID fire).
5. **`test_metadata_actions.py::test_dismissing_the_extract_confirm_enqueues_nothing`** —
   `wait_for_timeout(1000)` then `assert posted == []`, guarding a *dismissed* `hx-confirm` never
   enqueuing. The docstring already names the tradeoff ("short, and only ever paid by this one
   test"). Lower actual cold-boot exposure than it looks: whether a broken confirm boundary fires a
   request is decided by browser-side dialog handling, not a server round trip, so backend latency
   does not change when a broken implementation would have issued the request.

#### Bounded-polling helpers: a fixed ceiling, not a bare sleep

These retry on a predicate rather than sleeping blind, so they are far safer than #1-5, but they
still have a ceiling, and one of them has already produced a CI-only failure that never reproduced
locally — direct evidence, not conjecture, that this class of race is CI-sensitive.

6. **`tests/browser/helpers.py::settled_focus`** (5000ms ceiling, 100ms poll) — used across
   record/drawer dismiss-focus-restore assertions.
7. **`test_keyboard_screen_reader.py::_settle`** (2000ms default ceiling) — used for drawer and
   command-palette focus-restore. Its own comment records the precedent directly: "Failed exactly
   that way on phone/dark in CI while passing locally (phaze-bdeih is the same race in the
   drawer)."
8. **`tests/browser/conftest.py::_wait_until_serving`** (180s ceiling) — already named in
   `FLAKE_RECORD.md`; this is the boot-latency ceiling the measurement gap below is meant to fill
   with real numbers.

#### Infrastructure and session shape

9. **`tests/browser/axe.py`'s CDN fetch** is cached per Python *process* (`_SOURCE_CACHE`, a module
   dict), not per test. A fresh-process-per-attempt shape — like the one that found the
   `phaze-39eiy` flake — pays the network round trip on every attempt; a normal CI session pays it
   once.
10. **`conftest.py::live_server` is session-scoped** — see "Correction to the framing" above. This
    is the load-bearing fact for reading every other finding in this entry correctly.

#### Ruled out

- **`test_responsive_matrix.py::test_the_shell_survives_a_slow_workspace_fetch`** — its 900ms delay
  is injected client-side via `page.route`, independent of real server speed. Not cold-boot
  sensitive.
- **`test_command_palette_search.py::test_a_query_that_matches_nothing_still_offers_the_navigation_rows`**
  — has a `wait_for_timeout(1200)`, but the actual negative assertion downstream is a proper
  `wait_for_function` with a 15s timeout, not the fixed window. Not the dangerous shape.

#### Harness artifact, or product characteristic? (settled, with numbers)

`run_migrations()`, the `SELECT 1` connectivity check, and the queue/task-router/redis wiring all
run inside FastAPI's `lifespan`, in `src/phaze/main.py`, **before** `/health` returns 200. So the
literal migration-and-boot cost is front-loaded and gated behind `_wait_until_serving` — a real
operator restarting phaze would wait slightly longer before the app answers *at all*, but would not
see it as "requests are slow" once it does. That reads as a harness/deploy-timing fact, not a
per-request product defect, and needed no measurement to state.

The narrower question — whether specific request paths are measurably slower on their *first* hit
after boot than on later ones — is now measured, and the answer is **yes, there is a real,
consistent effect, and it is a product characteristic, not a harness artifact.**

**[CI-like shape: one session, boot paid once]** One `live_server`-shaped boot (paid once, matching CI's own session scope — see the correction
above), then every page route the browser suite actually exercises was hit once ("first hit") and
then nine more times on the same warm process ("steady state", mean of the last nine):

| Route | First hit | Steady-state mean | Ratio |
| --- | --- | --- | --- |
| `/health` (bare `SELECT 1`, nothing to warm) | 5.1ms | 3.9ms | 1.3x |
| `/s/summary` | 199.3ms | 42.2ms | **4.7x** |
| `/s/files` | 39.0ms | 10.3ms | **3.8x** |
| `/s/analyze` | 182.8ms | 112.0ms | 1.6x |
| `/s/rename` | 41.5ms | 18.0ms | 2.3x |
| `/s/apply` | 43.7ms | 26.3ms | 1.7x |
| `/s/audit` | 27.8ms | 14.0ms | 2.0x |
| `/pipeline/stats` | 132.4ms | 67.9ms | 1.9x |
| `/search/?q=test` (302 redirect; no real search work done) | 6.3ms | 0.7ms | 9x, but sub-10ms absolute |

The pattern is what names the mechanism: **every genuinely templated route is 1.6-4.7x slower on
its first hit; `/health`, which does nothing but a bare `SELECT 1`, barely moves at all.** That is
the signature of a compile-on-first-use cache — SQLAlchemy caching a query's compiled statement
object, Jinja2 caching a template's compiled bytecode, or both — rather than of network or database
variance, which would not spare `/health` and would not track "how much does this route render."

**Verdict: real effect, product characteristic, not absorbed here.** An operator's first click on
each distinct phaze page after a restart is measurably slower than their next click on the same
page — not dramatically (absolute deltas run 10-160ms, well under anything a person would
consciously notice, let alone report), but it is real, reproducible, and traceable to a named
mechanism rather than measurement noise. Per this bead's own scope rule, it gets its own bead
rather than a fix folded into this one; filed as **`phaze-2wxmg`** (P3, informational-with-evidence
given the modest absolute size), which carries this measurement's disk-cache caveat forward
explicitly and adds its own criterion against the tempting wrong fix: migrations already run inside
the lifespan before `/health` returns 200 (see above), so trading a ~200ms first click for a longer
restart needs arguing, not assuming. See `phaze-2wxmg` for the full finding rather than duplicating
it here.

#### Should the suite exercise the cold path deliberately? (recommendation, not adopted)

Given the session-scope correction above, the existing browser CI job already pays the true cold
cost once per run, which is representative of "CI is the cold shape" as originally stated. Adding a
second always-on cold-path lane would duplicate that rather than add coverage. What is arguably
missing is a **periodic fresh-process-per-test stress run** — the shape that actually reproduced
the `phaze-39eiy` flake — run on the `ADR-0011` bug-hunt cadence rather than on every PR, since that
is the instrument that forces the worst-case boot/cache-warming variables rather than the CI job's
steady state. This is a recommendation only; no CI change was made as part of this entry.

#### Measurement (2026-08-20, `doku9` seat, macOS/arm64)

Run once `phaze-39eiy`'s own cold-boot measurement had cleared the machine, so the two did not
corrupt each other. Standalone scripts against `TEST_DATABASE_URL`'s seat, not the pytest suite
itself, so a run could be timed and printed rather than asserted on. Boots use the same subprocess shape as
`tests/browser/conftest.py::live_server` (fresh-database-per-attempt, real uvicorn, real Alembic,
`--host 127.0.0.1`), polling `/health` exactly as `_wait_until_serving` does.

**Read this caveat before any number below — it is not a footnote.** Every "steady" figure in this
section was measured on a dev machine that had just run this app repeatedly in the same session
(`uv sync`, the full browser suite, several other measurements) — genuinely warm OS disk cache. The
first cold-boot attempt in the EXTREME-shape run below took **12.89s**; the next nine, on the same
now-warm machine, took **1.68-1.85s**. That ~7x gap is not the app behaving differently — it is
OS-level disk cache warmth for the venv and its native dependencies (essentia-tensorflow's compiled
extension among them), and discarding that first sample as noise would have been the natural thing
to do and would have been wrong. The browser CI job (`.github/workflows/tests.yml`, `browser:`)
runs on `runs-on: ubuntu-latest`: a **fresh VM every run**, with the Python venv and its native
deps installed fresh by that run's own `just install` step (only the Chromium binary is cached
across runs, via `actions/cache`). So **CI's own first boot of a run is the closer analog to this
measurement's 12.89s outlier, not to its 1.7-1.8s steady figure** — every steady-state number below
should be read as a floor (what boot costs once the OS has already paid for everything once), not
as an estimate of a fresh CI runner's first job. The exact CI number was not measured here (that
would mean running on an actual fresh `ubuntu-latest` runner, out of scope for this pass) and is
worth a follow-up if boot latency ever becomes a suspect on its own, rather than a contributor
among several.

**[Isolated component, neither shape — no uvicorn, no queue/router wiring] Migration only**
(fresh db, just `await run_migrations()`), 3 runs: **0.666s / 0.715s / 0.785s, mean ~0.72s.**

**[EXTREME shape: fresh process + fresh db every attempt] Cold boot** (the full lifespan gated
behind `/health` — migration, connectivity check, dev-agent seed, queue/task-router/redis wiring),
10 runs back to back: **12.89s** on the first (cold disk cache), **1.68-1.85s (mean ~1.74s)** on
the other nine (warm disk cache) — see the caveat above for which of those two numbers CI actually
pays, and note this is the same shape that reproduced the `phaze-39eiy` flake, not the shape CI
runs every job.

**[CI-like shape: one boot, same warm process] First-request latency, per route, first-hit vs
steady-state**: see the table in "Harness artifact, or product characteristic?" above — same
measurement run, same boot as the CI-like figure two paragraphs up.

**[process-cold vs process-warm, independent of app-boot shape] Browser launch time**: 5 runs each,
Chromium via Playwright. Cold (fresh Python process per launch): **0.272-0.303s, mean 0.279s.**
Warm (same process, five sequential launches): **0.269-0.296s, mean 0.279s.** No measurable
difference between either shape — confirms what `tests/browser/conftest.py`'s own docstring already
implies ("Playwright is launched per test" regardless of whether the app boot itself is cold or
warm), so browser launch is **ruled out** as a cold/warm differentiator; it was never one.

**Every fixed window this entry's audit named (6s / 7s / 1s / 5s / 2s / 180s) — now measured
against, not merely assumed.** This list replaces the earlier "design assumption, not a validated
bound" placeholder: each entry below is now a measured statement, not an unvalidated one, but read
the qualifier on each — this is a warm-machine result.

- **6s** (`test_execute_dispatch.py`'s two SSE reconnect windows, finding #1/#2) and **7s**
  (`test_analyze_lane_detail.py`'s own-tick poll windows, finding #3/#4) are both far larger than
  anything measured here: the largest single first-hit delta recorded was 199ms (`/s/summary`), and
  the slowest steady-state boot was 1.85s. **On a warm machine, none of these four fixed windows
  looks threatened** by the effects this pass measured.
- **1s** (`test_metadata_actions.py`'s dismissed-confirm window, finding #5) is closer to the
  measured deltas in absolute terms, but that finding was already downgraded in the audit above —
  it guards browser-side dialog handling, not a server round trip, so these latency numbers do not
  bear on it either way.
- **5s / 2s** (`helpers.py::settled_focus` and `test_keyboard_screen_reader.py::_settle`, findings
  #6/#7) are focus-restore polling ceilings, not server-latency windows; nothing measured here
  times focus-restore JavaScript, so this pass neither confirms nor threatens them directly — they
  stay exactly as risky as the audit already said, with #7's real CI-only failure precedent unchanged.
- **180s** (`conftest.py::_wait_until_serving`, finding #8) is the one this pass measured most
  directly: the slowest boot observed, cold-disk-cache included, was 12.89s — **14x margin even on
  the single worst sample seen**, cold or warm.

**None of the above is a cold-VM result, and that is the caveat that matters most.** Every number in
this list — including the 12.89s outlier — was still measured on a machine that had a filesystem
warm enough for uv, Postgres, and the Python interpreter itself to already be resident before the
measurement began. A genuinely fresh `ubuntu-latest` CI runner, with the OS, uv, and every system
library also cold, is unmeasured territory. So the honest statement is: **these fixed windows look
safe, measured warm; the cold-VM case remains unmeasured** — not that they are safe.

**What this measurement does NOT establish.** It characterizes boot and first-request latency in
isolation, on one machine, outside the actual pytest/Playwright harness and its concurrency with a
real test body, a real assertion sequence, or CPU contention from other agents. It is not a
reproduction of the `phaze-39eiy` failure and does not attempt to be — it answers "what differs,
and by how much," which is what this bead asked for; `phaze-39eiy` answers "does that difference
break this specific test." The original measurement's ~8.5s-per-iteration figure covers a full
test attempt (boot + browser launch + the test body + teardown), not boot alone, so it is not
directly comparable to the ~1.7-12.9s boot-only figures above; boot is a plausible major
contributor to that number on a cold machine, not confirmed as the sole one.

### 2026-08-20 — first-hit compile cost: mechanism confirmed, /health corrected, Jinja2 duplication closed (phaze-hqhjr)

**Status: complete.**

`phaze-doku9`'s entry above named a hypothesis and declined to prove it: "the pattern is what
names the mechanism... that is the signature of a compile-on-first-use cache... rather than of
network or database variance" — plausible from the shape of the data, not yet confirmed by looking
at what actually runs. It filed the follow-on as `phaze-hqhjr` rather than absorb it. This entry is
that follow-on: it instruments the mechanism directly, corrects a claim the earlier entry made about
its own control route, quantifies (and closes) a duplication question raised while reviewing the
draft, and answers AC2/AC3 with reasoning rather than a fix.

#### Mechanism, confirmed by instrumentation, not shape-matching (AC1)

The earlier entry inferred the mechanism from ratios: templated routes moved, the bare-SQL control
barely did. This pass instruments the two candidate caches directly — `phaze.database.engine
.sync_engine._compiled_cache` (SQLAlchemy's compiled-statement LRU) and, per router module,
`Jinja2Templates.env.cache` (Jinja2's compiled-template LRU) — and diffs each one immediately before
and after every route's first hit, in a real FastAPI lifespan against a real Postgres database
(`starlette.testclient.TestClient`, which runs the app's actual `lifespan` on `__enter__`). The
result is a direct count of newly-compiled artifacts per first hit, not an inference from timing
alone:

| route | new SQL statements compiled | new templates compiled |
| --- | --- | --- |
| `/health` | 3 | 0 |
| `/s/summary` | 23 | 10 |
| `/s/files` | 1 | 5 |
| `/s/analyze` | 20 | 1 |
| `/pipeline/stats` | 0 | 13 |
| `/s/rename` | 4 | 2 |
| `/s/apply` | 6 | 1 |
| `/s/audit` | 3 | 4 |

This is why the earlier ratio table looks the way it does: `/s/summary`'s 23 newly-compiled
statements plus 10 newly-compiled templates is why it carries both the largest ratio (4.7-5.2x
across two independent measurement passes) and the largest absolute delta; `/s/files`'s 1+5 sits
much lower on both. **Verdict: CONFIRMED by instrumentation.** The compile-count table is the answer
AC1 asked for — it shows *why* the timing table's pattern exists, not merely that it exists.

#### The /health control needed a correction

The earlier entry describes `/health` as having "nothing to warm." That claim is not literally
true, and the instrumentation above is what catches it: `/health` still compiles 3 new SQL
statements on its own first hit, not 0. The reason is a path mismatch that has nothing to do with
warm-up per se — the lifespan's own connectivity check (`src/phaze/main.py`, "Verify connectivity")
runs `async with engine.begin() as conn: await conn.execute(text("SELECT 1"))`, the Core
`Connection.execute` path, while the `/health` route handler (`src/phaze/routers/health.py`) takes
`session: AsyncSession = Depends(get_session)` and calls `session.execute(text("SELECT 1"))`, the
ORM `Session.execute` path. Identical SQL text, but SQLAlchemy's compiled-statement cache keys on
more than the literal string, and the ORM execution path constructs a different cache key than the
Core path — so the boot-time connectivity check's own compile does not pre-warm the route handler's.

This corrects the earlier entry's framing without weakening its conclusion: the difference between
`/health` and the templated routes is **degree, not kind**. 3 newly-compiled statements against
`/s/summary`'s 23 is still an order-of-magnitude-clean signal — `/health` remains the right control
to reason from, it just is not literally warm-free. A reader relying on "the control does zero
compilation" as a load-bearing fact would be relying on something false; a reader relying on "the
control compiles far less than any templated route" is on solid ground, instrumented ground now
rather than inferred.

#### Methodological trap: `TestClient`'s async-portal start-up cost

Worth recording plainly, because it produces a specific, wrong, plausible-looking number: the first
run of the in-process instrumentation above measured `/health`'s own first-hit-to-steady-state ratio
at **9-11x** — an order of magnitude worse than the timing table below, and worse than every
templated route except `/s/summary`. That number is an artifact, not a finding.
`starlette.testclient.TestClient` runs the ASGI app through a background anyio "portal" thread that
is lazily started on the client's first request, not at `TestClient.__enter__()` — so whichever
route happens to be hit first inside the `with TestClient(app) as client:` block pays a one-time
portal/event-loop start-up cost that has nothing to do with the application, and in the naive
version of this pass that cost landed squarely on `/health`, the route being used as the control.
It was caught only by cross-checking against a real-subprocess measurement (below), where `/health`
comes back flat (0.95x) as expected — not by anything internal to the in-process pass itself. The
fix used here was a throwaway warm-up request to a nonexistent path (`client.get("/__warmup_probe__")`,
a 404 that touches neither the Jinja2 nor the SQLAlchemy caches) before the timed loop starts, so
the portal cost lands there instead of on a measured route. Anyone reaching for `TestClient` to
instrument this application's first-hit behavior again will hit the same trap; this paragraph is
here so they recognize the number rather than report it.

#### Real-subprocess cross-check, on a machine verified to have zero other agent activity

The compile-count table above establishes mechanism; it does not by itself validate absolute
timing, both because of the `TestClient` artifact just described and because in-process ASGI calls
skip the real TCP round trip a browser or `httpx`-over-the-network pays. So the original
real-uvicorn-subprocess method — same shape as `tests/browser/conftest.py::live_server` (fresh
database, real uvicorn, real Alembic, `--host 127.0.0.1`), first hit vs. mean of nine steady-state
hits — was re-run, this time on a machine the dispatcher held genuinely idle for the duration (no
other agent's test runs, verified before and after), which the original 2026-08-20 pass above could
not claim about its own machine state:

| route | first hit | steady-state mean | ratio (this pass) | ratio (original `doku9` pass, above) |
| --- | --- | --- | --- | --- |
| `/health` | 6.5ms | 6.8ms | 0.95x | 1.3x |
| `/s/summary` | 219.1ms | 42.0ms | 5.21x | 4.7x |
| `/s/files` | 34.0ms | 8.7ms | 3.91x | 3.8x |
| `/s/analyze` | 147.3ms | 112.4ms | 1.31x | 1.6x |
| `/pipeline/stats` | 128.7ms | 66.3ms | 1.94x | 1.9x |
| `/s/rename` | 34.9ms | 15.2ms | 2.30x | 2.3x |
| `/s/apply` | 40.6ms | 20.7ms | 1.96x | 1.7x |
| `/s/audit` | 23.9ms | 11.3ms | 2.11x | 2.0x |

Every route reproduces within noise, on a machine that this time can genuinely rule out "another
agent's load leaked into the numbers" — a caveat the original pass could not close. `/health`'s
ratio here (0.95x, i.e. flat-to-negative) confirms it as a clean timing control despite the
statement-cache correction above: the 3-statement compile cost measured directly is real but too
small to surface over run-to-run noise at the wall-clock level, which is exactly what "degree, not
kind" predicts.

#### The Jinja2-duplication question, quantified and closed

Reviewing this bead's draft findings raised a sharper question than "first hits are slow": since
`Jinja2Templates(directory=...)` is instantiated as a separate module-level global in each of 11
router files (`src/phaze/routers/{shell,tags,duplicates,execution,record,search,pipeline_scans,
routing,proposals,admin_agents,cue}.py`, plus `pipeline/_common.py`), each gets its own `Environment`
and its own independent template-bytecode cache — even though **all 11 point at the same source
directory**, `src/phaze/templates/`. If the same partial file is rendered by more than one of these
routers, it is compiled once per environment that renders it rather than once for the whole process,
which would mean some of the first-hit cost is duplicated work, not merely unavoidable work — a
different problem with a different fix (consolidating to one shared `Environment` rather than a
warm-up).

A static closure analysis (`{% extends %}` / `{% include %}`, regex-parsed, walked from every
router's `TemplateResponse(name=...)` call to its full transitive template set) over all 121
template files under `src/phaze/templates/` found **6 files (~5%) compiled by more than one
router's `Environment`**: `pipeline/partials/_diff_row.html` (proposals, shell, tags — 3
environments), `proposals/partials/analysis_timeline.html` and `proposals/partials/row_detail.html`
(proposals, record), `pipeline/partials/_changes_list.html` (proposals, shell),
`pipeline/partials/_stage_pill.html` (record, shell), and
`shell/partials/_routing_override_warning.html` (routing, shell). The runtime compile-count table
above corroborates one of these directly: `shell/partials/_routing_override_warning.html` shows up
as newly compiled in `shell`'s own environment on `/s/summary`'s first hit, and it is separately
reachable from `routing.py`'s own `TemplateResponse` calls — a real, observed duplication, not just
a statically-possible one.

**This is a lower bound, stated as one rather than left implicit.** The regex closure misses one
known dynamic include — `shell/_stage_fragment.html` contains `{% include stage_partial %}`, a
Jinja2 variable rather than a literal string, which the static graph cannot resolve. `stage_partial`
cycles through several `pipeline/partials/*_workspace.html` files that already appear in more than
one router's `TemplateResponse` set, so the true shared-template count is plausibly a little higher
than 6; chasing the exact figure was out of scope for the time available.

**Verdict: real, but negligible — not worth a separate bead.** 6 of 121 files is 5% of the template
corpus, all of them small partials rather than page-level templates, and the compile-count table
shows each route's first-hit cost is dominated by its own non-shared templates (`/s/summary`'s 10
newly-compiled templates include only one or two members of the shared set). Consolidating 11
independent `Environment` instances that share a directory into one shared `Environment` is a real
refactor with real blast radius — every router's module-level `templates` global is used at every
render call site in that file — and the evidence here does not support that cost for a
low-single-digit-millisecond saving repeated across two or three environments. The question is
closed with a number rather than left as an open suspicion for someone to re-discover.

#### AC3 — decided: document only, no start-up warm-up

Weighed explicitly against AC4's constraint (the lifespan already gates `/health`'s 200 behind
migrations and connectivity — see "Harness artifact, or product characteristic?" above — so anything
added there extends the window before the app answers *at all*). A warm-up cheap enough to be worth
that trade does not exist here: the mechanism confirmed above is not one lazy singleton but eleven
independent Jinja2 environments (each would need every template it will ever render walked through
`env.get_template()` once) plus the ORM-vs-Core SQLAlchemy cache-key split identified above (meaning
a warm-up would need to exercise the same `Depends(get_session)` path every route actually uses, not
just the lifespan's own Core-path connectivity check, to be effective). That is real engineering
surface purchased for a benefit only the first click after a restart ever pays, on a single-operator
home-server tool where restarts are infrequent. **Decision: document as a known characteristic; do
not add a warm-up.** Anyone re-proposing a warm-up should engage this argument specifically rather
than re-derive it.

#### AC2 — the genuinely cold machine question remains open, by design

Not measured, and deliberately not faked. Simulating a cold OS page cache locally (e.g. macOS
`purge`, which needs elevated privileges on this machine and would not be a faithful analog to a
fresh Linux `ubuntu-latest` VM's page cache regardless) would produce a number that looked like the
gap was closed without actually closing it — worse than leaving it open, per this bead's own
"measure, do not extrapolate" instruction. The only faithful way to answer this is a timing capture
added to an actual `ubuntu-latest` CI run, which is deliberately **not** folded into this P3 bead —
if that answer is wanted, it should be its own scoped piece of work rather than smuggled into a
browser CI job change here. `phaze-doku9`'s own caveat stands unchanged: the closest available
analog remains its measured 12.89s cold-disk-cache boot against 1.68-1.85s warm, and every first-hit
ratio in both tables above should be read as a warm-machine floor, not an upper bound.

#### What changed between this entry and the one above it

No production code changed (AC6). The instrumentation used to produce the compile-count table and
the two timing tables was throwaway (ad hoc scripts run against an isolated `test-db-for` seat, not
committed) — this document is the durable artifact. Net effect of this bead: the mechanism is now
confirmed rather than inferred, the control route's own claim is corrected, a duplication question
is answered with a number instead of left open, and the tempting warm-up fix is argued against
explicitly rather than defaulted away from.

## Consequences

- Desktop behaviour is unchanged; the expanded rail and full-density tables are preserved.
- Phone navigation costs one tap to open the drawer, accepted in exchange for legible labels.
- The guards are structural, so they run in milliseconds and cannot be satisfied by a mocked render.
- Adding a template with a bare `vh`, an ungated animation, an unwrapped table, or an unnamed icon
  button fails the suite immediately, naming the file and line.
