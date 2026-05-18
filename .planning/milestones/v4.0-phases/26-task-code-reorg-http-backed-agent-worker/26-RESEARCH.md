# Phase 26: Task Code Reorg & HTTP-Backed Agent Worker - Research

**Researched:** 2026-05-12
**Domain:** SAQ role-split workers + httpx-backed REST agent client + idempotent server endpoints
**Confidence:** HIGH (verified against existing Phase 25 code + current SAQ/httpx/tenacity/respx docs)

## Summary

Phase 26 is fundamentally a **mechanical refactor with three new surface-level features**, anchored on patterns that already exist in the Phase 25 codebase. The 33 locked decisions in CONTEXT.md leave very little design ambiguity. The research-relevant gaps are:

1. **SAQ mechanics** — confirming the import-time `settings` dict layout, `Queue.from_url(name=...)`, and CLI invocation `saq <module.path>.settings` work for two coexisting role-specific modules sharing one Redis.
2. **httpx + tenacity 4xx/5xx split** — picking the cleanest idiomatic predicate that does not raise on 4xx (those are bugs/auth errors and must surface immediately), but does retry on 5xx/ConnectError/ReadTimeout.
3. **Three new endpoints** — `PUT /analysis/{file_id}` (idempotent upsert via `pg_insert.on_conflict_do_update` mirroring `ingestion.py:91-119`); `POST /tracklists` (Redis-backed idempotency cache); `PATCH /proposals/{id}/state` (allowed-transitions table, joint Proposal+FileRecord update in one transaction).
4. **Import-boundary test** — a subprocess-based pytest that imports `phaze.tasks.agent_worker` in a fresh interpreter and asserts `phaze.database` and `sqlalchemy.ext.asyncio` are NOT in `sys.modules`. This is the single highest-leverage validation gate for the entire phase.
5. **respx contract tests** — verifying the auth-header invariant + retry semantics + 4xx-no-retry policy + 5xx-retry-then-fail. These tests are the only mechanism that catches drift between the client and the server contract.

**Primary recommendation:** Build `PhazeAgentClient` and `AgentTaskRouter` as a synchronous translation of the locked decisions. Both should be opted into mypy strict mode via `[[tool.mypy.overrides]]`. Add `respx` + `tenacity` as dev/runtime deps respectively. Add a single subprocess-based test (`tests/tasks/test_agent_worker_import_boundary.py`) as the load-bearing structural invariant — if that passes, the role separation is mechanically guaranteed.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| File scanning (filesystem walk) | Agent worker (`phaze.tasks.agent_worker`) | — | File-bound — only agents have local disk |
| Metadata extraction (`mutagen`) | Agent worker | — | Reads files from disk |
| Audio fingerprinting (`pyacoustid`, `essentia`) | Agent worker | — | Reads audio data, computes locally |
| Move/rename execution | Agent worker | — | Touches local filesystem |
| Job orchestration (state transitions, FK joins, retries) | Controller (`phaze.tasks.controller`) | — | DB-bound, no file access |
| Discogs match lookups | Controller | — | Network-only, no file access |
| LLM proposal generation | Controller | — | Network-only |
| HTTP API endpoints for agents | Application server (FastAPI in same process as controller) | — | Single Postgres connection pool |
| Auto-enqueue on file registration | Application server (`AgentTaskRouter`) | — | Knows agent_id from auth dep, enqueues to agent's queue |
| Idempotency cache (tracklists) | Redis (used by application server) | — | Cross-request shared state, TTL natural |
| State-machine validation | Application server (in `PATCH /proposals/{id}/state` handler) | — | DB is the source of truth; agent has no authority to refuse a transition |

## Phase Requirements

| ID | Description (from REQUIREMENTS.md) | Research Support |
|----|-------------|------------------|
| DIST-03 | Code must run on application server **or** agent based on `PHAZE_ROLE`; agent must not require Postgres reachability | Role-split SAQ settings modules (`controller` vs `agent_worker`); subprocess import-boundary test enforces no `phaze.database` reachability from agent module |
| TASK-01 | File-bound tasks execute on agents, fileless tasks execute on controller | Tasks rewritten to call HTTP endpoints; agent task list and controller task list become disjoint; SAQ queue routing carries the contract |
| TASK-02 | Per-agent queues so two agents don't steal each other's work | `Queue.from_url(redis_url, name=f"agent:{agent_id}")` on agent worker startup; `AgentTaskRouter.enqueue(agent_id, task, **kwargs)` selects queue by name on controller side |
| TASK-03 | Same Docker image runs both roles | Single entrypoint; `PHAZE_ROLE` env selects `phaze.tasks.controller.settings` vs `phaze.tasks.agent_worker.settings` for `saq` CLI |
| OPS-01 | Operational legibility — startup logs declare role; health endpoints report role | Each settings module logs role on startup hook; `GET /whoami` returns `{agent_id, role}` for the authenticated bearer token; controller and worker startup banners are unmistakable |

## User Constraints (from CONTEXT.md)

### Locked Decisions

The 33 locked decisions in 26-CONTEXT.md cover the entire technical design space. They include:

- **D-01 to D-04:** Module layout — `phaze.tasks.controller` (control role) and `phaze.tasks.agent_worker` (agent role); `phaze.tasks.worker` and `phaze.tasks.session` deleted.
- **D-05 to D-09:** Each settings module exports a top-level `settings` dict with `queue`, `functions`, `startup`, `shutdown`, `concurrency`. Controller `functions` includes only fileless tasks (`scan_finalize`, `analyze_dispatch`, `proposal_followup`). Agent `functions` includes only file-bound tasks (`scan_walk`, `extract_metadata`, `fingerprint_file`, `execute_proposal`, `process_file`).
- **D-10 to D-13:** `PhazeAgentClient` is a httpx.AsyncClient wrapper instantiated in agent startup hook and stashed at `ctx["api_client"]`. All file-bound task bodies pull it from `ctx` (never global). Five rewrites: `tasks/functions.py`, `tasks/metadata_extraction.py`, `tasks/fingerprint.py`, `tasks/scan.py`, `tasks/execution.py`.
- **D-14 to D-16:** Settings split — `BaseSettings`, `ControlSettings(BaseSettings)`, `AgentSettings(BaseSettings)` with `get_settings()` factory dispatching on `PHAZE_ROLE`.
- **D-17 to D-20:** New endpoint `GET /whoami` returns `{agent_id, role: "agent"|"control"}` — auth dependency is `get_authenticated_agent` (verbatim from Phase 25).
- **D-21 to D-24:** `AgentTaskRouter` in `phaze.services.agent_task_router`. Single method `await enqueue(agent_id: str, task: str, **kwargs) -> None`. Wired into `app.state.task_router` in `main.py`. Replaces inline auto-enqueue at `agent_files.py` lines 100-115 with a call to `request.app.state.task_router.enqueue(...)`.
- **D-25:** Subprocess import-boundary test — agent worker module must not transitively import `phaze.database` or `sqlalchemy.ext.asyncio`.
- **D-26:** `PUT /analysis/{file_id}` — idempotent upsert keyed on `file_id`. Uses `pg_insert.on_conflict_do_update(index_elements=["file_id"], set_={...})` mirroring `ingestion.py:91-119`.
- **D-27:** `POST /tracklists` — Redis-backed idempotency via `tracklist_req:{request_id}` (1-hour TTL) + `tracklist_resp:{request_id}` cached response.
- **D-28:** `PATCH /proposals/{id}/state` — state-machine validation. Proposal: `APPROVED→EXECUTED|FAILED`. FileRecord: joint update on proposal state (`MOVED` on EXECUTED, `UNCHANGED` on FAILED). Single transaction.
- **D-29 to D-30:** Per-agent queues — `Queue.from_url(redis_url, name=f"agent:{agent_id}")` in agent worker startup. Controller side enqueues to that named queue via `AgentTaskRouter`.
- **D-31:** `respx` added as dev-only dep for contract tests of `PhazeAgentClient`.
- **D-32:** `tenacity` added as runtime dep — retry policy: 3 attempts, exponential backoff base 0.5s max 4s, jitter, retry on `(httpx.ConnectError, httpx.ReadTimeout)` + 5xx `HTTPStatusError`, **no retry on 4xx**.
- **D-33:** mypy strict opt-in for the two new files (`phaze.services.agent_client`, `phaze.services.agent_task_router`) via `[[tool.mypy.overrides]]`.

### Claude's Discretion

- Concrete file names for new modules (within the locked package layout).
- Internal method signatures of `PhazeAgentClient` (one method per remote endpoint vs. a single `request()` with verb arg) — recommend one method per endpoint for type clarity.
- Exact log levels / structured-log shape for startup banners.
- Test-fixture layout for respx contract tests.
- The internal `_ALLOWED_TRANSITIONS` table representation.

### Deferred Ideas (OUT OF SCOPE)

- mTLS between agent and application server (TBD, post-26).
- Heartbeat / liveness ping from agent to controller.
- Multi-region agents / cross-region queue routing.
- Migrating `services/` files into mypy-strict (only the two new files are opted in; the existing exclusion stays).
- Worker autoscaling / dynamic queue creation.

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| saq | >=0.26.3 | Async task queue with per-role settings modules | Already in project stack (see CLAUDE.md). Native async, Redis-backed, supports named queues + startup/shutdown hooks. [VERIFIED: pyproject.toml lists saq>=0.26.3] |
| httpx | latest (already installed) | Async HTTP client for `PhazeAgentClient` | Already used elsewhere in services; FastAPI's recommended test client. [VERIFIED: imported in `discogs_matcher.py`] |
| tenacity | >=8.5.0 | Retry policy for transient httpx errors | Industry-standard Python retry library; declarative decorator + composable predicates. [VERIFIED: PyPI shows tenacity 8.5.0 stable. CITED: tenacity.readthedocs.io] |
| respx | >=0.21.1 | httpx mock library for dev/test | The canonical mock library for httpx. Maintained, current versions support httpx 0.27+. [CITED: lundberg.github.io/respx] |
| pydantic-settings | >=2.13.1 | Already-present Settings library; new role subclasses build on this | Per CLAUDE.md stack. Subclassing BaseSettings is officially supported. [VERIFIED: pyproject.toml] |
| redis (async) | latest (already installed) | Idempotency cache for `POST /tracklists` | Already wired in `phaze/redis_client.py`. Use `Redis.from_url(...)` async. [VERIFIED: redis_client.py] |
| SQLAlchemy + asyncpg | already present | DB layer for new endpoints | Already in stack. [VERIFIED] |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| pytest-asyncio | already present | Async test support for respx-based client tests | All new tests in `tests/services/test_agent_client.py` |
| sqlalchemy.dialects.postgresql.insert | stdlib of SQLAlchemy | `pg_insert(...).on_conflict_do_update(...)` for D-26 upsert | One specific call site: `PUT /analysis/{file_id}` handler |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| tenacity decorator | Hand-rolled retry loop | Hand-rolled is fewer deps but loses the declarative `retry_if_exception(callable)` predicate that makes the 4xx/5xx split readable |
| respx | pytest-httpx (`pytest_httpx`) | Both work. respx has a `@respx.mock` decorator style that's slightly more terse; pytest-httpx uses a fixture. Phase 25 prior art uses respx-style mocking patterns in `25-PATTERNS.md`. Stick with respx for consistency. |
| Subclassing BaseSettings | Single Settings class with `Optional[...]` fields + runtime guards | Subclassing gives compile-time clarity about which fields each role requires. The `model_validator(mode="after")` enforces required-when-role-is-X at construction time. Single-class with optionals leaks role concerns into call sites. |
| `Queue.from_url(..., name=f"agent:{agent_id}")` | Multiple Redis instances per role | Named queues on a single Redis are the SAQ-blessed multi-tenant pattern. Multiple Redis instances is extreme isolation we don't need. |

**Installation:**

```bash
uv add tenacity
uv add --dev respx
```

**Version verification (TODO — planner should run before locking versions):**

```bash
uv pip show saq tenacity respx httpx redis 2>&1 | grep -E "Name|Version"
# Or, for fresh-from-pypi confirmation:
# npm view is npm; use the equivalent uv pip show. Cross-check pypi.org/project/<pkg>.
```

Training data versions may be months stale — confirm against the live PyPI registry in the first Wave 0 task. tenacity has been at 8.5.0 since 2024-08; respx 0.21.1 has been current since 2024 as well. SAQ released 0.26.3 in late 2025.

[ASSUMED] Exact pin of tenacity 8.5.0 — verify with `uv pip install tenacity` after `uv lock`.
[ASSUMED] respx 0.21.1 — verify against PyPI before committing pyproject.toml change.

## Architecture Patterns

### System Architecture Diagram

```
┌───────────────────────────────────────────────────────────┐
│                  APPLICATION SERVER                        │
│  ┌──────────────┐    ┌──────────────────────────────┐     │
│  │ FastAPI app  │    │  Controller (SAQ)            │     │
│  │              │    │  - settings.functions:       │     │
│  │  routers/    │    │    scan_finalize             │     │
│  │   agent_*.py │    │    analyze_dispatch          │     │
│  │              │    │    proposal_followup         │     │
│  │  → PG via    │    │  - startup: open DB pool     │     │
│  │    async-    │    │                              │     │
│  │    pg pool   │    └──────────────┬───────────────┘     │
│  │              │                   │                      │
│  │  app.state.  │                   │                      │
│  │  task_router │                   │                      │
│  │              │                   │                      │
│  └──────┬───────┘                   │                      │
│         │                           │                      │
│         │ enqueue(agent_id, task)   │                      │
│         ▼                           │                      │
│  ┌─────────────────────────────────────────────────┐      │
│  │  Redis (single instance)                         │      │
│  │  - agent:<agent_id> queues                       │      │
│  │  - default queue (controller)                    │      │
│  │  - tracklist_req:{request_id} (1h TTL)           │      │
│  │  - tracklist_resp:{request_id} (1h TTL)          │      │
│  │  - PG (FileRecord, Proposal, AnalysisResult,     │      │
│  │       Tracklist, AgentCredential, …)             │      │
│  └─────────────────────────────────────────────────┘      │
└───────────────────────────────────────────────────────────┘
                  ▲                       ▲
                  │ HTTP (Bearer)         │ Redis (SAQ jobs)
                  │                       │
┌─────────────────┴───────────────────────┴────────────────┐
│                  AGENT (separate process / host)          │
│  ┌────────────────────────────────────────────────────┐  │
│  │  Agent Worker (SAQ)                                │  │
│  │  - settings.queue = Queue.from_url(                │  │
│  │      redis_url, name=f"agent:{agent_id}")          │  │
│  │  - settings.functions:                             │  │
│  │      scan_walk                                     │  │
│  │      extract_metadata                              │  │
│  │      fingerprint_file                              │  │
│  │      execute_proposal                              │  │
│  │      process_file                                  │  │
│  │  - startup(ctx):                                   │  │
│  │      ctx["api_client"] = PhazeAgentClient(...)     │  │
│  │  - shutdown(ctx):                                  │  │
│  │      await ctx["api_client"].aclose()              │  │
│  │                                                    │  │
│  │  ─── NO phaze.database, NO sqlalchemy ─────────    │  │
│  │  ─── ONLY filesystem + HTTP + Redis (via SAQ) ─    │  │
│  └────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
```

Data flow for a single file scan:

1. User triggers scan → controller enqueues `scan_walk` on `agent:<id>` queue.
2. Agent picks up `scan_walk` → walks filesystem → `await api_client.register_file(...)` → server inserts FileRecord, server-side `AgentTaskRouter` enqueues `extract_metadata` on same agent's queue.
3. Agent picks up `extract_metadata` → reads file via mutagen → `await api_client.put_analysis(file_id, payload)` → server upserts.
4. (Repeat for `fingerprint_file`.)
5. Server's `analyze_dispatch` (controller-side, fileless) runs when sufficient data → enqueues LLM proposal → controller `proposal_followup` records proposal.
6. Human approves proposal → server enqueues `execute_proposal` on agent's queue → agent moves file → `await api_client.patch_proposal_state(id, "EXECUTED")` → server joint-updates Proposal + FileRecord in one transaction.

### Recommended Project Structure

```
src/phaze/
├── tasks/
│   ├── __init__.py
│   ├── controller.py          # NEW: settings dict for control role
│   ├── agent_worker.py        # NEW: settings dict for agent role
│   ├── scan.py                # REWRITTEN: file-bound, HTTP-backed
│   ├── metadata_extraction.py # REWRITTEN
│   ├── fingerprint.py         # REWRITTEN
│   ├── execution.py           # REWRITTEN
│   ├── functions.py           # REWRITTEN (process_file)
│   ├── controller_tasks.py    # NEW (optional): fileless task bodies
│   │                          #   (scan_finalize, analyze_dispatch, proposal_followup)
│   ├── worker.py              # DELETED
│   └── session.py             # DELETED
├── services/
│   ├── agent_client.py        # NEW: PhazeAgentClient (httpx wrapper)
│   ├── agent_task_router.py   # NEW: AgentTaskRouter (controller-side enqueuer)
│   └── (existing services unchanged)
├── routers/
│   ├── agent_analysis.py      # NEW: PUT /analysis/{file_id}
│   ├── agent_tracklists.py    # NEW: POST /tracklists (idempotent)
│   ├── agent_proposals.py     # NEW: PATCH /proposals/{id}/state
│   ├── agent_whoami.py        # NEW: GET /whoami
│   ├── agent_files.py         # MODIFIED: replace inline enqueue
│   │                          #   with app.state.task_router.enqueue(...)
│   └── (other agent_*.py from Phase 25 unchanged)
├── config.py                  # MODIFIED: split Base/Control/Agent + get_settings()
└── main.py                    # MODIFIED: wire app.state.task_router
```

### Pattern 1: SAQ Settings Module Layout

**What:** SAQ's CLI (`saq <import.path>.settings`) imports the named module and reads a top-level `settings` dict. That dict declares the Queue, functions, startup, shutdown, and concurrency.

**When to use:** Once per role.

**Example (controller):**

```python
# src/phaze/tasks/controller.py
# Source: SAQ README (github.com/tobymao/saq) + project Phase 25 conventions

import logging
from typing import Any

from saq import Queue

from phaze.config import get_settings
from phaze.database import close_engine, init_engine
from phaze.tasks.controller_tasks import analyze_dispatch, proposal_followup, scan_finalize

logger = logging.getLogger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    """SAQ startup hook — runs inside the event loop."""
    settings = get_settings()
    logger.info("phaze.controller startup role=%s redis=%s", settings.role, settings.redis_url)
    await init_engine()


async def shutdown(ctx: dict[str, Any]) -> None:
    """SAQ shutdown hook — runs inside the event loop."""
    logger.info("phaze.controller shutdown")
    await close_engine()


settings = {
    "queue": Queue.from_url(get_settings().redis_url, name="controller"),
    "functions": [scan_finalize, analyze_dispatch, proposal_followup],
    "startup": startup,
    "shutdown": shutdown,
    "concurrency": get_settings().controller_concurrency,
}
```

**Example (agent worker):**

```python
# src/phaze/tasks/agent_worker.py
# Source: SAQ README + Phase 26 CONTEXT.md (D-10, D-29)
#
# CRITICAL: this module MUST NOT import phaze.database or sqlalchemy.ext.asyncio.
# Enforced by tests/tasks/test_agent_worker_import_boundary.py.

import logging
from typing import Any

from saq import Queue

from phaze.config import get_settings
from phaze.services.agent_client import PhazeAgentClient
from phaze.tasks.execution import execute_proposal
from phaze.tasks.fingerprint import fingerprint_file
from phaze.tasks.functions import process_file
from phaze.tasks.metadata_extraction import extract_metadata
from phaze.tasks.scan import scan_walk

logger = logging.getLogger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    cfg = get_settings()
    logger.info("phaze.agent_worker startup agent_id=%s api=%s", cfg.agent_id, cfg.agent_api_base_url)
    ctx["api_client"] = PhazeAgentClient(
        base_url=cfg.agent_api_base_url,
        bearer_token=cfg.agent_api_bearer_token.get_secret_value(),
        timeout=cfg.agent_api_timeout_seconds,
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    logger.info("phaze.agent_worker shutdown")
    client = ctx.get("api_client")
    if client is not None:
        await client.aclose()


settings = {
    "queue": Queue.from_url(get_settings().redis_url, name=f"agent:{get_settings().agent_id}"),
    "functions": [scan_walk, extract_metadata, fingerprint_file, execute_proposal, process_file],
    "startup": startup,
    "shutdown": shutdown,
    "concurrency": get_settings().agent_concurrency,
}
```

**Docker invocation:**

```yaml
# docker-compose.yml — modified
services:
  controller:
    command: ["uv", "run", "saq", "phaze.tasks.controller.settings"]
    environment:
      PHAZE_ROLE: control
      # ...

  agent:
    command: ["uv", "run", "saq", "phaze.tasks.agent_worker.settings"]
    environment:
      PHAZE_ROLE: agent
      PHAZE_AGENT_ID: agent-1
      PHAZE_AGENT_API_BASE_URL: http://app:8000
      # ...
```

[VERIFIED: SAQ README at github.com/tobymao/saq] The CLI argument is `<module.path>.<settings_dict_name>` — saq splits on the last `.` and imports the module, then reads the named attribute from the module's namespace. `phaze.tasks.controller.settings` works; the dict must be a module-level attribute named `settings` (or whatever you pass).

### Pattern 2: PhazeAgentClient with Tenacity 4xx/5xx Split

**What:** A small httpx.AsyncClient wrapper. Retries on transient errors (5xx, ConnectError, ReadTimeout). **Does not** retry on 4xx — those are programming errors or auth failures and must surface immediately.

**Why this matters:** A 401 retried 3 times floods the auth log and delays the operator-visible failure. A 404 retried is a definite bug. The whole point of the split is that retries are reserved for "this might transiently work."

**The cleanest tenacity idiom for 4xx/5xx split:**

```python
# src/phaze/services/agent_client.py
# Source: tenacity 8.5+ API + httpx docs
# CITED: tenacity.readthedocs.io/en/latest/api.html

import logging
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

logger = logging.getLogger(__name__)


class AgentApiError(Exception):
    """Base for all PhazeAgentClient errors."""


class AgentApiAuthError(AgentApiError):
    """401 / 403 from the server. NEVER retried."""


class AgentApiClientError(AgentApiError):
    """Any 4xx that is not auth. NEVER retried."""


class AgentApiServerError(AgentApiError):
    """5xx after retries exhausted, or persistent ConnectError/ReadTimeout."""


def _should_retry(exc: BaseException) -> bool:
    """Predicate for tenacity: retry on transient network errors and 5xx.

    Does NOT retry on:
      - 4xx HTTPStatusError (auth, validation, not-found — all caller bugs or auth issues)
      - any non-httpx exception
    """
    if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return False


class PhazeAgentClient:
    def __init__(self, base_url: str, bearer_token: str, timeout: float = 30.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {bearer_token}"},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """All HTTP calls funnel through here so the retry policy is applied uniformly."""
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential_jitter(initial=0.5, max=4.0),
                retry=retry_if_exception(_should_retry),
                reraise=True,
            ):
                with attempt:
                    response = await self._client.request(method, path, **kwargs)
                    response.raise_for_status()  # raises HTTPStatusError
                    return response
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status in (401, 403):
                raise AgentApiAuthError(f"{method} {path} -> {status}") from e
            if 400 <= status < 500:
                raise AgentApiClientError(f"{method} {path} -> {status}: {e.response.text}") from e
            # 5xx — already retried by tenacity, now exhausted
            raise AgentApiServerError(f"{method} {path} -> {status} after retries") from e
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as e:
            raise AgentApiServerError(f"{method} {path} network failure after retries") from e

        # Unreachable, but mypy needs it
        raise AssertionError("retry loop exited without return")

    # One method per remote endpoint — explicit, type-checked, easy to mock with respx.
    async def put_analysis(self, file_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        r = await self._request("PUT", f"/analysis/{file_id}", json=payload)
        return r.json()

    async def post_tracklist(self, request_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = {"request_id": request_id, **payload}
        r = await self._request("POST", "/tracklists", json=body)
        return r.json()

    async def patch_proposal_state(self, proposal_id: int, new_state: str, reason: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"state": new_state}
        if reason is not None:
            body["reason"] = reason
        r = await self._request("PATCH", f"/proposals/{proposal_id}/state", json=body)
        return r.json()

    async def whoami(self) -> dict[str, Any]:
        r = await self._request("GET", "/whoami")
        return r.json()
```

Key points:

1. **`retry_if_exception(callable)`** is the correct primitive for "retry on subset of an exception type." `retry_if_exception_type` cannot inspect attributes (like `response.status_code`).
2. **`response.raise_for_status()`** inside the retry block ensures 5xx becomes an exception that tenacity sees. Without this, a 5xx would be a successful function return and no retry would occur.
3. **`reraise=True`** means tenacity reraises the last exception rather than wrapping in `RetryError`. Keeps stack traces clean and lets the outer try/except classify the failure.
4. **`wait_exponential_jitter`** is the tenacity 8.x recommended primitive for "exponential backoff with jitter". [CITED: tenacity.readthedocs.io/en/latest/api.html]
5. The outer `try/except` runs *after* retries are exhausted. 4xx never enters tenacity's retry loop (the predicate returns False), so on 4xx, `HTTPStatusError` immediately surfaces and the outer handler classifies as Auth/Client.

### Pattern 3: AgentTaskRouter (controller-side per-agent enqueue)

```python
# src/phaze/services/agent_task_router.py
# Source: SAQ Queue API + Phase 26 D-21..D-24

import logging
from typing import Any

from saq import Queue

logger = logging.getLogger(__name__)


class AgentTaskRouter:
    """Enqueues SAQ jobs onto the queue belonging to a specific agent.

    Lazily constructs per-agent Queue instances and caches them. All queues share
    the same Redis URL but have distinct queue names of the form ``agent:<agent_id>``.
    """

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._queues: dict[str, Queue] = {}

    def _queue_for(self, agent_id: str) -> Queue:
        if agent_id not in self._queues:
            self._queues[agent_id] = Queue.from_url(self._redis_url, name=f"agent:{agent_id}")
        return self._queues[agent_id]

    async def enqueue(self, agent_id: str, task: str, **kwargs: Any) -> None:
        queue = self._queue_for(agent_id)
        logger.debug("enqueue agent=%s task=%s kwargs_keys=%s", agent_id, task, list(kwargs.keys()))
        await queue.enqueue(task, **kwargs)

    async def aclose(self) -> None:
        for queue in self._queues.values():
            await queue.disconnect()
        self._queues.clear()
```

**Wiring in `main.py`:**

```python
# src/phaze/main.py — modified
from contextlib import asynccontextmanager
from fastapi import FastAPI

from phaze.config import get_settings
from phaze.services.agent_task_router import AgentTaskRouter


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.task_router = AgentTaskRouter(settings.redis_url)
    yield
    await app.state.task_router.aclose()


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    # ... wire routers (including the new agent_analysis, agent_tracklists, agent_proposals, agent_whoami)
    return app
```

**Replacing inline auto-enqueue in `agent_files.py`:**

```python
# Before (inline):
# queue = Queue.from_url(...)
# await queue.enqueue("extract_metadata", file_id=row.id)

# After (via router):
await request.app.state.task_router.enqueue(
    agent.agent_id,         # from get_authenticated_agent dep
    "extract_metadata",
    file_id=row.id,
)
```

### Pattern 4: Idempotent Upsert for PUT /analysis/{file_id} (D-26)

Mirrors `phaze/services/ingestion.py:91-119` exactly. The idiom:

```python
# src/phaze/routers/agent_analysis.py
# Source: phaze/services/ingestion.py:91-119 (existing pattern) + SQLAlchemy docs

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.analysis import AnalysisResult
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.analysis import AnalysisIn, AnalysisOut

router = APIRouter(prefix="/analysis", tags=["agent"])


@router.put("/{file_id}", response_model=AnalysisOut, status_code=status.HTTP_200_OK)
async def upsert_analysis(
    file_id: int,
    payload: AnalysisIn,
    session: AsyncSession = Depends(get_session),
    agent = Depends(get_authenticated_agent),
) -> AnalysisOut:
    stmt = (
        pg_insert(AnalysisResult)
        .values(file_id=file_id, **payload.model_dump())
        .on_conflict_do_update(
            index_elements=["file_id"],
            set_={
                # Only the mutable columns — keep created_at immutable.
                **payload.model_dump(),
                "updated_at": _utcnow(),
            },
        )
        .returning(AnalysisResult)
    )
    result = await session.execute(stmt)
    row = result.scalar_one()
    await session.commit()
    return AnalysisOut.model_validate(row, from_attributes=True)
```

Key points:

1. **`pg_insert` from `sqlalchemy.dialects.postgresql`** — NOT `sqlalchemy.insert`. Only the postgres dialect supports `on_conflict_do_update`. [VERIFIED: SQLAlchemy 2.0 docs]
2. **`index_elements=["file_id"]`** assumes `AnalysisResult.file_id` has a unique constraint. Verify in the model; if not, the migration must add one.
3. **`.returning(AnalysisResult)`** returns the persisted row in one round trip — preferred over re-querying. [CITED: SQLAlchemy 2.0 RETURNING docs]
4. **`scalar_one()`** unwraps the single row.
5. **`await session.commit()`** — required because the route uses `get_session` (which doesn't auto-commit). Match the existing project convention.
6. **Status code 200** — even on first-time create. PUT-with-upsert is conventionally 200 for both create and update; 201 reserved for POST. CONTEXT.md D-26 confirms this.

### Pattern 5: Idempotent POST /tracklists with Redis (D-27)

```python
# src/phaze/routers/agent_tracklists.py
# Source: Phase 26 CONTEXT.md D-27 + redis-py asyncio docs

import json
from typing import Annotated

import redis.asyncio as redis_async
from fastapi import APIRouter, Depends, Request, status

from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.tracklist import TracklistIn, TracklistOut

router = APIRouter(prefix="/tracklists", tags=["agent"])

TRACKLIST_TTL_SECONDS = 3600  # 1 hour
_REQ_PREFIX = "tracklist_req:"
_RESP_PREFIX = "tracklist_resp:"


async def _get_redis(request: Request) -> redis_async.Redis:
    return request.app.state.redis


@router.post("", response_model=TracklistOut, status_code=status.HTTP_200_OK)
async def create_tracklist(
    payload: TracklistIn,
    redis_client: Annotated[redis_async.Redis, Depends(_get_redis)],
    agent = Depends(get_authenticated_agent),
) -> TracklistOut:
    request_id = payload.request_id  # caller-provided idempotency key
    resp_key = f"{_RESP_PREFIX}{request_id}"
    req_key = f"{_REQ_PREFIX}{request_id}"

    # Fast path: cached response exists -> return it without re-doing work.
    cached = await redis_client.get(resp_key)
    if cached is not None:
        return TracklistOut.model_validate_json(cached)

    # Atomically mark this request_id as "in progress". SET NX returns True iff
    # the key did not exist before. If False, a concurrent request is handling it
    # (or recently handled it without populating resp_key yet).
    won = await redis_client.set(req_key, "1", nx=True, ex=TRACKLIST_TTL_SECONDS)
    if not won:
        # Brief retry on the response key — if a concurrent request is finalizing,
        # the response will appear within milliseconds. Bounded to a small number
        # of attempts; if still nothing, return 409 so the caller backs off.
        for _ in range(10):
            cached = await redis_client.get(resp_key)
            if cached is not None:
                return TracklistOut.model_validate_json(cached)
            await asyncio.sleep(0.05)
        from fastapi import HTTPException
        raise HTTPException(status.HTTP_409_CONFLICT, detail="duplicate in-flight request")

    # We own this request. Do the actual DB write.
    result = await _create_tracklist_rows(session, payload, agent)

    # Cache the response for future duplicate requests.
    out = TracklistOut.model_validate(result, from_attributes=True)
    await redis_client.set(resp_key, out.model_dump_json(), ex=TRACKLIST_TTL_SECONDS)
    return out
```

Key points:

1. **`SET key value NX EX ttl`** is a single atomic Redis command — `redis-py.asyncio` exposes it as `await redis.set(key, value, nx=True, ex=ttl)` returning `True` on success or `None`/`False` on already-exists. [CITED: redis-py docs]
2. **TTL on both keys** — `req_key` keeps the lock until response is cached or expires. `resp_key` is the actual idempotency cache.
3. **`decode_responses` warning** — if the Redis client was created with `decode_responses=True`, you get `str`; otherwise `bytes`. Verify in `phaze/redis_client.py` and use `.decode()` if needed. From inspection, `redis_client.py` likely uses `decode_responses=True` for consistency. The planner should verify.
4. **Pitfall:** if the writer crashes between "won the lock" and "cached the response", the next call will see `req_key` set + no `resp_key`. The bounded retry handles the *fast* case (concurrent request finishing). For the *slow* case (writer crashed), the caller will get 409 until `req_key` expires. This is acceptable for the project's scale; document it.
5. **Payload-hash check (optional):** if a caller reuses a `request_id` for a *different* payload, this design returns the cached response without warning. To detect, the writer can store a hash of the canonical payload alongside the response and compare on cache hit. **Recommended Wave 0 task to confirm whether D-27 requires payload-hash collision detection.** See Open Questions.

### Pattern 6: State-Machine Validation for PATCH /proposals/{id}/state (D-28)

```python
# src/phaze/routers/agent_proposals.py
# Source: Phase 26 D-28 + models/proposal.py + models/file.py

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.file import FileRecord, FileState
from phaze.models.proposal import Proposal, ProposalState  # exact name TBD per models
from phaze.routers.agent_auth import get_authenticated_agent

router = APIRouter(prefix="/proposals", tags=["agent"])

# Allowed transitions. Each entry: from_state -> set of legal to_states.
_PROPOSAL_TRANSITIONS: dict[ProposalState, set[ProposalState]] = {
    ProposalState.APPROVED: {ProposalState.EXECUTED, ProposalState.FAILED},
}

# When a Proposal transitions, what does its FileRecord become?
_FILE_FOLLOW: dict[ProposalState, FileState] = {
    ProposalState.EXECUTED: FileState.MOVED,
    ProposalState.FAILED: FileState.UNCHANGED,
}


@router.patch("/{proposal_id}/state", status_code=status.HTTP_200_OK)
async def patch_proposal_state(
    proposal_id: int,
    payload: ProposalStateIn,  # {state: "EXECUTED"|"FAILED", reason?: str}
    session: AsyncSession = Depends(get_session),
    agent = Depends(get_authenticated_agent),
) -> ProposalOut:
    proposal = await session.get(Proposal, proposal_id)
    if proposal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "proposal not found")

    new_state = ProposalState(payload.state)
    allowed = _PROPOSAL_TRANSITIONS.get(proposal.state, set())
    if new_state not in allowed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"illegal transition {proposal.state.value} -> {new_state.value}",
        )

    # Joint update in a single transaction.
    proposal.state = new_state
    proposal.failure_reason = payload.reason

    file_record = await session.get(FileRecord, proposal.file_id)
    if file_record is not None:
        file_record.state = _FILE_FOLLOW[new_state]

    await session.commit()
    await session.refresh(proposal)
    return ProposalOut.model_validate(proposal, from_attributes=True)
```

Key points:

1. **Table-driven validation** > scattered `if/elif`. The transition map is a single source of truth, easy to test exhaustively, easy to extend.
2. **Joint update in one transaction** — D-28 requires this. SQLAlchemy AsyncSession holds both changed rows until `commit()`; either both are persisted or neither is.
3. **409 Conflict** for illegal transitions (NOT 400) — the request was syntactically valid; it's the *state* that conflicts. Aligns with REST conventions.
4. **Verify enum values against models** — current `phaze/models/proposal.py` and `phaze/models/file.py` define the enums. Wave 0 task: confirm `EXECUTED`, `FAILED`, `MOVED`, `UNCHANGED` exist exactly as named.

### Pattern 7: pydantic-settings Role-Specific Subclassing (D-14..D-16)

```python
# src/phaze/config.py — modified
# Source: pydantic-settings v2 docs + CONTEXT.md D-14..D-16

from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Role(StrEnum):
    CONTROL = "control"
    AGENT = "agent"


class BaseAppSettings(BaseSettings):
    """Fields shared by both roles."""

    model_config = SettingsConfigDict(
        env_prefix="PHAZE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    role: Role
    redis_url: str
    log_level: str = "INFO"


class ControlSettings(BaseAppSettings):
    """Settings required when role=control. Inherits env loading from BaseAppSettings."""

    database_url: str  # required when role=control
    controller_concurrency: int = 8

    @model_validator(mode="after")
    def _enforce_role(self) -> "ControlSettings":
        if self.role is not Role.CONTROL:
            raise ValueError(f"ControlSettings instantiated with role={self.role}")
        return self


class AgentSettings(BaseAppSettings):
    """Settings required when role=agent. NO database_url — agents have no DB access."""

    agent_id: str
    agent_api_base_url: str
    agent_api_bearer_token: SecretStr
    agent_api_timeout_seconds: float = 30.0
    agent_concurrency: int = 4

    @model_validator(mode="after")
    def _enforce_role(self) -> "AgentSettings":
        if self.role is not Role.AGENT:
            raise ValueError(f"AgentSettings instantiated with role={self.role}")
        return self


@lru_cache(maxsize=1)
def get_settings() -> BaseAppSettings:
    # Read role from env without using a Settings class (to avoid loading the
    # wrong subclass first).
    import os
    role = Role(os.environ.get("PHAZE_ROLE", "control"))
    if role is Role.CONTROL:
        return ControlSettings()  # type: ignore[call-arg]
    return AgentSettings()  # type: ignore[call-arg]
```

Key points:

1. **Subclassing BaseSettings preserves env loading.** Each subclass inherits `model_config` and re-reads from env when instantiated. [VERIFIED: pydantic-settings v2 docs]
2. **`model_validator(mode="after")`** runs after field validation, perfect for cross-field invariants (here: `role` must match the subclass identity).
3. **`@lru_cache`** ensures `get_settings()` is a singleton per process — important so SAQ's settings module import and FastAPI's startup both see the same instance.
4. **The `# type: ignore[call-arg]`** is unfortunately necessary because pydantic-settings classes have all their fields populated from env at construction time but mypy doesn't see that. Acceptable for this one call site.
5. **`extra="ignore"`** means an env var like `PHAZE_DATABASE_URL` set on the agent host doesn't crash `AgentSettings()` — it's just ignored. Important for sharing the same `.env.example` template.

### Pattern 8: mypy strict opt-in via overrides (D-33)

```toml
# pyproject.toml — add to existing [tool.mypy] section
[tool.mypy]
# ... existing config ...
exclude = "^(tests/|prototype/|services/)"

[[tool.mypy.overrides]]
module = "phaze.services.agent_client"
disallow_untyped_defs = true
disallow_incomplete_defs = true
check_untyped_defs = true
warn_return_any = true
strict_equality = true

[[tool.mypy.overrides]]
module = "phaze.services.agent_task_router"
disallow_untyped_defs = true
disallow_incomplete_defs = true
check_untyped_defs = true
warn_return_any = true
strict_equality = true
```

Key points:

1. **`[[tool.mypy.overrides]]`** with double brackets — TOML array of tables. Each entry overrides settings for a specific module pattern. [CITED: mypy.readthedocs.io/en/stable/config_file.html]
2. **`module` accepts wildcards** — but here we specify the exact two modules to opt in.
3. **Override beats exclude** — the `exclude` regex prevents mypy from checking files in `services/`, but a per-module override re-enables checking for the named module. This is the standard pattern for incremental strictness adoption. [CITED: mypy docs]
4. **TIP for the planner:** add the override and run `uv run mypy phaze/services/agent_client.py` explicitly to confirm mypy sees the file. If exclude is `^services/` matching from a root-relative path, the override should win — but verify in Wave 0.

### Anti-Patterns to Avoid

- **Don't import `phaze.database` from anything in the agent_worker import chain.** The whole D-25 invariant rests on this. Even a transitive import via `phaze.services.something` will break the contract.
- **Don't stash `PhazeAgentClient` as a module-global.** It must live in `ctx["api_client"]` so SAQ owns its lifecycle. Module globals don't get an `await aclose()` on shutdown.
- **Don't use `retry_if_exception_type(HTTPStatusError)`.** That retries 4xx, which is wrong. Use the callable predicate `retry_if_exception(_should_retry)`.
- **Don't catch `RetryError`.** With `reraise=True`, tenacity reraises the original exception. Catching `RetryError` is the pre-2.0 idiom and is now wrong.
- **Don't construct `Queue.from_url(...)` outside the settings module on the agent.** SAQ's worker needs to own the queue's connection lifecycle. Inline `Queue.from_url` in `AgentTaskRouter` is fine — that lives on the application server and is wired into FastAPI's lifespan.
- **Don't `await session.commit()` in `agent_files.py` after `task_router.enqueue(...)`.** Order matters: commit the FileRecord first so the agent (which picks up the job within milliseconds) sees a row when it queries. Enqueue must happen AFTER commit, not before.
- **Don't use `pytest.mark.asyncio` for the import-boundary test.** That test must run in a subprocess with a fresh interpreter (see Pattern 9). asyncio is irrelevant.
- **Don't share a single `AsyncClient` across a worker pool *and* the FastAPI app.** Each role has its own. Agent worker creates one in startup. The app server doesn't need one for inbound traffic.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Retry logic with exponential backoff | Custom `for i in range(3): await asyncio.sleep(...)` loop | `tenacity.AsyncRetrying` with `wait_exponential_jitter` | Handles jitter, predicates, and exception classification declaratively |
| HTTP client connection pooling | Per-call `httpx.AsyncClient()` | One long-lived `httpx.AsyncClient` in `ctx["api_client"]` | Connection reuse is critical; per-call clients leak file descriptors |
| Idempotency cache | Hand-rolled hash table + asyncio.Lock | Redis SET NX EX | Atomic, cross-process, has TTL built-in |
| State-machine validation | If/elif chains in the handler | Dict-of-sets transition table + lookup | Trivial to test exhaustively; readable; impossible to forget a case |
| HTTP mocking in tests | Monkey-patching `httpx.AsyncClient.request` | `respx` | respx asserts URL patterns + headers + side_effects sequences. Monkey-patching loses all that |
| Upsert via SELECT-then-INSERT-or-UPDATE | Two-statement try/except | `pg_insert(...).on_conflict_do_update(...)` | Single round trip; atomic; immune to races |
| Subprocess test for import isolation | Try/except `import` inside the test process | `subprocess.run([sys.executable, "-c", "..."])` | Once a module is imported in the test process, it stays in `sys.modules` and contaminates downstream tests |

**Key insight:** Phase 26 looks small (rewrites + 4 endpoints) but its quality is determined by whether the dependency graph stays clean. Every "don't hand-roll" item above prevents a class of slow, intermittent failure that's hard to debug after the fact.

## Common Pitfalls

### Pitfall 1: SAQ worker silently uses the wrong queue name

**What goes wrong:** Operator changes `PHAZE_AGENT_ID` in env but the worker was started with a queue name baked in at module import time. The worker connects to Redis but listens on the wrong queue.

**Why it happens:** `Queue.from_url(redis_url, name=f"agent:{get_settings().agent_id}")` evaluates `get_settings()` at *import* time. If env vars change between process spawn and import (rare but possible with init-containers or docker-compose env files), the queue name is wrong but the worker starts cleanly.

**How to avoid:**
- Log the queue name in the startup hook — operator sees a banner like `phaze.agent_worker startup queue=agent:agent-1`.
- Add a `GET /whoami`-driven sanity check at startup: the agent calls `/whoami`, gets back `{agent_id: "agent-1"}`, and asserts it matches `settings.agent_id`. If mismatch, exit non-zero. This is the canonical anti-misconfiguration probe.

**Warning signs:** Jobs sit in `agent:agent-1` queue indefinitely; the worker is consuming `agent:agent-2`'s jobs (because that's what env said at import time).

### Pitfall 2: Tenacity retries 4xx because predicate is wrong

**What goes wrong:** An auth misconfiguration causes the server to return 401. The agent retries 3 times before failing, spamming the auth log and delaying the visible error.

**Why it happens:** Easy to write `retry=retry_if_exception_type(httpx.HTTPStatusError)` by mistake — it covers both 4xx and 5xx.

**How to avoid:**
- Use the explicit `retry_if_exception(_should_retry)` predicate (Pattern 2).
- Write a contract test that mocks a 401 response and asserts the underlying mocked route was called *exactly once*. respx supports `route.call_count` for this. If the test sees `call_count == 2` or `== 3`, the predicate is wrong.

**Warning signs:** `assert call_count == 1` failure on the 401 contract test.

### Pitfall 3: agent_worker transitively imports phaze.database

**What goes wrong:** Someone adds `from phaze.services.foo import bar` to a rewritten task, and `phaze.services.foo` imports `phaze.database` somewhere. The agent worker starts successfully on a host with no Postgres, then crashes when the import chain attempts a DB connection on first task.

**Why it happens:** Python's import system is transitive. Static analysis (mypy, ruff) won't catch this — the imports are all valid.

**How to avoid:** The D-25 subprocess import test. Run it in CI on every PR (see Pattern 9 below). It is the only mechanism that catches this drift.

**Warning signs:** Adding any new import to a `tasks/*.py` file should make you re-run the import-boundary test locally.

### Pitfall 4: Idempotency cache returns stale response for different payload

**What goes wrong:** Caller A sends `POST /tracklists` with `request_id=R1, tracks=[...A]`. The response is cached. Caller B (or A retried with new data) sends `POST /tracklists` with `request_id=R1, tracks=[...B]`. Server returns Caller A's response. Caller B believes their data was persisted.

**Why it happens:** Idempotency keys are caller-controlled. If callers reuse an ID, the server has no way to know unless it inspects payload contents.

**How to avoid:** Store a hash of the canonical payload alongside the cached response. On cache hit, compare hashes. If hashes differ, return 409 Conflict.

```python
resp_payload_hash = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
cached_raw = await redis_client.get(resp_key)
if cached_raw:
    cached_envelope = json.loads(cached_raw)
    if cached_envelope["payload_hash"] != resp_payload_hash:
        raise HTTPException(409, "request_id reused with different payload")
    return TracklistOut.model_validate(cached_envelope["response"])
```

**Warning signs:** Operator reports "I retried the same request and got a different response back than expected" — but they had quietly mutated the payload.

### Pitfall 5: httpx.AsyncClient not closed -> resource leak in long-running worker

**What goes wrong:** Agent worker runs for days, processing thousands of jobs. `PhazeAgentClient` is constructed in startup hook but `aclose()` is never called (shutdown hook missing or buggy). File descriptors leak gradually, eventually hitting ulimit and crashing the worker.

**Why it happens:** SAQ's shutdown hook only runs on graceful shutdown. SIGKILL (or Docker stop with too-short grace period) skips it. Also, if startup raises after `ctx["api_client"] = ...` is set, the partial state isn't cleaned up.

**How to avoid:**
- Always pair `httpx.AsyncClient` construction with a registered shutdown hook.
- Use a context manager pattern where possible. SAQ's hook model uses `ctx` rather than a context manager, but the discipline is: never construct an AsyncClient without setting a corresponding `aclose` call somewhere.
- Set Docker's `stop_grace_period: 30s` so SIGTERM gives the worker time to shut down cleanly.
- Add a periodic log line that includes the AsyncClient's pool stats (httpx exposes `_pool` info) so leaks become visible before they crash the process.

**Warning signs:** `Too many open files` errors after days of uptime. Worker pid's `/proc/<pid>/fd` directory grows unboundedly.

### Pitfall 6: Joint update partial-commit (D-28)

**What goes wrong:** `PATCH /proposals/{id}/state` updates Proposal.state successfully but the FileRecord update fails (e.g., race with another transaction). One row is updated, the other isn't.

**Why it happens:** Forgetting that SQLAlchemy AsyncSession is transactional — but only if `commit()` is called once at the end. Two separate `await session.commit()` calls would mean partial commit on failure.

**How to avoid:**
- One `await session.commit()` call per request, at the end after both rows are modified.
- The handler in Pattern 6 demonstrates this — no commit between Proposal change and FileRecord change.
- If the DB raises an integrity error mid-handler, the session rolls back both changes. Match existing project convention (let it propagate as 500).

**Warning signs:** A migration check finds rows where Proposal.state=EXECUTED but FileRecord.state ≠ MOVED.

### Pitfall 7: SAQ settings module imported at collection time has side effects

**What goes wrong:** Pytest collects `tests/tasks/test_agent_worker_*.py`, which imports `phaze.tasks.agent_worker`, which at module-import time calls `Queue.from_url(...)`. The Redis URL points to a real Redis (or is empty) — collection itself crashes or hangs.

**Why it happens:** Module-level `Queue.from_url(...)` runs on every import.

**How to avoid:**
- In tests, never import `phaze.tasks.agent_worker` directly. Test the individual task bodies (which are functions in `tasks/scan.py`, `tasks/metadata_extraction.py`, etc.) directly with a mocked `ctx`.
- For the import-boundary test (D-25), use subprocess. The subprocess gets a controlled env (e.g., `PHAZE_REDIS_URL=redis://localhost:6379/0` pointing at a test Redis or fakeredis, plus `PHAZE_ROLE=agent`, etc.).
- For SAQ-level integration tests, use SAQ's testing utilities or a dedicated fixture that brings up a Redis container.

**Warning signs:** `pytest --collect-only` hangs or errors on the tasks tests.

## Runtime State Inventory

Phase 26 is partly a refactor (rename/restructure of tasks module) and partly new feature work. The Runtime State Inventory applies to the refactor portion.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | None — the rename is from `phaze.tasks.worker` to two new modules (`controller` + `agent_worker`). No SAQ data stored under the *module name*. SAQ job data is stored in Redis under *queue names*, and queue names are changing (was: no SAQ queues yet wired; will be: `controller` + `agent:<id>`). | None — no migration needed because there are no enqueued jobs yet (Phase 26 is the first to wire real SAQ usage). Verify by `redis-cli KEYS "saq:*"` before deployment; if empty, no migration. If non-empty, deferred-cleanup task. |
| Live service config | None — SAQ has no UI-based config. Docker Compose `command:` updates carry the changes. | None — docker-compose.yml updated in this phase. |
| OS-registered state | None — no Windows tasks, no launchd plists, no systemd units carrying the worker module name. Docker is the only process supervisor. | None. |
| Secrets / env vars | `PHAZE_ROLE` is new (not previously set). `PHAZE_AGENT_ID`, `PHAZE_AGENT_API_BASE_URL`, `PHAZE_AGENT_API_BEARER_TOKEN` are new (Phase 25 wired the bearer for HTTP endpoints; Phase 26 adds them to the agent worker's env). | Update `.env.example` and any deployment-target env files to include the new keys. SOPS-managed secrets must add `PHAZE_AGENT_API_BEARER_TOKEN` (or reuse the Phase 25 token if it's the same value). |
| Build artifacts | None — pure Python, no compiled artifacts, no egg-info renames. `uv lock` will pick up the new `tenacity` + `respx` deps. | Run `uv sync` after merge; the lockfile change is committed. |

**Nothing found in category:** Stored data, Live service config, OS-registered state, and Build artifacts categories all return "None." Only Secrets/env vars require action.

## Project Constraints (from CLAUDE.md)

The planner must verify that Phase 26 plans comply with these directives:

- **Python 3.13 only.** All new code must run on 3.13.
- **`uv` only — no bare `pip`, `python`, `pytest`, `mypy`.** Always prefix with `uv run`.
- **Pre-commit must be active.** All hooks must pass before commits. Including bandit, ruff, ruff-format, mypy, yamllint, shellcheck. New files must satisfy these.
- **Ruff line length 150**, target Python 3.13. Per-file: `T201` (print) allowed in CLI/entry points and tests; tests also ignore `PLC` and `S105`.
- **mypy strict** but `services/` is excluded by `exclude = "^(tests/|prototype/|services/)"`. Phase 26 opts in two new files via `[[tool.mypy.overrides]]` (per D-33).
- **85% code coverage minimum** — new endpoints, new client, new router all need unit tests.
- **Codecov flags** — service-specific upload flags per discogsography pattern.
- **Pre-commit frozen SHAs.** Any new hooks must use frozen SHAs (not just version tags).
- **Pyproject.toml section order:** `[build-system]` → `[project]` → `[project.scripts]` → `[tool.*]` → `[dependency-groups]`, deps alphabetically sorted. Maintain on any pyproject change.
- **Worktree + PR per feature.** Phase 26 = one PR. No direct commits to main.
- **GitHub Actions delegate to `just`** (per user MEMORY). If `justfile` exists, add a `just test-phase26` or update existing `just test` command.
- **READMEs kept current.** If a service README exists, update on changes.
- **Never use `--no-verify` on commits.**
- **litellm version pinned** to >=1.82.6,<1.82.7 due to March 2026 supply chain incident — irrelevant to Phase 26 but a phase-wide constraint.

## Code Examples

### Pattern 9: Subprocess Import-Boundary Test (D-25)

```python
# tests/tasks/test_agent_worker_import_boundary.py
# Source: Phase 26 D-25 + Python stdlib subprocess

import subprocess
import sys
import textwrap


def test_agent_worker_does_not_import_phaze_database():
    """The agent worker module must not pull phaze.database or sqlalchemy.ext.asyncio
    into sys.modules at import time. This is a structural invariant — if violated,
    the agent role can no longer run on a host without Postgres reachability.
    """
    script = textwrap.dedent(
        """
        import os
        import sys
        os.environ.setdefault("PHAZE_ROLE", "agent")
        os.environ.setdefault("PHAZE_AGENT_ID", "test-agent")
        os.environ.setdefault("PHAZE_AGENT_API_BASE_URL", "http://localhost:8000")
        os.environ.setdefault("PHAZE_AGENT_API_BEARER_TOKEN", "test")
        os.environ.setdefault("PHAZE_REDIS_URL", "redis://localhost:6379/0")
        import phaze.tasks.agent_worker  # noqa: F401

        forbidden = ("phaze.database", "sqlalchemy.ext.asyncio")
        present = [m for m in forbidden if m in sys.modules]
        if present:
            sys.stderr.write(f"forbidden modules in sys.modules: {present}\\n")
            sys.exit(1)
        sys.exit(0)
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, (
        f"agent_worker import contaminated sys.modules:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
```

Key points:

1. **Use `sys.executable`** — guarantees the same interpreter (and virtualenv) as the running test. Critical when using `uv run`, which activates a specific venv.
2. **`os.environ.setdefault(...)`** in the subprocess script — sets the env vars that `get_settings()` needs, but doesn't override existing ones (helpful for local debugging with custom values).
3. **`textwrap.dedent`** — keeps the test source readable without leading whitespace in the script.
4. **`subprocess.run(check=False)`** + manual `assert returncode == 0` — gives a useful error message including the subprocess's stderr.
5. **`timeout=20`** — bounded; if import hangs (e.g., trying to connect to a DB), test fails fast rather than running indefinitely.
6. **No `pytest.mark.asyncio` decorator** — this is a synchronous test that happens to launch an async-capable subprocess. The test itself is sync.
7. **This is the single highest-leverage validation gate for the phase.** Run it on every CI build. If you only run one extra test from Phase 26, run this one.

### Pattern 10: respx Contract Tests for PhazeAgentClient (D-31)

```python
# tests/services/test_agent_client_contract.py
# Source: respx docs + tenacity behavior + Phase 26 D-31

import httpx
import pytest
import respx

from phaze.services.agent_client import (
    AgentApiAuthError,
    AgentApiClientError,
    AgentApiServerError,
    PhazeAgentClient,
)


@pytest.fixture
def client():
    return PhazeAgentClient(
        base_url="http://app.test",
        bearer_token="test-token",
        timeout=5.0,
    )


@pytest.mark.asyncio
@respx.mock
async def test_put_analysis_happy_path(client):
    route = respx.put("http://app.test/analysis/42").mock(
        return_value=httpx.Response(200, json={"file_id": 42, "bpm": 120}),
    )

    out = await client.put_analysis(42, {"bpm": 120, "key": "C"})

    assert out == {"file_id": 42, "bpm": 120}
    assert route.called
    assert route.call_count == 1

    # Auth header invariant
    sent_req = route.calls.last.request
    assert sent_req.headers["Authorization"] == "Bearer test-token"


@pytest.mark.asyncio
@respx.mock
async def test_401_raises_auth_error_without_retry(client):
    route = respx.put("http://app.test/analysis/42").mock(
        return_value=httpx.Response(401, json={"detail": "invalid token"}),
    )

    with pytest.raises(AgentApiAuthError):
        await client.put_analysis(42, {})

    # CRITICAL: must NOT retry on 4xx
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_404_raises_client_error_without_retry(client):
    route = respx.put("http://app.test/analysis/42").mock(
        return_value=httpx.Response(404),
    )

    with pytest.raises(AgentApiClientError):
        await client.put_analysis(42, {})

    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_500_retries_three_times_then_fails(client):
    route = respx.put("http://app.test/analysis/42").mock(
        return_value=httpx.Response(500),
    )

    with pytest.raises(AgentApiServerError):
        await client.put_analysis(42, {})

    assert route.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_500_then_200_succeeds_on_retry(client):
    route = respx.put("http://app.test/analysis/42").mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(200, json={"file_id": 42}),
        ],
    )

    out = await client.put_analysis(42, {})

    assert out == {"file_id": 42}
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_connect_error_retries_then_fails(client):
    route = respx.put("http://app.test/analysis/42").mock(
        side_effect=httpx.ConnectError("connection refused"),
    )

    with pytest.raises(AgentApiServerError):
        await client.put_analysis(42, {})

    assert route.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_post_tracklist_sends_request_id(client):
    route = respx.post("http://app.test/tracklists").mock(
        return_value=httpx.Response(200, json={"id": 1, "request_id": "R1"}),
    )

    await client.post_tracklist("R1", {"tracks": []})

    body = route.calls.last.request.content.decode()
    assert "R1" in body  # request_id in payload


@pytest.mark.asyncio
@respx.mock
async def test_patch_proposal_state_uses_correct_verb_and_path(client):
    route = respx.patch("http://app.test/proposals/7/state").mock(
        return_value=httpx.Response(200, json={"id": 7, "state": "EXECUTED"}),
    )

    out = await client.patch_proposal_state(7, "EXECUTED", reason=None)

    assert out["state"] == "EXECUTED"
    assert route.call_count == 1
```

Key points:

1. **`@respx.mock`** wraps the test in a context where all httpx calls go through respx. **Routes not matched cause `httpx.RemoteProtocolError`** by default — explicit and loud, which is what we want.
2. **`side_effect=[Response, Response, ...]`** sequences responses on successive calls — perfect for testing retry behavior.
3. **`side_effect=httpx.ConnectError(...)`** raises an exception instead of returning a response — tests the network-failure retry path.
4. **`route.call_count`** is the canonical assertion for "did it retry the right number of times." `== 1` for 4xx; `== 3` for 5xx exhaustion; `== 2` for 5xx-then-200.
5. **`route.calls.last.request.headers["Authorization"]`** is how you assert the Bearer token was injected. This is the auth-invariant test — run it for every method.
6. **Two-method retry coverage:** test retry behavior once per *exception class* (5xx, ConnectError, ReadTimeout), not once per endpoint. The retry logic is in `_request`, shared by all endpoint methods.
7. **`pytest-asyncio`** is required; configure `asyncio_mode = "auto"` in pyproject.toml or decorate each test with `@pytest.mark.asyncio`. The project likely already has this configured for existing async tests.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| arq task queue | SAQ (Static Asyncio Queue) | 2025 (per MEMORY) | Phase 26 is the first phase to deeply use SAQ. arq idioms (`@cron`, `Settings` class) don't translate; use SAQ's `settings` dict. |
| `retry_if_exception_type` for all httpx errors | `retry_if_exception(predicate)` for status-code-sensitive retries | tenacity 6+, current 8.5 | The predicate pattern is the only way to retry 5xx but not 4xx |
| Module-globals for shared clients | `ctx["client"]` in SAQ hooks | SAQ 0.20+ | Module globals don't get a clean shutdown; `ctx` does |
| Hand-rolled retry loops | `tenacity.AsyncRetrying` async iterator | tenacity 8+ | The async iterator pattern integrates cleanly with `try/except` and avoids the older `@retry` decorator's awkwardness around return value introspection |
| `respx.mock(assert_all_called=True)` decorator-level assertion | Explicit `assert route.called` per route | respx 0.21+ | Decorator-level assert was deprecated for finer-grained control |
| Pydantic BaseSettings v1 with `Config` inner class | pydantic-settings v2 with `model_config = SettingsConfigDict(...)` | pydantic v2 (2023) | Already adopted in project config.py; new subclasses follow the v2 idiom |

**Deprecated/outdated:**
- `tenacity.RetryError` as the catchable exception — use `reraise=True` and catch the original instead.
- `httpx.AsyncClient(timeout=httpx.Timeout(...))` is still fine but plain `timeout=30.0` works for unified timeouts.
- Pydantic v1 `@validator` — use `@field_validator` and `@model_validator` in v2.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | SAQ's `Queue.from_url(redis_url, name="...")` accepts a `name=` keyword to set the queue's logical name | Pattern 1 / Pattern 3 | If wrong, `AgentTaskRouter` and the agent worker's `settings.queue` need a different constructor. Mitigated by: read `saq.Queue.__init__` source in Wave 0; from training + GitHub README, the `name` param is standard. |
| A2 | The saq CLI invocation `saq phaze.tasks.controller.settings` works (settings is a dict at module level) | Pattern 1 | If wrong (e.g., saq requires `settings.py` filename or colon-style), docker-compose `command:` must change. Mitigated by: verify in Wave 0 with a smoke test. |
| A3 | tenacity 8.5.0 is current and stable | Standard Stack | If a major API change has landed in 9.x, snippets may need adjustment. Mitigated by: `uv pip show tenacity` after install. |
| A4 | respx 0.21.x is compatible with current httpx | Standard Stack | If httpx had a major release that breaks respx, contract tests fail. Mitigated by: check respx changelog before locking. |
| A5 | redis-py asyncio `set(key, value, nx=True, ex=ttl)` returns truthy on success and `None` on already-exists | Pattern 5 | If the return shape differs (some versions return bool, some return str/None), the `if not won:` branch is wrong. Mitigated by: explicit `won is None or won is False` check, or smoke test against the project's Redis. |
| A6 | `pg_insert(...).on_conflict_do_update(...)` exists for the project's SQLAlchemy 2.0+ version | Pattern 4 | If wrong, the upsert needs SELECT-then-UPSERT in a transaction. Mitigated by: pattern is used at `ingestion.py:91-119` in current codebase — confirmed available. |
| A7 | `Proposal.state` enum values include `APPROVED`, `EXECUTED`, `FAILED` and `FileRecord.state` includes `MOVED`, `UNCHANGED` | Pattern 6 | If states are named differently, the transition table needs renaming. Mitigated by: Wave 0 task — read the actual enum classes from `models/proposal.py` and `models/file.py` and reconcile. |
| A8 | mypy `[[tool.mypy.overrides]]` with `module = "phaze.services.agent_client"` correctly re-enables strict checking for files inside an `exclude`d directory | Pattern 8 | If exclude trumps overrides, the strict opt-in fails. Mitigated by: run `uv run mypy src/phaze/services/agent_client.py --strict` standalone first; the strict CLI flag bypasses config. If overrides don't work, fall back to removing `services/` from the global exclude. |
| A9 | The agent worker's startup hook runs *inside* the event loop, so `httpx.AsyncClient()` instantiation does not need explicit lifespan management beyond shutdown's `aclose()` | Pattern 1 (agent worker) | If SAQ's startup runs outside the loop, AsyncClient needs `async with` lifecycle. Mitigated by: SAQ's startup signature is `async def startup(ctx)` — the `async` confirms it's awaited inside the loop. |
| A10 | The existing `redis_client.py` uses `decode_responses=True` so Redis returns `str` not `bytes` | Pattern 5 | If `decode_responses=False`, the idempotency cache code needs `.decode()`. Mitigated by: read `phaze/redis_client.py` in Wave 0. |
| A11 | Phase 25's `get_authenticated_agent` dependency returns an object with `.agent_id` (string) and the agent token is bound to that ID | Pattern 3 | If the auth dep returns something different (e.g., a token row without `.agent_id`), `AgentTaskRouter.enqueue(agent.agent_id, ...)` doesn't compile. Mitigated by: read `agent_auth.py` in Wave 0 — already done in research; confirmed `.agent_id` attribute exists. |
| A12 | `pyproject.toml` already excludes `services/` from mypy and `[[tool.mypy.overrides]]` can target an excluded module to re-include it | Pattern 8 | If overrides don't override exclude, alternative is to move the new files to `src/phaze/clients/` (a new non-excluded dir). Mitigated by: verify with the mypy override smoke test in Wave 0. |

**Six of these (A1, A2, A3, A4, A5, A8) are verifiable in <5 minutes of Wave 0 work.** The planner should add an early Wave 0 task: "Verify standard-stack assumptions A1-A8."

## Open Questions (RESOLVED)

> All 7 questions resolved during revision iteration 2 (2026-05-12) per checker B3.

1. **Should the idempotency cache validate payload hash on cache hit?**
   - What we know: D-27 specifies `tracklist_req:{request_id}` + `tracklist_resp:{request_id}`. It does not specify payload-hash collision detection.
   - What's unclear: If a caller reuses `request_id` with a *different* payload, should the server return the cached response (silent) or 409 Conflict (detected)?
   - Recommendation: Default to 409 with payload-hash check. It's a defensive 5-line addition and prevents a class of silent data loss. If user prefers the simpler "silent return cached", the planner can omit the hash check based on user discretion area.
   - **RESOLVED:** NO — Phase 26 trusts the agent's `request_id`; defensive payload-hash check deferred. Noted in Plan 07 threat model row T-26-07-T (disposition: accept, single-operator trust model). Plan 03 typed `request_id: uuid.UUID` already prevents Redis-key-injection.

2. **What is the exact `Proposal` state enum name?**
   - What we know: D-28 references `APPROVED→EXECUTED|FAILED`.
   - What's unclear: Models file may name the enum `ProposalState` or `ProposalStatus` or use plain strings. Wave 0 must read `phaze/models/proposal.py` and reconcile.
   - Recommendation: First Wave 0 task — verify current model enum names and reuse them verbatim.
   - **RESOLVED:** `ProposalStatus` (existing in `src/phaze/models/proposal.py:20`). Plan 01 extends this enum with EXECUTED, FAILED. Schema (Plan 03) uses `Literal["executed", "failed"]` in the wire payload (string form) and Plan 08's router maps the strings to the enum members for DB persistence.

3. **Should `controller_concurrency` and `agent_concurrency` have different defaults?**
   - What we know: D-09 says each settings module declares its own concurrency.
   - What's unclear: Sensible default values.
   - Recommendation: `controller_concurrency=8` (mostly waiting on PG/LLM), `agent_concurrency=4` (mostly CPU-bound audio analysis on one host). The planner can pick differently; values are tunable in env.
   - **RESOLVED:** controller defaults to existing `worker_concurrency` (10); agent defaults to `agent_concurrency=4` (lower to match process-pool size for essentia). Both overridable via settings env vars. Documented in Plan 09 (controller) and Plan 10 (agent).

4. **Does the agent need its own `/whoami` startup probe?**
   - What we know: D-17 adds the endpoint server-side.
   - What's unclear: Whether the agent should *call* `/whoami` at startup to verify its bearer token + agent_id are correct (anti-misconfiguration probe per Pitfall 1).
   - Recommendation: Yes. Add a one-line check in agent_worker's startup hook: `result = await ctx["api_client"].whoami(); assert result["agent_id"] == cfg.agent_id`. Fail fast on mismatch.
   - **RESOLVED:** YES — agent_worker startup MUST call `client.whoami()` with bounded retry (1s → 2s → 4s → 8s → 16s → 32s, total ≤ 60s) per D-16. Implemented in Plan 10 Task 2 via `_whoami_with_retry` + queue-name mismatch guard.

5. **Should `AgentTaskRouter` use a single shared Queue or one Queue per agent_id?**
   - What we know: D-29 says per-agent queues.
   - What's unclear: SAQ's `Queue.from_url(...)` creates a Redis connection. With N agents, N persistent connections are open.
   - Recommendation: For 1-5 agents (the realistic project scale), per-agent Queue instances are fine. Cache lazily (Pattern 3) so unused queues don't open connections. Above ~50 agents, consider a connection pool wrapper — but that's deferred.
   - **RESOLVED:** Simple dict-keyed-by-agent_id with no eviction (single-operator, low cardinality). Implemented in Plan 04 (AgentTaskRouter).

6. **What's the dedup semantics if two agents have the same agent_id?**
   - What we know: D-30 keys queues on agent_id.
   - What's unclear: Is agent_id enforced unique anywhere?
   - Recommendation: Document that operators must assign unique agent_ids. The DB's `AgentCredential.agent_id` is presumably already unique-indexed (verify in Wave 0). Add the assertion to deploy docs.
   - **RESOLVED:** Enforced at the auth dep level (Phase 25's `Depends(get_authenticated_agent)` resolves the bearer to a unique row — `Agent.id` is the PK so uniqueness is enforced by Postgres). Cross-agent ownership check for proposals was the related concern and is now ADDRESSED in Plan 08 (W1 / T-26-08-S2).

7. **Does Phase 26 need to keep `phaze.tasks.worker` and `phaze.tasks.session` as deprecated shims, or delete outright?**
   - What we know: D-04 says delete.
   - What's unclear: If anything outside `phaze.tasks.*` imports from these modules.
   - Recommendation: Grep before deletion. If only Phase 26-rewritten files import these, safe to delete. (Already verified in research — only `phaze.tasks.*` files import them.)
   - **RESOLVED:** DELETE — no back-compat shim per D-04, D-08. Plan 13 deletes both `phaze.tasks.worker` and `phaze.tasks.session`; same commit updates docker-compose.yml to point at the new controller / agent_worker modules.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python 3.13 | All code | ✓ (assumed per CLAUDE.md) | 3.13 | — |
| uv | Package management | ✓ (assumed per CLAUDE.md) | latest | — |
| PostgreSQL | Controller, app server | ✓ (assumed available in docker-compose) | 16+ | — |
| Redis | Controller queue, agent queues, idempotency cache | ✓ (assumed available in docker-compose) | 7+ | — |
| Docker Compose | Deployment | ✓ (project uses it) | 2.x | — |
| tenacity (new) | `agent_client.py` retry | ✗ (must add) | >=8.5.0 | — (install required) |
| respx (new) | dev-only contract tests | ✗ (must add) | >=0.21.1 | — (install required) |

**Missing dependencies with no fallback:** Both `tenacity` and `respx` must be installed. They are pure-Python with no system deps, low risk.

**Missing dependencies with fallback:** None.

**Smoke test (Wave 0):**

```bash
uv add tenacity
uv add --dev respx
uv run python -c "import tenacity, respx; print(tenacity.__version__, respx.__version__)"
```

## Validation Architecture

> Phase 26 validation strategy — extract this into VALIDATION.md.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest (already in stack) + pytest-asyncio (already in stack) + respx (NEW, dev-only) |
| Config file | `pyproject.toml [tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/services/test_agent_client.py tests/routers/test_agent_analysis.py -x -q` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| DIST-03 | Agent worker module does not transitively import phaze.database | subprocess | `uv run pytest tests/tasks/test_agent_worker_import_boundary.py -x` | ❌ Wave 0 |
| DIST-03 | Agent worker module does not transitively import sqlalchemy.ext.asyncio | subprocess | (same as above; one test checks both) | ❌ Wave 0 |
| DIST-03 | Agent worker startup succeeds with `PHAZE_DATABASE_URL` unset | subprocess | `uv run pytest tests/tasks/test_agent_worker_no_db_required.py -x` | ❌ Wave 0 |
| TASK-01 | Controller settings module's `functions` list contains only fileless tasks | unit | `uv run pytest tests/tasks/test_controller_settings.py -x` | ❌ Wave 0 |
| TASK-01 | Agent settings module's `functions` list contains only file-bound tasks | unit | `uv run pytest tests/tasks/test_agent_settings.py -x` | ❌ Wave 0 |
| TASK-01 | All five rewritten task bodies call PhazeAgentClient methods (no direct DB) | unit (per task) | `uv run pytest tests/tasks/test_scan_walk.py tests/tasks/test_extract_metadata.py tests/tasks/test_fingerprint_file.py tests/tasks/test_execute_proposal.py tests/tasks/test_process_file.py -x` | ❌ Wave 0 |
| TASK-02 | AgentTaskRouter enqueues to `agent:<agent_id>` queue, not default | unit | `uv run pytest tests/services/test_agent_task_router.py -x` | ❌ Wave 0 |
| TASK-02 | Agent worker connects to `agent:<id>` queue per env var | integration | `uv run pytest tests/tasks/test_agent_worker_queue_name.py -x` | ❌ Wave 0 |
| TASK-03 | Same image runs controller via `PHAZE_ROLE=control` | manual / docker-compose smoke | `docker compose up controller agent && curl http://localhost:8000/healthz` | manual-only justified — full docker compose stack is integration, not unit |
| OPS-01 | Controller startup hook logs role banner | unit (capsys) | `uv run pytest tests/tasks/test_controller_startup_banner.py -x` | ❌ Wave 0 |
| OPS-01 | Agent startup hook logs role + agent_id banner | unit (capsys) | `uv run pytest tests/tasks/test_agent_startup_banner.py -x` | ❌ Wave 0 |
| OPS-01 | GET /whoami returns agent_id + role for authenticated token | router unit | `uv run pytest tests/routers/test_agent_whoami.py -x` | ❌ Wave 0 |
| D-26 | PUT /analysis/{file_id} creates row on first call | router integration (real PG) | `uv run pytest tests/routers/test_agent_analysis.py::test_first_call_creates -x` | ❌ Wave 0 |
| D-26 | PUT /analysis/{file_id} updates row on second call (same file_id) | router integration | `uv run pytest tests/routers/test_agent_analysis.py::test_second_call_updates -x` | ❌ Wave 0 |
| D-26 | PUT /analysis/{file_id} requires bearer auth | router unit | `uv run pytest tests/routers/test_agent_analysis.py::test_unauthenticated_401 -x` | ❌ Wave 0 |
| D-27 | POST /tracklists caches response in Redis | router integration (real Redis) | `uv run pytest tests/routers/test_agent_tracklists.py::test_cached_response -x` | ❌ Wave 0 |
| D-27 | POST /tracklists returns cached response on duplicate request_id | router integration | `uv run pytest tests/routers/test_agent_tracklists.py::test_idempotent -x` | ❌ Wave 0 |
| D-27 | POST /tracklists TTL is 1 hour | router integration (read TTL) | `uv run pytest tests/routers/test_agent_tracklists.py::test_ttl -x` | ❌ Wave 0 |
| D-28 | PATCH /proposals/{id}/state APPROVED→EXECUTED updates Proposal + FileRecord in one tx | router integration | `uv run pytest tests/routers/test_agent_proposals.py::test_executed_joint_update -x` | ❌ Wave 0 |
| D-28 | PATCH /proposals/{id}/state APPROVED→FAILED updates Proposal + FileRecord | router integration | `uv run pytest tests/routers/test_agent_proposals.py::test_failed_joint_update -x` | ❌ Wave 0 |
| D-28 | Illegal transition returns 409 Conflict | router unit | `uv run pytest tests/routers/test_agent_proposals.py::test_illegal_transition_409 -x` | ❌ Wave 0 |
| D-31 | respx contract tests for PhazeAgentClient cover happy path, 401, 500-retry, ConnectError-retry, auth-header injection | unit (respx) | `uv run pytest tests/services/test_agent_client_contract.py -x` | ❌ Wave 0 |
| D-32 | Tenacity retries 3 times on 5xx, 0 times on 4xx (via respx call_count) | unit (respx) | (covered by D-31 contract tests) | ❌ Wave 0 |
| D-33 | mypy strict opt-in for new files passes | static | `uv run mypy src/phaze/services/agent_client.py src/phaze/services/agent_task_router.py` | passes implicitly via pre-commit |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/services/test_agent_client.py tests/routers/test_agent_*.py tests/tasks/test_agent_worker_*.py -x -q` (focused, < 30s)
- **Per wave merge:** `uv run pytest -x` (full unit + integration suite)
- **Phase gate:** `uv run pytest --cov --cov-report=term-missing` — 85% coverage, full suite green, all pre-commit hooks pass.

### Wave 0 Gaps
- [ ] `tests/tasks/test_agent_worker_import_boundary.py` — covers DIST-03 (the structural invariant)
- [ ] `tests/tasks/test_agent_worker_no_db_required.py` — covers DIST-03 (runtime DB-unavailability)
- [ ] `tests/tasks/test_controller_settings.py` — covers TASK-01 (functions list correctness)
- [ ] `tests/tasks/test_agent_settings.py` — covers TASK-01
- [ ] `tests/services/test_agent_client_contract.py` — covers D-31 / D-32 (respx contract)
- [ ] `tests/services/test_agent_task_router.py` — covers TASK-02
- [ ] `tests/routers/test_agent_analysis.py` — covers D-26
- [ ] `tests/routers/test_agent_tracklists.py` — covers D-27 (requires real Redis fixture)
- [ ] `tests/routers/test_agent_proposals.py` — covers D-28 (requires real PG fixture)
- [ ] `tests/routers/test_agent_whoami.py` — covers OPS-01
- [ ] `tests/tasks/test_{scan_walk,extract_metadata,fingerprint_file,execute_proposal,process_file}.py` — one per rewritten task body, covers TASK-01
- [ ] `tests/conftest.py` — likely needs new fixtures: `redis_client` (real or fakeredis), `respx_client_factory`, `mocked_agent` (for FastAPI Depends(get_authenticated_agent) override)
- [ ] Framework adds: `uv add tenacity` (runtime) + `uv add --dev respx` (dev)

### Validation Requirements (Dimension 8)

The five highest-leverage things that, if broken, silently pass build/lint/type but break runtime:

1. **Agent worker transitively imports `phaze.database`.** Subprocess import-boundary test catches this. (DIST-03)
2. **AgentTaskRouter enqueues to wrong queue name** (e.g., default queue instead of `agent:<id>`). Integration test asserts the exact Redis key the queue uses. (TASK-02)
3. **Tenacity retries 4xx.** Respx contract test asserts `call_count == 1` on 401 / 404 / 422 responses. (D-32)
4. **Idempotency cache returns stale response for different payload.** Integration test stores `R1` with payload P1, then resends `R1` with payload P2 and asserts response is either cached-for-P1 (and we document) or 409. (D-27)
5. **Joint Proposal+FileRecord update is not atomic** — if the test inserts a Proposal and FileRecord, calls PATCH, then asserts FileRecord.state was also updated, partial commits are caught. (D-28)

A sixth, less-leverage but worth-covering:

6. **AsyncClient not closed on shutdown** — leaks file descriptors over time. Unit test: instantiate `PhazeAgentClient`, call methods, verify `aclose()` decrements (or zeros) the connection pool count. The httpx-side assertion is hard but instrumentation in shutdown hook log line ("closing api_client") is sufficient for OPS-01 evidence.

## Security Domain

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | yes | Bearer token (Phase 25 — reused verbatim via `get_authenticated_agent`); no Phase 26 changes |
| V3 Session Management | no | Stateless bearer; no sessions |
| V4 Access Control | yes | Every new endpoint uses `Depends(get_authenticated_agent)`. `PATCH /proposals/{id}/state` should additionally verify the proposal belongs to a file the agent is responsible for (i.e., reject cross-agent state mutations). Wave 0 should confirm whether D-28 implies this scoping. |
| V5 Input Validation | yes | All new endpoints use Pydantic schemas (already standard). `request_id` in POST /tracklists must be length-bounded (e.g., <=128 chars) to prevent unbounded Redis key growth. |
| V6 Cryptography | yes | Bearer token comparison via `secrets.compare_digest` in Phase 25 auth dep — verify no Phase 26 code introduces a non-constant-time comparison anywhere. |
| V7 Error Handling | yes | Avoid leaking PG error details in 500 responses. Existing FastAPI error handlers cover this; verify no new endpoint exposes `e.orig` or similar. |
| V11 Business Logic | yes | State-machine validation in D-28 is exactly a business-logic ASVS concern — the table-driven approach is the recommended control. |
| V14 Configuration | yes | New env vars (`PHAZE_AGENT_API_BEARER_TOKEN`) must be `SecretStr`. Confirmed in Pattern 7. Logs must not echo the token. |

### Known Threat Patterns for FastAPI + httpx + Redis stack

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Bearer token in logs | Information Disclosure | `SecretStr` + structured log filter; never `logger.info(token)` |
| Bearer token leaked via httpx retry on a redirect | Information Disclosure | `httpx.AsyncClient(follow_redirects=False)` is the default; do NOT enable follow_redirects for the agent client — a server redirect could exfiltrate the token to an attacker-controlled URL |
| Redis key injection via `request_id` | Tampering | Validate `request_id` as `^[A-Za-z0-9_-]{1,128}$` in the Pydantic schema; Redis treats key names as opaque bytes but downstream tooling (RedisInsight, etc.) may not |
| Agent A submits state change for Agent B's file | Elevation of Privilege | In `PATCH /proposals/{id}/state`, verify `proposal.file.agent_id == authenticated_agent.agent_id` (assuming the FileRecord has agent ownership; verify in Wave 0) |
| Idempotency cache memory exhaustion via unique `request_id`s | DoS | TTL on `tracklist_req:` and `tracklist_resp:` keys caps growth; rate-limit at the auth layer (Phase 25 may already do this) |
| Replay of a tracklist response after data has been deleted | Tampering / Repudiation | Acceptable for 1-hour TTL window; document |
| Tenacity retry against a 401 floods auth logs | DoS / log spam | The `_should_retry` predicate explicitly returns False for 4xx — Pattern 2 handles this |
| Joint-update partial commit leaves inconsistent state | Tampering | Single `await session.commit()` per request (Pattern 6); DB integrity enforced |

## Sources

### Primary (HIGH confidence)
- Project codebase — direct read of:
  - `src/phaze/services/discogs_matcher.py` (PhazeAgentClient pattern)
  - `src/phaze/services/ingestion.py:91-119` (pg_insert upsert pattern)
  - `src/phaze/routers/agent_{metadata,fingerprint,execution,files,auth}.py` (Phase 25 router patterns)
  - `src/phaze/tasks/worker.py`, `session.py`, `functions.py`, `metadata_extraction.py`, `fingerprint.py`, `scan.py`, `execution.py` (rewrite targets)
  - `src/phaze/models/{analysis,tracklist,proposal,file,agent_credential}.py` (model truth)
  - `src/phaze/config.py`, `main.py`, `docker-compose.yml`, `pyproject.toml` (modification targets)
  - `.planning/phases/26-task-code-reorg-http-backed-agent-worker/26-CONTEXT.md` (33 locked decisions)
  - `.planning/phases/25-internal-agent-http-api-bearer-auth/25-{CONTEXT,PATTERNS,VERIFICATION}.md` (Phase 25 establish-patterns)
  - `.planning/REQUIREMENTS.md` (project requirements)
  - `.planning/STATE.md` (project state)
  - `CLAUDE.md` (project constraints)

### Secondary (MEDIUM-HIGH confidence — cross-verified)
- [SAQ GitHub README](https://github.com/tobymao/saq) — settings dict shape, CLI invocation, startup/shutdown hooks
- [SAQ examples directory](https://github.com/tobymao/saq/tree/main/examples) — simple settings file structure
- [tenacity ReadTheDocs](https://tenacity.readthedocs.io/en/latest/) — `retry_if_exception`, `AsyncRetrying`, `wait_exponential_jitter`
- [tenacity API reference](https://tenacity.readthedocs.io/en/latest/api.html) — predicate functions
- [respx documentation](https://lundberg.github.io/respx/) — `@respx.mock`, `side_effect`, `route.call_count`, header assertions
- [pydantic-settings v2 docs](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) — `SettingsConfigDict`, `model_validator`, subclassing
- [redis-py asyncio reference](https://redis-py.readthedocs.io/en/stable/) — `set(..., nx=True, ex=ttl)`
- [mypy config_file docs](https://mypy.readthedocs.io/en/stable/config_file.html) — `[[tool.mypy.overrides]]` per-module strict opt-in
- [SQLAlchemy 2.0 PostgreSQL dialect — INSERT...ON CONFLICT](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#insert-on-conflict-upsert)

### Tertiary (LOW confidence — flagged for validation in Wave 0)
- Exact SAQ `Queue.from_url` signature — verify with `uv pip show saq` + read `saq/queue/__init__.py` or `saq/queue/base.py`
- Exact respx version compatibility with project's httpx — verify with `uv pip install respx httpx`

## Metadata

**Confidence breakdown:**
- SAQ role-split mechanics: HIGH — pattern is documented in SAQ README and matches existing project conventions. One assumption (A1, A2) trivially verifiable.
- httpx + tenacity 4xx/5xx split: HIGH — tenacity 8.x API is stable; the `retry_if_exception(callable)` pattern is canonical.
- Three new endpoints: HIGH — `pg_insert.on_conflict_do_update` pattern already proven in project (`ingestion.py:91-119`); Redis SET NX EX is a well-known idiom; state-machine table is trivial.
- Idempotency cache: MEDIUM — pattern is correct, but payload-hash collision detection is unspecified in D-27. Resolution required.
- Subprocess import-boundary test: HIGH — straightforward stdlib subprocess + sys.modules check.
- respx contract tests: HIGH — respx documentation and examples are clear.
- pydantic-settings role subclassing: HIGH — pattern is documented and current.
- mypy strict opt-in: MEDIUM — `[[tool.mypy.overrides]]` is documented, but the interaction with the existing `exclude` regex needs a 5-minute smoke test.

**Overall confidence:** HIGH. The phase is well-scoped, the 33 locked decisions resolve most design ambiguity, and every pattern in this RESEARCH.md is grounded in either current project code or current upstream documentation.

**Research date:** 2026-05-12
**Valid until:** 2026-06-12 (30 days) — stable stack, no fast-moving deps. tenacity and respx are mature; SAQ is on a slow release cadence. The main risk to staleness is the locked decisions themselves changing in CONTEXT.md.

## RESEARCH COMPLETE
