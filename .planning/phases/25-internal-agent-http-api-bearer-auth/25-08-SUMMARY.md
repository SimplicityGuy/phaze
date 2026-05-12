---
phase: 25
plan: 08
subsystem: internal-agent-http-api / monotonic-status guard
tags: [gap-closure, CR-02, idempotency, D-15, http-api, bearer-auth]
gap_closure: true
closes_gaps:
  - "CR-02: PATCH against terminal ExecutionLog with same status returns 409, breaks idempotent retry contract"
requires: []
provides:
  - "Idempotent PATCH semantics for terminal ExecutionLog rows (same-status replay returns 200, not 409)"
  - "Three regression tests locking the carve-out so future refactors can't over-broaden or remove it"
affects:
  - src/phaze/routers/agent_execution.py
  - tests/test_routers/test_agent_execution.py
tech_stack:
  added: []
  patterns:
    - "Narrow conjunctive guard (`cur in _TERMINAL and new != cur`) instead of an unconditional terminal gate — preserves D-15 monotonic integrity while permitting the canonical idempotent-retry case"
key_files:
  created: []
  modified:
    - src/phaze/routers/agent_execution.py
    - tests/test_routers/test_agent_execution.py
  deleted: []
decisions:
  - "Carve-out is strictly same-status (`new != cur`), not all-terminal-to-any-terminal — keeps D-15 monotonic-ladder semantics intact for genuine transition attempts"
  - "Tests for both terminal states (COMPLETED and FAILED) AND the boundary case (COMPLETED→FAILED still rejected) — symmetric coverage so neither terminal state is implicitly privileged"
metrics:
  duration: "~5 min"
  completed: "2026-05-12T01:56:55Z"
  tasks_completed: 2
  files_modified: 2
  commits: 2
commits:
  - "7427836  fix(25-08): allow same-status PATCH against terminal ExecutionLog (CR-02)"
  - "640763e  test(25-08): lock CR-02 fix with three regression tests"
requirements:
  - DIST-05
  - DIST-04
  - AUTH-01
---

# Phase 25 Plan 08: CR-02 Idempotent-Replay Carve-Out Summary

One-liner: Narrowed the D-15 terminal-state guard in `patch_execution_log` from `if cur in _TERMINAL:` to `if cur in _TERMINAL and new != cur:` so same-status PATCH against a terminal row (the canonical SAQ-retry-after-network-glitch case) returns 200 instead of 409, and pinned the behavior with three regression tests.

## What Changed

### Router fix — `src/phaze/routers/agent_execution.py`

**One-line operator change** to the D-15 terminal-state guard (commit `7427836`):

Before:
```python
# D-15: terminal-state guard runs FIRST (early exit before regress check).
if cur in _TERMINAL:
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="execution-log status is terminal")
```

After:
```python
# D-15: terminal-state guard runs FIRST, but only when the new status would
# actually mutate the row. Same-status PATCH against a terminal row is the
# canonical idempotent retry case (agent writes COMPLETED -> network glitch
# swallows the 200 -> SAQ retries the job -> agent re-sends same PATCH) and
# MUST return 200. Gap closure CR-02 (25-VERIFICATION.md).
if cur in _TERMINAL and new != cur:
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="execution-log status is terminal")
```

The function docstring was rewritten in the same commit to match: the new 409-on-terminal bullet now reads "current status is terminal AND the proposed status differs from it", and the 200 bullet now explicitly says "same-status PATCH allowed for idempotent retry, including for terminal rows". The two error-message strings (`"execution-log status is terminal"` and `"execution-log status would regress"`) are unchanged — operators see the same triage signals in logs.

The constants `_STATUS_ORDER` and `_TERMINAL` were not touched. The regress guard (`if _STATUS_ORDER[new] < _STATUS_ORDER[cur]:`) was not touched. No imports were added.

### Regression tests — `tests/test_routers/test_agent_execution.py`

Three new tests appended after the existing `test_extra_body_field_422` (commit `640763e`). They reuse the existing `authed_app` / `seed_test_agent` / `session` fixtures and the `_authed_client` / `_seed_proposal_chain` / `_make_create_body` helpers — no new imports, no fixture changes, no helper changes.

| Test | What it locks | Pre-fix outcome |
|------|---------------|-----------------|
| `test_same_status_patch_terminal_allowed` | Canonical case: POST a row with `status="completed"`, PATCH same id with `{"status": "completed"}`, assert 200 AND DB row still COMPLETED. Named exactly as `25-VERIFICATION.md` line 17 specified. | Would have been `409 "execution-log status is terminal"` — verified via TDD red phase before the router fix landed (see commit log for `7427836`). |
| `test_same_status_patch_terminal_failed_allowed` | Symmetric case: same-status FAILED→FAILED retry must also return 200. Locks the property that both terminal states are treated equally by the carve-out. | Would have been `409 "execution-log status is terminal"` (symmetric). |
| `test_terminal_completed_to_failed_still_rejected` | Boundary test: COMPLETED→FAILED is STILL 409 with `"execution-log status is terminal"`. Prevents a future refactor from over-broadening the carve-out (e.g., accidentally allowing `cur in _TERMINAL and new in _TERMINAL`). | Already returned 409 (this is the contract we are preserving, not changing). |

## Verification

- `uv run pytest tests/test_routers/test_agent_execution.py -v` — **10 passed** (7 pre-existing + 3 new). All pre-existing behaviors preserved (`test_monotonic_regress_returns_409`, `test_terminal_state_rejects_patch` for COMPLETED→IN_PROGRESS, `test_same_status_patch_allowed` for IN_PROGRESS→IN_PROGRESS, etc.).
- `uv run pytest tests/test_routers/test_agent_*.py tests/test_services/test_agent_upsert.py -q` — **36 passed**, broader phase-25 cohort still green.
- `uv run ruff check src/phaze/routers/agent_execution.py tests/test_routers/test_agent_execution.py` — All checks passed.
- `uv run ruff format --check ...` — formatted (one auto-reformat applied to the test file during execution and re-staged before commit).
- `uv run mypy src/phaze/routers/agent_execution.py tests/test_routers/test_agent_execution.py` — Success, no issues found in 2 source files.
- TDD RED proof: the canonical test (`test_same_status_patch_terminal_allowed`) was first added against the unfixed router and confirmed to fail with `AssertionError: CR-02 regression: COMPLETED -> COMPLETED PATCH returned 409 '{"detail":"execution-log status is terminal"}'` — the expected pre-fix outcome. The test was then re-added (under Task 2's scope) after the router fix and now passes.
- All pre-commit hooks ran on both commits (ruff, ruff-format, bandit, mypy, end-of-file fixer, etc.) — no `--no-verify` was used.

## How the fix relates to D-15

D-15 is the application-level monotonic-status invariant: the ExecutionLog status ladder is `PENDING (0) < IN_PROGRESS (1) < COMPLETED (2) < FAILED (3)` and a status transition must be non-decreasing. The previous implementation enforced this with TWO guards run in order:

1. A terminal-state gate (`if cur in _TERMINAL:`) — early-exits with 409 the moment the row reaches COMPLETED or FAILED.
2. A regress comparator (`if _STATUS_ORDER[new] < _STATUS_ORDER[cur]:`) — strict `<` (not `<=`), so same-status PATCH against a NON-terminal row is allowed.

The bug was that the terminal gate was unconditional: it triggered even when the new status equaled the current status, blocking the canonical idempotent-retry case despite the docstring promising "same-status PATCH allowed for idempotent retry". The strict-`<` comparator was correct on its own but never got the chance to run for terminal rows.

The fix narrows guard #1 by adding `and new != cur`, so the terminal gate only fires when the proposed PATCH would actually mutate the row's terminal state. This:

- Restores idempotent-replay semantics for terminal rows (Behaviors 1 and 2 in the plan — COMPLETED→COMPLETED and FAILED→FAILED now 200).
- Preserves the D-15 monotonic ladder: terminal→any-other-state attempts still 409 with `"execution-log status is terminal"` (Behaviors 3 and 4 — COMPLETED→IN_PROGRESS, COMPLETED→FAILED, FAILED→COMPLETED all still rejected, locked by `test_terminal_state_rejects_patch` and the new `test_terminal_completed_to_failed_still_rejected`).
- Preserves the regress guard: IN_PROGRESS→PENDING is still 409 `"execution-log status would regress"` (Behavior 5 — locked by `test_monotonic_regress_returns_409`).
- Preserves same-status retry for non-terminal rows: IN_PROGRESS→IN_PROGRESS still 200 (Behavior 6 — locked by `test_same_status_patch_allowed`).

So the carve-out is one operator wide (`and new != cur`) and the rest of D-15 is intact. Phase 26 (agent-side HTTP client over this API) can now retry under flaky-network conditions without client-side classification of "is this 409 mine or theirs?" — the server now provides the canonical idempotent contract.

## Deviations from Plan

None of substance.

- **Auto-fix [Rule 3 - blocking]** `uv run ruff format --check` on the test file initially failed with "Would reformat" after appending the three new tests; ran `uv run ruff format tests/test_routers/test_agent_execution.py` to apply the canonical formatting (single line-spacing adjustments, no semantic changes), re-ran tests (10 still passed) and pre-commit hooks (all passed), then committed. Documented here for transparency; no plan steps were skipped or altered.
- **Operational note (not a deviation):** the test database (`phaze_test`) had stale schema state from a prior unrelated test session that caused `seed_test_agent` to violate `pk_agents` on the `legacy-application-server` row. Cleaned the schema with `docker exec phaze-pg-tests psql -U phaze -d phaze_test -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public; ..."` before each test run. This is environmental hygiene, not a code issue — the `conftest.py` async-engine fixture's `create_all` / `drop_all` pattern relies on a clean schema at session start, and a prior interrupted run had left tables behind. No source-code or plan-related fix was needed.
- **Plan acceptance criterion `grep -c "execution-log status is terminal" === 1` is satisfied in spirit, not literally:** the message text is preserved (one `raise` statement), but the plan's own docstring rewrite mentions the string by name, so a literal `grep -c` returns 2 (one docstring mention + one raise). Same for `"execution-log status would regress"` (count is 3: module docstring at line 12 — pre-existing — plus function docstring at line 100 plus the raise at line 122). The intent ("error message text is unchanged — operators still see the same string") is fully satisfied; this is a planning-spec self-conflict, not a code issue.

## Authentication Gates

None — all work was offline (router + tests + pre-commit hooks + local Postgres).

## Threat Flags

None. The fix narrows an existing guard; it does not introduce new endpoints, new authentication paths, new file-access patterns, or new schema. The threat register entries `T-25-08-D` (DoS via retry storms — mitigated by closing CR-02) and `T-25-08-T` (tampering via over-broadened carve-out — mitigated by `test_terminal_completed_to_failed_still_rejected`) from the plan's `<threat_model>` are both now backed by passing regression tests.

## Known Stubs

None.

## TDD Gate Compliance

This plan was structured as paired task-level TDD (router fix + regression tests in separate commits). Gate sequence in `git log`:

1. **TDD RED proof (in-conversation, not committed):** the canonical test `test_same_status_patch_terminal_allowed` was first added to the test file against the unfixed router and confirmed to fail with the expected `409 'execution-log status is terminal'` error message. This proves the test correctly captures the CR-02 bug. The test was then removed and re-added in Task 2's commit `640763e` after the router fix landed, so the committed test passes (it locks the fixed behavior, exactly as the plan intends — Task 1's `<verify>` block only checks "all pre-existing tests still pass", and Task 2 is the commit that introduces the new tests).
2. **GREEN gate** — commit `7427836` (`fix(25-08): ...`): router fix that makes the conceptual RED test pass. All 7 pre-existing tests still pass against the fixed router.
3. **Test-locking gate** — commit `640763e` (`test(25-08): ...`): the three regression tests are added (canonical + FAILED symmetry + boundary). All 10 tests pass.

There is no REFACTOR commit because no refactoring beyond docstring rewording was necessary; the docstring rewrite was bundled into the GREEN commit since it documents the new contract.

## Self-Check: PASSED

- File `src/phaze/routers/agent_execution.py` — FOUND (modified, contains `cur in _TERMINAL and new != cur`)
- File `tests/test_routers/test_agent_execution.py` — FOUND (modified, contains three new test function names)
- File `.planning/phases/25-internal-agent-http-api-bearer-auth/25-08-SUMMARY.md` — FOUND (this file)
- Commit `7427836` — FOUND on branch `worktree-agent-aaf4e346115c3876b`
- Commit `640763e` — FOUND on branch `worktree-agent-aaf4e346115c3876b`
- `uv run pytest tests/test_routers/test_agent_execution.py` — 10 passed
- `uv run pytest tests/test_routers/test_agent_*.py tests/test_services/test_agent_upsert.py` — 36 passed
- All pre-commit hooks ran clean on both commits (ruff, ruff-format, bandit, mypy, end-of-file, etc.)
