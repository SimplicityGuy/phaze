---
phase: 83-cloud-routing-sidecar-cutover
verified: 2026-07-09T23:59:00Z
status: passed
score: 7/7 must-haves verified
overrides_applied: 0
re_verification:
  previous_status: gaps_found
  previous_score: 6/7
  gaps_closed:
    - "D-01/D-02: a single go-forward writer of cloud_job.status='awaiting' exists and is shared, not hand-copied three times"
  gaps_remaining: []
  regressions: []
---

# Phase 83: Cloud-Routing Sidecar Cutover Verification Report

**Phase Goal:** Cloud routing (`AWAITING_CLOUD`/`PUSHING`/`PUSHED`/`LOCAL_ANALYZING`) via the `cloud_job`
sidecar / derived `in_flight(analyze)`, one atomic consistency domain, CAS-guard collapse (closes the
missing `/upload-failed` guard) (SIDECAR-01)
**Verified:** 2026-07-09T23:59:00Z
**Status:** passed
**Re-verification:** Yes — after gap closure (plan 83-07)

## Goal Achievement

### Observable Truths (ROADMAP Success Criteria + PLAN must-haves)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | SC#1 — drain query, dispatch, callbacks all read/write `cloud_job`/`in_flight`, no `FileRecord.state` routing read | VERIFIED (regression check) | Unchanged since prior verification. `get_cloud_staging_candidates` still joins `CloudJob` and filters on the sidecar, zero `FileRecord.state` predicate. Re-confirmed by full `tests/shared/routers/test_pipeline.py` (114 passed, isolated run). |
| 2 | SC#2 — `report_upload_failed` CAS guard closing the `agent_s3.py:195` clobber bug, proven by a regression test | VERIFIED | `test_upload_failed_cas_noop_on_advanced_cloud_job` (`tests/agents/routers/test_agent_s3.py:670`) — unmodified assertions, now exercises the code path through `hold_awaiting_cloud`'s CAS branch. Ran live: PASS. |
| 3 | SC#3 (D-08 HARD GATE) — shadow-compare gate stays green, no double-dispatch/re-pick window | VERIFIED | `tests/integration/test_drain_double_dispatch.py` — 3/3 passed live in isolation (`test_sc3_case_a/b/c`). `tests/shadow_compare` tests unaffected (verified live). |
| 4 | D-12 — all four callback CAS guards anchor per backend kind, no universal PUSHING/PUSHED predicate | VERIFIED | Unchanged; additionally re-verified that the spill branch's `clear_cloud_phase` flag correctly differentiates the s3 (`True`) vs push (omitted) anchors (`test_hold_awaiting_cloud_spill_preserves_cloud_phase_when_flag_omitted`, PASS). |
| 5 | D-14 — reaper DELETEs inert `awaiting` rows at both analyze-terminal seams | VERIFIED (regression check) | Untouched by 83-07; part of the green 452-test `agents` bucket run. |
| 6 | Hard invariant `AWAITING_CLOUD ⇒ cloud_job(status='awaiting')` — go-forward writer + existing-corpus repair | VERIFIED (regression check) | Hold-path behavior byte-identical (`test_hold_awaiting_cloud_hold_branch_returns_true` — unconditional upsert + `file.state` dual-write, returns `True`). Migration 034 untouched. |
| 7 | **D-01/D-02 — the awaiting writer is a SINGLE shared writer, not three hand-copied writers** (LOCKED constraint, the gap closed by 83-07) | **VERIFIED** | `hold_awaiting_cloud()` (`src/phaze/services/backends.py:86-151`) is now dual-mode: `expect_status is None` → the unconditional hold-path upsert (unchanged); `expect_status` set → a rowcount-guarded CAS spill branch. Both `report_upload_failed` (`routers/agent_s3.py:212-218`) and `report_push_mismatch` (`routers/agent_push.py:285-290`) call it directly — confirmed by direct read of both call sites. `grep -c "CloudJobStatus.AWAITING.value"` returns **0** in both `agent_s3.py` and `agent_push.py` (independently re-run, matches SUMMARY claim). `grep -c "hold_awaiting_cloud"` is ≥1 in both. The hermetic AST anti-drift test (`tests/analyze/services/test_single_awaiting_writer.py`) asserts the writer set equals exactly `{services/backends.py}` and passes live. |

**Score:** 7/7 truths verified — the one gap from the prior verification (truth #7) is now closed.

### Deferred Items

None new. `deferred-items.md` entries `83-01`, `83-03` (pre-existing non-hermetic `pk_agents` / bucket-pollution test-infra flakes) and `83-06` (backfill-held compute file mis-routes to local, not stranded) remain correctly out of scope for this gap-closure plan — re-confirmed not to be new regressions (see Anti-Patterns / regression discussion below).

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/services/backends.py` | `hold_awaiting_cloud()` dual-mode CAS-preserving writer | VERIFIED | Signature `(session, file, *, attempts=0, expect_status: Sequence[str] \| None = None, clear_cloud_phase: bool = False) -> bool`. Hold branch: unconditional upsert + `file.state` dual-write, `return True`. Spill branch: `update(CloudJob).where(file_id=…, status.in_(expect_status)).values(**values)`, `return res.rowcount > 0`. Never commits in either mode (Landmine L1 — confirmed no `session.commit()` call in the function body). |
| `src/phaze/routers/agent_s3.py` | `report_upload_failed` spill routed through `hold_awaiting_cloud` | VERIFIED | Line 212: `cleared = file is not None and await hold_awaiting_cloud(session, file, attempts=settings.cloud_submit_max_attempts, expect_status=(UPLOADING.value, UPLOADED.value), clear_cloud_phase=True)`. `pg_advisory_xact_lock` (D-11) at line 186 untouched. FULL no-op on `not cleared` verbatim (lines 219-230). |
| `src/phaze/routers/agent_push.py` | `report_push_mismatch` spill routed through `hold_awaiting_cloud` | VERIFIED | Line 285: `cleared = file is not None and await hold_awaiting_cloud(session, file, attempts=settings.cloud_submit_max_attempts, expect_status=(SUBMITTED.value,))` — no `clear_cloud_phase` (D-12). `pg_advisory_xact_lock` at line 249 untouched. FULL no-op verbatim (lines 291-300). |
| `tests/analyze/services/test_single_awaiting_writer.py` | Hermetic AST anti-drift test | VERIFIED + mutation-tested live | Resolves literal-keyword, dict-splat (`.values(**vals)`), and subscript-mutation (`vals["status"]=...`) forms; flags unresolvable splats only when the statement targets `CloudJob` AND the module references `AWAITING` (avoiding a false-positive on `services/proposal.py`'s unrelated `.values(**row)`, confirmed live — no AWAITING reference in that file). Independently re-confirmed by this verifier: injecting a synthetic `.values(**vals)` awaiting writer into `agent_push.py` turned this test RED; `git checkout --` restored the source cleanly (`git status` clean after). |
| `tests/analyze/services/test_backends.py` | `hold_awaiting_cloud` unit tests | VERIFIED | 4 new cases (hold-return-True, spill-CAS-hit-restamp-clears-phase, **spill-CAS-miss-full-noop**, spill-preserves-cloud_phase-when-omitted) all present and passing, matching the plan's `must_haves`. |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `routers/agent_s3.py report_upload_failed` (spill) | `services/backends.hold_awaiting_cloud` | `cleared = file is not None and await hold_awaiting_cloud(...)` | **WIRED** (was PARTIAL/inline in the prior verification) | Direct call confirmed at `agent_s3.py:212`. |
| `routers/agent_push.py report_push_mismatch` (spill) | `services/backends.hold_awaiting_cloud` | `cleared = file is not None and await hold_awaiting_cloud(...)` | **WIRED** (was PARTIAL/inline in the prior verification) | Direct call confirmed at `agent_push.py:285`. |
| `routers/pipeline.py trigger_analysis` | `services/backends.hold_awaiting_cloud` | direct call, `expect_status=None` | WIRED (unchanged) | Behavior-identical to pre-83-07. |
| `tests/analyze/services/test_single_awaiting_writer.py` | `src/phaze/**.py` | hermetic `ast.parse` scan | WIRED + non-vacuous | Confirmed RED on mutation (splat form), GREEN on clean source, no false-positive on readers/reaper/`proposal.py`. |

### Data-Flow Trace (Level 4)

Not applicable — same rationale as the initial verification: this phase changes routing/CAS predicates on
backend Postgres rows, not UI-rendered dynamic data. The spill CAS reads/writes real `cloud_job` rows via
live DB-backed tests (`tests/analyze/services/test_backends.py`, `tests/agents/routers/test_agent_s3.py`,
`tests/agents/routers/test_agent_push.py`), not static/hardcoded returns.

### Behavioral Spot-Checks / Live Test Runs (run live during THIS verification, not taken from SUMMARY.md)

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| D-02 sole-writer artifacts + unit tests | `uv run pytest tests/analyze/services/test_single_awaiting_writer.py tests/analyze/services/test_backends.py tests/agents/routers/test_agent_s3.py tests/agents/routers/test_agent_push.py -p no:randomly -q` | 92 passed | PASS |
| SC#3 hard gate (isolated, exact 3 named cases) | `uv run pytest tests/integration/test_drain_double_dispatch.py -p no:randomly -v` | `test_sc3_case_a/b/c` — 3 passed | PASS |
| Shadow-compare + drain hard gate (combined) | `uv run pytest tests/integration/test_drain_double_dispatch.py tests/integration/test_shadow_compare.py -p no:randomly -q` | 39 passed | PASS |
| Full `agents` bucket (SC#2/T-83-PUSH-CLOBBER regression surface) | `uv run pytest tests/agents -p no:randomly -q` | 452 passed | PASS |
| NULL-GUARD tests (named) | `uv run pytest tests/agents/routers/test_agent_s3.py::test_upload_failed_over_cap_null_guard_no_file_is_full_noop tests/agents/routers/test_agent_push.py::test_push_mismatch_over_cap_null_guard_no_file_is_full_noop -p no:randomly -v` | 2 passed | PASS |
| Mutation test: splat-form inline awaiting writer | Injected synthetic `.values(**vals)` writer into `agent_push.py`; ran `test_single_awaiting_writer.py`; restored via `git checkout --` | Test went RED with the mutation, GREEN after restore; `git status` clean post-restore | PASS (discriminating, confirmed non-vacuous) |
| Full project type check | `uv run mypy .` | Success: no issues found in 205 source files | PASS |
| Full project lint | `uv run ruff check .` | All checks passed | PASS |
| Full repo test suite (isolated ordering, single session) | `uv run pytest tests/ -p no:randomly -q` | 3136 passed, 4 failed, 17 errors, 561s | See regression analysis below |

**Full-suite failure/error triage:** All 4 failures + 17 errors are concentrated in `tests/shared/core/*`
and `tests/shared/routers/test_pipeline.py` / `test_pipeline_scans.py` / `test_pipeline_localqueue.py` —
none of which are files this phase (or 83-07) modified. Re-ran every failing/erroring module in isolation:
`tests/shared/core/test_migration_019_dedupe.py` + `test_task_split.py` → 18 passed; `tests/shared/routers/test_pipeline.py`
+ `tests/shared/core/test_routing_seam.py` → 114 passed. All pass cleanly in isolation, confirming this is
the documented pre-existing "local full-suite colima flake" / `pk_agents` cross-test-pollution class
(recorded in project memory and `deferred-items.md` 83-01/83-03) — not a regression introduced by 83-07.
The relevant regression surfaces for this gap-closure (agents bucket, integration bucket, analyze/services)
were run both as part of the full suite and independently in isolation, and are 100% green in both modes.

### Probe Execution

No `scripts/*/tests/probe-*.sh` files exist and none are declared in the PLAN/SUMMARY files for this phase.
Step 7c: SKIPPED (no probes declared or discovered) — unchanged from the initial verification.

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|--------------|------------|--------------|--------|----------|
| SIDECAR-01 | 83-01..07 | Cloud-routing status via `cloud_job` sidecar; CAS guards preserved/strengthened; closes `agent_s3.py:195` bug | **SATISFIED** (no caveat) | All three ROADMAP Success Criteria verified true in code + live tests, AND the plan-level D-01/D-02 single-writer must-have (the sole remaining gap from the prior verification) is now realized in code and self-enforced by a mutation-tested AST guard. |

No orphaned requirements: REQUIREMENTS.md line 149 maps only SIDECAR-01 to Phase 83; all 7 plans (including
83-07) declare `requirements: [SIDECAR-01]`.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `tests/analyze/services/test_single_awaiting_writer.py` | (resolved) | The WR-01 finding from `83-07-REVIEW.md` (guard blind to `**splat` awaiting writes) — **CLOSED** by follow-up commit `f6a969e4`, independently mutation-tested by this verifier | — (resolved, not a live finding) | No longer an issue; recorded here for audit continuity with the prior review. |
| `src/phaze/services/backends.py` | 563-566 | Duplicated comment block in `KueueBackend.reconcile` (pre-existing, confirmed NOT part of the 83-07 diff) | ℹ️ Info | Cosmetic, carried forward unchanged from `83-07-REVIEW.md` IN-01. |
| `src/phaze/services/backends.py` | 122-138 | Hold-mode `on_conflict_do_update` resets `attempts` on re-stamp (pre-existing behavior, retained; `83-07-REVIEW.md` IN-02) | ℹ️ Info | Not a regression — identical to the pre-83-07 helper; only reachable if `trigger_analysis` ever re-holds an already-spilled file, which the review notes may be unreachable given current call sites. |
| `src/phaze/services/pipeline.py` | `get_analyze_stage_files` (787-834) | WR-01 (already known, carried forward from the ORIGINAL 83-REVIEW.md): held/locally-spilled files display lane `"a1"` instead of `"local"` | ℹ️ Info | Display-only, explicitly excluded from 83-07's scope by operator decision. Not re-flagged as new. |

No `TBD`/`FIXME`/`XXX` markers found in any file touched by 83-07 (`backends.py`, `agent_s3.py`,
`agent_push.py`, `test_single_awaiting_writer.py`, `test_backends.py`, and the two router test modules).

### Human Verification Required

None. All items from the prior verification's deferred manual-only list (live-corpus shadow-compare run,
`EXPLAIN (ANALYZE, BUFFERS)` query-plan check) remain the same non-blocking, explicitly-deferred items from
`83-VALIDATION.md`'s "Manual-Only Verifications" table — unaffected by 83-07's scope (a pure consolidation of
existing writers, no new query-plan surface).

### Gaps Summary

**No gaps remain.** The single failed must-have from the prior verification — the LOCKED D-01/D-02
"single shared writer" invariant — is now realized in code:

- `hold_awaiting_cloud()` is the SOLE writer of `cloud_job.status='awaiting'`, called from all three
  go-forward sites (the hold path in `trigger_analysis`, and both over-cap spill paths in
  `report_upload_failed` / `report_push_mismatch`). Confirmed by direct code read, by `grep -c
  "CloudJobStatus.AWAITING.value"` returning 0 in both routers, and by the hermetic AST anti-drift test
  passing live.
- The rowcount-guarded CAS (SC#2 / T-83-PUSH-CLOBBER) was **not** weakened by the consolidation — this was
  the highest-risk regression surface (the plan explicitly forbade the naive unconditional-upsert swap) and
  it was verified NOT to have regressed: `test_upload_failed_cas_noop_on_advanced_cloud_job`,
  `test_push_mismatch_over_cap_does_not_clobber_when_cloud_job_not_submitted`, and
  `test_hold_awaiting_cloud_spill_cas_miss_is_full_noop` all exist under their original names, are
  genuinely discriminating (verified by reading their assertions, not just their pass/fail status), and
  pass live.
- The anti-drift AST guard's own blind spot (found by the 83-07 code review, WR-01: it missed the
  `**splat` form the allowed writer itself uses) was subsequently hardened in commit `f6a969e4` and
  independently mutation-tested by this verifier: injecting a synthetic splat-form inline awaiting writer
  into `agent_push.py` turned the guard test RED; restoring the source (`git checkout --`) turned it GREEN
  again, with a clean `git status` confirmed afterward.
- The NULL-GUARD (absent FileRecord → FULL no-op, no `AttributeError`) is implemented and covered by a
  named test on both routers, both confirmed passing live.
- D-03, D-09, D-10, D-11, D-12, and Landmine L1 (never commits) are all preserved byte-identically per
  direct code read plus the green regression suite (452-test `agents` bucket, 39-test integration subset).
- The full-repo test suite shows 4 failures + 17 errors, but every one of them is confined to
  `tests/shared/*` modules NOT touched by 83-07, and every one passes cleanly when re-run in isolation —
  matching the documented pre-existing "local full-suite colima flake" test-infra class, not a regression.

Phase 83's goal — cloud routing via the `cloud_job` sidecar as one atomic consistency domain, with the
CAS-guard collapse closing the `/upload-failed` gap — is achieved in the codebase, backed by passing,
non-vacuous, live-run tests, and the drift-prevention invariant the phase's own LOCKED decisions require is
now self-enforcing. Ready to proceed.

---

_Verified: 2026-07-09T23:59:00Z_
_Verifier: Claude (gsd-verifier)_
