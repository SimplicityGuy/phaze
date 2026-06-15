---
phase: 42-recovery-only-pipeline-automation
plan: 01
subsystem: pipeline-recovery
tags: [saq, recovery, postgres-broker, idempotency, anti-drift]
requires:
  - services/pipeline.py pending-set + busy helpers (Phase 39-41)
  - services/analysis_enqueue.enqueue_process_file (keyed producer)
  - services/enqueue_router.select_active_agent / NoActiveAgentError
  - tasks/_shared/deterministic_key.apply_deterministic_key (before_enqueue chokepoint)
provides:
  - services/pipeline.get_metadata_pending_files / get_fingerprint_pending_files / get_untracked_files / get_proposal_pending_batches
  - services/pipeline.count_inflight_jobs (saq_jobs queue-loss detector)
  - tasks/reenqueue.recover_orphaned_work(ctx, *, force=False) -> dict (all-stages recovery producer)
affects:
  - routers/pipeline.py (8 trigger endpoints now consume the shared pending-set helpers)
tech-stack:
  added: []
  patterns:
    - "One-source-of-truth pending-set helpers shared by manual triggers + recovery (D-03 anti-drift)"
    - "Deterministic sorted proposal batching so the set-hash key aligns across paths (Pitfall 2)"
    - "Degrade-safe SAVEPOINT saq_jobs reads (never raise into boot/poll)"
key-files:
  created:
    - tests/test_tasks/test_recovery.py
  modified:
    - src/phaze/services/pipeline.py
    - src/phaze/routers/pipeline.py
    - src/phaze/tasks/reenqueue.py
    - tests/test_services/test_pipeline.py
    - tests/test_routers/test_pipeline.py
decisions:
  - "Retained reenqueue_discovered (its removal + cron/registration changes are 42-02 controller wiring)"
  - "Stage labels in the recovery result: analyze/metadata/fingerprint/scan_live_set + search/scrape/match/proposals"
metrics:
  duration: ~50m
  completed: 2026-06-14
  tasks: 2
  commits: 2
---

# Phase 42 Plan 01: Backend Recovery Engine Summary

Built the gated, all-stages, idempotent `recover_orphaned_work(ctx, *, force=False)` producer plus the shared pending-set service helpers that guarantee recovery and the Phase 39-41 manual DAG triggers read ONE definition of "pending" (D-03 anti-drift). Wave 1 = the producer + helpers + their tests only; controller wiring (cron removal, startup hook, the `/pipeline/recover` endpoint, the DAG Recover button) is deferred to Wave 2 (42-02).

## What shipped

**Task 1 — shared pending-set helpers + queue-loss detector (commit `c730ce3`)**
- `services/pipeline.py`: `get_metadata_pending_files`, `get_fingerprint_pending_files` (METADATA_EXTRACTED ∪ failed-retry, deduped by id), `get_untracked_files`, `get_proposal_pending_batches` (convergence query → **sorted** ids → chunked so the `generate_proposals:<sha256(sorted ids)>` set-hash key is deterministic and matches between manual + recovery — closes 42-RESEARCH Pitfall 2).
- `services/pipeline.count_inflight_jobs(session)`: static-SQL `SELECT COUNT(*) FROM saq_jobs WHERE status IN ('queued','active')` inside a `begin_nested()` SAVEPOINT, degrades to 0 on any error (never raises). Parked/paused rows (status still `queued`) ARE counted, so a paused queue is not misread as lost (Open Q4).
- Refactored 8 router endpoints (`trigger_metadata_extraction`, `trigger_extraction_ui`, `trigger_fingerprint`, `trigger_fingerprint_ui`, `trigger_search_ui`, `trigger_scan_live_sets_ui`, `trigger_proposals`, `trigger_proposals_ui`) to consume the helpers — identical enqueues, one source of truth.
- **Intended behavior change (locked by a new test):** `trigger_fingerprint_ui` now routes through `get_fingerprint_pending_files`, so it GAINS the failed-fingerprint-retry scope (aligns the HTMX endpoint with the API endpoint + recovery).

**Task 2 — recovery producer + detector gate (commit `b458c39`)**
- `tasks/reenqueue.recover_orphaned_work(ctx, *, force=False) -> dict`: DETECT gate (no-op when `count_inflight_jobs > 0` and not forced) → RECONCILE all 8 stages through the IDENTICAL keyed producers the manual triggers use. Controller stages route to `ctx["queue"]`; agent stages select the active agent once and route to `ctx["task_router"].queue_for(agent.id)` (never the controller queue — Pitfall 1), skipping all agent stages on `NoActiveAgentError` (cold boot, D-05). `None` producer returns (deterministic-key dedup) count as `skipped`. `force=True` bypasses ONLY the no-op gate, never the per-item dedup.
- Return shape consumed by 42-02: `{"detected_loss": bool, "forced": bool, "stages": {<stage>: {"reenqueued": N, "skipped": M}}}`.
- Module docstring rewritten to record the Postgres-broker durability reframe.

## Tests

- `tests/test_services/test_pipeline.py`: per-helper membership, fingerprint dedup, deterministic sorted proposal batching, convergence exclusion, and `count_inflight_jobs` happy/degrade/no-poison.
- `tests/test_routers/test_pipeline.py`: new `test_trigger_fingerprint_ui_enqueues_failed_retry_file` locks the intended alignment; existing assertions unchanged (no proposal-batch composition was asserted, so deterministic sorting needed no edits).
- `tests/test_tasks/test_recovery.py` (new): no-op-on-durable-restart, all-stages reconcile (correct queue + task-name/key-prefix per stage), idempotent dedup (half skipped), agent-skip with WARNING while controller stages reconcile, force-bypasses-gate-not-dedup, plus a self-contained `@pytest.mark.integration` real-`saq_jobs` detector test.

Result: `uv run pytest tests/test_tasks tests/test_services/test_pipeline.py tests/test_routers/test_pipeline.py -q` → **250 passed**. `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .` all clean.

## Deviations from Plan

### 1. [Rule 3 — Blocking-issue scope boundary] Retained `reenqueue_discovered` instead of removing it
- **Found during:** Task 2.
- **Issue:** The plan's Task 2 says "REMOVE `reenqueue_discovered`", and its verify step greps for its absence. But `src/phaze/tasks/controller.py` still imports and registers it (lines 40, 114, 173, 185). Removing the function would break `controller.py`'s import and fail collection for every test module that imports the controller — and the **dispatch explicitly scopes** controller wiring (cron removal, startup hook, registration) to Wave 2 (42-02): "Do NOT remove the cron or touch controller wiring … This wave is the producer + helpers + their tests only."
- **Resolution:** Kept `reenqueue_discovered` in place and ADDED `recover_orphaned_work` alongside it. The module docstring documents that the legacy function is retained ONLY until 42-02 drops its registration + cron and re-points the startup hook. Wave 1 passes its full scoped suite independently; 42-02 will perform the removal together with the controller rewire (where the plan's grep assertion belongs).
- **Files:** `src/phaze/tasks/reenqueue.py` (function retained; new producer added).
- **Commit:** `b458c39`.

### 2. [Test-strategy adaptation] "exactly one enqueue per stage" → exact deterministic per-stage tallies
- **Found during:** Task 2 test authoring.
- **Issue:** The plan's all-stages test wording ("exactly one keyed enqueue per stage") is not literally achievable because two helpers overlap by design: `get_metadata_pending_files` returns ALL music/video files and `get_untracked_files` returns ALL untracked music/video files. A single seeded mp3 therefore appears in metadata + search + scan simultaneously.
- **Resolution:** The test seeds a crafted set (3 mp3 files in distinct states + one bare tracklist) and asserts the EXACT deterministic per-stage tallies (metadata/search/scan = 3; analyze/fingerprint/proposals/scrape/match = 1) plus the correct queue + task-name (= deterministic key prefix) per stage. This proves all-stages-reconcile-on-correct-queue faithfully; the overlap is documented in the test.
- **Files:** `tests/test_tasks/test_recovery.py`.
- **Commit:** `b458c39`.

### 3. [Environment] Integration detector test is self-contained (not via `stage_env`)
- **Found during:** Task 2 test run (`fixture 'stage_env' not found`).
- **Issue:** The plan suggested the integration test use the `stage_env` fixture, but that fixture lives in `tests/integration/conftest.py` and is out of reach from `tests/test_tasks/`.
- **Resolution:** Made the `@pytest.mark.integration` test self-contained, mirroring the proven `test_reenqueue.test_real_broker_dedup_returns_none` pattern (probe Postgres, build a real `PostgresQueue` via `AgentTaskRouter`, enqueue a keyed `process_file`, assert `count_inflight_jobs` rises ≥1, clean up). Skips when Postgres is unavailable.
- **Files:** `tests/test_tasks/test_recovery.py`.
- **Commit:** `b458c39`.

## Notes for Wave 2 (42-01 → 42-02 handoff)

- `recover_orphaned_work(ctx, *, force=False)` is ready to wire: it reads `ctx["async_session"]`, `ctx["queue"]` (controller PostgresQueue), `ctx["task_router"]` (cached `AgentTaskRouter`).
- Stage keys in the result dict: `analyze`, `metadata`, `fingerprint`, `scan_live_set`, `search`, `scrape`, `match`, `proposals`.
- 42-02 must: remove `reenqueue_discovered` (function + the `from phaze.tasks.reenqueue import reenqueue_discovered` import + the `functions` entry + the `*/5` `CronJob`), re-point the controller `startup` hook from `reenqueue_discovered(ctx)` to `await recover_orphaned_work(ctx)` (keep the broad try/except), update `tests/test_tasks/test_controller_reenqueue.py` + `test_reenqueue.py` accordingly, and add the `/pipeline/recover` endpoint + DAG Recover button calling `recover_orphaned_work(ctx, force=True)`.

## Self-Check: PASSED
- FOUND: tests/test_tasks/test_recovery.py, src/phaze/tasks/reenqueue.py, src/phaze/services/pipeline.py, src/phaze/routers/pipeline.py
- FOUND commits: c730ce3, b458c39
