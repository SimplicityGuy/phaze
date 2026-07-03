---
phase: 64-per-module-coverage-uplift-gate-raise
plan: 02
subsystem: testing
tags: [coverage, degrade-path, review, agent-liveness, test-only]
requires:
  - "services/review.py degrade + formatter branches (Phase 60)"
  - "services/agent_liveness.classify_compute_lanes degrade branch (Phase 61)"
provides:
  - "services/review.py combined coverage >= 85% (COV-01 sub-floor gap closed)"
  - "services/agent_liveness.py margin above the 85% floor"
affects:
  - "the per-module coverage floor the Phase 64 gate enforces (review.py was the only sub-floor module)"
tech-stack:
  added: []
  patterns:
    - "raising-stub session (no DB) to drive a service degrade branch into its except block"
    - "assert BOTH the default return AND the named caplog warning key (D-07 observable outcome)"
    - "pure-formatter return-value assertions (exact / endswith / startswith)"
key-files:
  created:
    - "tests/review/services/test_review_degrade.py"
  modified:
    - "tests/agents/services/test_agent_liveness.py"
decisions:
  - "Degrade tests inject a synchronous-raising begin_nested stub (no D-08 seam, no src/phaze edit) — the async with call site enters except when begin_nested raises synchronously"
  - "Combined coverage measured via the full DB-backed suite (review.py happy-path bodies live in router/integration buckets, not the review service bucket)"
metrics:
  duration: "~30m (incl. 11m45s full-suite coverage run)"
  completed: 2026-07-03
  tasks: 2
  files: 2
---

# Phase 64 Plan 02: Per-Module Coverage Uplift (review.py floor clear) Summary

Closed the single real per-module coverage floor gap by adding behavior-asserting degrade +
formatter tests for `services/review.py` (the ONLY sub-floor module at 83.16%), plus an
optional-margin degrade test for `services/agent_liveness.classify_compute_lanes`. Test-only,
zero `src/phaze/**` change — the milestone's "no backend behavior change" invariant is preserved
by construction.

## What Was Built

- **Task 1** — `tests/review/services/test_review_degrade.py` (new, review bucket): four degrade
  tests (`get_pending_proposal_rows`, `get_tagwrite_review_rows`, `get_dedupe_groups`,
  `get_cue_review_cards`) each asserting BOTH `result == []` AND the named `*_degraded` warning
  key in `caplog`, driven by a `_RaisingSession` stub whose `begin_nested` raises synchronously.
  Two pure-formatter tests cover `_format_size` (None/0/MB/PB) and `_format_quality`
  (with/without bitrate).
- **Task 2** — `tests/agents/services/test_agent_liveness.py` (extended, agents bucket): one
  `@pytest.mark.asyncio` degrade test injecting a session whose `.execute` raises
  `SQLAlchemyError`, asserting the observable `("IDLE", 0)` return. Existing pure-function
  `classify`/`sort_key` tests untouched.

## Verification Results

- `uv run pytest tests/review/services/test_review_degrade.py -q` → 6 passed.
- `uv run pytest tests/agents/services/test_agent_liveness.py -q` → 27 passed (26 existing + 1 new).
- Full DB-backed suite (`tests/`, ephemeral Postgres:5433 + Redis:6380): **2593 passed**.
- Combined per-module coverage (authoritative, full suite):
  - `services/review.py` — **98.95%** (was 83.16%); clears the 85% floor. Only line 122 uncovered
    (a `continue` on a zero-change tag row — not in scope; already ≥ floor).
  - `services/agent_liveness.py` — **95.83%** (was 85.42%); margin gained. Only the
    rollback-failure log lines 178-179 uncovered (a nested double-failure branch).

## Deviations from Plan

None — plan executed exactly as written. No Rule 1-4 deviations, no auth gates, no checkpoints.

## Notes

- The review bucket in ISOLATION shows review.py at 58.95% because its happy-path bodies are
  exercised by router/integration tests in other buckets; the combined full-suite number (98.95%)
  is the authoritative figure per RESEARCH §Re-Baselining. My new tests specifically cover the
  degrade `except` blocks (74-76/134-136/197-199/267-269) and formatters (142/148/156) that were
  the actual sub-floor gap.
- `just test-bucket review` surfaced 163 pre-existing DB-connection errors when no test DB is up
  — an infra condition (DB-fixture tests need a running Postgres), NOT a regression from this plan.
  With the test DB up, the review + agents buckets are green (444 passed in the targeted run).

## Self-Check: PASSED

- FOUND: tests/review/services/test_review_degrade.py
- FOUND: tests/agents/services/test_agent_liveness.py (modified)
- Commits verified below.
