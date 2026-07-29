# Fingerprint removal inventory (phaze-0jpe.1)

Authoritative removal map for epic **phaze-0jpe — Remove audio fingerprinting entirely**. This
document names every file/symbol to remove or amend, grouped by the molecule issue that owns the
edit, and calls out what must survive because other stages depend on it. It does not remove any
production code itself (phaze-0jpe.1's acceptance forbids that) — it is the map the other five
issues drive from.

Method: `rg`-driven sweep for `fingerprint|audfprint|panako|fprint|maxtimebits|olaf|segment_match
|combined_query|EngineQueryError|AllEnginesFailedError|FingerprintEnqueueResult` across the whole
tree (222 files matched), plus targeted reads of the highest-risk modules (`stage_status.py`,
`enums/stage.py`, `enqueue_router.py`, `tasks/reenqueue.py`, `tasks/_shared/*`) to separate
fingerprint-owned code from shared dispatch machinery that merely has a `Stage.FINGERPRINT` (or
`fingerprint_file`/`scan_live_set`) branch inside it.

**Two additional grep terms the brief did not list, discovered during the sweep — add them to any
follow-up grep-level absence check:** `trackid` (the whole "Track ID" workspace is audfprint/panako
match-confidence UI with no other purpose) and `scan_live_set` (the SAQ task name itself doesn't
contain the substring "fingerprint" but is 100% fingerprint-engine-dependent).

---

## 0. Read this first — what must NOT be removed

This is the highest-risk part of the removal: several files the naive grep flags are **shared
dispatch machinery**, parameterized generically over `Stage` (or over a task-name registry), that
also serves `metadata` / `analyze` / `tracklist` / `propose` / `review` / `apply`. The fix in every
case below is **delete only the `FINGERPRINT` branch/entry**, never the function or the file.

| Shared mechanism | File | What to keep | What to remove |
|---|---|---|---|
| SQL predicate dispatch ladder | `src/phaze/services/stage_status.py` | `done_clause` / `failed_clause` / `skipped_clause` / `inflight_clause` / `domain_completed_clause` / `eligible_clause` / `orphaned_clause` / `resolved_ledger_clause` / `stage_status_case` / `stage_status_sort_case` / `saq_detail` — ALL of these, verbatim, for the other 6 stages | Only the `if stage is Stage.FINGERPRINT:` branch inside each; the `FingerprintResult` import; the `_DONE_FP` constant |
| DB-free resolver twin | `src/phaze/enums/stage.py` | `Stage` enum (5 remaining members), `Status`, `resolve_status`, `domain_completed`, `eligible`, the whole precedence-ladder machinery | `Stage.FINGERPRINT` member, `_fingerprint_status`, its `_DONE_FP`, its three dict entries in `ELIGIBILITY_DAG` / `FAILURE_IS_TERMINAL` / `ELIGIBLE_AFTER_FAILURE`, its branch in `resolve_status`/`eligible` |
| Equivalence lock between the two above | `tests/integration/test_stage_status_equivalence.py`, `tests/shared/test_stage_resolver.py` | The whole drift-guard test structure, for the remaining 5 stages | Fingerprint's test fixtures/cases only |
| Lane routing | `src/phaze/services/enqueue_router.py` | `LANE_TASKS["analyze"]`, `["meta"]` (minus `scan_live_set`), `["io"]`; `LANES`; `lane_for_task`; `AGENT_TASKS`; `CONTROLLER_TASKS` (untouched — no fingerprint task is a controller task) | `LANE_TASKS["fingerprint"]` entry entirely (collapses `LANES` from 4 to 3: `analyze`, `meta`, `io`); `scan_live_set` out of `LANE_TASKS["meta"]` |
| Per-agent lane queue plumbing | `src/phaze/services/agent_task_router.py` | The whole 3-lane (not 4) `queue_for` / lane-queue-list mechanism | Docstring's "four lane queues" / "analyze/fingerprint/meta/io" language; nothing else code-wise (it derives `LANES` from `enqueue_router`, so shrinks automatically once that's fixed) |
| Lane concurrency knobs | `src/phaze/config.py` | `lane_analyze_concurrency`, `lane_meta_concurrency`, `lane_io_concurrency` | `lane_fingerprint_concurrency` Field; `audfprint_url` / `panako_url` Fields + the `_enforce_localhost_only` validator's `audfprint`/`panako` entries in `allowed_hosts` |
| Per-lane queue-depth reporting | `src/phaze/services/backends.py` (`get_lane_queue_depths`, `resolve_lane_queue_agent`), `src/phaze/services/pipeline.py` (`get_queue_activity`-style readers) | The `{analyze, meta, io}` dict shape, iterating `LANES` | Nothing extra once `LANES` shrinks — these already iterate `LANES` generically. Just re-verify the `KUEUE_NO_SAQ_QUEUE_NOTE`/docstring comments that spell out "analyze 0 · fingerprint 0 · meta 0 · io 0" as an example string |
| Deterministic SAQ keying | `src/phaze/tasks/_shared/deterministic_key.py` | `_KEY_BUILDERS` dict + both hooks, for the other 7 entries | `"fingerprint_file"` and `"scan_live_set"` entries only |
| Pipeline dashboard counters | `src/phaze/services/pipeline_counters.py` | `PIPELINE_FUNCTIONS` tuple (shrinks 9 → 7), the Redis INCR mechanism | `"fingerprint_file"` and `"scan_live_set"` entries; update the docstring's "9 pipeline functions" |
| Stage pause/priority control | `src/phaze/tasks/_shared/stage_control.py` | `STAGE_TO_FUNCTION`, `apply_stage_control`, `enforce_stage_pause_on_process`, `repark_if_stage_paused` — the whole SENTINEL-park mechanism, for `metadata`+`analyze` | `STAGE_TO_FUNCTION["fingerprint"]` entry (collapses 3 stages → 2); every docstring phrase "the three agent stages" |
| Stage-pause DB table | `src/phaze/models/pipeline_stage_control.py` | The table, model, `priority_range` CHECK, for `metadata`+`analyze` rows | No CHECK enumerates `stage` values (only `priority BETWEEN 0 AND 100`), so this is a **data** change (delete the seeded `fingerprint` row), not a schema change — see §4. Update the class docstring's `(metadata|analyze|fingerprint)` |
| Force-skip marker | `src/phaze/models/stage_skip.py` | The table/model for `metadata`+`analyze` | The CHECK **does** enumerate stages: `CheckConstraint("stage IN ('metadata','analyze','fingerprint')", name="enrich_only")` — this **is** a schema change, §4 |
| Recovery / orphan-reconciliation | `src/phaze/tasks/reenqueue.py` | `recover_orphaned_work`, the whole ledger-scoped `orphaned = ledger MINUS live MINUS domain-completed` machinery, for `process_file` / `extract_file_metadata` / `push_file` (predicate-covered) and the 4 controller tracklist tasks (live-keys-only) | `fingerprint_file` out of `_DOMAIN_COMPLETED_STAGES`-equivalent dict and its replay branch; `scan_live_set` out of the live-keys-only set and its replay branch; the `fingerprint_done` id-set query (~lines 281–296); every docstring paragraph describing the `fingerprint_file`/`scan_live_set` predicate rules |
| Scan-batch cascade delete | `src/phaze/services/scan_deletion.py` | The whole per-batch cascade over `analysis`/`metadata`/`proposals`/`tracklists`/... | Only the `(FingerprintResult.__tablename__, delete(FingerprintResult)...)` tuple entry and the `FingerprintResult` import |
| Sortable-column contract | `src/phaze/routers/column_sort.py` | The whole `SortContract`/`SortableColumn` mechanism (used by 8+ other tables) | Nothing code-wise — the one `FingerprintResult.confidence` mention is an illustrative **docstring** example, not real wiring (the real `TRACKID_SORT` only sorts by filename). Update the example so it doesn't cite a type being deleted |
| Admin agent-activity stages tuple | `src/phaze/routers/admin_agents.py` | `_ACTIVITY_STAGES` for the other 5 stages | `Stage.FINGERPRINT` entry |

**Do not let a removal PR touch any of the "keep" columns above.** The equivalence test
(`test_stage_status_equivalence.py`) and the task-split boundary test
(`tests/shared/core/test_task_split.py`) are the automatic backstops if it does.

---

## 1. Explicit non-removals — grep hits that are NOT fingerprinting

Two false positives worth flagging by name so nobody "fixes" them by association:

1. **`AnalysisResult.fingerprint`** (`src/phaze/models/analysis.py:24`, wired through
   `_ANALYSIS_COLUMN_FIELDS` in `src/phaze/routers/agent_analysis.py:84`). This is a nullable
   `Text` column written by the **analyze** stage (essentia), not by audfprint/panako, and has no
   relationship to `FingerprintResult`/`Stage.FINGERPRINT` at all. **Keep.**
2. **`chromaprint` / `fpcalc` / `libchromaprint-tools`** in the main app `Dockerfile` and
   `Dockerfile.agent-arm64` (the base image, NOT `services/audfprint/Dockerfile.audfprint`).
   Confirmed by both the Dockerfile's own comment ("essentia-tensorflow's native `_essentia`
   extension... the decode/fingerprint toolchain needs ffmpeg + ffprobe... and fpcalc +
   libchromaprint.so.1. Without these, `import essentia` fails at runtime and every analysis job
   dead-letters") and `docs/essentia-analysis.md`'s own out-of-scope note ("audio fingerprinting is
   *not* essentia — it is handled entirely by the `audfprint` and `panako` HTTP sidecars"). This is
   a **runtime dependency of essentia-tensorflow itself**, wholly independent of the two engines
   being removed here. **Keep the apt packages in the main/agent Dockerfiles.** Only
   `services/audfprint/**` and `services/panako/**` (their own separate Dockerfiles, own
   dependencies) go away.

---

## 2. Critical structural findings needing an explicit decision in phaze-0jpe.2

These are not simple deletions — they are places where fingerprint removal changes what an
*unrelated* stage means, and the next bead needs to land a considered answer, not a reflexive grep-delete.

### 2.1 `Stage.TRACKLIST` depends on `Stage.FINGERPRINT` in the eligibility DAG

`src/phaze/enums/stage.py`:

```python
ELIGIBILITY_DAG: dict[Stage, tuple[Stage, ...]] = {
    ...
    Stage.TRACKLIST: (Stage.FINGERPRINT,),
    ...
}
```

`TRACKLIST` currently becomes eligible only once `FINGERPRINT` is DONE for a file. With
`Stage.FINGERPRINT` removed, this upstream conjunct has no target. **phaze-0jpe.2 must decide**
whether `TRACKLIST` becomes upstream-independent (empty tuple, like `METADATA`/`ANALYZE` today) or
gets a new upstream (e.g. discovery-only). Given the epic's scope note that 1001tracklists-sourced
tracklists (`phaze-fq9h`) are independent of audio matching and already have their own producers
(`search_tracklist` et al., which are `CONTROLLER_TASKS` with no ledger-eligibility gate today),
the empty-tuple answer looks consistent with current behavior for that path — but this needs a
deliberate choice recorded in phaze-0jpe.2's commit, not a silent default.

### 2.2 The `agent_tracklists` router/schema is the ENTIRE fingerprint-scan tracklist path

`src/phaze/routers/agent_tracklists.py` (306 lines, `POST /api/internal/agent/tracklists`) and
`src/phaze/schemas/agent_tracklists.py` exist **only** to let `scan_live_set` post a
fingerprint-matched tracklist back (`schemas/agent_tracklists.py:45`: `source:
Literal["fingerprint"]  # D-27 -- only fingerprint-sourced tracklists for now`). This is a
**whole-file removal**, distinct from `src/phaze/routers/tracklists.py` (the 1001tracklists
controller-facing router, which has **zero** fingerprint references and is untouched — confirms the
epic's phaze-fq9h scope boundary is real and clean in code, not just in the epic description).

### 2.3 `src/phaze/tasks/scan.py` needs a **surgical**, not whole-file, removal

This file defines two unrelated SAQ tasks: `scan_live_set` (100% fingerprint — calls
`orchestrator.combined_query`, raises/handles `FingerprintQueryUnavailableError`, posts
`source="fingerprint"` tracklists) and `scan_directory` (directory-walk file discovery, entirely
unrelated). **Remove only `scan_live_set` and its supporting imports**
(`FingerprintQueryUnavailableError`, the `FingerprintOrchestrator` TYPE_CHECKING import); keep
`scan_directory`, `_classify`, `_count_ingestible`, `_resolve_chunk_size` untouched. Same surgical
rule applies to its test file, `tests/discovery/tasks/test_scan.py` (11 fingerprint-ish hits mixed
with scan_directory coverage — do not delete the file, delete the `scan_live_set` test class/cases).

### 2.4 The "Track ID" workspace is fingerprint-only but doesn't say "fingerprint" everywhere

`src/phaze/services/pipeline.py`'s `_trackid_engine_badge`, `_trackid_linked_conf_subq`,
`_trackid_files_select`, `_trackid_page_stmt`, `get_trackid_files_page` (reads
`audfprint_status`/`panako_status` per file) plus `TRACKID_SORT` and the `/pipeline/trackid-files`
route, `src/phaze/templates/pipeline/partials/trackid_workspace.html` and
`_trackid_files.html`, and the `"trackid"` entry in `column_sort.py`'s docstring-cited workspace
list — this entire "Track ID" surface exists to show per-file audfprint/panako match status.
**Whole-file/whole-function removal**, but grep for the literal string `fingerprint` alone will
**miss** most of it — search for `trackid` too.

### 2.5 Lane / stage-control collapse changes shipped constants, not just fingerprint-owned ones

Three "N of M" constants shrink as a direct consequence and every doc/comment citing the old N must
move together (grep will find these as false-negatives if only searching for "fingerprint"):

- `LANES`: 4 (`analyze`/`fingerprint`/`meta`/`io`) → 3 (`analyze`/`meta`/`io`).
- `STAGE_TO_FUNCTION` / the "three agent pipeline stages": 3 (`metadata`/`analyze`/`fingerprint`) → 2 (`metadata`/`analyze`).
- `PIPELINE_FUNCTIONS`: 9 → 7 (drop `fingerprint_file`, `scan_live_set`).
- The Files matrix "six-pill stage matrix" (`stage_status_sort_case`, `_stage_matrix.html`,
  `files_table_view.html`, `record_body.html`): 6 pills → 5 (`Meta`/`Analyze`/`Prop`/`Appr`/`Exec`).
- `_ACTIVITY_STAGES` in `admin_agents.py`: 6 stages → 5.
- `docker-compose.agent.yml`'s "FOUR lane workers": 4 → 3 (remove `worker-fingerprint` service
  entirely, not just rename).

### 2.6 Existing `Tracklist` rows with `source='fingerprint'` — a decision for bead .4/.6, not code

`Tracklist.source` has no DB CHECK constraint (free `String(30)`), so no migration is *required* to
keep the schema consistent. But once `scan_live_set` is gone, no code path ever creates
`source='fingerprint'` rows again, and `tasks/tracklist.py::refresh_tracklists` already carries a
`phaze-p1vy` guard explicitly excluding them (they are "structurally un-rescrapeable" — empty
`source_url`). Given both engines were confirmed silently dead in production for weeks
(phaze-p3hj/phaze-iq65), there are likely few or zero real rows, but **bead .4 or .6 should
explicitly decide and record** whether to (a) leave any historical `fingerprint`-sourced tracklist
rows in place as-is (harmless, `refresh_tracklists` already skips them), or (b) purge them as part
of the data-removal runbook alongside the volumes. Also touches
`src/phaze/routers/cue.py:46` (`(Tracklist.source == "fingerprint").desc()` — a CUE-list display
ordering preference for rows that, per (a), may still exist) and its toast copy at
`cue.py:226,233` ("timing data from fingerprinting or 1001Tracklists").

---

## 3. phaze-0jpe.2 — runtime code (service layer, tasks, stage, routers, UI, config)

### 3.1 Whole-file removal

- `src/phaze/services/fingerprint.py` (Protocol, `AudfprintAdapter`, `PanakoAdapter`,
  `FingerprintOrchestrator`, `EngineQueryError`, `FingerprintQueryUnavailableError`,
  `get_fingerprint_progress`)
- `src/phaze/services/fingerprint_requeue.py` (`FingerprintEnqueueResult`, `enqueue_fingerprint_jobs`, `select_outage_failed_files`)
- `src/phaze/tasks/fingerprint.py` (`fingerprint_file`, `FingerprintEnginesUnavailable`)
- `src/phaze/routers/agent_fingerprint.py` (PUT/POST `/api/internal/agent/fingerprints/*`)
- `src/phaze/routers/agent_tracklists.py` (see §2.2)
- `src/phaze/schemas/agent_fingerprint.py` (`FingerprintWriteRequest/Response`, `FingerprintFailureResponse`)
- `src/phaze/schemas/agent_tracklists.py` (see §2.2 — the `TracklistCreatePayload`/`TracklistTrackPayload`/`ScanTerminalAckResponse` family used only by `scan_live_set`)
- `src/phaze/models/fingerprint.py` (`FingerprintResult`)
- `src/phaze/templates/pipeline/partials/fingerprint_workspace.html`
- `src/phaze/templates/pipeline/partials/trackid_workspace.html` + `_trackid_files.html` (§2.4)

### 3.2 Symbol-level removal inside a file that otherwise stays (surgical)

- `src/phaze/tasks/scan.py` — `scan_live_set` only (§2.3)
- `src/phaze/tasks/agent_worker.py` — `FingerprintOrchestrator`/`AudfprintAdapter`/`PanakoAdapter`
  imports; `ctx["fingerprint_orchestrator"]` construction (~lines 215–223); `"fingerprint_file":
  fingerprint_file` from the functions map; `"fingerprint_file"` from whatever task-list constant
  sits at line ~314; the lane-tag comment block
- `src/phaze/tasks/reenqueue.py` — see §0's row (this is the highest-risk file in the whole
  molecule after `stage_status.py`/`enums/stage.py`; read the module docstring's "THE PER-STAGE
  DOMAIN-COMPLETED PREDICATE" section fully before editing)
- `src/phaze/tasks/_shared/deterministic_key.py`, `_shared/stage_control.py`,
  `_shared/model_bootstrap.py` (comment only) — entries only, per §0
- `src/phaze/tasks/aborting_reaper.py` — docstring example cites `fingerprint_file:<file_id>` as
  the illustrative deterministic key; update the example, no logic changes needed (it's generic
  over any key)
- `src/phaze/tasks/tracklist.py` — `refresh_tracklists`'s `phaze-p1vy` guard/docstring: keep the
  behavior (still correct for any surviving historical rows, §2.6), but the docstring's phrasing
  ("Fingerprint-sourced tracklists... routers/agent_tracklists.py creates them") will dangle once
  that router is gone — reword to past tense / historical-data framing
- `src/phaze/services/backends.py`, `services/enqueue_router.py`,
  `services/agent_task_router.py`, `services/pipeline_counters.py`, `services/queue_introspection.py`,
  `services/pagination.py`, `services/discogs_matcher.py` (comment-only "Follows the same pattern as
  AudfprintAdapter/PanakoAdapter" — update wording, no logic), `services/scan_deletion.py`,
  `services/agent_client.py` (`put_fingerprint`, `report_fingerprint_failed` methods + the
  `schemas.agent_fingerprint` import) — see §0/§2.5 for the shared-machinery ones
- `src/phaze/services/pipeline.py` — the single largest surgical edit in the repo (2746 lines).
  Named symbols/regions to remove: `FingerprintResult` import; `get_fingerprint_progress` /
  `enqueue_fingerprint_jobs` imports; `get_fingerprint_pending_files` import + its definition
  wherever it lives; the `"fingerprint": ("fingerprint_file",)` mapping (~line 134); every
  `"fingerprint"` key in the stage-name tuples/dicts (~lines 216, 259-260, 287, 301, 310, 1196,
  1934, 2004, 2035-2036); the whole "Fingerprint endpoints" section (~lines 2500-2614: `trigger_fingerprint`,
  `fingerprint_progress`, `trigger_fingerprint_ui`, `_enqueue_fingerprint_jobs`); the "Tracklist
  fingerprint-scan endpoint" section (~lines 2667-2736, the `POST /pipeline/scan-live-sets` HTMX
  trigger — this is the DAG "Fingerprint-Scan"/"Identify Set" node's producer, not the 1001tracklists
  Search node); the `_PENDING_SORTS["fingerprint"]` contract entry (~line 1012); the whole Track ID
  block (§2.4); `_ENRICH_STAGE_LABELS["fingerprint"]` (~line 1934); `FILES_SORT`'s `"fingerprint"`
  column (~line 1069, collapses to 5 columns per §2.5); `_PENDING_STAGES["fingerprint"]` (~line 1196).
  Keep the shared pending-files fragment mechanism (`pending_files_fragment`) for the surviving
  `"metadata"` key.
- `src/phaze/routers/pipeline_stages.py` — docstring's `(metadata/analyze/fingerprint)` → `(metadata/analyze)`; no route logic change expected beyond the `STAGE_TO_FUNCTION` shrink already covered upstream
- `src/phaze/routers/tags.py`, `routers/agent_heartbeat.py` — comment-only, reword
- `src/phaze/routers/cue.py` — see §2.6
- `src/phaze/routers/column_sort.py` — docstring example only, see §0
- `src/phaze/routers/admin_agents.py` — `_ACTIVITY_STAGES` tuple, see §2.5
- `src/phaze/routers/shell.py` — the `"fingerprint": "pipeline/partials/fingerprint_workspace.html"`
  workspace-template map entry; the `metadata_files`/`fingerprint_files` historical-context comment
- `src/phaze/main.py` — `agent_fingerprint` import + `app.include_router(agent_fingerprint.router)`;
  `agent_tracklists` import + its include (§2.2 — confirm nothing else in `main.py` needs
  `agent_tracklists.router` for the 1001tracklists path; it currently doesn't, that router is
  `routers/tracklists.py` mounted separately); the "mount ALL FOUR lane queues" comment (→ THREE)
- `src/phaze/cli/__init__.py` — the entire `fingerprint`/`fingerprint requeue` subcommand
  (`_main_fingerprint`, its `argparse` wiring, `FingerprintEnqueueResult` import,
  `enqueue_fingerprint_jobs`/`select_outage_failed_files` imports)
- `src/phaze/models/__init__.py` — `FingerprintResult` import + `__all__` entry
- `src/phaze/models/file.py` — docstring mention of `fingerprint_results` in the output-tables list, reword
- `src/phaze/models/analysis.py` — **no change** (§1.1 false positive)
- `src/phaze/models/tracklist.py` — module/class docstring says "Tracklist models for 1001Tracklists
  and fingerprint-sourced tracklists" / "Sources: ... or 'fingerprint' (generated by fingerprint scan
  of a live set)" — reword to reflect §2.6's historical-only framing; no column/constraint change
- `src/phaze/models/pipeline_stage_control.py`, `models/stage_skip.py` — docstrings; stage_skip's
  CHECK constraint change is a **migration**, see §4
- `src/phaze/schemas/agent_tasks.py` — `FingerprintFilePayload`, `ScanLiveSetPayload` classes +
  their docstring mentions
- `src/phaze/schemas/agent_heartbeat.py`, `schemas/wire_bounds.py` — comment-only
- Templates needing partial edits (remove the fingerprint pill/column/poll-seed, keep the rest):
  `_stage_matrix.html`, `_file_table.html`, `_force_skip_dialog.html`, `_lane_detail.html`,
  `_pending_files.html`, `_status_filter_bar.html`, `_workspace_poll_seeds.html`,
  `analyze_workspace.html`, `dedupe_workspace.html`, `empty_state.html`, `files_table_view.html`,
  `stats_bar.html`, `record/record_body.html`, `cue/partials/cue_row.html`,
  `shell/partials/rail.html` (the whole "2b — Fingerprint" nav-rail block: done/total counters,
  orphan badge, priority stepper, pause/resume buttons — all wired to Alpine store keys removed in
  the next bullet), `shell/shell.html` + `base.html` (Alpine `$store.pipeline` seed object:
  `fingerprintBusy`, `fingerprintDone`, `fingerprintTotal`, `fingerprintPaused`,
  `fingerprintPriority`, `fingerprintOrphan`)
- `tests/buckets.json` / `tests/BUCKETS.md` — the `"fingerprint"` test bucket (6 files) disappears
  once `tests/fingerprint/**` is deleted; regenerate/hand-edit alongside the test removal below so
  CI's bucket manifest doesn't reference a deleted directory

### 3.3 Tests — whole-directory/whole-file removal

- `tests/fingerprint/` (all of it: `models/test_fingerprint.py`, `routers/test_agent_fingerprint.py`,
  `routers/test_agent_fingerprint_failure.py`, `routers/test_pipeline_fingerprint.py`,
  `services/test_fingerprint.py`, `services/test_fingerprint_locality.py`,
  `services/test_queue_introspection.py` — **check this last one**, it may cover the shared
  queue-introspection helper generically rather than fingerprint-only, verify before deleting vs.
  trimming — `tasks/test_aborting_reaper.py` — same caution, `tasks/test_fingerprint.py`,
  `test_skipped_leaves_pending.py`)
- `tests/identify/routers/test_agent_tracklists.py`, `tests/identify/schemas/test_agent_tracklists.py` (§2.2)
- `tests/agents/cli/test_fingerprint_requeue.py`
- `tests/shared/test_fingerprint_import_boundary.py` (verified: 100% about `services/fingerprint.py`, safe whole-file delete)

### 3.4 Tests — surgical trim (file covers multiple stages/concerns; remove fingerprint cases only)

`tests/discovery/tasks/test_scan.py` (§2.3), `tests/identify/tasks/test_tracklist.py`,
`tests/shared/test_stage_pill_render.py`, `tests/shared/test_stage_eligibility_dag.py`,
`tests/shared/test_rail_priority_controls.py`, `tests/shared/test_stage_resolver.py`,
`tests/shared/test_partition_guard.py`, `tests/shared/test_domain_completed_contract.py`,
`tests/shared/test_eligibility_trace.py`, `tests/shared/core/test_pipeline_dag_context.py`,
`tests/shared/core/test_no_default_queue_producers.py`, `tests/shared/core/test_phase04_gaps.py`,
`tests/shared/core/test_workspace_poll_seeds.py`, `tests/shared/core/test_shell_routes.py`,
`tests/shared/core/test_config_worker.py`, `tests/shared/services/test_enqueue_router.py`,
`tests/shared/services/test_pipeline.py`, `tests/shared/tasks/test_saq_claim_retry_calibration.py`,
`tests/shared/test_domain_completed_contract.py`, `tests/shared/routers/test_pipeline.py`,
`tests/shared/routers/test_pipeline_stats.py`, `tests/shared/schemas/test_wire_bounds_contract.py`,
`tests/analyze/**` (core/routers/services/tasks — the pipeline-counters/stage-control/ledger/recovery
test files listed in the original sweep), `tests/agents/**` (routers/schemas/services/tasks/core —
heartbeat-lane, agent-client, agent-worker-lanes coverage that enumerates 4 lanes → 3),
`tests/integration/**` (the dozen files matched — `test_stage_status_equivalence.py` and
`test_stage_progress_buckets.py`/`test_fingerprint_progress.py` need the most care;
`test_fingerprint_progress.py` itself is probably whole-file, verify), `tests/review/routers/test_cue.py`,
`tests/review/routers/test_tags.py`, `tests/metadata/routers/test_agent_metadata.py`,
`tests/discovery/services/test_scan_deletion.py`. **This list is the full grep hit-set minus §3.3's
whole-file candidates — the assigned developer should re-run the sweep after each file's edit and
confirm the remaining hits in it are all intentional (shared-machinery) leftovers, not missed cases.**

---

## 4. phaze-0jpe.4 — Alembic migration

- `DROP TABLE fingerprint_results` (model: `src/phaze/models/fingerprint.py`; baseline creates it at
  `alembic/versions/039_baseline_schema.py:172`, PK at :331-332, indexes `ix_fprint_file_engine`/
  `ix_fprint_success` at :389-390, FK at :431-432)
- Alter `stage_skip`'s CHECK constraint `ck_stage_skip_enrich_only` from
  `stage IN ('metadata','analyze','fingerprint')` to `stage IN ('metadata','analyze')`
  (`alembic/versions/039_baseline_schema.py:257`; ORM twin in `src/phaze/models/stage_skip.py:55`).
  **Must first `DELETE FROM stage_skip WHERE stage = 'fingerprint'`** (or the ALTER will fail against
  any existing force-skip rows) — coordinate with the operator runbook (bead .6) on whether any such
  rows exist in production.
- `pipeline_stage_control`: no CHECK to alter, but the baseline's `_SEED_STAGES = ("metadata",
  "analyze", "fingerprint")` seed list (line 452) should drop `"fingerprint"` going forward, and this
  migration should `DELETE FROM pipeline_stage_control WHERE stage = 'fingerprint'` for consistency
  (harmless to leave, since nothing reads `pipeline_stage_control` by an allowlist of stage values
  the way `stage_skip`'s CHECK does — but a stale row is confusing to an operator inspecting the table).
- **Open decision, not this bead's to make:** whether to also purge `tracklists`/`tracklist_versions`/
  `tracklist_tracks` rows where `tracklists.source = 'fingerprint'` (§2.6). No schema change is
  required either way (`source` has no CHECK), so this is purely a data-hygiene call for bead .4 or
  the bead .6 runbook to make explicitly.
- Confirm no other migration after 039 touches `fingerprint_results` / `stage_skip` /
  `pipeline_stage_control` (baseline squash — verify there are no post-039 versions files; if there
  are, check them too before assuming 039 is the only touchpoint).

---

## 5. phaze-0jpe.3 — infrastructure (sidecars, Dockerfiles, compose, CI)

### 5.1 Whole-directory removal

- `services/audfprint/` (`app.py`, `Dockerfile.audfprint`, `osv-scanner.toml`, `pyproject.toml`,
  `README.md`, `uv.lock`)
- `services/panako/` (same shape)

### 5.2 Compose files

- `docker-compose.agent.yml`: `worker-fingerprint` service (lines ~75-90, includes
  `PHAZE_AGENT_LANE=fingerprint` / `PHAZE_LANE_FINGERPRINT_CONCURRENCY`); `audfprint` service
  (~165-183) + its `audfprint_data` volume; `panako` service (~186-203) + `panako_data` volume;
  header comment block describing "FOUR lane workers" / the service topology (~lines 13-26)
- `docker-compose.cloud-agent.yml`: comment-only references (no services to remove — it explicitly
  has no watcher/audfprint/panako already); update the explanatory comments (~lines 10, 23, 75)
- `docker-compose.yml`: comment-only (~line 4)

### 5.3 Dockerfiles

- `Dockerfile`, `Dockerfile.agent-arm64`: **keep** the chromaprint/ffmpeg/libsndfile apt packages
  (§1.2) — only reword the comment if it implies the fingerprinting *feature* rather than the
  essentia runtime dependency (re-read carefully before editing; don't let a keyword match delete a
  system dependency)

### 5.4 CI workflows

- `.github/workflows/docker-publish.yml`: matrix entries for `audfprint` (~line 36-40) and `panako`
  (~line 41-45)
- `.github/workflows/docker-validate.yml`: matrix entries (~lines 28-32); the whole "Fingerprint
  smoke — panako stores and queries a fixture" step (~lines 85-173)
- `.github/workflows/cleanup-images.yml`: `phaze/audfprint` / `phaze/panako` image-name matrix
  entries (~lines 22-23)
- `osv-scanner.toml`: comment referencing `services/audfprint/osv-scanner.toml` as an example — reword

### 5.5 Tests covering the sidecars/infra directly (belongs with this bead, not .2, since they test `services/**` code and compose/Dockerfile shape rather than `src/phaze`)

- `tests/services/` — whole directory: `conftest.py` (the `audfprint_app`/`panako_app` fixture
  loaders), `test_audfprint_app.py`, `test_panako_app.py`, `test_smoke.py`
- `tests/agents/deployment/test_audfprint_pin.py` — whole file
- `tests/agents/deployment/test_sidecar_uid.py` — whole file (verified: 100% audfprint/panako UID-pin assertions)
- `tests/agents/deployment/test_agent_compose.py`, `test_cloud_agent_compose.py`,
  `test_api_filesystem_isolation.py` — surgical trim (these files assert broader compose/Dockerfile
  shape too; remove only the fingerprint/audfprint/panako-specific assertions)

---

## 6. phaze-0jpe.5 — documentation, justfile, tech-stack doc

- `justfile`: the `[group('fingerprint')]` recipes (`fingerprint`, `fingerprint-progress`,
  `audfprint-health`, `panako-health`, lines ~877-896); the Dockerfile-iteration arrays at
  ~lines 648, 665-666, 674-675 (`audfprint`/`panako` entries)
- `docs/api.md`: the `/api/v1/fingerprint*`, `/pipeline/fingerprint`, `/pipeline/scan-live-sets`,
  `/api/internal/agent/fingerprints/*` endpoint rows; the DAG-poll seed-key list's
  `fingerprintDone`/`fingerprintTotal`/`scanBusy` entries; the stage-control section's
  `(metadata/analyze/fingerprint)` phrase → `(metadata/analyze)`; the scan-batch-delete cascade
  description's "fingerprints" mention
- `docs/database.md`: `fingerprint_results` table row + its ER-diagram edge; `Stage` enum's 7→5
  description; the `tracklists` row's `(1001tracklists or fingerprint source)` parenthetical (§2.6:
  reword to historical)
- `docs/configuration.md`: `PHAZE_LANE_FINGERPRINT_CONCURRENCY` row; the whole "Fingerprint service
  settings" section (`AUDFPRINT_URL`/`PANAKO_URL` rows + the localhost-only validation note)
- `docs/agent-queue-lanes.md`: the `fingerprint` lane row in the concurrency table; "FOUR lane
  workers" / `analyze(4) + fingerprint(2) = 6 CPU-bound slots` sizing math (recompute for 3 lanes);
  the `docker compose ... up -d worker-analyze worker-fingerprint worker-meta worker-io watcher
  audfprint panako` example command
- `docs/essentia-analysis.md`: the "Out of scope: audio fingerprinting..." note — this can likely
  stay verbatim (it already correctly describes fingerprinting as separate from essentia and names
  the sidecars only in passing) but re-read once the sidecars are gone in case it needs a "removed"
  note instead of "out of scope"
- `docs/architecture.md`, `docs/cloud-burst.md`, `docs/deployment.md`, `docs/project-structure.md`,
  `docs/quick-start.md`, `docs/README.md`: sweep hits present, hand-check each (not read in depth by
  this bead — flagged for .5 to triage)
- `docs/design/0001-audiomuse-ai-no-go.md`, `docs/spikes/phaze-p3hj.1-audfprint-total-outage-diagnosis.md`,
  `docs/spikes/phaze-ytgo.*.md`, `docs/spikes/phaze-37i1.1-audit-log-diagnosis.md`,
  `docs/spikes/phaze-ytgo.7-verdict.md`: **historical records — do not edit.** These document
  decisions/investigations as they stood at the time; per this repo's own convention, spike docs are
  a point-in-time record, not living documentation. Leave them untouched (the epic's own charter
  cites `phaze-p3hj` and the essentia spikes as the evidentiary basis for this removal — editing them
  would erase the trail this decision rests on).
- Root `CLAUDE.md` / `CONVENTIONS.md` / `README.md`: the technology-stack tables reference
  `pyacoustid` (already noted as "not currently used") and `chromaprint (system)` as an
  essentia-tensorflow runtime dep (§1.2, keep) — no audfprint/panako/fingerprint-stage entries appear
  to need changes in the stack tables themselves beyond removing any lingering
  fingerprinting-feature description in the "Project" prose section; hand-check
- `design/showcase.html`, `docs/superpowers/specs/**`: these are UI-redesign spec/prototype assets
  (static HTML mockups + a design doc), not live app templates — confirm with bead .5 whether they're
  archived reference material (likely: leave alone, same historical-record logic as the spike docs)
  or need updating; not resolved by this bead

---

## 7. phaze-0jpe.6 — obsoleted beads + operator runbook (not this molecule's code, flagged for awareness)

- The operator data-removal runbook must cover: the live `audfprint`/`panako` Docker volumes
  (explicitly **not** touched by any agent per the epic's DESIGN section), and per §2.6/§4, a
  decision on any historical `source='fingerprint'` tracklist rows.
- `docs/runbook.md` already exists — check whether it's the right place to append the
  fingerprint-volume-removal step, or whether a new doc is warranted.

---

## 8. Summary counts

- 222 files matched the literal sweep terms (excluding `.beads/`); `trackid` and bare `scan_live_set`
  add several more not counted above (mostly already covered incidentally since they live in files
  the sweep did catch via other terms).
- Whole-file/whole-directory removals identified: ~30 source files, 2 full service directories,
  ~15 whole test files, 2 whole templates.
- Surgical (partial-edit) files identified: ~45 source files, ~13 templates, ~40 test files.
- Shared machinery requiring "delete the branch, not the function" discipline: 12 named mechanisms (§0).
- Structural decisions flagged for the next bead to make explicitly, not infer: 6 (§2).
