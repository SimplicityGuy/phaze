---
phase: 90-destructive-migration-writer-removal
plan: 01
subsystem: pipeline-readers
tags: [readers-first, derived-state, dedup-undo, search-facet, D-09, D-11, D-12]
requires:
  - stage_status.py clause builders (Phase 78)
  - cloud_job sidecar (Phase 77/83)
provides:
  - "Every live FileRecord.state reader now derives from output tables (markers / cloud_job); no dashboard/proposal/backfill/held/retry/search/analyze-workspace path reads files.state"
  - "FileState-independent dedup-undo: marker DELETE + gate keyed on the payload id-set alone"
affects:
  - src/phaze/services/pipeline.py
  - src/phaze/services/dedup.py
  - src/phaze/services/search_queries.py
  - src/phaze/routers/pipeline.py
  - src/phaze/routers/search.py
  - src/phaze/templates/pipeline/partials/analyze_workspace.html
tech-stack:
  added: []
  patterns:
    - "compose LOCKED stage_status clause builders verbatim where join-safe; where the builder's target table must be outer-joined for display, spell the predicate against the joined column using the builder's exact semantics"
    - "derived idempotency guard ~exists(active cloud_job) replaces a former state-transition guard"
key-files:
  created: []
  modified:
    - src/phaze/services/pipeline.py
    - src/phaze/routers/pipeline.py
    - src/phaze/services/dedup.py
    - src/phaze/services/search_queries.py
    - src/phaze/routers/search.py
    - src/phaze/templates/pipeline/partials/analyze_workspace.html
decisions:
  - "get_analyze_stage_files derives membership + flags from the outer-joined analysis/cloud_job columns (builder semantics), NOT by composing the AnalysisResult/CloudJob-correlated exists builders, to avoid a SQLAlchemy auto-correlation InvalidRequestError; inflight_clause (over the un-joined scheduling_ledger) IS composed verbatim"
  - "_backfill_candidates_stmt gained ~exists(active cloud_job) as the derived idempotency guard, replacing the removed state==ANALYSIS_FAILED->AWAITING_CLOUD transition that formerly blocked a double-click backfill"
  - "search: kept the union `state` slot as a neutral NULL literal for the file branch (column parity) so tracklist/discogs status still populate it (D-11 option A)"
  - "dedup-undo previous_state restore neutralises returned ids lacking a parseable previous_state to FileState.DISCOVERED (shadow invariant stays green)"
metrics:
  duration_min: 60
  tasks: 4
  files_modified: 15
  completed: 2026-07-12
---

# Phase 90 Plan 01: Readers-First state-reader cutover (PR-A) Summary

Converted every live `FileRecord.state` reader — dashboard count cards, the analyze workspace, the proposal pending-set, the analyze re-drive backfill, the held-files ledger seed, the failure-retry endpoints, and the search facet — to derive from the Phase-78 `stage_status.py` clause builders and the `cloud_job` sidecar while `files.state` remains fully intact (DDL-free, reversible). Also decoupled the dedup-undo marker DELETE from `FileState` so PR-B removing `previous_state` can never no-op the undo.

## What shipped

- **Task 1 — dashboard counts (D-12):** `get_analysis_failed_count` → `failed_clause(Stage.ANALYZE)`; `get_pushing_count` → `cloud_job.status IN (uploading, submitted)`; `get_pushed_count` → `cloud_job.status IN (uploaded, running)`. (commit `bf9b5c0a`)
- **Task 2 — analyze workspace / proposal / backfill / held / retry:** `get_analyze_stage_files` membership + `completed`/`awaiting_cloud`/`analysis_failed` derived flags (raw `state` dict key removed); `get_proposal_pending_batches` uses `~done_clause(Stage.PROPOSE)` (Pitfall 4 re-propose exclusion); `_backfill_candidates_stmt` + `get_analysis_failed_files` use `failed_clause(Stage.ANALYZE)`; generic `get_files_by_state` deleted; router `held_files` drops the redundant `state==AWAITING_CLOUD` sub-filter; `retry_analysis_failed_file` scopes on `failed_clause`; template switched to `f.awaiting_cloud`/`f.analysis_failed`. (commit `3b801b02`)
- **Task 3 — search facet (D-11):** deleted the `file_state` search facet (service + route) with no derived replacement; kept the union `state` slot as a neutral literal for tracklist/discogs status parity; files/tracklists/discogs always union. (commit `beaffb04`)
- **Task 4 — dedup-undo blocker fix:** `undo_resolve` marker DELETE + early-return gate now key on the payload id-set (`entry["id"]`) alone; a separate best-effort `previous_by_id` feeds only the legacy (PR-B-doomed) state restore, neutralising to `DISCOVERED` when unparseable. Real `/resolve`→`/undo`, `/resolve-all`→`/undo-all`, and id-only round-trip regressions use the ACTUAL server-rendered `file_states` payload. (commit `c6f0a040`)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] get_analyze_stage_files could not compose the AnalysisResult/CloudJob-correlated builders verbatim**
- **Found during:** Task 2
- **Issue:** The plan directed composing `done_clause`/`failed_clause`/`awaiting_candidate_clause` verbatim in the WHERE, but this query OUTER-JOINS AnalysisResult + CloudJob for the display columns. SQLAlchemy auto-correlated those tables out of the builders' inner `exists(...)`, raising `InvalidRequestError: returned no FROM clauses`.
- **Fix:** Spelled the membership + flags against the already-joined columns using the builders' EXACT semantics (`analysis_completed_at IS NOT NULL` = done_clause; `failed_at IS NOT NULL` = failed_clause; `cloud_job.status IN (awaiting,uploading,submitted,uploaded,running)`), while composing `inflight_clause` (over the un-joined `scheduling_ledger`) verbatim. The derived result is byte-equivalent; `test_stage_status_equivalence.py` stays green (59 passed).
- **Files modified:** src/phaze/services/pipeline.py
- **Commit:** 3b801b02

**2. [Rule 1 - Bug] backfill double-click idempotency regressed under the derived marker**
- **Found during:** Task 2 (`test_backfill_double_click_holds_nothing_new`)
- **Issue:** The old `state==ANALYSIS_FAILED` backfill gate transitioned to `AWAITING_CLOUD` after the first click, blocking re-selection. The derived `failed_clause` marker does NOT transition (backfill routes to cloud without clearing it), so a second click re-found the same files — re-opening the over-enqueue class (D-10).
- **Fix:** Added `~exists(select(CloudJob.id).where(file_id AND status IN _ACTIVE_CLOUD_STATUSES))` to `_backfill_candidates_stmt`, mirroring the identical guard already in `get_discovered_files_with_duration`. A file already held/pushing/pushed is excluded.
- **Files modified:** src/phaze/services/pipeline.py
- **Commit:** 3b801b02

**3. [Rule 3 - Blocking] test fallout beyond the plan's declared test files**
- **Found during:** Tasks 2 & 3
- **Issue:** The source cutovers broke tests outside the plan's declared `files_modified` because they seeded `state=` without the derived markers/cloud_job: `tests/shared/routers/test_pipeline.py` (retry/backfill/dashboard), `tests/shared/core/test_enrich_analyze_workspaces.py` (analyze workspace render), `tests/shared/core/test_pipeline_dag_context.py` (window counts), `tests/shared/core/test_no_default_queue_producers.py` (backfill source guard), and the actual search test locations `tests/identify/...` (plan cited `tests/shared/...`).
- **Fix:** Updated each to seed the derived source (failed marker / cloud_job) and to assert the derived contract; the source-guard now asserts `failed_clause(Stage.ANALYZE)` instead of `FileState.ANALYSIS_FAILED`.
- **Commits:** 3b801b02, beaffb04

## Mutation check (Task 4)

Temporarily reverted `undo_resolve`'s gate + DELETE scope to the old `previous_state`-parsed authority and confirmed `test_undo_roundtrip_id_only_payload_still_deletes_marker` went RED (1 failed), then restored. The id-only guard genuinely protects the blocker.

## Process note

During the Task-4 mutation check a `git restore src/phaze/services/dedup.py` (used to undo the mutation) also wiped the still-uncommitted Task-4 rewrite. Detected immediately via `grep`/`git diff`, re-applied the rewrite, and re-verified. No committed work was lost. Lesson: never `git restore` a file that carries uncommitted intended work — back up to a scratch path instead.

## Verification

- `uv run ruff check .` — clean.
- `uv run mypy .` — Success, 209 source files.
- `test_stage_status_equivalence.py` — 59 passed (no clause re-spelling).
- Buckets (isolated, port 5433): shared 1129 passed · analyze 576 passed · review 431 passed · integration 274 passed.
- `files.state` column + `FileState` enum untouched; dedup `:270` capture / `:274` dual-write / `:346` restore write-side set left intact for PR-B.

## Scope boundary

No writer removed, no migration, no column drop — PR-A is independently shippable and reversible. STATE.md / ROADMAP.md untouched (orchestrator-owned).

## Self-Check: PASSED
- Commits FOUND: bf9b5c0a, 3b801b02, beaffb04, c6f0a040
- Key files FOUND: pipeline.py, dedup.py, search_queries.py, routers/pipeline.py, routers/search.py, analyze_workspace.html
