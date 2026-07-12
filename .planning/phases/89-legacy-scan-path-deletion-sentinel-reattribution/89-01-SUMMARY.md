---
phase: 89-legacy-scan-path-deletion-sentinel-reattribution
plan: 01
subsystem: ingestion / data-model
tags: [legacy-retirement, deletion, model-default, agent-attribution, test-blast-radius]
requires:
  - "agents.id FK target (Agent model)"
  - "agent_files.py ON-CONFLICT upsert (surviving ingest path, AUTH-01)"
provides:
  - "No POST /api/v1/scan or GET /api/v1/scan/{batch_id} route; app boots without the scan router (LEGACY-01)"
  - "agent_id required at model level — no Python default; no row can silently be attributed to legacy-application-server (LEGACY-03 model half)"
  - "Two fewer FileState-writing upsert sites (discover_and_hash_files/bulk_upsert_files gone) — smaller Phase 90 migration surface"
affects:
  - "Phase 90 (destructive 034 + writer removal): fewer FileState writers remain"
tech-stack:
  added: []
  patterns:
    - "Explicit agent_id at every FileRecord/ScanBatch construction (no reliance on a model default)"
    - "Shared test seed is a real kind='fileserver' agent (test-fileserver), not the sentinel"
key-files:
  created: []
  modified:
    - src/phaze/main.py
    - src/phaze/models/file.py
    - src/phaze/models/scan_batch.py
    - tests/conftest.py
    - tests/shared/core/test_phase02_gaps.py
    - tests/shared/core/test_no_auto_metadata_enqueue.py
    - tests/metadata/tasks/test_metadata_extraction.py
    - "62 test files (Pitfall-2 explicit agent_id injection)"
  deleted:
    - src/phaze/routers/scan.py
    - src/phaze/services/ingestion.py
    - src/phaze/schemas/scan.py
    - tests/discovery/routers/test_scan.py
    - tests/discovery/services/test_ingestion.py
    - tests/discovery/test_rescan_preserves_state.py
decisions:
  - "D-08: shared conftest seed repointed legacy-application-server -> test-fileserver (kind='fileserver' explicit)"
  - "D-06/D-07: dropped only the default= kwarg on agent_id; kept nullable=False + RESTRICT FK (no DDL)"
  - "Kept models/agent.py LEGACY_AGENT_ID constant (labels historical DB data + still test-referenced) — models grep interpreted as 'no default=' per plan must-have"
  - "Injected the literal agent_id=\"test-fileserver\" everywhere (equals each integration test's repointed _LEGACY_AGENT_ID), FK-valid on every DB path"
metrics:
  tasks: 2
  commits: 2
  files_changed: 77
  tests_passing: 3426
  duration: ~2h
  completed: 2026-07-11
---

# Phase 89 Plan 01: Legacy Scan Path Deletion & agent_id Default Removal Summary

Deleted the orphaned `POST /api/v1/scan` -> `run_scan` ingestion trio (`routers/scan.py`, `services/ingestion.py`, `schemas/scan.py`), unwired it from `main.py`, and dropped the `agent_id` Python model default from `FileRecord`/`ScanBatch` so every write must now supply an explicit owning agent — the entire suite kept green by repointing the shared seed to a real `test-fileserver` fileserver and making all 84 default-reliant test constructions explicit.

## What shipped

- **LEGACY-01 (source + test deletion):** three source files deleted wholesale; `main.py` no longer imports or mounts `scan.router`; `import phaze.main` boots clean; `grep run_scan|discover_and_hash_files|bulk_upsert_files src/phaze/` returns nothing. This also removed the two surviving `FileState`-writing upsert sites inside the deleted trio, shrinking the Phase 90 migration surface.
- **LEGACY-03 (model half):** `default="legacy-application-server"` dropped from the `agent_id` `mapped_column` in both `models/file.py` and `models/scan_batch.py` (kept `nullable=False` + `ondelete="RESTRICT"` — pure model-code change, no DDL). `agent_id` is now a required construction argument.
- **Test-fileserver seed (D-08):** the shared `conftest.py` `async_engine` fixture seeds a real `kind="fileserver"` agent (`test-fileserver`) instead of the sentinel; the 11 integration `_LEGACY_AGENT_ID` constants and the explicit sentinel literals in `test_scan_deletion.py`/`test_scan_reaper.py`/`test_pipeline.py` were repointed to it.
- **Pitfall-3 preservation:** the deleted `tests/discovery/test_rescan_preserves_state.py` exercised the removed `bulk_upsert_files` path; the MIG-03 ON-CONFLICT state-preservation invariant remains covered by the surviving `tests/agents/test_rescan_preserves_state.py` (agent_files endpoint), so no assertion needed porting.

## Deviations from Plan

The plan's `files_modified` frontmatter listed ~26 files, but the real Pitfall-2 blast radius was substantially larger. All expansions are Rule-3 (auto-fix blocking issues) — required for the suite to stay green once the default was removed. No user-facing behavior change; all are test-only.

**1. [Rule 3 - Blocking] 84 default-reliant constructions across 62 files (plan estimated far fewer explicitly)**
- **Found during:** Task 1 (AST sweep of `FileRecord(`/`ScanBatch(` calls with no `agent_id` argument).
- **Issue:** The model default's value (`legacy-application-server`) was tied to the old seed identity. Repointing the conftest seed to `test-fileserver` immediately invalidates every construction that relied on the default — not just after Task 2's default removal. So all 84 sites had to become explicit in Task 1.
- **Fix:** Scripted injection of `agent_id="test-fileserver"` (FK-valid on every DB path, since each integration test's `_LEGACY_AGENT_ID` was repointed to the same string). Formatted with ruff.
- **Commit:** 0a6d10b8

**2. [Rule 3 - Blocking] `_make_file` `**kwargs` splat helper in test_staging_cron.py**
- **Found during:** Task 1 full-suite run (21 FK violations, all in `test_staging_cron.py`).
- **Issue:** The AST injector correctly skipped `**kwargs`-splat constructions (can't safely auto-inject a kwarg alongside a splat). This one helper relied on the default; its `**kwargs` only ever carries `created_at`.
- **Fix:** Added explicit `agent_id="test-fileserver"` to the `_make_file` FileRecord. All 21 staging_cron tests pass.
- **Commit:** 0a6d10b8

**3. [Rule 3 - Blocking] Shared-conftest tests depending on the legacy seed identity (not in plan file list)**
- **Found during:** Task 1 analysis.
- **Issue:** `test_agent_bootstrap.py` deletes the conftest-seeded agent to simulate a fresh DB and asserts exact agent counts; `test_scan_reaper.py`/`test_pipeline.py` seed `ScanBatch`/`FileRecord` with `agent_id=LEGACY_AGENT_ID`; `test_scan_deletion.py` uses explicit `agent_id="legacy-application-server"` literals. All break under the reseed.
- **Fix:** Repoint `test_agent_bootstrap` fresh-DB deletes to `test-fileserver`; repoint the explicit sentinel references in the other three (dropping now-unused `LEGACY_AGENT_ID` imports in `test_scan_reaper`/`test_pipeline`).
- **Commit:** 0a6d10b8

**4. [Rule 3 - Blocking] `scan_batch.py:46` prose comment referenced `run_scan`**
- **Found during:** Task 2 acceptance grep (`run_scan` must not appear in `src/`).
- **Fix:** Reworded the heartbeat comment ("run_scan's terminal updates" -> "the batch's terminal updates").
- **Commit:** 8946f683

**Plan/acceptance note (interpretation, not a deviation):** the Task-2 acceptance `grep -rn "legacy-application-server" src/phaze/models/ returns nothing` conflicts with the must-have "keep `models/agent.py` `LEGACY_AGENT_ID`" (that constant literally contains the string). Interpreted per the parenthetical "(defaults gone)": verified no `default="legacy-application-server"` remains in `file.py`/`scan_batch.py`; the `agent.py` constant is intentionally retained.

## Verification

- `import phaze.main` exits 0 (app boots without the scan router).
- All Task-1 and Task-2 acceptance greps pass (three source files deleted; no trio refs; no `include_router(scan.router)`; no model `default=`).
- ruff check + ruff format + mypy clean on all changed source.
- Full `uv run pytest`: **3426 passed, 3 failed.** The 3 failures are `tests/shared/core/test_task_split.py` only — a documented pre-existing full-suite cross-test pollution flake (`agent_worker` import poisons the `get_settings` lru_cache singleton). They pass 16/16 in isolation and in the project's per-bucket CI (`tests/buckets.json`), and are unrelated to Phase 89 (no task-registration code was touched). Pre-commit hooks (ruff/ruff-format/bandit/mypy) passed on every committed change.
- D-05 contingency discharged: no template/JS references `/api/v1/scan`; the only residual mentions are the plan-sanctioned prose comments in `agent_files.py` and the unrelated tracklist fingerprint-scan (`/tracklists/scan/status`).

## Notes for downstream

- Phase 90 (destructive `034` + writer removal) now has two fewer `FileState` writers to reason about.
- The `legacy-application-server` sentinel row + column-default DB migration (LEGACY-02) and historical reattribution are Plan 89-02 / later — this plan is the source-and-model-code half only.

## Self-Check: PASSED

- All 6 claimed deletions confirmed absent; all modified files present.
- Both commit hashes (0a6d10b8, 8946f683) exist in the branch history.
