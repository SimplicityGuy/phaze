---
phase: 71
slug: deployment-config-docs-n-lane-ui
status: draft
nyquist_compliant: true
wave_0_complete: false
created: 2026-07-04
---

# Phase 71 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x (pytest-asyncio, httpx AsyncClient / TestClient) |
| **Config file** | `pyproject.toml` ([tool.pytest.ini_options]) |
| **Quick run command** | `uv run pytest tests/<touched>/ -q` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~full-suite (bucketed in CI); quick subset ~seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/<touched>/ -q`
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite green + `scripts/coverage_floor.py` per-module ≥90 AND project `fail_under=95`
- **Max feedback latency:** 60 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 01-T1 | 71-01 | 1 | BEUI-01 | T-71-01, T-71-02 | admission GROUP BY + bounded isolated probes; log backend_id only | unit | `uv run pytest tests/shared/services/test_lane_snapshot.py -k "admission_per_backend or probe_timeout_isolation" -x` | ❌ new (Wave 0) | ⬜ |
| 01-T2 | 71-01 | 1 | BEUI-01 | T-71-01, T-71-03 | rank-ascending secret-free lane dicts; `[]` on error | unit | `uv run pytest tests/shared/services/test_lane_snapshot.py -x` | ❌ new (Wave 0) | ⬜ |
| 02-T1 | 71-02 | 1 | BEUI-02 | T-71-04 | route_control table + migration 031 bound-param seed | integration (migration) | `uv run pytest tests/integration/test_migrations/test_migration_031_route_control.py -x` | ❌ new (Wave 0) | ⬜ |
| 02-T2 | 71-02 | 1 | BEUI-02 | T-71-03 | get_route_control degrades to False on error | unit | `uv run pytest tests/shared/routers/test_routing.py -k route_control_degrades -x` | ❌ new (Wave 0) | ⬜ |
| 02-T3 | 71-02 | 1 | BEUI-02 | T-71-08 | drain no-op + duration router routes-local when forced; select_backend pure | unit | `uv run pytest tests/analyze/core/test_staging_cron.py -k forced_local tests/shared/routers/test_routing.py -k "route_forced_local_no_hold or route_control_degrades" -x` | ⚠️ extend + ❌ new | ⬜ |
| 03-T1 | 71-03 | 2 | BEUI-01 | T-71-06 | lanes seeded identically both builders; cloud_lane_kind retired | unit | `uv run pytest tests/shared/routers/test_pipeline.py -k "lanes or dashboard_context" -x` | ⚠️ extend :1026 | ⬜ |
| 03-T2 | 71-03 | 2 | BEUI-01 | T-71-05, T-71-06 | N cards OOB on #analyze-lanes, rank order, WCAG word-labels, no secrets | integration (render) | `uv run pytest tests/shared/routers/test_enrich_analyze_workspaces.py -k lane -x` | ❌ new (Wave 0) | ⬜ |
| 04-T1 | 71-04 | 2 | BEUI-02 | T-71-07 | force-local write round-trip; boolean-coerced form; internal realm | integration | `uv run pytest tests/shared/routers/test_routing.py -k force_local_toggle_roundtrip -x` | ❌ new (Wave 0) | ⬜ |
| 04-T2 | 71-04 | 2 | BEUI-02 | T-71-05, T-71-10 | pill state seeded on every page; no optimistic mutation; word-labelled | integration | `uv run pytest tests/shared/routers/test_routing.py -k "pill or force_local" -x` | ❌ new (Wave 0) | ⬜ |
| 05-T1 | 71-05 | 1 | BEUI-03 | T-71-12 | configuration.md contradiction reconciled; no code touched | docs guard | `uv run pytest tests/shared/core/test_docs_beui03.py -k configuration -x` | ❌ new (Wave 0) | ⬜ |
| 05-T2 | 71-05 | 1 | BEUI-03 | T-71-11 | runbook covers toggle/lanes/spillover/held-files/_FILE; no secret values | docs guard | `uv run pytest tests/shared/core/test_docs_beui03.py -x` | ❌ new (Wave 0) | ⬜ |

---

## Wave 0 Requirements

New test files the executor creates before/while implementing (all use existing pytest + fresh-DB fixtures + httpx AsyncClient + template-render assertions — no new infra):

- [ ] `tests/shared/services/test_lane_snapshot.py` — snapshot shape, rank order, `[]` degrade, per-`backend_id` admission GROUP BY, probe-timeout isolation (Plan 01)
- [ ] `tests/integration/test_migrations/test_migration_031_route_control.py` — table create + seeded `'global'` row + downgrade (Plan 02-T1)
- [ ] `tests/shared/routers/test_routing.py` — get_route_control degrade, drain/router gate, force-local write round-trip, pill state (Plans 02/04)
- [ ] `tests/shared/core/test_docs_beui03.py` — hermetic docs-content guard for runbook + configuration.md reconciliation (Plan 05)
- [ ] extend `tests/shared/routers/test_pipeline.py:1026` — new `lanes` key in both builders; retire `cloud_lane_kind` assertions (Plan 03-T1)
- [ ] extend `tests/analyze/core/test_staging_cron.py` — forced-local drain no-op (Plan 02-T3)
- [ ] template-render test in `tests/shared/routers/test_enrich_analyze_workspaces.py` — `#analyze-lanes` OOB + WCAG word-labels (Plan 03-T2)

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live N-lane render (≥2 lanes) wraps rank-ascending on the 5s poll | BEUI-01 | Visual/perception | Boot local uvicorn + fresh phaze_uat DB with a `backends.toml` holding ≥2 backends; open Analyze; watch the grid refresh |
| Force-local header pill: amber engaged state + toast + persistence across nav | BEUI-02 | Visual/perception | Toggle the header pill; confirm `FORCED LOCAL` amber + polite toast; navigate to another stage and confirm state persists; revert |
| Keyboard/SR operability of the pill (role=switch, focus ring, aria-checked) | BEUI-02 | Perception (a11y) | Tab to the pill, Enter/Space toggles; verify visible focus ring + screen-reader announces switch state |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 60s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
