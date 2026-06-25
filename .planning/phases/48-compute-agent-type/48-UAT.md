---
status: testing
phase: 48-compute-agent-type
source: [48-01-SUMMARY.md, 48-02-SUMMARY.md, 48-03-SUMMARY.md]
started: 2026-06-25T19:04:33Z
updated: 2026-06-25T19:04:33Z
---

## Current Test
<!-- OVERWRITE each test - shows where we are -->

number: 1
name: Cold Start Smoke Test
expected: |
  Against a fresh/clean database, the app boots without errors, Alembic migration 024
  applies cleanly (single head 024), and the Agents admin page loads and returns live data.
awaiting: user response

## Tests

### 1. Cold Start Smoke Test
expected: Against a fresh DB, the app boots without errors, migration 024 applies cleanly (alembic head = 024), and the Agents admin page (/admin/agents) loads showing live data.
result: [pending]

### 2. Register a Compute Agent (CLI)
expected: Running `uv run phaze agents add --kind compute --id oci-a1 --name "OCI A1"` with NO --scan-roots succeeds — prints a bearer token (once, to stdout) and inserts an agents row with kind='compute' and empty scan_roots. No error about missing scan roots.
result: [pending]

### 3. Fileserver Still Requires Scan Roots (CLI guard)
expected: Running `uv run phaze agents add --kind fileserver --id fs1 --name "FS1"` WITHOUT --scan-roots fails (non-zero exit) with a message that scan roots are required — the default kind still demands scan roots.
result: [pending]

### 4. Kind Badge on the Agents Admin Page
expected: The Agents admin page shows a new "Kind" column between Agent and Status. A compute agent renders an indigo "COMPUTE" badge; a file-server agent renders a slate "FILE SERVER" badge. The badge persists across the 5-second auto-refresh (no flicker/disappearance) and is screen-reader labeled (aria-label "Kind: compute" / "Kind: file server").
result: [pending]

### 5. Live Compute Capacity End-to-End (manual-only)
expected: A real compute agent (the Phase 47 arm64 image), once registered and started while draining its queue, appears on the live Agents admin page with the kind badge + green liveness pill + queue depth — so cloud capacity is visible at a glance. (Per 48-VALIDATION this is the only leg requiring a live deployment; routing work lands in Phase 49+.)
result: [pending]

## Summary

total: 5
passed: 0
issues: 0
pending: 5
skipped: 0
blocked: 0

## Gaps

[none yet]
