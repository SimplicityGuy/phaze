# Agent Queue Lanes — Design

**Date:** 2026-07-07
**Status:** Approved (design), pending implementation plan
**Scope:** nox file-server agent worker only. **k8s burst clusters are unaffected** (burst pods are one-shot `phaze.job_runner`, not the persistent SAQ worker).

## Problem

The nox agent runs a **single** SAQ worker with one shared concurrency pool
(`concurrency = worker_max_jobs`, default 8) that serves every file-touching task
(`agent_worker.py` `settings["functions"]`). Two consequences:

1. **I/O offload is starved.** `s3_upload` (httpx multipart PUT) and `push_file`
   (rsync-over-SSH) compete for the same slots as CPU-bound essentia analysis.
   When there is a local analysis backlog, offload jobs sit behind it — on
   2026-07-07 this left 4 files stuck in `pushing`. The user wants offload to
   happen on-demand in its own lane, "then and there."
2. **Head-of-line blocking across analysis types.** A deep `process_file`
   backlog makes a newly-enqueued `fingerprint_file` or `extract_file_metadata`
   wait behind it, even though those are cheaper / differently-bound.

## Hard constraint

nox has **8 physical cores** and is already at load ~18 (the current single pool
oversubscribes — essentia/TensorFlow spawns threads *inside* each slot).
`process_file` (essentia, in-process) and `fingerprint_file` (drives the
panako/audfprint **sidecar containers**, which burn CPU on the same 8 cores) are
both CPU-bound against a **shared, finite** core budget.

Therefore per-type lanes buy **fairness / no head-of-line-blocking**, not
unlimited parallelism. CPU-bound concurrency across lanes must **sum to ≈ cores**.
I/O lanes (`s3_upload`, `push_file`) are network-bound and run **off** the CPU
budget.

## Design

### Lanes — 4 queues, one worker each, tiered by real cost

| Lane          | Tasks                                                                          | Bound by                         | Default concurrency (env)                     |
|---------------|--------------------------------------------------------------------------------|----------------------------------|-----------------------------------------------|
| `analyze`     | `process_file`                                                                 | Host CPU (in-process essentia)   | 4  (`PHAZE_LANE_ANALYZE_CONCURRENCY`)         |
| `fingerprint` | `fingerprint_file`                                                             | Host CPU (via panako/audfprint)  | 2  (`PHAZE_LANE_FINGERPRINT_CONCURRENCY`)     |
| `meta`        | `extract_file_metadata`, `scan_directory`, `scan_live_set`, `execute_approved_batch` | Light / fast                | 2  (`PHAZE_LANE_META_CONCURRENCY`)            |
| `io`          | `s3_upload`, `push_file`                                                        | Network (off CPU budget)         | 4  (`PHAZE_LANE_IO_CONCURRENCY`)              |

CPU budget: `analyze(4) + fingerprint(2) = 6` CPU-bound slots on 8 cores, leaving
headroom for the fast `meta` lane, sidecar overhead, and the OS. All concurrencies
are env-overridable; defaults above.

### Predictable core accounting

Pin essentia/TensorFlow to single-threaded so one `analyze` slot ≈ one core and
the budget is honest: `OMP_NUM_THREADS=1`, TF intra-op = inter-op = 1 (set in the
agent image / lane env). This directly addresses the load-18-on-8-cores
oversubscription observed today.

### Task → lane map (single source of truth)

Introduce `LANE_TASKS: dict[str, frozenset[str]]` as the canonical mapping,
replacing the flat `AGENT_TASKS` frozenset in `services/enqueue_router.py`.
`AGENT_TASKS` becomes a derived union (kept for any existing membership checks).
The map is the ONE place task→lane membership lives; both the producer
(`enqueue_router`) and the consumer (worker settings) derive from it, mirroring
the existing "MUST mirror" contract between `AGENT_TASKS` and
`agent_worker.settings["functions"]`.

### Queue naming

Per-agent, per-lane: `phaze-agent-<agent_id>-<lane>` (e.g. `phaze-agent-nox-analyze`).
Derived from the existing `PHAZE_AGENT_QUEUE` base (`phaze-agent-nox`) + `-<lane>`
suffix. `enqueue_router` resolves `(agent, task) → lane → queue name`; the routed
enqueue targets that lane's queue.

### Worker settings — parametrized by lane

Replace the single `agent_worker.settings` with a lane-parametrized builder driven
by `PHAZE_AGENT_LANE`. For the selected lane it:
- builds its queue via the existing `build_pipeline_queue` seam (same
  `before_enqueue` hook chain, deterministic keys, cache-redis counters — all
  per-queue, unchanged),
- registers **only** that lane's functions (from `LANE_TASKS`),
- sets `concurrency` from that lane's env knob.

Keep the SAQ entry-point contract intact (`saq <module>.settings` reads a
top-level `settings` attribute). Expose one settings module per lane (or one
module that resolves `settings` from `PHAZE_AGENT_LANE` at import — planner's
call, but the entry-point must remain a static top-level attribute).

The liveness heartbeat (Phase 46 asyncio background task) is **agent-level, not
lane-level** — it should run in exactly one lane worker (the `analyze` lane, or a
dedicated flag) to avoid N duplicate heartbeats per agent. Registration
(`agent_id = nox`) is unchanged and remains a single identity across lanes.

### Deployment — homelab compose only

Run the 4 lanes as **4 compose services from the one existing agent image**
(`Dockerfile.agent-arm64`), each with its own `command` / `PHAZE_AGENT_LANE` and
concurrency env. No new image, no in-container supervisor — compose owns process
management. `agent_watcher` and the panako/audfprint sidecar services are
unchanged. **No k8s change** (burst pods are one-shot).

Files: `docker-compose.agent.yml` (homelab), and the x86 `docker-compose.agent.yml` /
`docker-compose.cloud-agent.yml` variants if they should mirror the lane split
(confirm during planning — the arm64 nox homelab agent is the primary target).

### Migration

New enqueues route to lane queues immediately. In-flight jobs on the legacy
`phaze-agent-nox` queue must drain: keep a transitional consumer of the legacy
queue until it reports empty, then remove it. The scheduling-ledger and
deterministic-key dedup (`s3_upload:<file_id>`, etc.) are keyed by file_id, not
queue, so a job re-driven onto a lane queue collapses correctly. Nail the exact
drain mechanism in the plan (options: a short-lived legacy-queue worker, or a
one-off reconcile that re-enqueues legacy-queue jobs onto lane queues).

## Non-goals

- No change to the k8s burst path or either cluster.
- No change to the control-plane controller worker (its `CONTROLLER_TASKS` queue
  is separate and unaffected).
- Job **priority** (existing backend feature) is orthogonal and out of scope here.

## Testing

- Unit: `LANE_TASKS` totality (every `AGENT_TASKS` member maps to exactly one lane;
  no orphans, no duplicates), queue-name derivation, lane→functions selection.
- Router: `enqueue_router` routes each task to the correct lane queue.
- Worker settings: each lane builds a queue with the right name, functions, and
  concurrency; heartbeat runs in exactly one lane.
- Maintain the 90% coverage gate; `uv run pytest` per affected bucket, `ruff`,
  `mypy` clean, pre-commit (never `--no-verify`).

## Rollout note (deferred — ops)

Landing this in prod requires a homelab redeploy (new compose services) plus the
already-pending presign-409 deploy. Both are parked under the user's "ops later"
decision; this spec covers the code + compose change only.
