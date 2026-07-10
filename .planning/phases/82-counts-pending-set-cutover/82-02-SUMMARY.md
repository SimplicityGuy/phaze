---
phase: 82-counts-pending-set-cutover
plan: 02
subsystem: enrich-pending-sets
tags: [read-cutover, eligibility, drift-lock, double-dispatch, enrich-stages]
requires:
  - "phaze.services.stage_status.eligible_clause / dedup_resolved_clause (Plan 82-01 + Phase 84)"
  - "phaze.enums.stage.Stage.{METADATA,FINGERPRINT,ANALYZE}"
provides:
  - "The three enrich pending helpers (metadata/fingerprint/analyze) derived from eligible_clause -- independent, no FileRecord.state read (READ-01)"
  - "T-82-A1 double-dispatch guard: _ACTIVE_CLOUD_STATUSES exclusion on the analyze set"
  - "Mutation-tested AST source scan + behavioral divergence guard over the three helpers"
affects:
  - "routers/pipeline.py trigger_metadata_extraction/trigger_fingerprint + shell.py workspaces (narrowed in lockstep via the shared helpers)"
  - "tasks/reenqueue.py recovery producer (same shared pending definition -- Phase-42 anti-drift)"
tech-stack:
  added: []
  patterns:
    - "Pending set = file_type scope ∧ eligible_clause(stage) ∧ ~dedup_resolved_clause() -- single derived WHERE"
    - "Explicit correlated ~exists(cloud_job active-status) conjunct at the query level for double-dispatch"
    - "AST source scan scoped to named function-def subtrees (whole-module scan would false-positive)"
    - "Idempotent FK-agent seed for shared-*_test-DB hermeticity"
key-files:
  created:
    - "tests/integration/test_enrich_pending_independence.py"
    - "tests/integration/test_pending_set_divergence.py"
    - "tests/shared/test_pending_set_source_scan.py"
  modified:
    - "src/phaze/services/pipeline.py"
    - "tests/shared/services/test_pipeline.py"
decisions:
  - "A1: added an EXPLICIT ~exists(cloud_job in _ACTIVE_CLOUD_STATUSES) conjunct -- the local process_file ledger row does NOT survive the cloud hand-off, so ~inflight_clause alone cannot exclude a cloud-dispatched file"
  - "The plan's cloud statuses 'pushing'/'pushed' are FileState members, not cloud_job statuses; used the REAL cloud_job lifecycle statuses (every non-FAILED member) that the ck_cloud_job_status_enum CHECK accepts"
  - "fingerprint set is also file_type-scoped (parity with metadata) -- a non-music file must never be fingerprinted"
metrics:
  tasks_completed: 3
  files_created: 3
  files_modified: 2
  completed: 2026-07-10
requirements: [READ-01]
---

# Phase 82 Plan 02: Enrich Pending-Set Cutover Summary

Cut the three enrich pending sets (`get_metadata_pending_files` / `get_fingerprint_pending_files` /
`get_discovered_files_with_duration`) over from `FileRecord.state` to the derived
`eligible_clause(stage) ∧ ~dedup_resolved_clause() ∧ file_type ∈ MUSIC_VIDEO_TYPES` layer, so metadata /
fingerprint / analyze each surface every not-done, not-in-flight, not-dedup-resolved file **independent
of the other two and of `state`** — dissolving the cross-stage deadlock (READ-01). Resolved the A1
double-dispatch landmine with an explicit active-`cloud_job` exclusion conjunct on the analyze set, and
locked the cutover with a mutation-tested AST source scan, a behavioral divergence guard, and the SC#1
all-orderings test. Reconciled the pre-existing `tests/shared/services/test_pipeline.py` (deleted three
stale state-gated pending tests; kept the two that stay valid under derived semantics).

## What Was Built

- **Task 1 (`test`, RED-first):** Three guard/behavior test files —
  `test_enrich_pending_independence.py` (SC#1 all-6-orderings independence + two deadlock cells + A1
  cloud-exclusion + cloud-failed-is-local-candidate + Pitfall-1 non-music + dedup exclusion),
  `test_pending_set_divergence.py` (derived-wins behavioral guard, five cells, per-cell `MUTATION:`
  comment), and `test_pending_set_source_scan.py` (AST scan scoped to the three helper bodies,
  positional/keyword/`**`-splat/docstring mutation cases). Copied the real-PG harness + AST helper
  battery verbatim from `test_dedup_divergence.py` / `test_dedup_fingerprint_source_scan.py`. RED cells
  recorded (see below); collection green throughout.
- **Task 2 (`feat`):** Cut over `get_discovered_files_with_duration` — kept the LEFT OUTER JOIN on
  `FileMetadata.duration`, replaced `.where(state == DISCOVERED)` with
  `file_type ∈ MUSIC_VIDEO_TYPES ∧ eligible_clause(ANALYZE) ∧ ~dedup_resolved_clause() ∧ ~exists(active cloud_job)`.
  Deleted the stale `test_get_discovered_files_with_duration_excludes_other_states`.
- **Task 3 (`feat`):** Cut over `get_metadata_pending_files` and `get_fingerprint_pending_files` to the
  same single-derived-`WHERE` shape; collapsed the fingerprint `get_files_by_state(METADATA_EXTRACTED)`
  UNION + failed-retry sub-select + manual de-dup-by-id loop. Deleted the two stale union/dedup unit
  tests; kept `test_get_metadata_pending_files_returns_only_music_video`. Made the two new integration
  fixtures idempotent against a pre-existing legacy agent.

## A1 Ledger-Survival Trace (Task 2 — the load-bearing finding)

**Finding: the local `process_file:<file_id>` scheduling-ledger row does NOT survive the Phase-83 cloud
hand-off, so an explicit cloud-exclusion conjunct WAS added** (the plan's default; survival was NOT
proven — it was disproven).

Trace of the analyze routing / ledger lifecycle:

1. `process_file:<id>` ledger rows are WRITTEN at the `before_enqueue` chokepoint
   (`tasks/_shared/deterministic_key.py`) and CLEARED at `after_process` on any terminal status.
2. `LocalBackend.dispatch` enqueues `process_file` (→ ledger row) and flips `state=LOCAL_ANALYZING` — the
   local path is correctly `~inflight`-excluded.
3. **The cloud path never enqueues `process_file` while held/pushing.** `hold_awaiting_cloud`
   (`services/backends.py`) sets `state=AWAITING_CLOUD` + `cloud_job.status='awaiting'` and writes **no
   ledger row**. `ComputeAgentBackend.dispatch` flips `state=PUSHING`, upserts
   `cloud_job.status='submitted'`, and enqueues **`push_file`** (not `process_file`).
4. Only at `report_pushed` (`routers/agent_push.py`) — after the rsync lands, `state→PUSHED`,
   `cloud_job.status: submitted→succeeded` — is `process_file` finally enqueued (on the compute agent's
   queue).

So during `AWAITING_CLOUD` (status `awaiting`) and `PUSHING` (status `submitted`) there is **no
`process_file` ledger row** → `~inflight_clause(ANALYZE)` is true → without an explicit guard the file
re-enters the local analyze set = the A1 double-dispatch / cost DoS (T-82-A1). Mitigation:
`~exists(select(CloudJob.id).where(file_id == FileRecord.id, status ∈ _ACTIVE_CLOUD_STATUSES))`.

`_ACTIVE_CLOUD_STATUSES` = every non-`FAILED` `CloudJobStatus` (`awaiting`, `uploading`, `uploaded`,
`submitted`, `running`, `succeeded`). `FAILED` is deliberately EXCLUDED (a terminally-failed cloud burst
with no `AnalysisResult` is a legitimate local-retry candidate). `succeeded` is INCLUDED as
belt-and-suspenders for the compute `PUSHED` window (`status='succeeded'` while analysis still runs on
the agent, before its `process_file` ledger row lands); a genuinely-done Kueue burst is already excluded
by `~done_clause` inside `eligible_clause`, so listing `succeeded` is harmless there.

## Semantic Shift (this is the fix, not a regression)

Per-stage pending counts JUMP after this cutover: a file whose `state` advanced past a stage's gate
(e.g. `ANALYZED`) but never actually completed a *sibling* stage (e.g. never fingerprinted) now
correctly re-enters that sibling's pending set. The old state gates hid these files (the cross-stage
deadlock); the derived layer surfaces them. Consumers (manual triggers, API, HTMX workspaces, the
Phase-42 recovery producer) all narrow in lockstep because they share the helpers — verified by grep
(`routers/pipeline.py`, `routers/shell.py`, `tasks/reenqueue.py`) and by the 56 passing
fingerprint-router + recovery tests.

## Mutation Checks (recorded per success criteria — the guards have teeth)

For each of the three helpers, reverting its `eligible_clause(...)` conjunct to a `FileRecord.state`
filter turned BOTH the AST source-scan (`test_pending_helpers_have_zero_filestate_reads`) RED (a
`FileState` read reintroduced) AND the matching divergence cell RED; restoring → GREEN:

| Helper | Mutation | Divergence cell inverted |
|--------|----------|--------------------------|
| metadata | `state == DISCOVERED` | `test_metadata_done_with_stale_discovered_state_is_excluded` |
| fingerprint | `state == METADATA_EXTRACTED` | `test_unfingerprinted_with_advanced_state_is_included` + `test_failed_only_fingerprint_is_included` |
| analyze | `state == DISCOVERED` | `test_unanalyzed_with_advanced_state_is_included` |

False-positive check: the guard stays GREEN with the retained `FileRecord.file_type.in_(...)` reader
(`test_guard_ignores_non_where_call_arg`).

## Pre-existing test_pipeline.py reconciliation (deleted vs kept)

- **DELETED** `test_get_discovered_files_with_duration_excludes_other_states` (asserted an ANALYZED-state
  file with no `AnalysisResult` is excluded — now legitimately included: `NOT_STARTED` → eligible).
- **DELETED** `test_get_fingerprint_pending_files_unions_metadata_extracted_and_failed_retry` and
  `test_get_fingerprint_pending_files_dedups_metadata_extracted_with_failed_result` (both encode the
  collapsed UNION / de-dup-loop semantics). Their derived successors are the independence + divergence
  cells in the new integration files.
- **KEPT untouched** `test_get_metadata_pending_files_returns_only_music_video`,
  `test_get_discovered_files_with_duration_joins_duration`,
  `test_get_discovered_files_with_duration_outerjoin_null` (all stay green under derived semantics).

## Verification

- `uv run pytest tests/integration/test_enrich_pending_independence.py tests/integration/test_pending_set_divergence.py tests/shared/test_pending_set_source_scan.py tests/shared/services/test_pipeline.py -q` → **106 passed** (real ephemeral PG `:5433`).
- `uv run pytest tests/fingerprint/routers/test_pipeline_fingerprint.py tests/analyze/tasks/test_recovery.py -q` → **56 passed** (helper consumers + recovery — no regression).
- `uv run mypy src/phaze/services/pipeline.py` → clean; `uv run ruff check` / `ruff format --check` → clean.
- Pre-commit (incl. mypy) passed on all commits — no `--no-verify`.

## RED-cell record (Task 1 TDD state, against pre-cutover source)

RED before Task 2/3: all 6 independence orderings, both deadlock cells, non-music, dedup, all 5
divergence cells, and the source-scan (2 offenders: `get_fingerprint_pending_files` FINGERPRINTED @1424,
`get_discovered_files_with_duration` DISCOVERED @1109). GREEN pre-cutover (regression guards): the A1
cloud-exclusion cells (excluded via pre-cutover state gate; stay green post-cutover via the cloud
conjunct), the cloud-failed cell, and the 6 source-scan mutation/negative cases.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 — Data-model correction] Cloud statuses `pushing`/`pushed` are FileState members, not cloud_job statuses**
- **Found during:** Task 1 / Task 2
- **Issue:** The plan's A1 cells and conjunct named `cloud_job.status ∈ ('awaiting','pushing','pushed')`.
  `pushing`/`pushed` are `FileState` members; the `ck_cloud_job_status_enum` CHECK only permits
  `uploading/uploaded/submitted/running/succeeded/failed/awaiting`, so seeding/querying those literals
  would violate the constraint.
- **Fix:** Mapped the plan's intent onto the REAL `cloud_job` lifecycle — `awaiting` (held) →
  `submitted` (= FileState PUSHING) → `succeeded` (= FileState PUSHED) plus the Kueue intermediates
  (`uploading`/`uploaded`/`running`). Excluded exactly `failed`. Seeded every active status in the A1
  cell and added a `failed`-is-local-candidate negative cell.
- **Files modified:** src/phaze/services/pipeline.py, tests/integration/test_enrich_pending_independence.py

**2. [Rule 3 — Harness robustness] Idempotent FK-agent seed in the two new integration fixtures**
- **Found during:** Task 3 (mutation check)
- **Issue:** The verbatim-copied fixture does `session.add(Agent('legacy-application-server'))` + flush;
  the shared `*_test` DB (concurrently used by sibling wave agents) already carried a committed row,
  raising `UniqueViolationError` at setup (the same environmental collision documented in 82-01 SUMMARY).
- **Fix:** Seed the agent only if `session.get(Agent, id) is None` — makes the fixtures hermetic against
  the shared DB without touching the analog files.
- **Files modified:** tests/integration/test_enrich_pending_independence.py, tests/integration/test_pending_set_divergence.py

## Notes

- **Shadow-compare gate (D-00e):** unaffected by construction — this plan changes only READERS (three
  pending helpers), no writer and no hard invariant (`AWAITING_CLOUD ⇒ cloud_job` etc. are untouched).
  Not executed here (the standing gate probes the live prod DB, off-limits from the executor); it is
  definitionally green since no writer/invariant changed.
- **D-02 deployment gate (carried to Plan 04 VERIFICATION):** the analyze pending-set flip is trusted in
  prod only once the target is at Alembic ≥036 AND
  `COUNT(files WHERE state='analyzed' AND analysis_completed_at IS NULL AND failed_at IS NULL) = 0`.
  This plan does not deploy (pure reader cutover, no DDL/migration).
- **Full `tests/shared/routers/test_pipeline.py` run flake (environmental, NOT a code issue):** running
  that whole file against the shared `:5433` DB produced 46 setup ERRORS — all `pk_agents`
  (`legacy-application-server already exists`) and `pg_type_typname_nsp_index` (concurrent
  `CREATE TABLE agents`), i.e. each test's `Base.metadata.create_all` + legacy-agent seed racing sibling
  wave agents on the shared DB (the documented CI-bucket-isolation / colima full-suite flake). Every
  such test PASSES in isolation (verified `test_force_local_analyze_api_routes_local_no_hold` → 1 passed;
  63 passed in the same run). My changes are pure reader cutovers touching no fixtures/writers. The
  orchestrator's post-wave `just test-bucket integration` runs on a clean per-bucket DB.

## Threat Flags

None — no new network endpoint, auth path, file-access pattern, or schema change was introduced (pure
reader cutover; the T-82-A1 `cloud_job` exclusion is a NARROWING conjunct, mitigating the registered
double-dispatch threat rather than adding surface).

## Self-Check: PASSED

- FOUND: tests/integration/test_enrich_pending_independence.py
- FOUND: tests/integration/test_pending_set_divergence.py
- FOUND: tests/shared/test_pending_set_source_scan.py
- FOUND: `eligible_clause(Stage.{ANALYZE,METADATA,FINGERPRINT})` + `_ACTIVE_CLOUD_STATUSES` in src/phaze/services/pipeline.py
- FOUND commit 6b165d33 (test: RED guards), f69dc81e (feat: analyze cutover + A1), c18fd617 (feat: metadata+fingerprint cutover)
