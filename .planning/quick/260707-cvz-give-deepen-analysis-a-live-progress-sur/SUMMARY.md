---
quick_id: 260707-cvz
status: complete
subsystem: pipeline-ui
tags: [htmx, deepen, poll, analysis, ui]
requirements_completed: [DEEPEN-PROGRESS-01]
key_files:
  created:
    - src/phaze/templates/pipeline/partials/deepen_progress.html
  modified:
    - src/phaze/templates/pipeline/partials/deepen_response.html
    - src/phaze/routers/pipeline.py
    - tests/shared/routers/test_pipeline.py
commits:
  - 671b2b13  # feat: templates + poll endpoint + since wiring
  - 22e59fe1  # test: poll states + success-path poller
metrics:
  duration: ~20 min
  tasks: 3
  files: 4
  tests_added: 7
completed: 2026-07-07
---

# Quick 260707-cvz: Live progress surface for Deepen analysis Summary

Gave the "Deepen analysis" action a live, in-place HTMX progress surface: the
`#deepen-result-{file_id}` anchor now shows a self-polling fragment that renders the
`N/M windows` idiom ("Re-analyzing · 34/62 windows") and settles into a terminal
"Deepen complete" state that stops polling — replacing the old single static line.

## What changed

- **New `deepen_progress.html`** — three-state (queued/running/complete) + gone poll target.
  Mirrors `scan_progress_card.html`'s terminal-halt idiom exactly: non-terminal branches
  (running/queued) carry `hx-get` + `hx-trigger="every 2s"` + `hx-swap="outerHTML"`; terminal
  branches (gone/complete) OMIT all three, so the outerHTML swap removes the trigger and HTMX
  halts automatically. Root element is an inline `<span aria-live="polite">` in every branch.
  Counts are numeric ints only (None-guarded to 0 by the endpoint) — no essentia strings, no raw HTML.
- **`deepen_response.html`** — replaced ONLY the success `{% else %}` branch with a bootstrap
  poller (`hx-trigger="load, every 2s"`) that fires the first fetch and hands off to
  `deepen_progress.html` (single poll loop, no double-poll). `not_found` / `no_active_agent`
  branches are byte-unchanged static one-liners.
- **`pipeline.py`** — added `GET /pipeline/files/{file_id}/deepen-progress?since=<epoch>` and
  threaded `since = datetime.now(UTC).timestamp()` (captured BEFORE the enqueue) + `file_id`
  into the deepen POST success context. The deepen POST guards/enqueue/dedup/routing and the
  not_found/no_active_agent branches are untouched.

## Completion predicate (verbatim from plan)

```
requested_at = datetime.fromtimestamp(since, tz=UTC)
complete = (analysis is not None
            and analysis.analysis_completed_at is not None
            and analysis.analysis_completed_at > requested_at)
```

`since` is captured at click time. A stale pre-click sampled result has
`completed_at <= requested_at` ⇒ NOT complete (kills the misleading-complete edge). A fresh
`put_analysis` stamps `func.now() > requested_at` ⇒ complete. `post_analysis_progress` is
counter-only and never touches `analysis_completed_at`, so a re-deepen of an already-ANALYZED
file keeps its OLD completed_at until the fresh `put_analysis` lands — completion is NOT gated
on completed_at being NULL.

## Tests (7 added, all green)

- `test_deepen_progress_queued_state_polls` — pre-click completed_at + equal counts → "Queued", poll present.
- `test_deepen_progress_running_state_shows_counts_and_polls` — 34/62 → "34/62 windows", poll present.
- `test_deepen_progress_complete_state_halts_poll` — completed_at > since → "Deepen complete", no `hx-trigger`.
- `test_deepen_progress_gone_state_halts_poll` — unknown uuid → "no longer available", no trigger, no 500.
- `test_deepen_progress_stale_sampled_result_not_complete` — completed_at == since (strict `>` boundary) → not complete.
- `test_deepen_progress_non_numeric_since_is_422` — typed float param 422s a non-numeric `since` (T-cvz-01).
- `test_deepen_success_path_returns_bootstrap_poller` — success POST emits the bootstrap poller, not the old static line.

## Verification

- `uv run pytest tests/shared/routers/test_pipeline.py -k deepen` → **13 passed** (6 pre-existing + 7 new).
- Full `test_pipeline.py` suite → **103 passed**; `phaze.routers.pipeline` coverage **95.45%** (≥90 gate).
- `uv run ruff check` clean, `uv run ruff format` clean, `uv run mypy src/phaze/routers/pipeline.py` clean.
- Pre-commit hooks ran on both commits (no `--no-verify`).

## Deviations from Plan

- **[Rule 3 - Blocking]** The plan's verify commands used `just test-db up`, but the `test-db`
  recipe takes no argument (`up` was forwarded to docker → exit 125). Ran `just test-db`
  (no arg) and exported `TEST_DATABASE_URL` / `MIGRATIONS_TEST_DATABASE_URL` / `PHAZE_REDIS_URL`
  (per the `integration-test` recipe) so pytest hit the ephemeral DB on 5433/6380 instead of the
  dev DB on 5432. No source impact.

## Deferred (intentional, per plan)

- Chart auto-refresh after completion is deferred: the poll endpoint has `file_id` only, but
  `analysis_timeline` is keyed by `proposal_id`, so a refetch would require threading proposal_id
  through the poll (higher risk). The terminal "reload to see the updated analysis" message is the
  accepted low-risk fallback (planner's call, required_outcome #2).

## Known Stubs

None.

## Self-Check: PASSED

- src/phaze/templates/pipeline/partials/deepen_progress.html — FOUND
- src/phaze/templates/pipeline/partials/deepen_response.html — FOUND
- src/phaze/routers/pipeline.py — FOUND
- commit 671b2b13 — FOUND
- commit 22e59fe1 — FOUND
