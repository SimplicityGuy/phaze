<!-- gsd:doc quick-260707-dh1 -->
# Agent Queue Lanes

**Status:** implemented (quick-260707-dh1) — code + compose only; a homelab redeploy lands it in prod.
**Scope:** the file-server (nox) SAQ agent worker. **k8s burst clusters are unaffected** (burst pods are one-shot `phaze.job_runner`, not the persistent SAQ worker).

## Why

The nox agent used to run a **single** SAQ worker with one shared concurrency pool
(`concurrency = worker_max_jobs`, default 8) serving every file-touching task. Two failures:

1. **I/O offload starved.** `s3_upload` (httpx multipart PUT) and `push_file` (rsync-over-SSH)
   competed for the same slots as CPU-bound essentia analysis; a local analysis backlog left
   offload jobs stuck (4 files stuck in `pushing`, 2026-07-07).
2. **Head-of-line blocking across analysis types.** A deep `process_file` backlog made a
   newly-enqueued `fingerprint_file` / `extract_file_metadata` wait behind it.

Splitting into per-type **lanes** buys **fairness / no head-of-line blocking** — not unlimited
parallelism. nox has **8 physical cores**; CPU-bound lanes must sum to ≈ cores.

## Lane topology

Each lane is its own SAQ queue `phaze-agent-<agent_id>-<lane>` consumed by one worker that
registers ONLY that lane's functions. The task→lane map lives in ONE place —
`LANE_TASKS` in `src/phaze/services/enqueue_router.py` (the single source of truth; `AGENT_TASKS`
is its derived union). Both the producer (`lane_for_task` / `resolve_queue_for_task`) and the
consumer (the lane worker settings) derive from it.

| Lane          | Tasks                                                                                 | Bound by                        | Concurrency env                          | Default |
|---------------|---------------------------------------------------------------------------------------|---------------------------------|------------------------------------------|---------|
| `analyze`     | `process_file`                                                                        | Host CPU (in-process essentia)  | `PHAZE_LANE_ANALYZE_CONCURRENCY`         | 4       |
| `fingerprint` | `fingerprint_file`                                                                    | Host CPU (panako/audfprint)     | `PHAZE_LANE_FINGERPRINT_CONCURRENCY`     | 2       |
| `meta`        | `extract_file_metadata`, `scan_directory`, `scan_live_set`, `execute_approved_batch`  | Light / fast                    | `PHAZE_LANE_META_CONCURRENCY`            | 2       |
| `io`          | `s3_upload`, `push_file`                                                              | Network (off CPU budget)        | `PHAZE_LANE_IO_CONCURRENCY`              | 4       |

### Core-budget rationale

`analyze(4) + fingerprint(2) = 6` CPU-bound slots on 8 cores, leaving headroom for the fast
`meta` lane, sidecar overhead, and the OS. The `io` lane is network-bound and runs **off** the
CPU budget. All concurrencies are env-overridable.

**`WORKER_MAX_JOBS` is a ceiling in lane mode (quick-260707-g84).** In lane mode the per-lane
concurrency knob (`PHAZE_LANE_<LANE>_CONCURRENCY`) **governs** the worker's concurrency, and
`WORKER_MAX_JOBS` acts only as an upper bound: `concurrency = min(lane knob, worker_max_jobs)`.
So an explicit, lower `WORKER_MAX_JOBS` is authoritative and clamps every lane, but setting
`WORKER_MAX_JOBS` alone does **not** raise a lane above its knob. On the file-server defaults
(lane ≤ 4, `worker_max_jobs` 8) the ceiling never bites and behavior is unchanged. The effective
concurrency, the lane, and whether the ceiling clamped it are logged once at worker startup.

### Thread pinning

essentia/TensorFlow are pinned single-threaded on the CPU lanes (`analyze`, `fingerprint`) so one
slot ≈ one core and the budget stays honest: `OMP_NUM_THREADS=1`, `TF_NUM_INTRAOP_THREADS=1`,
`TF_NUM_INTEROP_THREADS=1` (set in `docker-compose.agent.yml`). This addresses the load-18-on-8-cores
oversubscription observed under the old single pool.

## Heartbeat — every lane beats, tagged with its lane (phaze-30fo)

The liveness heartbeat (Phase 46 asyncio background task) runs in **every** lane worker —
`PHAZE_AGENT_HEARTBEAT=true` on all four (`docker-compose.agent.yml`) — and each beat carries a
`lane` tag (`analyze` | `fingerprint` | `meta` | `io`).

This **replaced** the original quick-260707-dh1 convention (heartbeat on exactly `worker-analyze`,
false on the other three). Pinning the agent's entire liveness signal to one process meant that when
that process stalled, the agent was classified DEAD after 300s while its other three lanes were
actively working (observed on nox, 2026-07-18). That was never only a display bug:
`Agent.last_seen_at` is also the **work-routing key** — `enqueue_router.select_active_agent` orders by
`last_seen_at DESC` — so a stale beat sorted the busiest machine in the fleet to the bottom and cost
it work (`src/phaze/tasks/agent_worker.py`, `src/phaze/routers/agent_heartbeat.py`).

Two consequences, both handled server-side:

- **`last_seen_at` is refreshed by ANY lane's beat.** It is always set to `now()`, so it is inherently
  `max(last_seen)` across lanes — no explicit `GREATEST` needed, and one stalled lane can no longer
  paint the whole agent DEAD.
- **`last_status` keeps a per-lane breakdown under `lanes`, and the top-level `queue_depth` is the
  cross-lane SUM.** The merge is a single atomic statement (`_LANE_MERGE_SQL` in
  `routers/agent_heartbeat.py`) rather than a Python read-modify-write, because four lanes beat
  concurrently (~4 writes/30s per agent) and an interleaved Python merge would silently drop a lane
  from the breakdown until its next tick. The admin table already renders
  `last_status['queue_depth']`, so that column went from analyze-lane-only to the agent's true
  all-lane total with no template change.

The `lane` field on the heartbeat schema is **optional** (`schemas/agent_heartbeat.py`): an agent on
an older image, or in all-mode (no lane split), posts without it, and a required field would 422 every
one of those beats — turning a liveness fix into a liveness outage during a rolling deploy. `None`
means "unlaned beat" and is stored the way it always was. `worker-drain` stays
`PHAZE_AGENT_HEARTBEAT=false` for the same reason: it is unlaned, so its beat carries no lane tag.

The dashboard's `get_queue_activity` (`src/phaze/services/pipeline.py`) remains the broker-side
in-flight view, summing queued+active across all four lane queues **plus** the legacy base queue
per agent.

## Compute (cloud/x86) agent — single lane

The compute agent is media-less and analysis-only; its ONLY task is `process_file`. Because producers
target lane-suffixed queue names uniformly, the compute agent consumes the **single `analyze` lane**
(`docker-compose.cloud-agent.yml` sets `PHAZE_AGENT_LANE=analyze`). It is NOT a 4-service split — the
`fingerprint` / `meta` / `io` lanes would be permanently empty on a compute host, and the I/O-starvation /
head-of-line problems the lane split solves are file-server-only. Single lane ⇒ single heartbeat (its
`PHAZE_AGENT_HEARTBEAT` is left unset → default true). k8s burst pods are untouched.

**Memory-safety cap (quick-260707-g84).** The OCI Ampere A1 compute host has only 12 GB RAM and a
single `process_file` job peaks ~8 GB, so the `analyze` lane is pinned to **1** concurrent job via
`PHAZE_LANE_ANALYZE_CONCURRENCY=1` in `docker-compose.cloud-agent.yml`. This is the knob that
actually governs a lane worker; setting only `WORKER_MAX_JOBS=1` is **inert in lane mode** (it is a
ceiling — `concurrency = min(lane knob, worker_max_jobs)` — so it can never lift the analyze lane's
default 4). Without this pin the compute agent silently ran 4 concurrent ~8 GB jobs and OOM-killed.

## Migration / drain runbook

New enqueues route to lane queues immediately on deploy. In-flight jobs on the legacy un-suffixed
`phaze-agent-nox` queue must drain. The chosen mechanism is a **transitional all-mode consumer** — NOT a
re-enqueue.

Why not re-enqueue: re-driving an already-**active** multi-hour `process_file` onto a lane queue would
duplicate a running job (deterministic-key dedup guards *queued* enqueues, not an *active* job on a
different queue name). Finishing in place has no duplicate-active hazard, and any legitimate retry stays
idempotent via the deterministic key (`s3_upload:<file_id>`, `push_file:<file_id>`, `process_file:<file_id>`).

**Steps (homelab):**

1. Deploy the new compose. Bring up the four lane workers (+ the compute `analyze` lane on the cloud host):
   ```bash
   docker compose -f docker-compose.agent.yml up -d worker-analyze worker-fingerprint worker-meta worker-io watcher audfprint panako
   ```
   Producers now enqueue ONLY onto the lane queues, so `phaze-agent-nox` only drains (never grows).
2. Start the transitional drain consumer (all-mode: `PHAZE_AGENT_LANE` unset → all 8 functions on the
   legacy base queue; `PHAZE_AGENT_HEARTBEAT=false`):
   ```bash
   docker compose -f docker-compose.agent.yml --profile drain up -d worker-drain
   ```
3. Watch the legacy queue drain. When `phaze-agent-nox` reports **0 queued + 0 active** (visible in the
   `/saq` dashboard — the base queue is mounted for exactly this window), remove the drain service:
   ```bash
   docker compose -f docker-compose.agent.yml --profile drain rm -sf worker-drain
   ```

Once `worker-drain` is removed, the migration is complete and all work flows through the four lane queues.

## Related files

- `src/phaze/services/enqueue_router.py` — `LANE_TASKS`, `LANES`, `lane_for_task`, `AGENT_TASKS` (derived union).
- `src/phaze/services/agent_task_router.py` — `queue_for(agent_id, lane)` (lane required), `all_lane_queues`, `legacy_base_queue`.
- `src/phaze/tasks/agent_worker.py` — lane-parametrized settings driven by `PHAZE_AGENT_LANE`; single-lane heartbeat gate.
- `src/phaze/config.py` — `PHAZE_LANE_*_CONCURRENCY` + `PHAZE_AGENT_HEARTBEAT`.
- `docker-compose.agent.yml` / `docker-compose.cloud-agent.yml` — the lane services + drain profile.
