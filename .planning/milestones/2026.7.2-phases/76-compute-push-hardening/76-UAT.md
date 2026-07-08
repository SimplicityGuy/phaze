---
status: complete
phase: 76-compute-push-hardening
source: [76-01-SUMMARY.md, 76-02-SUMMARY.md, 76-03-SUMMARY.md]
started: 2026-07-06T00:00:00Z
updated: 2026-07-06T00:00:00Z
---

## Current Test

[testing complete]

## Tests

### 1. N-compute liveness probe stays correct and session-safe (HARD-01)
expected: With ≥2 online compute backends, the availability probe returns the correct per-backend `{backend_id: available}` map with no SQLAlchemy concurrent-session error — probes run sequentially on the one shared AsyncSession, a hung lane still degrades to offline within the bounded timeout, and healthy lanes stay online.
result: pass
evidence: "uv run pytest tests/shared/services/test_lane_snapshot.py -k probe → 6 passed (incl. test_compute_probe_real_fanout_keeps_both_lanes_online, test_probe_timeout_isolation, test_probe_failure_degrades_to_offline)"

### 2. Concurrent push-mismatch reports cannot lose an increment (HARD-02)
expected: Two simultaneous `/mismatch` callbacks for the same file bump `push_attempt` to exactly 2 (no lost update); the `push_max_attempts` cap still trips at the exact boundary (spills to AWAITING_CLOUD); and the real `apply_deterministic_key` before_enqueue hook does not deadlock (advisory-xact-lock, not row lock).
result: pass
evidence: "TEST_DATABASE_URL=…:5433 uv run pytest tests/agents/routers/test_agent_push.py -k 'mismatch_concurrent or mismatch_cap or mismatch_real_enqueue' → 3 passed (real Postgres; no-lost-update and no-deadlock both RED-verified in SUMMARY)"

### 3. Malformed agent_id is rejected at the API boundary (HARD-03)
expected: `GET /tracklists/scan/status` and `GET /pipeline/scans/agent-roots` with a malformed `agent_id` (e.g. `Bad_ID!`) return 422 instead of a silent empty 200; a well-formed id (e.g. `test-agent-01`) still passes validation and reaches the handler.
result: pass
evidence: "uv run pytest tests/identify/routers/test_tracklists.py tests/shared/routers/test_pipeline_scans.py -k agent_id → 4 passed (malformed→422 + well-formed→pass for both endpoints)"

## Summary

total: 3
passed: 3
issues: 0
pending: 0
skipped: 0
blocked: 0

## Gaps

[none]

## Notes

Phase 76 is a backend hardening phase (concurrency safety + HTTP input validation) with no UI surface, so UAT was executed autonomously via the automated regression suite rather than interactive manual confirmation. All three user-observable behaviors verified green against the port-5433 real-Postgres test DB. Two of the HARD-02 tests are RED-verified (fail with the mitigation removed), confirming the tests genuinely exercise the fixed behavior.
