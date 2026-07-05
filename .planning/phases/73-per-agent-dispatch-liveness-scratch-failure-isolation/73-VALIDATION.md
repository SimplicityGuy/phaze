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
| **Quick run command** | `uv run pytest tests/test_backends.py tests/test_agent_push.py tests/test_push.py -q` |
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

> Filled during planning/execution. Every MCOMP-02..06 behavior below must map to at least one automated regression test. Phase framing per D-08: **adds regression tests only** — no new scheduler policy.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| TBD | — | — | MCOMP-02 | — | Offline bound agent makes only that backend unavailable; file holds/spills, never dispatches to a dead agent | regression | `uv run pytest tests/test_backends.py -q` | ❌ W0 | ⬜ pending |
| TBD | — | — | MCOMP-03 | — | File dispatched to a specific backend is pushed to that agent's host/scratch (payload-carried, not global) | integration | `uv run pytest tests/test_push.py -q` | ❌ W0 | ⬜ pending |
| TBD | — | — | MCOMP-04 | — | Tiered drain spreads N compute agents by rank + per-agent cap; spills to next-eligible when at cap/offline | regression | `uv run pytest tests/test_release_awaiting_cloud.py -q` | ❌ W0 | ⬜ pending |
| TBD | — | — | MCOMP-05 | — | One flaky/offline agent degrades to 0 slots without failing the drain tick or blocking healthy agents | regression | `uv run pytest tests/test_release_awaiting_cloud.py -q` | ❌ W0 | ⬜ pending |
| TBD | — | — | MCOMP-06 | T-73-07 | `/pushed` + `/mismatch` resolve scratch/terminalization from recorded `backend_id`; reporter token validated, mismatch rejected — no cross-agent mis-attribution | integration | `uv run pytest tests/test_agent_push.py -q` | ❌ W0 | ⬜ pending |
| TBD | — | — | MCOMP-03 | — | Behavior-preservation: ≤1-compute single-agent deploy pushes identically once payload carries the (single) destination | regression | `uv run pytest tests/test_push.py -q` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_backends.py` — extend for per-agent `is_available` spill/hold + reconcile `WHERE backend_id == self.id` scoping (MCOMP-02/06)
- [ ] `tests/test_agent_push.py` — extend for `/pushed` + `/mismatch` backend_id-scoped resolution + reporter-token validation reject (MCOMP-06)
- [ ] `tests/test_push.py` — extend for payload-carried `_build_rsync_argv` destination + ≤1-compute behavior-preservation (MCOMP-03)
- [ ] `tests/test_release_awaiting_cloud.py` — extend for N-compute rank/cap load-spread + one-flaky isolation-to-0-slots (MCOMP-04/05)

*Existing pytest infrastructure covers all phase requirements — Wave 0 adds test cases to existing suites, no new framework.*

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
