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

### Thread pinning

essentia/TensorFlow are pinned single-threaded on the CPU lanes (`analyze`, `fingerprint`) so one
slot ≈ one core and the budget stays honest: `OMP_NUM_THREADS=1`, `TF_NUM_INTRAOP_THREADS=1`,
`TF_NUM_INTEROP_THREADS=1` (set in `docker-compose.agent.yml`). This addresses the load-18-on-8-cores
oversubscription observed under the old single pool.

## Heartbeat — exactly one per agent (A1 caveat)

The liveness heartbeat (Phase 46 asyncio background task) is **agent-level, not lane-level**. It runs
in **exactly one** lane worker — `worker-analyze`, via `PHAZE_AGENT_HEARTBEAT=true` (false on the other
three lanes) — so an agent reports one authoritative `last_seen`, never N duplicate heartbeats.

**Caveat (A1, by design):** the heartbeat reads `ctx["worker"].queue`, which in the analyze-lane worker
is the **analyze lane's depth only**. The heartbeat's `queue_depth` field is therefore analyze-lane-only,
NOT the whole agent. This is intentional and acceptable — the heartbeat needs only liveness, and cross-lane
reads would add coupling for a cosmetic field. The **authoritative all-lane in-flight figure** is the
dashboard's `get_queue_activity` (`src/phaze/services/pipeline.py`), which sums queued+active across all
four lane queues **plus** the legacy base queue per agent.

## Compute (cloud/x86) agent — single lane

The compute agent is media-less and analysis-only; its ONLY task is `process_file`. Because producers
target lane-suffixed queue names uniformly, the compute agent consumes the **single `analyze` lane**
(`docker-compose.cloud-agent.yml` sets `PHAZE_AGENT_LANE=analyze`). It is NOT a 4-service split — the
`fingerprint` / `meta` / `io` lanes would be permanently empty on a compute host, and the I/O-starvation /
head-of-line problems the lane split solves are file-server-only. Single lane ⇒ single heartbeat (its
`PHAZE_AGENT_HEARTBEAT` is left unset → default true). k8s burst pods are untouched.

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
