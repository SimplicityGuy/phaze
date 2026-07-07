---
phase: 76-compute-push-hardening
reviewed: 2026-07-06T00:00:00Z
depth: standard
files_reviewed: 7
files_reviewed_list:
  - src/phaze/services/backends.py
  - src/phaze/routers/agent_push.py
  - src/phaze/routers/tracklists.py
  - src/phaze/routers/pipeline_scans.py
  - tests/shared/services/test_lane_snapshot.py
  - tests/agents/routers/test_agent_push.py
  - tests/shared/routers/test_pipeline_scans.py
  - tests/identify/routers/test_tracklists.py
findings:
  critical: 1
  warning: 1
  info: 1
  total: 3
resolved:
  critical: 1
  warning: 1
status: resolved
resolution_note: "CR-01 fixed via pg_advisory_xact_lock (operator-approved, supersedes D-05 .with_for_update); WR-01 docstring corrected to N x timeout bound. Both re-verified green; IN-01 (Info, docstring-dup nit) left as-is."
---

# Phase 76: Code Review Report

**Reviewed:** 2026-07-06
**Depth:** standard
**Files Reviewed:** 8 (4 source + 4 test)
**Status:** resolved (was issues_found)

## Resolution (post-review, same execution)

- **CR-01 (Critical) — FIXED.** The `.with_for_update()` ledger row lock self-deadlocked against the `apply_deterministic_key` before_enqueue hook (nested upsert on the same `push_file:<id>` row in its own session while the request held the lock). Operator chose the advisory-lock remediation: replaced the row lock with `pg_advisory_xact_lock(hashtext(key))` — same RMW serialization, different lock space, no deadlock. Added `test_mismatch_real_enqueue_hook_does_not_deadlock` (drives the REAL hook; RED-verified: times out on the row lock, passes on the advisory lock). Commit `fix(76-02): use advisory xact lock, not row lock, for push_attempt RMW (CR-01)`.
- **WR-01 (Warning) — FIXED.** `_probe_availability` docstring corrected to state the true `N x _PROBE_TIMEOUT_SEC` aggregate bound (deliberate D-01 trade-off) instead of implying the old ~1x `asyncio.gather` bound. Commit `docs(76-01): correct _probe_availability latency bound to N x timeout (WR-01)`.
- **IN-01 (Info) — left as-is** (minor docstring-duplication nit between `_probe_one`/`_probe_availability`; no functional impact).

## Summary

HARD-01 (probe serialization) and HARD-03 (agent_id boundary validation) are clean,
byte-precise implementations of their plans: the diffs match the CONTEXT/plan decisions
exactly, the regex is verbatim-identical to the canonical `Agent.id` CHECK constraint and
`AGENT_ID_RE`, there is no ReDoS risk (confirmed Pydantic v2's pattern validator performs a
full-string match, not a `re.match`-with-trailing-`$`-newline-bypass), and the new regression
tests are well-targeted.

HARD-02, however, introduces a **new correctness bug** while fixing the one it targeted. Adding
`.with_for_update()` to the ledger SELECT correctly closes the lost-update race between two
concurrent `/mismatch` requests (D-05's stated goal), but the lock is now held across the
`fileserver_queue.enqueue(...)` call in the same request — and that call's own `before_enqueue`
hook chain (`apply_deterministic_key` → `upsert_ledger_entry`) opens a **second, independent**
DB session and performs an `INSERT ... ON CONFLICT (key) DO UPDATE` against the **exact same
`scheduling_ledger` row** (same `push_file:<file_id>` key), from a session sharing the same
connection pool. That nested write blocks on the row lock the outer transaction is holding, and
the outer transaction cannot release that lock (commit) until the nested call it is waiting on
returns — a self-inflicted lock-wait that will not resolve on its own (no `lock_timeout` /
`statement_timeout` is configured anywhere in the stack). This is not hit by the new regression
tests because they exercise the route through `FakeTaskRouter`/`FakeQueue`, which never invoke
SAQ's real `before_enqueue` hooks or `ledger_sessionmaker` — so the exact interaction this phase's
own fix creates is invisible to its own test suite.

## Critical Issues

### CR-01: `.with_for_update()` in `report_push_mismatch` self-blocks against its own `push_file` enqueue hook

**File:** `src/phaze/routers/agent_push.py:231` (lock acquired) through `:335` (`fileserver_queue.enqueue(...)`) and `:343` (final commit that would release the lock)

**Issue:**

The under-cap re-drive branch of `report_push_mismatch` now does, in order, within **one** open
transaction on `session`:

1. `L231`: `select(SchedulingLedger).where(key == ledger_key).with_for_update()` — acquires and
   holds an exclusive row lock on the `push_file:<file_id>` ledger row (this row normally already
   exists at `/mismatch` time, since it was created when the original `push_file` job was first
   enqueued).
2. `L301`-`L332`: resolves `FileRecord`, the fileserver agent, and builds the re-drive
   `PushFilePayload` — all still inside the same open transaction, lock still held.
3. `L335`: `await fileserver_queue.enqueue("push_file", key=ledger_key, ...)`. `fileserver_queue`
   is built by `AgentTaskRouter.queue_for(...)` → `build_pipeline_queue(...)`, which registers
   `apply_deterministic_key` as a SAQ `before_enqueue` hook and — because `main.py:119/130`
   construct the app's `task_router` with `ledger_sessionmaker=async_session` (the **same**
   sessionmaker/engine `get_session` uses) — that hook's `ledger_sessionmaker` branch fires. For
   `push_file` (present in `_KEY_BUILDERS`), the hook computes `job.key = f"push_file:{file_id}"`
   — the identical `ledger_key` this handler's own `session` already holds `FOR UPDATE` — opens a
   **new** `AsyncSession` via `sm()`, and calls `upsert_ledger_entry(...)` which is
   `INSERT ... ON CONFLICT (key) DO UPDATE` (`src/phaze/services/scheduling_ledger.py:80-91`). Since
   the row already exists, Postgres must take the same row lock to apply the `DO UPDATE`, which is
   currently held by the outer (still-open) transaction.
4. That nested `await session.commit()` inside the hook (`deterministic_key.py:160`) blocks
   indefinitely on the row lock. The outer transaction cannot commit — and thus cannot release the
   lock — until the `await fileserver_queue.enqueue(...)` call at `L335` returns. Neither side can
   make progress: this is a genuine self-inflicted lock-wait within a single request's own control
   flow, not a race between two different requests.

No `lock_timeout` / `statement_timeout` is configured anywhere (`engine = create_async_engine(...)`
in `src/phaze/database.py:24-28` sets only `pool_size`/`max_overflow`), so this will hang for as
long as the client/proxy allows, tying up **two** pooled connections (the outer request session
plus the nested hook session) out of a 5+10-connection pool the whole time. Repeated `/mismatch`
calls under normal operation (not just a race) would each hit this same interaction and could
exhaust the shared connection pool, since the ledger row for a file mid-push almost always already
exists.

This is **not exercised by the new HARD-02 regression tests**: `test_mismatch_concurrent_no_lost_update`
and `test_mismatch_cap_trips_exactly_at_boundary` both drive the route through `_GatedTaskRouter`/
`_GatedQueue` and `FakeTaskRouter`/`FakeQueue` (`tests/_queue_fakes.py`), whose `enqueue()` is a pure
in-memory stub (`tests/_queue_fakes.py:152-164`) — it never calls a real SAQ `before_enqueue` hook and
never touches `ledger_sessionmaker`. The fakes cannot surface this interaction; only the real
`build_pipeline_queue`-constructed queue (as used in production via `AgentTaskRouter`) triggers it.

**Fix:**

Do not hold the ledger row lock across the enqueue call. The RMW that needs atomicity is just
"read `push_attempt`, compute `next_attempt`" versus a concurrent `/mismatch` — it does not need to
hold the lock across the FileRecord/fileserver-agent resolution and the `enqueue()` call. Commit
(and thus release the lock) immediately after establishing `next_attempt`, before doing anything
that can recursively touch the same row:

```python
# Acquire the lock, compute next_attempt, and release the lock in its own short transaction —
# BEFORE any code path that might recurse into the same row (fileserver_queue.enqueue's
# before_enqueue hook writes this exact key via a separate session/connection).
row = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == ledger_key).with_for_update())).scalar_one_or_none()
current_attempt = 0
if row is not None and isinstance(row.payload, dict):
    current_attempt = int(row.payload.get("push_attempt", 0) or 0)
next_attempt = current_attempt + 1
# Stamp the counter immediately and commit here to drop the FOR UPDATE lock before any
# downstream call (enqueue) can recurse into the same row from a different session.
if row is not None:
    await session.execute(update(SchedulingLedger).where(SchedulingLedger.key == ledger_key).values(payload={**(row.payload or {}), "push_attempt": next_attempt}))
    await session.commit()
```

...and then perform the subsequent `fileserver_queue.enqueue(...)` re-drive (which will itself
refresh the row's `payload` via the hook) without an open lock. If the final "stamp `push_attempt`
onto the freshly-enqueued payload" step must still run after `enqueue()` (to keep the merged
payload's other fields fresh), do it as its own `UPDATE ... SET payload = payload || jsonb_build_object('push_attempt', :next_attempt)` (no `FOR UPDATE`, no held lock) rather than reusing the original locked `SELECT`. Whatever the exact restructuring, the guiding constraint is: **never hold `.with_for_update()` across a call that itself writes to the same locked row from a different session** — add a regression test that uses the *real* `build_pipeline_queue`/hook chain (not `FakeQueue`) against the port-5433 test DB to catch this class of bug, since the existing fakes cannot.

## Warnings

### WR-01: HARD-01's reworded docstring overstates the fan-out's latency bound

**File:** `src/phaze/services/backends.py:664-666` (docstring), `589` (`_PROBE_TIMEOUT_SEC`)

**Issue:** Before this phase, `_probe_availability`'s docstring correctly noted that
`asyncio.gather` bounded the **whole** fan-out to ~one `_PROBE_TIMEOUT_SEC` "even when a lane
hangs." The new sequential loop trades that latency guarantee away in exchange for structural
session-safety (an explicit, reasonable, plan-approved trade-off per D-01 — "N is tiny"). However
the reworded docstring still says: *"The fan-out stays bounded because each `_probe_one` is itself
capped by `asyncio.wait_for(..., _PROBE_TIMEOUT_SEC)`"* — this reads as if the fan-out as a whole
remains bounded, but it is now bounded by `N * _PROBE_TIMEOUT_SEC` (1.5s per backend), not a single
`_PROBE_TIMEOUT_SEC`. `get_backend_lane_snapshot` (which calls `_probe_availability`) feeds the 5s
`/pipeline/stats` hot poll (`src/phaze/routers/pipeline.py:661`); if 4+ non-local backends are
simultaneously unreachable/hanging, the probe alone could now take ≥6s — longer than the poll
interval it serves. This won't raise an exception (the degrade-safe outer `try/except` still
applies), but it is a real, previously-absent latency regression the docstring doesn't accurately
describe.

**Fix:** Reword the "stays bounded" sentence to state the actual bound, e.g.: *"Each `_probe_one`
is capped at `_PROBE_TIMEOUT_SEC`, so the total fan-out is bounded by `len(backends) *
_PROBE_TIMEOUT_SEC` in the worst case (all lanes hanging simultaneously) — acceptable because the
registry is small (a handful of backends), per D-01."* This keeps the reader from assuming the old
~1.5s total-latency guarantee still holds.

## Info

### IN-01: `_probe_availability`'s inline "fan-out stays bounded" language is duplicated between the module docstring and function docstring

**File:** `src/phaze/services/backends.py:636-639` and `:664-666`

**Issue:** Both `_probe_one`'s docstring and `_probe_availability`'s reworded docstring separately
describe the timeout-bounding behavior; after D-03's rewording, the two are now slightly
inconsistent in how strongly they imply an aggregate bound (see WR-01). Not a functional issue,
just a documentation-maintenance note: consider consolidating the latency-bound statement in one
place (e.g. only in `_probe_one`, with `_probe_availability` referencing it) so future edits don't
have to keep two prose descriptions of the same timeout math in sync.

**Fix:** Optional consolidation; no code change required.

---

_Reviewed: 2026-07-06_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
