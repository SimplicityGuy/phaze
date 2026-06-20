---
phase: quick-260620-jvu
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/phaze/tasks/scan.py
  - src/phaze/tasks/metadata_extraction.py
  - src/phaze/tasks/fingerprint.py
  - src/phaze/schemas/agent_metadata.py
  - src/phaze/schemas/agent_fingerprint.py
  - tests/test_tasks/test_scan.py
  - tests/test_tasks/test_metadata_extraction.py
  - tests/test_tasks/test_fingerprint.py
  - tests/test_routers/test_agent_metadata.py
  - tests/test_routers/test_agent_fingerprint.py
  - .planning/phases/45-scheduling-ledger-for-orphan-recovery/45-REVIEW.md
autonomous: true
requirements: [WR-01, WR-02]

must_haves:
  truths:
    - "A double-failure on a terminal SAQ attempt (task fails AND the terminal ack also raises) re-raises the ORIGINAL task error, not the ack error, in scan match-failure / metadata / fingerprint handlers"
    - "Constructing MetadataFailureResponse or FingerprintFailureResponse with cleared=False raises pydantic ValidationError"
    - "The agent worker task modules stay Postgres-free (no phaze.database / phaze.models / sqlalchemy imports)"
    - "WR-01 and WR-02 are marked resolved in 45-REVIEW.md with frontmatter warning count updated"
  artifacts:
    - path: src/phaze/tasks/scan.py
      provides: "Nested try/except around report_scan_terminal on the match-failure path"
      contains: "logger.warning"
    - path: src/phaze/schemas/agent_metadata.py
      provides: "cleared: Literal[True] invariant"
      contains: "Literal[True]"
    - path: src/phaze/schemas/agent_fingerprint.py
      provides: "cleared: Literal[True] invariant"
      contains: "Literal[True]"
  key_links:
    - from: "src/phaze/tasks/scan.py except handler"
      to: "report_scan_terminal"
      via: "nested try/except that swallows on terminal attempt, raise after re-raises E1"
      pattern: "try:.*report_scan_terminal.*except Exception:.*logger.warning"
---

<objective>
Harden the two advisory warnings from Phase 45's 45-REVIEW.md. No phase reopening — this is a quick code-quality pass.

- WR-01: Three terminal-ack `except` handlers (scan match-failure, metadata, fingerprint) call the
  terminal ack BARE. If the ack raises (E2) while handling the original failure (E1), E2 masks E1
  and the trailing `raise` never re-raises E1 — SAQ records the wrong error and the ledger row can
  leak. Fix: wrap the ack in a nested `try/except` that swallows + logs on the terminal attempt,
  then always re-raise the original E1 (mirrors the already-shipped no-match path at scan.py:106-113).
- WR-02: Change the failure-response `cleared: bool` field to `cleared: Literal[True]` in both
  failure schemas so Pydantic machine-enforces the always-True invariant.

Purpose: close the L-02 invariant gap ("cleared on completion AND terminal failure") at the type
level and eliminate exception-masking in the recovery-critical handlers.
Output: 3 task modules + 2 schemas hardened, new tests proving both fixes, 45-REVIEW.md updated.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/phases/45-scheduling-ledger-for-orphan-recovery/45-REVIEW.md

<constraints>
- Python 3.14, `uv` only — every command prefixed with `uv run`. Never bare pytest/mypy/ruff.
- Agent worker boundary: tasks/*.py MUST NOT import phaze.database, phaze.models, or sqlalchemy.
  Enforced by tests/test_task_split.py — do not add such imports.
- 150-char lines, double quotes, type hints. Ruff + mypy strict must pass.
- Match the EXACT fix structure from 45-REVIEW.md WR-01 (lines 145-159) and WR-02 (lines 186-211).
</constraints>

<interfaces>
The fix structure is fully specified in 45-REVIEW.md. The authoritative target for each handler:

```python
# match-failure / metadata / fingerprint except block (the WR-01 target shape)
except Exception:
    job = ctx.get("job")
    if job is not None and not job.retryable:
        try:
            await api.report_<X>_failed(payload.file_id)   # report_scan_terminal for scan
        except Exception:
            logger.warning("<task> terminal-ack failed", file_id=str(payload.file_id), exc_info=True)
    raise  # ALWAYS re-raises the original E1
```

The already-shipped no-match path at scan.py:106-113 is the proven precedent for this nesting.
Note the behavioral difference between the two scan paths, both already correct after the fix:
- no-match (clean COMPLETE): swallow on terminal → RETURN no_matches (does not re-raise).
- match-failure (a real failure): swallow on terminal → still `raise` the original E1.

WR-02 target: add `from typing import Literal` to each schema (stdlib import; ruff will order it),
change `cleared: bool` → `cleared: Literal[True]` in MetadataFailureResponse (agent_metadata.py:48)
and FingerprintFailureResponse (agent_fingerprint.py:38).
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: WR-01 — guard the three terminal-ack except blocks + prove no exception masking</name>
  <read_first>
    - src/phaze/tasks/scan.py (no-match guard at 106-113 = the pattern to mirror; match-failure handler at 143-152 = the fix target)
    - src/phaze/tasks/metadata_extraction.py (except block at 65-75)
    - src/phaze/tasks/fingerprint.py (except block at 55-66)
    - tests/test_tasks/test_scan.py (no-match terminal-ack-raise tests at 202-271 = template; existing match terminal/retryable/absent tests at 141-194)
    - tests/test_tasks/test_metadata_extraction.py (terminal/retryable/absent tests at 141-201 = template)
    - tests/test_tasks/test_fingerprint.py (terminal/retryable/absent tests at 148-221 = template)
  </read_first>
  <files>src/phaze/tasks/scan.py, src/phaze/tasks/metadata_extraction.py, src/phaze/tasks/fingerprint.py, tests/test_tasks/test_scan.py, tests/test_tasks/test_metadata_extraction.py, tests/test_tasks/test_fingerprint.py</files>
  <behavior>
    For each of the three failure handlers (scan_live_set match-failure path, extract_file_metadata,
    fingerprint_file), with `ctx["job"]` set to a non-retryable stub:
    - Test A (terminal-attempt ack failure): primary call raises E1 (e.g. RuntimeError "controller 5xx");
      the terminal ack (report_scan_terminal / report_metadata_failed / report_fingerprint_failed)
      raises E2 (e.g. AgentApiServerError "ack boom"). The task MUST raise E1 (assert via
      `pytest.raises(RuntimeError, match="controller 5xx")`), NOT E2; the ack is awaited once.
    - Existing retryable test (job.retryable=True): ack NOT awaited, E1 re-raises. Already present —
      keep green; the ack is gated by `not job.retryable` so it is never called on retryable attempts.
    - Existing job-absent test: ack NOT awaited, E1 re-raises. Already present — keep green.
  </behavior>
  <action>
    Apply the WR-01 fix to all three handlers, mirroring scan.py's no-match guard exactly:
    1. scan.py match-failure handler (lines 149-152): wrap `await api.report_scan_terminal(payload.file_id)`
       in a nested `try/except Exception` that logs `logger.warning("scan_live_set match-failure terminal-ack failed", file_id=str(payload.file_id), exc_info=True)`; keep the trailing `raise` so E1 always re-raises.
    2. metadata_extraction.py (line 74): same nesting around `await api.report_metadata_failed(payload.file_id)`
       with `logger.warning("extract_file_metadata terminal-ack failed", file_id=str(payload.file_id), exc_info=True)`.
    3. fingerprint.py (line 65): same nesting around `await api.report_fingerprint_failed(payload.file_id)`
       with `logger.warning("fingerprint_file terminal-ack failed", file_id=str(payload.file_id), exc_info=True)`.
    Do NOT change the `if job is not None and not job.retryable:` gate — the ack stays terminal-only;
    only the ack CALL gets the nested try/except. Do NOT add any phaze.database/phaze.models/sqlalchemy import.
    Then add Test A (terminal-attempt ack-failure → original error re-raised) to each of the three test
    files, mirroring tests/test_tasks/test_scan.py::test_scan_no_match_terminal_ack_raise_on_terminal_attempt_swallows_and_returns
    but on the FAILURE path (assert the ORIGINAL error type/message is raised, not the ack error).
  </action>
  <verify>
    <automated>uv run pytest tests/test_tasks/test_scan.py tests/test_tasks/test_metadata_extraction.py tests/test_tasks/test_fingerprint.py tests/test_task_split.py -q</automated>
  </verify>
  <acceptance_criteria>
    - `uv run pytest tests/test_tasks/test_scan.py tests/test_tasks/test_metadata_extraction.py tests/test_tasks/test_fingerprint.py tests/test_task_split.py -q` passes.
    - One new terminal-ack-failure test in each of the three test files asserts the ORIGINAL error
      (not the ack error) propagates via `pytest.raises(<E1 type>, match=<E1 msg>)`.
    - `grep -n "try:" src/phaze/tasks/metadata_extraction.py src/phaze/tasks/fingerprint.py` shows a
      nested try inside the except block of each; `grep -c "terminal-ack failed" src/phaze/tasks/scan.py src/phaze/tasks/metadata_extraction.py src/phaze/tasks/fingerprint.py` returns 1 per file (scan has 2: no-match + match).
    - `uv run ruff check src/phaze/tasks/ tests/test_tasks/` and `uv run mypy src/phaze/tasks/` pass.
    - No new `phaze.database`/`phaze.models`/`sqlalchemy` import in tasks/*.py (test_task_split passes).
  </acceptance_criteria>
  <done>All three handlers wrap the terminal ack in a swallow-and-log nested try/except; the original failure always re-raises; new tests prove no masking; agent-worker Postgres-free boundary intact.</done>
</task>

<task type="auto" tdd="true">
  <name>Task 2: WR-02 — make cleared a Literal[True] invariant + prove cleared=False is rejected</name>
  <read_first>
    - src/phaze/schemas/agent_metadata.py (MetadataFailureResponse at 35-48; imports at 1-5)
    - src/phaze/schemas/agent_fingerprint.py (FingerprintFailureResponse at 25-38; imports at 1-5)
    - tests/test_routers/test_agent_metadata.py (existing `body["cleared"] is True` asserts at 322, 340 — must stay green)
    - tests/test_routers/test_agent_fingerprint.py (existing `cleared is True` asserts at 243, 261 — must stay green)
  </read_first>
  <files>src/phaze/schemas/agent_metadata.py, src/phaze/schemas/agent_fingerprint.py, tests/test_routers/test_agent_metadata.py, tests/test_routers/test_agent_fingerprint.py</files>
  <behavior>
    - MetadataFailureResponse(agent_id="a", file_id=<uuid>, cleared=True) constructs successfully.
    - MetadataFailureResponse(agent_id="a", file_id=<uuid>, cleared=False) raises pydantic.ValidationError.
    - Same two cases for FingerprintFailureResponse.
    These are pure schema-construction tests — they do NOT require the test DB.
  </behavior>
  <action>
    In agent_metadata.py and agent_fingerprint.py: add `from typing import Literal` (stdlib section;
    ruff format will order it relative to `import uuid`) and change the failure-response field from
    `cleared: bool` to `cleared: Literal[True]`. Do not touch the success-response schemas or any
    router code (the router already passes `cleared=True`, which remains valid).
    Add the four pure-construction tests from <behavior> — import the schema directly and assert
    `pytest.raises(ValidationError)` on cleared=False. Place them in the existing router test files
    (importing the schema at module top, no DB fixture needed) or a dedicated schema test module;
    keep them runnable WITHOUT the test DB.
  </action>
  <verify>
    <automated>uv run pytest tests/test_routers/test_agent_metadata.py tests/test_routers/test_agent_fingerprint.py -q -k "cleared or Literal or failure" && uv run mypy src/phaze/schemas/agent_metadata.py src/phaze/schemas/agent_fingerprint.py</automated>
  </verify>
  <acceptance_criteria>
    - `grep -c "cleared: Literal\[True\]" src/phaze/schemas/agent_metadata.py src/phaze/schemas/agent_fingerprint.py` returns 1 per file.
    - `grep -c "from typing import Literal" src/phaze/schemas/agent_metadata.py src/phaze/schemas/agent_fingerprint.py` returns 1 per file.
    - `grep -c "cleared: bool" src/phaze/schemas/agent_metadata.py src/phaze/schemas/agent_fingerprint.py` returns 0 per file.
    - New tests assert `pytest.raises(ValidationError)` for cleared=False on BOTH failure schemas and pass.
    - `uv run ruff check src/phaze/schemas/` and `uv run mypy src/phaze/schemas/agent_metadata.py src/phaze/schemas/agent_fingerprint.py` pass.
    - DB-backed router tests asserting cleared is True still pass when run with the ephemeral test DB
      (see <db_setup>); the new schema-rejection tests pass WITHOUT the DB.
  </acceptance_criteria>
  <done>Both failure schemas enforce cleared=Literal[True] at the type level; cleared=False is rejected by Pydantic; existing cleared=True paths unaffected.</done>
</task>

<task type="auto">
  <name>Task 3: Mark WR-01 and WR-02 resolved in 45-REVIEW.md</name>
  <read_first>
    - .planning/phases/45-scheduling-ledger-for-orphan-recovery/45-REVIEW.md (frontmatter findings counts at 15-20; WR-01 at 80-162; WR-02 at 166-212)
  </read_first>
  <files>.planning/phases/45-scheduling-ledger-for-orphan-recovery/45-REVIEW.md</files>
  <action>
    Mark both warnings resolved without deleting their content (preserve the audit trail):
    1. Frontmatter: set `findings.warning: 0`, keep `total` accurate (info: 1 remains, so total: 1),
       and update `status:` from `issues_found` to `resolved` (or `issues_resolved` if that is the
       repo's convention — check sibling review docs; default to `resolved`).
    2. Prepend a `**Resolved:** 2026-06-20 — fixed in quick task 260620-jvu (...)` line to the WR-01
       and WR-02 headings (or a `> RESOLVED` blockquote under each), noting the nested try/except fix
       and the Literal[True] change respectively.
    3. Add a one-line note to the Summary that the two warnings were closed by the 260620-jvu hardening pass.
    Leave IN-01 (info) untouched — it is out of scope.
  </action>
  <verify>
    <automated>grep -c "warning: 0" .planning/phases/45-scheduling-ledger-for-orphan-recovery/45-REVIEW.md && grep -ci "resolved" .planning/phases/45-scheduling-ledger-for-orphan-recovery/45-REVIEW.md</automated>
  </verify>
  <acceptance_criteria>
    - Frontmatter `findings.warning` is 0 and `total` is updated to reflect only the remaining info finding.
    - Both WR-01 and WR-02 carry an explicit resolved marker referencing this quick pass.
    - WR-01/WR-02 issue bodies are preserved (not deleted); IN-01 unchanged.
  </acceptance_criteria>
  <done>45-REVIEW.md frontmatter and both warning sections reflect the closed state; audit trail preserved.</done>
</task>

</tasks>

<db_setup>
The DB-backed router tests in tests/test_routers/test_agent_metadata.py and test_agent_fingerprint.py
need an ephemeral Postgres + Redis. Bring it up before running them, tear down after:

```bash
just test-db
export TEST_DATABASE_URL=postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test
export MIGRATIONS_TEST_DATABASE_URL=postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_migrations_test
export PHAZE_REDIS_URL=redis://localhost:6380/0
# ... run the router test suites ...
just test-db-down
```

The Task 1 pure-task tests and the Task 2 schema-rejection tests do NOT need the DB.
</db_setup>

<verification>
Full local gate before considering the plan done:

```bash
# Pure-task + schema tests (no DB)
uv run pytest tests/test_tasks/test_scan.py tests/test_tasks/test_metadata_extraction.py \
  tests/test_tasks/test_fingerprint.py tests/test_task_split.py -q

# DB-backed router tests (bring up the ephemeral DB per <db_setup> first)
uv run pytest tests/test_routers/test_agent_metadata.py tests/test_routers/test_agent_fingerprint.py -q

# Lint + types
uv run ruff check src/phaze/tasks/ src/phaze/schemas/ tests/
uv run ruff format --check src/phaze/tasks/ src/phaze/schemas/
uv run mypy src/phaze/tasks/ src/phaze/schemas/agent_metadata.py src/phaze/schemas/agent_fingerprint.py
```
</verification>

<success_criteria>
- WR-01: all three terminal-ack except blocks wrap the ack in a swallow-and-log nested try/except;
  a terminal-attempt ack failure re-raises the ORIGINAL task error (proven by new tests).
- WR-02: both failure schemas use `cleared: Literal[True]`; cleared=False raises ValidationError (proven by new tests).
- Agent-worker Postgres-free boundary intact (test_task_split passes).
- 45-REVIEW.md frontmatter warning count is 0 and both warnings are marked resolved.
- ruff + mypy clean; all touched test suites green; coverage stays >=85%.
</success_criteria>

<output>
Create `.planning/quick/260620-jvu-harden-ledger-ack-warnings/260620-jvu-SUMMARY.md` when done.
</output>
