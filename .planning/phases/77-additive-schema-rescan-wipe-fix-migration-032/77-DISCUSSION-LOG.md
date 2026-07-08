# Phase 77: Additive Schema & Rescan-Wipe Fix (migration `032`) - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-07
**Phase:** 77-additive-schema-rescan-wipe-fix-migration-032
**Areas discussed:** Failure-marker shape, Cloud-routing sidecar, Dedup marker shape, Rescan-wipe fix + backfill/downgrade

---

## Failure-marker shape (design D-02)

| Option | Description | Selected |
|--------|-------------|----------|
| Columns on existing tables | Nullable `failed_at` + `error_message` on `analysis`/`metadata`; no new FK; tightens `done(metadata)` to `failed_at IS NULL` | ✓ |
| Generic `stage_failure` table | One table for all stages; keeps `done()` as pure row-existence but adds a table + FK + second write path | |

**User's choice:** Columns on existing tables (design rec)
**Notes:** → CONTEXT D-01, D-02.

### Failure-marker backfill asymmetry

| Option | Description | Selected |
|--------|-------------|----------|
| Analyze backfills, metadata doesn't | `analysis.failed_at` set from `state=ANALYSIS_FAILED` w/ placeholder message; metadata gets no backfill (no historical source) | ✓ |
| Neither backfills | Only create columns; legacy `ANALYSIS_FAILED` files re-derive as not_started (re-opens terminal-analyze risk) | |

**User's choice:** Analyze backfills, metadata doesn't (recommended)
**Notes:** → CONTEXT D-03. Document the metadata no-source asymmetry in the migration docstring + VERIFICATION.

---

## Cloud-routing sidecar (design D-03)

| Option | Description | Selected |
|--------|-------------|----------|
| Add `awaiting` to `cloud_job.status` | Extend `CloudJobStatus` StrEnum + CHECK; awaiting file = `cloud_job` row, `s3_key`/`upload_id` NULL; reuses existing sidecar | ✓ |
| Dedicated `analyze_route` table | New table separating routing decision from S3 lifecycle; adds table + FK + second source to union later | |

**User's choice:** Add 'awaiting' to cloud_job.status (design rec)
**Notes:** → CONTEXT D-04.

### LOCAL_ANALYZING + PUSHING/PUSHED backfill

| Option | Description | Selected |
|--------|-------------|----------|
| LOCAL_ANALYZING derived / no row; reconcile PUSHING/PUSHED | No sidecar for LOCAL_ANALYZING (= `in_flight(analyze)`); ensure `cloud_job` row exists for PUSHING/PUSHED, fill only gaps | ✓ |
| Create explicit rows for both | Materialize a row for LOCAL_ANALYZING too; redundant write path contradicting derive-don't-store | |

**User's choice:** LOCAL_ANALYZING derived / no row; reconcile PUSHING/PUSHED (rec)
**Notes:** → CONTEXT D-05, D-06.

---

## Dedup marker shape (SIDECAR-02)

| Option | Description | Selected |
|--------|-------------|----------|
| `file_id + canonical_file_id + resolved_at` | Records the duplicate→canonical link; undo = DELETE row; backfill derives canonical from sha256 group | ✓ |
| `file_id + resolved_at` only | Minimal; canonical stays implicit; loses the "duplicate of X" affordance | |

**User's choice:** file_id + canonical_file_id + resolved_at (recommended)
**Notes:** → CONTEXT D-07. `previous_state` from the enum-era undo is a transition artifact, not needed under derivation.

---

## Rescan-wipe fix (MIG-03)

| Option | Description | Selected |
|--------|-------------|----------|
| Standalone early task, remove state from both sites | Drop `"state": excluded.state` from ON CONFLICT set in `ingestion.py` + `agent_files.py`; regression test asserts ANALYZED survives rescan | ✓ |
| Conditional preserve (COALESCE/CASE) | Guard state overwrite by "more advanced" ordering; encodes a state ordering the milestone is deleting | |

**User's choice:** Standalone early task, remove state from both sites (rec)
**Notes:** → CONTEXT D-08.

### 032 downgrade completeness

| Option | Description | Selected |
|--------|-------------|----------|
| Full reversal incl. awaiting-row cleanup | Order-sensitive: delete `awaiting` rows before restoring CHECK; integration test asserts clean pre-032 schema | |
| Schema-only reversal, leave data | Reverse DDL only; still needs awaiting cleanup to avoid CHECK-restore failure | |
| **User override (free text)** | "don't worry about downgrades. do the simplest thing here. we're focused on forward looking upgrade paths only." | ✓ |

**User's choice:** (free text) Minimal downgrade — forward upgrade path is the focus.
**Notes:** → CONTEXT D-09. Explicitly relaxes ROADMAP success criterion #4; best-effort DDL downgrade is acceptable.

---

## Claude's Discretion

- Backfill batching: set-based `INSERT … SELECT` / `UPDATE … FROM` (house style); chunk only if 200K proves problematic.
- `error_message` column type: `Text` (unbounded).
- Exact partial-index set (PERF-01), `IS NOT NULL`-shaped, mirrored into ORM `__table_args__`.
- Index build lock behavior (`CREATE INDEX CONCURRENTLY` vs plain) on the live 200K table.

## Deferred Ideas

None net-new. Later-phase milestone scope (not deferrals): `stage_status()` derivation + pending-query rewrites (READ-*); shadow-compare gate (MIG-02); destructive `033` (MIG-04); the §4.1 latent-bug guard fixes.
