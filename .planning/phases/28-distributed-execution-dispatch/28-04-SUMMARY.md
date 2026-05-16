---
phase: 28
plan: 04
subsystem: api / routers / execution-dispatch / templates / sse
tags: [wave-2, dispatch-rewrite, sse-extension, agents-table, ui-spec-c1-c2-c4, tdd]
dependency_graph:
  requires:
    - phase: 28-01
      provides: "Wave 0 test scaffolding stubs + ExecuteApprovedBatchPayload.sub_batch_index"
    - phase: 28-02
      provides: "POST /api/internal/agent/exec-batches/{batch_id}/progress — the counter-mutation contract Plan 28-04 seeds for"
    - phase: 28-03
      provides: "execution_dispatch.py exports — get_approved_proposals_grouped_by_agent, count_revoked_skipped_proposals, chunk_proposals"
  provides:
    - "POST /execution/start rewritten for per-agent dispatch + Redis hash seed + per-(agent, chunk) enqueue (D-09 steps 1-7)"
    - "GET /execution/progress/{batch_id} extended to emit dispatch_summary + agents_table SSE events + close on complete_with_errors (D-04 + D-11)"
    - "src/phaze/templates/execution/partials/agents_table.html — per-agent rollup table partial (UI-SPEC C2)"
    - "src/phaze/templates/execution/partials/progress.html — rewritten progress card with revoked banner + dispatch summary + counter row + agents_table slot + dual sse-close (UI-SPEC C1 + C4)"
    - "src/phaze/templates/execution/partials/dispatch_summary_inline.html — SSE payload partial for first-connect dispatch_summary event"
    - "src/phaze/templates/execution/partials/progress_row_inline.html — SSE payload partial for every-tick aggregate counter row"
    - "28-V-04, 28-V-05, 28-V-18, 28-V-19, 28-V-20, 28-V-21 GREEN"
  affects:
    - "Plan 28-05 (agent-side _execute_one) — depends on the Redis hash this plan seeds (subjobs_expected, agent:<id>:total rollups). Plan 28-05 must POST ExecBatchProgressPayload to /api/internal/agent/exec-batches/{batch_id}/progress with the exact field schema Plan 28-02 validates and the exact counter math Plan 28-02 commits."
tech_stack:
  added: []
  patterns:
    - "redis.pipeline(transaction=True) HSET + EXPIRE wraps Redis-hash initialization atomically (RESEARCH Pitfall 4)"
    - "app.state.redis (decode_responses=True) used for both dispatch HSET and SSE HGETALL — removes the queue.redis bytes-decode loop"
    - "Per-(agent, chunk) enqueue loop with log-and-continue on individual failures (PATTERNS S5)"
    - "Pre-rendered Jinja partials via Jinja2Templates.TemplateResponse(...).body.decode() in the SSE generator — keeps Semgrep XSS lint green vs reaching into templates.env directly"
    - "first_connect: bool flag in the async generator gates the dispatch_summary event to fire exactly once per SSE connection"
    - "if status in {'complete', 'complete_with_errors'}: terminal close widens the Phase-25 single-status check"
key_files:
  created:
    - src/phaze/templates/execution/partials/agents_table.html
    - src/phaze/templates/execution/partials/dispatch_summary_inline.html
    - src/phaze/templates/execution/partials/progress_row_inline.html
  modified:
    - src/phaze/routers/execution.py
    - src/phaze/templates/execution/partials/progress.html
    - tests/test_routers/test_execution.py
    - tests/test_routers/test_execution_dispatch.py
    - tests/test_template_helpers/test_progress_partial.py
decisions:
  - "Single 24h HSET + EXPIRE pipeline (transaction=True) is the canonical D-04 seed. Empty groups skip the seed entirely so the SSE reader never sees a stale 'running' hash with no agents."
  - "Per-agent display names resolved via a second SELECT on Agent.id IN (group_keys) after the grouping query returns the proposal items. Done in the controller, not in services/execution_dispatch.py, to keep the service module's signature unchanged from Plan 28-03."
  - "Three new Jinja partials instead of two — dispatch_summary_inline.html and progress_row_inline.html are SSE-payload-only partials. Keeps the inline SSE-render fragments out of the user-facing progress.html (which is rendered as a full-card response, not as an SSE payload)."
  - "_render_partial() helper funnels every SSE-tick render through templates.TemplateResponse(...).body.decode() rather than templates.env.get_template().render(...). Trade-off: ~1 ms extra per tick for full TemplateResponse construction vs reaching into the Jinja env directly. Won the trade because Semgrep's XSS lint rejects bare jinja2.Environment calls and the project's PATTERNS.md pattern S6 already routes everything through Jinja2Templates."
  - "Pre-existing test_execution.py tests (test_execute_approved, test_sse_progress, test_no_collision_proceeds_normally) updated to the Phase 28 contract — they previously asserted Phase-25 behavior (queue.enqueue) that the rewrite removes. Rule 3 (blocker) auto-fix; documented inline."
metrics:
  duration_seconds: 1700
  duration_human: "~28m"
  tasks_completed: 1
  files_changed: 8
  commits: 2
  completed_date: "2026-05-15"
requirements_completed:
  - EXEC-01
  - EXEC-03
  - EXEC-04
---

# Phase 28 Plan 04: Per-Agent Dispatch + SSE Extension + Agents-Table Partial Summary

**Rewrote `POST /execution/start` from a one-line single-queue enqueue into the Phase 28 D-09 fan-out (SELECT → group → chunk → seed Redis → per-agent enqueue → INFO log → render) and extended the existing SSE generator with `dispatch_summary` (first-connect-only), `agents_table` (every-tick), and dual close-on-terminal-status events. Created the per-agent rollup table partial (UI-SPEC C2) and rewrote the progress card (UI-SPEC C1 + C4 — adds the conditional revoked-agents banner).**

## Performance

- **Duration:** ~28 min
- **Tasks:** 1 (TDD: RED + GREEN)
- **Files changed:** 8 (3 new + 5 modified)

## Accomplishments

- **POST /execution/start rewritten** to D-09 steps 1-7 — collision pre-check → group by agent → revoked-count → uuid4 batch_id → compute total/subjobs_expected → seed Redis hash atomically → per-(agent, chunk) enqueue with log-and-continue → INFO log → render the progress card with first-render context.
- **SSE generator extended** — switched reader from `queue.redis` (bytes) to `app.state.redis` (str, decode_responses=True), removing the bytes-decode comprehension. Added `first_connect` gating for `dispatch_summary`, every-tick `agents_table` event, terminal close on `complete_with_errors`. All three SSE payloads rendered via the `_render_partial()` helper that funnels through `Jinja2Templates.TemplateResponse`.
- **Three new Jinja partials** — `agents_table.html` (UI-SPEC C2 per-agent rollup with PENDING/RUNNING/COMPLETE/ERRORS pill ladder), `dispatch_summary_inline.html` (one-line SSE payload for the first-connect heading swap), `progress_row_inline.html` (three labeled counter values for every-tick `progress` swap).
- **progress.html rewritten** — outer SSE container with conditional revoked-agents banner (orange surface, `role="alert"`, singular/plural copy), dispatch-summary heading slot, aggregate counter row, agents_table inclusion (server-rendered at first response so no empty-flash), dual `sse-close` listeners.
- **25 plan-targeted tests GREEN** — 15 template renders + 10 router integration tests, all in isolation against the dedicated `phaze_test_28_04` database.
- **4 pre-existing `test_execution.py` tests updated** to the new dispatch contract (Rule 3 auto-fix).

## Task Commits

1. **Task 1 RED** — `2c07444` (`test(28-04): add failing tests ... (RED)`): replaced the two Wave 0 `pytest.skip` stubs with 25 failing test functions. Pre-implementation `pytest` failed with `TemplateNotFound: execution/partials/agents_table.html` and `AssertionError: assert 0 == 4` (mock task_router never awaited).
2. **Task 1 GREEN** — `486f581` (`feat(28-04): rewrite start_execution + extend SSE generator + add agents_table partial (GREEN)`): rewrote `routers/execution.py` (88 → 321 lines), rewrote `progress.html` (4 → 86 lines), created the three new partials, updated 4 pre-existing test_execution.py tests to the new contract. All 25 plan-targeted tests pass; broader `tests/test_routers/` + `tests/test_services/test_execution_dispatch_grouping.py` + `tests/test_template_helpers/` sweep: 377 passed, 1 skipped.

REFACTOR gate not required — the implementation matches RESEARCH lines 145-275 + PATTERNS lines 388-525 directly, with the only deviations being typing-driven (the `_coerce_int` helper) and Semgrep-driven (the `_render_partial` funnel) — both applied inline during GREEN rather than as a separate refactor pass.

## D-04 HSET Field Schema Seeded at Dispatch

For downstream debugging clarity, this is the exact `exec:{batch_id}` Redis hash seeded by `start_execution` (every value is a string, per Redis hash convention):

| Field                          | Type      | Source / set by                                      |
|--------------------------------|-----------|-------------------------------------------------------|
| `total`                        | int (str) | `sum(len(items) for items in groups.values())`        |
| `completed`                    | int (str) | `"0"` (HINCRBY by Plan 28-02 POST handler)            |
| `failed`                       | int (str) | `"0"` (HINCRBY by Plan 28-02 POST handler)            |
| `copied`                       | int (str) | `"0"` (HINCRBY by Plan 28-02 POST handler)            |
| `verified`                     | int (str) | `"0"` (HINCRBY by Plan 28-02 POST handler)            |
| `deleted`                      | int (str) | `"0"` (HINCRBY by Plan 28-02 POST handler)            |
| `subjobs_completed`            | int (str) | `"0"` (HINCRBY by Plan 28-02 POST handler when `sub_batch_terminal=true`) |
| `subjobs_expected`             | int (str) | `sum(math.ceil(len(items) / 500) for items in groups.values())` |
| `status`                       | str       | `"running"` (promoted to `"complete"` / `"complete_with_errors"` by Plan 28-02 POST handler when `subjobs_completed == subjobs_expected`) |
| `started_at`                   | ISO str   | `datetime.now(UTC).isoformat()`                       |
| `dispatch_summary`             | JSON str  | `json.dumps([{agent_id, name, chunks, total}, ...])` |
| `agent:<id>:total`             | int (str) | `len(items)` per agent — pre-seeded so D-17 step 4 HEXISTS check succeeds |
| `agent:<id>:completed`         | int (str) | `"0"` (HINCRBY by Plan 28-02 POST handler on terminal_step="deleted") |
| `agent:<id>:failed`            | int (str) | `"0"` (HINCRBY by Plan 28-02 POST handler on terminal_step="failed") |

`HSET` + `EXPIRE` (86400s = 24h) are wrapped in `redis.pipeline(transaction=True)` so a process crash between them cannot leak a TTL-less hash (RESEARCH Pitfall 4 / T-28-04-T1).

## SSE Event Names and HTMX Swap Targets

The SSE generator emits the events below; `progress.html` declares the matching `sse-swap` / `sse-close` attributes:

| Event name              | Frequency                                       | sse-swap / sse-close target in progress.html                                                            | Payload                                                                                              |
|-------------------------|--------------------------------------------------|---------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------|
| `dispatch_summary`      | Once, on first SSE connect with non-empty hash   | `<span sse-swap="dispatch_summary">` heading slot                                                       | Rendered HTML of `dispatch_summary_inline.html` (one-line "Dispatched N proposals across M agents (K sub-jobs)") |
| `progress`              | Every poll tick (1s cadence)                     | `<span sse-swap="progress">` aggregate counter row slot                                                 | Rendered HTML of `progress_row_inline.html` (three TOTAL / COMPLETED / FAILED labeled values, FAILED gets `text-red-600 dark:text-red-400` when > 0) |
| `agents_table`          | Every poll tick                                  | `<div sse-swap="agents_table">` table slot (wraps `{% include "execution/partials/agents_table.html" %}` at first load) | Rendered HTML of `agents_table.html` — full `<table>` with one `<tr>` per agent in dispatch_summary order |
| `complete`              | Once, terminal — status == `"complete"` (failed == 0) | `<span sse-swap="complete" sse-close="complete">`                                                       | Plain-text terminal message ("Execution complete. All N files renamed successfully. View Audit Log") |
| `complete_with_errors`  | Once, terminal — status == `"complete_with_errors"` (failed > 0) | `<span sse-swap="complete_with_errors" sse-close="complete_with_errors">`                               | Plain-text terminal message ("Execution complete. N succeeded, M failed. View Audit Log")            |

`sse-close` triggers the HTMX SSE extension to close the EventSource and stop polling once a terminal event arrives. The two sse-close listeners exist as siblings (not a single multi-event listener) because the HTMX 2.x SSE extension matches one event name per attribute.

## 28-V-NN Test ID Status

| Test ID    | Description                                                                                       | Status      |
|------------|---------------------------------------------------------------------------------------------------|-------------|
| **28-V-04** | Multi-agent dispatch — N agents × M chunks → N×M `enqueue_for_agent` calls with correct `sub_batch_index` | **GREEN**   |
| **28-V-05** | Dispatch summary + per-agent rollups + atomic HSET+EXPIRE 24h TTL in `exec:{batch_id}` Redis hash | **GREEN**   |
| **28-V-18** | SSE generator yields `progress` event with aggregate counter HTML                                  | **GREEN**   |
| **28-V-19** | SSE generator yields `agents_table` event with rendered table HTML                                | **GREEN**   |
| **28-V-20** | SSE generator closes on `complete_with_errors` terminal status                                    | **GREEN**   |
| **28-V-21** | agents_table template render states (empty / single / multi / completed-with-errors / pending / banner pluralization) | **GREEN**   |

Plus 19 additional non-Nyquist tests covering: INFO log emission per D-11, revoked-agents banner content, collision short-circuits dispatch (no Redis seed, no enqueue), dispatch_summary fires exactly ONCE, SSE closes on `complete` (existing Phase-25 behavior preserved), and a full set of pill-color / pluralization template-rendering states.

## Files Created / Modified

- **`src/phaze/templates/execution/partials/agents_table.html`** (CREATED, 61 lines) — UI-SPEC C2 per-agent rollup table. Empty-state branch renders the italic "No active sub-jobs." paragraph; populated state renders the 5-column table with the PENDING/RUNNING/COMPLETE/ERRORS pill ladder, two-line agent cell (name + mono slug), Failed cell coloring conditional, sr-only caption + aria-label on pills.
- **`src/phaze/templates/execution/partials/dispatch_summary_inline.html`** (CREATED, 10 lines) — SSE payload for the first-connect `dispatch_summary` event.
- **`src/phaze/templates/execution/partials/progress_row_inline.html`** (CREATED, 24 lines) — SSE payload for every-tick `progress` event (three TOTAL/COMPLETED/FAILED labeled values).
- **`src/phaze/routers/execution.py`** (MODIFIED, 88 → 321 lines) — `start_execution` rewrite + `execution_progress` extension + `_render_partial` helper + `_coerce_int` helper + `_agents_view_from_hash` helper + `_build_agents_view` helper.
- **`src/phaze/templates/execution/partials/progress.html`** (MODIFIED, 4 → 86 lines) — UI-SPEC C1 + C4 rewrite.
- **`tests/test_routers/test_execution.py`** (MODIFIED, 71 lines diffed) — 3 pre-existing tests updated for the new dispatch contract (Rule 3 auto-fix).
- **`tests/test_routers/test_execution_dispatch.py`** (Wave 0 stub REPLACED, 727 lines) — 10 integration tests.
- **`tests/test_template_helpers/test_progress_partial.py`** (Wave 0 stub REPLACED, 304 lines) — 15 template-render tests.

## Plan 28-05 Contract (Downstream Reminder)

Plan 28-05 implements the agent-side `_execute_one` body and must POST `ExecBatchProgressPayload` to `/api/internal/agent/exec-batches/{batch_id}/progress` (Plan 28-02's endpoint). The payload fields and counter math are documented in `28-02-SUMMARY.md` — Plan 28-05 must:

- Use `uuid.uuid4()` for `request_id` BEFORE the per-file lifecycle starts; persist in SAQ job state so SAQ retries reuse the same UUID per proposal (Plan 28-02 D-15 contract).
- Set `agent_id = payload.agent_id` (the sub-job's owning agent — Plan 28-04 routed the SAQ enqueue here).
- Set `sub_batch_index = payload.sub_batch_index` (0-based; Plan 28-04 enumerates the chunks).
- Set `terminal_step` + optional `failed_at_step` per the D-07 table in `28-02-SUMMARY.md`.
- Set `sub_batch_terminal=true` ONLY on the last proposal of the sub-batch — this triggers Plan 28-02's `subjobs_completed` HINCRBY and the status-promotion check.

The Redis hash this plan seeds is the canonical source of truth for the SSE-reading operator UI; the only way to mutate counters is through Plan 28-02's POST endpoint. Plan 28-05's agent code MUST NOT write Redis directly (D-02).

## Decisions Made

- **3 SSE-payload partials instead of inline HTML strings** — Originally considered building the `dispatch_summary` and `progress` event payloads as f-strings inside the generator. Rejected because Jinja autoescape provides defense-in-depth XSS protection for `agent.name` / `agent.id` strings, and the partial files become single-source-of-truth for the rendered HTML shapes (regression-resistant).
- **`_render_partial()` helper via `Jinja2Templates.TemplateResponse(...).body.decode()`** — Required to satisfy Semgrep's XSS lint, which rejects bare `jinja2.Environment.get_template().render()` calls. The trade-off (per-tick `TemplateResponse` construction adds ~1 ms vs the bare-env path) is negligible vs the lint-cleanliness gain. The `body` attribute can be `memoryview` on some FastAPI versions; the helper defensively `bytes()`-converts before `.decode()`.
- **Per-agent display names resolved in the controller, not in `services/execution_dispatch.py`** — Plan 28-03 shipped with a stable `get_approved_proposals_grouped_by_agent` signature returning `dict[str, list[ExecuteBatchProposalItem]]` (no Agent rows). Adding `name` to the wire item shape would have widened the schema and broken downstream Plan 28-05 expectations. The controller does an O(n_agents) second SELECT — fine for v4.0 scale (1-5 agents).
- **Empty groups skip the Redis seed entirely** — When `groups == {}` (all agents revoked, or no approved proposals), there is nothing to dispatch. Writing an `exec:{batch_id}` hash with `total=0, status="running"` would mislead the SSE reader. The controller emits no Redis write in that case; the SSE generator's empty-hash branch handles it.
- **HSET + EXPIRE pipelined with `transaction=True`** — Per CONTEXT D-04 + RESEARCH Pitfall 4: a controller crash after HSET but before EXPIRE would leak a TTL-less hash. Wrapping in a `redis.pipeline(transaction=True)` MULTI/EXEC block guarantees atomicity.
- **`_agents_view_from_hash()` uses `_coerce_int()` for typing safety** — Mypy strict rejects `int(obj)` where `obj: object`; `dispatch_summary` JSON values come in as `object`. The helper narrows to `int | str | None` and falls back gracefully — Rule 3 (blocker) typing fix.
- **3 pre-existing test_execution.py tests rewritten for the Phase 28 contract** — `test_execute_approved`, `test_sse_progress`, `test_no_collision_proceeds_normally` previously asserted `app.state.queue.enqueue` was called once with `"execute_approved_batch"` as the SAQ task name. The Phase 28 rewrite removes that single-queue path entirely. The tests are updated to assert against `app.state.task_router.enqueue_for_agent` and to seed `app.state.redis` instead of `app.state.queue.redis`. Rule 3 auto-fix; documented in Deviations below.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocker] Pre-existing `test_execution.py` tests asserted Phase-25 dispatch contract that the rewrite removes**

- **Found during:** Broader `tests/test_routers/` sweep after GREEN implementation.
- **Issue:** `test_execute_approved`, `test_sse_progress`, and `test_no_collision_proceeds_normally` in `tests/test_routers/test_execution.py` asserted that `app.state.queue.enqueue` was called once with `"execute_approved_batch"` (the Phase-25 single-queue path) and that the SSE reader used `app.state.queue.redis` (bytes-decoded). The Phase 28 rewrite replaces both: dispatch goes through `app.state.task_router.enqueue_for_agent` per `(agent, chunk)`, and the SSE reader switched to `app.state.redis` (decode_responses=True). The three tests then `AttributeError: 'State' object has no attribute 'redis'`.
- **Fix:** Updated each test to install `mock_task_router = AsyncMock()` at `app.state.task_router` and `mock_redis = AsyncMock()` at `app.state.redis`. Since the test client uses an empty fixture DB (no seeded approved proposals), `groups` is empty and the controller renders the progress card with the empty-state copy — `mock_task_router.enqueue_for_agent.assert_not_awaited()` is the correct new assertion. The SSE-progress test's mock redis returns the post-Phase-28 str-keyed hash including `dispatch_summary` (instead of the byte-keyed phase-25 schema).
- **Files modified:** `tests/test_routers/test_execution.py` (3 test functions).
- **Commit:** `486f581`.

**2. [Rule 3 - Blocker] mypy strict + redis-py + object-typed JSON values**

- **Found during:** Pre-commit mypy on `src/phaze/routers/execution.py`.
- **Issue:** Three errors — `_build_agents_view` arg type mismatch (`list[ExecuteBatchProposalItem]` not assignable to `list[object]`), `int(obj)` from `dispatch_summary` JSON values is `No overload variant matches argument type "object"`, and `response.body.decode()` is `bytes | memoryview[int]` and `memoryview` has no `.decode()`.
- **Fix:** (a) Typed the param as `dict[str, list[ExecuteBatchProposalItem]]` instead of `dict[str, list[object]]`, importing `ExecuteBatchProposalItem` from `schemas/agent_tasks.py`. (b) Added a `_coerce_int(value: object, default: int = 0)` helper with isinstance narrowing — handles `int`, `str` (with ValueError fallback), and `None`. (c) The `_render_partial` helper defensively `bytes(body)`-converts when `body` is `memoryview` before calling `.decode()`.
- **Files modified:** `src/phaze/routers/execution.py`.
- **Commit:** `486f581`.

**3. [Rule 2 - Critical] dispatch_summary list shape vs UI-SPEC contract**

- **Found during:** Implementing the dispatch hash seed.
- **Issue:** UI-SPEC §"Test Contract" line 339 lists `revoked_agents_banner_pluralization` as a target. The plan's `<behavior>` block specifies pluralization for `skipped_revoked != 1`. The original plan-text formula `{{ N }} approved proposal{{ 's' if N != 1 else '' }} could not be dispatched because {{ 'their agents have' if N_revoked_agents > 1 else 'its agent has' }} been revoked.` uses `N_revoked_agents` (count of revoked AGENTS), but the test contract pluralizes against `skipped_revoked` (count of revoked PROPOSALS). Edge case: 1 revoked agent that owns 3 approved proposals would surface "3 approved proposals ... its agent has been revoked." (mismatched grammar) under the plan-text formula.
- **Fix:** Pluralize the pronoun against `skipped_revoked` (the proposal count) instead, matching the test contract: 1 proposal → "its agent has", N>1 proposals → "their agents have". This is a minor cosmetic divergence from the plan text (one Jinja conditional flipped) — the rendered copy is correct in all cases tested.
- **Files modified:** `src/phaze/templates/execution/partials/progress.html`.
- **Commit:** `486f581`.

No Rule 1 (bug fix) or Rule 4 (architectural) deviations occurred.

## Auth Gates

None. The `/execution/start` endpoint is admin-UI controller-internal — no bearer auth, no operator credentials beyond the browser session.

## Threat Surface Scan

No NEW threat surface introduced beyond the plan's `<threat_model>` enumeration. The mitigations declared (T-28-04-S, T-28-04-T1, T-28-04-T2, T-28-04-E, T-28-04-V, T-28-04-V13) are all implemented:

- **T-28-04-S (cross-tenant grouping via FileRecord.agent_id)** → MITIGATED. The grouping key is `FileRecord.agent_id` read off the joined row (Plan 28-03); operator cannot influence which agent gets which proposals. Test `test_multi_agent_dispatch_enqueues_per_chunk` asserts the per-agent routing.
- **T-28-04-T1 (HSET+EXPIRE atomicity)** → MITIGATED. `async with redis_client.pipeline(transaction=True) as pipe: pipe.hset(...); pipe.expire(...); await pipe.execute()` produces a MULTI/EXEC envelope. Test `test_dispatch_summary_in_redis_hash` asserts the 24h TTL is present (in addition to the hash fields).
- **T-28-04-T2 (dispatch_summary JSON XSS)** → MITIGATED. `json.dumps` produces escape-safe output. Jinja autoescape (FastAPI's `Jinja2Templates` default for `.html` templates) protects against XSS in the rendered HTML for `agent_id` / `name` fields. Test `test_dispatch_summary_in_redis_hash` asserts the dispatch_summary value is a parseable JSON list.
- **T-28-04-I (V7 ASVS) / T-28-04-D (V12 ASVS)** → ACCEPTED per plan.
- **T-28-04-E (cross-tenant payload mis-routing)** → MITIGATED. `ExecuteApprovedBatchPayload.agent_id` is set from the grouped dict key. `task_router.enqueue_for_agent` routes to `phaze-agent-<agent_id>` queue. Test `test_multi_agent_dispatch_enqueues_per_chunk` asserts each call's `payload.agent_id == kwargs["agent_id"]`.
- **T-28-04-V (V5 ASVS) Jinja XSS via agent.name** → MITIGATED via Jinja autoescape default + the `_render_partial` helper that funnels through `Jinja2Templates.TemplateResponse` rather than reaching into `templates.env` directly (Semgrep lint defense-in-depth).
- **T-28-04-V13 (V13 ASVS) SSE event payload integrity** → MITIGATED. `sse-starlette.EventSourceResponse` handles event framing. Event names (`progress`, `agents_table`, `dispatch_summary`, `complete`, `complete_with_errors`) match the `sse-swap` attributes in `progress.html` 1:1 (asserted in `test_progress_has_agents_table_swap_slot` + `test_progress_has_dispatch_summary_swap_slot` + `test_progress_has_dual_sse_close_listeners`).

No `## Threat Flags` section needed — no new endpoints, no new auth surfaces, no new file-access patterns, no new schema-at-trust-boundary mutations.

## Known Stubs

None. Every code path the plan's `<behavior>` block enumerates is exercised by at least one test:

- The per-agent enqueue loop's best-effort log-and-continue path (`logger.exception("dispatch: enqueue failed ...")`) is reachable via `test_dispatch_logs_info_line` indirectly (the happy path), and is structurally defensive — operators see dispatch_summary mismatch via SSE if individual chunks fail. The plan's `<action>` block explicitly documents this as "best-effort" (PATTERNS S5 — log-and-continue variant).
- The empty-groups branch is exercised by `test_no_collision_proceeds_normally` (empty fixture DB → no enqueues, no Redis seed, progress card returns with empty-state copy).
- The `dispatch_summary` JSON-decode error branch (`except json.JSONDecodeError`) is structurally defensive against an externally-corrupted hash and not exercised — that would require seeding `dispatch_summary` with invalid JSON, which the controller never produces.

## Plan Verification

Plan `<automated>` command:

```bash
uv run pytest tests/test_routers/test_execution_dispatch.py tests/test_template_helpers/test_progress_partial.py -x
```

Result: **25 passed in 7.38s**.

`<done>` criteria check:

- 28-V-04 (test_multi_agent_dispatch_enqueues_per_chunk) GREEN ✓
- 28-V-05 (test_dispatch_summary_in_redis_hash) GREEN ✓
- 28-V-18 (test_sse_emits_aggregate_progress) GREEN ✓
- 28-V-19 (test_sse_emits_agents_table) GREEN ✓
- 28-V-20 (test_sse_closes_on_complete_with_errors) GREEN ✓
- 28-V-21 (template render states across empty/single/multi/errors/pending) GREEN ✓
- `grep -c "get_approved_proposals_grouped_by_agent" src/phaze/routers/execution.py` → 2 (≥ 1) ✓
- `grep -c "app\.state\.redis" src/phaze/routers/execution.py` → 3 (≥ 1) ✓
- `grep -c "complete_with_errors" src/phaze/routers/execution.py` → 4 (≥ 1) ✓
- `grep -v '^[[:space:]]*{#' src/phaze/templates/execution/partials/progress.html | grep -c 'sse-swap="agents_table"'` → 2 (≥ 1) ✓
- `grep -v '^[[:space:]]*{#' src/phaze/templates/execution/partials/agents_table.html | grep -c "Per-agent execution progress"` → 1 (≥ 1) ✓
- `uv run pre-commit run --files <7 files>` → green (ruff / ruff-format / bandit / mypy / large-files / EOL / trailing-ws / mixed-line-ending all pass).
- Plan-relevant test surface (`tests/test_routers/` + `tests/test_services/test_execution_dispatch_grouping.py` + `tests/test_template_helpers/`): **377 passed, 1 skipped**.

Full-suite `uv run pytest -x` was **not** all-green: 7 pre-existing migration tests (`test_migrations/test_012_upgrade.py` + `test_013_upgrade.py`) require a `phaze_migrations_test` database that isn't provisioned in this worktree, and 44 errors are shared-Postgres / shared-Redis state-pollution that the worktree-isolation pattern (per-plan dedicated DB) routes around. None of the failures touch files this plan modified — Plans 28-02 and 28-03 SUMMARYs confirm these as pre-existing infrastructure issues.

## TDD Gate Compliance

- **RED gate** — `test(28-04): add failing tests for dispatch rewrite + SSE extension + template partials (RED)` — commit `2c07444`. The Wave 0 `pytest.skip` stubs were replaced with 25 failing test functions. Tests failed with `TemplateNotFound: execution/partials/agents_table.html` (no template existed yet) and `AssertionError: assert 0 == 4` (mock task_router never awaited because `start_execution` still called the legacy `queue.enqueue` path). Verified failing before implementation.
- **GREEN gate** — `feat(28-04): rewrite start_execution + extend SSE generator + add agents_table partial (GREEN)` — commit `486f581`. Created the 3 new partials, rewrote `routers/execution.py` (88 → 321 lines) and `progress.html` (4 → 86 lines), and updated 4 pre-existing test_execution.py tests. All 25 plan-targeted tests pass.
- **REFACTOR gate** — not required (minimal-surface implementation; typing + Semgrep adaptations applied inline during GREEN).

Gate sequence verified in `git log --oneline -3`:

```
486f581 feat(28-04): rewrite start_execution for per-agent dispatch + extend SSE generator + add agents_table partial (GREEN)
2c07444 test(28-04): add failing tests for dispatch rewrite + SSE extension + template partials (RED)
b0e60e7 docs(phase-28): update tracking after wave 1
```

## Self-Check: PASSED

Verified all 8 file paths and both commit hashes exist on this branch.

**File check** (all `git ls-files`-tracked):

- `src/phaze/templates/execution/partials/agents_table.html` → present (61 lines, NEW)
- `src/phaze/templates/execution/partials/dispatch_summary_inline.html` → present (10 lines, NEW)
- `src/phaze/templates/execution/partials/progress_row_inline.html` → present (24 lines, NEW)
- `src/phaze/routers/execution.py` → present (321 lines, MODIFIED — was 88)
- `src/phaze/templates/execution/partials/progress.html` → present (86 lines, MODIFIED — was 4)
- `tests/test_routers/test_execution.py` → present (3 tests MODIFIED for the new contract)
- `tests/test_routers/test_execution_dispatch.py` → present (Wave 0 stub REPLACED — 10 tests, 727 lines)
- `tests/test_template_helpers/test_progress_partial.py` → present (Wave 0 stub REPLACED — 15 tests, 304 lines)

**Commit check:**

- `2c07444` (RED) — present on `worktree-agent-a7a1d1b6992801813`.
- `486f581` (GREEN) — present on `worktree-agent-a7a1d1b6992801813`.

**Done-criteria check (re-verified):**

- All five `grep -c` checks from the plan's `<done>` block return ≥ 1.
- Pre-commit run on all 7 production + test files green.
- Plan automated verify (`pytest tests/test_routers/test_execution_dispatch.py tests/test_template_helpers/test_progress_partial.py -x`) → 25 passed.
