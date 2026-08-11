# ADR-0003 — Accept the residual backfill ledger-DELETE / concurrent-enqueue window

| | |
| --- | --- |
| **Status** | Accepted — residual window kept, not closed |
| **Date** | 2026-07-28 |
| **Decider** | Repository owner |
| **Investigation** | `phaze-8xbv` (follow-up to `phaze-g31m`) |
| **Supersedes** | — |

______________________________________________________________________

## Context

`trigger_backfill_cloud` (`src/phaze/routers/pipeline.py`) re-drives timed-out long files to the
cloud. Part of that is DELETING the orphaned `process_file:<id>` scheduling-ledger row for each
candidate, after a lock-free `live_keys` snapshot (`phaze-l1km`) has already excluded any candidate
with a currently-queued/active `saq_jobs` row.

`phaze-g31m` closed the wider version of this race: a concurrent re-enqueue (the since-removed
`deepen_analysis`, a recovery replay, /
`retry_analysis_failed`'s background loop) whose ledger write lands in the gap between the
`live_keys` snapshot and the DELETE. The fix CAS-guards the DELETE on the exact `enqueued_at` value
observed immediately before it runs, so a concurrent `upsert_ledger_entry` refresh in that gap
changes the row out from under the DELETE's `WHERE` clause and the row survives.

That CAS closes the case where the concurrent enqueue's **ledger write itself** lands in the gap —
the row visibly changes, so the CAS sees it. It does **not** close a narrower interleaving: the
concurrent enqueue's ledger row was already committed **before** the `live_keys` snapshot, but the
matching `saq_jobs` row insert (the actual broker enqueue) is still pending when the DELETE runs.
In that shape the ledger row's content never changes across the gap — `enqueued_at` was already
what it is — so no read of the row, CAS or otherwise, can distinguish "a live producer's write that
just happens to not have moved `enqueued_at` again yet" from "a genuinely stale orphan". Closing it
requires locking, not a smarter read.

### Why the lock is not a small addition

SAQ's `Queue.enqueue()` (`saq/queue/base.py`) runs the project's `before_enqueue` hook chain —
including `apply_deterministic_key`, which calls `upsert_ledger_entry` — and only **after that
coroutine returns** does it call `self._enqueue(job)`, the step that actually inserts the
`saq_jobs` row:

```python
await self._before_enqueue(job)
return await self._enqueue(job)
```

These are two separate `await`s using two separate connections from two separate pools:

- the ledger write runs on `job.queue.ledger_sessionmaker` — a SQLAlchemy `async_sessionmaker` over
  the project's asyncpg engine (`src/phaze/tasks/_shared/deterministic_key.py`);
- `_enqueue` runs on `PostgresQueue`'s own psycopg3 `AsyncConnectionPool`, entirely internal to the
  `saq` library (`build_pipeline_queue` in `src/phaze/tasks/_shared/queue_factory.py` constructs a
  stock `PostgresQueue`, no subclass).

A Postgres advisory lock is server-side and keyed globally, so it *can* in principle serialize
across those two different pools/drivers. But to actually hold one across this specific gap, the
lock has to be acquired and released around **both** steps as a unit — which means intercepting
`PostgresQueue.enqueue()` itself (subclassing or monkey-patching a third-party method we don't
otherwise touch), pinning one physical connection out of its internal pool for the full
`before_enqueue` + `_enqueue` span instead of letting SAQ check connections in and out per call, and
choosing a lock granularity for backfill's side: a per-key lock means acquiring and releasing one
lock per candidate around the snapshot+DELETE (contending only with that key's own producer), while
anything coarser serializes backfill's DELETE step against **every** `process_file` enqueue in the
system, cloud-routed or not, for the duration of the backfill click.

That is a real integration surface change to a third-party queue library, for a single-user,
low-throughput deployment, to close a window that is one DB round trip wide and already
self-limiting: a candidate the DELETE misses stays a live ledger row (correctly — the file remains
in-flight) and is simply left for the next backfill click, exactly like the `phaze-g31m` CAS-miss
case already handled. It never produces a double-dispatch (the failure mode the whole family of
checks in this endpoint exists to prevent) — it only produces a missed opportunistic reap, which
running the same idempotent backfill click again resolves.

## Decision

**Accept the residual window permanently.** No advisory-lock serialization is added between
`trigger_backfill_cloud`'s ledger reap and the SAQ `before_enqueue` chokepoint
(`upsert_ledger_entry`).

The in-code comment in `trigger_backfill_cloud` is updated to point at this record instead of
describing the gap as open follow-up work.

## Consequences

- No new lock contention or third-party integration surface is added to the enqueue hot path.
- The window is bounded (one DB round trip), does not cause double-dispatch, and self-heals on the
  next backfill invocation — consistent with how the `phaze-g31m` CAS-miss case is already handled.
- If phaze ever moves beyond a single-user deployment, or `saq` grows a first-class hook that spans
  `before_enqueue` through the actual job insert, this decision should be revisited — the analysis
  above (particularly the two-pool problem) is what would need to change for the lock to become a
  clean addition rather than a library-internals intrusion.

## Reinstatement

Not applicable — this is a permanent acceptance, not a deferral. Re-open only if the deployment
model or the `saq` dependency changes in a way that invalidates the "single-user, self-limiting"
premise above.
