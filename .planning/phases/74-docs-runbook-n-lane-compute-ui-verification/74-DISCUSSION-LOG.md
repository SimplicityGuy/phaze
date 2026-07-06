# Phase 74: Docs, Runbook & N-Lane Compute UI Verification - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-06
**Phase:** 74-docs-runbook-n-lane-compute-ui-verification
**Areas discussed:** Doc home & structure, Cost-tiering example depth, UI verification rigor, Multi-agent compose artifact

---

## Doc home & structure

| Option | Description | Selected |
|--------|-------------|----------|
| Extend cloud-burst.md | Add a section to the existing single-A1-agent compute home | |
| New runbook.md section | Put it beside the N-lane / force-local operator content | |
| New dedicated doc | Standalone doc (e.g. docs/multi-compute.md) | ✓ |

**User's choice:** New dedicated doc
**Notes:** cloud-burst.md stays the single-agent walkthrough; the new doc is the "do it N times, cost-tiered" operator guide, cross-linked from cloud-burst/runbook/configuration/README.

---

## Cost-tiering example depth (arm64 vs x86)

| Option | Description | Selected |
|--------|-------------|----------|
| Real x86 compute agent | Document a standard-x86-image compute agent on a paid VM as a real deployable; worked backends.toml with arm64 rank-low + x86 rank-high | ✓ |
| Existing Kueue x86 path | Cost-tier = arm64 compute preferred, spill to the shipped Kueue x86 cluster | |
| Illustrative only | Worked arm64 example + note ranks generalize; no committed x86 deploy | |

**User's choice:** Real x86 compute agent
**Notes:** Commits the docs to a concrete two-compute-backend example. Raised the x86-image-tag wrinkle (arm64 compose pulls `-arm64`; x86 agent needs the standard tag) → captured as research flag R-1, not a user decision.

---

## UI verification rigor

| Option | Description | Selected |
|--------|-------------|----------|
| Verify + regression test | Assert each compute backend renders its own lane; fix code only if a gap surfaces | ✓ |
| Verify-only | Manually confirm the registry-derived loop; no new test unless a gap | |
| Verify + live UAT | Boot app with a 2-compute backends.toml and screenshot lanes | |

**User's choice:** Verify + regression test
**Notes:** Lanes already registry-derived (get_backend_lane_snapshot, Phase 71 BEUI-01), so a code gap is unlikely; the test locks the guarantee (parity with Phase 70 MKUE test discipline + ≥90% coverage floor).

---

## Multi-agent compose artifact

| Option | Description | Selected |
|--------|-------------|----------|
| Parametrize existing compose | Run docker-compose.cloud-agent.yml N times with distinct AGENT_ID/QUEUE/SCRATCH/host per agent | ✓ |
| Second compose file | Ship a concrete docker-compose.cloud-agent-2.yml | |
| Override/env-file pattern | Documented compose override + per-agent env-file convention | |

**User's choice:** Parametrize existing compose
**Notes:** No new file to drift. Must also cover the per-agent image-tag swap for the x86 agent (ties to R-1).

---

## Claude's Discretion

- New-doc filename/slug, section order, and mermaid diagram choices (follow existing docs style).
- Cost-tier rationale table wording.
- Regression-test file location (candidate: tests/analyze/services/test_backends.py).

## Deferred Ideas

- Milestone close + CalVer release tag (`/gsd:complete-milestone 2026.7.2`) — separate step after this phase merges.
- PROV-02 (capability-aware routing) and PROV-03 (on-demand provisioning) — v2-deferred at milestone scoping.
