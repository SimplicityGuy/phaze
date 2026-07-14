---
status: partial
phase: 90-destructive-migration-writer-removal
source: [90-01-SUMMARY.md, 90-02-SUMMARY.md, 90-03-SUMMARY.md, 90-04-SUMMARY.md]
started: 2026-07-13T16:21:01Z
updated: 2026-07-13T16:21:01Z
---

## Current Test

[testing paused — 2 items outstanding (operator-only, blocked)]

## Tests

<!-- Phase 90 is a backend safety refactor (retire files.state; derive per-stage status
from output tables). Its user-observable contract is "the app behaves identically with
files.state fully removed." Tests 1-8 were EXECUTED by Claude against the live test DB
(port 5433) + Redis (6380) on 2026-07-13. Tests 9-10 are physically operator-only. -->

### 1. Cold-Start Smoke Test
expected: The app imports and boots from scratch with the FileState enum, `files.state` column, and `shadow_compare` subsystem all removed — no dangling import, no registration error.
result: pass
<!-- `uv run python -c "import phaze; import phaze.cli"` → IMPORT OK -->

### 2. Dashboard counts & reader cutovers derive correctly
expected: Every converted reader (count cards, analyze workspace, proposal batches, backfill, held-files, retry, search-facet removal) returns the same rows it did pre-cutover, now derived from the Phase-78 clause builders + cloud_job sidecar instead of `files.state`.
result: pass
<!-- test_stage_status_equivalence.py: 59 passed (SQL⇔Python byte-equivalence lock);
     tests/shared/routers/test_pipeline.py: 110 passed (dashboard counts + held-files paths) -->

### 3. Dedup undo round-trip works from the server-rendered payload
expected: `/resolve`→`/undo`, `/resolve-all`→`/undo-all`, and an id-only payload all delete the DedupResolution marker using the ACTUAL server-rendered `file_states`; stale-replay stays a no-op; the undo can never no-op after `previous_state` was removed.
result: pass
<!-- test_duplicates.py (undo/roundtrip/resolve) + test_dedup_resolve_undo_shadow.py: 19 passed -->

### 4. No `files.state` writer survives; CAS idempotency preserved
expected: All ~17 `FileRecord.state` writers (incl. both metadata + S3 CAS guards) are gone; endpoints that lost a CAS guard remain idempotent under a double call via the ON CONFLICT / marker authority.
result: pass
<!-- grep for `.values(state=)` / `.state = FileState` in src/phaze → NONE;
     test_metadata_callback_idempotent_after_cas_removal + test_s3_push_status_transition_idempotent_after_cas_removal: 2 passed (each invokes endpoint TWICE) -->

### 5. Migration 039 drops the column safely and reversibly
expected: `039` guards first (raises + rolls back on mid-flight rows or a hard-invariant violation, passes clean on an empty DB), snapshots `files_state_archive`, drops `files.state` + `ix_files_state`; `downgrade()` restores durable states verbatim from the archive with a derived fallback for post-039 rows.
result: pass
<!-- test_migration_039_drop_files_state_column.py: 14 passed on :5433 (all 5 guard/archive/drop/downgrade cases) -->

### 6. `FileState` fully gone from the model; anti-drift guard + drift sentinel hold
expected: The `FileState` enum, `state` column, and `ix_files_state` index are removed from `models/file.py`; the model↔DB autogenerate diff is empty; a reintroduced `files.state` read/write is caught by the tokenize-based D-08 guard.
result: pass
<!-- git grep FileState in src → prose-only (comments/HTML/docstrings); git grep -l FileState tests → only the guard;
     test_no_filestate_guard.py: 3 passed (mutation-tested); test_039_autogenerate_diff_is_empty_for_dropped_objects: 1 passed -->

### 7. Regression suite green on the derived sources
expected: The broader test surface (migration/cutover/integration) passes against the derived authority with `files.state` gone — no behavioral regression.
result: pass
<!-- tests/integration bucket: 239 passed (independently re-run 2026-07-13);
     full suite reported 3443 passed at phase close (90-02-SUMMARY); combined coverage 97.33% ≥ 95% (validate-phase) -->

### 8. Static quality gates clean
expected: mypy and ruff pass with the model/enum/subsystem removed — no unresolved reference, no unused import, no lint regression.
result: pass
<!-- uv run mypy . → Success: no issues found in 208 source files; uv run ruff check . → All checks passed! -->

### 9. Migration 039 rehearsal against a real-corpus restore (ROADMAP success-criterion 3)
expected: Restore a real prod snapshot → apply 032→038 → shadow-compare green on the DRAINED corpus (`--profile drain`) → run 039 → assert `files_state_archive` one-row-per-file and `files.state`/`ix_files_state` gone → `downgrade()` → assert verbatim restore → record ACCESS EXCLUSIVE lock + DDL timing. Deploy order: through 038 → shadow-compare green (drained) → THEN 039 (prod is at Alembic 031; 039 is never its first migration).
result: blocked
blocked_by: release-build
reason: "Requires a restore of live prod data (~11,428 rows @ Alembic 031); cannot run in CI or against the ephemeral test DB. Intentional pre-DEPLOY operator gate — documented in 90-03-PLAN.md <verification> and 039's downgrade() docstring. Tracked in 90-HUMAN-UAT.md; not required for phase code-completion."

### 10. Post-deploy live dashboard / analyze-workspace browser render
expected: After the drain lifts post-deploy, load the pipeline dashboard and confirm Staged(pushing)/Analyzing(cloud) counts, analyze-workspace states, the failed-count card, and search (facet removed) all render correctly on live traffic.
result: blocked
blocked_by: release-build
reason: "Browser UAT — no automated test exercises the rendered HTMX/Jinja templates on live traffic (memory project_htmx_hxon_alpine_scope_trap). Requires a live post-deploy environment. Pre-deploy/post-deploy operator gate, not a code gap."

## Summary

total: 10
passed: 8
issues: 0
pending: 0
skipped: 0
blocked: 2

## Gaps

[none — 8/8 runnable tests passed; 2 blocked items are operator-only deploy gates, not code defects]
