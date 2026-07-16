---
status: passed
verified: 2026-07-16
---

# Phase 95 — End-to-End Scale Verification (phaze-zqvh.5)

Final verify-bead for the Phase 95 epic (`phaze-zqvh`, CONSOLE-04). Closes the loop the
`phaze-zqvh.1` baseline (`95-BASELINE.md`, frozen — not edited here) left open: a REAL headless
Chromium against the LIVE `/s/analyze` route, at 200K-corpus scale, with all three fix beads
(`.2` bounded working set, `.3` idempotent lane-grid swap, `.4` stats fan-out) merged. Verdict
against all four ROADMAP phase-95 success criteria, below.

## Method

**Server-side** — identical harness/corpus as `95-BASELINE.md` / `95-STATS-BUDGET.md`: the Phase
82 PERF-02 bench harness (`scripts/perf_analyze_workspace.py`, unmodified) against the same
dedicated perf Postgres (`phaze-perf-db`, port 5545, database `phaze_perf82`, seeded ~200K-file /
92,335-row-Analyze-membership corpus, unchanged), same env exports:

```bash
export PHAZE_DATABASE_URL=postgresql+asyncpg://phaze:phaze@localhost:5545/phaze_perf82
export PHAZE_QUEUE_URL=postgresql://phaze:phaze@localhost:5545/phaze_perf82
export PHAZE_REDIS_URL=redis://localhost:6380/0
uv run python scripts/perf_analyze_workspace.py \
    --dsn postgresql://phaze:phaze@localhost:5545/phaze_perf82 --iterations 10
```

Note: under `httpx.ASGITransport` (no lifespan startup), a handful of optional `app.state`
attributes the harness doesn't wire (`task_router`, `controller_queue`, the pipeline-counters
Redis client) raise inside try/except-guarded call sites and are logged, not raised to the
caller — this is pre-existing harness behavior (the same shape `95-BASELINE.md`/`95-STATS-BUDGET.md`
ran under) and does not affect the `get_analyze_working_set` / `GET /s/analyze` / `GET
/pipeline/stats`-payload-size numbers below, which don't touch those attributes.

**Browser-side (new, this bead)** — `scripts/analyze_browser_soak.py`, a standalone `uv run
--with playwright` companion driving a real headless Chromium against a LIVE `uvicorn
phaze.main:create_app --factory --port 8123` process pointed at the same perf DB (env identical
to the server-side method above, plus a running Postgres/Redis pair — no product code touched;
read-only GET/navigation traffic only). It measures OPEN (navigation timing + JS heap),
behavior-preservation (poll-driven lane updates, record drill-in, filter/pager survival across a
poll tick, lane-grid DOM-identity stability), residual-size interaction responsiveness, and a
>=30-minute heap/long-task soak. Every soak sample is printed with `flush=True` AND appended as
one JSON line to an on-disk log as it is taken (not buffered to a single end-of-run write) so a
mid-soak crash would not lose completed samples; the whole run is wrapped in a top-level
try/except that prints a full traceback before a nonzero exit. A 2-minute smoke run was executed
and its output verified well-formed before the real >=30-minute soak was started.

```bash
uv run --with playwright playwright install chromium   # once
uv run --with playwright python scripts/analyze_browser_soak.py \
    --base-url http://127.0.0.1:8123 --soak-minutes 31 --sample-interval-seconds 120 \
    --out <report>.json   # samples also stream incrementally to <report>.json.jsonl
```

The dev instance was already running against the perf DB for this bead (inherited from the prior
session); its `curl -o /dev/null -w '%{http_code}'` health check returned `200` before and after
every measurement pass below, and both the smoke run and the full soak completed cleanly (browser
+ context closed, script exit 0) with no stray Chromium/uvicorn processes left behind.

## Headline numbers — before / after

| Measurement | BEFORE (`95-BASELINE.md`, unbounded table) | AFTER (this bead, bounded working set) |
|---|---|---|
| `get_analyze_*` DIRECT (row read only) | `get_analyze_stage_files`: p50 ~750-850ms, **92,335 rows** | `get_analyze_working_set`: p50 **420.7ms**, p95 **537.9ms**, **13,052 rows** |
| `GET /s/analyze` server render (wall-clock) | p50 **~4.8-5.3s** | p50 **2045.9ms**, p95 **2250.5ms** (≈**2.0s**) |
| `GET /s/analyze` response payload size | **~105 MB** (104,963,282 bytes) | **~13.2 MB** (13,890,713 bytes) |
| `GET /s/analyze` DOM row count (server) | **92,335** rows | **13,052** rows |
| `GET /pipeline/stats` OOB fragment size (per 5s tick) | **11,956 bytes** (~12 KB) | **12,005 bytes** (~12 KB, unchanged — confirms it never scaled with corpus size, before or after) |

**Browser (new this bead — the `95-BASELINE.md` gap):**

| Measurement | Value |
|---|---|
| `GET /s/analyze` navigation, wall-clock to `load` | **4116.0 ms** |
| `domContentLoadedEventEnd` (from nav start) | 4065.0 ms |
| `domInteractive` | 3187.3 ms |
| First idle after load (`requestIdleCallback` proxy for time-to-interactive) | **4134.6 ms** |
| JS heap used, immediately after load | **83,895,468 bytes** (~80.0 MB) |
| Rendered `<tr>` row count (client DOM) | **13,052** (matches server row count exactly) |
| Rendered lane-card count | 1 |

**Interpretation against success criterion 1**: opening the Analyze workspace at 200K-corpus
scale now costs ~4.1s wall-clock to `load` (browser-measured, includes network+parse+render+
htmx/Alpine init) and reaches idle ~4.13s after nav start, with an ~80MB JS heap footprint — a
single, bounded, sub-5-second open with no hang, down from a baseline whose SERVER render alone
was ~4.8-5.3s before the browser even started parsing a 105MB response. **PASS** — opens without
severe slowdown or hang.

## Soak — >=30 minutes, JS heap + long-task sampling every 2 minutes

Full 31.0-minute soak, 17 samples at 120s intervals (`t=0.0s` through `t=1860.0s`), sampled via
CDP `Performance.getMetrics` (`JSHeapUsedSize`) + a buffered `PerformanceObserver` on
`longtask` entries, against the default bounded working-set view (13,052 rows) with the 5s
`/pipeline/stats` poll running the whole time (**375** poll requests observed over 1860s ≈ one
every 4.96s — matches the 5s cadence exactly, confirming the single-poll architecture (WORK-05)
held for the whole soak).

| Sample | t (s) | JS heap (bytes) | Long-task ms since last sample | Long-task count |
|---|---|---|---|---|
| 0 | 0.1 | 210,957,912 | 1149.0 | 5 |
| 1 | 120.1 | 80,280,376 | 793.0 | 15 |
| 2 | 240.1 | 80,380,764 | 203.0 | 4 |
| 3 | 360.1 | 78,744,212 | 423.0 | 8 |
| 4 | 480.1 | 79,565,652 | 416.0 | 8 |
| 5 | 600.1 | 80,418,896 | 470.0 | 9 |
| 6 | 720.2 | 79,180,816 | 373.0 | 7 |
| 7 | 840.2 | 80,034,920 | 313.0 | 6 |
| 8 | 960.2 | 78,709,916 | 311.0 | 6 |
| 9 | 1080.2 | 79,632,448 | 370.0 | 7 |
| 10 | 1200.2 | 80,436,760 | 226.0 | 4 |
| 11 | 1320.2 | 79,114,440 | 261.0 | 5 |
| 12 | 1440.2 | 80,255,932 | 374.0 | 7 |
| 13 | 1560.2 | 79,041,788 | 572.0 | 11 |
| 14 | 1680.2 | 80,114,348 | 327.0 | 6 |
| 15 | 1800.0 | 78,619,176 | 213.0 | 4 |
| 16 | 1860.0 | 79,013,748 | 209.0 | 4 |

**Summary** (computed by the script's `_summarize_soak`, first-half vs second-half comparison):

- Heap: min **78,619,176** / max **210,957,912** bytes (the max is sample 0, taken seconds after
  the record-drill-in/filter/pager behavior checks that immediately preceded the soak — a
  one-time settling artifact of the prior interactions' DOM churn, not a soak trend; by sample 1
  the heap has already dropped to the steady ~79-80MB band it holds for the remaining 30 minutes).
  First-half mean **96,195,443 bytes**, second-half mean **79,437,617 bytes** — a growth ratio of
  **0.826** (i.e. the second half is SMALLER, not larger — GC caught up on sample 0's transient
  spike and the heap stayed flat after). `heap_flat_pass: true` (generous <1.5 bound, satisfied
  with wide margin by an actually-*decreasing* trend).
- Long tasks: first-half mean **517.5 ms**/interval, second-half mean **318.1 ms**/interval — also
  decreasing, not growing. `long_task_not_growing_pass: true`.

**PASS** — flat memory (in fact trending down after the one-time post-interaction settle), no
growing long-task cost, single-poll architecture intact for the full 31-minute run. Satisfies the
epic's >=30-minute soak requirement with no progressive degradation.

## Behavior preservation checklist (success criterion 4)

Checked with a real browser against the running 200K-scale instance, in this order (each check's
raw values embedded, not referenced by path):

| Check | Result |
|---|---|
| 5s poll fires on natural cadence (2+ ticks observed in a ~10.5s idle wait) | **PASS** — 2 ticks observed |
| `#analyze-lanes` grid DOM-node identity stable across idle poll ticks (phaze-zqvh.3 hash-skip) | **PASS** — a JS marker property set on the element survived 2 poll ticks untouched |
| `#analyze-lanes` `data-lanes-hash` unchanged across idle ticks (`d04f5bcb0a40ba89` → `d04f5bcb0a40ba89`) | **PASS** — idempotent-swap confirmed at 200K scale, not just unit-tested |
| Per-file row drill-in opens `/record/{id}` into the slide-in (`role=dialog[aria-modal=true]`) | **PASS** — dialog opened, body populated |
| Per-row State cell never renders hue-only (WCAG 1.4.1 — every visible row carries a status word) | **PASS** — all 200 sampled cells nonempty |
| Status filter bar (`#analyze-filter-status`) switches to `failed` and re-renders a bounded page | **PASS** — 50 rows (one bounded page) |
| Pagination controls present + `Next` advances to "Page 2" | **PASS** |
| Filter selection survives a poll tick (not reset by the 5s tick) | **PASS** |
| Page position survives a poll tick | **PASS** |

All behavior-preservation checks from the epic's success criterion 4 **PASS** at 200K scale with
a real browser, not just server-side unit tests.

**One sub-check is INCONCLUSIVE, not failed** — see the residual finding below (windowed-progress
`N/M windows` text form).

## Residual-size finding (dispatcher flag: the ~13,052-row default working set)

The default bounded view here renders **13,052 rows** (the working set + bounded completions
window this corpus resolves to). Interaction responsiveness was measured directly at this size,
immediately following the soak's steady-state:

| Interaction | Latency |
|---|---|
| Full-page scroll-to-bottom-and-back (rAF round trip) | **3.7 ms** |
| Click a row → record slide-in body populated | **210.5 ms** |
| Switch the status filter (`in_flight`) → new page rendered | **444.6 ms** |

**No jank observed at this working-set size** — scroll is imperceptible, click-to-open and
filter-switch are both sub-500ms (htmx round-trip + Alpine dispatch, not raw client cost — the
13,052-row DOM itself is not the bottleneck at this size). This confirms the bounded-working-set
design (phaze-zqvh.2) leaves headroom: even the "active + recent" default view, at ~14% of the
original unbounded 92,335-row table, interacts smoothly. **No follow-up bound-the-failed-set work
is indicated by this measurement** — record it as a clean pass, honestly, rather than assuming a
problem the numbers don't show.

### Windowed mid-flight progress form — corpus-limited, not code-limited

`_analyze_files.html:98` renders `running · {fine_done}/{fine_total} windows` for an in-flight
file only when `f.fine_total` is truthy; otherwise it falls back to plain `running`. Sampling the
first 200 rows of the default view, and separately paging through 5 full pages (~140 rows) of the
`in_flight` status filter, **every in-flight row rendered plain `running`** — the `N/M windows`
form was not observed anywhere in this corpus. This is a **seed-data limitation, not a
regression**: the synthetic perf corpus's in-flight rows do not populate
`fine_windows_analyzed`/`fine_windows_total` on the `analysis` row, so the template branch that
renders the windowed form is simply never reached by this dataset — the code path itself was
read directly (`src/phaze/services/pipeline.py:1333-1356` projects `fine_done`/`fine_total`
through unchanged; `_analyze_files.html:88-99` is unchanged since Phase 57/61) and is not part of
any Phase 95 fix bead's diff. Recorded honestly per the dispatcher's flag: **the windowed-progress
signal's presence at 200K browser scale is UNVERIFIED by this measurement (corpus gap), not
disproven** — a fast-follow could re-seed a handful of in-flight rows with nonzero
`fine_windows_total` to close this specific gap, but it is out of scope for this verify bead
(no product code or seed script was touched here, per the read-only method).

## Root-cause narrative (final, consolidating `95-BASELINE.md` + `95-STATS-BUDGET.md` + this bead)

1. **The dominant root cause was the client-side render of an unbounded per-file table**
   (`95-BASELINE.md`): every Analyze-stage file (92,335 rows at 200K scale, monotonically
   growing) got one `<tr>` with htmx/Alpine bindings, server-rendered inline on every `/s/analyze`
   open — ~105MB HTML, ~5s server render, before the browser even began parsing. **phaze-zqvh.2**
   fixed this at the source: an active-first bounded working set (in-flight/awaiting-cloud/failed
   + a bounded recent-completions window) plus server-paginated full listing behind the existing
   status filter bar. Measured here: 92,335 → 13,052 rows (-86%), ~105MB → ~13.2MB payload (-87%),
   ~4.8-5.3s → ~2.0s server render (-58 to -62%).
2. **The compounding, soak-visible half was the 5s poll's per-tick DOM churn**
   (`95-BASELINE.md`'s reasoned-not-measured estimate): destroy-and-recreating ~35 `x-init` seed
   nodes + the whole `#analyze-lanes` grid every tick, against an already-large DOM, forced
   layout/reactive-fanout cost every 5 seconds regardless of whether lane state actually changed.
   **phaze-zqvh.3** made the `#analyze-lanes` swap idempotent (server-computed content hash,
   skip-if-unchanged). Measured here with a REAL browser (closing the baseline's one estimated,
   not measured, gap): across a 31-minute soak with the 5s poll firing 375 times, the lane grid's
   DOM-node identity and `data-lanes-hash` were both provably stable across idle ticks, and heap +
   long-task cost trended FLAT-TO-DOWN, not up. The estimated degradation path is now measured and
   closed.
3. **The stats-poll endpoint itself was investigated and is NOT the primary driver**
   (`95-STATS-BUDGET.md`): `/pipeline/stats` p50 was 1099.5-1147.1ms across three independent
   200K measurements (baseline, before, after `phaze-zqvh.4`'s fan-out fix), close to but not
   dramatically over the ~1s soft budget (D-07), and its OOB fragment payload is small
   (~12KB) and provably corpus-size-independent (11,956 bytes baseline → 12,005 bytes here,
   effectively unchanged). **DENORM-01 stays deferred** — this bead's measurements are consistent
   with, and do not revise, that decision.

## Verdict against ROADMAP phase-95 success criteria

1. **"Opening the Analyze workspace at corpus scale does not severely slow or freeze the
   browser."** — **TRUE.** Real-browser measurement: ~4.1s to `load`, ~4.13s to first idle, ~80MB
   heap, 13,052 rendered rows (matching the server's bounded count exactly). No hang, no freeze.
2. **"The slowdown root cause is identified ... and recorded in the phase artifacts."** —
   **TRUE.** Recorded across `95-BASELINE.md` (identification), `95-STATS-BUDGET.md` (poll-budget
   verification), and this artifact (root-cause narrative + browser confirmation of the
   soak-degradation half, closing the one previously-estimated gap).
3. **"If the root cause traces to the stats poll, the DENORM-01 deferral is revisited ...;
   otherwise the client-side render/poll cost is bounded at the source."** — **TRUE.** Root cause
   did NOT trace primarily to the stats poll (`95-STATS-BUDGET.md`); the client-side cost was
   bounded at the source on both axes — the per-file table (`phaze-zqvh.2`) and the per-tick poll
   churn (`phaze-zqvh.3`) — and DENORM-01's deferred status was formally revisited and recorded
   with numbers either way.
4. **"Existing Analyze workspace behavior ... is preserved."** — **TRUE**, with one honestly-flagged
   corpus-data gap. Lane cards render/update on the poll, per-file rows drill into `/record/{id}`,
   the status filter bar + pagination work and survive a poll tick, and the lane-grid hash-skip
   holds its identity across ticks — all confirmed with a real browser at 200K scale. The
   windowed-progress (`N/M windows`) mid-flight signal's specific text form was not exercised by
   this corpus's in-flight seed rows (they carry no `fine_windows_total`) — the code path was
   read and is unchanged by any Phase 95 fix, but a real-browser observation of that exact text
   form is not part of this measurement. Not a regression; a measurement-corpus gap, recorded
   per the dispatcher's instruction rather than silently assumed passing.

**Overall: all four ROADMAP phase-95 success criteria are verifiably TRUE.** CONSOLE-04
("Operator can open the Analyze workspace without the browser severely slowing or hanging") is
satisfied by this bead's measurements and is ready for the epic to close.

## Files consulted / instrumented (this bead)

- `scripts/analyze_browser_soak.py` (**new**, this bead) — real-browser Playwright harness:
  OPEN/behavior-preservation/residual-size/soak measurements against a live `/s/analyze`. Rewrites
  a prior draft of this script that buffered all output in memory and wrote nothing until process
  exit (a 31-minute run of that draft produced zero output on a mid-soak interruption); this
  version streams every printed line with `flush=True` and appends every soak sample to an
  on-disk JSONL log as it is taken, wrapped in a top-level try/except that prints a traceback
  before a nonzero exit. Verified end-to-end with a 2-minute smoke run before the real
  >=30-minute soak.
- `scripts/perf_analyze_workspace.py` (Phase 95, `phaze-zqvh.1`, unmodified) — re-run for the
  server-side after-numbers in the before/after table above.
- `src/phaze/templates/pipeline/partials/_analyze_files.html`,
  `src/phaze/templates/pipeline/partials/_analyze_lanes.html`,
  `src/phaze/templates/pipeline/partials/_file_table.html`,
  `src/phaze/templates/pipeline/partials/stats_bar.html` — read (unmodified) to confirm the
  element/attribute contracts the browser harness asserts against (`#analyze-file-table`,
  `#analyze-lanes` + `data-lanes-hash`, `#analyze-filter-status`, `#analyze-files-view`,
  `#record-body`), and to trace the windowed-progress state-text branch for the residual finding.
- `src/phaze/services/pipeline.py:1333-1356` — read to confirm `fine_done`/`fine_total`
  projection is unchanged.
- No product code was modified by this bead. `pyproject.toml` carries one addition: a
  `[[tool.mypy.overrides]]` for `playwright`/`playwright.*` (`ignore_missing_imports = true`) —
  required because Playwright is intentionally never a project dependency (milestone's
  zero-new-dependencies constraint; `scripts/analyze_browser_soak.py` is run via ephemeral `uv
  run --with playwright`, never installed into the main `.venv`, never in CI) and `scripts/` is
  not excluded from `uv run mypy .`.
