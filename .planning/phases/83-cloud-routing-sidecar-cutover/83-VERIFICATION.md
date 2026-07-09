---
phase: 83-cloud-routing-sidecar-cutover
verified: 2026-07-09T22:08:14Z
status: gaps_found
score: 6/7 must-haves verified
overrides_applied: 0
gaps:
  - truth: "D-01/D-02: a single go-forward writer of cloud_job.status='awaiting' exists and is shared, not hand-copied three times (83-01-PLAN.md must_have, CONTEXT D-02 LOCKED constraint: \"one writer, reused by the hold path and both spill paths — not three hand-written copies\")"
    status: failed
    reason: >
      hold_awaiting_cloud() (src/phaze/services/backends.py:84) is called from exactly ONE site —
      routers/pipeline.py:351 (trigger_analysis). Both over-cap spill paths do NOT call it:
      report_upload_failed (routers/agent_s3.py:206-215) and report_push_mismatch
      (routers/agent_push.py:273-283) each re-implement their own inline CAS `UPDATE cloud_job SET
      status='awaiting', attempts=... WHERE status IN (...)`. This is three independent writers of
      `status='awaiting'`, not the single shared writer the plan's own must_haves and the CONTEXT's
      LOCKED D-02 constraint require. All three currently produce a correct `awaiting` row (no live
      routing defect — confirmed by passing tests and the shadow-compare gate), but the drift-prevention
      invariant the docstring claims ("the single go-forward writer ... shared ... instead of three
      hand-copied writers", backends.py:87-90) does not hold in code. This is exactly code-review
      finding WR-03 in 83-REVIEW.md, confirmed here by direct inspection — not merely a review note
      that was silently accepted; it is a documented FAILED must-have from 83-01-PLAN.md's frontmatter.
    artifacts:
      - path: "src/phaze/services/backends.py"
        issue: "hold_awaiting_cloud() docstring/must-have claims single shared writer; only 1 of 3 required call sites use it"
      - path: "src/phaze/routers/agent_s3.py"
        issue: "report_upload_failed's over-cap spill re-stamp (lines ~206-215) writes its own inline CAS UPDATE instead of calling hold_awaiting_cloud()"
      - path: "src/phaze/routers/agent_push.py"
        issue: "report_push_mismatch's over-cap spill re-stamp (lines ~273-283) writes its own inline CAS UPDATE instead of calling hold_awaiting_cloud()"
    missing:
      - "Route both spill-path CAS re-stamps through hold_awaiting_cloud(session, file, attempts=cloud_submit_max_attempts), OR correct the docstring/must-have text to describe three independently-CAS-guarded writers (each enumerating the fields that must stay in lockstep) instead of claiming a single shared writer."
---

# Phase 83: Cloud-Routing Sidecar Cutover Verification Report

**Phase Goal:** Cloud routing (`AWAITING_CLOUD`/`PUSHING`/`PUSHED`/`LOCAL_ANALYZING`) via the `cloud_job`
sidecar / derived `in_flight(analyze)`, one atomic consistency domain, CAS-guard collapse (closes the
missing `/upload-failed` guard) (SIDECAR-01)
**Verified:** 2026-07-09T22:08:14Z
**Status:** gaps_found
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths (ROADMAP Success Criteria + PLAN must-haves)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | SC#1 — drain query, three dispatch route flips, four callback CAS guards all read/write `cloud_job`/`in_flight`, no `FileRecord.state` routing read | VERIFIED | `get_cloud_staging_candidates` (`services/pipeline.py:1269-1316`) joins `CloudJob`, filters on `status='awaiting' AND ~inflight_clause(ANALYZE) AND ~domain_completed_clause(ANALYZE)` — zero `FileRecord.state` predicate. `LocalBackend.dispatch`/`ComputeAgentBackend.dispatch`/`KueueBackend.dispatch` (`services/backends.py:247,347,442`) only WRITE `file.state` (dual-write, D-00c), never read it as a routing predicate. All four callbacks (`report_uploaded`, `report_upload_failed`, `report_pushed`, `report_push_mismatch`) CAS on `cloud_job.status` (verified by direct code read below). |
| 2 | SC#2 — `report_upload_failed` gains a CAS guard closing the `agent_s3.py:195` clobber bug, proven by a regression test | VERIFIED | `agent_s3.py:206-217`: CAS `UPDATE CloudJob WHERE status IN ('uploading','uploaded')`; `rowcount==0` → FULL no-op (D-10), no FileRecord write, no S3 cleanup, no ledger clear. `tests/agents/routers/test_agent_s3.py::test_upload_failed_cas_noop_on_advanced_cloud_job` seeds an ANALYZED file with `cloud_job=RUNNING/SUCCEEDED`, asserts `cleared=False`, `cloud_job` UNCHANGED, `file.state` stays `ANALYZED` (i.e. NOT clobbered back to `AWAITING_CLOUD`) — genuinely fails against the pre-fix unguarded write. Test passes (`uv run pytest tests/agents/routers/test_agent_s3.py` — 100% pass, run live during this verification). |
| 3 | SC#3 (D-08 HARD GATE) — shadow-compare gate stays green, no double-dispatch/re-pick window | VERIFIED | `tests/integration/test_drain_double_dispatch.py` exists, is NOT skipped/xfail, drives two sequential `stage_cloud_window` ticks across (a) local dispatch, (b) rolled-back tick with committed ledger row, (c) terminally-failed local analyze. Each assertion is non-vacuous: asserts `dispatched_ids` lists and `cloud_job.status`, not just tick return counts. **Ran live: 3 passed.** `tests/integration/test_shadow_compare.py` — awaiting_cloud hard invariant green on a held-file fixture. **Ran live: full `tests/integration` bucket — 164 passed, 0 failed, in isolation with `-p no:randomly`.** |
| 4 | D-12 — all four callback CAS guards anchor per backend kind, no universal PUSHING/PUSHED predicate | VERIFIED | `report_uploaded` CAS `status=='uploading'` (kueue, pre-existing); `report_upload_failed` CAS `status IN ('uploading','uploaded')` (D-09); `report_pushed` CAS `status=='submitted'` (`agent_push.py:135`); `report_push_mismatch` over-cap CAS `status=='submitted'` (`agent_push.py:277`). No `backends.toml`/kind-resolution leak into `enums/stage.py`. |
| 5 | D-14 — reaper DELETEs inert `awaiting` rows at both analyze-terminal seams | VERIFIED | `routers/agent_analysis.py:272` (`put_analysis`) and `:395` (`report_analysis_failed`) both execute `delete(CloudJob).where(file_id=..., status==AWAITING)`. Tests `test_analysis_put_reaps_awaiting_cloud_job`, `test_analysis_failed_reaps_awaiting_cloud_job`, `test_analysis_failed_leaves_succeeded_cloud_job`, `test_analysis_failed_leaves_running_cloud_job` — non-vacuous (assert row absent vs. present-and-unchanged). All pass live. |
| 6 | Hard invariant `AWAITING_CLOUD ⇒ cloud_job(status='awaiting')` — go-forward writer + existing-corpus repair | VERIFIED | Go-forward: `trigger_analysis` (`routers/pipeline.py:351`) calls `hold_awaiting_cloud(session, file)`. Existing-corpus: `alembic/versions/034_backfill_cloud_awaiting.py` re-runs `032`'s backfill `INSERT…SELECT…'awaiting'…ON CONFLICT (file_id) DO NOTHING`, sync/static/parameter-free, no ORM schema touch. Migration test asserts idempotent repair + empty-autogenerate-diff + downgrade. Ran live: passes. |
| 7 | D-01/D-02 — the awaiting writer is a SINGLE shared writer, not three hand-copied writers (LOCKED constraint, 83-01-PLAN.md must_have) | **FAILED** | `hold_awaiting_cloud()` has exactly one caller (`pipeline.py:351`). Both over-cap spill paths (`agent_s3.py:206-215`, `agent_push.py:273-283`) write their own independent inline CAS `UPDATE`s instead of calling the shared helper — confirmed by direct grep/read, matching code-review finding WR-03. Not a live routing bug (all three produce correct output today, proven by green tests), but the stated must-have and the CONTEXT's LOCKED constraint ("one writer... not three hand-written copies") is not realized in code. |

**Score:** 6/7 truths verified (the 7th is a documented, non-blocking-to-routing but must-have-violating gap)

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/services/backends.py` | `hold_awaiting_cloud()` shared writer | VERIFIED (exists, substantive) / ⚠️ PARTIALLY WIRED | Function exists, correct upsert shape (`on_conflict_do_update(index_elements=["file_id"])`), but only 1 of 3 intended call sites use it (see gap above) |
| `alembic/versions/034_backfill_cloud_awaiting.py` | corpus-repair migration | VERIFIED | Sync, static SQL, `ON CONFLICT DO NOTHING`, chains `down_revision="033"` |
| `src/phaze/routers/agent_analysis.py` | D-14 reaper at both terminal seams | VERIFIED | `delete(CloudJob)...status==AWAITING` at both `put_analysis` (:272) and `report_analysis_failed` (:395) |
| `src/phaze/routers/agent_s3.py` | `report_upload_failed` CAS rewrite (D-09/D-10/D-11/D-03) | VERIFIED | CAS on `status IN (uploading,uploaded)`, full no-op on miss, `pg_advisory_xact_lock` before RMW read, re-stamp to `awaiting` retaining spent `attempts` |
| `src/phaze/routers/agent_push.py` | `report_pushed`/`report_push_mismatch` CAS anchor swap | VERIFIED | Both anchor on `cloud_job.status=='submitted'` |
| `src/phaze/services/pipeline.py` | drain cutover (D-05/D-06/D-07) + `get_awaiting_cloud_count` (D-15) | VERIFIED | Conjunct composes `inflight_clause`/`domain_completed_clause` verbatim; `with_for_update(of=CloudJob, skip_locked=True)`; FIFO on `FileRecord.created_at`, staleness clock on `CloudJob.updated_at`; count card re-anchored on identical clause |
| `tests/integration/test_drain_double_dispatch.py` | SC#3 hard gate | VERIFIED | 3 non-vacuous tests, all pass live |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `routers/pipeline.py trigger_analysis` | `services/backends.hold_awaiting_cloud` | direct call | WIRED | `pipeline.py:351` |
| `services/pipeline.py get_cloud_staging_candidates` | `cloud_job` sidecar | INNER join + FOR UPDATE OF CloudJob | WIRED | Verified query text |
| `routers/agent_s3.py report_upload_failed` (spill) | `cloud_job` CAS re-stamp | inline `UPDATE` (NOT via `hold_awaiting_cloud`) | **PARTIAL** (functionally correct, architecturally not the shared-writer path) | See gap #7 |
| `routers/agent_push.py report_push_mismatch` (spill) | `cloud_job` CAS re-stamp | inline `UPDATE` (NOT via `hold_awaiting_cloud`) | **PARTIAL** (functionally correct, architecturally not the shared-writer path) | See gap #7 |
| `routers/agent_analysis.py` (both terminal seams) | `cloud_job` DELETE reaper | `session.execute(delete(CloudJob)...)` in existing txn | WIRED | Confirmed at both seams |

### Data-Flow Trace (Level 4)

Not applicable — this phase changes routing/CAS predicates on backend data (Postgres rows), not
UI-rendered dynamic data. The drain query and count-card were traced above (Key Link Verification) and
directly execute real SQL against `cloud_job`/`files`, not static/hardcoded returns.

### Behavioral Spot-Checks / Live Test Runs

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| SC#3 hard gate | `uv run pytest tests/integration/test_drain_double_dispatch.py -p no:randomly -q` | 3 passed | PASS |
| SC#2 CAS regression + D-11 concurrency | `uv run pytest tests/agents/routers/test_agent_s3.py tests/agents/routers/test_agent_push.py tests/agents/routers/test_agent_analysis.py -p no:randomly -q` | 76 passed | PASS |
| Migration 034 + shadow-compare | `uv run pytest tests/integration/test_migrations/test_migration_034_backfill_cloud_awaiting.py tests/integration/test_shadow_compare.py -p no:randomly -q` | 40 passed | PASS |
| Full integration bucket (isolation) | `uv run pytest tests/integration -p no:randomly -q` | 164 passed | PASS |
| Drain/backend/dispatch/count-card units | `uv run pytest tests/analyze/services/test_backends.py tests/analyze/services/test_backend_selection.py tests/analyze/tasks/test_release_awaiting_cloud.py tests/analyze/core/test_staging_cron.py tests/analyze/core/test_dispatch_snapshot.py tests/shared/routers/test_pipeline.py tests/shared/services/test_pipeline.py -p no:randomly -q` | 286 passed | PASS |
| Lint (touched files) | `uv run ruff check <touched files>` | All checks passed | PASS |
| Types | `uv run mypy .` | Success: no issues found in 205 source files | PASS |

All runs executed live during this verification (test DB on port 5433, both `TEST_DATABASE_URL` and
`MIGRATIONS_TEST_DATABASE_URL` exported per the test-env note) — not taken from SUMMARY.md claims.

### Probe Execution

No `scripts/*/tests/probe-*.sh` files exist and none were declared in the PLAN/SUMMARY files for this
phase. Step 7c: SKIPPED (no probes declared or discovered).

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|--------------|------------|--------------|--------|----------|
| SIDECAR-01 | 83-01..06 | Cloud-routing status via `cloud_job` sidecar; CAS guards preserved/strengthened; closes `agent_s3.py:195` bug | SATISFIED (with the D-01/D-02 caveat above) | All three ROADMAP Success Criteria verified true in code + live tests; the one documented gap (D-01/D-02 single-writer) does not violate SIDECAR-01's literal text ("CAS-guard behavior ... preserved or strengthened") — CAS behavior IS strengthened at all four callbacks — but does violate a plan-level must-have about writer architecture. |

No orphaned requirements: REQUIREMENTS.md line 149 maps only SIDECAR-01 to Phase 83; all 6 plans declare `requirements: [SIDECAR-01]`.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `src/phaze/services/backends.py` | 87-90 (docstring), see gap #7 | Docstring/must-have asserts an invariant ("single shared writer... not three hand-copied writers") that code does not realize | ⚠️ Warning (also a BLOCKER-class must-have failure per the plan's own frontmatter) | Future edits to one writer (e.g. also stamping `cloud_phase`/clearing `backend_id`) could silently diverge from the other two — no test would catch it today |
| `src/phaze/routers/agent_push.py` | 225 (`report_push_mismatch`) | D-07 reporter-authorization gate skipped when `backend is None`, unlike `report_pushed`'s hold-before-CAS | ⚠️ Warning (code review WR-02; not reachable in default config per review's own analysis — kueue never accrues `push_file:<id>` attempts) | Latent asymmetry; config-dependent, not a live exploit path today |
| `src/phaze/tasks/release_awaiting_cloud.py` | 173, 181, 203 | Candidate-fetch + GATE-2 sit outside the "never raises" `try` boundary (WR-04) | ℹ️ Info | Low impact — SAQ logs the failed tick, files stay `AWAITING_CLOUD`, next tick retries; contradicts stated discipline but not stranding |
| `src/phaze/services/backends.py` | 236 (`LocalBackend` docstring) | Stale docstring says `dispatch` is "NOT wired into the single-path drain" (IN-02) | ℹ️ Info | Cosmetic — contradicted by the actual wiring exercised in `test_drain_double_dispatch.py` |
| `src/phaze/services/pipeline.py` | `get_analyze_stage_files` (787-834) | WR-01 (already known/reported by code review): new awaiting-row writer causes `AWAITING_CLOUD`/spilled-local files to display lane `"a1"` instead of `"local"` | ℹ️ Info — already surfaced in 83-REVIEW.md, display-only regression, no routing impact | Not re-flagged as new; carried forward per `<already_known_do_not_reflag>` |

No `TBD`/`FIXME`/`XXX` markers found in any file touched by this phase.

### Human Verification Required

None required to determine phase-goal achievement. Two items are explicitly deferred by the phase's own
`83-VALIDATION.md` "Manual-Only Verifications" table, consistent with the accepted Phase-79 precedent
(live-corpus verification deferred to the next homelab rollout) — not new blocking items introduced by
this verification:

- Live-corpus shadow-compare run against the real ~200K-row corpus post-rollout (records the
  `awaiting_cloud` invariant divergence count, expected 0).
- Optional `EXPLAIN (ANALYZE, BUFFERS)` check that the drain query plan uses `ix_cloud_job_awaiting`
  rather than a seq scan at scale (the durable defense is the D-14 reaper, not this assertion).

### Gaps Summary

Every ROADMAP-level Success Criterion for Phase 83 is verified TRUE in the codebase and backed by
passing, non-vacuous tests run live during this verification (not taken from SUMMARY.md claims):

- SC#1 (no `FileRecord.state` routing read across drain/dispatch/callbacks) — VERIFIED.
- SC#2 (`report_upload_failed` CAS guard + regression test closing the `agent_s3.py:195` bug) —
  VERIFIED, with a genuinely discriminating regression test (`test_upload_failed_cas_noop_on_advanced_cloud_job`).
- SC#3 (shadow-compare stays green, no double-dispatch/re-pick window, HARD GATE test) — VERIFIED,
  `test_drain_double_dispatch.py` is real and passes; it is written to fail against a state-based drain
  per its own docstring and exercises exactly the rolled-back-tick hazard the phase's most load-bearing
  decision (D-05, conjunct-over-deletion) depends on.

One plan-level must-have is FAILED: 83-01-PLAN.md declares "D-01: a single go-forward writer of
`cloud_job.status='awaiting'` now exists ... and is shared, not hand-copied three times", and the
CONTEXT's D-02 discretion carries a binding (LOCKED) constraint — "one writer, reused by the hold path
and both spill paths — not three hand-written copies." Direct code inspection confirms `hold_awaiting_cloud()`
is called from exactly one site; the two over-cap spill paths (`agent_s3.py`, `agent_push.py`) each
write their own independent CAS `UPDATE`. This reproduces code-review finding WR-03 exactly — it is not
a live routing/data-integrity defect (all three writers currently produce a correct row, proven by green
tests including the shadow-compare gate), but it is a documented, un-remediated must-have failure, not
merely a "nice to have" review note.

**This looks intentional/acceptable if the operator judges the architectural drift-risk as tolerable**
(the review already recommends a fix). To accept this deviation instead of routing a closure plan through
`/gsd:plan-phase --gaps`, add to this file's frontmatter:

```yaml
overrides:
  - must_have: "D-01/D-02: a single go-forward writer of cloud_job.status='awaiting' now exists and is shared, not hand-copied three times"
    reason: "All three writers (hold_awaiting_cloud, agent_s3.py spill CAS, agent_push.py spill CAS) independently produce correct status='awaiting' rows; each is individually CAS-guarded and covered by passing tests. Drift-prevention is a hardening concern, not a routing-correctness gap."
    accepted_by: "<name>"
    accepted_at: "<ISO timestamp>"
```

Otherwise, the recommended fix (per 83-REVIEW.md WR-03) is to route both spill-path re-stamps through
`hold_awaiting_cloud(session, file, attempts=cloud_submit_max_attempts)` — the helper already accepts an
`attempts=` argument shaped exactly for this reuse.

---

_Verified: 2026-07-09T22:08:14Z_
_Verifier: Claude (gsd-verifier)_
