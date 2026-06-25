---
phase: 49
slug: duration-routing-backfill
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-06-25
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
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~quick <30s · full suite per existing CI |

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
| SC-1 | CLOUDROUTE-01: ≥threshold → compute queue | A ≥5400s file enqueues `process_file` onto `phaze-agent-<compute-id>`, NOT the fileserver queue | router/integration | `tests/test_routers/test_pipeline.py` (FakeQueue/FakeTaskRouter capture) | ⬜ pending |
| SC-2 | CLOUDROUTE-03: sub-threshold/null → local unchanged | A <5400s or null-duration file enqueues `process_file` onto `phaze-agent-<fileserver-id>` with the same key/payload/policy as today | router/service | `tests/test_routers/test_pipeline.py`; `tests/test_services/test_analysis_enqueue.py` | ⬜ pending |
| SC-3 | CLOUDROUTE-02: no compute online → held, never local | A ≥threshold file with only a fileserver agent online ends in `state=AWAITING_CLOUD`, NO `process_file` enqueue captured; count card shows it; split-count reports `awaiting` | router + service + template | `tests/test_routers/test_pipeline.py`; `get_awaiting_cloud_count` in `tests/test_services/test_pipeline.py` | ⬜ pending |
| SC-4 | CLOUDROUTE-04: ledger-scoped backfill of 144, no over-enqueue | Backfill enqueues exactly the `ANALYSIS_FAILED ∧ duration≥threshold` set; double-click dedups to no-op (deterministic key); never-failed/short files untouched | router + service | `tests/test_routers/test_pipeline.py`; `tests/test_services/test_pipeline.py`; `tests/test_tasks/test_recovery.py` | ⬜ pending |
| D-13 | Kind-filtered agent selection | `select_active_agent(session, kind="compute")` returns only the compute agent; `kind="fileserver"` excludes it; no-match raises `NoActiveAgentError` | unit | `tests/test_services/test_enqueue_router.py` | ⬜ pending |
| D-04 | AWAITING_CLOUD stays pending | An `AWAITING_CLOUD` file is NOT in the analyze done-set `{ANALYZED, ANALYSIS_FAILED}` / not treated domain-completed | unit | `tests/test_tasks/test_recovery.py` | ⬜ pending |
| D-03 | Held-file release cron (state-driven) | `release_awaiting_cloud` scans `state=AWAITING_CLOUD`, and when a compute agent is online enqueues to compute + resets state to DISCOVERED; no-op when no compute agent | task/integration | `tests/test_tasks/test_recovery.py` or new `tests/test_tasks/test_release_awaiting_cloud.py` | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] Extend `seed_active_agent` (`tests/_queue_fakes.py`) with a `kind` param (default `"fileserver"`) so a `kind="compute"` agent can be seeded. (RESEARCH A3)
- [ ] New test fixtures: FileRecord + FileMetadata.duration pairs (≥threshold, <threshold, null) reusing the real PG `session`.
- [ ] Confirm `FakeTaskRouter`/`DedupFakeTaskRouter` capture the per-agent queue name so a test can assert compute-vs-fileserver destination (research confirms they do).
- [ ] No framework install needed — existing infra covers all of it.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live "144" backfill count | CLOUDROUTE-04 | Depends on live Postgres data (A1), not statically verifiable | Before trusting the figure, run `SELECT count(*) FROM files f JOIN metadata m ON m.file_id=f.id WHERE f.state='analysis_failed' AND m.duration >= 5400;` and confirm the dashboard button label matches |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 30s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
