# Phase 28: Distributed Execution Dispatch - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-05-14
**Phase:** 28-distributed-execution-dispatch
**Areas discussed:** Per-operation PATCH granularity, exec:{batch_id} aggregation ownership, Dispatch UI + per-agent breakdown surface, Sub-batch size + multi-job-per-agent, TASK-04 sidecar surfacing

---

## Per-operation PATCH granularity

| Option | Description | Selected |
|--------|-------------|----------|
| Option A | Extend ExecutionStatus enum + monotonic ladder to 5 states (started, copied, verified, deleted, failed). One POST + four PATCHes per file. Richer audit but adds Alembic migration, quadruples per-file HTTP call count (~800 for a 200-file batch), extends monotonic-ladder logic in `agent_execution.py`. | |
| Option B + C | 2-state ExecutionLog audit stays (`IN_PROGRESS → COMPLETED \| FAILED`). Per-operation progress (started, copied, verified, deleted) goes into `exec:{batch_id}` Redis HINCRBYs only. Failed rows put `<step>: <reason>` in `error_message` (current `_execute_one` code already does this — Phase 28 locks it as the contract). | ✓ |
| Option B only | Redis-only progress, audit stays 2-state, no `error_message` contract. Cheapest, but failure forensics rely entirely on agent-side logs. | |

**User's choice:** Option B + C (Recommended).
**Notes:** Audit trail survives the HTTP boundary per the roadmap; the per-step counters surface only in the live SSE view. The `error_message` `<step>: <reason>` prefix gives operators a mechanical way to slice failures by sub-step without touching agent host logs.

---

## exec:{batch_id} aggregation ownership

| Option | Description | Selected |
|--------|-------------|----------|
| Option A+B hybrid | Existing PATCHes (`/proposals/{id}/state`, `/execution-log/{id}`) piggyback `completed`/`failed` counter writes. New `/progress` endpoint carries sub-step deltas only. Two write paths — more complex coupling. | |
| Option B | New POST `/api/internal/agent/exec-batches/{batch_id}/progress` per-file with 4 PATCH calls per file (after copy, after verify, after delete, on failure). Real-time SSE but 4× the HTTP traffic and 4× the retry surface. | |
| Option D | Same new endpoint, but called ONCE per file at terminal state with the final step reached (`copied`/`verified`/`deleted`/`failed`) and `failed_at_step` on failure. Controller HINCRBYs all the steps the file actually completed. ~200 POSTs for a 200-file batch, SSE moves in file-sized jumps. | ✓ |

**User's choice:** Option D (Recommended).
**Notes:** Given D-01 locked sub-step counters in Redis-only, the app server has to own the Redis hash regardless. Option D is the lowest-traffic shape that still delivers the sub-step counters; sub-step granularity in SSE is preserved at the *aggregate* level even though individual file events only fire at terminal state.

---

## Dispatch UI + per-agent breakdown surface

| Option | Description | Selected |
|--------|-------------|----------|
| Option A | Expand the existing SSE progress card (`templates/execution/partials/progress.html`) with a per-agent table. Same trigger, same card, minimal new UI surface. | ✓ |
| Option B | Dedicated `/execution/batches/{batch_id}` page with per-agent table + per-proposal drill-down + recent-batches list. Operator-friendly for debugging multi-agent partial failures, but heavier UI surface. | |
| Option C | Extend `/audit/` with a batch filter + per-agent column. Reuses audit chrome but SSE in audit-log UX is awkward; aggregate progress would need a separate banner. | |

**User's choice:** Option A (Recommended).
**Notes:** Fits the Phase 27 "progress lives where you triggered it" pattern. The card grows from a one-line counter into a small server-rendered table updated via HTMX SSE-swap. Drill-down for debugging is deferred to a future operations-dashboard phase.

---

## Sub-batch size + multi-job-per-agent

| Option | Description | Selected |
|--------|-------------|----------|
| Option A | Fail-fast on overflow: any agent group >500 → 400 from `/execution/start` ("approve in waves"). Aggregator is simple (one sub-job per agent), but bad UX for bulk-approve. | |
| Option B | Chunk per agent into N sub-jobs under the same parent `batch_id`. Each sub-job carries `sub_batch_index`. Aggregator tracks `subjobs_completed` vs `subjobs_expected`. Handles real bulk-approve cases without operator intervention. | ✓ |

**User's choice:** Option B (Recommended).
**Notes:** `ExecuteApprovedBatchPayload.proposals` cap of 500 stays per Phase 26 D-22. A 1500-proposal agent group becomes 3 sub-jobs. The agent reports `sub_batch_terminal=true` on its last file in each sub-job; the controller increments `subjobs_completed` and flips `status` to `complete`/`complete_with_errors` when it reaches `subjobs_expected`.

---

## TASK-04 sidecar surfacing (no cross-file-server fingerprint)

| Option | Description | Selected |
|--------|-------------|----------|
| Option A | Structural test + docs only, no admin UI banner. Operator only learns the limitation if they read docs. | |
| Option B | Test + admin UI banner only, no docs entry. Banner is operator-visible but docs lack the canonical statement. | |
| Option C | Test + docs + admin UI banner. Structurally verified, documented in PROJECT.md, AND visible to operator on the fingerprint matches page. | ✓ |

**User's choice:** Option C (Recommended).
**Notes:** Structural test asserts AudfprintAdapter / PanakoAdapter accept only localhost URLs (pydantic-settings field validator). Docs entry in PROJECT.md "Constraints" section. Banner partial `templates/_partials/cross_fs_fingerprint_notice.html` inserted above fingerprint matches page (planner picks the exact host page during pattern-mapping); dismissible per session but re-appears on next page load.

---

## Claude's Discretion

- Exact Redis hash field naming (`agent:<id>:completed` vs `agent.<id>.completed`) — colon recommended, matches existing Redis idioms.
- SSE poll cadence — keep 1s; D-03's per-file granularity doesn't need faster polling.
- Dispatch summary placement above vs below the aggregate row — above is recommended.
- `sub_batch_index` 0-based vs 1-based — 0-based.
- Progress POST logged at DEBUG vs INFO — DEBUG (matches Phase 26 D-13).
- Pre-set vs lazy per-agent rollup hash keys at dispatch — pre-set (makes HEXISTS the cross-tenant guard).
- `dispatch_summary` SSE event on first connect only vs every tick — first connect only.
- HTMX SSE library — keep current `hx-ext="sse"` pattern.
- Banner blocking vs inline — inline-above; never block.
- Router prefix `/api/internal/agent/exec-batches` vs symmetry with `/execution-log` — use `exec-batches` (collision-free, semantically distinct).

## Deferred Ideas

- Per-sub-step PATCH-to-audit-log granularity (5-state ExecutionStatus enum) — deferred; D-01 chose 2-state.
- Dedicated `/execution/batches/{batch_id}` page with drill-down — deferred; D-08 chose the inline card.
- `/audit/` batch filter + per-agent column — deferred.
- Cross-file-server fingerprint matching (XAGENT-01) — out of scope per PROJECT.md.
- Real-time per-sub-step SSE counters — D-03 chose per-file grain.
- Dedicated `/dispatch` admin endpoint — `dispatch_summary` on Redis hash echoed via SSE is sufficient (D-11).
- Scheduled re-execution of FAILED proposals (cron) — deferred.
- Multi-batch dashboard / history view — defer until v5.0.
- Atomic "execution in progress" lock — idempotency invariants make this unnecessary at single-user scale.
- Per-agent retry policies — keep Phase 26 D-11 tenacity policy.
- Banner localization / theming — defer to milestone UI polish.
- `dispatch_summary` queryable history — deferred (would need new `ExecutionBatch` table).
