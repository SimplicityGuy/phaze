# Phase 78: Derivation Layer, Eligibility & Anti-Drift Test Harness - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-08
**Phase:** 78-derivation-layer-eligibility-anti-drift-test-harness
**Areas discussed:** in_flight source (D-01/INFLIGHT-03), in_flight failure default (INFLIGHT-02), done(metadata) predicate (DERIV-03), predicate-module boundary & drift-lock (DERIV-01/04)

---

## in_flight source (INFLIGHT-03 / D-01 — required written decision)

| Option | Description | Selected |
|--------|-------------|----------|
| Ledger authoritative; saq_jobs corroborating | in_flight = ledger row exists (durable, satisfies safety property); saq_jobs read defensively, non-load-bearing. | ✓ |
| scheduling_ledger alone | No saq_jobs read at all; simplest, fully decoupled; loses the corroboration hook. | |
| saq_jobs ∪ scheduling_ledger (union) | Most conservative; couples the hot poll to the live broker as load-bearing; enlarges false-positive-stuck set. | |

**User's choice:** Ledger authoritative; saq_jobs corroborating.
**Notes:** Consistent with the existing `recover_orphaned_work` reconcile (`orphaned = ledger − live saq_jobs − domain-completed`). The ledger is the durable "was scheduled" fact and outlives a crashed job's lost saq_jobs row, so it satisfies "never falsely re-enqueued as not_started" without making the live broker authoritative.

---

## in_flight failure default (INFLIGHT-02)

| Option | Description | Selected |
|--------|-------------|----------|
| Ledger decides the bool; saq_jobs = detail only | in_flight bool = ledger row exists (also degrade-safe default); saq_jobs SAVEPOINT-wrapped, enriches busy pills only, never flips the bool. | ✓ |
| saq_jobs can add in_flight, ledger can't be overridden | Union for the bool with ledger-only fallback on error. | |
| Degrade to in_flight=true on error | Assume in_flight on any saq_jobs failure. | |

**User's choice:** Ledger decides the bool; saq_jobs = detail only.
**Notes:** Falls out cleanly from D-01. Guarantees `/pipeline/stats` never 500s on a broker read hiccup; a broker read failure never freezes an otherwise-eligible file.

---

## done(metadata) predicate (DERIV-03)

| Option | Description | Selected |
|--------|-------------|----------|
| row exists AND failed_at IS NULL | Honors Phase 77 D-02; additive-safe today; failure-only row derives NOT done. | ✓ |
| row exists (bare presence) | Ignores failed_at; wrong post-cutover. | |
| row exists AND has real tags AND failed_at IS NULL | Adds a content check; only needed if empty successful rows occur. | |

**User's choice:** row exists AND failed_at IS NULL.
**Notes:** Matches the Phase 77 handoff exactly (metadata failure writes a row with failed_at set; metadata backfill was skipped so all current rows are NULL → unchanged behavior now).

---

## Predicate-module boundary & drift-lock (DERIV-01 / DERIV-04)

| Option | Description | Selected |
|--------|-------------|----------|
| enums = DB-free resolver + DAG; service = ColumnElement; test locks | enums/stage.py (agent-safe) holds enums + DAG topology + pure-Python resolver; services/stage_status.py holds the SQLAlchemy ColumnElement builders; the equivalence test is the drift-lock. | ✓ |
| Single shared builder callable both ways | One callable returning ColumnElement or bool via operator overloading; fragile at IS NOT NULL / IN(...). | |
| SQL is the only source; Python resolver generated from query | Breaks agent-safe/DB-free requirement; per-row resolution becomes a query. | |

**User's choice:** enums = DB-free resolver + DAG; service = ColumnElement; test locks.
**Notes:** Keeps enums/stage.py importable by agents with no DB dependency; the DERIV-04 parametrized equivalence test (incl. DERIV-05 one-success/one-failed fingerprint) is authoritative where SQL and Python idioms diverge.

## Claude's Discretion

- Exact fixture-matrix shape for the equivalence test, the internal signature of the shared predicate builders, and the precise SAVEPOINT/degrade helper — left to research + planning.

## Deferred Ideas

- Reader/writer cutover to derived status (DAG busy pills reading in_flight; pending-set queries using eligible()) — later milestone phases (79 shadow-compare first).
- Tightening a metadata writer to set failed_at on failure — writer-side, later phase.
