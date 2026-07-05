---
status: partial
phase: 71-deployment-config-docs-n-lane-ui
source: [71-VERIFICATION.md]
started: 2026-07-05T03:55:00Z
updated: 2026-07-05T03:55:00Z
---

## Current Test

[awaiting human testing]

## Tests

### 1. N-lane grid visual rendering
expected: With a real ≥2-backend `backends.toml`, the Analyze workspace renders one card per registry backend, rank-ascending. Each card shows RANK, `{in_flight}/{cap}`, availability (online vs greyed "offline"), and the per-lane Kueue admission caption (quota-wait vs Inadmissible). The grid wraps cleanly at narrow widths and swaps smoothly on the live 5s `/pipeline/stats` OOB poll without flicker. When the snapshot degrades, the degrade panel shows in place of the grid.
result: [pending]

### 2. Force-local pill toggle end-to-end
expected: The header force-local pill shows neutral `CLOUD ROUTING` (aria-checked=true) by default. Clicking it engages force-local: the pill swaps in place to loud amber `FORCED LOCAL` (aria-checked=false), an accessible aria-live toast confirms, and the state persists across page navigation (durable `route_control` row). Clicking again reverts. While engaged, new cloud routing stops (drain + duration router + backfill all hold) but already-held `AWAITING_CLOUD` files stay held (no retroactive drain).

## Summary

total: 2
passed: 0
issues: 0
pending: 2
skipped: 0
blocked: 0

## Gaps
