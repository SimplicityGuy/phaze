---
phase: 74
slug: docs-runbook-n-lane-compute-ui-verification
status: final
nyquist_compliant: true
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
| 74-01-01 | 01 | 1 | MCOMP-07 | T-74-01 | Doc example discloses no secrets/creds | doc-grep | `test "$(sed -n '1p' docs/multi-compute.md)" = '<!-- generated-by: gsd-doc-writer -->' && grep -q 'configuration.md#backend-registry-backendstoml' docs/multi-compute.md` | ✅ | ⬜ pending |
| 74-01-02 | 01 | 1 | MCOMP-07 | — | N/A | doc-grep | `grep -q 'multi-compute.md' docs/README.md docs/runbook.md docs/configuration.md docs/cloud-burst.md` | ✅ | ⬜ pending |
| 74-02-01 | 02 | 1 | MCOMP-07 | — | Compose parametrization adds no new exposed port/credential; arm64 default preserved | unit | `uv run python -c "import yaml; d=yaml.safe_load(open('docker-compose.cloud-agent.yml')); ..."` (arm64 default renders) | ✅ | ⬜ pending |
| 74-02-02 | 02 | 1 | MCOMP-07 | — | Guard test still asserts arm64 default (substring, not blanket removal) | unit | `uv run pytest tests/agents/deployment/test_cloud_agent_compose.py -x -q` | ✅ | ⬜ pending |
| 74-03-01 | 03 | 1 | MCOMP-07 | — | Each of N compute backends renders its own lane card (deterministic Variant A) | unit | `uv run pytest tests/shared/services/test_lane_snapshot.py -k one_lane_per_compute -x -q` | ❌ W0 | ⬜ pending |
| 74-03-02 | 03 | 1 | MCOMP-07 | T-74-06 | Real `_probe_availability` fan-out with 2 online compute agents (race arbiter) | unit | `uv run pytest tests/shared/services/test_lane_snapshot.py -k compute_probe_real -x -q` | ❌ W0 | ⬜ pending |
| 74-04-01 | 04 | 2 | MCOMP-07 | T-74-06 | Stale ≤1-compute docstring corrected; conditional compute-probe serialization (gated on 74-03-02) | unit | `uv run pytest tests/shared/services/test_lane_snapshot.py -x -q && uv run mypy src/phaze/services/backends.py` | ✅ | ⬜ pending |
| 74-04-02 | 04 | 2 | MCOMP-07 | — | MCOMP-07 traceability/ROADMAP closeout; docs-drift green | traceability | `just docs-drift` | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

*Anchor: the N≥2-compute lane regression test (74-03-02 real probe fan-out variant per RESEARCH R-2) is the arbiter of whether the `_probe_availability` fix (74-04-01) is needed; plus the compose guard-test edit (R-1/D-05, 74-02) and `just docs-drift` traceability green (MCOMP-07 checkbox, 74-04-02). Wave 2 (74-04) `depends_on: ["74-03"]` — verify-then-fix. `File Exists ❌ W0` = the new `test_lane_snapshot.py` cases are written in this phase (Wave 0 requirement below).*

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

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (74-03 test_lane_snapshot.py cases)
- [x] No watch-mode flags
- [x] Feedback latency < 5s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-07-06
