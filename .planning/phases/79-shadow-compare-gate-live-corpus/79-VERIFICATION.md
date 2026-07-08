---
phase: 79-shadow-compare-gate-live-corpus
verified: 2026-07-08T00:00:00Z
status: passed
score: 11/11 code-verifiable truths verified; phase deliverable (the committed re-runnable gate) complete
overrides_applied: 0
deferred_manual_verification:
  - test: "Gate passes on a restore of the live ~200K-file corpus after the `032` backfill"
    expected: "`just shadow-compare --database-url <restore-dsn>` (or `python -m phaze.cli.shadow_compare --database-url <restore-dsn>`) exits 0, or exits 1 only on FINGERPRINTED/LOCAL_ANALYZING (soft) divergence with all HARD invariants at zero"
    blocking: false
    tracked_in: [79-HUMAN-UAT.md, 79-VALIDATION.md]
    note: "Not a phase-79 deliverable gap: MIG-02 ships the committed re-runnable gate (done + verified). Running it against the live corpus is an OPERATIONAL precondition for Phase 90's destructive `033`, deliberately scoped out of Phase 79 by decision D-02 (no live dump exists in this worktree; standard deployment-gated pattern). Deferred to the next homelab rollout; its output is appended here before `033` proceeds."
---

# Phase 79: Shadow-Compare Gate (live corpus) Verification Report

**Phase Goal:** A committed, re-runnable implication check between legacy `files.state` and the derived representation (state↔derived shadow-compare gate); must pass before any reader cutover and before the destructive `033` (Phase 90). Requirement MIG-02.
**Verified:** 2026-07-08
**Status:** passed
**Re-verification:** Yes — reconciled 2026-07-08 (see note below)

## Goal Achievement

All code-level truths (registry shape, gate semantics, dual entry points, CLI/justfile wiring) are directly verified against the codebase and by re-running the real test suite against a live ephemeral Postgres — not merely inferred from SUMMARY.md. MIG-02's deliverable — *a committed, re-runnable shadow-compare check* — is fully built and verified. The live 200K-corpus restore run (ROADMAP Success Criterion 3) is NOT a Phase-79 deliverable gap: it is an OPERATIONAL precondition for Phase 90's destructive `033`, deliberately scoped out of this phase by decision D-02 (no live dump exists in this worktree). It is tracked as a deferred manual verification in `79-HUMAN-UAT.md` and `79-VALIDATION.md` and must be recorded before `033` proceeds.

**Status reconciliation (2026-07-08):** the initial verifier returned `human_needed` because a human/homelab action remains outstanding. After the user elected to mark Phase 79 complete with that item tracked as a deferred operational precondition, this report was reconciled to `status: passed` so the three tracking artifacts agree (ROADMAP `[x]` + REQUIREMENTS `MIG-02 Complete` + VERIFICATION `passed`) — which the `test_requirements_traceability` drift guard (DOCS-01) requires. The deferred live-corpus run is preserved verbatim below and in `deferred_manual_verification` frontmatter; it is non-blocking for phase closure but blocking for Phase 90.

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | `run_shadow_compare(session, *, sample_cap, verbose)` returns a `Report` with per-invariant divergent count + capped `file_id` sample + `hard_fail_total` (D-05) | ✓ VERIFIED | `src/phaze/services/shadow_compare.py:200-221`; `test_report_shape_respects_sample_cap_and_verbose` passes live against PG (capped at 2, uncapped at verbose=True) |
| 2 | `INVARIANTS` has one entry per FileState value except `DISCOVERED` (documented-vacuous comment); soft allowlist is exactly `{fingerprinted, local_analyzing}` (D-04/D-06) | ✓ VERIFIED | `shadow_compare.py:123-152` (16 entries, `DISCOVERED` only in comment at :118-121); `FileState` enum (`src/phaze/models/file.py:20-71`) has 17 members, 17-1=16 matches; `test_core_registry_shape_locks_coverage_and_allowlist` passes (asserts exact set equality + no duplicates) |
| 3 | A seeded divergent row (state=X, no backing derived row) is counted and drives `hard_fail_total > 0` for every HARD invariant | ✓ VERIFIED | `test_divergent_hard_invariant_flags` parametrized over 14 HARD invariants — all 14 PASSED against real PG (`localhost:5433/phaze_test`) |
| 4 | A consistent corpus AND a more-derived-than-scalar file both yield zero HARD divergence (implication, not equality) | ✓ VERIFIED | `test_consistent_hard_invariant_clean` (14 parametrized cases) + `test_implication_more_derived_than_scalar_does_not_flag` — all PASSED live |
| 5 | A seeded FINGERPRINTED / LOCAL_ANALYZING divergence is counted and printed as expected divergence but never contributes to `hard_fail_total` | ✓ VERIFIED | `test_allowlist_soft_divergence_counted_but_not_gated` PASSED; `false()` soft placeholder at `shadow_compare.py:150-151`, `Report.hard_fail_total` sums only `not r.soft` (:174-176) |
| 6 | The derived side reuses `done_clause`/`failed_clause` and NEVER `stage_status_case` (D-03) | ✓ VERIFIED | `shadow_compare.py:54` imports `done_clause, failed_clause` from `phaze.services.stage_status`; `grep -n stage_status_case` on the module matches only the negated docstring sentence at line 18 |
| 7 | Apply-outcome states (MOVED/UNCHANGED/EXECUTED/FAILED/APPROVED/REJECTED) query `RenameProposal.status`, not `ExecutionLog`; PUSHING/PUSHED assert only `exists(CloudJob WHERE file_id)`; AWAITING_CLOUD filters `status=='awaiting'` | ✓ VERIFIED | `shadow_compare.py:90-97` (`_proposal_status`), `:131-134` (`_cloud_job_exists` no status filter for pushing/pushed), `:80-82` (`_cloud_awaiting` exact status). No `ExecutionLog` import in the module at all |
| 8 | `python -m phaze.cli.shadow_compare` runs the SAME `run_shadow_compare` core — no duplicated assertion logic | ✓ VERIFIED | `cli/shadow_compare.py:32` imports `run_shadow_compare`; module is 105 lines with zero invariant/predicate logic; `uv run mypy`/`ruff` clean |
| 9 | The CLI exits nonzero when any HARD invariant diverges and zero on a clean corpus (D-05) | ✓ VERIFIED | `test_cli_main_exits_nonzero_on_hard_divergence` and `test_cli_main_exits_zero_on_clean_corpus` PASSED live (drive `main()` against a committed real-PG corpus) |
| 10 | `--sample-cap`, `--verbose`, `--database-url` flags thread through; CLI never prints the full DSN | ✓ VERIFIED | `uv run python -m phaze.cli.shadow_compare --help` lists all 3 flags; `_safe_target()` uses `make_url(...).host`/`.database` only (`cli/shadow_compare.py:59-65`); no code path calls `print`/`logger` with the raw `database_url` string |
| 11 | `just shadow-compare *ARGS` in the `db` group invokes `uv run python -m phaze.cli.shadow_compare` | ✓ VERIFIED | `justfile:482-485`: `[doc(...)]`/`[group('db')]`/`shadow-compare *ARGS:` → `uv run python -m phaze.cli.shadow_compare {{ ARGS }}`; `just --evaluate` parses clean |
| SC-3 | The gate passes on a restore of the live corpus after the `032` backfill, output recorded in VERIFICATION (ROADMAP Success Criterion 3) | ? HUMAN NEEDED | Not code-verifiable from this worktree — no live corpus dump available. Explicitly and deliberately deferred to homelab per `79-CONTEXT.md` D-02 and tracked in `79-VALIDATION.md` "Manual-Only Verifications" table |

**Score:** 11/11 code-verifiable truths verified; 1 truth (SC-3) requires a human/homelab action, tracked as deferred by design (D-02), not a code defect.

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/services/shadow_compare.py` | `INVARIANTS` registry + `Invariant`/`Report` dataclasses + async `run_shadow_compare` (min 120 lines) | ✓ VERIFIED | 221 lines; `INVARIANTS` present; `ruff check` + `mypy` both exit 0; 100% line coverage measured live (`--cov=phaze.services.shadow_compare` → 63/63 stmts) |
| `tests/integration/test_shadow_compare.py` | Hermetic fixture-corpus CI gate, `pytest.mark.integration` | ✓ VERIFIED | 362 lines; `pytestmark = pytest.mark.integration` present; 34 tests collected, 34 PASSED live against `localhost:5433/phaze_test` |
| `src/phaze/cli/shadow_compare.py` | Thin argparse runner, min 30 lines, contains `run_shadow_compare` | ✓ VERIFIED | 105 lines; imports `run_shadow_compare`; `ruff`/`mypy` clean |
| `justfile` | `[group('db')] shadow-compare` recipe | ✓ VERIFIED | Recipe present at line 482-485; `just --evaluate` and `just shadow-compare --help` both confirmed working |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|----|--------|---------|
| `shadow_compare.py` | `stage_status.py` | `done_clause`/`failed_clause` import | ✓ WIRED | `from phaze.services.stage_status import done_clause, failed_clause` at line 54; used in `INVARIANTS` entries at :125-129 |
| `test_shadow_compare.py` | `shadow_compare.py` | `run_shadow_compare`/`INVARIANTS` import | ✓ WIRED | Line 46: `from phaze.services.shadow_compare import INVARIANTS, Invariant, InvariantResult, Report, run_shadow_compare`; exercised across all 34 test cells |
| `cli/shadow_compare.py` | `services/shadow_compare.py` | `run_shadow_compare` import | ✓ WIRED | Line 32; called in `_run()` at :77 and :83 |
| `justfile` | `cli/shadow_compare.py` | `python -m phaze.cli.shadow_compare` | ✓ WIRED | `justfile:485`; confirmed runnable via `uv run python -m phaze.cli.shadow_compare --help` |

### Data-Flow Trace (Level 4)

Not applicable in the standard UI-data-flow sense — this is a batch/CLI assertion tool, not a component rendering derived data. The equivalent trace (does the SELECT anti-join actually run against real rows and produce non-vacuous counts) was directly exercised: `test_divergent_hard_invariant_flags` seeds a real row with no backing table row and asserts `count >= 1` and the seeded `file_id` appears in the sample — this was independently re-run against a live PG instance in this verification pass (not just trusted from SUMMARY.md), and passed for all 14 HARD invariants.

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Full integration bucket green in isolation | `just test-bucket integration` (with `TEST_DATABASE_URL`/`MIGRATIONS_TEST_DATABASE_URL` set to `:5433`) | `130 passed` | ✓ PASS |
| `shadow_compare.py` module coverage | `pytest tests/integration/test_shadow_compare.py --cov=phaze.services.shadow_compare` | `63/63 stmts, 100.00%` | ✓ PASS |
| CLI help lists all 3 flags | `uv run python -m phaze.cli.shadow_compare --help` | lists `--sample-cap`, `--verbose`, `--database-url` | ✓ PASS |
| justfile parses | `just --evaluate` | exits 0 | ✓ PASS |
| Ruff + mypy on touched files | `uv run ruff check ...` / `uv run mypy ...` | `All checks passed!` / `Success: no issues found in 2 source files` | ✓ PASS |

Note: a combined `--cov=phaze.services.shadow_compare --cov=phaze.cli.shadow_compare` invocation crashed with a native-code segfault trace inside CPython 3.14.5's tail-call interpreter (`async_gen_asend_send`/`task_step_impl` frames) — this reproduced identically regardless of which shadow-compare module was targeted second, and disappeared when covering one target at a time or when running the same tests via the project's own `just test-bucket integration` recipe (which passed cleanly, 130 passed). This is assessed as a coverage.py/CPython-3.14-interpreter interaction artifact of this local environment, not a defect in the phase's code — the identical test file passes both with and without coverage instrumentation, and the project's own recipe (which is what CI runs) is unaffected.

### Probe Execution

Step 7c N/A — this is not a migration/tooling phase with declared `scripts/*/tests/probe-*.sh` probes. The equivalent CLI-exit contract is covered under Behavioral Spot-Checks and Observable Truths #8-9 instead.

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|-------------|--------|----------|
| MIG-02 | 79-01-PLAN.md, 79-02-PLAN.md | "A committed, re-runnable shadow-compare check asserts per-file implication invariants... with FINGERPRINTED documented as the one expected divergence; it must pass before any reader cutover and before the destructive migration." | ✓ SATISFIED (code) / ? PENDING (live-corpus run, D-02) | Registry, gate semantics, dual entry points (pytest + CLI) all implemented and passing live; the live-corpus pass itself is the deferred SC-3 item above. `.planning/REQUIREMENTS.md:160` still shows `MIG-02 \| Phase 79 \| Pending` — a documentation bookkeeping lag (consistent with MIG-01/MIG-03 which were flipped to Complete only once their phases fully closed); not itself evidence of a code gap |

No orphaned requirements: `grep "Phase 79" .planning/REQUIREMENTS.md` returns only the `MIG-02` row.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `src/phaze/services/shadow_compare.py` | 148 | `placeholder` (in "The benign `false()` placeholder makes NO derived claim...") | ℹ️ Info | Not a stub — this is a deliberate, documented, tested design choice (the soft-allowlist `false()` predicate exists specifically so every soft-state row surfaces as expected divergence); covered by `test_allowlist_soft_divergence_counted_but_not_gated`. No debt marker (`TBD`/`FIXME`/`XXX`) found in any file touched by this phase |

No blocker-level anti-patterns found. No `TODO`/`HACK`/`PLACEHOLDER`/"not yet implemented" markers in any of the three phase-created files.

### Human Verification Required

### 1. Live 200K-corpus shadow-compare run

**Test:** After the next homelab rollout, `pg_restore` the live production corpus into a scratch DB, then run `just shadow-compare --database-url <restore-dsn>` (or `python -m phaze.cli.shadow_compare --database-url <restore-dsn>`).
**Expected:** All HARD invariants report zero divergence (`hard_fail_total == 0`); only `FINGERPRINTED`/`LOCAL_ANALYZING` (soft) may show counted, non-gating divergence. Record the full `report.render(verbose=True)` output and pass/fail in this VERIFICATION.md (or a follow-up VERIFICATION addendum) before Phase 90's destructive `033` migration is allowed to proceed.
**Why human:** No live corpus dump is available inside this development worktree/verifier sandbox. This was deliberately scoped out of Phase 79 per `79-CONTEXT.md` decision D-02 ("the live 200K-corpus restore run is deferred to the next homelab rollout... consistent with this project's other deployment-gated UAT items") and is tracked as the sole entry in `79-VALIDATION.md`'s "Manual-Only Verifications" table. This directly corresponds to ROADMAP Success Criterion 3 for Phase 79.

### Gaps Summary

No code-level gaps found. Every artifact, key link, and locally-testable observable truth for the shadow-compare gate (registry comprehensiveness, implication-not-equality semantics, soft-allowlist behavior, D-03 reuse discipline, dual entry points, exit-code contract, DSN-safety, justfile wiring) was independently re-verified against the codebase — including re-running the full `tests/integration/test_shadow_compare.py` suite (34/34 passed) and the full `integration` bucket (130/130 passed) against a live ephemeral Postgres instance, not merely trusting SUMMARY.md's claims. Static checks (`ruff`, `mypy`) are clean and module coverage is 100%.

The sole open item is ROADMAP Success Criterion 3 (live-corpus restore run), which is a genuine, currently-unsatisfiable-from-this-environment precondition rather than a coding gap — it was scoped as a deliberate deferral (D-02) at planning time and is the standard pattern this project uses for deployment-gated verification (homelab rollout + redeploy). It must be performed and its result appended to VERIFICATION before Phase 90 (the destructive `033` migration) proceeds, per both the ROADMAP success criteria and the phase's own stated purpose.

---

*Verified: 2026-07-08*
*Verifier: Claude (gsd-verifier)*
