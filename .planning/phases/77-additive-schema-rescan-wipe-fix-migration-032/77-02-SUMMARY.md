---
phase: 77-additive-schema-rescan-wipe-fix-migration-032
plan: 02
subsystem: database
tags: [sqlalchemy, orm, partial-index, check-constraint, alembic-autogenerate, schema, dedup, cloud-job]

# Dependency graph
requires: []
provides:
  - "analysis + metadata carry nullable failed_at (timestamptz) + error_message (Text) failure-marker columns (D-01)"
  - "analysis/metadata partial indexes ix_analysis_completed, ix_analysis_failed, ix_metadata_failed (IS NOT NULL predicates)"
  - "CloudJobStatus.AWAITING = 'awaiting' + widened ck_cloud_job_status_enum CHECK to 7 members (D-04)"
  - "ix_cloud_job_awaiting + ix_fprint_success partial indexes; ix_fprint_success spelled = ANY (ARRAY[...]) not bare IN (PERF-01/Pitfall 1)"
  - "New DedupResolution 1:1 sidecar model (unique file_id FK, nullable canonical_file_id) registered on Base.metadata (D-07)"
  - "The ORM half of migration 032's empty-autogenerate-diff contract: every column/CHECK/table/index the migration will create is declared with byte-identical names + normalized predicate text"
affects: [77-03, 78, 81, 82, 83, 84, "stage_status derivation", "migration 032"]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Partial-index ORM mirror: Index(name, col, postgresql_where=text(...)) in __table_args__ with byte-identical name to the migration's op.create_index (empty-autogenerate-diff contract)"
    - "Multi-value partial-index predicate spelled = ANY (ARRAY['a','b']) — never bare IN (...) — because Postgres reserializes IN to = ANY(ARRAY[...]) and breaks the diff comparison"
    - "String-backed StrEnum member add = enum member + CHECK membership-list widening only (no Postgres enum-type migration); bare CHECK name status_enum re-prefixed by the ck_%(table_name)s_%(constraint_name)s convention"
    - "1:1 sidecar model via unique-FK to files.id + TimestampMixin (cloud_job precedent); best-effort pointer column left NULLABLE"

key-files:
  created:
    - "src/phaze/models/dedup_resolution.py"
  modified:
    - "src/phaze/models/analysis.py"
    - "src/phaze/models/metadata.py"
    - "src/phaze/models/cloud_job.py"
    - "src/phaze/models/fingerprint.py"
    - "src/phaze/models/__init__.py"
    - "tests/shared/models/test_core_models.py"

key-decisions:
  - "Failure markers are nullable failed_at + error_message columns on the existing 1:1 analysis/metadata tables (D-01) — analysis_completed_at stays NULL for a failed analyze row so the future done-over-failed precedence holds."
  - "AnalysisResult and FileMetadata had NO __table_args__ before this phase; both gained one carrying only the additive partial indexes (existing columns untouched)."
  - "ix_fprint_success authored as status = ANY (ARRAY['success','completed']) in the ORM mirror to match Postgres's normalized serialization — the sharpest empty-diff risk (RESEARCH Pitfall 1)."
  - "cloud_job status CHECK widened via the bare name status_enum (convention re-prefixes to ck_cloud_job_status_enum); 'awaiting' (8 chars) fits status String(16) with no column widening."
  - "DedupResolution.canonical_file_id is NULLABLE (RESEARCH Pitfall 4 — a sha256 group may have 0 or >1 non-resolved members; the original human keeper is not recoverable); the marker's primary job is resolved-ness."
  - "DedupResolution has NO extra __table_args__ index — the unique file_id constraint's implicit index serves the marker-EXISTS lookup (per plan interfaces)."

patterns-established:
  - "ORM-declares-migration-shape: for the additive migration 032, the ORM model half is authored FIRST (this plan) so Plan 03's migration mirrors byte-for-byte and autogenerate diffs empty."

requirements-completed: [MIG-01, PERF-01]

# Metrics
duration: ~20min
completed: 2026-07-08
---

# Phase 77 Plan 02: Additive ORM Schema (Migration 032 ORM Half) Summary

**Declared every column, CHECK member, table, and partial index that migration `032` will create — nullable `failed_at`/`error_message` markers on `analysis`/`metadata`, `CloudJobStatus.AWAITING` + widened CHECK, and the new `DedupResolution` sidecar — with byte-identical index names and normalized predicate text, satisfying the ORM half of PERF-01's empty-autogenerate-diff contract (MIG-01, D-01/D-04/D-07).**

## Performance

- **Duration:** ~20 min
- **Completed:** 2026-07-08
- **Tasks:** 2 completed
- **Files:** 1 created, 6 modified (5 source + 1 test)

## Accomplishments
- **analysis + metadata failure markers (D-01):** added nullable `failed_at` (timestamptz) + `error_message` (Text) to both 1:1 output tables (not a generic `stage_failure` table), and gave each a brand-new `__table_args__` with the `IS NOT NULL`-shaped partial indexes (`ix_analysis_completed`, `ix_analysis_failed`, `ix_metadata_failed`).
- **cloud_job AWAITING (D-04):** appended `AWAITING = "awaiting"` to `CloudJobStatus` and widened the `status_enum` CHECK to the 7-member list via the bare-name idiom; added `ix_cloud_job_awaiting` (`status = 'awaiting'`) to the existing `__table_args__` tuple.
- **fingerprint success index (PERF-01):** extended the existing `__table_args__` with `ix_fprint_success` spelled `= ANY (ARRAY['success','completed'])` — never bare `status IN (...)` — to survive Postgres predicate reserialization and keep the autogenerate diff empty.
- **DedupResolution model (D-07):** created the new 1:1 sidecar (`unique file_id` FK, `nullable canonical_file_id` FK, `resolved_at` server-default now, `TimestampMixin` for created/updated) and registered it in `models/__init__.py` (import + `__all__`) so `Base.metadata`/autogenerate attaches the table.

## Task Commits

1. **Task 1: failure-marker columns + partial-index __table_args__ on analysis & metadata** — `659cf3f5` (feat)
2. **Task 2: AWAITING + CHECK/index on cloud_job & fingerprint; create + register DedupResolution** — `149b6ef8` (feat)
3. **Deviation fix: register dedup_resolution in the all-tables enumeration test** — `d4490cf6` (test)

## Files Created/Modified
- `src/phaze/models/dedup_resolution.py` (NEW) — `DedupResolution` 1:1 dedup marker sidecar; existence = resolved, undo = DELETE the row.
- `src/phaze/models/analysis.py` — `failed_at` + `error_message` columns; new `__table_args__` (`ix_analysis_completed`, `ix_analysis_failed`); added `Index`, `text` imports.
- `src/phaze/models/metadata.py` — same two columns; new `__table_args__` (`ix_metadata_failed`); added `datetime`, `DateTime`, `Index`, `text` imports.
- `src/phaze/models/cloud_job.py` — `CloudJobStatus.AWAITING`; widened `status_enum` CHECK; `ix_cloud_job_awaiting` added to `__table_args__`; added `Index`, `text` imports.
- `src/phaze/models/fingerprint.py` — `ix_fprint_success` (`= ANY (ARRAY[...])`) added to `__table_args__`; added `text` import.
- `src/phaze/models/__init__.py` — registered `DedupResolution` (import + `__all__`).
- `tests/shared/models/test_core_models.py` — added `dedup_resolution` to the expected all-tables set (see Deviations).

## Verification
- **Task 1 automated verify:** ruff + mypy clean; metadata assertion confirms `failed_at`/`error_message` on both tables and the three partial indexes present → `OK`.
- **Task 2 automated verify:** ruff + mypy clean; `CloudJobStatus.AWAITING == 'awaiting'`, `dedup_resolution` in `Base.metadata.tables` with all six expected columns (`file_id` unique+NOT NULL, `canonical_file_id` nullable), and `ix_cloud_job_awaiting` + `ix_fprint_success` present → `OK`.
  - The plan's verify string imported `Base` from `phaze.models`, where it is not re-exported; corrected to `phaze.models.base` (verify-command typo only — the models are correct).
- **Bare-IN grep guard:** `grep -n "status IN" src/phaze/models/fingerprint.py` matches only the explanatory comment — no `postgresql_where` uses a bare `IN`.
- **Touched bucket (`shared`) in isolation, against the ephemeral :5433 DB:** `uv run pytest tests/shared/models/` → **39 passed** — including the DB-backed `test_cloud_job.py` CHECK-constraint persistence test (validates the widened `status_enum` at DDL level) and the corrected all-tables enumeration.
- Pre-commit hooks ran on every commit (ruff, ruff-format, bandit, mypy, file hygiene) → all Passed; no `--no-verify`.
- **NOTE (deferred to Plan 03):** the empty-`--autogenerate`-diff acceptance is only assertable AFTER migration `032` lands (Plan 03) — against a not-yet-migrated DB the diff correctly shows these objects as pending. This plan's gate is metadata-presence + lint/type, per the plan's `<verification>`.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Registered the new table in the all-tables enumeration test**
- **Found during:** post-Task-2 bucket run (`tests/shared/models/`).
- **Issue:** `tests/shared/models/test_core_models.py::test_all_tables_defined` asserts an exact set of `Base.metadata.tables` keys; the required additive `dedup_resolution` model (D-07) made the assertion fail with an extra item.
- **Fix:** added `"dedup_resolution"` to the expected set and relaxed the hardcoded "19 expected tables" docstring count. No production behavior changed — the test was rendered stale by the plan's own additive model.
- **Files modified:** `tests/shared/models/test_core_models.py`
- **Commit:** `d4490cf6`
- **Note:** the file is tracked but matches a broad `models/` gitignore pattern, so it required `git add -f` (pre-existing repo condition, not introduced here).

Otherwise the plan executed as written.

## Threat Surface

Per the plan's `<threat_model>`: T-77-05 (Tampering — `cloud_job` status CHECK membership) is honored — the DB CHECK remains the authoritative status gate; the enum member alone cannot bypass it (the CHECK widening lands in Plan 03's migration). T-77-06 (Info disclosure — `dedup_resolution` FKs to `files.id`) accepted — both FK columns reference internal file UUIDs only, no PII, `canonical_file_id` nullable/best-effort. No new network endpoint, auth path, or trust boundary introduced (ORM schema declarations only; nothing reads them yet).

## Self-Check: PASSED
