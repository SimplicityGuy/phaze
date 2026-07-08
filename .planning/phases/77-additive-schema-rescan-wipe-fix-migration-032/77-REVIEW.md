---
phase: 77-additive-schema-rescan-wipe-fix-migration-032
reviewed: 2026-07-08T00:00:00Z
depth: standard
files_reviewed: 13
files_reviewed_list:
  - alembic/versions/032_add_derived_status_schema.py
  - src/phaze/models/__init__.py
  - src/phaze/models/analysis.py
  - src/phaze/models/cloud_job.py
  - src/phaze/models/dedup_resolution.py
  - src/phaze/models/fingerprint.py
  - src/phaze/models/metadata.py
  - src/phaze/routers/agent_files.py
  - src/phaze/services/ingestion.py
  - tests/agents/test_rescan_preserves_state.py
  - tests/discovery/test_rescan_preserves_state.py
  - tests/integration/test_migrations/test_migration_032_additive_schema.py
  - tests/shared/models/test_core_models.py
findings:
  critical: 1
  warning: 2
  info: 2
  total: 5
status: issues_found
---

# Phase 77: Code Review Report

**Reviewed:** 2026-07-08T00:00:00Z
**Depth:** standard
**Files Reviewed:** 13
**Status:** issues_found

## Summary

Reviewed the additive-only migration `032`, its ORM mirror (`AnalysisResult`/`FileMetadata`/`FingerprintResult`/`CloudJob`/new `DedupResolution`), the two rescan-wipe fix sites (`agent_files.py::upsert_files`, `ingestion.py::bulk_upsert_files`), and the associated tests.

The migration itself is careful and internally consistent: the partial-index predicate text is byte-identical between the ORM `__table_args__` and the raw DDL (verified against the `ix_fprint_success` `= ANY (ARRAY[...])` spelling requirement and the four `IS NOT NULL` / `status = 'awaiting'` predicates), the backfill SQL is 100% static literals (no bandit S608 surface), `saq_jobs` is never referenced, `files.state` is never written by the migration, the CHECK-constraint widen/narrow round-trips correctly against the precedent set by migrations 025/026, and the analyze-failed backfill correctly implements the UPSERT contract (verified against the `_FE` partial-row / `_FD` no-row test cases). The rescan-wipe fix at both upsert sites is a minimal, correct diff (only the `"state"` key removed from each `ON CONFLICT DO UPDATE` `set_` dict; new-row `INSERT` still stamps `DISCOVERED`), and `AUTH-01` (agent_id always from the auth dependency, never the request body) is preserved.

However, the new `dedup_resolution` table introduces an FK integrity regression against an existing, wired-up production code path (`services/scan_deletion.py` / `DELETE /scans/{batch_id}`) that was not in this phase's file set but is broken by it. See CR-01.

## Critical Issues

### CR-01: New `dedup_resolution` FK is not accounted for by the existing scan-batch delete cascade — crashes `DELETE /scans/{batch_id}`

**File:** `src/phaze/models/dedup_resolution.py:35,39` (introducing change); affected call site `src/phaze/services/scan_deletion.py:82-99` (not in this phase's diff, but broken by it); triggered via `src/phaze/routers/pipeline_scans.py:293`

**Issue:** `DedupResolution.file_id` and `DedupResolution.canonical_file_id` are both plain `ForeignKey("files.id")` with no `ondelete` rule (default `NO ACTION`/`RESTRICT`). `services/scan_deletion.py::delete_scan_cascade` is the application-level cascade this codebase uses in place of DB-level `ON DELETE CASCADE` (its own docstring: "most of the FK columns in this schema were declared with no `ondelete` rule... so the database will not cascade for us"). It explicitly deletes `AnalysisResult`, `FileMetadata`, `FingerprintResult`, `TagWriteLog`, `FileCompanion`, etc. before deleting `FileRecord`, but it has **no delete step for `dedup_resolution`** (the table this phase adds).

Migration 032's own backfill (`_BACKFILL_DEDUP`) immediately populates `dedup_resolution` from every existing `state='duplicate_resolved'` file the moment the migration runs, so this is not a theoretical edge case — it is live data as of migration 032.

Concretely, `DELETE /scans/{batch_id}` (`routers/pipeline_scans.py:267-303`, no exception handling around `delete_scan_cascade`) will now raise an unhandled `IntegrityError` (500) whenever the batch being deleted contains:
1. a file that itself has a `dedup_resolution.file_id` row (i.e. it was ever marked `duplicate_resolved`), or
2. a file that is referenced as `canonical_file_id` by *any* `dedup_resolution` row, even one belonging to a file in a completely different, unrelated scan batch.

Case (2) means deleting almost any old, canonical (non-duplicate) file's scan batch can now fail if it happens to be the sha256-canonical target recorded for some duplicate elsewhere in the database — a much wider blast radius than the batch being deleted.

**Fix:** Add a `dedup_resolution` delete step to the ordered cascade in `scan_deletion.py`, scoped by `file_id` (and null out or delete rows where `canonical_file_id` points into the batch, since a `NULL`-able `canonical_file_id` can safely be `UPDATE`d rather than requiring the whole row deleted):

```python
from phaze.models.dedup_resolution import DedupResolution

# child -> parent ordering, before FileRecord is deleted:
(DedupResolution.__tablename__, delete(DedupResolution).where(DedupResolution.file_id.in_(files_of_batch))),
```

and additionally handle the `canonical_file_id`-points-into-batch case, e.g. an `UPDATE dedup_resolution SET canonical_file_id = NULL WHERE canonical_file_id IN (files_of_batch)` step before the delete above (or before the `FileRecord` delete), so a canonical file living in a *different* batch is never blocked by a dangling `canonical_file_id` pointer into the batch being removed.

## Warnings

### WR-01: `cloud_job` has the same missing-cascade shape, now more heavily populated by the 032 backfill

**File:** `src/phaze/models/cloud_job.py:76` (reviewed file, modified this phase to add `AWAITING`); affected call site `src/phaze/services/scan_deletion.py`

**Issue:** `CloudJob.file_id` is also a bare `ForeignKey("files.id")` (no `ondelete`) and is likewise absent from `delete_scan_cascade`'s ordered list. This gap pre-dates Phase 77 (migration 025), so it isn't a new regression, but this phase's backfill (`_BACKFILL_CLOUD_AWAITING`/`_PUSHING`/`_PUSHED`) actively increases the population of `cloud_job` rows tied to historical files (any file that was ever `awaiting_cloud`/`pushing`/`pushed`), widening the set of files whose scan-batch deletion will now hit the same `IntegrityError` class of failure as CR-01.

**Fix:** While fixing CR-01, add the equivalent `delete(CloudJob).where(CloudJob.file_id.in_(files_of_batch))` step to `scan_deletion.py`'s ordered cascade in the same pass, rather than leaving this latent gap in place.

### WR-02: No regression test exercises scan-batch deletion against the new/backfilled sidecar tables

**File:** `tests/integration/test_migrations/test_migration_032_additive_schema.py` (reviewed); missing coverage in `tests/` for `services/scan_deletion.py`

**Issue:** The migration test thoroughly proves the backfill's correctness in isolation, and the two rescan tests thoroughly prove the upsert fix, but nothing in the reviewed test set (or, per grep, anywhere in the repo) exercises `delete_scan_cascade` against a file that has a `dedup_resolution` or `cloud_job` row. CR-01 would have been caught by a straightforward integration test: seed a batch with a `duplicate_resolved` file, run migration 032 (or directly insert a `dedup_resolution` row), then call `DELETE /scans/{batch_id}` and assert it succeeds rather than 500s.

**Fix:** Add a regression test (e.g. `tests/pipeline/test_scan_deletion_dedup_resolution.py`) that seeds a `dedup_resolution` row (and ideally a `cloud_job` row) for a file inside the batch under deletion, then asserts `delete_scan_cascade` succeeds and the sidecar rows are gone/updated afterward.

## Info

### IN-01: Hardcoded backfill placeholder string embedded as permanent production data

**File:** `alembic/versions/032_add_derived_status_schema.py:76`

**Issue:** `_BACKFILL_ANALYZE_FAILED` sets `error_message = 'backfilled from ANALYSIS_FAILED'` as a literal on first insert. This is reasonable as a one-time migration marker, but it becomes permanent, unstructured production data with no constant/enum backing it — a future `grep`/dedup effort on `analysis.error_message` values will need to know this magic string exists out-of-band.

**Fix:** Consider a short comment pointer to this exact string in `models/analysis.py` near `error_message`, or define it as a shared module-level constant if any other code (tests, docs) needs to match against it — currently only the migration file itself contains the literal.

### IN-02: `downgrade()` silently assumes no `status='awaiting'` `cloud_job` rows remain

**File:** `alembic/versions/032_add_derived_status_schema.py:167-181`

**Issue:** `downgrade()` restores the narrower 6-member `status_enum` CHECK unconditionally. If any `cloud_job` row still has `status='awaiting'` at downgrade time (e.g. an operator downgrades without first draining/deleting those rows, unlike the per-migration test which does `DELETE FROM cloud_job` first), `create_check_constraint` will fail with a CHECK-violation error. This is a safe failure (no silent data corruption — the migration just aborts), and is explicitly called out as an assumption in the docstring, but it's worth a one-line operator-facing note in the module docstring's `D-09` paragraph (not just the test) so a future downgrade doesn't get to that error unprepared.

**Fix:** Optional: add a `SELECT count(*) FROM cloud_job WHERE status = 'awaiting'` guard in `downgrade()` that raises a clear `RuntimeError` pointing at the cleanup requirement, instead of surfacing a raw Postgres CHECK-violation traceback.

---

_Reviewed: 2026-07-08T00:00:00Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
