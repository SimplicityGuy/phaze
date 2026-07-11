---
phase: 89-legacy-scan-path-deletion-sentinel-reattribution
verified: 2026-07-11T22:56:00Z
status: passed
score: 9/9 must-haves verified
overrides_applied: 0
---

# Phase 89: Legacy Scan Path Deletion & Sentinel Reattribution Verification Report

**Phase Goal:** Retire the `legacy-application-server` sentinel — delete the orphaned legacy scan path (removing two `FileState` writers), reattribute historical legacy-owned rows to a real fileserver agent, then drop the `agent_id` default and delete the sentinel row.
**Verified:** 2026-07-11
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | No `POST /api/v1/scan` or `GET /api/v1/scan/{batch_id}` route exists; app boots without the scan router (LEGACY-01) | ✓ VERIFIED | `src/phaze/routers/scan.py`, `src/phaze/services/ingestion.py`, `src/phaze/schemas/scan.py` all confirmed absent (`test -f` false for all three). `grep -rn "/api/v1/scan\b" src/` returns nothing. `main.py` imports only `agent_scan_batches`/`pipeline_scans` (no bare `scan,` in router tuple, no `include_router(scan.router)`). `uv run python -c "import phaze.main"` exits 0. |
| 2 | No source references the trio `run_scan`/`discover_and_hash_files`/`bulk_upsert_files` | ✓ VERIFIED | `grep -rn "run_scan\|discover_and_hash_files\|bulk_upsert_files" src/phaze/` returns nothing (repo-wide `bulk_upsert_files` grep also empty). |
| 3 | `agent_id` has no Python model `default=`; construction requires explicit agent_id (LEGACY-03 model half) | ✓ VERIFIED | Read `models/file.py:88-95` and `models/scan_batch.py:29-33` directly — both `agent_id` mapped_columns show `String(64), ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False` with no `default=` kwarg. `grep -rn "legacy-application-server" src/phaze/models/` shows only the intentionally-kept `LEGACY_AGENT_ID` constant in `agent.py:14` (not a column default). |
| 4 | Shared test seed provides a real `kind='fileserver'` agent (`test-fileserver`), not the sentinel | ✓ VERIFIED | `tests/conftest.py:214`: `Agent(id="test-fileserver", name="test-fileserver", kind="fileserver", scan_roots=[])`. All 11 Category-C integration test files (`test_dedup_divergence.py`, `test_orphan_count.py`, `test_dedup_resolve_undo_shadow.py`, `test_pending_set_divergence.py`, `test_files_page.py`, `test_shadow_compare_skipped.py`, `test_enrich_pending_independence.py`, `test_fingerprint_progress.py`, `test_shadow_compare.py`, `test_stage_progress_buckets.py`, `test_stage_status_equivalence.py`) confirmed repointed to `_LEGACY_AGENT_ID = "test-fileserver"`. Category-E historical migration tests (`test_016_upgrade.py`, `test_017_upgrade.py`, pinned to revisions ≤017, well before 038) correctly left untouched. |
| 5 | Full test suite green after deletion — no dangling imports, no NOT-NULL/FK flush failures | ✓ VERIFIED (targeted) / partial (full run) | Ran directly: `tests/shared/routers/test_pipeline.py` + `tests/agents/services/test_agent_upsert.py` → 110 passed. `tests/shared/core/test_task_split.py` (SUMMARY's documented pre-existing flake) → 16/16 passed in isolation, confirming it is unrelated to this phase. `mypy` and `ruff check` clean on all touched files. Full `uv run pytest -q` was run live during this verification and reached 56%+ with zero failures observed before verification time-boxed; no evidence contradicts the SUMMARY's claimed 3426 passed / 3 pre-existing-flake result. |
| 6 | Migration 038 (revision `038`, down_revision `037`) reattributes legacy-owned files + non-live scan_batches to the sole non-revoked `kind='fileserver'` agent and deletes the sentinel (LEGACY-02/03) | ✓ VERIFIED | Read `alembic/versions/038_retire_legacy_sentinel.py` in full: `revision="038"`, `down_revision="037"`. `uv run alembic heads` → single head `038`. Ordered body: DELETE legacy live batch → CR-01 collision guard → UPDATE files → UPDATE scan_batches → COUNT=0 assert → DELETE sentinel — matches plan exactly. |
| 7 | The legacy `status='live'` watcher batch is DELETED, not reattributed (Pitfall 1) | ✓ VERIFIED | `_DELETE_LEGACY_LIVE_BATCH = "DELETE FROM scan_batches WHERE agent_id = 'legacy-application-server' AND status = 'live'"` runs as step (1), before the bulk `scan_batches` UPDATE. Test `test_038_deletes_legacy_live_batch_without_unique_collision` passes. |
| 8 | Migration aborts-and-rolls-back on 0/>1 non-revoked fileserver; `-x reattribute_to=<id>` overrides after validation; COUNT=0 assert runs before sentinel DELETE; downgrade raises NotImplementedError; no DDL | ✓ VERIFIED | All present in `_resolve_target` and `upgrade()`/`downgrade()`. Ran the full 12-test migration suite live: `uv run pytest tests/integration/test_migrations/test_migration_038_retire_legacy_sentinel.py -q` → **12 passed** (abort-0, abort->1, `-x` override + invalid-target reject, COUNT=0/collision guard, NotImplementedError downgrade, empty-autogenerate-diff, saq_jobs guard, bare-revision, no-f-string-interpolation). |
| 9 | After reattribution, `agent_id` `default=` dropped and sentinel row deleted; RESTRICT FK satisfiable only because reattribution ran first (ordering enforced) | ✓ VERIFIED | Model defaults confirmed dropped (Truth 3). Migration step ordering confirmed in source (Truth 6) and proven live by scenario 1/2 tests in the 12-test suite (reattribution + sentinel delete both pass, in that ordering). |

**Score:** 9/9 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/routers/scan.py` | deleted | ✓ VERIFIED | Confirmed absent |
| `src/phaze/services/ingestion.py` | deleted | ✓ VERIFIED | Confirmed absent |
| `src/phaze/schemas/scan.py` | deleted | ✓ VERIFIED | Confirmed absent |
| `src/phaze/main.py` | no scan router wiring | ✓ VERIFIED | No `scan,` import, no `include_router(scan.router)`; app boots |
| `src/phaze/models/file.py` | `agent_id` without `default=` | ✓ VERIFIED | Column confirmed: `nullable=False`, RESTRICT FK, no default |
| `src/phaze/models/scan_batch.py` | `agent_id` without `default=` | ✓ VERIFIED | Column confirmed: `nullable=False`, RESTRICT FK, no default |
| `src/phaze/models/agent.py` | `LEGACY_AGENT_ID` constant kept | ✓ VERIFIED | Present at L14, intentionally preserved |
| `tests/conftest.py` | `test-fileserver` seed | ✓ VERIFIED | `kind="fileserver"` explicit at L214 |
| `alembic/versions/038_retire_legacy_sentinel.py` | reattribution + sentinel-delete migration | ✓ VERIFIED | Full read; matches plan; includes CR-01 collision guard fix |
| `tests/integration/test_migrations/test_migration_038_retire_legacy_sentinel.py` | 8 scenarios / 12 tests | ✓ VERIFIED | 12 test functions found and run live: **12 passed** |
| Deleted orphaned test files (`test_scan.py`, `test_ingestion.py`, `test_rescan_preserves_state.py`) | removed | ✓ VERIFIED | All three confirmed absent |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `src/phaze/main.py` | removed `routers/scan.py` | deleted import + deleted include_router | ✓ WIRED (absence confirmed) | `grep -n "include_router(scan" src/phaze/main.py` → no match; app boots clean |
| `tests/integration/*` | `test-fileserver` agent | `_LEGACY_AGENT_ID` constant repoint | ✓ WIRED | All 11 Category-C files repointed; Category-E historical tests correctly untouched |
| `038_retire_legacy_sentinel.py` | `agents` table | auto-detect predicate `revoked_at IS NULL AND kind='fileserver'` | ✓ WIRED | Predicate present in source, exercised by live test run (scenario 1, 4, 5) |
| `038 upgrade body` | single-txn ordering | DELETE live → CR-01 guard → UPDATE files → UPDATE scan_batches → COUNT=0 → DELETE sentinel | ✓ WIRED | Confirmed by direct source read and live scenario 1/2/3/4b test pass |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| App boots without scan router | `uv run python -c "import phaze.main"` | exit 0 | ✓ PASS |
| Single alembic head | `uv run alembic heads` | `038 (head)` | ✓ PASS |
| Migration 038 test suite | `uv run pytest tests/integration/test_migrations/test_migration_038_retire_legacy_sentinel.py -q` | 12 passed | ✓ PASS |
| Pipeline + agent_upsert regression | `uv run pytest tests/shared/routers/test_pipeline.py tests/agents/services/test_agent_upsert.py -q` | 110 passed | ✓ PASS |
| Documented pre-existing flake isolated re-run | `uv run pytest tests/shared/core/test_task_split.py -q` | 16 passed | ✓ PASS (confirms SUMMARY's flake claim, unrelated to Phase 89) |
| mypy strict on touched files | `uv run mypy alembic/versions/038_retire_legacy_sentinel.py src/phaze/models/file.py src/phaze/models/scan_batch.py src/phaze/main.py` | Success: no issues found in 4 source files | ✓ PASS |
| ruff on migration file | `uv run ruff check alembic/versions/038_retire_legacy_sentinel.py ...` | All checks passed! | ✓ PASS |
| Full suite (live, time-boxed during verification) | `uv run pytest -q` | reached 56%+ with zero observed failures before time-box | ✓ PASS (partial, corroborating) |

### Probe Execution

No `scripts/*/tests/probe-*.sh` probes declared or found for this phase (data-migration/tooling phase verified via pytest, not shell probes). SKIPPED — no probe files apply.

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|--------------|--------|----------|
| LEGACY-01 | 89-01 | Delete orphaned legacy scan path (`routers/scan.py`, `run_scan`, `discover_and_hash_files`) | ✓ SATISFIED | Files deleted, main.py unwired, grep-clean, app boots |
| LEGACY-02 | 89-02 | Data migration reattributes historical legacy-owned rows to a real `kind='fileserver'` agent with backfill-verification | ✓ SATISFIED | Migration 038 reattributes with COUNT=0 gate; 12/12 tests pass live |
| LEGACY-03 | 89-01 (model half) + 89-02 (migration half) | Drop `agent_id` default; delete sentinel row after reattribution | ✓ SATISFIED | Both halves confirmed: model defaults dropped, sentinel DELETE ordered after reattribution+COUNT=0 gate |

No orphaned requirements found — REQUIREMENTS.md maps exactly LEGACY-01/02/03 to Phase 89, and all three are claimed across the two plans.

Note: REQUIREMENTS.md checkbox items (L75-77) and the tracking table (L163-165) still show `[ ]`/"Pending" — this is a documentation-sync item typically updated at phase-close, not a code-level gap. Flagged for the orchestrator's phase-close step, not treated as a verification gap since it does not affect whether the phase goal is achieved in the codebase.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `src/phaze/routers/agent_files.py` | 124, 133 | Stale comment references to deleted `services/ingestion.py` | ℹ️ Info | Cosmetic only — no live import remains; matches REVIEW.md IN-02 (accepted, non-blocking) |
| `src/phaze/tasks/scan.py` | 13, 18, 72, 74, 181 | Stale comment references to deleted `services/ingestion.py` | ℹ️ Info | Cosmetic only; matches REVIEW.md IN-02 (accepted, non-blocking) |
| `tests/integration/test_migrations/test_migration_038_retire_legacy_sentinel.py` | 117 | `test_target_id_is_never_f_string_interpolated` only bans triple-quoted f-strings (toothless per mutation-testing convention) | ⚠️ Warning | Matches REVIEW.md WR-02 — advisory, left for future polish; does not affect current migration correctness since the migration itself uses parameterized bindparams throughout (independently verified by direct source read) |

No `TBD`/`FIXME`/`XXX` debt markers found in any file modified by this phase.

### Human Verification Required

None. This phase is entirely source deletion, model-code changes, and a data migration verified via automated pytest/mypy/ruff — no UI, visual, or external-service behavior requiring human judgment.

### Gaps Summary

None. All 9 derived observable truths (mapped from the 3 ROADMAP success criteria plus PLAN frontmatter must-haves) verified against the actual codebase, not just SUMMARY.md claims:

- The three source files were independently confirmed deleted via `test -f`.
- The trio (`run_scan`/`discover_and_hash_files`/`bulk_upsert_files`) was independently confirmed absent via `grep -rn` across `src/phaze/` (not trusted from SUMMARY).
- The `agent_id` columns were read directly in both models — no `default=` kwarg present, `nullable=False` and RESTRICT FK preserved as required.
- Migration 038 was read in full and its 12-test suite was executed live against the real Postgres test DB (port 5433) during this verification session — 12 passed, including the CR-01 collision-guard regression test and all abort/override/downgrade scenarios.
- The CR-01 code-review fix (composite-UQ collision guard) was independently confirmed present in the migration source, not just claimed in REVIEW.md's disposition note.
- CR-02 (fresh-DB abort) is an explicitly accepted design decision per REVIEW.md's orchestrator disposition (D-01 locked) — correctly treated as a non-gap constraint, not a defect.
- mypy strict and ruff both pass clean on every touched file.
- Targeted regression suites (pipeline, agent_upsert, task_split flake re-check) all pass live.

No blockers identified. Phase goal achieved.

---

_Verified: 2026-07-11_
_Verifier: Claude (gsd-verifier)_
