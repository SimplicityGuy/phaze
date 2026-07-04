---
phase: 70-multi-kueue-n-clusters
plan: 01
subsystem: infra
tags: [alembic, postgres, sqlalchemy, s3, kr8s, kubeconfig, pydantic, cloud-job, multi-cluster]

# Dependency graph
requires:
  - phase: 68-backend-protocol-refactor
    provides: "cloud_job.backend_id column + migration 029 (the additive-column analog mirrored here)"
  - phase: 67-backend-registry
    provides: "KubeConfig / BucketConfig discriminated-union submodels in config_backends.py"
provides:
  - "cloud_job.staging_bucket nullable column + migration 030 (records which BucketConfig.id staged the object; D-01/D-02/MKUE-04)"
  - "pure pick_bucket(file_id, bucket_ids) deterministic per-file bucket selector (sha256-of-UUID-bytes mod sorted; D-06/MKUE-02)"
  - "KubeConfig.context field for kubeconfig+context auth path (A1/MKUE-01)"
  - "PyYAML declared explicitly in [project].dependencies (hygiene for Plan 03 kubeconfig parse)"
affects: [70-02, 70-03, 70-04, 70-05, multi-kueue, s3-staging, kube-staging, reconcile, backends]

# Tech tracking
tech-stack:
  added: ["PyYAML>=6.0.3 (explicit declaration of an already-resolved transitive kr8s dep)"]
  patterns:
    - "Additive nullable column + mirror-029 migration (no CHECK, no backfill, never references saq_jobs)"
    - "Pure restart-stable hash selector (sha256 of UUID bytes, not Python salted hash()) recording an authoritative value read (never re-derived) downstream"

key-files:
  created:
    - alembic/versions/030_add_cloud_job_staging_bucket.py
    - tests/integration/test_migrations/test_migration_030_staging_bucket.py
  modified:
    - src/phaze/models/cloud_job.py
    - src/phaze/services/s3_staging.py
    - tests/analyze/services/test_s3_staging.py
    - src/phaze/config_backends.py
    - pyproject.toml
    - uv.lock

key-decisions:
  - "PyYAML declared as PyYAML>=6.0.3 (currently-resolved floor, >=7d old so exclude-newer cooldown holds); placed alphabetically after python-multipart"
  - "pick_bucket lands in s3_staging.py at the L77 TRANSITIONAL marker; only this helper added — no other verb parameterized (that is Plan 02), keeping the module ORM-free"
  - "staging_bucket kept plain free-text (no CHECK/enum) mirroring backend_id; unique(file_id) untouched (D-02)"

patterns-established:
  - "Migration 030 mirrors 029 exactly minus the 029-only s3_key NOT-NULL leg; static (no-DB) revision-id + saq_jobs-grep tests plus a DB round-trip guarded by a file-exists skipif"
  - "pick_bucket is authoritative-at-stage-time: the returned id is recorded on cloud_job.staging_bucket; presign/cleanup read the column, never re-derive"

requirements-completed: [MKUE-01, MKUE-02, MKUE-04]

# Metrics
duration: 8min
completed: 2026-07-04
---

# Phase 70 Plan 01: Multi-Kueue Additive Foundation Summary

**Additive, behavior-preserving foundation for N-cluster Kueue: the `cloud_job.staging_bucket` column + migration 030, the pure restart-stable `pick_bucket` per-file bucket selector, and the `KubeConfig.context` field — all landing green without changing runtime behavior.**

## Performance

- **Duration:** ~8 min
- **Started:** 2026-07-04T18:33:00Z
- **Completed:** 2026-07-04T18:40:00Z
- **Tasks:** 3
- **Files created/modified:** 8 (2 created, 6 modified)

## Accomplishments
- `cloud_job.staging_bucket` nullable column + migration 030 (mirror of 029: additive `add_column`/`drop_column`, no CHECK, no backfill, `unique(file_id)` preserved, never references `saq_jobs`) — D-01/D-02/MKUE-04.
- Pure `pick_bucket(file_id, bucket_ids)` deterministic selector using `sha256(file_id.bytes) mod len(sorted(bucket_ids))` — restart-stable (proven against a hand-computed digest, not Python's salted `hash()`), empty-set fails loud, output always a member — D-06/MKUE-02. `s3_staging` stays ORM-free.
- `KubeConfig.context: str | None = None` optional non-secret field for the kubeconfig+context auth path — A1/MKUE-01.
- `PyYAML>=6.0.3` declared explicitly in `[project].dependencies` (hygiene; already a resolved transitive kr8s dep) — `uv sync` resolves cleanly.

## Task Commits

Each task committed atomically (TDD tasks show test → feat):

1. **Task 1: cloud_job.staging_bucket column + migration 030** — `eb0dd07` (test, RED) → `0fb88c9` (feat, GREEN)
2. **Task 2: pure pick_bucket deterministic selector** — `e8b98ad` (test, RED) → `6e8fd29` (feat, GREEN)
3. **Task 3: KubeConfig.context field + declare PyYAML** — `2ab0ecb` (feat)

## Files Created/Modified
- `alembic/versions/030_add_cloud_job_staging_bucket.py` — additive nullable `staging_bucket` migration; upgrade `add_column`, downgrade `drop_column`; CRITICAL never-references-`saq_jobs` banner kept.
- `tests/integration/test_migrations/test_migration_030_staging_bucket.py` — mirrors the 029 test: bare-number revision ids, `saq_jobs` grep, upgrade/downgrade round-trip probing `information_schema.columns` for existence + `is_nullable == "YES"`, plus a `unique(file_id)` preservation assertion.
- `src/phaze/models/cloud_job.py` — `staging_bucket: Mapped[str | None]` after `backend_id`; free-text, nullable, `__table_args__`/`unique(file_id)` untouched.
- `src/phaze/services/s3_staging.py` — `import hashlib` + pure `pick_bucket` helper at the L77 TRANSITIONAL marker; no other verb changed.
- `tests/analyze/services/test_s3_staging.py` — 4 pure `pick_bucket` cases (order-independence, hand-computed sha256 formula/restart-stability, empty-set-raises, always-a-member).
- `src/phaze/config_backends.py` — `KubeConfig.context` optional plain string field.
- `pyproject.toml` / `uv.lock` — explicit `PyYAML>=6.0.3` dependency (alphabetical).

## Decisions Made
- PyYAML written as `PyYAML>=6.0.3` (canonical PyPI casing) floored at the currently-resolved `uv.lock` version (6.0.3, well over 7 days old so the `exclude-newer` cooldown is satisfiable), placed alphabetically after `python-multipart`.
- Only `pick_bucket` was added to `s3_staging.py`; the public-verb parameterization (`bucket: BucketConfig`) and `_staging_config()` retirement are explicitly deferred to Plan 02, so the module stays green and ORM-free.
- No runtime `import uuid` added to `s3_staging.py`: `from __future__ import annotations` defers the `uuid.UUID` annotation and `pick_bucket` only does attribute access (`file_id.bytes`), so `hashlib` is the only new runtime import.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
- The migration round-trip test requires a live Postgres; the default `localhost:5432` was not running. Started the project's ephemeral test DB via `just test-db` (Postgres on `localhost:5433`, `phaze_migrations_test` auto-created) and ran the round-trip with `MIGRATIONS_TEST_DATABASE_URL=...5433...` — all 3 migration tests pass. The two static (no-DB) tests pass regardless.
- `tests/analyze/services/test_backends.py` errored under the default DB port (asyncpg connect failures); confirmed environmental (not caused by this plan) by re-running against the running test DB (`TEST_DATABASE_URL=...5433...`) — those tests pass.

## Known Stubs
None — all three additions are complete, tested, and wired for the downstream plans that consume them. No placeholder data or unwired components.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Plan 02 can now stamp `staging_bucket` on the `cloud_job` upsert, thread `bucket: BucketConfig` through the `s3_staging` verbs, and resolve recorded buckets at the router call sites.
- Plan 03 can consume `KubeConfig.context` in `kube_staging._api` and parse inline kubeconfig YAML via the now-explicit PyYAML dependency.
- Migration 030 is the new head off 029; no migration conflicts introduced.

## Verification
- `uv run pytest tests/integration/test_migrations/test_migration_030_staging_bucket.py tests/analyze/services/test_s3_staging.py` → 20 passed (against the ephemeral test DB on 5433).
- `uv run python -c "from phaze.config_backends import KubeConfig; assert 'context' in KubeConfig.model_fields"` → OK.
- `uv run ruff check .` → All checks passed; `uv run mypy .` → no issues in 192 source files.
- All pre-commit hooks green on every task commit.

---
*Phase: 70-multi-kueue-n-clusters*
*Completed: 2026-07-04*
