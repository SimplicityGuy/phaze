# Phase 102 — Alembic Migration-Chain Flatten — VERIFICATION

**Date:** 2026-07-16
**Beads:** epic phaze-8hfu (phaze-8hfu.1 baseline, phaze-8hfu.2 fidelity, phaze-8hfu.3 retest, phaze-8hfu.4 close-out)
**Design:** `docs/superpowers/specs/2026-07-14-alembic-baseline-flatten-design.md`

## Fidelity merge gate (MIG-02) — all checks PASSED

Environment: Postgres 18.4 (`phaze-test-db`, port 5433). `DB_old` = `phaze_flatten_old`,
provisioned by the pre-flatten 001–039 chain (`alembic upgrade 037` → seed one non-revoked
fileserver agent, required by 038's D-01 abort guard → `alembic upgrade head`).
`DB_new` = `phaze_flatten_new`, provisioned by `039_baseline_schema.py` **alone** via a
temporary Alembic `script_location` containing only the baseline.

### 1. Schema-equivalence diff — EMPTY

`pg_dump --schema-only` both DBs → `scripts/normalize_schema_dump.py` (strips session
`SET`s, ownership, comments, the psql `\restrict` guard, and the `alembic_version`
bookkeeping table) → `diff` is **empty**.

SHA-256 of the normalized dumps (identical, including after the round-trip re-upgrade):

```
92d457a73bb0041ca36327361a202b4ca184ac3d168e293260805a5765fbe211  old_schema_normalized.sql  (chain-built)
92d457a73bb0041ca36327361a202b4ca184ac3d168e293260805a5765fbe211  new_schema_normalized.sql  (baseline-built)
92d457a73bb0041ca36327361a202b4ca184ac3d168e293260805a5765fbe211  roundtrip_schema_normalized.sql  (after downgrade base → upgrade head)
```

Authoring note: pg_dump's rendering of the four varchar-enum CHECK constraints
(`ck_agents_kind_enum`, `ck_cloud_job_status_enum`, `ck_cloud_job_cloud_phase_enum`,
`ck_stage_skip_enrich_only`) is not a parse fixed point — re-executing the dumped
`(ARRAY[...])::text[]` form stores a per-element-cast variant that dumps differently. The
baseline therefore embeds the ORIGINAL migrations' `IN (...)` expressions (024/025/027/037),
whose parse is exactly what produced the chain DB's stored form. With that, the diff is empty.

### 2. Seed rows — IDENTICAL

`pipeline_stage_control` (3 rows: metadata/analyze/fingerprint, `paused=f`, `priority=50`)
and `route_control` (`global`, `force_local=f`) match exactly on all stable columns.
`created_at`/`updated_at` are migration-time `NOW()` on both paths (inherently
run-specific, identical semantics).

### 3. `--autogenerate` drift — IDENTICAL (42 pre-existing entries, 0 new)

`alembic.autogenerate.compare_metadata` (compare_type=True, compare_server_default=False)
against `DB_old` and `DB_new` returns **the same 42 diffs** (repr-compared with memory
addresses normalized). These 42 are PRE-EXISTING, known ORM↔schema gaps produced by the
chain itself — `files_state_archive` (039's archive table, deliberately ORM-less),
generated `search_vector` tsvector columns, trgm/partial/functional indexes, and
timestamp-typing nuances — NOT flatten regressions. The design's "empty autogenerate
diff" assumption did not hold for the pre-flatten chain either; the durable invariant is
**zero NEW drift**, enforced going forward by `test_baseline_schema.py`'s frozen
known-drift check.

### 4. Upgrade-from-empty + `downgrade base` round-trip — CLEAN

- Baseline alone on an empty DB: `upgrade head` → `alembic_version = 039`, seeds present.
- `downgrade base` → zero tables in `public` (only `alembic_version`, Alembic-managed),
  `pg_trgm` extension dropped.
- Re-`upgrade head` → normalized dump byte-identical to `DB_old` (checksum above).

## Prod no-op precondition (MIG-02 / SC-3)

Prod (`lux.lan`) was at `alembic_version = '039'` as of the 2026-07-14 deploy. **Re-confirm
via the read-only PG probe (`ssh datum@lux.lan`, `BEGIN TRANSACTION READ ONLY`) immediately
before the PR merges; if not `039`, the merge holds.** Result recorded on bead phaze-8hfu.4
at merge time.
