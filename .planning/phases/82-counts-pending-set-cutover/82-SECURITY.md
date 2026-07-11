---
phase: 82
slug: counts-pending-set-cutover
status: verified
threats_open: 0
asvs_level: 1
created: 2026-07-10
---

# Phase 82 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| recovery/manual trigger → SAQ enqueue | The three enrich pending helpers define what gets enqueued for metadata/fingerprint/analyze; an over-broad set drives cost / over-enqueue. | file_ids (internal), enqueue volume |
| cloud dispatch ↔ local analyze pending set | A file dispatched to a compute agent must not simultaneously be a local analyze candidate (double-dispatch). | file_id, cloud_job status (internal) |
| 5s `/pipeline/stats` poll → DB | The hot operator-console poll fans out counting reads; a query that errors or full-scans degrades the console. | aggregate counts (internal, no PII) |
| seed script → test DB | The perf-corpus seeder writes bulk synthetic rows to a local ephemeral DB; must never touch prod and must use parameterized inserts. | synthetic file rows (test-only) |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-82-01 | Tampering | `eligible_clause` SQL construction (`stage_status.py`) | mitigate | Composed only from LOCKED `ColumnElement` builders (`not_(inflight_clause)` ∧ `not_(done_clause)` ∧ `not_(failed_clause)`) via `and_(*conjuncts)`; failed carve-out table-driven off `ELIGIBLE_AFTER_FAILURE`; no f-string/interpolated SQL (T-42-03). Verified `stage_status.py:285-327`. | closed |
| T-82-02 | Denial of Service (over-enqueue) | analyze eligibility | mitigate | `not_(failed_clause(ANALYZE))` conjunct keeps a failed analyze terminal (ELIG-03); anti-drift cell `(ANALYZE, seed_analysis_failed, False)` + mutation check guard the 44.5K over-enqueue class. Verified `stage_status.py:325-326`, `test_stage_status_equivalence.py:518`. | closed |
| T-82-A1 | Denial of Service (double-dispatch / cost) | `get_discovered_files_with_duration` vs `cloud_job` (`pipeline.py`) | mitigate | Explicit `~exists(cloud_job WHERE status ∈ _ACTIVE_CLOUD_STATUSES)` conjunct on the analyze set (all non-FAILED statuses); PUSHING/PUSHED-absent regression parametrized over every active status. Verified `pipeline.py:60-66,1179`, `test_enrich_pending_independence.py:244-251`. | closed |
| T-82-OE | Denial of Service (44.5K over-enqueue) | analyze eligibility | mitigate | `eligible_clause(ANALYZE)` keeps a failed analyze terminal; the analyze set never auto-re-enqueues a failed row. Same evidence as T-82-02. | closed |
| T-82-03 | Tampering | pending-set SQL (`pipeline.py`) | mitigate | Pure `ColumnElement` composition + bound params in all three helpers, no f-string SQL; AST source scan forbids any `FileState` read in the helper bodies (positional/keyword/`**`-splat covered). Verified `pipeline.py:1172-1180,1452-1456,1494-1498`, `test_pending_set_source_scan.py:70-104`. | closed |
| T-82-DRIFT | Repudiation (silent reader drift) | 3 pending helpers | mitigate | Mutation-tested AST source scan (crafted-string RED cases) + behavioral divergence guard with per-cell `MUTATION:` comments; green guards re-proven RED-on-break/GREEN-on-restore. Verified `test_pending_set_source_scan.py:129-148`, `test_pending_set_divergence.py:136-185`. | closed |
| T-82-04 | Denial of Service (5s poll) | `_safe_bucket_counts` / `get_stage_progress` | mitigate | Every read wrapped in the `_safe_count` degrade discipline (try → `logger.warning` → guarded rollback → all-zero dict); never 500s the poll (INFLIGHT-02). Verified `pipeline.py:303-359`. | closed |
| T-82-05 | Tampering | four-bucket `GROUP BY` SQL | mitigate | Reuses the LOCKED `stage_status_case` `ColumnElement` (materialized inner subquery); no fresh CASE, no f-string SQL; `group_by(FileRecord.state)` absent from `src/`. Verified `pipeline.py:347`. | closed |
| T-82-06 | Repudiation (silent count drift) | `stats_bar` OOB store keys | mitigate | Alpine store keys kept stable (`$store.pipeline.discovered/.metadataExtracted/.analyzed`) via `_derive_stats`; poll-partial test asserts the three OOB ids still emit. Verified `stats_bar.html:55-57`, `routers/pipeline.py:137`, `test_pipeline_stats.py:137-141`. | closed |
| T-82-07 | Tampering | `seed_perf_corpus.py` bulk inserts | mitigate | All VALUES bound as `unnest` arrays via `conn.execute(stmt, *arrays)`; the one f-string interpolates only hard-coded table/column identifiers (no data value); default DSN is the local perf DB (`localhost:5433/phaze_perf82`). Verified `seed_perf_corpus.py:61,150,155`. | closed |
| T-82-08 | Denial of Service (accidental prod probe) | PERF-02 measurement | mitigate | Measurement runs ONLY on the local synthetic corpus at HEAD (≥036); live prod is a read-only COUNT sanity-check, never the EXPLAIN target; destructive `--reseed` TRUNCATE hard-gated. Verified `seed_perf_corpus.py:167-168`, `82-VERIFICATION.md` PERF-02. | closed |
| T-82-DDL | Tampering | schema | accept | No Alembic revision / DDL authored this phase (pure reader/count cutover, D-02); `git diff --name-only main...HEAD -- alembic/` returns nothing. Nothing to migrate. | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-82-01 | T-82-DDL | Pure reader/count cutover introduces no schema change; no Alembic revision authored (verified no `alembic/` diff). Nothing to mitigate. | Phase 82 plan (D-02) | 2026-07-10 |

*Accepted risks do not resurface in future audit runs.*

---

## Hardening Follow-Ups (non-blocking)

| ID | Threat Ref | Finding | Recommendation |
|----|------------|---------|----------------|
| WR-02 | T-82-07 / T-82-08 | The `--reseed` destructive TRUNCATE guard uses a loose `"perf" in db_name` substring check (`seed_perf_corpus.py:167`) rather than an anchored suffix. Auditor judged it does NOT weaken the prod-safety mitigation — the known prod DB `phaze` and shared `phaze_test` both fail the substring test, and TRUNCATE additionally requires the explicit `--reseed` opt-in. Residual risk is only a hypothetical DB whose name coincidentally contains `perf` (none exist in this project). | Anchor the guard to a `*_perf`/`_perf`/`*_test` token match to close the substring loophole. Tracked, WARNING-level, not an open threat. |

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-07-10 | 11 | 11 | 0 | gsd-security-auditor (verify-mitigations mode; register authored at plan time) |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-07-10
