---
phase: 81-per-stage-failure-persistence-retry-paths
plan: 03
subsystem: api
tags: [fastapi, pydantic, sqlalchemy, postgres, saq, metadata, failure-persistence]

# Dependency graph
requires:
  - phase: 77-additive-schema-rescan-wipe-fix-migration-032
    provides: "metadata.failed_at / error_message columns + ix_metadata_failed partial index (migration 032)"
  - phase: 78-derivation-layer-eligibility-anti-drift-test-harness
    provides: "done_clause/failed_clause(METADATA) + resolve_status Python twin — done(metadata)=row present AND failed_at IS NULL"
provides:
  - "report_metadata_failed persists a durable metadata failure row (failed_at set, payload columns NULL) instead of nothing"
  - "Optional-body endpoint (MetadataFailurePayload | None = None) — bodyless old-agent POST returns 200 + clears ledger (CR-02 version-skew guard)"
  - "put_metadata unconditionally clears failed_at/error_message on success, including the empty-body branch (D-13)"
  - "MetadataFailurePayload schema (Literal reason + bounded error + extra='forbid')"
  - "agent_client.report_metadata_failed widened to send an optional triage payload; metadata task composes it on terminal ack"
affects: [phase-80-recovery-derivation, metadata-stage, agent-client, recover_orphaned_work]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Optional-body FastAPI endpoint (body: Model | None = None, no Body() wrapper) for agent version-skew safety"
    - "Shared pg_insert(...).on_conflict_do_update failure-row idiom persisting failed_at with payload columns NULL"
    - "Unconditional failure-marker clear in the success upsert set_ (outside exclude_unset) + empty-body clear branch"

key-files:
  created:
    - .planning/phases/81-per-stage-failure-persistence-retry-paths/81-03-SUMMARY.md
  modified:
    - src/phaze/schemas/agent_metadata.py
    - src/phaze/routers/agent_metadata.py
    - src/phaze/services/agent_client.py
    - src/phaze/tasks/metadata_extraction.py
    - tests/metadata/routers/test_agent_metadata.py
    - tests/metadata/tasks/test_metadata_extraction.py

key-decisions:
  - "Bodyless POST binds body=None and returns 200 (D-10/CR-02) so an old agent image still persists the marker AND clears the ledger — a required body would 422 and reopen the unbounded recovery loop"
  - "failed_at server-set via func.now() (mirrors put_analysis analysis_completed_at); payload columns stay NULL so done(metadata)=failed_at IS NULL derives FAILED not DONE"
  - "put_metadata clears failed_at/error_message unconditionally on both the field branch (added to set_) and the empty-body branch (switched DO NOTHING -> DO UPDATE set_ NULLs) — a successful retry must never read FAILED forever"
  - "error_message composed as '<reason>: <error>' truncated to 2000 (payload wire bound) — the NUL/surrogate + oversize class is bounded at the payload's error max_length"

patterns-established:
  - "Version-skew-safe terminal-ack endpoints: optional Pydantic body with None default, keyed off PATH file_id only (AUTH-01/T-45-05)"
  - "Failure-only output row derives FAILED via the Phase 78 done/failed clauses with zero derivation change (D-04)"

requirements-completed: [FAIL-02]

# Metrics
duration: 22min
completed: 2026-07-09
---

# Phase 81 Plan 03: Metadata Failure Persistence Summary

**`report_metadata_failed` now persists a durable metadata failure row (failed_at set, payload NULL) via a version-skew-safe optional-body endpoint, and `put_metadata` unconditionally clears the marker on success — a terminally-failed metadata file finally derives FAILED instead of being invisible.**

## Performance

- **Duration:** ~22 min
- **Started:** 2026-07-09T05:00:00Z (approx)
- **Completed:** 2026-07-09T05:22:00Z (approx)
- **Tasks:** 3
- **Files modified:** 6

## Accomplishments
- Added `MetadataFailurePayload` (mirrors `AnalysisFailurePayload`: `Literal["timeout","crashed","error"]` reason, bounded `error`, `extra='forbid'`).
- `report_metadata_failed` writes a `metadata` failure row via the shared `pg_insert(...).on_conflict_do_update` idiom (`failed_at=func.now()`, payload columns NULL) and keeps the same-transaction ledger clear. Bodyless POST → 200 (D-10/CR-02); with-body POST populates `error_message` as `"<reason>: <error>"`.
- `put_metadata` unconditionally clears `failed_at`/`error_message` on success in both the field branch and the empty-body branch (D-13), so a successful retry no longer reads FAILED forever.
- Widened `agent_client.report_metadata_failed(file_id, payload=None)` (httpx-only) and made the metadata task compose `MetadataFailurePayload(reason="error", error=str(exc)[:2000])` on the terminal ack.
- Proven in the metadata bucket in isolation (74 passed) and the agents bucket serially (441 passed).

## Task Commits

Each task was committed atomically:

1. **Task 1: Add MetadataFailurePayload schema** - `d6d76e4f` (feat)
2. **Task 2: failure-row writer + clear-on-success + widen client/task** - `76dd52e4` (feat)
3. **Task 3: tests (both body paths, extra-forbid, clear-on-success) + task-test updates** - `7b02fe72` (test)

_Plan is tdd-tagged on Task 3 only; implementation (Tasks 1-2) preceded the tests within this plan by design._

## Files Created/Modified
- `src/phaze/schemas/agent_metadata.py` - Added `MetadataFailurePayload`.
- `src/phaze/routers/agent_metadata.py` - Optional-body failure-row writer in `report_metadata_failed`; unconditional clear in `put_metadata` (both branches); `func` import + module constants.
- `src/phaze/services/agent_client.py` - `report_metadata_failed` widened to send optional JSON payload; TYPE_CHECKING import added.
- `src/phaze/tasks/metadata_extraction.py` - `except Exception as exc`; composed truncated `MetadataFailurePayload` passed to the ack.
- `tests/metadata/routers/test_agent_metadata.py` - Five new tests (bodyless persist+derive FAILED, with-body error_message, unknown-field 422, empty-body clear, field-PUT clear).
- `tests/metadata/tasks/test_metadata_extraction.py` - Updated two terminal-ack assertions for the widened payload argument.

## Decisions Made
See `key-decisions` frontmatter. Core: optional body (never `Body()`-wrapped) is the CR-02 version-skew guard; `failed_at` server-set; both `put_metadata` branches clear the marker; `error` bounded/truncated to 2000.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated two pre-existing terminal-ack task tests for the widened interface**
- **Found during:** Task 3 (running the metadata bucket)
- **Issue:** `test_terminal_attempt_acks_then_raises` and `test_terminal_ack_failure_reraises_original_error` asserted `report_metadata_failed.assert_awaited_once_with(file_id)`; Task 2 widened the call to pass a `MetadataFailurePayload`, so both assertions failed.
- **Fix:** Updated both to `assert_awaited_once_with(file_id, MetadataFailurePayload(reason="error", error=<exc-message>))`.
- **Files modified:** tests/metadata/tasks/test_metadata_extraction.py
- **Verification:** metadata bucket 74 passed in isolation.
- **Committed in:** `7b02fe72` (Task 3 commit)

**2. [Rule 1 - Bug] Truncate `str(exc)` before constructing the payload**
- **Found during:** Task 2 (task-side payload composition)
- **Issue:** `MetadataFailurePayload.error` has `max_length=2000`; a long exception string would raise a `ValidationError` inside the `except` block and clobber the original error E1 (WR-01 violation).
- **Fix:** `error=str(exc)[:2000]` before construction.
- **Files modified:** src/phaze/tasks/metadata_extraction.py
- **Verification:** WR-01 reraise test passes; mypy clean.
- **Committed in:** `76dd52e4` (Task 2 commit)

---

**Total deviations:** 2 auto-fixed (2 bugs)
**Impact on plan:** Both necessary for correctness (interface consistency + WR-01 error-precedence preservation). No scope creep.

## Issues Encountered
- `just test-bucket` does not export the test DB env vars (the `integration-test` recipe does). Ran buckets with `TEST_DATABASE_URL`/`MIGRATIONS_TEST_DATABASE_URL`/`PHAZE_REDIS_URL` pointed at the running `phaze-test-db` (5433/6380).
- The `agents` bucket is a serial DB bucket — running it with `-n auto` produced ~100 DB-contention errors; running it serially yields 441 passed (matches the known "DB buckets serial" convention).

## Threat Coverage
All `<threat_model>` mitigations implemented: T-81-03-01 (path-only file_id keying), T-81-03-02 (`extra='forbid'` → 422, tested), T-81-03-03 (bodyless → 200 + ledger clear, tested), T-81-03-04 (bounded/truncated `error`). No new threat surface introduced.

## Next Phase Readiness
- FAIL-02 writer is live (go-forward only, no backfill per D-03). Phase 80 recovery derivation can now rely on a persisted metadata failure marker.
- D-04 holds: no change to the Phase 78 `done_clause`/`failed_clause(METADATA)`; the Phase 79 shadow gate should stay green at wave merge (verified at the orchestrator's integration step, not here).

---
*Phase: 81-per-stage-failure-persistence-retry-paths*
*Completed: 2026-07-09*
