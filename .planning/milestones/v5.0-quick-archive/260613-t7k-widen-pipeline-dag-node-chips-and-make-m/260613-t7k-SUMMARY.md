---
phase: quick-260613-t7k
plan: 01
subsystem: pipeline-dashboard-ui
tags: [dag-canvas, alpine-store, saq-postgres, per-stage-gating, htmx]
requires:
  - Phase 36 (SAQ Redis→Postgres backend, saq_jobs table)
  - Phase 37 (pipeline_stage_control + STAGE_TO_FUNCTION constants)
  - Phase 38 (per-stage pause/priority control row on agent chips)
provides:
  - Widened 240px DAG node chips (control row no longer clips "▼ Lower")
  - get_stage_busy_counts service (degrade-safe per-stage in-flight count)
  - Per-stage enqueue gating (metadataBusy/analyzeBusy/fingerprintBusy)
affects:
  - src/phaze/templates/pipeline/partials/dag_canvas.html
  - src/phaze/services/pipeline.py
  - src/phaze/routers/pipeline.py
  - src/phaze/templates/base.html
tech-stack:
  added: []
  patterns:
    - "SAVEPOINT (session.begin_nested) degrade — recover an aborted PG transaction without expiring loaded ORM objects"
key-files:
  created: []
  modified:
    - src/phaze/templates/pipeline/partials/dag_canvas.html
    - src/phaze/services/pipeline.py
    - src/phaze/routers/pipeline.py
    - src/phaze/templates/base.html
    - tests/test_dag_canvas_render.py
    - tests/test_pipeline_dag_context.py
    - tests/test_routers/test_pipeline_scans.py
    - tests/test_services/test_pipeline.py
decisions:
  - "Per-stage busy read wrapped in a SAVEPOINT (begin_nested), not a plain session.rollback(), so an absent saq_jobs / DB hiccup never expires the dashboard's loaded ORM objects (no 500) and never poisons later queries."
  - "Uniform 240px chip width across all 9 nodes; four columns re-gridded to x=24/392/760/1128 with 128px gaps (> the ±60px bézier control-point offset) so the 9 anchor-derived edges stay clean; canvas/SVG grown 1132→1392 wide."
metrics:
  duration: ~55 min
  completed: 2026-06-14
  tasks: 2
  files: 8
---

# Phase quick-260613-t7k: Widen DAG Node Chips + Per-Stage Enqueue Gating Summary

Widened the pipeline-DAG agent chips to 240px so the Phase-38 per-stage control row stops clipping "▼ Lower", and replaced the single global `agentBusy` enqueue gate with independent per-stage in-flight counts so Metadata, Analyze and Fingerprint run in parallel.

## What Was Built

### Task 1 — Widen DAG node chips (commit `6e31de2`)
- `NODE_LAYOUT` in `dag_canvas.html`: every node `w` 180→240; columns re-gridded to x = 24 / 392 / 760 / 1128 (all `y`/`h` unchanged, preserving the Phase-38 vertical overlap guard).
- Canvas wrapper + SVG grown 1132→1392 wide (height stays 1000); `viewBox` updated to `0 0 1392 1000`. The 9 bézier edge `d` strings are anchor-derived, so they auto-recomputed.
- Each 128px column gap exceeds the ±60px bézier control-point offset (`sx+60` / `tx-60`), so `sx+60 < tx-60` holds for every edge — curves stay clean. Canvas width derivation kept valid: `col3.x(1128) + 240 + 24 = 1392`.
- New test `test_topology_chips_widened_to_240_and_columns_do_not_overlap` (9× `width: 240px`; col-1 right edge 632 clears col-2 left edge 760).

### Task 2 — Per-stage enqueue gating (commit `ac6469e`)
- `services/pipeline.py`: new `get_stage_busy_counts(session) -> {metadata, analyze, fingerprint}`. One static grouped SQL — `SELECT split_part(key,':',1) AS fn, COUNT(*) ... FROM saq_jobs WHERE status IN ('queued','active') GROUP BY fn` — bucketed by the deterministic-key function prefix via the local inverse of `STAGE_TO_FUNCTION`. No operator input interpolated (T-t7k-01).
- `routers/pipeline.py` `_build_dag_context`: seeds `metadataBusy`/`analyzeBusy`/`fingerprintBusy` ints onto the `dag` map — they ride the existing `dag.items()` full-page seed + 5s `/pipeline/stats` OOB loop with no `stats_bar.html` edit.
- `base.html`: store seeds the 3 per-stage busy keys (`agentBusy` kept — the queue_progress card still reads `agent_busy` server-side).
- `dag_canvas.html`: the metadata/analyze/fingerprint `mk()` gates now read their OWN busy key (`s.metadataBusy`/`s.analyzeBusy`/`s.fingerprintBusy`); the `'Agent busy'` reason copy is preserved per-stage. Running one agent stage no longer locks the other two.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Degrade discipline changed from plain rollback to SAVEPOINT**
- **Found during:** Task 2 (dashboard integration tests 500'd)
- **Issue:** The plan specified mirroring `get_stage_controls` (warn → `session.rollback()` → zeros). In the test environment `saq_jobs` is absent (SAQ creates it at queue-connect during the app lifespan, which the `client`/`session` fixtures skip), so the read raised `UndefinedTableError`. A plain `session.rollback()` rolls back the WHOLE outer transaction, expiring the dashboard's already-loaded `agents`/`recent_scans` ORM objects; the template's next lazy-load then 500'd. (In production `saq_jobs` always exists, so this never triggers there — but the degrade path must still be correct.)
- **Fix:** Wrapped the `saq_jobs` read in a SAVEPOINT (`async with session.begin_nested()`). A failed read rolls back ONLY the nested scope — recovering the aborted Postgres transaction (`ROLLBACK TO SAVEPOINT`) without expiring loaded ORM objects and without poisoning later queries. Added `test_get_stage_busy_counts_degrade_does_not_poison_session` (drops `saq_jobs` in-transaction to force the branch deterministically, then proves a follow-up query still runs on the same session).
- **Files modified:** src/phaze/services/pipeline.py, tests/test_services/test_pipeline.py
- **Commit:** ac6469e

**2. [Rule 1 - Bug] Updated a pre-existing test that pinned the old shared-agentBusy gate**
- **Found during:** Task 2 full-suite run
- **Issue:** `tests/test_routers/test_pipeline_scans.py::test_button_disabled_binds_to_store_not_frozen_literal` asserted the old agent-stage predicate `s.discovered === 0 || s.agentBusy > 0` — exactly the string FIX2 changes. Not listed in the plan's Task-2 file set, but it directly tests the code under change.
- **Fix:** Updated the assertion to the per-stage gate `s.discovered === 0 || s.analyzeBusy > 0` (consistent with the `nodes.analyze.blocked` binding asserted in the same test).
- **Files modified:** tests/test_routers/test_pipeline_scans.py
- **Commit:** ac6469e

### Rejected approach (documented for posterity)
A first attempt created a minimal `saq_jobs` table in the shared `tests/conftest.py` so dashboard tests would see an empty table. This was **reverted** — the SAQ real-broker integration tests (`BROKER_DSN` defaults to `TEST_DATABASE_URL`, the same DB) create the FULL `saq_jobs` schema via `CREATE TABLE IF NOT EXISTS`; a pre-existing minimal table blocked that and broke 13 integration tests (missing `queue`/`job`/`function` columns). The SAVEPOINT fix (Deviation 1) solves the dashboard-test problem with zero conftest blast radius.

## Threat Surface

No new request input. The only new surface is a read of the SAQ broker table `saq_jobs` (already in the plan's `<threat_model>`): static `sqlalchemy.text` query, no operator input interpolated (T-t7k-01); SAVEPOINT-guarded degrade keeps the 5s poll alive on any DB error (T-t7k-02). No new threat flags.

## Verification

- `uv run pytest tests/test_dag_canvas_render.py tests/test_pipeline_dag_context.py tests/test_services/test_pipeline.py` — pass.
- `uv run ruff check .` — clean. `uv run ruff format --check` — clean. `uv run mypy .` — `Success: no issues found in 155 source files`.
- Full suite + coverage (ephemeral Postgres + Redis via `just test-db`): **1755 passed, 0 failed, coverage 97.55%** (≥85% gate).
- Pre-commit hooks ran and passed on both commits (never `--no-verify`).

## Self-Check: PASSED

- Created files: none.
- Modified files all exist (verified on disk).
- Commits exist: `6e31de2` (Task 1), `ac6469e` (Task 2) — confirmed via `git log`.
- No accidental deletions in either commit.
