---
phase: 77-additive-schema-rescan-wipe-fix-migration-032
plan: 03
subsystem: database
tags: [alembic, migration, backfill, upsert, partial-index, autogenerate-diff, dedup, cloud-job, integration-test]

# Dependency graph
requires:
  - "77-02: the ORM half (failed_at/error_message columns, DedupResolution model, CloudJobStatus.AWAITING + widened CHECK, 5 partial-index __table_args__ mirrors) so 032's DDL mirrors it byte-for-byte"
provides:
  - "Migration 032: additive DDL (analysis/metadata failure-marker columns, dedup_resolution table, widened cloud_job status CHECK, 5 partial indexes) + set-based read-only backfill from files.state"
  - "analyze-failed UPSERT marker backfill (INSERT..ON CONFLICT (file_id) DO UPDATE — no analysis row guaranteed)"
  - "dedup_resolution backfill with deterministic nullable canonical_file_id derivation (ORDER BY c.id LIMIT 1 among non-resolved same-sha256 members)"
  - "cloud_job awaiting/uploading/uploaded sidecar gap-fill for awaiting_cloud/pushing/pushed files (D-04/D-06); LOCAL_ANALYZING gets no row (D-05)"
  - "PERF-01 empty-autogenerate-diff proven automated (scoped to the 032 objects) — ORM↔DB parity"
  - "Per-migration integration test proving upgrade+backfill+invariants, saq_jobs banner guard, files.state byte-unchanged, minimal downgrade (D-09)"
affects: [78, 80, 81, 82, 83, 84, 90, "stage_status derivation", "migration 033 (destructive, Phase 90)"]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Additive migration mirrors a pre-authored ORM half → empty autogenerate diff (ORM-declares-migration-shape, 77-02 precedent)"
    - "analyze-failed marker backfill as INSERT..SELECT..ON CONFLICT (file_id) DO UPDATE — idempotent upsert because report_analysis_failed writes no analysis row (RESEARCH Pitfall 2)"
    - "Deterministic nullable canonical derivation via correlated subquery ORDER BY c.id LIMIT 1 (RESEARCH Pitfall 4)"
    - "Set-based static-literal op.execute(sa.text(...)) backfill, files.state read-only, saq_jobs never referenced (016 precedent)"
    - "Automated empty-autogenerate-diff assertion scoped to a named object set (new in-tree capability) via alembic.autogenerate.compare_metadata over run_sync — filters unrelated pre-existing drift"

key-files:
  created:
    - "alembic/versions/032_add_derived_status_schema.py"
    - "tests/integration/test_migrations/test_migration_032_additive_schema.py"
  modified: []

key-decisions:
  - "Empty-autogenerate-diff scoped to the 032 objects only (dedup_resolution, the 4 marker columns, the 5 indexes) — the migrations-test DB carries pre-existing unrelated ORM↔DB drift (TimestampMixin DateTime() naive vs timestamptz on legacy tables; dropped search_vector/trgm indexes) that predates and is out of scope for this phase; the filter proves 032's own parity without fighting that noise."
  - "ix_fprint_success KEPT — the empty-diff assertion passed with the = ANY (ARRAY['success','completed']) spelling; the plan's drop-and-defer-to-Phase-82 contingency was NOT triggered (offenders empty)."
  - "The integration test deletes ALL cloud_job rows before downgrading (029 precedent) — the backfilled awaiting rows violate the restored 6-member CHECK and the NULL-s3_key uploading/uploaded rows trip migration 029's s3_key NOT NULL re-imposition during the teardown walk to base."
  - "dedup_resolution created_at/updated_at authored as sa.DateTime() (naive) to match the ORM TimestampMixin; resolved_at as sa.DateTime(timezone=True) to match the model — keeps the new table's own columns diff-clean."

patterns-established:
  - "Automated empty-autogenerate-diff gate: compare_metadata(MigrationContext(compare_type=True), Base.metadata) run via conn.run_sync, flattened, filtered to a named 032 object set — first in-tree automation of PERF-01's SC#2 (no prior precedent)."

requirements-completed: [MIG-01, PERF-01]

# Metrics
duration: ~25min
completed: 2026-07-08
---

# Phase 77 Plan 03: Migration 032 (Additive DDL + Backfill) + Integration Test Summary

**Authored the additive migration `032` mirroring Plan 02's ORM byte-for-byte — the analyze/metadata `failed_at`/`error_message` markers, the `dedup_resolution` sidecar, the widened `cloud_job` status CHECK (`'awaiting'`), and 5 partial indexes — with set-based read-only backfills (analyze-failed UPSERT, deterministic nullable dedup canonical, cloud awaiting/pushing/pushed gap-fill) that never write `files.state` or reference `saq_jobs`, plus a per-migration integration test that proves the upgrade + backfill invariants and an automated EMPTY autogenerate diff (MIG-01, PERF-01, D-02..D-09).**

## Performance

- **Duration:** ~25 min
- **Completed:** 2026-07-08
- **Tasks:** 2 completed
- **Files:** 2 created, 0 modified

## Accomplishments

- **Migration 032 additive DDL (MIG-01):** `revision="032"`, `down_revision="031"`. `upgrade()` adds nullable `failed_at` (timestamptz) + `error_message` (Text) to `analysis` and `metadata`; creates the `dedup_resolution` 1:1 sidecar (UUID PK, unique `file_id` FK, nullable `canonical_file_id` FK, `resolved_at` timestamptz, TimestampMixin cols) with `op.f(...)`-named PK/UQ/FK; widens the `status_enum` CHECK to the 7-member list via the bare-name idiom (D-04); and creates the 5 partial indexes with names + predicate text byte-identical to the ORM `__table_args__`.
- **Read-only set-based backfills:** analyze-failed is an `INSERT..SELECT..ON CONFLICT (file_id) DO UPDATE` UPSERT (D-03/Pitfall 2 — `report_analysis_failed` persists no `analysis` row, so a failed file may have none); dedup derives a deterministic nullable `canonical_file_id` via `ORDER BY c.id LIMIT 1` among non-resolved same-`sha256` members (D-07/Pitfall 4); cloud `awaiting`/`uploading`/`uploaded` rows are gap-filled with `ON CONFLICT (file_id) DO NOTHING` for `awaiting_cloud`/`pushing`/`pushed` files missing one (D-04/D-06); `metadata` gets NO backfill (D-03); `LOCAL_ANALYZING` gets no row (D-05). `files.state` is never written.
- **Empty-autogenerate-diff automation (PERF-01 / SC#2):** the test runs `alembic.autogenerate.compare_metadata` against `Base.metadata` (via `conn.run_sync`, `compare_type=True`) at the 032 head and asserts NO add/remove op touches any 032 object. It passed with `ix_fprint_success` present (`= ANY (ARRAY[...])` spelling) — the plan's drop-and-defer contingency was NOT needed.
- **Per-migration integration test:** seeds a corpus with a file in each legacy `files.state` (incl. an `analysis_failed` file WITHOUT a prior `analysis` row to exercise the INSERT branch, one WITH a partial row for DO UPDATE, a matching `sha256` group for canonical derivation, a lonely-hash resolved file for the NULL-canonical case, and a `pushing` file with a pre-existing `cloud_job` row for the gap-fill DO-NOTHING case). Asserts columns/table/CHECK exist, backfill counts match legacy state counts, `metadata.failed_at` all-NULL, canonical derivation (target + NULL), `files.state` byte-unchanged, 5 indexes in `pg_indexes`, and a minimal-downgrade reversal. DB-free `test_revision_identifiers_are_bare_numbers` + `test_migration_never_references_saq_jobs` guards mirror the 031 analog.

## Task Commits

1. **Task 1: migration 032 additive DDL + set-based backfill + minimal downgrade** — `72081512` (feat)
2. **Task 2: per-migration integration test + saq guard + empty-autogenerate-diff assertion** — `99301dbc` (test)

## Files Created/Modified

- `alembic/versions/032_add_derived_status_schema.py` (NEW) — additive migration: columns, `dedup_resolution` table, CHECK widen, 5 partial indexes, 5 set-based backfills, minimal DDL downgrade. Docstring carries the CRITICAL `saq_jobs` never-reference banner + the D-02 (`done(metadata)` tightens to `failed_at IS NULL`), D-03 (analyze backfills, metadata does not), and D-05 (LOCAL_ANALYZING derived) callouts.
- `tests/integration/test_migrations/test_migration_032_additive_schema.py` (NEW) — DB-free static guards + seeded-corpus upgrade/backfill/invariant integration body + automated empty-autogenerate-diff assertion + minimal-downgrade smoke. Bucket = `integration`.

## Verification

- **Task 1:** `uv run ruff check` + `uv run mypy` clean; static probe confirms `revision=='032'`/`down_revision=='031'`/`branch_labels is None`, the `ON CONFLICT (file_id) DO UPDATE` upsert, the `= ANY (ARRAY['success','completed'])` fingerprint predicate, and no non-comment `saq_jobs` reference.
- **Task 2:** `test_migration_032_additive_schema.py` → **3 passed** against the `:5433` ephemeral DB (2 DB-free statics + the integration body incl. the automated empty-diff assertion).
- **Bucket in isolation:** `just test-bucket integration` (with the `:5433` DB env exported, mirroring `just integration-test`) → **71 passed** — per-bucket hermeticity holds.
- **Pre-commit on every commit:** ruff, ruff-format, bandit, and the whole-tree `uv run mypy .` hook all Passed; no `--no-verify`. (ruff-format reflowed two long `execute` calls in the test on the first Task-2 commit attempt; re-staged and committed.)
- **Empty-`--autogenerate`-diff (PERF-01 SC#2):** proven AUTOMATED in the test (scoped to the 032 objects). The DB carries pre-existing unrelated ORM↔DB drift (naive `DateTime()` TimestampMixin vs `timestamptz` on legacy tables; dropped `search_vector`/trgm indexes) that the scoped filter correctly ignores — those predate this phase and are not in its contract.
- **No cloud-lane drain required (RESEARCH §Runtime State):** `032` is additive/non-destructive; the drain gate belongs to the destructive `033` (Phase 90).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 — Bug] Docstring `saq_jobs` mentions tripped the banner grep guard**
- **Found during:** Task 1 static verify.
- **Issue:** two docstring lines describing the D-05 LOCAL_ANALYZING derivation used the literal token `saq_jobs` without the phrase "never reference", so the `test_migration_never_references_saq_jobs` grep guard (mirrored from the 031 analog) flagged them as offending.
- **Fix:** reworded both to "the SAQ in-flight job set" — the CRITICAL banner line (which contains "never reference `saq_jobs`") is the only remaining literal-token mention and is correctly excluded.
- **Files modified:** `alembic/versions/032_add_derived_status_schema.py`
- **Commit:** `72081512` (folded into Task 1).

**2. [Rule 1 — Bug] Downgrade/teardown tripped migration 029's `s3_key NOT NULL` re-imposition**
- **Found during:** Task 2 first test run.
- **Issue:** the backfilled `cloud_job` `uploading`/`uploaded` sidecar rows have `s3_key NULL`; the test's `finally` teardown walks `downgrade_to(base)` through migration `029`, which re-imposes `s3_key NOT NULL` and aborted on those rows (also poisoning the DB for the next run). The initial `DELETE ... WHERE status='awaiting'` cleaned only the CHECK violators, not the NULL-`s3_key` rows.
- **Fix:** delete ALL `cloud_job` rows before downgrading (the `test_migration_029` precedent); reset the poisoned migrations-test schema once.
- **Files modified:** `tests/integration/test_migrations/test_migration_032_additive_schema.py`
- **Commit:** `99301dbc` (folded into Task 2).

### Contingency NOT triggered

The plan's `ix_fprint_success` drop-and-defer-to-Phase-82 contingency was NOT applied: the automated empty-diff assertion passed with the index present (the `= ANY (ARRAY[...])` spelling round-trips cleanly), so it stays in both the migration and `models/fingerprint.py`.

## Threat Surface

Per the plan's `<threat_model>`: **T-77-01** (Tampering — backfill SQL) mitigated: every backfill is a static string literal via `op.execute(sa.text(...))` with only fixed `FileState` literals, no interpolation/model import (bandit clean). **T-77-02** (Tampering — `saq_jobs`) mitigated: the CRITICAL docstring banner + the `test_migration_never_references_saq_jobs` grep guard both enforce it; the migration touches only `analysis`/`metadata`/`dedup_resolution`/`cloud_job`. **T-77-07** (Tampering — writing `files.state`) mitigated: `files.state` is a READ-only SELECT source in every backfill; the integration test snapshots and asserts it byte-unchanged. No new network endpoint, auth path, or trust boundary introduced.

## Self-Check: PASSED

Both created files exist on disk; both task commits (`72081512`, `99301dbc`) are in the git log.
