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

**Status: audit complete (below); the measurement table is a known gap, tracked in the same bead.**

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

#### Harness artifact, or product characteristic? (partial answer)

`run_migrations()`, the `SELECT 1` connectivity check, and the queue/task-router/redis wiring all
run inside FastAPI's `lifespan`, in `src/phaze/main.py`, **before** `/health` returns 200. So the
literal migration-and-boot cost is front-loaded and gated behind `_wait_until_serving` — a real
operator restarting phaze would wait slightly longer before the app answers *at all*, but would not
see it as "requests are slow" once it does. That much reads as a harness/deploy-timing fact, not a
per-request product defect, and needs no further measurement to state.

The open question is narrower and still needs Phase 2's numbers: whether specific request paths
are measurably slower on their *first* hit after boot than on later ones — a SQLAlchemy
statement-cache miss, a Jinja2 template-compile miss, or some other lazy singleton initialized on
first use. Nothing in the routers this audit read does an obviously heavy first-call lazy
initialization on the request path these browser tests exercise (essentia-tensorflow model loading
is SAQ-worker-side, not touched here), so this audit could not confirm or refute the effect from
static reading alone. **If it is real, it is a product finding, not a harness one** — an operator's
first few clicks after every phaze restart would be measurably slower — and it belongs in its own
bead, not folded into this one.

#### Should the suite exercise the cold path deliberately? (recommendation, not adopted)

Given the session-scope correction above, the existing browser CI job already pays the true cold
cost once per run, which is representative of "CI is the cold shape" as originally stated. Adding a
second always-on cold-path lane would duplicate that rather than add coverage. What is arguably
missing is a **periodic fresh-process-per-test stress run** — the shape that actually reproduced
the `phaze-39eiy` flake — run on the `ADR-0011` bug-hunt cadence rather than on every PR, since that
is the instrument that forces the worst-case boot/cache-warming variables rather than the CI job's
steady state. This is a recommendation only; no CI change was made as part of this entry.

#### Measurement gap (tracked, not yet filled)

`_wait_until_serving` duration, first-request latency, migration time, and browser-launch time,
cold versus warm, with real numbers — the direct ask of this bead's first acceptance criterion —
are **not yet measured**. Measurement was deliberately deferred: `phaze-39eiy` was running its own
cold-boot failure-rate measurement on this machine at the time of this audit, and a second cold-boot
loop running concurrently would have added load and corrupted both processes' numbers. This section
is the place that measurement lands once it runs; until then, treat every duration named above
(6s, 7s, 1s, 5s, 2s, 180s) as a design assumption, not a validated bound.

## Consequences

- Desktop behaviour is unchanged; the expanded rail and full-density tables are preserved.
- Phone navigation costs one tap to open the drawer, accepted in exchange for legible labels.
- The guards are structural, so they run in milliseconds and cannot be satisfied by a mocked render.
- Adding a template with a bare `vh`, an ungated animation, an unwrapped table, or an unnamed icon
  button fails the suite immediately, naming the file and line.
