# Phase 33: SAQ Monitoring UI (mounted in phaze-api) - Context

**Gathered:** 2026-06-11
**Status:** Ready for planning
**Source:** ROADMAP Phase 33 (prescriptive approach, locked) + codebase/SAQ investigation

<domain>
## Phase Boundary

**In scope:** Expose SAQ's built-in monitoring web UI by mounting it into the existing `phaze-api` FastAPI ASGI app at the `/saq` subpath, reusing the already-wired Redis-connected `saq.Queue` instance(s). Operators get queue/job visibility (queued/active/complete/failed, retries) without a separate service.

**Out of scope:** The standalone `saq --web` server. Any new bound port. Any app-layer auth/middleware (the reverse proxy already terminates TLS + enforces internal-realm auth — the dashboard is INTENTIONALLY unauthenticated at the app layer). A second Redis connection pool. Any change to task/queue behavior.
</domain>

<decisions>
## Implementation Decisions (LOCKED — from ROADMAP, prescriptive)

### Mount mechanism
- `from saq.web.starlette import saq_web` → `app.mount("/saq", saq_web("/saq", queues=[...]))`. **VERIFIED (installed saq==0.26.4):** the import path is `saq.web.starlette` (NOT bare `saq.web`); signature is `saq_web(root_path: str, queues: list[Queue]) -> Starlette`. The mount target is the app factory `create_app()` in `src/phaze/main.py` (`app = FastAPI(...)`, entrypoint `phaze.entrypoint` → uvicorn :8000).
- The only change is mounting `saq_web` into the existing app. No standalone server, no new port, no auth middleware.

### Queues to monitor (LOCKED): controller + per-agent
- The named **controller** queue (`app.state.controller_queue`, name `controller`) PLUS the per-agent queues (`AgentTaskRouter.queue_for(agent_id)`, name `phaze-agent-<id>`). The agent queue is where `process_file` jobs live — monitoring controller-only would be nearly useless, so agent queues MUST be included.

### Reuse existing Queue instances — NO second pool (LOCKED)
- Reuse the same `saq.Queue` instance(s) created in the lifespan (`main.py` lifespan ~L49 creates `app.state.controller_queue` + `app.state.task_router` + `app.state.redis` from `REDIS_URL`/`REDIS_URL_FILE`). Do NOT construct a second connection pool for the dashboard.

### Security posture (LOCKED)
- Intentionally unauthenticated at the app layer — only reachable behind the reverse proxy's internal-realm auth. The PR description MUST state this explicitly. No auth middleware added.

### Dependencies (LOCKED, verify in RESEARCH)
- SAQ is already a direct dependency. **VERIFIED:** `saq.web.starlette` imports cleanly and `starlette` + `aiohttp` are already present; `aiohttp_jinja2` is NOT installed. RESEARCH must confirm whether the **starlette** `saq_web` path renders without `aiohttp_jinja2` (it should, since it targets Starlette, not aiohttp) or whether a `saq[web]` extra / `jinja2` (already present for app templates) is required at runtime. If an extra is genuinely needed, add `saq[web]` pinned consistently with the existing `saq` pin — no NEW top-level package.

## KEY TECHNICAL RISK — queue-wiring timing (RESEARCH MUST RESOLVE)
`saq_web(queues=[...])` takes a **static** queue list at mount time. But:
- The `controller_queue` / `task_router` / `redis` are created in the **lifespan startup**, which runs AFTER `create_app()` returns — so at `app.mount(...)` time in `create_app()`, `app.state.controller_queue` does not exist yet.
- The per-agent queues are derived from agents in **Postgres**, and the DB engine/session is also only available after lifespan startup.

RESEARCH must determine the correct wiring that satisfies BOTH "reuse the same Queue instances / no second pool" AND "include controller + per-agent queues". Candidate approaches to evaluate (pick the simplest that works + is safe):
1. **Mount inside the lifespan startup** (after the queues exist + after enumerating non-revoked agents from the DB), via `app.mount("/saq", saq_web("/saq", queues=[controller_queue, *agent_queues]))`. Routes added during lifespan startup are in place before the server serves requests. Confirm FastAPI/Starlette supports adding a mount during lifespan startup (router not frozen yet) — this is the leading candidate because it gives real agent-queue coverage with the real instances.
2. **Construct the shared Queue instances earlier** (in `create_app()` or module-level) and have the lifespan reuse those same instances; mount at `create_app()` with controller only (agent queues unavailable pre-DB) — loses agent-queue coverage, so weaker.
3. Any SAQ-supported lazy/dynamic queue registration on the mounted app.

**Operator-acceptable behavior for dynamic agents (decided):** enumerate non-revoked agents at **app startup**; agents registered AFTER startup won't appear until the next `phaze-api` restart. Acceptable for a single-user homelab (api restarts on every redeploy). Document this limitation; do NOT build live agent-queue hot-reload.

## Claude's Discretion
- Exact module placement of the mount helper (inline in `main.py` lifespan vs a small `phaze/web/saq_mount.py` helper). Recommend a tiny helper for testability.
- Whether to guard the mount behind a settings flag (e.g. `enable_saq_ui`, default on). Recommend a flag, default-enabled, so it can be disabled without code change.
- How `saq_web`'s `root_path="/saq"` interacts with the reverse-proxy path prefix — verify links/assets resolve under `/saq`.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### App factory + lifespan queue wiring (the mount site)
- `src/phaze/main.py` — `create_app()` (~L115, `app = FastAPI(...)`) and the lifespan (~L49) that creates `app.state.controller_queue` (name `controller`), `app.state.task_router` (`AgentTaskRouter`), `app.state.redis`. This is where the mount + queue reuse happen. `/health` healthcheck + existing routers must keep working.
- `src/phaze/services/agent_task_router.py` — `AgentTaskRouter.queue_for(agent_id)` returns the cached per-agent `saq.Queue` (name `phaze-agent-<id>`).
- `src/phaze/services/enqueue_router.py` — non-revoked / active-agent enumeration to reuse when building the agent-queue list.
- `src/phaze/models/agent.py` — `Agent.revoked_at` for the non-revoked filter.
- `src/phaze/config.py` — settings (`redis_url`/`REDIS_URL_FILE`); add an `enable_saq_ui` flag here if chosen.

### SAQ web
- Installed `saq==0.26.4`: `from saq.web.starlette import saq_web`; `saq_web(root_path: str, queues: list[saq.Queue]) -> starlette.Starlette`. Mount via `app.mount("/saq", saq_web("/saq", queues=[...]))`.
</canonical_refs>

<specifics>
## Specific Ideas / Evidence
- Verified at probe time: `from saq.web.starlette import saq_web` succeeds; `starlette` + `aiohttp` present; `aiohttp_jinja2` absent (starlette path shouldn't need it).
- Live queue topology (from Phase 30/34 work): `controller` queue + `phaze-agent-nox` queue carry all the jobs worth watching; `process_file` (the bulk) is on `phaze-agent-nox`.
- This phase pairs naturally with Phase 32 (reboot re-enqueue) and Phase 34 (queue-depth status) — the `/saq` dashboard is the deep operator view; the pipeline card is the at-a-glance view.
</specifics>

<deferred>
## Deferred Ideas
- App-layer auth for `/saq` (intentionally deferred to the reverse proxy).
- Live hot-reload of agent queues registered after `phaze-api` startup (restart picks them up).
- Standalone `saq --web` service / separate port.
</deferred>

---

*Phase: 33-saq-monitoring-ui-mounted-in-phaze-api*
*Context gathered: 2026-06-11 via ROADMAP prescriptive approach + SAQ/codebase investigation*
