---
phase: 56-deployment-runbook-config-docs
plan: 02
subsystem: pipeline-dashboard
tags: [dashboard, htmx, oob-alert, kueue, k8s, observability]
requires:
  - phaze.services.pipeline.get_localqueue_unreachable (56-01)
  - phaze.services.pipeline.get_inadmissible_count (pattern source)
provides:
  - src/phaze/templates/pipeline/partials/localqueue_card.html
  - localqueue_unreachable seeded in both pipeline render paths
affects:
  - src/phaze/routers/pipeline.py
  - src/phaze/templates/pipeline/dashboard.html
  - src/phaze/templates/pipeline/partials/stats_bar.html
tech-stack:
  added: []
  patterns:
    - "carrier-always / body-conditional OOB alert (clone of inadmissible_card.html)"
    - "service-owns-degrade read; no router try/except"
key-files:
  created:
    - src/phaze/templates/pipeline/partials/localqueue_card.html
  modified:
    - src/phaze/routers/pipeline.py
    - src/phaze/templates/pipeline/dashboard.html
    - src/phaze/templates/pipeline/partials/stats_bar.html
decisions:
  - "Reused inadmissible_card.html verbatim (amber, role=alert, stable section id) rather than a new pattern — keeps the OOB contract identical across alert cards."
  - "Read the redis handle via getattr(request.app.state, 'redis', None) in both render paths, matching the existing degrade-safe counter wiring."
metrics:
  duration: ~10m
  completed: 2026-06-29
requirements: [KDEPLOY-04]
---

# Phase 56 Plan 02: LocalQueue-Unreachable Dashboard Alert Summary

Surfaces the K8s LocalQueue-unreachable flag (written by the 56-01 controller probe, read by `get_localqueue_unreachable`) on the pipeline dashboard as an amber, non-blocking OOB alert (D-05) — loud when the k8s lane can't reach its LocalQueue, silent when healthy.

## What Was Built

**Task 1 — `localqueue_card.html` amber OOB alert partial** (commit `9bfb373`)
Cloned `inadmissible_card.html` verbatim: a single top-level `<section id="localqueue-card">` carrier that always renders (so the OOB swap has a stable target), with `{% if oob %}hx-swap-oob="true"{% endif %}` and the alert body gated behind `{% if localqueue_unreachable %}`. The body uses amber classes (`border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-950`), `role="alert"` + `aria-labelledby`, the locked heading `⚠ K8s LocalQueue unreachable`, and the locked body `K8s LocalQueue unreachable — verify PHAZE_KUBE_LOCAL_QUEUE / cluster connectivity.` All copy is static through Jinja autoescape — no operator free-text interpolated (T-56-XSS).

**Task 2 — seed the flag into both render paths + wire includes** (commit `73a490c`)
- `src/phaze/routers/pipeline.py`: added `get_localqueue_unreachable` to the existing `from phaze.services.pipeline import (...)` block; computed `localqueue_unreachable = await get_localqueue_unreachable(getattr(request.app.state, "redis", None))` in BOTH the first-load `dashboard()` handler and the 5s `pipeline_stats_partial()` handler, seeding `"localqueue_unreachable"` into both template context dicts beside `"inadmissible_count"`.
- `dashboard.html`: includes `localqueue_card.html` next to `inadmissible_card.html`, OUTSIDE `#pipeline-stats`.
- `stats_bar.html`: re-pushes it with `{% with oob = True %}...{% endwith %}` on the same 5s poll, same stable id (the OOB contract).

## Verification

- `uv run pytest tests/test_routers/test_pipeline_localqueue.py -x` → **4 passed** (empty-when-reachable, renders-when-flagged, stable id on first-load + OOB, degrade-to-False unit read). DB-backed; run against the ephemeral test Postgres + Redis (`just test-db`, ports 5433/6380).
- `uv run ruff check src/phaze/routers/pipeline.py` → all checks passed.
- `uv run mypy src/phaze/routers/pipeline.py` → no issues.
- `pre-commit` hooks passed on both commits (ruff, ruff-format, bandit, mypy).

## Deviations from Plan

None — plan executed exactly as written. The Task 1 verify (`-k "flagged or empty"`) showed the `flagged` case failing until Task 2 wired the router/templates; this is the expected cross-task dependency (the partial alone cannot satisfy the render path), and the full file is green after Task 2.

## Threat Surface

No new surface beyond the plan's `<threat_model>`. T-56-POLL (degrade-safe read, never 500s) is owned by `get_localqueue_unreachable` from 56-01; T-56-XSS (static copy only) is satisfied — no variable operator input is interpolated.

## Self-Check: PASSED
- FOUND: src/phaze/templates/pipeline/partials/localqueue_card.html
- FOUND: commit 9bfb373 (Task 1)
- FOUND: commit 73a490c (Task 2)
