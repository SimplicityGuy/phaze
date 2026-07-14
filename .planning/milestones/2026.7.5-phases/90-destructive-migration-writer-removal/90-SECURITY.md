---
phase: 90
slug: destructive-migration-writer-removal
status: verified
threats_open: 0
asvs_level: 1
created: 2026-07-13
---

# Phase 90 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.
> Final phase of milestone 2026.7.5 — retire the linear `files.state` column and derive
> per-file per-stage status from output tables. Shipped across 4 plans (90-01 readers,
> 90-02 writers, 90-03 migration 039, 90-04 enum/`shadow_compare` retirement + test migration).

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| client → `/pipeline/stats` & `/search` | untrusted query params cross into derived-clause reads | query params (non-sensitive) |
| browser → `/duplicates/undo(-all)` | attacker/stale-tab-controllable `file_states` payload crosses into the marker DELETE | server-rendered id-set payload |
| agent → `/api/internal/agent/*` & control-plane routing | writers formerly stamped `files.state` on these paths | job status transitions |
| migration SQL → live Postgres catalog | destructive DDL + guard/archive SQL execute against the live corpus | full `files` corpus (single admin) |
| operator `-x` args → migration | optional force-skip escape hatch crosses into the guard | operator intent |
| Model schema ↔ shipped migration 039 | ORM (`models/file.py`) must converge to the already-dropped DB shape | schema drift signal |
| Retired tests ↔ covered source | deleting the sole exerciser of live source silently drops coverage | test coverage |
| Future edits ↔ deleted `FileState` | a later change could reintroduce a `files.state` read/write | anti-drift guard |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-90A-01 | Tampering | reader cutovers compose `pipeline.py` clause builders | mitigate | Builders composed VERBATIM; drift-locked by `test_stage_status_equivalence.py` (5 test fns, green) | closed |
| T-90A-02 | Info disclosure | dropping the search `file_state` facet | accept | Facet removed with no UI consumer, single admin user; no data-exposure change (see Accepted Risks) | closed |
| T-90A-03 | Tampering | naive delete could re-propose already-proposed files | mitigate | `~done_clause(Stage.PROPOSE)` at `pipeline.py:1747` in `get_proposal_pending_batches` | closed |
| T-90A-04 | Tampering | `undo_resolve` marker DELETE keyed on payload id-set | mitigate | `delete(DedupResolution).where(file_id.in_(ids)).returning(file_id)` scoped to payload ids; `if not ids: return 0` gate (`dedup.py:308-333`); round-trip regressions in `test_duplicates.py` | closed |
| T-90A-SC | Tampering (supply chain) | package installs | N/A | Zero dependency delta (`git diff main...HEAD -- pyproject.toml uv.lock` empty) | closed |
| T-90B-01 | Tampering | removing CAS writer drops an idempotency guard | mitigate | Derived/marker + `ON CONFLICT` authority; double-invoke regressions `test_metadata_callback_idempotent_after_cas_removal` / `test_s3_push_status_transition_idempotent_after_cas_removal` | closed |
| T-90B-02 | Tampering | removing a backends writer could strand a routing signal | mitigate | Only pure state mirrors removed; cloud_job/marker left as sole authority; equivalence test green | closed |
| T-90B-03 | Repudiation | dedup-undo loses its restore path | accept | Marker DELETE is the true undo authority (`dedup.py:296-299`); state restore was redundant (see Accepted Risks) | closed |
| T-90B-SC | Tampering (supply chain) | package installs | N/A | Zero dependency delta | closed |
| T-90-guard | Tampering/DoS | 039 drops `files.state` on a mid-flight / shadow-inconsistent corpus | mitigate | `_guard()` raises before drop on mid-flight COUNT + hard-invariant anti-join; `begin_nested` + `SET LOCAL lock_timeout`; `files_state_archive` snapshot pre-drop (`039...py:189-241`) | closed |
| T-90-sqli | Tampering | injection via migration/guard SQL | mitigate | Guard/archive SQL are fixed-literal `sa.text` constants (no dynamic operands — stronger than parameterization); static test `test_no_f_string_interpolated_sql` | closed |
| T-90-loss | Info disclosure / data loss | `downgrade()` cannot reconstruct exact scalar | mitigate | Verbatim `_RESTORE_FROM_ARCHIVE` UPDATE FROM archive + scoped `_DERIVED_FALLBACK`; lossy transient cases enumerated in docstring; round-trip test | closed |
| T-90-pii | Info disclosure | migration/guard/test output leaks paths | mitigate | Guard/loggers emit counts + file_id UUIDs only; no `original_path`/`original_filename` in 039 | closed |
| T-90-frozen | Tampering | migration coupling to mutable app code | mitigate | D-07: no `import phaze` in 039; static asserts `test_migration_never_references_saq_jobs` + no-`from phaze` test | closed |
| T-90-SC | Tampering (supply chain) | package installs | N/A | Zero dependency delta | closed |
| T-90-01 | Tampering (dropped coverage) | retired shadow/divergence tests + ~91 migrated tests | mitigate | Tests MIGRATED not deleted; both coverage gates wired — per-module `FLOOR=90.0` (`coverage_floor.py:33`) + combined `--fail-under=95` (`justfile:134`) | closed |
| T-90-02 | Tampering/Repudiation (guard toothlessness) | `test_no_filestate_guard.py` | mitigate | Tokenize-strips COMMENT+STRING, scoped regexes (excludes `app.state`), DOTALL multi-line `.values(state=)`, planted-match self-test; independently mutation-tested RED→GREEN (VERIFICATION truth #8) | closed |
| T-90-03 | Info disclosure / DoS | migration 039 (landed in 90-03) | accept | Out of scope for 90-04; 039 untouched (single `feat(90-03)` commit) — code/test-only plan (see Accepted Risks) | closed |
| T-90-04 | Tampering (model↔DB drift) | `models/file.py` vs 039 | mitigate | `test_039_autogenerate_diff_is_empty_for_dropped_objects` (GREEN); `class FileState` removed from source | closed |
| T-90-04-SC | Tampering (supply chain) | package installs | N/A | Zero dependency delta | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-90-01 | T-90A-02 | Search `file_state` facet had no UI consumer; removing it changes no data exposure — results are already scoped to the single admin user. Confirmed removed (`grep -c file_state` == 0 in `search_queries.py` and `routers/search.py`) with no derived replacement. | Robert (plan author) | 2026-07-13 |
| AR-90-02 | T-90B-03 | The `DedupResolution` marker DELETE (`dedup.py`) is the sole true undo authority; the former `files.state` restore was redundant bookkeeping (D-05). With `files.state` fully dropped, no restore path is structurally possible or needed. | Robert (plan author) | 2026-07-13 |
| AR-90-03 | T-90-03 | Migration 039 shipped in 90-03 and is quarantined behind the verified `_guard()` (T-90-guard); plan 90-04 is code/test-only and correctly did not touch it (single `feat(90-03)` commit on the file). | Robert (plan author) | 2026-07-13 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-07-13 | 20 | 20 | 0 | gsd-security-auditor (opus) |

**Audit notes (non-blocking):**
- **T-90-sqli** — register wording said operands are "parameterized via `sa.text().bindparams()`", but the implementation is *stronger*: the guard SQL has no dynamic operands at all (fixed FileState-value literals), so `bindparams` is absent by design. The no-untrusted-operand property holds and is test-enforced.
- **T-90A-04** — the register's "neutralises to DISCOVERED" clause described the transient PR-A state; final code (post PR-B/PR-C) has no `files.state`, so the id-set-scoped marker DELETE is the sole authority and no `duplicate_resolved`-without-marker divergence is structurally possible.

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-07-13
