# Phase 75: Engineering Hygiene — Guard Hardening, Tech-Debt & Stale-Tracking Cleanup - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-06
**Phase:** 75-engineering-hygiene-guard-hardening-tech-debt-stale-tracking
**Areas discussed:** HYG-03 reconciliation, HYG-01 disposition, HYG-02 disposition, HYG-04 test style

---

**Pre-discussion finding (scouted against shipped code):** Three of the five HYG requirements were
authored 2026-07-06 from a stale snapshot and their premises had already been overtaken by shipped code
(PR #207 for HYG-01; Phases 72/73 for HYG-03; and HYG-02's "env line" that does not exist). The
discussion focused on reconciling each item against reality.

---

## HYG-03 — `>1`-compute fail-fast (premise stale vs shipped N-compute)

| Option | Description | Selected |
|--------|-------------|----------|
| Harden probes (WR-01) | Reinterpret as the WR-01 fix — serialize `_probe_availability` so N≥2 compute is structurally race-free; keeps N-compute, closes the real open gap | |
| Drop HYG-03 as superseded | No code change; document that Phase 72 D-03 retired the fail-fast and N-compute is the shipped capability; boot guard already exists as duplicate-`agent_ref`; reconcile requirement/tracking | ✓ |
| Literal: add `>1` boot reject | Implement verbatim — rejects 2+ compute backends at boot; WARNING: rolls back N-compute, breaks Phases 72-74 | |

**User's choice:** Drop HYG-03 as superseded.
**Notes:** No code change. WR-01 (concurrent-session probe race) stays a tracked deferred item — user
chose not to fix it here. Reconcile requirement text + STATE.md:246 to SUPERSEDED, citing Phase 72 D-03.

---

## HYG-01 — traceability-guard hardening (already landed in PR #207)

| Option | Description | Selected |
|--------|-------------|----------|
| Add explicit regression test | Add a test asserting the active-milestone checks skip when REQUIREMENTS.md absent | |
| Close as already-satisfied | Verify existing skipif green, document HYG-01 satisfied by PR #207, no new test; reconcile requirement | ✓ |

**User's choice:** Close as already-satisfied.
**Notes:** `_NO_ACTIVE_MILESTONE` + skipif already present (`test_requirements_traceability.py:64`, landed
`ec80a53a`). No new test; requirement/traceability reconciled to satisfied.

---

## HYG-02 — stale `cloud_target` docker-compose comments

| Option | Description | Selected |
|--------|-------------|----------|
| Delete the two comments | Remove both "Replaces the removed cloud_target selector…" lines (docker-compose.yml:24,52); keep backends.toml explainer | ✓ |
| Keep/reword — they're accurate | Comments correctly document the Phase-67 removal; keep or lightly reword; reconcile HYG-02 as a no-op | |

**User's choice:** Delete the two comments.
**Notes:** No `PHAZE_CLOUD_TARGET` env line exists (git grep clean) — premise "env lines" is inaccurate;
only the two comments exist. Executor removes comments only.

---

## HYG-04 — force-local duration-router gate regression test

| Option | Description | Selected |
|--------|-------------|----------|
| Real route + toggle flip | Drive actual endpoints (2 duration-router triggers + backfill) with `get_route_control` True/False; assert 0 AWAITING_CLOUD when forced, registry honored when not | ✓ |
| Focused unit assertion | Assert `effective_cloud_enabled = cloud_enabled AND NOT force_local` per site with lighter fixtures (mock `get_route_control`) | |

**User's choice:** Real route + toggle flip.
**Notes:** Highest fidelity to the 3 gate sites (`pipeline.py:396/718/793`). Must assert absence of
AWAITING_CLOUD rows under force-local (T-71-08 zero-mutation backfill no-op), not just a routing count.

---

## Claude's Discretion

- Exact reconciled requirement-text wording for HYG-01 (satisfied) and HYG-03 (superseded) + the STATE.md
  deferred-row edits.
- HYG-04 test fixture mechanics within the real-route constraint.

## Deferred Ideas

- **WR-01** — serialize the N-compute probe fan-out (`_probe_availability`, `backends.py:665`); the real
  open robustness gap adjacent to HYG-03, deliberately deferred out of Phase 75. Bounded (flaps one lane
  for one 5s poll, self-heals). Keep tracked.
- **PROV-02 / PROV-03** — capability-aware routing + on-demand compute provisioning; v2 requirements,
  future milestone.
