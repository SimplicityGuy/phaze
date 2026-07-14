---
phase: 77-additive-schema-rescan-wipe-fix-migration-032
verified: 2026-07-08T09:30:00Z
status: passed
score: 4/4 success criteria verified; 3/3 requirements satisfied
overrides_applied: 0
---

# Phase 77: Additive Schema & Rescan-Wipe Fix (migration `032`) — Verification Report

**Phase Goal:** Land the additive `032` migration so the derived model's schema exists — analyze/metadata failure markers, the dedup marker, and the cloud-routing sidecar representation — backfilled from `files.state`, with partial indexes sized to the exact predicates, WITHOUT touching `files.state`; plus the independently-shippable rescan progress-wipe fix.

**Verified:** 2026-07-08T09:30:00Z
**Status:** passed
**Method:** Static code read (migration, ORM models, upsert sites, cascade-delete) + **live execution** of the full test suite against the ephemeral Postgres :5433 DB (`phaze_test` / `phaze_migrations_test`, both already running via `phaze-test-db` container) — not just SUMMARY claims. All commands below were re-run independently by the verifier.

## Goal Achievement

### Observable Truths (ROADMAP Success Criteria)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | `alembic upgrade head` applies `032` on a corpus copy — creates the analyze/metadata failure-marker columns, the dedup-marker table, and the cloud-routing sidecar rows, all backfilled from `files.state` — with `files.state` byte-unchanged and `saq_jobs` never referenced. | ✓ VERIFIED | Ran `uv run pytest tests/integration/test_migrations/test_migration_032_additive_schema.py -v` live against `:5433` — **3 passed**. The integration test seeds a 9-file corpus across every relevant `files.state` value, snapshots `files.state`, upgrades 031→032, and asserts: `failed_at`/`error_message` columns exist on `analysis`/`metadata`; `dedup_resolution` table exists; analyze-failed UPSERT backfill count == `state='analysis_failed'` count (both no-row and partial-row branches exercised); `metadata.failed_at` stays all-NULL (D-03); dedup backfill count == `state='duplicate_resolved'` count with correct canonical derivation (target + NULL case); cloud sidecar gap-fill produces `awaiting`/`uploading`/`uploaded` rows and does not duplicate an existing row; **`files.state` byte-identical before/after** (`after_state == before_state` assertion, line 234). `test_migration_never_references_saq_jobs` passed (grep guard); manual `grep -n saq_jobs alembic/versions/032_add_derived_status_schema.py` confirms the only 2 hits are the docstring CRITICAL banner and a "no saq_jobs" comment — zero executable references. `grep -n "UPDATE files\|files.state\s*="` over the migration file returns nothing — `upgrade()` never writes `files.state`. |
| 2 | Each new partial index is `IS NOT NULL`-shaped (or the documented equality/membership shape — `ix_cloud_job_awaiting`, `ix_fprint_success` are intentionally `= 'awaiting'` / `= ANY(ARRAY[...])`, NOT bare `IN`), exists in the DB, and is mirrored into the ORM `__table_args__` — `alembic revision --autogenerate` produces an empty diff (scoped to the 032 objects). | ✓ VERIFIED | Read `alembic/versions/032_add_derived_status_schema.py` (`op.create_index` calls, lines 150-156) side-by-side with the ORM `__table_args__` in `models/analysis.py`, `models/metadata.py`, `models/cloud_job.py`, `models/fingerprint.py` — index **names and `postgresql_where` predicate text are byte-identical** in all 5 cases: `ix_analysis_completed` (`analysis_completed_at IS NOT NULL`), `ix_analysis_failed`/`ix_metadata_failed` (`failed_at IS NOT NULL`), `ix_cloud_job_awaiting` (`status = 'awaiting'`), `ix_fprint_success` (`status = ANY (ARRAY['success','completed'])` — not bare `IN`, satisfying RESEARCH Pitfall 1). The integration test's part (j) confirms all 5 exist in `pg_indexes` after upgrade, and part (k) runs a **live, automated** `alembic.autogenerate.compare_metadata` against `Base.metadata` at the `032` head, scoped to the 032 object set (5 indexes, `dedup_resolution` table, 4 marker columns) — asserts `offenders == []`. This ran live and passed (see truth #1 test run). The scoping is legitimate: it excludes pre-existing, phase-unrelated ORM↔DB drift (naive `DateTime()` TimestampMixin vs `timestamptz` on legacy tables predating Phase 77) — documented in the SUMMARY and confirmed by reading the migration/models; no 032-introduced column uses this drift-prone pattern except `dedup_resolution.created_at`/`updated_at`, which are deliberately authored as naive `sa.DateTime()` to match `TimestampMixin` exactly (keeping the new table's own columns diff-clean, per the SUMMARY's key-decision). |
| 3 | Re-scanning an already-advanced file no longer resets progress: `ON CONFLICT DO UPDATE SET state = excluded.state` removed from BOTH upsert sites (`services/ingestion.py`, `routers/agent_files.py`), proven by a test that rescans an `ANALYZED` file and asserts its output rows survive. | ✓ VERIFIED | Read both `set_` dicts directly: `services/ingestion.py:109-121` and `routers/agent_files.py:127-139` — the `"state": stmt.excluded.state` / `"state": base_stmt.excluded.state` key is absent from both; `grep -n "state.*excluded" src/phaze/services/ingestion.py src/phaze/routers/agent_files.py` returns **zero matches**. New-file INSERT still stamps `state = DISCOVERED` via the VALUES/record dict (unaffected). Ran `uv run pytest tests/discovery/test_rescan_preserves_state.py tests/agents/test_rescan_preserves_state.py -v` live — **2 passed**. Both tests are substantive (not vacuous): each advances a file to `ANALYZED` + creates its `analysis` row, re-upserts/re-POSTs the identical `(agent_id, original_path)` at `DISCOVERED`, and asserts (a) `state` stays `ANALYZED` and (b) the `analysis` row survives; the agent-endpoint test additionally asserts `inserted == 0` on the rescan (proving it hit the UPDATE branch, not INSERT) and that `agent_id` is stamped from the auth dependency (AUTH-01), never the request body. |
| 4 | `032.downgrade()` reverses the additive objects — per CONTEXT D-09 this is intentionally MINIMAL (forward-upgrade focus); a best-effort DDL downgrade satisfies this. | ✓ VERIFIED (relaxed per D-09, as designed) | Read `downgrade()` (lines 167-186): drops the 5 indexes, drops `dedup_resolution`, restores the 6-member CHECK, drops the 4 marker columns — in correct reverse-dependency order. Data backfills are explicitly NOT reversed (documented no-op, matching the 016 precedent). The integration test's part (l) exercises this live: after `DELETE FROM cloud_job` (required because the restored 6-member CHECK would reject the backfilled `'awaiting'` rows — a documented, intentional precondition, not a bug), `downgrade_to(cfg, "031")` succeeds and asserts `dedup_resolution` is gone, the marker columns are gone, and the partial indexes are gone. This ran live and passed. CONTEXT D-09 explicitly relaxes ROADMAP SC#4 to "best-effort DDL reversal" — ROADMAP.md itself documents this relaxation inline (`*(Relaxed to best-effort DDL reversal per CONTEXT D-09 — forward upgrade path is the focus.)*`), so no separate override entry is needed; this is a pre-accepted design decision, not a deviation discovered during verification. |

**Score:** 4/4 truths verified.

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `alembic/versions/032_add_derived_status_schema.py` | Additive migration: columns, `dedup_resolution` table, CHECK widen, partial indexes, backfills, minimal downgrade | ✓ VERIFIED | 186 lines; `revision="032"`, `down_revision="031"`, `branch_labels is None`. All 5 objects created; all 5 backfills are static-literal `op.execute(sa.text(...))` (bandit S608-clean, no interpolation). |
| `src/phaze/models/analysis.py` | `failed_at`+`error_message` columns; `__table_args__` with `ix_analysis_completed`+`ix_analysis_failed` | ✓ VERIFIED | Both columns nullable; both indexes present with matching predicate text. |
| `src/phaze/models/metadata.py` | Same two columns; `__table_args__` with `ix_metadata_failed` | ✓ VERIFIED | Confirmed by direct read. |
| `src/phaze/models/cloud_job.py` | `CloudJobStatus.AWAITING`; widened CHECK; `ix_cloud_job_awaiting` | ✓ VERIFIED | `AWAITING = "awaiting"` present; 7-member CHECK text byte-identical to migration's `_STATUS_ENUM_NEW`; index present. |
| `src/phaze/models/fingerprint.py` | `ix_fprint_success` spelled `= ANY(ARRAY[...])` | ✓ VERIFIED | Confirmed, not bare `IN`. |
| `src/phaze/models/dedup_resolution.py` | `DedupResolution` 1:1 sidecar model | ✓ VERIFIED | 41 lines; unique `file_id` NOT NULL, nullable `canonical_file_id`, `resolved_at` server-default now, TimestampMixin for created/updated. |
| `src/phaze/models/__init__.py` | `DedupResolution` registered (import + `__all__`) | ✓ VERIFIED | Both present. |
| `tests/discovery/test_rescan_preserves_state.py` | Regression: `bulk_upsert_files` rescan preserves state+analysis row | ✓ VERIFIED, WIRED | Ran live — passed. |
| `tests/agents/test_rescan_preserves_state.py` | Regression: agent upsert endpoint rescan preserves state+analysis row | ✓ VERIFIED, WIRED | Ran live — passed. |
| `tests/integration/test_migrations/test_migration_032_additive_schema.py` | Per-migration integration test + saq_jobs guard + empty-autogenerate-diff | ✓ VERIFIED, WIRED | 284 lines; 3 tests, all ran live and passed. |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|----|--------|---------|
| `alembic/versions/032` `op.create_index` names + `postgresql_where` | ORM `__table_args__` (5 models) | Byte-identical names + normalized predicate text | ✓ WIRED | Confirmed by direct text comparison of all 5 index definitions; the live empty-autogenerate-diff assertion (`offenders == []`) is the strongest possible proof — it ran against a real DB at the `032` head. |
| `032` analyze-failed backfill | `analysis.failed_at` | `INSERT..SELECT..ON CONFLICT (file_id) DO UPDATE` | ✓ WIRED | Confirmed the statement is an UPSERT (not a plain UPDATE), correctly handling `report_analysis_failed`'s no-row case (RESEARCH Pitfall 2); the integration test exercises both the no-row (`_FD`) and partial-row (`_FE`) branches and both pass. |
| `032` backfill sources | `files.state` | READ-ONLY `SELECT`; `files.state` never written | ✓ WIRED | Grep confirms no `UPDATE files` / `files.state =` write statement anywhere in the migration; the live test's before/after snapshot assertion (`after_state == before_state`) is the strongest proof and it passed. |
| `services/ingestion.py` / `routers/agent_files.py` rescan upsert | `files.state` | `ON CONFLICT DO UPDATE set_` dict no longer writes `state` | ✓ WIRED | Grep confirms zero `state.*excluded` matches at both sites; both regression tests ran live and passed, proving the wiring end-to-end (not just the source diff). |
| `dedup_resolution` / `cloud_job` FKs | `services/scan_deletion.py::delete_scan_cascade` | Ordered delete steps scoped by `file_id` (+ `canonical_file_id` for the cross-batch case) | ✓ WIRED | Not originally in this phase's `files_modified`, but the code-review (CR-01/WR-01) correctly caught that migration 032's new FK-carrying tables were NOT accounted for by the existing cascade, which would 500 `DELETE /scans/{batch_id}` for any batch touching a backfilled file. Fixed in commit `9976f290` (confirmed present in `git log`) — both tables added to the ordered cascade, `dedup_resolution` scoped by both FK columns. New regression test `test_cascade_removes_dedup_resolution_and_cloud_job_sidecars` (in `tests/discovery/services/test_scan_deletion.py`) ran live — passed — covering the cross-batch `canonical_file_id` case exactly as the review demanded (WR-02). |

### Requirements Coverage

| Requirement | Source Plan(s) | Description | Status | Evidence |
|-------------|----------------|--------------|--------|----------|
| MIG-01 | 77-02, 77-03 | Migration `032` is additive-only — creates failure markers, dedup marker, cloud sidecar, partial indexes, backfilled from `FileRecord.state`, without touching `files.state`. | ✓ SATISFIED | Truths #1, #2 above; live integration test proves it end-to-end. REQUIREMENTS.md already marks `[x]` and maps Phase 77 → Complete — consistent with the evidence. |
| MIG-03 | 77-01 | Rescanning a file no longer resets pipeline progress. | ✓ SATISFIED | Truth #3 above; both regression tests ran live and passed. REQUIREMENTS.md already marks `[x]`. (Note: REQUIREMENTS.md's MIG-03 wording says "with `FileRecord.state` gone... structurally impossible" — that refers to the *later* destructive migration `033` [Phase 90]; Phase 77's actual, narrower contribution — removing the wipe from both upsert sites — is exactly what CONTEXT D-08 and both PLAN frontmatters scope it to, and that is what's verified here.) |
| PERF-01 | 77-02, 77-03 | Partial indexes sized to exact predicates, mirrored into ORM `__table_args__` so `autogenerate` stays in sync. | ✓ SATISFIED | Truth #2 above; live empty-autogenerate-diff assertion is the direct proof. REQUIREMENTS.md already marks `[x]`. |

No orphaned requirements: REQUIREMENTS.md's Phase-77 mapping table lists exactly MIG-01, MIG-03, PERF-01, matching the union of all three plans' `requirements:` frontmatter.

### Anti-Patterns Found

None. Scanned all 14 phase-touched files (`alembic/versions/032_add_derived_status_schema.py`, the 6 model files, `ingestion.py`, `agent_files.py`, `scan_deletion.py`, and the 4 test files) for `TBD|FIXME|XXX|TODO|HACK|PLACEHOLDER` and placeholder-language patterns — zero matches. `ruff check` and `mypy` both clean on the full touched-file set (re-run live by the verifier, not taken from SUMMARY).

### Behavioral Spot-Checks / Live Test Execution

All of the following were re-run live by the verifier against the ephemeral `:5433` Postgres (containers `phaze-test-db`/`phaze-test-redis`, already running) — not taken on SUMMARY's word:

| Check | Command | Result | Status |
|-------|---------|--------|--------|
| Migration 032 integration test | `pytest tests/integration/test_migrations/test_migration_032_additive_schema.py -v` | 3 passed | ✓ PASS |
| Rescan regressions (both sites) | `pytest tests/discovery/test_rescan_preserves_state.py tests/agents/test_rescan_preserves_state.py -v` | 2 passed | ✓ PASS |
| Scan-deletion cascade (incl. CR-01/WR-01/WR-02 fix) | `pytest tests/discovery/services/test_scan_deletion.py -v` | 5 passed | ✓ PASS |
| `discovery` bucket in isolation | `just test-bucket discovery` | 204 passed | ✓ PASS |
| `agents` bucket in isolation | `just test-bucket agents` | 441 passed | ✓ PASS |
| `integration` bucket in isolation | `just test-bucket integration` | 71 passed | ✓ PASS |
| `shared/models` (dedup_resolution registration) | `pytest tests/shared/models/ -q` | 39 passed | ✓ PASS |
| Partition guard | `pytest tests/shared/test_partition_guard.py -q` | 3 passed | ✓ PASS |
| Ruff on all phase-touched files | `ruff check <14 files>` | clean | ✓ PASS |
| Mypy on all phase-touched source files | `mypy <9 source files>` | Success: no issues found | ✓ PASS |
| `saq_jobs` non-reference grep | `grep -n saq_jobs alembic/versions/032_*.py` | only docstring banner hits | ✓ PASS |
| `state.*excluded` absence grep | `grep -n "state.*excluded" ingestion.py agent_files.py` | 0 matches | ✓ PASS |

All numbers match the SUMMARY.md claims exactly (204/441/71/39/3) — independently reproduced, not merely trusted.

### Probe Execution

Not applicable — this phase has no `scripts/*/tests/probe-*.sh` convention; the per-migration integration test *is* the probe-equivalent artifact and was run directly (see above).

### Human Verification Required

None. This phase is entirely backend/schema/migration work with no UI or user-facing behavior — all success criteria are mechanically verifiable and were verified via live test execution + direct code reading.

### Observations (non-blocking)

1. **Pre-existing ORM↔DB drift outside phase scope.** The migrations-test DB carries unrelated drift (naive `DateTime()` vs `timestamptz` on legacy tables predating Phase 77; some dropped `search_vector`/trgm indexes). The integration test's empty-diff assertion is correctly scoped to exclude this — it predates and is out of contract for this phase. Not a gap.
2. **`just docs-drift` currently flags MIG-01/MIG-03/PERF-01 as "Complete but Phase 77 not passed."** This is the expected, self-resolving state immediately before this VERIFICATION.md is written and the ROADMAP.md Phase-77 checkbox is flipped to `[x]` — not a defect introduced by this phase's code. It will clear once this VERIFICATION.md is committed and the ROADMAP checkbox updated (normal phase-close sequencing).
3. **CR-01/WR-01/WR-02 fix (commit `9976f290`) touched files outside the original three plans' `files_modified` lists** (`src/phaze/services/scan_deletion.py`, `tests/discovery/services/test_scan_deletion.py`). This was the correct response to a critical code-review finding (a new FK-carrying table that would 500 an existing, unrelated endpoint) and is fully covered by a new regression test that passed live. Not a gap — it is the review process working as intended, and 77-REVIEW.md documents it was resolved before this verification ran.
4. **IN-01/IN-02 (Info-level review findings)** — a hardcoded backfill placeholder string (`'backfilled from ANALYSIS_FAILED'`) and a downgrade precondition assumption — were accepted as Info-level per 77-REVIEW.md and do not block the phase goal.

## Gaps Summary

None. All 4 ROADMAP success criteria and all 3 phase requirements (MIG-01, MIG-03, PERF-01) are verified with live evidence, not just static review or SUMMARY trust.

---

_Verified: 2026-07-08T09:30:00Z_
_Verifier: Claude (gsd-verifier)_
