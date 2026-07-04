---
phase: 69-tiered-drain-scheduler
verified: 2026-07-04T15:28:48Z
status: passed
score: 5/5 must-haves verified
overrides_applied: 0
re_verification:
  previous_status: gaps_found
  previous_score: 3/5
  gaps_closed:
    - "Each AWAITING_CLOUD file is dispatched to the available backend with lowest rank whose in_flight_count() < cap, per-candidate; local (rank 99) reached only when every higher-ranked backend is full/offline."
    - "A backend going offline or a job failing mid-flight returns the file to AWAITING_CLOUD; next tick re-dispatches to next eligible backend against CURRENT availability; a black-hole/cooldown guard prevents infinite thrash (bounded attempts -> ANALYSIS_FAILED or local terminal)."
  gaps_remaining: []
  regressions: []
deferred:
  - truth: "config._validate_registry should require >=1 kind==local backend when cloud_enabled (WR-02)"
    addressed_in: "Phase 70"
    evidence: "69-05-PLAN.md 'Out of scope' section and 69-05-SUMMARY.md explicitly defer WR-02 to Phase-70 registry-validation territory; not part of SCHED-01..05's literal wording."
  - truth: "ComputeAgentBackend.reconcile should own recovery of a stuck compute PUSHING row instead of being a no-op (WR-03)"
    addressed_in: "Phase 70"
    evidence: "69-05-PLAN.md 'Out of scope' section and 69-05-SUMMARY.md explicitly defer WR-03 to Phase-70 robustness; SCHED-05's literal 'no second recovery path' wording is still satisfied (zero-owner is not a double-owner)."
human_verification: []
---

# Phase 69: Tiered Drain Scheduler Verification Report

**Phase Goal:** Long files drain across every eligible backend simultaneously, cheapest-rank-first with per-backend caps, spilling to the next rank when the preferred one is full or offline. The first behavior-changing phase.
**Verified:** 2026-07-04T15:28:48Z
**Status:** passed
**Re-verification:** Yes — after gap-closure plan 69-05 (CR-01 fix)

## Goal Achievement

### Observable Truths

| # | Truth (ROADMAP success criterion) | Status | Evidence |
|---|---|---|---|
| 1 | Rank-first per-candidate dispatch with spill to next rank; local reached only when all higher ranks full/offline | ✓ VERIFIED | `LocalBackend.dispatch` (src/phaze/services/backends.py:193-227) now flips `file.state = FileState.LOCAL_ANALYZING` in the caller session, after the fileserver gate and before the `process_file` enqueue, mirroring `ComputeAgentBackend.dispatch:252` / `KueueBackend.dispatch:316`'s `FileState -> PUSHING` flip. `get_cloud_staging_candidates` (pipeline.py:1253-1260) still selects only `state == AWAITING_CLOUD`, so a `LOCAL_ANALYZING` file no longer matches and is not re-selected on a later tick. Confirmed by direct code read AND by the new regression test `test_local_spill_not_redispatched_to_cloud` (tests/analyze/core/test_staging_cron.py:838-901), which spills a file to local (tick 1, compute offline), then brings compute online with a free slot (tick 2), and asserts `result2 == {"staged": 0, "skipped": 0}`, state stays `LOCAL_ANALYZING`, and the file's `cloud_job` row COUNT is 0 — independently re-run and green. |
| 2 | Global window replaced by per-backend cap, enforced by count-and-claim under `pg_advisory_xact_lock` — no overshoot across overlapping drain/reconcile ticks | ✓ VERIFIED (no regression) | Unchanged since initial verification: `stage_cloud_window` takes `pg_advisory_xact_lock(5_000_504)`, snapshots availability/in-flight once, decrements a local `remaining[]`, single post-loop commit; `KueueBackend.reconcile` shares the same lock key. Re-confirmed by direct read of `src/phaze/tasks/release_awaiting_cloud.py` and `src/phaze/services/backends.py:325-380` — no code in these paths was touched by 69-05. |
| 3 | Offline/mid-flight-failure returns file to AWAITING_CLOUD; next tick re-dispatches against current availability; black-hole/cooldown guard bounds attempts to ANALYSIS_FAILED or local terminal | ✓ VERIFIED | Same fix as truth 1 closes this: local is now a genuine drain-terminal target — a file in `LOCAL_ANALYZING` is not a drain candidate, so the black-hole guard's "bounded attempts → local terminal" outcome is actually terminal (no thrash, no cross-backend double-analysis). Compute/kueue spill-back-to-`AWAITING_CLOUD`-at-cap paths (`agent_push.report_push_mismatch`, `agent_s3.report_upload_failed`, `reconcile_cloud_jobs._handle_no_callback_terminal`) are unchanged and still bounded/tested. |
| 4 | Equal-rank backends tie-broken deterministically + statelessly (lowest utilization, then stable id) | ✓ VERIFIED (no regression) | `select_backend` step 5 (backend_selection.py:117-119) unchanged: `eligible.sort(key=lambda slot: (slot["backend"].rank, _utilization(slot), slot["backend"].id))`. File untouched by 69-05; re-confirmed by direct read. |
| 5 | Exactly one recovery owner per backend kind; no cloud-owned file gains a second recovery path | ✓ VERIFIED (no regression; WR-03 caveat still tracked, deferred to Phase 70) | `reconcile_cloud_jobs` still dispatches per-backend via `resolve_backends`; `_in_flight_cloud_job_ids` (reenqueue.py:204-219) still excludes any file with a live `cloud_job` row. `LOCAL_ANALYZING` files carry NO `cloud_job` row (confirmed: `LocalBackend.dispatch` writes none), so they are unaffected by this exclusion and remain covered by the ordinary ledger recovery path if a local agent dies mid-analysis — verified against `_select_done_analyze_ids`/`_select_done_push_ids` (neither includes `LOCAL_ANALYZING`). WR-03 (compute `reconcile` no-op leaving a zero-owner gap for a lost `push_file`) remains an open robustness item, explicitly deferred to Phase 70 per 69-05's plan — it does not violate SCHED-05's literal "no second recovery path" wording (zero-owner ≠ double-owner). |

**Score:** 5/5 truths verified

### Deferred Items

Items not required by this phase's literal success criteria wording, explicitly tracked for Phase 70.

| # | Item | Addressed In | Evidence |
|---|------|-------------|----------|
| 1 | WR-02: no whole-registry invariant requiring ≥1 `kind == "local"` backend when `cloud_enabled` | Phase 70 | 69-05-PLAN.md "Out of scope" section: "Pre-existing / Phase-70 registry-validation territory." |
| 2 | WR-03: `ComputeAgentBackend.reconcile` no-op leaves a stuck compute `PUSHING` row with zero recovery owner | Phase 70 | 69-05-PLAN.md "Out of scope" section: "Broader robustness; Phase-70 territory." |

### Required Artifacts

| Artifact | Expected | Status | Details |
|---|---|---|---|
| `src/phaze/models/file.py` (`FileState.LOCAL_ANALYZING`) | Code-only StrEnum value, no migration | ✓ VERIFIED | Line 63: `LOCAL_ANALYZING = "local_analyzing"` (15 chars ≤ `String(30)`), with a doc-comment matching the ANALYSIS_FAILED/AWAITING_CLOUD/PUSHING precedent (lines 54-63). |
| `src/phaze/services/backends.py` (`LocalBackend.dispatch`) | Flips state before enqueue; returns `job is not None` | ✓ VERIFIED | Lines 193-227: fileserver gate first (clean hold on `NoActiveAgentError` → `return False`, nothing mutated), then `file.state = FileState.LOCAL_ANALYZING`, then `job = await enqueue_process_file(...)`, then `return job is not None`. No `session.commit()` in the method (drain owns the single post-loop commit). |
| `tests/analyze/services/test_backends.py` | State-flip + candidate-exclusion + WR-01 return-value tests | ✓ VERIFIED | `test_local_dispatch_flips_to_local_analyzing` (258), `test_local_dispatch_excluded_from_staging_candidates` (278), `test_local_dispatch_returns_true_on_enqueue` (303), `test_local_dispatch_returns_false_on_dedup_noop` (316) — all present, all pass. |
| `tests/analyze/core/test_staging_cron.py` | Two-tick spill-to-local-then-cloud-frees regression | ✓ VERIFIED | `test_local_spill_not_redispatched_to_cloud` (838-901) — present, passes, asserts exactly the verifier scenario (cloud_job count 0, state LOCAL_ANALYZING after tick 2). |

### Key Link Verification

| From | To | Via | Status | Details |
|---|---|---|---|---|
| `backends.LocalBackend.dispatch` | `pipeline.get_cloud_staging_candidates` (candidate exclusion) | `file.state = FileState.LOCAL_ANALYZING` flip removing it from the `AWAITING_CLOUD` predicate | ✓ WIRED | Confirmed by direct read of both files: dispatch flips state in the caller session before enqueue; the candidate query still filters `state == FileState.AWAITING_CLOUD`, so a flipped file no longer matches. Regression-tested end-to-end via the drain (`test_local_spill_not_redispatched_to_cloud`) and unit-tested directly (`test_local_dispatch_excluded_from_staging_candidates`). |
| `backends.LocalBackend.dispatch` | `tasks/reenqueue._select_done_analyze_ids` / `_select_done_push_ids` | `LOCAL_ANALYZING` deliberately absent from both done-sets | ✓ WIRED | Confirmed: `_select_done_analyze_ids` = `{ANALYZED, ANALYSIS_FAILED}`; `_select_done_push_ids` = `{PUSHED, ANALYZED, ANALYSIS_FAILED}` — neither includes `LOCAL_ANALYZING`, so a lost local job stays recoverable via the scheduling ledger rather than being falsely treated as done. |
| `release_awaiting_cloud.stage_cloud_window` | `backend_selection.select_backend` / `backends.resolve_backends` | unchanged since initial verification | ✓ WIRED | Re-confirmed, no code touched by 69-05. |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|---|---|---|---|---|
| SCHED-01 | 69-01, 69-02, 69-05 | Rank-first per-candidate dispatch, spill to next rank | ✓ SATISFIED | Selection policy correct (69-01/02, unchanged); local-leg exclusivity now closed (69-05, CR-01 fix verified above). |
| SCHED-02 | 69-02, 69-03 | Per-backend cap via count-and-claim under advisory lock | ✓ SATISFIED | Unchanged, no regression (verified end-to-end previously; re-confirmed this pass). |
| SCHED-03 | 69-01, 69-03, 69-04, 69-05 | Offline/mid-flight-failure spill-back + black-hole guard bounding to ANALYSIS_FAILED or local terminal | ✓ SATISFIED | Compute/kueue spill-back paths correct and tested (unchanged); "local terminal" is now actually terminal (69-05 CR-01 fix). |
| SCHED-04 | 69-01 | Deterministic stateless tie-break (utilization, then id) | ✓ SATISFIED | Verified in `select_backend` + unit tests; unchanged. |
| SCHED-05 | 69-03, 69-04 | Exactly one recovery owner per backend kind | ✓ SATISFIED (WR-03 caveat, deferred to Phase 70) | No double-owner path found; WR-03 zero-owner robustness gap explicitly tracked as a Phase-70 follow-up, not a literal SCHED-05 violation. |

REQUIREMENTS.md's tracking-table checkbox rows for SCHED-01..05 (lines 89-93) still show `Pending` — this is a documentation bookkeeping field, not a code gap; it is normally flipped to `Done` during `/gsd:complete-milestone`. Flagging for the record, not as a verification gap (the ROADMAP success criteria, which are the binding contract per the verification methodology, are all met).

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|---|---|---|---|---|
| — | — | No `TBD`/`FIXME`/`XXX`/`TODO`/`HACK`/`PLACEHOLDER` markers found in any file modified by 69-05 (`src/phaze/models/file.py`, `src/phaze/services/backends.py`, `tests/analyze/services/test_backends.py`, `tests/analyze/core/test_staging_cron.py`) | — | — | — |

The two previously-flagged Blocker/Warning anti-patterns from the initial review (LocalBackend.dispatch omitting the state-flip; ignoring the enqueue return value) are both resolved by this gap-closure plan. WR-02/WR-03 remain as tracked, deferred, non-blocking warnings (see Deferred Items).

### Behavioral Spot-Checks / Test Execution

Full phase-touched test suite independently re-run by this verifier (not taken from SUMMARY.md) against the live `phaze-test-db`/`phaze-test-redis` containers:

| Behavior | Command | Result | Status |
|---|---|---|---|
| Full phase-touched suite (141 baseline + 5 new gap-closure tests) | `TEST_DATABASE_URL=postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test uv run pytest tests/analyze/services/test_backend_selection.py tests/analyze/services/test_backends.py tests/analyze/core/test_staging_cron.py tests/analyze/tasks/test_reconcile_cloud_jobs.py tests/analyze/tasks/test_recovery.py tests/shared/config/test_cloud_spill_to_local.py tests/agents/routers/test_agent_push.py tests/agents/routers/test_agent_s3.py -q` | 146 passed, 1 warning | ✓ PASS |
| Ruff lint on modified files | `uv run ruff check src/phaze/services/backends.py src/phaze/models/file.py` | All checks passed | ✓ PASS |
| Mypy on modified files | `uv run mypy src/phaze/services/backends.py src/phaze/models/file.py` | Success: no issues found in 2 source files | ✓ PASS |
| Commits present in git log | `git log --oneline` | `4f44e7c` (test), `333e990` (feat CR-01), `c973a03` (feat WR-01) all present, RED-before-GREEN order confirmed | ✓ PASS |

### Probe Execution

No `scripts/*/tests/probe-*.sh` files exist in this repository and no probe is referenced in any Phase-69 PLAN/SUMMARY. Step 7c: SKIPPED (no probes declared or discovered).

### Human Verification Required

None. The CR-01 fix and its regression coverage are fully diagnosable and verifiable by direct code read plus an independently-re-run test suite.

### Gaps Summary

Both previously-failed success criteria (1 and 3) shared a single root cause: `LocalBackend.dispatch` never removed a locally-spilled file from the `AWAITING_CLOUD` candidate set, so it was re-selected on every subsequent drain tick and could be double-dispatched to a cloud backend while its local `process_file` was still running.

Gap-closure plan 69-05 fixes this with a new `FileState.LOCAL_ANALYZING` (code-only, no migration) that `LocalBackend.dispatch` flips into, in the caller's session, before the enqueue — mirroring the `PUSHING` flip already used by `ComputeAgentBackend`/`KueueBackend`. This verifier independently confirmed, by direct code read (not by trusting 69-05-SUMMARY.md's claims):

1. `FileState.LOCAL_ANALYZING` exists and fits the `String(30)` column.
2. `LocalBackend.dispatch` performs the flip in the right place (after the fileserver gate, before enqueue, no commit) and now returns `job is not None` (WR-01 folded in).
3. `get_cloud_staging_candidates` still selects only `AWAITING_CLOUD`, so a `LOCAL_ANALYZING` file is provably excluded from re-selection.
4. `LOCAL_ANALYZING` is absent from both `_select_done_analyze_ids` and `_select_done_push_ids`, so a lost local job still re-drives via the recovery ledger (no new stranding introduced).
5. The regression tests (`test_local_dispatch_flips_to_local_analyzing`, `test_local_dispatch_excluded_from_staging_candidates`, `test_local_dispatch_returns_true_on_enqueue`, `test_local_dispatch_returns_false_on_dedup_noop`, `test_local_spill_not_redispatched_to_cloud`) exist, are substantive (not stubs — they assert state, candidate-set membership, return values, and cloud_job row counts against a real DB), and pass when independently re-run by this verifier (146/146, not just claimed by the executor).
6. Criteria 2, 4, and 5 — previously verified — are unaffected: none of their supporting files were touched by 69-05, and the full regression suite covering them is still green.

WR-02 and WR-03 remain open but are explicitly out-of-scope items tracked for Phase 70 per 69-05's plan; they do not contradict the literal wording of SCHED-01..05 and are recorded here as deferred, non-blocking follow-ups rather than gaps.

All 5 ROADMAP success criteria for Phase 69 are now verified. The phase goal — long files draining across every eligible backend simultaneously, cheapest-rank-first with per-backend caps, spilling to the next rank when full/offline, with local as a genuine terminal safety net — is achieved.

---

_Verified: 2026-07-04T15:28:48Z_
_Verifier: Claude (gsd-verifier)_
