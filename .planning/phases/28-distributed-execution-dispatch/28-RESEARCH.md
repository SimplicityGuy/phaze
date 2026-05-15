# Phase 28: Distributed Execution Dispatch - Research

**Researched:** 2026-05-14
**Domain:** distributed task dispatch, per-agent SAQ fan-out, Redis-backed SSE aggregation, write-ahead audit
**Confidence:** HIGH (CONTEXT.md locks ~all major decisions; codebase already carries every primitive needed)

## Phase Boundary

`POST /execution/start` (currently a one-line `queue.enqueue("execute_approved_batch", batch_id=...)`) becomes a controller-side fan-out that **groups APPROVED proposals by `FileRecord.agent_id`, chunks each agent's group at the existing `ExecuteApprovedBatchPayload.proposals` cap (500), enqueues N sub-jobs via the existing `AgentTaskRouter.enqueue_for_agent`, and seeds an `exec:{batch_id}` Redis hash** the SSE generator (already at `routers/execution.py:56-88`) reads. The agent task body `phaze.tasks.execution.execute_approved_batch` ships unchanged except for (a) one new fire-and-forget `POST /api/internal/agent/exec-batches/{batch_id}/progress` call per proposal at its terminal state and (b) consuming a new `sub_batch_index` field on the payload. TASK-04 lands as a structural test + PROJECT.md paragraph + dismissible Alpine.js banner on the fingerprint matches page.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

**D-01 — 2-state ExecutionLog audit + Redis-only per-step progress + `error_message` carries failed sub-step.**
ExecutionLog stays at the Phase 25 D-15 monotonic ladder `PENDING < IN_PROGRESS < COMPLETED < FAILED`. No new enum values; no Alembic migration; `routers/agent_execution.py:60..133` is untouched. Per-operation progress (started, copied, verified, deleted) lands ONLY in the `exec:{batch_id}` Redis hash via HINCRBY on the controller side. Failed `ExecutionLog` rows put `"<step>: <reason>"` in `error_message`. Phase 28 formalizes the `<step>: <reason>` prefix convention as the contract.

**D-02 — Application server owns `exec:{batch_id}` writes exclusively.**
Agents NEVER write to Redis directly. The new endpoint `POST /api/internal/agent/exec-batches/{batch_id}/progress` is the single mutation point. SSE (`GET /execution/progress/{batch_id}`) continues to read with HGETALL.

**D-03 — One progress POST per file at terminal state.**
The agent's `_execute_one` calls `api.post_exec_batch_progress(batch_id, ExecBatchProgressPayload(...))` exactly once per proposal — at the end of the success path or end of the failure path. SSE moves in file-sized jumps (200 progress POSTs for a 200-file batch, not 800).

**D-04 — `exec:{batch_id}` hash field schema.**
Top-level fields: `total`, `completed`, `failed`, `copied`, `verified`, `deleted`, `subjobs_expected`, `subjobs_completed`, `status` (`running` | `complete` | `complete_with_errors`), `started_at` (ISO), `dispatch_summary` (JSON). Per-agent rollups: `agent:<agent_id>:completed`, `agent:<agent_id>:failed`, `agent:<agent_id>:total`. Hash TTL = 24h. Terminal detection: `subjobs_completed == subjobs_expected` → `complete` if `failed == 0` else `complete_with_errors`.

**D-05 — New router `src/phaze/routers/agent_exec_batches.py`** with one endpoint:
`POST /api/internal/agent/exec-batches/{batch_id}/progress`. Auth: `Depends(get_authenticated_agent)`. Returns `200 {}`. Cross-tenant guard: `agent.id == body.agent_id` BEFORE any state read. Idempotent on `request_id`: `SET NX EX 3600` on key `exec_progress_req:{request_id}`.

**D-06 — `ExecBatchProgressPayload` schema** in `src/phaze/schemas/agent_exec_batches.py`:
```python
class ExecBatchProgressPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    batch_id: UUID
    agent_id: str
    sub_batch_index: int
    proposal_id: UUID
    terminal_step: Literal["copied", "verified", "deleted", "failed"]
    failed_at_step: Literal["copy", "verify", "delete"] | None = None
    sub_batch_terminal: bool = False
```
`model_validator(mode="after")` asserts `failed_at_step` non-null iff `terminal_step == "failed"`.

**D-07 — Counter update rules (controller-side handler):**
- `terminal_step == "deleted"` → HINCRBY `copied 1`, `verified 1`, `deleted 1`, `completed 1`, `agent:<agent_id>:completed 1`.
- `terminal_step == "verified"` → HINCRBY `copied 1`, `verified 1`.
- `terminal_step == "copied"` → HINCRBY `copied 1`.
- `terminal_step == "failed"` → HINCRBY `failed 1`, `agent:<agent_id>:failed 1`, AND prior-step bumps based on `failed_at_step`.
- `sub_batch_terminal == true` → additionally HINCRBY `subjobs_completed 1`; check terminal-batch status.

**D-08 — Expand `execution/partials/progress.html` with a per-agent table.** Same trigger, same partial location, same SSE endpoint. Server-rendered at first load + HTMX-swapped on every SSE tick. SSE event names: `progress` (aggregate text) + `agents_table` (HTMX OOB swap, full per-agent table HTML) + existing `complete` close.

**D-09 — Chunk per-agent groups exceeding 500 into N sub-jobs under same parent `batch_id`.** Controller flow:
1. SELECT approved proposals JOIN FileRecord, grouped by `file_record.agent_id`.
2. Filter revoked agents (banner: "Agent <name> revoked; <N> proposals skipped").
3. Chunk groups at 500. Compute `subjobs_expected = sum_over_agents(ceil(len(group) / 500))`.
4. Generate `batch_id = uuid4()`.
5. Initialize Redis hash with totals, per-agent rollups, `dispatch_summary`, `EXPIRE 86400`.
6. Enqueue one `ExecuteApprovedBatchPayload(batch_id, agent_id, proposals=chunk, sub_batch_index=i)` per (agent, chunk).
7. Return the redesigned progress partial.

**D-10 — Extend `ExecuteApprovedBatchPayload` with `sub_batch_index: int = 0`.** Wire-format change (`extra="forbid"`); default `0` keeps single-chunk dispatch working.

**D-11 — Dispatch decision is visible.** Structured log line at INFO `dispatch batch_id=... total=... n_agents=... subjobs_expected=... [agent_id=... chunks=... proposals=...] ...`. Admin endpoint requirement satisfied by `dispatch_summary` field on the Redis hash, echoed as a `dispatch_summary` SSE event on first connect.

**D-12 — TASK-04 structural test** in `tests/test_task_split.py` (or new sibling): assert that `AudfprintAdapter` / `PanakoAdapter` config field validators reject any non-localhost host.

**D-13 — Doc entry** in `PROJECT.md` "Constraints" section: per-agent fingerprint indices, no cross-fs matching in v4.0 (XAGENT-01).

**D-14 — Admin UI banner** on fingerprint matches page. Dismissible Alpine.js banner. Copy lives in single Jinja partial `templates/_partials/cross_fs_fingerprint_notice.html`.

**D-15 — Progress POST idempotency.** Agent generates `request_id = uuid4()` BEFORE per-file lifecycle in `_execute_one`, stores in SAQ job state alongside `execution_log_id`. Server `SET NX EX 3600` on `exec_progress_req:{request_id}`. On dup: 200 no-body, no HINCRBY.

**D-16 — Agent-side retry policy** uses the existing Phase 26 D-11 tenacity decorator on the new `PhazeAgentClient.post_exec_batch_progress` method. Fire-and-forget at batch level: if it fails after retries, `_execute_one` LOGs WARNING and continues. Aggregate counter may be slightly under-reported in rare case.

**D-17 — Cross-tenant guard placement on the new endpoint:**
1. Resolve `agent` from `Depends(get_authenticated_agent)`.
2. Reject 403 BEFORE state read if `body.agent_id != agent.id`.
3. Reject 404 if `exec:{batch_id}` hash doesn't exist (HEXISTS check on `total`).
4. Reject 403 if `agent:<body.agent_id>:total` is absent (agent wasn't part of dispatch).

**D-18 — Tests added in Phase 28** (7 new modules listed in CONTEXT.md).

**D-19 — Doc sweep at end of Phase 28:** STATE.md, PROJECT.md, new banner partial, register new router in `phaze.main.create_app`, optional README touch.

### Claude's Discretion

- Field naming on `exec:{batch_id}` hash: colon-delimited matches existing Redis idioms (`agent:<id>:completed`).
- SSE poll cadence: keep existing 1s.
- Dispatch summary rendered ABOVE aggregate row in partial.
- `sub_batch_index` 0-based (Python convention).
- Controller logs each progress POST at DEBUG (matches PhazeAgentClient convention).
- Per-agent rollup hash keys pre-set at dispatch time (enables HEXISTS-based D-17 step 4 cross-tenant guard).
- `dispatch_summary` SSE event fires only on first connect.
- `progress.html` keeps `hx-ext="sse"` `sse-swap` pattern.
- Banner D-14 inline-above, never blocks.
- Router prefix: `/api/internal/agent/exec-batches` (collision-free with existing `execution-log`).

### Deferred Ideas (OUT OF SCOPE)

- Per-sub-step PATCH-to-audit-log granularity (5-state ExecutionStatus).
- Dedicated `/execution/batches/{batch_id}` page with per-proposal drill-down.
- `/audit/` batch filter + per-agent column.
- Cross-file-server fingerprint matching (XAGENT-01).
- Real-time per-sub-step SSE counters (move per-step not per-file).
- Dedicated `/dispatch` admin GET endpoint (Redis-hash echo is sufficient).
- Scheduled re-execution of FAILED proposals (cron).
- Multi-batch dashboard.
- Atomic "execution in progress" lock for concurrent batches.
- Per-agent tenacity policies.
- Banner localization/theming.
- `dispatch_summary` as queryable history (would need ExecutionBatch table).

</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| EXEC-01 | When operator triggers approved-batch execution, application server groups approved proposals by `FileRecord.agent_id` and enqueues one `execute_approved_batch` sub-job per affected agent under a shared parent `batch_id`. | Focus Area 1 (Batch Grouping & Sub-Job Dispatch). `AgentTaskRouter.enqueue_for_agent` already exists; `routers/execution.py:start_execution` rewrite is mechanical. `dispatch_summary` Redis field + INFO log satisfy "visible in logs and via admin endpoint." |
| EXEC-02 | Each agent performs copy-verify-delete locally for its sub-batch and reports per-operation status to the application server via PATCH so the write-ahead `ExecutionLog` audit trail is preserved across HTTP. | Focus Area 2 (Local copy-verify-delete) + Focus Area 3 (PATCH protocol). `_execute_one` already POSTs ExecutionLog at `IN_PROGRESS` and PATCHes to `COMPLETED`/`FAILED` (Phase 26 B2). D-01 keeps this 2-state ladder + adds Redis-only per-step granularity. No behavior change to ExecutionLog — only adds a parallel progress POST. |
| EXEC-03 | Agents PATCH per-file progress updates to the application server; the application server owns `exec:{batch_id}` Redis hash and serves SSE progress from a single aggregated key. | Focus Area 3 + Focus Area 4 (Redis aggregation & SSE). Single new endpoint `POST /api/internal/agent/exec-batches/{batch_id}/progress`; SSE generator (already exists) extended to read per-agent rollup fields. |
| EXEC-04 | A batch spanning multiple agents reports unified progress (`total`, `completed`, `failed`); per-agent breakdown available for debugging. | Focus Area 4. Unified via top-level hash fields; per-agent via `agent:<id>:*` rollup fields rendered in expanded `progress.html` partial table. |
| TASK-04 | Each file server runs its own audfprint and panako sidecars indexing only that file server's files; no cross-file-server fingerprint matching in v4.0. | Focus Area 5 (Sidecar locality). Structural test on adapter config validators + PROJECT.md paragraph + dismissible Alpine.js banner partial. |

</phase_requirements>

## Project Constraints (from CLAUDE.md)

- **Python 3.13 exclusively**; `uv run` prefix on every dev command (`uv run pytest`, `uv run ruff check .`, `uv run mypy .`).
- **Ruff:** line length 150, double quotes, Python 3.13 target. Rules `ARG B C4 E F I PLC PTH RUF S SIM T20 TCH UP W W191`. Per-file `T201` allowed in CLI/tests; tests also ignore `PLC` and `S105`. `isort: force-sort-within-sections, lines-after-imports=2, combine-as-imports=true`.
- **Mypy strict** (`disallow_untyped_defs`, `warn_return_any`, `warn_unreachable`, etc.) — tests opt out of `disallow_untyped_decorators`. `phaze.services.agent_task_router` has explicit strict-mode opt-in via `[[tool.mypy.overrides]]`.
- **Pre-commit hooks must pass** before commit; use frozen SHAs. Includes bandit (`-x tests -s B608`), ruff, mypy local, shellcheck, yamllint strict, actionlint.
- **Minimum 85% coverage**; upload to Codecov with service-specific flags.
- **Per-feature worktree + PR.** No direct main pushes. Phase 28 PR per the v4.0 milestone pattern (memory: "PR per phase").
- **GitHub Actions delegates to `just` commands** (memory: "Workflows use just"). Update `justfile` if new commands emerge for Phase 28 (none expected).
- **Frequent commits during phase execution**, not batched at the end (memory).
- **Per-service README kept up to date**; new banner partial + new router → touch `src/phaze/routers/README.md` if it exists (D-19).
- **Generic server names** in design docs ("file server" / "application server" not host names).
- **Never `--no-verify`.** Pre-commit must run on every commit (memory).

## Focus Area 1 — Batch Grouping & Sub-Job Dispatch (EXEC-01, EXEC-02)

### Concrete Approach

**Net-new helper (recommended location):** `src/phaze/services/execution_dispatch.py` (matches existing service-naming convention; `services/execution.py` is the legacy in-process executor and `services/execution_queries.py` is the audit-log reader — a third file keeps the grouping logic distinct from both).

Inside that helper, one async function:

```python
async def group_approved_proposals_by_agent(
    session: AsyncSession,
) -> dict[str, list[ExecuteBatchProposalItem]]:
    """SELECT APPROVED proposals JOIN FileRecord, group by agent_id, filter revoked agents.

    Returns dict[agent_id, list[ExecuteBatchProposalItem]].
    Revoked-agent groups (agents.revoked_at IS NOT NULL) are EXCLUDED.
    Caller surfaces a banner with the count of skipped proposals from the difference
    between approved-proposal count and grouped-proposal count.
    """
```

The query JOINs `RenameProposal -> FileRecord -> Agent`, filters `RenameProposal.status == APPROVED`, excludes `Agent.revoked_at IS NOT NULL`, and builds `ExecuteBatchProposalItem(proposal_id, file_id, original_path, proposed_path, sha256_hash=file.sha256_hash)`.

**Note:** `services/execution.py:97-113` already has `get_approved_proposals` but it returns ORM objects with `selectinload(file)` and doesn't filter revoked. The new helper is conceptually similar but returns the wire-format dataclass and groups + filters in one query — keep them separate to avoid breaking the legacy path until it's removed.

**Controller-side dispatch (`routers/execution.py:start_execution` rewrite):**

```python
@router.post("/execution/start", response_class=HTMLResponse)
async def start_execution(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    # 1. Existing collision pre-check stays at the top (D-Specifics) -- destination
    #    paths collide GLOBALLY, not per-agent, so the check is unchanged.
    collisions = await detect_collisions(session)
    if collisions:
        return templates.TemplateResponse(
            request=request,
            name="execution/partials/collision_block.html",
            context={"request": request, "collisions": collisions},
        )

    # 2. Group by agent + filter revoked
    grouped = await group_approved_proposals_by_agent(session)
    # (separate query for revoked-agent banner -- count of APPROVED proposals
    # whose FileRecord.agent_id is revoked. Surfaced in the response partial.)
    skipped_revoked = await count_revoked_skipped(session)

    # 3. Generate batch_id + chunk + seed Redis + enqueue
    batch_id = uuid4()
    redis = request.app.state.queue.redis
    task_router: AgentTaskRouter = request.app.state.task_router

    dispatch_summary: list[dict[str, Any]] = []
    subjobs_expected = 0
    init_fields: dict[str, Any] = {
        "total": str(sum(len(items) for items in grouped.values())),
        "completed": "0",
        "failed": "0",
        "copied": "0",
        "verified": "0",
        "deleted": "0",
        "subjobs_completed": "0",
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
    }

    for agent_id, items in grouped.items():
        chunks = [items[i:i+500] for i in range(0, len(items), 500)]
        subjobs_expected += len(chunks)
        dispatch_summary.append({"agent_id": agent_id, "chunks": len(chunks), "total": len(items)})
        # Pre-set per-agent rollup keys so D-17 step 4's HEXISTS check works
        init_fields[f"agent:{agent_id}:total"] = str(len(items))
        init_fields[f"agent:{agent_id}:completed"] = "0"
        init_fields[f"agent:{agent_id}:failed"] = "0"

    init_fields["subjobs_expected"] = str(subjobs_expected)
    init_fields["dispatch_summary"] = json.dumps(dispatch_summary)

    # HSET + EXPIRE atomic via pipeline
    async with redis.pipeline(transaction=True) as pipe:
        await pipe.hset(f"exec:{batch_id}", mapping=init_fields)
        await pipe.expire(f"exec:{batch_id}", 86400)
        await pipe.execute()

    # 4. Enqueue per-(agent, chunk_idx). Order: every chunk for agent A first,
    #    then agent B, etc -- arbitrary; SAQ processes them concurrently.
    for agent_id, items in grouped.items():
        for chunk_idx, chunk in enumerate(_chunked(items, 500)):
            payload = ExecuteApprovedBatchPayload(
                batch_id=batch_id,
                agent_id=agent_id,
                proposals=chunk,
                sub_batch_index=chunk_idx,
            )
            await task_router.enqueue_for_agent(
                agent_id=agent_id,
                task_name="execute_approved_batch",
                payload=payload,
            )

    logger.info(
        "dispatch batch_id=%s total=%d n_agents=%d subjobs_expected=%d ...",
        batch_id, init_fields["total"], len(grouped), subjobs_expected,
    )

    return templates.TemplateResponse(
        request=request,
        name="execution/partials/progress.html",
        context={
            "request": request,
            "batch_id": str(batch_id),
            "dispatch_summary": dispatch_summary,
            "skipped_revoked": skipped_revoked,
        },
    )
```

### Files Likely Touched
- `src/phaze/routers/execution.py` (rewrite `start_execution`)
- `src/phaze/services/execution_dispatch.py` (NEW — grouping helper + revoked-count helper)
- `src/phaze/schemas/agent_tasks.py` (add `sub_batch_index: int = 0` to `ExecuteApprovedBatchPayload`)
- `src/phaze/templates/execution/partials/progress.html` (table + dispatch summary section)
- `src/phaze/templates/execution/partials/agents_table.html` (NEW — partial used both at first render AND as SSE `agents_table` event payload)

### Landmines / Open Questions

- **L1 (MEDIUM):** `ExecuteBatchProposalItem.sha256_hash` is `str | None`. The current `_execute_one` runs the verify step ONLY if it's not None. For Phase 28 we want sha256 verification to be the norm — every `FileRecord.sha256_hash` is NOT NULL in the DB (Phase 2). Confirm with planner whether to populate it always (recommended) or keep optional for back-compat.
- **L2 (LOW):** `ExecuteApprovedBatchPayload.proposals` has `Field(min_length=1, max_length=500)`. If an agent has zero approved proposals (only possible if all APPROVED → REJECTED concurrently), `enqueue_for_agent` would be skipped naturally because the dict won't contain that agent_id. No special-case needed.
- **L3 (LOW):** Concurrent operator triggering `POST /execution/start` twice in quick succession would create two `batch_id`s and double-execute. CONTEXT.md "Deferred" explicitly defers a lock. Document but don't fix.
- **L4 (MEDIUM):** The seed `init_fields` dict mixes int-valued counters (stored as str via Redis convention) and a single JSON-encoded `dispatch_summary` string. SSE generator must `json.loads` `dispatch_summary` before rendering — add to the SSE generator's decode loop.
- **L5 (LOW):** Banner copy for revoked-agent-skipped proposals needs to render in the same response as the progress card. Recommend: the `progress.html` partial extends to conditionally render the banner above the dispatch summary section if `skipped_revoked > 0`.

## Focus Area 2 — Local copy-verify-delete on the Agent (EXEC-02)

### Concrete Approach

`phaze.tasks.execution._execute_one` body is **already correct** for Phase 28 (Phase 26 B2 Option A landed the full implementation). Phase 28 changes are surgical:

1. **At the START of `_execute_one`** (just after the `execution_log_id = uuid.uuid4()` line at `tasks/execution.py:89`): add `progress_request_id = uuid.uuid4()`. Both UUIDs persist via SAQ retry state because they're closures over the same `_execute_one` invocation; SAQ's retry replays the entire task function which means the same payload is re-deserialized, but the same `_execute_one` is re-entered for each item, generating fresh UUIDs. **THIS IS A POTENTIAL BUG IF SAQ RETRIES THE WHOLE BATCH.**

   Re-read of `tasks/execution.py:89`: `execution_log_id = uuid.uuid4()` is created fresh on each retry, which means the `INSERT ... ON CONFLICT (id) DO NOTHING` on the server effectively becomes an INSERT every retry — the agent-supplied id idempotency in Phase 25 D-13 only works if the agent persists the id across retries. **The current code does not do that;** the `execution_log_id` flows as a local variable inside `_execute_one`, not via SAQ job state. The Phase 25 D-13 invariant "agent persists id in SAQ job state" is therefore not currently honored end-to-end. [VERIFIED via reading the file.] CONTEXT.md D-15 asserts the agent SHOULD persist `request_id` in SAQ state. This is consistent with deferring proper SAQ-state-backed idempotency for the progress endpoint AND quietly suggests the existing `execution_log_id` also needs the same lift. Planner should flag this and decide: (a) lift both to SAQ state in Phase 28, or (b) accept the current local-variable behavior and document as known limitation.

2. **At the END of the SUCCESS path** (after `patch_proposal_state(executed)` ~`tasks/execution.py:156`): one new fire-and-forget call:
   ```python
   try:
       await api.post_exec_batch_progress(
           payload.batch_id,
           ExecBatchProgressPayload(
               request_id=progress_request_id,
               batch_id=payload.batch_id,
               agent_id=payload.agent_id,
               sub_batch_index=payload.sub_batch_index,
               proposal_id=item.proposal_id,
               terminal_step="deleted",
               sub_batch_terminal=(index == len(payload.proposals) - 1),
           ),
       )
   except AgentApiError as exc:
       logger.warning("progress POST failed for %s: %s", item.proposal_id, exc)
   ```

3. **At the END of the FAILURE path** (after `patch_proposal_state(failed)` ~`tasks/execution.py:196`): one new fire-and-forget call:
   ```python
   try:
       await api.post_exec_batch_progress(
           payload.batch_id,
           ExecBatchProgressPayload(
               request_id=progress_request_id,
               batch_id=payload.batch_id,
               agent_id=payload.agent_id,
               sub_batch_index=payload.sub_batch_index,
               proposal_id=item.proposal_id,
               terminal_step="failed",
               failed_at_step=_classify_failure_step(exc),
               sub_batch_terminal=(index == len(payload.proposals) - 1),
           ),
       )
   except AgentApiError as exc:
       logger.warning("progress POST failed for %s: %s", item.proposal_id, exc)
   ```

4. **In `execute_approved_batch` outer loop** (`tasks/execution.py:220`): the loop becomes `for index, item in enumerate(payload.proposals):` and `_execute_one` takes `index` + `is_last_in_subbatch` (or accesses it via closure on `len`).

5. **Helper `_classify_failure_step`:** maps an exception path to `"copy"` / `"verify"` / `"delete"`. The current `_execute_one` raises generic `ValueError` (for path traversal + sha256 mismatch — these become `"verify"` because they happen before/during the verify phase) or lets `OSError` from `read_bytes`/`write_bytes` propagate. Recommend: track the current step in a local `_step: str` variable that the except-handler reads, OR introduce a tiny custom `StepError` exception with a `.step` attribute.

   **Preferred:** track a local `current_step` variable inside the try block that updates as the code progresses through `"copy"` → `"verify"` → `"delete"`. The except clause reads `current_step`. This is ~3 LOC, no new exception class.

### Files Likely Touched
- `src/phaze/tasks/execution.py` (3 surgical insertions: imports, `_execute_one` signature + body, outer loop `enumerate`)
- `src/phaze/services/agent_client.py` (new method `post_exec_batch_progress`)
- `src/phaze/schemas/agent_exec_batches.py` (NEW — `ExecBatchProgressPayload` schema)

### Landmines / Open Questions

- **L6 (HIGH):** SAQ retry idempotency for `execution_log_id`. See `_execute_one` UUID discussion above. The Phase 25 D-13 contract says agent persists row PK in SAQ state; the current code generates locally. Confirm with planner whether Phase 28 lifts both UUIDs (`execution_log_id` and `progress_request_id`) into SAQ state, or accepts duplicate ExecutionLog rows on retry. CONTEXT.md D-15 only addresses `progress_request_id`.
- **L7 (MEDIUM):** Today the `delete` step is "swallowed as warning" (`tasks/execution.py:127-145` — actually no, current code raises on delete-failure as part of the outer try). Re-read confirms: `original.unlink()` is inside the same try as copy+verify, so a delete failure surfaces as a failure. CONTEXT.md D-07 documents an edge case "executor reports successful verify but delete step failed inside the same call." This appears to be a HYPOTHETICAL based on a possible future where delete-failure becomes a warning. **Currently delete-failure → failed proposal.** Confirm intent: Phase 28 keeps current behavior (delete fail → failed proposal → terminal_step=failed with failed_at_step=delete), OR moves to the D-07 edge-case behavior (delete fail → success + terminal_step=verified, file is MOVED). Recommend the planner ask the user.
- **L8 (LOW):** `sub_batch_terminal` is the agent's signal that "this sub-job is fully done." It's piggy-backed on the LAST file's progress POST. If the last file's POST fails after retries, the controller never decrements `subjobs_completed` and the batch never reaches `complete`. CONTEXT.md "Constraints to Plan Around" calls out this rare case as acceptable for v4.0 scale; document in tests.
- **L9 (LOW):** `_classify_failure_step` for a path-traversal `ValueError` raised BEFORE any file op should map to... what? "copy" is the first step, so `failed_at_step="copy"` matches the operator's mental model (the copy didn't happen). Document this.

## Focus Area 3 — PATCH Protocol for ExecutionLog (EXEC-03)

### Concrete Approach

**ExecutionLog is unchanged.** Phase 28's "PATCH per-operation status" lives in two streams:

**Stream A — Existing ExecutionLog write-ahead trail** (untouched, runs verbatim from Phase 26 B2):
- One `POST /api/internal/agent/execution-log` per proposal at `IN_PROGRESS` (start of `_execute_one`).
- One `PATCH /api/internal/agent/execution-log/{id}` per proposal at `COMPLETED` or `FAILED` (end).
- Failed `error_message` adopts `"<step>: <reason>"` prefix convention (D-01 — `_execute_one` already writes `str(exc)[:500]`, Phase 28 reformats the raise sites to prefix `f"{step}: {reason}"`).
- Idempotency: existing Phase 25 D-13 INSERT-on-conflict-do-nothing for POST + monotonic ladder for PATCH. **L6 caveat applies: planner should decide whether to lift `execution_log_id` into SAQ state.**

**Stream B — New per-step progress POST** (NEW for Phase 28):
- One `POST /api/internal/agent/exec-batches/{batch_id}/progress` per proposal at terminal state (D-03).
- Idempotency: server-side `SET NX EX 3600` on `exec_progress_req:{request_id}` (D-15). Dup → 200 no-body, no HINCRBY.
- Auth: bearer token via `Depends(get_authenticated_agent)`.
- Cross-tenant: 4-stage guard per D-17 (auth, body.agent_id vs auth, hash exists, per-agent rollup pre-seeded).
- Out-of-order PATCHes from concurrent agents are not a problem: HINCRBY is atomic in Redis, and per-agent rollup keys are pre-seeded at dispatch so any non-participating agent's POST 403s before any state mutation.

### New Router File Structure

`src/phaze/routers/agent_exec_batches.py` — mirrors `agent_scan_batches.py` byte-for-byte for structural patterns. Single endpoint:

```python
@router.post(
    "/{batch_id}/progress",
    status_code=status.HTTP_200_OK,
    response_class=Response,  # empty body
)
async def post_exec_batch_progress(
    batch_id: uuid.UUID,
    body: ExecBatchProgressPayload,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    redis_client: Annotated[redis_async.Redis, Depends(_get_redis)],
) -> Response:
    # 1. Cross-tenant guard (body.agent_id must match auth)
    if body.agent_id != agent.id:
        raise HTTPException(403, detail="agent_id in body does not match authenticated agent")

    # 2. Batch existence
    if not await redis_client.hexists(f"exec:{batch_id}", "total"):
        raise HTTPException(404, detail="batch not found")

    # 3. Per-agent participation (D-17 step 4)
    if not await redis_client.hexists(f"exec:{batch_id}", f"agent:{body.agent_id}:total"):
        raise HTTPException(403, detail="agent was not part of this dispatch")

    # 4. Idempotency: SET NX EX on request_id
    req_key = f"exec_progress_req:{body.request_id}"
    won = await redis_client.set(req_key, "1", nx=True, ex=3600)
    if not won:
        # Dup -- return 200 no-body without HINCRBY (D-15)
        return Response(status_code=200)

    # 5. Compute HINCRBY set based on terminal_step + failed_at_step (D-07)
    increments = _compute_increments(body)
    async with redis_client.pipeline(transaction=False) as pipe:
        for field, by in increments.items():
            await pipe.hincrby(f"exec:{batch_id}", field, by)
        if body.sub_batch_terminal:
            await pipe.hincrby(f"exec:{batch_id}", "subjobs_completed", 1)
        await pipe.execute()

    # 6. After-increment: if subjobs_completed == subjobs_expected, set status
    if body.sub_batch_terminal:
        sc = int(await redis_client.hget(f"exec:{batch_id}", "subjobs_completed"))
        se = int(await redis_client.hget(f"exec:{batch_id}", "subjobs_expected"))
        if sc == se:
            failed = int(await redis_client.hget(f"exec:{batch_id}", "failed"))
            final = "complete" if failed == 0 else "complete_with_errors"
            await redis_client.hset(f"exec:{batch_id}", "status", final)

    return Response(status_code=200)
```

### Files Likely Touched
- `src/phaze/routers/agent_exec_batches.py` (NEW)
- `src/phaze/schemas/agent_exec_batches.py` (NEW — request schema)
- `src/phaze/main.py` (register new router)
- `src/phaze/services/agent_client.py` (`post_exec_batch_progress` method)

### Landmines / Open Questions

- **L10 (MEDIUM):** The 4-stage cross-tenant guard above is sequenced: 403-mismatch → 404-batch → 403-not-participant → idempotency. The 403-mismatch and 404-batch checks intentionally have DIFFERENT detail strings so a leaked batch_id from another agent is indistinguishable from an unknown batch_id. CONTEXT.md D-17 step 3 says "no further state leak — both unknown and expired batches look the same." Both return 404 with the same detail. Re-read of D-17: step 2 is 403 with `"agent_id in body does not match authenticated agent"`. The first 403 fires before the 404 check, so a wrong-token request short-circuits before any Redis read — correct. The structural concern: are the second 403 (per-agent rollup missing) and the 404 (batch missing) distinguishable to an attacker? Yes, by status code. CONTEXT.md leaves this as acceptable (the threat model already accepts that auth = real). Document but don't change.
- **L11 (LOW):** After-increment terminal-status detection requires two extra HGETs. Acceptable cost for sub-batch terminal calls only (~N HGETs per batch where N = subjobs_expected, typically 1-3). Could use a single `EVAL` Lua script, but YAGNI for v4.0 scale.
- **L12 (LOW):** `_compute_increments` returns a flat `dict[str, int]`. Easy unit-test target — every branch of D-07's counter rules is a single dict comparison.
- **L13 (LOW):** The POST response is empty (200 + no body). Existing handlers use `response_model=...Response` Pydantic shapes. Choose: `Response(status_code=200)` direct or `class EmptyResponse(BaseModel): pass` to keep OpenAPI schema clean. Either works. Recommend direct `Response` to match the existing `heartbeat` endpoint's 204-no-content style — actually D-05 says "200 {}". The empty-dict body is fine; FastAPI's default JSON encoder handles it.

## Focus Area 4 — Redis Aggregation & SSE (EXEC-03, EXEC-04)

### Concrete Approach

**Redis data structure:** single hash per batch — `exec:{batch_id}`. Hash fields enumerated in D-04. TTL 24h via `EXPIRE` at dispatch time. No streams, no sorted sets — a hash with HGETALL is sufficient because the SSE generator polls once per second (no need for change-notification).

**SSE generator (`routers/execution.py:execution_progress`)** — surgical changes to the existing function:

1. Decode `dispatch_summary` JSON field on first connect, emit as `dispatch_summary` event (D-Discretion: first-connect-only — track via a local `first_connect: bool = True` flag in the generator).
2. Compute per-agent rollups from `decoded["agent:<id>:completed"]` / `failed` / `total` fields. Iterate the agent set from `dispatch_summary` (avoids enumerating all hash fields).
3. Render the `agents_table` partial server-side from `decoded` + dispatch_summary, emit as `agents_table` event each tick.
4. Extend close-on-complete: `if status in {"complete", "complete_with_errors"}:` (matches Discretion bullet).
5. Continue emitting the existing `progress` aggregate text event.

**`agents_table` SSE event payload** is the rendered HTML of `templates/execution/partials/agents_table.html` (a new partial). HTMX's `sse-swap="agents_table"` swaps the table's inner HTML on each event. Server-side render via:

```python
agents_table_html = templates.TemplateResponse(
    request=request,
    name="execution/partials/agents_table.html",
    context={"request": request, "agents": [...]},
).body.decode()
```

(Or render via Jinja env directly without the full TemplateResponse wrapper to avoid HTTP-level overhead.)

**Template `agents_table.html` shape:**

```html
<table class="...">
  <tbody>
    {% for a in agents %}
    <tr>
      <td>{{ a.agent_id }}</td>
      <td>{{ a.completed }} / {{ a.total }}</td>
      <td>{{ a.failed }}</td>
      <td>{{ a.status_class }}</td>
    </tr>
    {% endfor %}
  </tbody>
</table>
```

### Files Likely Touched
- `src/phaze/routers/execution.py` (extend SSE generator; ~15-20 LOC change)
- `src/phaze/templates/execution/partials/progress.html` (extend layout with table + dispatch summary div)
- `src/phaze/templates/execution/partials/agents_table.html` (NEW — server-rendered table partial)
- Possibly `src/phaze/templates/execution/partials/dispatch_summary.html` (NEW — separate partial for the "Dispatched to N agents" header section, rendered both at first load and on first SSE connect)

### Landmines / Open Questions

- **L14 (MEDIUM):** SSE generator currently polls every 1s. At 1-second cadence, the `agents_table` HTML render fires on every tick — for a batch with 5 agents, that's a Jinja render per second per active SSE connection. Should be fine for single-operator v4.0 deployment but document the cost. If polling cadence becomes a concern, render-once-and-diff is a future optimization.
- **L15 (LOW):** First-connect detection inside the async generator: a local `first_connect: bool = True` flag flipped to False after first yield. Simple and works.
- **L16 (LOW):** What does the table render when status flips to `complete` mid-stream? Recommend: the SSE generator emits the final `agents_table` HTML with all-terminal rows (`completed == total` or `failed > 0`) ON the same iteration where it emits the `complete` event, then closes. The browser sees the final table + close in sequence.
- **L17 (LOW):** HTMX `sse-swap` semantics: the entire element with that attribute swaps its innerHTML on each event. The table partial therefore contains ONLY the `<tbody>` rows OR includes a wrapping `<table>` that gets replaced each tick. Cleaner: `sse-swap="agents_table"` is on the `<tbody>` (or a `<div>` that wraps the table), and the SSE payload is just the inner row HTML. Decide between the two when laying out the partial.
- **L18 (LOW):** Dispatch summary is rendered at first load (by `start_execution`'s template context) AND as a first-connect SSE event. To avoid duplicate rendering, the template can conditionally render the dispatch summary div based on a context flag and the SSE event swaps the same div's inner HTML on first connect. Recommend: render at first load only; SSE first-connect event is redundant when the template already had the summary in context. **Re-read CONTEXT.md D-11:** "the redesigned progress partial renders this summary above the per-agent table." Yes — first-load render is sufficient, and the `dispatch_summary` SSE event is belt-and-suspenders for the SSE-reconnect case. Document.

## Focus Area 5 — Audfprint/Panako Sidecar Locality (TASK-04)

### Concrete Approach

**Current state (from `services/fingerprint.py:84-87, 135-138`):**
- `AudfprintAdapter.__init__(self, base_url: str = "http://audfprint:8001", ...)` — Docker service-name URL.
- `PanakoAdapter.__init__(self, base_url: str = "http://panako:8002", ...)` — Docker service-name URL.
- Config keys in `BaseSettings`: `audfprint_url: str = "http://audfprint:8001"` and `panako_url: str = "http://panako:8002"` (`config.py:60-61`).

**Both URLs resolve to the local-host Compose network only by virtue of how Compose service-name DNS works.** That's a structural property but not a `pydantic-settings` validator. D-12 wants a validator that REJECTS non-localhost / non-service-name values at construction time.

**Recommended D-12 implementation:** add `@field_validator("audfprint_url", "panako_url", mode="after")` to the settings class that asserts the parsed hostname is one of `{"audfprint", "panako", "localhost", "127.0.0.1"}` (or matches a `^\w+(-\w+)*$` Compose-service-name regex). Anything else raises `ValueError` with text like `"audfprint_url must point at a sidecar on the local Compose network (got: <host>)"`.

**Note on config split:** `audfprint_url` and `panako_url` are currently in the base `BaseSettings` class (alongside `discogsography_url`). For the v4.0 separation (controller has no audfprint, only agent does), these should arguably move to `AgentSettings`. However, `services/fingerprint.py` is loaded by the controller for `get_fingerprint_progress`. Re-read of that function shows it's pure-DB, doesn't touch any adapter — but the module imports `httpx` at the top and constructs adapters elsewhere. Phase 28 D-12 doesn't require moving the config fields, only adding the validator. Recommend: keep fields where they are, add validator, defer the role-split refactor to a later cleanup.

### Structural Test (D-12)

New test file `tests/test_services/test_fingerprint_locality.py` (or append to `tests/test_task_split.py`):

```python
def test_audfprint_url_rejects_external_host() -> None:
    with pytest.raises(ValidationError) as exc:
        ControlSettings(audfprint_url="http://evil.example.com:8001")
    assert "local Compose network" in str(exc.value)

def test_audfprint_url_accepts_compose_service_name() -> None:
    s = ControlSettings(audfprint_url="http://audfprint:8001")
    assert s.audfprint_url == "http://audfprint:8001"

# Symmetric pair for panako_url.
```

### Docs (D-13)

Append to `PROJECT.md`'s Constraints section (or wherever per-agent fingerprint DB note already lives):

> **Per-agent fingerprint indices (v4.0).** Each file server's `audfprint` and `panako` sidecars index ONLY that file server's local files. Duplicate audio content landing on different file servers will NOT cross-match. Cross-file-server fingerprint matching is XAGENT-01 (deferred to a post-v4.0 milestone). The fingerprint matches admin UI surfaces this constraint as an inline banner on every matches page.

### Banner (D-14)

`src/phaze/templates/_partials/cross_fs_fingerprint_notice.html` (NEW; the `_partials` directory does not yet exist — create it):

```html
<div x-data="{ show: true }" x-show="show"
     class="bg-amber-50 dark:bg-amber-900/30 border border-amber-200 dark:border-amber-700
            text-amber-900 dark:text-amber-100 rounded-md p-3 mb-4 text-sm
            flex items-start justify-between">
  <span>
    Fingerprint matches are scoped to the local file server's index.
    Cross-file-server matches are not supported in v4.0
    (<a href="/docs/constraints#xagent-01" class="underline">XAGENT-01</a>).
  </span>
  <button @click="show = false" class="ml-3 text-amber-700 hover:text-amber-900">
    &times;
  </button>
</div>
```

**Insertion point — audit required.** CONTEXT.md says "the planner audits and picks the right one." The candidates from the existing template tree:
- `src/phaze/templates/duplicates/list.html` — Duplicate Resolution page (this is the dedup workflow, NOT the fingerprint matches page; banner does not belong here).
- There is **no current explicit "fingerprint matches" page** in the templates. The matches surface lives inside the duplicates list (since fingerprint hits drive dedup proposals) and possibly in proposal review templates.

**Recommended:** insert the banner partial via `{% include "_partials/cross_fs_fingerprint_notice.html" %}` into `templates/duplicates/list.html` immediately under the page title (`<h1>` line 11) — it's the closest existing surface to "fingerprint matches." Document the choice in the plan. If the user wants a dedicated fingerprint matches page in a future phase, the partial moves with the page.

### Files Likely Touched
- `src/phaze/config.py` (validator on `audfprint_url`, `panako_url`)
- `tests/test_services/test_fingerprint_locality.py` (NEW) OR append to `tests/test_task_split.py`
- `PROJECT.md` (paragraph in Constraints section)
- `src/phaze/templates/_partials/cross_fs_fingerprint_notice.html` (NEW; create `_partials` dir)
- `src/phaze/templates/duplicates/list.html` (include the banner partial)

### Landmines / Open Questions

- **L19 (MEDIUM):** The "right page for the banner" is ambiguous because there's no dedicated fingerprint matches page. Confirm with user/planner: (a) duplicates page is correct surface, (b) banner also goes on proposal review pages where fingerprint-derived metadata is displayed, or (c) defer banner placement until a future fingerprint-explorer page exists.
- **L20 (LOW):** If `audfprint_url` / `panako_url` later move to `AgentSettings`, the validators must move with them. Document the validator placement so a future refactor doesn't drop them.
- **L21 (LOW):** The structural test checks the config-time validator. It does NOT check runtime behavior of the orchestrator — i.e., a future agent that constructs adapters with raw `httpx.AsyncClient(base_url=...)` could bypass `BaseSettings`. Acceptable scope for v4.0; document.

## Focus Area 6 — Retry, Crash, and Partial-Failure Semantics

### Concrete Approach

| Scenario | Behavior | Recovery |
|----------|----------|----------|
| Agent dies mid-batch (process crash) | SAQ retries the entire `execute_approved_batch` job. Existing per-file `ExecutionLog` row at `IN_PROGRESS` has its monotonic guard — retry's POST is no-op (`INSERT ON CONFLICT DO NOTHING`). PATCH to `COMPLETED` is allowed (idempotent same-state). Progress POST is idempotent via `request_id` SET NX. **BUT: L6 caveat — `execution_log_id` is fresh each retry; need to lift to SAQ state.** | If L6 fixed: clean replay. If L6 deferred: duplicate ExecutionLog rows per retry (audit log gets noisier but correctness preserved). |
| App server crashes mid-aggregation | Redis hash persists (TTL 24h). On restart, SSE generator continues HGETALL polling. No state loss. | Operator may need to refresh the page to re-establish SSE. |
| Progress POST 5xx after retries | Tenacity exhausts after ~4s wall-clock, raises `AgentApiServerError`. `_execute_one` catches and logs WARNING (D-16). Per-agent counter is under-reported by 1. File state is correct (ExecutionLog + ProposalState already persisted via separate calls). | Operator sees `completed + failed < total` in UI, investigates via `/audit/` page. |
| App server returns 404 on progress POST | Means hash expired (>24h batch) or never existed (race with batch creation). Agent logs WARNING, continues. | Acceptable. |
| App server returns 403 on progress POST (`agent_id` mismatch) | Bug — agent's auth identity doesn't match its `payload.agent_id`. Should never happen in normal operation. | Tenacity does NOT retry 403 (D-11); `AgentApiAuthError` surfaces. `_execute_one` logs WARNING. **Document as integration alarm.** |
| Sub-batch terminal POST never arrives | `subjobs_completed` never reaches `subjobs_expected`; batch stays `running` forever. SSE never closes. Hash TTLs out after 24h. | Operator manually reconciles via `/audit/` (D-16 + CONTEXT.md Constraints). |
| Two operators trigger `POST /execution/start` simultaneously | Each gets its own `batch_id`. Both fan out to agents. Approved proposals get double-executed (second SAQ job sees them as APPROVED, tries to copy a file that's now at the proposed_path). | CONTEXT.md "Deferred — atomic lock." Phase 28 accepts this for single-operator v4.0. |
| Agent revoked DURING a running batch | The agent's auth dep returns 401 on the progress POST. Agent's tenacity does not retry 401. WARN log. File ops complete locally because they don't require auth. Aggregate counter under-reports by N for the remaining files. | Operator investigates. |
| Per-agent rollup field missing (D-17 step 4) | 403 — happens only if Redis hash was tampered with externally. Defensive guard. | N/A — invariant violation. |

### Files Likely Touched (cross-cutting)
- Test files for each scenario above (see Focus Area 7).

### Landmines / Open Questions

- **L22 (HIGH):** L6 promotion. The current code's behavior on SAQ retry is "create duplicate ExecutionLog rows because `execution_log_id = uuid.uuid4()` is a local variable, not persisted in SAQ state." The Phase 25 D-13 invariant says the agent persists row PK in SAQ state. **The current code does not honor this.** Phase 28's D-15 says progress POST `request_id` should be persisted in SAQ state. Confirm with the user: should Phase 28 ALSO lift `execution_log_id` to SAQ state (recommended; small change), or document the existing behavior as a known limitation and defer? If deferred, the duplicate-row audit-log noise persists but correctness is preserved (because all duplicate rows go through the monotonic-ladder PATCH and end at terminal state).
- **L23 (LOW):** SAQ's "persist in job state" mechanism — re-check. Phase 25 D-13 references this as a pattern but actual SAQ API for stashing per-job state across retries needs verification. Likely uses `ctx['job'].meta` or a side-channel `update_job_meta` call. [VERIFIED: ASSUMED — needs SAQ docs check.] **Context7 lookup recommended for the planner.**

## Focus Area 7 — Validation Architecture (Nyquist Dimension 8)

See `## Validation Architecture` section below for the full breakdown. Summary:

- **Unit layer:** schema validators, `_compute_increments`, dispatch-grouping query (mocked session), template helpers.
- **Integration layer:** new endpoint contract tests with real DB + real Redis; agent-task tests with real tmp_path + mocked HTTP client; SSE generator tests with mocked Redis.
- **E2E layer:** one happy-path test that triggers `POST /execution/start`, simulates an agent posting progress, and asserts SSE stream produces the expected events. Optional; can be deferred if integration coverage is sufficient.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Approval grouping by agent_id | API / Backend (controller) | — | Controller owns `FileRecord.agent_id` and the dispatch decision. Agents are passive consumers. |
| Sub-batch chunking | API / Backend (controller) | — | The 500-cap is a controller-enforced wire-format invariant. |
| Per-agent SAQ enqueue | API / Backend (controller) | Database (Redis) | `AgentTaskRouter` lives in the controller; queues live in Redis. |
| Copy-verify-delete | API / Backend (agent worker) | — | File operations must run local to the file. Agent is the only tier with the file. |
| Per-proposal ExecutionLog write | API / Backend (controller via HTTP from agent) | — | Audit log is centralized; agent is the trigger but controller persists. |
| Progress aggregation | Database (Redis on controller) | — | Single source of truth for SSE; controller is the only writer (D-02). |
| SSE stream | API / Backend (controller) | Browser | Controller renders, browser consumes via HTMX `sse-swap`. |
| Per-agent table render | API / Backend (controller, Jinja server-render) | Browser (HTMX swap) | Server-rendered each SSE tick; browser swaps innerHTML. |
| Dismissible banner | Browser (Alpine.js) | — | Pure UI concern; partial rendered server-side, dismissal state lives in client. |
| Fingerprint sidecar locality | API / Backend (agent) | — | Sidecar is a per-agent Compose service; controller has no sidecars in v4.0. |

## Standard Stack

All facilities already in the repo. No new dependencies (CONTEXT.md `pyproject.toml — no new dependencies`).

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| FastAPI | as installed | New router + endpoint | Existing internal-agent routers use it. |
| SAQ | >=0.26.3 | Per-agent queue enqueue + agent worker | Project-locked task queue (memory: "arq replaced by SAQ"). [VERIFIED: phase 26 STATE entries] |
| redis-py asyncio | as installed | Redis hash mutation + idempotency | `app.state.redis` and `app.state.queue.redis` already wired in `main.py` lifespan. |
| sse-starlette | as installed | EventSourceResponse | Already used in `routers/execution.py:execution_progress`. |
| pydantic v2 | as installed | Schema validation | Existing pattern, `extra="forbid"` mandated. |
| tenacity | as installed | 4xx-no-retry / 5xx-with-retry on PhazeAgentClient | Existing `_request` funnel. |
| httpx | as installed | Agent → controller HTTP | Existing PhazeAgentClient. |
| Jinja2 + HTMX `sse-swap` | as installed | Server-rendered table + SSE swap | Existing `progress.html` pattern. |
| Alpine.js | CDN | Dismissible banner | Project convention for client-side dismiss/toggle. |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| respx | as installed | Mock httpx client in tests | Existing `tests/test_services/test_agent_client.py` pattern for new `post_exec_batch_progress` tests. |
| pytest-asyncio | as installed | Async test support | All new tests use `async def` + `@pytest.mark.asyncio`. |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Redis hash + HGETALL polling | Redis stream + XREAD | Streams give push semantics but require subscriber bookkeeping. CONTEXT.md locks the hash. Don't reconsider. |
| Per-agent SAQ queue | Single queue + filter by message metadata | Phase 26 D-18 already shipped per-agent queues. Stick with them. |
| Per-step PATCH to ExecutionLog | What CONTEXT.md rejected (D-01) | New ExecutionStatus values + Alembic migration. Don't do it. |

**Installation:** none — every library is already in `pyproject.toml`.

**Version verification:** N/A — no new packages.

## Architecture Patterns

### System Architecture Diagram

```
                          OPERATOR (browser)
                              |
                              | POST /execution/start
                              v
+---------------------------------------------------+
|                APPLICATION SERVER                  |
|                                                    |
|  routers/execution.py:start_execution              |
|     |                                              |
|     | 1. detect_collisions (existing)              |
|     | 2. group_approved_proposals_by_agent (NEW)   |
|     |    [SELECT proposals JOIN files JOIN agents] |
|     | 3. chunk per-agent groups @ 500              |
|     | 4. uuid4 batch_id                            |
|     | 5. HSET exec:{batch_id} + EXPIRE 86400  ---> [REDIS hash]
|     | 6. for each (agent, chunk):                  |
|     |     task_router.enqueue_for_agent(...)  ---> [Redis: phaze-agent-<id>]
|     | 7. log INFO dispatch line                    |
|     | 8. render progress.html partial              |
|     v                                              |
|  HTMX response: progress.html + agents_table.html  |
|     |                                              |
|     | hx-ext="sse" sse-connect="/execution/progress/{batch_id}"
|     v                                              |
|  routers/execution.py:execution_progress (SSE)     |
|     | HGETALL exec:{batch_id} every 1s             |
|     | yield events: progress, agents_table,        |
|     |        dispatch_summary (first), complete    |
|     v                                              |
+---------------------------------------------------+
                              ^
                              | POST /api/internal/agent/exec-batches/{batch_id}/progress
                              | (one per proposal at terminal step)
                              |
+---------------------------------------------------+
|                  FILE SERVER (AGENT)               |
|                                                    |
|  SAQ worker pulls phaze-agent-<id>                 |
|     |                                              |
|     v                                              |
|  tasks/execution.execute_approved_batch            |
|     |                                              |
|     | for each item in payload.proposals:          |
|     |   _execute_one(api, item, scan_roots):       |
|     |     - POST execution-log (IN_PROGRESS)  ---> APP SERVER (existing)
|     |     - resolve+check scan_roots               |
|     |     - copy original -> proposed              |
|     |     - sha256 verify                          |
|     |     - delete original                        |
|     |     - PATCH execution-log (COMPLETED)   ---> APP SERVER (existing)
|     |     - PATCH proposals/{id}/state         ---> APP SERVER (existing)
|     |     - POST exec-batches/{batch_id}/progress  | <-- NEW
|     |       (terminal_step, sub_batch_terminal)    |
|     v                                              |
|  Local fingerprint sidecars:                       |
|    audfprint (http://audfprint:8001)               |
|    panako    (http://panako:8002)                  |
|    Index ONLY local files (TASK-04)                |
+---------------------------------------------------+
```

**Component responsibilities:**

| Component | Responsibility |
|-----------|----------------|
| `routers/execution.py:start_execution` | Collision check, dispatch grouping, batch_id minting, Redis seed, SAQ fan-out, dispatch logging, partial render |
| `routers/execution.py:execution_progress` | SSE polling loop, HGETALL decode, per-agent table render, dispatch_summary first-emit, status-terminal close |
| `routers/agent_exec_batches.py` (NEW) | POST handler for per-proposal progress: 4-stage cross-tenant guard, `SET NX EX` idempotency, HINCRBY counter math, terminal-status promotion |
| `services/execution_dispatch.py` (NEW) | SELECT-and-group helper; revoked-agent count helper |
| `tasks/execution.py:_execute_one` | Per-proposal copy-verify-delete + ExecutionLog POST/PATCH + ProposalState PATCH + (NEW) progress POST at terminal step |
| `tasks/execution.py:execute_approved_batch` | Outer loop; (NEW) `sub_batch_terminal` flag on the last item |
| `services/agent_client.py:post_exec_batch_progress` (NEW method) | httpx call to controller's new POST endpoint via existing tenacity funnel |
| `schemas/agent_tasks.py:ExecuteApprovedBatchPayload` | (CHANGE) add `sub_batch_index: int = 0` |
| `schemas/agent_exec_batches.py` (NEW file) | `ExecBatchProgressPayload` with `@model_validator(mode="after")` for failed_at_step coupling |
| `templates/execution/partials/progress.html` | (CHANGE) outer card with dispatch_summary slot, aggregate counter row, agents_table slot, conditional revoked-banner |
| `templates/execution/partials/agents_table.html` (NEW) | Per-agent rollup table |
| `templates/_partials/cross_fs_fingerprint_notice.html` (NEW) | Dismissible Alpine.js banner |
| `templates/duplicates/list.html` | (CHANGE) include the banner partial |
| `config.py:ControlSettings` | (CHANGE) `@field_validator` on `audfprint_url`/`panako_url` rejecting non-localhost |
| `main.py:create_app` | (CHANGE) `app.include_router(agent_exec_batches.router)` |

### Recommended Project Structure

```
src/phaze/
├── routers/
│   ├── execution.py                     # CHANGE — start_execution rewrite + SSE extension
│   └── agent_exec_batches.py            # NEW — POST .../{batch_id}/progress
├── services/
│   └── execution_dispatch.py            # NEW — group + filter helpers
├── schemas/
│   ├── agent_tasks.py                   # CHANGE — add sub_batch_index
│   └── agent_exec_batches.py            # NEW — ExecBatchProgressPayload
├── tasks/
│   └── execution.py                     # CHANGE — progress POST + sub_batch_terminal
├── templates/
│   ├── execution/partials/
│   │   ├── progress.html                # CHANGE — table layout
│   │   └── agents_table.html            # NEW
│   ├── _partials/                       # NEW directory
│   │   └── cross_fs_fingerprint_notice.html  # NEW
│   └── duplicates/
│       └── list.html                    # CHANGE — include banner
├── services/
│   ├── agent_client.py                  # CHANGE — post_exec_batch_progress method
│   └── fingerprint.py                   # UNCHANGED (CONTEXT.md D-12: validator on config field, not adapter)
├── config.py                            # CHANGE — field_validator on audfprint_url/panako_url
└── main.py                              # CHANGE — include_router(agent_exec_batches)
```

### Pattern 1: Smoke-app Contract Test Fixture

```python
# Source: tests/test_routers/test_agent_scan_batches.py:34-44 (Phase 27)
def _make_smoke_app(session: AsyncSession, redis_client: redis_async.Redis | None = None) -> FastAPI:
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_exec_batches.router)
    app.dependency_overrides[get_session] = lambda: session
    if redis_client is not None:
        app.state.redis = redis_client
    return app
```

### Pattern 2: Cross-tenant Guard Placement

```python
# Source: src/phaze/routers/agent_proposals.py:62-76 (Phase 26 D-08)
# 403 BEFORE state-machine to prevent timing side-channel via 409 vs 200.
file_record = await session.get(FileRecord, proposal.file_id)
if file_record is not None and file_record.agent_id != agent.id:
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="...")
```

Applied to the new endpoint as: `body.agent_id != agent.id` check BEFORE any Redis read.

### Pattern 3: SET NX EX Idempotency

```python
# Source: src/phaze/routers/agent_tracklists.py:84-104 (Phase 26 D-27)
req_key = f"exec_progress_req:{body.request_id}"
won = await redis_client.set(req_key, "1", nx=True, ex=3600)
if not won:
    # Concurrent or duplicate -- noop response
    return Response(status_code=200)
```

For the progress endpoint we DON'T need the concurrent-poll fallback that `agent_tracklists.py` uses, because the progress POST has no DB-bound response to cache — it's pure side-effect (HINCRBY). Just dup → 200 no-body.

### Anti-Patterns to Avoid
- **Hand-rolling SSE event semantics.** Use existing `sse-starlette.EventSourceResponse` + HTMX `sse-swap` exactly as in `routers/execution.py`.
- **Multiple Redis writers to `exec:{batch_id}`.** Only the controller writes (D-02). Agents NEVER touch the hash directly.
- **Adding a new ExecutionStatus enum value.** D-01 locks the 2-state ladder.
- **Adding an Alembic migration.** Phase 28 has no DB schema changes.
- **Modifying `agent_execution.py`'s monotonic ladder.** Phase 25 D-15 contract is untouched.
- **Skipping idempotency tokens.** Every new POST endpoint carries a `request_id` and uses `SET NX EX`.
- **Render-then-mutate split for atomic ops.** HSET + EXPIRE + HINCRBY operations use Redis `pipeline(transaction=True)` to keep them atomic.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Idempotency window | Custom dedup table | Redis `SET NX EX 3600` (Phase 26 D-27 pattern) | One Redis call, atomic, TTL-based cleanup. |
| Per-agent queue routing | Routing keys / metadata filter | `AgentTaskRouter.enqueue_for_agent` (Phase 26 D-19) | Already exists; per-agent SAQ queue is the project invariant. |
| SSE event streaming | WebSocket / polling endpoint | `sse-starlette.EventSourceResponse` | Already wired. HTMX `sse-swap` consumes natively. |
| 4xx/5xx retry semantics | Custom retry loop | Existing `PhazeAgentClient._request` tenacity funnel | Phase 26 D-11 already correct. |
| HTTP test client | bare httpx | `tests/conftest.py:client` / `authenticated_client` fixtures | Existing override of `get_session` + bearer header. |
| Cross-tenant authorization | Header check in handler | `Depends(get_authenticated_agent)` + body-vs-auth comparison | Phase 25 D-05; 403-before-state-machine pattern. |

**Key insight:** every primitive Phase 28 needs already exists in the codebase. The work is composition + UI + tests, not new infrastructure.

## Common Pitfalls

### Pitfall 1: Per-retry duplicate ExecutionLog rows (L6/L22)
**What goes wrong:** SAQ retries `execute_approved_batch` after a transient agent crash. `_execute_one` generates a fresh `execution_log_id = uuid.uuid4()` per invocation. Result: each retry creates a NEW row at IN_PROGRESS that the server INSERTs (the on-conflict-do-nothing only fires for the SAME id).
**Why it happens:** The `execution_log_id` lives in a function-local variable, not in SAQ job state.
**How to avoid:** Lift `execution_log_id` (and the new `progress_request_id`) into the SAQ job's persisted state so retries reuse the same UUIDs per proposal. (D-15 says this for `progress_request_id`; planner decides if `execution_log_id` also moves.)
**Warning signs:** Multiple ExecutionLog rows for one `proposal_id` in `/audit/`. Re-test scenario: kill the agent mid-batch and inspect rows.

### Pitfall 2: Per-agent rollup field collisions
**What goes wrong:** Two agents with overlapping kebab-case slugs (e.g., `fileserver-01` and `file-server-01`) would write to overlapping hash field namespaces.
**Why it happens:** The kebab-case slug constraint in Phase 24 D-01 prevents most collisions, but a UI banner enumerator over `agent:*:` patterns must not assume slug uniqueness.
**How to avoid:** `dispatch_summary` JSON encodes the canonical agent list at dispatch time; renderers iterate `dispatch_summary` not raw hash-field globs.
**Warning signs:** N/A in practice — slug regex is strict — but document for future multi-tenant.

### Pitfall 3: `agents_table.html` cyclic render
**What goes wrong:** SSE generator renders the table partial every tick. If the partial uses `request` for url_for / context lookups, every render has a transient cost. Worse, if it accidentally calls back into a router, it could deadlock.
**Why it happens:** Server-side render-in-loop is unusual in SSE.
**How to avoid:** Pre-render the Jinja Template object once outside the generator (`templates.env.get_template("execution/partials/agents_table.html")`) and call `.render(...)` on it per tick. Skip the FastAPI TemplateResponse machinery.
**Warning signs:** SSE generator CPU spikes during long-running batches.

### Pitfall 4: HSET + EXPIRE race window
**What goes wrong:** `HSET` then `EXPIRE` as separate commands — between them, the hash is live but has no TTL. If the process dies between calls, the hash is leaked forever.
**Why it happens:** Two-step Redis calls aren't atomic.
**How to avoid:** Use `redis.pipeline(transaction=True)` to bundle HSET + EXPIRE in one MULTI/EXEC. Or use `HSET ... EX 86400` (Redis 7.4+ — check the deployed version; if older, pipeline is the right answer).
**Warning signs:** Stale `exec:{batch_id}` keys in Redis after several deployments.

### Pitfall 5: SSE `complete_with_errors` not closing the stream
**What goes wrong:** Existing SSE generator at `routers/execution.py:74` closes on `status == "complete"`. CONTEXT.md adds `complete_with_errors`. If the check isn't widened, the stream never terminates.
**Why it happens:** Mechanical oversight.
**How to avoid:** Change `if status == "complete":` to `if status in {"complete", "complete_with_errors"}:`.
**Warning signs:** Browser keeps SSE connection open after batch completion; operator's browser tab accumulates SSE state.

### Pitfall 6: Banner partial path not yet existing
**What goes wrong:** `templates/_partials/` directory does not exist (verified by `find` — only feature-specific partials directories exist). First file creation must `mkdir -p`.
**Why it happens:** Convention does not yet have a project-wide `_partials/` directory.
**How to avoid:** Plan task explicitly creates the directory and the partial.
**Warning signs:** `TemplateNotFound: _partials/cross_fs_fingerprint_notice.html` at first include.

### Pitfall 7: Approval proposal SELECT does not eagerly load FileRecord
**What goes wrong:** New `group_approved_proposals_by_agent` helper SELECTs proposals + FileRecord — if it doesn't pre-join, the `selectinload(file)` lazy-load fires N+1 queries.
**Why it happens:** Default SQLAlchemy laziness.
**How to avoid:** Explicit JOIN clause + select FileRecord columns inline (we only need agent_id, original_path, sha256_hash, current_path — not the full ORM row).
**Warning signs:** Slow `POST /execution/start` for large approval backlogs.

### Pitfall 8: Collision detection happens BEFORE per-agent grouping
**What goes wrong:** If two agents have proposals that would land at the same `proposed_path`, the collision is global and is caught before dispatch (good). But the collision check is on `proposed_path`, which is the absolute destination — every agent's destination must be unique GLOBALLY. CONTEXT.md "Specifics" affirms this.
**Why it happens:** Destination paths collide regardless of source agent — defensive correctness.
**How to avoid:** Keep `detect_collisions` global. Document in the test for `start_execution` that collisions across agents block ALL agents.
**Warning signs:** N/A — current behavior is correct.

### Pitfall 9: Banner-page placement audit (L19)
**What goes wrong:** Banner placed on the wrong page; operator never sees the v4.0 limitation.
**Why it happens:** No dedicated fingerprint matches page exists.
**How to avoid:** Explicit user confirmation during plan-phase: "We're putting this on `/duplicates/`. Confirm." Provide alternate locations: `/duplicates/`, every page that shows fingerprint-derived data, or a docs route.
**Warning signs:** User feedback during verification: "I never see the warning."

## Runtime State Inventory

This phase is **NOT a rename/refactor/migration**. State inventory is N/A.

## Code Examples

### Example: New POST endpoint handler skeleton

```python
# Source: pattern from src/phaze/routers/agent_tracklists.py + agent_scan_batches.py
from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
import redis.asyncio as redis_async

from phaze.models.agent import Agent
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_exec_batches import ExecBatchProgressPayload


router = APIRouter(prefix="/api/internal/agent/exec-batches", tags=["agent-internal"])


async def _get_redis(request: Request) -> redis_async.Redis:
    redis_client: redis_async.Redis = request.app.state.redis
    return redis_client


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


@router.post("/{batch_id}/progress", status_code=status.HTTP_200_OK)
async def post_exec_batch_progress(
    batch_id: uuid.UUID,
    body: ExecBatchProgressPayload,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    redis_client: Annotated[redis_async.Redis, Depends(_get_redis)],
) -> Response:
    """D-05 / D-17 / D-15 / D-07 implementation. See module docstring + Focus Area 3."""
    if body.agent_id != agent.id:
        raise HTTPException(403, detail="agent_id in body does not match authenticated agent")

    key = f"exec:{batch_id}"
    if not await redis_client.hexists(key, "total"):
        raise HTTPException(404, detail="batch not found")

    if not await redis_client.hexists(key, f"agent:{body.agent_id}:total"):
        raise HTTPException(403, detail="agent was not part of this dispatch")

    req_key = f"exec_progress_req:{body.request_id}"
    won = await redis_client.set(req_key, "1", nx=True, ex=3600)
    if not won:
        return Response(status_code=200)

    increments = _compute_increments(body)
    async with redis_client.pipeline(transaction=False) as pipe:
        for field, by in increments.items():
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

### Example: New PhazeAgentClient method

```python
# Source: pattern from services/agent_client.py:296-313 (patch_scan_batch)
async def post_exec_batch_progress(
    self,
    batch_id: uuid.UUID,
    payload: ExecBatchProgressPayload,
) -> None:
    """POST /api/internal/agent/exec-batches/{batch_id}/progress -- per-proposal terminal progress (Phase 28 D-05).

    Inherits the tenacity retry policy (D-11) + exception hierarchy (D-12) via
    the `_request` funnel -- 5xx retries, 4xx surface immediately. Caller in
    `tasks/execution._execute_one` swallows AgentApiError after retries (D-16);
    the underlying file ops are already committed and the per-proposal PATCH
    has already landed via patch_proposal_state.
    """
    await self._request(
        "POST",
        f"/api/internal/agent/exec-batches/{batch_id}/progress",
        json=payload.model_dump(mode="json"),
    )
```

### Example: ExecBatchProgressPayload with cross-field validator

```python
# Source: src/phaze/schemas/agent_exec_batches.py (NEW)
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, model_validator


class ExecBatchProgressPayload(BaseModel):
    """Per-proposal terminal-state progress event (Phase 28 D-06).

    failed_at_step is required iff terminal_step == "failed" (enforced by
    model_validator). request_id is generated agent-side BEFORE the
    per-file lifecycle and persisted in SAQ state for retry idempotency.
    sub_batch_terminal is True only on the last item of an agent's sub-job.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID
    batch_id: uuid.UUID
    agent_id: str
    sub_batch_index: int
    proposal_id: uuid.UUID
    terminal_step: Literal["copied", "verified", "deleted", "failed"]
    failed_at_step: Literal["copy", "verify", "delete"] | None = None
    sub_batch_terminal: bool = False

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

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Single-queue `queue.enqueue("execute_approved_batch", batch_id=...)` | Per-agent SAQ queues via `AgentTaskRouter.enqueue_for_agent` | Phase 26 (queues shipped), Phase 28 (this is the first dispatch that uses them at the execution layer) | Agents only process their own files; controller fans out by `FileRecord.agent_id`. |
| In-process `services/execution.py:execute_single_file` | Agent-local `tasks/execution.py:_execute_one` via HTTP-backed audit | Phase 26 B2 Option A | File ops never run on controller; controller has no file mounts in v4.0. |
| Single-source SSE counter from in-process worker | Controller-owned `exec:{batch_id}` Redis hash + HTTP-driven HINCRBY from agents | Phase 28 (new) | Multi-agent fan-out supported with unified progress view. |

**Deprecated/outdated:**
- `services/execution.py:get_approved_proposals` (no agent grouping). Phase 28 introduces a parallel helper in `execution_dispatch.py`; the old function is not yet removed because legacy in-process execution path still uses it. Future cleanup phase can collapse the two.

## Files Likely Touched (Consolidated)

### New files
- `src/phaze/routers/agent_exec_batches.py`
- `src/phaze/schemas/agent_exec_batches.py`
- `src/phaze/services/execution_dispatch.py`
- `src/phaze/templates/execution/partials/agents_table.html`
- `src/phaze/templates/execution/partials/dispatch_summary.html` (optional — may inline into `progress.html`)
- `src/phaze/templates/_partials/cross_fs_fingerprint_notice.html`
- `tests/test_routers/test_agent_exec_batches.py`
- `tests/test_routers/test_execution_dispatch.py`
- `tests/test_tasks/test_execute_approved_batch_progress.py`
- `tests/test_services/test_agent_client_exec_batch_progress.py`
- `tests/test_services/test_execution_dispatch_grouping.py`
- `tests/test_services/test_fingerprint_locality.py` (or extend `tests/test_task_split.py`)
- `tests/test_template_helpers/test_progress_partial.py` (NEW directory or extend an existing helper test file)

### Modified files
- `src/phaze/routers/execution.py` — rewrite `start_execution`, extend `execution_progress` SSE
- `src/phaze/schemas/agent_tasks.py` — add `sub_batch_index: int = 0` to `ExecuteApprovedBatchPayload`
- `src/phaze/tasks/execution.py` — `_execute_one` progress POST insertions, outer-loop `sub_batch_terminal` wiring, `<step>: <reason>` error_message prefix
- `src/phaze/services/agent_client.py` — `post_exec_batch_progress` method
- `src/phaze/main.py` — `app.include_router(agent_exec_batches.router)`
- `src/phaze/config.py` — `@field_validator` on `audfprint_url`, `panako_url`
- `src/phaze/templates/execution/partials/progress.html` — table layout + dispatch summary section
- `src/phaze/templates/duplicates/list.html` — include the banner partial
- `PROJECT.md` — Constraints paragraph on per-agent fingerprint indices
- `.planning/STATE.md` — phase 28 decisions accumulation (per D-19)
- `tests/test_task_split.py` — extend with fingerprint-locality test (D-12) OR a sibling file

### Read-only references
- `.planning/PROJECT.md`, `.planning/REQUIREMENTS.md`, `.planning/ROADMAP.md`, `.planning/STATE.md`
- `.planning/phases/25-internal-agent-http-api-bearer-auth/25-CONTEXT.md`
- `.planning/phases/26-task-code-reorg-http-backed-agent-worker/26-CONTEXT.md`
- `.planning/phases/27-watcher-service-user-initiated-scan/27-CONTEXT.md`

## Open Questions / Landmines

| # | Severity | Item | Resolution |
|---|----------|------|-----------|
| L1 | MEDIUM | `ExecuteBatchProposalItem.sha256_hash` is optional; should Phase 28 always populate it (`FileRecord.sha256_hash` is NOT NULL)? | Recommend always-populate. Planner asks user during plan if uncertain. |
| L2 | LOW | Zero-proposal agent_id groups → naturally skipped. | Document only. |
| L3 | LOW | Concurrent operator double-trigger. | CONTEXT.md "Deferred." Document. |
| L4 | MEDIUM | `dispatch_summary` JSON field requires SSE generator decode. | Add `json.loads(decoded["dispatch_summary"])` in the SSE handler; covered in plan. |
| L5 | LOW | Revoked-agent skipped banner placement in `progress.html`. | Inline at top of partial, conditional render. |
| **L6 / L22** | **HIGH** | `execution_log_id` not persisted in SAQ state → SAQ retries create duplicate ExecutionLog rows (existing bug; surfaced by reading the code). | **Planner must surface this to the user. Recommend lift to SAQ job meta in Phase 28 alongside `progress_request_id`. Otherwise document as known limitation.** |
| L7 | MEDIUM | Delete-step failure semantics: current behavior is "failed proposal." CONTEXT.md D-07 mentions a possible future "delete fails but file is moved" edge case. | Confirm with user: keep current "delete-fail = proposal-fail" OR adopt the D-07 edge case. Recommend keeping current. |
| L8 | LOW | If last-file progress POST fails after retries, batch never reaches `complete`. | Accepted per CONTEXT.md Constraints. Document. |
| L9 | LOW | `_classify_failure_step` for path-traversal `ValueError` → maps to `"copy"` (first step). | Document in the helper's docstring. |
| L10 | MEDIUM | 4-stage cross-tenant guard sequencing leaks "is this batch known" via 403/404 status code. | CONTEXT.md accepts this. Document. |
| L11 | LOW | After-increment terminal-status detection adds 2 HGET round-trips per sub_batch_terminal call. | YAGNI single Lua EVAL. Document. |
| L12 | LOW | `_compute_increments` testable in isolation. | Plan a unit-test target. |
| L13 | LOW | Empty 200 response body: direct `Response(status_code=200)`. | Use that. |
| L14 | MEDIUM | SSE 1s polling × per-tick Jinja render cost. | Document; mitigate via template-object caching outside the generator loop. |
| L15 | LOW | First-connect flag inside async generator. | Local bool. |
| L16 | LOW | Final `agents_table` HTML emit on the same iteration as `complete` close. | Document the ordering. |
| L17 | LOW | HTMX `sse-swap` target = `<tbody>` vs `<div>` wrapper. | Pick `<tbody>`. |
| L18 | LOW | Dispatch summary first-load vs first-connect dual emission. | First-load only is sufficient; first-connect SSE event is belt-and-suspenders. |
| **L19** | **MEDIUM** | Banner D-14 has no dedicated fingerprint matches page. Banner placement = `templates/duplicates/list.html`. | **Plan should explicitly ask user to confirm.** |
| L20 | LOW | If `audfprint_url`/`panako_url` later move to `AgentSettings`, validators must follow. | Document in config validator. |
| L21 | LOW | TASK-04 structural test only checks config-time validator, not runtime adapter construction. | Document. |
| **L23** | **MEDIUM** | SAQ "persist in job state" mechanism is not verified. CONTEXT.md D-13 (Phase 25) references it. | **Planner should `mcp__context7__get-library-docs` for SAQ to confirm `ctx['job'].meta` or equivalent. If unavailable, this changes the L6/L22 resolution.** |

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest with pytest-asyncio (already configured) |
| Config file | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| Quick run command | `uv run pytest tests/test_routers/test_agent_exec_batches.py tests/test_services/test_execution_dispatch_grouping.py -x` |
| Full suite command | `uv run pytest -x --cov=src --cov-report=term-missing` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|--------------|
| EXEC-01 | Group APPROVED proposals by `FileRecord.agent_id` | unit | `uv run pytest tests/test_services/test_execution_dispatch_grouping.py::test_groups_by_agent_id -x` | ❌ Wave 0 |
| EXEC-01 | Skip revoked agents with banner | unit | `uv run pytest tests/test_services/test_execution_dispatch_grouping.py::test_revoked_agent_filtered_with_count -x` | ❌ Wave 0 |
| EXEC-01 | Chunk groups at 500 | unit | `uv run pytest tests/test_services/test_execution_dispatch_grouping.py::test_1000_proposals_split_into_2_chunks -x` | ❌ Wave 0 |
| EXEC-01 | `start_execution` enqueues one job per (agent, chunk) | integration | `uv run pytest tests/test_routers/test_execution_dispatch.py::test_multi_agent_dispatch_enqueues_per_chunk -x` | ❌ Wave 0 |
| EXEC-01 | Dispatch INFO log + `dispatch_summary` field | integration | `uv run pytest tests/test_routers/test_execution_dispatch.py::test_dispatch_summary_in_redis_hash -x` | ❌ Wave 0 |
| EXEC-02 | Agent posts one progress per successful proposal | unit | `uv run pytest tests/test_tasks/test_execute_approved_batch_progress.py::test_success_emits_one_deleted_progress_post -x` | ❌ Wave 0 |
| EXEC-02 | Agent posts one progress per failed proposal with `failed_at_step` | unit | `uv run pytest tests/test_tasks/test_execute_approved_batch_progress.py::test_failure_emits_failed_progress_post -x` | ❌ Wave 0 |
| EXEC-02 | `sub_batch_terminal` set on last item only | unit | `uv run pytest tests/test_tasks/test_execute_approved_batch_progress.py::test_sub_batch_terminal_set_on_last_item -x` | ❌ Wave 0 |
| EXEC-02 | ExecutionLog write-ahead invariant preserved (POST→PATCH chain unchanged) | integration | `uv run pytest tests/test_tasks/test_execute_approved_batch.py -x` (existing, regression) | ✅ |
| EXEC-03 | Endpoint 401 without token | contract | `uv run pytest tests/test_routers/test_agent_exec_batches.py::test_unauthenticated_401 -x` | ❌ Wave 0 |
| EXEC-03 | Endpoint 403 on `agent_id` mismatch | contract | `uv run pytest tests/test_routers/test_agent_exec_batches.py::test_cross_tenant_agent_id_mismatch_403 -x` | ❌ Wave 0 |
| EXEC-03 | Endpoint 404 on missing batch | contract | `uv run pytest tests/test_routers/test_agent_exec_batches.py::test_unknown_batch_404 -x` | ❌ Wave 0 |
| EXEC-03 | Endpoint 403 on agent not in dispatch | contract | `uv run pytest tests/test_routers/test_agent_exec_batches.py::test_non_participating_agent_403 -x` | ❌ Wave 0 |
| EXEC-03 | Idempotent dup (`request_id`) → 200 + no HINCRBY | contract | `uv run pytest tests/test_routers/test_agent_exec_batches.py::test_duplicate_request_id_does_not_re_increment -x` | ❌ Wave 0 |
| EXEC-03 | Counter math per D-07 branch (all 4 terminal_step branches × 3 failed_at_step paths) | contract | `uv run pytest tests/test_routers/test_agent_exec_batches.py -k counter_math -x` | ❌ Wave 0 |
| EXEC-03 | `sub_batch_terminal=true` triggers terminal status | contract | `uv run pytest tests/test_routers/test_agent_exec_batches.py::test_sub_batch_terminal_promotes_status_complete -x` | ❌ Wave 0 |
| EXEC-03 | Schema-layer `failed_at_step` required iff `terminal_step="failed"` | unit | `uv run pytest tests/test_schemas/test_agent_exec_batches.py -x` | ❌ Wave 0 |
| EXEC-04 | SSE emits aggregate counts | integration | `uv run pytest tests/test_routers/test_execution_dispatch.py::test_sse_emits_aggregate_progress -x` | ❌ Wave 0 |
| EXEC-04 | SSE emits per-agent breakdown | integration | `uv run pytest tests/test_routers/test_execution_dispatch.py::test_sse_emits_agents_table -x` | ❌ Wave 0 |
| EXEC-04 | SSE closes on `complete_with_errors` | integration | `uv run pytest tests/test_routers/test_execution_dispatch.py::test_sse_closes_on_complete_with_errors -x` | ❌ Wave 0 |
| EXEC-04 | Template `agents_table.html` renders empty / single / multi / errors states | template | `uv run pytest tests/test_template_helpers/test_progress_partial.py -x` | ❌ Wave 0 |
| TASK-04 | Config-validator rejects non-localhost audfprint_url | unit | `uv run pytest tests/test_services/test_fingerprint_locality.py::test_audfprint_url_rejects_external_host -x` | ❌ Wave 0 |
| TASK-04 | Config-validator rejects non-localhost panako_url | unit | `uv run pytest tests/test_services/test_fingerprint_locality.py::test_panako_url_rejects_external_host -x` | ❌ Wave 0 |
| TASK-04 | Banner partial renders + dismisses | template | manual + smoke via existing template-helper harness | manual (smoke) |
| (agent client) | `post_exec_batch_progress` happy path + 4xx no-retry + 5xx with-retry | unit | `uv run pytest tests/test_services/test_agent_client_exec_batch_progress.py -x` | ❌ Wave 0 |

### Seams: Fakes vs Real Services

| Seam | Layer | What it covers | Real vs Fake |
|------|-------|----------------|-------------|
| `get_session` | Controller dependency | DB I/O for grouping query and proposal SELECTs | **Real** PostgreSQL (existing `session` fixture; integration mark). |
| `app.state.redis` | Controller dependency | Hash mutation, idempotency, HEXISTS guards | **Real** Redis (already required by `test_agent_task_router.py`). Falls back to fakeredis only if SAQ is not involved. **Recommend real.** |
| `app.state.queue` (SAQ) | Controller dependency | enqueue path | **Real** SAQ via real Redis. Or mock `enqueue_for_agent` at the router level. Recommend mock the `task_router` to assert call signature without spinning up SAQ workers in tests. |
| `ctx['api_client']` | Agent task | HTTP calls back to controller | **Mock** (`AsyncMock` of `PhazeAgentClient`), per the existing `tests/test_tasks/test_execute_approved_batch.py:28-34` pattern. |
| `httpx.AsyncClient` | PhazeAgentClient | HTTP wire | **respx** mocked in `tests/test_services/test_agent_client*.py` pattern. |
| Filesystem | Agent task | copy/verify/delete | **Real** `tmp_path` per the existing `_seed_files(tmp_path)` pattern. |
| `get_settings()` | Agent task | scan_roots | **Monkeypatched** `phaze.tasks.execution.get_settings` per existing test pattern. |
| `get_authenticated_agent` | All agent-internal endpoints | bearer auth | **Real** auth via `seed_test_agent` fixture (real DB row + real token hash). |
| SSE generator | Controller | yield loop | **Real** generator iteration with mocked Redis returning seeded hash states. |
| Jinja templates | Controller | rendered output | **Real** via `templates.TemplateResponse` in handler tests. |

### Sampling Rate

- **Per task commit:** `uv run pytest tests/test_routers/test_agent_exec_batches.py tests/test_services/test_execution_dispatch_grouping.py tests/test_schemas/test_agent_exec_batches.py -x` (~few seconds)
- **Per wave merge:** `uv run pytest tests/test_routers/ tests/test_services/ tests/test_tasks/ -x --cov=src --cov-report=term-missing`
- **Phase gate:** Full suite `uv run pytest -x --cov=src` ≥ 85% project coverage; `uv run mypy .`; `uv run ruff check .`; `pre-commit run --all-files`.

### Wave 0 Gaps

- [ ] `tests/test_routers/test_agent_exec_batches.py` — contract tests for new endpoint
- [ ] `tests/test_routers/test_execution_dispatch.py` — controller dispatch + SSE tests
- [ ] `tests/test_tasks/test_execute_approved_batch_progress.py` — agent-task progress POST tests (parallel to existing `test_execute_approved_batch.py`)
- [ ] `tests/test_services/test_agent_client_exec_batch_progress.py` — PhazeAgentClient method (respx pattern)
- [ ] `tests/test_services/test_execution_dispatch_grouping.py` — grouping/chunking unit
- [ ] `tests/test_services/test_fingerprint_locality.py` — D-12 structural test
- [ ] `tests/test_schemas/test_agent_exec_batches.py` — `model_validator` cross-field test
- [ ] `tests/test_template_helpers/test_progress_partial.py` — template render test (may require new `tests/test_template_helpers/` directory)
- [ ] Extend `tests/test_task_split.py` if D-12 is placed there instead of a sibling file

*(No framework installation needed — pytest + pytest-asyncio + respx + httpx ASGITransport are all already in.)*

## Security Domain

**Security enforcement: ENABLED** (default; not explicitly disabled in `.planning/config.json`).

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | yes | Bearer token via `Depends(get_authenticated_agent)` (Phase 25 D-05); per-agent token hash in `agents.token_hash`. |
| V3 Session Management | no | Stateless HTTP + bearer auth — no sessions. |
| V4 Access Control | yes | Cross-tenant guard: `body.agent_id == agent.id` BEFORE state mutation (D-17 / Phase 26 D-08 pattern). |
| V5 Input Validation | yes | Pydantic `extra="forbid"` on every new schema; `model_validator(mode="after")` for cross-field constraint. |
| V6 Cryptography | no | No new crypto operations; existing sha256 verify reuses Phase 26 `hashlib`. |
| V7 Error Handling & Logging | yes | DEBUG on progress POST success; WARNING on failure; bearer token NEVER logged (Phase 26 D-13). Structured INFO log on dispatch. |
| V9 Communication | yes | HTTPS termination is Phase 29 scope; bearer-token over plain HTTP for Phase 28 (private LAN, accepted in CLAUDE.md). |
| V11 Business Logic | yes | Idempotency on retries via `SET NX EX 3600` on `request_id`; monotonic ladder on ExecutionLog (Phase 25 D-15). |
| V12 Files & Resources | yes | Path-traversal guard `_resolve_and_check_containment` in `_execute_one` (Phase 26 T-26-11-S1); unchanged. |
| V13 API & Web Service | yes | Schema strict-extra; auth-dep on every internal-agent endpoint; status-code differentiation matches existing pattern. |

### Known Threat Patterns for Python / FastAPI / SAQ / Redis Stack

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Forged `agent_id` in request body | Spoofing | Cross-tenant guard: `body.agent_id == agent.id` (D-17 step 2). Auth dep is the source of truth for `agent.id`. |
| Replayed progress POST after success | Tampering / Repudiation | Server-side `SET NX EX 3600` on `request_id` → dup returns 200 with NO HINCRBY (D-15). |
| Timing side-channel via 409 vs 200 to probe batch state | Information Disclosure | 403-before-state-machine pattern (D-17 step 2 fires before any state read). Same `404 detail: "batch not found"` for missing AND expired batches. |
| Cross-tenant batch poking | Information Disclosure / Elevation | HEXISTS check on per-agent rollup field (D-17 step 4) — an agent NOT in the dispatch gets 403 before any HINCRBY. |
| Hash key collision via slug forgery | Tampering | Agent ID kebab-case constraint (Phase 24 D-01) `^[a-z0-9]+(-[a-z0-9]+)*$` prevents Redis key injection. |
| Path-traversal in `proposed_path` | Tampering | Existing `_resolve_and_check_containment` (Phase 26 T-26-11-S1). Unchanged in Phase 28. |
| Bearer token leak via logs | Information Disclosure | PhazeAgentClient never stores token as instance attribute (Phase 26 D-13); never logs Authorization header. |
| Resource exhaustion via giant proposals list | Denial of Service | `ExecuteApprovedBatchPayload.proposals` already has `Field(min_length=1, max_length=500)`. New `ExecBatchProgressPayload` is single-item; no list DoS surface. |
| Cross-file-server fingerprint inadvertent matching | Information Disclosure | TASK-04 banner + structural test on adapter config (D-12, D-13, D-14). |

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `_execute_one`'s `execution_log_id` is generated locally per invocation (current code) | Focus Area 2, Pitfall 1 | If actually persisted via SAQ state (verified by reading file: NOT persisted), then L6/L22 is real. [VERIFIED from reading `tasks/execution.py:89` — local variable.] |
| A2 | SAQ exposes per-job persisted meta for retry-stable UUIDs | Focus Area 2, L23 | If SAQ does not support this pattern out of the box, D-15 (and possibly D-13) is infeasible as written and the planner must propose an alternative. [ASSUMED — Context7 SAQ lookup recommended.] |
| A3 | Per-agent fingerprint sidecar URLs use Compose service names (`http://audfprint:8001`, `http://panako:8002`) | Focus Area 5 | Validator regex must accept these. [VERIFIED from `config.py:60-61` and `docker-compose.yml:128-148`.] |
| A4 | `templates/_partials/` directory does NOT yet exist | Focus Area 5, Pitfall 6 | Plan task must `mkdir -p`. [VERIFIED via `find` — no such directory.] |
| A5 | No dedicated fingerprint matches admin page exists; `templates/duplicates/list.html` is the closest existing surface | Focus Area 5, L19 | If a fingerprint matches page is later added (not in Phase 28 scope), the banner moves. [VERIFIED via template tree listing.] |
| A6 | Phase 27 success-criterion 1 (compose service `phaze-agent-watcher`) means watcher service is already wired in `docker-compose.yml`; Phase 28 needs no Compose changes | Architecture | [VERIFIED via STATE.md entry "Phase 27-07: Compose 'watcher' service lives in root docker-compose.yml..."] |
| A7 | Redis client at `app.state.queue.redis` and `app.state.redis` are both available; `app.state.redis` is the right one for the new endpoint (decode_responses=True) | Focus Areas 3 & 4 | [VERIFIED via `main.py:81-86` — `app.state.queue = Queue.from_url(...)`, `app.state.redis = redis_async.Redis.from_url(..., decode_responses=True)`.] The SSE generator currently uses `app.state.queue.redis` which returns bytes; the new endpoint uses `app.state.redis` which returns str. Both writers/readers must agree on encoding. The SSE generator already decodes bytes (`routers/execution.py:67-68`); the new endpoint should use `app.state.redis` for consistency with `agent_tracklists.py:_get_redis`. |
| A8 | `dispatch_summary` JSON-stringified into a single Redis hash field is acceptable | Focus Area 1, L4 | [ASSUMED — straightforward; risk is low.] |
| A9 | HSET + EXPIRE wrapped in `redis.pipeline(transaction=True)` is atomic in redis-py asyncio | Focus Area 1, Pitfall 4 | [VERIFIED from redis-py docs convention.] |
| A10 | The PhazeAgentClient `_request` funnel handles 401/403/4xx/5xx mapping correctly for the new method without changes | Focus Area 3 | [VERIFIED via `services/agent_client.py:138-182`.] |

## References

### Direct predecessors (READ in full before planning)
- `.planning/phases/28-distributed-execution-dispatch/28-CONTEXT.md` — D-01..D-19 locked decisions; Claude's Discretion bullets; Deferred items
- `.planning/phases/27-watcher-service-user-initiated-scan/27-CONTEXT.md` — D-08 (SSE deferred to Phase 28); D-10 (PATCH cross-tenant + idempotent); D-21 (cross-tenant guard placement)
- `.planning/phases/26-task-code-reorg-http-backed-agent-worker/26-CONTEXT.md` — D-09..D-13 (PhazeAgentClient + tenacity); D-18..D-19 (per-agent SAQ queue); D-22..D-24 (agent_tasks schemas); D-28 (PATCH proposals/{id}/state)
- `.planning/phases/25-internal-agent-http-api-bearer-auth/25-CONTEXT.md` — D-05 (auth dep); D-13 (agent-supplied row PKs in SAQ state); D-15 (monotonic ladder); D-16 (extra="forbid")

### Code files the planner MUST read
| File | What to look at |
|------|-----------------|
| `src/phaze/routers/execution.py` | Lines 31-88 — current `start_execution` + SSE generator; Phase 28 rewrites both. |
| `src/phaze/tasks/execution.py` | Lines 47-234 — `_execute_one` lifecycle + `execute_approved_batch` outer loop; Phase 28 inserts progress POST. |
| `src/phaze/schemas/agent_tasks.py` | Lines 88-118 — `ExecuteBatchProposalItem` + `ExecuteApprovedBatchPayload`; Phase 28 adds `sub_batch_index`. |
| `src/phaze/services/agent_task_router.py` | Lines 74-98 — `enqueue_for_agent`; the dispatch primitive. |
| `src/phaze/services/agent_client.py` | Lines 138-182 (`_request` funnel) + lines 296-313 (`patch_scan_batch` — template for new method). |
| `src/phaze/routers/agent_tracklists.py` | Lines 84-104 — Redis SET NX EX idempotency pattern; mirror for new endpoint. |
| `src/phaze/routers/agent_scan_batches.py` | Full — closest structural twin to the new router. |
| `src/phaze/routers/agent_proposals.py` | Lines 62-76 — cross-tenant guard pattern. |
| `src/phaze/routers/agent_execution.py` | Lines 60-133 — POST + PATCH execution-log; Phase 28 leaves untouched but reads as ground truth. |
| `src/phaze/services/execution.py` | Lines 97-113 — legacy `get_approved_proposals`; Phase 28 introduces a parallel helper. |
| `src/phaze/services/collision.py` | Full — pre-dispatch collision check; Phase 28 preserves placement. |
| `src/phaze/services/fingerprint.py` | Lines 84-87, 135-138 — adapter URL defaults; D-12 validator targets the BaseSettings field, not the adapter. |
| `src/phaze/config.py` | Lines 40-92 — `BaseSettings` with `audfprint_url`/`panako_url`; D-12 adds field_validators. |
| `src/phaze/models/file.py` | Lines 47-75 — `FileRecord` with `agent_id` FK to agents. |
| `src/phaze/models/agent.py` | Full — `Agent.revoked_at` for D-09 step 2 filtering. |
| `src/phaze/models/proposal.py` | Full — `RenameProposal` + `ProposalStatus.APPROVED`. |
| `src/phaze/models/execution.py` | Full — `ExecutionLog` + `ExecutionStatus` re-export; Phase 28 does NOT modify. |
| `src/phaze/main.py` | Lines 80-90 — lifespan wiring + `app.state.redis`. |
| `src/phaze/templates/execution/partials/progress.html` | Full (4 lines) — extended to a table card. |
| `src/phaze/templates/execution/partials/collision_block.html` | Full — pattern for the revoked-banner. |
| `src/phaze/templates/duplicates/list.html` | Lines 9-22 — where to include the banner partial. |
| `tests/conftest.py` | Full — `client`, `authenticated_client`, `seed_test_agent` fixtures. |
| `tests/test_routers/test_agent_scan_batches.py` | Lines 1-120 — smoke-app pattern for new contract tests. |
| `tests/test_routers/test_agent_tracklists.py` | Reference for idempotency-cache tests. |
| `tests/test_tasks/test_execute_approved_batch.py` | Reference for agent-task tests; Phase 28 parallels with a new file. |
| `tests/test_services/test_agent_client.py` | Reference for respx-mocked client tests. |
| `tests/test_task_split.py` | D-25 import-boundary test (Phase 26); D-22 watcher extension (Phase 27); Phase 28 may extend with D-12. |

### Documentation lookups recommended for the planner
- **Context7 SAQ docs:** verify persistent job-meta API for L23 (`mcp__context7__resolve-library-id` with `libraryName: "saq"`, then `mcp__context7__get-library-docs` with `topic: "job meta retry"`).
- **Context7 redis-py asyncio:** verify `pipeline(transaction=True)` atomicity guarantee (`topic: "asyncio pipeline transaction"`).
- **Context7 sse-starlette:** confirm event-name + close semantics (`topic: "EventSourceResponse close event"`).

## Sources

### Primary (HIGH confidence)
- `.planning/phases/28-distributed-execution-dispatch/28-CONTEXT.md` — locked decisions [VERIFIED via direct read]
- `.planning/REQUIREMENTS.md` — EXEC-01..04, TASK-04 [VERIFIED]
- `.planning/STATE.md` — Phase 25/26/27 accumulated decisions [VERIFIED]
- `src/phaze/**/*.py` (read directly): `routers/execution.py`, `tasks/execution.py`, `schemas/agent_tasks.py`, `services/agent_task_router.py`, `services/agent_client.py`, `routers/agent_execution.py`, `routers/agent_scan_batches.py`, `routers/agent_proposals.py`, `routers/agent_tracklists.py`, `services/fingerprint.py`, `services/collision.py`, `services/execution_queries.py`, `services/execution.py`, `models/file.py`, `models/agent.py`, `models/proposal.py`, `models/execution.py`, `main.py`, `config.py`, `schemas/agent_execution.py`
- `tests/test_task_split.py`, `tests/test_routers/test_agent_scan_batches.py`, `tests/test_tasks/test_execute_approved_batch.py`, `tests/conftest.py` — fixture + smoke-app conventions [VERIFIED]
- `docker-compose.yml` lines 128-159 — audfprint + panako Compose definitions [VERIFIED]
- `CLAUDE.md` (project root) — toolchain + workflow invariants [VERIFIED]

### Secondary (MEDIUM confidence)
- Phase 27 STATE.md entries on watcher/scan implementation choices — used to confirm Phase 27 actually delivered the per-agent queue and `pipeline_scans` admin router [INFERRED via STATE.md log entries]

### Tertiary (LOW confidence)
- SAQ "persist job meta across retries" mechanism — NOT yet verified via Context7 / docs. Phase 25 D-13 references it as a pattern; Phase 28 D-15 reuses the pattern. **Planner should verify.**

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — every primitive is in the repo, no new dependencies.
- Architecture: HIGH — CONTEXT.md is unusually prescriptive; codebase confirms every assumed call site.
- Pitfalls: HIGH — eight pitfalls identified from direct code reading (especially Pitfall 1, the `execution_log_id` retry issue, surfaced from reading `tasks/execution.py:89`).
- Validation: HIGH — test layering mirrors Phase 25/26/27 conventions.
- TASK-04 banner placement: MEDIUM — no dedicated fingerprint matches page; L19 flags the choice.
- L6/L22 (SAQ retry idempotency for `execution_log_id`): MEDIUM — verified the bug exists by reading the file; resolution depends on whether the planner lifts the UUIDs to SAQ state.

**Research date:** 2026-05-14
**Valid until:** 2026-06-13 (30 days; stable platform, no new dependencies)
