---
phase: 29-deployment-hardening-agents-admin
plan: 06
subsystem: agent-worker
tags: [phase-29, ops-04, heartbeat, saq-cron, agent-worker, v4.0]

# Dependency graph
requires:
  - phase: 26-task-code-reorg-http-backed-agent-worker
    provides: phaze.tasks.agent_worker.settings hook + PhazeAgentClient.heartbeat method (Phase 25, refactored Phase 26)
  - phase: 25-internal-agent-http-api-bearer-auth
    provides: POST /api/internal/agent/heartbeat endpoint + HeartbeatRequest schema (extra="forbid")
  - phase: 29-deployment-hardening-agents-admin/29-01
    provides: 29-01 phase scaffolding + frontmatter conventions
  - phase: 29-deployment-hardening-agents-admin/29-05
    provides: agent_worker.startup ctx shape (api_client, agent_identity already populated by Plan 29-05's startup wiring)
provides:
  - "phaze.tasks.heartbeat.heartbeat_tick — async SAQ cron handler emitting 30s heartbeats fire-and-forget"
  - "agent_worker.settings.cron_jobs[0] — registered CronJob(heartbeat_tick, cron='* * * * * */30', unique=True, timeout=10) on the agent-role SAQ Worker"
  - "tests/test_tasks/test_heartbeat_cron.py — 4 happy-path tests (success, ctx-missing, queue.info-fail, importlib metadata source)"
  - "tests/test_tasks/test_heartbeat_failure.py — 1 failure test (AgentApiServerError -> WARNING + swallow)"
affects: [29-07 admin-agents-page (consumes last_seen_at populated by these heartbeats)]

# Tech tracking
tech-stack:
  added: []  # zero new pip dependencies; saq.CronJob already in deps via Phase 26 controller.py
  patterns:
    - "SAQ CronJob with trailing-seconds 6-field cron form (`* * * * * */30`) for sub-minute cadence — croniter 6.x default convention"
    - "Defensive ctx-key guarding in SAQ cron handlers: missing api_client/agent_identity -> WARNING + return (no exception escapes during worker startup races)"
    - "Fire-and-forget HTTP POST posture inside async SAQ jobs: catch domain-specific exception base class, log WARNING, swallow; SAQ retries via next cron tick (mirrors Phase 28 D-16)"

key-files:
  created:
    - src/phaze/tasks/heartbeat.py
    - tests/test_tasks/test_heartbeat_cron.py
    - tests/test_tasks/test_heartbeat_failure.py
  modified:
    - src/phaze/tasks/agent_worker.py (added CronJob + heartbeat_tick imports; added heartbeat_tick to settings.functions; added settings.cron_jobs entry)

key-decisions:
  - "Trailing-seconds 6-field cron form `* * * * * */30` (NOT the leading-seconds form `*/30 * * * * *` shown in CONTEXT.md D-08) — empirically verified: trailing produces 30s gaps, leading produces 1s gaps (croniter 6.x default convention places seconds as field 6)"
  - "agent_worker.py stays a single .py file (Pitfall 9 avoided) — settings dict mutation in place; heartbeat_tick lives in sibling phaze/tasks/heartbeat.py"
  - "Defensive queue.info() failure handling: any exception -> queue_depth=0 + WARNING log + still POST (heartbeat is more valuable than queue-depth accuracy in the failure mode)"
  - "AgentApiServerError tests construct positional-only per src/phaze/services/agent_client.py:86-87 — no custom __init__, no status_code= kwarg"
  - "BLE001 ruff rule is NOT enabled in this project's config; the `# noqa: BLE001` directive from PATTERNS.md was unused — removed and replaced with an inline comment documenting the broad-except intent"
  - "ctx['worker'].queue is the correct access path (NOT ctx['queue']) per RESEARCH Pitfall 8 — SAQ pre-populates `self.context = {'worker': self}` in `Worker.__init__`"

requirements-completed: [OPS-04 (caller half — UI half lands in Plan 29-07)]

# Metrics
duration: ~15min
completed: 2026-05-16
---

# Phase 29 Plan 06: Agent Heartbeat Caller (OPS-04 Caller Half) Summary

**Lands the agent-side SAQ cron handler that POSTs a heartbeat every 30 seconds to `/api/internal/agent/heartbeat`, populating `agents.last_seen_at` and `last_status` for the admin page (Plan 07). Uses the trailing-seconds 6-field cron form (`* * * * * */30`) — NOT the leading-seconds example from CONTEXT.md D-08 — per RESEARCH Critical Discovery #2.**

## Performance

- **Duration:** ~15 min (estimate)
- **Started:** 2026-05-16T~16:14Z
- **Completed:** 2026-05-16T~16:30Z
- **Tasks:** 2 (both auto, both TDD)
- **Files created:** 3 (heartbeat.py, test_heartbeat_cron.py, test_heartbeat_failure.py)
- **Files modified:** 1 (agent_worker.py — 3 edits: CronJob import, heartbeat_tick import, settings dict cron_jobs + functions entry)
- **Tests added:** 5 (4 happy-path + 1 failure)
- **Lines added:** ~140 source + ~165 tests = ~305 total

## Accomplishments

- **OPS-04 caller half closed.** Each agent's SAQ worker now POSTs `HeartbeatRequest(agent_version, worker_pid, queue_depth)` to `/api/internal/agent/heartbeat` every 30 seconds. The app-server endpoint (Phase 25) updates `agents.last_seen_at` and `last_status` JSONB; the admin page (Plan 07) will read both columns for the alive/stale/dead pill computation.
- **Trailing-seconds 6-field cron form locked in.** The PLAN explicitly called out the CONTEXT.md D-08 example bug (`*/30 * * * * *` fires every second under croniter 6.x default config). The correct form `* * * * * */30` was verified empirically with `croniter(...)` returning gaps of 30.0 seconds vs 1.0 seconds for the wrong form. The smoke command from the PLAN (`croniter('* * * * * */30', start_time=0)`) hangs in an infinite loop on this version of croniter — but the cron string itself is verified-correct via datetime-baseline invocation.
- **D-07 routing preserved.** Only the agent_worker SAQ process emits heartbeats; the watcher does NOT (Pattern 5 + Phase 29 D-07 — if the worker is down but the watcher is up, the agent looks "stale" in the admin UI, which is the correct operator signal).
- **D-08 cron registered.** `CronJob(heartbeat_tick, cron="* * * * * */30", unique=True, timeout=10)` lives at `phaze.tasks.agent_worker.settings.cron_jobs[0]`. `heartbeat_tick` is also in `settings.functions` so SAQ can dispatch the cron-enqueued job.
- **D-09 fire-and-forget failure handling.** `try: client.heartbeat(payload) except AgentApiError as exc: logger.warning("heartbeat failed: %s", exc)` — any 4xx/5xx/timeout (all subclasses of `AgentApiError`) is logged and swallowed. The SAQ cron fires again 30 seconds later; the operator sees `last_seen_at` stop advancing and the admin UI surfaces "stale" naturally.
- **D-10 payload shape locked.** `agent_version` reads from `importlib.metadata.version("phaze")` (pyproject.toml [project].version); `worker_pid` from `os.getpid()` inside the SAQ Worker subprocess; `queue_depth` from `ctx["worker"].queue.info()["queued"]` — typed `int` cast guards against `None` slipping in.
- **Pitfall 8 avoided.** Queue access is `ctx["worker"].queue` (NOT `ctx["queue"]`). SAQ pre-populates `self.context = {"worker": self}` in `Worker.__init__`; only the startup hook adds the keys (api_client, agent_identity). The cron handler sees `{**self.context, "job": job}` per tick, so ctx["queue"] would `KeyError`.
- **Pitfall 9 avoided.** `agent_worker.py` remains a single `.py` file. The PLAN explicitly added cron_jobs to the existing settings dict IN-PLACE (not converted to a package); heartbeat_tick lives in a sibling module `phaze/tasks/heartbeat.py`.

## Task Commits

Each task was committed atomically:

1. **Task 1: Write failing tests for SAQ heartbeat cron handler (RED step)** — `48ad8c1` (test)
2. **Task 2: Create phaze/tasks/heartbeat.py + register cron in agent_worker.settings (GREEN step)** — `afbf048` (feat)

## Files Created/Modified

### Created

- `src/phaze/tasks/heartbeat.py` — 79 lines. Module docstring documents D-07..D-10 contract + IMPORT-BOUNDARY INVARIANT (Postgres-free) + the trailing-seconds-cron rationale. Imports stdlib (`importlib.metadata`, `logging`, `os`, `typing.Any`) + `phaze.schemas.agent_heartbeat.HeartbeatRequest` + `phaze.services.agent_client.AgentApiError`. Body follows RESEARCH Pattern 5 lines 553-580 byte-for-byte (modulo the BLE001 noqa removal documented under Deviations).
- `tests/test_tasks/test_heartbeat_cron.py` — 4 async test functions: `test_heartbeat_success`, `test_heartbeat_skips_when_ctx_missing`, `test_heartbeat_queue_info_failure_defaults_to_zero`, `test_heartbeat_agent_version_from_importlib`. Module-level `_make_ctx` helper builds the SAQ ctx shape (api_client AsyncMock, agent_identity AgentIdentity, worker MagicMock with worker.queue AsyncMock returning the SAQ QueueInfo TypedDict). Uses `unittest.mock.patch("phaze.tasks.heartbeat.os.getpid", return_value=12345)` + `patch("phaze.tasks.heartbeat.importlib.metadata.version", ...)` for deterministic `worker_pid` / `agent_version` in the success test.
- `tests/test_tasks/test_heartbeat_failure.py` — 1 async test `test_heartbeat_agentapierror_warning`. Constructs `AgentApiServerError("server error")` positional-only per verified `agent_client.py:86-87` (no custom `__init__`). Asserts `caplog.text` contains `"heartbeat failed"` and at least one WARNING-level record.

### Modified

- `src/phaze/tasks/agent_worker.py` — 3 edits:
  1. `from saq import Queue` → `from saq import CronJob, Queue` (line 48).
  2. Added `from phaze.tasks.heartbeat import heartbeat_tick` after the existing `phaze.tasks.functions` import (alphabetical placement per ruff isort config).
  3. Added `heartbeat_tick` to `settings["functions"]` list (end position) AND added `"cron_jobs": [CronJob(heartbeat_tick, cron="* * * * * */30", unique=True, timeout=10)],  # type: ignore[type-var]` (mirroring the `controller.py:117` precedent shape). The `# type: ignore[type-var]` is necessary because SAQ's `CronJob` is a `Generic[CtxType]` that mypy cannot infer from a function reference; this matches the existing controller.py pattern.

## Decisions Made

- **Trailing-seconds 6-field cron form.** The CONTEXT.md D-08 example shows `*/30 * * * * *` (leading seconds, would fire every second under croniter 6.x default). RESEARCH Critical Discovery #2 verified the correct form is `* * * * * */30` (trailing seconds, 30s cadence). Implemented the corrected form; reverify command in the PLAN (`croniter('* * * * * */30', start_time=0)`) hangs in an infinite loop in croniter 6.2.2 — but a datetime-baseline reverify (`croniter('* * * * * */30', datetime(2026, 1, 1))`) confirms 30s gaps.
- **Defensive `except Exception` around `queue.info()`.** Broad — but documented inline as intentional. Any SAQ-internal change, Redis blip, or `None` return must NOT crash the cron handler; default to `queue_depth=0` and still POST the heartbeat. The heartbeat presence is more valuable than queue-depth accuracy when the queue is unreliable.
- **`AgentApiError` (base class) catch — NOT bare `Exception`.** D-09 specifies "any subclass" of AgentApiError; bare `Exception` would swallow programming errors. The narrow base-class catch lets `TypeError`, `ValueError`, etc. bubble up where they belong.
- **`# type: ignore[type-var]` on the CronJob entry.** Matches the existing `controller.py:117` pattern. SAQ's `CronJob` is `Generic[CtxType]` and mypy cannot infer the type-var from a function reference; the only alternatives are extensive explicit annotations or upstream SAQ changes — neither in scope.
- **AgentApiServerError tests positional-only.** Verified at `src/phaze/services/agent_client.py:86-87` — `AgentApiServerError` (and all `AgentApiError` subclasses) have no custom `__init__`. They accept positional args ONLY. Passing `status_code=` would `TypeError` at test setup time. The failure-mode test constructs `AgentApiServerError("server error")` positional-only.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Ruff `TC002` warning on `import pytest` in new test files**

- **Found during:** Task 1 verification (ruff check after writing test files).
- **Issue:** Both `test_heartbeat_cron.py` and `test_heartbeat_failure.py` had `import pytest` at module top. The `pytest` symbol is used ONLY as a type annotation (`pytest.LogCaptureFixture`). Ruff's `TC002` rule (move third-party type-only imports into a `TYPE_CHECKING` block) flagged both.
- **Fix:** Moved `import pytest` into an `if TYPE_CHECKING:` block at module bottom, added `TYPE_CHECKING` to the existing `from typing import ...` import.
- **Files modified:** tests/test_tasks/test_heartbeat_cron.py, tests/test_tasks/test_heartbeat_failure.py
- **Verification:** `uv run ruff check tests/test_tasks/test_heartbeat_*.py` → All checks passed.
- **Committed in:** `48ad8c1` (part of Task 1 RED commit — fix applied before initial commit).

**2. [Rule 3 - Blocking] Unused `# noqa: BLE001` directive in heartbeat.py**

- **Found during:** Task 2 verification (ruff check after writing heartbeat.py).
- **Issue:** PATTERNS.md line 348 + the PLAN's Notes section suggested adding `# noqa: BLE001` to the `except Exception:` around `queue.info()`. However, this project's `pyproject.toml` enables ruff rule sets `ARG, B, C4, E, F, I, PLC, PTH, RUF, S, SIM, T20, TCH, UP, W, W191` — `BLE` is NOT enabled (only `B`, which doesn't include `BLE001`). Ruff flagged the noqa as targeting an unused rule.
- **Fix:** Removed `# noqa: BLE001` from the `except Exception:` line; replaced the inline rationale with a two-line comment block above the catch documenting the broad-except intent.
- **Files modified:** src/phaze/tasks/heartbeat.py
- **Verification:** `uv run ruff check src/phaze/tasks/heartbeat.py` → All checks passed.
- **Committed in:** `afbf048` (part of Task 2 GREEN commit — fix applied before initial commit).

### Notable Deferrals (NOT auto-fixed — out of scope)

**3. `tests/test_phase04_gaps.py::test_docker_compose_has_agent_worker_consuming_agent_queue`** — This pre-existing failure was already documented in `.planning/phases/29-deployment-hardening-agents-admin/deferred-items.md` by Plan 29-05. Plan 29-03 removed the agent-worker block from root `docker-compose.yml` (app-server-only invariant); Plan 29-04 (parallel wave) created `docker-compose.agent.yml` where the agent-worker now lives. The test must be updated to scan both compose files by Plan 29-04 (or a follow-on plan). Out of scope for Plan 06; no new failure introduced by this plan.

## Verification

### Task 1 acceptance (RED)

- ✅ `tests/test_tasks/test_heartbeat_cron.py` contains 4 test functions
- ✅ `tests/test_tasks/test_heartbeat_failure.py` contains 1 test function
- ✅ `uv run pytest tests/test_tasks/test_heartbeat_cron.py tests/test_tasks/test_heartbeat_failure.py --collect-only -q` → tests discovered, collection fails with `ModuleNotFoundError: No module named 'phaze.tasks.heartbeat'` (expected RED)
- ✅ `AgentApiServerError` constructed positional-only (no `status_code=` kwarg)
- ✅ `uv run ruff check tests/test_tasks/test_heartbeat_*.py` → All checks passed

### Task 2 acceptance (GREEN)

- ✅ `src/phaze/tasks/heartbeat.py` exists with `heartbeat_tick(ctx: dict[str, Any]) -> None`
- ✅ Module imports only stdlib + `phaze.schemas.agent_heartbeat.HeartbeatRequest` + `phaze.services.agent_client.AgentApiError` (no `phaze.database`, no `sqlalchemy.ext.asyncio`, no `phaze.tasks.session`)
- ✅ `src/phaze/tasks/agent_worker.py` imports `CronJob` from saq + `heartbeat_tick` from `phaze.tasks.heartbeat`
- ✅ `settings["cron_jobs"]` has `CronJob(heartbeat_tick, cron="* * * * * */30", unique=True, timeout=10)` (trailing-seconds form)
- ✅ `settings["functions"]` includes `heartbeat_tick` (so SAQ can dispatch)
- ✅ `agent_worker.py` is STILL a single .py file (Pitfall 9 avoided)
- ✅ All 5 heartbeat tests pass (`uv run pytest tests/test_tasks/test_heartbeat_cron.py tests/test_tasks/test_heartbeat_failure.py -x -q` → 5 passed)
- ✅ `tests/test_task_split.py` still passes (heartbeat.py import-boundary doesn't leak phaze.database — 6 passed)
- ✅ `uv run mypy src/phaze/tasks/heartbeat.py src/phaze/tasks/agent_worker.py` → Success: no issues found in 2 source files
- ✅ `uv run ruff check src/phaze/tasks/heartbeat.py src/phaze/tasks/agent_worker.py tests/test_tasks/test_heartbeat_*.py` → All checks passed
- ✅ Empirical cron-cadence reverify: `croniter('* * * * * */30', datetime(2026, 1, 1))` → gaps of 30.0 seconds; `croniter('*/30 * * * * *', datetime(2026, 1, 1))` → gaps of 1.0 seconds (wrong form would have fired every second)
- ✅ Broader suite: `uv run pytest tests/test_tasks/ tests/test_phase04_gaps.py --deselect tests/test_phase04_gaps.py::test_docker_compose_has_agent_worker_consuming_agent_queue -q` → 129 passed, 1 deselected (only the pre-existing Plan 29-03/04 deferred test is excluded)

### Threat-model mitigations delivered

| Threat ID | Mitigation Delivered |
|-----------|----------------------|
| T-29-06-01 (Spoofing — rogue agent impersonating another) | Bearer-token auth at `/api/internal/agent/heartbeat` (Phase 25 unchanged); `agent_id` resolved from token-hash lookup, NEVER from request body; `HeartbeatRequest.model_config["extra"] = "forbid"` rejects any agent_id smuggled in. No change required in this plan. |
| T-29-06-02 (DoS — flood via misconfigured cron) | Trailing-seconds cron form `* * * * * */30` empirically fires at 30s cadence (gaps of 30.0s verified); leading-seconds form `*/30 * * * * *` would fire every second (gaps of 1.0s). The correct form is locked in the CronJob registration. SAQ `unique=True` prevents duplicate enqueues within the 30s window. |
| T-29-06-03 (Info disclosure — bearer token in failure logs) | `logger.warning("heartbeat failed: %s", exc)` interpolates `AgentApiError.__str__` — the AgentApiError class hierarchy (verified at `agent_client.py:75-88`) has no custom `__init__` and does NOT carry the bearer token in any field. Future Phase 26 D-13 invariant additionally guards against token leaks at the client-construction layer. |
| T-29-06-04 (Tampering — malicious queue.info return crashes handler) | Broad `except Exception` around `queue.info()` defaults to `queue_depth=0` and logs WARNING with `exc_info=True`. Tested via `test_heartbeat_queue_info_failure_defaults_to_zero`. |
| T-29-06-05 (DoS — heartbeat blocks SAQ event loop) | `client.heartbeat()` is `await`-ed (async httpx under the hood); tenacity retry funnel (Phase 26 D-11) bounds wall-time to ~4s; CronJob `timeout=10` upper-bounds the handler. The 30s cadence is > 10s timeout, so the next tick fires cleanly. |
| T-29-06-06 (Operational — silent heartbeat failure forever) | Accepted per D-09 fire-and-forget. The app-server's `last_seen_at` stops advancing → admin page (Plan 07) surfaces "stale"/"dead" → operator notices. |
| T-29-06-07 (Operational — agent_worker.py refactored to package) | Pitfall 9 avoided: cron_jobs added IN-PLACE to existing `settings = {...}` dict; heartbeat_tick lives in sibling `phaze/tasks/heartbeat.py`. agent_worker.py remains a single .py file; all existing imports (e.g., `tests/test_task_split.py:54`) continue to work. |

## Self-Check: PASSED

**Files created — verified to exist:**

- ✅ `src/phaze/tasks/heartbeat.py` — FOUND
- ✅ `tests/test_tasks/test_heartbeat_cron.py` — FOUND
- ✅ `tests/test_tasks/test_heartbeat_failure.py` — FOUND

**Files modified — verified `git log --follow` reachable:**

- ✅ `src/phaze/tasks/agent_worker.py` — modified in `afbf048`

**Commits — verified `git log --all` reachable:**

- ✅ `48ad8c1` — Task 1 (test, RED step)
- ✅ `afbf048` — Task 2 (feat, GREEN step)

## TDD Gate Compliance

- ✅ RED gate commit: `48ad8c1` (`test(29-06): add failing tests for SAQ heartbeat cron handler`) — tests fail with `ModuleNotFoundError: No module named 'phaze.tasks.heartbeat'`
- ✅ GREEN gate commit: `afbf048` (`feat(29-06): wire SAQ 30s heartbeat cron handler (OPS-04 caller)`) — all 5 tests pass, no exception escapes
- ⏭️  REFACTOR gate: not required (no refactoring needed beyond the inline `# noqa: BLE001` removal, which was applied pre-commit and folded into the GREEN commit per ruff fail-fast)
