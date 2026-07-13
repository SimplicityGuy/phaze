---
phase: 90-destructive-migration-writer-removal
verified: 2026-07-13T05:58:55Z
status: human_needed
score: 9/9 must-haves verified
overrides_applied: 0
human_verification:
  - test: "Migration rehearsal against a real-corpus restore (ROADMAP success criterion 3)"
    expected: "Restore a real prod snapshot, apply 032-038, run shadow-compare green on the drained corpus, run 039, assert files.state/ix_files_state gone and files_state_archive row count matches the pre-drop file count, downgrade, assert durable states restored verbatim, record lock-acquisition/DDL timing."
    why_human: "This is an operator runbook step against production data/infrastructure (a real corpus restore) — it cannot be executed or observed from the repository. It is DOCUMENTED (90-03-PLAN.md verification block, migration 039 downgrade() docstring) but not, and cannot be, executed in this environment."
---

# Phase 90: Destructive Migration & Writer Removal Verification Report

**Phase Goal:** The gated, last, highest-risk step — after the shadow-compare is green on the live corpus and the cloud-push lanes are drained/quiesced, drop `ix_files_state`, drop `files.state`, delete the `FileState` enum, and remove the remaining `.state=` writers (readers before writers, always).
**Verified:** 2026-07-13T05:58:55Z
**Status:** human_needed
**Re-verification:** No — initial verification

This phase was delivered across four plans: 90-01 (PR-A, readers-first cutover), 90-02 (PR-B, writer removal), 90-03 (PR-C migration, partial — Task 3 split), 90-04 (PR-C finale — model/enum retirement + shadow_compare removal + test migration + D-08 guard). Verified as a combined unit per the phase's declared execution shape.

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Destructive migration 039 archives files.state, delta-tops-up, and drops ix_files_state + files.state in ONE transaction under a lock_timeout + savepoint-retry guard (ROADMAP SC1) | VERIFIED | `alembic/versions/039_drop_files_state_column.py` read in full: `_guard()` runs first (mid-flight + shadow-compare anti-join COUNTs, raises RuntimeError on violation), `files_state_archive` created+filled, delta top-up, `_drop_state_ddl()` wraps drop in `begin_nested()` + `SET LOCAL lock_timeout='2s'` with 5-attempt backoff on SQLSTATE 55P03. All in one `upgrade()` body under env.py's single outer transaction. |
| 2 | 039 self-guards on data (mid-flight / shadow-violation) but passes clean on empty DB (D-06) | VERIFIED | Both COUNT queries evaluate to 0 on an empty `files` table by construction (no rows to match); `tests/integration/test_migrations/test_migration_039_drop_files_state_column.py::test_039_passes_cleanly_on_empty_files_table` present and passing (see spot-check below). |
| 3 | Guard SQL is inline, transcribed (not imported) from shadow_compare; migration never imports phaze.* or references saq_jobs/scheduling_ledger (D-07) | VERIFIED | `grep -c "import phaze\|from phaze" 039...py` == 0. The two `saq_jobs`/`scheduling_ledger` string hits are both self-documenting comments ("NEVER references saq_jobs...") — no actual reference. |
| 4 | downgrade() restores files.state verbatim from files_state_archive (primary), with the D-04/D-05 derived-fallback documented for post-039 rows (D-10) | VERIFIED | `downgrade()` recreates column+index, runs `_RESTORE_FROM_ARCHIVE` (verbatim UPDATE FROM archive) then `_DERIVED_FALLBACK` (CASE reconstruction scoped `WHERE NOT EXISTS (...archive...)`) — exactly the primary+fallback split required. Docstring (migration header lines 41-48) enumerates the lossy transient cases. |
| 5 | FileState enum, state column, and Index('ix_files_state') deleted from models/file.py; models/__init__ no longer re-exports FileState; no src/ code has an EXECUTABLE FileState reference | VERIFIED | `git grep -c "class FileState" src/phaze/models/file.py` == 0; column/index absent from the model (read in full — only `id/sha256_hash/original_path/.../file_metadata` + two unrelated indexes remain). `models/__init__.py` exports only `FileRecord`, not `FileState`. `git grep -l FileState src/phaze` returns 20 files, all confirmed by direct grep to be comment/docstring prose only (spot-checked every hit). `import phaze; import phaze.cli` succeeds. |
| 6 | Remaining FileState writers removed; codebase no longer imports FileState for writing (grep-guarded) (ROADMAP SC2) | VERIFIED | Same evidence as #5 plus the D-08 guard (`tests/shared/test_no_filestate_guard.py`) — independently re-run and MUTATION-TESTED by this verifier (see below), not merely trusted from SUMMARY. |
| 7 | `git grep -l FileState tests` returns only the guard file itself | VERIFIED | Ran directly: `git grep -l FileState tests` → `tests/shared/test_no_filestate_guard.py` only. |
| 8 | D-08 guard is mutation-meaningful (tokenize-strips comments+strings, scoped regexes, multi-line `.values(state=)` detection, planted-match self-test with negative lookalikes) | VERIFIED | Read guard source in full: uses `tokenize.generate_tokens` to blank COMMENT+STRING(+fstring) tokens before scanning; forbids `FileState`, `FileRecord.state`, `files.state` (scoped, not bare `.state`), and DOTALL `.values([^)]*state=)`; has `test_guard_flags_planted_matches` (positive+negative) and `test_guard_strips_docstring_and_comment_filestate_mentions`. INDEPENDENTLY MUTATED by this verifier: injected a real multi-line `.values(state="pushing")` block into `routers/pipeline.py` (with `update`/`FileRecord` already imported) — guard went RED with the exact expected AssertionError; reverted via `git checkout --`, guard back to 3 passed. This is real evidence, not a re-statement of the SUMMARY's claim. |
| 9 | Migration downgrade() documents reconstruction; a rehearsal against a real-corpus restore is DOCUMENTED as a manual operator runbook step (ROADMAP SC3) | VERIFIED (documentation) / HUMAN NEEDED (execution) | downgrade() reconstruction is documented in the migration docstring (verbatim primary + CASE fallback, lossy cases enumerated). The rehearsal step is explicitly written as a MANUAL runbook item in `90-03-PLAN.md`'s `<verification>` block: "MANUAL (operator, runbook — record in VERIFICATION): rehearsal against a real-corpus restore...". Per this phase's verification scope, only the DOCUMENTATION of this step is required, not its execution — routed to human_verification below since it cannot be run against a real prod corpus in this environment. |

**Score:** 9/9 truths verified (1 of the 9 carries a mandatory human-execution component, routed to Human Verification below per the ROADMAP's own framing of it as a manual step).

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `alembic/versions/039_drop_files_state_column.py` | Destructive migration: archive + delta top-up + guarded DDL drop + lossless-archive downgrade | VERIFIED | Read in full; matches all D-06/D-07/D-10 requirements; 14/14 integration tests pass (independently re-run, see below) |
| `tests/integration/test_migrations/test_migration_039_drop_files_state_column.py` | upgrade guard (violation→raise, empty→pass, mid-flight→raise) + archive populated + DDL gone + downgrade restores | VERIFIED | 8 named test functions covering all 5 required cases + 6 static/contract asserts; re-ran directly: 14 passed |
| `tests/shared/test_no_filestate_guard.py` | Mutation-tested source-grep anti-drift guard | VERIFIED | Re-ran; independently mutation-tested by this verifier (RED confirmed, then restored) |
| `src/phaze/models/file.py` | FileState class + state column + ix_files_state index removed | VERIFIED | File read in full; column/class/index absent, only a docstring note remains |
| `src/phaze/services/shadow_compare.py`, `src/phaze/cli/shadow_compare.py` | Fully removed | VERIFIED | `ls` confirms both absent; `git grep` for importers of either returns nothing in src or tests |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `src/phaze/models/file.py` (column/index removal) | `alembic/versions/039_drop_files_state_column.py` | model↔migration consistency via autogenerate | WIRED | `test_039_autogenerate_diff_is_empty_for_dropped_objects` — re-ran directly, PASSES (was documented RED in 90-03, now GREEN per 90-04) |
| `tests/shared/test_no_filestate_guard.py` | `src/phaze/**` | tokenize-stripped source scan | WIRED | Re-ran + mutation-verified by this verifier independently |
| `tests/review/routers/test_duplicates.py` round-trip tests | `services/dedup.py undo_resolve` | server-rendered `file_states` payload → marker DELETE | WIRED | Re-ran `-k "roundtrip or undo"`: 6 passed |
| `tests/integration/test_drain_double_dispatch.py` | `services/backends.py` / `scheduling_ledger` | ledger exactly-once dispatch, migrated off FileState | WIRED | File exists, no FileState reference, re-ran: 3 passed |
| `tests/integration/test_dedup_resolve_undo_shadow.py` | `services/dedup.py` | marker-based coverage (migrated, not retired, per 90-04 diff verdict) | WIRED | Re-ran: 9 passed |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| `import phaze` succeeds (no dangling FileState/shadow_compare import) | `uv run python -c "import phaze; import phaze.cli"` | `OK` | PASS |
| mypy clean (no unresolved FileState reference) | `uv run mypy .` | `Success: no issues found in 208 source files` | PASS |
| ruff clean | `uv run ruff check .` | `All checks passed!` | PASS |
| D-08 guard passes at baseline | `uv run pytest tests/shared/test_no_filestate_guard.py -q` | `3 passed` | PASS |
| D-08 guard catches a real reintroduction (independent mutation, not trusting SUMMARY) | inject `.values(state="pushing")` into `routers/pipeline.py`, re-run guard | `1 failed` — AssertionError naming the exact injected file/pattern | PASS (RED as expected) |
| D-08 guard restores to green after revert | `git checkout -- src/phaze/routers/pipeline.py` then re-run | `3 passed` | PASS |
| Migration 039 full integration suite | `MIGRATIONS_TEST_DATABASE_URL=...:5433 uv run pytest tests/integration/test_migrations/test_migration_039_drop_files_state_column.py -q` | `14 passed` | PASS |
| Drain double-dispatch hard gate | `uv run pytest tests/integration/test_drain_double_dispatch.py -q` | `3 passed` | PASS |
| Dedup resolve/undo shadow (migrated) | `uv run pytest tests/integration/test_dedup_resolve_undo_shadow.py -q` | `9 passed` | PASS |
| Dedup-undo round-trip regressions | `uv run pytest tests/review/routers/test_duplicates.py -q -k "roundtrip or undo"` | `6 passed` | PASS |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|--------------|-------------|-------------|--------|----------|
| MIG-04 | 90-01, 90-02, 90-03, 90-04 (all four) | Destructive migration drops ix_files_state/files.state/FileState enum after shadow-compare green + drain; downgrade() documents reconstruction + rehearsal | SATISFIED (code) / see human_verification for the rehearsal-execution component | All three ROADMAP success criteria (SC1 archiving/drop, SC2 writer removal, SC3 downgrade docs + rehearsal-documented) confirmed against the actual codebase, not SUMMARY claims. `.planning/REQUIREMENTS.md:98,162` maps MIG-04 to Phase 90; no orphaned requirements found for this phase. |

No orphaned requirements: `.planning/REQUIREMENTS.md` maps only MIG-04 to Phase 90, and all four plans declare `requirements: [MIG-04]`.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| (none) | - | Scanned `models/file.py`, `models/__init__.py`, `services/pipeline.py`, `services/dedup.py`, `services/backends.py`, `039_drop_files_state_column.py`, `test_no_filestate_guard.py` for TBD/FIXME/XXX/placeholder patterns | - | None found — no debt markers in phase-touched files |

Stale `.pyc` cache files for the deleted `shadow_compare` modules were found under `__pycache__/` directories (harmless build artifacts, not source; removed during this verification pass as housekeeping — not a code gap).

### Human Verification Required

### 1. Migration rehearsal against a real-corpus restore (ROADMAP success criterion 3)

**Test:** Restore a real production snapshot to a scratch environment, apply migrations 032→038, run shadow-compare green on the drained corpus, run migration `039`, assert `files.state`/`ix_files_state` are gone and `files_state_archive` row count equals the pre-drop file count, run `downgrade()`, assert durable states are restored verbatim from the archive, and record lock-acquisition/DDL timing.
**Expected:** The migration completes cleanly against real production-shaped data volume and the round-trip (upgrade→downgrade) is lossless for all durable states, with acceptable DDL lock timing.
**Why human:** This step requires access to a real production database snapshot/restore and live infrastructure timing measurement — it is inherently outside what this verifier (or any automated check against the repository) can execute. It is correctly scoped as a documented manual runbook step per the phase's own design (90-03-PLAN.md `<verification>` block and the migration's downgrade() docstring), not a coding gap. This finding does not block the phase goal being technically achieved in the codebase — it is the pre-existing, intentional manual gate before deploying 039 to production.

### Gaps Summary

No code-level gaps found. All 9 derived observable truths for MIG-04 were independently verified against the actual codebase (not SUMMARY claims): the model/enum/column/index deletion is real and complete, the destructive migration 039 has the required guard/archive/drop/downgrade structure and passes its full integration suite (14/14, independently re-run), all ~17 writers are gone, the D-08 anti-drift guard is genuinely mutation-meaningful (independently RED→GREEN verified by this verifier, not merely trusted from the SUMMARY), `git grep -l FileState tests` returns only the guard file, and mypy/ruff/import are all clean. The one remaining item — a migration rehearsal against a real-corpus restore — is by design a manual operator runbook step that cannot be executed in this environment; it is documented as required, which satisfies the phase's own success-criterion wording ("verify it is DOCUMENTED, not that it was executed"). Routing this to human_verification is the correct disposition per the escalation-gate pattern, not a defect.

---

_Verified: 2026-07-13T05:58:55Z_
_Verifier: Claude (gsd-verifier)_
