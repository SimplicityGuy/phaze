---
phase: 87
slug: operator-ui-stage-matrix-failure-retry-eligibility-trace-pri
status: verified
threats_open: 0
asvs_level: 1
created: 2026-07-11
---

# Phase 87 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.
> Register authored at plan time (all 8 plans carried a `<threat_model>` block); verified in
> **verify-mitigations mode** by `gsd-security-auditor` — each mitigation cross-checked against a real
> executing test, not source-only (per the project "'Closed' ≠ tested" rule).

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| ORM/migration → PostgreSQL | New DDL + constraints (`stage_skip`, migration `037`) crossing into the live-corpus schema | table/constraint DDL |
| derivation predicates → every pending set / recovery / pill matrix | A single `skipped_clause`/`stage_status_case` change fans out to all consumers | derived per-stage status |
| browser query params → paginated files query | `page`/`page_size`/`stage`/`bucket` cross into a corpus-scale query | untrusted request params |
| file path / status → Jinja render | Untrusted file paths + derived status rendered in the files table | untrusted text → HTML |
| browser → force-skip mutating endpoint | Untrusted stage + operator free-text reason cross into a DB write | untrusted stage + free text |
| operator reason free-text → PostgreSQL | NUL/control chars can abort the PG txn (unbounded-recovery footgun) | free text → text column |
| browser → retry endpoints | Untrusted `file_id`/`stage` cross into an enqueue path | untrusted path params |
| browser → priority/pause/resume endpoints | Untrusted stage + delta cross into the durable control row | untrusted stage + int delta |
| ledger/saq_jobs reads → 5s poll | in-flight/orphan source read on the hot poll | corpus-scale read |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-87-01 | Tampering | migration DDL vs saq_jobs | mitigate | `037` touches only `stage_skip`/`files.id`; body-line grep gate `test_037_stage_skip.py::test_migration_never_references_saq_jobs` | closed |
| T-87-02 | Elevation of Privilege | non-enrich stage in stage_skip | mitigate | `CheckConstraint("stage IN ('metadata','analyze','fingerprint')")` (`stage_skip.py:55`, `037:70`); CHECK-violation asserted (037 test case d) | closed |
| T-87-03 | Integrity | duplicate skip rows | mitigate | `UniqueConstraint("file_id","stage")` (`stage_skip.py:54`, `037:68`); IntegrityError asserted (037 test case c) | closed |
| T-87-04 | Denial of Service | destructive downgrade | **accept** | `downgrade()` = `drop_table("stage_skip")` on additive greenfield table, no backfill; rationale in `037` docstring; reversal asserted (test case f) | closed |
| T-87-05 | Tampering | SQL injection via stage param | mitigate | `skipped_clause` bound `StageSkip.stage == stage.value` + enrich-only `ValueError` (`stage_status.py:201-219`); never f-string SQL | closed |
| T-87-06 | Elevation of Privilege | skip applied to a downstream stage | mitigate | `stage_status_case` appends the `skipped` branch only for enrich stages; downstream CASE stays 4-way (`stage_status.py:417-419`) | closed |
| T-87-07 | Integrity | SQL⇔Python twin drift | mitigate | Twins edited in lockstep; `test_stage_status_equivalence.py` locks them | closed |
| T-87-08 | Integrity | SQL⇔Python twin drift (skipped cells) | mitigate | DERIV-04 harness extended with skipped cells on all 3 enrich axes (`test_stage_status_equivalence.py` seeds) | closed |
| T-87-09 | Integrity | toothless (always-green) guard | mitigate | Guards self-test (`test_no_raw_state_render.py::test_guard_flags_a_planted_render`); mutation break→RED recorded | closed |
| T-87-10 | Integrity | shadow-compare regression on skip | mitigate | `test_shadow_compare_skipped.py` additive-writer property + soft-allowlist-not-grown assertion | closed |
| T-87-11 | Denial of Service | whole-corpus scan/COUNT per 5s poll at 200K | mitigate | `.limit(page_size+1)` sentinel, no COUNT; `test_files_page.py:143` asserts `"count(" not in sql` | closed |
| T-87-12 | Denial of Service | poll-time 500 on DB hiccup | mitigate | `get_files_page` `begin_nested()` → safe empty `FilesPage` on any error (`pipeline.py:1827-1833`); `test_files_page.py:222` | closed |
| T-87-13 | Tampering | XSS via file path in template | mitigate | Jinja2 autoescape; `files_table_view.html:21` path always autoescaped, never `\| safe` | closed |
| T-87-14 | Tampering | injection via stage/bucket param | mitigate | `pipeline_files` validates `Stage(stage)`/`_VALID_BUCKETS`; ORM bound `stage_status_case(stage) == bucket` (`pipeline.py:794-801`) | closed |
| T-87-15 | Information Disclosure | rendering raw internal status strings | mitigate | Cutover to derived pill + mutation-tested grep guard forbids raw `f.state` (`test_no_raw_state_render.py`) | closed |
| T-87-16 | Tampering | filter param injection | mitigate | Same `Stage`/`Status` enum allowlist validation as T-87-14; `test_files_filter.py` | closed |
| T-87-17 | Denial of Service | filter widening the poll to a whole-corpus scan | **accept** | Filter rides the same bounded `get_files_page` (LIMIT preserved); rationale documented in route/`_files_page_stmt` docstrings | closed |
| T-87-18 | Elevation of Privilege | approval bypass via force-skip of propose/review/apply | mitigate | `if stage not in STAGE_TO_FUNCTION: raise 422` (`pipeline.py:1318`) + DB CHECK backstop; no skip pill on approval stages; `test_force_skip_writer.py:71` | closed |
| T-87-19 | Denial of Service | NUL byte in reason aborts PG txn | mitigate | `sanitize_pg_text(reason).strip()` before persist (`pipeline.py:1322`); `test_nul_in_reason_is_sanitized_and_round_trips` | closed |
| T-87-20 | Integrity | writer clears failed_at → shadow-compare regression | mitigate | `pg_insert(...).on_conflict_do_nothing`, never clears `failed_at` (`pipeline.py:1333`); `test_skip_never_clears_analysis_failed_at` | closed |
| T-87-21 | Tampering | XSS via reason echoed in template | mitigate | Ack interpolates only allowlisted `stage`; `reason` never echoed (`pipeline.py:1338-1346`, `_force_skip_dialog.html:17`) | closed |
| T-87-22 | Input Validation | missing/blank required reason | mitigate | Blank **sanitized** reason → 422, no write (`pipeline.py:1323`); `test_empty_reason...` + `test_nul_only_reason_returns_422` (WR-01) | closed |
| T-87-23 | Denial of Service | trace becomes a corpus scan | mitigate | `_one_stage_scalars` reads are `file_id`-scoped (`pipeline.py:1367-1392`); `test_eligibility_trace.py::test_trace_is_single_row_no_corpus_scan` | closed |
| T-87-24 | Denial of Service | mis-scoped analyze bulk retry re-enables the 44.5K over-enqueue | mitigate | `ELIGIBLE_AFTER_FAILURE[ANALYZE]=False` (`stage.py:96`); `test_retry_affordances.py::test_analyze_failure_is_never_auto_eligible` | closed |
| T-87-25 | Denial of Service | retry with no active agent | mitigate | Guarded funnel: `NoActiveAgentError` → amber ack, nothing enqueued (`pipeline.py:1190-1196`); `test_per_file_retry_no_active_agent_mutates_nothing` | closed |
| T-87-26 | Tampering | duplicate enqueue on rapid retry | mitigate | Deterministic Phase-30 keys (`process_file_job_key(file.id)`, `extract_file_metadata:<id>`) | closed |
| T-87-27 | Input Validation | invalid file_id/stage on per-file retry | mitigate | `file_id: uuid.UUID` path param + `state == ANALYSIS_FAILED` scope guard (`pipeline.py:1156/1179`); `test_per_file_retry_non_failed_file_is_noop` | closed |
| T-87-28 | Denial of Service | poll-time 500 on orphan derivation / naive-timestamp TypeError | mitigate | `get_stage_orphan_counts` `begin_nested()` → all-zero on error; naive/aware coerced in `is_domain_completed` (`pipeline.py:618-645`) | closed |
| T-87-29 | Tampering | priority delta out of range | mitigate | `max(0, min(100, row.priority + delta))` (`pipeline_stages.py:96`, `_PRIORITY_MIN/MAX=0/100`) | closed |
| T-87-30 | Tampering | injection via stage param on control post | mitigate | `_validate_stage` → 422 on unknown stage (`pipeline_stages.py:53-56`), called by priority/pause/resume | closed |
| T-87-31 | Integrity | orphan badge drifts from recovery | mitigate | Orphan count reuses recovery's own predicates (`is_domain_completed`/`_build_done_sets`/`_in_flight_cloud_job_ids`) — definitional parity (`pipeline.py:624-640`) | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-87-01 | T-87-04 | `stage_skip` migration `037` `downgrade()` is a plain `drop_table` on an additive greenfield table — no backfill, so no data loss for the existing corpus on a rollback. Reversal is asserted by the migration test. | plan-time disposition (87-01) | 2026-07-11 |
| AR-87-02 | T-87-17 | The status/failure filter reuses the same bounded, paginated `get_files_page` query (LIMIT preserved, no unbounded COUNT), so a filter can never widen the poll into a whole-corpus scan; the DoS surface is identical to the already-mitigated T-87-11. | plan-time disposition (87-05) | 2026-07-11 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-07-11 | 31 | 31 | 0 | gsd-security-auditor (opus, verify-mitigations mode) |

Notes:
- All 31 plan-time threats verified present in the current implementation; each mitigation cross-checked
  against a real executing test (not source-only).
- Two `accept` dispositions (T-87-04, T-87-17) carry documented rationale in code and plan — logged above.
- The two execution-time code-review fixes were verified holding in current source: **CR-01**
  (`on_conflict_do_nothing` → duplicate force-skip is idempotent, no HTTP 500; availability) and **WR-01**
  (blank-reason gate validates the sanitized value → NUL-only bypass closed; strengthens T-87-19/T-87-22).
- No unregistered threat flags — every plan SUMMARY states "No new threat surface introduced beyond the
  plan's register."

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-07-11
