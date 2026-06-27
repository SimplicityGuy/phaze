---
phase: 51-deployment-config-docs
plan: 01
subsystem: control-plane-config
tags: [config, cloud-burst, master-toggle, routing, pydantic-settings]
requires:
  - "Phase 49 cloud routing seam (_route_discovered_by_duration)"
  - "Phase 50 staging cron (stage_cloud_window) + backfill trigger (trigger_backfill_cloud)"
provides:
  - "ControlSettings.cloud_burst_enabled master toggle (default False)"
  - "Toggle-gated routing seam, staging cron, and backfill trigger (OFF = all-local)"
affects:
  - "src/phaze/routers/pipeline.py (_route_discovered_by_duration signature: +cloud_enabled)"
  - "pipeline/partials/backfill_response.html (optional disabled flag)"
tech-stack:
  added: []
  patterns:
    - "pydantic bool Field + AliasChoices dual-form env binding (mirrors enable_saq_ui)"
    - "clean cron no-op gate (never raise) + explicit router early-return guard"
key-files:
  created:
    - tests/test_config/test_cloud_burst_toggle.py
  modified:
    - src/phaze/config.py
    - src/phaze/routers/pipeline.py
    - src/phaze/tasks/release_awaiting_cloud.py
    - src/phaze/templates/pipeline/partials/backfill_response.html
    - tests/test_routing_seam.py
    - tests/test_staging_cron.py
    - tests/test_routers/test_pipeline.py
decisions:
  - "cloud_burst_enabled lives on ControlSettings (not BaseSettings): both reader sites resolve ControlSettings under PHAZE_ROLE=control"
  - "Backfill uses an explicit early-return guard (not routing-seam-only) so OFF never resets ANALYSIS_FAILED files to DISCOVERED (Pitfall 2 / T-51-02)"
  - "test_pipeline.py gets a module autouse cloud-on fixture so Phase 49/50 ON regressions keep passing under the new default-OFF toggle"
metrics:
  duration: "~40 min"
  completed: "2026-06-26"
  tasks: 3
  files: 8
  tests_added: 8
requirements: [CLOUDDEPLOY-04, CLOUDDEPLOY-02]
---

# Phase 51 Plan 01: Cloud-Burst Master Toggle Summary

The `cloud_burst_enabled` master switch (default `False`) gates all three cloud entry points — the duration routing seam, the staging cron, and the backfill trigger — so a fresh v5.0 deploy behaves all-local with zero cloud activity until the operator opts in.

## What Was Built

**Task 1 — `cloud_burst_enabled` field (CLOUDDEPLOY-04 + master-toggle slice of CLOUDDEPLOY-02).**
Added a plain `bool` `Field` to `ControlSettings` alongside the existing cloud knob block, `default=False`, bound from `PHAZE_CLOUD_BURST_ENABLED` (or bare `cloud_burst_enabled`) via `AliasChoices` — mirroring the `enable_saq_ui` kill-switch pattern. No `gt=/lt=` bounds (bool), not secret-bearing (absent from `SECRET_FILE_FIELDS`).

**Task 2 — Gate the routing seam + staging cron (D-02, D-03).**
- `_route_discovered_by_duration` gained a `cloud_enabled: bool` parameter (placed adjacent to `threshold_sec`); line `is_long = cloud_enabled and duration is not None and duration >= threshold_sec`. All three call sites (`trigger_analysis`, `trigger_analysis_ui`, `trigger_backfill_cloud`) now pass `settings.cloud_burst_enabled`. OFF ⇒ nothing is "long" ⇒ every file falls to the local branch, no row ever reaches `AWAITING_CLOUD`.
- `stage_cloud_window` got a top-of-function gate (`cfg = get_settings(); if not cfg.cloud_burst_enabled: return {"staged": 0, "skipped": 0}`) BEFORE the advisory lock / window logic — a clean no-op that never raises (T-50-cron-raise discipline).

**Task 3 — Gate the backfill trigger with an explicit early-return (D-03, Pitfall 2 / T-51-02).**
`trigger_backfill_cloud` short-circuits BEFORE `count_backfill_candidates` when the toggle is off, returning the existing `backfill_response.html` partial with `count=0, disabled=True` and mutating ZERO `file.state` rows. This prevents the OFF feature from silently resetting the 144 `ANALYSIS_FAILED` long files to `DISCOVERED` and re-routing them local to re-time-out.

## Key Decisions

- **`ControlSettings`, not `BaseSettings`** — both reader sites (the module-level `settings` singleton in `pipeline.py` and `get_settings()` in `stage_cloud_window`) resolve `ControlSettings` under `PHAZE_ROLE=control`. This is the established home for the sibling cloud knobs.
- **Explicit backfill early-return** rather than relying on the routing-seam gate alone — the routing gate would route the reset candidates local, but the reset itself is the harm (Pitfall 2). The guard sits above the candidate query so nothing mutates.
- **Module autouse `_cloud_burst_on` fixture** in `test_pipeline.py` — because the new default flips cloud behavior off, the Phase 49/50 regression tests (long-file hold, backfill reset) are pinned ON via the fixture; the new OFF tests override it to `False` in their own bodies.

## Deviations from Plan

None of substance — plan executed as written. One optional enhancement within plan latitude: the `backfill_response.html` partial now renders a distinct "Cloud burst is disabled" notice on the `disabled` branch (the plan explicitly allowed surfacing the optional `disabled` flag; no new template created).

## Verification

- `uv run pytest tests/test_config/test_cloud_burst_toggle.py tests/test_routing_seam.py tests/test_staging_cron.py tests/test_routers/test_pipeline.py -q` → 106 passed
- `uv run pytest --cov --cov-report=term-missing` → 2215 passed, total coverage 97.46% (≥ 85% gate)
- `pre-commit run --all-files` → all hooks pass (ruff, ruff-format, bandit, mypy, etc.)
- `uv run mypy src/phaze/config.py src/phaze/routers/pipeline.py src/phaze/tasks/release_awaiting_cloud.py` → clean

## TDD Gate Compliance

Each of the 3 tasks followed RED → GREEN: a `test(51-01)` commit with a verified-failing test preceded each `feat(51-01)` implementation commit (6 commits total, no REFACTOR needed).

## Commits

- `de2a949` test(51-01): add failing test for cloud_burst_enabled toggle
- `9f111df` feat(51-01): add cloud_burst_enabled master toggle to ControlSettings
- `c4ae773` test(51-01): add failing tests for routing-seam + staging-cron toggle gates
- `105a5c3` feat(51-01): gate routing seam + staging cron on cloud_burst_enabled
- `557c3b1` test(51-01): add failing test for backfill cloud_burst_enabled gate
- `ded1353` feat(51-01): gate backfill trigger with explicit cloud_burst_enabled early-return

## Known Stubs

None. The toggle is fully wired at all three entry points; no placeholder data paths introduced.

## Threat Flags

None. No new network endpoints, auth paths, file access, or schema changes. The single net-new surface (the operator config toggle) is the subject of the plan's own threat register (T-51-01..03) and is fully mitigated/accepted.

## Self-Check: PASSED

- Files created/modified all exist on disk (verified).
- All 6 commit hashes present in `git log` (verified).
