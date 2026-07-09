---
phase: 81-per-stage-failure-persistence-retry-paths
plan: 06
subsystem: api
tags: [fastapi, htmx, sqlalchemy, postgres, saq, metadata, failure-retry]

# Dependency graph
requires:
  - phase: 81-per-stage-failure-persistence-retry-paths
    plan: 03
    provides: "report_metadata_failed persists a metadata failure row (failed_at set, payload NULL); put_metadata clears failed_at on success"
provides:
  - "get_metadata_failed_files(session) -> list[FileRecord] — correlated exists on metadata.failed_at IS NOT NULL (reuses the failed_clause(METADATA) shape)"
  - "POST /pipeline/metadata-failed/retry — bulk operator retry re-enqueuing every failed-metadata file with the COMPLETE ExtractMetadataPayload, Phase-30-guarded, no state flip (D-11)"
  - "metadata_retry_response.html — stage-labelled HTMX ack fragment (metadata-worded, not analyze)"
affects: [metadata-stage, operator-ui, pipeline-router]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Bulk failed-stage retry endpoint mirroring retry_analysis_failed's Phase-30 guard ordering, MINUS the state flip (metadata has no terminal FileState)"
    - "Correlated-exists failed-set reader reusing the Phase-78 failed_clause(stage) shape for a stage without a FileState bucket"

key-files:
  created:
    - src/phaze/templates/pipeline/partials/metadata_retry_response.html
    - tests/integration/routers/test_pipeline_metadata_retry.py
    - .planning/phases/81-per-stage-failure-persistence-retry-paths/81-06-SUMMARY.md
  modified:
    - src/phaze/services/pipeline.py
    - src/phaze/routers/pipeline.py

key-decisions:
  - "D-11: the retry LEAVES the metadata failure row in place and re-enqueues; NO failed_at clear, NO row delete, NO f.state flip — put_metadata's clear-on-success (81-03) wipes failed_at only when real metadata lands (a zero-metadata row would otherwise read DONE forever)"
  - "D-12: mirror retry_analysis_failed's Phase-30 guard ordering — resolve the per-agent meta-lane queue ONCE, catch NoActiveAgentError and return WITHOUT enqueuing or mutating state (never the consumer-less default queue); reuse _enqueue_extraction_jobs which builds the COMPLETE ExtractMetadataPayload; central extract_file_metadata:<file_id> key dedups in-flight"
  - "Enqueue is awaited synchronously in the handler (like retry_analysis_failed's enqueue loop), not via asyncio.create_task — no state to commit before enqueue since D-11 flips nothing, and a deterministic ack count is testable"

requirements-completed: [FAIL-03]

# Metrics
duration: 13min
completed: 2026-07-09
---

# Phase 81 Plan 06: Metadata Failure Retry Path Summary

**`POST /pipeline/metadata-failed/retry` now re-enqueues every terminally-failed metadata file (`metadata.failed_at IS NOT NULL`) with the COMPLETE `ExtractMetadataPayload` on the per-agent `meta` lane, leaves the failure row in place (D-11), and returns a metadata-worded HTMX fragment — closing gap G-01 so a failed-metadata file is no longer a permanent dead-end that blocks `propose` (SC#3).**

## Performance

- **Duration:** ~13 min
- **Tasks:** 3
- **Files:** 2 modified, 2 created (+ this SUMMARY)

## Accomplishments
- Added `get_metadata_failed_files(session)` to `services/pipeline.py` — a pure-ORM correlated `exists(select(FileMetadata.id).where(file_id == FileRecord.id, FileMetadata.failed_at.isnot(None)))` reusing the Phase-78 `failed_clause(Stage.METADATA)` shape (no f-string SQL, T-42-03).
- Added `POST /pipeline/metadata-failed/retry` (`retry_metadata_failed`) to `routers/pipeline.py`, mirroring `retry_analysis_failed`'s Phase-30-hardened ordering minus the state flip: resolve the per-agent `meta`-lane queue once, catch `NoActiveAgentError` and return without enqueuing/mutating (no default-queue fallthrough), then re-enqueue via the shared `_enqueue_extraction_jobs` producer (COMPLETE `ExtractMetadataPayload`, central `extract_file_metadata:<file_id>` dedup key). NO `f.state` assignment (D-11).
- Created the stage-labelled `metadata_retry_response.html` fragment (worded "…failed file(s) for metadata extraction." — never "for analysis").
- Integration tests (5, all passing in `integration`-bucket isolation): re-enqueue count + complete payload on `phaze-agent-nox-meta`, failure rows survive a not-yet-succeeded retry (D-11), `NoActiveAgentError` enqueues nothing / mutates nothing / no default-queue fallthrough, zero-failed no-op, and `get_metadata_failed_files` returns exactly the failed set.

## Task Commits

1. **Task 1: get_metadata_failed_files** — `8645f242` (feat)
2. **Task 2: POST /pipeline/metadata-failed/retry endpoint + fragment** — `8abcb10a` (feat)
3. **Task 3: integration tests** — `27e920e0` (test)

_Plan is tdd-tagged on Task 3 only; the endpoint (Tasks 1-2) is the retry twin of an existing shipped path, so implementation preceded the tests within this plan (same shape as 81-03)._

## Files Created/Modified
- `src/phaze/services/pipeline.py` — added `get_metadata_failed_files`.
- `src/phaze/routers/pipeline.py` — added `retry_metadata_failed` endpoint + `get_metadata_failed_files` import.
- `src/phaze/templates/pipeline/partials/metadata_retry_response.html` — new metadata-worded HTMX ack.
- `tests/integration/routers/test_pipeline_metadata_retry.py` — 5 integration tests.

## Decisions Made
See `key-decisions` frontmatter. Core: D-11 leaves the row (no clear/delete/flip); D-12 mirrors the Phase-30 guard; enqueue is awaited synchronously (nothing to commit pre-enqueue).

## Deviations from Plan

None — plan executed exactly as written. All three tasks landed as specified; no auto-fixes were required.

## Threat Coverage
All `<threat_model>` mitigations implemented:
- **T-81-06-01** (default-queue fallthrough): queue resolved once, `NoActiveAgentError` caught → return without enqueue/mutation; asserted by `test_retry_no_active_agent_enqueues_nothing_and_mutates_nothing` (capture == []).
- **T-81-06-02** (fan-out / duplicate in-flight): central `extract_file_metadata:<file_id>` deterministic key dedups; no pre-enqueue commit needed (D-11 mutates nothing).
- **T-81-06-03** (dead-lettered file_id-only enqueue): reuse `_enqueue_extraction_jobs` (COMPLETE 4-field `ExtractMetadataPayload`, `extra='forbid'`); asserted payload validates.
- **T-81-06-04** (premature row clear/delete): D-11 leaves the row; asserted by `test_retry_leaves_failure_rows_in_place`.
No new threat surface introduced.

## Issues Encountered
- The `integration` bucket run surfaced ~8-14 **setup errors** (varying run-to-run) in `test_shadow_compare.py` / `test_stage_status_equivalence.py` — an `IntegrityError` on the shared `phaze_test` legacy-agent seed. Confirmed **pre-existing and unrelated to 81-06**: the two modules error identically when run alone (no metadata-retry file involved), with counts varying run-to-run — the documented local colima shared-DB-contention flake, not an assertion failure. 81-06 touches only `pipeline.py` (service + router), a new template, and a new test file — none of which the erroring modules import. The 5 new tests pass deterministically in isolation and in the bucket.
- Explicit `mypy` on the test file follows the import into `tests/_queue_fakes.py:336` and reports 5 pre-existing errors there (untouched shared harness); `tests/` is excluded from the enforced mypy config, and the new test file itself is clean. Pre-commit `mypy .` passed on every commit.

## Next Phase Readiness
- FAIL-03 backend retry path is live. The operator UI stage-matrix / failure-retry surface (Phase 87) can now wire a "Retry failed" button to `POST /pipeline/metadata-failed/retry` (the button-render + `/pipeline/stats` count wiring is Phase-87 scope, not this plan).
- D-11 holds: no change to the Phase-78 `done_clause`/`failed_clause(METADATA)`; the Phase-79 shadow gate should stay green at wave merge (verified at the orchestrator's integration step, not here).

## Self-Check: PASSED

- SUMMARY.md present.
- Created files verified on disk: `metadata_retry_response.html`, `test_pipeline_metadata_retry.py`.
- Commits verified: `8645f242`, `8abcb10a`, `27e920e0`.

---
*Phase: 81-per-stage-failure-persistence-retry-paths*
*Completed: 2026-07-09*
