---
phase: 84-dedup-fingerprint-progress-cutover
asvs_level: 1
threats_total: 16
threats_closed: 16
threats_open: 0
status: secured
audited: 2026-07-10
auditor: gsd-security-auditor
block_on: high
register_authored_at_plan_time: true
notes: >
  All 16 plan-time threats resolve to CLOSED against source. One threat (T-84-06-02) shipped with a
  FICTITIOUS stated mitigation; it is re-dispositioned here with its true, verified controls. Two threats
  (T-84-04-01, T-84-03-03) were source-only at plan time and were locked by tests added this audit cycle.
---

# Phase 84 — Dedup & Fingerprint-Progress Cutover: Security Audit

**Phase:** 84 — dedup-fingerprint-progress-cutover
**ASVS Level:** 1
**Block-on:** high
**Threats:** 16 total · 16 CLOSED · 0 OPEN
**Result:** SECURED

Every mitigation was verified against the shipped source (`file:line`), not against plan prose. Where a
mitigation is a behavioural claim, the test that locks it is named; where no test locks it, that is stated
explicitly. The plan-time register attributed one control that does not exist in production
(T-84-06-02) — it is corrected below with the real controls, which were independently verified.

---

## Threat Verification

| Threat ID | Category | Disp. | Status | Evidence (source + test) |
|-----------|----------|-------|--------|--------------------------|
| T-84-01-01 | Tampering | accept | CLOSED | Both `035` statements are static string constants, no interpolation: `alembic/versions/035_reconcile_dedup_resolution.py:80-100` (`_BACKFILL_DEDUP`, `_DELETE_ORPHANED_MARKERS`); `upgrade()` only `op.execute(sa.text(CONST))` at `:105-106`. Test lock: `test_backfill_sql_is_static_and_parameter_free` (`tests/integration/test_migrations/test_migration_035_reconcile_dedup_resolution.py:103`) + `test_migration_never_references_saq_jobs` (`:96`). |
| T-84-01-02 | Denial of Service | accept | CLOSED | Single set-based `DELETE … USING files … WHERE f.state <> 'duplicate_resolved'` over the indexed unique FK `uq_dedup_resolution_file_id`; runs once at deploy. Source: `035:96-100`. Safe failure mode (reappear-for-review) documented `035:26-28,92-95`. |
| T-84-01-SC | Tampering | accept | CLOSED | Zero new deps: `84-01-SUMMARY.md` `tech-stack.added: []`. Migration imports only stdlib + `sqlalchemy` + `alembic` (`035:59-63`). |
| T-84-02-01 | Tampering | accept | CLOSED | `dedup_resolved_clause()` is a correlated `exists(select(DedupResolution.id).where(DedupResolution.file_id == FileRecord.id))` over a static column set, no user input: `src/phaze/services/stage_status.py:90`. Drift-lock: the predicate is deliberately kept OUT of the five `Stage` ladders (verified absent from `done_clause`/`failed_clause`/`inflight_clause`/`domain_completed_clause`/`stage_status_case`); Phase-78 equivalence test still green — `tests/integration/test_stage_status_equivalence.py` (exists; 36 passed per 84-02-SUMMARY). |
| T-84-02-SC | Tampering | accept | CLOSED | Zero new deps: `84-02-SUMMARY.md` `tech-stack.added: []`. |
| T-84-03-01 | Elevation of Privilege | mitigate | CLOSED | D-06 CAS: `delete(DedupResolution).where(file_id.in_(restore_by_id)).returning(DedupResolution.file_id)`; state restore iterates only the `returned` set — `src/phaze/services/dedup.py:337-346`. A payload of ids that never held a marker returns zero rows → restores nothing. Test lock: `test_stale_undo_replay_is_a_noop` (`tests/integration/test_dedup_resolve_undo_shadow.py:150`). |
| T-84-03-02 | Tampering | mitigate | CLOSED | `previous_state` coerced via `FileState(raw_state)` with `ValueError` skip and validated into `restore_by_id` **before** any DELETE — `dedup.py:311-329` (esp. `:327`). Strengthened after code review (WR-01/WR-02, commit `5215d82c`): validate-then-scope ordering means a marker is deleted only when its restore is guaranteed, closing the marker-less `duplicate_resolved` divergence. Test lock: `test_undo_with_invalid_previous_state_keeps_marker_and_gate_green` (`:191`), `test_undo_with_malformed_uuid_does_not_raise` (`:213`), `test_undo_duplicate_entries_do_not_inflate_count` (`:233`) — all verified RED against pre-fix `a67ed16a`. |
| T-84-03-03 | Tampering | mitigate | CLOSED | `pg_insert(DedupResolution).values(rows).on_conflict_do_nothing(index_elements=["file_id"])` on the unique `file_id` — first-writer-wins, cannot raise `IntegrityError`: `dedup.py:285`. Test lock (added this audit cycle): `test_second_resolve_of_same_group_is_a_noop` (`:256`) + `test_concurrent_double_submit_insert_conflict_is_a_noop` (`:275`, blinds the selection clause so the conflict is actually reached across transactions). See Corrections §2. |
| T-84-03-SC | Tampering | accept | CLOSED | Zero new deps: `84-03-SUMMARY.md` `tech-stack.added: []`. |
| T-84-04-01 | Denial of Service | mitigate | CLOSED | D-00e: every DB dependency imported function-locally inside `get_fingerprint_progress` — `src/phaze/services/fingerprint.py:286-291` (all `# noqa: PLC0415`); no module-level `phaze.models`/`phaze.database`/`phaze.services.pipeline`/`phaze.services.stage_status`. Test lock (added this audit cycle): `tests/shared/test_fingerprint_import_boundary.py` — `test_no_forbidden_module_level_imports` (AST scan) + `test_importing_fingerprint_does_not_load_orm` (fresh-interpreter `sys.modules` check). See Corrections §2. |
| T-84-04-02 | Information Disclosure | accept | CLOSED | Endpoint returns exactly three integers, no per-file data/PII: `get_fingerprint_progress` returns `{"total", "completed", "failed"}` — `fingerprint.py:301`. Sole caller is the parameterless read-only `routers/pipeline.py:1339`. |
| T-84-04-SC | Tampering | accept | CLOSED | Zero new deps: `84-04-SUMMARY.md` `tech-stack.added: []`. |
| T-84-05-01 | Tampering | mitigate | CLOSED | The guard IS the mitigation: an `ast.parse`/`ast.walk` scan that walks positional (`Call.args`) AND keyword (`Call.keywords`) args of `where`/`filter`/`filter_by`/`having` — `tests/shared/test_dedup_fingerprint_source_scan.py:91-98`. Asserts dedup.py has exactly 1 writer / 0 reads and fingerprint.py has 0 `FINGERPRINTED` attribute access (`:125`, `:148`). Mutation-tested both directions in-repo: positional (`:168`), keyword (`:187`), compare (`:200`), writer-allowed false-positive (`:208`), fingerprinted (`:218`), docstring false-positive (`:226`). Verifier independently mutated live source at `dedup.py:81` → RED. |
| T-84-05-SC | Tampering | accept | CLOSED | Zero new deps — `ast` is stdlib. `test_dedup_fingerprint_source_scan.py` imports only `ast` + `pathlib`. |
| T-84-06-01 | Information Disclosure | mitigate | CLOSED | `make_url` masks the password in every `str()`/`repr()`; operator output renders host/db only — `src/phaze/cli/shadow_compare.py:_parse_dsn_or_exit` (`:52-63`), `_safe_target` (`:90-95`), the `print(... {_safe_target(url)})` at `:131`, and `--database-url` help text "NEVER echoed in full" (`:85`). Procedural half held: `84-06-SUMMARY.md` contains no DSN/password (Method note, `:122-125`). Code control is structural (SQLAlchemy `URL` masking); the "don't paste the DSN" clause is operator discipline, not test-locked. |
| T-84-06-02 | Tampering | mitigate | CLOSED (corrected) | **Stated mitigation is FICTITIOUS** — no `_test`-suffix destructive-write guard exists in `src/phaze/` (see Corrections §1). **Actual, verified controls:** (a) `services/shadow_compare.py` is read-only *by construction* — every DB touch is `session.execute(select(...))` at `:213` and `:220`, zero `insert`/`update`/`delete`/`commit`/`flush`/`add`; (b) `cli/shadow_compare.py` imports neither `phaze.main` nor Alembic (verified), so `--database-url` cannot migrate; (c) the live run was executed under `BEGIN TRANSACTION READ ONLY` (`SHOW transaction_read_only → on`, `84-06-SUMMARY.md:23-26`) — a DB-level guarantee. Residual CLOSED 2026-07-10: `tests/shared/test_shadow_compare_readonly.py` now locks the read-only property (AST; mutation-verified). |

---

## Accepted Risks Log

The following `accept`-disposition threats are recorded here as the standing accepted-risk register for
Phase 84. Each was verified to hold in source at audit time.

| Threat ID | Category | Accepted rationale | Verified at |
|-----------|----------|--------------------|-------------|
| T-84-01-01 | Tampering | `035`'s two SQL statements are compile-time string constants with a fixed `'duplicate_resolved'` literal — no interpolation, f-string, `.format`, or model import, so there is no SQL-injection surface. | `035:80-100,105-106`; test `:103` |
| T-84-01-02 | Denial of Service | The orphaned-marker `DELETE` is one set-based statement over an indexed FK, bounded by corpus size, run once at deploy. On the live corpus it is a 0-row no-op (production at rev `031`, `84-06-SUMMARY.md`). Safe failure mode documented. | `035:96-100` |
| T-84-01-SC / -02-SC / -03-SC / -04-SC / -05-SC | Tampering (supply chain) | Zero new dependencies in any of the five code plans (`ast` in 84-05 is stdlib). No package-legitimacy surface. | `tech-stack.added: []` in each SUMMARY |
| T-84-02-01 | Tampering | The dedup predicate is built from a static ORM column set with no runtime input; the Phase-78 equivalence drift-lock guards against it leaking into a `Stage` dispatch ladder. | `stage_status.py:90` |
| T-84-04-02 | Information Disclosure | The progress endpoint returns three corpus-wide integers — no per-file rows, filenames, paths, or PII; internal-realm reverse-proxy posture unchanged. | `fingerprint.py:301`; `routers/pipeline.py:1339` |

---

## Corrections to the plan-time register

### §1 — T-84-06-02's stated mitigation is fictitious (control corrected, threat still CLOSED)

The plan-time register (84-06-PLAN.md:114) claimed:

> "the destructive-write guard refuses any DB whose name does not end in `_test`"

**No such guard exists in `src/phaze/`.** A repository-wide search (`endswith.*_test`,
`destructive.?write`, `refus…`) over `cli/shadow_compare.py` and `services/shadow_compare.py` returns
nothing production-side. The only `_test`-suffix guard in the codebase is a **test-suite** skip —
`tests/integration/test_dedup_resolve_undo_shadow.py:55`
(`if not _TARGET_DB.endswith("_test"): pytest.skip(...)`) — which protects the CI test database, not the
production CLI. The register attributed a test-harness guard to a production control.

The accidental-write threat is nonetheless genuinely mitigated by **real, verified controls**, so the
threat is CLOSED with corrected mitigation text (see the T-84-06-02 row above):
1. `services/shadow_compare.py` issues only `SELECT`s — read-only by construction (`:213`, `:220`; zero
   write/DML calls anywhere in the module).
2. `cli/shadow_compare.py` imports no `phaze.main` and no Alembic — `--database-url` can open a session
   but cannot migrate or DDL.
3. The live-corpus run (84-06) was, per its own SUMMARY, executed **read-only against the real
   production database** (not a `_test` DB) inside `BEGIN TRANSACTION READ ONLY` — the stated mitigation
   was not merely absent, it was not the control that was used. The actual controls held.

### §2 — Two threats were source-only (no test) until this audit cycle

Both are now CLOSED with mutation-verified tests:

- **T-84-04-01** (agent-worker module-level DB import → worker crash). The function-local imports existed
  in `fingerprint.py` but nothing locked them; 84-04's "`sys.modules` leak spot-check" was performed by
  hand and never committed. Locked by `tests/shared/test_fingerprint_import_boundary.py` (AST scan of
  module-level imports + fresh-interpreter `sys.modules` assertion). Mutation: hoisting
  `from phaze.services.stage_status import done_clause` to module scope turns both tests RED.
- **T-84-03-03** (concurrent HTMX double-submit). `on_conflict_do_nothing(...)` existed in source but no
  test reached the conflict — a sequential second POST is filtered out by `~dedup_resolved_clause()`
  before the INSERT. Locked by `test_second_resolve_of_same_group_is_a_noop` (sequential) and
  `test_concurrent_double_submit_insert_conflict_is_a_noop` (blinds the selection clause so the conflict
  fires cross-transaction). Mutation: dropping `.on_conflict_do_nothing(...)` turns the concurrent test
  RED while the sequential one stays GREEN.

### §3 — T-84-03-02 hardened after code review (confirmed in shipped code)

Code review (84-REVIEW.md, WR-01/WR-02) found the original `undo_resolve` coerced `previous_state`
*after* the marker `DELETE`, so a coercion failure left `state='duplicate_resolved'` with no marker — the
exact hard `shadow_compare.py:135` divergence this phase's SC#3 must keep green. The shipped code
(`dedup.py:311-329`, commit `5215d82c`) validates the entire payload into `restore_by_id` **before** any
write and scopes the `DELETE` to those ids. Verified: the current source matches this description exactly.

---

## Hardening Recommendation — CLOSED (2026-07-10)

**T-84-06-02 residual — the read-only property of `services/shadow_compare.py` was not test-locked.**
The auditor raised this as a non-blocking WARNING: the threat was mitigated by construction, but a
future edit adding `INSERT`/`UPDATE`/`DELETE`/`commit` to that module would have shipped green.

**Closed in the same cycle.** `tests/shared/test_shadow_compare_readonly.py` (DB-free, `shared` bucket)
now asserts two independent properties of the module:

1. it imports none of `insert` / `update` / `delete` / `text` — so it cannot *build* a write statement;
2. it calls no mutating `Session` method (`add`, `add_all`, `commit`, `flush`, `merge`, `delete`,
   `bulk_*`) — `execute` is deliberately allowed, since that is how `select()`s are issued.

AST-based rather than grep-based on purpose: the module's own docstring (line 21) and a comment
(line 66) contain the words `text()` and `update` as **prose**, describing the anti-patterns it avoids.
A line-oriented grep guard would false-positive on them. This is the identical trap that made two
Phase-83 guards toothless.

**Mutation-verified, both directions:**

| Mutation | Expected | Observed |
|---|---|---|
| add `delete` to the `from sqlalchemy import …` line | RED | `test_shadow_compare_imports_no_write_constructs` failed |
| add a `session.add(object())` call | RED | `test_shadow_compare_calls_no_session_mutators` failed |
| leave the `text()` / `update` docstring prose untouched | GREEN | both passed |

Source restored, working tree clean. The read-only invariant that actually mitigates T-84-06-02 is now
standing insurance rather than a construction accident — which matters, because it is the property that
was relied upon to run the Phase-84 live-corpus check directly against the production database.

---

## Unregistered Flags

None. The `## Threat Flags` section of every plan SUMMARY (84-01 … 84-06) reads **None** — no new network
endpoints, auth paths, or schema surface were introduced during implementation. No new attack surface
appeared without a threat mapping.

---

## Audit Trail

- **2026-07-10** — Audit performed against shipped source at branch `SimplicityGuy/phase-84`.
- Loaded all six plans (threat_model blocks), all six SUMMARYs, 84-REVIEW.md, 84-VERIFICATION.md,
  84-CONTEXT.md, and read the shipped source: `services/dedup.py`, `services/fingerprint.py`,
  `services/stage_status.py`, `services/shadow_compare.py`, `cli/shadow_compare.py`,
  `models/dedup_resolution.py`, `alembic/versions/035_reconcile_dedup_resolution.py`, and the seven
  cited test files.
- Verified each of the 16 plan-time threats by disposition (mitigate → grep the pattern in the cited
  file + name the test; accept → recorded in the Accepted Risks Log above).
- Independently confirmed all three orchestrator findings against source:
  1. T-84-06-02's `_test`-suffix destructive-write guard is absent from `src/phaze/` (grep returned no
     production match); the real controls (read-only-by-construction service + no-Alembic CLI) verified.
  2. T-84-04-01 and T-84-03-03 are now test-locked (`test_fingerprint_import_boundary.py`;
     `test_second_resolve_of_same_group_is_a_noop` + `test_concurrent_double_submit_insert_conflict_is_a_noop`).
  3. T-84-03-02's validate-before-delete ordering (commit `5215d82c`) is present in shipped source.
- Implementation files were **not** modified. Only this file (`84-SECURITY.md`) was written.

---

*Audited: 2026-07-10 · gsd-security-auditor · ASVS Level 1 · block-on: high*
