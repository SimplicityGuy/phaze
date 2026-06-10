# Phase 30 Context — Systemic control-plane SAQ queue misrouting

> Source: live incident triage on the nox/lux homelab (v4.0.6), 2026-06-09.
> User clicked "Run analysis" on 11,428 discovered files; nothing happened.

## Root cause

In the v4.0 distributed-agents split, task **consumption** moved to two named SAQ queues:

- `controller` — lux `phaze-worker` (`src/phaze/tasks/controller.py:106`, `Queue.from_url(..., name="controller")`).
  Registers: `generate_proposals`, `match_tracklist_to_discogs`, `search_tracklist`,
  `scrape_and_store_tracklist`, `reap_stalled_scans` + cron `refresh_tracklists`.
- `phaze-agent-<id>` — nox `phaze-agent-worker` (`src/phaze/tasks/agent_worker.py:179`, name from `PHAZE_AGENT_QUEUE`).
  Registers: `process_file`, `extract_file_metadata`, `fingerprint_file`, `scan_live_set`,
  `scan_directory`, `execute_approved_batch`, `heartbeat_tick`.

But the API's `app.state.queue` (`src/phaze/main.py:89`, `Queue.from_url(settings.redis_url)` — **unnamed ⇒ "default"**)
was never repointed. **No worker consumes `default`.** SAQ's `queue.enqueue()` to a consumer-less
queue succeeds silently (returns a Job, status stays `queued` forever), so every affected HTTP
endpoint returns 200 while the jobs rot in `saq:job:default:*`.

**Live confirmation:** 11,428 `process_file` jobs stranded in `saq:job:default:*`; DB shows
11,428 files all in `discovered` state, `metadata`=11,428, `analysis`=0. Metadata extraction
worked only because its path routes through `AgentTaskRouter.enqueue_for_agent`, not `app.state.queue`.

## Scope — 9 misrouted enqueue sites (all use `app.state.queue` = default, no consumer)

| File:line | Task | Correct destination |
|---|---|---|
| `routers/pipeline.py:42` | `process_file` | per-agent queue |
| `routers/pipeline.py:48` | `generate_proposals` | `controller` |
| `routers/pipeline.py:241` | `extract_file_metadata` | per-agent queue |
| `routers/pipeline.py:304` | `fingerprint_file` | per-agent queue |
| `routers/tracklists.py:213` | `scan_live_set` | per-agent queue |
| `routers/tracklists.py:354` | `scrape_and_store_tracklist` | `controller` |
| `routers/tracklists.py:427` | `search_tracklist` | `controller` |
| `routers/tracklists.py:627` | `match_tracklist_to_discogs` | `controller` |
| `scan.py:49` → `services/ingestion.py:182` | `extract_file_metadata` | per-agent queue (legacy `/scan` endpoint) |

Each `pipeline.py`/`tracklists.py` site typically has an `/api/v1/*` JSON twin and a
`/pipeline/*` (or tracklist) HTMX twin sharing a `_enqueue_*_jobs` helper — ~13 handlers total.

## Correct reference paths (already migrated — do NOT break)

- `routers/agent_files.py:143` — auto `extract_file_metadata` on agent file insert.
- `routers/execution.py:171` — `execute_approved_batch`.
- `routers/pipeline_scans.py:394` — distributed scan dispatch.

All use `app.state.task_router.enqueue_for_agent` (`services/agent_task_router.py`,
`AgentTaskRouter`; `_queue_for(agent_id)` builds `Queue(name=f"phaze-agent-{agent_id}")`,
applies `apply_project_job_defaults` before_enqueue hook).

## Fix direction

Introduce a shared **enqueue-routing helper** that maps `task_name` → correct destination:

- **Controller-bound** (`generate_proposals`, `search_tracklist`, `scrape_and_store_tracklist`,
  `match_tracklist_to_discogs`, `refresh_tracklists`) → a `Queue(name="controller")` handle.
  Consider wiring `app.state.controller_queue` in the lifespan, or repurposing `app.state.queue`
  to be the named controller queue.
- **Per-agent** (`process_file`, `extract_file_metadata`, `fingerprint_file`, `scan_live_set`,
  `scan_directory`, `execute_approved_batch`) → `AgentTaskRouter.enqueue_for_agent` with
  **active-agent selection**: pick a non-revoked, recently-seen agent. `agents` table has
  `revoked_at` (NULL = active) and `last_seen_at`. Today nox is the sole live agent;
  `legacy-application-server` is permanently revoked (token_hash NULL, revoked_at=created_at) —
  exclude it. Selection must handle 0 agents (surface a clear error/empty-state) and >1 agents
  (round-robin or least-loaded — keep simple; document choice).

Update all 9 sites to use the helper. Keep the working task_router paths unchanged.

## Tests (regression)

Assert each affected endpoint enqueues onto the **expected named queue**, not `default`.
A mock/fake queue capturing `(queue_name, task_name, kwargs)` per enqueue is sufficient.
Cover the 0-active-agents and revoked-agent-excluded branches of agent selection.

## Constraints

Python 3.14, `uv` only; 85% coverage; pre-commit must pass (no `--no-verify`);
worktree branch + PR (no direct main commit). The default queue should end up with **no**
producers (or be eliminated) so this class of bug can't silently recur.

## Operational note (not part of this phase)

11,428 `process_file` jobs remain stranded in `saq:job:default:*` on the live homelab.
After this fix ships + redeploys, re-trigger analysis (re-enqueues correctly), and clear the
dead `default` jobs from Redis. User opted to fix code first rather than hand-re-enqueue.
