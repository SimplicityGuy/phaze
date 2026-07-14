---
phase: 87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri
plan: 08
subsystem: operator-ui
tags: [dag-rail, orphan-badge, recovery-candidate, priority-stepper, pause-resume, degrade-safe, ui-05, prio-01, deriv-read]
requires:
  - phase: 87-02
    provides: skipped_clause threaded into domain_completed_clause / done_clause — the derivation the orphan count reads domain-completed through
  - phase: 87-04
    provides: services/pipeline.py + routers/pipeline.py Wave-3 dag-seed / #pipeline-stats OOB fanout the orphan count rides
  - phase: 87-07
    provides: routers/pipeline.py Wave-5 retry endpoints (built on, not clobbered — orphan seed added alongside)
provides:
  - "services/pipeline.py:get_stage_orphan_counts — per-enrich-stage orphaned/stuck (recovery-candidate) count, degrade-safe, reusing recover_orphaned_work's OWN classifier (no drift)"
  - "routers/pipeline.py:_build_dag_context seeds metadataOrphan/analyzeOrphan/fingerprintOrphan onto the single #pipeline-stats OOB fanout (no self-poll)"
  - "shell/partials/rail.html — amber orphan badge (role=status, hidden at 0) + per-enrich-stage priority stepper + pause/resume re-wired to the LIVE /pipeline/stages/{stage}/{priority,pause,resume} endpoints"
  - "base.html $store.pipeline.{metadata,fingerprint,analyze}Orphan store defaults (so the badge x-show reads a number before the first poll)"
affects:
  - "The DAG rail now surfaces stuck-work visibility where recovery acts, and re-exposes the orphaned-since-v7.0 durable-control endpoints"
tech-stack:
  added: []
  patterns:
    - "orphan count = recovery-candidate count: reuse recover_orphaned_work's is_domain_completed + _build_done_sets + _in_flight_cloud_job_ids predicate (parity is DEFINITIONAL — the badge cannot drift from recovery)"
    - "function-local reenqueue import inside a services.pipeline helper: breaks the reenqueue<->pipeline import cycle AND keeps reenqueue (control-only) off the agent-worker import path (tests/shared/core/test_task_split.py stays green)"
    - "SAVEPOINT (begin_nested) degrade → all-zeros on any DB error; never a plain session.rollback() that would expire the dashboard's loaded ORM objects and 500 the 5s poll"
    - "store-driven live control: hx-swap=none on the priority/pause posts (the {stage,priority,paused} JSON is not swapped); the label/badge read $store.pipeline, refreshed each 5s poll from the durable control row"
    - "interactive rail controls live in a sub-row OUTSIDE the nav <button> (never nested interactive elements)"
key-files:
  created:
    - tests/integration/test_orphan_count.py
    - tests/shared/test_rail_priority_controls.py
  modified:
    - src/phaze/services/pipeline.py
    - src/phaze/routers/pipeline.py
    - src/phaze/templates/base.html
    - src/phaze/templates/shell/partials/rail.html
decisions:
  - "Reused recover_orphaned_work's OWN classification predicate (is_domain_completed + _build_done_sets + _in_flight_cloud_job_ids) rather than re-deriving the done clauses from awaiting_candidate_clause as the plan interfaces suggested: this makes badge/recovery parity DEFINITIONAL (truth #1 / T-87-31), not an assertion that could rot. The reenqueue import is function-local to break the cycle + keep the control-only boundary."
  - "Added the three *Orphan store keys to base.html (not a declared file): the locked live-vs-static rail rule requires a $store.pipeline key to pre-exist before an x-show/x-text binds it, so the badge would read undefined before the first poll without it. Direct precedent: the Phase-38 metadata/analyze/fingerprint Paused/Priority keys live in base.html the same way."
  - "hx-swap=none on the priority/pause/resume posts: the LIVE endpoints return JSON {stage,priority,paused} (not HTML), so a DOM swap would render raw JSON. The control display is store-driven (refreshed each 5s poll from the durable control row via get_stage_controls), consistent with the rail's existing store-driven counts — the response 're-renders the control' on the next tick, not via a JSON swap."
  - "No staleness threshold is used (OQ-2 RESOLVED = recovery-candidate count), so the naive-enqueued_at footgun (Pitfall 4) never bites in the helper; the only naive/aware comparison is the D-10 metadata cell inside is_domain_completed, which already coerces naive->aware (CR-02)."
requirements-completed: [UI-05, PRIO-01]
metrics:
  duration: ~55min
  completed: 2026-07-11
  tasks: 2
  files: 6
---

# Phase 87 Plan 08: DAG-rail orphan badge (UI-05) + priority/pause re-wire (PRIO-01) Summary

**The two ambient DAG-rail additions: a per-enrich-stage amber orphaned/stuck badge whose count is
EXACTLY the recovery-candidate set `recover_orphaned_work` would re-enqueue (no drift — it reuses
recovery's own predicate), and the per-stage priority stepper + pause/resume re-wired to the still-live
durable-control endpoints that were orphaned from the UI in the v7.0 redesign. Both ride the single
`#pipeline-stats` OOB fanout (no self-poll) and degrade fail-safe so the 5s poll can never 500.**

## TOP-OF-SUMMARY FLAG (issue found)

- **Pre-existing, app-wide, cosmetic:** `x-cloak` is INERT in this app — neither `assets/src/app.css`
  nor the compiled CSS defines the `[x-cloak]{display:none}` rule Alpine v3 requires you to add
  yourself. So every existing `x-cloak` (base.html theme-toggle SVGs, header, cmdk_modal, record_host,
  agents_table) — and now the rail orphan badge / Resume / Paused caption — briefly flashes its
  fallback content (~sub-100ms) on first paint before `x-show` hides it. No functional impact (store
  defaults hide them the instant Alpine inits). NOT introduced by this plan. Logged with the one-line
  fix (`[x-cloak]{display:none !important;}` in `assets/src/app.css`) to `deferred-items.md` because the
  fix touches a non-declared file whose compiled output is gitignored + rebuilt via `just tailwind`.

## What Was Built

- **`get_stage_orphan_counts` (Task 1, `services/pipeline.py`)**: returns `{metadata, analyze,
  fingerprint}` where each value is the number of `scheduling_ledger` rows for the stage's function that
  are NEITHER live (a queued/active `saq_jobs` key) NOR domain-completed NOR owned by an in-flight
  `cloud_job` — the EXACT set `recover_orphaned_work` would re-enqueue for that stage. Parity is
  definitional: it reuses recovery's own `is_domain_completed` + `_build_done_sets` +
  `_in_flight_cloud_job_ids` predicate (imported function-locally to break the `reenqueue`↔`pipeline`
  cycle and keep `reenqueue`, which is control-only, off the agent-worker import path). Wrapped in a
  `begin_nested()` SAVEPOINT that degrades to all-zeros on any error — never a plain `rollback()` that
  would expire the dashboard's loaded ORM objects and 500 the poll (T-87-28). Only the three
  `STAGE_TO_FUNCTION` enrich functions are bucketed (via the existing `_BUSY_FUNCTION_TO_STAGE` inverse);
  `push_file`/`scan_live_set`/controller rows are not part of the enrich badge.
- **`#pipeline-stats` seed (Task 1, `routers/pipeline.py`)**: `_build_dag_context` now seeds
  `dag["metadataOrphan"]`/`["analyzeOrphan"]`/`["fingerprintOrphan"]` alongside the existing
  pause/priority overlay loop, so the badge rides the SAME `dag.items()` OOB fanout (`stats_bar.html:74`)
  with **no new poll and no `stats_bar.html` edit**.
- **`base.html`**: the three `*Orphan` `$store.pipeline` defaults (int 0) so the badge's `x-show` reads a
  number before the first poll (same pattern the Phase-38 `*Paused`/`*Priority` keys use).
- **Rail (Task 2, `shell/partials/rail.html`)**: per enrich node (metadata/fingerprint/analyze):
  - an amber orphan numeral badge bound to `$store.pipeline.{stage}Orphan`, `role="status"`, hidden at 0
    (`x-show ... > 0`), amber `bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-400` (never
    red — "needs attention, not failure"), inside the nav button next to the done count;
  - a controls **sub-row OUTSIDE the nav `<button>`** (never nested interactive controls) carrying a
    `▲`/`▼` priority stepper (`hx-post="/pipeline/stages/{stage}/priority"` `hx-vals` delta `-10`/`+10`)
    and a pause→`/pause` / resume→`/resume` toggle, all `hx-swap="none"`. The label reads
    `Priority: {High|Normal|Low} ({n})` from the store; each control carries an explicit `aria-label`
    (Raise/Lower/Pause/Resume {stage}) PLUS the D-11 clarifying tooltip; a "Paused" amber caption shows
    when paused. Fresh markup against the live endpoints — **no Phase-38 template resurrected**.
    Collapsed-rail hidden (`max-lg:hidden`) per the CUT-04 contract.
- **Tests**: `tests/integration/test_orphan_count.py` (7) — a no-progress file is 1 orphan for its
  stage; a mixed corpus's badge counts equal the **inline recovery-candidate derivation** over the same
  session (real no-drift proof, not a restatement of internals) AND the concrete `{metadata:1, analyze:2,
  fingerprint:1}`; each exclusion mirrors recovery (domain-completed, force-skip [behavior 5], live key,
  in-flight cloud); and the forced-error degrade returns all-zeros while leaving the session usable (the
  SAVEPOINT rollback did not poison the outer txn). `tests/shared/test_rail_priority_controls.py` (23) —
  steppers post to the live endpoints with the ±10 deltas; pause/resume post to `/pause`/`/resume`;
  explicit aria-labels + the D-11 tooltip; the High/Normal/Low label; the amber orphan badge binds the
  store key, is `role="status"` + amber, hidden at 0; and the stepper buttons are NOT nested in the nav
  `<button>`.

## How to Verify

With the test DB up (port 5433):
```
export TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test"
export PHAZE_QUEUE_URL="postgresql://phaze:phaze@localhost:5433/phaze_test"
uv run pytest tests/integration/test_orphan_count.py tests/shared/test_rail_priority_controls.py -q   # 30 passed
```
- Regression: `tests/analyze/tasks/test_recovery.py` + `tests/integration/test_stage_progress_buckets.py`
  + `tests/integration/test_orphan_count.py` → **65 passed** (the reused recovery classifier is intact).
- Rail regression: `tests/shared/core/test_rail_narrow_width.py` + `test_shell_routes.py` +
  `test_a11y_guards.py` + `test_base_html_sri.py` + the new suite → **53 passed**.
- Poll path: `tests/shared/routers/test_pipeline.py` → **109 passed** (`_build_dag_context` still builds
  the dashboard/stats context with the new orphan seed).
- `uv run ruff check .` clean; `uv run mypy .` clean (both ran green via pre-commit on every commit).

### Mutation observation (project rule: mutation-test guard tests)

Mutated `rail.html` (analyze raise-delta `-10`→`-99`; `aria-label="Pause metadata"`→`"Halt metadata"`)
→ `test_priority_stepper_posts_to_live_endpoint_with_delta[analyze]` +
`test_stepper_controls_carry_explicit_aria_labels[metadata]` both went **RED** (2 failed, 21 passed);
restoring from a saved copy → **23 passed**. The endpoint/delta + aria-label guards have teeth.

### Counts will look different, not broken (operator note)

The orphan badge surfaces recovery-candidate work that was previously invisible; a non-zero amber pill is
the intended UI-05 effect (stuck work made visible where recovery acts), not a regression.

## Deviations from Plan

**1. [Rule 3 — Design] Reused recovery's OWN classifier instead of re-deriving the done clauses**
- **Found during:** Task 1.
- **Issue:** The plan's `<interfaces>` suggested composing the orphan count from `awaiting_candidate_clause`
  + `stage_status.py` clauses + `_safe_count`. But truth #1 / T-87-31 demand the count "matches what
  `recover_orphaned_work` would re-enqueue (no drift)" — re-deriving the clauses risks silent drift.
- **Fix:** `get_stage_orphan_counts` reuses recovery's exact predicate (`is_domain_completed` +
  `_build_done_sets` + `_in_flight_cloud_job_ids` + the `row.key not in live` filter), so parity is
  DEFINITIONAL. The `reenqueue` import is function-local (breaks the `reenqueue`↔`pipeline` cycle and
  keeps the control-only module off the agent-worker import path — `test_task_split.py` stays green, 16
  passed). `# noqa: PLC0415` with a cycle/boundary reason (established precedent across the codebase).
- **Files:** `services/pipeline.py`. **Commit:** `9d8fcbc2`.

**2. [Rule 3 — Blocking] Added the three `*Orphan` store keys to `base.html` (not a declared file)**
- **Found during:** Task 1/2.
- **Issue:** The locked live-vs-static rail rule requires a `$store.pipeline` key to pre-exist before an
  `x-show`/`x-text` binds it (else the badge reads `undefined`/flashes before the first poll).
- **Fix:** Added `metadataOrphan`/`analyzeOrphan`/`fingerprintOrphan: 0` to the store defaults — the same
  place the Phase-38 `*Paused`/`*Priority` keys live. Committed with Task 1 (the seed's landing target).
- **Files:** `templates/base.html`. **Commit:** `9d8fcbc2`.

**3. [Rule 3 — Design] `hx-swap="none"` on the priority/pause/resume controls**
- **Found during:** Task 2.
- **Issue:** The plan says the `{stage,priority,paused}` response "re-renders the control", but the LIVE
  endpoints return JSON (not HTML) — a DOM swap would render raw JSON.
- **Fix:** `hx-swap="none"`; the control display is store-driven (`$store.pipeline.{stage}Priority/Paused`,
  refreshed each 5s poll from the durable control row via `get_stage_controls`), consistent with the
  rail's existing store-driven counts. The response "re-renders the control" on the next poll tick.
- **Files:** `templates/shell/partials/rail.html`. **Commit:** `186b6385`.

**4. [Rule 1 — Conformance] `max-lg:hidden` on the priority label spans**
- **Found during:** Task 2 (regression run).
- **Issue:** `test_rail_narrow_width::test_counts_hidden` requires EVERY `x-text` span to carry
  `max-lg:hidden` (visual-only numeric data drops out of the collapsed icon-rail, CUT-04). The new
  priority-label spans initially only inherited it from the wrapper.
- **Fix:** Added `max-lg:hidden` to each priority-label span (redundant with the wrapper but satisfies the
  per-span contract). **Commit:** `186b6385`.

No auto-fixed product bugs, no auth gates, no architectural (Rule 4) escalations, no package installs.
One pre-existing out-of-scope finding (inert `x-cloak`) logged to `deferred-items.md` and flagged at the
top of this summary (commit `d649d520`).

## Threat Register Coverage

- **T-87-28** (poll-time 500 / naive-timestamp TypeError on orphan derivation): mitigated — the whole
  derivation runs inside a `begin_nested()` SAVEPOINT that degrades to all-zeros on any error (asserted by
  `test_degrades_to_zero_and_session_stays_usable`, which also proves the outer session survives). No
  staleness threshold is used, so the naive-`enqueued_at` footgun (Pitfall 4) never fires; the only
  naive/aware comparison is the D-10 metadata cell inside `is_domain_completed`, which coerces
  naive→aware (CR-02).
- **T-87-29** (priority delta out of range): mitigated — preserved at the live endpoint
  (`_PRIORITY_MIN/_MAX` clamp + DB CHECK, T-37-02); the UI only ever posts fixed ±10 `hx-vals`.
- **T-87-30** (injection via stage param on control post): mitigated — the live endpoint validates `stage`
  against the `STAGE_TO_FUNCTION` allowlist (422 on unknown, T-37-01); the rail posts FIXED stage paths
  (`/pipeline/stages/metadata|analyze|fingerprint/...`), never operator input.
- **T-87-31** (orphan badge drifts from recovery): mitigated — the orphan count is DEFINED AS the
  recovery-candidate set by reusing recovery's own classifier; asserted no-drift by
  `test_orphan_count_matches_recovery_candidate_set` (badge == inline recovery-candidate derivation over
  the same corpus) + the four exclusion tests (domain-completed / force-skip / live key / in-flight cloud).

No new threat surface beyond the register: the endpoints are the already-threat-modeled Phase-37 durable
controls (re-exposed, not rebuilt); the orphan read is read-only (no writes, no new auth path, no schema).

## Known Stubs

None — the orphan count is a live derived read wired to real ledger/output-table data, and the priority/
pause controls post to the fully-live durable-control endpoints. No hardcoded/placeholder values.

## Self-Check: PASSED

Both created test files + all four modified files present on disk; all three task/chore commits
(`9d8fcbc2`, `186b6385`, `d649d520`) in git history. 30 plan tests + 65/53/109 regression suites green;
ruff + mypy clean; mutation-verified the rail endpoint/aria-label guards. (Verified below at write time.)

---
*Phase: 87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri*
*Completed: 2026-07-11*
