---
phase: 55-routing-state-ledger-integration-the-live-seam
plan: 05
subsystem: pipeline-dashboard
tags: [observability, htmx, jinja2, cloud-phase, kueue, degrade-safe]
requires:
  - "55-02: cloud_job.cloud_phase column + CloudPhase enum"
  - "55-04: shared routers/pipeline.py + services/pipeline.py edit sites"
provides:
  - "get_cloud_phase_counts: degrade-safe per-cloud_phase reader (4 counts)"
  - "admission_state_card.html: carrier-always / body-conditional admission card"
  - "dual-context router seeding of the four counts (dashboard + stats poll)"
affects:
  - "src/phaze/templates/pipeline/dashboard.html"
  - "src/phaze/templates/pipeline/partials/stats_bar.html"
tech-stack:
  added: []
  patterns:
    - "_safe_count-backed per-source read (mirror get_inadmissible_count)"
    - "carrier-always / body-conditional OOB card (mirror inadmissible_card.html)"
    - "hx-swap-oob 5s-poll re-push from stats_bar.html"
key-files:
  created:
    - "src/phaze/templates/pipeline/partials/admission_state_card.html"
  modified:
    - "src/phaze/services/pipeline.py"
    - "src/phaze/routers/pipeline.py"
    - "src/phaze/templates/pipeline/dashboard.html"
    - "src/phaze/templates/pipeline/partials/stats_bar.html"
    - "tests/test_services/test_pipeline_counts.py"
    - "tests/test_routers/test_pipeline.py"
decisions:
  - "finished tile uses GREEN (dashboard's done/approved hue), not a second neutral gray — two gray tiles would be indistinguishable (55-UI-SPEC divergence, deliberate)"
  - "role='alert' and amber DELIBERATELY OMITTED — healthy progression, not a fault; amber+alert stay exclusive to inadmissible_card"
  - "service owns the degrade (per-phase _safe_count); NO router try/except — mirrors inadmissible_count wiring"
metrics:
  duration: ~25m
  completed: 2026-06-28
  tasks: 2
  files: 6
requirements: [KROUTE-06]
---

# Phase 55 Plan 05: Admission-State Dashboard Cards Summary

KROUTE-06 ships the pipeline-dashboard admission-state card — a thin, degrade-safe server-rendered read over `cloud_job.cloud_phase` showing per-phase Kueue admission counts (queued_behind_quota / admitted / running / finished), kept live by the existing 5s `/pipeline/stats` OOB poll.

## What was built

**Task 1 — `get_cloud_phase_counts` + dual-context router seeding** (commit `c12aa55`)
- Added `get_cloud_phase_counts(session) -> dict[str, int]` to `services/pipeline.py`: four independent `_safe_count`-backed COUNT reads (one per `CloudPhase` member), each with a distinct `node=` tag. A DB error degrades that phase to 0 and rolls back the aborted transaction rather than 500ing the hot poll (T-55-CARD-01). NULL `cloud_phase` rows (a1/local deploys) count toward no phase.
- Imported `CloudPhase` into `services/pipeline.py`.
- Seeded the four counts (`queued_behind_quota_count` / `admitted_count` / `running_count` / `finished_count`) identically into BOTH the `dashboard()` and `pipeline_stats_partial()` router contexts; imported `get_cloud_phase_counts` into the router. No router try/except — the service owns the degrade (mirrors `inadmissible_count`).
- Tests: per-phase counts over mixed seeded rows, all-zero NULL path, and forced-DB-error degrade-to-0 for every count.

**Task 2 — admission card partial + mount + OOB re-push** (commit `ce9f67f`)
- Created `partials/admission_state_card.html`: a `<section id="admission-state-card" aria-labelledby="admission-state-heading">` carrier ALWAYS emitted; an outer `{% if any count %}` gating the `Cloud · Admission` heading + a `grid grid-cols-2 sm:grid-cols-4 gap-4`; four per-tile blocks each gated on its own count, using the 55-UI-SPEC hues (gray / blue / violet / green) and the verbatim Copywriting-Contract labels + sub-labels. `hx-swap-oob` emitted only when `oob` is truthy. OMITS `role="alert"` and amber.
- Mounted the include in `dashboard.html` immediately after the inadmissible card (OUTSIDE `#pipeline-stats` and `#pipeline-stages`).
- Added the OOB re-push in `stats_bar.html` alongside the inadmissible OOB include, inside the `{% if oob_counts %}` block.
- Tests (extending `test_pipeline.py`): carrier-always-renders (all-zero → no heading), matching-tile renders for a seeded phase (other phases invisible), finished tile is green with no `role="alert"`/amber, NULL `cloud_phase` stays quiet, and the 5s stats poll re-pushes the card OOB with the matching tile.

## Verification

- `uv run pytest tests/test_services/test_pipeline_counts.py tests/test_routers/test_pipeline.py` — 104 passed (against the ephemeral `just test-db` Postgres on 5433).
- `uv run mypy .` — clean (182 source files).
- Degrade-safe: forced DB error → all four counts 0 → quiet empty carrier, no 500 (unit-proven).
- Gating: all-zero → empty carrier (no heading/tiles); non-zero → only the matching tiles render.
- 55-UI-SPEC conformance: heading + four label/sub-labels verbatim; finished green; no `role="alert"`; no amber; counts seeded in both contexts; card outside `#pipeline-stats`.

## Deviations from Plan

None — plan executed exactly as written. The two 55-UI-SPEC "divergences from naive mirroring" (green `finished`, omitted `role="alert"`) are part of the locked spec, not deviations.

## Threat surface

No new surface beyond the plan's `<threat_model>`. The mitigations are implemented: `_safe_count` degrade (T-55-CARD-01), plain-int-only autoescaped interpolation (T-55-CARD-02), card outside `#pipeline-stats`/`#pipeline-stages` so the OOB swap never clobbers DAG buttons (T-55-CARD-03). Zero packages installed (T-55-CARD-SC).

## Known Stubs

None.
