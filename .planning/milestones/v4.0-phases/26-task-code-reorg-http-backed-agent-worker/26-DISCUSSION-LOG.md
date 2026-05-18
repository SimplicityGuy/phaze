# Phase 26: Task Code Reorg & HTTP-Backed Agent Worker - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-05-12
**Phase:** 26-task-code-reorg-http-backed-agent-worker
**Areas discussed:** HTTP client abstraction, Code organization & shim, Agent identity at startup, Enqueuer & payload shape, API contract gap closure

---

## HTTP client abstraction

| Option | Description | Selected |
|--------|-------------|----------|
| PhazeAgentClient wrapper class | One class with one method per endpoint; mirrors DiscogsographyClient. Single httpx.AsyncClient with retry/timeout in one place. Stored in ctx['api_client']. | ✓ |
| Raw httpx in each task | Each task constructs its own httpx call. Less indirection but bearer-token/base URL/retry duplicate across 5+ tasks. | |
| Thin module-level helpers | Stateless functions taking httpx.AsyncClient as a param. Halfway between class and inline. | |

**User's choice:** PhazeAgentClient wrapper class
**Notes:** Captured as D-09..D-13.

### Retry policy

| Option | Description | Selected |
|--------|-------------|----------|
| httpx + tenacity, narrow retry | 3 retries, exponential backoff (0.5→1→2s), retry only on 5xx/network errors, not 4xx. ~4s wall-time before bubble to SAQ. | ✓ |
| No client-side retry | Raw httpx, raise on any non-2xx. Let SAQ handle retries. Simpler but more re-runs. | |
| Configurable per-call | Client method args take optional retry= override. | |

**User's choice:** httpx + tenacity, narrow retry
**Notes:** Tenacity is a new dependency; added to `[project].dependencies` in pyproject.toml. Captured as D-11, D-12.

### Client config (bearer token + base URL)

| Option | Description | Selected |
|--------|-------------|----------|
| Settings fields on agent-only Settings subclass | AgentSettings(BaseSettings) with `agent_api_url`, `agent_token: SecretStr`. ControlSettings has neither. | ✓ |
| Shared Settings with optional fields | Single Settings with optional fields; role-check validator. | |

**User's choice:** Settings fields on agent-only Settings subclass
**Notes:** Drives the Settings split into BaseSettings + ControlSettings + AgentSettings. Captured as D-14.

---

## Code organization & shim

| Option | Description | Selected |
|--------|-------------|----------|
| Flat tasks/ with two settings modules | Task files stay flat; file-bound tasks rewritten in place to use HTTP client. Two settings modules import only their half. | ✓ |
| Split into tasks/control/ + tasks/agent/ subpackages | Clearer physical boundary but bigger diff and breaks all imports. | |
| Parallel new modules during migration | Keep tasks/functions.py AND add tasks/agent_functions.py. Adds churn. | |

**User's choice:** Flat layout, in-place rewrite
**Notes:** User added: "avoid exposing hostnames. lux is the app server, nox is the file server" — flagging that the roadmap's `phaze.tasks.lux_worker` leaks the application server's hostname. Captured as D-02 (renaming + doc-sweep) and D-05 (flat layout).

### Naming for the fileless settings module

| Option | Description | Selected |
|--------|-------------|----------|
| phaze.tasks.control_worker | Matches `PHAZE_ROLE=control` enum. Symmetric with agent_worker. | |
| phaze.tasks.app_worker | "App server worker." Reads naturally but less symmetric. | |
| phaze.tasks.fileless_worker | Describes capability, not role. | |
| phaze.tasks.controller (user-proposed) | Clean name, pairs with PHAZE_ROLE=control. | ✓ |

**User's choice:** `phaze.tasks.controller` (user-proposed alternative)
**Notes:** Asymmetric with `phaze.tasks.agent_worker` but reads more naturally. Compose passes the full module path, so asymmetry is operator-invisible. Captured as D-02.

### Backward-compat shim for `phaze.tasks.worker.settings`

| Option | Description | Selected |
|--------|-------------|----------|
| Rip out, update compose now | Delete tasks/worker.py; update docker-compose.yml in this phase. | ✓ |
| Keep tasks/worker.py as alias to lux_worker.settings | One-liner re-export; delays compose touch. | |

**User's choice:** Rip out now
**Notes:** Captured as D-04, D-08.

### Roadmap doc-sweep

| Option | Description | Selected |
|--------|-------------|----------|
| Yes — sweep all hostname leaks now | Tiny doc-only task at end of Phase 26 renames `lux_worker` → `controller` everywhere. | ✓ |
| No — just override here | CONTEXT.md states canonical name; downstream agents follow it. | |

**User's choice:** Yes
**Notes:** Captured as D-33 (single commit listing all touched docs).

---

## Agent identity at startup

| Option | Description | Selected |
|--------|-------------|----------|
| Derive from token via /whoami | Agent worker calls GET /api/internal/agent/whoami at boot. Single source of truth. New endpoint. | ✓ |
| Env var PHAZE_AGENT_ID alongside token | Operator supplies both in agent's .env. No new endpoint but risk of typo. | |
| Hybrid — env var + boot-time verify | Operator supplies PHAZE_AGENT_ID; agent verifies via /whoami. | |

**User's choice:** Derive from token via /whoami
**Notes:** Resolves the SAQ-queue-name-at-import-time tension by combining env-supplied `PHAZE_AGENT_QUEUE` (operator's responsibility) with a startup assertion that the env matches the token-derived agent_id. Captured as D-15, D-16, D-18.

### /whoami location and shape

| Option | Description | Selected |
|--------|-------------|----------|
| New router file agent_identity.py, returns full identity | Matches Phase 25 D-08 one-router-per-resource. Response includes agent_id, name, scan_roots, created_at. | ✓ |
| Add to existing agent_heartbeat.py | Mixes liveness + identity. | |
| Inline in agent_auth.py | Breaks agent_auth.py's helper-only convention. | |

**User's choice:** New router file agent_identity.py
**Notes:** Captured as D-15.

### Startup failure handling

| Option | Description | Selected |
|--------|-------------|----------|
| Retry with backoff, then fail loudly | Exponential backoff 1→2→4→8→16→32s (≤60s total). Fatal log + exit non-zero. Docker restart policy handles container retry. | ✓ |
| Fail immediately on first error | Single attempt → exit. Simpler but tight crashloop risk during deploys. | |
| Boot anyway, fail per-job | Worker boots without verified identity; first job fails. Hides misconfig. | |

**User's choice:** Retry with backoff, then fail loudly
**Notes:** Captured as D-16 step 3.

---

## Enqueuer & payload shape

### Queue routing logic

| Option | Description | Selected |
|--------|-------------|----------|
| AgentTaskRouter helper service | New phaze.services.agent_task_router.AgentTaskRouter with `.enqueue_for_file(file_record, ...)`. One place to evolve queue naming. | ✓ |
| Inline f-string at every call site | Each enqueue site does `Queue.from_url(...).enqueue('extract_file_metadata', queue_name=f'phaze-agent-{agent.id}', ...)`. | |
| Helper function (not a class) | Module-level `enqueue_for_agent(queue, agent_id, ...)`. | |

**User's choice:** AgentTaskRouter helper service
**Notes:** Captured as D-19, D-20, D-21.

### Payload typing

| Option | Description | Selected |
|--------|-------------|----------|
| Pydantic models in phaze.schemas.agent_tasks | BaseModel with extra='forbid'. Controller .model_dump() before enqueue; agent .model_validate() on entry. | ✓ |
| Typed kwargs (TypedDict) | No runtime validation; static-only. | |
| Plain dicts with mypy any | Status quo. Workable but verbose. | |

**User's choice:** Pydantic models
**Notes:** Captured as D-22.

### Payload field contents

| Option | Description | Selected |
|--------|-------------|----------|
| Minimal: file metadata only | file_id, original_path, file_type, agent_id (+ models_path for process_file). | ✓ |
| Full FileRecord snapshot | Embed whole FileRecord dict. Risk of stale data. | |
| File + companion paths | Minimal + companion-file paths for tasks that need them. | |

**User's choice:** Minimal: file metadata only
**Notes:** Agent walks companion files locally from `original_path` if needed. Captured as D-22, D-23, D-24.

---

## API contract gap closure (raised mid-discussion)

During discussion, grep of `session.add` / `session.execute` in task files surfaced that Phase 25's endpoints don't cover all writes Phase 26's file-bound tasks need:

- `process_file` writes `AnalysisResult` — no Phase 25 endpoint
- `scan_live_set` writes `Tracklist + TracklistVersion + TracklistTrack` — no Phase 25 endpoint
- `execute_approved_batch` writes proposal/file state transitions — only ExecutionLog covered

| Option | Description | Selected |
|--------|-------------|----------|
| Add them as part of Phase 26 | Three new endpoints: PUT /analysis/{file_id}, POST /tracklists (request_id idempotency), PATCH /proposals/{id}/state (joint Proposal + FileRecord update). | ✓ |
| Defer scan_live_set + execute_approved_batch out of Phase 26 | Roadmap success criterion #1 lists all 5 tasks — would require amending roadmap. | |
| Phase 26 adds only AnalysisResult endpoint; Phase 27/28 add the rest | Compromise: process_file works now; tracklist + proposal-state defer. | |

**User's choice:** Add them as part of Phase 26
**Notes:** Closes the contract gap so Phase 26 fully delivers its roadmap promise. Captured as D-26, D-27, D-28.

### Tracklist write idempotency contract

| Option | Description | Selected |
|--------|-------------|----------|
| POST creates new version each call | Idempotency via agent-supplied request_id (1h Redis TTL). Matches scan_live_set's existing "new version on rescan" behavior. | ✓ |
| PUT replace-or-create on (file_id, source) | Idempotent by natural key alone; no version bump on retry. Diverges from scan_live_set behavior. | |
| Two endpoints: tracklist + version | Two HTTP calls per scan. More surface, simpler each. | |

**User's choice:** POST creates new version each call
**Notes:** Captured as D-27.

### Proposal/FileRecord state-transition endpoint

| Option | Description | Selected |
|--------|-------------|----------|
| PATCH /api/internal/agent/proposals/{id}/state with structured body | Joint update in one transaction; state-machine validated; 409 on disallowed transitions. | ✓ |
| Two separate endpoints | PATCH /proposals/{id}/state + PATCH /files/{id}/state. Inconsistency window. | |
| Embed in /execution-log PATCH | Reuse Phase 25's PATCH endpoint with extended body. Conflates audit log + state transition. | |

**User's choice:** Single PATCH /proposals/{id}/state
**Notes:** Captured as D-28.

---

## Claude's Discretion

Items where the user accepted Claude's recommendation or left details open:

- Exact tenacity wait_exponential parameters within ~5s total wall-time
- Internal cache implementation for AgentTaskRouter (dict-with-lock vs functools.cached_property vs LRU)
- Whether `AgentApiError` subtypes live in `phaze.services.agent_client` or a sibling errors module
- Test-fixture names for agent-side mocks (`agent_client_mock` recommended)
- Whether a `phaze.tasks._shared` module helps deduplicate controller/agent startup helpers
- Whether `phaze.config.get_settings()` returns a cached singleton (recommended: yes)
- Import-boundary test runs in subprocess (recommended) vs in-process
- Exact log format for tenacity retry attempts (key=value, consistent with existing service logs)
- Pydantic schema field names — match existing model field names exactly to minimize cognitive load
- Redis key TTL on tracklist request_id idempotency (recommended: 1 hour)
- Whether `ProposalStatePatch.current_path` is conditional-required via model_validator (recommended yes when `file_state == "moved"`)

## Deferred Ideas

- Agent-side process-pool sizing (per-agent override — Phase 29)
- Watcher service & user-initiated scan (Phase 27)
- Group-by-agent execution dispatch (Phase 28)
- Deployment hardening: HTTPS, Redis password, docker-compose.agent.yml, model download tooling, agents admin page (Phase 29)
- mypy strict opt-in for new services (planner may decide inline)
- Bearer-token rotation overlap window (future milestone)
- Cross-file-server fingerprint matching (XAGENT-01; v4.0 doc'd limitation)
- Tracklist endpoint chunked POST (not needed at v4.0 scale)
- Structured-JSON logging for agent client (observability pass)
- Removing module-level `settings` singleton (per-request DI pattern — future)
