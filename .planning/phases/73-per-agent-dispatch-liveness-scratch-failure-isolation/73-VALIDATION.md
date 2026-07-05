---
phase: 73
slug: per-agent-dispatch-liveness-scratch-failure-isolation
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-07-05
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
| 73-01-01 | 73-01 | 1 | MCOMP-03 | — | `ComputeBackend.push_host` required + validated (id-tagged fail-fast) | unit | `uv run pytest tests/shared/config/test_bucket_registry.py -x -q` | ✅ | ⬜ pending |
| 73-01-02 | 73-01 | 1 | MCOMP-03 | T-73-argv | `PushFilePayload.dest_*` added with `extra="forbid"` + absolute-path/argv-injection validators | unit | `uv run pytest tests/analyze/core/test_push_pipeline.py -x -q -k "payload or dest or validator"` | ✅ | ⬜ pending |
| 73-01-03 | 73-01 | 1 | MCOMP-03, MCOMP-06 | — | `dispatch` stamps dest from recorded `backend_id` via `resolve_compute_backend` (record-don't-rederive) | integration | `uv run pytest tests/analyze/services/test_backends.py -x -q -k "dispatch or resolve_compute_backend or push"` | ✅ | ⬜ pending |
| 73-02-01 | 73-02 | 2 | MCOMP-03 | — | `_build_rsync_argv` reads destination from payload, not `cfg.*` | integration | `uv run pytest tests/analyze/core/test_push_pipeline.py -x -q -k "rsync or argv or dest"` | ✅ | ⬜ pending |
| 73-02-02 | 73-02 | 2 | MCOMP-03 | — | Fileserver single-destination env retired (`_require_push_config` reduced); `cloud_scratch_dir` field KEPT for agent janitor | unit | `uv run pytest tests/analyze/core/test_push_pipeline.py -x -q -k "require or config or missing"` | ✅ | ⬜ pending |
| 73-03-01 | 73-03 | 2 | MCOMP-03, MCOMP-06 | T-73-08 | `/pushed` resolves scratch/terminalization from recorded `backend_id`, never `select_active_agent` (no cross-attribution via record-don't-rederive) | integration | `uv run pytest tests/agents/routers/test_agent_push.py -x -q -k "pushed"` | ✅ | ⬜ pending |
| 73-03-02 | 73-03 | 2 | MCOMP-06 | T-73-07 | `/mismatch` validates reporter token == `backend_id → agent_ref`, rejects (4xx) + no-terminalize on mismatch; re-drive re-stamps `dest_*` (Landmine 1) | integration | `uv run pytest tests/agents/routers/test_agent_push.py -x -q -k "mismatch"` | ✅ | ⬜ pending |
| 73-04-01 | 73-04 | 3 | MCOMP-02, MCOMP-04, MCOMP-05 | — | N-compute rank/cap load-spread; offline agent holds/spills; one flaky agent isolated to 0 slots without failing the drain tick | regression | `uv run pytest tests/analyze/services/test_backends.py tests/analyze/services/test_backend_selection.py tests/analyze/tasks/test_release_awaiting_cloud.py -x -q -k "compute or rank or spread or flaky or isolat or available"` | ✅ | ⬜ pending |
| 73-04-02 | 73-04 | 3 | MCOMP-03 | — | `active_compute_scratch_dir` accessor deleted (last reader removed) | unit | `uv run pytest tests/shared/config/test_bucket_registry.py tests/analyze/services/test_backends.py -x -q` | ✅ | ⬜ pending |
| 73-04-03 | 73-04 | 3 | MCOMP-03 | — | Behavior-preservation golden: ≤1-compute single-agent deploy pushes byte-identically; `reenqueue.py:374` documented as PROV-01 known-limitation | regression | `uv run pytest tests/analyze/services/test_compute_binding_golden.py -x -q` | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

*File Exists ✅ = target test module already present in the suite; this phase adds cases to it (no new framework, no Wave-0 scaffolding gap).*

---

## Wave 0 Requirements

- [ ] `tests/analyze/services/test_backends.py` — per-agent `is_available` spill/hold + `dispatch` dest-stamp + reconcile `WHERE backend_id == self.id` scoping (MCOMP-02/03/06)
- [ ] `tests/analyze/services/test_backend_selection.py` — N-compute rank/cap load-spread + spill-when-full/offline (MCOMP-04)
- [ ] `tests/analyze/tasks/test_release_awaiting_cloud.py` — one-flaky-compute isolation-to-0-slots without failing the drain tick (MCOMP-05)
- [ ] `tests/agents/routers/test_agent_push.py` — `/pushed` + `/mismatch` backend_id-scoped resolution + reporter-token validation reject (MCOMP-06)
- [ ] `tests/analyze/core/test_push_pipeline.py` — `PushFilePayload.dest_*` validators + payload-carried `_build_rsync_argv` destination (MCOMP-03)
- [ ] `tests/shared/config/test_bucket_registry.py` — `ComputeBackend.push_host` required-field validator (MCOMP-03)
- [ ] `tests/analyze/services/test_compute_binding_golden.py` — ≤1-compute behavior-preservation golden (MCOMP-03)

*Existing pytest infrastructure covers all phase requirements — this phase adds test cases to existing suites (plus the golden module in 73-04), no new framework.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live multi-agent rsync push over Tailscale to real hosts | MCOMP-03 | Requires N deployed compute hosts + SSH key auth; deployment-gated | Deferred to rollout — regression tests prove payload/argv correctness in-process |

*All in-process phase behaviors have automated verification; only live multi-host transport is deployment-gated.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 90s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
