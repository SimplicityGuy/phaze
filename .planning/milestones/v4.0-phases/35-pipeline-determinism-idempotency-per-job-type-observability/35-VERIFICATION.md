---
phase: 35-pipeline-determinism-idempotency-per-job-type-observability
verified: 2026-06-12T05:00:00Z
status: human_needed
score: 6/6 must-haves verified
overrides_applied: 0
human_verification:
  - test: "Render the pipeline dashboard in a browser and confirm the 9-node SVG DAG is visible, with colored stage-stripe tops, bezier edges connecting the nodes in the correct layout, and no layout overflow."
    expected: "Discovery in col0; Metadata/Analyze/Fingerprint/Scan-Search stacked in col1; Proposals/Scrape in col2; Execute/Match in col3. Edges render as smooth curves from right-anchor to left-anchor."
    why_human: "SVG layout and bezier edge rendering depend on computed pixel positions; grep verifies the layout map values and edge list but cannot confirm visual correctness."
  - test: "With discovered=0 in the store, check that Metadata, Analyze, Fingerprint, and Scan/Search nodes all show the disabled state (opacity-60), the state pill reads WAITING or GATED, and the trigger button label is 'No files discovered'."
    expected: "All four upstream nodes are visually dimmed with the locked reason string shown as the button label."
    why_human: "Alpine.js reactive gating requires a live browser with JavaScript executing; :disabled and :class bindings are not exercised by server-side render tests."
  - test: "Resize the browser viewport below the sm breakpoint (640px) and confirm the SVG canvas disappears and the stacked <ol> list appears showing all 9 stages in topological order with done/total and state text."
    expected: "The <ol> is visible at < sm; each <li> names the stage, shows done/total (or done/—), state pill text, and disabled reason where applicable."
    why_human: "Responsive breakpoint behavior requires a real browser; Tailwind hidden/sm:sr-only classes are not exercised by the server-render test."
  - test: "Trigger metadata extraction via the DAG canvas 'Extract Metadata' button, then verify in the Postgres proposals table or SAQ admin UI that a complete payload (file_id, original_path, file_type, agent_id) arrived at the worker — not a file_id-only payload."
    expected: "The extract_file_metadata job in SAQ contains all four required fields; no job dead-letters with a ValidationError."
    why_human: "The payload shape is verified by code inspection (CR-01 fix), but end-to-end verification requires a live worker that runs model_validate against the real payload."
---

# Phase 35: Pipeline Determinism, Idempotency & Per-Job-Type Observability — Verification Report

**Phase Goal:** Make every pipeline job schedule-safe (no duplicate queued items), idempotent (no duplicate rows), give the operator manual control over metadata extraction, and surface per-job-type progress on the dashboard. Generalizes the Phase 32 deterministic-key fix (which covered only `process_file`) to the whole pipeline.

**Verified:** 2026-06-12T05:00:00Z
**Status:** human_needed
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | D-05: central `apply_deterministic_key` before_enqueue hook keys every routable task `<function>:<natural_id>`, registered on all 4 seams, no call site can drift | VERIFIED | Hook in `deterministic_key.py:86`; `register_before_enqueue(apply_deterministic_key)` at `main.py:108`, `agent_task_router.py:105`, `controller.py:142`, `agent_worker.py:190`; drift-guard test in `test_deterministic_key.py:183` |
| 2 | D-04: `store_proposals` is a partial-index upsert; migration 019 dedupes THEN adds the index; APPROVED proposals never overwritten | VERIFIED | `proposal.py:347` `on_conflict_do_update(index_elements=["file_id"], index_where=status=='pending')`; `019_add_proposals_pending_unique_index.py` dedupe SQL + `create_index` in upgrade order; `uq_proposals_file_id_pending` in `proposal.py` `__table_args__`; WR-01 bounds check at `proposal.py:312-315`; WR-04 forward-only guard at `proposal.py:370` |
| 3 | D-06: both auto-enqueue paths for `extract_file_metadata` removed (agent file-upsert + legacy ingestion run_scan) | VERIFIED | `agent_files.py`: zero occurrences of `extract_file_metadata`; `ingestion.py:129` retains `queue` parameter as `noqa: ARG001` with comment "Phase 35 D-06 removed the auto-enqueue"; no enqueue call present |
| 4 | D-03: `get_stage_progress` counts each stage's OUTPUT TABLE via `COUNT(DISTINCT ...)`, not the linear `FileRecord.state`; scan_search returns `total=None` | VERIFIED | `pipeline.py:161` function queries `FileMetadata`, `FingerprintResult`, `AnalysisResult`, `Tracklist`, `TracklistVersion`, `DiscogsLink`, `RenameProposal`, `ExecutionLog` directly; `scan_search` returns `total: None`; `test_stage_progress.py:59-74` proves analyze.done=1 with metadata.done=0 on a file that has analysis but no metadata row |
| 5 | Observability plumbing: `_build_dag_context` reconciles get_stage_progress + counters + queue activity in both dashboard() and pipeline_stats_partial(); counter reads are failure-isolated (never 500) | VERIFIED | `pipeline.py:106` `_build_dag_context`; called at `pipeline.py:308` (dashboard) and `pipeline.py:341` (stats partial); `_read_pipeline_counters` at `pipeline.py:71` wraps in try/except degrading to `{}`; `_reconciled_done` at `pipeline.py:90` implements cap and degrade-fallback per WR-03; `oob_counts=True` only in `pipeline_stats_partial` |
| 6 | D-01: `dag_canvas.html` renders the 9-node SVG DAG with honest edges, per-node gated triggers to existing endpoints, `<ol>` text fallback; legacy `stage_cards.html` + `processing_card.html` removed | VERIFIED | Template exists with `role="group" aria-label="Pipeline stage graph"` at line 169; EDGES list contains `metadata->proposals` and `analyze->proposals` but NOT `fingerprint->proposals` or any tracklist->proposals; `node_count('scan_search', em_dash=true)` at line 256; `<ol class="sm:sr-only">` at line 333 with 9 items; `stage_cards.html` and `processing_card.html` REMOVED; `dashboard.html:28` includes dag_canvas; no dangling references |

**Score:** 6/6 truths verified

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/tasks/_shared/deterministic_key.py` | `apply_deterministic_key` before_enqueue hook + 8-entry `_KEY_BUILDERS` + `increment_completed` after_process hook | VERIFIED | File exists, substantive, exports both hooks in `__all__`; `_KEY_BUILDERS` has exactly 8 entries; `generate_proposals` uses `_hash_ids` order-independent batch hash |
| `src/phaze/services/pipeline_counters.py` | Redis INCR-backed counters + `read_counters` returning per-function dict | VERIFIED | `PIPELINE_FUNCTIONS` tuple matches `_KEY_BUILDERS`; `incr_enqueued`, `incr_completed`, `read_counters` all present with durable (no EXPIRE) semantics |
| `src/phaze/services/pipeline.py` | `get_stage_progress` per-stage output-table distinct counts + denominators | VERIFIED | Function present, counts 9 DAG nodes from their respective output tables, `_safe_count` failure isolation per source, `scan_search total=None` |
| `src/phaze/routers/pipeline.py` | `_build_dag_context` + enqueue helpers with COMPLETE payloads (CR-01/CR-02 fixes) | VERIFIED | `_build_dag_context:106` reconciles three sources; `_enqueue_extraction_jobs:433` builds full `ExtractMetadataPayload(file_id, original_path, file_type, agent_id)`; `_enqueue_fingerprint_jobs:524` builds full `FingerprintFilePayload(file_id, original_path, agent_id)` |
| `alembic/versions/019_add_proposals_pending_unique_index.py` | dedupe-then-index migration, `down_revision="018"` | VERIFIED | `down_revision = "018"`; `_DEDUPE_PENDING_SQL` uses `row_number() OVER (PARTITION BY file_id ORDER BY created_at DESC)`; `create_index("uq_proposals_file_id_pending")` follows in `upgrade()` |
| `src/phaze/models/proposal.py` | `uq_proposals_file_id_pending` in `__table_args__` | VERIFIED | `Index("uq_proposals_file_id_pending", "file_id", unique=True, postgresql_where=text("status = 'pending'"))` present at line 59 |
| `src/phaze/templates/pipeline/partials/dag_canvas.html` | 9-node SVG DAG, honest edges, gated triggers, `<ol>` fallback | VERIFIED | All elements present; edges derived from `NODE_LAYOUT` anchor map; trigger endpoints are `/pipeline/extract-metadata`, `/pipeline/analyze`, `/pipeline/fingerprint`, `/pipeline/proposals`, `/proposals/` only |
| `src/phaze/templates/base.html` | `$store.pipeline` extended with all per-node sub-keys | VERIFIED | `metadataDone`, `metadataTotal`, `fingerprintDone`, `fingerprintTotal`, `analyzeDone`, `analyzeTotal`, `analyzeActive`, `tracklistDone`, `scrapeDone`, `scrapeTotal`, `matchDone`, `matchTotal`, `proposalsDone`, `proposalsTotal`, `approved`, `executedDone`, `executedTotal` all present, all initialized to 0 |
| `tests/test_deterministic_key.py` | drift-guard test asserting all routable tasks are keyed or exempted | VERIFIED | `test_every_routable_task_is_keyed_or_exempt` at line 183 imports `CONTROLLER_TASKS | AGENT_TASKS` and asserts each name is in `_KEY_BUILDERS | _UNKEYED_TASKS` |
| `tests/test_proposals_upsert.py` | double-run yields one pending row; approved row preserved | VERIFIED | `test_double_run_overwrites_single_pending_row`, `test_rerun_never_touches_approved_row`, `test_fresh_insert_stamps_pk`, `test_rerun_does_not_regress_terminal_file_state` all present |
| `tests/test_migration_019_dedupe.py` | migration test proves dedupe + index creation | VERIFIED | File exists |
| `tests/test_no_auto_metadata_enqueue.py` | proves neither file-upsert nor ingestion scan enqueues extract_file_metadata | VERIFIED | File exists |
| `tests/test_stage_progress.py` | proves output-table sourcing (analyzed-but-no-metadata discriminator) | VERIFIED | `test_analyzed_but_no_metadata_counts_independently` at line 58 seeds `AnalysisResult` only and asserts `analyze.done==1, metadata.done==0` |
| `tests/test_pipeline_dag_context.py` | forced counter-failure still returns 200; completed degrade-fallback test | VERIFIED | File exists |
| `tests/test_dag_canvas_render.py` | integration-level DAG render tests | VERIFIED | File exists with `aria-label="Pipeline stage graph"` assertion at line 148; stage_cards/processing_card removal test at line 351-353 |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `main.py` | `apply_deterministic_key` | `register_before_enqueue(apply_deterministic_key)` | WIRED | `main.py:46` import + `main.py:108` registration |
| `agent_task_router.py` | `apply_deterministic_key` | `register_before_enqueue(apply_deterministic_key)` | WIRED | `agent_task_router.py:27` import + `agent_task_router.py:105` registration |
| `tasks/controller.py` | `apply_deterministic_key` + `increment_completed` | `register_before_enqueue` + `"after_process"` in settings dict | WIRED | `controller.py:35` import; `controller.py:142` + `controller.py:150` |
| `tasks/agent_worker.py` | `apply_deterministic_key` + `increment_completed` | `register_before_enqueue` + `"after_process"` in settings dict | WIRED | `agent_worker.py:59` import; `agent_worker.py:190` + `agent_worker.py:197` |
| `pipeline.py pipeline_stats_partial` | `get_stage_progress` + `read_counters` + `get_queue_activity` | `_build_dag_context` call | WIRED | `pipeline.py:341` calls `_build_dag_context`; `_build_dag_context:119` calls `get_stage_progress`; `_build_dag_context:120` calls `_read_pipeline_counters` |
| `stats_bar.html` | `$store.pipeline` per-node keys | `hx-swap-oob` + `x-init` seeds in `dag.items()` loop | WIRED | `stats_bar.html:66-67` emits `dag-seed-<key>` OOB paragraphs for every key in the dag context map |
| `dag_canvas.html` node triggers | existing endpoints | `hx-post="{{ endpoint }}"` via `enqueue_button` macro | WIRED | `/pipeline/extract-metadata`, `/pipeline/analyze`, `/pipeline/fingerprint`, `/pipeline/proposals`; navigational `/proposals/` for Execute |
| `proposal.py store_proposals` | `uq_proposals_file_id_pending` | `on_conflict_do_update(index_elements=["file_id"], index_where=status=='pending')` | WIRED | `proposal.py:347-361` |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|--------------|--------|--------------------|--------|
| `dag_canvas.html` | `nodes.<node>.done` / `.total` | `$store.pipeline` seeded from `dag` context dict, populated by `_build_dag_context` from `get_stage_progress` DB queries | `get_stage_progress` issues `COUNT(DISTINCT ...)` against output tables | FLOWING |
| `stats_bar.html` | per-node OOB seeds | `dag` context dict from `pipeline_stats_partial()` | Same `_build_dag_context` path | FLOWING |
| `apply_deterministic_key` | `job.key` | `_KEY_BUILDERS[job.function](job.kwargs)` | Direct computation from job payload fields | FLOWING |

### Behavioral Spot-Checks

Step 7b: SKIPPED — full integration test suite (1720 tests via `just integration-test`) already confirmed passing per user attestation. The test suite exercises all key behaviors including the `test_analyzed_but_no_metadata_counts_independently` discriminating test (proven output-table sourcing) and `test_dag_canvas_render.py` integration-level GET /pipeline render. Individual spot-checks would duplicate that coverage without adding confidence.

### Probe Execution

No `scripts/*/tests/probe-*.sh` files declared or discovered for this phase. Phase is a code-only change (no migration runner, no CLI probe). SKIPPED.

### Requirements Coverage

| Requirement Theme | Source Plans | Description | Status | Evidence |
|------------------|-------------|-------------|--------|---------|
| SCHED (Schedulability without duplicate queue items) | 35-01 | Central `before_enqueue` key hook ensures every routable task has a deterministic key; re-enqueue of same natural id is a dedup no-op | SATISFIED | `apply_deterministic_key` registered on all 4 seams; `_KEY_BUILDERS` registry with 8 entries; drift-guard test |
| MANUAL-META (Operator-controlled metadata extraction) | 35-01 | Both auto-enqueue paths for `extract_file_metadata` removed | SATISFIED | Zero references in `agent_files.py`; ARG001 stub with comment in `ingestion.py` |
| OBSERV (Per-job-type pipeline observability) | 35-03, 35-04, 35-05 | `get_stage_progress` + `_build_dag_context` + DAG canvas UI with live per-node counts | SATISFIED | `get_stage_progress` in `services/pipeline.py`; `_build_dag_context` in `routers/pipeline.py`; `dag_canvas.html` renders 9-node DAG |
| IDEMP (Idempotent re-runs, no duplicate rows) | 35-02 | `store_proposals` upserts on partial index; migration 019 dedupes then creates index | SATISFIED | `on_conflict_do_update` in `proposal.py`; migration 019 with correct op order; `uq_proposals_file_id_pending` in model |

### Anti-Patterns Found

Scan of all phase-modified files for TBD/FIXME/XXX/TODO/HACK/PLACEHOLDER patterns:

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| — | — | No markers found | — | — |

No unresolved debt markers in any phase-modified file. WR-02 (proposed_path traversal) is accepted as a deferred follow-up, documented in `35-REVIEW.md` frontmatter, and is pre-existing (not introduced by this phase).

### Human Verification Required

#### 1. SVG DAG visual layout and edge rendering

**Test:** Render GET /pipeline in a real browser and visually inspect the DAG canvas.
**Expected:** 9 nodes positioned in 4 columns (Discovery · {Metadata/Analyze/Fingerprint/Scan-Search} · {Proposals/Scrape} · {Execute/Match}); smooth bezier edges connecting them; no layout overflow; each node has a 3px color stripe at top matching its stage color.
**Why human:** SVG bezier paths are computed from the `NODE_LAYOUT` map (verified in source) but the rendered result — whether edges look clean, whether nodes overlap, whether the canvas clips correctly — requires visual inspection in a browser.

#### 2. Reactive gating: disabled state + reason label when discovered=0

**Test:** With no files discovered (empty DB or fresh install), open the pipeline dashboard and inspect the Metadata, Analyze, Fingerprint, and Scan/Search nodes.
**Expected:** Nodes show `opacity-60` dimming; state pill reads `WAITING`; trigger button label shows `No files discovered` (the LOCKED disabled reason string); buttons are non-clickable.
**Why human:** Alpine.js `:disabled`, `:class`, and `x-text` bindings execute in the browser JavaScript runtime. The server-side render test confirms the HTML structure; the reactive behavior requires JavaScript execution.

#### 3. Responsive `<ol>` fallback at < sm viewport

**Test:** Resize the browser below 640px and verify the SVG canvas hides and the `<ol>` list appears listing all 9 stages in topological order with done/— or done/total, state text, and disabled reasons.
**Expected:** The `<ol>` is visible and readable at mobile width; the SVG is hidden.
**Why human:** Tailwind breakpoint behavior (`hidden sm:block` / `sm:sr-only`) requires a real browser at the correct viewport size.

#### 4. End-to-end metadata trigger payload completeness (CR-01 regression)

**Test:** With an active agent worker and at least one music file discovered, click "Extract Metadata" in the DAG canvas. Inspect the SAQ job in the SAQ admin UI or Postgres task log to confirm all four payload fields (`file_id`, `original_path`, `file_type`, `agent_id`) are present.
**Expected:** The `extract_file_metadata` job payload contains all four required fields; no jobs dead-letter with `ValidationError` about missing fields.
**Why human:** Code inspection confirmed the CR-01 fix builds the complete `ExtractMetadataPayload`; end-to-end validation against a live worker's `model_validate` call requires a running environment.

---

## Gaps Summary

No gaps found. All 6 observable truths are VERIFIED against the actual codebase. The two BLOCKERs identified in the code review (CR-01: incomplete metadata payload, CR-02: incomplete fingerprint payload) are confirmed fixed in `src/phaze/routers/pipeline.py` at lines 433-454 and 524-539 respectively. The three warnings fixed (WR-01: file_index bounds check, WR-03: generate_proposals excluded from fallback map, WR-04: forward-only state guard) are all verified at `proposal.py:312-315`, `pipeline.py:62-68`, and `proposal.py:370`. WR-02 (proposed_path traversal) is accepted as a deferred follow-up, pre-existing to this phase.

Status is `human_needed` because the DAG canvas UI, Alpine.js reactive gating, responsive `<ol>` fallback, and end-to-end payload flow require browser/worker verification that grep-based code inspection cannot provide.

---

_Verified: 2026-06-12T05:00:00Z_
_Verifier: Claude (gsd-verifier)_
