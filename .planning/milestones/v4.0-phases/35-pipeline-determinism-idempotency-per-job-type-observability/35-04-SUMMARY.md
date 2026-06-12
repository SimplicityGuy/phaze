---
phase: 35-pipeline-determinism-idempotency-per-job-type-observability
plan: 04
subsystem: pipeline-observability
tags: [dashboard, dag, alpine-store, htmx-oob, reconcile, observability]

requires:
  - "35-01 read_counters (maintained enqueued/completed per-function Redis counters) + the completed backstop contract (D-02)"
  - "35-03 get_stage_progress(session) — per-DAG-node DB-truth done/total (D-03)"
  - "existing get_queue_activity(app_state, session) — agent_active/controller_active per-node ACTIVE signal"
  - "Phase-34 $store.pipeline + 5s /pipeline/stats OOB-swap seed mechanism (stats_bar.html)"
provides:
  - "$store.pipeline extended with all 17 per-DAG-node sub-keys, every key seeded to 0 (base.html)"
  - "routers/pipeline._build_dag_context — reconciles get_stage_progress + read_counters + get_queue_activity into the per-node store-key map; both dashboard() and pipeline_stats_partial() carry it"
  - "stats_bar.html OOB x-init seed paragraphs (id=dag-seed-<storeKey>) re-pushing per-node counts on every 5s poll"
  - "The store-key + OOB-seed-id contract for 35-05 to mirror in the static full-page canvas seeds"
affects:
  - "35-05 DAG canvas (consumes the store keys + dashboard context; mirrors the dag-seed ids for in-place full-page seeding)"

tech-stack:
  added: []
  patterns:
    - "Per-node reconcile-on-read: DB-truth (get_stage_progress) is authority; the maintained completed counter is a DOCUMENTED degrade-fallback applied only when DB done==0 AND completed>0 (D-02 satisfied, D-03 authority preserved)"
    - "Counter-source failure isolation mirroring get_queue_activity: _read_pipeline_counters degrades to {} on a missing handle / Redis hiccup so the 5s poll never 500s (T-35-09)"
    - "DRY Jinja OOB seed loop: one hx-swap-oob x-init paragraph emitted per per-node store key (id=dag-seed-<key>), gated behind oob_counts so it fires only on the poll swap"

key-files:
  created:
    - tests/test_pipeline_dag_context.py
  modified:
    - src/phaze/templates/base.html
    - src/phaze/routers/pipeline.py
    - src/phaze/templates/pipeline/partials/stats_bar.html
    - tests/test_routers/test_pipeline_scans.py

decisions:
  - "_NODE_COMPLETED_FNS maps each DB-sourced node to its completed-counter function(s); scan_search sums scan_live_set+search_tracklist; discovery/execute have no maintained counter (key-exempt tasks) so never fall back"
  - "totals coerced to int (None em-dash sentinels -> 0); Scan/Search keeps NO tracklistTotal store key, so its em-dash stays a 35-05 render-side concern (no fabricated denominator)"
  - "approved store key = execute.total (the approved-proposal count) — the Approve->Execute gate reads it"
  - "Preferred redis handle is app.state.redis (decode_responses); app.state.controller_queue.redis is the fallback. read_counters._to_int handles both bytes and str"
  - "Full-page actual-value seeding of the new keys is deferred to 35-05's canvas (in-place x-init using this plan's dashboard context); 35-04 guarantees store defaults=0 + both contexts carry values + OOB poll seeds"

requirements-completed: [OBSERV]

metrics:
  duration: "~35m"
  completed: "2026-06-12"
  tasks: 2
  files: 5
---

# Phase 35 Plan 04: Per-Job-Type Observability Data Plumbing Summary

**Extended `$store.pipeline` with all 17 per-DAG-node sub-keys (every key seeded to 0), and plumbed per-node DB-truth `done`/`total`/`active` into both the `dashboard()` full-page and `pipeline_stats_partial()` 5s-poll contexts — reconciling `get_stage_progress` (DB authority) with the maintained `completed` counter (documented degrade backstop) and `get_queue_activity`, re-pushed every poll via `id=dag-seed-<storeKey>` OOB x-init seeds, with counter-source failure isolation so the poll never 500s.**

## What Was Built

### Task 1 — `$store.pipeline` extension (`src/phaze/templates/base.html`)
The `Alpine.store('pipeline', {...})` literal now registers all 17 per-node sub-keys alongside the preserved Phase-34 keys, every value seeded to `0` so no node binding reads `undefined` before the first poll tick. The Phase-34 gating keys (`discovered`, `analyzed`, `metadataExtracted`, `agentBusy`, `controllerBusy`) are untouched.

### Task 2 — Router context + OOB seeds (`routers/pipeline.py`, `stats_bar.html`)
- `_build_dag_context(app_state, session, activity)` reconciles three sources into the per-node store-key map: `get_stage_progress` (DB-truth `done`/`total`, authority per D-03), `read_counters` (the `completed` counter as a degrade backstop via `_reconciled_done`, D-02), and the already-computed `get_queue_activity` (`agent_active` -> `analyzeActive`).
- `_read_pipeline_counters` isolates the counter read in try/except (degrade to `{}`) mirroring `get_queue_activity` — a missing `app.state` handle or Redis hiccup never 500s the poll (T-35-09).
- Both `dashboard()` and `pipeline_stats_partial()` now spread the `dag` context.
- `stats_bar.html` emits one hidden `hx-swap-oob` `x-init` paragraph per store key (`id=dag-seed-<key>`), gated behind `oob_counts` so they fire ONLY on the poll swap.

## Contract for 35-05 (mirror these EXACTLY)

### Per-node `$store.pipeline` store keys (17) — all seeded to 0 in base.html
```
metadataDone   metadataTotal
fingerprintDone fingerprintTotal
analyzeDone    analyzeTotal   analyzeActive
tracklistDone
scrapeDone     scrapeTotal
matchDone      matchTotal
proposalsDone  proposalsTotal
approved
executedDone   executedTotal
```
(Plus the preserved Phase-34 keys: `discovered`, `analyzed`, `metadataExtracted`, `agentBusy`, `controllerBusy`.)

### OOB seed ids (emitted on the 5s poll, gated behind `oob_counts`)
One per store key, `id="dag-seed-<storeKey>"` with `x-init="$store.pipeline.<storeKey> = <int>"`:
```
dag-seed-metadataDone     dag-seed-metadataTotal
dag-seed-fingerprintDone  dag-seed-fingerprintTotal
dag-seed-analyzeDone      dag-seed-analyzeTotal   dag-seed-analyzeActive
dag-seed-tracklistDone
dag-seed-scrapeDone       dag-seed-scrapeTotal
dag-seed-matchDone        dag-seed-matchTotal
dag-seed-proposalsDone    dag-seed-proposalsTotal
dag-seed-approved
dag-seed-executedDone     dag-seed-executedTotal
```
The 35-05 full-page canvas seeds the SAME keys in-place (non-OOB) from the `dag` dashboard context so bindings are correct before the first poll.

### Source-of-truth mapping (which value feeds which key)
| Store key | Source |
|-----------|--------|
| `metadataDone` / `metadataTotal` | `get_stage_progress["metadata"]` (done via completed-backstop) / `total` |
| `fingerprintDone` / `fingerprintTotal` | `get_stage_progress["fingerprint"]` / `total` |
| `analyzeDone` / `analyzeTotal` | `get_stage_progress["analyze"]` / `total` |
| `analyzeActive` | `get_queue_activity["agent_active"]` |
| `tracklistDone` | `get_stage_progress["scan_search"]["done"]` (Scan/Search has NO total store key — em-dash is render-side) |
| `scrapeDone` / `scrapeTotal` | `get_stage_progress["scrape"]` |
| `matchDone` / `matchTotal` | `get_stage_progress["match"]` |
| `proposalsDone` / `proposalsTotal` | `get_stage_progress["proposals"]` |
| `approved` | `get_stage_progress["execute"]["total"]` (approved-proposal count) |
| `executedDone` / `executedTotal` | `get_stage_progress["execute"]` |

## Verification

- `uv run pytest tests/test_pipeline_dag_context.py` — 11 passed (store extension text + DB-truth done + completed-fallback + DB-wins + never-500 + OOB-seed + dashboard-200)
- `uv run pytest tests/test_routers/ tests/test_services/ tests/test_pipeline_dag_context.py tests/test_pipeline_counters.py tests/test_stage_progress.py` — 981 passed after the one contract-update fix (see Deviations)
- `uv run mypy .` — clean (150 source files); `uv run ruff check .` — clean
- Coverage `--cov=phaze.routers.pipeline` over the pipeline test set — **90%**; the new `_build_dag_context` / `_read_pipeline_counters` / `_reconciled_done` are fully exercised (uncovered lines are unrelated trigger endpoints)
- Acceptance greps: `grep -c get_stage_progress src/phaze/routers/pipeline.py` = 5 (≥1); `grep -c hx-swap-oob stats_bar.html` rose 5 -> 10 (the per-node loop), rendered poll body carries ≥17 `hx-swap-oob` seeds

Integration tests run against the ephemeral `just test-db` Postgres (5433) + Redis (6380); they are auto-marked `integration` and also pass in CI.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated `test_dashboard_seeds_pipeline_store_from_server_count` to the extended store literal**
- **Found during:** Task 2 broader-suite verification
- **Issue:** `tests/test_routers/test_pipeline_scans.py:1200` asserted the EXACT old single-line `Alpine.store('pipeline', { discovered: 0, ... controllerBusy: 0 })` literal. The planned 35-04 store extension reformats it to a multi-line literal with the new per-node keys, so the substring assertion broke.
- **Fix:** Replaced the exact-literal assertion with three substring checks: the store is registered (`Alpine.store('pipeline', {`), the Phase-34 keys are preserved (the original key line), and a sample new key (`analyzeActive: 0`) is seeded. The in-place `x-init` seed assertions for `discovered`/`analyzed` are unchanged.
- **Files modified:** tests/test_routers/test_pipeline_scans.py
- **Verification:** `uv run pytest tests/test_routers/test_pipeline_scans.py` — 55 passed
- **Committed in:** ccf6a01

**Total deviations:** 1 auto-fixed (Rule 1 — a mandatory consequence of the planned store-literal reformat). No production-behavior scope creep.

## Known Stubs

None — the per-node values are wired end to end from `get_stage_progress` (DB-truth). The full-page in-place seeding of the new keys (the visible DAG nodes) is the explicit scope of 35-05; this plan delivers the store defaults + both router contexts + the OOB poll seeds, which is the agreed data-plumbing boundary.

## Threat Flags

None — read-only `COUNT`/counter reads plumbed into server-computed integers rendered through Jinja autoescape into `x-init` numeric assignments (no user-controlled string reaches the template; T-35-11). The counter read is failure-isolated (T-35-09). No new endpoints, auth paths, or schema changes.

## Self-Check: PASSED

- FOUND: src/phaze/templates/base.html (store extended, all keys seeded to 0)
- FOUND: src/phaze/routers/pipeline.py (`_build_dag_context` + `get_stage_progress`/`read_counters` wired)
- FOUND: src/phaze/templates/pipeline/partials/stats_bar.html (dag-seed-<key> OOB loop)
- FOUND: tests/test_pipeline_dag_context.py
- FOUND commits: 5993497 (Task 1 store), 4945da2 (Task 2 context + seeds + tests), ccf6a01 (Rule 1 test-contract fix)
