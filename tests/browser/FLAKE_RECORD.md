# Browser suite flake record

Tracks the evidence needed to promote the `browser` CI job from `continue-on-error: true` to
blocking. Owned by `phaze-8p1uq`; the gate itself is stated in ADR-0009 (§ "The browser contract
suite") and in `.github/workflows/tests.yml`'s `browser:` job comment.

## Why the job is non-blocking today

A brand-new browser suite has no flake record, and gating merges on an unproven suite trains
everyone to re-run CI on red — which is exactly how a real failure gets waved through. The bar is a
**clean run of 10 consecutive post-merge CI runs**.

## Status: NOT MET — 0 of 10 CI runs (count reset 2026-08-20)

Epic `phaze-tzy6s` merged to main and the `browser:` job has been running post-merge since
2026-08-18. The gate has been measured, reset, and re-measured since; see "Counting convention"
below for how to read a run, and `phaze-8p1uq`'s comment history for the full per-run audit trail.

As of 2026-08-20, the count is **0 of 10**, reset three times since an earlier 8/10 checkpoint:
twice by an infrastructure failure (the "Install the Chromium build" step failing on
2026-08-19, runs `32227865905` and `32274358220`), then by a real product regression (three
consecutive failures of `test_analysis_timeline.py::test_timeline_inspects_with_pointer_touch_and_keyboard_and_cues_overflow`,
traced to `3e2557bd`). That regression's fix merged as `e0b72d43`; the count restarts at the first
post-fix run, `32404559677` on `827052af`, which was **green**.

That regression is the argument for this whole record, and it is worth stating plainly: the suite
found a **real product defect on the platform CI runs and developers do not**. It reproduced 3/3 in
CI and never once locally, and it was fixed rather than re-run. That is exactly the outcome the
"do not promote on local evidence alone" bar was written for — the local 5/5 below could not have
found it, because macOS overlay scrollbars are precisely what hid it.

**Do not flip `continue-on-error` on the strength of the local runs below.** They are evidence that
the suite is not flaky *on a developer machine*, which is the easy half. CI is where the timing is
different, and CI is what the bar is measured against.

## Counting convention

Two agents independently converged on this reading of the bar before it was written down here;
this section states the existing judgement, it is not new policy. Apply all five rules together
when updating the count.

1. **Count the browser job's own conclusion, never the workflow conclusion.** The job runs with
   `continue-on-error: true`, so a *failing* browser job still reports a *green* workflow — reading
   the workflow conclusion has produced false-green counts in practice. Read the job itself, per
   run: `gh api repos/SimplicityGuy/phaze/actions/runs/<id>/jobs`, job name
   `test / Browser contract (non-blocking)`.
2. **Count main-branch post-merge runs only.** PR runs and branch runs do not count toward the 10 —
   the bar is about what the suite does against integrated main.
3. **A run where the job did not execute is neutral** — excluded from the sequence entirely, neither
   advancing nor resetting the count. Two cases produce this: a docs-only push where
   `detect-changes` skips the whole test job, and a run cancelled by the concurrency group when a
   newer push supersedes it. Neither carries any signal about suite health. Measured 2026-08-20:
   5 of the last 40 main runs (~12%) did not run the browser job at all — routine, not an edge case.
4. **An infrastructure failure still resets the count.** An executed run that comes up red — even
   for a reason unrelated to the suite's own code, such as the Chromium install step failing before
   any test runs — is not a clean run, and the bar is "the browser job is reliably green on main."
   Record the cause alongside the reset so a reader can tell infrastructure flake from product
   regression at a glance, rather than re-deriving it from CI logs later.
5. **A red run must be classified, not just counted.** `phaze-17ni3` (traces + failure artifact
   upload, landed 2026-08-18 via PR #462) is the precondition that makes this possible — without it
   a red run cannot be told apart from a flake, and the count cannot be honestly maintained.

### The gate has already been met once, and lost unobserved

The browser job ran 28 consecutive executed-and-green runs across 2026-08-18/19 with nobody
counting live; the window opened and closed before anyone applied the rules above to it.
Reconstructing the count after the fact costs a full agent round each time it is asked, and can
miss a window entirely if the audit doesn't happen to land inside it. A CI step that appended each
run's classification to this file as runs happen — rather than an agent auditing `gh api` by hand
on request — would make this record self-maintaining and remove that risk; worth doing as part of
closing this bead rather than repeating the manual audit a third time.

## Local runs

Recorded 2026-08-17 on branch `refactor/code-quality-decomposition`, worktree seat `wt8p1uq`,
macOS/arm64, against the shared `just test-db` harness (Postgres 5433, Redis 6380 DB 36).

| Run | Result | Wall clock |
|-----|--------|-----------|
| 1 | 59 passed, 11 xfailed | 84.9 s |
| 2 | 59 passed, 11 xfailed | 84.4 s |
| 3 | 59 passed, 11 xfailed | 82.9 s |
| 4 | 59 passed, 11 xfailed | 89.0 s |
| 5 | 59 passed, 11 xfailed | 84.4 s |

**5/5 green, 0 flakes, spread 82.9–89.0 s (7.3%).** The 11 `xfailed` are two recorded, strict
known-failures — WCAG AA contrast and the palette listbox's `role="status"` child — not
instability; see `test_accessibility.py`. Because they are `strict=True`, a run reporting `xpassed`
is a *product fix*, not a flake, and the response is to delete the marker rather than to re-run.

## What to watch when the CI runs start

Ranked by how likely each is to produce a CI-only failure. None of these produced a local flake;
they are listed because local timing is the wrong instrument for all of them.

1. **The axe CDN fetch** (`tests/browser/axe.py`). One network round trip per pytest process,
   digest-verified, cached for the session. It is the suite's only hard dependency on a host other
   than the app itself, and therefore the first suspect for a run that fails once and passes on
   re-run. If it flakes, vendor the bundle rather than adding a retry — a retry hides an outage
   behind a slower green.
2. **The SSE reconnect windows** (`test_execute_dispatch.py`). Two tests sleep 6 s to prove the
   EventSource did *not* reconnect. That is a lower bound on Chromium's ~3 s reconnect delay with
   generous margin, but it is wall-clock reasoning, and a heavily loaded runner is where wall-clock
   reasoning breaks. A failure here reads as "the stream reconnected" and would be a REAL defect
   (phaze-047gd) — do not lengthen the window without first checking the request log in the failure
   output, which names how many connections were made.
3. **The 5 s stats poll** (`test_live_refresh_and_states.py`). Waits up to 20 s for a poll tick, so
   it tolerates three missed ticks. Generous, but it is the only assertion whose success depends on
   a timer the test does not control.
4. **App boot** (`conftest._wait_until_serving`, 180 s). Runs migrations from an empty database on
   every session. Comfortable locally; unmeasured on a GitHub runner.
5. **Postgres/Redis service readiness.** The CI job gives both health checks, and the browser
   database is derived by appending `_browser`, so it never collides with the unit matrix — which
   runs in a different job with its own services.

## Promotion procedure

1. Merge epic `phaze-tzy6s` to main so the `browser:` job starts running post-merge. (Done,
   2026-08-18 — see "Status" above.)
2. Record each post-merge run below, applying the counting convention above — **run number, SHA,
   result, duration, and (for a reset) the cause**. A run where the job did not execute is neutral
   and is noted but does not consume a row in the consecutive count. A run that fails resets the
   count to zero; note the cause before resetting, since "reset without a diagnosis" is how a
   genuine recurring defect gets laundered into a flake statistic.
3. On 10 consecutive green runs, delete `continue-on-error: true` from the `browser:` job in
   `.github/workflows/tests.yml`, and update its comment plus ADR-0009 §"The browser contract
   suite" to say the gate was met, with the date and the run range.
4. Consider dropping `(non-blocking)` from the job's display name in the same change — a blocking
   job labelled non-blocking is worse than either.

### CI run log

| # | SHA | Result | Duration | Notes |
|---|-----|--------|----------|-------|
| — | `6ca6bbb3` | 1 failed, 161 passed, 1 skipped | 304.2 s | run 32337835166 — scrollbar-gutter defect (below) |
| — | `393b15d8` | 1 failed, 161 passed, 1 skipped | 363.1 s | run 32387112841 — same |
| — | `98446be3` | 1 failed, 161 passed, 1 skipped | 259.9 s | run 32389611422 — same |
| 1 | `827052af` | 162 passed, 2 skipped | 273 s | run 32404559677 — first post-fix run, **green**. Count restarts here. |

The count restarts at run `32404559677`. Full per-run history through 2026-08-20 — including the
28-run window that met and then lost the gate unobserved — is in `phaze-8p1uq`'s comment thread;
this table is the live log going forward and should be kept current rather than reconstructed
from bead comments each time.

### Diagnosis of the first three failures — a real defect, not a flake

All three failed identically, in
`test_analysis_timeline.py::test_timeline_inspects_with_pointer_touch_and_keyboard_and_cues_overflow`:

```
AssertionError: assert '←' in 'SCROLL TIMELINE ↔'
```

Scrolled fully right, the timeline still advertised room to the right. **Cause:**
`.analysis-timeline-viewport` carried `scrollbar-gutter: stable`, which reserves inline-end space
for a vertical scrollbar — space `overflow-y: hidden` guarantees will never be used. A reserved
gutter is excluded from `clientWidth` but cannot be scrolled into, so `analysis_timeline.js`'s
`scrollWidth - clientWidth` **overstated the maximum scroll offset by the gutter width** and
`canRight` could never reach false.

Measured on Ubuntu/Chromium 151 (the CI browser), 1440×900, on the page the test builds:

| | `scrollWidth` | `clientWidth` | `scrollWidth - clientWidth` | offset `scrollLeft` actually clamps to |
|---|---|---|---|---|
| Linux (classic scrollbars) | 1344 | 1077 | 267 | **252** |
| macOS (overlay scrollbars) | 1344 | 1092 | 252 | 252 |

macOS reserves a 0px gutter, so the arithmetic is accidentally right there and the suite stayed
green on every developer machine. **Fix:** drop the gutter (it was pure cost), and compute the
maximum from the padding box so a reserved gutter can never make the affordance lie again.

Note what this says about the "what to watch" list above: all five entries are about *timing*, and
none of them could have predicted this. CI differs from a developer machine in **what it renders**,
not only in how fast it does so — classic vs overlay scrollbars, font metrics, form controls. A
CI-only browser failure is not presumptively a flake; check the rendering axis first, because that
class of failure reproduces 10/10 and is a real defect every time.

**Reproducing a Linux-only browser failure without a Linux box.** The job uploads a Playwright
trace on failure (artifact `browser-test-results`); its DOM snapshots carry the recorded
`__playwright_scroll_left_` and class list, which is what localised this to the scroll arithmetic.
From there, serving the captured page to `mcr.microsoft.com/playwright/python:v1.62.0-noble`
reproduces the geometry exactly, in seconds, with no app boot. Reach for that before assuming a
timing flake — none of the five suspects listed above was involved, and CPU throttling to 20×
never reproduced it, because it was never a race.
