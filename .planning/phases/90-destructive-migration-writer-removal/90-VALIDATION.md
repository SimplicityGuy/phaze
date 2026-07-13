---
phase: 90
slug: destructive-migration-writer-removal
status: approved
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-12
audited: 2026-07-13
---

# Phase 90 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Source: `90-RESEARCH.md` §Validation Architecture. Task IDs are assigned by the planner; rows below are
> requirement-level until plans exist.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio (`uv run pytest`) |
| **Config file** | `pyproject.toml` + `tests/buckets.json` (per-bucket isolation; `tests/shared/test_partition_guard.py` enforces one bucket per file) |
| **Quick run command** | `uv run pytest tests/shared/services/test_pipeline.py -x` |
| **Migration run command** | `MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_migrations_test" just test-bucket integration` |
| **Full suite command** | `uv run pytest` |
| **Estimated runtime** | ~180 s full suite; ~5 s quick |

---

## Sampling Rate

- **After every task commit:** Run the quick run command (or the affected bucket).
- **After every plan wave:** Run `uv run pytest` (90% floor; re-run failed subset in isolation on colima flake — memory `reference_local_fullsuite_colima_flake`).
- **Before `/gsd:verify-work`:** Full suite must be green, including the `:5433` migration bucket.
- **Max feedback latency:** 180 seconds.

---

## Per-Task Verification Map

> Statuses reconstructed from executed reality (SUMMARYs 90-01…90-04 + 90-VERIFICATION.md) at the 2026-07-13 audit. Every plan-time Wave 0 dependency landed and re-ran green.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Delivered Test File(s) | Evidence | Status |
|---------|------|------|-------------|------------|-----------------|-----------|------------------------|----------|--------|
| 90-01-T1/T2 (PR-A) | readers | 1 | MIG-04 / D-09 | T-90A-01/03 | Each converted reader returns correct rows from derived sources (count cards, analyze workspace, proposal batches via `~done_clause(PROPOSE)`, backfill via `failed_clause`, search-facet removal) | integration | `tests/integration/test_stage_status_equivalence.py` + migrated `tests/shared/routers/test_pipeline.py`, `core/test_enrich_analyze_workspaces.py`, `core/test_pipeline_dag_context.py` | `test_stage_status_equivalence.py` 59 passed; buckets: shared 1129 · analyze 576 · review 431 · integration 274 (90-01-SUMMARY) | ✅ green |
| 90-01-T2 (PR-A) | readers | 1 | D-09 | — | `held_files` ledger-seed path stays correct after cutover (plan-time "likely uncovered" — now COVERED) | integration | `tests/shared/routers/test_pipeline.py::test_analyze_ui_no_agents_surfaces_held_count`, `::test_backfill_seeds_a_ledger_row_for_every_held_candidate`, `::test_analyze_long_held_even_without_fileserver`; `tests/analyze/tasks/test_recovery.py::test_held_file_with_process_file_seed_is_in_the_held_set` | 4+ held-path tests present & green (audit filesystem confirm) | ✅ green |
| 90-01-T3 (PR-A) | readers | 1 | D-05/D-06 | T-90A-04 | `undo_resolve` marker DELETE decoupled from `FileState`; id-only payload still deletes marker; stale-replay no-op preserved | integration | `tests/review/routers/test_duplicates.py` (id-only round-trips) | mutation-proven: reverting to `previous_state` gate turned `test_undo_roundtrip_id_only_payload_still_deletes_marker` RED, then restored (90-01-SUMMARY); re-ran 6 passed (VERIFICATION) | ✅ green |
| 90-02 (PR-B) | writers | 2 | MIG-04 | T-90B-01/02 | No writer of `state` survives; both CAS guards removed atomically; SQL⇔Python equivalence still green; idempotency preserved | integration | `tests/metadata/routers/test_agent_metadata.py::test_metadata_callback_idempotent_after_cas_removal`, `tests/agents/routers/test_agent_s3.py::test_s3_push_status_transition_idempotent_after_cas_removal` + 15 migrated files | grep proves zero `.values(state=)` writers in src; full suite 3443 passed (90-02-SUMMARY) | ✅ green |
| 90-03 (PR-C) | destructive | 3 | MIG-04 | T-90-guard / T-90-sqli / T-90-pii / T-90-frozen | `039` drops column+index; guard aborts on shadow-violation / mid-flight rows; runs clean on empty DB; `files_state_archive` populated; parameterized SQL; no `phaze.*` imports | integration (migration) | `tests/integration/test_migrations/test_migration_039_drop_files_state_column.py` (462 lines, 8 fns) | 14 passed on `:5433` (re-run in VERIFICATION); static f-string + no-import asserts green | ✅ green |
| 90-03 (PR-C) | destructive | 3 | MIG-04 / D-10 | T-90-loss | `downgrade()` restores column+index verbatim from `files_state_archive`; derived CASE fallback for post-039 rows | integration | same file (round-trip cases) | covered within 14 passed | ✅ green |
| 90-04 (PR-C) | destructive | 3 | MIG-04 / D-08 | T-90-02 | Anti-drift: `FileState` / `FileRecord.state` / `files.state` / multi-line `.values(state=)` cannot reappear in `src/` (mutation-tested; tokenize-strips comments+strings) | unit (source-scan) | `tests/shared/test_no_filestate_guard.py` (3 fns incl. planted-match self-test) | 3 passed; manual RED→GREEN mutation run VERBATIM (90-04-SUMMARY); independently re-mutated GREEN→RED→GREEN by verifier (VERIFICATION truth #8) | ✅ green |
| 90-04 (PR-C) | destructive | 3 | MIG-04 / D-04 | T-90-04 | Model↔DB drift sentinel: `models/file.py` column/index/enum removal exactly matches 039's drops | integration | `test_039_autogenerate_diff_is_empty_for_dropped_objects` | flipped RED→GREEN when model surface deleted; re-ran GREEN (VERIFICATION) | ✅ green |
| 90-04 (PR-C) | destructive | 3 | MIG-04 / T-90-01 | T-90-01 | Migrated (not deleted) coverage survives: ledger exactly-once + service-level dedup undo | integration | `tests/integration/test_drain_double_dispatch.py` (3 passed), `tests/integration/test_dedup_resolve_undo_shadow.py` (9 passed) | both re-ran green (VERIFICATION); combined coverage 97.33% ≥ 95%, all modules ≥ 90% | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements — COMPLETE (2026-07-13)

- [x] `tests/integration/test_migrations/test_migration_039_drop_files_state_column.py` — upgrade (guard: shadow-violation→raise, empty DB→pass, mid-flight rows→raise), `files_state_archive` populated, index+column gone, `FileState` deleted; downgrade restores durable states from archive. **14 passed on `:5433`.**
- [x] `tests/shared/test_no_filestate_guard.py` — mutation-tested tokenize-based source-scan (D-08); RED→GREEN mutation run recorded VERBATIM (90-04-SUMMARY) and independently re-mutated by the verifier. **3 passed.**
- [x] Extended `tests/shared/routers/test_pipeline.py` + analyze/metadata/shared bucket tests for each reader cutover; **`held_files` ledger-seed path now COVERED** (`test_analyze_ui_no_agents_surfaces_held_count`, `test_backfill_seeds_a_ledger_row_for_every_held_candidate`, et al.) — the plan-time "likely uncovered" worry is resolved.
- [x] `get_files_by_state` deleted with the reader cutover; search `file_state`-facet removed (D-11), `grep -c file_state` == 0 in `search_queries.py`/`routers/search.py`.
- [x] `MIGRATIONS_TEST_DATABASE_URL` at `:5433` exercised for the migration bucket (memory `reference_migrations_test_db_port`).

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Migration rehearsal against a restore of the real corpus passes (ROADMAP success-criterion 3) | MIG-04 | Requires a restore of live prod data (~11,428 rows @ Alembic 031); cannot run in CI | Restore prod snapshot → apply 032–038 → run shadow-compare green (drained) → run `039` → assert column/index gone + `files_state_archive` row count matches → `downgrade()` → assert durable states restored. Recipe in `90-RESEARCH.md` Pattern 5. |
| Dashboard cards + analyze workspace render correctly on live traffic after drain lifts (D-12 cloud cards, reader cutovers) | MIG-04 | Browser UAT — no test runs JS/templates (memory `project_htmx_hxon_alpine_scope_trap`) | Load pipeline dashboard post-deploy; confirm Staged(pushing)/Analyzing(cloud) counts, analyze workspace states, failed-count card, search (facet removed). |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies (confirmed by plan-checker Dimension 8, iteration 1)
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (039 migration test, anti-drift guard, held_files coverage)
- [x] No watch-mode flags
- [x] Feedback latency < 180s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-07-12

---

## Validation Audit 2026-07-13

Re-audited the plan-time contract against executed reality (SUMMARYs 90-01…90-04 + 90-VERIFICATION.md + filesystem confirmation of every named test file). Gap analysis found **zero MISSING or PARTIAL** requirements — every Wave 0 dependency landed and re-ran green, so no `gsd-nyquist-auditor` spawn was required.

| Metric | Count |
|--------|-------|
| Requirements audited | 9 |
| COVERED (✅ green) | 9 |
| PARTIAL | 0 |
| MISSING | 0 |
| Gaps found | 0 |
| Resolved | 0 (none to fill) |
| Escalated | 0 |

**Notes:**
- The one plan-time coverage worry — `held_files` ledger-seed path "likely uncovered today" — is resolved: 4+ held-path tests exist and pass.
- Coverage gates confirmed wired: per-module floor 90% (`scripts/coverage_floor.py:33`) + combined `--fail-under=95` (`justfile:134`); combined reported 97.33% at phase close.
- The `shadow_compare`/`FileState` string hits remaining in `src/phaze` are **prose-only** docstring cross-references (`backends.py:97`, `stage_status.py:101`, etc.), tokenize-stripped by the D-08 guard — not executable references.
- Manual-Only items (migration 039 real-corpus rehearsal + post-deploy browser UAT) remain correctly manual — physically un-automatable in CI, tracked in `90-HUMAN-UAT.md`. They are pre-deploy gates, not validation gaps.
