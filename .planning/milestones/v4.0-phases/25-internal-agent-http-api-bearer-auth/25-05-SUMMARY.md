---
phase: 25-internal-agent-http-api-bearer-auth
plan: 05
subsystem: api
tags: [fastapi, pydantic, sqlalchemy, postgresql, on-conflict, monotonic-state, audit-trail, bearer-auth]

# Dependency graph
requires:
  - phase: 25-internal-agent-http-api-bearer-auth
    plan: 01
    provides: seed_test_agent + authenticated_client fixtures, Agent model with last_status, migration 014
  - phase: 25-internal-agent-http-api-bearer-auth
    plan: 02
    provides: get_authenticated_agent FastAPI dependency in phaze.routers.agent_auth
provides:
  - phaze.routers.agent_execution module exporting `router` (prefix=/api/internal/agent/execution-log, tags=[agent-internal])
  - POST /api/internal/agent/execution-log -- agent-supplied UUID via D-13 INSERT ... ON CONFLICT (id) DO NOTHING, replay-safe
  - PATCH /api/internal/agent/execution-log/{id} -- D-15 monotonic status guard with two distinct 409 detail strings
  - phaze.schemas.agent_execution module with ExecutionLogCreate / ExecutionLogPatch (extra="forbid") plus loose response schemas
  - DIST-04 (4/5), DIST-05 (4/5), DIST-05 (5/5), D-13, D-15 (terminal + regress) covered by 7 green tests at 100% coverage
affects:
  - 25-06 (main.py wires this router via app.include_router(agent_execution.router))
  - 28-distributed-execution-dispatch (mirrors _STATUS_ORDER + _TERMINAL on the agent side; reads the two exact 409 detail strings for retry classification)
  - 26-* (future plans that PATCH ExecutionLog rows must respect the same monotonic ladder)

# Tech tracking
tech-stack:
  added: []  # no new dependencies -- uses existing FastAPI + SQLAlchemy + Pydantic
  patterns:
    - "Agent-supplied PK + INSERT ... ON CONFLICT (id) DO NOTHING for replay-safe writes (D-13)"
    - "Application-level monotonic state machine: dict[Enum, int] + terminal frozenset; strict `<` (not `<=`) comparator preserves idempotent-retry semantics"
    - "Two distinct 409 detail strings (`is terminal` vs `would regress`) so distributed retry clients can classify failure cause without re-hitting the server"
    - "Pydantic exclude_unset=True for partial-update PATCH bodies -- default-None fields do not clobber existing data"
    - "Smoke FastAPI app fixture pattern: an inline `_make_authed_app(session)` that mounts ONLY the target router so the test suite is parallel-safe and decoupled from main.py wiring"
    - "FK-chain seed helper: pre-seeds FileRecord + RenameProposal with the caller's agent_id so multi-row tests don't violate uq_files_agent_id_original_path"

key-files:
  created:
    - src/phaze/schemas/agent_execution.py
    - src/phaze/routers/agent_execution.py
    - tests/test_routers/test_agent_execution.py

key-decisions:
  - "DROPPED `from __future__ import annotations` in both src/phaze/routers/agent_execution.py and src/phaze/schemas/agent_execution.py -- matches Plan 25-02's deviation note (FastAPI dep-injection cannot resolve deferred forward-refs; Pydantic schema TypeAdapter cannot resolve string forward-refs). This is a DEVIATION from PATTERNS.md (Rule 1)."
  - "Smoke-app fixture strategy (over the plan's literal authenticated_client usage): the real create_app() does not include this router until Plan 25-06 wires it in; tests build their own inline FastAPI app per test via `_make_authed_app(session)` so Plan 25-05 is parallel-safe and does NOT need Plan 06 to land first. Mirrors Plan 25-02's smoke-app pattern."
  - "FileRecord in `_seed_proposal_chain` is seeded with `agent_id=agent.id` (the test agent), NOT the legacy default. This is necessary because multi-row tests would otherwise trip uq_files_agent_id_original_path on (legacy-application-server, original_path) collisions when the same path-template is reused across tests."
  - "Two 409 detail strings are intentionally distinct ('execution-log status is terminal' vs 'execution-log status would regress'): Phase 28's distributed worker uses these for retry classification ('retry past terminal' = client bug worth alerting; 'stale backward retry' = race condition worth swallowing)."

patterns-established:
  - "Monotonic state-machine guard pattern: _STATUS_ORDER dict + _TERMINAL frozenset + terminal-first guard (return 409 BEFORE the regress check). Order matters: terminal guard runs first so a COMPLETED row PATCHed with COMPLETED still returns 409 'is terminal' (not 200 same-status), making the audit trail truly immutable past terminal."
  - "Agent-supplied UUID write pattern: pg_insert(Model).values([payload]).on_conflict_do_nothing(index_elements=['id']) + return body.id (NOT a SELECT round-trip). Replay-safe at zero cost (single INSERT statement) and idempotent without changing client semantics."

requirements-completed:
  - DIST-04
  - DIST-05
  - AUTH-01

# Metrics
duration: 8min
completed: 2026-05-12
---

# Phase 25 Plan 05: Execution-Log Router with Monotonic Status Guard Summary

**ExecutionLog router shipping the multi-verb (POST + PATCH) audit endpoint with D-13's agent-supplied UUID replay-safety and D-15's four-state monotonic lifecycle enforced application-side; 7 green tests at 100% coverage on the new router + schemas; all 409 detail strings byte-exact for Phase 28's retry classifier.**

## Performance

- **Duration:** ~8 min
- **Started:** 2026-05-12T00:14:23Z
- **Completed:** 2026-05-12T00:22:46Z
- **Tasks:** 3 of 3 completed
- **Files created:** 3 (2 source, 1 test)

## Accomplishments

- **Strict Pydantic schemas** (`src/phaze/schemas/agent_execution.py`, 70 LOC):
  - `ExecutionLogCreate` and `ExecutionLogPatch` BOTH set `model_config = ConfigDict(extra="forbid")` (D-16) so any wire-level extra field returns 422 `extra_forbidden`
  - `status: ExecutionStatus` (typed enum) on both request schemas — Pydantic validates the four lifecycle values (`pending`, `in_progress`, `completed`, `failed`)
  - `id: uuid.UUID` on `ExecutionLogCreate` — agent supplies the PK (D-13)
  - `ExecutionLogCreateResponse` and `ExecutionLogPatchResponse` are loose (no `extra="forbid"`) so future extension is non-breaking
- **Execution-log router** (`src/phaze/routers/agent_execution.py`, 127 LOC):
  - `prefix="/api/internal/agent/execution-log"`, `tags=["agent-internal"]` (D-10)
  - `POST ""` uses `pg_insert(ExecutionLog).values([payload]).on_conflict_do_nothing(index_elements=["id"])` — D-13 first-create wins, replays are silent no-ops
  - `PATCH "/{execution_log_id}"` enforces D-15 monotonic invariant:
    - `session.get(ExecutionLog, id)` → 404 `"execution-log not found"` if missing
    - Terminal-state guard runs FIRST: `if cur in _TERMINAL: raise 409 "execution-log status is terminal"`
    - Regress guard second: `if _STATUS_ORDER[new] < _STATUS_ORDER[cur]: raise 409 "execution-log status would regress"` (strict `<`, NOT `<=` — same-status PATCH allowed for idempotent retry)
    - Partial-update via `body.model_dump(exclude_unset=True)` so default-None fields don't clobber existing data
- **Seven pytest-asyncio cases** (`tests/test_routers/test_agent_execution.py`, 274 LOC):
  - `test_execution_log_create_and_patch` — DIST-04 (4/5): POST + PATCH happy path; PENDING → IN_PROGRESS advance verified via SELECT
  - `test_create_replay_no_op` — DIST-05 (4/5) + D-13: same agent-supplied id POSTed twice = `COUNT(*) = 1`
  - `test_monotonic_regress_returns_409` — DIST-05 (5/5) + D-15: IN_PROGRESS → PENDING → 409 with EXACT detail `"execution-log status would regress"`
  - `test_terminal_state_rejects_patch` — D-15: COMPLETED row rejects further PATCH with EXACT detail `"execution-log status is terminal"`
  - `test_same_status_patch_allowed` — D-15 footnote: IN_PROGRESS → IN_PROGRESS = 200 (idempotent retry honored)
  - `test_patch_unknown_id_returns_404` — PATCH against fresh `uuid.uuid4()` = 404
  - `test_extra_body_field_422` — D-16: `{"agent_id": "evil", ...}` → 422 `extra_forbidden`, `loc == ["body", "agent_id"]`
- **100% line coverage** on both `agent_execution.py` (router, 35 stmts) and `agent_execution.py` (schemas, 25 stmts).
- **All gates pass:** `uv run mypy .` clean across 87 source files; `uv run ruff check` clean on all three files; `pre-commit run` clean on all three.

## Task Commits

Each task committed atomically:

1. **Task 1: Pydantic schemas (4 classes, extra="forbid" on both requests)** — `bec6493` (feat)
2. **Task 2: Seven failing tests in RED state (ModuleNotFoundError on phaze.routers.agent_execution)** — `b528a5b` (test)
3. **Task 3: Router with POST + PATCH + monotonic guard (GREEN, 7/7 pass on first run)** — `781c0ad` (feat)

## Files Created/Modified

- `src/phaze/schemas/agent_execution.py` (CREATED, 70 LOC) — Four Pydantic schemas. Two requests use `extra="forbid"`; two responses are loose. Status field typed as `ExecutionStatus` enum. No `agent_id` field on either request schema (AUTH-01 stamping).
- `src/phaze/routers/agent_execution.py` (CREATED, 127 LOC) — Two handlers with the monotonic check inline. `_STATUS_ORDER` dict + `_TERMINAL` frozenset are module-level constants Phase 28's worker can mirror byte-for-byte. Imports `AsyncSession` at runtime (NOT under `TYPE_CHECKING`) so FastAPI dep-injection resolves `Annotated[AsyncSession, Depends(get_session)]` at app-build time.
- `tests/test_routers/test_agent_execution.py` (CREATED, 274 LOC) — Seven pytest-asyncio cases. Uses a fixture-scoped `authed_app` (smoke-app pattern from Plan 02) so the test suite is parallel-safe regardless of Plan 06 landing order. `_seed_proposal_chain(session, agent_id)` helper seeds the FK chain FileRecord → RenameProposal so ExecutionLog FK constraints are satisfied.

## Decisions Made

- **Drop `from __future__ import annotations` in BOTH the router AND the schemas module:** with future-annotations active, FastAPI cannot resolve `Annotated[AsyncSession, Depends(get_session)]` (Plan 25-02 deviation), AND ruff TC001/TC003 incorrectly tries to push runtime-needed Pydantic types into `TYPE_CHECKING` blocks (which would break schema validation at runtime). Matches `src/phaze/schemas/scan.py`, `src/phaze/routers/duplicates.py`, `src/phaze/routers/tags.py`, and `src/phaze/routers/agent_auth.py` (all use runtime imports). The plan's code-block in the `<action>` section used `from __future__ import annotations` for both files — that was an unverified template carried forward from PATTERNS.md.
- **Smoke-app fixture over the plan's `authenticated_client`:** the plan's test code instructs the executor to use `authenticated_client` directly. But the real `create_app()` does NOT include this router until Plan 06 wires it in. Using `authenticated_client` would give a 404 on every test. Two options: (a) wait for Plan 06 to land first (defeats parallel waves), or (b) build an inline smoke app per test (Plan 02 pattern). Chose (b) for parallel-safety. The test file still uses `seed_test_agent` for the bearer + `session` for the session override, so the auth dep still executes the real database-backed lookup against the partial index — Plan 02's mitigations remain in force.
- **Seed FileRecord with the test agent's `agent_id`, NOT the legacy default:** `tests/test_routers/test_execution.py:35-46`'s `create_test_execution_log` lets `agent_id` default to `"legacy-application-server"`. That works for single-row UI tests, but for multi-row router tests reusing the same `original_path` template would trip `uq_files_agent_id_original_path (agent_id, original_path)`. Solution: parameterize `_seed_proposal_chain(session, agent_id)` and pass `agent.id` explicitly. Each test still gets a unique `original_path` via `uuid.uuid4()` interpolation, so the unique index is satisfied even if Postgres state leaks between parallel-agent runs.
- **Terminal-state guard runs FIRST (before regress check):** order matters because a COMPLETED row PATCHed with COMPLETED would otherwise look like an "allowed same-status retry" and return 200. Running the terminal guard first means a COMPLETED → COMPLETED PATCH returns 409 `"is terminal"`, preserving the audit-trail immutability invariant. The PATTERNS.md template explicitly calls this out; my implementation matches.
- **Same-status PATCH (IN_PROGRESS → IN_PROGRESS) returns 200:** the D-15 footnote requires comparator `<` (strict less-than), NOT `<=`. Implementing as `<=` would break idempotent agent retries (any SAQ retry of the IN_PROGRESS PATCH would 409). `test_same_status_patch_allowed` is the regression guard.

## Acceptance Criteria — All Met

### Task 1 — Schemas
- [x] `src/phaze/schemas/agent_execution.py` exists (70 LOC)
- [x] `grep -c 'model_config = ConfigDict(extra="forbid")'` returns 2 (Create + Patch)
- [x] `grep -F 'from phaze.models.execution import ExecutionStatus'` exits 0
- [x] All 4 class names present exactly once: `ExecutionLogCreate`, `ExecutionLogPatch`, `ExecutionLogCreateResponse`, `ExecutionLogPatchResponse`
- [x] `agent_id` only appears in response schemas (twice as fields; once in module docstring as explanatory text, never as a request-schema field)
- [x] `grep -F 'id: uuid.UUID'` exits 0 (D-13)
- [x] `grep -F 'status: ExecutionStatus'` exits 0 (typed enum)
- [x] `uv run mypy src/phaze/schemas/agent_execution.py` → `Success: no issues found`
- [x] `uv run ruff check src/phaze/schemas/agent_execution.py` exits 0
- [x] `pre-commit run --files src/phaze/schemas/agent_execution.py` all hooks Passed

### Task 2 — Tests (RED)
- [x] `tests/test_routers/test_agent_execution.py` exists with 7 `async def test_*` functions (≥6 required)
- [x] All six required test names present
- [x] `grep -F '"execution-log status would regress"'` exits 0 (D-15 exact string)
- [x] `grep -F '"execution-log status is terminal"'` exits 0 (D-15 exact string)
- [x] `grep -F '_seed_proposal_chain'` exits 0 (helper exists)
- [x] `grep -F "from phaze.models.proposal import RenameProposal"` exits 0 (singular module + correct class name)
- [x] `grep -F "RenameProposal("` exits 0 (canonical class)
- [x] `! grep -F "phaze.models.proposals"` (no plural module ref)
- [x] `! grep -F " Proposal("` (no bare Proposal)
- [x] `grep -c "from phaze.models.execution import"` returns 1
- [x] Tests fail in RED before Task 3 (ModuleNotFoundError on `phaze.routers.agent_execution`)
- [x] `uv run ruff check tests/test_routers/test_agent_execution.py` exits 0
- [x] `pre-commit run --files tests/test_routers/test_agent_execution.py` exits 0

### Task 3 — Router (GREEN)
- [x] `src/phaze/routers/agent_execution.py` exists (127 LOC)
- [x] `grep -F 'prefix="/api/internal/agent/execution-log"'` exits 0
- [x] `grep -F 'on_conflict_do_nothing(index_elements=["id"])'` exits 0 (D-13)
- [x] `grep -F '"execution-log status would regress"'` exits 0 (D-15 exact string)
- [x] `grep -F '"execution-log status is terminal"'` exits 0 (D-15 exact string)
- [x] `grep -F '"execution-log not found"'` exits 0 (404 detail)
- [x] `grep -F 'if _STATUS_ORDER[new] < _STATUS_ORDER[cur]:'` exits 0 (`<`, NOT `<=`)
- [x] `grep -F 'if cur in _TERMINAL:'` exits 0 (terminal check)
- [x] `grep -F 'body.model_dump(exclude_unset=True)'` exits 0
- [x] `grep -c "from phaze.routers.agent_auth import get_authenticated_agent"` returns 1
- [x] All 7 tests pass: `uv run pytest tests/test_routers/test_agent_execution.py -v` → `7 passed in 1.48s`
- [x] `uv run mypy src/phaze/routers/agent_execution.py` → `Success: no issues found`
- [x] `uv run ruff check src/phaze/routers/agent_execution.py` exits 0
- [x] Coverage on `agent_execution.py` is 100% (≥95% gate met)
- [x] `pre-commit run --files src/phaze/routers/agent_execution.py` exits 0

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Dropped `from __future__ import annotations` from BOTH the router AND the schemas module**

- **Found during:** Task 1 (ruff TC001 / TC003 errors on `import uuid` and `from phaze.models.execution import ExecutionStatus`)
- **Issue:** With `from __future__ import annotations` at the top of `agent_execution.py` (router), FastAPI's signature inspector cannot resolve `Annotated[AsyncSession, Depends(get_session)]` (Plan 25-02 deviation reproduced). At the top of `agent_execution.py` (schemas), it causes ruff to suggest pushing `uuid` and `ExecutionStatus` into `if TYPE_CHECKING:` — but those symbols are needed at runtime by Pydantic's field validators. Pushing them under TYPE_CHECKING would NameError at validation time.
- **Fix:** Removed `from __future__ import annotations` from both modules. Matches the convention established by `src/phaze/schemas/scan.py`, `src/phaze/routers/duplicates.py`, `src/phaze/routers/tags.py`, and Plan 02's `src/phaze/routers/agent_auth.py`.
- **Files modified:** `src/phaze/schemas/agent_execution.py`, `src/phaze/routers/agent_execution.py`
- **Verification:** All 7 tests pass; `uv run ruff check` clean (TC001/TC003 don't fire because annotations are evaluated eagerly and the symbols are genuinely runtime-needed); full project mypy clean across 87 files.
- **Committed in:** `bec6493` (Task 1, schemas) + `781c0ad` (Task 3, router)
- **Rationale:** The plan's code blocks in `<action>` sections used `from __future__ import annotations` carried forward from PATTERNS.md, but the parallel-execution prompt explicitly cited Plan 25-02's discovery: "do NOT use `from __future__ import annotations` in router files because FastAPI dep-injection cannot resolve deferred forward-refs." Rule 1 (auto-fix bugs) applies — the future-annotations import directly blocks Task 3's done criterion ("all 7 tests pass").

**2. [Rule 3 - Blocking] Used smoke-app fixture instead of `authenticated_client` for tests**

- **Found during:** Task 2 (planning the test architecture)
- **Issue:** The plan's test code calls `authenticated_client.post(...)`. `authenticated_client` builds `create_app()`, but `create_app()` does NOT include the agent_execution router until Plan 25-06 wires it in (Wave 4). Tests using `authenticated_client` would 404 on every call, blocking the GREEN state for Task 3 without Plan 06 landing first.
- **Fix:** Built an inline `_make_authed_app(session)` smoke-app fixture that mounts ONLY this router and overrides `get_session`. Per-test bearer headers come from `seed_test_agent`. Mirrors Plan 02's `_make_smoke_app(session)` strategy verbatim. This means Plan 25-05 is parallel-safe and decoupled from Plan 06 landing order.
- **Files modified:** `tests/test_routers/test_agent_execution.py` (added `authed_app` fixture + `_authed_client` async generator)
- **Verification:** All 7 tests pass; the auth dep still executes the real database-backed lookup against the partial index (Plan 02's mitigations still in force).
- **Committed in:** `b528a5b` (Task 2)
- **Rationale:** Rule 3 (auto-fix blocking issues) applies — without this change, Task 3's done criterion ("all 7 tests pass") could not be reached until Plan 06 lands.

---

**Total deviations:** 2 auto-fixed (1 Rule 1 bug + 1 Rule 3 blocking fix)
**Impact on plan:** No scope creep; both deviations are mechanical adjustments to make Plans 02-05's verified conventions compose. Downstream Plan 06 reads this summary for the router's exact prefix and tags.

## Issues Encountered

- **Pre-existing test isolation issue in `test_tracklists.py`:** running `uv run pytest tests/test_routers/` reveals 19 errors in `test_tracklists.py` with `UniqueViolationError: duplicate key value violates unique constraint "pk_agents"` (DETAIL: `Key (id)=(legacy-application-server) already exists`). This is a parallel-agent test-DB-state leak (multiple worktree agents share the same `phaze_test` database; one agent's failed teardown leaves the legacy agent row in the DB, which the next `async_engine` fixture's seeding step can't re-insert). My 7 tests pass cleanly in isolation; the failures are unrelated to this plan's code and are out of scope. Logged here for downstream tracking — a future plan should consider database-per-agent isolation or a teardown safety net.

## Threat Mitigations Verified

- **T-25-05-T (Tampering — retry replay):** Mitigated. `test_create_replay_no_op` POSTs the same agent-supplied id twice → `COUNT(*) = 1`. `pg_insert(...).on_conflict_do_nothing(index_elements=["id"])` makes replays silent no-ops.
- **T-25-05-T (Tampering — backward status walk):** Mitigated. `test_monotonic_regress_returns_409` POSTs at IN_PROGRESS then PATCHes to PENDING → 409 with EXACT detail `"execution-log status would regress"`. The `_STATUS_ORDER[new] < _STATUS_ORDER[cur]` check is byte-exact.
- **T-25-05-R (Repudiation — rewrite terminal):** Mitigated. `test_terminal_state_rejects_patch` POSTs at COMPLETED then PATCHes to IN_PROGRESS → 409 with EXACT detail `"execution-log status is terminal"`. The terminal guard runs BEFORE the regress check so error-message triage can distinguish "agent has a bug" from "stale retry."
- **T-25-05-S (Spoofing — body agent_id forge):** Mitigated. `test_extra_body_field_422` POSTs with `{"agent_id": "evil", ...}` → 422 `extra_forbidden`. Handler stamps `agent.id` from the auth dep into the response only; the ExecutionLog table has no agent_id column.
- **T-25-05-I (InfoDisclosure — stack trace leak):** Mitigated. Default FastAPI `{"detail": "..."}` envelope per CONTEXT.md; no custom envelope; no leaked exception detail in production (`debug=False` is the project default).
- **T-25-05-T (Tampering — same-status PATCH blocked):** Mitigated. `test_same_status_patch_allowed` POSTs at IN_PROGRESS then PATCHes to IN_PROGRESS → 200. Comparator is `<` (strict), NOT `<=`. Idempotent retries are first-class behavior.
- **T-25-05-T (Tampering — mass-assignment via unknown id):** Mitigated. `test_patch_unknown_id_returns_404` PATCHes `uuid.uuid4()` → 404. `session.get(ExecutionLog, id)` returns None → `HTTPException(404, "execution-log not found")` before any setattr.

## Notes for Downstream Plans

### For Plan 25-06 (main.py wiring)

**Router import + include (byte-for-byte):**
```python
from phaze.routers import agent_execution

app.include_router(agent_execution.router)
```

The router already declares its own `prefix="/api/internal/agent/execution-log"` and `tags=["agent-internal"]` — do NOT add a second `prefix=` or `tags=` argument to `include_router(...)`.

Once Plan 06 wires this router via `create_app()`, the test suite's smoke-app fixture remains valid (it tests the router in isolation against the same auth dep). A complementary integration test in Plan 06's test file (which exercises the REAL `create_app()` instance) is fine but is NOT required to retire the smoke-app fixture.

### For Phase 28 (distributed-execution-dispatch — agent-side retry classifier)

**The two 409 detail strings are PUBLIC API for retry classification.** Mirror them on the agent side:

```python
# In the agent worker's retry classifier:
TERMINAL_DETAIL = "execution-log status is terminal"
REGRESS_DETAIL = "execution-log status would regress"

# 409 with TERMINAL_DETAIL  -> bug worth alerting (agent re-tried past its
#                               own terminal write; halt the worker, page operator)
# 409 with REGRESS_DETAIL   -> race condition worth swallowing (a stale
#                               retry walked status backward after another
#                               retry already advanced; treat as success)
```

**Mirror `_STATUS_ORDER` + `_TERMINAL` on the agent side:**

```python
_STATUS_ORDER = {
    ExecutionStatus.PENDING: 0,
    ExecutionStatus.IN_PROGRESS: 1,
    ExecutionStatus.COMPLETED: 2,
    ExecutionStatus.FAILED: 3,
}
_TERMINAL = frozenset({ExecutionStatus.COMPLETED, ExecutionStatus.FAILED})
```

So the agent can short-circuit retries that would trip the regress guard rather than burning a network round trip to discover it.

### For Plans 26+ (future ExecutionLog mutators)

- Use the same monotonic ladder; never insert a backward PATCH.
- If you need a new lifecycle state, insert it in the right ordinal slot of `_STATUS_ORDER` AND update Phase 28's agent mirror. Do NOT renumber existing slots — `_STATUS_ORDER` values are an internal sort key, not exposed on the wire, but renumbering would break the cross-version migration story.
- Use `body.model_dump(exclude_unset=True)` in any PATCH handler so default-None fields don't clobber existing data — this is now the project convention for partial-update routes.

### For test authors (Phases 26, 28)

- The `_seed_proposal_chain(session, agent_id)` helper in `tests/test_routers/test_agent_execution.py` is the canonical FK-chain seed pattern for ExecutionLog tests. Reuse it (and pass `agent.id` explicitly, NOT the legacy default).
- The `_make_authed_app(session)` smoke-app pattern is the canonical strategy for testing a router in isolation while Plan 06 has not yet wired it. Mirror it for Plans 03, 04 as well if their suite needs the same parallel-safety.

## Self-Check: PASSED

**Files verified to exist:**
- `src/phaze/schemas/agent_execution.py` (CREATED)
- `src/phaze/routers/agent_execution.py` (CREATED)
- `tests/test_routers/test_agent_execution.py` (CREATED)

**Commits verified in git log:**
- `bec6493` — feat(25-05): add Pydantic schemas for /agent/execution-log endpoints
- `b528a5b` — test(25-05): add failing tests for /agent/execution-log monotonic invariant
- `781c0ad` — feat(25-05): implement /agent/execution-log POST + PATCH with monotonic check

**Coverage verified:**
- `src/phaze/routers/agent_execution.py`: 35 stmts, 0 missing, 100.00% coverage
- `src/phaze/schemas/agent_execution.py`: 25 stmts, 0 missing, 100.00% coverage
- Combined: 60 stmts, 0 missing, 100.00% (≥95% gate met)

**Final gates verified:**
- `uv run pytest tests/test_routers/test_agent_execution.py -v` → `7 passed in 1.48s`
- `uv run mypy src/phaze/routers/agent_execution.py src/phaze/schemas/agent_execution.py` → `Success: no issues found`
- `uv run mypy .` → `Success: no issues found in 87 source files` (full project clean)
- `uv run ruff check src/phaze/schemas/agent_execution.py src/phaze/routers/agent_execution.py tests/test_routers/test_agent_execution.py` → `All checks passed!`
- `pre-commit run --files src/phaze/schemas/agent_execution.py src/phaze/routers/agent_execution.py tests/test_routers/test_agent_execution.py` → all hooks Passed

## TDD Gate Compliance

All three tasks are tagged `tdd="true"`. Gate sequence in git log:
- RED gate: `b528a5b` — test commit (Task 2, RED state — collection error on missing module)
- GREEN gate: `781c0ad` — feat commit (Task 3, GREEN state — 7/7 pass)
- Task 1 (schemas) commit (`bec6493`) was non-test infrastructure committed before RED; this is permitted because Task 1's `<verify>` block is grep + mypy + ruff (no pytest), so no RED-before-GREEN gate applies to it.

---
*Phase: 25-internal-agent-http-api-bearer-auth*
*Plan: 05*
*Completed: 2026-05-12*
