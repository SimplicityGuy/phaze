# Phase 81: Per-Stage Failure Persistence & Retry Paths - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-08
**Phase:** 81-per-stage-failure-persistence-retry-paths
**Areas discussed:** Analyze write-path cutover, Metadata marker lifecycle + retry, FAILURE_IS_TERMINAL encoding, FAIL-04 fingerprint scope

---

## Analyze write-path cutover

### Q1 — Does `report_analysis_failed` keep writing `state = ANALYSIS_FAILED`?

| Option | Description | Selected |
|--------|-------------|----------|
| Dual-write until 033 | Write `failed_at` + `error_message` AND keep `state=ANALYSIS_FAILED`. Readers keep working, shadow gate stays green. | ✓ |
| Stop writing state now | Only `failed_at`. Empties the red bucket, breaks `retry_analysis_failed`, re-opens the 44.5K over-enqueue class. | |
| You decide | Claude picks. | |

**User's choice:** Dual-write until 033
**Notes:** FAIL-01's "replacing the enum value" reinterpreted as *reliance* replaced, not the write. Phase 90 removes the write.

### Q2 — How to prevent a row with BOTH `analysis_completed_at` and `failed_at`?

| Option | Description | Selected |
|--------|-------------|----------|
| Mutually exclusive + CHECK | Success clears `failed_at`; failure clears `completed_at`; DB CHECK enforces it. | ✓ |
| Guarded failure stamp | Stamp `failed_at` only when `completed_at IS NULL`; preserves prior good analysis. | |
| Allow mixed, fix the SQL | Tighten `failed_clause(analyze)` with `AND completed_at IS NULL`. Edits Phase 78's shipped module. | |
| You decide | Claude picks. | |

**User's choice:** Mutually exclusive + CHECK
**Notes:** Consequence surfaced during discussion — this makes Phase 81 ship a migration; it is no longer writer-only.

### Q3 — What lands in `analysis.error_message`?

| Option | Description | Selected |
|--------|-------------|----------|
| Composed `"reason: error"` | Keeps classification + detail in one Text column. No schema change. | ✓ |
| `error` only | Loses the `reason` classification. | |
| Separate `reason` column | Most queryable; a schema change in a writer-scoped phase. | |

**User's choice:** Composed `"reason: error"`, truncated to the column bound

### Q4 — Migration numbering, given "033" is reserved by name for Phase 90

| Option | Description | Selected |
|--------|-------------|----------|
| 81 takes 033, Phase 90 → 034 | Sequential + honest. Doc churn across ROADMAP ×5, design doc, REQUIREMENTS MIG-02/04. | ✓ |
| Writers-only, defer CHECK to Phase 90 | Keeps 81 writer-only; mixed rows survive and Phase 82 double-counts them. | |
| 81 ships 033 as data-cleanup only | No CHECK; invariant has no DB guard. | |

**User's choice:** 81 takes 033, Phase 90 → 034

### Q5 — Which side wins when cleaning the mixed rows `032` created?

| Option | Description | Selected |
|--------|-------------|----------|
| Clear `failed_at`, keep `completed_at` | `done ≻ failed`; matches `_analyze_status`. No file changes derived status. | ✓ |
| Clear `completed_at`, keep `failed_at` | Flips files DONE→FAILED; trips the shadow gate. | |
| Abort migration if any exist | Safe but near-certain to block the deploy. | |

**User's choice:** Clear `failed_at`, keep `completed_at`
**Notes:** Grounded in `032`'s `_BACKFILL_ANALYZE_FAILED` (lines 74-80), whose `ON CONFLICT DO UPDATE` has no `completed_at` guard — so mixed rows almost certainly exist in the live corpus.

---

## Metadata marker lifecycle + retry

### Q1 — Does `report_metadata_failed` gain a request body?

| Option | Description | Selected |
|--------|-------------|----------|
| Optional body, default placeholder | Old bodyless agents still 200 + clear the ledger. No version-skew hazard. | ✓ |
| Required body, mirror AnalysisFailurePayload | Best triage data; 422s old agents → ledger never clears → CR-02 loop returns. | |
| No body, fixed placeholder | Zero risk, zero triage detail. | |

**User's choice:** Optional body, default placeholder
**Notes:** Control-plane and agent ship as separate images, so a required body is a live deployment hazard.

### Q2 — What does the FAIL-03 retry do to the failure row?

| Option | Description | Selected |
|--------|-------------|----------|
| Leave the row, just re-enqueue | `eligible(metadata)` already admits FAILED; `put_metadata`'s clear-on-success resolves it. | ✓ |
| Delete the metadata row | Equivalent end state; destroys the record before the retry succeeds. | |
| Clear `failed_at` in place | UNSAFE — payload-NULL row would read DONE and never be extracted again. | |

**User's choice:** Leave the row, just re-enqueue
**Notes:** The third option is recorded in CONTEXT.md as explicitly rejected, not merely unchosen.

### Q3 — What shape is the FAIL-03 endpoint?

| Option | Description | Selected |
|--------|-------------|----------|
| Bulk mirror of `retry_analysis_failed` | `POST /pipeline/metadata-failed/retry`, HTMX, donor guard ordering. | ✓ |
| Per-file endpoint | No failed-metadata list to drive it from until Phase 82. | |
| Both | Doubles endpoint + template surface in a phase that already grew a migration. | |

**User's choice:** Bulk mirror of `retry_analysis_failed`
**Notes:** Simpler than the donor — metadata has no terminal `FileState` to flip out of.

---

## FAILURE_IS_TERMINAL encoding

### Q1 — How are the terminality tables encoded?

| Option | Description | Selected |
|--------|-------------|----------|
| Two explicit tables | `FAILURE_IS_TERMINAL` (recovery) + `ELIGIBLE_AFTER_FAILURE` (eligibility). Kills the inlined carve-out. | ✓ |
| One table, recovery-only | Smallest diff; analyze terminality stays in two places that can drift. | |
| One table, refactor `eligible()` | Breaks Phase 78's ELIG-01/04 tests and silently disables the FAIL-03 retry. | |

**User's choice:** Two explicit tables
**Notes:** Raised before the question: `eligible()` and `domain_completed()` are different axes. Metadata is terminal for recovery *and* eligible for a manual trigger. Conflating them was the trap.

### Q2 — How far does Phase 81 carry `domain_completed()`?

| Option | Description | Selected |
|--------|-------------|----------|
| Table + pure helper + SQL twin | Drift-locked now via Phase 78's equivalence test. Phase 80 only wires recovery. | ✓ |
| Table + pure helper only | Python and SQL land one phase apart — the drift window 78 D-04 closed. | |
| Table only | Phase 80's SC#3 would have no derivation-layer encoding to contrast with. | |

**User's choice:** Table + pure helper + SQL twin

---

## FAIL-04 fingerprint scope

### Q1 — What code does Phase 81 actually write for FAIL-04?

| Option | Description | Selected |
|--------|-------------|----------|
| Regression tests + docstring only | No new writer. `report_fingerprint_failed` keeps persisting nothing. | ✓ |
| Also fix `done(fingerprint)` per-engine | Overturns Phase 78's locked DERIV-05; re-baselines the shadow gate. | |
| Also bound retries with an attempts cap | New capability; no requirement asks for it. | |

**User's choice:** Regression tests + docstring only
**Notes:** Discussion established that `put_fingerprint` never advances `state` — the sole `FINGERPRINTED` writer is `retry_analysis_failed` — so today's mixed-engine file re-fingerprints forever, and derivation *inverts* that. The regression bites at Phase 82, not 81.

### Q2 — Where are the mixed-engine hole and the attempts cap tracked?

| Option | Description | Selected |
|--------|-------------|----------|
| Deferred ideas in 81-CONTEXT.md | Captured with file:line evidence, addressed to Phase 82. | ✓ |
| Raise as ROADMAP edits to Phase 82 now | Edits another phase's contract from inside 81's discussion. | |
| File as backlog items | Decouples a correctness regression that should gate 82. | |

**User's choice:** Deferred ideas in 81-CONTEXT.md

---

## Claude's Discretion

None — the operator decided every gray area presented.

Two items were **recorded as must-haves without being asked**, because no defensible alternative
exists: `put_analysis` and `put_metadata` must each explicitly clear `failed_at`/`error_message` on
success. Their `exclude_unset` upserts would otherwise leave a successful retry reading as
permanently failed, and `put_metadata`'s empty-body `on_conflict_do_nothing` branch would never
clear the marker at all. (CONTEXT.md D-13.)

Left to research/planning: the ordering of `report_analysis_failed`'s new upsert against its
existing `clear_ledger_entry` + `_delete_staged_object_if_cloud` side effects; whether
`get_metadata_failed_files` lives in `services/` or the router; whether the HTMX fragment reuses
`retry_failed_response.html`; and whether the Phase 90 `033→034` rename lands in this PR or its own.

## Deferred Ideas

- **Mixed-engine fingerprint retry hole** → Phase 82 (READ-01). `done(fingerprint)` = "any engine
  success wins", so a partially-failed file reads DONE and never retries once the pending set is
  derived. Behavior inverts from today. (`.planning/research/PITFALLS.md:188`)
- **`MAX_FINGERPRINT_ATTEMPTS` bound** → Phase 82. A poison file re-enqueues on every trigger click
  forever once derivation removes the accidental linear-enum gate. (`PITFALLS.md:90`)
- **UI surface for failed metadata** → Phase 82 (READ-02). FAIL-03's criterion names a backend
  endpoint only; the failed chip + retry button belong with the four-bucket per-stage counts.
