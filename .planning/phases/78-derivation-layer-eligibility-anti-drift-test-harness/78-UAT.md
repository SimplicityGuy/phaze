---
status: complete
phase: 78-derivation-layer-eligibility-anti-drift-test-harness
source: [78-01-SUMMARY.md, 78-02-SUMMARY.md]
started: 2026-07-08
updated: 2026-07-08
---

## Current Test

[testing complete]

## Tests

> Phase 78 is a purely additive developer/derivation-layer phase — no live-pipeline UI or
> operator workflow cuts over (that is Phase 79+). "User-observable" here means the
> developer-facing contract: the new modules import cleanly, stay agent-safe, and the
> resolver / eligibility / SQL⇔Python drift-lock all hold. All checks were run by the
> agent on the operator's behalf ("run the uat for me").

### 1. Agent-safe DB-free module boundary
expected: `import phaze.enums.stage` succeeds with no `phaze.models` / `phaze.database` / `sqlalchemy` in its import graph; the subprocess banned-import guard test enforces it.
result: pass
evidence: import OK (Stage/Status); banned-import grep empty; subprocess guard test passed.

### 2. DB-free per-row resolver correctness (DERIV-01/02/03/05)
expected: `resolve_status(stage, scalars)` returns the correct 4-way status for every stage with precedence `in_flight ≻ done ≻ failed ≻ not_started`; a one-success/one-failed fingerprint reads `done`; metadata failure-only reads `failed` (D-03); analyze partial row reads `not_started`.
result: pass
evidence: `uv run pytest tests/shared/test_stage_resolver.py` → 27 passed.

### 3. Eligibility predicate + ELIG-03 terminal-failed-analyze guard (ELIG-01/02/03/04)
expected: a discovered file is eligible for all three enrich stages; a failed analyze is NOT eligible (44.5K over-enqueue guard); a failed fingerprint stays eligible; apply is gated on an approved proposal, not bare done(review).
result: pass
evidence: `uv run pytest tests/shared/test_stage_eligibility_dag.py` → 17 passed; `-k terminal_failed_analyze` selects + passes the ELIG-03 regression.

### 4. SQL⇔Python anti-drift equivalence + in_flight ledger/SAVEPOINT-degrade (DERIV-04/05, INFLIGHT-01/02/03)
expected: for every (stage × status) fixture cell the SQL-derived status equals the Python-derived status equals the expected label; `in_flight` reads authoritatively from `scheduling_ledger`; a poisoned/dropped `saq_jobs` read degrades to a safe default without raising and `in_flight` still reads True from the ledger.
result: pass
evidence: ephemeral Postgres `:5433` (`just test-db`) → `uv run pytest tests/integration/test_stage_status_equivalence.py` → 25 passed; DB torn down after.

## Summary

total: 4
passed: 4
issues: 0
pending: 0
skipped: 0
blocked: 0

## Gaps

[none]

## Manual-Only / Deferred

- `in_flight(propose)` derivation (INFLIGHT-01 per-file sub-case): `generate_proposals` is keyed by a set-hash of `file_ids`, not per-file, so a per-file ledger key cannot derive it. ELIG-02 scopes propose eligibility to upstream conjuncts only. Documented deferral — no live behavior to UAT this phase.
