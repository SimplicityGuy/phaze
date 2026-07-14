---
phase: 81-per-stage-failure-persistence-retry-paths
plan: 05
subsystem: agent-api
tags: [fastapi, sqlalchemy, postgres, on-conflict-upsert, failure-markers, dual-write]

# Dependency graph
requires:
  - phase: 81-per-stage-failure-persistence-retry-paths
    plan: 02
    provides: "migration 033 + AnalysisResult.__table_args__ analysis_completed_xor_failed CHECK -- the DB mutual-exclusion this writer must satisfy"
  - phase: 77-additive-schema-rescan-wipe-fix-migration-032
    provides: "migration 032 -- the additive analysis.failed_at / error_message columns + the ON CONFLICT (file_id) DO UPDATE backfill shape reused here"
  - phase: 81-per-stage-failure-persistence-retry-paths
    plan: 03
    provides: "report_metadata_failed's pg_insert(...).on_conflict_do_update idiom + put_metadata's unconditional clear-on-success, mirrored for analyze"
provides:
  - "report_analysis_failed dual-write: durable analysis.failed_at + error_message marker AND the kept files.state = ANALYSIS_FAILED write, in one transaction"
  - "put_analysis unconditional clear-on-success: failed_at/error_message wiped so a (re)analysis converges to done without violating the D-06 XOR CHECK"
  - "tests/analyze/routers/test_agent_analysis_failure.py -- proves no-prior-row upsert, XOR invariant, clear-on-success in the analyze bucket in isolation"
affects: [80-recovery-derivation, 82-reader-cutover, 90-destructive-migration]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "failure-marker dual-write: a durable per-stage marker (failed_at + error_message) is stamped ALONGSIDE the legacy files.state write, in one transaction, so recovery can derive from the marker while three live state readers stay working until cutover"
    - "insert-not-update for a pure-failure row: report_analysis_failed uses ON CONFLICT (file_id) DO UPDATE because a pure analyze failure never wrote an analysis row -- a bare UPDATE would silently no-op and lose the marker"
    - "clear-on-success outside exclude_unset: put_analysis's on_conflict set_ unconditionally sets failed_at=None/error_message=None (the wire body never carries them) so a real success clears a stale marker AND satisfies the completed-XOR-failed CHECK"

key-files:
  created:
    - tests/analyze/routers/test_agent_analysis_failure.py
  modified:
    - src/phaze/routers/agent_analysis.py

key-decisions:
  - "D-05: report_analysis_failed dual-writes -- it stamps analysis.failed_at + error_message AND keeps writing state = FileState.ANALYSIS_FAILED in the same transaction (three live files.state readers stay working until Phase 80/82; the state write dies in Phase 90)"
  - "D-06: report_analysis_failed clears analysis_completed_at when stamping failed_at (mutual exclusion), satisfying the migration-033 CHECK; the upsert is INSERT .. ON CONFLICT (file_id) DO UPDATE because a pure analyze failure has no prior analysis row"
  - "D-07: analysis.error_message = f'{reason}: {error}' from AnalysisFailurePayload, truncated to _ERROR_MESSAGE_MAX=2000 -- no schema change (mirrors report_metadata_failed's bound)"
  - "D-13: put_analysis unconditionally clears failed_at + error_message on success (not exclude_unset-driven) -- this is also what makes the completion branch satisfy the D-06 CHECK"

patterns-established:
  - "Marker dual-write with kept legacy write: additive durable marker + retained files.state write in one txn, marker-then-state ordering (RESEARCH Discretion #1)"
  - "Both analyze failure writers (report_analysis_failed persist, put_analysis clear) share metadata's pg_insert(...).on_conflict_do_update idiom, keeping the two failure subsystems structurally identical"

requirements-completed: [FAIL-01]

# Metrics
duration: 18min
completed: 2026-07-08
---

# Phase 81 Plan 05: Analyze Failure Dual-Write (report_analysis_failed) Summary

**`report_analysis_failed` now persists a durable `analysis.failed_at` + `error_message` marker (clearing `analysis_completed_at` so the migration-033 XOR CHECK never sees a mixed row) while KEEPING the `state = ANALYSIS_FAILED` write, and `put_analysis` unconditionally clears that marker on success -- unblocking Phase 80's recovery derivation from a durable marker without perturbing any Phase-78/79 derived status.**

## Performance

- **Duration:** ~18 min
- **Tasks:** 3/3
- **Files:** 2 (1 created, 1 modified)

## Accomplishments

### Task 1 — `report_analysis_failed` dual-write (`cc24e38c`)

`src/phaze/routers/agent_analysis.py`. Added a module constant `_ERROR_MESSAGE_MAX = 2000` (mirroring `agent_metadata.py`'s FAIL-02 bound) and rewrote the `report_analysis_failed` body so, in ONE transaction ordered per RESEARCH Discretion #1:

1. **marker upsert** — `pg_insert(AnalysisResult).values([{... "failed_at": now, "error_message": error_message, "analysis_completed_at": None}]).on_conflict_do_update(index_elements=["file_id"], set_={"failed_at": now, "error_message": error_message, "analysis_completed_at": None})`. `ON CONFLICT DO UPDATE` (not a bare UPDATE) because a pure analyze failure has no prior `analysis` row (D-06); the PK `id` is stamped explicitly because `AnalysisResult.id` has a Python-only default `pg_insert` bypasses; `analysis_completed_at` is cleared in the same row so the migration-033 XOR CHECK can never see a mixed row (D-06). `error_message = f"{body.reason}: {body.error}"[:_ERROR_MESSAGE_MAX]` (D-07), `failed_at = func.now()` server-set.
2. **kept state write** — `state = FileState.ANALYSIS_FAILED` retained (D-05 dual-write; three live readers depend on it until Phases 80/82, it dies in Phase 90).
3. **ledger clear** — `clear_ledger_entry(session, f"process_file:{file_id}")` (the poison-case guard, key from the PATH `file_id` only).
4. **staged-object delete** — `_delete_staged_object_if_cloud` (unchanged).
5. a single `session.commit()`.

`mypy` clean.

### Task 2 — `put_analysis` clear-on-success (`a787caa1`)

The success-path `on_conflict_do_update` `set_` clause now reads `{**{k: stmt.excluded[k] for k in dumped}, "failed_at": None, "error_message": None}` — an UNCONDITIONAL marker clear outside `exclude_unset` (the wire body never carries those columns, D-13). This makes a successful (re)analysis after a failure wipe the stale marker, and is also what lets the completion branch (which stamps `analysis_completed_at = func.now()`) satisfy the D-06 CHECK — both columns can never be non-NULL at commit. `mypy` clean.

### Task 3 — analyze-bucket tests (`ffe94492`)

`tests/analyze/routers/test_agent_analysis_failure.py`, inline smoke-app + authed-agent client (mirrors `tests/agents/routers/test_agent_analysis.py`), four `asyncio` tests:

- **no prior row** (RESEARCH OQ2): `POST .../failed` with no prior `analysis` row INSERTs the marker via ON CONFLICT DO UPDATE, `failed_at` set, `error_message == "timeout: boom"`, `analysis_completed_at` NULL, `state == ANALYSIS_FAILED`, `process_file` ledger cleared.
- **bodyless error**: an omitted `error` composes `"crashed: None"` (the field defaults to None on the wire) — the marker still records the reason.
- **failure after success**: a `POST .../failed` on a previously-analyzed file clears `analysis_completed_at` and stamps `failed_at` — the migration-033 XOR CHECK holds through the flip (no `IntegrityError`).
- **success after failure**: a real `put_analysis` after a failure clears `failed_at` + `error_message` and stamps `analysis_completed_at` (D-13).

Every test re-asserts the D-06 invariant via `_no_mixed_row_exists` (no row has both `analysis_completed_at` and `failed_at` set).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] `_file_state` test helper compared a raw enum value by identity**

- **Found during:** Task 3
- **Issue:** `select(FileRecord.state)` returned the raw enum VALUE (the str `'analyzed'`), so `... is FileState.ANALYZED` failed (`assert 'analyzed' is <FileState.ANALYZED: 'analyzed'>`) in all three state-asserting tests.
- **Fix:** Normalized the helper to `return FileState(raw)` so callers can compare by identity regardless of the column's native/non-native enum representation. Documented why in a comment.
- **Files modified:** `tests/analyze/routers/test_agent_analysis_failure.py`
- **Commit:** `ffe94492` (fixed before the task committed)

## Verification

| Command | Result |
|---------|--------|
| `uv run mypy src/phaze/routers/agent_analysis.py` | Success: no issues found |
| `uv run pytest tests/analyze/routers/test_agent_analysis_failure.py` | 4 passed |
| `uv run just test-bucket analyze` (isolation) | **510 passed**, 6 setup ERRORS (known colima DB-contention flake — see below) |
| re-run of the 6 erroring files in isolation | **62 passed** — confirmed flake, not regression |
| `uv run pytest tests/integration/test_migrations/test_migration_033_additive_check.py` | 5 passed (migration-033 XOR CHECK holds) |

The `just test-bucket analyze` run reported 6 `ERROR` (setup) results in `test_stage_progress.py`, `test_backends.py`, and `test_submit_cloud_job.py` — all unrelated to this change. Per the project's documented footgun, many *setup errors* (not assertion failures) under colima VM pressure are the known DB-contention flake. Re-running exactly those three files in isolation returned **62 passed**, confirming infra-flake, not regression. My four new tests passed both in the bucket and standalone.

Phase 79 shadow gate (D-04): the `state = ANALYSIS_FAILED` write is retained and the marker is purely additive, so no file's derived analyze status changes — the standing state↔derived implication gate is not perturbed. The `analyze` bucket that houses the derivation/shadow suites passed (minus the isolated flake).

## Success Criteria

- [x] An analyze failure persists a durable marker (`failed_at` + `error_message`) with the state write kept (Task 1).
- [x] No mixed row is ever written; the D-06 CHECK holds (Task 1 clears `analysis_completed_at`; Task 3 asserts the invariant; migration-033 test green).
- [x] `put_analysis` clears the marker on success (Task 2; Task 3 success-after-failure test).

## Known Stubs

None. No placeholder values, no `TODO`/`FIXME`, no unwired data sources introduced by this plan.

## TDD Gate Compliance

Task 3 carried `tdd="true"`, but the plan deliberately sequences Task 1 + Task 2 (implementation) BEFORE Task 3 (test) — the same design as sibling plan 81-02. The gate commits therefore appear as `feat(...)`, `feat(...)`, then `test(...)`.

A genuine RED did occur within Task 3: the first run failed 3/4 tests on the `_file_state` identity comparison (deviation 1 above), a real assertion failure caught and fixed before the task committed. The four behaviors themselves (dual-write, no-prior-row upsert, XOR invariant, clear-on-success) exercise the Task 1/2 implementation directly — no test passes vacuously.

## Threat Flags

None. The plan's `<threat_model>` mitigations are all present:

- **T-81-05-01** (forged agent/file identity) — `agent` from `Depends(get_authenticated_agent)`; both the marker `file_id` and the ledger key are reconstructed from the PATH `file_id` only, never the body.
- **T-81-05-02** (mixed row violating the invariant) — the writer clears `analysis_completed_at` when stamping `failed_at`; `put_analysis` clears `failed_at` on success; the migration-033 DB CHECK is the backstop. Proven by the no-mixed-row assertion in every test.
- **T-81-05-03** (oversized/PG-invalid error free text) — `error` bounded via `AnalysisFailurePayload(max_length=2000)`; `error_message` truncated to `_ERROR_MESSAGE_MAX` before persist.
- **T-81-05-SC** (package installs) — accepted; zero new dependencies, no installs performed.

No new network endpoints, auth paths, file-access patterns, or trust-boundary schema surface introduced.

## Commits

| Task | Commit | Message |
|------|--------|---------|
| 1 | `cc24e38c` | `feat(81-05): dual-write durable analyze failure marker in report_analysis_failed` |
| 2 | `a787caa1` | `feat(81-05): put_analysis clears failed_at/error_message on success` |
| 3 | `ffe94492` | `test(81-05): prove analyze failure dual-write, no-prior-row upsert, XOR CHECK, clear-on-success` |

All commits landed on `worktree-agent-a32a1760c61b1649e` (worktree branch). None on `main` or `SimplicityGuy/phase-81`.

## Self-Check: PASSED

Both files exist on disk; all three task commits resolve in `git log`.
