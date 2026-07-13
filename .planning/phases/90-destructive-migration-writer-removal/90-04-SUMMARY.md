---
phase: 90-destructive-migration-writer-removal
plan: 04
subsystem: model-enum-retirement
tags: [MIG-04, PR-C, FileState-removal, shadow-compare-retirement, derived-state, D-08-guard, mutation-tested]
requires:
  - phase: 90-01
    provides: "Every live FileRecord.state reader derives from output tables (markers / cloud_job)"
  - phase: 90-02
    provides: "All FileRecord.state writers removed; files.state written by nothing"
  - phase: 90-03
    provides: "Migration 039 landed (irreversible drop of ix_files_state + files.state)"
provides:
  - "The FileState StrEnum, the files.state column, and the ix_files_state index are DELETED from the ORM; models/__init__ no longer re-exports FileState; pipeline.py FileState import + PIPELINE_STAGES list removed"
  - "The dead shadow_compare subsystem (service + CLI) is FULLY removed with no relocation"
  - "The ~91 dependent test files are migrated to the derived authority (markers / cloud_job); git grep -l FileState tests == 0 (only the D-08 guard names the token)"
  - "A mutation-tested tokenize-based D-08 anti-drift guard forbids any executable FileState/FileRecord.state/files.state/.values(state=) reappearance in src/phaze"
affects:
  - "Phase 90 (MIG-04) is complete and ready for verification"
tech-stack:
  added: []
  patterns:
    - "Retire a vestigial file.state assertion when the derived authority (cloud_job.status / analysis.failed_at / analysis_completed_at / DedupResolution / FileMetadata marker) is co-asserted; migrate scoping checks to the marker rather than delete coverage"
    - "Derive a cloud-drain held/dispatched label purely from the cloud_job sidecar (held = no in-flight row OR awaiting; dispatched = any non-awaiting row); refresh identity-mapped rows via populate_existing, NOT expire_all (which lazy-reloads the caller's FileRecords)"
    - "tokenize-blank COMMENT + STRING (incl. f-string literal) tokens before a source-scan guard so ~20 docstring FileState mentions cannot self-fail it; a #-only strip is insufficient"
key-files:
  created:
    - tests/shared/test_no_filestate_guard.py
  modified:
    - src/phaze/models/file.py
    - src/phaze/models/__init__.py
    - src/phaze/services/pipeline.py
    - tests/conftest.py
  deleted:
    - src/phaze/services/shadow_compare.py
    - src/phaze/cli/shadow_compare.py
    - tests/integration/test_shadow_compare.py
    - tests/integration/test_shadow_compare_skipped.py
    - tests/shared/test_shadow_compare_cli.py
    - tests/shared/test_shadow_compare_readonly.py
    - tests/integration/test_dedup_divergence.py
    - tests/integration/test_pending_set_divergence.py
    - tests/shared/test_dedup_fingerprint_source_scan.py
    - tests/shared/test_pending_set_source_scan.py
    - tests/shared/test_proposals_cutover_source_scan.py
    - tests/shared/test_reenqueue_reconcile_source_scan.py
decisions:
  - "shadow_compare: FULL REMOVAL, no relocation (couplings are prose; dependency runs shadow_compare->stage_status; hard invariants frozen inline in migration 039)"
  - "test_dedup_resolve_undo_shadow: MIGRATED, not retired (its stale-replay/concurrent-CAS/malformed-payload coverage is service-level and NOT duplicated by PR-A's router-level test_duplicates)"
  - "The 4 per-file AST source-scan guards are RETIRED as strictly superseded by the global D-08 guard (which forbids executable FileState across ALL of src/phaze); no src coverage lost (they are pure source-text scans)"
  - "SearchResult DTO keeps its own `state` field (a distinct search-union slot, D-11); the guard's scoped regexes correctly ignore it"
metrics:
  duration_min: 300
  tasks: 4
  files_changed: 101
  insertions: 710
  deletions: 3686
  completed: 2026-07-12
---

# Phase 90 Plan 04: Model/Enum Retirement + shadow_compare Cleanup (PR-C finale, MIG-04) Summary

**Deleted the `FileState` enum + `files.state` column + `ix_files_state` index from the ORM (matching the shipped migration 039), fully retired the now-void `shadow_compare` subsystem with no relocation, migrated ~91 dependent test files off `FileState`/`state=` seeds to the derived authority PR-A/PR-B established, and added a mutation-tested tokenize-based D-08 anti-drift guard — flipping the `test_039_autogenerate_diff_is_empty_for_dropped_objects` sentinel GREEN with both coverage gates (combined 97.33% ≥ 95%; all modules ≥ 90%) intact.**

## What shipped (by task)

- **Task 1 — shadow_compare retirement (`e580028c`):** Deleted `services/shadow_compare.py` + `cli/shadow_compare.py` wholesale (verified safe: the couplings in `pipeline.py`/`backends.py`/`stage_status.py` are 100% prose; the dependency runs shadow_compare → stage_status, not the reverse; the 13 hard invariants are frozen inline in migration 039). Retired the dedicated-subsystem tests + the readonly source-guard + the two unseedable divergence tests. **MIGRATED** `test_drain_double_dispatch` (ROADMAP double-dispatch hard gate — stripped the fake stubs' state dual-writes + the AWAITING_CLOUD seed; kept the ledger exactly-once / never-cloud core) and `test_dedup_resolve_undo_shadow` (see diff verdict below).
- **Task 2 — central lever + ~91-file sweep (`f51d599f`, `003f3e95`, `a061fb0b`, `346dd8b6`, `ac3f81e9`):** Dropped `make_file`'s `state=` param + the 8 sibling factories' `state=` kwargs in `conftest.py` (the derived markers they already seed are the authority), then swept every FileState-referencing test file. Retired the 4 per-file AST source-scan guards (superseded by the D-08 global guard). `git grep -l FileState tests` reaches 0 (only the D-08 guard names the token).
- **Task 3 — model surface deletion (`90e71672`):** Removed the `FileState` StrEnum (+ `import enum`), the `state` mapped_column, and the `ix_files_state` index from `models/file.py`; dropped the `FileState` import/`__all__` re-export from `models/__init__.py`; removed the `FileState` import + `PIPELINE_STAGES` list from `services/pipeline.py`. mypy + ruff + `import phaze` green; `test_039_autogenerate_diff_is_empty_for_dropped_objects` flipped GREEN.
- **Task 4 — mutation-tested D-08 guard (`9857ca55`):** Added `tests/shared/test_no_filestate_guard.py` (tokenize-based; see the verbatim mutation run below).

## `test_dedup_resolve_undo_shadow` — migrate-vs-retire verdict

**Verdict: MIGRATED (not retired).** Diffed against PR-A's id-only resolve→undo regressions (`90-01` commit `c6f0a040`, which added `tests/review/routers/test_duplicates.py`). PR-A's coverage is **router-level** (`/resolve`→`/undo` round-trips). This file carries **service-level** coverage that is NOT duplicated there: the stale-replay CAS no-op, the concurrent-double-submit `on_conflict_do_nothing` conflict, the duplicate-entry RETURNING cardinality, and the malformed/uuid-typed/null-`previous_state` payload branches — all against `resolve_group`/`undo_resolve` directly. Stripped the `run_shadow_compare(...).hard_fail_total == 0` and scalar `dup.state == …` assertions; kept the marker-set (`DedupResolution`) and `restored`-count coverage. No net coverage loss.

## D-08 guard manual mutation run (VERBATIM — feedback_mutation_test_guard_tests: a green guard proves nothing)

1. Baseline GREEN: `pytest tests/shared/test_no_filestate_guard.py` → `3 passed`.
2. Backed up `src/phaze/services/pipeline.py` to a scratch path (NOT `git restore` — 90-01 lost uncommitted work that way).
3. Injected a **multi-line** `.values(state=…)` into a pipeline.py function body (compiled but not executed at import, so it imports cleanly and exercises the SECONDARY regex, not the import-fail PRIMARY guard):
   ```python
   _mutant_stmt = (
       update(FileRecord)
       .values(
           state="pushing",
       )
   )  # noqa
   ```
4. RED confirmed: `test_no_filestate_in_src` FAILED —
   `AssertionError: executable FileState reintroduced in src/phaze (D-08): …/pipeline.py: matched /\.values\([^)]*\bstate\s*=/` (`1 failed`).
5. Restored from the scratch backup; `git diff --stat src/phaze/services/pipeline.py` empty (clean); guard back to `3 passed`.

An earlier mutation attempt (`_MUTANT = update(FileRecord).values(state='pushing')` at module level) surfaced the PRIMARY guard instead — `NameError: name 'update' is not defined` at import — confirming that both the type/import layer AND the source-scan regex catch a reintroduction.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Raw string-literal `files.state` seeds the FileState token-grep missed (`e726d119`)**
- **Found during:** Task 3 verification (integration + identify buckets: 64 + 1 failures)
- **Issue:** `tests/integration/test_files_page.py` and `tests/integration/test_stage_status_equivalence.py` seeded `FileRecord(state="discovered")` with a RAW STRING (no `FileState` token), so `git grep FileState` never flagged them. Once the column was dropped they `TypeError: 'state' is an invalid keyword argument for FileRecord`; the equivalence file's shared `_seed` helper cascaded to ~60 failures.
- **Fix:** Removed the `state="discovered"` kwarg from both FileRecord constructors. Verified via `grep -rE 'state\s*=\s*"'` that no other raw-literal FileRecord seed survives (the migration tests' `WHERE state = '…'` raw SQL is CORRECT — those test historical migrations against the pre-039 versioned schema and must keep it).
- **Commit:** `e726d119`

**2. [Rule 1 - Bug] Over-removed a SearchResult DTO field during the string-literal fix (`e726d119`)**
- **Found during:** the same verification pass (`test_search_queries::test_fields`)
- **Issue:** The initial removal also stripped `state="discovered"` from a `SearchResult(...)` construction. `SearchResult.state` is a DISTINCT, still-live search-union slot (D-11 column-parity), NOT `files.state`.
- **Fix:** Restored the `SearchResult` field. The D-08 guard's scoped regexes correctly ignore `SearchResult.state` (only `FileRecord.state`/`files.state`/bare `FileState` are forbidden).

### Deferred / structural notes

- **`_states_for` MissingGreenlet:** `test_staging_cron`'s derived `_states_for` initially used `session.expire_all()`, which expired the caller's FileRecords and lazy-reloaded `.id` outside the greenlet. Switched to `select(CloudJob)…execution_options(populate_existing=True)` — refreshes the drain-mutated cloud_job rows without expiring anything else (mirrors the 90-02 fix).
- **Test-DB pollution:** an interrupted bucket run left a committed `test-fileserver` agent (`pk_agents` UniqueViolation, a documented flake); resolved by `just test-db-down && just test-db`, not a code regression.

## Known Stubs

None — no stubs introduced. The `SearchResult.state` search-union slot is a pre-existing intentional field (D-11), not a stub.

## Verification

- `uv run python -c "import phaze; import phaze.cli"` — OK (no dangling shadow import; CLI clean).
- `uv run ruff check .` — All checks passed. `uv run mypy .` — Success, 208 source files.
- `git grep -l FileState tests` → **only `tests/shared/test_no_filestate_guard.py`** (the guard names the token it forbids; all ~91 migrated test files are clean).
- `git grep -ln "…shadow_compare import…" src/phaze tests` → NONE; `services/shadow_compare.py` + `cli/shadow_compare.py` deleted; `test_drain_double_dispatch.py` still present and green.
- Buckets (isolated, port 5433, +asyncpg): discovery 172 · metadata 85 · fingerprint 84 · identify 229 · review 431 · agents 463 · integration 239 · analyze 576 · shared 1075 — **all passed**.
- `test_039_autogenerate_diff_is_empty_for_dropped_objects` — GREEN (full 039 file: 14 passed).
- `just coverage-combine` — `coverage report --fail-under=95` TOTAL **97.33%**; `scripts/coverage_floor.py` **✅ All tracked modules ≥ 90%**.

## Scope boundary

Did NOT touch migration 039 or the shipped 90-01/02/03 artifacts. STATE.md / ROADMAP.md untouched (orchestrator-owned).

## Self-Check: PASSED
- Commits FOUND: e580028c, f51d599f, 003f3e95, a061fb0b, 346dd8b6, ac3f81e9, 90e71672, 9857ca55, e726d119
- Key files FOUND: src/phaze/models/file.py (FileState/state/ix_files_state removed), tests/shared/test_no_filestate_guard.py (created), tests/conftest.py (lever)
- Deleted files CONFIRMED absent: services/shadow_compare.py, cli/shadow_compare.py
- `git grep FileState tests` == 1 (the guard only); shadow importers == 0; 039 autogen GREEN; both coverage gates GREEN
