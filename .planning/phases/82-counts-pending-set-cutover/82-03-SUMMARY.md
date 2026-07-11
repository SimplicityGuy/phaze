---
phase: 82-counts-pending-set-cutover
plan: 03
subsystem: dashboard-counts
tags: [read-cutover, four-bucket, stats-removal, degrade-safe, dag-counts]
requires:
  - "phaze.services.stage_status.stage_status_case (Phase 78 D-04 -- the LOCKED 4-way CASE ladder)"
  - "phaze.enums.stage.Status (not_started/in_flight/done/failed)"
  - "phaze.services.pipeline.get_stage_progress / _safe_count (the per-DAG-node reconcile source)"
provides:
  - "_safe_bucket_counts + four-bucket enrich nodes in get_stage_progress -- a VISIBLE failed count per enrich stage (READ-02)"
  - "get_pipeline_stats DELETED -- no FileRecord.state GROUP BY survives in the stats path (D-05)"
  - "routers/pipeline._derive_stats -- the seven former stats keys re-expressed off get_stage_progress"
  - "single-read stage_progress pass-through into _build_dag_context (no double heavy count on the 5s poll)"
affects:
  - "routers/pipeline.py three callers (_build_dag_context / build_dashboard_context / pipeline_stats_partial) + stats_bar.html -- all read derived counts"
  - "routers/shell.py get_stage_progress consumer -- unaffected (enrich nodes only GAINED keys; done+total preserved)"
tech-stack:
  added: []
  patterns:
    - "Four-bucket per stage = GROUP BY a MATERIALIZED status label (inner subquery) -- top-level GROUP BY on the correlated-exists CASE is a Postgres GroupingError"
    - "Derived stats dict preserves the seven key NAMES so the template needs zero functional churn (server-side source swap only)"
    - "Single get_stage_progress read threaded through both the derived stats dict and the DAG context (poll-path perf; T-82-04)"
key-files:
  created:
    - "tests/integration/test_stage_progress_buckets.py"
    - "tests/shared/routers/test_pipeline_stats.py"
  modified:
    - "src/phaze/services/pipeline.py"
    - "src/phaze/routers/pipeline.py"
    - "src/phaze/templates/pipeline/partials/stats_bar.html"
    - "tests/shared/services/test_pipeline.py"
decisions:
  - "Materialized-subquery GROUP BY: the RESEARCH Pattern-3 snippet's direct group_by(stage_status_case(stage)) does NOT execute on real Postgres (the CASE embeds correlated ~exists(... == files.id), so a top-level GROUP BY re-projects the ungrouped files.id). Fixed by evaluating the per-row status in an inner subquery and grouping the scalar label."
  - "Zero template churn: _derive_stats preserves the seven former key names (discovered/metadata_extracted/...), so stats_bar.html's six cards + three OOB store writes read the SAME stats.<key> -- only a documenting comment was added. The Alpine $store.pipeline.* keys stay stable (Pitfall 4)."
  - "Tasks 2 and 3 committed as ONE atomic cutover: a removed function and its callers cannot land in separate commits under the whole-repo `uv run mypy .` pre-commit hook (an intermediate commit would be import/type-broken)."
metrics:
  tasks_completed: 3
  files_created: 2
  files_modified: 4
  completed: 2026-07-10
requirements: [READ-02]
---

# Phase 82 Plan 03: Counts Four-Bucket + get_pipeline_stats Removal Summary

Extended `get_stage_progress`'s three enrich nodes (metadata / fingerprint / analyze) from `{done, total}`
to the four-bucket `{not_started, in_flight, done, failed, total}` via one degrade-safe
`GROUP BY stage_status_case(stage)` per stage, and REMOVED `get_pipeline_stats`'s linear
`GROUP BY FileRecord.state` entirely — re-expressing its seven consumed keys and its three router callers
+ the `stats_bar.html` partial off the derived `get_stage_progress` counts (READ-02, D-04, D-05). The DAG
now surfaces a VISIBLE failed count per enrich stage and the stats path no longer reads `FileRecord.state`.
Reconciled the two pre-existing test files that imported/exercised the removed function so the suite stays
green.

## What Was Built

- **Task 1 (`test`, RED-first):** two guard files —
  `tests/integration/test_stage_progress_buckets.py` (four-bucket-sums-to-total invariant on a healthy
  mixed corpus + visible-failed-count + in-flight-from-ledger + downstream-shape-unchanged; the real-PG
  `db_session`/`_file`/`*_test`-guard harness copied from `test_enrich_pending_independence.py`) and
  `tests/shared/routers/test_pipeline_stats.py` (the three callers derive the seven keys off
  `get_stage_progress`; `notYetEnriched == metadata.total - metadata.done`; the `/pipeline/stats` poll
  emits the three stable OOB store ids; `get_pipeline_stats` removed + no `FileRecord.state` GROUP BY).
  All the four-bucket-key + removed-function cells RED against pre-cutover source; collection green
  throughout.
- **Task 2 (`feat`):** added `_safe_bucket_counts(session, stage)` mirroring the `_safe_count` degrade
  discipline (zero-fill → GROUP BY the materialized status label → on any error log + guarded-rollback +
  all-zero, never 500s the poll), reusing the LOCKED `stage_status_case` (no fresh CASE, D-04). The three
  enrich nodes became `{**_safe_bucket_counts(...), "total": music_video_total}`; downstream nodes
  untouched. Deleted `get_pipeline_stats` in full. Reconciled `tests/shared/services/test_pipeline.py`:
  dropped the import + the three direct tests, repointed the SIX degrade canaries.
- **Task 3 (`feat`):** added `routers/pipeline._derive_stats` (the seven keys off `get_stage_progress`);
  changed `_build_dag_context` to take the already-computed `stage_progress` (single read) and compute
  `notYetEnriched = max(metadata.total - metadata.done, 0)`; both context builders now derive `stats`
  from ONE `get_stage_progress` read and thread it through. `stats_bar.html` gained a documenting comment
  only (key names preserved → zero functional churn). Audited the two pre-existing router tests (below).

## The load-bearing execution finding: RESEARCH Pattern-3 SQL does not execute on Postgres

The RESEARCH Pattern-3 snippet proposed `select(stage_status_case(stage), count).group_by(stage_status_case(stage))`.
On real Postgres this raises `GroupingError: subquery uses ungrouped column "files.id" from outer query` —
`stage_status_case` composes correlated `~exists(select(...).where(... == FileRecord.id))` subqueries, and a
TOP-LEVEL `GROUP BY` on that CASE re-projects the ungrouped `files.id`. Fix (Rule 1): evaluate the per-row
status label in an INNER subquery (where `files.id` is per-row in scope), then `GROUP BY` the materialized
scalar label in the outer aggregation:

```python
status_subq = select(stage_status_case(stage).label("status")).where(FileRecord.file_type.in_(MUSIC_VIDEO_TYPES)).subquery()
stmt = select(status_subq.c.status, func.count()).group_by(status_subq.c.status)
```

The four buckets still sum to `music_video_total` on a healthy query (every music/video file resolves to
exactly one `stage_status_case` bucket); the drift-lock to the Python resolver is intact (the CASE ladder
is reused verbatim, never re-authored).

## Semantic shift (this is the fix, not a regression)

Per-enrich-stage counts change meaning after this cutover — the numbers legitimately differ:

- **`metadata_extracted` (= `metadata.done`)** now counts every music/video file with a done `metadata`
  row (present AND `failed_at IS NULL`), NOT the transient linear `METADATA_EXTRACTED` state (a file left
  that state on advancing to FINGERPRINTED/ANALYZED). This is the cross-stage deadlock dissolving.
- **`analyze.done`** is now `stage_status_case`'s done bucket (`analysis_completed_at IS NOT NULL`), so a
  partial in-flight analysis row now correctly lands in the `in_flight` bucket, not `done` (the old
  `COUNT(distinct AnalysisResult.file_id)` counted partials as done).
- **`metadata.done` / `fingerprint.done`** now exclude failure-only rows (they surface in the visible
  `failed` bucket) and are music/video-scoped, where the old `done` counts were file-type-agnostic.
- `discovered` (= `discovery.done`, COUNT of ALL files) differs from the old state-`discovered` count
  (files in the transient `DISCOVERED` state) — the divergence the router tests assert on.

## get_pipeline_stats removal (READ-02 / D-05)

`get_pipeline_stats` (`services/pipeline.py`, the `select(FileRecord.state, count).group_by(FileRecord.state)`
counter) is DELETED. Its three callers — `_build_dag_context` (notYetEnriched only), `build_dashboard_context`,
`pipeline_stats_partial` — now derive their seven keys from `get_stage_progress` via `_derive_stats`.
`PIPELINE_STAGES` is RETAINED (still consumed by the ANALYSIS_FAILED-bucket invariant test + the
`get_analysis_failed_count` docstring; it is not on the hot poll path). A `NOTE` block records the removal
at the former definition site.

## The six repointed degrade canaries

The six `*_degrade_does_not_poison_session` tests each ran `follow_up = await get_pipeline_stats(session)` /
`assert follow_up["discovered"] == 0` to prove the outer transaction survives the SAVEPOINT/rollback degrade.
All six were repointed to `follow_up = await get_stage_progress(session)` / `assert follow_up["discovery"]["done"] == 0`
(sites at test_pipeline.py :397/:490/:582/:712/:752/:1113). The probe's only purpose is proving the
transaction is not poisoned after the degrade; any successful derived read satisfies it. All six still
exercise the SAVEPOINT recovery and pass.

## Router-test audit (Task 3)

- `test_dashboard_context_binds_lanes` — asserts the `lanes` snapshot + `cloud_lane_kind`/`cloud_target`
  absence; NO stats-shape assumption. **GREEN unchanged** against the derived stats shape.
- `test_pipeline_stats_partial` — asserts 200 + rendered "Discovered"/"Analyzed" text; NO removed-dict
  assumption. **GREEN unchanged.**
- The full `tests/shared/routers/test_pipeline.py` (72 tests) + `tests/shared/core/test_pipeline_dag_context.py`
  ran GREEN against the derived shape. No test held an old `get_pipeline_stats`-dict assumption, so none
  needed updating.

## Verification

Run on a DEDICATED private `phaze_stats82_test` DB (the shared `:5433` `phaze_test` carried a leaked
committed `legacy-application-server` row from a concurrent session, tripping the conftest `async_engine`
fixture's unconditional agent seed — the documented `pk_agents` shared-DB race; the pre-existing
`test_pipeline.py::test_pipeline_stats_partial` errors identically on the shared DB right now). All exports
per the test-environment contract.

- `uv run pytest tests/integration/test_stage_progress_buckets.py tests/shared/routers/test_pipeline_stats.py tests/shared/services/test_pipeline.py -q` → **83 passed**.
- `uv run pytest tests/shared/core/test_pipeline_dag_context.py tests/shared/routers/test_pipeline.py tests/integration/test_stage_status_equivalence.py -q` → **176 passed** (DAG-context + full router suite + the DERIV-04 drift-lock — no regression).
- `uv run pytest tests/ -k "shell or dashboard or analyze_workspace or stats_bar" -q` → **64 passed** (the `shell.py` get_stage_progress consumer + Analyze/Identify workspaces render unbroken).
- `uv run mypy .` → **Success: no issues found in 207 source files**; `uv run ruff check` clean; pre-commit (incl. mypy) passed on every commit — no `--no-verify`.
- **Mutation check (guards have teeth, per project memory):** re-injecting a `get_pipeline_stats` def
  with a `group_by(FileRecord.state)` body turned `test_get_pipeline_stats_is_removed_no_filestate_group_by`
  RED; restoring → GREEN. `_safe_bucket_counts` uses only `stage_status_case(` (0 fresh `case(` in body).
- `grep -rn "group_by(FileRecord.state)" src/phaze/` → **NONE**; no executable `get_pipeline_stats`
  def/call/import survives (only historical doc-comment mentions).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 — Bug] RESEARCH Pattern-3 GROUP BY does not execute on Postgres**
- **Found during:** Task 2 (first four-bucket test run)
- **Issue:** `group_by(stage_status_case(stage))` raised `asyncpg GroupingError: subquery uses ungrouped
  column "files.id"` — the CASE embeds correlated `~exists(... == files.id)` subqueries, so a top-level
  GROUP BY on it re-projects the ungrouped `files.id`.
- **Fix:** materialize the per-row status label in an inner subquery, GROUP BY the scalar label in the
  outer query. Buckets still sum to total; the `stage_status_case` reuse (D-04) is unchanged.
- **Files modified:** src/phaze/services/pipeline.py
- **Commit:** 89b68f1b

**2. [Rule 3 — Test authoring] test guard tripped by its own documenting prose**
- **Found during:** Task 3 verification
- **Issue:** the removed-function guard used a blanket `"get_pipeline_stats" not in inspect.getsource(router_mod)`
  substring check, which false-positived on the `_derive_stats` docstring that names the removed function
  for context; and the `group_by(FileRecord.state)` substring caught the service `NOTE` comment.
- **Fix:** the router guard now asserts `not hasattr(router_mod, "get_pipeline_stats")` (the name is not
  IMPORTED/bound — the meaningful contract); the service group-by guard strips `#`-comment lines before
  the substring check (live SQL only). Reworded the service `NOTE` to avoid the literal call form.
- **Files modified:** tests/shared/routers/test_pipeline_stats.py, src/phaze/services/pipeline.py
- **Commit:** 89b68f1b

### Structural note (not a code deviation)

**Tasks 2 and 3 were committed as ONE atomic `feat` commit (89b68f1b).** A removed module function and its
callers cannot land in separate commits under the project's whole-repo `uv run mypy .` pre-commit hook —
any intermediate commit would be import/type-broken, and `--no-verify` is forbidden (project CLAUDE.md).
The commit message documents the merge. Task 1 (RED tests) remains its own commit (5231c3f5).

## Notes

- **Shadow-compare gate (D-00e):** definitionally green — this plan changes only READERS (count derivation)
  and no writer / hard invariant. Not executed here (the standing gate probes the live prod DB, off-limits
  from the executor), consistent with 82-02.
- **Poll-path DoS (T-82-04):** the four `_safe_bucket_counts` reads + the single threaded `get_stage_progress`
  each ride the `_safe_count` degrade discipline (log → guarded rollback → all-zero) and never 500 the 5s
  `/pipeline/stats` poll; the stage_progress read now happens ONCE per request (threaded into
  `_build_dag_context`), where the old code did two heavy reads (get_pipeline_stats + get_stage_progress).
- **Private test DB:** `phaze_stats82_test` was created for isolated verification and dropped at teardown;
  the shared `phaze_test` DB was never mutated (its leaked agent row is a concurrent-session artifact, not
  mine). The orchestrator's post-wave `just test-bucket <pipeline/integration>` runs on a clean per-bucket DB.

## Threat Flags

None — no new network endpoint, auth path, file-access pattern, or schema change (pure reader/count cutover;
the four-bucket GROUP BY reuses the LOCKED `stage_status_case` `ColumnElement`, no f-string SQL, mitigating
T-82-05).

## Self-Check: PASSED

- FOUND: tests/integration/test_stage_progress_buckets.py
- FOUND: tests/shared/routers/test_pipeline_stats.py
- FOUND: .planning/phases/82-counts-pending-set-cutover/82-03-SUMMARY.md
- FOUND: `_safe_bucket_counts` in src/phaze/services/pipeline.py; `get_pipeline_stats` REMOVED
- FOUND commit 5231c3f5 (test: RED guards), 89b68f1b (feat: four-bucket + removal cutover)
