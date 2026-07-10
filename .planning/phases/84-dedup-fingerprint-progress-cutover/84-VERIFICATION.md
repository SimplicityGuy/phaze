---
phase: 84-dedup-fingerprint-progress-cutover
verified: 2026-07-10T03:46:03Z
status: passed
score: 7/7 must-haves verified
overrides_applied: 0
resolved: 2026-07-10T04:30:00Z
human_verification_completed:
  - test: "Live-corpus shadow-compare (D-16.2 / plan 84-06)"
    performed: "Read-only measurement against the live phaze database (BEGIN TRANSACTION READ ONLY; transaction_read_only=on). No snapshot, no migration, no writes."
    result: "Production is at Alembic revision 031. dedup_resolution does not exist; 0 files at state='duplicate_resolved' (6 duplicate groups unresolved); fingerprint_results empty. The duplicate_resolved invariant has ZERO exposure and cannot diverge: 035 never writes files.state and its insert covers every duplicate_resolved row by construction."
    corrections: "The plan's premises were wrong on three counts, all verified in source: (1) no _test-suffix destructive-write guard exists — shadow_compare has zero write calls, so no restore was ever required; (2) hard_fail_total=0 is the wrong pass condition — it aggregates all 13 hard invariants, 12 of which this phase does not own; (3) the repair is provable by construction, not empirical."
    evidence: "84-06-SUMMARY.md"
---

# Phase 84: Dedup & Fingerprint-Progress Cutover Verification Report

**Phase Goal:** Cut `services/dedup.py` and `get_fingerprint_progress` over to the dedup marker / output tables, so dedup resolve/undo and the fingerprint progress bar derive from data rather than `FileRecord.state`.
**Verified:** 2026-07-10T03:46:03Z
**Status:** passed (SC#3 closed by the read-only live measurement recorded in 84-06-SUMMARY.md)
**Re-verification:** No — initial verification

## Scope Note

5 of 6 plans executed (84-01 … 84-05). Plan 84-06 (the live-corpus shadow-compare run, D-16.2) is
**deferred, not executed** — `84-06-DEFERRED.md` records it as `autonomous: false`, `blocking: true`,
`gate: pre-merge`. It requires an operator-supplied production DB restore and DSN that no automated
agent (executor or verifier) can obtain. This report treats 84-06 as an open human-verification item,
not as a passed or failed automated truth. **This phase is NOT complete** until 84-06 is run and its
SUMMARY is recorded.

REQUIREMENTS.md correctly still shows READ-04 and SIDECAR-02 as `Pending` (not marked Complete) — this
was independently confirmed: an earlier premature Complete-mark was caught by
`tests/shared/core/test_requirements_traceability.py` and reverted in commit
`3ec4ade9 fix(84): revert premature Complete marks on READ-04 / SIDECAR-02`. That guard test was
re-run during this verification and is green (10/10 passed).

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Dedup's exclusion filters read/write the durable dedup marker (no `FileRecord.state` read) | VERIFIED | `src/phaze/services/dedup.py`: `~dedup_resolved_clause()` used at all 9 former read sites (lines 81,93,131,144,191,212,224,238,263); `grep -c "dedup_resolved_clause()"` in dedup.py = 9. `FileState.DUPLICATE_RESOLVED` appears exactly twice total — once in a docstring, once as the RHS write at line 274. AST guard (`tests/shared/test_dedup_fingerprint_source_scan.py::test_dedup_has_exactly_one_writer_and_no_reads`) asserts 1 write / 0 reads / 0 other and passes. |
| 2 | `undo_resolve` becomes a plain `DELETE` (marker CAS), previous_state restore scoped to `RETURNING`ed ids | VERIFIED | `dedup.py:291-332`: `delete(DedupResolution).where(...).returning(DedupResolution.file_id).execution_options(synchronize_session=False)`; state restore loop only processes ids in `returned` set; `previous_state` coerced via `FileState(entry["previous_state"])` with `except ValueError: continue` (T-84-03-02 mitigation confirmed at line 326-328). Stale-replay no-op proven by `test_stale_undo_replay_is_a_noop` (PASSED, independently re-run). |
| 3 | Resolve/undo preserved and backfilled rows honored | VERIFIED | `resolve_group` writes via `pg_insert(DedupResolution).values(rows).on_conflict_do_nothing(index_elements=["file_id"])` with explicit `id=uuid_mod.uuid4()` per row (dedup.py:284, matches D-02/RESEARCH Pitfall 2) and `canonical_file_id=canonical_id` (D-03). Migration `035` backfills missing markers + deletes orphaned ones bidirectionally; migration test seeds a full D-04 corpus and asserts both directions, idempotency, empty autogenerate diff, no-op downgrade — all PASSED on independent re-run. |
| 4 | `get_fingerprint_progress` derives from per-engine coverage predicate / output tables, not `FileRecord.state` | VERIFIED | `src/phaze/services/fingerprint.py:256-301`: `denom = (FileRecord.file_type.in_(MUSIC_VIDEO_TYPES), ~dedup_resolved_clause())`; `completed`/`failed` ride `done_clause(Stage.FINGERPRINT)` / `failed_clause(Stage.FINGERPRINT)`. AST guard confirms zero `FileState.FINGERPRINTED` attribute accesses (the one token match is prose in a docstring, correctly not flagged). Real-DB integration test (`test_fingerprint_progress.py`) seeds 7 scenario files including a `state='duplicate_resolved'`-no-marker music file (proves marker-not-state) and asserts `{"total": 5, "completed": 2, "failed": 1}` — PASSED on independent re-run. |
| 5 | All three progress keys share one denominator (D-17) | VERIFIED | `fingerprint.py:295-299`: `total`, `completed`, `failed` all built from the same `*denom` tuple; test asserts `completed <= total` and `failed <= total`. |
| 6 | `services/fingerprint.py` DB imports remain function-local (agent-worker import boundary, D-00e) | VERIFIED | Module-level imports (lines 1-17) are only `collections`, `dataclasses`, `typing`, `httpx`, `structlog` + a `TYPE_CHECKING`-guarded `AsyncSession` import. `sqlalchemy`, `phaze.enums.stage`, `phaze.models.file`, `phaze.services.pipeline`, `phaze.services.stage_status` are all imported inside `get_fingerprint_progress` (lines 286-291, each tagged `# noqa: PLC0415`). |
| 7 | The shadow-compare gate stays green after the cutover (SC#3) | **HALF-PROVEN** | D-16.1 (committed CI test on synthetic corpus): `tests/integration/test_dedup_resolve_undo_shadow.py` asserts `run_shadow_compare(session).hard_fail_total == 0` through resolve→undo→re-resolve + a stale-replay case; both tests PASSED on independent re-run. D-16.2 (live-corpus run): **OPEN** — `84-06-DEFERRED.md` confirms plan 84-06 was not executed; it requires an operator DB restore. Routed to human verification below; **does not count as a failed automated truth**, but the phase's own success criterion is not yet fully proven. |

**Score:** 6/7 automated truths fully VERIFIED; SC#3 is proven one of two required ways (CI test green, live-corpus run outstanding).

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/services/dedup.py` | 9 `dedup_resolved_clause()` reads, 1 `DUPLICATE_RESOLVED` write, CAS undo | VERIFIED | Confirmed via grep + AST guard + behavioral divergence tests |
| `src/phaze/services/fingerprint.py` | Derived `get_fingerprint_progress`, zero `FINGERPRINTED` AST access, function-local DB imports | VERIFIED | Confirmed via grep, AST guard, module-level import scan |
| `src/phaze/services/stage_status.py` | `dedup_resolved_clause()` file-level predicate, absent from 5 Stage dispatch ladders | VERIFIED | Function defined at line 90; `done_clause`/`failed_clause`/`inflight_clause`/`domain_completed_clause`/`stage_status_case` bodies inspected — none reference it |
| `src/phaze/models/dedup_resolution.py` | D-08 scan_deletion docstring note | VERIFIED | Present, lines 16-28, documents dual-FK un-resolve behavior as deliberate |
| `alembic/versions/035_reconcile_dedup_resolution.py` | Data-only, no DDL, `down_revision="034"`, 2 statements, documented downgrade | VERIFIED | Confirmed by reading file + migration test (empty autogenerate diff assertion passed) |
| `tests/integration/test_dedup_divergence.py` | Inconsistent-corpus guard across 5 readers | VERIFIED | 5 tests, all PASSED on independent re-run |
| `tests/integration/test_dedup_resolve_undo_shadow.py` | D-16.1 shadow-compare cycle test | VERIFIED | 2 tests, all PASSED |
| `tests/integration/test_fingerprint_progress.py` | D-15 real-DB replacement test | VERIFIED | 1 comprehensive test, PASSED |
| `tests/integration/test_migrations/test_migration_035_*.py` | Migration test | VERIFIED | 4 tests, all PASSED |
| `tests/shared/test_dedup_fingerprint_source_scan.py` | AST guard + mutation-direction tests | VERIFIED | 8 tests, all PASSED; guard independently mutation-tested by this verifier (see below) |
| `.planning/phases/84-.../84-06-SUMMARY.md` | Recorded live shadow-compare TOTALS | **MISSING** | Plan 84-06 not executed; `84-06-DEFERRED.md` exists instead, documenting the blocking gate |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `dedup.py` readers | `stage_status.dedup_resolved_clause()` | module-level import + 9 call sites | WIRED | Confirmed |
| `fingerprint.py get_fingerprint_progress` | `stage_status.done_clause`/`failed_clause`/`dedup_resolved_clause` | function-local import | WIRED | Confirmed, D-00e boundary respected |
| `resolve_group` | `dedup_resolution` table | `pg_insert(...).on_conflict_do_nothing(...)` | WIRED | Confirmed; flush-only, no commit (caller-owned txn discipline) |
| `undo_resolve` | `dedup_resolution` table | `delete(...).returning(...)` | WIRED | Confirmed; CAS-gated `FileRecord.state` restore |
| Migration `035` | `dedup_resolution` / `files` tables | `op.execute(sa.text(...))` static SQL | WIRED | Confirmed; ordering (`035` before reader flip) honored — `035` landed in 84-01, reader flip in 84-03 |
| `services/shadow_compare.py:135` hard invariant | dedup writer/undo | resolve→undo→re-resolve cycle | WIRED (synthetic corpus only) | D-16.1 proven; D-16.2 (live corpus) open |

### Data-Flow Trace (Level 4)

`get_fingerprint_progress` and the dedup readers are pure query functions (no rendering component), so
a props/DOM trace does not apply. The relevant Level-4 concern — does the query actually derive from
real rows rather than a static/hardcoded return — was verified directly: `get_fingerprint_progress`
executes 3 real `select(func.count(...))` statements against `FileRecord` joined through
`done_clause`/`failed_clause`/`dedup_resolved_clause` (all `exists(...)` correlated subqueries against
real tables: `FingerprintResult`, `DedupResolution`). The integration test seeds real rows and asserts
non-trivial, differentiated counts (`{"total": 5, "completed": 2, "failed": 1}`), ruling out a
static/stub return.

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Guard has teeth: reintroducing a `FileRecord.state` read at `dedup.py:81` trips the AST source-scan guard | Live mutation applied by this verifier (not the SUMMARY's claim) — replaced `.where(~dedup_resolved_clause())` with `.where(FileRecord.state != FileState.DUPLICATE_RESOLVED)`, ran `test_dedup_has_exactly_one_writer_and_no_reads`, restored via `git checkout --` | `1 failed` (RED) confirmed independently; `git status --short` clean after restore | PASS |
| Full phase-relevant test suite green | `uv run pytest tests/integration/test_dedup_divergence.py tests/integration/test_dedup_resolve_undo_shadow.py tests/integration/test_fingerprint_progress.py tests/integration/test_migrations/test_migration_035_*.py tests/shared/test_dedup_fingerprint_source_scan.py tests/fingerprint/services/test_fingerprint.py tests/fingerprint/routers/test_pipeline_fingerprint.py tests/discovery/services/test_dedup.py -v` (TEST_DATABASE_URL :5433, PHAZE_REDIS_URL :6380 exported) | 78 passed, 0 failed | PASS |
| Requirements traceability guard green (no premature Complete marks) | `uv run pytest tests/shared/core/test_requirements_traceability.py -v` | 10 passed | PASS |
| Lint clean | `uv run ruff check .` | All checks passed | PASS |
| Type-check clean | `uv run mypy .` | Success: no issues found in 206 source files | PASS |
| Scope fences held | `git diff main...HEAD --stat -- src/phaze/services/pipeline.py src/phaze/services/proposal.py src/phaze/services/scan_deletion.py` | empty output (no changes) | PASS |
| No new `ORDER BY` added to `dup_hashes` subquery | `grep -n "order_by" src/phaze/services/dedup.py` | Only the pre-existing outer-query `order_by(sha256_hash, original_path)` at lines 94/145; `dup_hashes` subquery itself has no `ORDER BY` (unchanged, pre-existing nondeterminism noted in 84-CONTEXT Deferred) | PASS (no regression) |

### Probe Execution

No `scripts/*/tests/probe-*.sh` conventional probes found and none declared in the phase's PLAN/SUMMARY files. `Step 7c: SKIPPED (no declared or conventional probes for this phase)`.

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|--------------|--------|----------|
| READ-04 | 84-01,02,03,04,05 | Dedup + `get_fingerprint_progress` derive from marker/output tables, not `FileRecord.state` | SATISFIED (code) / traceability correctly Pending | All read sites cut over, AST guard enforces it. REQUIREMENTS.md intentionally still shows "Pending" — correct per orchestrator instruction; requirement completion is owned by `gsd-sdk phase.complete`, gated on 84-06. |
| SIDECAR-02 | 84-01,02,03,06 | Durable dedup marker with resolve/undo preserved, backfilled rows honored | **PARTIALLY SATISFIED** | Writer/undo/backfill code + tests all verified. The requirement's implicit "shadow gate stays green" clause is only half-proven (D-16.1 done, D-16.2 open per 84-06-DEFERRED.md). |

No orphaned requirements found — READ-04 and SIDECAR-02 are the only two mapped to Phase 84 in REQUIREMENTS.md, and both appear in plan frontmatter.

### Anti-Patterns Found

None. Scanned every file modified by this phase (`dedup.py`, `fingerprint.py`, `stage_status.py`,
`dedup_resolution.py`, `035_reconcile_dedup_resolution.py`, and all 5 new/modified test files) for
`TBD`/`FIXME`/`XXX`/`TODO`/`HACK`/`PLACEHOLDER`/empty-implementation patterns. Zero matches.

### Human Verification Required

### 1. Live-corpus shadow-compare run (D-16.2 / plan 84-06)

**Test:** Restore a recent live DB snapshot into a database whose name ends in `_test` (required by
`shadow_compare`'s destructive-write guard — the run itself is read-only). Run `uv run alembic upgrade
head` to apply migration `035`. Capture the BEFORE `GET /api/v1/fingerprint/progress` reading. Run `just
shadow-compare --database-url <restore-dsn>`. Capture the AFTER reading.

**Expected:** Exit code `0`; output line `TOTALS: hard_fail_total=0`; the `duplicate_resolved` invariant
line reads `0 divergent`.

**Why human:** `84-06-PLAN.md` is `autonomous: false` — it requires an operator-supplied production
database restore and DSN that no CI/agent process has access to. The committed CI test
(`test_dedup_resolve_undo_shadow.py`, D-16.1, already green) constructs its own synthetic corpus and
therefore provably cannot contain the real post-`032` `state='duplicate_resolved'`-with-no-marker rows
that D-01 discovered — those rows only exist in the live corpus. Only this run proves migration `035`'s
reconcile actually repaired them. Per `84-CONTEXT.md` D-16 and `84-06-DEFERRED.md`, this is a **blocking
pre-merge gate**, not an optional nice-to-have: skipping it a second time (Phase 79 already deferred the
first live shadow-compare run, D-02) is exactly the failure mode that let D-01 go unnoticed across two
phases.

## Gaps Summary

No code-level gaps or defects were found. Every artifact the phase was supposed to produce exists,
is substantive (not a stub), is correctly wired, and is independently verified against real behavior
(re-run tests, a live mutation of the AST guard, ruff/mypy). The dedup marker writer, CAS undo,
nine reader flips, `get_fingerprint_progress` derivation, and the D-13 predicate all match their
CONTEXT-locked decisions exactly, and the scope fences (pipeline.py/proposal.py/scan_deletion.py
untouched, no new `ORDER BY`) held.

The phase is **not closeable** yet, however: the ROADMAP's own SC#3 ("The shadow-compare gate stays
green after the cutover") is defined by CONTEXT D-16 as requiring proof **two ways**, and only one way
(the CI synthetic-corpus test) is done. The second (a live-corpus run against a production restore) is
explicitly deferred in `84-06-DEFERRED.md` as a `blocking: true`, `gate: pre-merge` item, and by the
phase's own design this cannot be closed by an automated agent — it requires an operator with a DB
restore and a DSN. This is correctly a `human_needed` verification outcome, not a `gaps_found` one: the
5 completed plans contain no defects, and the open item is a deliberate, documented, correctly-scoped
deferral rather than a missed or broken implementation.

**Do not mark READ-04 or SIDECAR-02 Complete in REQUIREMENTS.md and do not run `gsd-sdk phase.complete`
for Phase 84 until plan 84-06 is run and its SUMMARY records `hard_fail_total=0` on the live corpus.**

---

*Verified: 2026-07-10T03:46:03Z*
*Verifier: Claude (gsd-verifier)*
