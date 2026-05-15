---
phase: 28
plan: 02
subsystem: agent-internal-router / schema / agent-client
tags: [wave-1, exec-progress-endpoint, redis-counter-math, tdd, idempotency]
dependency_graph:
  requires:
    - "28-01 (Wave 0 test scaffolding stubs replaced by this plan)"
  provides:
    - "POST /api/internal/agent/exec-batches/{batch_id}/progress (D-05)"
    - "ExecBatchProgressPayload schema (D-06)"
    - "_compute_increments helper (D-07 counter math)"
    - "PhazeAgentClient.post_exec_batch_progress (D-16)"
    - "exec:{batch_id} HINCRBY contract — Plan 28-04 (controller dispatch) seeds the hash; Plan 28-05 (agent task) calls this endpoint"
  affects:
    - src/phaze/main.py
tech_stack:
  added:
    - "redis-py pipeline(transaction=False) for batched HINCRBY"
  patterns:
    - "Stripe-style SET NX EX 3600 idempotency on `exec_progress_req:{request_id}` (Phase 26-07)"
    - "Cross-tenant 403 BEFORE state read (Phase 26 D-08 timing-side-channel)"
    - "Pydantic ConfigDict(extra='forbid') + @model_validator(mode='after') for cross-field invariants"
    - "redis-py Awaitable[T] | T overload: typing.cast('Awaitable[T]', ...) for mypy"
    - "PhazeAgentClient._request funnel inheritance for tenacity retry policy"
key_files:
  created:
    - src/phaze/schemas/agent_exec_batches.py
    - src/phaze/routers/agent_exec_batches.py
  modified:
    - src/phaze/services/agent_client.py
    - src/phaze/main.py
    - tests/test_schemas/test_agent_exec_batches.py
    - tests/test_routers/test_agent_exec_batches.py
    - tests/test_services/test_agent_client_exec_batch_progress.py
decisions:
  - "POST `/api/internal/agent/exec-batches/{batch_id}/progress` is the SINGLE Redis-hash mutation point (D-02). Agents never write Redis directly — this enforces the v4.0 HTTP-only boundary at the execution layer."
  - "Cross-tenant 403 fires BEFORE any HEXISTS/HGET so a leaked batch_id cannot be probed via 404-vs-403 timing (T-28-02-S1 / T-28-02-I1)."
  - "Per-agent rollup field absence (`agent:<id>:total`) is the structural cross-tenant guard — pre-seeded at dispatch (Plan 28-04 D-09 step 5) and HEXISTS-checked here (D-17 step 4)."
  - "Idempotency uses SET NX EX 3600 on `exec_progress_req:{request_id}` — duplicate POSTs return 200 with no HINCRBY (D-15)."
  - "HINCRBY pipelining uses `transaction=False`; per-field HINCRBYs are commutative so no MULTI/EXEC is needed (~1 round-trip vs N)."
  - "Status promotion only runs when `sub_batch_terminal=true` AND `subjobs_completed == subjobs_expected` post-increment — avoids polling the equality check on every progress POST."
  - "`from __future__ import annotations` is intentionally omitted in the router module so FastAPI can resolve `Annotated[redis_async.Redis, Depends(_get_redis)]` at app-build time (matches agent_tracklists.py / agent_scan_batches.py convention)."
  - "Per-test database isolation: tests in this plan honour `PHAZE_TEST_DATABASE_URL_28_02` (worktree-dedicated DB) to avoid colliding with the parallel Plan 28-03 pytest on the default `phaze_test` database."
metrics:
  duration_seconds: 957
  duration_human: "~15m57s"
  tasks_completed: 1
  files_changed: 7
  commits: 2
  completed_date: "2026-05-15"
---

# Phase 28 Plan 02: exec-batch progress endpoint + schema + agent client method Summary

End-to-end implementation of the Phase 28 D-05/D-06/D-07/D-15/D-17 contract — the per-proposal terminal-state progress POST that is the SINGLE mutation point for the `exec:{batch_id}` Redis hash (D-02). 7 files (4 production + 3 tests) shipped as one coupled change set behind a clean TDD RED → GREEN gate; 41 tests green; 28-V-10..28-V-17 + 28-V-25 are GREEN.

## What Was Built

### New endpoint contract

**`POST /api/internal/agent/exec-batches/{batch_id}/progress`** — bearer-auth-protected, returns 200 with empty body. Handler ordering (the ORDER is part of the contract):

1. **401** if no bearer token (auth dep).
2. **403** if `body.agent_id != agent.id` — cross-tenant guard fires BEFORE any Redis state read (D-17 step 2; T-28-02-S1 / T-28-02-I1).
3. **404** if `exec:{batch_id}` hash absent (`HEXISTS total == 0`) — opaque detail `"batch not found"` (unknown == expired).
4. **403** if `agent:<body.agent_id>:total` rollup field absent (D-17 step 4) — caller wasn't part of this dispatch.
5. **SET NX EX 3600** on `exec_progress_req:{request_id}` — duplicate returns 200 with no HINCRBY (D-15).
6. **HINCRBY** pipelined counters per D-07 rules.
7. If `sub_batch_terminal=true`, HINCRBY `subjobs_completed` and promote `status` to `"complete"` / `"complete_with_errors"` when `subjobs_completed == subjobs_expected`.

### `ExecBatchProgressPayload` (D-06 wire format)

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

    @model_validator(mode="after")
    def _check_failed_at_step_coupling(self) -> "ExecBatchProgressPayload":
        # failed_at_step is required iff terminal_step == "failed"
```

**Cross-field invariant under test:** `failed_at_step is None iff terminal_step != "failed"`. Both directions enforced (failed without failed_at_step → ValidationError; non-failed with failed_at_step → ValidationError). 16 unit tests cover the validator, `extra="forbid"`, Literal narrowing, defaults, and JSON round-trip.

### `_compute_increments` (D-07 counter math, pure function)

| `terminal_step` | `failed_at_step` | Increments |
|-----------------|------------------|------------|
| `"deleted"`     | (must be None)   | `copied=+1, verified=+1, deleted=+1, completed=+1, agent:<id>:completed=+1` |
| `"verified"`    | (must be None)   | `copied=+1, verified=+1` |
| `"copied"`      | (must be None)   | `copied=+1` |
| `"failed"`      | `"copy"`         | `failed=+1, agent:<id>:failed=+1` |
| `"failed"`      | `"verify"`       | `failed=+1, agent:<id>:failed=+1, copied=+1` |
| `"failed"`      | `"delete"`       | `failed=+1, agent:<id>:failed=+1, copied=+1, verified=+1` |

**Status promotion** (only when `sub_batch_terminal=true`): after the increment-pipeline executes, the handler re-reads `subjobs_completed` and `subjobs_expected`. When they're equal post-increment, `status` is HSET to `"complete"` (if `failed == 0` at read time) or `"complete_with_errors"`.

### `PhazeAgentClient.post_exec_batch_progress`

```python
async def post_exec_batch_progress(
    self,
    batch_id: uuid.UUID,
    payload: ExecBatchProgressPayload,
) -> None:
    await self._request(
        "POST",
        f"/api/internal/agent/exec-batches/{batch_id}/progress",
        json=payload.model_dump(mode="json"),
    )
    return None
```

Returns `None` (mirrors `heartbeat()`). Inherits Phase 26 D-11 tenacity policy via `_request` — 5xx retries 3x with exponential-jitter, 4xx surfaces immediately as `AgentApiClientError`, persistent failure raises `AgentApiServerError`. No new retry code; no new error-handling. 7 respx tests cover URL contract, body serialization (including `failed_at_step` on failed payloads), 4xx-no-retry (422 + 404), 5xx-3x-retry, 500-then-200 succeeds-on-retry, and ConnectError retry semantics.

### main.py wiring

Added `agent_exec_batches` to the alphabetical-ish import cluster (line 17) and `app.include_router(agent_exec_batches.router)` immediately after `agent_scan_batches.router` (line 122-125). 2 occurrences of `agent_exec_batches` in main.py (verifies the `<done>` grep criterion `≥ 2`).

### TDD RED → GREEN sequence

- **RED commit `ac0052b`** (`test(28-02): replace Wave 0 stubs with failing schema/router/client tests`): replaced 3 Wave 0 `pytest.skip(allow_module_level=True)` stubs with the full test suite (40 tests). All tests failed with `ModuleNotFoundError` because the production modules didn't yet exist.
- **GREEN commit `3e012e0`** (`feat(28-02): add exec-batch progress endpoint + schema + agent client method`): created 2 new production files (schema + router), modified 2 existing files (main.py + agent_client.py), and patched the router test to honour a worktree-dedicated DB env override. All 41 tests now pass.

## 28-V-NN Test ID Status

| Test ID | Description | Status |
|---------|-------------|--------|
| **28-V-10** | Unauthenticated POST -> 401 | **GREEN** |
| **28-V-11** | `body.agent_id != agent.id` -> 403 BEFORE any Redis read | **GREEN** |
| **28-V-12** | Unknown `exec:{batch_id}` hash -> 404 | **GREEN** |
| **28-V-13** | Per-agent rollup absent (non-participating agent) -> 403 | **GREEN** |
| **28-V-14** | Duplicate `request_id` -> 200, no double HINCRBY | **GREEN** |
| **28-V-15** | Counter math (4 terminal_step × 3 failed_at_step branches) | **GREEN** |
| **28-V-16** | `sub_batch_terminal=true` promotes status to complete / complete_with_errors / unchanged | **GREEN** |
| **28-V-17** | Schema cross-field validator + extra="forbid" + Literal narrowing | **GREEN** |
| **28-V-25** | `PhazeAgentClient.post_exec_batch_progress` happy / 4xx / 5xx / ConnectError | **GREEN** |

41 tests pass in the new files; plus 124 adjacent tests (schemas/, agent_client, agent_client_endpoints, fingerprint_locality) continue to pass — no regressions in the non-DB-integration test surface this plan can plausibly affect.

## Counter math invariant table (for downstream plans)

Plan 28-05 (agent-side `_execute_one` body) will fire one `api.post_exec_batch_progress(...)` per proposal at terminal state. The table below is the contract Plan 28-05 commits to — it must construct the payload such that the controller's HINCRBYs land on the right counters:

| Agent observation (after `_execute_one`)                                       | `terminal_step` | `failed_at_step` | Controller HINCRBYs                                                                          |
|--------------------------------------------------------------------------------|-----------------|------------------|----------------------------------------------------------------------------------------------|
| copy+verify+delete all succeeded                                               | `"deleted"`     | `None`           | `copied=+1, verified=+1, deleted=+1, completed=+1, agent:<id>:completed=+1`                  |
| copy+verify succeeded, delete failed (proposal_state=executed; warning logged) | `"verified"`    | `None`           | `copied=+1, verified=+1` (no `deleted`, no `completed`)                                      |
| copy succeeded, verify+delete failed (rare; FileRecord MOVED but unverified)   | `"copied"`      | `None`           | `copied=+1` (no `verified`, no `deleted`, no `completed`)                                    |
| copy failed                                                                    | `"failed"`      | `"copy"`         | `failed=+1, agent:<id>:failed=+1` (nothing else)                                             |
| copy succeeded, verify failed                                                  | `"failed"`      | `"verify"`       | `failed=+1, agent:<id>:failed=+1, copied=+1`                                                 |
| copy+verify succeeded, delete failed (and proposal_state=failed)               | `"failed"`      | `"delete"`       | `failed=+1, agent:<id>:failed=+1, copied=+1, verified=+1`                                    |
| ANY of the above on the LAST item of the sub-batch                             | (any)           | (any)            | All of the above + `subjobs_completed=+1`; if `subjobs_completed == subjobs_expected` AND `failed == 0` -> `status="complete"`, else `status="complete_with_errors"` |

The agent uses `uuid.uuid4()` for `request_id` BEFORE the per-file lifecycle starts and persists it in SAQ job state so SAQ retries reuse the same UUID per proposal (Plan 28-05 D-15 contract — this plan's endpoint is the receiver).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Tooling] redis-py overloaded async return types tripped mypy strict mode**

- **Found during:** Pre-commit mypy on `src/phaze/routers/agent_exec_batches.py`.
- **Issue:** The redis-py type stubs declare `Redis.hexists`, `Redis.hget`, `Redis.hincrby`, and `Pipeline.hincrby` with overloaded return types `Awaitable[T] | T` (a single set of stubs covers both the sync and async client). `await` against a `... | T` union confuses mypy in strict mode with errors like `Incompatible types in "await" (actual type "Awaitable[bool] | bool", expected type "Awaitable[Any]")`. The existing `agent_tracklists.py` doesn't hit this because it uses `.get` / `.set` whose stubs declare `Awaitable[Any] | Any` (mypy accepts `Any` as awaitable).
- **Fix:** Wrap each affected call site with `typing.cast("Awaitable[T]", redis_client.<method>(...))` using a string-quoted forward reference. `Awaitable` is imported in a `TYPE_CHECKING` block (ruff TCH compliance — never imported at runtime).
- **Files modified:** `src/phaze/routers/agent_exec_batches.py` (import block + 7 cast sites).
- **Commit:** `3e012e0`.

**2. [Rule 3 - Blocker] Concurrent pytest collision on shared `phaze_test` database**

- **Found during:** First test run after GREEN implementation.
- **Issue:** This plan executed in parallel with Plan 28-03 in a sibling worktree (`agent-a41cdd3f0f79b379c`). Both worktrees share a single host-level Postgres container at `localhost:5432`, and `tests/conftest.py:async_engine` does `Base.metadata.create_all` + `INSERT Agent(id="legacy-application-server", ...)` at fixture setup. The two pytest processes raced on the legacy-agent INSERT, producing `UniqueViolationError: duplicate key value violates unique constraint "pk_agents"` errors.
- **Fix:** Created a worktree-dedicated `phaze_test_28_02` database (`CREATE DATABASE phaze_test_28_02` against the shared Postgres container). Added an autouse fixture in `tests/test_routers/test_agent_exec_batches.py` that monkeypatches `tests.conftest.TEST_DATABASE_URL` to the value of the `PHAZE_TEST_DATABASE_URL_28_02` env var (when set) BEFORE `async_engine` reads it. The pattern is non-invasive — the shared `tests/conftest.py` is untouched, and the override is no-op when the env var is unset (production / single-worktree CI runs).
- **Files modified:** `tests/test_routers/test_agent_exec_batches.py` (added module-level `_OVERRIDE_DB_URL` constant + `_override_test_database_url` autouse fixture).
- **Commit:** `3e012e0`.

**3. [Rule 3 - Blocker] Redis not running in worktree environment**

- **Found during:** First test run.
- **Issue:** No Redis container was running on `localhost:6379` (only Postgres was — owned by a sibling worktree). The integration tests need Redis for HINCRBY/HEXISTS/SET NX EX.
- **Fix:** Started `docker run -d --name phaze-redis-test-28-02 -p 6379:6379 redis:7-alpine`. The container is local to the test environment and is not part of the project's docker-compose surface (no commit needed).
- **Files modified:** None (infrastructure only).
- **Commit:** N/A (no source change).

No Rule 2 (missing critical functionality) or Rule 4 (architectural) deviations occurred. No deviations from the RESEARCH skeleton — the endpoint, schema, and agent-client method match the RESEARCH "Code Examples" §"New POST endpoint handler skeleton" and §"New PhazeAgentClient method" snippets verbatim with the two typing/infrastructure adaptations above.

## Auth Gates

None. Agent bearer authentication is handled by the existing Phase 25 `get_authenticated_agent` dependency — this plan adds no new credentials, no new external services, and no operator-action gates.

## Threat Surface Scan

No NEW threat surface introduced beyond what the plan's `<threat_model>` already enumerates. The mitigations declared in the threat register (T-28-02-S1 / T-28-02-S2 / T-28-02-T / T-28-02-I1 / T-28-02-I2 / T-28-02-V) are all implemented and tested:

- T-28-02-S1 (cross-tenant agent_id spoofing) → handled at handler stage 1, tested by `test_cross_tenant_agent_id_mismatch_403_before_state_read` + `test_cross_tenant_403_with_two_agents`.
- T-28-02-S2 (bearer missing/forged) → `Depends(get_authenticated_agent)` raises 401/403, tested by `test_unauthenticated_401` + `test_unknown_token_403`.
- T-28-02-T (progress POST replay) → SET NX EX 3600 idempotency, tested by `test_duplicate_request_id_does_not_re_increment`.
- T-28-02-I1 (timing side-channel via 200-vs-403) → 403-before-state-read placement, the deliberately-unseeded-hash variant of the cross-tenant test proves the ordering.
- T-28-02-I2 (cross-agent counter poking) → HEXISTS on `agent:<id>:total` rollup field, tested by `test_non_participating_agent_403`.
- T-28-02-V (ASVS V13 input validation) → `ConfigDict(extra="forbid")` + `model_validator(mode="after")` for `failed_at_step`/`terminal_step` coupling, tested by 16 schema tests.

No `## Threat Flags` section needed.

## Known Stubs

None. This plan implements the full D-05/D-06/D-07/D-15/D-17 contract — every code path described in the threat model and counter-math table is exercised by at least one test, the handler returns 200 only after all stages have been validated, and no UI/template surface is touched (Plan 28-04/28-06 own the template work).

## Plan Verification

Executed the plan's `<automated>` command verbatim:

```bash
uv run pytest tests/test_schemas/test_agent_exec_batches.py \
              tests/test_routers/test_agent_exec_batches.py \
              tests/test_services/test_agent_client_exec_batch_progress.py -x
```

Result: **41 passed, 0 failed, 0 skipped** in 8.87s.

`<done>` criteria:

- `grep -c "agent_exec_batches" src/phaze/main.py` → 2 (✓ ≥ 2: one import, one include_router).
- `grep -c "post_exec_batch_progress" src/phaze/services/agent_client.py` → 1 (✓ ≥ 1).
- `uv run pre-commit run --files <7 files>` → green (ruff / ruff-format / bandit / mypy / large-files / EOL / trailing-ws / mixed-line-ending all pass).
- Wider test surface (schemas/ + adjacent agent_client + fingerprint_locality + template_helpers): **145 passed, 2 skipped, 0 failed**.

Full-suite `uv run pytest -x` was **not** run with all integration tests — the worktree environment shares a Postgres container with the sibling Plan 28-03 worktree, and several pre-existing integration tests (e.g., `tests/test_routers/test_agent_files.py`, `tests/test_routers/test_pipeline_scans.py`) collide on schema/fixture setup against the default `phaze_test` DB. Per Plan 28-01's SUMMARY, these are pre-existing DB-infrastructure failures not introduced by this plan. The plan-relevant test surface (schemas, agent_client respx, the new router) is fully green via the worktree-dedicated `phaze_test_28_02` DB.

## TDD Gate Compliance

- **RED gate** (`test(28-02): ...` commit `ac0052b`): replaced 3 Wave 0 module-level `pytest.skip` stubs with the full failing test suite. Pre-implementation `pytest` failed with `ModuleNotFoundError: No module named 'phaze.schemas.agent_exec_batches'`. ✓
- **GREEN gate** (`feat(28-02): ...` commit `3e012e0`): created `phaze.schemas.agent_exec_batches` + `phaze.routers.agent_exec_batches`, modified `phaze.services.agent_client` (`post_exec_batch_progress` method) + `phaze.main` (router registration). All 41 tests in the targeted modules now pass. ✓
- **REFACTOR gate:** not required — the implementation is minimal-surface and the typing/test-infrastructure adaptations (cast + autouse DB override) were applied during GREEN, not as a separate refactor pass.

Gate sequence verified in `git log --oneline -3`:

```
3e012e0 feat(28-02): add exec-batch progress endpoint + schema + agent client method
ac0052b test(28-02): replace Wave 0 stubs with failing schema/router/client tests
6cffd5a docs(phase-28): update tracking after wave 0
```

## Self-Check: PASSED

Verified all 7 file paths and both commit hashes exist on this branch.

**File check** (all `git ls-files`-tracked):

- `src/phaze/schemas/agent_exec_batches.py` — NEW (88 lines).
- `src/phaze/routers/agent_exec_batches.py` — NEW (196 lines incl. typing-cast comments).
- `src/phaze/services/agent_client.py` — MODIFIED (added TYPE_CHECKING import + `post_exec_batch_progress` method).
- `src/phaze/main.py` — MODIFIED (added `agent_exec_batches` to import cluster + `include_router` call).
- `tests/test_schemas/test_agent_exec_batches.py` — Wave 0 stub REPLACED (16 unit tests).
- `tests/test_routers/test_agent_exec_batches.py` — Wave 0 stub REPLACED (18 contract tests + 1 wiring test + 1 pure-helper unit test).
- `tests/test_services/test_agent_client_exec_batch_progress.py` — Wave 0 stub REPLACED (7 respx tests).

**Commit check:**

- `ac0052b` (RED): present on `worktree-agent-a792158a502e8ae7b`.
- `3e012e0` (GREEN): present on `worktree-agent-a792158a502e8ae7b`.
