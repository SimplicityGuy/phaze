---
phase: 26-task-code-reorg-http-backed-agent-worker
plan: 08
subsystem: api
tags: [python, fastapi, state-machine, sqlalchemy, http-api, agent]

# Dependency graph
requires:
  - phase: 26
    provides: "ProposalStatus.EXECUTED/FAILED + FileState.MOVED/UNCHANGED enum values (Plan 01)"
  - phase: 26
    provides: "ProposalStatePatch + ProposalStateResponse Pydantic schemas with extra='forbid' and _require_path_when_moved validator (Plan 03)"
  - phase: 25
    provides: "agent_execution.py PATCH handler analog + Depends(get_authenticated_agent) auth dep + smoke-app test pattern"
provides:
  - "PATCH /api/internal/agent/proposals/{proposal_id}/state -- joint Proposal+FileRecord state transition (D-28)"
  - "_PROPOSAL_TRANSITIONS table -- single source of truth for allowed state transitions"
  - "Cross-tenant guard (W1 / T-26-08-S2): 403 when FileRecord.agent_id != authenticated_agent.id"
  - "Server-side state-machine validation with idempotent same-state no-op semantics"
affects: [phase-26-plan-11, phase-26-plan-12, phase-28]

# Tech tracking
tech-stack:
  added: []  # No new deps; all leverage existing stack
  patterns:
    - "Table-driven state transitions via dict[Enum, frozenset[Enum]]"
    - "Cross-tenant guard placed BEFORE state-machine logic (prevents timing-side-channel via 409 vs 403)"
    - "Joint multi-table mutation in single await session.commit() (Pitfall 6)"
    - "Idempotent same-state PATCH returns 200 with current state, no DB writes"

key-files:
  created:
    - "src/phaze/routers/agent_proposals.py"
    - "tests/test_routers/test_agent_proposals.py"
    - ".planning/phases/26-task-code-reorg-http-backed-agent-worker/deferred-items.md"
  modified:
    - "src/phaze/services/agent_client.py (Rule 3 fix: removed now-unused 'type: ignore[import-not-found]' tripwires)"

key-decisions:
  - "Mirror agent_execution.py PATCH structure byte-for-byte (table-driven transition + idempotent same-state retry pattern)"
  - "W1 cross-tenant guard placed BEFORE state-machine logic: returns 403 before 409 evaluation so a leaked proposal_id cannot be probed via timing"
  - "Same-state no-op makes ZERO DB writes: avoid the 'update with no change' pattern that would still bump updated_at on the underlying row"
  - "Used smoke-app pattern (mirrors test_agent_execution.py) to keep tests independent of Plan 12 wiring router into main.py"

patterns-established:
  - "Cross-tenant guard placement: 403 returns BEFORE state-machine evaluation to prevent timing side-channels"
  - "Joint Proposal+FileRecord mutation: single session.commit() at end; no intermediate commits between row updates"
  - "Idempotent state PATCH: cur==new short-circuit echoes current row state without mutation"

requirements-completed: [TASK-03]

# Metrics
duration: 14min
completed: 2026-05-12
---

# Phase 26 Plan 08: Task Code Reorg & HTTP-Backed Agent Worker -- agent_proposals Router Summary

**PATCH /api/internal/agent/proposals/{id}/state -- joint Proposal+FileRecord state-machine endpoint with table-driven transitions, idempotent same-state retry, and cross-tenant guard (W1 / T-26-08-S2)**

## Performance

- **Duration:** 14 min
- **Started:** 2026-05-12T21:37:02Z
- **Completed:** 2026-05-12T21:51:12Z
- **Tasks:** 2 (TDD: RED + GREEN)
- **Files modified:** 3 (1 created router, 1 created test, 1 fix to unrelated file as Rule 3 blocker)

## Accomplishments

- Shipped `src/phaze/routers/agent_proposals.py` with `_PROPOSAL_TRANSITIONS` table-driven state machine covering APPROVED -> {EXECUTED, FAILED}
- Implemented joint Proposal + FileRecord update in ONE transaction (single `await session.commit()`, RESEARCH Pitfall 6 verified)
- Implemented W1 / T-26-08-S2 cross-tenant guard: 403 when `file.agent_id != authenticated_agent.id`, placed BEFORE state-machine evaluation
- 11 contract tests covering every transition path, both legal and illegal, plus auth surface (401/403/404/422/409/200)
- Cleared a pre-existing mypy blocker (Plan 03 had landed but Plan 02's agent_client.py still carried `# type: ignore[import-not-found]` parallelization tripwires that mypy's `warn_unused_ignores` now flagged)

## Task Commits

1. **Task 1 (RED): contract tests** -- `e2e35e0` (test)
2. **Task 2 (GREEN): router implementation + test bug fix** -- `8c94069` (feat)

**Pre-task blocker fix:** `03b3d28` (fix) -- cleared 4 unused `# type: ignore` tripwires in `src/phaze/services/agent_client.py` that prevented commits until Plan 03 schemas were imported by real consumers.

## Files Created/Modified

- `src/phaze/routers/agent_proposals.py` (131 lines) -- PATCH handler with `_PROPOSAL_TRANSITIONS` + `_FILE_FOLLOW` tables, joint update logic, W1 cross-tenant guard, idempotent same-state no-op
- `tests/test_routers/test_agent_proposals.py` (247 lines) -- 11 contract tests using smoke-app pattern from `test_agent_execution.py`
- `src/phaze/services/agent_client.py` -- removed 4 unused `# type: ignore[import-not-found]` parallelization-debt directives (Rule 3 blocker fix)
- `.planning/phases/26-task-code-reorg-http-backed-agent-worker/deferred-items.md` (new) -- logged a pre-existing test-infrastructure flake (DEF-26-08-01) for future hardening

## Decisions Made

1. **Cross-tenant guard placement (W1 / T-26-08-S2):** the `file_record.agent_id != agent.id` check fires BEFORE the same-state and transition-machine evaluation. Rationale: an adversary who guesses a proposal_id should hit 403 regardless of whether the proposal's current state would make their target transition legal or illegal. Placing the check after the state-machine would leak a timing/oracle distinction (409 = transition forbidden but you own it; 403 = you don't own it). Cost: one extra `session.get(FileRecord, proposal.file_id)` per request, paid even on legitimate calls. Acceptable: file lookup is already required for the joint update.

2. **Same-state no-op echoes current row state without writes:** the canonical SAQ retry scenario is "agent commits, network hiccups, SAQ retries the PATCH". The retry sees `cur == new` and returns 200. We DO NOT bump `updated_at` (no `setattr`) and DO NOT issue a DB write. The response body echoes the current row state read during the file_record lookup.

3. **Mirrored `agent_execution.py:83-133` byte-for-byte where applicable:** the import order, the Annotated[AsyncSession, Depends(get_session)] pattern (NO `from __future__ import annotations`), the `session.get(Model, pk)` then `is None` then `raise 404` pattern, and the response body shape all match Phase 25's analog. Future maintainers can read the two files side-by-side and see structural parity.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Removed now-unused `# type: ignore[import-not-found]` tripwires in `src/phaze/services/agent_client.py`**
- **Found during:** Task 1 commit (pre-commit mypy hook failed before any Plan 08 code committed)
- **Issue:** `src/phaze/services/agent_client.py` (from Plan 26-02) annotated 4 Plan 03 schema imports with `# type: ignore[import-not-found]` as parallelization debt, with an explicit comment declaring them a self-deleting tripwire that would fire once Plan 03 landed. Plan 03 had landed (commit `6ae8a49`), so mypy's `warn_unused_ignores` flagged all 4 as `Unused "type: ignore" comment` -- a hard error that blocked all commits via the local mypy pre-commit hook.
- **Fix:** Removed the 4 `# type: ignore[import-not-found]` directives. No runtime behaviour change; the imports were always real, just temporarily suppressed.
- **Files modified:** `src/phaze/services/agent_client.py`
- **Verification:** `uv run mypy .` -> `Success: no issues found in 101 source files`
- **Committed in:** `03b3d28` (separate fix commit before Task 1)
- **Rationale for separate commit:** the fix is unrelated to Plan 08's domain (state-machine router); isolating it preserves git-bisect clarity and matches the author's original intent that this would be a tripwire-triggered cleanup.

**2. [Rule 1 - Bug] Fixed `await session.expire_all()` in test file (sync method)**
- **Found during:** Task 2 (first pytest run revealed `TypeError: object NoneType can't be used in 'await' expression`)
- **Issue:** The Plan 08 plan's verbatim test contents prescribed `await session.expire_all()`, but SQLAlchemy's `AsyncSession.expire_all()` is a sync method (it just clears the identity map; no I/O). The `await` raised TypeError.
- **Fix:** Stripped `await` from all 3 callsites in `tests/test_routers/test_agent_proposals.py`. The fix landed in the same commit as the GREEN router (Task 2) since the test was written from a buggy plan and the test+impl need to GREEN together.
- **Files modified:** `tests/test_routers/test_agent_proposals.py`
- **Verification:** `uv run pytest tests/test_routers/test_agent_proposals.py -q --no-cov` -> `11 passed`
- **Committed in:** `8c94069` (Task 2 GREEN commit, atomic with the router impl)

---

**Total deviations:** 2 auto-fixed (1 blocking, 1 bug)
**Impact on plan:** Both fixes were prerequisites for the plan to complete. The Rule 3 fix is independent housekeeping that would have to land in some plan eventually; landing it here unblocked the pre-commit hook. The Rule 1 fix was an artifact of the plan's verbatim test code carrying a sync/async mistake.

## Issues Encountered

- **Pre-existing test-fixture flakiness (DEF-26-08-01):** while validating the full test suite, observed that running `tests/test_routers/test_tags.py` and even the existing `tests/test_routers/test_agent_execution.py` against a polluted local Postgres `phaze_test` database produced `pg_type_typname_nsp_index` UniqueViolations -- the `Base.metadata.drop_all` teardown in `tests/conftest.py:async_engine` does not run when its setup raises, leaving schema cruft for subsequent tests. Logged to `.planning/phases/26-task-code-reorg-http-backed-agent-worker/deferred-items.md` as out-of-scope. The Plan 08 tests pass cleanly against a freshly-recreated `phaze_test` database. Verified by `docker exec phaze-pg-tests psql ... DROP DATABASE phaze_test; CREATE DATABASE ...; uv run pytest tests/test_routers/test_agent_proposals.py` -> `11 passed in 2.37s`.

## Threat Flags

(none -- threat model accurately covered the file changes)

## TDD Gate Compliance

- RED gate: commit `e2e35e0` (`test(26-08)`) -- 11 failing tests against missing router
- GREEN gate: commit `8c94069` (`feat(26-08)`) -- router implementation makes all 11 tests pass

## User Setup Required

None -- router is internal-agent only, no external service configuration required. The endpoint is NOT yet wired into `main.py` -- that lands in Plan 26-12.

## Next Phase Readiness

- **Plan 26-12 (router wiring):** `phaze.routers.agent_proposals.router` is exported and ready to be included in `phaze.main.create_app()` alongside the other agent-internal routers.
- **Plan 26-11 (execute_approved_batch HTTP rewrite):** can now call `PATCH /api/internal/agent/proposals/{id}/state` via `PhazeAgentClient.patch_proposal_state` to record execution results.
- **Plan 26-02 (PhazeAgentClient):** the `patch_proposal_state` method's contract matches this router's payload schema.

## Self-Check: PASSED

- File `src/phaze/routers/agent_proposals.py` exists (131 lines, >=80 required) -- FOUND
- File `tests/test_routers/test_agent_proposals.py` exists (247 lines, >=200 required) -- FOUND
- Commit `e2e35e0` (Task 1 RED) -- FOUND in git log
- Commit `8c94069` (Task 2 GREEN) -- FOUND in git log
- Commit `03b3d28` (Rule 3 fix) -- FOUND in git log
- `_PROPOSAL_TRANSITIONS` defined and used -- VERIFIED (2 occurrences)
- `if cur == new` same-state short-circuit -- VERIFIED (1 occurrence)
- Single `await session.commit()` (Pitfall 6) -- VERIFIED (1 occurrence)
- `HTTP_403_FORBIDDEN` for W1 cross-tenant guard -- VERIFIED (1 occurrence)
- `proposal does not belong` detail string -- VERIFIED (1 occurrence)
- 11 tests pass on fresh DB -- VERIFIED via `uv run pytest tests/test_routers/test_agent_proposals.py --no-cov` -> `11 passed`
- `uv run mypy src/phaze/routers/agent_proposals.py` exits 0 -- VERIFIED
- `uv run ruff check ... agent_proposals.py` exits 0 -- VERIFIED
- `pre-commit run --all-files` exits 0 -- VERIFIED

---
*Phase: 26-task-code-reorg-http-backed-agent-worker*
*Completed: 2026-05-12*
