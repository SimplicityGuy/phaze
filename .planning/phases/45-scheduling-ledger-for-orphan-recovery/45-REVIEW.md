---
phase: 45-scheduling-ledger-for-orphan-recovery
reviewed: 2026-06-19T00:00:00Z
depth: standard
files_reviewed: 20
files_reviewed_list:
  - src/phaze/models/scheduling_ledger.py
  - src/phaze/models/__init__.py
  - alembic/versions/022_add_scheduling_ledger.py
  - src/phaze/services/scheduling_ledger.py
  - src/phaze/services/pipeline.py
  - src/phaze/tasks/_shared/deterministic_key.py
  - src/phaze/tasks/_shared/queue_factory.py
  - src/phaze/tasks/controller.py
  - src/phaze/tasks/reenqueue.py
  - src/phaze/tasks/scan.py
  - src/phaze/routers/agent_analysis.py
  - src/phaze/routers/agent_metadata.py
  - src/phaze/routers/agent_fingerprint.py
  - src/phaze/routers/agent_tracklists.py
  - src/phaze/schemas/agent_tracklists.py
  - src/phaze/services/agent_client.py
  - src/phaze/services/agent_task_router.py
  - src/phaze/main.py
  - tests/test_tasks/test_ledger_backfill.py
  - tests/test_tasks/test_recovery.py
findings:
  critical: 2
  warning: 6
  info: 4
  total: 12
status: issues_found
---

# Phase 45: Code Review Report

**Reviewed:** 2026-06-19
**Depth:** standard
**Files Reviewed:** 20
**Status:** issues_found

## Summary

Phase 45 introduces a durable `scheduling_ledger` table so orphan-recovery re-queues only
previously-scheduled-and-lost work, closing the 2026-06-18 over-enqueue incident. The architecture is
sound: the WRITE hook is at the single `before_enqueue` chokepoint, the agent Postgres-free boundary is
preserved via function-local lazy imports gated on `ledger_sessionmaker`, the `saq_jobs` reads run inside
SAVEPOINTs, clear keys are reconstructed from PATH ids only (never body fields), and the backfill is
idempotent (`ON CONFLICT DO NOTHING`). The incident regression is well-tested.

However, two correctness gaps remain in the agent-side terminal-ack paths that directly undermine the
ledger's "exactly-once clear" invariant — they can leave orphaned ledger rows that re-enqueue on every
recovery, or convert a legitimate no-match into a retrying failure. Several robustness/consistency
warnings round out the review.

## Critical Issues

### CR-01: `scan_live_set` no-match ack is unguarded — a controller hiccup turns a clean no-match into a retrying failure and leaks the ledger row

**File:** `src/phaze/tasks/scan.py:96-100`
**Issue:** On the no-match path the terminal ack is called with no error handling:

```python
if not matches:
    await api.report_scan_terminal(payload.file_id)
    return {"file_id": str(payload.file_id), "status": "no_matches"}
```

`report_scan_terminal` routes through the tenacity funnel and raises `AgentApiServerError` if the
controller is down/5xx after retries (or `AgentApiAuthError`/`AgentApiClientError` on 4xx). When it
raises:
- the function never returns `no_matches` — the job records a FAILED attempt and SAQ retries it;
- on a retryable attempt the `scan_live_set:<file_id>` ledger row is NOT cleared, so recovery will
  re-enqueue this file on every pass — exactly the "no-match re-enqueues on every recovery" failure the
  endpoint was added to prevent (Blocker 2 / T-45-16).

The match path (scan.py:119-138) carefully gates its ack on `not job.retryable` and re-raises so the
row survives a real retry but clears exactly once on the terminal attempt. The no-match path has no such
discipline.
**Fix:** Mirror the match-path terminal discipline — only ack-and-swallow on the terminal (non-retryable)
attempt, and let a retryable failure re-raise so the row survives for the real retry:
```python
if not matches:
    try:
        await api.report_scan_terminal(payload.file_id)
    except Exception:
        job = ctx.get("job")
        if job is not None and job.retryable:
            raise  # let SAQ retry; the row survives for the real retry
        # terminal attempt: best-effort, do not block the no-match COMPLETE
        logger.warning("scan_live_set no-match terminal-ack failed", file_id=str(payload.file_id), exc_info=True)
    return {"file_id": str(payload.file_id), "status": "no_matches"}
```

### CR-02: agent-stage ledger clears are not durable against a successful result write followed by a recovery race — terminal `failed` result writes never advance metadata/fingerprint state, so their domain-completed predicate can never fire

**File:** `src/phaze/tasks/reenqueue.py:107-205`, `src/phaze/routers/agent_metadata.py:70-74`, `src/phaze/routers/agent_fingerprint.py:48-54`
**Issue:** For `extract_file_metadata` and `fingerprint_file`, the ONLY ledger clear is in the success
PUT handler (`put_metadata` / `put_fingerprint`). The module docstring (reenqueue.py:48-49) explicitly
notes these stages have "NO `/failed` callback (Plan 02 residual gap)", so the domain-completed predicate
is their *primary* net. But that predicate is unreliable for the very rows recovery must handle:

- `is_domain_completed` for `extract_file_metadata` returns True iff the file is NOT in
  `get_metadata_pending_files` — which is *all music/video files* (pipeline.py:670). So a real music file
  that was scheduled, failed terminally (retries exhausted, no callback, row never cleared) is STILL in
  the pending set → NOT domain-completed → recovery re-enqueues it on every pass indefinitely.
- `fingerprint_file`: `get_fingerprint_pending_files` returns `METADATA_EXTRACTED` files. A file that
  failed fingerprinting terminally but is still `METADATA_EXTRACTED` is in the pending set → re-enqueued
  forever; if it has a `FingerprintResult(status="failed")` it is *also* in the pending set (pipeline.py:692-697),
  so even the "failed result was written" case keeps re-queuing.

The net effect: a terminally-failed metadata/fingerprint job whose result/state was never advanced past
its gate becomes a permanent recovery re-enqueue loop (bounded per-pass by the deterministic-key dedup,
so it won't *double*, but it will re-enqueue every recovery and never drain). This is the same class of
"never terminates" defect the ledger was meant to fix, just narrowed to the no-failed-callback stages.
**Fix:** Give metadata/fingerprint a terminal-failure ack endpoint that clears the ledger row (mirroring
`agent_analysis.report_analysis_failed` and `ack_scan_terminal`), OR have the agent worker's
retries-exhausted handler PUT a sentinel result that advances state past the gate, OR make
`is_domain_completed` for these stages consult a real "terminal failed" signal rather than the
complement-of-pending. Without one of these, the documented "residual gap" is an unbounded re-enqueue
source for any terminally-failed metadata/fingerprint job.

## Warnings

### WR-01: `create_tracklist` clears the ledger using `body.file_id`, but the clear key is built from a body-supplied field rather than a trusted path/auth value

**File:** `src/phaze/routers/agent_tracklists.py:168`
**Issue:** Unlike every other agent handler in this phase (which reconstruct the clear key from the PATH
`file_id` only, per AUTH-01 / T-45-05), `create_tracklist` clears
`scan_live_set:{body.file_id}` from a request-body field. The docstring calls `body.file_id` "the trusted
tracklist target", but it is attacker-influenceable wire input — a caller can POST a tracklist with an
arbitrary `file_id` and clear a *different* file's `scan_live_set` ledger row, which would make that other
file's genuinely-orphaned scan invisible to recovery. The other handlers were specifically hardened
against exactly this; this one is the inconsistent outlier.
**Fix:** This endpoint has no path `file_id`, so either (a) move the scan-terminal clear out of
`create_tracklist` and rely solely on the explicit `ack_scan_terminal`/`report_scan_terminal` path
(scan.py already acks on the match path via `create_tracklist`'s side effect — make that an explicit
`report_scan_terminal` call from the task instead), or (b) document and accept the single-operator trust
model explicitly the way `TracklistCreatePayload` already accepts request-id reuse. Given the rest of the
phase enforces path-only keys, prefer (a).

### WR-02: `force=True` recovery re-runs the WRITE hook on every replay, refreshing `enqueued_at`/`payload` for rows that dedup to no-ops

**File:** `src/phaze/tasks/reenqueue.py:208-223`, `src/phaze/tasks/_shared/deterministic_key.py:117-140`
**Issue:** `_replay_row` calls `queue.enqueue(...)`, which fires `apply_deterministic_key`'s ledger
`upsert_ledger_entry` (ON CONFLICT DO UPDATE) *before* SAQ's per-key dedup decides the job is a no-op.
So a forced reconcile over a live queue (the operator "Recover" button path) rewrites `enqueued_at` and
`payload` for every still-live item even though no new job is enqueued. This is mostly harmless but it
means `enqueued_at` no longer reflects the original schedule time after any forced recovery, and a replay
payload could overwrite a fresher hook-written payload for a live row. Combined with CR-02, a repeatedly
re-enqueued failed row keeps bumping its own `enqueued_at` forever.
**Fix:** Either accept and document this (the upsert is idempotent and the row content is identical for a
deterministic replay), or have `_replay_row` skip the WRITE hook for replays (e.g. enqueue through a path
that does not refresh the ledger). At minimum, document that `enqueued_at` is "last (re)enqueue", which
the model docstring already implies but recovery's refresh-on-no-op is worth calling out.

### WR-03: `_replay_row` calls `queue.connect()` for the controller queue inside the recovery transaction, but the controller queue's ledger WRITE hook opens a nested session on the same sessionmaker

**File:** `src/phaze/tasks/reenqueue.py:208-223`, `src/phaze/tasks/controller.py:112`
**Issue:** Recovery runs the whole replay loop inside `async with ctx["async_session"]() as session`.
Each `_replay_row` → `enqueue` → `apply_deterministic_key` opens ANOTHER session from the SAME
`ctx["async_session"]` (the controller queue's `ledger_sessionmaker` IS `ctx["async_session"]`). For a
large orphan set this holds the outer read session open while serially opening+committing a second
session per row. It is correct (separate sessions, `pool_size=10`), but on a genuine queue-loss with
thousands of orphaned rows this is N short-lived nested sessions under one long-held outer session — a
pool-pressure and latency risk on the boot path. Not a correctness bug, but worth bounding.
**Fix:** Read the ledger rows / live keys / done sets into memory, close the outer read session before
the replay loop, then replay (the WRITE hook manages its own sessions). This shortens the outer
session's lifetime and avoids holding a read connection across the entire enqueue storm.

### WR-04: backfill `inserted` tally is a misleading upper bound that counts DO-NOTHING no-ops as inserts

**File:** `src/phaze/tasks/reenqueue.py:392-393`, `src/phaze/tasks/controller.py:146`
**Issue:** `backfill_ledger_from_saq_jobs` increments `tally["inserted"]` for every
`insert_ledger_if_absent` *call*, not every row actually written. Because the conflict clause is DO
NOTHING, a row already present from the WRITE hook counts as "inserted" anyway. The startup log
(`controller.py:146`) reports `inserted=tally["inserted"]`, so an operator reading boot logs after the
transition cohort has drained will see a non-zero "inserted" that overstates real writes — the integration
test even documents it must assert row counts, not this tally (test_ledger_backfill.py:322-325). A
misleading observability counter on the exact incident-recovery path is a real operational hazard.
**Fix:** Use `RETURNING` (or `result.rowcount`) on `insert_ledger_if_absent` to count rows actually
inserted, and rename the existing counter to `attempted`/`seeded_calls` if you want to keep it. The
startup log should report the true write count.

### WR-05: `_build_done_sets` for the `generate_proposals` / batch case is silently absent; a batch ledger row is never domain-completed even when its files are done

**File:** `src/phaze/tasks/reenqueue.py:128-205`
**Issue:** `generate_proposals` is correctly classified live-keys-only (not in
`_DOMAIN_COMPLETED_STAGES`), and `_natural_id` only understands `file_id` (the batch payload carries
`file_ids`, a list). So a `generate_proposals` ledger row relies entirely on its after_process clear +
the live-key filter. That is the intended design, but the after_process clear for `generate_proposals`
runs only on the *controller* worker (deterministic_key.py:174-190), and the manual UI trigger enqueues
it via `app.state.controller_queue` whose `ledger_sessionmaker` is the API engine. If a batch job is
enqueued by the API (ledger row written by API engine) but the controller worker's after_process clear
runs against the controller engine, the clear DOES land (same DB, different pool) — but only if the
controller worker actually has `ledger_sessionmaker` wired, which it does (controller.py:112). This is
fine *today*, but it is load-bearing and undocumented that the clear and write may hit different engines;
a future change that drops `ledger_sessionmaker` from either side silently reintroduces a leak.
**Fix:** Add an explicit assertion/test that the controller worker's after_process clear path has
`ledger_sessionmaker` set, and document that write-engine and clear-engine may differ but must point at
the same database.

### WR-06: ledger `key` PK is `String(255)` but `generate_proposals` keys are `generate_proposals:<sha256-hex>` (~80 chars) while file-keyed entries embed UUIDs — no length validation guards a future longer natural id

**File:** `src/phaze/models/scheduling_ledger.py:51`, `alembic/versions/022_add_scheduling_ledger.py:53`
**Issue:** `key` is `String(255)`. Today's keys fit (`<function>:<uuid>` ≈ 55 chars,
`generate_proposals:<64-hex>` ≈ 83 chars). But the deterministic key is built unconditionally from
`f"{job.function}:{builder(job.kwargs)}"` with no length cap (deterministic_key.py:103). If a future
keyed function used a longer natural id (e.g. a path), the WRITE hook's `upsert_ledger_entry` would raise
a `StringDataRightTruncation` inside the best-effort try/except — silently degrading to "row not
written" and a permanent recovery blind spot for that stage. The 255 limit is an implicit contract on
all current and future natural ids.
**Fix:** Either document the 255-char key contract loudly at the `_KEY_BUILDERS` definition, or widen
`key` to `Text` (Postgres TEXT has no meaningful length penalty and the PK index handles it), removing
the silent-truncation footgun.

## Info

### IN-01: `recover_orphaned_work` casts settings then discards it

**File:** `src/phaze/tasks/reenqueue.py:256`
**Issue:** `_ = cast("ControlSettings", get_settings())` is a no-op the docstring admits recovery no longer
reads. It exists only "for parity". Dead-ish code that invites confusion.
**Fix:** Remove the cast and the `get_settings` import if nothing else uses it; the role contract is
already enforced by the controller-only registration.

### IN-02: recovery imports a private name `_KEY_BUILDERS` across modules

**File:** `src/phaze/tasks/reenqueue.py:80,305`, `tests/test_tasks/test_recovery.py:44`
**Issue:** `reenqueue` and the tests import the leading-underscore `_KEY_BUILDERS` from
`deterministic_key`. The cross-module use is deliberate (documented at reenqueue.py:301-305) but a
leading underscore signals "module-private" to readers and linters.
**Fix:** Promote the keyed-function universe to a public name (e.g. `KEYED_FUNCTIONS` or
`KEY_BUILDERS`) exported in `__all__`, and have `_KEY_BUILDERS` alias it if you want to preserve the
internal name.

### IN-03: `_natural_id` / `is_domain_completed` stringly-typed done-set keys rely on module-level constants defined AFTER first use in source order

**File:** `src/phaze/tasks/reenqueue.py:150-162`
**Issue:** `_build_done_sets` (line 150) references `_ANALYZE_DONE`/`_METADATA_PENDING`/
`_FINGERPRINT_PENDING`, which are defined at lines 160-162 — after the function. This works (they resolve
at call time, not def time) but reads as use-before-define and is fragile to refactoring.
**Fix:** Move the three constant definitions above `_build_done_sets`.

### IN-04: integration tests create a hand-rolled `saq_jobs` schema that can drift from SAQ's canonical migration

**File:** `tests/test_tasks/test_ledger_backfill.py:265-279`
**Issue:** The integration test `CREATE TABLE IF NOT EXISTS saq_jobs (...)` reproduces SAQ's schema by
hand with a comment acknowledging it must match `saq.queue.postgres_migrations`. A future SAQ schema bump
will silently diverge, and `IF NOT EXISTS` means a stale shared table is reused rather than corrected.
**Fix:** Build a real `PostgresQueue` and call its `init_db()` to materialize the canonical schema, or
key the test on a SAQ-version guard, rather than maintaining a parallel DDL copy.

---

_Reviewed: 2026-06-19_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
