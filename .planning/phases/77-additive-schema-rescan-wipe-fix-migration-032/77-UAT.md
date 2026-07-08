---
status: complete
phase: 77-additive-schema-rescan-wipe-fix-migration-032
source: [77-01-SUMMARY.md, 77-02-SUMMARY.md, 77-03-SUMMARY.md]
started: 2026-07-08
updated: 2026-07-08
mode: self-driven (backend/no-UI phase — orchestrator exercised each behavior directly)
---

## Current Test

[testing complete]

## Tests

### 1. Cold Start Smoke Test (migration chain applies on a fresh DB)
expected: On a clean DB, `alembic upgrade base→head` boots without error and 032 objects exist at head.
result: pass
evidence: |
  `alembic downgrade base` then `alembic upgrade head` on a fresh phaze_migrations_test DB ran the
  full chain through `031 → 032` with no error; `alembic current` = `032 (head)`. Post-upgrade psql
  confirmed: analysis.failed_at/error_message + metadata.failed_at/error_message columns present;
  dedup_resolution table with (id, file_id, canonical_file_id, resolved_at, created_at, updated_at);
  cloud_job status CHECK lists all 7 members incl 'awaiting'; all 5 partial indexes
  (ix_analysis_completed, ix_analysis_failed, ix_metadata_failed, ix_cloud_job_awaiting, ix_fprint_success) present.

### 2. Rescan preserves progress — both upsert sites (MIG-03)
expected: Re-scanning an already-ANALYZED file keeps state=ANALYZED and its analysis row survives, at BOTH the discovery/ingestion path (bulk_upsert_files) and the agent-API path (upsert_files). Agent path also reports the row as updated (inserted=false) with agent_id from the auth dep, never the body.
result: pass
evidence: |
  `tests/discovery/test_rescan_preserves_state.py` + `tests/agents/test_rescan_preserves_state.py`
  → 2 passed. Both advance a file to ANALYZED + create its analysis row, rescan the same
  (agent_id, original_path), and assert state stays ANALYZED, the analysis row survives, and (agent
  path) inserted=false with agent_id sourced from the auth dependency.

### 3. Migration 032 additive schema + backfill + invariants (MIG-01)
expected: upgrade 031→032 on a seeded corpus creates failed_at/error_message on analysis+metadata, the dedup_resolution table, and admits status='awaiting'; backfill counts equal legacy files.state counts; metadata.failed_at stays all-NULL; awaiting/uploading/uploaded cloud_job rows appear; dedup canonical derived (target + NULL cases); files.state byte-unchanged; migration never references saq_jobs.
result: pass
evidence: |
  `test_migration_032_additive_schema.py` → 3 passed, including
  `test_upgrade_032_creates_backfills_and_autogenerate_is_empty_then_downgrade_reverses` (seeded corpus:
  analyze-failed with/without prior analysis row, sha256 group for canonical + lonely-hash NULL case,
  pushing file with pre-existing cloud_job for gap-fill DO-NOTHING) and the DB-free
  `test_migration_never_references_saq_jobs` guard. Asserts backfill counts == legacy files.state counts,
  metadata.failed_at all-NULL (D-03), awaiting/uploading/uploaded rows present, files.state byte-unchanged.

### 4. ORM↔DB parity — empty autogenerate diff (PERF-01)
expected: With the DB at the 032 head, `compare_metadata` against Base.metadata produces NO add/remove op for any 032 object (columns, dedup_resolution, indexes incl. ix_fprint_success spelled `= ANY (ARRAY[...])`).
result: pass
evidence: |
  The migration test's step (k) runs `alembic.autogenerate.compare_metadata` (via conn.run_sync,
  compare_type=True) at the 032 head and asserts an empty diff scoped to the 032 objects → passed with
  ix_fprint_success present; the drop-and-defer-to-Phase-82 contingency was NOT triggered.

### 5. Downgrade reverses additive DDL (D-09)
expected: `032.downgrade()` drops the 5 indexes, the dedup_resolution table, and the 4 marker columns, and restores the 6-member status CHECK — the additive objects are gone after downgrade to 031.
result: pass
evidence: |
  Same integration test's tail: `downgrade_to(cfg,"031")` then asserts the additive objects are gone.
  Part of the 3-passed run.

## Summary

total: 5
passed: 5
issues: 0
pending: 0
skipped: 0

## Gaps

[none — all behaviors verified]
