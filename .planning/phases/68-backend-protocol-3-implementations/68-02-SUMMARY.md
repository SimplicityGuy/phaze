---
phase: 68-backend-protocol-3-implementations
plan: 02
subsystem: cloud-backends
tags: [schema, migration, cloud_job, backend-registry, BACK-02]
requires:
  - "028 migration head (analysis_completed_at)"
  - "Wave 0 guarded migration test (68-01)"
provides:
  - "cloud_job.backend_id nullable column (config-derived, no backfill)"
  - "cloud_job.s3_key nullable (compute rows carry no S3 object)"
  - "alembic migration 029 (revises 028)"
affects:
  - "in_flight_count per-backend accounting (Wave 2)"
  - "ComputeAgentBackend.dispatch cloud_job row (Wave 3)"
tech-stack:
  added: []
  patterns:
    - "additive nullable column migration mirroring 026 (no CHECK/enum swap)"
    - "cloud_job-only DDL, never saq_jobs (020 banner)"
key-files:
  created:
    - "alembic/versions/029_add_cloud_job_backend_id.py"
  modified:
    - "src/phaze/models/cloud_job.py"
decisions:
  - "D-06: backend_id nullable, additive, no backfill (no live rows exist)"
  - "D-08: s3_key becomes nullable (compute burst has no S3 object)"
  - "D-10 (confirmed): in-flight status set will be {UPLOADING, UPLOADED, SUBMITTED, RUNNING} — consumed in Wave 2, not this plan"
metrics:
  duration: "~7 min"
  completed: "2026-07-03"
requirements: [BACK-02]
---

# Phase 68 Plan 02: cloud_job backend_id + s3_key nullability Summary

Added the per-backend accounting schema substrate: a nullable, config-derived `cloud_job.backend_id`
column and `s3_key` nullability (so a compute row with no S3 object is valid), via additive migration
029 revising 028. No backfill, no CHECK/enum change, cloud_job-only.

## What Was Built

**Task 1 — model** (`src/phaze/models/cloud_job.py`, commit `671c6ca`):
- Added `backend_id: Mapped[str | None] = mapped_column(String(255), nullable=True)` mirroring the
  existing optional kube columns, commented as config-derived / no-backfill (D-06).
- Changed `s3_key` from `Mapped[str]` / `nullable=False` to `Mapped[str | None]` / `nullable=True`
  (D-08), with a docstring note that compute rows leave it NULL.
- `ck_cloud_job_status_enum` CheckConstraint and the unique FK on `file_id` left untouched.

**Task 2 — migration 029** (`alembic/versions/029_add_cloud_job_backend_id.py`, commit `b94069b`):
- `revision = "029"`, `down_revision = "028"`, `branch_labels = None`, `depends_on = None`.
- `upgrade()`: `op.add_column("cloud_job", ... backend_id String(255) nullable=True)` then
  `op.alter_column("cloud_job", "s3_key", existing_type=String(255), nullable=True)`.
- `downgrade()`: reverse order — re-impose `s3_key NOT NULL` then `op.drop_column(backend_id)`.
- No CHECK/enum change (backend_id is free-text), no backfill, no `saq_jobs` reference (026-style
  banner in docstring). `alembic heads` reports a single head `029`.

## Verification

- `uv run pytest tests/integration/test_migrations/test_migration_029_backend_id.py -x` — **3 passed**
  (previously skip-guarded, now lit up): static revision assertions, grep-assert no `saq_jobs`, and the
  full 028→029→028 round-trip proving nullable `backend_id`, nullable `s3_key`, a compute-shaped row
  (`s3_key=NULL, backend_id='compute-a1'`) inserting, and the downgrade re-imposing `NOT NULL`.
- Model verify: `backend_id` and `s3_key` both nullable (assert passed); `uv run mypy` clean.
- `alembic heads` → `029 (head)` (single head, no branch).
- Regression `uv run pytest tests/analyze/ tests/shared/ -x` — **1175 passed, 1 skipped** (no
  regression; the 41 warnings are pre-existing AsyncMock coroutine warnings in unrelated modules).

## Deviations from Plan

None — plan executed exactly as written.

## Known Stubs

None. `backend_id` is intentionally NULL for existing/future non-stamped rows (D-06 config-derived,
stamped at dispatch in Wave 3); this is a documented schema substrate, not a stub.

## Self-Check: PASSED

- FOUND: src/phaze/models/cloud_job.py (modified)
- FOUND: alembic/versions/029_add_cloud_job_backend_id.py
- FOUND commit: 671c6ca (model)
- FOUND commit: b94069b (migration 029)
