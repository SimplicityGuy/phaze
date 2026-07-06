---
phase: 75
slug: engineering-hygiene-guard-hardening-tech-debt-stale-tracking
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-07-06
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
| HYG-04 (analyze API) | HYG-04 | 1 | HYG-04 | — | N/A (internal operator toggle, Phase 71) | route | `uv run pytest tests/shared/routers/test_pipeline.py -k force_local_analyze_api -x` | ❌ W0 | ⬜ pending |
| HYG-04 (analyze UI) | HYG-04 | 1 | HYG-04 | — | N/A | route | `uv run pytest tests/shared/routers/test_pipeline.py -k force_local_analyze_ui -x` | ❌ W0 | ⬜ pending |
| HYG-04 (backfill no-op) | HYG-04 | 1 | HYG-04 | — | N/A | route | `uv run pytest tests/shared/routers/test_pipeline.py -k force_local_backfill -x` | ❌ W0 | ⬜ pending |
| HYG-04 (False control) | HYG-04 | 1 | HYG-04 | — | N/A | route | `uv run pytest tests/shared/routers/test_pipeline.py -k force_local -x` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

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

- [ ] New force-local test region in `tests/shared/routers/test_pipeline.py` (3–4 cases) — covers HYG-04 gate sites L396/L718/L793.

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

- [ ] HYG-04 has automated route-level verify (4 cases) with Wave 0 dependency
- [ ] Sampling continuity: HYG-04 covered; reconciliation items validated by `just docs-drift`/`git grep`
- [ ] Wave 0 covers the missing test region
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter (planner sets once tasks map cleanly)

**Approval:** pending
