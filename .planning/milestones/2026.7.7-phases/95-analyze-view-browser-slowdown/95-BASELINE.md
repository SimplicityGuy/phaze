# Phase 95 — Analyze-Workspace 200K-Scale Baseline (phaze-zqvh.1)

Instrumentation-and-baseline bead for the Phase 95 epic (`phaze-zqvh`, CONSOLE-04). Satisfies
ROADMAP phase-95 success criterion 2: the slowdown root cause identified and recorded here. This
artifact is the numeric before-picture the fix bead(s) and the final verify bead cite for
before/after deltas.

## Method — how to reproduce these numbers

All measurements ride the **Phase 82 PERF-02 bench harness** against a dedicated, throwaway perf
Postgres (own container/port — never the shared `phaze-test-db`, never prod). No product code was
touched to take these measurements (D-06 / PERF-02 precedent): a read-only ASGI client against a
synthetic seeded corpus.

```bash
# 1. Start the dedicated perf Postgres (own port 5545, never wiped by test-db recreates)
just perf-db-up

# 2. Migrate to HEAD + seed a ~200K-file synthetic corpus (idempotent; --reseed to force a fresh corpus)
just perf-seed 200000
# equivalent to:
#   PHAZE_DATABASE_URL=postgresql+asyncpg://phaze:phaze@localhost:5545/phaze_perf82 uv run alembic upgrade head
#   uv run python scripts/seed_perf_corpus.py --n 200000 --dsn postgresql://phaze:phaze@localhost:5545/phaze_perf82 --reseed

# 3. /pipeline/stats direct + endpoint timings (existing PERF-02 harness, unmodified) — comparable
#    to the Phase 92 861ms/1072ms numbers.
just perf-explain 20
# equivalent to:
#   uv run python scripts/perf_explain.py --dsn postgresql://phaze:phaze@localhost:5545/phaze_perf82 \
#       --redis-url redis://localhost:6380/0 --iterations 20

# 4. NEW sibling script (this bead) — get_analyze_stage_files direct timing, the /s/analyze
#    full-shell render (time + payload size + DOM row count), and the /pipeline/stats OOB
#    fragment payload size per tick.
export PHAZE_DATABASE_URL=postgresql+asyncpg://phaze:phaze@localhost:5545/phaze_perf82
export PHAZE_QUEUE_URL=postgresql://phaze:phaze@localhost:5545/phaze_perf82
export PHAZE_REDIS_URL=redis://localhost:6380/0
uv run python scripts/perf_analyze_workspace.py \
    --dsn postgresql://phaze:phaze@localhost:5545/phaze_perf82 --iterations 10
```

The `PHAZE_DATABASE_URL` / `PHAZE_QUEUE_URL` / `PHAZE_REDIS_URL` exports are required for steps 3-4
because `create_app()` reads `phaze.config.settings`, whose defaults point at the in-Compose
hostnames (`postgres`, `redis`) — outside Docker those must be overridden to the perf DB / test
Redis host ports. `scripts/perf_explain.py` needs only `--redis-url` (it does not construct the
full app for `/s/analyze`); `scripts/perf_analyze_workspace.py` (new, this bead) constructs the
real `create_app()` ASGI app for `/s/analyze` and `/pipeline/stats`, so it needs the env vars too.

Corpus shape actually seeded (`seed_perf_corpus.py`'s deterministic D-06 distribution, verified at
measurement time): **200,000** `files` rows, **90,000** `analysis` rows (~40% completed + ~5%
failed, disjoint). `get_analyze_stage_files` membership (any analysis row OR analyze in-flight OR
active cloud_job) resolved to **92,335 rows** at this corpus.

## Headline numbers

| Measurement | Value | Comparable baseline |
|---|---|---|
| `get_stage_progress()` DIRECT (feeds `/pipeline/stats`) | p50 **975.0ms**, p95 **1121.0ms** | Phase 92: 861ms direct |
| `GET /pipeline/stats` endpoint (full poll tick) | p50 **1099.5ms**, p95 **1205.4ms** | Phase 92: 1072ms endpoint |
| `get_analyze_stage_files()` DIRECT (row read only, no template) | p50 **~750-850ms** (771.2ms / 851.3ms one run, 732.2ms / 769.2ms another), row count **92,335** | — (new in Phase 95) |
| `GET /s/analyze` full-shell server render (direct-nav "open the workspace") | p50 **~4.8-5.3s** (4782.8-4970.7ms one run, 4966.4-5343.9ms another) | — (new in Phase 95) |
| `GET /s/analyze` response payload size | **~105 MB** (104,963,282 bytes, stable across runs) | — (new in Phase 95) |
| `GET /s/analyze` approximate per-file DOM row count | **92,335** rows (one `<tr>` + ~5 `<td>` per Analyze-stage file, one-to-one with the `hx-get="/record/{file_id}"` marker) | — (new in Phase 95) |
| `GET /pipeline/stats` OOB fan-out fragment payload size (per 5s tick) | **11,956 bytes** (~12 KB, stable across runs/ticks — does NOT scale with corpus size) | — (new in Phase 95) |

Two independent runs (10-20 iterations each) reproduced the same shape: `/s/analyze` server render
time (~5s) and payload (~105MB / 92,335 rows) were stable to within run-to-run noise; the
`/pipeline/stats` fragment was byte-identical across both runs (11,956 bytes) because it carries no
per-file content.

## What was measured vs. estimated

**Measured** (server-side, via the real route handlers over `httpx.ASGITransport` — the actual
`GET /s/analyze` / `GET /pipeline/stats` code paths, not a hand-rewrite):
- `get_analyze_stage_files` direct DB-read latency + row count.
- `GET /s/analyze` server-side render wall-clock, response payload bytes, and DOM row count
  (via the `hx-get="/record/{file_id}"` per-row marker `_file_table.html` emits — an exact proxy
  for rendered `<tr>` count, since Phase 61 RECORD-01 wires that attribute on every Analyze row).
- `GET /pipeline/stats` OOB fragment payload bytes per tick.
- `get_stage_progress` / `GET /pipeline/stats` endpoint latency (re-run of the existing Phase 82
  PERF-02 harness, unmodified, to get a like-for-like comparison point against the Phase 92
  baseline).

**Estimated, NOT measured** (no headless browser available in this environment — no `playwright`
package installed, no local Chrome/Chromium binary on the dev box; acceptable per the bead's
acceptance criteria when a headless-browser measurement isn't readily available):
- Browser time-to-interactive for `/s/analyze` at 200K scale. **Reasoned estimate**: a 105MB HTML
  response with ~92,335 `<tr>` rows (each carrying `hx-get` / `hx-target` / `hx-trigger` /
  `tabindex` / two Alpine `@click` / `@keydown` bindings, per `_file_table.html:52`) requires the
  browser to (a) download/parse ~105MB of HTML, (b) construct ~92K+ DOM nodes with attached
  htmx/Alpine directive processing per node, and (c) run htmx's startup `htmx:load` node scan +
  Alpine's `x-data`/`@click` directive compilation over that whole tree. Each of those steps is
  strictly more expensive than raw HTML parse; multi-second-to-tens-of-seconds time-to-interactive
  on a 200K-scale corpus is a reasonable order-of-magnitude expectation given the ~5s SERVER render
  alone. This was not directly measured and should be confirmed with a real browser trace before
  claiming a precise number.
- The >=15-minute soak's browser memory/long-task progression while the 5s poll runs. **Reasoned
  estimate, not measured**: because the poll's OOB fragment payload is small and CONSTANT (~12KB,
  does not grow with corpus size or time — confirmed above), any progressive memory/long-task
  growth during a soak would have to come from the CLIENT-SIDE cost of repeatedly
  destroy-and-recreating the OOB-targeted nodes (`stats_bar.html`'s `x-init` seeds +
  `#analyze-lanes` grid) against an already-massive ~92K-row DOM every 5s — i.e. a fixed per-tick
  client-side cost multiplied by tick count, not a growing payload. This is consistent with, but
  does not by itself prove, progressive degradation; it was not directly observed in a browser.

## Root-cause verdict

**Client-side render of the unbounded per-file table is the dominant, confirmed root cause of the
severe slowdown on open.** The stats poll is NOT the primary driver, though it plausibly compounds
degradation over a long-lived tab (reasoned, not directly measured — see above).

Evidence:

1. **The `/s/analyze` open path costs ~5s server-side and ships ~105MB to the browser**, one `<tr>`
   per Analyze-stage file (92,335 rows at 200K scale) — this is the ENTIRE severe-slowness cost on
   open; there is no other request in that critical path.
2. **The `/pipeline/stats` 5s poll fragment is ~12KB and does not scale with corpus size** — it
   never re-emits the per-file table (`pipeline_stats_partial` in `routers/pipeline.py` never calls
   `get_analyze_stage_files`; only `build_dashboard_context`, the ONE-TIME open-path context
   builder, does). The poll's DB-side cost (`get_stage_progress`, p50 975ms / p95 1121ms direct,
   p50 1099.5ms / p95 1205.4ms full endpoint) sits close to, and in this run slightly over, the
   ~1s soft budget (D-07) — consistent with, not materially worse than, the Phase 92 baseline
   (861ms / 1072ms). It is NOT the source of the "severe slowness immediately on open" symptom the
   epic names, because the poll fragment is small and does not touch the per-file table.
3. `get_analyze_stage_files` itself (the query, isolated from templating/ASGI) takes ~750-850ms at
   this corpus scale for 92,335 rows — a meaningful chunk of the ~5s `/s/analyze` render, but the
   remaining ~4s is templating ~92K Jinja `{% for row in rows %}` iterations into ~105MB of HTML
   (`analyze_workspace.html:100-153` + `_file_table.html:51-57`), confirming the render (not just
   the query) is the expensive part.

**Decision on ROADMAP phase-95 success criterion 3 (DENORM-01 revisit)**: the root cause does
**NOT** trace primarily to the stats poll — its endpoint latency (p50 1099.5ms) is in the same
order of magnitude as the Phase 92 baseline (1072ms), not a new regression at 200K scale, and its
payload is corpus-size-independent. **DENORM-01 stays deferred/YAGNI** (no new evidence licenses
building the denormalized stage-status bitmap column). The fix-bead effort belongs on bounding the
client-side per-file render at the source (the epic's "active-first working set" design: render the
in-flight/awaiting-cloud/failed working set + a bounded recent-completions window by default, with
the full corpus reachable via the existing server-side-paginated status filter bar) — this is the
epic's ARCHITECTURE DECISION already recorded on `phaze-zqvh`, and this baseline's numbers confirm
it targets the right cost center.

## Files consulted / instrumented

- `src/phaze/services/pipeline.py:1175` (`get_analyze_stage_files`) — timed directly, unmodified.
- `src/phaze/routers/shell.py` (`_render_stage`, `STAGE_PARTIALS["analyze"]`) — the `/s/analyze`
  route timed via ASGI, unmodified.
- `src/phaze/routers/pipeline.py` (`build_dashboard_context`, `pipeline_stats_partial`) — read to
  confirm the poll does NOT re-read `get_analyze_stage_files` (only the one-time open-path
  context builder does), unmodified.
- `src/phaze/templates/pipeline/partials/analyze_workspace.html`, `_file_table.html`,
  `stats_bar.html` — read to confirm the row-per-file / OOB fan-out shapes named in the epic
  design doc, unmodified.
- `scripts/perf_analyze_workspace.py` (**new**, this bead) — the sibling bench script producing
  the `get_analyze_stage_files` / `/s/analyze` / `/pipeline/stats`-payload-size numbers above.
  No production code was modified to take these measurements.
