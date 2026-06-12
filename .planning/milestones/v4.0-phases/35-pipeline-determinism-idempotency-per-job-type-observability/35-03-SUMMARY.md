---
phase: 35-pipeline-determinism-idempotency-per-job-type-observability
plan: 03
subsystem: pipeline-observability
tags: [reconcile, dashboard, dag, stage-progress, sqlalchemy]
requires:
  - "services/pipeline.py get_pipeline_stats / get_queue_activity (linear-state stats + failure-isolation idiom)"
  - "routers/pipeline.py:116-128 convergence-gate query (files with both metadata AND analysis)"
  - "constants.EXTENSION_MAP / FileCategory (music/video denominator filter)"
provides:
  - "services/pipeline.py get_stage_progress(session) — authoritative per-DAG-node DB-truth reconcile (D-03)"
  - "Per-node done/total dict the DAG nodes (35-04/35-05) consume"
affects:
  - "35-04 / 35-05 DAG canvas nodes (data source)"
tech-stack:
  added: []
  patterns:
    - "COUNT(DISTINCT file_id/tracklist_id) on each stage's OUTPUT table as reconcile truth"
    - "Per-source failure isolation via _safe_count (degrade to 0 + rollback, never raise into the 5s poll)"
    - "Denominator-less node (scan_search total=None) — no fabricated fraction"
key-files:
  created:
    - "tests/test_stage_progress.py"
  modified:
    - "src/phaze/services/pipeline.py"
decisions:
  - "get_stage_progress counts stage OUTPUT tables (not the linear FileRecord.state) so parallel stages report independent done-counts (RESEARCH Q5)"
  - "match.done walks discogs_links→tracklist_tracks→tracklist_versions for DISTINCT tracklist_id (discogs_links carries only track_id)"
  - "execute.done walks execution_log→proposals for DISTINCT file_id of COMPLETED rows (execution_log carries only proposal_id)"
  - "scan_search.total is None (em-dash sentinel); no DB table defines 'should get a tracklist'"
  - "get_pipeline_stats left untouched — still truth for the strictly-linear Proposals/Approved/Executed tail"
metrics:
  duration: "~25m"
  completed: "2026-06-12"
  tasks: 2
  files: 2
---

# Phase 35 Plan 03: Per-Stage Reconcile Query (get_stage_progress) Summary

Added `get_stage_progress(session)` — the authoritative per-DAG-node reconcile source (D-03) that counts each stage's OWN output table via `COUNT(DISTINCT file_id/tracklist_id)`, replacing the structurally-wrong linear `FileRecord.state` stats for every parallel node, with a denominator-less Scan/Search head node and per-source failure isolation.

## What Was Built

### Task 1 — `get_stage_progress` (`src/phaze/services/pipeline.py`)
`async def get_stage_progress(session) -> dict[str, dict[str, int | None]]` returns `{node: {"done": int, "total": int | None}}` for nine DAG nodes:

| Node | done source (verified output table) | total |
|------|-------------------------------------|-------|
| discovery | `COUNT(files)` | itself (bar 100%) |
| metadata | `COUNT(DISTINCT file_id)` in `metadata` | music/video file count |
| fingerprint | `COUNT(DISTINCT file_id)` in `fingerprint_results` (status='completed') | music/video count |
| analyze | `COUNT(DISTINCT file_id)` in `analysis` | music/video count |
| scan_search | `COUNT(DISTINCT file_id)` in `tracklists` | **None** (em-dash; no fabricated denom) |
| scrape | `COUNT(DISTINCT tracklist_id)` in `tracklist_versions` | `COUNT(tracklists)` |
| match | `COUNT(DISTINCT tracklist_id)` walked `discogs_links→tracklist_tracks→tracklist_versions` | `COUNT(tracklists)` |
| proposals | `COUNT(DISTINCT file_id)` in `proposals` | convergence set (both `metadata` AND `analysis`) |
| execute | `COUNT(DISTINCT file_id)` walked `execution_log→proposals` (status COMPLETED) | approved-proposal count |

Each source runs through a private `_safe_count` helper that mirrors `get_queue_activity`'s per-source failure isolation: on any exception it logs `stage_progress_degraded`, rolls back the session (so a Postgres aborted-transaction from one failed source can't poison later stages), and returns 0 — the function never raises into the 5s dashboard poll. The shared music/video filter (`MUSIC_VIDEO_TYPES`) mirrors `routers/pipeline.py:318-319`. `get_pipeline_stats` is left completely untouched.

### Task 2 — Discriminating tests (`tests/test_stage_progress.py`)
9 tests, all passing. The KEY discriminator (`test_analyzed_but_no_metadata_counts_independently`) seeds a file with an `analysis` row but NO `metadata` row and asserts `analyze.done == 1` AND `metadata.done == 0` — impossible to express through the single-valued linear state enum, proving output-table sourcing. Also covered: completed-only fingerprint counting, `scan_search.total is None`, proposals.total == convergence-set size, scrape/match distinct-tracklist walking, completed-execution-log counting, and single-source DB-error degradation (forced failure on the fingerprint source → `fingerprint.done == 0` while sibling stages stay correct, no raise).

## Verification

- `uv run mypy .` — clean (147 source files)
- `uv run ruff check .` — clean
- `uv run pytest tests/test_stage_progress.py` — 9 passed
- Combined pipeline-service coverage (`--cov=phaze.services.pipeline`) — **95.52%** (≥85%); `get_stage_progress` fully covered
- Broader `tests/test_services/ tests/test_routers/` — 922 passed; the 9 failed / 42 errored were entirely in two Redis-dependent files (`test_execution_dispatch.py`, `test_agent_tracklists.py`) that default to `PHAZE_REDIS_URL=redis://localhost:6379/0`. Re-running both with `PHAZE_REDIS_URL=redis://localhost:6380/0` (the ephemeral test Redis) → **18 passed**. Environmental (wrong Redis port), unrelated to this plan's changes.

## Deviations from Plan

None — plan executed exactly as written. Per-source failure isolation additionally performs a `session.rollback()` inside `_safe_count` (beyond the literal "degrade to 0") so that a real Postgres aborted-transaction state from one failed source does not cascade and zero every subsequent stage; this strengthens the failure isolation the plan mandated and is exercised by `test_single_source_db_error_degrades_to_zero`.

## Known Stubs

None.

## Threat Flags

None — read-only `COUNT(DISTINCT)` query code; no new endpoints, auth paths, or schema changes. Matches threat register T-35-07/08 dispositions (output tables already carry `file_id`/`tracklist_id` indexes; failure isolation degrades to 0).

## Self-Check: PASSED

- FOUND: src/phaze/services/pipeline.py (get_stage_progress present)
- FOUND: tests/test_stage_progress.py
- FOUND commit 3872a0b (feat: get_stage_progress)
- FOUND commit 38969a4 (test: stage-progress discriminators)
