---
quick_id: 260707-d79
title: Add operator retry affordance for ANALYSIS_FAILED files
status: complete
date: 2026-07-07
requirements: [D79-RETRY]
commits:
  - 69d79ac4  # feat: endpoint + templates + reenqueue comment
  - fa4b3832  # test: route + render coverage
files_created:
  - src/phaze/templates/pipeline/partials/retry_failed_response.html
files_modified:
  - src/phaze/routers/pipeline.py
  - src/phaze/tasks/reenqueue.py
  - src/phaze/templates/pipeline/partials/straggler_failed_card.html
  - tests/shared/routers/test_pipeline.py
---

# Quick Task 260707-d79: Operator Retry for ANALYSIS_FAILED Files Summary

Bulk operator-gated retry that re-drives every `FileState.ANALYSIS_FAILED` file through the
same guarded funnel `deepen_analysis` uses — but with NORMAL caps — flipping each file out of
the terminal red bucket so it re-runs analysis, while leaving auto-recovery untouched.

## What shipped

- **`POST /pipeline/analysis-failed/retry` → `retry_analysis_failed`** (`routers/pipeline.py`,
  placed just before `deepen_analysis`). Order mirrors the Phase-30 / RESEARCH-Pitfall-3 guards:
  1. `get_analysis_failed_files(session)` — empty ⇒ ack `count=0`, no enqueue.
  2. Resolve the per-agent queue ONCE via `enqueue_router.resolve_queue_for_task("process_file", …)`.
     `NoActiveAgentError` ⇒ ack `no_active_agent=True`, ZERO state flips, ZERO enqueues, never the
     consumer-less default queue.
  3. Flip every file `ANALYSIS_FAILED → FINGERPRINTED` and `session.commit()` BEFORE any enqueue
     (get_session does not auto-commit) so the red count drops on the next 5s poll regardless of
     enqueue outcome.
  4. Loop `enqueue_process_file(routed.queue, f, agent_id, settings.models_path)` — NO
     `fine_cap`/`coarse_cap` override (normal 60/30 caps; a retry is a fresh re-analysis, not a
     deepen). The deterministic `process_file:<id>` key dedups any live in-flight job.
  5. `logger.info("retry_analysis_failed re-queued files", count=…)` for observability.
- **`retry_failed_response.html`** — ack fragment, three branches from ints/bools only
  (`no_active_agent` / `count == 0` / else). No `hx-swap-oob` id (swaps into the button's sibling
  span only; the 5s stats poll re-pushes `#straggler-failed-card` untouched).
- **`straggler_failed_card.html`** — "Retry failed" button in the red tile, rendered only when
  `analysis_failed_count > 0`, with `hx-confirm`, targeting a sibling `#retry-failed-result` span.
  The `#straggler-failed-card` OOB wrapper + `{% if oob %}` guard are untouched.
- **`reenqueue.py`** — documentation-only comment on `_select_done_analyze_ids` (body UNCHANGED):
  ANALYSIS_FAILED is deliberately analyze-DONE so `recover_orphaned_work` never auto-loops an
  un-analyzable file; the new endpoint is the manual counterpart that flips files out first.

## Tests (`tests/shared/routers/test_pipeline.py`, +4)

- `test_retry_reenqueues_all_failed_and_flips_state` — N failed files → N captures on `phaze-agent-nox`
  (never `default`), each a complete `ProcessFilePayload` with `fine_cap`/`coarse_cap` **None** (the
  retry-vs-deepen guard, whose deepen sentinel is 0); all files now `FINGERPRINTED`, 0 `ANALYSIS_FAILED`.
- `test_retry_no_active_agent_enqueues_nothing_and_keeps_state` — no agent → zero captures, files STAY
  `ANALYSIS_FAILED`, ack surfaces "no active agent".
- `test_retry_zero_failed_is_noop` — empty bucket → 200, zero captures, ack count 0.
- `test_retry_button_renders_only_when_count_positive` — `/pipeline/stats` render: absent at count 0,
  button + `hx-confirm` present at count > 0.

## Verification

- `uv run ruff check` / `ruff format --check` / `uv run mypy` — clean on all changed files.
- Ephemeral PG/Redis (`just test-db`, ports 5433/6380): `-k retry` = **5 passed**; full
  `tests/shared/routers/test_pipeline.py` = **107 passed**. DB torn down.
- pre-commit ran on both commits (never `--no-verify`); all hooks Passed.

## Deviations from Plan

**1. [Rule 3 - Blocking] `just test-db` takes no `up` argument.** The plan's Task-3 verify line
uses bare `just test-db` (matching the constraint note); `just test-db up` is invalid. Ran bare
`just test-db` + exported `TEST_DATABASE_URL` / `MIGRATIONS_TEST_DATABASE_URL` / `PHAZE_REDIS_URL`
for 5433/6380, tore down with `just test-db-down`. No code impact.

Otherwise executed exactly as written. `_select_done_analyze_ids` / `recover_orphaned_work` logic
left byte-for-byte unchanged (comment only).

## Known Stubs

None.

## Threat Flags

None — no new trust-boundary surface beyond the plan's threat register (T-d79-01..04 all honored:
deterministic-key dedup, `NoActiveAgentError` no-enqueue guard, unchanged `_select_done_analyze_ids`,
ints/bools-only through Jinja autoescape).

## Self-Check: PASSED

- FOUND: src/phaze/templates/pipeline/partials/retry_failed_response.html
- FOUND commit 69d79ac4 (feat), fa4b3832 (test)
