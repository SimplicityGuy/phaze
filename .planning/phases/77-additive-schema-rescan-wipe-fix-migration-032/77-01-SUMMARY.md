---
phase: 77-additive-schema-rescan-wipe-fix-migration-032
plan: 01
subsystem: database
tags: [postgres, sqlalchemy, upsert, on-conflict, ingestion, agent-api, regression-test]

# Dependency graph
requires: []
provides:
  - "Rescan of an already-advanced file no longer resets its state to DISCOVERED (MIG-03 / D-08)"
  - "state removed from the ON CONFLICT DO UPDATE set_ dict at BOTH upsert sites (services/ingestion.py + routers/agent_files.py)"
  - "Two regression tests locking the invariant: state + downstream analysis row survive a rescan at each site"
affects: [78, 79, 80, 82, "stage_status derivation", "reader cutover", "033 destructive migration"]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "ON CONFLICT DO UPDATE set_ dict is INSERT-branch-only for state: new rows stamp DISCOVERED via VALUES; existing rows keep their state"
    - "Rescan regression pattern: seed -> advance to ANALYZED + create output row -> re-upsert same natural key -> assert state + output row survive"

key-files:
  created:
    - "tests/discovery/test_rescan_preserves_state.py"
    - "tests/agents/test_rescan_preserves_state.py"
  modified:
    - "src/phaze/services/ingestion.py"
    - "src/phaze/routers/agent_files.py"

key-decisions:
  - "Deleted exactly one key (state) from each on_conflict_do_update set_ dict; every other key (sha256_hash, file_size, batch_id, file_type), index_elements, .returning(), and itertools.batched batching left untouched."
  - "Agent-endpoint regression uses a self-contained smoke app mounting only agent_files.router (mirrors the Phase-25 smoke-app pattern) so the test does not depend on main.py wiring; agent_id stamped from the auth dep, never the body (AUTH-01)."
  - "One bucket per file: ingestion-service test in tests/discovery/, agent-endpoint test in tests/agents/ (partition guard green)."

patterns-established:
  - "Two-part rescan invariant (D-08): (1) state survives, (2) the stage output row survives — asserted at each of the two mirror upsert sites."

requirements-completed: [MIG-03]

# Metrics
duration: ~20min
completed: 2026-07-08
---

# Phase 77 Plan 01: Rescan-Wipe Fix Summary

**Removed the `ON CONFLICT DO UPDATE SET state = excluded.state` progress-wipe from both file-upsert sites, so re-scanning an `ANALYZED` file preserves its state and its `analysis` output row (MIG-03 / CONTEXT D-08).**

## Performance

- **Duration:** ~20 min
- **Started:** 2026-07-08T07:48:00Z
- **Completed:** 2026-07-08T07:56:00Z
- **Tasks:** 2 completed
- **Files modified:** 4 (2 source, 2 new tests)

## Accomplishments
- Deleted the `state` key from the `on_conflict_do_update` `set_` dict at BOTH near-identical mirror sites — `services/ingestion.py::bulk_upsert_files` and `routers/agent_files.py::upsert_files` — so the bug cannot survive on either the legacy application-server discovery path or the agent-API path.
- Locked the fix with two regression tests (RED→GREEN), one per upsert site / bucket, each asserting the two-part D-08 invariant: state stays `ANALYZED` AND the file's `analysis` row survives a rescan.
- Verified new-file INSERT still stamps `state = DISCOVERED` via the VALUES dict at both sites (newly discovered files unaffected), and that the agent endpoint's AUTH-01 contract is preserved (`agent_id` from the auth dependency, never the body).

## Task Commits

Each task was committed atomically:

1. **Task 1: Add rescan-preserves-state regression tests (RED)** — `23446842` (test)
2. **Task 2: Remove the state-wipe from both ON CONFLICT set_ dicts (GREEN)** — `7d80f580` (fix)

## Files Created/Modified
- `tests/discovery/test_rescan_preserves_state.py` — regression for `bulk_upsert_files`: advance a file to `ANALYZED` + create its `analysis` row, re-upsert the same `(agent_id, original_path)` at `DISCOVERED`, assert state + analysis row survive.
- `tests/agents/test_rescan_preserves_state.py` — regression for the agent `upsert_files` endpoint via a self-contained smoke app; same invariant plus `inserted == 0` on rescan (row updated, not inserted).
- `src/phaze/services/ingestion.py` — removed `"state": stmt.excluded.state` from the `set_` dict; added a Phase-77 rationale comment.
- `src/phaze/routers/agent_files.py` — removed `"state": base_stmt.excluded.state` from the `set_` dict; added a mirror rationale comment noting AUTH-01 is unchanged.

## Verification
- `uv run pytest tests/discovery/test_rescan_preserves_state.py tests/agents/test_rescan_preserves_state.py -x` → **2 passed** (GREEN); both were RED before Task 2 (failing on `discovered != analyzed`).
- `grep -n "state.*excluded"` over both upsert modules → **no matches** (wipe line gone from both).
- `uv run ruff check` + `uv run mypy` on both modified source files → **clean**.
- Per-bucket isolation (against the ephemeral :5433 DB): `just test-bucket discovery` → **203 passed**; `just test-bucket agents` → **441 passed**.
- `tests/shared/test_partition_guard.py` → **3 passed** (each new test in exactly one bucket).
- Pre-commit hooks ran on both task commits (ruff, ruff-format, bandit, mypy, file hygiene) → all Passed; no `--no-verify`.

## Deviations from Plan

None — plan executed exactly as written. Both tasks landed as the planned two-line deletions plus their regression tests; the only additions were explanatory Phase-77 rationale comments at each edited `set_` dict.

## Threat Surface

No new security-relevant surface introduced. The change is a pure deletion of one `set_` key at an already-authenticated endpoint and an internal service; T-77-03 (AUTH-01: `agent_id` from the auth dep) and T-77-04 (rescan can no longer regress progress) are both directly asserted by the agent-endpoint regression test.

## Self-Check: PASSED
