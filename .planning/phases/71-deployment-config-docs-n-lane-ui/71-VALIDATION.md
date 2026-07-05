---
phase: 71
slug: deployment-config-docs-n-lane-ui
status: complete
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-04
validated: 2026-07-05
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
| 01-T1 | 71-01 | 1 | BEUI-01 | T-71-01, T-71-02 | admission GROUP BY + bounded isolated probes; log backend_id only | unit | `uv run pytest tests/shared/services/test_lane_snapshot.py -k "admission_per_backend or probe_timeout_isolation" -x` | ✅ 2 | ✅ COVERED |
| 01-T2 | 71-01 | 1 | BEUI-01 | T-71-01, T-71-03 | rank-ascending secret-free lane dicts; `[]` on error | unit | `uv run pytest tests/shared/services/test_lane_snapshot.py -x` | ✅ 15 | ✅ COVERED |
| 02-T1 | 71-02 | 1 | BEUI-02 | T-71-04 | route_control table + migration 031 bound-param seed | integration (migration) | `uv run pytest tests/integration/test_migrations/test_migration_031_route_control.py -x` | ✅ 3 | ✅ COVERED |
| 02-T2 | 71-02 | 1 | BEUI-02 | T-71-03 | get_route_control degrades to False on error | unit | `uv run pytest tests/shared/routers/test_routing.py -k route_control_degrades -x` | ✅ 2 | ✅ COVERED |
| 02-T3 | 71-02 | 1 | BEUI-02 | T-71-08 | drain no-op + duration router routes-local when forced; select_backend pure | unit | `uv run pytest tests/analyze/core/test_staging_cron.py -k forced_local tests/shared/routers/test_routing.py -k "route_forced_local_no_hold or route_control_degrades" -x` | ✅ 1+5 | ✅ COVERED |
| 03-T1 | 71-03 | 2 | BEUI-01 | T-71-06 | lanes seeded identically both builders; cloud_lane_kind retired | unit | `uv run pytest tests/shared/routers/test_pipeline.py -k "lanes or dashboard_context" -x` | ✅ 1 (+92 file) | ✅ COVERED |
| 03-T2 | 71-03 | 2 | BEUI-01 | T-71-05, T-71-06 | N cards OOB on #analyze-lanes, rank order, WCAG word-labels, no secrets | integration (render) | `uv run pytest tests/shared/core/test_enrich_analyze_workspaces.py -k lane -x` | ✅ 4 | ✅ COVERED |
| 04-T1 | 71-04 | 2 | BEUI-02 | T-71-07 | force-local write round-trip; boolean-coerced form; internal realm | integration | `uv run pytest tests/shared/routers/test_routing.py -k "force_local or force-local" -x` | ✅ (test_routing 7) | ✅ COVERED |
| 04-T2 | 71-04 | 2 | BEUI-02 | T-71-05, T-71-10 | pill state seeded on every page; no optimistic mutation; word-labelled | integration | `uv run pytest tests/shared/routers/test_routing.py -k "pill or force_local" -x` | ✅ 2 | ✅ COVERED |
| 05-T1 | 71-05 | 1 | BEUI-03 | T-71-12 | configuration.md contradiction reconciled; no code touched | docs guard | `uv run pytest tests/shared/core/test_docs_beui03.py -k configuration -x` | ✅ 2 | ✅ COVERED |
| 05-T2 | 71-05 | 1 | BEUI-03 | T-71-11 | runbook covers toggle/lanes/spillover/held-files/_FILE; no secret values | docs guard | `uv run pytest tests/shared/core/test_docs_beui03.py -x` | ✅ 9 | ✅ COVERED |

---

## Wave 0 Requirements

New test files the executor creates before/while implementing (all use existing pytest + fresh-DB fixtures + httpx AsyncClient + template-render assertions — no new infra):

- [x] `tests/shared/services/test_lane_snapshot.py` — snapshot shape, rank order, `[]` degrade, per-`backend_id` admission GROUP BY, probe-timeout isolation (Plan 01) — 15 tests incl. the WR-01 DB-poisoning isolation regression
- [x] `tests/integration/test_migrations/test_migration_031_route_control.py` — table create + seeded `'global'` row + downgrade (Plan 02-T1) — 3 tests
- [x] `tests/shared/routers/test_routing.py` — get_route_control degrade, drain/router gate, force-local write round-trip, pill state (Plans 02/04) — 7 tests
- [x] `tests/shared/core/test_docs_beui03.py` — hermetic docs-content guard for runbook + configuration.md reconciliation (Plan 05) — 9 tests
- [x] extend `tests/shared/routers/test_pipeline.py` — new `lanes` key in both builders; retire `cloud_lane_kind` assertions (Plan 03-T1) — file 92 tests
- [x] extend `tests/analyze/core/test_staging_cron.py` — forced-local drain no-op (Plan 02-T3) — 23 tests
- [x] template-render test in `tests/shared/core/test_enrich_analyze_workspaces.py` — `#analyze-lanes` OOB + WCAG word-labels (Plan 03-T2; landed in `tests/shared/core/` not `.../routers/`) — incl. the UAT-01 `#analyze-lanes` sink invariant test

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

**Approval:** approved 2026-07-05 (all 11 mapped tasks COVERED)

---

## Validation Audit 2026-07-05

Post-execution audit (State A). All Wave-0 test files now exist and run green; every plan-time `-k` filter resolved to real tests (03-T2 path corrected `tests/shared/routers/` → `tests/shared/core/` per the 71-03 executor deviation). Two extra regression tests added during the gate sweep are folded into the map (WR-01 DB-poisoning lane-isolation in `test_lane_snapshot.py`; UAT-01 `#analyze-lanes` OOB sink in `test_enrich_analyze_workspaces.py`).

| Metric | Count |
|--------|-------|
| Requirements (BEUI-01/02/03) | 3 |
| Tasks mapped | 11 |
| COVERED | 11 |
| PARTIAL | 0 |
| MISSING | 0 |
| Gaps found | 0 |
| Manual-only (perception/a11y) | 3 — items 1 (live N-lane render) + 2 (pill toggle/persistence) driven live via Playwright, PASS (71-HUMAN-UAT.md); item 3 (keyboard/SR operability) partially confirmed via the Playwright accessibility snapshot (`role="switch"` + `aria-checked` semantics present on the native-button pill), explicit Tab/Enter keydrive not exercised |

Mapped-suite run: `160 passed` across `test_lane_snapshot`, `test_migration_031_route_control`, `test_routing`, `test_staging_cron`, `test_pipeline`, `test_enrich_analyze_workspaces`, `test_docs_beui03` (test-DB pg5433). **nyquist_compliant: true — no gaps to fill.**
