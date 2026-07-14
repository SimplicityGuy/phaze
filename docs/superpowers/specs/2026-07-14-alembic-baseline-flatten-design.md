# Alembic Migration Chain Flatten — Design Spec

**Date:** 2026-07-14
**Status:** Approved (design); planned for the next milestone
**Author:** Robert Wlodarczyk (with Claude)

## Problem

`alembic/versions/` holds a linear chain of 39 migrations (`001`→`039`). For a
project that provisions every environment via `alembic upgrade head`, the long
chain is dead weight for fresh installs and slows onboarding/CI. We want a
**single baseline setup** — one migration that stands the current schema up from
scratch — without breaking the one long-lived database (prod).

## Precondition (satisfied)

The flatten is only safe once **every persistent Alembic-managed database is at
`039` (head)**. Prod is the *only* persistent DB, and as of the 2026-07-14
deploy **prod is at `039`**. Ephemeral CI / test / migration-test DBs are always
built from scratch, so they are irrelevant to re-stamping.

**Pre-merge gate:** re-confirm prod's `alembic_version.version_num == '039'` via
the read-only PG probe (`ssh datum@lux.lan`, direct `:5432`, `BEGIN TRANSACTION
READ ONLY`, base64'd `SELECT version_num FROM alembic_version`) immediately
before merge. If prod is not at `039`, the flatten holds.

## Approach — reuse `039` as the collapsed base (Alembic "prune old files")

This is Alembic's documented **"Prune Old Migration Files"** pattern (cookbook:
*Building an Up-to-Date Database from Scratch*): delete all but one file and set
the earliest remaining file's `down_revision = None`, making it the new base.

We keep exactly one file and **reuse revision id `039`**:

- Replace all 39 files with one `039_baseline_schema.py` whose
  `revision = "039"`, `down_revision = None`, and whose `upgrade()` builds the
  entire current schema **plus** the two required seed rows.
- **Fresh DB** (CI / test / new prod): runs the single `039` → full schema. ✅
- **Prod** (already stamped `039`): Alembic sees head == current and runs
  *nothing*. Zero manual steps, zero DDL, zero risk. This automatically
  satisfies the cookbook's `stamp` requirement (existing DB must recognize the
  new base) — prod's `alembic_version` already reads `039`.

### Why not the other documented option (`create_all` + `command.stamp`)

The cookbook's alternative — provision fresh DBs from `metadata.create_all()`
then `command.stamp("head")` — was rejected because:

1. `create_all` **misses non-metadata DDL**: partial indexes, the `033` XOR
   `CHECK`, tsvector/gin search vectors, and enums.
2. `create_all` **misses required seed rows** (below).
3. It **forks the provisioning path**: `src/phaze/database.py:73`
   (`run_migrations`) provisions *every* environment via `alembic upgrade head`.
   The prune pattern keeps that single mechanism unchanged everywhere; the
   `create_all` variant would make fresh installs bootstrap differently from how
   migrations are applied.

## Baseline authoring — output-anchored, not metadata-anchored

The baseline must reproduce what the **real 39-migration chain** produces, not
what the ORM metadata declares (they differ — see the non-metadata DDL + seeds).

Build procedure:

1. On an empty Postgres (the 5433 migration-test DB), run the **current** chain
   `alembic upgrade head` → ground truth (schema + seeds).
2. `pg_dump --schema-only` it; plus a data-only dump of the two control tables.
3. Author `039_baseline_schema.py`:
   - `upgrade()` = the normalized dumped DDL via `op.execute(...)` blocks,
     ordered enums → tables → constraints → indexes → tsvector/gin, then the two
     seed `INSERT`s using **bound params, no string interpolation** (matching the
     `020`/`031` threat-T-37-01 style).
   - `downgrade()` = drop everything (tables `CASCADE` + enums) so
     `downgrade base` round-trips cleanly in tests.

**Embed the dumped DDL** rather than reconstructing via `op.create_table`: the
project is Postgres-only, embedding is faithful by construction, and review
reduces to "matches the dump." (Reconstruction is more readable but reintroduces
the exact drift risk we are eliminating.)

### Required seed rows (must be in the baseline)

- `020` seeds `pipeline_stage_control` (one row per stage, `paused=false`,
  `priority=50`).
- `031` seeds `route_control` (the `force_local=false` singleton).

A schema-only baseline would produce a **broken** fresh install without these.

## Acceptance gate — proof of fidelity

The guarantee is a **schema-equivalence diff**, run once during execution and
captured as evidence in the phase VERIFICATION artifact:

- `DB_old` = run the pre-flatten chain from the git ref before deletion;
  `DB_new` = run the baseline alone.
- `pg_dump -s` both → normalize (strip `SET`/ownership/comments) → **`diff` must
  be empty**.
- Assert `pipeline_stage_control` + `route_control` rows are byte-identical
  between the two DBs.

An empty diff is the merge gate.

## Permanent test coverage

Replace the per-migration suite with durable **baseline schema-invariant** tests
on the existing 5433 harness (`tests/integration/test_migrations/conftest.py`):

- `033` XOR `CHECK` rejects invalid rows.
- `pipeline_stage_control` + `route_control` are seeded after `upgrade head`.
- Partial-unique indexes exist; search-vector/gin works; expected enums present;
  expected tables/columns present.
- Baseline upgrades clean from empty; `downgrade base` round-trips.
- `--autogenerate` yields an empty diff vs ORM metadata (catches future drift).

## Footprint (files)

- **Delete** all 39 files in `alembic/versions/`; **add** the single
  `039_baseline_schema.py`. Git history is the archive — nothing is moved to a
  keep-dir (Alembic loads *every* `.py` in `versions/`).
- **Delete** the ~22 per-migration test files under
  `tests/integration/test_migrations/test_*.py` (and the 2 stragglers in
  `tests/shared/core/`) — each `import`s a specific `alembic.versions.NNN_*`
  module and asserts `revision == "NNN"`; they break on deletion. Their durable
  value is preserved by the new `test_baseline_schema.py` (above).
- `alembic.ini` / `env.py`: **unchanged.**
- App code: **unchanged** — ORM models are already the schema's source of truth.

## CI / test wiring

- New baseline invariant test runs on the 5433 DB via
  `MIGRATIONS_TEST_DATABASE_URL` — **export both DB URLs** so it passes in bucket
  isolation (the known 5432/5433 footgun).
- **Re-verify the 90% coverage gate** after deleting both the migration modules
  and their tests (expected net wash — confirm, don't assume).

## Rollback

`git revert` restores the 39-file chain intact (identical revision ids). Prod
(at `039`) and fresh installs are unaffected either way, because the reused `039`
id keeps prod a no-op in both directions.

## Out of scope

- The 3 post-deploy cloud-burst bugs (orphan ledger / xenolab endpoint / empty
  analysis) — separate hotfix track.
- Any schema change. This is a pure migration-ledger collapse; the resulting
  schema is byte-identical to the `039` chain output.
