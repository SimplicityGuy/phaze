---
phase: 73
slug: per-agent-dispatch-liveness-scratch-failure-isolation
status: complete
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-05
updated: 2026-07-05
---

# Phase 73 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x (pytest-asyncio) |
| **Config file** | pyproject.toml `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/analyze/services/test_backends.py tests/agents/routers/test_agent_push.py tests/analyze/core/test_push_pipeline.py -x -q` |
| **Full suite command** | `uv run pytest -q` |
| **Estimated runtime** | ~90 seconds (targeted) / several minutes (full) |

---

## Sampling Rate

- **After every task commit:** Run the quick run command for the touched suite
- **After every plan wave:** Run the full suite command
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 90 seconds (targeted subset)

---

## Per-Task Verification Map

> Task IDs are positional within each plan (73-PP-TT). Every MCOMP-02..06 behavior maps to at least one automated test. Phase framing per D-08: MCOMP-04/05 **add regression tests only** — no new scheduler policy. `❌ W0` = test case added in this phase (Wave-0-style extension of an existing suite).

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 73-01-01 | 73-01 | 1 | MCOMP-03 | — | `ComputeBackend.push_host` required + validated (id-tagged fail-fast) | unit | `uv run pytest tests/shared/config/test_bucket_registry.py -x -q` | ✅ | ✅ green |
| 73-01-02 | 73-01 | 1 | MCOMP-03 | T-73-argv | `PushFilePayload.dest_*` added with `extra="forbid"` + absolute-path/argv-injection validators | unit | `uv run pytest tests/analyze/core/test_push_pipeline.py -x -q -k "payload or dest or validator"` | ✅ | ✅ green |
| 73-01-03 | 73-01 | 1 | MCOMP-03, MCOMP-06 | — | `dispatch` stamps dest from recorded `backend_id` via `resolve_compute_backend` (record-don't-rederive) | integration | `uv run pytest tests/analyze/services/test_backends.py -x -q -k "dispatch or resolve_compute_backend or push"` | ✅ | ✅ green |
| 73-02-01 | 73-02 | 2 | MCOMP-03 | — | `_build_rsync_argv` reads destination from payload, not `cfg.*` | integration | `uv run pytest tests/analyze/core/test_push_pipeline.py -x -q -k "rsync or argv or dest"` | ✅ | ✅ green |
| 73-02-02 | 73-02 | 2 | MCOMP-03 | — | Fileserver single-destination env retired (`_require_push_config` reduced); `cloud_scratch_dir` field KEPT for agent janitor | unit | `uv run pytest tests/analyze/core/test_push_pipeline.py -x -q -k "require or config or missing"` | ✅ | ✅ green |
| 73-03-01 | 73-03 | 2 | MCOMP-03, MCOMP-06 | T-73-08 | `/pushed` resolves scratch/terminalization from recorded `backend_id`, never `select_active_agent` (no cross-attribution via record-don't-rederive) | integration | `uv run pytest tests/agents/routers/test_agent_push.py -x -q -k "pushed"` | ✅ | ✅ green |
| 73-03-02 | 73-03 | 2 | MCOMP-06 | T-73-07 | `/mismatch` validates reporter token == `backend_id → agent_ref`, rejects (4xx) + no-terminalize on mismatch; re-drive re-stamps `dest_*` (Landmine 1) | integration | `uv run pytest tests/agents/routers/test_agent_push.py -x -q -k "mismatch"` | ✅ | ✅ green |
| 73-04-01 | 73-04 | 3 | MCOMP-02, MCOMP-04, MCOMP-05 | — | N-compute rank/cap load-spread; offline agent holds/spills; one flaky agent isolated to 0 slots without failing the drain tick | regression | `uv run pytest tests/analyze/services/test_backends.py tests/analyze/services/test_backend_selection.py tests/analyze/tasks/test_release_awaiting_cloud.py -x -q -k "compute or rank or spread or flaky or isolat or available"` | ✅ | ✅ green |
| 73-04-02 | 73-04 | 3 | MCOMP-03 | — | `active_compute_scratch_dir` accessor deleted (last reader removed) | unit | `uv run pytest tests/shared/config/test_bucket_registry.py tests/analyze/services/test_backends.py -x -q` | ✅ | ✅ green |
| 73-04-03 | 73-04 | 3 | MCOMP-03 | — | Behavior-preservation golden: ≤1-compute single-agent deploy pushes byte-identically; `reenqueue.py:374` documented as PROV-01 known-limitation | regression | `uv run pytest tests/analyze/services/test_compute_binding_golden.py -x -q` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

*File Exists ✅ = target test module already present in the suite; this phase adds cases to it (no new framework, no Wave-0 scaffolding gap).*

---

## Wave 0 Requirements

- [x] `tests/analyze/services/test_backends.py` — per-agent `is_available` spill/hold + `dispatch` dest-stamp + reconcile `WHERE backend_id == self.id` scoping (MCOMP-02/03/06)
- [x] `tests/analyze/services/test_backend_selection.py` — N-compute rank/cap load-spread + spill-when-full/offline (MCOMP-04)
- [x] `tests/analyze/tasks/test_release_awaiting_cloud.py` — one-flaky-compute isolation-to-0-slots without failing the drain tick (MCOMP-05)
- [x] `tests/agents/routers/test_agent_push.py` — `/pushed` + `/mismatch` backend_id-scoped resolution + reporter-token validation reject (MCOMP-06)
- [x] `tests/analyze/core/test_push_pipeline.py` — `PushFilePayload.dest_*` validators + payload-carried `_build_rsync_argv` destination (MCOMP-03)
- [x] `tests/shared/config/test_bucket_registry.py` — `ComputeBackend.push_host` required-field validator (MCOMP-03)
- [x] `tests/analyze/services/test_compute_binding_golden.py` — ≤1-compute behavior-preservation golden (MCOMP-03)

*Existing pytest infrastructure covers all phase requirements — this phase adds test cases to existing suites (plus the golden module in 73-04), no new framework.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live multi-agent rsync push over Tailscale to real hosts | MCOMP-03 | Requires N deployed compute hosts + SSH key auth; deployment-gated | Deferred to rollout — regression tests prove payload/argv correctness in-process |

*All in-process phase behaviors have automated verification; only live multi-host transport is deployment-gated.*

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 90s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** validated 2026-07-05 (all 10 task commands green; 3 explicit MCOMP-tagged regressions present)

---

## Validation Audit 2026-07-05

| Metric | Count |
|--------|-------|
| Requirements audited | 5 (MCOMP-02..06) |
| Task rows audited | 10 |
| COVERED | 10 |
| PARTIAL | 0 |
| MISSING | 0 |
| Gaps resolved | 0 (none found — all target suites already green) |
| Manual-only (deployment-gated) | 1 (live multi-host rsync over Tailscale) |

State-A audit run by the orchestrator directly (no `gsd-nyquist-auditor` spawn: zero MISSING gaps means no test generation was required — this was a verification pass). Each of the 10 per-task commands was executed against the ephemeral test DB (5433/6380) and returned N>0 passing tests, no failures. The three named MCOMP regressions are present in the suite: `test_mcomp02_two_compute_backends_only_the_online_bound_agent_is_available` (`tests/analyze/services/test_backends.py:384`), `test_mcomp04_compute_rank_cap_spread_prefers_free_arm64_then_spills_to_paid_x86` (`tests/analyze/services/test_backend_selection.py:107`), `test_mcomp05_flaky_compute_backend_degrades_to_zero_slots_healthy_compute_lane_still_dispatches` (`tests/analyze/tasks/test_release_awaiting_cloud.py:218`). The CR-01 code-review fix regression (`test_push_mismatch_over_cap_does_not_clobber_advanced_file`) is covered under the `/mismatch` row. `nyquist_compliant: true`.
