---
phase: 74
slug: docs-runbook-n-lane-compute-ui-verification
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-07-06
---

# Phase 74 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x (pytest-asyncio) |
| **Config file** | pyproject.toml |
| **Quick run command** | `uv run pytest tests/shared/services/test_lane_snapshot.py` |
| **Full suite command** | `uv run pytest` |
| **Estimated runtime** | ~2–5 seconds (targeted) / full suite minutes |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/shared/services/test_lane_snapshot.py`
- **After every plan wave:** Run `uv run pytest` (full suite)
- **Before `/gsd:verify-work`:** Full suite must be green + `just docs-drift` green
- **Max feedback latency:** ~5 seconds (targeted test)

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| {N}-XX-XX | XX | X | MCOMP-07 | — | N-lane render: N compute backends → N lane cards | unit | `uv run pytest tests/shared/services/test_lane_snapshot.py` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

*Planner fills concrete task IDs. Anchor: the N≥2-compute lane regression test (real probe fan-out variant per RESEARCH R-2) is the arbiter of whether the `_probe_availability` fix is needed, plus the compose guard-test edit (R-1/D-05) and `just docs-drift` traceability green (MCOMP-07 checkbox).*

---

## Wave 0 Requirements

- [ ] `tests/shared/services/test_lane_snapshot.py` — new regression test asserting each of N compute backends renders its own lane (happy-path snapshot + real-session probe fan-out variant per RESEARCH R-2)

*Existing pytest infrastructure otherwise covers phase requirements; docs deliverables are validated by `just docs-drift` traceability, not unit tests.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| New `docs/multi-compute.md` operator recipe reads correctly + cross-links resolve | MCOMP-07 | Prose/nav quality is not unit-testable | Read the doc; confirm cross-links from cloud-burst.md, runbook.md, configuration.md § Backend registry, and docs index/README resolve; confirm worked `backends.toml` fields match `configuration.md` schema |

*The N-lane rendering guarantee and MCOMP-07 traceability ARE automated (regression test + `just docs-drift`).*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 5s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
