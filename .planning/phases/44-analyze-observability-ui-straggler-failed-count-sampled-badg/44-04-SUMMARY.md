---
phase: 44-analyze-observability-ui-straggler-failed-count-sampled-badg
plan: 04
subsystem: pipeline-router + proposals-router + dashboard/timeline templates
tags: [observability, htmx, jinja2, tailwind, dashboard, sampled-badge, deepen, degrade-safe]
requires:
  - "44-02 get_straggler_count(session, threshold_sec) / get_analysis_failed_count(session) degrade-safe reads"
  - "44-02 settings.straggler_threshold_sec config knob"
  - "44-03 POST /pipeline/files/{file_id}/deepen HTMX endpoint"
  - "AnalysisResult.sampled + four coverage columns (Phase 43, migration 021)"
provides:
  - "Dashboard straggler + ANALYSIS_FAILED count card (pipeline/partials/straggler_failed_card.html), live on the 5s poll"
  - "Render-if-sampled badge (proposals/partials/sampled_badge.html) with the four coverage counts in the tooltip"
  - "Deepen-analysis button on the analysis timeline POSTing to the Plan-03 endpoint"
  - "proposal_timeline now seeds `analysis` (AnalysisResult) + `file_id` into the timeline context"
affects:
  - "Operator-facing pipeline dashboard + per-file proposal timeline UI"
tech-stack:
  added: []
  patterns:
    - "Out-of-band card re-render: the card lives outside #pipeline-stats, re-pushed hx-swap-oob on each 5s poll via an oob flag on the same partial"
    - "Render-if-present-else-nothing badge gated on a nullable bool (confidence_badge.html analog) — NULL/false renders no markup, never errors"
    - "Service-owns-degrade router wiring: no try/except around the Plan-02 reads (the never-500 SAVEPOINT/_safe_count lives in the service)"
key-files:
  created:
    - src/phaze/templates/pipeline/partials/straggler_failed_card.html
    - src/phaze/templates/proposals/partials/sampled_badge.html
  modified:
    - src/phaze/config.py
    - src/phaze/routers/pipeline.py
    - src/phaze/routers/proposals.py
    - src/phaze/templates/pipeline/dashboard.html
    - src/phaze/templates/pipeline/partials/stats_bar.html
    - src/phaze/templates/proposals/partials/analysis_timeline.html
    - tests/test_routers/test_pipeline.py
    - tests/test_routers/test_proposals.py
decisions:
  - "straggler_threshold_sec MOVED from AgentSettings to ControlSettings — the control-plane dashboard reads it off the module-level (Control-typed) settings; 44-02 had placed it on AgentSettings, which mypy correctly rejected (Rule 1 bug fix)"
  - "The straggler/failed card lives OUTSIDE #pipeline-stats and is kept live via an hx-swap-oob re-render emitted from stats_bar.html (oob=True), mirroring the DAG-seed OOB idiom — a blunt innerHTML swap of #pipeline-stats can never reach a sibling card"
  - "The sampled badge gates on `analysis is not none and analysis.sampled` so NULL coverage (pre-43 rows), sampled=False, and a missing analysis row all render nothing without error (D-03 / T-44-12)"
metrics:
  duration: ~30 min
  completed: 2026-06-18
  tasks: 2
  files: 10
  tests_added: 7
---

# Phase 44 Plan 04: Straggler/Failed Dashboard Card + Sampled Badge + Deepen Button Summary

Surfaced Phase-43's recorded analysis outcomes in the operator UI: (1) a dashboard "Analysis Health"
card showing the STRAGGLER ("still grinding") and ANALYSIS_FAILED ("gave up") counts, wired through
the existing 5s `/pipeline/stats` poll; (2) a render-if-sampled amber badge on the per-file analysis
timeline carrying the four coverage counts in its tooltip; (3) a "Deepen analysis" button (gated on
`sampled`) that POSTs to the Plan-03 `/pipeline/files/{file_id}/deepen` endpoint. All new dashboard
context comes from the Plan-02 degrade-safe service reads — the router adds no try/except.

## What Was Built

**Task 1 — straggler/failed counts + dashboard card (commit 630446f)**
- `routers/pipeline.py`: imported `get_straggler_count` / `get_analysis_failed_count`; seeded
  `straggler_count` + `analysis_failed_count` into BOTH `dashboard()` and `pipeline_stats_partial()`
  context (passing `settings.straggler_threshold_sec` to the straggler read). No try/except added —
  the Plan-02 services own the never-500 degrade (same idiom as the busy-count wiring at 175-178).
- New `pipeline/partials/straggler_failed_card.html`: two distinct buckets (44-02 D-02), rendered
  inline in `dashboard.html` on first load and re-pushed `hx-swap-oob="true"` from `stats_bar.html`
  on every 5s poll (an `oob` flag flips the partial). The card sits outside `#pipeline-stats`, so the
  poll's innerHTML swap never reaches it — the OOB re-render keeps it live.
- `config.py`: moved `straggler_threshold_sec` from `AgentSettings` to `ControlSettings` (see Deviations).
- Tests: card renders with both buckets; a seeded ANALYSIS_FAILED file's count reaches the dashboard
  card AND the stats poll; the OOB card emits `hx-swap-oob` on `/pipeline/stats`; stragglers read 0
  with no `saq_jobs` seeded.

**Task 2 — sampled badge + deepen button on the timeline (commit 6ba2960)**
- `routers/proposals.py::proposal_timeline`: after resolving `file_id`, also fetches the 1:1
  `AnalysisResult` (`scalar_one_or_none` -> `None` when absent) and seeds `analysis` + `file_id` into
  the timeline context.
- New `proposals/partials/sampled_badge.html`: copied `confidence_badge.html`'s render-if-present pill
  structure; gates on `analysis is not none and analysis.sampled` so NULL/false sampled renders
  nothing. When sampled, an amber "Sampled — more data available" pill carries the four coverage
  counts in its `title=` tooltip.
- `analysis_timeline.html`: `{% include %}`s the badge and (gated on `analysis.sampled`) renders a
  "Deepen analysis" `hx-post` button targeting `/pipeline/files/{file_id}/deepen`, swapping the
  endpoint's fragment into an adjacent `aria-live` result anchor.
- Tests: badge + button + coverage tooltip when `sampled=True`; NEITHER rendered when `sampled` is
  `False`, `None`, or there is no `AnalysisResult` row at all (the not-sampled / NULL path is proven
  to render no markup and never error).

## How It Works

The 5s `/pipeline/stats` poll swaps `innerHTML` of `#pipeline-stats` with `stats_bar.html`. The new
counts card is a sibling of that block, so to keep it live `stats_bar.html` re-includes the card with
`oob=True` inside its existing `oob_counts` gate; the partial then emits `hx-swap-oob="true"` on the
same `#straggler-failed-card` id, and HTMX lands the out-of-band swap on the sibling. On the initial
full-page render the card is included WITHOUT `oob`, so it renders inline once (no stray
`hx-swap-oob` at load).

The sampled badge follows the project's render-if-present idiom: a single `{% if %}` gate means a
pre-Phase-43 row (NULL coverage + NULL sampled), a full-budget result (`sampled=False`), or a file
with no analysis row all produce zero markup — the timeline can never 500 on the absence of coverage
data (T-44-12). The four coverage counts are plain ints / the `file_id` is a typed uuid, both rendered
through Jinja autoescape into a tooltip / a path-only URL — no operator free-text is interpolated
(T-44-13).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] `straggler_threshold_sec` was on the wrong settings class**
- **Found during:** Task 1 (`uv run mypy src/phaze/routers/pipeline.py` failed:
  `"ControlSettings" has no attribute "straggler_threshold_sec"`).
- **Issue:** 44-02 added `straggler_threshold_sec` to `AgentSettings`, but the control-plane dashboard
  reads it off the module-level `settings`, which is typed/constructed as `ControlSettings`
  (`config.py:585`). The field was therefore unreachable from the only place that consumes it —
  `settings.straggler_threshold_sec` was an attribute error. The 44-02 SUMMARY described it as
  `settings.straggler_threshold_sec` but placed the Field inside `AgentSettings`.
- **Fix:** Moved the `straggler_threshold_sec` Field from `AgentSettings` to `ControlSettings`
  (preserving the default 6600, `gt=0`/`lt=86400`, and the `PHAZE_STRAGGLER_THRESHOLD_SEC` alias).
  The dashboard is a control-plane concern, so `ControlSettings` is the correct home.
- **Files modified:** `src/phaze/config.py`
- **Commit:** 630446f

No other deviations — both tasks executed as written.

## TDD Gate Compliance

Plan frontmatter `type: execute` (not `tdd`); tasks are `type="auto"` with no `tdd="true"` attribute,
so the RED/GREEN gate sequence does not apply. Implementation and its tests were committed together
per task.

## Verification Results

- `uv run pytest tests/test_routers/test_pipeline.py -q -k "straggler or failed or dashboard or stats"` → 19 passed
- `uv run pytest tests/test_routers/test_proposals.py -q -k "timeline or sampled or badge or deepen"` → 11 passed
- `uv run pytest tests/test_routers/test_pipeline.py tests/test_routers/test_proposals.py tests/test_services/test_pipeline.py -q` → 153 passed
- `uv run pytest tests/test_config tests/test_config_role_split.py tests/test_config_worker.py -q` → 48 passed (config field move clean)
- `uv run mypy .` → Success: no issues found in 156 source files
- `uv run ruff check` (both routers + config + both test modules) → All checks passed
- `pre-commit run --all-files` → all hooks Passed (no `--no-verify`)
- Coverage: `routers/pipeline.py` 94.92%, `routers/proposals.py` 99.18% (both ≥85%); the new lines are covered, remaining misses are pre-existing untouched branches.

Test infra: DB-backed router tests require Postgres + Redis; run against the ephemeral test DB
(port 5433 Postgres, 6380 Redis), `TEST_DATABASE_URL=postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test`.

## Authentication Gates

None.

## Known Stubs

None — both counts are wired to live degrade-safe service reads and the deepen button POSTs to the
real Plan-03 endpoint. No placeholder data, no hardcoded empties flowing to the UI.

## Threat Flags

None — no new network surface, auth path, file access, or schema change beyond the plan's
`<threat_model>` (the deepen POST is the existing Plan-03 endpoint; the badge/card render existing
typed data).

## Commits

- `630446f` feat(44-04): wire straggler/ANALYSIS_FAILED counts into dashboard + stats card
- `6ba2960` feat(44-04): sampled badge + Deepen-analysis button on the analysis timeline

## Self-Check: PASSED

- src/phaze/templates/pipeline/partials/straggler_failed_card.html — FOUND
- src/phaze/templates/proposals/partials/sampled_badge.html — FOUND
- src/phaze/routers/pipeline.py (get_straggler_count / get_analysis_failed_count wired) — FOUND
- src/phaze/routers/proposals.py (AnalysisResult fetch) — FOUND
- Commit 630446f — FOUND
- Commit 6ba2960 — FOUND
