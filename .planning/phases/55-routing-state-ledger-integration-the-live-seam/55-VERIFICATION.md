---
phase: 55-routing-state-ledger-integration-the-live-seam
verified: 2026-06-28T00:00:00Z
status: human_needed
score: 5/5 must-haves verified
overrides_applied: 0
human_verification:
  - test: "Inspect the pipeline dashboard with cloud_target='k8s' and at least one file in each cloud_phase (queued_behind_quota/admitted/running/finished). Confirm the admission_state_card renders the four hue-coded tiles (gray/blue/violet/green), the heading reads 'Cloud · Admission', the per-tile sub-labels match the Copywriting Contract, and no amber or role=alert appears."
    expected: "Carrier section always present; four tiles visible with correct colors and labels; all-zero reverts to a quiet empty section."
    why_human: "Visual template rendering cannot be verified by grep; color/hue discrimination and layout require a browser."
  - test: "With cloud_target='k8s' and a real or simulated Kueue cluster, run a long file (duration >= cloud_route_threshold_sec) through the full path: AWAITING_CLOUD -> PUSHING (stage_cloud_window k8s branch) -> PUSHED (report_uploaded) -> submit_cloud_job enqueued -> reconcile_cloud_jobs writes cloud_phase progression -> ANALYSIS_FAILED (or SUCCEEDED via out-of-band callback)."
    expected: "File traverses all states; cloud_phase advances queued_behind_quota -> admitted -> running -> finished (success) or -> None (failure at cap); window count never exceeds cloud_max_in_flight; process_file ledger row NOT seeded."
    why_human: "Requires a live Kueue cluster (or a heavily-instrumented integration test environment) and actual kube API interactions. Cannot be validated without live infrastructure."
  - test: "Trigger 'Backfill to K8s' (POST /pipeline/backfill-cloud with cloud_target='k8s') on a set of ANALYSIS_FAILED long files, some with a prior scheduling_ledger row and some without. Confirm only the ledger-tracked files reset to DISCOVERED and reach AWAITING_CLOUD; the never-scheduled files are untouched."
    expected: "Ledger-scoped files: state resets to DISCOVERED, no process_file ledger row seeded. Never-scheduled files: state unchanged. Operator sees accurate count in the backfill response partial."
    why_human: "Integration behavior across DB state + HTMX partial response requires a running app with test data; not fully exercised by unit tests."
---

# Phase 55: Routing, State & Ledger Integration (The Live Seam) — Verification Report

**Phase Goal:** K8s becomes the third cloud target selected by a single config setting (`cloud_target`), wired into the existing duration router / `stage_cloud_window` / scheduling ledger as ONE new branch — the only phase that touches the live v5.0 seam.
**Verified:** 2026-06-28
**Status:** human_needed
**Re-verification:** No — initial verification

## Reconciliation Note (ROADMAP SC1 vs D-02)

ROADMAP Success Criterion 1 contains stale wording ("under the existing `cloud_burst_enabled` toggle"). CONTEXT decision D-02 (the authoritative post-planning decision) superseded this with a HARD-REPLACE: `cloud_burst_enabled` was removed entirely, and `cloud_target: Literal["local","a1","k8s"]` (default `"local"` == cloud off) is the single selector. The `important_context` note explicitly instructs verification against D-02, not the ROADMAP wording. This report verifies against D-02.

## Goal Achievement

### Observable Truths (ROADMAP Success Criteria)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| SC1 / KROUTE-01 | `cloud_target: Literal["local","a1","k8s"]` (default `"local"`) is the single routing source of truth; `cloud_burst_enabled` is gone everywhere in `src/phaze/` and `tests/` | VERIFIED | `src/phaze/config.py:406-410` defines the field + 3 per-target validators; `grep -rn cloud_burst_enabled src/phaze/ tests/` returns empty; `test_cloud_target.py` covers all 6 behaviors |
| SC2 / KROUTE-02 | K8s offload is a SINGLE new branch inside the existing `stage_cloud_window` advisory-locked loop, reusing window math + AWAITING_CLOUD hold; `report_uploaded` flips PUSHING→PUSHED and enqueues via `enqueue_router` | VERIFIED | `release_awaiting_cloud.py:177-190` forks on `cfg.cloud_target == "k8s"`, calls no-commit `_stage_file_to_s3`, single commit at line 190; `agent_s3.py:108-136` rowcount-guarded PUSHING→PUSHED flip + `resolve_queue_for_task("submit_cloud_job", ...)` |
| SC3 / KROUTE-03 | K8s files reuse PUSHING/PUSHED (no new FileRecord state); `cloud_phase` column on `cloud_job` sidecar via additive migration 027; FileRecord state machine unchanged | VERIFIED | `alembic/versions/027_add_cloud_job_cloud_phase.py` additive+reversible; `models/cloud_job.py:49-62` CloudPhase StrEnum + `cloud_phase: Mapped[str | None]`; submit seeds QUEUED_BEHIND_QUOTA; reconcile co-writes progression; inadmissible branch leaves cloud_phase untouched |
| SC4 / KROUTE-04 | Static AST guard asserts every k8s enqueue site routes through `enqueue_router`; no consumer-less default-queue enqueue; backfill is ledger-scoped not whole-backlog | VERIFIED | `test_no_default_queue_producers.py:235-271` adds `test_submit_cloud_job_is_a_routed_controller_task`, `test_submit_cloud_job_routes_to_controller_queue`, and `test_k8s_backfill_query_is_ledger_scoped_not_whole_backlog` (static AST check of `_backfill_candidates_stmt` source) |
| SC5 / KROUTE-05 | Backfill re-drives only `ANALYSIS_FAILED AND duration >= threshold AND EXISTS scheduling_ledger row`; k8s branch skips process_file ledger seed | VERIFIED | `services/pipeline.py:971-979` `_backfill_candidates_stmt` has EXISTS predicate on `SchedulingLedger.key == "process_file:" + cast(FileRecord.id, String)`; `routers/pipeline.py:744-754` k8s branch returns without calling `insert_ledger_if_absent` |
| KROUTE-06 | Pipeline dashboard admission-state cards driven by `cloud_job.cloud_phase`, refreshed by 5s OOB poll | VERIFIED | `get_cloud_phase_counts` in `services/pipeline.py:844-876` (_safe_count per phase); `admission_state_card.html` with 4 tiles, no role=alert, no amber; mounted in `dashboard.html:41` OUTSIDE #pipeline-stats; OOB re-push in `stats_bar.html:97` inside oob_counts block; both dashboard() and pipeline_stats_partial() seed all 4 count keys |

**Score:** 5/5 roadmap truths verified (plus KROUTE-06 delivered in 55-05)

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/config.py` | `cloud_target Literal` field + 3 per-target validators | VERIFIED | Field at :406-410; `_enforce_s3_config_when_k8s` (:601-619), `_enforce_compute_scratch_dir_when_a1` (:621-640), `_enforce_kube_config_when_k8s` (:642-666) |
| `tests/test_config/test_cloud_target.py` | 6+ behavior unit tests; replaces `test_cloud_burst_toggle.py` | VERIFIED | 9 test cases covering all 6 plan-specified behaviors + extras; `test_cloud_burst_toggle.py` confirmed absent |
| `alembic/versions/027_add_cloud_job_cloud_phase.py` | Additive/reversible cloud_phase column + CHECK constraint; cloud_job-only | VERIFIED | `revision="027"`, `down_revision="026"`; adds nullable `cloud_phase String(20)` + `check_constraint("cloud_phase_enum", ...)`; zero `saq_jobs` references |
| `src/phaze/models/cloud_job.py` | CloudPhase StrEnum + cloud_phase Mapped column + CheckConstraint | VERIFIED | `class CloudPhase(enum.StrEnum)` at :49-62; `cloud_phase: Mapped[str \| None]` at :91; `CheckConstraint(..., name="cloud_phase_enum")` at :98-101 |
| `src/phaze/services/cloud_staging.py` | `_stage_file_to_s3` no-commit core; public wrapper preserves commit | VERIFIED | `async def _stage_file_to_s3` at :71 (no commit); `stage_file_to_s3` at :52 delegates then `await session.commit()` |
| `src/phaze/tasks/release_awaiting_cloud.py` | k8s branch in `stage_cloud_window`; GATE-1 skipped; single post-loop commit | VERIFIED | `cfg.cloud_target == "k8s"` gate at :177 calls `_stage_file_to_s3`; GATE-1 conditional on `cfg.cloud_target == "a1"` at :142; single commit at :190 |
| `src/phaze/routers/agent_s3.py` | `report_uploaded` with `request: Request`; PUSHING→PUSHED flip; routed `submit_cloud_job` enqueue; CR-01 fix in `report_upload_failed` | VERIFIED | Signature at :60-66 includes `request: Request`; PUSHING→PUSHED rowcount-guarded at :115-124; `resolve_queue_for_task("submit_cloud_job", ...)` at :128-129; CR-01 fix at :181; WR-01 `cloud_phase=None` at :180 |
| `src/phaze/tasks/submit_cloud_job.py` | Seeds `cloud_phase=QUEUED_BEHIND_QUOTA` in both values and on_conflict set_ | VERIFIED | `cloud_phase=CloudPhase.QUEUED_BEHIND_QUOTA.value` at :89 (values) and `"cloud_phase": stmt.excluded.cloud_phase` at :98 (set_) |
| `src/phaze/tasks/reconcile_cloud_jobs.py` | Co-writes cloud_phase per admission condition; WR-01 fix clears on terminal | VERIFIED | `_record_success` :134 → FINISHED; healthy-Pending :254 → QUEUED_BEHIND_QUOTA; admitted/running :270-274 → ADMITTED or RUNNING; terminal cap :167 → None (WR-01 fix) |
| `src/phaze/services/pipeline.py` | `_backfill_candidates_stmt` with EXISTS predicate; `get_cloud_phase_counts` degrade-safe | VERIFIED | EXISTS at :977; `get_cloud_phase_counts` at :844 with `_safe_count` per CloudPhase member |
| `src/phaze/routers/pipeline.py` | k8s backfill fork skips ledger seed; cloud_target gates seeded in both contexts | VERIFIED | k8s branch at :744-754 returns without `insert_ledger_if_absent`; 4 cloud_phase counts seeded in dashboard() at :527-530 and pipeline_stats_partial() at :599-602 |
| `tests/test_no_default_queue_producers.py` | KROUTE-04 AST guard: submit_cloud_job as CONTROLLER_TASK + no-whole-backlog assertion | VERIFIED | `submit_cloud_job` in CONTROLLER_TASKS asserted at :242; routes to controller queue at :250; `test_k8s_backfill_query_is_ledger_scoped_not_whole_backlog` AST-checks `_backfill_candidates_stmt` source at :264-270 |
| `src/phaze/templates/pipeline/partials/admission_state_card.html` | Carrier-always / body-conditional; 4 phase tiles; no role=alert; no amber | VERIFIED | Carrier `<section id="admission-state-card">` always emitted; outer `{% if ... %}` gates heading+grid; 4 per-tile blocks; no role=alert (grep returns 0); no amber (grep returns 0); finished tile uses green; `hx-swap-oob` gated on `{% if oob %}` |
| `src/phaze/templates/pipeline/dashboard.html` | Admission card mounted OUTSIDE #pipeline-stats | VERIFIED | Include at :41, before the `<div id="pipeline-stats">` at :45 |
| `src/phaze/templates/pipeline/partials/stats_bar.html` | Admission card OOB re-push inside oob_counts block | VERIFIED | `{% with oob = True %}{% include "pipeline/partials/admission_state_card.html" %}{% endwith %}` at :97, inside `{% if oob_counts %}` block |
| `.env.example` | PHAZE_CLOUD_TARGET documented; loud rename callout; k8s knobs listed | VERIFIED | `PHAZE_CLOUD_TARGET=local` at :183; "PHAZE_CLOUD_BURST_ENABLED is removed" callout at :171-174; k8s/S3/_FILE knobs documented |
| `docker-compose.yml` | PHAZE_CLOUD_TARGET in api and worker env; NOT in agent compose files | VERIFIED | `PHAZE_CLOUD_TARGET=${PHAZE_CLOUD_TARGET:-local}` in api (line 25) and worker (line 52); zero in docker-compose.agent.yml and docker-compose.cloud-agent.yml |
| `docs/cloud-burst.md` | cloud_target runbook prose (local/a1/k8s); k8s setup knobs documented | VERIFIED | PHAZE_CLOUD_TARGET present at multiple locations including k8s setup section; "PHAZE_CLOUD_BURST_ENABLED is removed" callout |
| `docs/configuration.md` | cloud_target master selector table row; k8s required-field rows | VERIFIED | `cloud_target` row at :89 with three-value description; kube fields documented as required-when-k8s at :112-114 |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `release_awaiting_cloud.py` | `cfg.cloud_target == "local"` | master gate early return | WIRED | Line 126: `if cfg.cloud_target == "local": return {"staged": 0, "skipped": 0}` |
| `release_awaiting_cloud.py` | `_stage_file_to_s3` | k8s stage branch (no-commit core) | WIRED | Line 182: `await _stage_file_to_s3(session, file, task_router)` inside `cloud_target == "k8s"` branch |
| `routers/pipeline.py` | `settings.cloud_target != "local"` | duration-router cloud-on gate (2 sites) | WIRED | Lines 374, 638: `settings.cloud_target != "local"` passed as `cloud_enabled` arg |
| `routers/pipeline.py` | `settings.cloud_target == "local"` | backfill early-exit gate | WIRED | Line 702: `if settings.cloud_target == "local": return ...` |
| `routers/pipeline.py` | `settings.cloud_target == "k8s"` | backfill ledger-seed skip | WIRED | Line 744: `if settings.cloud_target == "k8s": return ...` (no `insert_ledger_if_absent`) |
| `routers/agent_s3.py` | `submit_cloud_job` | `resolve_queue_for_task` on controller queue | WIRED | Lines 128-129: `routed = await resolve_queue_for_task("submit_cloud_job", request.app.state, session); await routed.queue.enqueue(...)` |
| `submit_cloud_job.py` | `cloud_job.cloud_phase` | initial seed in pg_insert upsert | WIRED | Lines 89, 98: `CloudPhase.QUEUED_BEHIND_QUOTA.value` in both `.values(...)` and `on_conflict_do_update` `set_` |
| `reconcile_cloud_jobs.py` | `cloud_job.cloud_phase` | co-write per admission condition | WIRED | Lines 134 (FINISHED), 167 (None/terminal), 254 (QUEUED_BEHIND_QUOTA), 270-274 (ADMITTED/RUNNING) |
| `services/pipeline.py` | `scheduling_ledger` | EXISTS predicate in `_backfill_candidates_stmt` | WIRED | Line 977: `exists(select(SchedulingLedger.key).where(SchedulingLedger.key == "process_file:" + cast(FileRecord.id, String)))` |
| `routers/pipeline.py` | `admission_state_card.html` | cloud_phase counts seeded in both dashboard() and pipeline_stats_partial() | WIRED | Lines 527-530 (dashboard), 599-602 (stats partial) |
| `stats_bar.html` | `admission_state_card.html` | hx-swap-oob re-push on 5s poll | WIRED | Line 97: `{% with oob = True %}{% include "pipeline/partials/admission_state_card.html" %}{% endwith %}` inside oob_counts block |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|--------------------|--------|
| `admission_state_card.html` | `queued_behind_quota_count`, `admitted_count`, `running_count`, `finished_count` | `get_cloud_phase_counts` → `_safe_count` → `select(func.count(CloudJob.id)).where(CloudJob.cloud_phase == ...)` | Yes — live DB COUNT per phase; degrades to 0 on error | FLOWING |
| `stage_cloud_window` (k8s path) | `candidates` (AWAITING_CLOUD files) | `get_cloud_staging_candidates(session, slots)` → `SELECT ... FOR UPDATE SKIP LOCKED` | Yes — real DB rows | FLOWING |
| `_backfill_candidates_stmt` | ANALYSIS_FAILED long files with ledger row | JOIN FileMetadata + EXISTS scheduling_ledger | Yes — bounded real DB rows | FLOWING |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Zero `cloud_burst_enabled` in `src/phaze/` | `grep -rn cloud_burst_enabled src/phaze/` | empty | PASS |
| Zero `cloud_burst_enabled` in `tests/` | `grep -rn cloud_burst_enabled tests/` | empty | PASS |
| `cloud_target` Literal field exists in config.py | `grep -c 'cloud_target: Literal' src/phaze/config.py` | 1 | PASS |
| 3 per-target validators in config.py | `grep -c '@model_validator.*after' src/phaze/config.py` | verified via direct read | PASS |
| Migration 027 does not touch saq_jobs | `grep -c saq_jobs alembic/versions/027_add_cloud_job_cloud_phase.py` | 0 (only in comment) | PASS |
| `_stage_file_to_s3` no-commit core exists | `grep -c 'async def _stage_file_to_s3' src/phaze/services/cloud_staging.py` | 1 | PASS |
| `submit_cloud_job` in CONTROLLER_TASKS | confirmed via `enqueue_router.py` CONTROLLER_TASKS + test at line 242 | yes | PASS |
| EXISTS predicate in `_backfill_candidates_stmt` | `grep -c 'exists()' src/phaze/services/pipeline.py` within function | 1 | PASS |
| k8s backfill skips ledger seed | `grep -c 'cloud_target == "k8s"' src/phaze/routers/pipeline.py` gating insert_ledger_if_absent | verified via direct read | PASS |
| Admission card not inside #pipeline-stats | admission_state_card include at dashboard.html:41 (before `<div id="pipeline-stats">` at :45) | PASS | PASS |
| No `role="alert"` in admission card | `grep -c 'role="alert"' admission_state_card.html` | 0 | PASS |
| No amber hue in admission card | `grep -c 'amber' admission_state_card.html` | 0 | PASS |
| PHAZE_CLOUD_TARGET in .env.example | `grep -c PHAZE_CLOUD_TARGET .env.example` | ≥1 | PASS |
| PHAZE_CLOUD_TARGET in docker-compose.yml | `grep -c PHAZE_CLOUD_TARGET docker-compose.yml` | 2 (api + worker) | PASS |
| PHAZE_CLOUD_TARGET not in agent compose files | `grep -c PHAZE_CLOUD_TARGET docker-compose.agent.yml docker-compose.cloud-agent.yml` | 0 | PASS |
| CR-01 fix: ANALYSIS_FAILED in report_upload_failed cap path | `agent_s3.py:181`: `update(FileRecord)...values(state=FileState.ANALYSIS_FAILED)` | present | PASS |
| WR-01 fix: cloud_phase=None in both terminal paths | `agent_s3.py:180`: `values(status=..., cloud_phase=None)` and `reconcile_cloud_jobs.py:167`: `cloud_job.cloud_phase = None` | both present | PASS |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|---------|
| KROUTE-01 | 55-01, 55-06 | Single `cloud_target` selector replaces `cloud_burst_enabled` | SATISFIED | `cloud_target: Literal["local","a1","k8s"]` in config.py; zero legacy references; docs migrated |
| KROUTE-02 | 55-03 | K8s offload as single branch in existing window/router/lock | SATISFIED | `stage_cloud_window` k8s branch; `report_uploaded` PUSHING→PUSHED + `submit_cloud_job` via `enqueue_router` |
| KROUTE-03 | 55-02, 55-03 | PUSHING/PUSHED reused; `cloud_phase` column on sidecar; FileRecord state machine unchanged | SATISFIED | Migration 027; CloudPhase enum; no new FileState members; reconcile co-writes progression |
| KROUTE-04 | 55-04 | Static AST guard for k8s enqueue sites and no-whole-backlog property | SATISFIED | `test_no_default_queue_producers.py` extended with 3 new assertions covering `submit_cloud_job` routing and `_backfill_candidates_stmt` ledger scope |
| KROUTE-05 | 55-04 | Ledger-scoped backfill of timed-out long files to k8s; k8s skips process_file ledger seed | SATISFIED | EXISTS predicate in `_backfill_candidates_stmt`; k8s backfill branch returns before `insert_ledger_if_absent` |
| KROUTE-06 | 55-05 | Dashboard admission-state cards driven by cloud_phase (listed as future req in REQUIREMENTS.md but delivered in 55-05) | SATISFIED | `get_cloud_phase_counts`; `admission_state_card.html`; OOB wiring; both router contexts seeded |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `src/phaze/tasks/release_awaiting_cloud.py` | 126, 142, 177 | `type: ignore[attr-defined]` comments on `cfg.cloud_target` accesses | Info | Expected: `cfg` is typed as `Settings` (a union) but the attribute lives on `ControlSettings`; these ignores are the established pattern from pre-existing cloud window code and not a new smell introduced by this phase |

No TBD, FIXME, or XXX debt markers found in phase-modified files.

### Human Verification Required

#### 1. Admission-State Card Visual Appearance

**Test:** With `PHAZE_CLOUD_TARGET=k8s` and seeded `cloud_job` rows in each `cloud_phase` value, load the pipeline dashboard and inspect the admission-state card.
**Expected:** Section carrier always present with id `admission-state-card`; four hue-coded tiles when any count > 0 (gray=queued, blue=admitted, violet=running, green=finished); heading "Cloud · Admission"; correct per-tile sub-labels; no amber/warning hue; no role=alert; all-zero renders as quiet empty section.
**Why human:** Template rendering, color/hue discrimination, and layout correctness cannot be verified by grep or static analysis.

#### 2. End-to-End K8s Routing with Live Kueue

**Test:** With a configured Kueue cluster (`cloud_target=k8s`, all required env vars set), ingest a long file (duration >= `cloud_route_threshold_sec`), trigger "Run Analysis", and observe the file traverse: AWAITING_CLOUD -> stage_cloud_window picks it up -> PUSHING (s3_upload enqueued) -> report_uploaded -> PUSHED -> submit_cloud_job enqueued -> reconcile_cloud_jobs advances cloud_phase through queued_behind_quota/admitted/running -> out-of-band callback or terminal cap -> ANALYSIS_FAILED or ANALYZED.
**Expected:** Window count never exceeds `cloud_max_in_flight`; no `process_file:<id>` ledger row seeded for the k8s file; `cloud_phase` visible in dashboard admission card during progression.
**Why human:** Requires live Kueue cluster, real kube API, and real S3-compatible object storage. All unit/integration tests use fakes (kube_fakes.py, moto).

#### 3. Ledger-Scoped Backfill Operator Flow

**Test:** Seed both ledger-tracked and never-scheduled `ANALYSIS_FAILED` long files; trigger "Backfill to K8s" via the UI; confirm only the ledger-tracked files move to DISCOVERED and AWAITING_CLOUD, and the backfill response partial shows the accurate count.
**Expected:** Never-scheduled files untouched; ledger-tracked files: state = DISCOVERED then AWAITING_CLOUD; zero new process_file ledger rows; HTMX response partial shows correct count.
**Why human:** Requires running app + test data; the HTMX partial response format needs browser/httpx inspection.

### Gaps Summary

No gaps. All 5 ROADMAP success criteria and KROUTE-06 are verified in the codebase. The CR-01 (blocker) and WR-01 (warning) from the code review are both confirmed fixed at `b169382`. WR-02 (S3 multipart orphan on mid-loop failure) is explicitly deferred and TTL-backstopped as documented in the REVIEW.

**Documented deviation (non-blocking):** Plan 01's acceptance criterion `grep -c 'cloud_target != "local"' src/phaze/routers/pipeline.py == 3` counts 2 at verification time. The third backfill call-site (line 733) passes literal `True` after the definitive `cloud_target == "local"` early-return guard at line 702. This is a deliberate, documented deviation (55-01-SUMMARY key-decisions: "mypy strict_equality narrows cloud_target to non-local after the == 'local' early-return") and is behaviorally equivalent.

---

_Verified: 2026-06-28_
_Verifier: Claude (gsd-verifier)_
