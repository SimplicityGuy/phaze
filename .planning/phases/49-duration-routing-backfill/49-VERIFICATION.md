---
phase: 49-duration-routing-backfill
verified: 2026-06-25T21:30:00Z
status: human_needed
score: 10/10 must-haves verified
overrides_applied: 0
human_verification:
  - test: "Trigger 'Run Analysis' with only long files and NO agents online — check the HTMX response copy"
    expected: "The response should mention that K files are awaiting cloud (currently, the no_active_agent branch wins and renders 'No files enqueued for analysis' — WR-01 deferred cosmetic issue)"
    why_human: "Template branch precedence ({% if no_active_agent %} wins over split_counts) cannot be verified without a live browser rendering; automated tests confirm the counts ARE in the context but do not assert which template branch the UI renders when no_active_agent=1 and awaiting>0"
  - test: "Verify the 'Awaiting cloud' count card updates live on the dashboard"
    expected: "After files are held in AWAITING_CLOUD (either via Run Analysis or Backfill with no compute agent), the card renders a non-zero count on first load, and re-renders the correct count on the 5s pipeline/stats poll (OOB swap)"
    why_human: "Card wiring is unit-tested (dashboard() and pipeline_stats_partial() contexts confirmed, OOB include confirmed in templates), but the live update cadence is a browser-level interaction"
---

# Phase 49: Duration-Routing Backfill Verification Report

**Phase Goal:** Analysis jobs route by duration — long files (≥ configurable threshold, default 90 min) go to an online compute agent, short files stay local with unchanged behavior, and the existing timed-out long files can be backfilled to the cloud without re-detonating the queue. Long files held in AWAITING_CLOUD when no compute agent is online, released by a */5 cron when one comes online.
**Verified:** 2026-06-25T21:30:00Z
**Status:** human_needed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|---------|
| 1 | D-07: `cloud_route_threshold_sec` config knob exists (default 5400, alias PHAZE_CLOUD_ROUTE_THRESHOLD_SEC, bounded gt=0/lt=86400) | ✓ VERIFIED | `src/phaze/config.py:365-370` — Field with default=5400, gt=0, lt=86400, AliasChoices; 130 tests passed |
| 2 | D-01: `FileState.AWAITING_CLOUD = "awaiting_cloud"` exists as a code-only StrEnum member | ✓ VERIFIED | `src/phaze/models/file.py:43`; no migration added (24 migration files unchanged) |
| 3 | D-13: `select_active_agent(session, kind='compute')` returns only a compute agent; `kind='fileserver'` excludes compute agents; absent kind raises NoActiveAgentError | ✓ VERIFIED | `src/phaze/services/enqueue_router.py:118-119`; 4 kind-scoping tests in test_enqueue_router.py pass |
| 4 | Duration-join helper `get_discovered_files_with_duration` returns `(FileRecord, duration|None)` tuples via outerjoin without triggering lazy load | ✓ VERIFIED | `src/phaze/services/pipeline.py:796-802` — `outerjoin(FileMetadata, FileMetadata.file_id == FileRecord.id)` with in-memory capture |
| 5 | D-05: `get_awaiting_cloud_count` returns count of AWAITING_CLOUD files, degrades to 0 on error | ✓ VERIFIED | `src/phaze/services/pipeline.py:805-816` — `_safe_count` over `FileRecord.state == FileState.AWAITING_CLOUD` |
| 6 | D-09/D-10: Backfill-candidate query returns exactly ANALYSIS_FAILED files with duration >= threshold (explicit filter, not all ANALYSIS_FAILED) | ✓ VERIFIED | `src/phaze/services/pipeline.py:819-855` — INNER JOIN + `duration >= threshold_sec` bound param; test asserts short ANALYSIS_FAILED files are excluded |
| 7 | D-11/D-06: `_route_discovered_by_duration` routes long files to compute independently; short/null files to fileserver; long with no compute -> AWAITING_CLOUD (never silently local) | ✓ VERIFIED | `src/phaze/routers/pipeline.py:252-339`; resolves BOTH kinds in separate try/except blocks ONCE before loop; explicit `await session.commit()` on held state; tests assert compute-only capture for long files |
| 8 | D-12: Run-analysis response reports split counts (N local, M cloud, K awaiting cloud, S skipped) | ✓ VERIFIED | `src/phaze/templates/pipeline/partials/trigger_response.html:5-10` — `{% elif split_counts %}` branch renders all four buckets |
| 9 | D-08/D-09: POST /pipeline/backfill-cloud selects ANALYSIS_FAILED∧duration>=threshold, resets to DISCOVERED, routes via shared duration router; ledger row for held files; double-click is a no-op | ✓ VERIFIED | `src/phaze/routers/pipeline.py:627-690`; `insert_ledger_if_absent` called only for AWAITING_CLOUD-held files; 6 backfill tests pass |
| 10 | D-03/D-03a + CR-01: `release_awaiting_cloud` CronJob(*/5) drains AWAITING_CLOUD to compute queue; recovery (`recover_orphaned_work`) routes held process_file rows to compute-only (never fileserver) | ✓ VERIFIED | `src/phaze/tasks/release_awaiting_cloud.py` exists; `src/phaze/tasks/controller.py:232` CronJob registered; `src/phaze/tasks/reenqueue.py:304-332` CR-01 fix with `_get_awaiting_cloud_ids` partition; commit a55482d; 3 regression tests in test_recovery.py |

**Score:** 10/10 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/config.py` | `cloud_route_threshold_sec` Field | ✓ VERIFIED | Present at L365; default=5400, gt=0, lt=86400, alias PHAZE_CLOUD_ROUTE_THRESHOLD_SEC |
| `src/phaze/models/file.py` | `AWAITING_CLOUD` state member | ✓ VERIFIED | `AWAITING_CLOUD = "awaiting_cloud"` at L43 |
| `src/phaze/services/enqueue_router.py` | kind-filtered `select_active_agent` | ✓ VERIFIED | `kind: str | None = None` param at L93; `Agent.kind == kind` filter at L118-119 |
| `src/phaze/services/pipeline.py` | 4 duration-routing helpers | ✓ VERIFIED | All 4 at L788/L805/L833/L847; outerjoin and INNER JOIN semantics confirmed |
| `tests/_queue_fakes.py` | `seed_active_agent(kind=...)` | ✓ VERIFIED | `kind: str = "fileserver"` param at L331; passed to `Agent(kind=kind)` at L345 |
| `src/phaze/routers/pipeline.py` | `_route_discovered_by_duration` + backfill endpoint | ✓ VERIFIED | Router at L252; POST endpoint at L627 |
| `src/phaze/templates/pipeline/partials/awaiting_cloud_card.html` | Count card with OOB contract | ✓ VERIFIED | id="awaiting-cloud-card"; `{% if oob %}hx-swap-oob="true"{% endif %}` present |
| `src/phaze/templates/pipeline/partials/backfill_response.html` | Count-confirmed response partial | ✓ VERIFIED | Renders `{{ count }}` long files, `{{ cloud }}` cloud, `{{ awaiting }}` awaiting cloud |
| `src/phaze/templates/pipeline/partials/dag_canvas.html` | Backfill button | ✓ VERIFIED | `hx-post="/pipeline/backfill-cloud"` at L291; aria-label="Backfill timed-out long files to the cloud" |
| `src/phaze/tasks/release_awaiting_cloud.py` | State-driven held-file release | ✓ VERIFIED | File exists; SCAN→GATE→RELEASE flow implemented; FastAPI-free (no FastAPI imports) |
| `src/phaze/tasks/controller.py` | CronJob(release_awaiting_cloud, '*/5 * * * *') | ✓ VERIFIED | L232 — CronJob registered; L212 in functions list |
| `tests/test_tasks/test_recovery.py` | D-04 regression + CR-01 regression tests | ✓ VERIFIED | 4 AWAITING_CLOUD tests at L564-700; 3 CR-01 tests assert compute-only routing and fileserver exclusion |
| `src/phaze/tasks/reenqueue.py` | CR-01 fix — held rows route to compute-only in recovery | ✓ VERIFIED | L304-332; `_get_awaiting_cloud_ids` partitions held rows; `select_active_agent(session, kind="compute")` called for held partition; non-held rows keep kind-agnostic path |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `services/pipeline.py` | `models/metadata.py` | `outerjoin(FileMetadata, FileMetadata.file_id == FileRecord.id)` | ✓ VERIFIED | L798 — outerjoin in `get_discovered_files_with_duration` |
| `services/enqueue_router.py` | `models/agent.py` | `Agent.kind == kind` filter | ✓ VERIFIED | L118-119 in `select_active_agent` |
| `routers/pipeline.py` | `services/enqueue_router.py` | `select_active_agent(session, kind=...)` pre-selected once per kind | ✓ VERIFIED | L290, L294 — separate try/except blocks |
| `routers/pipeline.py` | `services/analysis_enqueue.py` | `enqueue_process_file` reused for both local and cloud | ✓ VERIFIED | L325-330 — identical call for both branches |
| `templates/pipeline/partials/stats_bar.html` | `templates/pipeline/partials/awaiting_cloud_card.html` | OOB include on 5s poll | ✓ VERIFIED | L78 — `{% with oob = True %}{% include "pipeline/partials/awaiting_cloud_card.html" %}{% endwith %}` |
| `routers/pipeline.py` | `services/scheduling_ledger.py` | `insert_ledger_if_absent` for AWAITING_CLOUD-held backfill files | ✓ VERIFIED | L678 — called only for held branch |
| `routers/pipeline.py` (backfill) | `routers/pipeline.py` | reuses `_route_discovered_by_duration` | ✓ VERIFIED | L664 — verbatim reuse |
| `tasks/release_awaiting_cloud.py` | `services/enqueue_router.py` | `select_active_agent(session, kind='compute')` | ✓ VERIFIED | L73 |
| `tasks/release_awaiting_cloud.py` | `services/analysis_enqueue.py` | `enqueue_process_file` onto compute queue | ✓ VERIFIED | L81 |
| `tasks/controller.py` | `tasks/release_awaiting_cloud.py` | CronJob registration | ✓ VERIFIED | L41 import, L212 functions, L232 CronJob |
| `tasks/reenqueue.py` (CR-01) | `services/enqueue_router.py` | `select_active_agent(session, kind="compute")` for held partition | ✓ VERIFIED | L323 — held rows get compute-only routing; non-held rows stay kind-agnostic at L337 |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| All Plan 01 primitives (config, state, selectors, helpers) | `uv run pytest tests/test_config/ tests/test_services/test_enqueue_router.py tests/test_services/test_pipeline.py -q` | 130 passed | ✓ PASS |
| Plan 02/03 router + backfill + template tests | `uv run pytest tests/test_routers/test_pipeline.py tests/test_dag_canvas_render.py -q` | 123 passed | ✓ PASS |
| Plan 04 release cron + D-04 + CR-01 regression | `uv run pytest tests/test_tasks/test_release_awaiting_cloud.py tests/test_tasks/test_recovery.py -q` | 41 passed | ✓ PASS |
| Import boundary (controller does not bleed into agent worker) | `uv run pytest tests/test_task_split.py tests/test_tasks/test_controller_reenqueue.py -q` | 14 passed | ✓ PASS |
| Full suite | `uv run pytest tests/ -q --ignore=tests/test_migrations` | 2078 passed, 40 warnings (pre-existing coroutine warnings in test_tracklist.py, unrelated to Phase 49) | ✓ PASS |
| Ruff lint | `uv run ruff check src/phaze tests` | All checks passed | ✓ PASS |
| Mypy type-check | `uv run mypy src/phaze` | Success: no issues found in 137 source files | ✓ PASS |
| No Alembic migration added | `find src -name "*.py" -path "*/migrations/*"` | 0 files (AWAITING_CLOUD confirmed code-only over String(30)) | ✓ PASS |

### Requirements Coverage

| Requirement | Source Plans | Description | Status | Evidence |
|-------------|-------------|-------------|--------|---------|
| CLOUDROUTE-01 | 49-01, 49-02 | Files ≥ threshold routed to compute agent's queue | ✓ SATISFIED | `_route_discovered_by_duration` routes `duration >= threshold_sec` files to compute queue; test `test_analyze_long_file_routes_to_compute_queue` confirms capture on compute queue not fileserver |
| CLOUDROUTE-02 | 49-02, 49-04, CR-01 | No compute agent → held in AWAITING_CLOUD, NEVER silently local; released by */5 cron | ✓ SATISFIED | AWAITING_CLOUD state committed before enqueues (L321-322); `release_awaiting_cloud` CronJob(*/5); `recover_orphaned_work` CR-01 fix routes held ledger rows to compute-only (L304-332) |
| CLOUDROUTE-03 | 49-01, 49-02 | Short/null files continue on local fileserver with unchanged behavior | ✓ SATISFIED | `duration is None or < threshold` branch enqueues via `fileserver_q` with same `enqueue_process_file` call and deterministic key |
| CLOUDROUTE-04 | 49-01, 49-03 | Operator can backfill existing timed-out long files scoped through scheduling ledger; no whole-backlog over-enqueue | ✓ SATISFIED | POST /pipeline/backfill-cloud with explicit `ANALYSIS_FAILED ∧ duration>=threshold` filter; `insert_ledger_if_absent` for held files; double-click is structural no-op (candidates leave ANALYSIS_FAILED on first click) |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| None | — | No TBD/FIXME/XXX markers found in any modified source file | — | — |
| `routers/pipeline.py` | 609, 676-686 | `_held_backfill_ledger_payload` + `insert_ledger_if_absent` for held backfill files | ℹ Info | Backfill seeds a ledger row for AWAITING_CLOUD files (D-09). This was CR-01's concern. The fix (Option A, commit a55482d) makes `recover_orphaned_work` route these rows to compute-only, closing the CLOUDROUTE-02 violation. Not a stub — intentional design confirmed by 3 passing regression tests. |

### Human Verification Required

#### 1. WR-01: Trigger response copy when both agents absent and files were held

**Test:** With no fileserver agent and no compute agent online, seed several long (>=5400s) DISCOVERED files and click "Run Analysis". Observe the HTMX response fragment.
**Expected:** The response should ideally report the number of files held in AWAITING_CLOUD ("K awaiting cloud"). Currently the `{% if no_active_agent %}` branch in `trigger_response.html` takes precedence over `{% elif split_counts %}`, so the operator sees only "No files enqueued for analysis" — the held count is hidden until the next 5s `awaiting_cloud_card` poll.
**Why human:** Template branch precedence requires a live browser to observe. Automated tests confirm the split counts ARE passed in context, but the template renders the first-matching branch. This was identified as WR-01 in the code review and explicitly deferred as non-blocking (the "Awaiting cloud" card corrects the count within 5s). Functional safety invariant is not affected.

#### 2. Awaiting cloud card live update cadence

**Test:** Seed an AWAITING_CLOUD file in the DB and load the pipeline dashboard. Observe the "Awaiting cloud" card on first load and after the 5s pipeline/stats poll.
**Expected:** Card shows correct count on initial page load (inline include in dashboard.html), then re-renders with correct count via OOB swap on the 5s poll (stats_bar.html).
**Why human:** Unit tests assert `awaiting_cloud_count` is in context and templates include the card inline + OOB; the live HTMX polling cycle requires a browser with the running application.

### Gaps Summary

No blocking gaps. All 10 observable truths are VERIFIED, all required artifacts exist and are substantive and wired, all key links hold, the CR-01 fix is confirmed in `src/phaze/tasks/reenqueue.py` (commit a55482d), and the full test suite passes (2078 tests). Two human verification items remain (WR-01 cosmetic template display and live HTMX card cadence), consistent with the deferred-non-blocking classification in 49-REVIEW.md.

---

_Verified: 2026-06-25T21:30:00Z_
_Verifier: Claude (gsd-verifier)_
