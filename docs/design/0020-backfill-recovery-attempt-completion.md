# Backfill completion belongs to the queued attempt

Date: 2026-10-03. Bead: phaze-za41v. Status: accepted.

## Operator decision

Question as put:

> For phaze-za41v — “Recover lost backfill jobs,” which mechanism should distinguish an old completed analysis from completion of the newly queued backfill? The bead explicitly requires your decision under ADR-0012 before a fix; reachability is confirmed by inspection, with reproduction still pending.

Answer as given, quoting only the selected option label:

> Compare completion time with ledger enqueue time (Recommended)

The operator selected this option in the dispatch conversation on 2026-10-03. This document
is the durable record of that exchange under ADR-0012 rule 2. The implementation details below
are engineering choices implementing that decision, not additional operator statements.

## Reproduction

The original bead's evidence was **INFERENCE FROM CODE, NOT MEASURED**. That qualification
applied until the synthetic reproduction on 2026-10-03 replaced it for reachability.

`tests/integration/test_reanalysis_backfill_recovery.py::test_lost_backfill_is_still_owed`
seeds a completed analysis with 1 of 2 fine windows covered. It calls the real
`enqueue_incomplete_reanalysis`, routes through the production queue factory and enqueue hooks,
and asserts that both the real Postgres broker job and scheduling-ledger obligation exist.
It then deletes the broker job without running a terminal callback, simulating queue loss.

Before the fix, both parametrized cases failed: recovery re-enqueued **0**, expected **1**;
the reaper deleted **1** ledger row, expected **0**. This is a measured synthetic reproduction
of the real producer-to-consumer path. It does not establish that a production loss occurred.

## Mechanism and preserved guards

For analysis recovery, a successful completion older than the current `process_file` ledger's
`enqueued_at` belongs to an earlier attempt. That row remains owed work when its broker job
disappears. Completion at or after enqueue satisfies the attempt. Force-skip markers and
terminal failures retain their existing completion semantics.

Both enqueue and successful completion are stamped by the database clock (`func.now()`),
including the production `agent_analysis` completion writer. Regression fixtures use that
same clock; comparing database enqueue to a test host's wall clock would introduce clock skew
into a test intended to model a completed attempt.

The recovery completion query, orphan reporting, and resolved-ledger reaper share the same
recovery-specific SQL predicate. The raw `domain_completed_clause` and its consumers outside
recovery keep their existing file-level semantics. Cloud ownership, live-job exclusion,
deterministic-key deduplication, and the requirement that work have been scheduled all remain
in force. Never-scheduled files cannot enter recovery; the historical over-enqueue guard is
preserved.

## Measured blast radius

Production aggregate-only SELECTs ran in a read-only transaction on 2026-10-03:

| Population | Count |
| --- | ---: |
| All files | 29,366 |
| Completed analyses with incomplete fine/coarse coverage, not applied | 2,021 |
| `process_file` scheduling-ledger rows | 2,376 |
| Those whose successful analysis completion predates ledger enqueue | 0 |
| Previous row, excluding analysis skips and terminal failures | 0 |
| Previous row, with no queued/active broker job | 0 |

The current affected ledger population is **0**. The **2,021** candidates are potential future
backfill input, not lost jobs. These measurements replace neither the original dated inventory
nor its controls; they are a separate current snapshot. The change applies only to analyze
recovery/reaping whose previous successful completion precedes a new durable obligation.
No production mutation was performed for this measurement.
