# Phase 26: Task Code Reorg & HTTP-Backed Agent Worker - Pattern Map

**Mapped:** 2026-05-12
**Files analyzed:** 38 (20 CREATE + 13 MODIFY + 2 DELETE + 3 config touchpoints)
**Analogs found:** 38 / 38

## File Classification

### CREATE

| New File | Role | Data Flow | Closest Analog | Match Quality |
|----------|------|-----------|----------------|---------------|
| `src/phaze/tasks/controller.py` | settings module (SAQ entry point) | event-driven (worker boot) | `src/phaze/tasks/worker.py:28-115` | exact (self-pattern subset) |
| `src/phaze/tasks/agent_worker.py` | settings module (SAQ entry point) | event-driven (worker boot) | `src/phaze/tasks/worker.py:28-115` (subset, no DB) | role-match |
| `src/phaze/services/agent_client.py` | service (httpx wrapper) | request-response | `src/phaze/services/discogs_matcher.py:19-51` | exact |
| `src/phaze/services/agent_task_router.py` | service (queue enqueuer) | event-driven (Redis enqueue) | `src/phaze/routers/agent_files.py:99-117` (inline pattern) + RESEARCH §Pattern 3 | role-match (no existing per-agent router service) |
| `src/phaze/schemas/agent_tasks.py` | schema (SAQ job payloads) | validation | `src/phaze/schemas/agent_files.py:25-43` (extra="forbid" pattern) | role-match |
| `src/phaze/routers/agent_identity.py` | router | request-response (GET) | `src/phaze/routers/agent_heartbeat.py:1-40` (simple single-handler) | role-match |
| `src/phaze/routers/agent_analysis.py` | router | CRUD (idempotent PUT upsert) | `src/phaze/routers/agent_metadata.py:1-72` | exact |
| `src/phaze/routers/agent_tracklists.py` | router | CRUD (idempotent POST) | `src/phaze/routers/agent_execution.py:60-80` (create + Redis idempotency) + RESEARCH §Pattern 5 | role-match (no existing Redis-cache idempotent endpoint) |
| `src/phaze/routers/agent_proposals.py` | router | CRUD (state-machine PATCH) | `src/phaze/routers/agent_execution.py:83-133` | exact |
| `src/phaze/schemas/agent_identity.py` | schema | validation | `src/phaze/schemas/agent_heartbeat.py:1-17` | exact |
| `src/phaze/schemas/agent_analysis.py` | schema | validation | `src/phaze/schemas/agent_metadata.py:1-33` | exact |
| `src/phaze/schemas/agent_tracklists.py` | schema | validation | `src/phaze/schemas/agent_files.py:25-53` (nested chunk) | role-match |
| `src/phaze/schemas/agent_proposals.py` | schema | validation | `src/phaze/schemas/agent_execution.py:41-71` (PATCH body) | role-match |
| `tests/test_routers/test_agent_identity.py` | test | integration | `tests/test_routers/test_agent_heartbeat.py:1-80` (smoke-app pattern) | exact |
| `tests/test_routers/test_agent_analysis.py` | test | integration | `tests/test_routers/test_agent_metadata.py:1-224` | exact |
| `tests/test_routers/test_agent_tracklists.py` | test | integration | `tests/test_routers/test_agent_metadata.py` (smoke-app shape) + `test_agent_execution.py:33-65` (FK seeding) | role-match |
| `tests/test_routers/test_agent_proposals.py` | test | integration | `tests/test_routers/test_agent_execution.py:33-180` (POST + PATCH + 409) | exact |
| `tests/test_services/test_agent_client.py` | test | unit (respx mock) | `tests/test_services/test_discogs_matcher.py:14-80` (AsyncMock httpx) + RESEARCH §Pattern 10 (respx) | role-match (no existing respx prior art) |
| `tests/test_services/test_agent_task_router.py` | test | integration (Redis) | `tests/test_routers/test_agent_files.py:79-100` (Queue mock) | partial — pure service test |
| `tests/test_task_split.py` | test | structural (subprocess) | RESEARCH §Pattern 9 (no analog) | no analog |

### MODIFY

| Modified File | Role | Data Flow | Closest Analog | Match Quality |
|---------------|------|-----------|----------------|---------------|
| `src/phaze/tasks/functions.py` | task body (rewrite) | request-response (HTTP call) | RESEARCH §Pattern 2 calling pattern + existing `tasks/functions.py:20-57` | partial (rewrite, not extend) |
| `src/phaze/tasks/metadata_extraction.py` | task body (rewrite) | request-response | existing `tasks/metadata_extraction.py:23-68` + `tasks/functions.py:20-57` (HTTP-rewrite shape) | partial (rewrite) |
| `src/phaze/tasks/fingerprint.py` | task body (rewrite) | request-response | existing `tasks/fingerprint.py:22-63` + multi-engine HTTP wrap | partial (rewrite) |
| `src/phaze/tasks/scan.py` | task body (rewrite) | request-response (multi-row POST) | existing `tasks/scan.py:23-114` + RESEARCH §Pattern 5 (idempotent POST) | partial (rewrite) |
| `src/phaze/tasks/execution.py` | task body (rewrite) | request-response | existing `tasks/execution.py:17-92` + RESEARCH §Pattern 6 calling pattern | partial (rewrite) |
| `src/phaze/config.py` | config (Base + role subclasses) | env loading | existing `src/phaze/config.py:7-63` (single class) | self-pattern subset |
| `src/phaze/main.py` | app-wiring | startup | existing `src/phaze/main.py:14-70` (router registration + lifespan) | exact (self-pattern) |
| `src/phaze/routers/agent_files.py` | router-patch (refactor lines 99-117) | event-driven | RESEARCH §Pattern 3 (router via `request.app.state.task_router`) | role-match |
| `src/phaze/models/proposal.py` | model-patch (extend enum) | DDL/ORM | existing `models/proposal.py:20-25` (ProposalStatus enum) + `models/execution.py:14-21` (extension shape) | exact |
| `src/phaze/models/file.py` | model-patch (extend enum) | DDL/ORM | existing `models/file.py:20-32` (FileState enum already has EXECUTED/FAILED; ADD MOVED/UNCHANGED) | exact |
| `docker-compose.yml` | deployment config | startup | existing `docker-compose.yml:19-39` (worker service block) | exact (self-pattern) |
| `pyproject.toml` | build config | deps + mypy | existing `pyproject.toml:11-29` (deps) + `pyproject.toml:84-90` (overrides) | exact (self-pattern) |

### DELETE

| Deleted File | Reason |
|--------------|--------|
| `src/phaze/tasks/worker.py` | Replaced by controller.py + agent_worker.py (D-04, D-08) |
| `src/phaze/tasks/session.py` | Already a deprecated stub (5 lines); no callers (D-06, D-08) |

## Pattern Assignments

### `src/phaze/tasks/controller.py` (SAQ settings, fileless role)

**Analog:** `src/phaze/tasks/worker.py:28-115` — subset (drop file-bound tasks + drop models check + drop process pool unless fileless task needs).

**Imports pattern** (compose from `tasks/worker.py:1-22`, drop file-bound task imports):
```python
"""SAQ controller settings -- entry point for ``saq phaze.tasks.controller.settings`` (fileless role)."""

import logging
from typing import Any

from saq import CronJob, Queue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.config import get_settings
from phaze.services.discogs_matcher import DiscogsographyClient
from phaze.services.proposal import ProposalService, load_prompt_template
from phaze.tasks.discogs import match_tracklist_to_discogs
from phaze.tasks.proposal import generate_proposals
from phaze.tasks.tracklist import refresh_tracklists, scrape_and_store_tracklist, search_tracklist
```

**Startup hook** (verbatim mirror of `tasks/worker.py:28-67`, keep only fileless wiring):
```python
async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    logger.info("phaze.controller startup role=%s redis=%s", settings.role, settings.redis_url)

    # Shared async engine pool (INFRA-01) -- fileless tasks need DB
    task_engine = create_async_engine(str(settings.database_url), echo=settings.debug, pool_size=10, max_overflow=5)
    ctx["async_session"] = async_sessionmaker(task_engine, class_=AsyncSession, expire_on_commit=False)
    ctx["task_engine"] = task_engine

    # Phase 19: Discogsography client
    ctx["discogs_client"] = DiscogsographyClient(base_url=settings.discogsography_url)

    # Phase 6: LLM proposal service
    prompt_template = load_prompt_template()
    ctx["proposal_service"] = ProposalService(model=settings.llm_model, prompt_template=prompt_template, max_rpm=settings.llm_max_rpm)
```

**Settings dict** (mirror `tasks/worker.py:91-115`, drop file-bound functions):
```python
queue = Queue.from_url(get_settings().redis_url, name="controller")

settings = {
    "queue": queue,
    "functions": [generate_proposals, match_tracklist_to_discogs, search_tracklist, scrape_and_store_tracklist],
    "concurrency": get_settings().controller_concurrency,
    "cron_jobs": [CronJob(refresh_tracklists, cron="0 3 1 * *")],
    "startup": startup,
    "shutdown": shutdown,
}
```

**What differs from analog:**
- Drops file-bound task imports (`process_file`, `extract_file_metadata`, `fingerprint_file`, `scan_live_set`, `execute_approved_batch`).
- Drops `create_process_pool()` + `FingerprintOrchestrator` + `models_path` check (file-bound only).
- Replaces module-level `from phaze.config import settings as app_settings` with `get_settings()` calls (D-14).
- Queue gets a `name="controller"` (was unnamed default).

**Gotchas:** Per RESEARCH §A2, the `saq <module>.settings` CLI imports the named attribute — keep `settings` as a module-level `dict`, not a callable.

---

### `src/phaze/tasks/agent_worker.py` (SAQ settings, file-bound role)

**Analog:** `src/phaze/tasks/worker.py:28-115` — file-bound subset + RESEARCH §Pattern 1 (agent worker example, lines 280-326).

**Imports pattern** (D-25 forbids `phaze.database`, `phaze.tasks.session`, `sqlalchemy.ext.asyncio`):
```python
"""SAQ agent_worker settings -- entry point for ``saq phaze.tasks.agent_worker.settings`` (file-bound role).

CRITICAL: this module MUST NOT import phaze.database or sqlalchemy.ext.asyncio.
Enforced by tests/test_task_split.py.
"""

import logging
import os
from pathlib import Path
from typing import Any

from saq import Queue

from phaze.config import get_settings
from phaze.services.agent_client import PhazeAgentClient
from phaze.tasks.execution import execute_approved_batch
from phaze.tasks.fingerprint import fingerprint_file
from phaze.tasks.functions import process_file
from phaze.tasks.metadata_extraction import extract_file_metadata
from phaze.tasks.pool import create_process_pool
from phaze.tasks.scan import scan_live_set
```

**Startup hook with /whoami probe** (CONTEXT.md D-16, RESEARCH §Pattern 1 + Pitfall 1):
```python
async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    logger.info("phaze.agent_worker startup api=%s token_preview=%s...",
                settings.agent_api_url, settings.agent_token.get_secret_value()[:12])

    # Models check (verbatim from tasks/worker.py:30-39)
    models_dir = Path(settings.models_path)
    if not models_dir.is_dir():
        raise RuntimeError(f"Models directory not found: {settings.models_path}")
    pb_files = list(models_dir.glob("*.pb"))
    if not pb_files:
        raise RuntimeError(f"No .pb model files in {settings.models_path}")

    # PhazeAgentClient -- single httpx instance reused across all jobs
    ctx["api_client"] = PhazeAgentClient(
        base_url=settings.agent_api_url,
        token=settings.agent_token.get_secret_value(),
        timeout=30.0,
    )

    # /whoami startup probe -- D-16, Pitfall 1
    identity = await ctx["api_client"].whoami()  # raises after bounded retry budget
    expected_queue = f"phaze-agent-{identity.agent_id}"
    actual_queue = os.environ["PHAZE_AGENT_QUEUE"]
    if expected_queue != actual_queue:
        raise RuntimeError(f"queue/token mismatch: token resolves to {identity.agent_id} but PHAZE_AGENT_QUEUE={actual_queue}")
    ctx["agent_identity"] = identity
    ctx["agent_queue_name"] = expected_queue

    # CPU-bound essentia pool (verbatim from tasks/worker.py:41)
    ctx["process_pool"] = create_process_pool()


async def shutdown(ctx: dict[str, Any]) -> None:
    pool = ctx.get("process_pool")
    if pool is not None:
        pool.shutdown(wait=True)
    client = ctx.get("api_client")
    if client is not None:
        await client.close()
```

**Settings dict** (queue name from env per D-16 step 5):
```python
queue = Queue.from_url(get_settings().redis_url, name=os.environ["PHAZE_AGENT_QUEUE"])

settings = {
    "queue": queue,
    "functions": [process_file, extract_file_metadata, fingerprint_file, scan_live_set, execute_approved_batch],
    "concurrency": get_settings().worker_max_jobs,
    "timeout": get_settings().worker_job_timeout,
    "retries": get_settings().worker_max_retries,
    "keep_result": get_settings().worker_keep_result,
    "startup": startup,
    "shutdown": shutdown,
}
```

**What differs from analog:**
- NO `from phaze.database import ...`, NO `from sqlalchemy.ext.asyncio import ...`, NO `from phaze.services.fingerprint import FingerprintOrchestrator` (the orchestrator imports SQLAlchemy via `from phaze.models.file import FileRecord` — verify in planner Wave 0; if it pulls SQLAlchemy, replace with adapter classes only).
- Queue name resolved from `PHAZE_AGENT_QUEUE` env at import time + /whoami assertion at startup.
- Drops `ctx["async_session"]`, `ctx["discogs_client"]`, `ctx["proposal_service"]`, `ctx["fingerprint_orchestrator"]` (those are fileless / controller).

**Gotchas:** RESEARCH Pitfall 7 — module-level `Queue.from_url(...)` runs on every import; tests must NOT import this module (use subprocess via `tests/test_task_split.py`).

---

### `src/phaze/services/agent_client.py` (service, request-response with retry)

**Analog:** `src/phaze/services/discogs_matcher.py:19-51` (single-class httpx.AsyncClient wrapper with `__init__(base_url, timeout)` and `async close()`).

**Constructor pattern** (verbatim shape from `services/discogs_matcher.py:19-28`):
```python
class PhazeAgentClient:
    """HTTP client adapter for the internal agent API on the application server.

    Follows the DiscogsographyClient pattern: create with base_url + token,
    call async methods, close when done. The bearer token is set as a default
    header on the underlying httpx.AsyncClient so every request inherits it.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        _client: httpx.AsyncClient | None = None,  # for respx injection per CONTEXT.md specifics
    ) -> None:
        self.base_url = base_url
        self._client = _client or httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    async def close(self) -> None:
        await self._client.aclose()
```

**Error type hierarchy** (verbatim from RESEARCH §Pattern 2 lines 378-391):
```python
class AgentApiError(Exception):
    """Base for all PhazeAgentClient errors."""

class AgentApiAuthError(AgentApiError):
    """401 / 403 from the server. NEVER retried."""

class AgentApiClientError(AgentApiError):
    """Any 4xx that is not auth. NEVER retried."""

class AgentApiServerError(AgentApiError):
    """5xx after retries exhausted, or persistent ConnectError/ReadTimeout."""
```

**Retry predicate + request funnel** (verbatim from RESEARCH §Pattern 2 lines 394-444):
```python
def _should_retry(exc: BaseException) -> bool:
    """retry only on transient errors and 5xx. NEVER on 4xx."""
    if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return False


async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
    """All HTTP calls funnel through here so retry policy is applied uniformly."""
    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential_jitter(initial=0.5, max=4.0),
            retry=retry_if_exception(_should_retry),
            reraise=True,
        ):
            with attempt:
                response = await self._client.request(method, path, **kwargs)
                response.raise_for_status()
                return response
    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code
        if status_code in (401, 403):
            raise AgentApiAuthError(f"{method} {path} -> {status_code}") from e
        if 400 <= status_code < 500:
            raise AgentApiClientError(f"{method} {path} -> {status_code}: {e.response.text}") from e
        raise AgentApiServerError(f"{method} {path} -> {status_code} after retries") from e
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as e:
        raise AgentApiServerError(f"{method} {path} network failure after retries") from e
```

**Method-per-endpoint** (CONTEXT.md D-10):
```python
async def whoami(self) -> AgentIdentity:
    r = await self._request("GET", "/api/internal/agent/whoami")
    return AgentIdentity.model_validate(r.json())

async def put_analysis(self, file_id: uuid.UUID, payload: AnalysisWritePayload) -> None:
    await self._request("PUT", f"/api/internal/agent/analysis/{file_id}", json=payload.model_dump(mode="json"))

async def create_tracklist(self, payload: TracklistCreatePayload) -> TracklistCreateResponse:
    r = await self._request("POST", "/api/internal/agent/tracklists", json=payload.model_dump(mode="json"))
    return TracklistCreateResponse.model_validate(r.json())

# ... 7 more methods, one per endpoint per D-10
```

**What differs from analog:**
- `DiscogsographyClient` returns lists/dicts; this returns Pydantic models from `phaze.schemas.agent_*`.
- Adds `token` kwarg + Authorization header injection (DiscogsographyClient has no auth).
- Adds tenacity retry funnel through `_request`; DiscogsographyClient just catches `httpx.ConnectError/TimeoutException` and returns empty list.
- Adds 4-level exception hierarchy; DiscogsographyClient has none.

**Gotchas:**
- RESEARCH §Pitfall 5 (FD leak): pair `httpx.AsyncClient` construction with `close()` in agent_worker shutdown.
- D-13: NEVER log token; preview is first 12 chars + `...` only.
- D-10 method `close()` (not `aclose()`) — matches existing `DiscogsographyClient.close()` convention.

---

### `src/phaze/services/agent_task_router.py` (service, controller-side enqueuer)

**Analog:** `src/phaze/routers/agent_files.py:99-117` (inline per-agent Queue + try/finally pattern) + RESEARCH §Pattern 3 (extracted class form).

**Class shape** (verbatim from RESEARCH §Pattern 3 lines 490-514):
```python
"""Controller-side per-agent enqueuer. Routes file-bound SAQ jobs to ``phaze-agent-<agent_id>`` queues."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from saq import Queue

if TYPE_CHECKING:
    from pydantic import BaseModel

    from phaze.models.file import FileRecord

logger = logging.getLogger(__name__)


class AgentTaskRouter:
    """Enqueues SAQ jobs onto the queue belonging to a specific agent.

    Lazily constructs per-agent Queue instances and caches them. All queues
    share one Redis URL but have distinct names ``phaze-agent-<agent_id>``.
    """

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._queues: dict[str, Queue] = {}

    def _queue_for(self, agent_id: str) -> Queue:
        if agent_id not in self._queues:
            self._queues[agent_id] = Queue.from_url(self._redis_url, name=f"phaze-agent-{agent_id}")
        return self._queues[agent_id]

    async def enqueue_for_agent(self, *, agent_id: str, task_name: str, payload: BaseModel) -> Any:
        queue = self._queue_for(agent_id)
        logger.debug("enqueue agent=%s task=%s", agent_id, task_name)
        return await queue.enqueue(task_name, **payload.model_dump(mode="json"))

    async def enqueue_for_file(self, *, file_record: FileRecord, task_name: str, payload: BaseModel) -> Any:
        return await self.enqueue_for_agent(agent_id=file_record.agent_id, task_name=task_name, payload=payload)

    async def close(self) -> None:
        for queue in self._queues.values():
            await queue.disconnect()
        self._queues.clear()
```

**What differs from analog:**
- Extracts the per-agent Queue lifecycle from `agent_files.py:99-117` (was inline, ad-hoc; now a reusable service).
- Adds a cache dict (vs. agent_files.py constructing+disconnecting Queue every call).
- Two enqueue methods: by FileRecord (derives agent_id) and by agent_id directly — covers Phase 27's user-initiated scan call site (D-21).

**Gotchas:**
- RESEARCH Pitfall 6 (FD leak): cached Queue instances must be `.disconnect()`'d on shutdown.
- Discretion area: cache is dict (simplest); planner may swap to `functools.cache` or LRU but must guarantee no Redis connection leak across rebuilds.

---

### `src/phaze/schemas/agent_tasks.py` (schema, SAQ job payloads)

**Analog:** `src/phaze/schemas/agent_files.py:25-43` (BaseModel + `ConfigDict(extra="forbid")` + nested item schema pattern).

**Pattern** (compose `schemas/agent_files.py:25-43` + Phase 25 D-16):
```python
"""Typed SAQ-job payload models for file-bound tasks (CONTEXT.md D-22..D-24).

Every payload carries the MINIMUM data the agent needs to execute without
reading state back from the controller. Models_path appears only in
ProcessFilePayload (essentia needs the .pb files); fingerprint/metadata/scan
tasks don't need it because their adapters point at local sidecars.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict


class ProcessFilePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: uuid.UUID
    original_path: str
    file_type: str
    agent_id: str
    models_path: str


class ExtractMetadataPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: uuid.UUID
    original_path: str
    file_type: str
    agent_id: str


class FingerprintFilePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: uuid.UUID
    original_path: str
    agent_id: str


class ScanLiveSetPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: uuid.UUID
    original_path: str
    agent_id: str


class ExecuteApprovedBatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch_id: uuid.UUID
    agent_id: str
    proposal_ids: list[uuid.UUID]
```

**What differs from analog:**
- Five sibling models (not one chunk+items).
- All payloads minimal — D-24 forbids `current_path` (agents work off `original_path`).
- No `agent_id` exclusion this time (these are job payloads, not HTTP bodies — `agent_id` IS needed on the wire so the SAQ job knows which agent picked it up).

---

### `src/phaze/routers/agent_identity.py` (router, single GET)

**Analog:** `src/phaze/routers/agent_heartbeat.py:1-40` — simplest single-handler shape.

**Full handler** (mirror `routers/agent_heartbeat.py:16-39`):
```python
"""GET /api/internal/agent/whoami -- agent identity probe (CONTEXT.md D-15..D-17)."""

from typing import Annotated

from fastapi import APIRouter, Depends, status

from phaze.models.agent import Agent
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_identity import AgentIdentity


router = APIRouter(prefix="/api/internal/agent/whoami", tags=["agent-internal"])


@router.get("", status_code=status.HTTP_200_OK, response_model=AgentIdentity)
async def whoami(
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
) -> AgentIdentity:
    """Return the calling agent's identity. Used by agent_worker startup probe (D-16)."""
    return AgentIdentity(
        agent_id=agent.id,
        name=agent.name,
        scan_roots=agent.scan_roots,
        created_at=agent.created_at,
    )
```

**What differs from analog:**
- GET instead of POST (read-only).
- No body — just returns the auth-dep result projected into an Identity model.
- No session dep needed (Agent already loaded by auth dep).

**Gotchas:** Verify `Agent.created_at` exists (TimestampMixin provides it per `models/base.py`).

---

### `src/phaze/routers/agent_analysis.py` (router, idempotent PUT upsert)

**Analog:** `src/phaze/routers/agent_metadata.py:1-72` — exact pattern match (idempotent PUT on single-column natural key, `pg_insert.on_conflict_do_update`).

**Imports + router** (verbatim from `routers/agent_metadata.py:1-17`, swap model):
```python
"""PUT /api/internal/agent/analysis/{file_id} -- idempotent audio-analysis upsert (CONTEXT.md D-26)."""

from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_analysis import AnalysisWritePayload, AnalysisWriteResponse


router = APIRouter(prefix="/api/internal/agent/analysis", tags=["agent-internal"])
```

**UPSERT handler** (verbatim shape from `routers/agent_metadata.py:20-71`, swap model + index_elements):
```python
@router.put("/{file_id}", status_code=status.HTTP_200_OK, response_model=AnalysisWriteResponse)
async def put_analysis(
    file_id: uuid.UUID,
    body: AnalysisWritePayload,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AnalysisWriteResponse:
    """Idempotent upsert on AnalysisResult.file_id (UQ from models/analysis.py:18).

    Field-level last-write-wins via Pydantic exclude_unset semantics (mirrors
    agent_metadata.py:52 -- CR-01 gap closure).
    """
    dumped = body.model_dump(exclude_unset=True)
    payload = {**dumped, "file_id": file_id, "id": uuid.uuid4()}
    stmt = pg_insert(AnalysisResult).values([payload])
    if dumped:
        stmt = stmt.on_conflict_do_update(
            index_elements=["file_id"],
            set_={k: stmt.excluded[k] for k in dumped},
        )
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=["file_id"])
    await session.execute(stmt)
    await session.commit()
    return AnalysisWriteResponse(agent_id=agent.id, file_id=file_id)
```

**What differs from analog:**
- Swaps `FileMetadata` → `AnalysisResult` and `FileMetadata.file_id` (UQ from `models/metadata.py:18`) → `AnalysisResult.file_id` (UQ from `models/analysis.py:18` — already exists).
- `AnalysisResult.id` has Python-only `default=uuid.uuid4` (same pattern as FileMetadata) → must stamp explicitly per `agent_metadata.py:55` PK NOTE.

**Gotchas:** CR-01 (Phase 25) regression — use `exclude_unset=True` so partial PUT preserves unset fields. Apply the empty-body `ON CONFLICT DO NOTHING` fallback (`agent_metadata.py:65-68`).

---

### `src/phaze/routers/agent_tracklists.py` (router, idempotent POST with Redis cache)

**Analogs:**
- `src/phaze/routers/agent_execution.py:60-80` (POST + idempotent insert, but uses `ON CONFLICT DO NOTHING` instead of Redis cache).
- `src/phaze/services/ingestion.py:91-119` (`pg_insert.on_conflict_do_update` for nested-row inserts).
- RESEARCH §Pattern 5 lines 614-677 (Redis idempotency via `SET NX EX`).

**Handler shape** (compose from RESEARCH §Pattern 5 + agent_execution.py auth dep):
```python
"""POST /api/internal/agent/tracklists -- idempotent atomic Tracklist+Version+Tracks create (CONTEXT.md D-27)."""

import hashlib
import json
from typing import Annotated

import redis.asyncio as redis_async
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_tracklists import TracklistCreatePayload, TracklistCreateResponse


router = APIRouter(prefix="/api/internal/agent/tracklists", tags=["agent-internal"])

_REQ_PREFIX = "tracklist_req:"
_RESP_PREFIX = "tracklist_resp:"
_TTL_SECONDS = 3600


async def _get_redis(request: Request) -> redis_async.Redis:
    """Pull the Redis instance from app.state (wired by lifespan in main.py)."""
    return request.app.state.redis  # MUST exist; planner Wave 0 verifies


@router.post("", status_code=status.HTTP_200_OK, response_model=TracklistCreateResponse)
async def create_tracklist(
    body: TracklistCreatePayload,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis_client: Annotated[redis_async.Redis, Depends(_get_redis)],
) -> TracklistCreateResponse:
    """Idempotent create keyed by body.request_id. RESEARCH Pattern 5."""
    resp_key = f"{_RESP_PREFIX}{body.request_id}"
    cached = await redis_client.get(resp_key)
    if cached is not None:
        return TracklistCreateResponse.model_validate_json(cached)

    req_key = f"{_REQ_PREFIX}{body.request_id}"
    won = await redis_client.set(req_key, "1", nx=True, ex=_TTL_SECONDS)
    if not won:
        for _ in range(10):  # bounded wait for concurrent writer to populate resp_key
            cached = await redis_client.get(resp_key)
            if cached is not None:
                return TracklistCreateResponse.model_validate_json(cached)
        raise HTTPException(status.HTTP_409_CONFLICT, "duplicate in-flight request")

    # Owner of this request_id -- do DB work in one transaction
    # 1. Upsert Tracklist on external_id (UQ from models/tracklist.py:47)
    # 2. SELECT max(version_number) WHERE tracklist_id=
    # 3. INSERT TracklistVersion + N TracklistTrack rows
    # 4. UPDATE Tracklist.latest_version_id
    # 5. Cache response under resp_key
    # [body of work mirrors tasks/scan.py:60-107 transactional pattern]
    response = TracklistCreateResponse(tracklist_id=..., version=..., track_count=len(body.tracks))
    await redis_client.set(resp_key, response.model_dump_json(), ex=_TTL_SECONDS)
    return response
```

**What differs from analogs:**
- `agent_execution.py:60-80` uses `ON CONFLICT (id) DO NOTHING` on a single row keyed by agent-supplied id. This router uses Redis idempotency because it writes 1+N rows atomically — no natural single-row conflict target spans all writes.
- `tasks/scan.py:60-107` does the same multi-row insert but inside a task with `ctx["async_session"]`. This router does it inside the request transaction.
- New dep: `_get_redis` from `request.app.state.redis` — planner Wave 0 must wire `app.state.redis = redis_async.Redis.from_url(...)` in `main.py` lifespan (currently `main.py:42` only wires `app.state.queue`).

**Gotchas:**
- RESEARCH Pitfall 4: if same `request_id` arrives with different payload, current pattern returns cached silently. Recommend payload-hash check (per RESEARCH §Pitfall 4 lines 939-947). Planner discretion per CONTEXT.md.
- RESEARCH §A10: verify `phaze.redis_client` uses `decode_responses=True` (returns `str`); else `.decode()` everywhere.

---

### `src/phaze/routers/agent_proposals.py` (router, state-machine PATCH)

**Analog:** `src/phaze/routers/agent_execution.py:83-133` — exact state-machine pattern (PATCH with 409 on illegal transitions, monotonic guard, terminal-state guard).

**Transition tables** (verbatim shape from `agent_execution.py:47-57`, replace lifecycle with D-28 transitions):
```python
"""PATCH /api/internal/agent/proposals/{id}/state -- joint Proposal+FileRecord state transition (CONTEXT.md D-28)."""

from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.file import FileRecord, FileState
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_proposals import ProposalStatePatch, ProposalStateResponse


router = APIRouter(prefix="/api/internal/agent/proposals", tags=["agent-internal"])


# D-28 allowed transitions. Same shape as agent_execution.py:47-57 _STATUS_ORDER/_TERMINAL.
_PROPOSAL_TRANSITIONS: dict[ProposalStatus, frozenset[ProposalStatus]] = {
    ProposalStatus.APPROVED: frozenset({ProposalStatus.EXECUTED, ProposalStatus.FAILED}),
}
_FILE_FOLLOW: dict[ProposalStatus, FileState] = {
    ProposalStatus.EXECUTED: FileState.MOVED,
    ProposalStatus.FAILED: FileState.UNCHANGED,
}
```

**PATCH handler** (verbatim shape from `agent_execution.py:83-133`):
```python
@router.patch("/{proposal_id}/state", status_code=status.HTTP_200_OK, response_model=ProposalStateResponse)
async def patch_proposal_state(
    proposal_id: uuid.UUID,
    body: ProposalStatePatch,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProposalStateResponse:
    """Joint Proposal+FileRecord state transition in one transaction (D-28).

    - 404 if proposal_id does not exist
    - 200 idempotent no-op if cur == new (e.g., re-PATCH EXECUTED -> EXECUTED)
    - 409 if transition not in _PROPOSAL_TRANSITIONS table
    """
    proposal = await session.get(RenameProposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="proposal not found")

    cur = ProposalStatus(proposal.status)
    new = ProposalStatus(body.proposal_state)

    if cur == new:  # idempotent retry
        return ProposalStateResponse(proposal_id=proposal_id, proposal_state=cur, file_state=None, current_path=None)

    allowed = _PROPOSAL_TRANSITIONS.get(cur, frozenset())
    if new not in allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"illegal transition {cur.value} -> {new.value}",
        )

    # Joint update -- one commit (RESEARCH Pitfall 6)
    proposal.status = new
    if body.error_message is not None:
        proposal.reason = body.error_message  # column name from models/proposal.py:40

    file_state = None
    if body.file_state is not None:
        file_record = await session.get(FileRecord, proposal.file_id)
        if file_record is not None:
            file_record.state = FileState(body.file_state)
            file_state = FileState(body.file_state)
            if body.current_path is not None:
                file_record.current_path = body.current_path

    await session.commit()
    return ProposalStateResponse(
        proposal_id=proposal_id,
        proposal_state=new,
        file_state=file_state,
        current_path=body.current_path,
    )
```

**What differs from analog:**
- `agent_execution.py:83-133` enforces monotonic ladder (`_STATUS_ORDER` integer ranking + `<` comparator). This router uses a literal transition table (set of allowed `to` states per `from`) — D-28 only allows one entry per `from`, so simpler.
- Joint update across two tables (Proposal + FileRecord) in one commit — single source of truth requires single `session.commit()` (Pitfall 6).
- New enum values: `ProposalStatus.EXECUTED`, `ProposalStatus.FAILED`; `FileState.MOVED`, `FileState.UNCHANGED`. **FileState already has EXECUTED + FAILED** (`models/file.py:30-31`); the modify task ADDS `MOVED` and `UNCHANGED` per D-28.

**Gotchas:**
- D-28 same-state PATCH (re-PATCH EXECUTED → EXECUTED) is 200 no-op, not 409.
- RESEARCH Pitfall 6 (joint partial-commit): single `await session.commit()` after both row mutations.
- Verify `RenameProposal.status` is the column name (it is per `models/proposal.py:38`).

---

### `src/phaze/schemas/agent_identity.py`, `agent_analysis.py`, `agent_tracklists.py`, `agent_proposals.py` (schemas)

**Analog:** `src/phaze/schemas/agent_metadata.py:1-33` + `agent_files.py:25-53` + `agent_execution.py:1-71`.

**Pattern per file:**

**`agent_identity.py`** — mirror `schemas/agent_heartbeat.py`:
```python
"""Pydantic schema for GET /api/internal/agent/whoami response (CONTEXT.md D-15)."""

from datetime import datetime
from pydantic import BaseModel


class AgentIdentity(BaseModel):
    """Response body for /whoami. NOT a request schema -- no extra="forbid" needed."""
    agent_id: str
    name: str
    scan_roots: list[str]
    created_at: datetime
```

**`agent_analysis.py`** — mirror `schemas/agent_metadata.py:8-33`:
```python
from pydantic import BaseModel, ConfigDict, Field


class AnalysisWritePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")  # D-26
    bpm: float | None = Field(default=None, ge=0.0)
    musical_key: str | None = None
    mood: dict[str, float] | None = None   # CONTEXT.md D-26 says dict[str, float]
    style: dict[str, float] | None = None
    danceability: float | None = None
    energy: float | None = None
```

**`agent_tracklists.py`** — mirror `schemas/agent_files.py:25-53` (nested chunk pattern):
```python
import uuid
from typing import Literal
from pydantic import BaseModel, ConfigDict


class TracklistTrackPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position: int
    artist: str | None = None
    title: str | None = None
    timestamp: str | None = None
    confidence: float | None = None


class TracklistCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")  # D-27
    file_id: uuid.UUID
    source: Literal["fingerprint"]  # D-27
    external_id: str
    tracks: list[TracklistTrackPayload]
    request_id: uuid.UUID  # idempotency key


class TracklistCreateResponse(BaseModel):
    tracklist_id: uuid.UUID
    version: int
    track_count: int
```

**`agent_proposals.py`** — mirror `schemas/agent_execution.py:41-71`:
```python
import uuid
from typing import Literal
from pydantic import BaseModel, ConfigDict, model_validator


class ProposalStatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")  # D-28
    proposal_state: Literal["executed", "failed"]
    file_state: Literal["moved", "unchanged"] | None = None
    current_path: str | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def _require_path_when_moved(self) -> "ProposalStatePatch":
        # CONTEXT.md Claude's Discretion: conditional path requirement
        if self.file_state == "moved" and self.current_path is None:
            raise ValueError("current_path is required when file_state='moved'")
        return self


class ProposalStateResponse(BaseModel):
    proposal_id: uuid.UUID
    proposal_state: str
    file_state: str | None = None
    current_path: str | None = None
```

**What differs from analog:**
- `agent_identity.py` is RESPONSE-only — no `extra="forbid"` (Phase 25 convention: only requests are strict).
- `agent_tracklists.py` adds `request_id: uuid.UUID` for idempotency key (D-27, Stripe-style).
- `agent_proposals.py` adds `model_validator(mode="after")` for conditional field requirement — none of the Phase 25 schemas use this.

---

### `tests/test_routers/test_agent_identity.py`, `test_agent_analysis.py`, `test_agent_tracklists.py`, `test_agent_proposals.py`

**Analogs:**
- `tests/test_routers/test_agent_heartbeat.py:1-80` (smoke-app pattern for identity).
- `tests/test_routers/test_agent_metadata.py:1-224` (idempotent PUT replay tests for analysis).
- `tests/test_routers/test_agent_execution.py:33-180` (multi-verb POST+PATCH + 409 tests for proposals + FK pre-seed for tracklists).

**Shared smoke-app pattern** (verbatim from `test_agent_metadata.py:30-38`):
```python
def _make_smoke_app(session: AsyncSession) -> FastAPI:
    """Build a small FastAPI app that wires the agent_<resource> router.

    Tests are parallel-safe and decoupled from main.py's create_app wiring.
    """
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_<resource>_router)
    app.dependency_overrides[get_session] = lambda: session
    return app
```

**Tracklist FK seeding** (mirror `test_agent_execution.py:68-102` shape — needs FileRecord pre-seeded):
```python
async def _seed_file(session: AsyncSession, agent_id: str) -> uuid.UUID:
    file_id = uuid.uuid4()
    session.add(FileRecord(id=file_id, agent_id=agent_id, ...))
    await session.commit()
    return file_id
```

**Test cases per router** (compose from `test_agent_metadata.py` cases + `test_agent_execution.py` 409 cases):

| Test | Source pattern |
|------|----------------|
| `test_<verb>_happy_path` | `test_agent_metadata.py:60-95` |
| `test_<verb>_replay_idempotent` | `test_agent_metadata.py:98-120` |
| `test_<verb>_extra_field_422` | `test_agent_metadata.py:123-144` |
| `test_partial_<verb>_preserves_unset` (analysis only) | `test_agent_metadata.py:147-189` |
| `test_<verb>_missing_auth_returns_401` | `test_agent_files.py` |
| `test_proposal_illegal_transition_409` | `test_agent_execution.py:155-180` |
| `test_proposal_same_state_no_op` | `test_agent_execution.py:120-140` |
| `test_tracklist_idempotency_replay_returns_cached` | RESEARCH §Pattern 5 — NEW (no existing analog for Redis-cached idempotency) |
| `test_tracklist_different_payload_returns_409` | RESEARCH §Pitfall 4 — NEW (planner discretion) |

**Gotchas:**
- All 4 test files use `seed_test_agent` + smoke-app override pattern from `test_agent_metadata.py:30-38` (NOT the conftest `authenticated_client` since Phase 25's pattern is per-router smoke apps).
- `test_agent_tracklists.py` needs a Redis fixture. Planner discretion: real Redis (matches `test_agent_files.py` Queue patch pattern) vs fakeredis. CONTEXT.md says real Redis preferred for `test_agent_task_router.py` (D-30) — apply same here.

---

### `tests/test_services/test_agent_client.py` (respx contract test)

**Analog:** `tests/test_services/test_discogs_matcher.py:14-80` (AsyncMock httpx pattern) but RESEARCH §Pattern 10 (lines 1086-1224) is the verbatim target.

**Imports + fixture** (verbatim from RESEARCH §Pattern 10 lines 1093-1110):
```python
import httpx
import pytest
import respx

from phaze.services.agent_client import (
    AgentApiAuthError, AgentApiClientError, AgentApiServerError, PhazeAgentClient,
)


@pytest.fixture
def client() -> PhazeAgentClient:
    return PhazeAgentClient(base_url="http://app.test", token="test-token", timeout=5.0)
```

**Three classes of tests** (verbatim from RESEARCH §Pattern 10 lines 1113-1224):

| Test class | Pattern | Source lines in RESEARCH |
|------------|---------|--------------------------|
| Happy path + auth header | `respx.put(...).mock(return_value=Response(200, ...))`; assert `route.calls.last.request.headers["Authorization"] == "Bearer test-token"` | 1113-1128 |
| 4xx no-retry | `Response(401)`/`Response(404)`; assert `route.call_count == 1` + `pytest.raises(AgentApiAuthError|AgentApiClientError)` | 1131-1155 |
| 5xx with-retry + exhaust | `Response(500)`; assert `route.call_count == 3` + `pytest.raises(AgentApiServerError)` | 1158-1168 |
| 5xx retry recovers | `side_effect=[Response(500), Response(200, json={...})]`; assert `route.call_count == 2` | 1171-1184 |
| ConnectError retry | `side_effect=httpx.ConnectError("connection refused")`; assert `call_count == 3` | 1187-1197 |

**What differs from analog:**
- `test_discogs_matcher.py` uses `MagicMock`/`AsyncMock` directly on `client._client`; this file uses `respx.mock` for clean URL routing + call_count assertions.
- Adds 4xx-no-retry assertions (no analog — Phase 26 first to add tenacity retry policy).
- One test per endpoint method (whoami, put_analysis, put_metadata, put_fingerprint, create_tracklist, post_execution_log, patch_execution_log, patch_proposal_state, heartbeat) for the Bearer-header invariant — only retry tests need to cover one endpoint exhaustively.

**Gotchas:**
- `respx>=0.21` is a new dev dep (D-31, pyproject.toml change).
- `pytest-asyncio` is already configured (`asyncio_mode = "auto"` per `pyproject.toml:36`); no decorator needed but consistent with project convention.

---

### `tests/test_services/test_agent_task_router.py` (integration test)

**Analog:** `tests/test_routers/test_agent_files.py:79-100` (Queue patch pattern) — but D-30 mandates a REAL Redis (Docker-Compose-provided).

**Test cases** (per D-30):
```python
import pytest
from phaze.services.agent_task_router import AgentTaskRouter
from phaze.schemas.agent_tasks import ExtractMetadataPayload


@pytest.mark.asyncio
@pytest.mark.integration
async def test_enqueue_for_two_agents_isolated() -> None:
    """Two agent IDs -> two distinct SAQ queues, no cross-talk."""
    router = AgentTaskRouter(redis_url="redis://localhost:6379/0")
    try:
        payload = ExtractMetadataPayload(file_id=..., original_path="/x", file_type="mp3", agent_id="agent-a")
        await router.enqueue_for_agent(agent_id="agent-a", task_name="extract_file_metadata", payload=payload)
        await router.enqueue_for_agent(agent_id="agent-b", task_name="extract_file_metadata", payload=payload)
        # Assert two queues exist in Redis with prefix `phaze-agent-`
        # (use saq.Queue.info() or raw redis SCAN)
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_lazy_queue_cache_reuses_instance() -> None:
    """Second enqueue for the same agent reuses cached Queue."""
    router = AgentTaskRouter(redis_url="redis://localhost:6379/0")
    try:
        q1 = router._queue_for("agent-a")
        q2 = router._queue_for("agent-a")
        assert q1 is q2  # identity, not just equality
    finally:
        await router.close()
```

**Gotchas:** `test_agent_task_router.py` lives in `tests/test_services/` (matches Phase 25 `test_agent_upsert.py:875`).

---

### `tests/test_task_split.py` (subprocess import-boundary test)

**Analog:** RESEARCH §Pattern 9 (lines 1028-1074) — verbatim. NO existing analog in the codebase; this is the first subprocess-based test.

**Full file** (verbatim from RESEARCH §Pattern 9):
```python
"""D-25 import-boundary test: agent_worker must not pull phaze.database / sqlalchemy.ext.asyncio.

Run as a subprocess so a contaminated import in the test process doesn't poison
downstream tests via sys.modules caching.
"""

import subprocess
import sys
import textwrap


def test_agent_worker_does_not_import_phaze_database() -> None:
    script = textwrap.dedent("""
        import os
        import sys
        os.environ.setdefault("PHAZE_ROLE", "agent")
        os.environ.setdefault("PHAZE_AGENT_QUEUE", "phaze-agent-test-agent")
        os.environ.setdefault("PHAZE_AGENT_API_URL", "http://localhost:8000")
        os.environ.setdefault("PHAZE_AGENT_TOKEN", "phaze_agent_test")
        os.environ.setdefault("PHAZE_REDIS_URL", "redis://localhost:6379/0")
        import phaze.tasks.agent_worker  # noqa: F401

        forbidden = ("phaze.database", "phaze.tasks.session", "sqlalchemy.ext.asyncio")
        present = [m for m in forbidden if m in sys.modules]
        if present:
            sys.stderr.write(f"forbidden modules in sys.modules: {present}\\n")
            sys.exit(1)
        sys.exit(0)
    """)
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

**What differs:** First synchronous test in the suite (no `@pytest.mark.asyncio`). First subprocess invocation.

**Gotchas:**
- `sys.executable` ensures uv-managed venv is reused.
- `timeout=20` bounds runaway imports (e.g., asyncpg waiting on a missing DB).
- This test does NOT require Postgres or Redis to be running — the import should fail BEFORE any connection attempt.

---

### MODIFY: `src/phaze/tasks/functions.py` (rewrite process_file)

**Analog:** existing `tasks/functions.py:20-57` (current ORM-bound shape).

**Before** (current `tasks/functions.py:20-57`):
```python
async def process_file(ctx, *, file_id: str) -> dict:
    async with ctx["async_session"]() as session:
        result = await session.execute(select(FileRecord).where(FileRecord.id == uuid.UUID(file_id)))
        file_record = result.scalar_one_or_none()
        ...
        analysis = await run_in_process_pool(ctx, analyze_file, file_record.current_path, settings.models_path)
        ...
        analysis_result.bpm = analysis["bpm"]
        ...
        file_record.state = FileState.ANALYZED
        await session.commit()
```

**After** (use ctx["api_client"] + payload validation):
```python
async def process_file(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Process a single file: SAQ kwargs validated via Pydantic, results POST'd via HTTP."""
    payload = ProcessFilePayload.model_validate(kwargs)
    api: PhazeAgentClient = ctx["api_client"]

    # CPU-bound analysis using local file (D-23: original_path in payload, no DB read)
    analysis = await run_in_process_pool(ctx, analyze_file, payload.original_path, payload.models_path)

    # Send result via HTTP -- server upserts (D-26)
    await api.put_analysis(
        payload.file_id,
        AnalysisWritePayload(
            bpm=analysis["bpm"],
            musical_key=analysis["musical_key"],
            mood=analysis["mood"],
            style=analysis["style"],
        ),
    )
    return {"file_id": str(payload.file_id), "status": "analyzed"}
```

**What differs:** All `session`, `FileRecord`, `AnalysisResult` ORM access deleted. `file_record.state = FileState.ANALYZED` becomes implicit via server-side state machine in `PUT /analysis/{file_id}` (Phase 26 may add a state transition there, or leave to Phase 28's dispatch). Payload validated via Pydantic per D-23.

**Apply same pattern to:**
- `tasks/metadata_extraction.py` → uses `ExtractMetadataPayload` + `api.put_metadata(file_id, MetadataWritePayload(...))`.
- `tasks/fingerprint.py` → uses `FingerprintFilePayload` + per-engine `api.put_fingerprint(file_id, engine, FingerprintWritePayload(...))`.
- `tasks/scan.py` → uses `ScanLiveSetPayload` + `api.create_tracklist(TracklistCreatePayload(request_id=..., ...))`.
- `tasks/execution.py` → uses `ExecuteApprovedBatchPayload` + `api.post_execution_log(...)`, `api.patch_execution_log(...)`, `api.patch_proposal_state(...)`.

---

### MODIFY: `src/phaze/config.py` (Base + ControlSettings + AgentSettings)

**Analog:** existing `src/phaze/config.py:1-63` (single Settings class) + RESEARCH §Pattern 7 lines 757-828.

**Refactor target** (compose existing fields + RESEARCH §Pattern 7):
```python
"""Pydantic settings: BaseSettings + role-specific subclasses (CONTEXT.md D-14)."""

from enum import StrEnum
from functools import lru_cache
import os

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings as PydanticBaseSettings, SettingsConfigDict


class Role(StrEnum):
    CONTROL = "control"
    AGENT = "agent"


class BaseSettings(PydanticBaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Existing shared fields from config.py:13-60
    database_url: str = "postgresql+asyncpg://phaze:phaze@postgres:5432/phaze"
    redis_url: str = "redis://redis:6379/0"
    debug: bool = False
    scan_path: str = "/data/music"
    models_path: str = "/models"
    output_path: str = "/data/output"
    worker_max_jobs: int = 8
    worker_job_timeout: int = 600
    worker_max_retries: int = 4
    worker_process_pool_size: int = 4
    worker_health_check_interval: int = 60
    worker_keep_result: int = 3600
    audfprint_url: str = "http://audfprint:8001"
    panako_url: str = "http://panako:8002"
    discogsography_url: str = "http://discogsography:8000"
    api_host: str = "0.0.0.0"  # noqa: S104
    api_port: int = 8000
    agent_token_prefix: str = "phaze_agent_"  # noqa: S105
    agent_file_chunk_max: int = 1000


class ControlSettings(BaseSettings):
    discogs_match_concurrency: int = 5
    openai_api_key: SecretStr | None = None
    llm_model: str = "claude-sonnet-4-20250514"
    anthropic_api_key: SecretStr | None = None
    llm_max_rpm: int = 30
    llm_batch_size: int = 10
    llm_max_companion_chars: int = 3000
    controller_concurrency: int = 8


class AgentSettings(BaseSettings):
    agent_api_url: str = ""
    agent_token: SecretStr = SecretStr("")

    @model_validator(mode="after")
    def _enforce_required(self) -> "AgentSettings":
        if not self.agent_api_url:
            raise ValueError("PHAZE_AGENT_API_URL is required when PHAZE_ROLE=agent")
        if not self.agent_token.get_secret_value():
            raise ValueError("PHAZE_AGENT_TOKEN is required when PHAZE_ROLE=agent")
        return self


@lru_cache(maxsize=1)
def get_settings() -> BaseSettings:
    role = os.environ.get("PHAZE_ROLE", "control")
    if role == "agent":
        return AgentSettings()  # type: ignore[call-arg]
    return ControlSettings()  # type: ignore[call-arg]


# Back-compat singleton for existing call sites (config.py:63 was `settings = Settings()`)
settings = get_settings()
```

**What differs from analog:**
- Class split: 1 → 3 (Base + Control + Agent).
- Adds `Role` StrEnum + `get_settings()` factory + `lru_cache`.
- LLM/discogs fields moved to ControlSettings; agent fields land in AgentSettings.
- `settings = get_settings()` at module-level keeps existing call sites (e.g., `routers/agent_files.py:25`, `database.py:7`, `schemas/agent_files.py:15`) working without edits.

**Gotchas:**
- Existing call sites use `from phaze.config import settings` (37 occurrences); the singleton wrapper preserves this.
- `database.py:7` reads `settings.database_url` — that field stays on `BaseSettings`, no change needed.
- New env vars: `PHAZE_AGENT_API_URL`, `PHAZE_AGENT_TOKEN`, `PHAZE_AGENT_QUEUE`, `PHAZE_ROLE` (document in .env.example).

---

### MODIFY: `src/phaze/main.py`

**Analog:** existing `src/phaze/main.py:14-70` (router registration + lifespan).

**Three changes:**

**1. Extended import block** (alphabetical):
```python
from phaze.routers import (
    agent_analysis,         # NEW
    agent_execution,
    agent_files,
    agent_fingerprint,
    agent_heartbeat,
    agent_identity,         # NEW
    agent_metadata,
    agent_proposals,        # NEW
    agent_tracklists,       # NEW
    companion,
    # ... rest
)
from phaze.services.agent_task_router import AgentTaskRouter  # NEW
```

**2. Lifespan wires AgentTaskRouter + Redis** (mirror `main.py:35-45`):
```python
@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
    _app.state.queue = Queue.from_url(settings.redis_url)
    _app.state.task_router = AgentTaskRouter(redis_url=settings.redis_url)  # NEW (D-20)
    _app.state.redis = redis_async.Redis.from_url(settings.redis_url, decode_responses=True)  # NEW (for agent_tracklists)
    yield
    await _app.state.task_router.close()  # NEW
    await _app.state.redis.aclose()  # NEW
    await _app.state.queue.disconnect()
    await engine.dispose()
```

**3. 4 new `include_router` calls** (mirror `main.py:64-68`):
```python
# Phase 26 internal-agent routers
app.include_router(agent_identity.router)
app.include_router(agent_analysis.router)
app.include_router(agent_tracklists.router)
app.include_router(agent_proposals.router)
```

**What differs from analog:** Pure extension; lifespan grows by 4 lines (Redis + task_router setup/teardown).

---

### MODIFY: `src/phaze/routers/agent_files.py:99-117` (refactor to use AgentTaskRouter)

**Analog (current):** the inline `Queue.from_url(...)` + `try/finally: queue.disconnect()` block at lines 99-117.

**Refactor target:**
```python
# DELETE lines 99-117 (queue construction, try/finally)
# REPLACE with:
task_router = request.app.state.task_router  # NEW dep
enqueued = 0
for row in rows:
    if not row.inserted:
        continue
    ext = "." + row.file_type.lower()
    if EXTENSION_MAP.get(ext, FileCategory.UNKNOWN) not in _EXTRACTABLE:
        continue
    try:
        await task_router.enqueue_for_agent(
            agent_id=agent.id,
            task_name="extract_file_metadata",
            payload=ExtractMetadataPayload(
                file_id=row.id,
                original_path=row.original_path,  # FROM RETURNING -- need to add original_path to .returning(...)
                file_type=row.file_type,
                agent_id=agent.id,
            ),
        )
        enqueued += 1
    except Exception:
        logger.exception("Failed to enqueue for file_id=%s", row.id)
```

**Handler signature change:** add `request: Request` dep so `request.app.state.task_router` is reachable.

**What differs from analog:**
- No more `Queue.from_url(...)` per call (router-cached, no FD churn).
- No more `try/finally: await queue.disconnect()` (cached in router).
- Payload validated via `ExtractMetadataPayload` per D-22.
- `.returning(...)` block at lines 86-90 must add `FileRecord.original_path` so the payload has `original_path`.

---

### MODIFY: `src/phaze/models/proposal.py` + `src/phaze/models/file.py` (extend enums)

**Analog:** existing `src/phaze/models/file.py:20-32` (FileState already has DISCOVERED, EXECUTED, FAILED, etc.) + `models/execution.py:14-21` (StrEnum extension shape).

**`models/proposal.py`** — extend `ProposalStatus`:
```python
# Current models/proposal.py:20-25
class ProposalStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"      # NEW (D-28)
    FAILED = "failed"          # NEW (D-28)
```

**`models/file.py`** — extend `FileState`:
```python
# Current models/file.py:20-32 already has EXECUTED + FAILED. ADD:
class FileState(enum.StrEnum):
    # ... existing values ...
    MOVED = "moved"            # NEW (D-28)
    UNCHANGED = "unchanged"    # NEW (D-28)
```

**What differs:** Pure enum extension; column type is `String(20)`/`String(30)` (`models/proposal.py:38`, `models/file.py:47`), so no migration needed for column width — values fit.

**Gotchas:** No new constraints needed. No alembic migration required (StrEnum values are stored as plain strings).

---

### MODIFY: `docker-compose.yml`

**Analog:** existing `docker-compose.yml:19-39` (worker service block).

**Refactor:**
```yaml
  worker:
    # ... build, env_file unchanged ...
    command: uv run saq phaze.tasks.controller.settings  # was: phaze.tasks.worker.settings
    environment:
      - MODELS_PATH=/models
      - PHAZE_ROLE=control  # NEW
    # depends_on: drop audfprint + panako (controller is fileless)
```

**What differs:** Command path swap + add `PHAZE_ROLE=control` env. Drop `audfprint`/`panako` deps (controller doesn't fingerprint).

Phase 29 will add `docker-compose.agent.yml` for the agent role (CONTEXT.md D-04).

---

### MODIFY: `pyproject.toml`

**Analog:** existing `pyproject.toml:11-29` (dependencies, alphabetical) + lines 84-90 (mypy overrides).

**Three additions:**

```toml
# [project.dependencies] -- add tenacity (alphabetical position)
dependencies = [
    # ... existing alphabetical entries ...
    "saq[redis]>=0.26.3",
    "sqlalchemy>=2.0.49",
    "sse-starlette>=3.4.1",
    "tenacity>=8.5.0",  # NEW (CONTEXT.md D-11)
    "uvicorn>=0.46.0",
]

# [dependency-groups.dev] -- add respx
dev = [
    # ... existing entries ...
    "pytest-cov>=7.1.0",
    "respx>=0.21.1",  # NEW (CONTEXT.md D-31)
    "ruff>=0.15.12",
]

# [[tool.mypy.overrides]] -- two NEW blocks (CONTEXT.md D-33 + RESEARCH §Pattern 8)
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

**Gotcha:** RESEARCH §A8 — verify `[[tool.mypy.overrides]]` re-enables strict checking for files inside the `services/` exclude. If it doesn't, alternative is to remove `services/` from the global exclude or relocate the two files to a new non-excluded directory.

---

## Shared Patterns

### Pattern A: Authentication dep (D-15..D-17, reused for new routers)
**Source:** `src/phaze/routers/agent_auth.py:62-84` (Phase 25, no change)
**Apply to:** All 4 new routers (`agent_identity`, `agent_analysis`, `agent_tracklists`, `agent_proposals`).
```python
agent: Annotated[Agent, Depends(get_authenticated_agent)],
```

---

### Pattern B: `pg_insert.on_conflict_do_update` UPSERT
**Source:** `src/phaze/services/ingestion.py:103-117` and `src/phaze/routers/agent_metadata.py:56-68`
**Apply to:** `agent_analysis.py` (single-column natural key `file_id`).

```python
stmt = pg_insert(Model).values([payload])
if dumped:  # exclude_unset semantics
    stmt = stmt.on_conflict_do_update(
        index_elements=[<natural_key>],
        set_={k: stmt.excluded[k] for k in dumped},
    )
else:
    stmt = stmt.on_conflict_do_nothing(index_elements=[<natural_key>])
```

| Endpoint | `index_elements` | Source |
|----------|------------------|--------|
| `agent_analysis.py` | `["file_id"]` | `models/analysis.py:18` (`unique=True`) |

---

### Pattern C: Strict Pydantic request schemas (D-22, D-26, D-27, D-28)
**Source:** `src/phaze/schemas/agent_metadata.py:15` + `src/phaze/schemas/agent_files.py:28, 41`
**Apply to:** Every new request schema in `phaze.schemas.agent_analysis`, `agent_tracklists`, `agent_proposals`, `agent_tasks`.

```python
model_config = ConfigDict(extra="forbid")
```

Response schemas remain loose (forward-compat). RESEARCH Pitfall 5 applies — nested item schemas (e.g., `TracklistTrackPayload`) need `extra="forbid"` separately.

---

### Pattern D: SAQ enqueue via per-agent named queue
**Source:** `src/phaze/routers/agent_files.py:99-117` (inline) → refactored to `src/phaze/services/agent_task_router.py` (this phase).
**Apply to:** Both new (`agent_files.py` refactor) and future Phase 27 scan call site.

Cache key: agent slug (kebab-case, regex `^[a-z0-9]+(-[a-z0-9]+)*$` per `models/agent.py:35`).
Queue name format: `phaze-agent-<agent_id>` (string literal, no template — D-18).

---

### Pattern E: State-machine PATCH with 409 on illegal transitions
**Source:** `src/phaze/routers/agent_execution.py:47-57` (transition tables) + `:83-133` (handler)
**Apply to:** `agent_proposals.py` (single transition table + joint table update).

```python
_TRANSITIONS: dict[ProposalStatus, frozenset[ProposalStatus]] = {
    ProposalStatus.APPROVED: frozenset({ProposalStatus.EXECUTED, ProposalStatus.FAILED}),
}
```

Same-state re-PATCH is idempotent 200 no-op (per D-28). Different-state-from-terminal is 409. Single `session.commit()` for joint update (Pitfall 6).

---

### Pattern F: Smoke-app test fixture per router
**Source:** `tests/test_routers/test_agent_metadata.py:30-38` (also `test_agent_files.py:43-65`, `test_agent_execution.py:33-65`, `test_agent_heartbeat.py:30-35`)
**Apply to:** All 4 new test files (`test_agent_identity.py`, `test_agent_analysis.py`, `test_agent_tracklists.py`, `test_agent_proposals.py`).

Each test file builds its own self-contained FastAPI app via `_make_smoke_app(session)` so tests are parallel-safe and don't depend on `main.py` wiring landing in any particular order.

---

### Pattern G: tenacity retry funnel (4xx no-retry / 5xx retry)
**Source:** RESEARCH §Pattern 2 lines 360-466 (NEW pattern — no existing analog in codebase).
**Apply to:** `services/agent_client.py` `_request` method only — every endpoint method funnels through it.

Critical: `retry_if_exception(_should_retry)` NOT `retry_if_exception_type(HTTPStatusError)` (would retry 4xx — RESEARCH Pitfall 2).

---

### Pattern H: Subprocess test for structural invariants
**Source:** RESEARCH §Pattern 9 lines 1028-1074 (NEW pattern — no existing analog).
**Apply to:** `tests/test_task_split.py` only (the single import-boundary structural test).

Subprocess required because `sys.modules` is process-global; importing `phaze.tasks.agent_worker` in the test process would poison every downstream test.

---

## No Analog Found

| File | Role | Data Flow | Reason |
|------|------|-----------|--------|
| `tests/test_task_split.py` | structural test | subprocess | First subprocess-based test in the suite. No codebase analog; pattern from RESEARCH §Pattern 9. |
| `services/agent_client.py` (tenacity retry funnel only) | retry policy | request-response | First tenacity usage in the codebase. Pattern from RESEARCH §Pattern 2. |
| `routers/agent_tracklists.py` (Redis idempotency cache only) | idempotency | request-response | First Redis `SET NX EX`-based idempotency endpoint. `agent_execution.py:60-80` uses DB-level idempotency (`ON CONFLICT DO NOTHING`); this needs Redis because the write spans 1+N rows. Pattern from RESEARCH §Pattern 5. |
| `tests/test_services/test_agent_client.py` (respx mocks) | unit | HTTP mock | First respx usage. `test_discogs_matcher.py` uses raw `AsyncMock`. Pattern from RESEARCH §Pattern 10. |

## Metadata

**Analog search scope:**
- `src/phaze/routers/agent_*.py` (5 Phase 25 files: auth, files, metadata, fingerprint, execution, heartbeat)
- `src/phaze/services/discogs_matcher.py`, `ingestion.py`, `fingerprint.py` (httpx + UPSERT patterns)
- `src/phaze/schemas/agent_*.py` (5 Phase 25 schema files for `extra="forbid"` pattern)
- `src/phaze/tasks/worker.py` (full SAQ settings shape)
- `src/phaze/tasks/{functions,metadata_extraction,fingerprint,scan,execution}.py` (current ORM-bound task bodies)
- `src/phaze/models/{agent,file,proposal,analysis,tracklist,execution}.py` (enum + UQ shapes)
- `src/phaze/config.py`, `main.py`, `database.py` (wiring shapes)
- `tests/test_routers/test_agent_*.py` (smoke-app + FK seeding patterns)
- `tests/test_services/test_discogs_matcher.py` (AsyncMock httpx prior art)
- `tests/conftest.py` (seed_test_agent + authenticated_client fixtures)
- `docker-compose.yml`, `pyproject.toml` (config refactor scope)
- `.planning/phases/25-internal-agent-http-api-bearer-auth/25-PATTERNS.md` (Phase 25 patterns)

**Files scanned:** ~35 source files + 5 schemas + 6 Phase 25 routers + 6 Phase 25 tests + 2 config files.

**Pattern extraction date:** 2026-05-12
