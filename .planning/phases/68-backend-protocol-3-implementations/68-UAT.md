---
status: complete
phase: 68-backend-protocol-3-implementations
source: [68-01-SUMMARY.md, 68-02-SUMMARY.md, 68-03-SUMMARY.md, 68-04-SUMMARY.md, 68-05-SUMMARY.md]
started: 2026-07-04
updated: 2026-07-04
---

## Current Test

[testing complete]

## Tests

### 1. Cold Start Smoke Test
expected: Control plane boots against a fresh empty DB; auto-migration reaches head 029; resolve_backends() boot guard resolves the zero-config all-local registry; app serves a live request without error.
result: pass
evidence: Booted `uvicorn phaze.main:create_app` against fresh empty `phaze_uat` DB (PHAZE_AUTO_MIGRATE=1, PG-backed queue). Alembic ran clean through `028 -> 029 Add cloud_job.backend_id + make s3_key nullable (Phase 68, D-06/D-08)`; "Application startup complete"; `/health` and `/` both HTTP 200 after 4s. resolve_backends() boot guard resolved the implicit all-local registry (no ValueError, app served).

### 2. Migration 029 additive & reversible
expected: `alembic upgrade head` applies 029 (nullable backend_id + s3_key nullable, no backfill); `alembic downgrade -1` reverses it cleanly; migration touches only cloud_job, never saq_jobs.
result: pass
evidence: Live UAT DB at head `029`; `cloud_job.backend_id` nullable=YES, `s3_key` nullable=YES. DDL touches only `cloud_job` (add_column backend_id, alter s3_key nullable; downgrade reverses both). The single `saq_jobs` token is a docstring guard (L15), not DDL. Round-trip integration test `test_migration_029_backend_id.py` — 3 passed.

### 3. Dispatch behavior byte-identical (the acceptance gate)
expected: The drain's observable dispatch sequence over {compute,kueue,local}×{agent up,down} is unchanged from pre-refactor code — the D-01 golden characterization snapshot passes byte-identical (only the one sanctioned compute cloud_job field flipped).
result: pass
evidence: `test_dispatch_snapshot.py` — 8 passed byte-identical (golden captured on post-67 code in Wave 0, held through the Wave-3 live rewire).

### 4. Per-backend in-flight accounting equivalence (D-02)
expected: sum(in_flight_count(backend)) == the legacy get_cloud_window_count() for the single-backend case; the new per-backend accounting does not double-count or drift.
result: pass
evidence: `test_in_flight_equivalence` — 1 passed.

### 5. All-local deploy keeps working with zero config edits
expected: The only deploy that ever ran (all-local, no cloud) still dispatches locally with no backends.toml/config change — the zero-config implicit all-local registry resolves and LocalBackend dispatches (in_flight_count always 0, no cloud_job rows).
result: pass
evidence: Cold-start booted with zero config edits on the implicit all-local registry; live all-local UAT DB held 0 cloud_job rows (LocalBackend writes none). LocalBackend protocol cells — 5 passed.

## Summary

total: 5
passed: 5
issues: 0
pending: 0
skipped: 0

## Gaps

[none yet]
