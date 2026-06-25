---
status: partial
phase: 48-compute-agent-type
source: [48-01-SUMMARY.md, 48-02-SUMMARY.md, 48-03-SUMMARY.md]
started: 2026-06-25T19:12:30Z
updated: 2026-06-25T19:12:30Z
---

## Current Test

[testing paused — 1 item outstanding (manual-only live-deployment leg)]

## Tests

### 1. Cold Start Smoke Test
expected: Against a fresh DB, the app boots without errors, migration 024 applies cleanly (alembic head = 024), and the Agents admin page loads showing live data.
result: pass
evidence: "Fresh phaze_test DB; alembic upgrade head ran 010→024 cleanly; alembic current = 024 (head); legacy-application-server row backfilled to kind='fileserver' via server_default."

### 2. Register a Compute Agent (CLI)
expected: `phaze agents add --kind compute --id oci-a1 --name "OCI A1"` with NO --scan-roots succeeds, prints a bearer token once, inserts a row with kind='compute' and empty scan_roots.
result: pass
evidence: "rc=0; token 'phaze_agent_…' printed to stdout only (INSERT logged the token_hash, not the token — D-13 preserved); DB row: id=oci-a1, kind=compute, scan_roots=[], token_hash set."

### 3. Fileserver Still Requires Scan Roots (CLI guard)
expected: `phaze agents add --kind fileserver` WITHOUT --scan-roots fails (non-zero exit) with a scan-roots-required message; no row inserted.
result: pass
evidence: "rc=1; 'error: --scan-roots is required for --kind fileserver (at least one absolute path)'; 0 rows for the rejected id. Valid fileserver (nox-fs, --scan-roots /data/music) registered fine."

### 4. Kind Badge on the Agents Admin Page
expected: The Agents admin page shows a "Kind" column between Agent and Status; compute → indigo "COMPUTE" badge, fileserver → slate "FILE SERVER" badge; present on BOTH full page and the 5s HTMX poll partial; aria-labels "Kind: compute" / "Kind: file server".
result: pass
evidence: "Real admin_agents router rendered against the live test DB (3 CLI-inserted agents). 18/18 assertions passed across GET /admin/agents AND GET /admin/agents/_table: COMPUTE + FILE SERVER labels, bg-indigo-100/dark:bg-indigo-950 + bg-slate-100 palettes, both aria-labels, Kind <th>, locked geometry 'text-xs font-semibold px-2 py-0.5 rounded-full'."

### 5. Live Compute Capacity End-to-End (manual-only)
expected: A real compute agent (Phase 47 arm64 image), once registered and started while draining its queue, appears on the live Agents admin page with kind badge + green liveness pill + queue depth.
result: blocked
blocked_by: prior-phase
reason: "Documented manual-only leg (48-VALIDATION §Manual-Only). Requires a live deployment: the Phase 47 arm64 image registered as a compute agent and actively draining its per-agent SAQ queue. Duration-based routing to cloud capacity lands in Phase 49 and the deploy in Phase 51 — not exercisable now. The static badge render (kind column) is already proven by Test 4; only the live liveness+queue-depth-while-draining observation remains."

## Summary

total: 5
passed: 4
issues: 0
pending: 0
skipped: 0
blocked: 1

## Gaps

[none — 4/4 testable items passed; test 5 is a prerequisite-gated manual observation, not a code gap]
