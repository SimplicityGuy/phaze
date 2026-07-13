---
phase: 90-destructive-migration-writer-removal
plan: 02
subsystem: pipeline-writers
tags: [readers-first, derived-state, dedup-undo, cloud-drain, CAS-guard, D-09, MIG-04]
requires:
  - phase: 90-01
    provides: "Every live FileRecord.state reader derives from output tables (markers / cloud_job); FileState-independent dedup-undo"
provides:
  - "All ~17 FileRecord.state WRITERS removed — files.state is written by nothing in src/phaze after this plan"
  - "Two CAS-guard writers (agent_metadata DISCOVERED→METADATA_EXTRACTED, agent_s3 PUSHING→PUSHED) deleted atomically (read+write) with named double-call idempotency proofs"
  - "dedup previous_state capture + DUPLICATE_RESOLVED dual-write + undo state-restore removed as a matched set; DedupResolution marker is the sole undo authority; undo_resolve payload is id-only"
  - "backends AWAITING_CLOUD/LOCAL_ANALYZING/PUSHING dual-writes removed — cloud_job sidecar + scheduling-ledger are the sole in-flight authorities"
affects:
  - 90-03 (PR-C: drops the files.state column + FileState enum — now safe, the column is dead)
tech-stack:
  added: []
  patterns:
    - "Delete a state dual-write and prove the surviving derived source (marker / cloud_job / ledger) preserves behavior with a named runnable test — never a threat-model claim"
    - "Migrate a broken `X.state == FileState.<value>` test assertion to the derived authority: cloud_job status for AWAITING_CLOUD/PUSHING, analysis_completed_at/failed_at for ANALYZED/ANALYSIS_FAILED, the metadata/DedupResolution marker for METADATA_EXTRACTED/DUPLICATE_RESOLVED"
    - "In cloud-drain tests, a helper that derives the old FileState semantics from the cloud_job sidecar keeps every existing assertion valid post-writer-removal"
key-files:
  created: []
  modified:
    - src/phaze/routers/agent_files.py
    - src/phaze/routers/agent_metadata.py
    - src/phaze/routers/agent_analysis.py
    - src/phaze/routers/agent_push.py
    - src/phaze/routers/agent_s3.py
    - src/phaze/routers/pipeline.py
    - src/phaze/services/backends.py
    - src/phaze/services/dedup.py
key-decisions:
  - "The two CAS guards were deleted atomically in PR-B (read+write together), NOT split into PR-A; idempotency is proven by named double-call regression tests, preserved by the outer cloud_job CAS (agent_s3) and the ON CONFLICT metadata upsert (agent_metadata)"
  - "dedup :270 previous_state capture + :274 DUPLICATE_RESOLVED write + :346 undo restore removed together; resolve_group now returns an id-only payload; undo is a pure marker DELETE"
  - "Cloud-drain test assertions on files.state migrated to the cloud_job sidecar via _is_awaiting_cloud / _awaiting_cloud_ids (test_pipeline) and a _states_for helper that derives AWAITING_CLOUD/PUSHING from cloud_job.status (test_staging_cron / test_dispatch_snapshot)"
patterns-established:
  - "Writer-removal proof: assert the surviving derived source still yields the correct status, end-to-end, with a runnable test"
requirements-completed: [MIG-04]
duration: 155min
completed: 2026-07-12
---

# Phase 90 Plan 02: Destructive-Migration Writer Removal (PR-B) Summary

**Removed all ~17 `FileRecord.state` writers (8 routers + 6 services incl. both CAS guards) now that PR-A made every reader derived — `files.state` is written by nothing, the column is dead and PR-C can drop it — proven by two named double-call CAS-idempotency tests and the full suite green on the derived sources.**

## Performance

- **Duration:** ~155 min
- **Started:** 2026-07-12
- **Completed:** 2026-07-12
- **Tasks:** 2 (plus extensive derived-source test migration)
- **Files modified:** 27 (8 src + 19 test)

## Accomplishments

- **Task 1 — router writers (8 sites):** `agent_files` DISCOVERED INSERT stamp; `agent_metadata` DISCOVERED→METADATA_EXTRACTED CAS (read+write, atomic); `agent_analysis` ANALYZED + ANALYSIS_FAILED writes; `agent_push` PUSHED + AWAITING_CLOUD dual-writes; `agent_s3` PUSHING→PUSHED CAS + AWAITING_CLOUD spill; `pipeline` backfill DISCOVERED reset + two retry FINGERPRINTED resets. Two named idempotency proofs added. (commit `a136f511`)
- **Task 2 — service writers (6 sites):** `backends` AWAITING_CLOUD (hold) + LOCAL_ANALYZING (local dispatch) + PUSHING×2 (compute/kueue dispatch); `dedup` DUPLICATE_RESOLVED writer + previous_state capture + undo restore (matched set). DedupResolution marker is the sole undo authority; `undo_resolve` payload is now id-only. (commit `cc0bec60`)
- **Derived-source test migration:** every test asserting a removed-writer state value migrated to the derived authority — cloud_job status, `analysis_completed_at`/`failed_at`, or the metadata/DedupResolution marker. (commits `bcc33e2f`, `51dd3fae`)
- **Post-removal proof:** `grep -rnE "\.values\([^)]*state=|FileRecord\.state\s*=\s*FileState|\.state\s*=\s*FileState" src/phaze` returns nothing (all writers gone); `FileState` enum + `files.state` column left intact (PR-C drops them); full suite 3443 passed.

## Task Commits

1. **Task 1: Remove router-side writers (8 sites incl. both CAS guards) + prove CAS-removal idempotency** — `a136f511` (feat)
2. **Task 2: Remove service-side writers (backends x4, dedup x2) + dead branches** — `cc0bec60` (feat)
3. **Fallout: routing-seam + analysis-spike derived assertions** — `bcc33e2f` (test)
4. **Fallout: cloud-drain + dedup-audit derived assertions** — `51dd3fae` (test)

## Files Created/Modified

**Source (writers removed):**
- `src/phaze/routers/agent_files.py` — dropped the DISCOVERED bulk-upsert INSERT stamp; removed unused FileState import
- `src/phaze/routers/agent_metadata.py` — removed the DISCOVERED→METADATA_EXTRACTED CAS (read+write); dropped now-unused `update` + model imports; the metadata ON CONFLICT upsert is the idempotency authority
- `src/phaze/routers/agent_analysis.py` — dropped ANALYZED + ANALYSIS_FAILED writes; `analysis_completed_at`/`failed_at` are the derived authority
- `src/phaze/routers/agent_push.py` — dropped PUSHED + AWAITING_CLOUD dual-writes gated behind the cloud_job CAS
- `src/phaze/routers/agent_s3.py` — removed the PUSHING→PUSHED CAS (idempotency preserved by the outer cloud_job UPLOADING→UPLOADED CAS + deterministic submit key) and the AWAITING_CLOUD spill write
- `src/phaze/routers/pipeline.py` — dropped the backfill DISCOVERED reset loop + its pre-routing commit, and the two retry FINGERPRINTED resets (marker clear is the sole retry effect)
- `src/phaze/services/backends.py` — removed AWAITING_CLOUD (hold), LOCAL_ANALYZING (local dispatch), PUSHING (compute + kueue dispatch); cloud_job sidecar / scheduling-ledger are the authorities; removed unused FileState import
- `src/phaze/services/dedup.py` — removed the DUPLICATE_RESOLVED writer, the `previous_state` capture, and the undo state-restore as a matched set; `undo_resolve` is a pure id-keyed marker DELETE; removed unused `update` + FileState imports

**Tests (2 required proofs + 17 derived-source migrations — see Deviations):**
- `tests/metadata/routers/test_agent_metadata.py` — added `test_metadata_callback_idempotent_after_cas_removal` (double-call proof); converted the advance test to the marker contract
- `tests/agents/routers/test_agent_s3.py` — added `test_s3_push_status_transition_idempotent_after_cas_removal` (double-call proof); converted the kueue-seam tests to no-state-flip
- 15 further test files migrated to derived assertions (agents/analyze/shared/integration/review/discovery buckets)

## Decisions Made

- **CAS guards deleted atomically in PR-B, proven not asserted:** both `.where(state==...)` READs were removed together with their `.values(state=...)` WRITEs. `test_metadata_callback_idempotent_after_cas_removal` and `test_s3_push_status_transition_idempotent_after_cas_removal` each invoke the endpoint TWICE and assert no duplicate/incorrect effect — the ON CONFLICT metadata upsert and the outer cloud_job CAS (+ deterministic submit key) supply idempotency.
- **dedup restore removed as a matched set:** `resolve_group` no longer captures `previous_state` and returns an id-only payload; `undo_resolve` is a pure marker DELETE keyed on the payload id-set. The DedupResolution marker is the sole undo authority (D-05).
- **Derived-authority test contract:** rather than delete broken state assertions, each was migrated to the source PR-A cut over to (cloud_job status, analysis markers, or the DedupResolution/metadata marker), so the tests still prove the affected flow end-to-end.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Extensive test fallout beyond the plan's 2 declared test files**
- **Found during:** Tasks 1 & 2 (and the full-suite sweep after each)
- **Issue:** Removing the writers broke every test that asserted a removed-writer state value (directly `X.state == FileState.<v>` or via `select(FileRecord).where(state == ...)`). The plan declared only the two idempotency test files; the real fallout spanned 17 more test files across the agents, analyze, shared, integration, review, and discovery buckets (mirroring PR-A's Rule-3 test fallout).
- **Fix:** Migrated each broken assertion to the derived authority PR-A established: cloud_job status for AWAITING_CLOUD/PUSHING (`_is_awaiting_cloud` / `_awaiting_cloud_ids` in test_pipeline; a cloud_job-deriving `_states_for` in test_staging_cron; derived `state_counts` in test_dispatch_snapshot), `analysis_completed_at`/`failed_at` for ANALYZED/ANALYSIS_FAILED, and the metadata/DedupResolution marker for METADATA_EXTRACTED/DUPLICATE_RESOLVED. Updated the dedup source-scan guard (`test_dedup_fingerprint_source_scan`) to require ZERO `FileState.DUPLICATE_RESOLVED` refs (clean absence, mirroring fingerprint.py) now that the last writer is gone.
- **Files modified:** the 17 non-declared test files listed under key-files/tests
- **Verification:** each file passes in isolation and in groups on the fresh test DB (port 5433, +asyncpg); full suite 3443 passed
- **Committed in:** `a136f511`, `cc0bec60`, `bcc33e2f`, `51dd3fae`

**2. [Rule 1 - Bug] Async lazy-reload after expire_all in migrated tests**
- **Found during:** the test migration (test_pipeline, test_shared)
- **Issue:** Two migrated helpers/tests accessed ORM attributes (`long_failed.id`, `f.id`) after a helper called `session.expire_all()`, triggering a MissingGreenlet async lazy-reload; and the initial `_is_awaiting_cloud` helper's `expire_all()` expired the caller's ORM objects.
- **Fix:** Removed the unnecessary `expire_all()` from the cloud_job query helpers (a fresh `execute` always hits the DB) and captured PK ids before any expiry.
- **Files modified:** tests/shared/routers/test_pipeline.py
- **Committed in:** `bcc33e2f`, `51dd3fae`

---

**Total deviations:** 2 auto-fixed (2 Rule-3/1 blocking test-fallout classes). **Impact:** No source scope creep — only the 8 declared src files changed; all extra work was test migration to the derived sources PR-A already established. No new dependencies, no migration, no column drop.

## Issues Encountered

- **Test-DB pollution from an interrupted full-suite run:** a killed 10-min run left a committed `test-fileserver` agent (`pk_agents` UniqueViolation) that produced spurious setup ERRORs. Resolved by reprovisioning the ephemeral DB (`just test-db-down && just test-db`).
- **Non-hermetic `test_task_split.py` isolation flake (pre-existing, NOT this plan):** 3 tests in `tests/shared/core/test_task_split.py` fail only in a single-process full-suite run and PASS in isolation (16 passed). The file has ZERO `state`/`FileState` references and is unrelated to files.state — this is the documented `get_settings` lru_cache / saq-stub cross-test leak that the hermetic `just test-bucket` runner isolates. Not introduced by this plan.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- **PR-C (90-03) is unblocked:** `files.state` is now written by nothing and read by nothing functional (only `shadow_compare.py` reads it as the migration-verification invariant checker, which PR-C removes alongside the column). The `FileState` enum + `String(30)` column remain defined so mypy/ruff stay green until PR-C drops them.
- No blockers.

## Self-Check: PASSED
- Commits FOUND: a136f511, cc0bec60, bcc33e2f, 51dd3fae
- Key src files FOUND: agent_files.py, agent_metadata.py, agent_analysis.py, agent_push.py, agent_s3.py, routers/pipeline.py, services/backends.py, services/dedup.py
- Writer-removal proof: `grep` for `.values(...state=` / `.state = FileState` in src/phaze returns nothing
- Idempotency proofs GREEN: test_metadata_callback_idempotent_after_cas_removal, test_s3_push_status_transition_idempotent_after_cas_removal
- Full suite: 3443 passed (3 pre-existing non-hermetic task_split isolation flakes, pass in isolation)

---
*Phase: 90-destructive-migration-writer-removal*
*Completed: 2026-07-12*
