---
phase: 67-backend-registry-config-model
plan: 03
subsystem: routing
tags: [routing, backfill, staging-cron, jinja, presentation, config, registry, transitional]

# Dependency graph
requires:
  - phase: 67-backend-registry-config-model
    plan: 02
    provides: "ControlSettings.cloud_enabled gate + transitional active_cloud_kind/active_cap accessors + backends field"
provides:
  - "routing seam + backfill guards read settings.cloud_enabled (Class A) — byte-identical for the all-local deploy"
  - "staging cron (stage_cloud_window) forks on active_cloud_kind/active_cap (Class B), every no-op early-return preserved"
  - "Analyze lane cards render off the NEUTRAL cloud_lane_kind context key (Class C); zero cloud_target string in pipeline.py or the 3 partials"
affects: [67-06, 68-backend-protocol, 71-backend-ui]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Class A on/off call sites read the registry-derived cloud_enabled @property (never the removed flat selector)"
    - "Class B dispatch forks read the # TRANSITIONAL — Phase 68 active_cloud_kind/active_cap accessors"
    - "Class C presentation: router hands templates a transitional legacy-shaped value under a NEUTRAL context key so no removed-field name survives in the template layer (keeps Plan 06's package-wide grep gate truthful)"

key-files:
  created: []
  modified:
    - src/phaze/tasks/release_awaiting_cloud.py
    - src/phaze/routers/pipeline.py
    - src/phaze/templates/pipeline/partials/analyze_workspace.html
    - src/phaze/templates/pipeline/partials/_lane_card.html
    - src/phaze/templates/pipeline/partials/backfill_response.html
    - tests/analyze/core/test_staging_cron.py
    - tests/shared/routers/test_pipeline.py
    - tests/shared/core/test_routing_seam.py
    - tests/shared/core/test_enrich_analyze_workspaces.py

key-decisions:
  - "Tests drive the registry directly (patch settings.backends / a registry-shaped stub exposing cloud_enabled/active_cap/active_cloud_kind) rather than the backends_toml_env toml fixture — exercises the real registry-derived @property logic while staying hermetic (no toml/env/DB-config coupling; models_path preserved)"
  - "cloud_lane_kind mapping is compute↔A1 lane, kueue↔k8s lane, consistent across the router bind + all 3 partials; the router supplies 'local' when all-local else active_cloud_kind"
  - "type: ignore[attr-defined] retained on the get_settings()-typed cron reads (module-level get_settings returns BaseSettings); active_cap is non-None on the cron path because the cloud_enabled gate short-circuits the all-local case"

requirements-completed: [REG-04]

# Metrics
duration: 35min
completed: 2026-07-03
---

# Phase 67 Plan 03: Registry Call-Site Rewire (Routing / Staging / Backfill / Presentation) Summary

**The routing seam, staging cron, backfill guards, and Analyze lane presentation now read the Plan-02 registry-derived gates instead of the flat `cloud_target` / `cloud_max_in_flight`: Class-A on/off sites read `settings.cloud_enabled`, Class-B a1/k8s forks read the transitional `active_cloud_kind` / `active_cap` accessors, and the dashboard renders off a NEUTRAL `cloud_lane_kind` context key — leaving ZERO `cloud_target` string in `pipeline.py`, `release_awaiting_cloud.py`, or the three partials, all-local behavior byte-identical, and the config `cloud_target` field still present (removed in Plan 06).**

## Performance
- **Duration:** ~35 min (incl. ephemeral-DB bring-up + colima-flake isolation)
- **Tasks:** 3
- **Files modified:** 9 (0 created)

## Accomplishments
- **Task 1 — staging cron (`release_awaiting_cloud.stage_cloud_window`):** the on/off gate reads `not cfg.cloud_enabled` (Class A), the window cap reads `cfg.active_cap` (was `cloud_max_in_flight`), the GATE-1 compute probe forks on `cfg.active_cloud_kind == "compute"`, and the S3-vs-rsync dispatch forks on `cfg.active_cloud_kind == "kueue"` (Class B). Every `{"staged":0,"skipped":0}` no-op early-return preserved (T-50-cron-raise). No `cloud_target` read or comment remains.
- **Task 2 — routing seam + backfill (`pipeline.py`):** both `_route_discovered_by_duration` bool args pass `settings.cloud_enabled`; the backfill no-op guard reads `not settings.cloud_enabled`; the held-file ledger-seed fork reads `settings.active_cloud_kind == "kueue"`; the Class-C presentation is bound under the neutral `cloud_lane_kind` key (`"local"` when all-local, else `active_cloud_kind`). The stale 791–805 mypy-narrowing comment block was rewritten; no `cloud_target` string survives.
- **Task 3 — Analyze lane templates:** the `cloud_target` context var is renamed to `cloud_lane_kind` in all three partials, the lane conditionals compare `compute`↔A1 / `kueue`↔k8s, the backfill "disabled" copy drops the `cloud_target=local` string, and the stale `config.py:406` comment is refreshed. No lane redesign (Phase 71 owns BEUI-01).

## Task Commits

| Task | Name | Commit |
| ---- | ---- | ------ |
| 1 | Rewire the staging cron off cloud_target onto registry reads | `9debf65` |
| 2 | Rewire the routing seam + backfill onto registry reads | `1cc3207` |
| 3 | Render Analyze lanes off the neutral cloud_lane_kind var | `831ca83` |

## Files Modified
- `src/phaze/tasks/release_awaiting_cloud.py` — cloud_enabled gate + active_cap + active_cloud_kind forks; type-ignore comments migrated to the new accessor names.
- `src/phaze/routers/pipeline.py` — cloud_enabled routing args (395/699) + backfill guard (763), active_cloud_kind ledger fork (805), neutral `cloud_lane_kind` context bind (572); all `cloud_target` comments scrubbed.
- `src/phaze/templates/pipeline/partials/{analyze_workspace,_lane_card,backfill_response}.html` — `cloud_target` context var renamed to `cloud_lane_kind`; compute/kueue lane conditionals; copy/comment cleanup.
- `tests/analyze/core/test_staging_cron.py` — `_StubCfg` + `_patch_settings` now model the registry-derived reads (cloud_enabled/active_cap/active_cloud_kind); implicit-local no-op test added (`test_cloud_disabled_stages_nothing`).
- `tests/shared/routers/test_pipeline.py` — autouse fixture + backfill overrides drive the registry via `settings.backends`; new `test_dashboard_context_binds_cloud_lane_kind` asserts the neutral key + absence of `cloud_target`.
- `tests/shared/core/test_routing_seam.py` — k8s-resolution test rebuilt to read the real `settings.cloud_enabled` property from a kueue registry.
- `tests/shared/core/test_enrich_analyze_workspaces.py` — implicit-local render docstring refreshed; new `test_lane_cards_render_on_compute_registry` proves the A1 lane resolves off `cloud_lane_kind`.

## Deviations from Plan

### Test-strategy choice (mechanism, not behavior)
The plan suggested driving the registry through the `backends_toml_env` conftest fixture. I instead drove it directly:
- **pipeline / routing-seam / enrich tests:** patch the `settings.backends` field (or `settings.model_copy(update=...)`) with a single `ComputeBackend` / `KueueBackend` / `LocalBackend`, so the endpoints read through the REAL `cloud_enabled` / `active_cloud_kind` `@property` logic.
- **staging-cron tests:** the cron monkeypatches `get_settings`, so the `_StubCfg` was updated to expose `cloud_enabled` / `active_cap` / `active_cloud_kind` (the exact reads the rewire makes).

Rationale: both approaches "drive the registry"; the field-patch/stub path exercises the same registry-derived properties while staying fully hermetic (no toml file, no `PHAZE_BACKENDS_CONFIG_FILE` env, no new `ControlSettings` construction / `models_path` drift). Behavior asserted is identical to the plan's intent. No scope change.

No Rule 1–4 auto-fixes were required.

## Issues Encountered
- **Colima DB flake (infra, not a regression):** running the four DB-backed test files together intermittently raised asyncpg `connection_lost()` / `UniqueViolationError (pk_agents / legacy-application-server)` during fixture setup — the documented colima VM-pressure flake (the erroring subset changes run-to-run). Every affected test passes in isolation, and a final clean batch run was **124 passed / 0 errors**. Recreated the ephemeral `phaze-test-db` mid-run once to clear leftover state from a crashed prior run.

## Threat Surface
All register threats mitigated as planned:
- **T-67-03-01 (scope creep):** rewire reads ONLY (`cloud_enabled` / transitional accessors); no `Backend` protocol, no `services/kube_staging` / `services/s3_staging` import added to either module.
- **T-67-03-02 (DoS via lost no-op):** every `{"staged":0,"skipped":0}` early-return preserved; `not cloud_enabled` is byte-identical to the former all-local selector for the all-local deploy (regression test `test_cloud_disabled_stages_nothing`).
- **T-67-03-03 (template render break):** router supplies a transitional value under the neutral `cloud_lane_kind` key; the three partials read it and the Analyze workspace renders with no exception for both the implicit-local and single-compute registries (render tests).
- **T-67-03-SC:** zero new dependencies; no install task.

No new security-relevant surface beyond the threat model.

## Known Stubs
None.

## Verification
- `uv run pytest tests/analyze/core/test_staging_cron.py tests/shared/routers/test_pipeline.py tests/shared/core/test_routing_seam.py tests/shared/core/test_enrich_analyze_workspaces.py` → **124 passed** (clean batch run).
- `grep -c cloud_target src/phaze/tasks/release_awaiting_cloud.py` → 0; `grep -c cloud_target src/phaze/routers/pipeline.py` → 0.
- `grep -rc cloud_target` across the three Analyze partials → 0 each.
- `uv run mypy src/phaze/routers/pipeline.py src/phaze/tasks/release_awaiting_cloud.py` → clean.
- Pre-commit hooks (ruff, ruff-format, bandit, mypy) pass on every commit — no `--no-verify`.

## Next Phase Readiness
- Plan 06's package-wide `grep -rc cloud_target src/phaze == 0` gate now holds for `pipeline.py`, `release_awaiting_cloud.py`, and the three templates (the remaining call sites — `agent_s3.py` / `agent_push.py` / `controller.py` — are Plans 04/05).
- The `cloud_target` / `cloud_max_in_flight` config FIELDS remain on `ControlSettings` (removed in Plan 06 after every call site is rewired), so the tree stays green.
- No blockers.

## Self-Check: PASSED
- `src/phaze/tasks/release_awaiting_cloud.py`, `src/phaze/routers/pipeline.py`, and the three partials all present on disk with the rewired reads.
- All three task commits (`9debf65`, `1cc3207`, `831ca83`) present in git history.

---
*Phase: 67-backend-registry-config-model*
*Completed: 2026-07-03*
