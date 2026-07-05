---
status: complete
phase: 71-deployment-config-docs-n-lane-ui
source: [71-VERIFICATION.md]
started: 2026-07-05T03:55:00Z
updated: 2026-07-05T04:08:00Z
method: agent-driven (Playwright on local uvicorn + fresh phaze_uat DB, 3-backend backends.toml)
---

## Current Test

[complete]

## Tests

### 1. N-lane grid visual rendering
expected: With a real ≥2-backend `backends.toml`, the Analyze workspace renders one card per registry backend, rank-ascending, with RANK / `{in_flight}/{cap}` / online-vs-offline / per-lane Kueue admission caption; wraps cleanly; live-poll OOB swap without flicker; degrade panel on `[]`.
result: pass — booted on a 3-backend registry (local + oci-a1 compute + k8s-uat kueue). Grid rendered exactly 3 cards rank-ascending (`#analyze-lanes` host present), `local` online, `oci-a1`/`k8s-uat` greyed "offline" (probes time out — no live agent/cluster), per-lane `{in_flight}/{cap}` counts, "…in flight across 3 lanes" subcount. Verified live via Playwright (screenshot captured).

### 2. Force-local pill toggle end-to-end
expected: Header pill neutral `CLOUD ROUTING` (aria-checked=true) by default; click engages loud amber `FORCED LOCAL` (aria-checked=false) with an aria-live toast; state persists across navigation (durable `route_control` row); click reverts.
result: pass — default `CLOUD ROUTING` [checked]. Click → `FORCED LOCAL` (aria-checked flipped, DB `route_control.force_local = t`). Navigated to a different page → pill still `FORCED LOCAL` (durable row read on every shell page). Click → back to `CLOUD ROUTING` (DB `force_local = f`). Full durable round-trip via Playwright.

## Defects found & fixed during UAT

Two Phase-71-introduced regressions against the documented single-poll-cleanliness invariant (WORK-05/SP-6) were surfaced live and fixed (both with red/green-verified tests):

1. **WR-01 (from code review, confirmed here):** a compute `is_available` probe that fails at the DB layer poisoned the shared session and collapsed the WHOLE lane grid to the `[]` degrade panel. Fixed by a post-probe `session.rollback()` (`fe1f0032`).
2. **UAT-01:** the chrome `/pipeline/stats` poll re-emits the `#analyze-lanes` grid OOB every 5s, but its real host lives only in the Analyze workspace — so every other stage and the first-run empty state logged `htmx:oobErrorNoTarget` every 5s. Fixed by sinking `#analyze-lanes` in `_workspace_poll_seeds.html` under the same gate as the six cloud cards (`1c0473b2`). Verified live: 0 `oobErrorNoTarget` over multiple ticks on the empty state and the metadata stage.

## Summary

total: 2
passed: 2
issues: 0
pending: 0
skipped: 0
blocked: 0

## Gaps
