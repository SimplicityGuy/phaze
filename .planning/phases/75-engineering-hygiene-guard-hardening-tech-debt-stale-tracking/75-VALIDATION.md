---
phase: 75
slug: engineering-hygiene-guard-hardening-tech-debt-stale-tracking
status: verified
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-06
updated: 2026-07-06
---

# Phase 75 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> **Scope note:** Only **HYG-04** is a testable code deliverable. HYG-01/02/03/05 are
> reconciliation edits validated by `just docs-drift` (already wired) + `git grep` guards,
> not by new tests. This strategy therefore centers on HYG-04.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.1.1 + pytest-asyncio 1.4.0 (async routes via httpx `AsyncClient`) |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` |
| **Target file** | `tests/shared/routers/test_pipeline.py` (add a new force-local test region) |
| **Bucket** | `shared` (per `tests/buckets.json`) |
| **Quick run command** | `uv run pytest tests/shared/routers/test_pipeline.py -k force_local -x` |
| **Full suite command** | `uv run pytest` (or `just test-bucket shared` for the isolated bucket) |
| **Estimated runtime** | ~5–15 seconds (single route test file) |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/shared/routers/test_pipeline.py -k force_local -x`
- **After the HYG-04 plan wave:** Run `uv run pytest tests/shared/routers/test_pipeline.py`
- **Before `/gsd:verify-work`:** Full suite must be green + `just docs-drift` green
- **Max feedback latency:** ~15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| HYG-04 (analyze API) | HYG-04 | 1 | HYG-04 | — | N/A (internal operator toggle, Phase 71) | route | `uv run pytest tests/shared/routers/test_pipeline.py -k force_local_analyze_api -x` | ✅ `test_force_local_analyze_api_routes_local_no_hold` | ✅ green |
| HYG-04 (analyze UI) | HYG-04 | 1 | HYG-04 | — | N/A | route | `uv run pytest tests/shared/routers/test_pipeline.py -k force_local_analyze_ui -x` | ✅ `test_force_local_analyze_ui_routes_local_no_hold` | ✅ green |
| HYG-04 (backfill no-op) | HYG-04 | 1 | HYG-04 | — | N/A | route | `uv run pytest tests/shared/routers/test_pipeline.py -k force_local_backfill -x` | ✅ `test_force_local_backfill_zero_mutation_no_op` | ✅ green |
| HYG-04 (False control) | HYG-04 | 1 | HYG-04 | — | N/A | route | `uv run pytest tests/shared/routers/test_pipeline.py -k force_local -x` | ✅ `test_force_local_analyze_api_false_control_still_holds` | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

> **Audit note (2026-07-06):** All 4 cases confirmed green vs the ephemeral test DB (5433). The
> backfill case was strengthened post-code-review (WR-01, commit `049638af`) — it now seeds a
> genuine ledger-scoped candidate (`with_ledger=True`) so the L793 gate is the ONLY thing holding
> it back; **mutation-verified** (removing the `or await get_route_control(session)` clause makes the
> case FAIL), so it is a real anti-cheat rather than a vacuous pass.

### Gate-site coverage (sampling adequacy)

| Gate site | Endpoint | Covered by |
|-----------|----------|------------|
| `pipeline.py:396` (`trigger_analysis`) | `POST /api/v1/analyze` | force-local True case (duration trigger #1) |
| `pipeline.py:718` (`trigger_analysis_ui`) | `POST /pipeline/analyze` | force-local True case (duration trigger #2) |
| `pipeline.py:793` (`trigger_backfill_cloud`) | `POST /pipeline/backfill-cloud` | force-local backfill no-op case |

A True case per trigger + a False control gives 2-sided evidence that the persisted
`RouteControl(force_local=True)` toggle — not some other condition — drives local routing.

### Observable signals (the assertions)

- **Force-local True:** long (`_LONG` ≥ threshold) DISCOVERED file routes **local**; a
  `SELECT ... WHERE state == AWAITING_CLOUD` returns **zero rows** (byte-identical to an all-local
  registry). Backfill returns `count=0, disabled=True`, leaves rows `ANALYSIS_FAILED`, seeds **no**
  `SchedulingLedger` row, enqueues nothing — zero-mutation no-op (T-71-08, `pipeline.py:789-793`).
- **Force-local False (control):** the same `_LONG` file **IS held** `AWAITING_CLOUD` (registry honored).

---

## Wave 0 Requirements

- [x] New force-local test region in `tests/shared/routers/test_pipeline.py` (4 cases) — covers HYG-04 gate sites L396/L718/L793 + a False control. Landed in commits `a01a7bf8` (analyze L396+L718 + control) + `63589cd5` (backfill L793) + `049638af` (WR-01 anti-cheat fix).

*No `conftest.py` changes required — the shared `session`/`client`/queue-fake harness already supports the persisted-`RouteControl`-row + real-route pattern. No framework install.*

**Fixture note (research correction):** `set_route_control(...)` does **not exist**. Seed the toggle
via a direct `RouteControl(id="global", force_local=True)` row insert on the shared `session` (the
`client` fixture overrides `get_session` to that same session; the `RouteControl` table is created by
`Base.metadata.create_all` in conftest). Keep the autouse `_cloud_compute_registry` so the toggle is
the only thing forcing local. Reuse `_make_file`, `_persist_files_with_duration([_LONG])`,
`_persist_failed_with_duration([_LONG])`, `seed_active_agent`, `wire_fakes`/`install_fake_queues`,
`_drain_background`, `_LONG`/`_SHORT`.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Requirement/tracking reconciliation (text edits) | HYG-01, HYG-03, HYG-05 | Doc/state edits, not runtime behavior | `just docs-drift` green + visual review of REQUIREMENTS/STATE/ROADMAP diffs |
| docker-compose comment deletion | HYG-02 | Comment-only change, no behavior | `git grep PHAZE_CLOUD_TARGET` clean + `git grep cloud_target docker-compose.yml` clean |

---

## Validation Sign-Off

- [x] HYG-04 has automated route-level verify (4 cases) with Wave 0 dependency
- [x] Sampling continuity: HYG-04 covered; reconciliation items validated by `just docs-drift`/`git grep`
- [x] Wave 0 covers the missing test region
- [x] No watch-mode flags
- [x] Feedback latency < 15s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** verified 2026-07-06

---

## Validation Audit 2026-07-06

State-A audit of the plan-time strategy against the executed codebase. All HYG-04 cases exist and
run green; the four reconciliation requirements are manual-only by design (docs-drift + `git grep`),
correctly captured in the Manual-Only section. No MISSING/PARTIAL gaps → no `gsd-nyquist-auditor`
spawn needed (pure verification pass).

| Metric | Count |
|--------|-------|
| Gaps found | 0 |
| Resolved | 0 |
| Escalated (manual-only) | 4 (HYG-01/02/03/05 — reconciliation edits, validated by `just docs-drift` + `git grep`) |

Commands re-run green (test DB 5433): `-k force_local` 4 passed; `-k force_local_analyze_api` 2
passed; `-k force_local_analyze_ui` 1 passed; `-k force_local_backfill` 1 passed; docs-drift 10
passed; `git grep PHAZE_CLOUD_TARGET|cloud_target|Phase 67 -- docker-compose.yml` clean.
