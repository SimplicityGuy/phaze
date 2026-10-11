# Testing risk inventory and bounded hunt slices

Assessment: `phaze-ww79u`, within `phaze-3l0q6`. Inspected 2026-10-11 UTC.
**Deepest testing belongs first at mutation/replay boundaries, then analysis and worker
ownership, then companion/provider persistence.** Low-line modules also have concrete backfill
opportunities, but their percentages alone do not outrank irreversible file operations.
This is an assessment, not a defect verdict or implementation authorization.

## Evidence and ranking method

The [coverage baseline](coverage-baseline.md) measures immutable source
`d1835b022f99397ed6be69359c57938a9ceaffd9`: 27,482/27,793 lines, **98.88101320476379%**;
6,273/6,530 diagnostic branches; 311 missing lines, 257 missing branches, 917 excluded lines.
All 384 tracked production Python files are measured. This task starts from `4f9c035c`, which
adds only the two assessment reports; source/test/CI bytes are unchanged.

Rank is a transparent ordinal judgment: (1) irreversible side effects or authority loss,
(2) observed recurrent fixes/churn and structural reach, (3) uncovered failure paths or
independent real-consumer evidence gaps, (4) complexity and likely repeated validation cost.
These factors are not converted into a defect probability. Existing strong tests reduce the
scope of each proposed hunt rather than being ignored because a module looks risky.

Repowise was initialized only in this worktree, in fast/no-prose mode, without editor setup,
hooks, generated agent files, seeding, or shared-index mutation. Its `risk` and `context`
CLI equivalents provide structural reach and symbol context; its in-process production
`health` report provides the table's max CCN, nesting and score. Health scores here have no
ingested coverage contribution: authentic coverage is joined separately. No dead-code or
refactoring recommendation was executed. Structural dependents are graph reach, not runtime
breakage proof; fast indexing limits history/co-change evidence.

Commit windows are fixed at **2026-10-11 00:00:00 UTC**, with 7/30/90-day inclusive lower
bounds, querying `git log d1835b02 --format=%ct%x09%s -- <path>`. Fix counts mean subjects
starting with `fix`, not independently verified defects. They differ intentionally from
Repowise's six-month defect episodes. Max CCN is a per-function maximum, not whole-file
complexity. Named mapped test IDs count distinct nonempty CI binary arc contexts after
stripping the `|phase` suffix; this includes setup/teardown contexts and does not mean each
test asserts the target. Empty import/global contexts are excluded.

| Target under `src/phaze/` | Lines covered/total; missing | Branch covered/total; missing | Commits/fixes 7d;30d;90d | Max CCN; nesting; health | Dependents; mapped test IDs |
|---|---|---|---|---|---|
| `tasks/execution.py` | 277/278; 1 (99.64%) | 38/38; 0 | 3/3; 5/3; 31/23 | 17; 5; 2.0 | 14; 141 |
| `tasks/execution_filesystem.py` | 171/172; 1 (99.42%) | 29/30; 1 | 3/3; 7/6; 7/6 | 5; 3; 7.0 | 5; 160 |
| `tasks/tag_write.py` | 77/80; 3 (96.25%) | 14/14; 0 | 2/2; 4/3; 7/6 | 9; 3; 7.01 | 5; 43 |
| `services/analysis.py` | 334/334; 0 (100.00%) | 74/74; 0 | 0/0; 6/2; 35/12 | 7; 3; 4.3 | 23; 200 |
| `tasks/controller.py` | 159/159; 0 (100.00%) | 31/32; 1 | 5/1; 19/5; 40/14 | 9; 3; 5.71 | 16; 36 |
| `tasks/agent_worker.py` | 201/206; 5 (97.57%) | 43/46; 3 | 3/0; 21/8; 36/16 | 8; 3; 6.65 | 13; 46 |
| `job_runner.py` | 291/294; 3 (98.98%) | 52/58; 6 | 0/0; 7/2; 26/9 | 9; 3; 5.0 | 10; 86 |
| `services/cloud_staging.py` | 116/117; 1 (99.15%) | 24/26; 2 | 0/0; 1/0; 18/12 | 5; 3; 8.4 | 15; 88 |
| `services/provider_persistence.py` | 212/217; 5 (97.70%) | 95/100; 5 | 4/0; 4/0; 4/0 | 24; 3; 4.74 | 12; 146 |
| `services/companion_capture.py` | 64/68; 4 (94.12%) | 26/30; 4 | 3/0; 3/0; 3/0 | 15; 2; 8.5 | 11; 125 |
| `services/source_workspace.py` | 64/70; 6 (91.43%) | 18/18; 0 | 1/0; 1/0; 1/0 | 18; 4; 7.26 | 2; 4 |
| `routers/duplicates.py` | 117/128; 11 (91.41%) | 18/24; 6 | 1/0; 1/0; 19/11 | 6; 3; 9.0 | 2; 78 |
| `runtime_config_notify.py` | 56/61; 5 (91.80%) | 7/10; 3 | 0/0; 2/1; 2/1 | 3; 2; 9.7 | 4; 53 |
| `runtime_config_catalog.py` | 23/25; 2 (92.00%) | 12/14; 2 | 0/0; 1/0; 1/0 | 7; 2; 10.0 | 2; 26 |
| `runtime_config_backends.py` | 52/55; 3 (94.55%) | 6/6; 0 | 0/0; 1/0; 1/0 | 5; 2; 9.85 | 3; 37 |
| `services/pipeline/analyze.py` | 94/100; 6 (94.00%) | 16/16; 0 | 1/1; 4/2; 5/2 | 6; 2; 8.71 | 5; 222 |
| `services/backends/lane_snapshot.py` | 141/149; 8 (94.63%) | 33/34; 1 | 0/0; 4/0; 7/0 | 12; 4; 5.89 | 5; 177 |
| `routers/cue.py` | 155/167; 12 (92.81%) | 33/40; 7 | 3/1; 4/1; 24/13 | 8; 2; 8.4 | 3; 37 |
| `services/selected_cue.py` | 53/57; 4 (92.98%) | 12/14; 2 | 1/0; 1/0; 1/0 | 8; 2; 10.0 | 6; 19 |
| `services/selected_discogs.py` | 40/43; 3 (93.02%) | 9/12; 3 | 1/0; 1/0; 1/0 | 9; 1; 8.22 | 6; 26 |

## Ranked targets, actual tests and gap classes

1. **Execution, tag mutation and durable replay.** Focus on `tasks/execution.py`,
   `tasks/execution_filesystem.py`, `tasks/tag_write.py`, `services/tag_write_disk.py`,
   `services/execution_preflight.py` and queue recovery. Despite 99.64% execution lines and
   all 38 branches covered, execution has 23 fix-subject commits in 90 days and 14 dependents.
   `tests/review/tasks/test_execution_commit_marker_durability.py` already checks actual
   fsync ordering and real SIGKILL before/after the commit marker. Its explicit limitation
   is that SIGKILL retains page cache: it does not simulate physical power failure.
   `test_execution_retry_contract.py`, `test_execution_committed_reporting.py`,
   `test_execution_recovery_contract.py`, `tests/integration/test_tag_write_sha256_seam.py`
   and `tests/review/services/test_tag_write_real_containers.py` provide existing recovery
   and real-container assertions. **Executed, assertion-strength unmeasured:** vary independent
   authority/identity/intent operands, commit-success/report-failure, destination collision
   plus replay, and tag-write-success/callback-loss. Do not describe those broad behaviors as
   missing. Prove any proposed gap with a surviving targeted negative control or a synthetic
   consumer failure. **Unmeasured:** power-loss durability and production mount behavior.

2. **Analysis, controller, worker and cloud orchestration.** `services/analysis.py` is
   100% lines/branches, yet it has 35 commits and 12 fix-subject commits in 90 days and
   23 dependents. `tests/analyze/services/pipeline/test_analysis_output_equivalence.py`
   runs real Essentia fine-tier decoding/BPM/key with an deliberately empty model directory;
   pure reductions over recorded coarse inputs are not real classifier evidence.
   **Genuinely missing real-consumer proof:** the coarse TensorFlow graph sweep, already
   canonical in `phaze-28883`. Existing `test_analysis_long_file.py`,
   `test_analysis_streaming_decode.py`, `test_analysis_degenerate.py`,
   `tests/analyze/recovery_cloud/orphan_replay/test_analysis_progress.py`,
   `test_recovery_replay_safety.py`, and `tests/integration/test_analysis_attempt_concurrency.py`
   prove meaningful boundaries. **Mock-only for remote provider behavior:** cloud staging
   unit tests pin timeouts, deterministic keys, TTL and enqueue failure with substituted
   provider/API behavior; they do not prove a live remote platform. Hunt exhaustive natural
   window accounting, chunk/RSS boundaries, teardown after cancellation/no progress,
   controller startup failures, stale attempt callbacks and replay of expiring material.
   A new surviving mutant/consumer failure is required before calling existing assertions weak.

3. **Companion/provider persistence and selected source authority.** New source growth
   deserves attention even where line coverage is high: provider persistence is 97.70%,
   max CCN 24, 12 dependents. Existing real Postgres
   `tests/integration/test_provider_persistence.py` covers forced identity collisions,
   revision conflicts, concurrent imports, deletion races and retained selection audit.
   `tests/integration/test_companion_capture_broker_seam.py` crosses a real broker and
   authenticated storage and tests bucket/inventory lock ordering.
   `tests/discovery/routers/test_agent_companion_capture.py` rejects cross-agent, anonymous,
   unlinked and malformed reports. **Genuinely unexecuted in this baseline:** capture lines
   44, 60, 128, 133 reject owner change before dispatch, bounded request-key violations,
   unavailable/ambiguous companions and matching-fingerprint junk. These are focused guard
   cases, not a claim that capture lacks authentication. Source-workspace lines 77–79 and
   143–145 are unexecuted query-error degrade paths. Verify rollback/last-good rendering and
   no review-authority minting from stale availability, selected revisions or changed inventory.
   Capture worker tests already exercise real subprocess cancellation, symlink escape,
   ancestor swap, byte/character caps and original text preservation.

4. **Duplicate resolution, config notify and reload.** The lowest-line percentages include
   genuine low-cost exception/guard cases and cosmetic helpers; prioritize destructive
   consumer paths. Duplicates lines 187/195 are resolve no-op paths and 306–308 are invalid
   bulk-plan rollback/rendering; lines 50–55 only format file sizes.
   `tests/review/services/test_dedup_review_guards.py` already refuses empty, repeated,
   partial, foreign and replayed plans; `tests/integration/test_dedup_resolve_undo_shadow.py`
   checks stale replay and CAS. **Genuinely unexecuted:** the named router failure paths;
   **assertion-strength unmeasured:** HTTP response plus unchanged filesystem/audit state,
   not just a service return value. Notify lines 99, 182–184, 188 cover unchanged native DSN,
   cancellation and defensive poll fault logging. Existing
   `tests/shared/config/test_runtime_config_notify.py` uses real NOTIFY and fallback polling;
   `test_runtime_config.py` already independently checks precedence and last-good rejection.
   Test listener/poll cleanup and preserved next iteration without long wall-clock sleeps;
   do not replace actual NOTIFY with a mock. Catalog/backends guards are follow-on candidates,
   not permission to change config semantics.

5. **Pipeline projection and backend status.** Pipeline analyze's missing lines 342–344
   and 384–386 are exception/degraded responses despite 100% branch arcs; line and branch
   gaps describe different things. Lane snapshot has 8 missing lines and max nesting 4.
   `tests/shared/services/test_lane_snapshot.py` already checks poisoned probes, isolated
   sibling lanes, failed rollback, real fanout and degrade-to-empty. Read and extend those
   specific assertions before adding parallel tests. **Genuinely unexecuted:** the named
   pipeline exception paths; **unmeasured combinations:** stale pagination and overlapping
   active/completed status versus backend liveness. Prove no lost status or false online
   claim through real route/template consumers. Selected cue/Discogs paths (92.98%/93.02%)
   and cue router (92.81%, 13 fix-subject commits/90d) belong with source selection and
   actual native forms, not isolated percentage padding.

6. **Browser, migrations, deployment and telemetry seams.** These are separate coverage
   populations, not zero-covered Python modules. Real browser CI ran 242 tests with 2 skips;
   default local collection deselects 244 browser tests. Existing
   `tests/browser/test_companion_viewing.py`, `test_htmx_native_submit_guard.py`,
   `test_changes_facet_selection_wire.py`, `test_execute_dispatch.py` and
   `test_analyze_lane_detail.py` exercise browser contracts beyond static source assertions.
   `tests/integration/test_migrations/test_baseline_schema.py` and numbered migration tests
   exercise real Alembic-to-ORM boundaries; SQL/migration text is outside `source=[phaze]`.
   A coverage percentage cannot certify every legacy-data upgrade or packaged-image import.
   Enumerate image/model availability, queue payload decoding, native form submission,
   dashboard metric names and Tailwind served CSS as **unmeasured at the complete consumer
   seam unless a corresponding existing test/run is identified**. Static template/schema
   lint is useful but cannot establish real rendering, CSS usability or Prometheus evaluation.
   This report does not independently interpret CI's 15 non-browser skip reasons.

## Excluded and absent scope audit

Zero production Python files are absent. The complete 917 excluded-line inventory was
classified from the immutable JSON and source AST: **714 type-checking-only, 111 protocol
interface, 53 explicit no-cover, 39 other default exclusions**. Classification is ordered
as written; it is an audit aid, not a proof that all exclusions are justified. The last
category includes ellipsis/abstract method tails, one module docstring and a decoder
`NotImplementedError`; not all are executable production paths.

The largest excluded block is `tasks/execution_filesystem.py` (44 lines), largely type imports
and protocol declarations; `services/tag_writer.py` has 13 excluded interface/type lines.
Do not delete those exclusions to inflate the denominator. The explicit no-cover category
includes process entry points (`analysis_child.py:223–224`), defensive runner branches
(`job_runner.py:481–483`, `865–866`), subprocess error paths
(`services/analysis_exec.py:325–328`), and stage fallbacks (`services/stage_status.py:213`,
`272`). Retain these as **excluded** rather than calling them measured; review their comments
and real subprocess/route consumer evidence in the relevant hunt. No blanket exclusion
removal or new exclusions are recommended here.

The Python line denominator omits tests and does not measure scripts outside the package,
shell harness, Jinja, JavaScript, Docker dependencies or remote model artifacts. Preserve
separate inventories and acceptance for those populations. Overall >95% lines is satisfied;
preserve the authentic 98.88101320476379% baseline rather than silently accepting a fall
to 95%. Diagnostic branches and exact covered/missing branch sets remain explicit evidence
for later matched experiments; this task changes no branch floor or branch-check policy.

## Canonical backlog and hunt boundaries

The full historical corpus contains 3,860 issues; closed historic defects are dedup evidence,
not current bugs. Reuse `phaze-5f1sv` for fused-condition policy, `phaze-28883` for real coarse
models, `phaze-5jo4c` for second-pass browser/migration/packaged consumer seams,
`phaze-pms85` for branch ratio-versus-count policy, `phaze-4ywkm` for permanent companion
dedup semantics, and `phaze-a3c6h` for asyncpg drop/reimport tracking. This inventory files no
second canonical bug, changes no product decision and performs no live-data population query.
New hunt candidates must be refuted, reproduced synthetically and fingerprint/body-deduped
against that historical corpus before filing. Similar titles alone are not conclusive dedup.

Each round preserves its filed three independent full-slice lenses: mutation uses
**concurrency/transactions, data-integrity/persistence and security/authorization**; analysis
uses **logic/numerical correctness, resource/error handling and concurrency/state-transition**;
companion uses **data-integrity/persistence, security/authorization and frontend/consumer-contract**.
Logic/authority, concurrency/replay and resource/lifecycle are useful cross-cutting themes,
not replacements for those named finder passes. All three lenses inspect the complete bounded
slice, rather than disjoint partitions. Different-model verification and measured yield/checkpoint
decisions remain the hunt skill's responsibility. Existing regressions are the starting controls.

| Round / canonical bead | Exact root-relative source slice | Special focus |
|---|---|---|
| Mutation, `phaze-d9ifa` | `src/phaze/tasks/{execution,execution_filesystem,tag_write,recovery_replay,_saq_reap}.py`; `src/phaze/services/{tag_writer,tag_write_disk,execution_preflight,execution_dispatch,execution_dispatch_protocol,review_tagwrite,enqueue_router}.py`; `src/phaze/routers/{tags,agent_tag_writes,agent_execution}.py`; `src/phaze/tasks/_shared/{replay_safety,deterministic_key,queue_factory}.py` | Independent authority/intent guards; publish/fsync/marker/unlink; lost callback after successful mutation; queue redrive and duplicate recovery |
| Analysis, `phaze-troov` | `src/phaze/services/{analysis,analysis_decoder,analysis_exec,analysis_attempt,analysis_windows,analysis_sizing,analysis_timeline,cloud_staging}.py`; `src/phaze/{analysis_child,job_runner}.py`; `src/phaze/tasks/{controller,agent_worker,recovery_replay,submit_cloud_job,reap_orphaned_backend_cloud_jobs,reconcile_cloud_jobs,functions,heartbeat}.py` | Exhaustive chunk windows; actual progress/teardown; stale attempt ownership; expiring cloud material; numerical independent oracles, with real coarse models separately gated |
| Companion, `phaze-6mipc` | `src/phaze/services/{provider_persistence,companion_capture,companion_capture_worker,companion_import_backfill,companion_content,companion_availability,source_workspace,selected_cue,selected_discogs,selected_tag_sources,selected_source_consumers,local_source_import,companion_features}.py`; `src/phaze/routers/{agent_companion_capture,companion,cue}.py`; `src/phaze/tasks/{companion_import,companion_features,companion_capture}.py`; `alembic/versions/{085_provider_source_observations,086_local_source_decisions,087_companion_import_progress,088_discogs_provider_track_binding}.py`; `src/phaze/templates/record/companions/`; `src/phaze/templates/pipeline/partials/{_selected_cue_preview,_recording_discogs,_local_source_sets}.html` | Immutable attribution and revisions; concurrent import/select/delete; content bounds; ownership before dispatch/callback; real browser/native-form selection authority |

Brace notation expands only the named files. Imports may be read for context, but broader
findings require explicit scope accounting. Mutation's operator checkpoint `phaze-bexe7`
precedes analysis; analysis checkpoint `phaze-7h65l` precedes companion. Kickoff approval is
not approval to bypass either checkpoint, run another round, access production, or implement fixes.

## Reproducibility and limits

Task-owned untracked artifacts live at `<scratch>/assessment-artifacts/phaze-ww79u/`:
the 384-file exact coverage/churn join, excluded-line inventory, Repowise health/risk/context
payloads, task-local index log, and metric-generation/audit scripts. Coverage binary/JSON
remain in the baseline bead's retained artifact directory. No test or application code changed;
no full suite or local coverage measurement was repeated for this inventory. The required
docs validation is recorded in the bead handoff. Risk rankings are reviewable hypotheses;
assertion weakness, production compatibility and mutation detection are not established until
the downstream synthetic hunt/consumer experiments provide those results.
