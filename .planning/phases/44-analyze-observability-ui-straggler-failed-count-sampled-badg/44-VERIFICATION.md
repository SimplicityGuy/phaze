---
phase: 44-analyze-observability-ui-straggler-failed-count-sampled-badg
verified: 2026-06-18T00:00:00Z
status: passed
score: 4/4
overrides_applied: 0
re_verification: false
---

# Phase 44: Analyze Observability UI Verification Report

**Phase Goal:** Surface the analysis outcomes Phase 43 starts recording. Add a dashboard count/list of failed/straggler files, a "sampled — more data available" badge on files that were strided, and a "deepen analysis" re-trigger that re-enqueues a sampled file with a higher/unbounded window budget.
**Verified:** 2026-06-18
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Dashboard straggler / ANALYSIS_FAILED count + list visible on 5s poll | VERIFIED | `straggler_count` + `analysis_failed_count` seeded into BOTH `dashboard()` and `pipeline_stats_partial()` at `routers/pipeline.py:361-362,401-402`; rendered via `straggler_failed_card.html`; 19 router tests pass (`-k "straggler or failed or dashboard or stats"`) |
| 2 | Sampled badge driven by coverage fields, NULL-safe | VERIFIED | `sampled_badge.html` gates on `analysis is not none and analysis.sampled`; `analysis_timeline.html` includes it; `routers/proposals.py` fetches `AnalysisResult` via `scalar_one_or_none`; 11 timeline/badge tests pass |
| 3 | "Deepen analysis" action enqueues process_file with elevated cap (via payload flag) | VERIFIED | `POST /pipeline/files/{file_id}/deepen` at `routers/pipeline.py:454`; calls `enqueue_process_file(..., fine_cap=0, coarse_cap=0)`; routes via `enqueue_router.resolve_queue_for_task("process_file")` never the default queue; 6 deepen tests pass |
| 4 | Regression tests for new reads + re-trigger | VERIFIED | 59 unit tests (schemas/enqueue/functions), 8 straggler/failed service tests, 6 deepen router tests, 11 timeline/badge tests — all passing |

**Score:** 4/4 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/schemas/agent_tasks.py` | `ProcessFilePayload.fine_cap / coarse_cap` optional fields (default None) | VERIFIED | Lines 42-43: `fine_cap: int | None = None`, `coarse_cap: int | None = None`; `extra="forbid"` unchanged |
| `src/phaze/services/analysis_enqueue.py` | `enqueue_process_file` extended with `fine_cap`/`coarse_cap` kwargs | VERIFIED | Lines 49-50: keyword-only trailing params; threaded into `ProcessFilePayload(...)` build at line 75-76; single funnel, deterministic key, `timeout=7200`/`retries=2` preserved |
| `src/phaze/tasks/functions.py` | `process_file` prefers payload cap override over `AgentSettings` | VERIFIED | Lines 157-158: `fine_cap = payload.fine_cap if payload.fine_cap is not None else cfg.analysis_fine_cap`; passed to `run_in_process_pool` at lines 166-167 |
| `src/phaze/config.py` | `straggler_threshold_sec` knob in `ControlSettings` | VERIFIED | Lines 350-354: Field in `ControlSettings` (line 327 class start), `PHAZE_STRAGGLER_THRESHOLD_SEC` alias, default 6600 |
| `src/phaze/services/pipeline.py` | `get_straggler_count` / `get_analysis_failed_count` / `get_analysis_failed_files` | VERIFIED | Lines 592, 603, 805; all degrade-safe via `session.begin_nested()` or `_safe_count`; `ANALYSIS_FAILED` absent from `PIPELINE_STAGES` (confirmed by comment at line 585-586) |
| `src/phaze/routers/pipeline.py` | `POST /pipeline/files/{file_id}/deepen` endpoint | VERIFIED | Line 454; per-agent routing, `fine_cap=0/coarse_cap=0`, `NoActiveAgentError` caught; `get_straggler_count`/`get_analysis_failed_count` imported and wired at lines 27,43,361-362,401-402 |
| `src/phaze/templates/pipeline/partials/straggler_failed_card.html` | Dashboard straggler + ANALYSIS_FAILED card | VERIFIED | Renders `{{ straggler_count }}` and `{{ analysis_failed_count }}`; OOB `hx-swap-oob` on `oob` flag keeps it live outside `#pipeline-stats` |
| `src/phaze/templates/proposals/partials/sampled_badge.html` | Render-if-sampled badge with coverage tooltip | VERIFIED | `{% if analysis is not none and analysis.sampled %}` gate; four coverage counts in `title=` tooltip |
| `src/phaze/templates/pipeline/partials/deepen_response.html` | HTMX deepen response fragment | VERIFIED | Three states: queued / no-active-agent / not-found |
| `src/phaze/templates/proposals/partials/analysis_timeline.html` | Deepen button wired to Plan-03 endpoint | VERIFIED | `hx-post="/pipeline/files/{{ file_id }}/deepen"` at line 10; badge included at line 8 |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `analysis_enqueue.py::enqueue_process_file` | `schemas/agent_tasks.py::ProcessFilePayload` | `ProcessFilePayload(fine_cap=fine_cap, coarse_cap=coarse_cap)` build | VERIFIED | Lines 49-50 (kwargs), lines 75-76 (payload build) |
| `tasks/functions.py::process_file` | `services/analysis.py::analyze_file` | `payload.fine_cap is not None` override-with-fallback | VERIFIED | Lines 157-167 |
| `routers/pipeline.py::deepen_analysis` | `services/analysis_enqueue.py::enqueue_process_file` | `enqueue_process_file(routed.queue, file, agent_id, settings.models_path, fine_cap=0, coarse_cap=0)` | VERIFIED | Line 502 |
| `routers/pipeline.py::deepen_analysis` | `enqueue_router.resolve_queue_for_task` | `resolve_queue_for_task("process_file", request.app.state, session)` | VERIFIED | Lines 492-493 |
| `routers/pipeline.py` (dashboard + stats partial) | `services/pipeline.py` (Plan-02 reads) | `get_straggler_count` / `get_analysis_failed_count` seeded into context | VERIFIED | Lines 361-362, 401-402 |
| `templates/proposals/partials/analysis_timeline.html` | `templates/proposals/partials/sampled_badge.html` | `{% include %}` gated on `analysis.sampled` + deepen button POST | VERIFIED | Lines 8, 10-11 of analysis_timeline.html |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|--------------------|--------|
| `straggler_failed_card.html` | `straggler_count` | `get_straggler_count` — queries `saq_jobs` via `_STRAGGLER_ACTIVE_SQL`, deserializes blobs in Python | Yes — live `saq_jobs` query | FLOWING |
| `straggler_failed_card.html` | `analysis_failed_count` | `get_analysis_failed_count` — `func.count` on `FileRecord.state == ANALYSIS_FAILED` | Yes — live DB count | FLOWING |
| `sampled_badge.html` | `analysis` | `proposal_timeline` fetches `AnalysisResult` via `select(AnalysisResult).where(file_id==...)` | Yes — live ORM query, `scalar_one_or_none` | FLOWING |
| `deepen_analysis` endpoint | enqueue result | `enqueue_process_file(routed.queue, file, ...)` — real queue enqueue | Yes — live SAQ queue | FLOWING |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Schema round-trip: `fine_cap`/`coarse_cap` optional fields | `uv run pytest tests/test_schemas/test_agent_tasks.py -q` | 59 passed | PASS |
| Cap threading producer->payload->worker | `uv run pytest tests/test_services/test_analysis_enqueue.py tests/test_tasks/test_functions.py -q` | 59 passed | PASS |
| Straggler + failed service reads (degrade-safe) | `uv run pytest tests/test_services/test_pipeline.py -q -k "straggler or analysis_failed"` | 8 passed | PASS |
| Deepen endpoint (routing, payload, dedup, no-agent, 404) | `uv run pytest tests/test_routers/test_pipeline.py -q -k "deepen"` | 6 passed | PASS |
| Dashboard card + stats wiring | `uv run pytest tests/test_routers/test_pipeline.py -q -k "straggler or failed or dashboard or stats"` | 19 passed | PASS |
| Sampled badge + deepen button on timeline | `uv run pytest tests/test_routers/test_proposals.py -q -k "timeline or sampled or badge or deepen"` | 11 passed | PASS |
| Full DB-backed router + service suite | `uv run pytest tests/test_routers/test_pipeline.py tests/test_routers/test_proposals.py tests/test_services/test_pipeline.py -q` | 153 passed | PASS |

### Probe Execution

Step 7c: SKIPPED — phase produces library/service code, no `scripts/*/tests/probe-*.sh` files declared or found.

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|-------------|--------|----------|
| Dashboard straggler/`ANALYSIS_FAILED` count + list | 44-02, 44-04 | Service reads + dashboard card wired to 5s poll | SATISFIED | `get_straggler_count`, `get_analysis_failed_count`, `straggler_failed_card.html`, 19 tests |
| Sampled badge driven by coverage fields | 44-04 | Render-if-sampled badge with four coverage counts in tooltip | SATISFIED | `sampled_badge.html`, `analysis_timeline.html`, `proposal_timeline` fetches `AnalysisResult`, 11 tests |
| "Deepen analysis" action enqueues `process_file` with elevated cap (via a payload flag) | 44-01, 44-03 | `ProcessFilePayload.fine_cap/coarse_cap` + `POST /pipeline/files/{file_id}/deepen` | SATISFIED | Schema fields, `enqueue_process_file` threading, deepen endpoint at line 454, `fine_cap=0`/`coarse_cap=0`, 6 tests |
| Regression tests for the new reads + re-trigger | 44-01, 44-02, 44-03, 44-04 | Tests for all four areas | SATISFIED | 59 unit + 8 service + 6 router-deepen + 11 timeline tests; all passing |

### Notable Deviations (Auto-Fixed, No Gaps)

1. **`straggler_threshold_sec` moved from `AgentSettings` to `ControlSettings`** (44-04 auto-fix): Plan 44-02 placed the field on `AgentSettings`; Plan 44-04 found mypy rejecting `settings.straggler_threshold_sec` on a control-plane router. Correctly moved to `ControlSettings` — the only consumer is the control-plane dashboard.
2. **Three pre-existing `test_pipeline.py` assertions updated** (44-03 auto-fix): Plan 44-01 added `fine_cap`/`coarse_cap` to the serialized payload; three tests in `test_routers/test_pipeline.py` still asserted the original five-key set. Updated to the correct seven-key set.
3. **`# noqa: TC003` on `uuid` import in `pipeline.py`** (44-03 auto-fix): `uuid` used only in annotation position for the path param; FastAPI resolves it at runtime via `get_type_hints`, so it must stay a runtime import. The noqa suppresses the ruff `TC003` false-positive.

### Anti-Patterns Found

None. No `TBD`, `FIXME`, `XXX`, `TODO`, `HACK`, or placeholder patterns found in any of the 10 modified source files and templates.

### Human Verification Required

1. **Dashboard card visual layout and live OOB update**

   **Test:** Load the pipeline dashboard in a browser with a running system. Confirm the "Analysis Health" card (straggler + ANALYSIS_FAILED counts) is visible and updates every 5 seconds alongside the stats bar — the card uses `hx-swap-oob` and lives outside `#pipeline-stats`.

   **Expected:** Two count boxes (amber for stragglers, red for analysis-failed) update live on the 5s poll without a full page reload.

   **Why human:** OOB HTMX swap and Tailwind styling cannot be verified programmatically.

2. **Sampled badge rendering and deepen flow**

   **Test:** Open a proposal timeline for a file whose `AnalysisResult.sampled = True`. Confirm the amber "Sampled — more data available" pill appears with the correct coverage counts in the hover tooltip. Click "Deepen analysis" — confirm the inline result span shows "Re-analysis queued at full window budget (deepen)" (or the no-agent / not-found message as appropriate).

   **Expected:** Badge visible with tooltip, button click triggers HTMX POST and renders deepen_response.html fragment inline.

   **Why human:** Visual badge rendering, tooltip hover, and live HTMX swap are not testable with the pytest/httpx harness.

---

_Verified: 2026-06-18_
_Verifier: Claude (gsd-verifier)_
