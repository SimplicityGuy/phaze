# Phase 27: Watcher Service & User-Initiated Scan - Pattern Map

**Mapped:** 2026-05-13
**Files analyzed:** 38 (24 NEW, 14 MODIFIED)
**Analogs found:** 34 strong / 4 partial (sweep loop + debouncer have no codebase analog; RESEARCH.md is the source for those)

> Planner: every `<read_first>` block in PLAN.md files must point to a section below; every `<action>` block must reference a concrete identifier from the excerpts (not "match the pattern"). All file paths are absolute.

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/phaze/agent_watcher/__init__.py` | package-marker | n/a | `src/phaze/tasks/__init__.py` | exact (empty marker) |
| `src/phaze/agent_watcher/__main__.py` | process-entrypoint | event-driven (thread→asyncio) | `src/phaze/tasks/agent_worker.py` (startup, ctx wiring) | partial (asyncio.run vs SAQ settings) |
| `src/phaze/agent_watcher/observer.py` | adapter (thread→asyncio bridge) | event-driven | RESEARCH.md §"Pattern 1" + `src/phaze/routers/agent_files.py:25, 38, 109` (EXTENSION_MAP filter) | partial (new pattern) |
| `src/phaze/agent_watcher/debouncer.py` | in-memory state machine | event-driven | RESEARCH.md §"Code Examples / Debouncer" | no in-repo analog |
| `src/phaze/agent_watcher/poster.py` | HTTP adapter | request-response | `src/phaze/tasks/scan.py:40-83` (uses `ctx["api_client"]` + `upsert_files` shape) | role-match |
| `src/phaze/agent_watcher/README.md` | doc | n/a | (no per-service README precedent in repo; memory rule `feedback_readme_per_service`) | none — copy CLAUDE.md tone |
| `src/phaze/tasks/_shared/__init__.py` | package-marker | n/a | `src/phaze/tasks/__init__.py` | exact |
| `src/phaze/tasks/_shared/agent_bootstrap.py` | shared helpers (Postgres-free) | request-response | `src/phaze/tasks/agent_worker.py:69-89` (extract `_WHOAMI_BACKOFF_S` + `_whoami_with_retry`) | exact (in-place refactor) |
| `src/phaze/routers/agent_scan_batches.py` | controller (PATCH) | request-response | `src/phaze/routers/agent_execution.py:83-133` (PATCH structure) + `src/phaze/routers/agent_proposals.py:53-131` (cross-tenant guard + idempotent same-state) | exact |
| `src/phaze/routers/pipeline_scans.py` | controller (admin UI + HTMX) | request-response | `src/phaze/routers/pipeline.py:119-211` (dashboard + HTMX swap handlers) + `src/phaze/routers/scan.py:30-54` (path-traversal rejection) | exact |
| `src/phaze/schemas/agent_scan_batches.py` | schema (request + response) | request-response | `src/phaze/schemas/agent_execution.py:41-71` (ExecutionLogPatch + Response with `extra="forbid"`) | exact |
| `src/phaze/schemas/pipeline_scans.py` | schema (form body) | request-response | `src/phaze/schemas/agent_proposals.py:21-50` (ProposalStatePatch + model_validator) | role-match |
| `src/phaze/templates/pipeline/partials/trigger_scan_card.html` | template (form card) | request-response | `src/phaze/templates/pipeline/partials/stage_cards.html` (button + spinner) + `src/phaze/templates/search/partials/search_form.html` (form layout) | role-match |
| `src/phaze/templates/pipeline/partials/scan_path_picker.html` | template (HTMX swap target) | request-response | (form-field layout from `search_form.html`); UI-SPEC.md Component 2 is the byte-level contract | partial |
| `src/phaze/templates/pipeline/partials/scan_progress_card.html` | template (HTMX poll partial) | request-response (poll) | `src/phaze/templates/tracklists/partials/scan_progress.html` (byte-for-byte halt-on-terminal-state pattern) | exact |
| `src/phaze/templates/pipeline/partials/recent_scans_table.html` | template (mini-table) | request-response (OOB) | `src/phaze/templates/execution/partials/audit_table.html` (table + empty-state + overflow-x-auto) | exact |
| `src/phaze/templates/pipeline/partials/scan_status_pill.html` | template (shared pill) | n/a | `src/phaze/templates/tracklists/partials/status_badge.html` (pill geometry, color tokens, `py-0.5`) | exact (geometry mirror) |
| `src/phaze/templates/pipeline/partials/scan_submit_error.html` | template (error card) | request-response | (no in-repo `role="alert"` red-surface error card precedent; UI-SPEC §"Failure surfacing" is contract) | partial |
| `tests/test_agent_watcher/__init__.py` | test-package-marker | n/a | `tests/test_routers/__init__.py` | exact |
| `tests/test_agent_watcher/conftest.py` | test fixtures | n/a | `tests/test_routers/test_agent_files.py:52-96` (smoke-app + AsyncMock pattern, but watcher has no FastAPI app) | partial |
| `tests/test_agent_watcher/test_debouncer.py` | unit test | n/a | `tests/test_tasks/test_scan.py:15-49` (pure async function tests with monkeypatched clock) | partial |
| `tests/test_agent_watcher/test_observer.py` | unit test | n/a | `tests/test_tasks/test_scan.py` (pattern), watchdog event harness | partial |
| `tests/test_agent_watcher/test_main.py` | integration test | n/a | `tests/test_tasks/test_scan.py:51-87` (full ctx + AsyncMock api_client) | partial |
| `tests/test_routers/test_agent_scan_batches.py` | contract test | request-response | `tests/test_routers/test_agent_proposals.py:25-247` (smoke-app, cross-tenant 403, idempotent same-state) | exact |
| `tests/test_routers/test_agent_files_batch_id.py` | contract test | request-response | `tests/test_routers/test_agent_files.py:52-200` (smoke-app + extra-field-422 + chunk-cap pattern) | exact |
| `tests/test_routers/test_pipeline_scans.py` | controller test | request-response | `tests/test_routers/test_pipeline.py:54-95` (dashboard render + HTMX swap response tests) | role-match |
| `tests/test_tasks/test_scan_directory.py` | task test | event-driven | `tests/test_tasks/test_scan.py:15-87` (mock api_client + payload kwargs) | exact |
| `src/phaze/schemas/agent_files.py` (M) | schema | request-response | existing `FileUpsertChunk` lines 38-43; add `batch_id` field as in `ProposalStatePatch` style | exact (in-place add) |
| `src/phaze/schemas/agent_tasks.py` (M) | schema | request-response | existing `ScanLiveSetPayload` lines 61-68 — new `ScanDirectoryPayload` mirrors structurally | exact (add new class) |
| `src/phaze/routers/agent_files.py` (M) | controller | request-response | existing handler lines 41-138; extend records-loop at 57-66 to thread `batch_id` from `body.batch_id`/LIVE sentinel | exact (in-place extend) |
| `src/phaze/tasks/scan.py` (M) | SAQ task | request-response | existing `scan_live_set` lines 40-83 (signature, ctx access, payload validation) + `src/phaze/services/ingestion.py:45-88` (walk body) | exact (add new function) |
| `src/phaze/tasks/agent_worker.py` (M) | SAQ entry | n/a | existing `_whoami_with_retry` lines 73-89 (refactor target); functions list lines 200-215 (add `scan_directory`) | exact (in-place edit) |
| `src/phaze/services/agent_client.py` (M) | HTTP client | request-response | `src/phaze/services/agent_client.py:280-293` (`patch_proposal_state` byte-for-byte) | exact (add new method) |
| `src/phaze/templates/pipeline/dashboard.html` (M) | template | n/a | existing 1-20; add 2 `{% include %}` lines above `#pipeline-stats` | exact (in-place add) |
| `src/phaze/config.py` (M) | config | n/a | existing `AgentSettings` lines 86-143; add 4 `AliasChoices` fields like 100-107 | exact (in-place add) |
| `src/phaze/main.py` (M) | app factory | n/a | existing `create_app` lines 72-99; add 2 `include_router` lines | exact (in-place add) |
| `docker-compose.yml` (M) | infra | n/a | existing `worker:` block lines 28-45; new `watcher:` mirrors `worker:` minus essentia + minus saq command | exact (additive block) |
| `pyproject.toml` (M) | build | n/a | existing `[project].dependencies` lines 11-30; alphabetized insert of `watchdog>=4.0` | exact (one-line add) |
| `tests/test_task_split.py` (M) | invariant test | n/a | existing function lines 19-59 (banned-modules subprocess); add parallel `test_agent_watcher_does_not_import_phaze_database` | exact (add sibling) |

---

## Pattern Assignments

### `src/phaze/agent_watcher/__main__.py` (process-entrypoint, event-driven)

**Analog:** `/Users/Robert/Code/public/phaze/src/phaze/tasks/agent_worker.py` (startup hook + ctx wiring)

**Imports pattern** (excerpt from `src/phaze/tasks/agent_worker.py:40-66`):
```python
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from saq import Queue  # NOT used by watcher

from phaze.config import AgentSettings, get_settings
from phaze.services.agent_client import AgentApiError, PhazeAgentClient

if TYPE_CHECKING:
    from phaze.schemas.agent_identity import AgentIdentity

logger = logging.getLogger(__name__)
```

**For watcher, drop `saq.Queue`; add:**
```python
import signal
import unicodedata
from watchdog.observers import Observer

from phaze.tasks._shared.agent_bootstrap import construct_agent_client, whoami_with_retry
from phaze.agent_watcher.debouncer import Debouncer
from phaze.agent_watcher.observer import WatcherEventHandler
from phaze.agent_watcher.poster import Poster
```

**Startup-sequence pattern** (excerpt from `src/phaze/tasks/agent_worker.py:92-165`):
```python
async def startup(ctx: dict[str, Any]) -> None:
    cfg = get_settings()
    if not isinstance(cfg, AgentSettings):
        msg = f"agent_worker requires PHAZE_ROLE=agent; get_settings() returned {type(cfg).__name__}"
        raise RuntimeError(msg)

    # D-13 invariant: NEVER log the full bearer; preview is first-12-chars + "..." only.
    token_preview = cfg.agent_token.get_secret_value()[:12] + "..."  # nosec B105
    logger.info(
        "phaze.tasks.agent_worker startup role=agent api=%s auth_id_prefix=%s queue=%s",
        cfg.agent_api_url,
        token_preview,
        os.environ.get("PHAZE_AGENT_QUEUE", "<unset>"),
    )
    # ...
    client = PhazeAgentClient(
        base_url=cfg.agent_api_url,
        token=cfg.agent_token.get_secret_value(),
        timeout=30.0,
    )
    ctx["api_client"] = client
    identity = await _whoami_with_retry(client)
```

**For watcher, replace SAQ startup hook with `asyncio.run(main())`. Sweep-loop sketch comes from RESEARCH.md §"Pattern 2" lines 405-478.** No new pattern invention; copy literally.

**Shutdown pattern** (excerpt from `src/phaze/tasks/agent_worker.py:168-184`):
```python
async def shutdown(ctx: dict[str, Any]) -> None:
    logger.info("phaze.tasks.agent_worker shutdown")
    # ...
    client = ctx.get("api_client")
    if client is not None:
        await client.close()
```

Watcher's `finally:` block calls `observer.stop(); observer.join(); await client.close()` in the same order.

---

### `src/phaze/agent_watcher/observer.py` (adapter, event-driven)

**Analog:** RESEARCH.md §"Pattern 1" (lines 346-395) is the byte-level reference — no codebase analog exists. **Filtering pattern** mirrored from `src/phaze/routers/agent_files.py:38, 105-110`:

```python
# src/phaze/routers/agent_files.py:38, 109
_EXTRACTABLE: frozenset[FileCategory] = frozenset({FileCategory.MUSIC, FileCategory.VIDEO})
# ...
ext = "." + row.file_type.lower()
if EXTENSION_MAP.get(ext, FileCategory.UNKNOWN) not in _EXTRACTABLE:
    continue
```

**For observer.py, the planner copies RESEARCH.md §"Pattern 1" verbatim**, with `_EXTRACTABLE` declared exactly as above. The thread→asyncio bridge is `self._loop.call_soon_threadsafe(self._debouncer_touch, normalized)` — this is the ONLY safe bridge per RESEARCH.md §"Don't Hand-Roll".

**NFC normalization** mirrored from `src/phaze/routers/agent_files.py:62`:
```python
data["original_path"] = unicodedata.normalize("NFC", data["original_path"])
```

---

### `src/phaze/agent_watcher/debouncer.py` (in-memory state machine, event-driven)

**Analog:** RESEARCH.md §"Code Examples / Debouncer state machine" (lines 768-829). No codebase analog. Planner copies the `_PendingEntry` dataclass + `Debouncer` class shape verbatim. Key invariants:

- `dict[str, _PendingEntry]` keyed on NFC-normalized absolute path.
- `time.monotonic()` for all timestamps (per CONTEXT specifics §2).
- Methods called from asyncio loop ONLY (Observer thread uses `call_soon_threadsafe`).
- `sweep(settle_period, max_pending) -> (ready, evicted)` mutates pending set; iterates over `list(self._pending.items())` to avoid `RuntimeError: dictionary changed size during iteration`.

---

### `src/phaze/agent_watcher/poster.py` (HTTP adapter, request-response)

**Analog:** RESEARCH.md §"Poster — chunk-of-1 POST" (lines 834-894) is the byte-level reference. The exception-handling triad mirrors `src/phaze/services/agent_client.py:70-83`:

**Exception hierarchy from analog** (excerpt from `src/phaze/services/agent_client.py:70-83`):
```python
class AgentApiError(Exception):
    """Base for all PhazeAgentClient errors."""


class AgentApiAuthError(AgentApiError):
    """401 / 403 from the server. NEVER retried (D-12)."""


class AgentApiClientError(AgentApiError):
    """Any 4xx that is not auth. NEVER retried (D-12)."""


class AgentApiServerError(AgentApiError):
    """5xx after retries exhausted, or persistent ConnectError/Timeout (D-12)."""
```

**For poster.py, the planner copies RESEARCH.md §"Poster" verbatim** including the `OSError` drop case (Pitfall 1: rsync atomic-rename vanishes the path between debouncer.sweep and stat).

---

### `src/phaze/tasks/_shared/agent_bootstrap.py` (shared helpers, request-response)

**Analog:** `/Users/Robert/Code/public/phaze/src/phaze/tasks/agent_worker.py:69-89` — refactor target.

**Constant + helper to extract** (excerpt from `src/phaze/tasks/agent_worker.py:69-89`):
```python
_WHOAMI_BACKOFF_S: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0)
"""Bounded retry budget for the /whoami startup probe (~63s total wall-clock)."""


async def _whoami_with_retry(client: PhazeAgentClient) -> AgentIdentity:
    """Call client.whoami() with bounded exponential backoff. Raises RuntimeError on exhaustion."""
    last_exc: Exception | None = None
    for delay in _WHOAMI_BACKOFF_S:
        try:
            return await client.whoami()
        except AgentApiError as e:
            last_exc = e
            logger.warning("/whoami probe failed: %s; retrying in %.1fs", e, delay)
            await asyncio.sleep(delay)
    # One final attempt with no delay.
    try:
        return await client.whoami()
    except AgentApiError as e:
        last_exc = e
    msg = f"agent_worker /whoami probe exhausted retry budget (~63s); last error: {last_exc}"
    raise RuntimeError(msg)
```

**For `_shared/agent_bootstrap.py`, planner copies these verbatim and additionally exports `construct_agent_client` (RESEARCH.md describes this — see CONTEXT.md D-17):**
```python
def construct_agent_client(cfg: AgentSettings) -> PhazeAgentClient:
    return PhazeAgentClient(
        base_url=cfg.agent_api_url,
        token=cfg.agent_token.get_secret_value(),
        timeout=30.0,
    )
```

**Then `agent_worker.py` lines 73-89 are deleted and replaced with an import:**
```python
from phaze.tasks._shared.agent_bootstrap import (
    _WHOAMI_BACKOFF_S,
    construct_agent_client,
    whoami_with_retry as _whoami_with_retry,
)
```

Per Pitfall 7 (RESEARCH.md), the planner should also tighten `whoami_with_retry` to NOT retry on `AgentApiAuthError` (401/403 is permanent misconfig).

---

### `src/phaze/routers/agent_scan_batches.py` (controller, request-response)

**Analog:** Composite — `src/phaze/routers/agent_proposals.py:53-131` for cross-tenant guard + idempotent same-state + 404, `src/phaze/routers/agent_execution.py:83-133` for PATCH structure + state-machine validation.

**Module header pattern** (excerpt from `src/phaze/routers/agent_proposals.py:1-37`):
```python
"""PATCH /api/internal/agent/proposals/{proposal_id}/state -- joint Proposal+FileRecord state transition (Phase 26 D-28)."""

from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_scan_batches import ScanBatchPatch, ScanBatchPatchResponse


router = APIRouter(prefix="/api/internal/agent/scan-batches", tags=["agent-internal"])
```

**404 + cross-tenant guard pattern** (excerpt from `src/phaze/routers/agent_proposals.py:62-76`):
```python
# 404 if proposal_id does not exist
proposal = await session.get(RenameProposal, proposal_id)
if proposal is None:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="proposal not found")

# W1 / T-26-08-S2: cross-tenant guard. Load FileRecord.agent_id and reject if
# the proposal's file belongs to a different agent than the authenticated one.
# ... Returns 403 BEFORE state-machine logic so a leaked proposal_id cannot be
# probed via 409 timing.
file_record = await session.get(FileRecord, proposal.file_id)
if file_record is not None and file_record.agent_id != agent.id:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="proposal does not belong to authenticated agent",
    )
```

**For Phase 27 `agent_scan_batches.py`, planner adapts:**
```python
batch = await session.get(ScanBatch, batch_id)
if batch is None:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="scan batch not found")
if batch.agent_id != agent.id:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="scan batch does not belong to authenticated agent",
    )
```

**State-machine + idempotent same-state pattern** (excerpt from `src/phaze/routers/agent_proposals.py:78-103`):
```python
cur = ProposalStatus(proposal.status)
new = ProposalStatus(body.proposal_state)

# Same-state PATCH is idempotent 200 no-op (D-28 invariant). Echo current row
# state without DB writes -- the SAQ retry's previous successful PATCH already
# persisted the canonical state, so we just report it back.
if cur == new:
    # ... echo current row state ...
    return ProposalStateResponse(...)

# Disallowed transition: 409 with explicit detail.
allowed = _PROPOSAL_TRANSITIONS.get(cur, frozenset())
if new not in allowed:
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"illegal transition {cur.value} -> {new.value}",
    )
```

**For Phase 27, the state-machine table is:**
```python
# LIVE is a terminal sentinel state -- watcher NEVER PATCHes its batch.
# PATCH endpoint only accepts running, completed, failed (NOT live).
_SCAN_TRANSITIONS: dict[ScanStatus, frozenset[ScanStatus]] = {
    ScanStatus.RUNNING: frozenset({ScanStatus.COMPLETED, ScanStatus.FAILED}),
}
```

**Partial-field application pattern** (excerpt from `src/phaze/routers/agent_execution.py:124-128`):
```python
# Apply explicit-set mutations only (Pydantic `exclude_unset=True` -- default-None
# values do NOT clobber existing data).
for field, value in body.model_dump(exclude_unset=True).items():
    setattr(existing, field, value)
await session.commit()
```

**For Phase 27, planner uses the same loop**, then echoes the updated row as `ScanBatchPatchResponse` per CONTEXT discretion §4 (echo the row, no follow-up GET needed agent-side).

---

### `src/phaze/routers/agent_files.py` (M) (controller, request-response — EXTEND existing)

**Analog:** itself (`src/phaze/routers/agent_files.py:41-138`) — extend in place.

**Existing UPSERT loop** (excerpt from `src/phaze/routers/agent_files.py:57-95`):
```python
# 1. Build raw record dicts with agent_id stamped from auth dep (NEVER from body)
raw_records: list[dict[str, Any]] = []
for r in body.files:
    data = r.model_dump()
    # RESEARCH Pitfall 7: NFC-normalize defensively
    data["original_path"] = unicodedata.normalize("NFC", data["original_path"])
    data["agent_id"] = agent.id  # AUTH-01 -- stamped from auth, NEVER from body
    data["state"] = FileState.DISCOVERED  # server stamps initial state
    data["id"] = uuid.uuid4()  # server-generates new id; ON CONFLICT preserves existing id
    raw_records.append(data)
# ...
base_stmt = pg_insert(FileRecord).values(records)
upsert_stmt: Executable = base_stmt.on_conflict_do_update(
    index_elements=["agent_id", "original_path"],
    set_={
        "sha256_hash": base_stmt.excluded.sha256_hash,
        "file_size": base_stmt.excluded.file_size,
        "state": base_stmt.excluded.state,
        "batch_id": base_stmt.excluded.batch_id,  # already in SET clause
        ...
    },
)
```

**Phase 27 EXTENSION** — before the records loop (lines ~56-57), planner inserts a `resolved_batch_id` resolution block:
```python
# Phase 27 D-09: resolve batch_id from body or LIVE sentinel.
from phaze.models.scan_batch import ScanBatch, ScanStatus  # local import to keep module top untouched
from sqlalchemy import select

if body.batch_id is not None:
    batch = await session.get(ScanBatch, body.batch_id)
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="scan batch not found")
    if batch.agent_id != agent.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="scan batch does not belong to authenticated agent")
    resolved_batch_id = batch.id
else:
    # D-18: resolve LIVE sentinel from bearer-token-derived agent_id.
    # uq_scan_batches_agent_id_live guarantees exactly one row (Phase 24 D-12).
    stmt = select(ScanBatch.id).where(ScanBatch.agent_id == agent.id, ScanBatch.status == ScanStatus.LIVE.value)
    resolved_batch_id = (await session.execute(stmt)).scalar_one()  # must exist per Phase 24 D-11 seeding
```

Then in the records loop, stamp `data["batch_id"] = resolved_batch_id` alongside `data["agent_id"] = agent.id` (line 63).

**Cross-tenant guard placement INVARIANT** — 403 BEFORE the SELECT for LIVE-sentinel resolution (both branches return 403 before any state evaluation), mirroring `agent_proposals.py:71-76`.

---

### `src/phaze/routers/pipeline_scans.py` (controller, request-response)

**Analog:** Composite — `src/phaze/routers/pipeline.py:119-211` for dashboard HTMX swap handlers + `src/phaze/routers/scan.py:30-54` for path-traversal validation.

**Template wiring pattern** (excerpt from `src/phaze/routers/pipeline.py:29-31, 119-132`):
```python
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(tags=["pipeline"])
# ...
@router.get("/pipeline/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the pipeline dashboard page (per D-03)."""
    stats = await get_pipeline_stats(session)
    context = {"request": request, "stats": stats, ...}
    return templates.TemplateResponse(request=request, name="pipeline/dashboard.html", context=context)
```

**Path-traversal rejection pattern** (excerpt from `src/phaze/routers/scan.py:38-46`):
```python
scan_path = request.path or settings.scan_path

# Reject path traversal attempts
if ".." in scan_path:
    raise HTTPException(status_code=400, detail="Path traversal is not allowed")

# Validate scan path is an existing directory
if not Path(scan_path).is_dir():
    raise HTTPException(status_code=400, detail=f"Scan path is not a valid directory: {scan_path}")
```

**For Phase 27, the controller does NOT call `Path.is_dir()` — agent-side filesystem (CONTEXT D-07). Validation:**
```python
# Phase 27 D-06: scan_path = NFC(scan_root + subpath); reject ".."; prefix-validate against agent.scan_roots.
import unicodedata
joined = unicodedata.normalize("NFC", f"{form.scan_root.rstrip('/')}/{form.subpath.lstrip('/')}" if form.subpath else form.scan_root)
if ".." in joined:
    raise HTTPException(status_code=400, detail="Subpath must not contain '..' path traversal.")
agent = await session.get(Agent, form.agent_id)
if agent is None or agent.revoked_at is not None:
    raise HTTPException(status_code=400, detail="Unknown or revoked agent.")
if not any(joined == r or joined.startswith(r.rstrip("/") + "/") for r in agent.scan_roots):
    raise HTTPException(status_code=400, detail="Resolved path is outside the selected scan root.")
```

**Enqueue pattern** (excerpt from `src/phaze/routers/agent_files.py:103-125`):
```python
task_router = request.app.state.task_router
# ...
await task_router.enqueue_for_agent(
    agent_id=agent.id,
    task_name="extract_file_metadata",
    payload=ExtractMetadataPayload(...),
)
```

**For Phase 27, planner uses:**
```python
batch = ScanBatch(id=uuid.uuid4(), agent_id=form.agent_id, scan_path=joined, status=ScanStatus.RUNNING, total_files=0, processed_files=0)
session.add(batch)
await session.commit()
await request.app.state.task_router.enqueue_for_agent(
    agent_id=form.agent_id,
    task_name="scan_directory",
    payload=ScanDirectoryPayload(scan_path=joined, batch_id=batch.id, agent_id=form.agent_id),
)
```

**HTMX swap response pattern** (excerpt from `src/phaze/routers/pipeline.py:149-169`):
```python
@router.post("/pipeline/analyze", response_class=HTMLResponse)
async def trigger_analysis_ui(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: trigger analysis and return response fragment."""
    # ... do work ...
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/trigger_response.html",
        context={"request": request, "action": "analysis", "count": count},
    )
```

---

### `src/phaze/services/agent_client.py` (M) — new `patch_scan_batch` method

**Analog:** `/Users/Robert/Code/public/phaze/src/phaze/services/agent_client.py:280-293` (`patch_proposal_state`).

**Verbatim mirror** (excerpt from `src/phaze/services/agent_client.py:280-293`):
```python
async def patch_proposal_state(
    self,
    proposal_id: uuid.UUID,
    payload: ProposalStatePatch,
) -> ProposalStateResponse:
    """PATCH /api/internal/agent/proposals/{id}/state -- joint Proposal + FileRecord (D-28)."""
    from phaze.schemas.agent_proposals import ProposalStateResponse  # noqa: PLC0415

    response = await self._request(
        "PATCH",
        f"/api/internal/agent/proposals/{proposal_id}/state",
        json=payload.model_dump(mode="json", exclude_unset=True),
    )
    return ProposalStateResponse.model_validate(response.json())
```

**For Phase 27, planner adds (just below this method):**
```python
async def patch_scan_batch(
    self,
    batch_id: uuid.UUID,
    payload: ScanBatchPatch,
) -> ScanBatchPatchResponse:
    """PATCH /api/internal/agent/scan-batches/{batch_id} -- update batch status/counts (Phase 27 D-10)."""
    from phaze.schemas.agent_scan_batches import ScanBatchPatchResponse  # noqa: PLC0415

    response = await self._request(
        "PATCH",
        f"/api/internal/agent/scan-batches/{batch_id}",
        json=payload.model_dump(mode="json", exclude_unset=True),
    )
    return ScanBatchPatchResponse.model_validate(response.json())
```

Add `ScanBatchPatch, ScanBatchPatchResponse` to the `TYPE_CHECKING` block at the top of the file (lines 36-64).

---

### `src/phaze/tasks/scan.py` (M) — new `scan_directory` function

**Analog:** `/Users/Robert/Code/public/phaze/src/phaze/tasks/scan.py:40-83` (`scan_live_set` shape) + `/Users/Robert/Code/public/phaze/src/phaze/services/ingestion.py:45-88` (walk body).

**Signature + ctx access pattern** (excerpt from `src/phaze/tasks/scan.py:40-46`):
```python
async def scan_live_set(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Run fingerprint-query against a live-set file; POST tracklist via HTTP."""
    payload = ScanLiveSetPayload.model_validate(kwargs)

    api: PhazeAgentClient = ctx["api_client"]
    orchestrator: FingerprintOrchestrator = ctx["fingerprint_orchestrator"]
```

**For Phase 27 `scan_directory`:**
```python
async def scan_directory(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Walk a directory, SHA-256 known-extension files, POST chunks of 500 via HTTP (Phase 27 D-11..D-13)."""
    from phaze.schemas.agent_tasks import ScanDirectoryPayload
    payload = ScanDirectoryPayload.model_validate(kwargs)
    api: PhazeAgentClient = ctx["api_client"]
    # ...
```

**Walk body pattern** (excerpt from `src/phaze/services/ingestion.py:45-88`):
```python
def discover_and_hash_files(scan_path: str, batch_id: uuid.UUID) -> list[dict[str, Any]]:
    scan_root = Path(scan_path)
    records: list[dict[str, Any]] = []

    for dirpath, _dirnames, filenames in os.walk(scan_root, followlinks=False):
        for filename in filenames:
            category = classify_file(filename)
            if category == FileCategory.UNKNOWN:
                continue

            full_path = Path(dirpath) / filename
            try:
                file_size = full_path.stat().st_size
                sha256_hash = compute_sha256(full_path)
            except OSError as exc:
                logger.warning("Skipping unreadable file %s: %s", full_path, exc)
                continue

            normalized_path = normalize_path(str(full_path))
            normalized_filename = normalize_path(filename)
            file_ext = Path(filename).suffix.lower().lstrip(".")

            records.append({
                "id": uuid.uuid4(),  # NOT included by scan_directory — controller stamps id
                "agent_id": LEGACY_AGENT_ID,  # NOT included by scan_directory — controller stamps from token
                "sha256_hash": sha256_hash,
                "original_path": normalized_path,
                ...
            })
    return records
```

**For Phase 27 `scan_directory`** — adapts this walk body but:
1. Does NOT stamp `agent_id` (controller does, AUTH-01).
2. Does NOT stamp `id` or `state` or `batch_id` in the record dict (controller handles).
3. **Chunks of 500** — flushes via `await api.upsert_files(FileUpsertChunk(files=batch, batch_id=payload.batch_id))` when `len(batch) == settings.scan_chunk_size`.
4. **After each chunk POST**, `await api.patch_scan_batch(payload.batch_id, ScanBatchPatch(processed_files=total_so_far))`.
5. **Per-file OSError** — same `continue` pattern (D-12 mid-walk skip).
6. **Hashes in `asyncio.to_thread`** — `sha256 = await asyncio.to_thread(compute_sha256, full_path)` (CONTEXT discretion §9; mirrors `services/ingestion.py:148`).
7. **On clean walk** — final PATCH `status=completed, total_files=N, processed_files=N`.
8. **On abort** — PATCH `status=failed, error_message=str(exc)`.
9. **NEVER** imports `phaze.database`, `phaze.models.*`, or `sqlalchemy` (D-13 + Phase 26 D-25 invariant — `tests/test_task_split.py` enforces).

**Module-level constants to copy locally** (since ingestion.py imports phaze.models which is banned for agent-side tasks):
```python
# Inline NFC normalize and classify -- don't import from services/ingestion.py (it touches phaze.models).
def _normalize_path(p: str) -> str:
    return unicodedata.normalize("NFC", p)

def _classify(filename: str) -> FileCategory:
    return EXTENSION_MAP.get(Path(filename).suffix.lower(), FileCategory.UNKNOWN)
```

---

### `src/phaze/tasks/agent_worker.py` (M) — register scan_directory + import refactor

**Existing functions list** (excerpt from `src/phaze/tasks/agent_worker.py:200-215`):
```python
settings = {
    "queue": queue,
    "functions": [
        process_file,
        extract_file_metadata,
        fingerprint_file,
        scan_live_set,
        execute_approved_batch,
    ],
    ...
}
```

**For Phase 27**, planner adds `scan_directory` to imports (line ~59) and to the list:
```python
from phaze.tasks.scan import scan_live_set, scan_directory
# ...
"functions": [
    process_file,
    extract_file_metadata,
    fingerprint_file,
    scan_live_set,
    scan_directory,  # Phase 27 D-13
    execute_approved_batch,
],
```

And replaces `_WHOAMI_BACKOFF_S` + `_whoami_with_retry` (lines 69-89) with import from `_shared.agent_bootstrap` (see §"_shared/agent_bootstrap.py" above).

---

### `src/phaze/schemas/agent_files.py` (M) — add `batch_id` field

**Existing class** (excerpt from `src/phaze/schemas/agent_files.py:38-43`):
```python
class FileUpsertChunk(BaseModel):
    """Body of POST /api/internal/agent/files: bounded list of FileUpsertRecord."""

    model_config = ConfigDict(extra="forbid")

    files: list[FileUpsertRecord] = Field(min_length=1, max_length=_CHUNK_MAX)
```

**For Phase 27** (D-09), planner appends one field + the import:
```python
from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

from phaze.config import settings


class FileUpsertChunk(BaseModel):
    """Body of POST /api/internal/agent/files: bounded list of FileUpsertRecord."""

    model_config = ConfigDict(extra="forbid")

    files: list[FileUpsertRecord] = Field(min_length=1, max_length=_CHUNK_MAX)
    batch_id: uuid.UUID | None = None  # Phase 27 D-09: present -> bind to batch; absent -> LIVE sentinel
```

`None` default makes this non-breaking for Phase 25 callers per `extra="forbid"` semantics.

---

### `src/phaze/schemas/agent_tasks.py` (M) — add `ScanDirectoryPayload`

**Existing analog** (excerpt from `src/phaze/schemas/agent_tasks.py:61-68`):
```python
class ScanLiveSetPayload(BaseModel):
    """SAQ job: fingerprint-query a live-set file and resolve a proposed tracklist."""

    model_config = ConfigDict(extra="forbid")

    file_id: uuid.UUID
    original_path: str
    agent_id: str
```

**For Phase 27 (D-14)**, planner adds (alphabetical or end-of-file — match existing order):
```python
class ScanDirectoryPayload(BaseModel):
    """SAQ job: walk a directory on the agent and stream FileRecord chunks back via HTTP (Phase 27 D-14)."""

    model_config = ConfigDict(extra="forbid")

    scan_path: str
    batch_id: uuid.UUID
    agent_id: str
```

---

### `src/phaze/schemas/agent_scan_batches.py` (NEW)

**Analog:** `/Users/Robert/Code/public/phaze/src/phaze/schemas/agent_execution.py:41-71`.

**Verbatim shape mirror** (excerpt from `src/phaze/schemas/agent_execution.py:41-71`):
```python
class ExecutionLogPatch(BaseModel):
    """Partial-update body for PATCH /execution-log/{id}."""

    model_config = ConfigDict(extra="forbid")

    status: ExecutionStatus
    error_message: str | None = None
    sha256_verified: bool | None = None


class ExecutionLogPatchResponse(BaseModel):
    """Minimal echo response confirming the patch (D-19)."""

    agent_id: str
    execution_log_id: uuid.UUID
    status: ExecutionStatus
```

**For Phase 27 (D-10)**, planner writes:
```python
"""Pydantic schemas for PATCH /api/internal/agent/scan-batches/{id} (Phase 27 D-10)."""

from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict


class ScanBatchPatch(BaseModel):
    """Partial-update body for PATCH /scan-batches/{batch_id}.

    LIVE is a terminal sentinel state (watcher-owned) -- NOT in the Literal.
    Allowed transitions: RUNNING -> COMPLETED | FAILED; same-state PATCH is 200 idempotent.
    """

    model_config = ConfigDict(extra="forbid")

    total_files: int | None = None
    processed_files: int | None = None
    status: Literal["running", "completed", "failed"] | None = None
    error_message: str | None = None


class ScanBatchPatchResponse(BaseModel):
    """Echo response per CONTEXT D-Discretion §4 (return the updated row)."""

    batch_id: uuid.UUID
    agent_id: str
    scan_path: str
    status: str
    total_files: int
    processed_files: int
    error_message: str | None = None
```

---

### `src/phaze/schemas/pipeline_scans.py` (NEW)

**Analog:** `/Users/Robert/Code/public/phaze/src/phaze/schemas/agent_proposals.py:21-50` for `extra="forbid"` + `model_validator` pattern, though no validator is needed here.

**For Phase 27**, planner writes:
```python
"""Form-body schema for POST /pipeline/scans (Phase 27 D-06)."""

from pydantic import BaseModel, ConfigDict


class TriggerScanForm(BaseModel):
    """Operator-submitted trigger-scan form. Validated by router (D-06)."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    scan_root: str
    subpath: str = ""  # optional; empty -> scan the entire scan_root
```

(In practice, FastAPI form bodies parse from `application/x-www-form-urlencoded` via `Form(...)` parameters or a Pydantic model with `Annotated[..., Form()]`. Planner picks the FastAPI pattern that matches the rest of the codebase — check `src/phaze/routers/pipeline.py` does not currently use Pydantic form bodies, only HTMX hidden inputs read via `Request.form()`. Planner picks consistent style.)

---

### `src/phaze/config.py` (M) — add `AgentSettings` watcher fields

**Existing AliasChoices pattern** (excerpt from `src/phaze/config.py:100-119`):
```python
agent_api_url: str = Field(
    default="",
    validation_alias=AliasChoices("PHAZE_AGENT_API_URL", "agent_api_url"),
)
agent_token: SecretStr = Field(
    default=SecretStr(""),
    validation_alias=AliasChoices("PHAZE_AGENT_TOKEN", "agent_token"),
)
scan_roots: Annotated[list[str], NoDecode] = Field(
    default_factory=list,
    validation_alias=AliasChoices("PHAZE_AGENT_SCAN_ROOTS", "scan_roots"),
    description=...,
)
```

**For Phase 27 (D-03 + D-11)**, planner adds 4 fields to `AgentSettings` after line 119:
```python
watcher_settle_seconds: int = Field(
    default=10,
    validation_alias=AliasChoices("PHAZE_WATCHER_SETTLE_SECONDS", "watcher_settle_seconds"),
    description="Seconds a file's mtime must be stable before the watcher posts it (D-01).",
)
watcher_max_pending_seconds: int = Field(
    default=3600,
    validation_alias=AliasChoices("PHAZE_WATCHER_MAX_PENDING_SECONDS", "watcher_max_pending_seconds"),
    description="Stuck-file cap; entries older than this are evicted from the pending set (D-02).",
)
watcher_sweep_interval_seconds: int = Field(
    default=2,
    validation_alias=AliasChoices("PHAZE_WATCHER_SWEEP_INTERVAL_SECONDS", "watcher_sweep_interval_seconds"),
    description="How often the watcher's sweep task checks for settled files (D-01).",
)
scan_chunk_size: int = Field(
    default=500,
    validation_alias=AliasChoices("PHAZE_SCAN_CHUNK_SIZE", "scan_chunk_size"),
    description="Number of FileUpsertRecord rows per chunk in scan_directory (D-11).",
)
```

The existing `model_validator(mode="after")` (lines 135-143) needs no extension — these fields have safe defaults.

---

### `src/phaze/main.py` (M) — add 2 routers

**Existing wire-up** (excerpt from `src/phaze/main.py:87-97`):
```python
# Phase 25 internal-agent routers (D-10)
app.include_router(agent_files.router)
app.include_router(agent_metadata.router)
app.include_router(agent_fingerprint.router)
app.include_router(agent_execution.router)
app.include_router(agent_heartbeat.router)
# Phase 26 internal-agent routers (D-15, D-26, D-27, D-28)
app.include_router(agent_identity.router)
app.include_router(agent_analysis.router)
app.include_router(agent_tracklists.router)
app.include_router(agent_proposals.router)
```

**For Phase 27**, planner adds to the imports block (lines 15-37) and to the wire-up:
```python
# Phase 27 routers
app.include_router(agent_scan_batches.router)
app.include_router(pipeline_scans.router)
```

`pipeline_scans` uses prefix `/pipeline/scans` (per CONTEXT D-06); `agent_scan_batches` uses prefix `/api/internal/agent/scan-batches` (per D-10).

---

### `docker-compose.yml` (M) — add `watcher` service

**Existing `worker:` block** (excerpt from `docker-compose.yml:28-45`):
```yaml
worker:
  build:
    context: .
    dockerfile: Dockerfile
  command: uv run saq phaze.tasks.controller.settings
  env_file: .env
  environment:
    - MODELS_PATH=/models
    - PHAZE_ROLE=control
  volumes:
    - "${SCAN_PATH:-/data/music}:/data/music:ro"
    - "${MODELS_PATH:-./models}:/models:ro"
    - "${OUTPUT_PATH:-/data/output}:/data/output:rw"
  depends_on:
    postgres:
      condition: service_healthy
    redis:
      condition: service_healthy
```

**For Phase 27 (D-19)**, planner adds (after `worker:` block, before `postgres:`):
```yaml
# Phase 27 D-19: always-on watcher. Will move to docker-compose.agent.yml in Phase 29
# alongside the renamed worker (per Phase 26 D-04 plan). Image is the same as `worker`
# but entry point is `python -m phaze.agent_watcher` (NOT saq settings).
watcher:
  build:
    context: .
    dockerfile: Dockerfile
  command: uv run python -m phaze.agent_watcher
  env_file: .env
  environment:
    - PHAZE_ROLE=agent
    # PHAZE_AGENT_API_URL, PHAZE_AGENT_TOKEN, PHAZE_AGENT_SCAN_ROOTS, PHAZE_AGENT_QUEUE come from .env
  volumes:
    - "${SCAN_PATH:-/data/music}:/data/music:ro"
  depends_on:
    api:
      condition: service_started
  restart: unless-stopped
```

`depends_on: api: service_started` (NOT `service_healthy`) — Phase 25 `api` has no healthcheck. The watcher's `whoami_with_retry` (~63s budget) absorbs uvicorn boot time per RESEARCH Pitfall 6.

---

### `pyproject.toml` (M) — add `watchdog>=4.0`

**Existing dependencies block** (excerpt from `pyproject.toml:11-30`):
```toml
dependencies = [
    "alembic>=1.18.4",
    "saq[redis]>=0.26.3",
    "asyncpg>=0.31.0",
    "beautifulsoup4>=4.14.3",
    "essentia-tensorflow>=2.1b6.dev1389; sys_platform != 'linux' or platform_machine == 'x86_64'",
    "fastapi>=0.136.1",
    "httpx>=0.28.1",
    ...
    "uvicorn>=0.46.0",
]
```

**For Phase 27 (D-23)**, planner inserts `"watchdog>=4.0",` alphabetically (between `uvicorn` and the closing `]`). Then runs `uv sync` to refresh the lock file in the same commit per CLAUDE.md `pyproject.toml` section-order rule.

---

### `tests/test_task_split.py` (M) — add parallel watcher test

**Existing test** (excerpt from `tests/test_task_split.py:19-59`):
```python
def test_agent_worker_does_not_import_phaze_database() -> None:
    script = textwrap.dedent("""
        import os
        import sys
        os.environ.setdefault("PHAZE_ROLE", "agent")
        os.environ.setdefault("PHAZE_AGENT_API_URL", "http://localhost:8000")
        os.environ.setdefault("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-1234567890abcdef")
        os.environ.setdefault("PHAZE_AGENT_QUEUE", "phaze-agent-test-agent")
        os.environ.setdefault("PHAZE_AGENT_SCAN_ROOTS", "/tmp")
        os.environ.setdefault("PHAZE_REDIS_URL", "redis://localhost:6379/0")
        import phaze.tasks.agent_worker  # noqa: F401

        forbidden = ("phaze.database", "phaze.tasks.session", "sqlalchemy.ext.asyncio")
        present = [m for m in forbidden if m in sys.modules]
        if present:
            for m in present:
                mod = sys.modules[m]
                sys.stderr.write(f"BANNED MODULE IMPORTED: {m} (file={getattr(mod, '__file__', '?')})\\n")
            sys.exit(1)
        sys.exit(0)
    """)
    result = subprocess.run(...)
    assert result.returncode == 0, ...
```

**For Phase 27 (D-22 + D-25 parity)**, planner adds a parallel function. Swap `import phaze.tasks.agent_worker` for `import phaze.agent_watcher` and **extend the forbidden tuple** to include `phaze.tasks.agent_worker` (per RESEARCH Pitfall 5 — watcher must not drag in the SAQ settings module):
```python
def test_agent_watcher_does_not_import_phaze_database() -> None:
    """Phase 27 D-22: watcher must stay Postgres-free AND must not pull in SAQ settings.

    Banned modules: phaze.database, phaze.tasks.session, sqlalchemy.ext.asyncio,
    phaze.tasks.agent_worker (RESEARCH Pitfall 5 -- watcher uses asyncio.run, NOT SAQ).
    """
    script = textwrap.dedent("""
        import os, sys
        os.environ.setdefault("PHAZE_ROLE", "agent")
        os.environ.setdefault("PHAZE_AGENT_API_URL", "http://localhost:8000")
        os.environ.setdefault("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-1234567890abcdef")
        os.environ.setdefault("PHAZE_AGENT_SCAN_ROOTS", "/tmp")
        # NB: NO PHAZE_AGENT_QUEUE -- watcher does not need it. Confirms agent_worker
        # is NOT pulled into the import graph (it would raise at module-load without QUEUE).
        os.environ.pop("PHAZE_AGENT_QUEUE", None)
        import phaze.agent_watcher  # noqa: F401

        forbidden = (
            "phaze.database",
            "phaze.tasks.session",
            "sqlalchemy.ext.asyncio",
            "phaze.tasks.agent_worker",
        )
        present = [m for m in forbidden if m in sys.modules]
        if present:
            for m in present:
                mod = sys.modules[m]
                sys.stderr.write(f"BANNED MODULE IMPORTED: {m} (file={getattr(mod, '__file__', '?')})\\n")
            sys.exit(1)
        sys.exit(0)
    """)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, f"agent_watcher import contaminated sys.modules:\nstdout={result.stdout}\nstderr={result.stderr}"
```

---

### `tests/test_routers/test_agent_scan_batches.py` (NEW)

**Analog:** `/Users/Robert/Code/public/phaze/tests/test_routers/test_agent_proposals.py:1-247` — verbatim mirror.

**Smoke-app fixture pattern** (excerpt from `tests/test_routers/test_agent_proposals.py:25-35`):
```python
def _make_smoke_app(session: AsyncSession) -> FastAPI:
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_proposals.router)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _make_client(session: AsyncSession, token: str | None = None) -> AsyncClient:
    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)
```

**For Phase 27**, planner copies this verbatim — substituting `agent_scan_batches` for `agent_proposals`.

**Cross-tenant 403 test pattern** (excerpt from `tests/test_routers/test_agent_proposals.py:201-225`):
```python
async def test_proposal_cross_agent_403(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """W1 / T-26-08-S2: agent B cannot mutate a proposal whose file belongs to agent A."""
    agent_a, _ = seed_test_agent
    _, proposal_id = await _seed_file_and_proposal(session, agent_a.id)

    # Seed a SECOND agent (B) inline, matching conftest.seed_test_agent's pattern.
    raw_token_b = "phaze_agent_" + secrets.token_urlsafe(32)
    token_hash_b = hashlib.sha256(raw_token_b.encode("utf-8")).hexdigest()
    agent_b = Agent(
        id="test-agent-b",
        name="test-agent-b",
        token_hash=token_hash_b,
        scan_roots=["/test/b"],
    )
    session.add(agent_b)
    await session.commit()

    async with _make_client(session, raw_token_b) as ac:
        r = await ac.patch(
            f"/api/internal/agent/proposals/{proposal_id}/state",
            json={"proposal_state": "executed", "file_state": "moved", "current_path": "/p"},
        )
    assert r.status_code == 403
```

**For Phase 27**, planner adapts — seeds a `ScanBatch` owned by agent A, PATCHes from agent B, asserts 403 + `"does not belong"` substring.

**Same-state idempotent pattern** (excerpt from `tests/test_routers/test_agent_proposals.py:117-136`):
```python
async def test_same_state_idempotent_no_op(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """PATCH executed -> executed twice -> both return 200, row stays EXECUTED."""
    ...
    r1 = await ac.patch(..., json={"proposal_state": "executed", ...})
    r2 = await ac.patch(..., json={"proposal_state": "executed"})
    assert r1.status_code == 200
    assert r2.status_code == 200
```

For Phase 27: PATCH `status=running` (the existing state) twice → both 200.

**Test inventory the planner must produce for `test_agent_scan_batches.py`:**
- `test_running_to_completed_200` — happy path.
- `test_running_to_failed_with_error_message_200` — error_message persisted.
- `test_same_state_idempotent_no_op` — PATCH `running` → `running` echoes row.
- `test_completed_to_running_409` — terminal-state guard (mirrors `agent_execution.py:117-118` pattern).
- `test_live_status_in_body_422` — `Literal["running","completed","failed"]` rejects `"live"`.
- `test_batch_not_found_404` — unknown batch_id.
- `test_extra_field_422` — `extra="forbid"`.
- `test_cross_agent_403` — verbatim mirror.
- `test_missing_auth_returns_401` — bearer-required.
- `test_unknown_token_returns_403` — hash miss.

---

### `tests/test_routers/test_agent_files_batch_id.py` (NEW)

**Analog:** `/Users/Robert/Code/public/phaze/tests/test_routers/test_agent_files.py:1-200` — verbatim fixture mirror.

**Test inventory:**
- `test_batch_id_present_binds_files_to_that_batch` — pass `batch_id` in body; verify `FileRecord.batch_id == batch_id`.
- `test_batch_id_absent_resolves_live_sentinel` — omit `batch_id`; verify resolved to LIVE batch (need to seed one in fixture, mirroring Phase 24 D-11).
- `test_batch_id_cross_agent_403` — pass agent A's batch_id with agent B's bearer → 403.
- `test_batch_id_unknown_404` — random UUID → 404.
- `test_batch_id_in_body_does_not_bypass_agent_id_stamp` — the chunk-level `batch_id` is permitted but `agent_id` on a record is still rejected (extra_forbidden); the per-record `agent_id` rejection at `test_agent_files.py:182-189` is reused.

The smoke-app fixture from `test_agent_files.py:52-65` works as-is (mock task_router required).

---

### `tests/test_routers/test_pipeline_scans.py` (NEW)

**Analog:** `/Users/Robert/Code/public/phaze/tests/test_routers/test_pipeline.py:54-95` — dashboard-render + HTMX swap.

**Dashboard test pattern** (excerpt from `tests/test_routers/test_pipeline.py:54-69`):
```python
async def test_dashboard_page(client: AsyncClient) -> None:
    """GET /pipeline/ returns 200 with Pipeline Dashboard heading."""
    response = await client.get("/pipeline/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Pipeline Dashboard" in response.text
```

**Mock task_router pattern** (excerpt from `tests/test_routers/test_pipeline.py:78-86`):
```python
mock_queue = AsyncMock()
mock_queue.enqueue = AsyncMock()
client._transport.app.state.queue = mock_queue  # type: ignore[union-attr]

response = await client.post("/api/v1/analyze")
```

**Test inventory:**
- `test_pipeline_dashboard_renders_trigger_scan_card` — GET `/pipeline/` → 200 + body contains `"Trigger Scan"`.
- `test_agent_roots_swap_returns_partial` — GET `/pipeline/scans/agent-roots?agent_id=...` → HTML partial with `<select id="scan-root">`.
- `test_agent_roots_swap_unknown_agent_yields_empty_state` — non-existent agent → "no scan roots" copy.
- `test_post_scans_happy_path` — POST `/pipeline/scans` with valid form → 200 + progress card HTML + `enqueue_for_agent` called with `task_name="scan_directory"`.
- `test_post_scans_subpath_rejects_dotdot` — `subpath=".."` → 400 + error partial.
- `test_post_scans_path_outside_scan_root` — synthesized path outside `agent.scan_roots` → 400.
- `test_post_scans_unknown_agent_400` — unknown `agent_id`.
- `test_get_scan_progress_running_returns_polling_partial` — GET `/pipeline/scans/{batch_id}` with RUNNING batch → response contains `hx-trigger="every 2s"`.
- `test_get_scan_progress_completed_omits_hx_trigger` — terminal-state response has NO `hx-trigger` (halts polling).

---

### `tests/test_tasks/test_scan_directory.py` (NEW)

**Analog:** `/Users/Robert/Code/public/phaze/tests/test_tasks/test_scan.py:15-87` (ctx mock + payload kwargs + AsyncMock api_client).

**ctx + payload fixture pattern** (excerpt from `tests/test_tasks/test_scan.py:15-30`):
```python
def _make_ctx(api_client: AsyncMock | None = None, orchestrator: AsyncMock | None = None) -> dict[str, Any]:
    """Create a minimal SAQ context dict with api_client + orchestrator mocks."""
    if api_client is None:
        api_client = AsyncMock()
        api_client.create_tracklist = AsyncMock(return_value=MagicMock(...))
    if orchestrator is None:
        orchestrator = AsyncMock()
    return {"api_client": api_client, "fingerprint_orchestrator": orchestrator}


def _make_payload_kwargs(file_id: uuid.UUID | None = None) -> dict[str, Any]:
    return {
        "file_id": str(file_id or uuid.uuid4()),
        "original_path": "/music/liveset.mp3",
        "agent_id": "test-agent",
    }
```

**For Phase 27 `test_scan_directory.py`:**
```python
def _make_ctx(api_client: AsyncMock | None = None) -> dict[str, Any]:
    if api_client is None:
        api_client = AsyncMock()
        api_client.upsert_files = AsyncMock(return_value=MagicMock(upserted=1, inserted=1, enqueued=1))
        api_client.patch_scan_batch = AsyncMock()
    return {"api_client": api_client}


def _make_payload_kwargs(scan_path: str, batch_id: uuid.UUID | None = None) -> dict[str, Any]:
    return {"scan_path": scan_path, "batch_id": str(batch_id or uuid.uuid4()), "agent_id": "test-agent"}
```

**Test inventory:**
- `test_scan_directory_walks_known_extensions` — drop fixture files; verify `upsert_files` called with mp3/flac records only.
- `test_scan_directory_chunks_at_500` — drop 1001 fixture files; verify `upsert_files` called 3 times with chunks of 500, 500, 1.
- `test_scan_directory_patches_progress_after_each_chunk` — 1500 files; verify `patch_scan_batch(processed_files=...)` called 3 times with monotonic values.
- `test_scan_directory_patches_final_status_completed` — clean walk → final PATCH with `status="completed"`.
- `test_scan_directory_patches_final_status_failed_on_missing_path` — scan_path doesn't exist → PATCH with `status="failed", error_message="..."`.
- `test_scan_directory_skips_unreadable_file` — chmod 000 a fixture file → walk continues, logs warning, completes.
- `test_scan_directory_nfc_normalizes_paths` — drop NFD-named file (combining-character); verify upserted path is NFC.
- `test_scan_directory_omits_extra_fields_in_records` — `FileUpsertRecord` does NOT carry `agent_id` or `id` (extra="forbid" would 422 anyway, but verify the record dict shape).

---

### `tests/test_agent_watcher/` (NEW package + 3 test files)

**Conftest pattern** (analog: `tests/conftest.py:30-66` for fixture shape; watcher has no app):

For `tests/test_agent_watcher/conftest.py`, planner provides:
- A `tmp_watcher_root(tmp_path)` fixture returning an isolated dir.
- An `async fake_clock()` fixture that monkeypatches `time.monotonic` to a controllable value.
- A `mock_api_client` fixture wrapping `PhazeAgentClient` with `respx` (already in dev deps if available; otherwise `AsyncMock`).

**test_debouncer.py inventory** (analog: pure-function unit tests, no FastAPI app):
- `test_touch_inserts_new_entry` — `touch(path)` → `pending_count == 1`.
- `test_touch_resets_last_change_at` — touch twice with monkeypatched clock; assert entry's `last_change_at` is the latest.
- `test_sweep_returns_ready_after_settle` — touch, advance clock past `settle_period`, sweep → `ready == [path]`.
- `test_sweep_evicts_stuck_entries` — touch, advance clock past `max_pending`, sweep → `evicted == [path]`.
- `test_sweep_does_not_return_unsettled_entry` — touch, advance < `settle_period`, sweep → `ready == []`.

**test_observer.py inventory** (analog: thread→asyncio bridge):
- `test_event_handler_filters_by_extension` — instantiate handler, fire synthetic `FileCreatedEvent(src_path="/foo/a.txt")` and `.../b.mp3` → only `.mp3` triggers debouncer callback.
- `test_event_handler_ignores_directories` — `is_directory=True` → no call.
- `test_event_handler_normalizes_path` — fire event with NFD path, captured callback arg is NFC.
- `test_event_handler_uses_call_soon_threadsafe` — patch loop, assert `call_soon_threadsafe` invoked (not direct call).

**test_main.py inventory** (analog: full ctx integration with mocked `PhazeAgentClient` via `respx`):
- `test_main_constructs_observer_per_scan_root` — multi-root identity → observer.schedule called N times.
- `test_main_calls_whoami_then_starts_observer` — order assertion.
- `test_main_graceful_shutdown_on_sigterm` — `shutdown_event.set()` → observer.stop + client.close awaited.
- `test_main_exits_nonzero_on_whoami_exhaustion` — mock whoami to always raise → RuntimeError.

---

### `src/phaze/templates/pipeline/dashboard.html` (M) — add 2 includes

**Existing template** (excerpt from `src/phaze/templates/pipeline/dashboard.html`):
```jinja
{% block content %}
<div class="space-y-6">
    <h1 class="text-2xl font-semibold leading-tight">Pipeline Dashboard</h1>

    <!-- Stats bar (polled via HTMX every 5s) -->
    <div id="pipeline-stats" hx-get="/pipeline/stats" hx-trigger="every 5s" hx-swap="innerHTML">
        {% include "pipeline/partials/stats_bar.html" %}
    </div>

    <!-- Stage cards with trigger buttons -->
    <div id="pipeline-stages">
        {% include "pipeline/partials/stage_cards.html" %}
    </div>
</div>
{% endblock %}
```

**For Phase 27**, planner inserts the 2 new includes BEFORE `#pipeline-stats` (UI-SPEC Component Contracts §"Page vertical rhythm"):
```jinja
{% block content %}
<div class="space-y-6">
    <h1 class="text-2xl font-semibold leading-tight">Pipeline Dashboard</h1>

    <!-- Phase 27: Trigger Scan card -->
    {% include "pipeline/partials/trigger_scan_card.html" %}

    <!-- Phase 27: Recent Scans mini-table -->
    {% include "pipeline/partials/recent_scans_table.html" %}

    <!-- Stats bar (polled via HTMX every 5s) -->
    <div id="pipeline-stats" hx-get="/pipeline/stats" hx-trigger="every 5s" hx-swap="innerHTML">
        {% include "pipeline/partials/stats_bar.html" %}
    </div>

    <!-- Stage cards with trigger buttons -->
    <div id="pipeline-stages">
        {% include "pipeline/partials/stage_cards.html" %}
    </div>
</div>
{% endblock %}
```

The `dashboard()` handler in `src/phaze/routers/pipeline.py:119-132` must additionally pass `agents=` and `recent_scans=` in the context (planner adds 2 SELECT queries — `select(Agent).where(Agent.revoked_at.is_(None)).order_by(Agent.name)` and `select(ScanBatch).where(ScanBatch.status != ScanStatus.LIVE).order_by(ScanBatch.created_at.desc()).limit(10)`).

---

### `src/phaze/templates/pipeline/partials/scan_progress_card.html` (NEW)

**Analog:** `/Users/Robert/Code/public/phaze/src/phaze/templates/tracklists/partials/scan_progress.html` — byte-for-byte halt-on-terminal-state pattern.

**Verbatim halt pattern** (excerpt from `tracklists/partials/scan_progress.html`):
```jinja
{% if done %}
<div class="bg-white dark:bg-phaze-bg border border-gray-200 dark:border-phaze-border rounded-lg p-4" aria-live="polite">
    {# terminal state — NO hx-trigger here, so polling halts when this replaces the in-progress markup #}
    <p class="text-sm text-gray-900 dark:text-gray-100">Scan complete. …</p>
</div>
{% else %}
<div class="bg-white dark:bg-phaze-bg border border-gray-200 dark:border-phaze-border rounded-lg p-4" aria-live="polite"
     hx-get="/tracklists/scan/status?job_ids={{ job_ids }}"
     hx-trigger="every 3s"
     hx-swap="innerHTML"
     hx-target="#scan-panel">
    <p class="text-sm text-gray-900 dark:text-gray-100">Scanning... ({{ completed }} of {{ total }} files)</p>
</div>
{% endif %}
```

**For Phase 27 `scan_progress_card.html`**, planner uses UI-SPEC Component 3 markup (lines 264-326 of 27-UI-SPEC.md). Critical differences:
- `hx-trigger="every 2s"` (not 3s; per UI-SPEC).
- `hx-swap="outerHTML"` (not innerHTML; per UI-SPEC line 269 — the swap replaces the polling element entirely so terminal-state markup naturally halts polling).
- Three states (running, completed, failed) — branch on `batch.status` not `done`.
- Status pill uses the shared `scan_status_pill.html` partial.

---

### `src/phaze/templates/pipeline/partials/scan_status_pill.html` (NEW)

**Analog:** `/Users/Robert/Code/public/phaze/src/phaze/templates/tracklists/partials/status_badge.html` — geometry mirror.

**Verbatim shape** (excerpt from `tracklists/partials/status_badge.html`):
```jinja
{% if tracklist.status == 'proposed' %}
<span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-yellow-100 dark:bg-yellow-950 text-yellow-700 dark:text-yellow-400">Proposed</span>
{% elif tracklist.status == 'approved' %}
<span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-green-100 dark:bg-green-950 text-green-700 dark:text-green-400">Approved</span>
{% elif tracklist.status == 'rejected' %}
<span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-red-100 dark:bg-red-950 text-red-700 dark:text-red-400">Rejected</span>
{% endif %}
```

**For Phase 27 (UI-SPEC Component 5)**:
```jinja
{% if batch.status == 'running' %}
<span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-blue-100 dark:bg-blue-950 text-blue-700 dark:text-blue-400" aria-label="Status: running">RUNNING</span>
{% elif batch.status == 'completed' %}
<span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-green-100 dark:bg-green-950 text-green-700 dark:text-green-400" aria-label="Status: completed">COMPLETED</span>
{% elif batch.status == 'failed' %}
<span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-red-100 dark:bg-red-950 text-red-700 dark:text-red-400" aria-label="Status: failed">FAILED</span>
{% endif %}
```

The `py-0.5` is the documented Phase 27 spacing exception (UI-SPEC §"Spacing Exceptions") — deliberately matching this analog.

---

### `src/phaze/templates/pipeline/partials/recent_scans_table.html` (NEW)

**Analog:** `/Users/Robert/Code/public/phaze/src/phaze/templates/execution/partials/audit_table.html` — table + empty state + `overflow-x-auto`.

**Verbatim table-shell pattern** (excerpt from `execution/partials/audit_table.html`):
```jinja
<div id="audit-table-container">
    {% if logs %}
    <div class="overflow-x-auto">
        <table id="audit-table" class="w-full text-sm text-left">
            <thead class="text-xs font-semibold text-gray-500 dark:text-gray-400 dark:text-gray-500 uppercase border-b border-gray-200 dark:border-phaze-border">
                <tr>
                    <th scope="col" class="px-4 py-3">Operation</th>
                    ...
                </tr>
            </thead>
            <tbody>
                {% for log in logs %}
                    {% include "execution/partials/audit_row.html" %}
                {% endfor %}
            </tbody>
        </table>
    </div>
    {% include "execution/partials/pagination.html" %}
    {% else %}
    <div class="text-center py-12">
        <p class="text-xl font-semibold text-gray-900 dark:text-gray-100 mb-2">No operations recorded</p>
        <p class="text-sm text-gray-500 dark:text-gray-400 dark:text-gray-500">Execute approved proposals to begin. Operations will be logged here as they run.</p>
    </div>
    {% endif %}
</div>
```

**For Phase 27**, planner copies the shell exactly — replaces columns with UI-SPEC Component 4 markup (lines 346-387 of 27-UI-SPEC.md). Phase 27 omits pagination (capped at 10 rows). Status-pill rendering uses `{% include "pipeline/partials/scan_status_pill.html" %}`.

---

### `src/phaze/templates/pipeline/partials/trigger_scan_card.html` (NEW)

**Analog:** Composite — form-button shell from `src/phaze/templates/pipeline/partials/stage_cards.html:11-23` (`hx-post` + `hx-indicator` + spinner span), form-field layout from `src/phaze/templates/search/partials/search_form.html`.

**Button + spinner pattern** (excerpt from `stage_cards.html:11-23`):
```jinja
<button
    hx-post="/pipeline/analyze"
    hx-target="#analyze-response"
    hx-swap="innerHTML"
    hx-indicator="#analyze-spinner"
    @click="loading = true"
    :disabled="loading || {{ stats.discovered }} === 0"
    class="px-4 py-2 bg-blue-600 dark:bg-blue-700 text-white rounded hover:bg-blue-700 dark:hover:bg-blue-600 disabled:opacity-50 disabled:cursor-not-allowed text-sm font-medium">
    Run Analysis
</button>
<span id="analyze-spinner" class="htmx-indicator ml-2 text-sm text-gray-500 dark:text-gray-400 dark:text-gray-500">Enqueuing...</span>
```

**For Phase 27 `trigger_scan_card.html`** — planner builds per UI-SPEC Component 1 (lines 138-208 of 27-UI-SPEC.md). Differences from stage_cards.html: form with `<select>` + `<input>` + submit; the submit button copy is `Start Scan` (not `Run Analysis`); the spinner copy is `Enqueuing…` (matching existing).

---

### `src/phaze/templates/pipeline/partials/scan_path_picker.html` (NEW)

**Analog:** `src/phaze/templates/search/partials/search_form.html` (form-field layout with `<label>` + `<select>`/`<input>` pairs).

**Form-field pattern** (excerpt from `search_form.html`):
```jinja
<div>
    <label for="filter-artist" class="block text-xs font-medium text-gray-600 dark:text-gray-400 dark:text-gray-500 mb-1">Artist</label>
    <input type="text"
           id="filter-artist"
           name="artist"
           value="{{ artist or '' }}"
           placeholder="Filter by artist..."
           class="w-full rounded-md border border-gray-300 dark:border-phaze-border px-3 py-2 text-sm">
</div>
```

**For Phase 27**, planner uses UI-SPEC Component 2 markup verbatim (lines 218-243 of 27-UI-SPEC.md). Critical: the partial returns BOTH the scan_root `<select>` AND the subpath `<input>` together (UI-SPEC line 183).

---

### `src/phaze/templates/pipeline/partials/scan_submit_error.html` (NEW)

**Analog:** No in-codebase `role="alert"` red-surface card precedent. Planner copies UI-SPEC §"Failure surfacing" markup verbatim (lines 440-444 of 27-UI-SPEC.md):
```jinja
<div class="bg-red-50 dark:bg-red-950/30 border border-red-200 dark:border-red-900 rounded-md p-3" role="alert">
    <p class="text-sm text-red-700 dark:text-red-400">{{ error_message }}</p>
</div>
```

---

### `src/phaze/agent_watcher/README.md` (NEW)

**Analog:** No per-service README in repo. Memory rule `feedback_readme_per_service` requires one.

**For Phase 27**, planner writes a brief README documenting:
1. Purpose (always-on file watcher; per-file SHA-256 + POST).
2. Entry point (`uv run python -m phaze.agent_watcher`).
3. Required env vars (`PHAZE_ROLE=agent`, `PHAZE_AGENT_API_URL`, `PHAZE_AGENT_TOKEN`, `PHAZE_AGENT_SCAN_ROOTS`).
4. Tunable env vars (`PHAZE_WATCHER_SETTLE_SECONDS`, `PHAZE_WATCHER_MAX_PENDING_SECONDS`, `PHAZE_WATCHER_SWEEP_INTERVAL_SECONDS`).
5. Import-boundary invariant ("Must NOT import phaze.database — enforced by `tests/test_task_split.py`").
6. Phase 29 migration note ("Will move to docker-compose.agent.yml in Phase 29").

Tone: terse, operator-grade, matches CLAUDE.md style. No emojis.

---

## Shared Patterns

### Authentication

**Source:** `/Users/Robert/Code/public/phaze/src/phaze/routers/agent_auth.py:62-84` (`get_authenticated_agent`)
**Apply to:** `src/phaze/routers/agent_scan_batches.py` AND the modified `src/phaze/routers/agent_files.py` cross-tenant branch.

```python
agent: Annotated[Agent, Depends(get_authenticated_agent)],
```

Token format `phaze_agent_<32 urlsafe-base64>`; verification is one indexed SELECT against `agents.token_hash` with `revoked_at IS NULL` predicate (partial index `ix_agents_token_hash_active`). Revocation is immediate (no in-process cache).

### Cross-Tenant 403 Guard (BEFORE State Machine)

**Source:** `/Users/Robert/Code/public/phaze/src/phaze/routers/agent_proposals.py:62-76`
**Apply to:** `src/phaze/routers/agent_scan_batches.py` AND `src/phaze/routers/agent_files.py` (batch_id-bound branch).

The 403 must be raised BEFORE any state-machine evaluation (409 timing oracle prevention). Cite Phase 26 D-08 in the inline comment.

### Idempotent Same-State PATCH (200 Echo, No DB Write)

**Source:** `/Users/Robert/Code/public/phaze/src/phaze/routers/agent_proposals.py:81-95`
**Apply to:** `src/phaze/routers/agent_scan_batches.py`.

Re-PATCHing the same status echoes the current row and does NOT bump `updated_at`. Tested in `test_agent_proposals.py::test_same_state_idempotent_no_op` (verbatim mirror needed in `test_agent_scan_batches.py`).

### Pydantic `extra="forbid"` + AUTH-01 (agent_id from auth)

**Source:** `/Users/Robert/Code/public/phaze/src/phaze/schemas/agent_files.py:25-43` + `/Users/Robert/Code/public/phaze/src/phaze/routers/agent_files.py:60-63`
**Apply to:** every new schema (`ScanBatchPatch`, `ScanDirectoryPayload`, `TriggerScanForm`, updated `FileUpsertChunk`) AND the modified upsert handler (agent_id is NEVER read from body).

```python
model_config = ConfigDict(extra="forbid")
# AND in router:
data["agent_id"] = agent.id  # AUTH-01 -- stamped from auth, NEVER from body
```

### `AliasChoices` per-field env mapping

**Source:** `/Users/Robert/Code/public/phaze/src/phaze/config.py:100-119`
**Apply to:** `src/phaze/config.py` — the 4 new `AgentSettings` fields (D-03 + D-11).

Both the `PHAZE_*` env var AND the bare field name resolve; tests can monkeypatch bare names.

### NFC Normalization on Path Input

**Source:** `/Users/Robert/Code/public/phaze/src/phaze/services/ingestion.py:33` + `/Users/Robert/Code/public/phaze/src/phaze/routers/agent_files.py:62`
**Apply to:** `agent_watcher/observer.py` (handler), `agent_watcher/poster.py` (record build), `tasks/scan.py` (`scan_directory` walk body), `routers/pipeline_scans.py` (path validation).

```python
unicodedata.normalize("NFC", str(full_path))
```

Single source of truth. Drift between watcher and scan_directory would cause duplicate FileRecord rows (RESEARCH Pitfall 3).

### `ctx["api_client"]` resource handle

**Source:** `/Users/Robert/Code/public/phaze/src/phaze/tasks/agent_worker.py:122-128` (writer) + `/Users/Robert/Code/public/phaze/src/phaze/tasks/scan.py:44` (reader)
**Apply to:** `src/phaze/tasks/scan.py` (`scan_directory` reads `ctx["api_client"]`).

The agent_watcher process constructs its own `PhazeAgentClient` directly (no SAQ ctx).

### Per-Agent SAQ Queue Routing

**Source:** `/Users/Robert/Code/public/phaze/src/phaze/services/agent_task_router.py:74-88` + `/Users/Robert/Code/public/phaze/src/phaze/routers/agent_files.py:111-121`
**Apply to:** `src/phaze/routers/pipeline_scans.py` (POST handler):
```python
await request.app.state.task_router.enqueue_for_agent(
    agent_id=form.agent_id,
    task_name="scan_directory",
    payload=ScanDirectoryPayload(...),
)
```

### HTMX Poll Partial With Final-State Halt

**Source:** `/Users/Robert/Code/public/phaze/src/phaze/templates/tracklists/partials/scan_progress.html`
**Apply to:** `src/phaze/templates/pipeline/partials/scan_progress_card.html`.

In-progress markup carries `hx-trigger="every 2s"` + `hx-swap="outerHTML"`. Terminal-state markup OMITS both — replacing the polling element ends polling.

### Subprocess Import-Boundary Test (D-25 invariant)

**Source:** `/Users/Robert/Code/public/phaze/tests/test_task_split.py:19-59`
**Apply to:** new `test_agent_watcher_does_not_import_phaze_database` (D-22 extension).

`subprocess.run([sys.executable, "-c", ...], timeout=20)` so sys.modules pollution doesn't poison the test session. Banned-set extended for Phase 27: add `"phaze.tasks.agent_worker"` to the forbidden tuple per RESEARCH Pitfall 5.

### Smoke-App + AsyncClient Test Pattern

**Source:** `/Users/Robert/Code/public/phaze/tests/test_routers/test_agent_proposals.py:25-35` AND `/Users/Robert/Code/public/phaze/tests/test_routers/test_agent_files.py:52-96`
**Apply to:** `tests/test_routers/test_agent_scan_batches.py`, `tests/test_routers/test_agent_files_batch_id.py`.

```python
def _make_smoke_app(session: AsyncSession) -> FastAPI:
    app = FastAPI(title="smoke", version="test")
    app.include_router(<new_router>.router)
    app.dependency_overrides[get_session] = lambda: session
    # If handler reads app.state.task_router: install AsyncMock here.
    app.state.task_router = AsyncMock()
    return app
```

### Seed-Two-Agents-For-Cross-Tenant-Test Pattern

**Source:** `/Users/Robert/Code/public/phaze/tests/test_routers/test_agent_proposals.py:208-217` (inline second-agent seeding)
**Apply to:** `tests/test_routers/test_agent_scan_batches.py::test_cross_agent_403` and `tests/test_routers/test_agent_files_batch_id.py::test_batch_id_cross_agent_403`.

---

## No Analog Found

Files with no close in-repo match (planner uses RESEARCH.md patterns instead):

| File | Role | Data Flow | Reason / Source |
|------|------|-----------|------------------|
| `src/phaze/agent_watcher/debouncer.py` | in-memory state machine | event-driven | No watchdog/debounce code exists yet. RESEARCH.md §"Code Examples / Debouncer state machine" (lines 768-829) is the byte-level source. |
| `src/phaze/agent_watcher/observer.py` | thread→asyncio bridge | event-driven | No `loop.call_soon_threadsafe` pattern exists in repo. RESEARCH.md §"Pattern 1" (lines 346-395) is the byte-level source. |
| `src/phaze/agent_watcher/__main__.py` (sweep-loop body) | async event loop with graceful shutdown | event-driven | Repo's only `asyncio.run` is in entry-point scripts that don't have signal handlers + sweep semantics. RESEARCH.md §"Pattern 2" (lines 405-478) is the byte-level source. |
| `src/phaze/templates/pipeline/partials/scan_submit_error.html` | `role="alert"` red-surface error card | n/a | No in-repo precedent for this exact card shape; UI-SPEC §"Failure surfacing" (lines 440-444) is the only source. |

---

## Metadata

**Analog search scope:**
- `/Users/Robert/Code/public/phaze/src/phaze/routers/`
- `/Users/Robert/Code/public/phaze/src/phaze/services/`
- `/Users/Robert/Code/public/phaze/src/phaze/schemas/`
- `/Users/Robert/Code/public/phaze/src/phaze/tasks/`
- `/Users/Robert/Code/public/phaze/src/phaze/templates/`
- `/Users/Robert/Code/public/phaze/tests/test_routers/`
- `/Users/Robert/Code/public/phaze/tests/test_tasks/`
- `/Users/Robert/Code/public/phaze/tests/test_task_split.py`
- `/Users/Robert/Code/public/phaze/docker-compose.yml`
- `/Users/Robert/Code/public/phaze/pyproject.toml`

**Files read in full (or near-full):** 22
- `src/phaze/tasks/agent_worker.py`
- `src/phaze/routers/agent_files.py`
- `src/phaze/routers/agent_proposals.py`
- `src/phaze/routers/agent_execution.py`
- `src/phaze/routers/agent_auth.py`
- `src/phaze/routers/pipeline.py`
- `src/phaze/routers/scan.py`
- `src/phaze/services/agent_client.py`
- `src/phaze/services/agent_task_router.py`
- `src/phaze/services/ingestion.py`
- `src/phaze/services/hashing.py`
- `src/phaze/schemas/agent_files.py`
- `src/phaze/schemas/agent_tasks.py`
- `src/phaze/schemas/agent_proposals.py`
- `src/phaze/schemas/agent_execution.py`
- `src/phaze/config.py`
- `src/phaze/main.py`
- `src/phaze/models/agent.py`
- `src/phaze/models/scan_batch.py`
- `src/phaze/models/file.py`
- `src/phaze/tasks/scan.py`
- `src/phaze/constants.py`
- `src/phaze/templates/pipeline/dashboard.html` + `partials/stage_cards.html` + `partials/stats_bar.html` + `partials/trigger_response.html`
- `src/phaze/templates/tracklists/partials/scan_progress.html` + `partials/status_badge.html`
- `src/phaze/templates/execution/partials/audit_table.html`
- `src/phaze/templates/search/partials/search_form.html`
- `tests/test_task_split.py`
- `tests/test_routers/test_agent_proposals.py`
- `tests/test_routers/test_agent_files.py`
- `tests/test_routers/test_pipeline.py`
- `tests/test_tasks/test_scan.py`
- `tests/conftest.py`
- `docker-compose.yml`
- `pyproject.toml`

**Pattern extraction date:** 2026-05-13

---

*Phase: 27-watcher-service-user-initiated-scan*
*Patterns mapped: 2026-05-13*
