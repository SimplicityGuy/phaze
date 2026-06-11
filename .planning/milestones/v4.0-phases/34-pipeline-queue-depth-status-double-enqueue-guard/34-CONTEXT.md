# Phase 34: Pipeline Queue-Depth Status & Double-Enqueue Guard - Context

**Gathered:** 2026-06-10
**Status:** Ready for planning
**Source:** Brainstorming session (operator-approved design) + live-system investigation (nox/lux)

<domain>
## Phase Boundary

**In scope:** The pipeline dashboard's "Pipeline Actions" area (`src/phaze/templates/pipeline/`) and the `/pipeline/stats` poll. Add a live queue-depth signal so an in-flight run is visible after a page refresh and the trigger buttons cannot double-enqueue.

**Out of scope:** Per-task-type queue accounting (coarse is accepted), the SAQ monitoring UI (that is Phase 33), reboot re-enqueue resilience (Phase 32), any change to how jobs are enqueued or processed, any new poll loop or websocket/SSE.

## The bug being fixed (verified live, 2026-06-10)

Operator clicked **Run Analysis**, refreshed, and all status vanished. Root cause: the dashboard only knows DB `FileState`. `process_file` does not move a file out of `DISCOVERED` until a worker finishes it, so after enqueue the page is byte-identical to before the click. Confirmed on the live stack: `phaze-agent-nox` SAQ queue held **11,429 incomplete / 11,421 queued** `process_file` jobs with **0 analyzed**, yet the button's `:disabled` (which only checks `discovered === 0`) stayed enabled — one more click would have enqueued another ~11,428 duplicate jobs.

The DB cannot distinguish "nothing queued" from "everything queued." The only authoritative signal is **live SAQ queue depth**.
</domain>

<decisions>
## Implementation Decisions (LOCKED)

### Data source — SAQ queue depth, read live
- Read queue depth with `Queue.count(kind)` where `kind ∈ {"queued","active"}` (SAQ 0.26.4 — verified installed). Each is a cheap single Redis op (`LLEN`/`ZCARD`). `count` excludes `scheduled`, so idle controller cron jobs (`reap_stalled_scans`, `refresh_tracklists`) do NOT inflate counts.
- Queues are already on `app.state` (wired in `main.py` lifespan, Phase 26/30): `app.state.controller_queue` (named `controller`) and `app.state.task_router` (`AgentTaskRouter`, per-agent queues `phaze-agent-<id>` via `task_router.queue_for(agent_id)`).
- **Sum agent depth across ALL non-revoked agents** (`Agent.revoked_at IS NULL`), not just the active one — robust if a second agent is ever added. Reuse the same agent-selection predicate style as `enqueue_router`.

### New service `get_queue_activity(app_state, session)` in `src/phaze/services/pipeline.py`
Returns a dict:
- `agent_queued`, `agent_active` — summed across non-revoked agents' queues
- `controller_queued`, `controller_active` — from `controller_queue`
- `agent_busy = agent_queued + agent_active`
- `controller_busy = controller_queued + controller_active`

Must not raise if a queue is unreachable/empty — degrade to 0 (a Redis hiccup must never 500 the dashboard poll).

### Surface via the EXISTING 5s poll (no new loop)
Extend the `/pipeline/stats` endpoint context (`src/phaze/routers/pipeline.py::pipeline_stats_partial`) with the queue-activity counts. The dashboard already polls `#pipeline-stats` every 5s — reuse it. Initial full-page `dashboard()` render must also seed the counts so the page is correct on first load (not only after the first poll tick).

### Persistent "Processing" card — `partials/processing_card.html`
- Lives in `dashboard.html` ABOVE the stats bar. OOB-swapped on each `/pipeline/stats` tick using the SAME `hx-swap-oob` pattern already established for the "files ready" counts in `stats_bar.html` (emit OOB block ONLY on poll responses via an `oob_*` flag, never on the initial full-page include — avoids duplicate-id DOM + stray render).
- When `agent_busy > 0`: show a progress bar + text `"{queued} queued · {active} active"`.
- **Progress denominator is DB-derived (operator choice):** `percent = analyzed / (analyzed + agent_busy)` using the existing `stats.analyzed` count as `done`. Chosen over SAQ's aggregated `complete` because it survives worker restarts (the bar won't jump backward). Accepted trade-off: pre-existing analyzed files count toward `done`. Guard `analyzed + agent_busy == 0` → render empty (no divide-by-zero, card hidden when idle).
- A second compact line covers the controller queue for proposals when `controller_busy > 0`.
- Card renders EMPTY (no visual) when both `agent_busy == 0` and `controller_busy == 0`.

### Coarse button disable via Alpine `$store.pipeline`
- Push `agent_busy` and `controller_busy` into `$store.pipeline` via the SAME `x-init` store-write trick already used for `discovered`/`analyzed` (OOB paragraphs in `stats_bar.html` set the store; the button subtree is never the swap target, so loading state is preserved).
- `stage_cards.html` buttons:
  - Analyze / Fingerprint / Extract-Metadata: `:disabled="loading || <ready>===0 || $store.pipeline.agentBusy > 0"`
  - Generate Proposals: `:disabled="loading || analyzed===0 || $store.pipeline.controllerBusy > 0"`
- **Coarse is intentional (operator choice):** the single agent worker processes one shared queue serially, so any agent-queue work disabling all three agent buttons is honest. Per-task counters were rejected (would require maintaining our own enqueue/complete counters — fragile).

### Store initialization
`$store.pipeline` must define `agentBusy`/`controllerBusy` with sane defaults (0) so `:disabled` bindings never reference `undefined` before the first poll. Seed from the initial full-page render.

## Claude's Discretion
- Exact Tailwind classes / progress-bar markup (match the existing partials' dark-mode-aware styling).
- Whether `get_queue_activity` takes `app.state` + `session` or narrower params — pick the cleanest testable signature.
- Helper to enumerate non-revoked agents (new vs reuse from `enqueue_router`).
- Number formatting (thousands separators) for large counts.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Existing pipeline UI + poll (the patterns to mirror)
- `src/phaze/templates/pipeline/dashboard.html` — page shell; where the processing card slots in (above `#pipeline-stats`).
- `src/phaze/templates/pipeline/partials/stats_bar.html` — THE reference for the OOB-swap + `x-init` `$store.pipeline` pattern (lines 27–48). Mirror this exactly for the new counts/card.
- `src/phaze/templates/pipeline/partials/stage_cards.html` — the trigger buttons + current `:disabled` bindings to extend.
- `src/phaze/routers/pipeline.py` — `dashboard()` (full-page context) and `pipeline_stats_partial()` (`/pipeline/stats` poll); both contexts need the new counts.

### Queue + service plumbing
- `src/phaze/services/pipeline.py` — where `get_queue_activity` goes (alongside `get_pipeline_stats`).
- `src/phaze/services/agent_task_router.py` — `AgentTaskRouter.queue_for(agent_id)` returns the cached per-agent `saq.Queue`.
- `src/phaze/services/enqueue_router.py` — non-revoked-agent selection predicate to reuse; controller-vs-agent task routing model.
- `src/phaze/main.py` (lifespan ~L98–106) — confirms `app.state.controller_queue` + `app.state.task_router` are the live queue handles.
- `src/phaze/models/agent.py` — `Agent.revoked_at` for the non-revoked filter.

### SAQ API
- `saq.Queue.count(kind)` (installed `saq==0.26.4`) — `kind`: `"queued" | "active" | "incomplete"`; returns `int`. Use `queued` + `active` (NOT `incomplete`, which would also count scheduled in some versions — keep `queued`/`active` explicit).
</canonical_refs>

<specifics>
## Specific Ideas
- Live evidence captured 2026-06-10 from lux: `redis-cli` against `phaze-redis` (password from `phaze_redis_url` secret) showed `zcard saq:phaze-agent-nox:incomplete = 11429`, `llen saq:phaze-agent-nox:queued = 11421`.
- Container topology: nox = file server (`phaze-agent-worker`, `phaze-agent-watcher`, panako, audfprint); lux = app server (`phaze-api`, `phaze-worker` controller, `phaze-redis`).
- The agent queue mixes `process_file`, `extract_file_metadata`, `fingerprint_file` (all AGENT_TASKS) — this is WHY per-task accounting is expensive (function name is inside each job hash, not the key) and coarse disable is the pragmatic choice.
</specifics>

<deferred>
## Deferred Ideas
- Per-task-type queue counts / per-stage progress bars — deferred (would need self-maintained counters or full job-hash scans).
- SAQ's built-in monitoring dashboard — that's Phase 33.
</deferred>

---

*Phase: 34-pipeline-queue-depth-status-double-enqueue-guard*
*Context gathered: 2026-06-10 via brainstorming + live investigation*
