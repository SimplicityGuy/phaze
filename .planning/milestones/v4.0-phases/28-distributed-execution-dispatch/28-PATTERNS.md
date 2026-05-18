# Phase 28: Distributed Execution Dispatch - Pattern Map

**Mapped:** 2026-05-15
**Files analyzed:** 13 new + 11 modified = 24 files
**Analogs found:** 24 / 24 (100% coverage)

## File Classification

### New Files

| New File | Role | Data Flow | Closest Analog | Match Quality |
|----------|------|-----------|----------------|---------------|
| `src/phaze/routers/agent_exec_batches.py` | router (FastAPI agent-internal, bearer auth, cross-tenant guard, Stripe-style request-id idempotency, Redis HINCRBY) | request-response | `src/phaze/routers/agent_tracklists.py` (SET NX EX idempotency + Redis) + `src/phaze/routers/agent_scan_batches.py` (cross-tenant guard ordering + 404→403 sequencing) | exact (composite) |
| `src/phaze/services/execution_dispatch.py` | service (SELECT-and-group helper, revoked-agent filter, chunk into sub-jobs of ≤500) | batch / transform | `src/phaze/services/execution.py:97-113` (`get_approved_proposals`) for the SELECT pattern; `src/phaze/services/agent_task_router.py:74-88` (`enqueue_for_agent`) for the per-agent dispatch primitive | role-match (no exact dispatch grouper exists) |
| `src/phaze/schemas/agent_exec_batches.py` | schema (Pydantic body with `extra="forbid"` + `model_validator(mode="after")` for cross-field `failed_at_step`/`terminal_step` coupling) | request-response | `src/phaze/schemas/agent_proposals.py` (model_validator for conditional field coupling) + `src/phaze/schemas/agent_tracklists.py` (`request_id: UUID` Stripe-style key) | exact |
| `src/phaze/templates/execution/partials/agents_table.html` | template partial (server-rendered HTMX-swap target for SSE `agents_table` event) | event-driven (SSE) | `src/phaze/templates/pipeline/partials/recent_scans_table.html` (table geometry + two-line agent cell + status pill cell) | exact |
| `src/phaze/templates/_partials/cross_fs_fingerprint_notice.html` | template partial (dismissible Alpine.js info banner) | event-driven (client-side state) | `src/phaze/templates/execution/partials/collision_block.html` (banner geometry, HTML-entity icon, semantic `role`); status pill conventions from `templates/pipeline/partials/scan_status_pill.html` | role-match (banner pattern exists; Alpine `x-data` dismissal is NEW to this template surface) |
| `tests/test_routers/test_agent_exec_batches.py` | test (router contract: 401/403/404, idempotent dup, counter math) | request-response | `tests/test_routers/test_agent_scan_batches.py` (smoke-app fixture + cross-tenant 403 + 404 ordering) + `tests/test_routers/test_agent_tracklists.py` (Redis idempotency dup-call test) | exact (composite) |
| `tests/test_routers/test_execution_dispatch.py` | test (router integration: multi-agent grouping, sub-batch chunking, Redis HSET init, revoked filter) | batch + integration | `tests/test_routers/test_pipeline_scans.py` (form router + enqueue mocking + smoke-app pattern) | role-match |
| `tests/test_tasks/test_execute_approved_batch_progress.py` | test (agent task: one progress POST per proposal terminal state, `sub_batch_terminal` on last item) | event-driven | `tests/test_tasks/test_execute_approved_batch.py` (existing per-proposal lifecycle tests) | exact |
| `tests/test_services/test_agent_client_exec_batch_progress.py` | test (respx happy/retry path for `post_exec_batch_progress`) | request-response | `tests/test_services/test_agent_client_endpoints.py` (respx mock per new method, URL assertion, response model validation) | exact |
| `tests/test_services/test_execution_dispatch_grouping.py` | test (pure unit: group-by-agent_id, chunking math, revoked filter) | batch / transform | `tests/test_services/test_agent_task_router.py` (per-agent service unit test) | role-match |
| `tests/test_services/test_fingerprint_locality.py` | test (config-validator rejects non-localhost audfprint/panako URLs) | unit | `tests/test_schemas/test_agent_scan_batches.py` (pydantic ValidationError raises) | role-match |
| `tests/test_template_helpers/test_progress_partial.py` | test (Jinja render: empty / single agent / multi-agent / errors states; banner pluralization) | unit | (no existing template-helper test directory; pattern derived from existing template usage in `tests/test_routers/test_pipeline_scans.py` template assertions) | partial — directory is new |
| `tests/test_schemas/test_agent_exec_batches.py` | test (Pydantic field validation, `extra="forbid"`, model_validator cross-field) | unit | `tests/test_schemas/test_agent_scan_batches.py` (Pydantic schema-validation patterns) | exact |

### Modified Files

| Modified File | Role | Data Flow | Closest Analog (for the NEW behavior) | Match Quality |
|---------------|------|-----------|----------------------------------------|---------------|
| `src/phaze/routers/execution.py` (`start_execution` rewrite + `execution_progress` extend) | router (Jinja+SSE; rewrite dispatch loop + extend SSE event payloads) | request-response → event-driven | `src/phaze/routers/pipeline_scans.py:134-278` (multi-validate → enqueue per agent → template response with first-render context); `src/phaze/routers/agent_files.py:130-162` (auto-enqueue best-effort loop pattern) | exact (the existing per-agent enqueue loop in pipeline_scans + agent_files is the direct shape) |
| `src/phaze/tasks/execution.py` (`_execute_one` + `execute_approved_batch` outer loop) | task (file-bound SAQ; per-proposal terminal POST + sub_batch_terminal flag) | event-driven (HTTP back-call) | Self (existing `_execute_one` is the analog; the change is appending one `api.post_exec_batch_progress(...)` call mirroring the existing `api.patch_proposal_state(...)` shape at `tasks/execution.py:148-155` and `tasks/execution.py:181-188`) | exact (self) |
| `src/phaze/schemas/agent_tasks.py` (`ExecuteApprovedBatchPayload`: add `sub_batch_index: int = 0`) | schema (extend Pydantic with default-zero field for backward compat) | unit | Self (the existing class is the analog; default=0 preserves single-chunk callers) | exact (self) |
| `src/phaze/services/agent_client.py` (`post_exec_batch_progress` method addition) | service (HTTP client; new method mirroring existing `_request` funnel) | request-response | `src/phaze/services/agent_client.py:296-313` (`patch_scan_batch`) — the structural twin (one-method, funnel through `_request`, no response body) | exact (sibling in same file) |
| `src/phaze/config.py` (`@field_validator` on `audfprint_url`, `panako_url`) | config (Pydantic field-level validator) | unit | `src/phaze/config.py:176-188` (`_split_scan_roots` validator); `src/phaze/config.py:190-198` (`model_validator(mode="after")` example) | exact (same file) |
| `src/phaze/main.py` (`app.include_router(agent_exec_batches.router)`) | wiring | unit | `src/phaze/main.py:111-126` (existing agent-internal router include block) | exact |
| `src/phaze/templates/execution/partials/progress.html` (rewrite outer card → table + dispatch summary + revoked banner) | template (HTMX+SSE shell) | event-driven | Self (lines 1-3 are the analog skeleton); structural extension references `collision_block.html` for the revoked-banner geometry and `recent_scans_table.html` for the inline table | exact (self) |
| `src/phaze/templates/duplicates/list.html` (include the banner partial) | template (host page edit) | unit | Self (line 9-10 `{% block content %}` + `space-y-6` div is the insertion point) | exact (self) |
| `PROJECT.md` (Constraints paragraph) | docs | unit | Existing "Key Decisions" rows in PROJECT.md; format is operator-facing markdown paragraph | exact |
| `.planning/STATE.md` (Phase 28 decisions accumulation) | docs | unit | Existing per-phase decision rows | exact |
| `tests/test_task_split.py` (extend with fingerprint-locality assertion) | test (structural import-boundary) | unit | Self | exact (self) |

## Pattern Assignments

---

### `src/phaze/routers/agent_exec_batches.py` (NEW — router, request-response)

**Primary analog A:** `src/phaze/routers/agent_tracklists.py` (Redis SET NX EX idempotency + `_get_redis` dep)
**Primary analog B:** `src/phaze/routers/agent_scan_batches.py` (cross-tenant guard ordering + 4-stage validation)

**Read first:**
- `src/phaze/routers/agent_tracklists.py` lines 1-105 (full Redis idempotency pattern)
- `src/phaze/routers/agent_scan_batches.py` lines 1-118 (full cross-tenant + ordering pattern)
- `src/phaze/routers/agent_execution.py` lines 1-80 (POST/PATCH/idempotency for execution-log — the structural sibling)

**Module-docstring pattern** (lines 1-23 of `agent_scan_batches.py`):
```python
"""POST /api/internal/agent/exec-batches/{batch_id}/progress -- per-proposal terminal-state event (Phase 28 D-05, D-17).

Handler ordering (the ORDER is part of the contract):
  1. 403 if `body.agent_id != agent.id` -- cross-tenant guard BEFORE any state read.
  2. 404 if `exec:{batch_id}` hash doesn't exist (HEXISTS total).
  3. 403 if `agent:<body.agent_id>:total` rollup field absent (caller wasn't in dispatch).
  4. SET NX EX dedup on `exec_progress_req:{request_id}` -- duplicate returns 200 with no HINCRBY.
  5. HINCRBY counters per D-07 rules.
  6. If `sub_batch_terminal`, HINCRBY subjobs_completed and set status if subjobs_completed == subjobs_expected.

This module deliberately omits `from __future__ import annotations` so FastAPI
can resolve `Annotated[redis_async.Redis, Depends(_get_redis)]` at app-build time
(matches the agent_tracklists.py / agent_scan_batches.py convention).
"""
```

**Imports pattern** (verbatim adapt from `agent_tracklists.py:20-33`):
```python
from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
import redis.asyncio as redis_async

from phaze.models.agent import Agent
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_exec_batches import ExecBatchProgressPayload


router = APIRouter(prefix="/api/internal/agent/exec-batches", tags=["agent-internal"])
```

**Redis dependency-injector** (verbatim from `agent_tracklists.py:45-53`):
```python
async def _get_redis(request: Request) -> redis_async.Redis:
    redis_client: redis_async.Redis = request.app.state.redis
    return redis_client
```

**Cross-tenant guard pattern** (mirrors `agent_scan_batches.py:77-84`):
```python
# 1. Cross-tenant guard runs BEFORE any state read (Phase 26 D-08 timing-side-channel pattern).
if body.agent_id != agent.id:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="agent_id in body does not match authenticated agent",
    )
```

**4-stage validation** (composes `agent_scan_batches.py:72-110` ordering with RESEARCH §"Example: New POST endpoint handler skeleton" lines 928-966):
```python
# 2. 404 if batch unknown (HEXISTS replaces session.get(...))
if not await redis_client.hexists(key, "total"):
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="batch not found")

# 3. 403 if agent not part of this dispatch (per-agent rollup field absent — D-17 step 4)
if not await redis_client.hexists(key, f"agent:{body.agent_id}:total"):
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="agent was not part of this dispatch")
```

**SET NX EX idempotency** (adapt from `agent_tracklists.py:84-104`):
```python
# 4. Stripe-style request-id dedup. Duplicate POST returns 200 with no HINCRBY.
req_key = f"exec_progress_req:{body.request_id}"
won = await redis_client.set(req_key, "1", nx=True, ex=3600)
if not won:
    return Response(status_code=200)
```

**Counter increments** (verbatim from RESEARCH lines 906-925 — D-07 rules):
```python
def _compute_increments(body: ExecBatchProgressPayload) -> dict[str, int]:
    """D-07 counter update rules. Returns the HINCRBY dict for this progress event."""
    agent_id = body.agent_id
    if body.terminal_step == "deleted":
        return {
            "copied": 1, "verified": 1, "deleted": 1, "completed": 1,
            f"agent:{agent_id}:completed": 1,
        }
    if body.terminal_step == "verified":
        return {"copied": 1, "verified": 1}
    if body.terminal_step == "copied":
        return {"copied": 1}
    # failed
    inc: dict[str, int] = {"failed": 1, f"agent:{agent_id}:failed": 1}
    if body.failed_at_step == "verify":
        inc["copied"] = 1
    elif body.failed_at_step == "delete":
        inc["copied"] = 1
        inc["verified"] = 1
    return inc
```

**Pipelined HINCRBY** (RESEARCH lines 951-966):
```python
async with redis_client.pipeline(transaction=False) as pipe:
    for field, by in _compute_increments(body).items():
        await pipe.hincrby(key, field, by)
    if body.sub_batch_terminal:
        await pipe.hincrby(key, "subjobs_completed", 1)
    await pipe.execute()

if body.sub_batch_terminal:
    sc = int(await redis_client.hget(key, "subjobs_completed") or 0)
    se = int(await redis_client.hget(key, "subjobs_expected") or 0)
    if sc == se:
        failed = int(await redis_client.hget(key, "failed") or 0)
        await redis_client.hset(key, "status", "complete" if failed == 0 else "complete_with_errors")

return Response(status_code=200)
```

**Variation notes:**
- Unlike `agent_scan_batches.py` (DB-backed batch), this endpoint's "batch" is the **Redis hash** `exec:{batch_id}` (no Postgres row). The 404 is `HEXISTS key "total"`, NOT `session.get(...)`.
- Unlike `agent_tracklists.py` (cache the response under `tracklist_resp:`), this endpoint has **no response body** — duplicates return `Response(status_code=200)` directly (RESEARCH L13). No need for a resp_key cache.
- The 4th validation stage (HEXISTS `agent:<id>:total`) is novel — see L19 in RESEARCH; rationale is the per-agent rollup field is **pre-set at dispatch time** (D-09 step 5) so its absence is structural cross-tenant proof.

---

### `src/phaze/services/execution_dispatch.py` (NEW — service, batch / transform)

**Primary analog:** `src/phaze/services/agent_task_router.py:74-88` (the `enqueue_for_agent` primitive Phase 28 calls in a loop) + `src/phaze/services/execution.py:97-113` (`get_approved_proposals` SELECT + selectinload pattern)

**Read first:**
- `src/phaze/services/execution.py:97-113` (existing approved-proposal SELECT)
- `src/phaze/services/agent_task_router.py:74-102` (per-agent enqueue primitive)
- `src/phaze/routers/pipeline_scans.py:243-266` (existing per-agent enqueue + rollback-on-fail pattern)
- `src/phaze/models/file.py` lines 47-73 for FileRecord.agent_id column shape
- `src/phaze/models/agent.py` lines 20-30 for Agent.revoked_at

**SELECT-with-join pattern** (extend `services/execution.py:97-113`):
```python
async def get_approved_proposals_grouped_by_agent(
    session: AsyncSession,
) -> dict[str, list[ExecuteBatchProposalItem]]:
    """Phase 28 D-09 step 1: SELECT approved proposals JOIN FileRecord, group by agent_id.

    Filters out proposals whose Agent.revoked_at IS NOT NULL (D-09 step 2).
    Returns dict[agent_id, list[ExecuteBatchProposalItem]] for direct enqueue use.
    """
    stmt = (
        select(RenameProposal, FileRecord)
        .join(FileRecord, RenameProposal.file_id == FileRecord.id)
        .join(Agent, FileRecord.agent_id == Agent.id)
        .where(
            RenameProposal.status == ProposalStatus.APPROVED,
            Agent.revoked_at.is_(None),  # mirrors agent_auth.py:80 idiom
        )
        .options(selectinload(RenameProposal.file))
        .order_by(FileRecord.agent_id, RenameProposal.created_at)
    )
    # ... group into dict[agent_id, list[ExecuteBatchProposalItem]] ...
```

**Revoked-agent filter pattern** (mirrors `routers/pipeline_scans.py:179-186`):
```python
# Defensive server-side filter -- mirrors the Phase 27 D-06 pattern.
# Returns (groups, skipped_count_per_agent) so the controller can surface
# the revoked-agents banner.
```

**Chunking pattern** (NEW; no codebase analog — derive from CONTEXT D-09 step 3):
```python
_CHUNK_SIZE = 500  # matches ExecuteApprovedBatchPayload.proposals max_length

def chunk_proposals(items: list[ExecuteBatchProposalItem], size: int = _CHUNK_SIZE) -> list[list[ExecuteBatchProposalItem]]:
    return [items[i : i + size] for i in range(0, len(items), size)]
```

**Variation notes:**
- No existing service does SELECT-and-group-by-foreign-key. Closest pattern is `proposal_queries.py:104-130` which uses `selectinload(RenameProposal.file)` + filtering — pattern reused, behavior is new.
- Agent.revoked_at filter idiom comes from `routers/agent_auth.py:80` — `Agent.revoked_at.is_(None)` (NOT `== None`).

---

### `src/phaze/schemas/agent_exec_batches.py` (NEW — schema, request-response)

**Primary analog:** `src/phaze/schemas/agent_proposals.py` (model_validator cross-field coupling) + `src/phaze/schemas/agent_tracklists.py` (request_id: UUID idempotency key + ConfigDict extra="forbid")

**Read first:**
- `src/phaze/schemas/agent_proposals.py` lines 1-51 (model_validator(mode="after") example)
- `src/phaze/schemas/agent_tracklists.py` lines 35-52 (request_id: UUID + extra="forbid")
- `src/phaze/schemas/agent_tasks.py` lines 88-118 (Literal types + Field constraints — sibling payload patterns)

**ConfigDict + extra="forbid"** (verbatim convention from `agent_proposals.py:24`):
```python
model_config = ConfigDict(extra="forbid")
```

**model_validator cross-field pattern** (mirrors `agent_proposals.py:31-41`):
```python
@model_validator(mode="after")
def _check_failed_at_step_coupling(self) -> "ExecBatchProgressPayload":
    if self.terminal_step == "failed" and self.failed_at_step is None:
        msg = "failed_at_step is required when terminal_step='failed'"
        raise ValueError(msg)
    if self.terminal_step != "failed" and self.failed_at_step is not None:
        msg = "failed_at_step must be null when terminal_step != 'failed'"
        raise ValueError(msg)
    return self
```

**Full schema** (verbatim from RESEARCH §"Example: ExecBatchProgressPayload with cross-field validator" lines 993-1032):
```python
class ExecBatchProgressPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID
    batch_id: uuid.UUID
    agent_id: str
    sub_batch_index: int
    proposal_id: uuid.UUID
    terminal_step: Literal["copied", "verified", "deleted", "failed"]
    failed_at_step: Literal["copy", "verify", "delete"] | None = None
    sub_batch_terminal: bool = False
```

**Variation notes:**
- `agent_id: str` (not UUID) — matches the slug pattern from `models/agent.py` and `agent_task_router.py:65` (`phaze-agent-<agent_id>` queue name).
- `request_id` UUID idempotency key mirrors `agent_tracklists.py:44` (`request_id: uuid.UUID`).
- This schema is **request-only**; no response model needed (handler returns `Response(status_code=200)` per RESEARCH L13).

---

### `src/phaze/templates/execution/partials/agents_table.html` (NEW — template, event-driven SSE)

**Primary analog:** `src/phaze/templates/pipeline/partials/recent_scans_table.html` (table geometry + agent cell + status pill cell)
**Secondary analog:** `src/phaze/templates/pipeline/partials/scan_status_pill.html` (pill geometry — verbatim re-use)

**Read first:**
- `src/phaze/templates/pipeline/partials/recent_scans_table.html` lines 22-60 (whole table structure)
- `src/phaze/templates/pipeline/partials/scan_status_pill.html` lines 1-12 (pill geometry)
- `.planning/phases/28-distributed-execution-dispatch/28-UI-SPEC.md` §"C2 — Per-Agent Table" (the contract)

**Outer container + table head** (mirrors `recent_scans_table.html:22-33`):
```html
<div class="overflow-x-auto">
    <table class="w-full text-sm text-left">
        <caption class="sr-only">Per-agent execution progress</caption>
        <thead class="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase border-b border-gray-200 dark:border-phaze-border">
            <tr>
                <th scope="col" class="px-4 py-3">Agent</th>
                <th scope="col" class="px-4 py-3">Status</th>
                <th scope="col" class="px-4 py-3">Completed</th>
                <th scope="col" class="px-4 py-3">Failed</th>
                <th scope="col" class="px-4 py-3">Total</th>
            </tr>
        </thead>
```

**Two-line agent cell** (mirrors `recent_scans_table.html:37` + UI-SPEC C2):
```html
<td class="px-4 py-3">
    <span class="text-sm font-semibold text-gray-900 dark:text-gray-100 block">{{ agent.name }}</span>
    <span class="font-mono text-xs text-gray-500 dark:text-gray-400 block">{{ agent.id }}</span>
</td>
```

**Status pill** (verbatim re-use of `scan_status_pill.html:5-11` geometry, extend for `PENDING`/`ERRORS` per UI-SPEC):
```html
{% if completed + failed == 0 %}
<span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400" aria-label="Status: pending">PENDING</span>
{% elif completed + failed < total %}
<span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-blue-100 dark:bg-blue-950 text-blue-700 dark:text-blue-400" aria-label="Status: running">RUNNING</span>
{% elif failed == 0 %}
<span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-green-100 dark:bg-green-950 text-green-700 dark:text-green-400" aria-label="Status: complete">COMPLETE</span>
{% else %}
<span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-red-100 dark:bg-red-950 text-red-700 dark:text-red-400" aria-label="Status: errors">ERRORS</span>
{% endif %}
```

**Variation notes:**
- Five columns instead of `recent_scans_table.html`'s six (no "Path" / "Elapsed" — Phase 28 doesn't surface those).
- Status pill ladder is FOUR states (`PENDING / RUNNING / COMPLETE / ERRORS`); the analog only has three (`RUNNING / COMPLETED / FAILED`). The new `PENDING` + `ERRORS` cases extend the analog's pattern.
- This partial is **also the SSE event payload** — the controller's SSE generator renders it on every poll tick and emits it as `event: agents_table`.

---

### `src/phaze/templates/_partials/cross_fs_fingerprint_notice.html` (NEW — template, event-driven dismissal)

**Primary analog:** `src/phaze/templates/execution/partials/collision_block.html` (banner geometry, HTML-entity icon, role="alert"/"status")

**Read first:**
- `src/phaze/templates/execution/partials/collision_block.html` lines 1-16 (full warning banner)
- `.planning/phases/28-distributed-execution-dispatch/28-UI-SPEC.md` §"C3 — Cross-FS-Fingerprint Notice" (the contract)
- `src/phaze/templates/base.html` (Alpine.js CDN — already loaded per UI-SPEC)

**Banner container + Alpine state** (extends `collision_block.html:1` geometry to blue surface; UI-SPEC C3):
```html
<div
    x-data="{ open: true }"
    x-show="open"
    role="status"
    class="rounded-lg border border-blue-200 dark:border-blue-900 bg-blue-50 dark:bg-blue-950/30 p-4 flex items-start gap-4"
>
```

**Icon column** (mirrors `collision_block.html:3` HTML-entity convention; UI-SPEC swaps `&#9888;` → `&#9432;` info glyph):
```html
<span class="text-blue-600 dark:text-blue-400 text-lg leading-none mt-0.5">&#9432;</span>
```

**Body column** (extends `collision_block.html:4-7` heading+paragraph shape):
```html
<div class="flex-1 min-w-0">
    <h3 class="text-sm font-semibold text-gray-900 dark:text-gray-100">Fingerprint matches are file-server-scoped</h3>
    <p class="text-sm text-gray-700 dark:text-gray-300 mt-1">
        Each file server indexes only its own files. A duplicate file landing on one file server will not match an existing copy on another. Cross-file-server fingerprint matching is not supported in v4.0.
        <a href="#" class="text-blue-600 dark:text-blue-400 hover:underline" title="See PROJECT.md">Learn more</a>.
    </p>
</div>
```

**Dismiss button** (NEW pattern — no existing dismissible banner in codebase):
```html
<button @click="open = false" type="button" aria-label="Dismiss notice"
        class="text-gray-400 hover:text-gray-600 dark:text-gray-500 dark:hover:text-gray-300 text-xl leading-none p-1">
    &times;
</button>
```

**Variation notes:**
- `_partials/` directory does NOT exist yet — plan MUST create it.
- `role="status"` (informational) vs `collision_block.html`'s `role="alert"` (urgent) — UI-SPEC C3 explicitly chose `status` because the limitation is by-design, not a problem.
- Alpine.js `x-data="{ open: true }"` is in-memory only — **NO `localStorage`** per CONTEXT D-14. Re-appears on reload.
- HTML-entity icon convention (`&#9432;` for info, `&#9888;` for warning) is the project pattern; do not use SVG.

---

### `src/phaze/routers/execution.py` — `start_execution` REWRITE (modified — router, request-response)

**Current shape** (`routers/execution.py:31-53`):
```python
@router.post("/execution/start", response_class=HTMLResponse)
async def start_execution(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    collisions = await detect_collisions(session)
    if collisions:
        return templates.TemplateResponse(
            request=request,
            name="execution/partials/collision_block.html",
            context={"request": request, "collisions": collisions},
        )
    queue = request.app.state.queue
    batch_id = uuid4().hex
    await queue.enqueue("execute_approved_batch", batch_id=batch_id)
    return templates.TemplateResponse(
        request=request,
        name="execution/partials/progress.html",
        context={"request": request, "batch_id": batch_id},
    )
```

**Primary analog for the rewrite:** `src/phaze/routers/pipeline_scans.py:134-278` (multi-validate → per-agent enqueue → progress template) + `src/phaze/routers/agent_files.py:130-162` (auto-enqueue best-effort loop)

**Read first:**
- `src/phaze/routers/pipeline_scans.py:134-278` (full trigger_scan rewrite as the dispatch template)
- `src/phaze/services/agent_task_router.py:74-88` (`enqueue_for_agent` primitive)
- `src/phaze/services/execution.py:97-113` (`get_approved_proposals` current SELECT shape — Phase 28 replaces with the new dispatch service helper)

**Rewrite sequence** (matches CONTEXT D-09 steps 1-7):
```python
@router.post("/execution/start", response_class=HTMLResponse)
async def start_execution(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    # 0. Collision pre-check stays at the top (CONTEXT specifics line 265).
    collisions = await detect_collisions(session)
    if collisions:
        return templates.TemplateResponse(...)  # unchanged

    # 1. SELECT + group + filter revoked (NEW service helper).
    groups, skipped = await get_approved_proposals_grouped_by_agent(session)

    # 2. Generate parent batch_id.
    batch_id = uuid4()

    # 3. Compute subjobs_expected and chunk per agent.
    subjobs_expected = sum(math.ceil(len(items) / 500) for items in groups.values())
    total = sum(len(items) for items in groups.values())

    # 4. Initialize exec:{batch_id} Redis hash (HSET + EXPIRE) — see D-09 step 5.
    redis_client = request.app.state.redis
    init_fields = {"total": total, "subjobs_expected": subjobs_expected, "subjobs_completed": 0,
                   "completed": 0, "failed": 0, "copied": 0, "verified": 0, "deleted": 0,
                   "status": "running", "started_at": datetime.now(UTC).isoformat()}
    for agent_id, items in groups.items():
        init_fields[f"agent:{agent_id}:total"] = len(items)
        init_fields[f"agent:{agent_id}:completed"] = 0
        init_fields[f"agent:{agent_id}:failed"] = 0
    init_fields["dispatch_summary"] = json.dumps([{"agent_id": a, "chunks": math.ceil(len(items)/500), "total": len(items)} for a, items in groups.items()])
    await redis_client.hset(f"exec:{batch_id}", mapping=init_fields)
    await redis_client.expire(f"exec:{batch_id}", 86400)

    # 5. Per-agent + per-chunk enqueue loop (mirrors pipeline_scans.py:243-266 best-effort pattern).
    task_router = request.app.state.task_router
    for agent_id, items in groups.items():
        for chunk_index, chunk in enumerate(chunk_proposals(items)):
            try:
                await task_router.enqueue_for_agent(
                    agent_id=agent_id,
                    task_name="execute_approved_batch",
                    payload=ExecuteApprovedBatchPayload(
                        batch_id=batch_id, agent_id=agent_id, proposals=chunk, sub_batch_index=chunk_index,
                    ),
                )
            except Exception:
                logger.exception("dispatch: enqueue failed for agent=%s chunk=%s", agent_id, chunk_index)

    # 6. INFO log per D-11.
    logger.info("dispatch batch_id=%s total=%d n_agents=%d subjobs_expected=%d", batch_id, total, len(groups), subjobs_expected)

    # 7. Return progress partial with first-render context.
    return templates.TemplateResponse(
        request=request,
        name="execution/partials/progress.html",
        context={"request": request, "batch_id": str(batch_id), "groups": groups, "skipped_revoked": skipped, "total": total, "subjobs_expected": subjobs_expected},
    )
```

**Variation notes:**
- Use `request.app.state.redis` (the Phase 26 D-27 shared Redis client with `decode_responses=True`) — NOT `queue.redis` (the SAQ-internal Redis). Existing `routers/execution.py:46` uses `queue.redis`; Phase 28 switches to `app.state.redis` because SET NX EX needs decode_responses.
- `batch_id` is now `uuid4()` returning UUID, not `uuid4().hex` — schemas require UUID type.

---

### `src/phaze/routers/execution.py` — `execution_progress` SSE EXTEND (modified — router, event-driven)

**Current shape** (`routers/execution.py:56-88`): existing SSE generator with 1-second poll and HGETALL.

**Read first:**
- `src/phaze/routers/execution.py:56-88` (current SSE generator — keep the structure)
- `.planning/phases/28-distributed-execution-dispatch/28-UI-SPEC.md` §"Interaction Contracts → SSE event handling" (event types contract)

**Extension pattern** (CONTEXT D-08; UI-SPEC §"SSE event handling"):
```python
async def event_generator() -> AsyncGenerator[dict[str, str]]:
    first_connect = True  # RESEARCH L15 — emit dispatch_summary once
    while True:
        data = await queue.redis.hgetall(f"exec:{batch_id}")
        if not data:
            yield {"event": "progress", "data": "Waiting for execution to start..."}
        else:
            decoded = {...}  # existing bytes-decode pattern at line 67-68

            # NEW: dispatch_summary on first connect (UI-SPEC L18; RESEARCH L4)
            if first_connect and "dispatch_summary" in decoded:
                dispatch_summary = json.loads(decoded["dispatch_summary"])
                yield {"event": "dispatch_summary", "data": render_dispatch_summary_html(dispatch_summary, total)}
                first_connect = False

            # Existing aggregate counter event
            yield {"event": "progress", "data": render_aggregate_html(decoded)}

            # NEW: per-agent table event on every tick (UI-SPEC C2)
            yield {"event": "agents_table", "data": render_agents_table_html(decoded)}

            # Extend close-on-terminal — existing line 74 becomes:
            if status in {"complete", "complete_with_errors"}:
                yield {"event": status, "data": render_terminal_message_html(decoded)}
                return

        await asyncio.sleep(1)
```

**Variation notes:**
- The existing `if status == "complete"` check at `routers/execution.py:74` widens to `if status in {"complete", "complete_with_errors"}` per CONTEXT specifics line 264.
- The existing HGETALL decode pattern at line 67-68 is preserved verbatim — CONTEXT specifics line 257 explicitly notes "no new decode logic."
- Two new SSE events (`dispatch_summary`, `agents_table`) — each must match an `sse-swap=` target in `progress.html`.

---

### `src/phaze/tasks/execution.py` — `_execute_one` + outer loop EXTEND (modified — task, event-driven HTTP back-call)

**Current shape** (`tasks/execution.py:74-198`): per-proposal lifecycle that already calls `api.patch_proposal_state(...)` at terminal state (lines 148-155 success, 181-188 failure).

**Read first:**
- `src/phaze/tasks/execution.py:74-198` (whole `_execute_one`)
- `src/phaze/tasks/execution.py:200-234` (`execute_approved_batch` outer loop)

**Insertion pattern — success path** (mirrors the existing `patch_proposal_state` call site at line 148-155):
```python
# After existing line 155 (api.patch_proposal_state success):
await api.post_exec_batch_progress(
    batch_id=payload.batch_id,
    payload=ExecBatchProgressPayload(
        request_id=progress_request_id,
        batch_id=payload.batch_id,
        agent_id=payload.agent_id,
        sub_batch_index=payload.sub_batch_index,
        proposal_id=item.proposal_id,
        terminal_step="deleted",
        sub_batch_terminal=is_last,
    ),
)
return True
```

**Insertion pattern — failure path** (mirrors line 181-188):
```python
# After existing line 188 (api.patch_proposal_state failure):
await api.post_exec_batch_progress(
    batch_id=payload.batch_id,
    payload=ExecBatchProgressPayload(
        request_id=progress_request_id,
        batch_id=payload.batch_id,
        agent_id=payload.agent_id,
        sub_batch_index=payload.sub_batch_index,
        proposal_id=item.proposal_id,
        terminal_step="failed",
        failed_at_step=_classify_failure_step(exc),  # new helper; see L9 RESEARCH
        sub_batch_terminal=is_last,
    ),
)
return False
```

**request_id generation** (mirrors `execution_log_id = uuid.uuid4()` at line 89):
```python
# At line 89 (next to execution_log_id):
progress_request_id = uuid.uuid4()  # Phase 28 D-15 — persisted in SAQ state for retry idempotency
```

**`<step>: <reason>` error message prefix** (CONTEXT D-01; replaces existing `str(exc)[:500]` at line 170):
```python
# Replace at line 170:
error_message=f"{_classify_failure_step(exc)}: {exc!s}"[:500],
```

**Outer loop `sub_batch_terminal`** (extend `execute_approved_batch` at line 220):
```python
for idx, item in enumerate(payload.proposals):
    is_last = idx == len(payload.proposals) - 1
    ok = await _execute_one(api, item, scan_roots, payload, is_last)  # signature widens
```

**Variation notes:**
- Per L6/L22 in RESEARCH: planner MUST surface to the user that `progress_request_id` (and `execution_log_id`) need to be persisted in SAQ job state (`ctx['job'].meta`) to survive retries. RESEARCH L23 flags this needs `mcp__context7__get-library-docs` verification on SAQ.
- New helper `_classify_failure_step(exc)` (D-07 line 59 + RESEARCH L9): classifies copy/verify/delete based on exception type. New private function in `tasks/execution.py`.
- `_execute_one` signature widens to accept `payload: ExecuteApprovedBatchPayload, is_last: bool` (or just the fields it needs: `batch_id`, `agent_id`, `sub_batch_index`, `is_last`).

---

### `src/phaze/schemas/agent_tasks.py` — `ExecuteApprovedBatchPayload` EXTEND (modified — schema, unit)

**Read first:** `src/phaze/schemas/agent_tasks.py:105-118` (current class — single-line addition only).

**Addition** (CONTEXT D-10 — default=0 preserves single-chunk callers):
```python
class ExecuteApprovedBatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: uuid.UUID
    agent_id: str
    proposals: list[ExecuteBatchProposalItem] = Field(min_length=1, max_length=500)
    sub_batch_index: int = 0  # Phase 28 D-10 -- 0-based; default preserves legacy callers
```

**Variation notes:**
- `extra="forbid"` is already set — so this is a **wire-format change**; any Phase 26 caller that sets `sub_batch_index=0` explicitly is forward-compatible, and any caller that omits it still works (default=0).
- 0-based per CONTEXT Discretion line 121.

---

### `src/phaze/services/agent_client.py` — `post_exec_batch_progress` method (modified — service, request-response)

**Primary analog:** `src/phaze/services/agent_client.py:296-313` (`patch_scan_batch` — structural twin: one method, funnel through `_request`, no response model).

**Read first:**
- `src/phaze/services/agent_client.py:296-313` (`patch_scan_batch` — the exact shape to mirror)
- `src/phaze/services/agent_client.py:315-322` (`heartbeat` — the no-response-body shape)

**Method pattern** (verbatim from RESEARCH §"Example: New PhazeAgentClient method" lines 969-991):
```python
async def post_exec_batch_progress(
    self,
    batch_id: uuid.UUID,
    payload: ExecBatchProgressPayload,
) -> None:
    """POST /api/internal/agent/exec-batches/{batch_id}/progress -- per-proposal terminal progress (Phase 28 D-05).

    Inherits the tenacity retry policy (D-11) + exception hierarchy (D-12) via
    the `_request` funnel -- 5xx retries, 4xx surface immediately.
    """
    await self._request(
        "POST",
        f"/api/internal/agent/exec-batches/{batch_id}/progress",
        json=payload.model_dump(mode="json"),
    )
```

**TYPE_CHECKING import addition** (next to `agent_client.py:57-65`):
```python
# Phase 28 schema import in TYPE_CHECKING block:
from phaze.schemas.agent_exec_batches import ExecBatchProgressPayload
```

**Variation notes:**
- Returns `None` (matches `heartbeat()` at line 315-322 which is the existing no-response-body sibling). No `model_validate` call on the response.
- All retry/error-mapping inherited from `_request` funnel at `agent_client.py:138-182` — Phase 28 adds zero new error-handling code.

---

### `src/phaze/config.py` — `@field_validator` on audfprint_url, panako_url (modified — config, unit)

**Primary analog:** `src/phaze/config.py:176-188` (`_split_scan_roots` `@field_validator(mode="before")`) + `src/phaze/config.py:190-198` (`@model_validator(mode="after")`)

**Read first:**
- `src/phaze/config.py:60-62` (current `audfprint_url`, `panako_url` fields — defaults pointing at Docker service names)
- `src/phaze/config.py:176-198` (existing validator examples in `AgentSettings`)
- `src/phaze/services/fingerprint.py:84-87` (`AudfprintAdapter.__init__(base_url=...)` — confirms which field flows where)

**Validator pattern** (extends `BaseSettings` at line 60-62 — add after the field definitions):
```python
@field_validator("audfprint_url", "panako_url")
@classmethod
def _enforce_localhost_only(cls, value: str) -> str:
    """Phase 28 D-12 / TASK-04: fingerprint sidecars MUST be local to the agent's file server.

    Per XAGENT-01 (deferred): cross-file-server fingerprint matching is not
    supported in v4.0. Each file server's audfprint+panako indices contain
    only that file server's files. Reject any URL whose host isn't
    127.0.0.1 / localhost / a Docker-compose service name on the agent's
    private network. Default values (`http://audfprint:8001`,
    `http://panako:8002`) are accepted because they resolve via the agent
    container's compose network — never cross-host.
    """
    from urllib.parse import urlparse
    parsed = urlparse(value)
    allowed_hosts = {"localhost", "127.0.0.1", "audfprint", "panako"}
    if parsed.hostname not in allowed_hosts:
        raise ValueError(
            f"audfprint_url/panako_url must point to localhost or a Docker-compose service "
            f"on the agent's network (got host={parsed.hostname!r}). "
            f"Cross-file-server fingerprint matching is not supported in v4.0 (see XAGENT-01)."
        )
    return value
```

**Variation notes:**
- The current `audfprint_url` / `panako_url` defaults at lines 60-62 (`"http://audfprint:8001"`, `"http://panako:8002"`) are Docker-compose service hostnames — these MUST be in the allow-list.
- Per RESEARCH L20: if these fields later move to `AgentSettings`, the validator must follow.
- The validator lives on `BaseSettings` (lines 26-92) where the fields currently live — NOT on the subclasses.

---

### `src/phaze/main.py` — `app.include_router(agent_exec_batches.router)` (modified — wiring)

**Read first:**
- `src/phaze/main.py:111-126` (existing agent-internal router include block)

**Addition** (verbatim follow of `main.py:111-126` pattern):
```python
# In create_app(), after line 122 (agent_scan_batches.router):
# Phase 28 internal-agent router (D-05): per-proposal progress reporting.
app.include_router(agent_exec_batches.router)
```

**Import addition** (extend the import block at lines 15-39):
```python
from phaze.routers import (
    agent_analysis,
    agent_exec_batches,  # NEW Phase 28
    agent_execution,
    ...
)
```

---

### `src/phaze/templates/execution/partials/progress.html` — REWRITE (modified — template, event-driven SSE)

**Current shape** (3 lines — outer card + counter span + sse-close span). UI-SPEC C1 specifies the full rewrite.

**Read first:**
- `src/phaze/templates/execution/partials/progress.html` lines 1-4 (current entire file)
- `src/phaze/templates/execution/partials/collision_block.html` lines 1-16 (geometry for the new revoked-banner inline block — UI-SPEC C4)
- `.planning/phases/28-distributed-execution-dispatch/28-UI-SPEC.md` §"C1 — Progress Card" and §"C4 — Revoked-Agents Banner" (the contract)

**Outer card preserved** (UI-SPEC C1 — verbatim from current line 1):
```html
<div class="bg-gray-50 dark:bg-phaze-panel rounded-lg p-6 border border-gray-200 dark:border-phaze-border"
     hx-ext="sse" sse-connect="/execution/progress/{{ batch_id }}" aria-live="polite">
```

**Revoked banner block** (UI-SPEC C4 — verbatim re-use of `collision_block.html:1` geometry):
```html
{% if skipped_revoked %}
<div role="alert" class="bg-orange-50 dark:bg-orange-950/30 border border-orange-200 dark:border-orange-900 rounded-lg p-4 mb-4">
    <div class="flex items-center gap-2">
        <span class="text-orange-600 dark:text-orange-400">&#9888;</span>
        <h3 class="text-sm font-semibold text-orange-800 dark:text-orange-300">Some proposals skipped</h3>
    </div>
    ...
</div>
{% endif %}
```

**Dispatch summary swap target** (UI-SPEC C1 step 2):
```html
<span sse-swap="dispatch_summary" class="text-xl font-semibold text-gray-800 dark:text-gray-200 mb-4 block">
    Dispatched {{ total }} proposals across {{ groups|length }} agent{{ 's' if groups|length != 1 else '' }} ({{ subjobs_expected }} sub-job{{ 's' if subjobs_expected != 1 else '' }})
</span>
```

**Aggregate counter row** (UI-SPEC C1 step 3 — preserves existing `sse-swap="progress"` event):
```html
<span sse-swap="progress" class="flex items-baseline gap-8">
    <!-- TOTAL / COMPLETED / FAILED labeled values -->
</span>
```

**Agents table inclusion** (UI-SPEC C1 step 4):
```html
<div sse-swap="agents_table" class="mt-6 block">
    {% include "execution/partials/agents_table.html" %}
</div>
```

**Dual sse-close** (UI-SPEC C1 step 5):
```html
<span sse-swap="complete" sse-close="complete"></span>
<span sse-swap="complete_with_errors" sse-close="complete_with_errors"></span>
```

**Variation notes:**
- The `sse-swap="progress"` event name is PRESERVED for backward compatibility (CONTEXT specifics line 264).
- New event names `dispatch_summary` and `agents_table` correspond to new SSE emissions in `routers/execution.py`.

---

### `src/phaze/templates/duplicates/list.html` — INCLUDE banner partial (modified — template, host edit)

**Read first:**
- `src/phaze/templates/duplicates/list.html` lines 9-21 (current `{% block content %}`)
- UI-SPEC C3 ("Included from: ... `<h1>`")

**Insertion** (inside the `space-y-6` div, immediately before `<h1>` at line 11):
```html
{% block content %}
<div class="space-y-6">
    {% include "_partials/cross_fs_fingerprint_notice.html" %}

    <h1 class="text-2xl font-semibold ...">Duplicate Resolution</h1>
    ...
```

**Variation notes:**
- The Tailwind `space-y-6` class on the parent div automatically applies vertical spacing between the new banner and the existing `<h1>` — no `mb-N` needed on the banner itself.
- L19 in RESEARCH flags this insertion point as needing user confirmation; planner should explicitly ask.

---

### `tests/test_routers/test_agent_exec_batches.py` (NEW — test, contract)

**Primary analogs:**
- `tests/test_routers/test_agent_scan_batches.py` (smoke-app fixture, cross-tenant 403 test, 404 test, ordering tests)
- `tests/test_routers/test_agent_tracklists.py` (Redis-backed idempotency dup-call test)

**Read first:**
- `tests/test_routers/test_agent_scan_batches.py` lines 1-80 (smoke-app fixture + cross-tenant test shape)
- `tests/test_routers/test_agent_tracklists.py` (full file for idempotency-dup test pattern)
- `tests/test_routers/conftest.py` (if it exists) for `seed_test_agent` fixture

**Smoke-app fixture pattern** (verbatim from `test_agent_scan_batches.py:34-44`):
```python
def _make_smoke_app(session: AsyncSession, redis_client: redis_async.Redis) -> FastAPI:
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_exec_batches.router)
    app.dependency_overrides[get_session] = lambda: session
    app.state.redis = redis_client  # Phase 28 — handler depends on app.state.redis
    return app
```

**Test cases to cover** (CONTEXT D-18 + RESEARCH §"Phase Requirements → Test Map"):
1. `test_unauthenticated_401` — no bearer token.
2. `test_cross_tenant_agent_id_mismatch_403` — body.agent_id != auth agent.id.
3. `test_unknown_batch_404` — exec:{batch_id} hash absent.
4. `test_non_participating_agent_403` — agent:<id>:total field absent.
5. `test_duplicate_request_id_does_not_re_increment` — idempotency dup.
6. Counter-math branches: 4 terminal_step values × 3 failed_at_step paths.
7. `test_sub_batch_terminal_promotes_status_complete` — terminal status update.

---

### `tests/test_services/test_agent_client_exec_batch_progress.py` (NEW — test, request-response)

**Primary analog:** `tests/test_services/test_agent_client_endpoints.py` lines 1-70 (respx happy-path per new method).

**Read first:**
- `tests/test_services/test_agent_client_endpoints.py` lines 1-70 (fixture + first respx test)

**Pattern** (mirrors lines 38-70):
```python
@respx.mock
async def test_post_exec_batch_progress_posts_to_correct_url(client):
    from phaze.schemas.agent_exec_batches import ExecBatchProgressPayload

    batch_id = uuid.uuid4()
    route = respx.post(f"{_BASE_URL}/api/internal/agent/exec-batches/{batch_id}/progress").mock(
        return_value=httpx.Response(200, json={}),
    )

    payload = ExecBatchProgressPayload(...)
    result = await client.post_exec_batch_progress(batch_id, payload)

    assert route.called
    assert result is None  # no response model — mirrors heartbeat()
```

---

### `tests/test_tasks/test_execute_approved_batch_progress.py` (NEW — test, agent-side task)

**Primary analog:** `tests/test_tasks/test_execute_approved_batch.py` (existing per-proposal lifecycle tests).

**Read first:**
- `tests/test_tasks/test_execute_approved_batch.py` (whole file — patch_proposal_state mock setup is the analog)

**Test cases:**
1. `test_success_emits_one_deleted_progress_post` — verify `api.post_exec_batch_progress` called once with `terminal_step="deleted"`.
2. `test_failure_emits_failed_progress_post` — `terminal_step="failed"` + correct `failed_at_step`.
3. `test_sub_batch_terminal_set_on_last_item` — only last proposal gets `sub_batch_terminal=true`.
4. `test_request_id_persisted_per_proposal` — unique UUID per proposal, stable across SAQ retry.

---

### `tests/test_services/test_execution_dispatch_grouping.py` (NEW — test, unit)

**Primary analog:** `tests/test_services/test_agent_task_router.py` (per-agent service unit test).

**Read first:**
- `tests/test_services/test_agent_task_router.py` (whole file — fixture + per-agent assertion pattern)

**Test cases:**
1. `test_groups_by_agent_id` — mixed-agent input → correct per-agent dict.
2. `test_revoked_agent_filtered_with_count` — revoked agent's proposals → skipped, count returned.
3. `test_1000_proposals_split_into_2_chunks` — chunking math.
4. `test_empty_groups_returns_empty_dict`.

---

### `tests/test_routers/test_execution_dispatch.py` (NEW — test, integration)

**Primary analog:** `tests/test_routers/test_pipeline_scans.py` (form router + enqueue mocking + smoke-app pattern with `app.state.task_router`).

**Read first:**
- `tests/test_routers/test_pipeline_scans.py` (full file for smoke-app + enqueue mock pattern)

**Test cases:**
1. `test_multi_agent_dispatch_enqueues_per_chunk` — N agents × M chunks → N×M `enqueue_for_agent` calls.
2. `test_dispatch_summary_in_redis_hash` — `exec:{batch_id}` hash has `dispatch_summary` field.
3. `test_sse_emits_aggregate_progress` — SSE generator yields `progress` event.
4. `test_sse_emits_agents_table` — SSE generator yields `agents_table` event.
5. `test_sse_closes_on_complete_with_errors` — SSE closes on new terminal status.

---

### `tests/test_services/test_fingerprint_locality.py` (NEW — test, config validator)

**Primary analog:** `tests/test_schemas/test_agent_scan_batches.py` lines 36-44 (pydantic ValidationError pattern).

**Read first:**
- `tests/test_schemas/test_agent_scan_batches.py` lines 36-44 (whole `test_scan_batch_patch_rejects_live_status`)

**Test cases:**
1. `test_audfprint_url_rejects_external_host` — `audfprint_url="http://evil.example.com:8001"` → ValidationError.
2. `test_panako_url_rejects_external_host` — same for panako.
3. `test_localhost_audfprint_url_accepted`.
4. `test_compose_service_name_accepted` — default `http://audfprint:8001` stays valid.

---

### `tests/test_template_helpers/test_progress_partial.py` (NEW — test, Jinja render)

**Primary analog:** None exists; this is a NEW test directory. Closest pattern is template-rendering assertions in `tests/test_routers/test_pipeline_scans.py`.

**Read first:**
- `tests/test_routers/test_pipeline_scans.py` (search for template-rendering assertions)
- UI-SPEC §"Test Contract (UI side)" (lines 332-342 — explicit test cases)

**Setup pattern** (Jinja2 environment with `TEMPLATES_DIR`):
```python
from jinja2 import Environment, FileSystemLoader
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "src/phaze/templates"
env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)
```

**Test cases** (UI-SPEC §"Test Contract"):
1. `test_empty_dispatch_summary_renders_italic_paragraph`.
2. `test_single_agent_renders_one_row_with_running_pill`.
3. `test_multi_agent_renders_rows_in_dispatch_order`.
4. `test_completed_with_errors_pill_red_classes`.
5. `test_revoked_agents_banner_pluralization`.
6. `test_cross_fs_notice_has_x_data_and_no_localstorage`.

---

## Shared Patterns

### Pattern S1: Bearer Auth Dependency (cross-cutting — all agent-internal routers)

**Source:** `src/phaze/routers/agent_auth.py:62-84` (`get_authenticated_agent`)
**Apply to:** `routers/agent_exec_batches.py`
**Excerpt:**
```python
agent: Annotated[Agent, Depends(get_authenticated_agent)],
```
Raises 401 (HTTPBearer auto_error) for missing token; 403 for unknown/revoked token. The token comparison is `Agent.revoked_at.is_(None)` — same idiom Phase 28's dispatch query uses to filter revoked agents.

### Pattern S2: Cross-Tenant 403-Before-State (Phase 26 D-08 invariant)

**Source:** `src/phaze/routers/agent_proposals.py:62-76` (the canonical reference) + `src/phaze/routers/agent_scan_batches.py:77-84`
**Apply to:** `routers/agent_exec_batches.py` (D-17 step 2 + step 4)
**Excerpt:**
```python
# Cross-tenant guard runs BEFORE state-machine evaluation (T-26-08-S2, T-27-01).
if batch.agent_id != agent.id:
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="...")
```
**Phase 28 twist:** Two cross-tenant checks in sequence (D-17 step 2 = body vs auth; step 4 = agent absent from dispatch) — the second one (HEXISTS `agent:<id>:total`) is novel to Phase 28.

### Pattern S3: Stripe-Style Request-ID Idempotency (Phase 26-07 / Phase 26 D-27)

**Source:** `src/phaze/routers/agent_tracklists.py:84-104` (full SET NX EX flow with concurrent-writer poll)
**Apply to:** `routers/agent_exec_batches.py` (D-15)
**Excerpt:**
```python
req_key = f"{prefix}{body.request_id}"
won = await redis_client.set(req_key, "1", nx=True, ex=_TTL_SECONDS)
if not won:
    # ... concurrent-writer handling (Phase 28 skips this — just return 200) ...
```
**Phase 28 variation:** Phase 28's progress endpoint has **no response body to cache** (RESEARCH L13). On dup, just `Response(status_code=200)` directly. No `tracklist_resp:` analog needed.

### Pattern S4: Pydantic `extra="forbid"` + `model_validator(mode="after")` for cross-field

**Source:** `src/phaze/schemas/agent_proposals.py:21-41` (canonical reference for "field X required iff field Y == Z")
**Apply to:** `schemas/agent_exec_batches.py` (`failed_at_step` required iff `terminal_step == "failed"`)
**Excerpt:**
```python
model_config = ConfigDict(extra="forbid")

@model_validator(mode="after")
def _check_X_when_Y(self) -> "ClassName":
    if self.Y == "Z" and self.X is None:
        raise ValueError("...")
    return self
```

### Pattern S5: Per-Agent SAQ Enqueue Loop with Best-Effort Failure

**Source:** `src/phaze/routers/pipeline_scans.py:243-266` (rollback-on-fail) + `src/phaze/routers/agent_files.py:130-162` (log-and-continue)
**Apply to:** `routers/execution.py:start_execution` rewrite
**Pattern (Phase 28 follows the agent_files.py log-and-continue variant):**
```python
task_router = request.app.state.task_router
for agent_id, items in groups.items():
    for chunk_index, chunk in enumerate(chunk_proposals(items)):
        try:
            await task_router.enqueue_for_agent(
                agent_id=agent_id,
                task_name="execute_approved_batch",
                payload=ExecuteApprovedBatchPayload(...),
            )
        except Exception:
            logger.exception("dispatch: enqueue failed for agent=%s chunk=%s", agent_id, chunk_index)
            # Best-effort; the operator sees the dispatch_summary mismatch in SSE.
```

### Pattern S6: HTMX SSE-Swap Slots

**Source:** `src/phaze/templates/execution/partials/progress.html` lines 1-3 (existing `sse-swap="progress"` + `sse-close="complete"`)
**Apply to:** Rewritten `progress.html` (adds `dispatch_summary`, `agents_table`, `complete_with_errors` swap slots)
**Pattern:**
```html
<div hx-ext="sse" sse-connect="..." aria-live="polite">
    <span sse-swap="<event_name>"> ... </span>
    <span sse-swap="<terminal>" sse-close="<terminal>"></span>
</div>
```

### Pattern S7: HTML-Entity Icon Convention

**Source:** `src/phaze/templates/execution/partials/collision_block.html:3` (`&#9888;` warning)
**Apply to:** `_partials/cross_fs_fingerprint_notice.html` (info `&#9432;`) + the revoked-agents banner inline block in `progress.html` (warning `&#9888;`)
**Pattern:**
```html
<span class="text-{color}-600 dark:text-{color}-400">&#9432;</span>
```

### Pattern S8: PhazeAgentClient `_request` Funnel (Phase 26 D-09..D-13)

**Source:** `src/phaze/services/agent_client.py:138-182` (the funnel — all retry + error-mapping)
**Apply to:** `post_exec_batch_progress` method addition
**Pattern:**
```python
async def post_exec_batch_progress(self, ...) -> None:
    await self._request("POST", "/api/internal/agent/exec-batches/.../progress", json=...)
```
All retry behavior (tenacity, 4xx-no-retry, 5xx-retry) and error mapping (AgentApiAuthError / AgentApiClientError / AgentApiServerError) is INHERITED from `_request`. The new method adds zero error-handling code.

### Pattern S9: Pydantic `@field_validator` on Config

**Source:** `src/phaze/config.py:176-188` (`_split_scan_roots` with `mode="before"`) + `src/phaze/config.py:190-198` (`@model_validator(mode="after")` for required-field group)
**Apply to:** `config.py` audfprint_url/panako_url validator (D-12)
**Pattern:**
```python
@field_validator("audfprint_url", "panako_url")
@classmethod
def _enforce_localhost_only(cls, value: str) -> str:
    # validate and return (or raise ValueError)
    return value
```

## No Analog Found

| File | Role | Data Flow | Reason / Mitigation |
|------|------|-----------|---------------------|
| `tests/test_template_helpers/test_progress_partial.py` | template-render test | unit | No `tests/test_template_helpers/` directory exists. Pattern derives from UI-SPEC §"Test Contract (UI side)" + Jinja `FileSystemLoader` setup. Planner must create the directory + an `__init__.py`. |
| `src/phaze/templates/_partials/` directory | template-partial dir | n/a | Directory does not exist yet. Plan must `mkdir -p src/phaze/templates/_partials/` before writing the banner partial. |

## Metadata

**Analog search scope:**
- `src/phaze/routers/` (24 files — all read or grepped)
- `src/phaze/services/` (24 files — agent_client, agent_task_router, execution, fingerprint, proposal_queries read in full or grepped)
- `src/phaze/schemas/` (16 files — agent_*.py read for ConfigDict + Field + validator patterns)
- `src/phaze/templates/` (whole tree — progress.html, collision_block.html, recent_scans_table.html, scan_status_pill.html, list.html read in full)
- `tests/test_routers/`, `tests/test_services/`, `tests/test_tasks/`, `tests/test_schemas/` (all enumerated; key analogs read)
- `src/phaze/main.py` + `src/phaze/config.py` (read in full)

**Files scanned:** ~60 source files + ~40 test files
**Pattern extraction date:** 2026-05-15

**Files read in full or extensive slices:**
- Routers: `agent_scan_batches.py`, `agent_tracklists.py`, `agent_proposals.py`, `agent_files.py`, `agent_execution.py`, `agent_auth.py`, `execution.py`, `pipeline_scans.py`
- Services: `agent_client.py`, `agent_task_router.py`, `execution.py`, `execution_queries.py`, `fingerprint.py` (excerpts)
- Schemas: `agent_proposals.py`, `agent_tracklists.py`, `agent_scan_batches.py`, `agent_tasks.py`, `agent_files.py`
- Tasks: `tasks/execution.py`
- Templates: `progress.html`, `collision_block.html`, `recent_scans_table.html`, `scan_status_pill.html`, `duplicates/list.html`
- Config/Wiring: `main.py`, `config.py`
- Tests: `test_agent_scan_batches.py`, `test_agent_client_endpoints.py`, `test_agent_scan_batches.py` (schemas)

**Pattern quality summary:**
- 24/24 files have a strong analog (exact, role-match, or self).
- 22/24 analogs come from existing codebase files (Phase 25/26/27 work).
- 2/24 files require a new directory creation (`tests/test_template_helpers/`, `src/phaze/templates/_partials/`).
- All cross-cutting patterns (auth, cross-tenant, idempotency, schema strictness, SSE-swap, agent-client method shape, config validator) have **direct verbatim-adaptable references** in existing code.
