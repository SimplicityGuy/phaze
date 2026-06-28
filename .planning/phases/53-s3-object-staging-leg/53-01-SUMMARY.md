---
phase: 53-s3-object-staging-leg
plan: 01
subsystem: infra
tags: [s3, aioboto3, moto, pydantic-settings, alembic, sqlalchemy, cloud-burst, object-staging]

# Dependency graph
requires:
  - phase: 51-cloud-burst-master-toggle
    provides: "cloud_burst_enabled master switch + _enforce_compute_scratch_dir_when_cloud_enabled fail-fast pattern"
  - phase: 45-scheduling-ledger
    provides: "per-file_id sidecar model + reversible-migration precedent (scheduling_ledger)"
provides:
  - "S3 ControlSettings config surface (endpoint/bucket/region/addressing + SecretStr creds via _FILE) — control-plane only (KSTAGE-02/05)"
  - "field_validator SSRF guard on s3_endpoint_url + cloud-enabled S3 fail-fast validator"
  - "bounded presign/lifecycle/part-size int knobs (T-53-03)"
  - "CloudJob ORM model + CloudJobStatus StrEnum (per-file_id staging sidecar, D-03)"
  - "alembic migration 025 — reversible cloud_job create-table with unique FK + status CHECK"
  - "aioboto3 (deps) + moto (dev) declared under the supply-chain cooldown"
affects: [54-kueue-submit-reconcile, 55-stage-cloud-window-routing, s3-staging-service, upload-task, presign-routes]

# Tech tracking
tech-stack:
  added: [aioboto3>=15.5.0, moto>=5.1.0]
  patterns:
    - "S3 config on ControlSettings only (KSTAGE-02): agent/pod never receive bucket creds"
    - "field_validator URL-scheme/netloc guard for operator-controlled endpoints (SSRF defense)"
    - "string-backed status StrEnum + DB CHECK constraint (FileState precedent, no enum-type migration)"

key-files:
  created:
    - src/phaze/models/cloud_job.py
    - alembic/versions/025_add_cloud_job.py
    - tests/test_config/test_s3_settings.py
    - tests/test_models/test_cloud_job.py
    - tests/test_migrations/test_migration_025_cloud_job.py
  modified:
    - pyproject.toml
    - src/phaze/config.py
    - src/phaze/models/__init__.py
    - tests/test_config/test_cloud_burst_toggle.py
    - tests/test_models/test_core_models.py

key-decisions:
  - "Placed moto alphabetically (after httpx, before mypy) per CLAUDE.md sorted-deps rule rather than the plan's 'after mypy' instruction"
  - "Added CheckConstraint to CloudJob.__table_args__ so create_all-based model tests enforce the same status enum as the migration"
  - "Bounded-int validation tests match the env-var alias in the error message (pydantic surfaces the alias, not the field name, for setenv-sourced values)"

patterns-established:
  - "S3 credentials honor the _FILE convention by extending ControlSettings.SECRET_FILE_FIELDS — zero new resolution code"
  - "Two ordered mode=after validators: S3-config guard runs before the compute_scratch_dir guard when cloud burst is enabled"

requirements-completed: [KSTAGE-04, KSTAGE-05]

# Metrics
duration: ~40min
completed: 2026-06-27
---

# Phase 53 Plan 01: S3 object-staging foundation Summary

**S3 ControlSettings config surface (endpoint/bucket/region/addressing + _FILE-resolved SecretStr creds, SSRF + fail-fast guards) and the CloudJob per-file_id staging sidecar with a reversible migration 025 — the contracts every downstream Phase 53 plan builds on.**

## Performance

- **Duration:** ~40 min
- **Completed:** 2026-06-27
- **Tasks:** 2 (both TDD)
- **Files created:** 5
- **Files modified:** 5

## Accomplishments
- S3 config surface on `ControlSettings` only (KSTAGE-02): `s3_endpoint_url`, `s3_bucket`, `s3_region`, `s3_addressing_style` (Literal path|virtual), `SecretStr` access/secret keys, and bounded presign/lifecycle/part-size knobs.
- `_FILE` secret resolution for S3 creds via a 2-line `SECRET_FILE_FIELDS` extension (KSTAGE-05) — no new resolution code.
- `field_validator` rejecting SSRF-shaped endpoints (non-http(s) / scheme-less); `_enforce_s3_config_when_cloud_enabled` fail-fast validator mirroring the compute-scratch-dir guard.
- `CloudJob` model + `CloudJobStatus` StrEnum (D-03): one row per `file_id` (unique FK), `s3_key`, DB-checked `status`, multipart `upload_id` — staging-only (no `kueue_workload`/`cloud_phase`).
- Reversible migration 025: `cloud_job` create-table with `pk/fk/uq` naming convention + `status` CHECK; `alembic upgrade head` and `downgrade -1` both verified against the integration DB.
- `aioboto3` (deps) + `moto` (dev) declared; full suite green (2292 passed).

## Task Commits

Each TDD task committed RED → GREEN:

1. **Task 1: S3 config surface (RED)** - `a3ab34c` (test)
2. **Task 1: S3 config surface (GREEN)** - `a2799b2` (feat)
3. **Task 2: CloudJob model + migration 025 (RED)** - `e69a5a0` (test)
4. **Task 2: CloudJob model + migration 025 (GREEN)** - `cd9d35a` (feat)

_Note: no REFACTOR commits needed; both implementations were minimal-and-clean at GREEN._

## Files Created/Modified
- `src/phaze/config.py` - S3 fields on ControlSettings, SECRET_FILE_FIELDS extension, endpoint field_validator, cloud-enabled S3 fail-fast validator
- `src/phaze/models/cloud_job.py` - CloudJobStatus StrEnum + CloudJob ORM model (per-file_id sidecar)
- `src/phaze/models/__init__.py` - register CloudJob/CloudJobStatus for Alembic autogenerate + metadata create_all
- `alembic/versions/025_add_cloud_job.py` - reversible create-table migration (unique FK + status CHECK)
- `pyproject.toml` - aioboto3 dependency + moto dev dependency
- `tests/test_config/test_s3_settings.py` - 20 tests covering binding, _FILE, SSRF, bounds, KSTAGE-02 isolation
- `tests/test_models/test_cloud_job.py` - schema + round-trip + unique-FK + status-CHECK tests
- `tests/test_migrations/test_migration_025_cloud_job.py` - revision/no-saq_jobs + upgrade/downgrade reversibility

## Decisions Made
- **moto placement:** CLAUDE.md mandates alphabetically-sorted dependencies; "moto" sorts before "mypy", so it was placed after `httpx`/before `mypy` rather than the plan's literal "after mypy, before respx" instruction. The plan's intent (declare moto in dev) is met; the ordering follows the project convention.
- **Model-level CheckConstraint:** added `CheckConstraint(..., name="status_enum")` to `CloudJob.__table_args__` so `Base.metadata.create_all` (used by the model `session` fixture) builds the same `ck_cloud_job_status_enum` the migration declares — the status-CHECK test would otherwise have no constraint to trip.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Updated existing cloud_burst_toggle tests for the new cross-validator requirement**
- **Found during:** Task 1 (S3 config surface)
- **Issue:** The new `_enforce_s3_config_when_cloud_enabled` validator made 3 existing `test_cloud_burst_toggle.py` tests fail — they enabled cloud burst with a scratch dir but no S3 config, and the S3 validator (defined first) now fired.
- **Fix:** Added `PHAZE_S3_BUCKET` + `PHAZE_S3_ENDPOINT_URL` to those tests so each exercises only its intended assertion; the compute-scratch-dir guard test now satisfies S3 config so the scratch-dir guard is the one that fires.
- **Files modified:** tests/test_config/test_cloud_burst_toggle.py
- **Verification:** full test_config suite green (83 passed)
- **Committed in:** a2799b2 (Task 1 GREEN commit)

**2. [Rule 3 - Blocking] Updated test_all_tables_defined for the new cloud_job table**
- **Found during:** Task 2 (CloudJob model)
- **Issue:** `test_core_models.py::test_all_tables_defined` asserts the EXACT metadata table set (was 17); adding `cloud_job` broke the equality.
- **Fix:** Added `"cloud_job"` to the expected set and updated the docstring count to 18.
- **Files modified:** tests/test_models/test_core_models.py
- **Verification:** test passes
- **Committed in:** cd9d35a (Task 2 GREEN commit)

**3. [Rule 1 - Bug] Seeded agent_id in migration-test file INSERTs**
- **Found during:** Task 2 (migration test)
- **Issue:** The migration-DB `files` table has a NOT NULL `agent_id` FK; the RED test's raw INSERTs omitted it (NotNullViolationError).
- **Fix:** Added `agent_id='legacy-application-server'` (the migration-012-seeded agent) to both `files` INSERTs.
- **Files modified:** tests/test_migrations/test_migration_025_cloud_job.py
- **Verification:** migration test passes (upgrade/downgrade + unique-FK + CHECK)
- **Committed in:** cd9d35a (Task 2 GREEN commit)

---

**Total deviations:** 3 auto-fixed (2 blocking test-contract updates, 1 test-data bug)
**Impact on plan:** All within scope — directly caused by this plan's own additions (new validator, new table, FK seeding). No scope creep; no production-code deviations beyond the planned surface.

## Issues Encountered
- No local Postgres on the default 5432; the integration/migration/model tests require a DB. Resolved by `just test-db` (ephemeral Postgres on 5433 + the `phaze_migrations_test` DB) and running with the matching `TEST_DATABASE_URL`/`MIGRATIONS_TEST_DATABASE_URL`/`PHAZE_REDIS_URL` env vars (the `just integration-test` wiring).

## User Setup Required
**External S3-compatible object storage must be configured before cloud burst is enabled.** Per the plan `user_setup`:
- `PHAZE_S3_ENDPOINT_URL` — operator's S3 provider endpoint (MinIO/Backblaze/AWS, etc.)
- `PHAZE_S3_BUCKET` — operator-created ephemeral staging bucket
- `PHAZE_S3_ACCESS_KEY_ID_FILE` / `PHAZE_S3_SECRET_ACCESS_KEY_FILE` — file-mounted secrets (control plane only)

These are optional while `PHAZE_CLOUD_BURST_ENABLED=false` (the default); the config fails fast at startup if cloud burst is on but bucket/endpoint are unset.

## Next Phase Readiness
- Config contracts and the `cloud_job` row are now defined for downstream Phase 53 plans (s3_staging service, presign routes, upload task, inline delete) and for Phase 54 (`kueue_workload`) / Phase 55 (`cloud_phase`), which add their own columns in their own migrations.
- No blockers. Migration head is now `025`.

## Self-Check: PASSED

All 5 created files verified present; all 4 task commits (`a3ab34c`, `a2799b2`, `e69a5a0`, `cd9d35a`) verified in git log. Full suite: 2292 passed.

---
*Phase: 53-s3-object-staging-leg*
*Completed: 2026-06-27*
