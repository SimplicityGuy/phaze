# ADR-0020: Backfill completion belongs to the queued attempt

Date: 2026-10-03. Bead: phaze-za41v. Status: accepted.

## Operator decision (phaze-za41v, 2026-10-03)

Question as put:

> For phaze-za41v — “Recover lost backfill jobs,” which mechanism should distinguish an old completed analysis from completion of the newly queued backfill? The bead explicitly requires your decision under ADR-0012 before a fix; reachability is confirmed by inspection, with reproduction still pending. <!-- Citation: 0012-verification-fidelity-and-operator-attribution.md; quoted question remains verbatim. -->

Answer as given, quoting only the selected option label:

> Compare completion time with ledger enqueue time (Recommended)

The operator selected this option in the dispatch conversation on 2026-10-03. This document
is the durable record of that exchange under [ADR-0012](0012-verification-fidelity-and-operator-attribution.md) rule 2. The implementation details below
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

Enqueue uses the database wall clock (`clock_timestamp()`), while the production `agent_analysis`
completion writer uses its later callback transaction clock (`func.now()`). Regression fixtures
use the database wall clock to advance completion within their long-lived outer transaction;
comparing database enqueue to a test host's wall clock would introduce clock skew into a test
intended to model a completed attempt.

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

## Review finding: terminal reanalysis failure has no durable attempt outcome

Review on 2026-10-03 identified a second reachable shape. `report_analysis_failed` preserves an
already successful analysis under its compare-and-set guard, leaving `failed_at` NULL. Its ledger
clear is guarded against a queued/active same-key broker job and can therefore leave the ledger
standing while the worker reports its terminal failure. Once that terminal broker row disappears,
the old successful completion plus fresh ledger is indistinguishable from a lost backfill.

A real-broker reproduction calls the actual failure handler, observes the preserved analysis and
surviving ledger, finishes the broker job with FAILED, and removes the broker row. Timestamp-only
recovery then re-enqueues **1**, while the terminal-attempt safety assertion expects **0**. The
earlier `failed` boundary case covers a persisted analysis failure marker, not this completed-file
failure path. It cannot establish that this second case is safe.

The operator authorized the durable outcome extension on **2026-10-03** under [ADR-0012](0012-verification-fidelity-and-operator-attribution.md).

Exact question:

> For phaze-za41v — “Recover lost backfill jobs,” the timestamp fix passes lost-job tests, but a real failed-backfill reproduction reveals that recovery can retry an already failed attempt after its broker record is purged. May we extend the fix to record a durable terminal outcome for each backfill attempt, preserving the older successful analysis?

Exact selected answer label:

> Record the attempt’s terminal outcome (Recommended)

Migration **077** adds nullable `scheduling_ledger.terminal_at`. A fresh analysis enqueue resets it;
an acknowledgement stamps it only if its `attempt_enqueued_at` equals the ledger's current enqueue
instant. The producer writes that DB timestamp into SAQ `Job.meta`, keeping the strict task payload
unchanged. Terminal failure callbacks carry it as an optional timezone-aware field. Recovery and
reaping regard an acknowledged terminal attempt as complete while preserving the old analysis.
Token-bearing failure callbacks take the same key's advisory transaction lock on their existing
session before inspecting the epoch. A newer ledger makes the old ACK an idempotent ignored reply
before domain mutation, ledger clear, or cloud cleanup, even if the new broker row is lost.
The callback keeps that lock through its outcome write and finalize commit; enqueue therefore
cannot replace the epoch between the check and cleanup. Lock-acquisition errors propagate through
the existing HTTP 5xx retry policy rather than authorizing an uncertain mutation. With no ledger,
the callback preserves best-effort domain failure reporting; it cannot recover missing identity.

The control queue serializes each deterministic analysis key across the ledger commit and broker
insert using a database-scoped advisory lock. An observed live key returns the documented dedup
result immediately, before hooks or insertion.
This linearizes dedup even if the worker finishes between the read and return, and preserves the
winning epoch, payload and outcome; a genuine later attempt resets them. A borrowed task-local
session uses the
already-held ledger connection, allowing pools of size **1** or **2** without nested acquisition.
The owned connection commits the lock-acquisition transaction before binding the session;
an independent observer verifies ledger visibility at broker-insert entry for both pool sizes.
Caller-owned test transactions remain untouched.
Broker failures release the lock and clear that context. Cancellation is covered at blocked
acquisition, after the server acquired the lock but before the acquisition await returns, and
paused broker insertion, for pool sizes **1** and **2**. The preceding helper leaked a lock in
both acquisition-completed cases (**2** failing regressions); guarded acquisition cleanup makes
all **6** cases pass. Actual blocked-query cancellation invalidates the asyncpg/SQLAlchemy
connection; valid connections are explicitly unlocked, and an uncertain unlock invalidates the
connection before pool return. Scoped backend lock checks and subsequent same-key enqueue prove
release, while the cancelled producer's own finally block confirms its task-local context clears.
Duplicate producers therefore no longer bump the non-authoritative soft enqueued counter; no rendered
denominator depends on its drift. Startup reconstruction retains a producer
timestamp from the broker meta. Sequential and concurrent duplicate producers, distinct keys,
new attempts, stale acknowledgements and failed inserts are covered against the actual broker.

Deployment order is migration, control/API, then agents. Older agents omit the optional timestamp:
their existing domain failure marker still works for fresh failures, but an old completed-result
failure cannot be safely assigned to an attempt without an identity. Such legacy or undelivered
acknowledgements retain the accepted lost-outcome residual. Ledger/lock outages remain best-effort;
they do not block enqueue. This change does not overwrite good analysis or weaken live-key clears.
Cloud one-shot backfill follows the existing cloud-attempt/reconcile authority and does not create a
`process_file` ledger obligation; this extension targets the local/compute SAQ attempt.

Current measured affected ledger population remains **0**; the failure above is synthetic.
