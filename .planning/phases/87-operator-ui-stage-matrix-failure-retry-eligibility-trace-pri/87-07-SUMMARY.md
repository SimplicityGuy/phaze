---
phase: 87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri
plan: 07
subsystem: operator-ui
tags: [retry, failure-recovery, per-file-scope, guarded-funnel, manual-analyze, htmx, ui-02, d-04]

requires:
  - phase: 87-04
    provides: files_table_view.html paginated stage-matrix table + per-cell _stage_pill (the retry host)
  - phase: 87-05
    provides: the failed-filter lens (active_stage/active_bucket) the bulk Retry-all button rides
  - phase: 81-05/81-06
    provides: retry_analysis_failed / retry_metadata_failed bulk endpoints + the 3-branch response partials
provides:
  - "POST /pipeline/files/{file_id}/analysis-failed/retry — per-file scoped analyze retry, manual-only (flip+clear+commit before enqueue)"
  - "POST /pipeline/files/{file_id}/metadata-failed/retry — per-file scoped metadata retry (leaves the failure row, D-11)"
  - "files_table_view.html per-row Retry on a failed metadata/analyze cell + bulk 'Retry all failed · {stage}' on the failed-filter view"
affects:
  - "87-08 (rail priority re-wire + orphan badge) composes into the same files-table / record host"

tech-stack:
  added: []
  patterns:
    - "per-file scoped retry = the bulk endpoint's funnel filtered to one file_id, scoped by state/EXISTS so a non-failed/unknown id is a safe no-op (T-87-27)"
    - "manual-only terminal-analyze: flip ANALYSIS_FAILED->FINGERPRINTED + clear analysis.failed_at in one txn, COMMIT before enqueue (Phase-81 CR-01); never auto-looped (ELIGIBLE_AFTER_FAILURE[ANALYZE]=False)"
    - "per-row HTMX retry inside a record-slide-in row uses @click.stop so the button click never triggers the row's hx-get"
    - "response partials reused VERBATIM with a count of 1/0 — ack is int/bool-only, no operator free-text through Jinja (T-d79-04)"
    - "fingerprint has NO manual retry backend or control (per-row/bulk): it self-retries via eligible_clause (ELIG-04)"

key-files:
  created:
    - tests/analyze/test_retry_affordances.py
    - tests/metadata/test_retry_affordances.py
  modified:
    - src/phaze/routers/pipeline.py
    - src/phaze/templates/pipeline/partials/files_table_view.html
    - src/phaze/templates/pipeline/partials/_stage_matrix.html

key-decisions:
  - "Per-file retry re-drives ONE file through the SAME Phase-30-hardened funnel as the bulk endpoint (resolve_queue_for_task -> NoActiveAgentError guard -> enqueue), never a new retry path."
  - "The per-row retry lives in files_table_view.html's cell loop (where row.file.id + the stage key are in scope), NOT in _stage_matrix.html (a pure token with only a buckets dict, no file_id). _stage_matrix.html gets a doc cross-reference; the record right pane can still reuse it as a plain token."
  - "fingerprint gets NO retry control at all — a failed fingerprint stays auto-eligible and self-retries (ELIG-04); adding a manual control would be a redundant second path."

patterns-established:
  - "scoped-variant-of-a-bulk-endpoint: reuse the guarded funnel + response partial, filter to one id, no_op on a non-member id"
  - "behavior-8 no-auto-loop encoded as a pure predicate (eligible({ANALYZE: FAILED}) is False) + a functional manual-only flip test"

requirements-completed: [UI-02]

duration: ~55min
completed: 2026-07-11
---

# Phase 87 Plan 07: Per-file + bulk retry affordances for failed enrich files Summary

**Failed enrich files are now actionable from the console (UI-02): a per-row Retry on each failed
metadata/analyze cell and a bulk "Retry all failed · {stage}" on the failed-filter view, both
re-wiring the already-live, Phase-30-hardened retry endpoints (plus a new per-file scoped variant) —
with the analyze terminal guard strictly preserved (manual retry, never an auto-loop) and fingerprint
deliberately left to self-retry.**

## Performance

- **Duration:** ~55 min
- **Completed:** 2026-07-11
- **Tasks:** 2
- **Files:** 5 (3 modified, 2 created)

## Accomplishments

- **Per-file scoped retry endpoints** (`routers/pipeline.py`): `POST
  /pipeline/files/{id}/analysis-failed/retry` and `/pipeline/files/{id}/metadata-failed/retry`. Each
  re-drives EXACTLY one file through the identical guarded funnel the bulk endpoints use
  (`enqueue_router.resolve_queue_for_task` → `NoActiveAgentError` guard → `enqueue_process_file` /
  `_enqueue_extraction_jobs` with the COMPLETE payload + deterministic dedup key), scoped by
  `id AND state == ANALYSIS_FAILED` (analyze) / `id AND EXISTS(metadata.failed_at)` (metadata) so a
  non-failed or unknown id is a safe no-op ack (T-87-27). Response partials reused verbatim (count 1/0).
- **Manual-only analyze guard preserved** (behavior 8 / T-87-24): the analyze per-file retry flips
  `ANALYSIS_FAILED → FINGERPRINTED` and clears `analysis.failed_at`/`error_message` in one transaction,
  then commits BEFORE the enqueue (Phase-81 CR-01) so the file leaves the failed disjunct and derives
  a fresh re-analysis. `ELIGIBLE_AFTER_FAILURE[ANALYZE]=False` stays respected — a FAILED analyze is
  never auto-re-enqueued; the operator button is its only mover. Metadata leaves its failure row (D-11).
- **Console affordances** (`files_table_view.html`): a per-row Retry renders ONLY on a failed
  metadata/analyze cell (HTMX POST to the per-file variant; `@click.stop` keeps the click off the row's
  record slide-in; ack swaps into the cell's result span; `aria-label="Retry {stage} for this file"`).
  A bulk primary (accent) "Retry all failed · {stage}" renders on the failed-filter view of an enrich
  stage, posting to the live bulk endpoint. The failed pill clears on the NEXT poll tick (not
  optimistic). **fingerprint gets no manual retry control** (per-row or bulk) — it self-retries.
- **Tests** (`tests/analyze/test_retry_affordances.py` + `tests/metadata/test_retry_affordances.py`,
  15 total): per-file + bulk route through the guarded funnel on the correct per-agent lane
  (`nox-analyze` / `nox-meta`, never `default`); no-agent → amber ack + zero enqueues + no mutation
  (Phase-30); the behavior-8 no-auto-loop predicate (`eligible({ANALYZE: FAILED})` is False, contrast
  metadata/fingerprint True); non-failed/unknown id no-op; independent-session read proving the analyze
  flip+clear committed; and Task-2 render assertions (per-row retry only on failed enrich cells, bulk
  button on the failed filter, NO fingerprint control — `-k render`).

## Task Commits

1. **Task 1: Per-file scoped retry variants (analyze + metadata), manual-analyze preserved** — `b37b546f` (feat)
2. **Task 2: Wire per-row Retry (failed cells) + bulk "Retry all failed · {stage}"** — `64450830` (feat)

## How to Verify

With the test DB up (port 5433):
```
export TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test"
export PHAZE_QUEUE_URL="postgresql://phaze:phaze@localhost:5433/phaze_test"
uv run pytest tests/analyze/test_retry_affordances.py tests/metadata/test_retry_affordances.py -q   # 15 passed
```
- Regression: `test_pipeline_metadata_retry` + `test_pipeline_analysis_retry_clears_marker` +
  `test_pipeline` + the two new suites → **133 passed**.
- `uv run ruff check` + `uv run mypy` clean on the touched router (ran green via pre-commit on each commit).

### Mutation observation (project rule: mutation-test guard tests)

Added `'fingerprint': 'fingerprint-failed'` to the `_retry_endpoints` map in `files_table_view.html`
→ `test_render_no_manual_fingerprint_retry_control` + `test_render_no_bulk_fingerprint_retry_button`
both went **RED**; restoring → **GREEN**. The no-fingerprint-control guards have teeth.

### Counts will look different, not broken (UI-SPEC operator note)

As the failed bucket becomes per-file and per-stage actionable, the numbers an operator sees shift
relative to the old serially-gated view — intended effect of the UI-01/UI-02 cutover, not a regression.

## Deviations from Plan

**1. [Rule 3 — Design] The per-row retry lives in `files_table_view.html`, not `_stage_matrix.html`**
- **Found during:** Task 2.
- **Issue:** The plan declares `_stage_matrix.html` among Task-2's files, implying the retry affordance
  is added there. But `_stage_matrix.html` is a pure presentational token taking only a `buckets` dict —
  it has no per-cell `file_id`, and the files table renders each stage cell by including `_stage_pill.html`
  directly (never the matrix). The matrix's only other consumer, the record right pane (87-06), uses trace
  triggers, not retry. Threading `file_id` through the matrix would break its reuse as a plain token.
- **Fix:** The per-row Retry is in `files_table_view.html`'s cell loop, where `row.file.id` + the stage
  key are in scope. `_stage_matrix.html` receives a doc cross-reference explaining why the pure token
  intentionally carries no retry affordance (so it IS touched, and a reader knows where the retry lives).
- **Files:** `files_table_view.html`, `_stage_matrix.html`. **Commit:** `64450830`.

No auto-fixed bugs, no auth gates, no architectural (Rule 4) escalations, no package installs. No
out-of-scope issues found; `deferred-items.md` not appended.

## Threat Register Coverage

- **T-87-24** (mis-scoped analyze retry re-enables the 44.5K over-enqueue): mitigated — analyze retry is
  MANUAL-only (`ELIGIBLE_AFTER_FAILURE[ANALYZE]=False`); `test_analyze_failure_is_never_auto_eligible`
  asserts `eligible({ANALYZE: FAILED})` is False (no auto-loop), and the per-file endpoint no-ops on a
  non-ANALYSIS_FAILED id (`test_per_file_retry_non_failed_file_is_noop`).
- **T-87-25** (retry with no active agent): mitigated — both per-file and bulk reuse the guarded funnel;
  `NoActiveAgentError` → amber ack, nothing enqueued/flipped. Asserted on both paths (per-file + bulk,
  analyze + metadata).
- **T-87-26** (duplicate enqueue on rapid retry): mitigated — reuses the live endpoints' deterministic
  dedup key (`process_file:<id>` / `extract_file_metadata:<id>`); a live in-flight job dedups to a no-op.
- **T-87-27** (invalid file_id/stage on per-file retry): mitigated — `uuid.UUID` path param + the
  `state`/`EXISTS` scope; a non-member id returns the "none" ack with zero enqueues (asserted).

No new threat surface beyond the register (the per-file endpoints reuse the bulk funnel + dedup key; the
only writes are the analyze flip+clear the bulk endpoint already performs, scoped to one file).

## Next Phase Readiness

- The files-table + record host is ready for 87-08 (rail priority re-wire + orphan badge).
- The failed bucket is now fully actionable: per-file retry, bulk retry, and (from 87-06) force-skip.

## Self-Check: PASSED

Both created test files + all three modified files present on disk; both task commits (`b37b546f`,
`64450830`) in git history. 15 plan tests + 133 combined regression tests green; ruff + mypy clean;
mutation-verified the no-fingerprint-retry guards.

---
*Phase: 87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri*
*Completed: 2026-07-11*
