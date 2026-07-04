---
phase: 68
slug: backend-protocol-3-implementations
status: validated
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-03
validated: 2026-07-04
---

# Phase 68 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Behavior-preserving refactor — the golden characterization snapshot (BACK-04) is the acceptance gate.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio (`asyncio_mode = "auto"`) |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/analyze/ tests/shared/ -x` |
| **Full suite command** | `uv run pytest` (or `just test` / `just test-cov`) |
| **Migration integration** | `just integration-test` (needs `just test-db` → `phaze_migrations_test`) |
| **Estimated runtime** | ~60–120 seconds (quick); full suite longer |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/analyze/ tests/shared/ -x`
- **After every plan wave:** Run `uv run pytest` (full suite; on colima flake, re-run failed subset in isolation — do NOT set `PHAZE_QUEUE_URL=redis`)
- **Before `/gsd:verify-work`:** Full suite green + `just integration-test` (migration 029)
- **Max feedback latency:** ~120 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| BACK-01 | 68-03 | 2 | BACK-01 | T-68-04/05/07 | protocol dispatch; bodies never raise/commit | unit | `uv run pytest tests/analyze/services/test_backends.py -x` | ✅ | ✅ green (16) |
| BACK-02 | 68-02 | 1 | BACK-02 | T-68-02/03 | additive migration; no saq_jobs; nullable | migration | `uv run pytest tests/integration/test_migrations/test_migration_029_backend_id.py` | ✅ | ✅ green (3) |
| BACK-03 | 68-03 | 2 | BACK-03 | — | in-flight equivalence (D-02) | unit | `uv run pytest tests/analyze/services/test_backends.py::test_in_flight_equivalence` | ✅ | ✅ green (1) |
| BACK-04 | 68-01→68-04 | 0→3 | BACK-04 | — | GATE-1 asymmetry preserved (D-01a); byte-identical | characterization | `uv run pytest tests/analyze/core/test_dispatch_snapshot.py -x` | ✅ | ✅ green (8) |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky. Every BACK-* ID maps to ≥1 green automated command above. BACK-04 golden captured in Wave 0 (68-01) on post-67 code, held byte-identical through the Wave-3 (68-04) live rewire (one plan-sanctioned compute field flip).*

### Code-Review Fix Regression Coverage (added post-execution)

| Finding | Test | Command | Status |
|---------|------|---------|--------|
| CR-01 (compute cloud_job leak on push-cap failure) | `test_mismatch_over_cap_terminalizes_compute_cloud_job` | `uv run pytest tests/agents/routers/test_agent_push.py` | ✅ green (9) |
| WR-02 (mid-tick fileserver → clean hold; cron never raises) | `test_fileserver_vanishes_mid_tick_holds_cleanly` | `uv run pytest tests/analyze/core/test_staging_cron.py` | ✅ green (in 41) |
| WR-01 (`resolved_non_local_kind` >1-non-local fail-fast) | `test_resolved_non_local_kind_raises_on_multiple_non_local` | `uv run pytest tests/analyze/services/test_backends.py` | ✅ green (in 16) |

---

## Validation Layers (behavior-preserving refactor)

**Layer 1 — Golden characterization snapshot (D-01, acceptance gate) → BACK-04**
Record the observable side-effect sequence over `{compute, kueue, local} × {agent up, agent down}` on
today's post-67 code, then assert unchanged after the refactor. Capture per cell: agent gate checked
vs skipped (`select_active_agent(kind="compute")` called for compute; NOT called for kueue — D-01a),
staging call (`_stage_file_to_s3` vs `_enqueue_push_file`), FileState transition (`AWAITING_CLOUD →
PUSHING`), `cloud_job` upsert (present for kueue; NEW for compute), enqueue (`s3_upload` vs
`push_file`; dedup no-op = skipped), tally (`{"staged":N,"skipped":M}`). Matrix truths: compute+down →
`{staged:0}` no-op (GATE-1); kueue+down → proceeds (GATE-1 skipped). Mechanism: `AsyncMock` on the
boundaries + serialize ordered call log + DB rows to an inline expected-dict per cell.

**Layer 2 — Equivalence invariant (D-02) → BACK-03**
`sum(in_flight_count(b)) == get_cloud_window_count()` for the single-backend case, over constructed
FileState/`cloud_job` states. In-flight status set = `{UPLOADING, UPLOADED, SUBMITTED, RUNNING}`
(Q3 recommendation). Guards Pitfall 1 double-count. Scope (prod-live vs characterization-only) governed
by the Q2 decision recorded in the plan.

**Layer 3 — Per-backend protocol unit tests (≥12 cells) → BACK-01/02/03**
3 impls × 4 methods. `is_available`: Local→always True; Compute→heartbeat GATE-1; Kueue→kube probe,
no compute dependency, returns bool never raises. `dispatch` D-03 atomicity: rollback between flip and
row-write → no limbo row (FileState in-flight ⟺ live non-terminal `cloud_job`). `in_flight_count` →
correct `COUNT(... WHERE backend_id AND status IN in-flight)`.

**Layer 4 — Migration test (029) → BACK-02**
Mirror `tests/integration/test_migrations/test_migration_026_kube_columns.py`: static revision-id /
down-revision assertions without a DB (additive-only, bare-number `029`, revises `028`); integration
body upgrades 028→029, asserts `backend_id` column exists + nullable + no backfill, downgrades and
asserts gone. Grep-assert the migration never references `saq_jobs`.

**Layer 5 — Call-site rewire regression (Q1)**
Removing `active_cloud_kind` must not change dashboard `cloud_lane_kind`, the pipeline ledger-seed fork
(`pipeline.py:810`), the `agent_s3` guard, and the controller LocalQueue-probe gate. Assert each reader
resolves through the new backend resolution without behavior change.

---

## Wave 0 Requirements

- [x] `tests/analyze/services/test_backends.py` — protocol unit tests (Layer 3) + invariant (Layer 2) — 16 green
- [x] `tests/analyze/core/test_dispatch_snapshot.py` — golden matrix (Layer 1) covering BACK-04 — 8 green, byte-identical
- [x] `tests/integration/test_migrations/test_migration_029_backend_id.py` — migration (Layer 4) — 3 green
- [x] Snapshot fixture shape/serialization — inline expected-dict per cell (as recommended)
- [x] Framework install: none — pytest/pytest-asyncio already present.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| (none) | — | Refactor is fully unit/characterization/migration testable | — |

*All phase behaviors have automated verification. No live-deploy behavior in scope (behavior-preserving; single-dispatch-path unchanged).*

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (plan 68-01 creates the 3 test files)
- [x] No watch-mode flags
- [x] Feedback latency < 120s
- [x] `nyquist_compliant: true` set in frontmatter
- [x] `wave_0_complete` — flipped true; plan 68-01 executed (golden captured)

**Approval:** approved 2026-07-03 (plan-checker VERIFICATION PASSED; contract satisfied by plans 68-01..05)

---

## Validation Audit 2026-07-04

Post-execution audit (State A). Every requirement re-run against the executed codebase on a fresh test DB.

| Metric | Count |
|--------|-------|
| Requirements audited | 4 (BACK-01..04) |
| COVERED (green automated) | 4 |
| PARTIAL / MISSING gaps | 0 |
| Gaps resolved | 0 (none found) |
| Escalated / manual-only | 0 |

**Result:** NYQUIST-COMPLIANT. All BACK-* requirements have green automated verification; the three code-review-fix findings (CR-01/WR-01/WR-02) each carry a dedicated regression test. No gap-filling required — auditor not spawned (Step 3 no-gap short-circuit).
