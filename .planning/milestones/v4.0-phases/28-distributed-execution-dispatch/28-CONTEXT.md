# Phase 28: Distributed Execution Dispatch - Context

**Gathered:** 2026-05-14
**Status:** Ready for planning

<domain>
## Phase Boundary

When the operator triggers execution of approved proposals (`POST /execution/start` on `routers/execution.py`), the application server **groups approved proposals by `FileRecord.agent_id`** and dispatches one or more sub-jobs per affected agent under a **shared parent `batch_id`**. Each sub-job is a `ExecuteApprovedBatchPayload` (Phase 26 D-22) enqueued via the existing Phase 26 D-19 `AgentTaskRouter.enqueue_for_agent(agent_id, "execute_approved_batch", payload)` primitive onto the per-agent SAQ queue `phaze-agent-<agent_id>`. Per-agent groups exceeding the existing `ExecuteApprovedBatchPayload.proposals` cap (`max_length=500`) are **split into N sub-jobs** under the same parent `batch_id`, each carrying a `sub_batch_index` so the aggregator can wait for all sub-jobs of an agent before considering that agent terminal.

The agent-side `execute_approved_batch` task (`src/phaze/tasks/execution.py`, already implemented in Phase 26 B2 Option A) performs local copy-verify-delete per proposal and continues to use the **existing Phase 25 `POST /api/internal/agent/execution-log` + `PATCH /execution-log/{id}` (per-proposal 2-state lifecycle, `IN_PROGRESS → COMPLETED|FAILED`)** and Phase 26 D-28 `PATCH /api/internal/agent/proposals/{id}/state` for the joint Proposal+FileRecord transition. The 2-state audit-log lifecycle stays unchanged — there is no new ExecutionStatus enum value and no Alembic migration. Failure rows carry the failed sub-step in `error_message` as `"<step>: <reason>"` (the current `_execute_one` code already does this; Phase 28 locks it as the contract).

In addition to the existing terminal PATCH calls, the agent fires **exactly one** progress call per proposal at terminal state: **`POST /api/internal/agent/exec-batches/{batch_id}/progress`** with a payload describing the **final step reached** (`copied/verified/deleted` on success; `failed_at_step` on failure). The application server is the sole owner of the `exec:{batch_id}` Redis hash; the controller HINCRBYs the appropriate counters (`completed`, `failed`, `copied`, `verified`, `deleted`, and per-agent rollups `per_agent:<agent_id>:{completed,failed,total}`). The SSE endpoint `GET /execution/progress/{batch_id}` continues to read this hash and now serves both the **unified aggregate** and the **per-agent breakdown** to a redesigned `execution/partials/progress.html` card that grows from a one-line counter into a small table (aggregate header row + one row per participating agent).

Phase 28 also delivers the TASK-04 lock: a structural test asserting that the agent's audfprint/panako adapters resolve only to localhost sidecars (no cross-host fingerprint URLs), a documentation entry in PROJECT.md / fingerprint admin docs, and a small admin-UI banner on the fingerprint matches page noting that matches are scoped to the local file server's index.

Phase 28 does **NOT** introduce new ExecutionStatus enum values, an Alembic migration, sub-step-granular ExecutionLog PATCHes, a dedicated `/execution/batches/{batch_id}` page, an extended `/audit/` batch-filter UI, or the deployment hardening / agents admin page (Phase 29). The `phaze.tasks.execution.execute_approved_batch` body lands largely as-is — Phase 28's behavioral changes there are limited to (a) adding the per-file `progress` POST at terminal state and (b) handling a `sub_batch_index` field on the payload for aggregator bookkeeping.

</domain>

<decisions>
## Implementation Decisions

### Audit-Trail Granularity (D-01)

- **D-01:** **2-state ExecutionLog audit + Redis-only per-step progress + `error_message` carries failed sub-step.** ExecutionLog stays at the Phase 25 D-15 monotonic ladder `PENDING < IN_PROGRESS < COMPLETED < FAILED`. No new enum values; no Alembic migration; the monotonic-ladder code in `routers/agent_execution.py:60..133` is untouched. Per-operation progress (started, copied, verified, deleted) lands ONLY in the `exec:{batch_id}` Redis hash via HINCRBY on the controller side. Failed `ExecutionLog` rows put `"<step>: <reason>"` in `error_message` — e.g. `"verify: sha256 mismatch expected=X got=Y"`. The current `tasks/execution.py:_execute_one` already writes `str(exc)[:500]`; Phase 28 formalizes the `<step>: <reason>` prefix convention as the contract so audit forensics can mechanically slice failures by sub-step without parsing free-form exception text.

### `exec:{batch_id}` Redis Hash Ownership (D-02, D-03, D-04)

- **D-02:** **Application server owns `exec:{batch_id}` writes exclusively.** Agents NEVER write to Redis directly. The new endpoint `POST /api/internal/agent/exec-batches/{batch_id}/progress` is the single mutation point. The controller's POST handler computes the HINCRBY set based on the payload's `step` field and the path the file took. SSE (`GET /execution/progress/{batch_id}`) continues to read with HGETALL; no SSE-side change beyond rendering the new per-agent fields.
- **D-03:** **One progress POST per file at terminal state.** The agent's `_execute_one` (`tasks/execution.py:74`) calls `api.post_exec_batch_progress(batch_id, ExecBatchProgressPayload(...))` exactly once per proposal — at the end of the success path (right after `patch_proposal_state(state=executed)`) or at the end of the failure path (right after `patch_proposal_state(state=failed)`). Payload shape (see D-06). Trade-off accepted: SSE moves in file-sized jumps (one bump per file), not sub-step jumps. For a 200-file batch that's ~200 progress POSTs, not 800.
- **D-04:** **`exec:{batch_id}` hash field schema.** Top-level fields: `total` (int, set at dispatch), `completed` (int, HINCRBY), `failed` (int, HINCRBY), `copied` (int, HINCRBY for every file that reached copy), `verified` (int, HINCRBY for every file that reached verify), `deleted` (int, HINCRBY for every file that reached delete), `subjobs_expected` (int, set at dispatch), `subjobs_completed` (int, HINCRBY on each sub-job's final `progress` call with `sub_batch_terminal=true`), `status` (string: `running` | `complete` | `complete_with_errors`), `started_at` (ISO timestamp, set at dispatch). Per-agent rollups under hash field naming convention `agent:<agent_id>:completed`, `agent:<agent_id>:failed`, `agent:<agent_id>:total` (set at dispatch, HINCRBY on each progress POST). Hash TTL set at dispatch to 24 hours; cleanup is passive. Terminal-state detection (controller-side): when `subjobs_completed == subjobs_expected`, the controller's progress handler sets `status` to `complete` if `failed == 0` else `complete_with_errors`.

### Progress-Endpoint Contract (D-05, D-06, D-07)

- **D-05:** **New router `src/phaze/routers/agent_exec_batches.py`** with one endpoint:
  ```
  POST /api/internal/agent/exec-batches/{batch_id}/progress
  ```
  Auth: `Depends(get_authenticated_agent)`. Returns `200 {}` (no body needed; aggregator state is read via SSE). Cross-tenant guard: validate that the calling `agent.id` matches the `agent_id` field in the payload (the payload's `agent_id` is the source of truth for the rollup; mismatch returns 403 BEFORE any HINCRBY — Phase 26 D-08 timing-side-channel pattern). The endpoint is **idempotent on request-id**: payload carries a `request_id: UUID` (agent-generated, persisted in SAQ job state per Phase 25 D-13 pattern). Controller uses Redis `SET NX EX 3600` on key `exec_progress_req:{request_id}` to dedup retries (Phase 26-07 Stripe-style pattern). Duplicate POST returns the same `200 {}` without re-HINCRBY.
- **D-06:** **`ExecBatchProgressPayload` schema in `src/phaze/schemas/agent_exec_batches.py`:**
  ```python
  class ExecBatchProgressPayload(BaseModel):
      model_config = ConfigDict(extra="forbid")
      request_id: UUID            # agent-generated, persisted in SAQ state
      batch_id: UUID              # parent batch id (matches URL path)
      agent_id: str               # caller's agent_id (validated against auth dep)
      sub_batch_index: int        # 0-based sub-job index (D-09)
      proposal_id: UUID           # the file this progress event is for
      terminal_step: Literal["copied", "verified", "deleted", "failed"]
      failed_at_step: Literal["copy", "verify", "delete"] | None = None  # required iff terminal_step == "failed"
      sub_batch_terminal: bool = False  # true if this is the agent's last file in this sub-job
  ```
  `model_validator(mode="after")` asserts that `failed_at_step` is non-null iff `terminal_step == "failed"`.
- **D-07:** **Counter update rules (controller-side handler):** Given a successful progress POST that wasn't deduped:
  - If `terminal_step == "deleted"` → HINCRBY `copied 1`, `verified 1`, `deleted 1`, `completed 1`, `agent:<agent_id>:completed 1`.
  - If `terminal_step == "verified"` → HINCRBY `copied 1`, `verified 1`. (Edge case: an executor reports a successful verify but the delete step failed inside the same `_execute_one` call. Today this can happen because `_execute_one` swallows delete-failure as a warning and still patches proposal_state=executed. Phase 28 keeps that behavior; the file moves and the FileRecord is `MOVED` but `deleted` does NOT bump.)
  - If `terminal_step == "copied"` → HINCRBY `copied 1`. (Same edge logic.)
  - If `terminal_step == "failed"` → HINCRBY `failed 1`, `agent:<agent_id>:failed 1`, AND any successful prior steps: if `failed_at_step == "verify"` HINCRBY `copied 1`; if `failed_at_step == "delete"` HINCRBY `copied 1, verified 1`.
  - If `sub_batch_terminal == true` → additionally HINCRBY `subjobs_completed 1`. If `subjobs_completed` equals `subjobs_expected` after the increment, SET `status` to `complete` if `failed == 0` else `complete_with_errors`. The SSE generator already polls for `status in {complete, ...}` to close — extend the existing equality check to recognize `complete_with_errors` too.

### Dispatch UI + Per-Agent Breakdown (D-08)

- **D-08:** **Expand `execution/partials/progress.html` with a per-agent table.** Same `POST /execution/start` trigger, same partial location, same SSE endpoint. The card grows from `Waiting for execution to start...` into an aggregate counter ROW + an HTMX-rendered table where each row is one participating agent. The table is populated server-side at first render (the partial returned by `POST /execution/start` already knows the agent set from dispatch — pass it through as a context dict) and updates live via SSE-swap. SSE event names: `progress` (aggregate text) stays; add `agents_table` (HTMX OOB swap that re-renders the whole per-agent table on every poll tick). Existing `complete` event closes the connection. Both the aggregate counters and the per-agent rollups come from the same `exec:{batch_id}` hash — no second source of truth.

### Dispatch Logic + Sub-Batch Chunking (D-09, D-10, D-11)

- **D-09:** **Chunk per-agent groups exceeding 500 into N sub-jobs under the same parent `batch_id`.** Controller flow in `routers/execution.py:start_execution`:
  1. SELECT approved proposals JOIN FileRecord, grouped by `file_record.agent_id`. Returns `dict[str, list[ExecuteBatchProposalItem]]` where keys are non-revoked agent IDs and values include the per-proposal data (`proposal_id`, `file_id`, `original_path`, `proposed_path`, `sha256_hash`).
  2. Filter: any group whose agent has been revoked since the proposal was approved is dropped from the dispatch and surfaced as a banner in the response partial (`"Agent <name> revoked; <N> proposals skipped"`). Those proposals remain `APPROVED`; they can be re-dispatched after the agent is rehydrated or the operator re-routes them.
  3. For each agent group, split into chunks of size `<= 500` (the `ExecuteApprovedBatchPayload.proposals` cap). Compute `subjobs_expected = sum_over_agents(ceil(len(group) / 500))`.
  4. Generate `batch_id = uuid4()`.
  5. Initialize `exec:{batch_id}` Redis hash: `HSET total <sum> subjobs_expected <N> subjobs_completed 0 completed 0 failed 0 copied 0 verified 0 deleted 0 status running started_at <iso>`. For each agent, `HSET agent:<agent_id>:total <group_size> agent:<agent_id>:completed 0 agent:<agent_id>:failed 0`. `EXPIRE exec:{batch_id} 86400`.
  6. For each (agent, chunk_index, chunk_items): build `ExecuteApprovedBatchPayload(batch_id=batch_id, agent_id=agent_id, proposals=chunk_items, sub_batch_index=chunk_index)` and call `task_router.enqueue_for_agent(agent_id=agent_id, task_name="execute_approved_batch", payload=...)`. The router's per-agent SAQ queue (`phaze-agent-<agent_id>`) is the destination.
  7. Return the redesigned progress partial with the dispatched agent list pre-rendered.
- **D-10:** **Extend `ExecuteApprovedBatchPayload` with `sub_batch_index: int = 0`.** Phase 26 D-22 declared `extra="forbid"` so this is a wire-format change; default `0` keeps single-chunk dispatch working without callers specifying it. The agent's `execute_approved_batch` task body passes `sub_batch_index` into each `progress` POST so the controller can identify which sub-job is reporting. Sub-jobs of the same agent under the same `batch_id` are processed independently by SAQ (one per queue worker slot); their HINCRBYs are atomic on the controller-side Redis.
- **D-11:** **Dispatch decision is visible.** Per roadmap success criterion #1: "the dispatch decision is visible in logs and via an admin endpoint." Controller logs structured `dispatch batch_id=<id> total=<n> n_agents=<m> subjobs_expected=<k> [agent_id=<id> chunks=<x> proposals=<y>] ...` at INFO. The "admin endpoint" requirement is satisfied by adding a `dispatch_summary` field to the `exec:{batch_id}` hash (Redis-friendly JSON-encoded array of `{agent_id, chunks, total}` rows) that the SSE generator can echo into a `dispatch_summary` event on first connect. The redesigned progress partial renders this summary above the per-agent table.

### TASK-04 Sidecar Scope Surfacing (D-12, D-13, D-14)

- **D-12:** **Structural test** in `tests/test_task_split.py` (or a new sibling): assert that `src/phaze/services/fingerprint.py` adapter constructors (`AudfprintAdapter`, `PanakoAdapter`) accept ONLY `localhost` / `127.0.0.1` / a config-key like `AUDFPRINT_URL` / `PANAKO_URL` that resolves only via the `agent` Compose service's loopback network — no cross-host URLs. The test asserts the config field validators (pydantic-settings) reject any non-localhost host with a clear message. Implementation detail for the planner: the test reads the current config field shape and either confirms localhost-only is already structurally enforced or adds the validator if it isn't.
- **D-13:** **Doc entry** in `PROJECT.md` under "Constraints" (or "Out of Scope" where XAGENT-01 already lives in REQUIREMENTS.md): explicitly note that each file server's audfprint+panako indices contain ONLY that file server's files, so a duplicate file landing on file-server-02 will NOT match an existing copy on file-server-01. Cross-file-server matching is XAGENT-01, deferred. The note already partially exists in PROJECT.md's "Key Decisions" table (`Per-agent fingerprint DB (v4.0)`); D-13 adds an operator-facing paragraph in the Constraints section.
- **D-14:** **Admin UI banner** on the fingerprint matches page (`src/phaze/templates/duplicates/duplicates.html` or whichever page surfaces fingerprint hits — the planner audits and picks the right one). A small Alpine.js-dismissible banner with text like: `"Fingerprint matches are scoped to the local file server's index. Cross-file-server matches are not supported in v4.0 (see XAGENT-01)."` The banner is dismissible per session but re-appears on next page load — operator can't permanently silence it. Banner copy lives in a single Jinja partial `templates/_partials/cross_fs_fingerprint_notice.html` so future copy tweaks don't sprinkle across templates.

### Idempotency, Retries, Cross-Tenant (D-15, D-16, D-17)

- **D-15:** **Progress POST idempotency.** The agent generates `request_id: UUID = uuid4()` BEFORE the per-file lifecycle starts in `_execute_one` and stores it in the SAQ job state (alongside `execution_log_id` already there at line 89) so SAQ retries of the entire job reuse the same UUIDs per proposal. Server uses `SET NX EX 3600` on `exec_progress_req:{request_id}` for dedup (Phase 26-07 invariant). On dup, return 200 with no body, do not HINCRBY.
- **D-16:** **Agent-side retry policy** uses the existing Phase 26 D-11 tenacity decorator on the new `PhazeAgentClient.post_exec_batch_progress` method — same 4xx-no-retry, 5xx-with-retry, total ~4s wall-clock budget, then bubble as `AgentApiServerError` for SAQ to retry. The progress POST is fire-and-forget in spirit but NOT silently swallowed: if it fails after retries, `_execute_one` LOGs WARNING and continues (file ops are already done; aggregator misses one increment but the per-proposal PATCHes on `proposals/{id}/state` are the source of truth for FileRecord state). The aggregate counter will be slightly under-reported in this rare case; the operator sees `completed + failed < total` and can investigate via `/audit/`.
- **D-17:** **Cross-tenant guard placement on the new endpoint.** `POST /api/internal/agent/exec-batches/{batch_id}/progress`:
  1. Resolve `agent` from `Depends(get_authenticated_agent)`.
  2. Reject 403 BEFORE any state read or HINCRBY if `body.agent_id != agent.id` — detail `"agent_id in body does not match authenticated agent"`. (Phase 26 D-08 timing-side-channel pattern.)
  3. Reject 404 if `exec:{batch_id}` hash doesn't exist (`HEXISTS exec:{batch_id} total == 0`) — detail `"batch not found"`. No further state leak — both unknown and expired batches look the same.
  4. The hash itself has no `agent_id` field per row (only the per-agent rollup fields), so the cross-tenant check on the BATCH is implicit: the agent_id rollup keyed under `agent:<agent_id>:*` will only be present if the agent was part of the dispatch. If the agent wasn't part of the dispatch, the HINCRBY on a missing per-agent rollup field creates the field — that's a Redis-level invariant we explicitly check by HEXISTS `agent:<body.agent_id>:total` and reject 403 if absent. (This is the deeper guard: "you can only report progress for batches that included you.")

### Test Infrastructure (D-18)

- **D-18:** **Tests added in Phase 28:**
  - `tests/test_routers/test_agent_exec_batches.py` — contract tests for the new progress endpoint: auth (401 without token), cross-tenant guard (403 on agent_id mismatch + 403 on per-agent rollup absent), batch-not-found (404), idempotent dup (200 + no double-HINCRBY), counter math (all branches of D-07).
  - `tests/test_routers/test_execution_dispatch.py` — controller dispatch tests: groups by FileRecord.agent_id, splits into chunks of <=500, initializes the Redis hash with correct totals + subjobs_expected, skips revoked agents with banner.
  - `tests/test_tasks/test_execute_approved_batch_progress.py` — agent-side task tests: every successful proposal emits one progress POST with `terminal_step="deleted"`, every failed proposal emits one with `terminal_step="failed"` + correct `failed_at_step`, sub_batch_terminal set true on the last item in the sub-batch, idempotent request_id is generated per proposal and persisted in SAQ state.
  - `tests/test_services/test_agent_client_exec_batch_progress.py` — `post_exec_batch_progress` method on PhazeAgentClient (mirrors Phase 26 D-31 respx pattern): happy-path, 4xx no-retry, 5xx with retries-then-fail.
  - `tests/test_template_helpers/test_progress_partial.py` (or e2e via existing pytest-Jinja harness) — rendering of the new per-agent table partial: empty agents list, single agent, multi-agent, completed-with-errors styling.
  - `tests/test_task_split.py` — extend with D-12's fingerprint adapter locality assertion.
  - `tests/test_services/test_execution_dispatch_grouping.py` — dispatch-logic unit test: a list of approved proposals with mixed agent_ids returns the expected per-agent grouping; 1000 proposals on one agent returns 2 chunks.

### Doc Sweep + Compose (D-19)

- **D-19:** **Doc touch at end of Phase 28** (single commit alongside the code):
  - `.planning/STATE.md` — accumulate Phase 28 decisions.
  - `PROJECT.md` — append paragraph in "Constraints" (D-13) on per-agent fingerprint indices.
  - `src/phaze/templates/_partials/cross_fs_fingerprint_notice.html` — new banner partial (D-14).
  - `src/phaze/routers/agent_exec_batches.py` — new router (D-05); register in `phaze.main.create_app` next to the other agent-internal routers.
  - `CLAUDE.md` — no change (deployment artifacts only).
  - Per-service READMEs — `src/phaze/routers/README.md` (if exists; otherwise skip) gets a one-liner for the new endpoint.

### Claude's Discretion

- Exact field naming on the `exec:{batch_id}` Redis hash (e.g., `agent:<id>:completed` vs `agent.<id>.completed`). Colon-delimited matches existing Redis idioms; use it.
- Whether the SSE generator polls every 1s (existing behavior in `routers/execution.py:86`) or every 500ms during active execution. Keep 1s — bandwidth and CPU benefit isn't worth the change.
- Whether the dispatch summary is rendered above OR below the aggregate row in the progress partial. Above (so operator immediately sees "dispatch went to N agents") is recommended.
- Whether `sub_batch_index` is 0-based or 1-based. 0-based matches Python idioms and existing code patterns.
- Whether the controller logs each progress POST at DEBUG vs INFO. DEBUG (matches PhazeAgentClient logging convention from Phase 26 D-13).
- Whether the per-agent rollup hash keys are pre-set at dispatch time or lazily on first HINCRBY. Pre-set — makes HEXISTS check in D-17 step 4 the cross-tenant guard.
- Whether the `dispatch_summary` SSE event fires only on first connect or on every poll. First connect is sufficient; the per-agent table covers ongoing visibility.
- Whether `progress.html` uses `hx-ext="sse"` like today or migrates to a different SSE library. Keep the existing `sse-swap` pattern.
- Whether the banner (D-14) blocks the page or sits inline above the matches list. Inline-above; never block.
- Whether `agent_exec_batches.py` reuses the `prefix="/api/internal/agent/exec-batches"` shape (matches existing convention) or `prefix="/api/internal/agent/execution"` for symmetry with `execution-log`. Use `exec-batches` — `execution-log` already exists and `exec-batches` is a different resource (batches vs individual log rows). Prefix collision-free.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project & Milestone Context
- `.planning/PROJECT.md` — v4.0 milestone scope, especially the "Distributed agents (v4.0)" / "HTTP-only agent boundary (v4.0)" rows in Key Decisions; "Per-agent fingerprint DB (v4.0)" row (D-13 extends this with operator-facing constraint text).
- `.planning/REQUIREMENTS.md` §"Distributed Execution" — EXEC-01 (group + dispatch), EXEC-02 (per-agent copy-verify-delete + PATCH write-ahead), EXEC-03 (Redis hash + SSE), EXEC-04 (unified counters + per-agent breakdown).
- `.planning/REQUIREMENTS.md` §"Task Execution" — TASK-04 (per-host fingerprint indices, no cross-fs matching).
- `.planning/REQUIREMENTS.md` §"Future Requirements → Cross-Agent Capabilities" — XAGENT-01 (deferred cross-fs fingerprint matching; D-13 banner references this).
- `.planning/ROADMAP.md` §"Phase 28: Distributed Execution Dispatch" — 5 success criteria.
- `.planning/STATE.md` §"Accumulated Context → Decisions" — locked v4.0 + Phase 24..27 invariants (especially Phase 26-08 cross-tenant 403-before-state-machine, Phase 26-07 Stripe-style request-id idempotency, Phase 26-11 ExecutionLog per-proposal schema invariant).

### Direct Predecessors (MUST read in full)
- `.planning/phases/25-internal-agent-http-api-bearer-auth/25-CONTEXT.md` — D-05 (auth dep `get_authenticated_agent`), D-12..D-16 (idempotency contract + `extra="forbid"`), D-13 (agent-supplied row PKs persisted in SAQ state — the same pattern Phase 28 uses for `request_id` on progress POSTs), D-15 (ExecutionLog monotonic ladder + same-status idempotent retry).
- `.planning/phases/26-task-code-reorg-http-backed-agent-worker/26-CONTEXT.md` — D-03/D-25 (import-boundary invariant), D-08 (cross-tenant 403-before-state-machine pattern), D-09..D-13 (PhazeAgentClient + tenacity retry policy + 4xx-no-retry/5xx-with-retry split), D-18/D-19 (`phaze-agent-<id>` queue naming + `AgentTaskRouter.enqueue_for_agent`), D-22..D-24 (agent_tasks payload schemas — `ExecuteApprovedBatchPayload` lives here; Phase 28 adds `sub_batch_index`), D-28 (PATCH `/api/internal/agent/proposals/{id}/state` joint Proposal+FileRecord transition — Phase 28 keeps calling this verbatim).
- `.planning/phases/27-watcher-service-user-initiated-scan/27-CONTEXT.md` — D-08 (SSE deferred to Phase 28 for cross-agent aggregation), D-10 (PATCH endpoint with cross-tenant guard + idempotent same-state — Phase 28's new endpoint mirrors this shape), D-21 (cross-tenant guard placement pattern).
- `.planning/phases/26-task-code-reorg-http-backed-agent-worker/26-VERIFICATION.md` (if present) — confirms what Phase 26 actually shipped, especially the B2 Option A execute_approved_batch implementation.

### Existing Code to Read Before Modifying

#### Controller-side dispatch
- `src/phaze/routers/execution.py:31-53` — current `POST /execution/start` that enqueues `execute_approved_batch(batch_id=...)` with no agent grouping (the broken-by-construction Phase 26 holdover that Phase 28 replaces).
- `src/phaze/routers/execution.py:56-88` — current SSE generator. Phase 28 extends the rendered output to include per-agent fields; the polling loop logic stays.
- `src/phaze/services/execution.py:97-113` — `get_approved_proposals` (controller-side, used by the legacy path). Phase 28 uses a similar query but JOINs on FileRecord and groups by `agent_id`. Net-new helper in `services/execution_queries.py` or `services/dispatch.py` recommended.
- `src/phaze/services/agent_task_router.py:74-98` — `enqueue_for_agent(agent_id, task_name, payload)` is the dispatch primitive Phase 28 uses.
- `src/phaze/templates/execution/partials/progress.html` — single-line SSE-swap card that Phase 28 expands into a table.
- `src/phaze/templates/execution/partials/collision_block.html` — pattern for the controller returning an error partial (the "agent revoked, N proposals skipped" banner reuses the same shape).

#### Models (READ — no migrations in Phase 28)
- `src/phaze/models/execution.py` — `ExecutionLog` + `ExecutionStatus` enum; Phase 28 does NOT modify the enum (D-01).
- `src/phaze/models/proposal.py` — `RenameProposal` + `ProposalStatus`; Phase 28 selects `APPROVED` and joins on `FileRecord`.
- `src/phaze/models/file.py` — `FileRecord` + `agent_id` column (Phase 24 D-02). Phase 28's dispatch query GROUP BYs on this.
- `src/phaze/models/agent.py` — `Agent` + `revoked_at`. Phase 28's dispatch filters revoked agents (D-09 step 2).

#### Agent-side execution task body
- `src/phaze/tasks/execution.py:47-198` — current `_execute_one` per-proposal lifecycle. Phase 28 adds exactly one `api.post_exec_batch_progress(...)` call near line 156 (success path, after `patch_proposal_state(executed)`) and one near line 196 (failure path, after `patch_proposal_state(failed)`). The `execution_log_id = uuid4()` pattern at line 89 is the template for `progress_request_id = uuid4()` per proposal.
- `src/phaze/tasks/execution.py:200-234` — `execute_approved_batch` outer loop. Phase 28 adds `sub_batch_terminal=True` on the last item's progress POST.
- `src/phaze/schemas/agent_tasks.py:88-118` — `ExecuteBatchProposalItem` + `ExecuteApprovedBatchPayload`. Phase 28 adds `sub_batch_index: int = 0` to the payload (D-10).

#### Existing internal-agent endpoints to mirror
- `src/phaze/routers/agent_execution.py:60-133` — POST + PATCH `/execution-log` (auth, request schema, idempotency, monotonic ladder). The structural pattern for the new `/exec-batches/{batch_id}/progress` POST.
- `src/phaze/routers/agent_scan_batches.py` — Phase 27 D-10 PATCH endpoint with cross-tenant + idempotent same-state. Closest precedent for cross-tenant guard placement on a batch-keyed endpoint.
- `src/phaze/routers/agent_proposals.py:53-131` — Phase 26 D-28 cross-tenant 403-before-state-machine pattern.
- `src/phaze/routers/agent_files.py` — Phase 27 D-09's `batch_id: UUID | None = None` field on the upsert; `POST /api/internal/agent/exec-batches/{batch_id}/progress` follows the same Pydantic strict-extra pattern.

#### Services + clients
- `src/phaze/services/agent_client.py:298-315` — `patch_scan_batch` method (closest existing pattern for the new `post_exec_batch_progress` method).
- `src/phaze/services/agent_task_router.py` — `enqueue_for_agent` used by controller dispatch.
- `src/phaze/services/fingerprint.py` — `AudfprintAdapter` + `PanakoAdapter` + `FingerprintOrchestrator`; D-12 audits config field shape to enforce localhost-only.

#### Templates the banner touches (D-14)
- `src/phaze/templates/duplicates/duplicates.html` — likely host page for fingerprint matches. The planner audits this and any duplicate-match templates and picks the right insertion point.
- `src/phaze/templates/_partials/` — new partial `cross_fs_fingerprint_notice.html` lands here (matches existing partial naming).

#### Reference patterns (READ, do not modify)
- `src/phaze/routers/agent_files.py:99-117` — Phase 25 D-20 auto-enqueue pattern; Phase 26 refactor to `task_router.enqueue_for_file`. Phase 28 dispatch is the analog at execution time.
- `src/phaze/services/discogs_matcher.py:21-46` — `DiscogsographyClient` retry pattern reflected in PhazeAgentClient.
- `src/phaze/routers/pipeline.py` (if present) — Phase 27 D-08's HTMX poll-partial halt pattern. Phase 28 uses SSE not poll, but the swap-on-finish principle is the same.

### Configuration & Wiring
- `src/phaze/main.py` — `create_app()`; Phase 28 adds `app.include_router(agent_exec_batches.router)` next to the other agent-internal routers and confirms `app.state.task_router` is set (already wired Phase 26 D-20).
- `src/phaze/config.py` — `BaseSettings` exposes `redis_url`; Phase 28 uses it via `request.app.state.queue.redis` (existing pattern in `routers/execution.py:46`). No new config fields.
- `docker-compose.yml` — no new service. Phase 28 is purely code changes inside existing containers.
- `pyproject.toml` — no new dependencies. All facilities (httpx, FastAPI, SAQ, sse-starlette, redis client) are already in.
- `CLAUDE.md` — Python 3.13, uv, mypy strict, ruff 150 char, pre-commit frozen SHAs. All preserved.

### Tests
- `tests/test_task_split.py` — Phase 26 D-25 import-boundary test; Phase 28 extends with D-12 fingerprint-localhost-only assertion.
- Phase 26 contract-test pattern under `tests/test_routers/test_agent_*.py` — mirrored for the new `agent_exec_batches.py` router.
- `tests/test_services/test_agent_task_router.py` (existing) — pattern for `tests/test_services/test_execution_dispatch_grouping.py`.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`AgentTaskRouter.enqueue_for_agent`** (`services/agent_task_router.py:74-98`) — controller-side dispatch primitive; Phase 28's `/execution/start` calls it once per (agent, chunk) pair.
- **`ExecuteApprovedBatchPayload` + `ExecuteBatchProposalItem`** (`schemas/agent_tasks.py:88-118`) — Phase 26 D-22 payload shapes; Phase 28 adds `sub_batch_index: int = 0`.
- **`get_authenticated_agent`** (`routers/agent_auth.py`) — auth dep for the new POST endpoint (`Depends`).
- **`request.app.state.queue.redis`** (`routers/execution.py:46`) — existing Redis handle for the SSE hash; reused for HSET/HINCRBY/HGETALL.
- **`sse_starlette.sse.EventSourceResponse`** (`routers/execution.py`) — existing SSE plumbing; Phase 28 extends the event payloads.
- **Phase 26-07 Stripe-style request-id idempotency** (Redis `SET NX EX`) — pattern reused for the progress POST's `request_id` dedup.
- **`AgentApiError` / `AgentApiServerError` / `AgentApiClientError`** (`services/agent_client.py`) — exception hierarchy from Phase 26 D-12; new `post_exec_batch_progress` method inherits the same retry semantics via tenacity decorator (Phase 26 D-11).
- **`_execute_one`** (`tasks/execution.py:74-197`) — current per-proposal lifecycle; Phase 28 adds exactly one progress POST at terminal state per proposal. Both success and failure paths converge on a `progress` POST before returning.

### Established Patterns
- **One router file per resource** — Phase 28 adds `routers/agent_exec_batches.py`.
- **`APIRouter(prefix="/api/internal/agent/<resource>", tags=["agent-internal"])`** — Phase 28's new router uses `prefix="/api/internal/agent/exec-batches"`.
- **Cross-tenant guard placement** — Phase 26 D-08: 403 BEFORE state-machine evaluation; Phase 28 D-17 follows.
- **Stripe-style idempotency** — `SET NX EX 3600` on `exec_progress_req:{request_id}` for dedup (Phase 26-07).
- **Pydantic `extra="forbid"`** — every new schema enforces strict input parsing.
- **`model_validator(mode="after")`** — `failed_at_step` required iff `terminal_step=="failed"` on `ExecBatchProgressPayload` (D-06).
- **HTMX SSE `sse-swap`** — pre-existing pattern in `progress.html`; Phase 28 adds a `sse-swap="agents_table"` slot for the new per-agent rollup.
- **Per-agent SAQ queue routing** — `phaze-agent-<agent_id>` (Phase 26 D-18); the dispatcher routes via `task_router.enqueue_for_agent`.

### Integration Points
- **1 new internal-agent endpoint** — `POST /api/internal/agent/exec-batches/{batch_id}/progress` registered in `main.create_app()`.
- **1 new agent-side PhazeAgentClient method** — `post_exec_batch_progress(batch_id, payload)`.
- **1 schema extension** — `ExecuteApprovedBatchPayload.sub_batch_index: int = 0` in `schemas/agent_tasks.py`.
- **1 new agent-side payload schema** — `ExecBatchProgressPayload` in `schemas/agent_exec_batches.py`.
- **1 controller dispatch rewrite** — `routers/execution.py:start_execution` from single-enqueue stub to per-agent grouping + chunking + Redis-hash initialization.
- **2 template changes** — `templates/execution/partials/progress.html` (table + per-agent rows + dispatch_summary section); new `templates/_partials/cross_fs_fingerprint_notice.html`.
- **2 agent-side task touches** — `_execute_one` (one progress POST per proposal); `execute_approved_batch` outer (set `sub_batch_terminal=true` on last item).
- **1 PROJECT.md / docs change** — D-13 paragraph on per-agent fingerprint indices.
- **~7 new test modules** (D-18).
- **1 banner partial** — `templates/_partials/cross_fs_fingerprint_notice.html`.
- **1 admin-page edit** — fingerprint matches page (planner audits + picks) includes the banner partial.
- **0 Alembic migrations** — D-01 keeps the enum; D-09 keeps existing ScanBatch + FileRecord + RenameProposal schemas.

### Constraints to Plan Around
- **No new ExecutionStatus enum values** (D-01). The audit ladder stays Phase 25 D-15.
- **No new Postgres columns or tables.** Phase 28 reuses RenameProposal, FileRecord, Agent, ExecutionLog. Redis is the only state store mutated by the new endpoint.
- **`extra="forbid"` everywhere.** New payload schemas reject unknown fields with 422.
- **`exec:{batch_id}` is the single source of truth for the SSE.** No second source. The SSE generator (`routers/execution.py:60-86`) is the only reader.
- **Dispatch happens controller-side; no agent ever writes to the Redis hash directly.** Every counter mutation goes through the new POST endpoint (D-02). This holds the v4.0 HTTP-only boundary at the execution layer.
- **Agent-side progress POSTs are fire-and-forget at the BATCH level** (D-16) — if they fail after tenacity retries, the file is still moved on-disk and reported via `patch_proposal_state`; the aggregate counter may be slightly under-reported, the operator sees the discrepancy in SSE and investigates via `/audit/`.
- **SubBatch terminality is reported by the agent.** Phase 28 does NOT have the controller count "files seen so far per sub-job" — the agent knows when it's done with its sub-batch and sets `sub_batch_terminal=true` on its last `progress` POST. If that POST never arrives, the batch never reaches `complete` and the operator has to manually reconcile (rare; SAQ retries cover most cases). Acceptable for v4.0 personal-collection scale.
- **Phase 26-11 v3.0 UI regression (scan_live_set artist/title)** still out-of-scope per Phase 27 CONTEXT — Phase 28 is NOT picking it up.

</code_context>

<specifics>
## Specific Ideas

- The SSE generator's existing decode pattern (`routers/execution.py:67`) already handles bytes-vs-str from the Redis client; extending it to read per-agent rollup fields just adds more `decoded.get("agent:<id>:completed", 0)` lookups. No new decode logic.
- The progress partial's per-agent table is server-rendered on first load (the partial returned by `POST /execution/start` has the agent list in its template context) AND HTMX-swapped on every SSE tick. SSE event name `agents_table` carries the full table HTML rendered server-side from the current Redis state. Pre-render at first load avoids an empty-flash before the first SSE tick.
- `sub_batch_index` on `ExecuteApprovedBatchPayload` defaults to 0 so single-chunk dispatches don't need to set it — keeps Phase 26 callers (if any latent test fixtures exist) compatible.
- The progress payload's `request_id` is uuid4-generated in the agent's `_execute_one` at the same call site as `execution_log_id = uuid4()` (line 89) — both UUIDs become job-local state and are reused on SAQ retries.
- `agent:<agent_id>:total` is pre-set at dispatch (D-09 step 5) so the HEXISTS-based cross-tenant check (D-17 step 4) gives a clean 403 for an agent that wasn't part of the dispatch — even if the agent is otherwise valid and reachable.
- Revoked-agent filter (D-09 step 2) reuses the SELECT pattern from `routers/pipeline_scans.py` (Phase 27 D-06) where revoked agents are excluded from the dropdown.
- The dispatch_summary on `exec:{batch_id}` is JSON-serialized into a single Redis hash field (`dispatch_summary`) at dispatch time and read raw by the SSE generator on first connect — saves a separate per-agent lookup loop.
- `complete_with_errors` is a new status value the SSE generator emits; the existing close-on-`complete` check at `routers/execution.py:74` becomes `if status in {"complete", "complete_with_errors"}:` — minimal change.
- The collision-block (`collision_block.html`) pre-check stays at the top of `start_execution`; Phase 28 dispatch runs only after no destination-path collisions are detected. The collision check is across ALL approved proposals globally, not per-agent — it would be confusing to surface "collision for proposal X on agent A" since the destination path is what collides, regardless of source agent.
- The fingerprint-locality banner copy: `"Fingerprint matches are scoped to the local file server's index. Cross-file-server matches are not supported in v4.0."` Add an inline link to the docs entry from D-13. Keep it short.

</specifics>

<deferred>
## Deferred Ideas

- **Per-sub-step PATCH-to-audit-log granularity (5-state ExecutionStatus).** D-01 chose the 2-state audit + Redis-only progress path. A future "audit forensics" milestone could extend `ExecutionStatus` and the monotonic ladder if operators frequently need to forensically slice by step on completed (not failed) rows.
- **Dedicated `/execution/batches/{batch_id}` page** with per-proposal drill-down and recent-batches list. D-08 chose to grow the existing card. A future "operations dashboard" phase could promote it to a top-level page.
- **`/audit/` batch filter + per-agent column.** Same reasoning — defer to operator-dashboard phase.
- **Cross-file-server fingerprint matching (XAGENT-01).** Documented limitation; agent-side orchestrator that fans out fingerprint queries to peer agents' sidecars is a v5.0 or later concern.
- **Real-time per-sub-step SSE counters that move per-step rather than per-file.** D-03 chose per-file granularity (one POST per proposal). For interactive operator UX on small batches (10..30 files) the current grain is fine; for very large batches (>1000) finer granularity might feel more alive — defer until operator feedback requests it.
- **`/dispatch` admin endpoint to inspect a batch's dispatch decision after the fact.** D-11 satisfies "visible in logs and via an admin endpoint" via the `dispatch_summary` field on the Redis hash echoed in SSE. A dedicated GET endpoint returning the dispatch decision as JSON is a future enhancement.
- **Scheduled re-execution of FAILED proposals (cron).** Operator currently re-approves manually. A future SAQ cron could pick up `FileRecord.state=FAILED` and re-enqueue periodically; out of scope for Phase 28.
- **Multi-batch dashboard (history view).** Phase 28's SSE is per-batch (`exec:{batch_id}`); a global "currently running batches across all operators" view is unnecessary for single-user scale.
- **Atomic "execution in progress" lock to prevent concurrent batches against overlapping proposals.** v4.0 personal-collection scale has one operator; idempotent PATCHes prevent state corruption even if a second batch started. Defer the lock until concurrent-batch workflows emerge.
- **Per-agent retry policies on the progress POST.** D-16 uses the standard Phase 26 D-11 tenacity policy. Per-agent overrides (slower hosts get longer backoff) deferred.
- **Banner localization / theming.** D-14 banner is plain English with a single dismissible state. Internationalization + theming deferred to a milestone-wide UI polish.
- **`dispatch_summary` as a queryable history.** Stored on the ephemeral `exec:{batch_id}` Redis hash (24h TTL). For a permanent history, we'd need a new `ExecutionBatch` table — defer until operator demands historical analytics.

</deferred>

---

*Phase: 28-distributed-execution-dispatch*
*Context gathered: 2026-05-14*
