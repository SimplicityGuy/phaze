---
phase: 80
slug: recovery-re-enqueue-cutover
status: verified
threats_open: 0
asvs_level: 1
created: 2026-07-10
---

# Phase 80 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

**Scope note:** Phase 80 is control-plane code only — an Alembic data migration, internal SQLAlchemy clause composition, a reconcile cron task, the recovery producer, and hermetic test/guard code. There is **no client-facing input surface**, so the Spoofing / injection / web STRIDE limbs are Not Applicable across every plan. Zero dependency changes were made (milestone-wide constraint), so the supply-chain checkpoint (T-80-SC) is N/A. Register authored at plan time; auditor verified mitigations against post-fix source on `SimplicityGuy/phase-80` (fix commit `5da4036e`), read-only.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| Alembic migration → live 200K corpus | A single `UPDATE` runs over the whole `analysis` table on first deploy (prod at `031`, sweeps `032→036`). Control-plane DML, not a request surface. | Internal timestamps (`analysis_completed_at`, `updated_at`) |
| service predicate layer → recovery/drain/count consumers | The `stage_status` builders govern which files are treated as drain/route/recovery candidates. Internal SQL-clause composition consumed by control-plane code. | Internal file/stage state (derived) |
| reconcile cron → cloud_job sidecar + files | The reconcile unit runs under a per-row `pg_advisory_xact_lock(5_000_504)`; the at-cap path mutates the sidecar. Reconciles kueue Job outcomes; no untrusted input. | Cloud-job status, staged S3 objects |
| recovery task → scheduling ledger + predicate layer + saq_jobs | `recover_orphaned_work` decides which ledger rows to replay onto agent queues; invoked by the controller startup hook and the manual "Recover" button (`force=True`). Control-plane orchestration over internal state. | Ledger rows, derived done/in-flight sets |
| CI source guard → the two cutover modules | Static AST gate; the threat it defends against is a future edit reintroducing a `FileRecord.state` read. | No runtime surface (source text only) |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-80-01 | Tampering (data-integrity) | `036` UPDATE aborting mid-migration | mitigate | `AND a.failed_at IS NULL` NAND guard keeps the UPDATE inside the `analysis` NAND CheckConstraint (`036_backfill_analysis_completed_at.py:73`); migration test asserts an `analysis_failed` control row is untouched (`test_migration_036…py:223`) | closed |
| T-80-02 | Denial of Service (over-enqueue) | 1001 prod rows staying NULL after cutover | mitigate | `036` (`down_revision='035'`) is a blocking prerequisite of 80-04, landing atomically; the UPDATE stamps every `state='analyzed'` NULL row so `done_clause(ANALYZE)` reads them done and recovery does not re-enqueue 4h jobs | closed |
| T-80-03 | Tampering | destructive-migration renumber corrupting historical record | accept/mitigate | Prose-only edits; historical Phase-83 `034` literal left intact; `just docs-drift` guards checkbox / requirement-ID / traceability-status integrity | closed |
| T-80-04 | Tampering (routing regression) | a locally-analyzing file mis-routed to a compute agent | mitigate | **Mechanism revised by CR-01 fix** (see Audit Notes): the `r.key not in live` orphan filter (`reenqueue.py:490`) + the recovery-specific `_get_awaiting_cloud_ids` predicate (no `~inflight_clause`) + mutation-proven regressions `test_held_process_file_orphan_is_not_analyzed_locally_on_a_fileserver` / `…_routes_to_a_compute_agent` | closed |
| T-80-05 | Tampering (latent over-enqueue trap) | a future edit adding `~inflight_clause` to `domain_completed_clause` | mitigate | D-11 prohibition docstring (`stage_status.py:214-220`); locked by the 80-04 recovery regression and the equivalence SCOPE comment | closed |
| T-80-06 | Elevation/Denial (import cycle) | adding `CloudJob` import to `stage_status.py` | accept | `models/cloud_job.py` imports only SQLAlchemy + `models.base`; no cycle (live import smoke-check passes) | closed |
| T-80-07 | Tampering (lost cloud-routing update) | autoflush racing the spill CAS | mitigate | `cloud_job.status = FAILED` pre-mutation removed; the rowcount-guarded `hold_awaiting_cloud` CAS owns the status write with `WHERE status IN (SUBMITTED,RUNNING)` (`reconcile_cloud_jobs.py:225-239`) | closed |
| T-80-08 | Repudiation/Integrity (shadow-gate divergence) | `state=AWAITING_CLOUD` + `cloud_job.status=FAILED` HARD shadow violation on main | mitigate | `FileRecord.state` write retired (D-04); spilled kueue file stays at `PUSHED`; regression `test_at_cap_spill_restamps_cloud_job_awaiting_not_failed` + AST guard | closed |
| T-80-09 | Tampering (double-count / S3 leak) | attempts double-increment or staged-object leak | mitigate | MKUE-04 preserved: `attempts=cap` not incremented at-cap (`:236`); `delete_staged_object` under the held lock before commit (`:213`); `delete_job` post-commit (`:243`) | closed |
| T-80-10 | Denial of Service (mass re-enqueue) | done-set derivation classifying a domain-complete file as recoverable | mitigate | Done derived via LOCKED `done_clause`/`domain_completed_clause` (`reenqueue.py:241-260`); SC-2/SC-3 regressions; `036` backfill covers the corpus | closed |
| T-80-11 | Denial of Service (asyncpg crash at scale) | bare `.in_(fids)` exceeding the 32767-param cap | mitigate | Single Postgres `= ANY(array)` bind everywhere (`_fids_scope` `:210`, metadata `:251`); zero bare `.in_(fids)` on the id list | closed |
| T-80-12 | Tampering (false-terminal / false-redrive) | the metadata `in_flight ∧ failed` cell | mitigate | **Reinforced by CR-02 fix**: D-10 `enqueued_at <= failed_at` gate with naive→UTC-aware coercion (`reenqueue.py:404`); DB-round-trip regression `test_d10_gate_does_not_crash_on_db_read_ledger_row` + cells A/B | closed |
| T-80-13 | Elevation (import-boundary breach) | `reenqueue` importing a non-control-safe module | mitigate | Control-only banner; `stage_status.py` imports only models+enums+`tasks._shared.stage_control`; `tests/shared/core/test_task_split.py` asserts the agent worker is Postgres-free | closed |
| T-80-14 | Tampering (silent drift) | a future `FileRecord.state` read creeping into recovery/reconcile | mitigate | Clean-absence AST guard over both files, walking `Call.args`+`Call.keywords`, mutation-proven against forms #1–#6 with GREEN false-positive checks (`test_reenqueue_reconcile_source_scan.py:225-357`) | closed |
| T-80-15 | Tampering (over-enqueue trap past CI) | `~inflight_clause` added to `domain_completed_clause` staying green in the equivalence test | accept (documented) + mitigate | Equivalence test is a documented silent no-op by design (`test_stage_status_equivalence.py:421-442`); the real lock is the 80-04 recovery regression (Cell B goes RED) | closed |
| T-80-SC | Tampering (supply chain) | package installs | N/A | Zero dependency changes across all plans; no npm/pip/cargo install occurs | closed (N/A) |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-80-01 | T-80-06 | `CloudJob` import into `stage_status.py` introduces no cycle (`models/cloud_job.py` imports only SQLAlchemy + `models.base`; module already imports nine models); accepted rather than mitigated. | Phase 80 plan (80-02) | 2026-07-10 |
| AR-80-02 | T-80-15 | The DERIV-04 equivalence test is a deliberate silent no-op for the `~inflight_clause` trap (documented at `test_stage_status_equivalence.py:421-442`); the enforcing lock is the recovery-layer regression in 80-04, not this test. | Phase 80 plan (80-05) | 2026-07-10 |

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-07-10 | 16 (15 + 1 N/A) | 15 | 0 | gsd-security-auditor (opus) |

**Notable audit findings**

- **T-80-04 (CR-01 re-verification):** The register's original mitigation wording ("one named source; card/drain/recovery cannot diverge") is intentionally no longer literally true. Recovery deliberately diverges via its own `_get_awaiting_cloud_ids` predicate (drops `~inflight_clause`) because the D-09 held-file seed (`routers/pipeline.py:861-867`) makes every held compute file inflight-by-construction — reusing `awaiting_candidate_clause` had made `held_agent_rows` provably empty (a CLOUDROUTE-02 routing regression, fixed post-execution). The **threat** stays CLOSED via the `r.key not in live` orphan filter + the recovery-specific predicate + two mutation-proven regressions.
- **T-80-12 (CR-02 re-verification):** The naive ledger `enqueued_at` (TIMESTAMP WITHOUT TIME ZONE, migration 022) is now coerced to UTC-aware before comparison to the aware `metadata.failed_at`, preventing the `TypeError` that previously aborted the entire recovery run. Exercised by a DB-round-trip regression (in-memory unit rows are already aware and would miss it).

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-07-10
