---
phase: 68
slug: backend-protocol-3-implementations
status: approved
nyquist_compliant: true
wave_0_complete: false
created: 2026-07-03
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
| (Wave 0) | — | 0 | BACK-01/03/04 | — | N/A | unit/characterization | `uv run pytest tests/analyze/services/test_backends.py tests/analyze/core/test_dispatch_snapshot.py` | ❌ W0 | ⬜ pending |
| BACK-01 | TBD | — | BACK-01 | — | N/A | unit | `uv run pytest tests/analyze/services/test_backends.py -x` | ❌ W0 | ⬜ pending |
| BACK-02 | TBD | — | BACK-02 | — | N/A | migration | `uv run pytest tests/integration/test_migrations/test_migration_029_backend_id.py` | ❌ W0 | ⬜ pending |
| BACK-03 | TBD | — | BACK-03 | — | N/A | unit | `uv run pytest tests/analyze/services/test_backends.py::test_in_flight_equivalence` | ❌ W0 | ⬜ pending |
| BACK-04 | TBD | — | BACK-04 | — | GATE-1 asymmetry preserved | characterization | `uv run pytest tests/analyze/core/test_dispatch_snapshot.py -x` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky. Task IDs finalized by the planner; every BACK-* ID must map to at least one automated command above.*

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

- [ ] `tests/analyze/services/test_backends.py` — protocol unit tests (Layer 3) + invariant (Layer 2)
- [ ] `tests/analyze/core/test_dispatch_snapshot.py` — golden matrix (Layer 1) covering BACK-04
- [ ] `tests/integration/test_migrations/test_migration_029_backend_id.py` — migration (Layer 4)
- [ ] Snapshot fixture shape/serialization (Claude's Discretion — inline expected-dict per cell recommended)
- [ ] Framework install: none — pytest/pytest-asyncio already present; reuse `tests/_queue_fakes.py`, `tests/kube_fakes.py`.

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
- [ ] `wave_0_complete` — flips to true once plan 68-01 executes

**Approval:** approved 2026-07-03 (plan-checker VERIFICATION PASSED; contract satisfied by plans 68-01..05)
