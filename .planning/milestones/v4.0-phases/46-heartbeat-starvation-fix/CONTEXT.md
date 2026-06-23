# Phase 46 Context — Heartbeat Starvation Fix

> Source: live incident triage on the nox homelab (v4.4.0), 2026-06-23.
> Agent `nox` showed **DEAD** (last seen 39m ago) in the admin UI while the
> `phaze-agent-worker` container was healthy and pegged at **~394% CPU / 18 GB RAM**,
> actively analyzing. Operator could not start fingerprinting ("No files ready") because
> the busy-but-DEAD agent also blocks new agent-task routing.

## Root cause

The agent liveness heartbeat competes for the **same SAQ job-dispatch concurrency slots**
as long-running analysis jobs, so a saturated worker cannot report liveness.

- `heartbeat_tick` is registered as a SAQ `CronJob` **in the agent worker**
  (`src/phaze/tasks/agent_worker.py:227`), alongside `process_file` in the same `functions`
  list and the same worker `concurrency = worker_max_jobs` (`agent_worker.py:229`, default
  **8** — `config.py:222`, no env override on nox).
- The cron fires every 30s (`"* * * * * */30"`, `unique=True`) and **enqueues a
  `heartbeat_tick` job** onto the `phaze-agent-nox` queue. That job must acquire one of the
  8 dispatch slots to run.
- All 8 slots are occupied by `process_file` analysis jobs that each take **2–3.6 hours**
  (long concert sets). Confirmed from worker logs: jobs `99c67ef6` (2.8h), `bc284e41` (3.6h),
  `e6bed181` (3.3h), `a37c4e5e` (3.0h)…
- Result: `heartbeat_tick` only runs when a multi-hour job frees a slot — observed gaps of
  **50+ minutes** between heartbeats (e.g. enqueued 15:35:59, not processed until 16:29:16).
- The liveness classifier flips an agent to `dead` after **300s (5 min)** of `last_seen`
  staleness (`src/phaze/constants.py:61`, `AGENT_LIVENESS_STALE_SECONDS`). A heartbeat every
  ~50 min is ~100× too slow → the healthy agent is permanently `DEAD`.

**Live confirmation:** control plane shows "last seen 39m ago" (not "never") → heartbeats DO
reach the control plane and the network/POST path is fine; the heartbeat simply cannot get CPU
time. `docker stats` = 393.96% CPU; container "Up 2 days"; `worker_max_jobs` env unset (=8).

## Decisive architecture finding (settles the fix design)

The worker's **asyncio event loop is NOT blocked** by analysis. Phase 43 moved essentia off
the event loop into a separate process pool:

- `process_file` is `async def` and does `await run_in_process_pool(...)`
  (`src/phaze/tasks/functions.py:139,160`), where `run_in_process_pool` dispatches the
  CPU-bound essentia work to a **`pebble.ProcessPool`** (`src/phaze/tasks/pool.py`, Phase 43,
  replacing the un-killable `ProcessPoolExecutor`).
- So the 8 concurrent `process_file` coroutines are merely **awaiting subprocess results** —
  the event loop has abundant free time. The 394% CPU is the pool's child processes, not the
  loop thread.

Therefore the **only** thing starving the heartbeat is SAQ's job-dispatch semaphore
(`worker_max_jobs`), not the event loop. A heartbeat that runs **outside SAQ job dispatch**
will tick reliably.

## Recommended approach (for planning to confirm/refine)

**Run the heartbeat as an asyncio background task in the worker startup hook**, decoupled from
SAQ's job concurrency pool entirely:

1. In `startup(ctx)` (`agent_worker.py:75`, after `ctx["api_client"]` / `ctx["agent_identity"]`
   are built), launch `ctx["heartbeat_task"] = asyncio.create_task(_heartbeat_loop(ctx))`.
2. `_heartbeat_loop` sleeps ~30s and calls the existing heartbeat-send logic (refactor the body
   of `heartbeat_tick`, `src/phaze/tasks/heartbeat.py:37`, into a reusable
   `send_heartbeat(ctx)` coroutine; the loop wraps it with sleep + broad try/except so one
   failure never kills the loop).
3. In `shutdown(ctx)` cancel + await `ctx["heartbeat_task"]`.
4. **Remove** the `CronJob(heartbeat_tick, …)` from `cron_jobs` (`agent_worker.py:223-228`) so
   it no longer consumes a dispatch slot. Decide whether to keep `heartbeat_tick` in `functions`
   for back-compat or drop it (tests reference it — `tests/test_task_split.py` import-boundary).

Why not the alternatives:
- **Separate heartbeat container / second worker** — works but adds a container to the homelab
  compose for what a background task solves in-process. Overkill.
- **Reserve a concurrency slot for cron/system jobs** — SAQ has no first-class slot reservation;
  fragile, and unnecessary once the heartbeat leaves the dispatch pool.
- **Dedicated OS thread** — only needed if the event loop were CPU-blocked; it is NOT (pebble
  pool finding above), so a plain asyncio task suffices.

## Constraints / notes for the plan

- Heartbeat must still report `queue_depth` via `ctx["worker"].queue.info()` — but `ctx["worker"]`
  is set by SAQ when it constructs the Worker; confirm it is populated before/inside the loop
  (the loop can read it lazily each tick, and degrade `queue_depth=0` on any error, as today —
  `heartbeat.py:62`).
- Preserve the existing defensive behavior: ctx-not-initialized guard, `queue.info()` failure →
  `queue_depth=0` + still POST, `AgentApiError` → WARNING + continue (next tick).
- Keep DEBUG-level "heartbeat sent" logging (the 30s cadence would flood at INFO — `heartbeat.py:73`).
- This is **distinct from Phase 43** (analyze throughput / bounding job cost). Phase 43 bounds how
  long a job runs; Phase 46 guarantees liveness *regardless* of how long jobs run. Even a
  perfectly bounded 4h job would still have starved the heartbeat under the old design.
- Out of scope: the analysis-backlog throughput problem itself (months of runtime at 8×~3h) —
  tracked separately (Phase 43 redeploy + the cloud-burst backlog item).

## Verification target

A worker with all `worker_max_jobs` slots saturated by long `process_file` jobs still POSTs a
heartbeat within the 90s `AGENT_LIVENESS_ALIVE_SECONDS` window, so the agent stays `alive`
(never `stale`/`dead`) throughout a multi-hour analysis run.
