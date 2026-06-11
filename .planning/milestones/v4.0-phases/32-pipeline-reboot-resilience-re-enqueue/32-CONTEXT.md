# Phase 32: Pipeline Reboot Resilience & Re-enqueue - Context

**Gathered:** 2026-06-11
**Status:** Ready for planning
**Source:** Operator design decisions (2026-06-11) + ROADMAP locked decision (2026-06-10) + codebase investigation

<domain>
## Phase Boundary

**In scope:** Make the analysis pipeline self-healing across full host reboots and container restarts. Postgres `FileState` is the durable source of truth; Redis stays disposable (no AOF). Re-enqueue `FileState.DISCOVERED` files that have no live job so a reboot resumes the remaining work automatically — no manual "Run Analysis" re-trigger. Resilience is idempotent and **per-file, NOT intra-file** (re-running an interrupted file is safe — `put_analysis` replaces a file's window rows, Phase 31 plan 31-03).

**Out of scope:** Redis AOF/persistence (explicitly rejected). Other pipeline stages — metadata-extraction / fingerprint / proposals re-enqueue (operator decision: analysis stage only this phase). Intra-file checkpointing/resume. The SAQ monitoring UI (Phase 33). The bounded `worker_job_timeout` (~4h) + `retries` policy that lets SAQ reclaim a dead worker's in-flight job already shipped in Phase 31 plan 31-05 — this phase is the reboot/queue-loss recovery layer ON TOP of that.
</domain>

<decisions>
## Implementation Decisions (LOCKED)

### Trigger: BOTH startup hook + periodic cron (operator, 2026-06-11)
- **Startup**: the controller worker's SAQ `startup` hook (`tasks/controller.py::startup`) runs the re-enqueue once on boot → immediate recovery after a reboot (Redis is empty post-reboot, so all DISCOVERED files re-enqueue).
- **Cron**: a periodic SAQ `CronJob` on the controller catches mid-run stalls (worker crash, transient agent loss) without waiting for a restart. Cadence = *Claude's Discretion* (recommend every 5 min — `reap_stalled_scans` is `* * * * *`/1-min; re-enqueue scans more rows so 5 min is the balance). Register alongside the existing `reap_stalled_scans` + `refresh_tracklists` CronJobs.

### Dedup: deterministic SAQ job key per file (operator, 2026-06-11)
- Enqueue `process_file` with a **deterministic key** `key=f"process_file:{file_id}"` so SAQ no-ops a re-enqueue while that file's job is still incomplete (queued or active). This makes the cron safe to run frequently and prevents the reboot-cron from double-enqueuing files that "Run Analysis" or a prior tick already queued.
- **CRITICAL**: the key MUST be added to the **shared** enqueue path so BOTH producers use it — the dashboard "Run Analysis" (`routers/pipeline.py::_enqueue_analysis_jobs`) AND the new reboot re-enqueue. Otherwise the two paths generate different keys and dedup fails. Today `_enqueue_analysis_jobs` enqueues with NO key (default = random uuid, verified: live Redis showed `saq:job:phaze-agent-nox:<uuid>`).
- RESEARCH MUST confirm the exact saq 0.26.4 API for a custom deterministic key + the dedup/no-op-on-duplicate-incomplete-key behavior (is it `enqueue(..., key=...)`? a `Job(key=...)`? does a duplicate incomplete key return `None` / the existing job?). This is the load-bearing primitive of the whole phase — see RESEARCH question 1.

### Location: controller worker (operator, 2026-06-11)
- Re-enqueue runs in the **controller** worker (`tasks/controller.py`), NOT the agent worker. Rationale: the controller has direct Postgres access (its `startup` builds the async engine) + the routing layer; the agent worker is HTTP-backed (Phase 26) with no direct DB. This **refines** the ROADMAP's "agent-worker startup" wording.
- Route the re-enqueue onto the **active agent's** queue using the same Phase 30 selection the dashboard uses. The controller has no FastAPI `app.state`, so it cannot call `enqueue_router.resolve_queue_for_task(...)` (that reads `app.state`). The re-enqueue service must obtain a routed agent queue another way — construct an `AgentTaskRouter(redis_url)` directly + the non-revoked/active-agent query. *Claude's Discretion / RESEARCH*: cleanest way to reuse the Phase 30 active-agent selection (`enqueue_router.select_active_agent` if it takes a session, vs the raw query) without `app.state`.
- **No active agent** at startup/cron time: skip with a clear `logger.warning` (cannot route `process_file` with zero agents) and return a count of 0 — never crash the controller startup or the cron.

### Scope: analysis stage only — DISCOVERED → process_file (operator, 2026-06-11)
- Query Postgres for `FileRecord.state == FileState.DISCOVERED` and re-enqueue `process_file` for each, building the **COMPLETE `ProcessFilePayload`** (file_id, original_path, file_type, agent_id, models_path) — the v4.0.8 lesson: enqueuing only `file_id` dead-letters every job. Reuse the same payload-build + `timeout=14400` + `retries=2` policy as `_enqueue_analysis_jobs` (Phase 31 31-05). The deterministic key is the only addition.
- Do NOT re-enqueue metadata/fingerprint/proposal stages this phase.

## Claude's Discretion
- Cron cadence (recommend `*/5 * * * *` — every 5 min).
- Throttling/batching of the re-enqueue loop at 11,428-file scale (the existing dashboard path enqueues in a background task; the controller cron/startup runs in the worker loop — keep each enqueue await'd, log progress, no need to background within the worker).
- Exact module layout: a new `tasks/reenqueue.py` (mirroring `tasks/scan_reaper.py`) vs a function in an existing module. Recommend a dedicated module mirroring `scan_reaper.py`.
- Whether to factor the deterministic-key + payload build into one shared helper imported by both `routers/pipeline.py` and the new re-enqueue task (recommended — single source of truth for the key format).
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Controller cron + startup machinery (the patterns to mirror)
- `src/phaze/tasks/controller.py` — `startup(ctx)` hook (builds engine ~L44-89), module-level `settings` with `functions` + `CronJob(reap_stalled_scans, cron="* * * * *")` / `CronJob(refresh_tracklists, cron="0 3 1 * *")` (~L115-130). The startup hook + a new CronJob both register here.
- `src/phaze/tasks/scan_reaper.py` — `reap_stalled_scans(ctx)` (~L38): the closest analog — a controller cron that queries Postgres and acts. Mirror its structure for the new re-enqueue task.

### Enqueue path + routing to reuse (deterministic key goes HERE, shared)
- `src/phaze/routers/pipeline.py` — `_enqueue_analysis_jobs(queue, files, agent_id, models_path)` (~L44-83): builds the COMPLETE `ProcessFilePayload` + `timeout=14400` + `retries=2`. The deterministic key must be added here (or a shared helper it calls) so the dashboard and reboot-recovery paths agree.
- `src/phaze/services/enqueue_router.py` — active-agent selection (`select_active_agent`) + controller-vs-agent routing; the predicate to reuse for "which agent queue."
- `src/phaze/services/agent_task_router.py` — `AgentTaskRouter(redis_url)` + `queue_for(agent_id)` returns the cached per-agent `saq.Queue`; the controller constructs one directly (no app.state).
- `src/phaze/schemas/agent_tasks.py` — `ProcessFilePayload` (extra="forbid"; all five fields required).
- `src/phaze/models/file.py` — `FileRecord`, `FileState.DISCOVERED`.
- `src/phaze/models/agent.py` — `Agent.revoked_at` / `last_seen_at` for active-agent selection.

### SAQ
- `saq==0.26.4` (installed). `CronJob`, `Queue.enqueue`. RESEARCH must pin the deterministic-`key` + dedup semantics (see below).
</canonical_refs>

<specifics>
## Specific Ideas / Evidence
- Live reality (2026-06-10): the whole 11,428-file corpus sits at `DISCOVERED`; a homelab reboot currently requires a manual "Run Analysis" re-click. This phase makes that automatic.
- `process_file` jobs currently enqueue with default uuid keys (`saq:job:phaze-agent-nox:<uuid>` in live Redis) — no dedup today.
- Single-agent reality (`phaze-agent-nox`): per-queue key dedup is sufficient; cross-agent key dedup (same key on two different agent queues) is an accepted edge case, not a blocker.
- Phase 30 already centralized control-plane routing so "Run Analysis" hits the agent queue, not the consumer-less default — reuse that, do not reinvent.
</specifics>

<deferred>
## Deferred Ideas
- Metadata / fingerprint / proposal stage reboot-recovery (this phase = analysis stage only).
- Redis AOF/persistence (explicitly rejected in favor of Postgres-as-truth).
- Intra-file resume/checkpointing (per-file re-run is the resilience unit).
- Cross-agent key dedup / multi-agent load balancing.
</deferred>

---

*Phase: 32-pipeline-reboot-resilience-re-enqueue*
*Context gathered: 2026-06-11 via operator decisions + codebase investigation*
