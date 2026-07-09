---
phase: 81-per-stage-failure-persistence-retry-paths
verified: 2026-07-09T00:00:00Z
status: passed
score: 4/4 must-haves verified
overrides_applied: 0
---

# Phase 81: Per-Stage Failure Persistence & Retry Paths Verification Report

**Phase Goal:** Make all three enrich stages persist a durable failure marker and gain a retry path — closing the latent bug where a failed metadata extraction records nothing and becomes invisible-and-permanently-ineligible.
**Verified:** 2026-07-09
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth (Success Criterion) | Status | Evidence |
|---|------|--------|----------|
| 1 | `analyze` failure persists a durable failure marker with an error reason, backfilled from existing `ANALYSIS_FAILED` rows | ✓ VERIFIED | `report_analysis_failed` (`src/phaze/routers/agent_analysis.py:329-370`) dual-writes `analysis.failed_at` + `analysis.error_message = f"{reason}: {error}"[:2000]` in the same transaction as `files.state = ANALYSIS_FAILED`. Backfill traced to migration `032` (`alembic/versions/032_add_derived_status_schema.py:73-82`, `_BACKFILL_ANALYZE_FAILED`), confirmed via `git log` to have shipped in Phase 77 (commit `faee8b8a`), NOT re-done by Phase 81. Phase 81's migration `033` adds only the mutual-exclusion CHECK + mixed-row cleanup (confirmed via `git log --follow`, commits `00c616c7`/`8f7b464d`, both on this branch). SC#1 is satisfied by the combination: Phase 77's backfill + Phase 81's go-forward writer + Phase 81's CHECK, exactly as the orchestrator's note anticipated. `tests/integration/test_migrations/test_migration_033_additive_check.py` and `tests/analyze/routers/test_agent_analysis_failure.py` pass (ran directly, 75/75 across the combined targeted run). |
| 2 | `report_metadata_failed` persists a durable metadata failure marker instead of nothing, so a terminally-failed metadata file is visible in derivation and counts | ✓ VERIFIED | `report_metadata_failed` (`src/phaze/routers/agent_metadata.py:99-157`) upserts a `FileMetadata` row with `failed_at=now()` + `error_message` (with a defined bodyless fallback `_BODYLESS_FAILURE_MESSAGE` for version-skew safety, D-10). `done(metadata)` (`services/stage_status.py:99-101`, `enums/stage.py:105-113`) requires `failed_at IS NULL`, so the row derives FAILED not DONE. Prior to this phase the endpoint persisted nothing (confirmed via 81-CONTEXT D-02 citing the pre-phase `agent_metadata.py:99`). `tests/metadata/routers/test_agent_metadata.py` passes (ran directly). |
| 3 | A terminally-failed metadata file has an operator retry path (backend endpoint), never a permanent dead-end blocking `propose` | ✓ VERIFIED | `POST /pipeline/metadata-failed/retry` (`src/phaze/routers/pipeline.py:968-1024`, `retry_metadata_failed`) exists, mirrors `retry_analysis_failed`'s guard ordering (resolve queue once → `NoActiveAgentError` guard, no default-queue fallthrough → `_enqueue_extraction_jobs` with the complete `ExtractMetadataPayload`). Backed by `get_metadata_failed_files` (`services/pipeline.py:1344-1361`), a correlated `exists()` query on `FileMetadata.failed_at IS NOT NULL`. `eligible(metadata)` already admits FAILED (`ELIGIBLE_AFTER_FAILURE[METADATA]=True`), so the re-enqueued file is runnable; `put_metadata`'s unconditional clear-on-success (`agent_metadata.py:79-91`, covering both the dumped and empty-body branches per D-13) wipes the marker once real metadata lands. `tests/integration/routers/test_pipeline_metadata_retry.py` passes (ran directly, 10 tests in the combined run). |
| 4 | `fingerprint` failure continues to persist via `fingerprint_results.status='failed'` (reused, not re-invented) and stays auto-retryable | ✓ VERIFIED | `put_fingerprint` (`src/phaze/routers/agent_fingerprint.py:22-64`) is unmodified as a writer and already upserts `status='failed'` per engine. `report_fingerprint_failed` (`:60-...`) persists NO row — confirmed by inspection and by running `tests/fingerprint/routers/test_agent_fingerprint_failure.py` directly (4 tests, all pass): row count unchanged across the terminal ack, no synthetic `engine='_task'` row ever appears, and `eligible({FINGERPRINT: FAILED}, FINGERPRINT)` returns `True` (`FAILURE_IS_TERMINAL[FINGERPRINT]=False`, `ELIGIBLE_AFTER_FAILURE[FINGERPRINT]=True`, both in `enums/stage.py:87-88`). `get_fingerprint_pending_files` (`services/pipeline.py:1362-1385`, pre-existing Phase 42 code, unmodified) already includes `FingerprintResult.status=='failed'` rows in the fingerprint pending/retry set, confirming the "stays auto-retryable" claim independent of any Phase 81 writer. |

**Score:** 4/4 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/enums/stage.py` | `FAILURE_IS_TERMINAL` + `ELIGIBLE_AFTER_FAILURE` tables, `domain_completed()` pure predicate | ✓ VERIFIED | Lines 87-88 (tables), 186-207 (`domain_completed`). Raises `ValueError` for non-enrich stages (CR-02 fix), not `KeyError`. |
| `src/phaze/services/stage_status.py` | `domain_completed_clause()` SQL twin | ✓ VERIFIED | Lines 170-194. Raises `ValueError` symmetrically with the Python twin (CR-02 fix confirmed present at lines 188-191). |
| `src/phaze/routers/agent_analysis.py` | `report_analysis_failed` dual-write marker + state | ✓ VERIFIED | Lines 329-370. |
| `src/phaze/routers/agent_metadata.py` | `report_metadata_failed` writer, `put_metadata` clear-on-success | ✓ VERIFIED | Lines 99-157 (writer); 79-91 (clear-on-success, both branches). |
| `src/phaze/routers/agent_fingerprint.py` | `report_fingerprint_failed` persists nothing (documented asymmetry) | ✓ VERIFIED | Docstring + behavior confirmed by test run. |
| `src/phaze/routers/pipeline.py` | `retry_analysis_failed` (CR-01 dual-clear), `retry_metadata_failed` (new) | ✓ VERIFIED | Lines 885-961 (dual-clear at 943-948), 968-1024 (new endpoint). |
| `alembic/versions/033_add_analysis_completed_xor_failed.py` | XOR CHECK + mixed-row cleanup, ordered cleanup-before-CHECK | ✓ VERIFIED | Cleanup at line 70, `create_check_constraint` at line 73 — correct order. |
| `tests/fingerprint/routers/test_agent_fingerprint_failure.py` | FAIL-04 regression/characterization tests | ✓ VERIFIED | 4 tests, all pass; asserts no-row persistence + no sentinel engine + ELIG-04 eligibility. |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `report_analysis_failed` | `analysis` table | `pg_insert...on_conflict_do_update` | WIRED | Confirmed writes `failed_at`/`error_message`/clears `analysis_completed_at` in one transaction with the `files.state` write. |
| `report_metadata_failed` | `metadata` table | `pg_insert...on_conflict_do_update` | WIRED | Confirmed writes `failed_at`/`error_message`; ledger cleared same transaction. |
| `retry_metadata_failed` endpoint | `get_metadata_failed_files` → `_enqueue_extraction_jobs` | direct call chain | WIRED | Confirmed via source read; `enqueue_router.resolve_queue_for_task` gates against no-active-agent, matching `retry_analysis_failed`'s pattern. |
| `retry_analysis_failed` endpoint | `AnalysisResult.failed_at` clear | `update(...).values(failed_at=None, error_message=None)` | WIRED | CR-01 fix confirmed present at `pipeline.py:943-948`; 4 dedicated regression tests (`test_pipeline_analysis_retry_clears_marker.py`) pass. |
| `domain_completed()` / `domain_completed_clause()` | `FAILURE_IS_TERMINAL` | dict lookup with `ValueError` guard | WIRED | CR-02 fix confirmed present in both twins; 17-test `test_domain_completed_contract.py` passes. |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Fingerprint failure characterization suite | `uv run pytest tests/fingerprint/routers/test_agent_fingerprint_failure.py` | 4 passed | ✓ PASS |
| Analyze failure marker + migration 033 + metadata retry + equivalence suites | `uv run pytest tests/analyze/routers/test_agent_analysis_failure.py tests/metadata/routers/test_agent_metadata.py tests/integration/routers/test_pipeline_metadata_retry.py tests/integration/test_migrations/test_migration_033_additive_check.py tests/integration/test_stage_status_equivalence.py` | 75 passed | ✓ PASS |
| CR-01 regression (retry_analysis_failed dual-clear) | `uv run pytest tests/integration/routers/test_pipeline_analysis_retry_clears_marker.py` | 4 passed | ✓ PASS |
| CR-02 regression (domain_completed twin symmetry) | `uv run pytest tests/shared/test_domain_completed_contract.py` | 17 passed | ✓ PASS |
| ELIG-01..04 semantics-preserving refactor (D-16) | `uv run pytest tests/shared/test_stage_eligibility_dag.py` | 17 passed | ✓ PASS |
| mypy on all touched modules | `uv run mypy src/phaze/enums/stage.py src/phaze/services/stage_status.py src/phaze/routers/agent_analysis.py src/phaze/routers/agent_metadata.py src/phaze/routers/agent_fingerprint.py src/phaze/routers/pipeline.py` | Success: no issues found in 6 source files | ✓ PASS |
| Fix commits present on branch | `git branch --contains 1ff92265 / a6398d33` | both list `* SimplicityGuy/phase-81` | ✓ PASS |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|-------------|--------|----------|
| FAIL-01 | 81-01, 81-02, 81-05 | analyze failure marker + backfill + XOR CHECK | ✓ SATISFIED | See Truth #1 |
| FAIL-02 | 81-03 | metadata failure marker | ✓ SATISFIED | See Truth #2 |
| FAIL-03 | 81-06 | metadata operator retry path | ✓ SATISFIED | See Truth #3 |
| FAIL-04 | 81-01, 81-04 | fingerprint reused, not re-invented | ✓ SATISFIED | See Truth #4 |

No orphaned requirements: `.planning/REQUIREMENTS.md:140-143` maps all four IDs to Phase 81, matching what the six plans collectively declare. (Note: REQUIREMENTS.md's checklist/table still shows "Pending" / unchecked — this appears to be a milestone-level bookkeeping field updated separately from phase verification, not a phase-81 code gap. Flagged as informational only, not a blocker.)

### Anti-Patterns Found

No `TBD`/`FIXME`/`XXX` markers in any of the seven files touched by this phase's core writers/predicates/migration. No placeholder or stub patterns found in the failure-marker writers, retry endpoints, or the `domain_completed`/`eligible` predicates — every path traced above is a real DB write or real predicate evaluation backed by passing tests.

Two code-review warnings (WR-01, WR-02) remain intentionally open per `deferred-items.md`, already surfaced to the operator and explicitly out of this phase's fix scope:
- **WR-01**: `report_metadata_failed`'s conflict branch does not null pre-existing payload columns, so a file with real tags that later fails extraction derives FAILED despite holding usable metadata. This is a data-correctness edge case, not a phase-goal blocker — SC#2 ("visible in derivation and counts") still holds; the row is visible and derives FAILED as designed, just with stale payload alongside.
- **WR-02**: `domain_completed_clause` has no `inflight` disjunct while the Python twin ranks `IN_FLIGHT` above `FAILED`, so the twins diverge on `in_flight ∧ failed` rows — a cell FAIL-03's retry now makes reachable. This affects Phase 80's future recovery cutover, not Phase 81's writer/retry goal.

Both are pre-existing, documented, and were reviewed/accepted by the orchestrator as open warnings rather than blockers — not something this verification independently discovered.

### Human Verification Required

None. All four success criteria are backed by direct source inspection plus passing automated tests re-run independently during this verification (not merely cited from SUMMARY.md).

### Gaps Summary

No gaps found. All four phase success criteria are observably true in the codebase:
1. Analyze failure marker + backfill (Phase 77 migration 032) + CHECK (Phase 81 migration 033) — combination confirmed via git history.
2. Metadata failure marker — confirmed via source read and passing tests.
3. Metadata operator retry endpoint — confirmed via source read and passing tests, including the CR-01-adjacent dual-write parity this phase's review caught and fixed for the analyze sibling endpoint.
4. Fingerprint reuse (no new writer) — confirmed via source read and a dedicated 4-test characterization suite that would fail if a synthetic row were ever introduced or if the failed-fingerprint eligibility flipped.

The two open review warnings (WR-01, WR-02) are pre-existing, documented in `deferred-items.md`, and do not block the phase goal — they are correctly scoped as follow-up work for Phase 80's recovery cutover and a metadata payload-retention edge case, not regressions of what Phase 81 promised to deliver.

---

*Verified: 2026-07-09*
*Verifier: Claude (gsd-verifier)*
