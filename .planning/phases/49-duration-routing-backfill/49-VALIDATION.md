---
phase: 49
slug: duration-routing-backfill
status: validated
nyquist_compliant: true
wave_0_complete: true
created: 2026-06-25
audited: 2026-06-25
---

# Phase 49 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Derived from 49-RESEARCH.md "Validation Architecture". Task-ID rows are filled by the planner/executor; the success-criterion map below is the authoritative coverage contract.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio (real Postgres `session` fixture) |
| **Config file** | `pyproject.toml` (`[tool.pytest.*]`), `tests/conftest.py` |
| **Quick run command** | `uv run pytest tests/test_services/test_enqueue_router.py tests/test_routers/test_pipeline.py -x` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` (test DB via `just test-db` + `TEST_DATABASE_URL`/`MIGRATIONS_TEST_DATABASE_URL`/`PHAZE_REDIS_URL`, or one-shot `just integration-test`) |
| **Estimated runtime** | ~quick <30s · full suite ~4–5 min |

---

## Sampling Rate

- **After every task commit:** Run the quick run command (plus the specific new test file for the task).
- **After every plan wave:** Run the full suite command.
- **Before `/gsd:verify-work`:** Full suite must be green (≥85% coverage).
- **Max feedback latency:** ~30 seconds (quick run).

---

## Per-Success-Criterion Verification Map

Coverage contract from research. Every row MUST have at least one automated test before the phase gate.

| # | Success Criterion / Decision | Observable signal | Test Type | Target test file | Status |
|---|------------------------------|-------------------|-----------|------------------|--------|
| SC-1 | CLOUDROUTE-01: ≥threshold → compute queue | A ≥5400s file enqueues `process_file` onto `phaze-agent-<compute-id>`, NOT the fileserver queue | router/integration | `tests/test_routers/test_pipeline.py::test_analyze_long_file_routes_to_compute_queue`, `::test_analyze_long_routes_compute_even_without_fileserver` | ✅ green |
| SC-2 | CLOUDROUTE-03: sub-threshold/null → local unchanged | A <5400s or null-duration file enqueues `process_file` onto `phaze-agent-<fileserver-id>` with the same key/payload/policy as today | router/service | `tests/test_routers/test_pipeline.py::test_analyze_short_and_null_route_to_fileserver_with_key` | ✅ green |
| SC-3 | CLOUDROUTE-02: no compute online → held, never local | A ≥threshold file with only a fileserver agent online ends in `state=AWAITING_CLOUD`, NO `process_file` enqueue captured; count card shows it; split-count reports `awaiting`; recovery never replays a held row onto a fileserver | router + service + template + recovery | `tests/test_routers/test_pipeline.py::test_analyze_long_file_no_compute_holds_awaiting_cloud`, `::test_analyze_ui_reports_split_counts`, `::test_analyze_ui_no_agents_surfaces_held_count`, `::test_dashboard_renders_awaiting_cloud_card`, `::test_stats_partial_emits_awaiting_cloud_card_oob`; `tests/test_services/test_pipeline.py::test_get_awaiting_cloud_count_happy_path`, `::test_get_awaiting_cloud_count_degrades_to_zero_on_db_error`; `tests/test_tasks/test_recovery.py::test_held_process_file_row_skips_when_only_fileserver_online`, `::test_held_process_file_row_routes_to_compute_when_online` | ✅ green |
| SC-4 | CLOUDROUTE-04: ledger-scoped backfill of 144, no over-enqueue | Backfill enqueues exactly the `ANALYSIS_FAILED ∧ duration≥threshold` set; double-click dedups to no-op (deterministic key); never-failed/short files untouched | router + service | `tests/test_routers/test_pipeline.py::test_backfill_selects_long_failed_resets_and_routes_to_compute`, `::test_backfill_no_compute_holds_awaiting_cloud_with_ledger_row`, `::test_backfill_enqueued_branch_has_no_explicit_ledger_row`, `::test_backfill_double_click_enqueues_nothing_new`, `::test_backfill_zero_candidates_returns_empty_fragment`; `tests/test_services/test_pipeline.py::test_backfill_candidates_filters_by_state_and_duration`, `::test_backfill_candidates_boundary_is_inclusive` | ✅ green |
| D-13 | Kind-filtered agent selection | `select_active_agent(session, kind="compute")` returns only the compute agent; `kind="fileserver"` excludes it; no-match raises `NoActiveAgentError`; no-kind back-compat preserved | unit | `tests/test_services/test_enqueue_router.py::test_select_active_agent_kind_compute_returns_only_compute`, `::test_select_active_agent_kind_fileserver_excludes_compute`, `::test_select_active_agent_no_kind_preserves_back_compat`, `::test_select_active_agent_kind_absent_raises` | ✅ green |
| D-04 | AWAITING_CLOUD stays pending | An `AWAITING_CLOUD` file is NOT in the analyze done-set `{ANALYZED, ANALYSIS_FAILED}` / not treated domain-completed | unit | `tests/test_tasks/test_recovery.py::test_awaiting_cloud_file_stays_pending_in_recovery` | ✅ green |
| D-03 | Held-file release cron (state-driven) | `release_awaiting_cloud` scans `state=AWAITING_CLOUD`, and when a compute agent is online enqueues to compute + resets state to DISCOVERED; no-op when no compute agent / no held files; dedup of a live key still resets state; registered as `*/5` cron; FastAPI-free | task/integration | `tests/test_tasks/test_release_awaiting_cloud.py::test_release_enqueues_to_compute_and_resets_state`, `::test_no_op_when_no_compute_agent_online`, `::test_no_op_when_no_held_files`, `::test_dedup_already_live_key_still_resets_state`, `::test_release_registered_in_controller_functions_and_cron`, `::test_release_module_is_fastapi_free` | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [x] Extend `seed_active_agent` (`tests/_queue_fakes.py`) with a `kind` param (default `"fileserver"`) so a `kind="compute"` agent can be seeded. (RESEARCH A3)
- [x] New test fixtures: FileRecord + FileMetadata.duration pairs (≥threshold, <threshold, null) reusing the real PG `session` (`_persist_files_with_duration`, `_LONG`/`_SHORT`).
- [x] Confirm `FakeTaskRouter`/`DedupFakeTaskRouter` capture the per-agent queue name so a test can assert compute-vs-fileserver destination (confirmed — `router.queues[<agent_id>].captured`).
- [x] No framework install needed — existing infra covers all of it.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live "144" backfill count | CLOUDROUTE-04 | Depends on live Postgres data (A1), not statically verifiable. The backfill SELECTION logic IS automated (`test_backfill_candidates_*`); only the live-count match needs eyeballing. | Before trusting the figure, run `SELECT count(*) FROM files f JOIN metadata m ON m.file_id=f.id WHERE f.state='analysis_failed' AND m.duration >= 5400;` and confirm the dashboard button label matches |
| Awaiting-cloud card 5s OOB poll | CLOUDROUTE-02 | The OOB swap cadence needs a live browser to confirm end-to-end (rendering + count are unit-tested; the HTMX poll cycle is not). | Open the pipeline dashboard with held files and confirm the "Awaiting cloud" card count updates on the 5s poll and drops as the release cron drains held files. Tracked in `49-HUMAN-UAT.md`. |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 30s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** validated 2026-06-25 — all 7 success-criteria/decision rows have green automated coverage.

---

## Validation Audit 2026-06-25

| Metric | Count |
|--------|-------|
| Criteria audited | 7 |
| COVERED (green) | 7 |
| PARTIAL | 0 |
| MISSING (gaps found) | 0 |
| Gaps filled this audit | 0 |
| Escalated to manual-only | 0 |

All success criteria and load-bearing decisions resolved to green automated tests; no gap-fill needed. The two manual-only rows are live-data / live-browser confirmations, not requirement-logic gaps. CR-01 (review) hardened SC-3's recovery path with `test_held_process_file_row_*`; WR-01 added `test_analyze_ui_no_agents_surfaces_held_count`.
