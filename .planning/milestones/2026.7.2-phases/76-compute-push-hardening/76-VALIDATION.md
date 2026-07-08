---
phase: 76
slug: compute-push-hardening
status: compliant
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-06
---

# Phase 76 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Reconstructed from artifacts (State B) — no gaps: every requirement has green automated verification.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x + pytest-asyncio |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest tests/shared/services/test_lane_snapshot.py -q` |
| **Full suite command** | `just test-db && TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test" uv run pytest tests/shared/services/test_lane_snapshot.py tests/agents/routers/test_agent_push.py tests/identify/routers/test_tracklists.py tests/shared/routers/test_pipeline_scans.py -q; just test-db-down` |
| **Estimated runtime** | ~40 seconds (four modules, real Postgres) |

---

## Sampling Rate

- **After every task commit:** Run the targeted module for the touched requirement.
- **After every plan wave:** Run the four-module full suite command above.
- **Before `/gsd:verify-work`:** Full suite must be green.
- **Max feedback latency:** ~40 seconds.

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 76-01-01 | 01 | 1 | HARD-01 | WR-01 (74-REVIEW) | N≥2 compute probes never touch the shared AsyncSession concurrently (serialized fan-out); deterministic per-backend availability; bounded per-probe timeout preserved | unit | `uv run pytest tests/shared/services/test_lane_snapshot.py -q` | ✅ | ✅ green |
| 76-02-01 | 02 | 1 | HARD-02 | AR-73-02 / T-73-13 / WR-04 | Concurrent `/mismatch` cannot lose a `push_attempt` increment (advisory-xact-lock RMW); cap trips at boundary; real before_enqueue hook does not deadlock | integration (real PG) | `TEST_DATABASE_URL=…:5433/phaze_test uv run pytest tests/agents/routers/test_agent_push.py -q` | ✅ | ✅ green |
| 76-03-01 | 03 | 1 | HARD-03 | AR-30-03 / Phase-30 REVIEW IN-01 | Malformed `agent_id` → 422 at both HTTP boundaries (`scan_status`, `agent_roots_swap`); well-formed id still passes | integration | `uv run pytest tests/identify/routers/test_tracklists.py tests/shared/routers/test_pipeline_scans.py -q` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

### Covering tests

- **HARD-01:** `test_compute_probe_real_fanout_keeps_both_lanes_online` (deterministic N≥2 probe), `test_probe_timeout_isolation`, `test_probe_local_short_circuit_no_io`, `test_probe_failure_degrades_to_offline`, `test_probe_reports_backend_availability` — `tests/shared/services/test_lane_snapshot.py`.
- **HARD-02:** `test_mismatch_concurrent_no_lost_update` (RED-verified genuine row contention → `push_attempt == 2`), `test_mismatch_cap_trips_exactly_at_boundary`, `test_mismatch_real_enqueue_hook_does_not_deadlock` (RED-verified: hangs on row-lock, passes on advisory lock) — `tests/agents/routers/test_agent_push.py`.
- **HARD-03:** `test_scan_status_malformed_agent_id_returns_422` / `_well_formed_agent_id_passes_validation` — `tests/identify/routers/test_tracklists.py`; `test_agent_roots_swap_malformed_agent_id_returns_422` / `_well_formed_agent_id_passes_validation` — `tests/shared/routers/test_pipeline_scans.py`.

---

## Wave 0 Requirements

Existing infrastructure covers all phase requirements. No Wave 0 test scaffolding was needed — all three requirements extended existing test modules with fixtures already in place (`session`/`client` real-Postgres fixtures, `install_fake_queues`).

---

## Manual-Only Verifications

All phase behaviors have automated verification.

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify (no Wave 0 dependencies)
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (none — no gaps)
- [x] No watch-mode flags
- [x] Feedback latency < 45s
- [x] `nyquist_compliant: true` set in frontmatter

**Audit result (2026-07-06):** 3/3 requirements COVERED · 0 PARTIAL · 0 MISSING. Full four-module suite: **155 passed** against the port-5433 real-Postgres test DB.

**Approval:** approved 2026-07-06
