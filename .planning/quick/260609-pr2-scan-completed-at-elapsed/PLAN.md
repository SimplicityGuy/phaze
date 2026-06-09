---
phase: quick-260609-pr2-scan-completed-at-elapsed
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - alembic/versions/016_backfill_scan_batches_completed_at.py
  - tests/test_migrations/test_016_upgrade.py
  - src/phaze/services/ingestion.py
  - tests/test_services/test_ingestion.py
  - src/phaze/routers/pipeline_scans.py
  - tests/test_routers/test_pipeline_scans.py
autonomous: true
requirements: [PR2-SCAN-ELAPSED]

must_haves:
  truths:
    - "A COMPLETED/FAILED ScanBatch never shows an elapsed timer that climbs against the wall clock."
    - "New terminal rows written by the legacy run_scan path always have completed_at set."
    - "Pre-existing terminal rows with completed_at IS NULL are backfilled to updated_at."
    - "A RUNNING batch's elapsed timer still ticks against now (unchanged)."
  artifacts:
    - path: "alembic/versions/016_backfill_scan_batches_completed_at.py"
      provides: "Data backfill of completed_at = updated_at for terminal NULL rows"
      contains: "down_revision"
    - path: "src/phaze/services/ingestion.py"
      provides: "completed_at stamped on COMPLETED and FAILED transitions in run_scan"
      contains: "completed_at="
    - path: "src/phaze/routers/pipeline_scans.py"
      provides: "Defensive elapsed_seconds that freezes terminal+NULL rows at updated_at"
      contains: "elapsed_seconds"
  key_links:
    - from: "src/phaze/routers/pipeline_scans.py::elapsed_seconds"
      to: "ScanBatch.updated_at"
      via: "freeze point for terminal status with NULL completed_at"
      pattern: "updated_at"
---

<objective>
Fix the "elapsed time keeps climbing even on COMPLETED scans" defect. A terminal
(`completed`/`failed`) `ScanBatch` row whose `completed_at IS NULL` causes
`elapsed_seconds()` to compute `now - created_at` forever. Three NULL-source paths
are closed:

1. **Data backfill** (migration 016) — fixes pre-existing terminal rows.
2. **Legacy writer fix** (`services/ingestion.py::run_scan`) — stops new NULL rows.
3. **Defensive read** (`routers/pipeline_scans.py::elapsed_seconds`) — freezes any
   remaining terminal+NULL row at `updated_at` instead of `now`.

Purpose: The admin pipeline dashboard's elapsed timer must stop when a scan is done.
Output: One Alembic migration + two source fixes + tests (3 atomic commits).

Scope guard: `completed_at`/elapsed correctness ONLY. Do NOT add a stall-heartbeat
column, reaper, or UI activity indicator (PR4). Do NOT introduce structlog (PR3).
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
</execution_context>

<context>
@CLAUDE.md

<interfaces>
<!-- Extracted from the codebase. Executor should use these directly. -->

ScanBatch — src/phaze/models/scan_batch.py
- TimestampMixin (src/phaze/models/base.py) provides:
    created_at: Mapped[datetime]  (server_default=func.now())
    updated_at: Mapped[datetime]  (server_default=func.now(), onupdate=func.now())
- completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
- status column is Mapped[str]; ScanStatus is a StrEnum:
    RUNNING="running", COMPLETED="completed", FAILED="failed", LIVE="live"
- Terminal states: COMPLETED, FAILED. Non-terminal: RUNNING, LIVE.

elapsed_seconds — src/phaze/routers/pipeline_scans.py:53-87 (current behavior)
    created_at = batch.created_at; if naive -> assume UTC
    end = batch.completed_at if batch.completed_at is not None else datetime.now(UTC)
    if end naive -> assume UTC
    return int((end - created_at).total_seconds())
  Imports already present at top: `from datetime import UTC, datetime`.

Reference (already correct, do NOT change) — routers/agent_scan_batches.py:118-124
  stamps `batch.completed_at = datetime.now(UTC)` on first terminal transition.

run_scan — src/phaze/services/ingestion.py:122-189
  COMPLETED branch (~167-176): update(ScanBatch).where(...).values(status=COMPLETED, processed_files=upserted)
  FAILED branch (~178-189): update(ScanBatch).where(...).values(status=FAILED, error_message=str(exc)); raise
  NOTE: module has `from __future__ import annotations` and does NOT yet import datetime/UTC.

Latest Alembic revision = "015" (alembic/versions/015_add_completed_at_to_scan_batches.py,
revision="015", down_revision="014"). Async template; models imported in env.py.

Migration test harness — tests/test_migrations/conftest.py exposes:
  migrated_engine (upgrades to head), upgrade_to(cfg, rev), downgrade_to(cfg, rev),
  _build_alembic_config(url), MIGRATIONS_TEST_DATABASE_URL.
  Pattern for testing a sequence (see tests/test_migrations/test_downgrade.py):
  downgrade to a base rev, mutate data, upgrade one step, assert.
  Operator pre-condition: DB `phaze_migrations_test` must exist on localhost:5432.

Session fixtures — tests/conftest.py: `async_engine` fixture; build a factory with
  async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False).
</interfaces>
</context>

<tasks>

<task type="auto">
  <name>Task 1: Migration 016 — backfill completed_at on terminal NULL rows</name>
  <files>alembic/versions/016_backfill_scan_batches_completed_at.py, tests/test_migrations/test_016_upgrade.py</files>
  <action>
    Create migration `016_backfill_scan_batches_completed_at.py`. Mirror the header/identifier
    style of `015_add_completed_at_to_scan_batches.py`: `revision = "016"`, `down_revision = "015"`,
    `branch_labels = None`, `depends_on = None`, typed as `str | Sequence[str] | None`.
    Module docstring: explain this is a data backfill closing the NULL-completed_at gap from
    incident 260608/260609 (rows that reached terminal state before completed_at existed, plus
    rows written by the legacy run_scan path that never stamped it).

    upgrade(): a single `op.execute(...)` running:
      UPDATE scan_batches SET completed_at = updated_at
      WHERE status IN ('completed', 'failed') AND completed_at IS NULL
    No model imports — raw SQL only. `updated_at` is the natural freeze point for a terminal
    row whose true completion time was never recorded.

    downgrade(): no-op with a docstring explaining why — a data backfill is not reversibly
    undoable because the migration cannot know which rows were originally NULL versus
    legitimately stamped; restoring NULLs would corrupt correctly-completed rows. Use
    `pass` (and keep an explanatory comment/docstring so it reads as intentional, not a stub).

    Add `tests/test_migrations/test_016_upgrade.py` following the test_downgrade.py sequence
    pattern (drive your own up/down, clean up in finally via downgrade_to('base')):
      - downgrade_to('base'), upgrade_to('015').
      - Insert (via raw SQL, an agents row if FK requires it — reuse the agent-insert style
        from test_downgrade.py) several scan_batches rows: one COMPLETED + NULL completed_at,
        one FAILED + NULL completed_at, one RUNNING + NULL completed_at, and one COMPLETED with
        completed_at ALREADY set to a distinct timestamp. Set updated_at to a known value per row.
      - upgrade_to('016').
      - Assert: COMPLETED-null and FAILED-null rows now have completed_at == their updated_at;
        the RUNNING row still has completed_at IS NULL (untouched); the already-stamped COMPLETED
        row keeps its original completed_at (not clobbered).
    Mark the test module/functions @pytest.mark.asyncio like the sibling migration tests.
  </action>
  <verify>
    <automated>cd /Users/Robert/Code/public/phaze-pr2-scan-elapsed && uv run alembic heads | grep -q 016 && uv run pytest tests/test_migrations/test_016_upgrade.py -x -q</automated>
  </verify>
  <done>Migration 016 is the single head (down_revision 015); backfill SQL updates only terminal NULL rows; test proves COMPLETED/FAILED NULL rows get updated_at, RUNNING and already-stamped rows are untouched. (Migration test requires phaze_migrations_test DB on localhost:5432.)</done>
</task>

<task type="auto" tdd="true">
  <name>Task 2: Stamp completed_at in legacy run_scan + audit terminal writers</name>
  <files>src/phaze/services/ingestion.py, tests/test_services/test_ingestion.py</files>
  <behavior>
    - run_scan success: after run_scan completes, the ScanBatch row has status=COMPLETED AND completed_at is not None (tz-aware, ~= now).
    - run_scan failure: when discover_and_hash_files raises, the ScanBatch row has status=FAILED AND completed_at is not None, and run_scan re-raises.
    - Audit: no terminal-status writer other than run_scan and agent_scan_batches exists; the watcher/scan_live_set path writes only the non-terminal LIVE status.
  </behavior>
  <action>
    In `src/phaze/services/ingestion.py`: add `from datetime import UTC, datetime` to the imports
    (the module has `from __future__ import annotations`; place the import with the other stdlib
    imports near the top, respecting the existing isort grouping — force-sort-within-sections).
    In `run_scan`, add `completed_at=datetime.now(UTC)` to BOTH `.values(...)` calls:
    the COMPLETED update (~line 171-175) and the FAILED update (~line 183-187). Do not change the
    `raise` in the except block. This matches the already-correct stamping in agent_scan_batches.py.

    Audit (no code change unless a gap is found): the only modules referencing ScanStatus.COMPLETED/
    FAILED or completed_at are pipeline_scans.py, agent_scan_batches.py, the ScanBatch model, and
    ingestion.py (confirmed via grep). The watcher path uses ScanStatus.LIVE (non-terminal), so it
    needs no completed_at. Record this in the run_scan docstring or a short comment: terminal
    completion is written in exactly two places (run_scan here + agent PATCH in agent_scan_batches),
    both now stamping completed_at. If a third terminal writer is discovered, stamp it the same way.

    Add tests to `tests/test_services/test_ingestion.py` (run_scan currently has no coverage).
    Build a session factory from the existing `async_engine` fixture:
    `async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)` (mirror
    tests/conftest.py).
      - Success test: point run_scan at a tmp_path dir containing one known music file (reuse the
        tmp_path file-creation style from the existing discover tests), pass the factory, queue=None.
        After run_scan returns, open a session and load the ScanBatch by id; assert status == COMPLETED
        and completed_at is not None.
      - Failure test: patch `phaze.services.ingestion.discover_and_hash_files` to raise (use the
        `unittest.mock.patch` already imported in this file); assert run_scan raises, then load the
        row and assert status == FAILED and completed_at is not None and error_message is set.
  </action>
  <verify>
    <automated>cd /Users/Robert/Code/public/phaze-pr2-scan-elapsed && uv run pytest tests/test_services/test_ingestion.py -x -q && uv run ruff check src/phaze/services/ingestion.py && uv run mypy src/phaze/services/ingestion.py</automated>
  </verify>
  <done>run_scan stamps completed_at on both COMPLETED and FAILED transitions; success and failure tests prove it; ruff + mypy clean; audit documented in code that exactly two terminal writers exist.</done>
</task>

<task type="auto" tdd="true">
  <name>Task 3: Defensive elapsed_seconds — freeze terminal+NULL rows at updated_at</name>
  <files>src/phaze/routers/pipeline_scans.py, tests/test_routers/test_pipeline_scans.py</files>
  <behavior>
    - Terminal status (COMPLETED/FAILED) + completed_at None -> elapsed == updated_at - created_at (frozen), NOT now-based.
    - Terminal status + completed_at set -> uses completed_at (unchanged from today).
    - Non-terminal (RUNNING) + completed_at None -> still now - created_at (ticks; unchanged).
    - tz-naive updated_at is treated as UTC (same guard already applied to created_at/completed_at).
  </behavior>
  <action>
    In `src/phaze/routers/pipeline_scans.py::elapsed_seconds`, replace the single `end = ...` line
    with branching that picks the freeze point in this precedence:
      1. completed_at is not None -> end = completed_at  (existing behavior; keep the naive->UTC guard).
      2. else if batch.status in terminal set (COMPLETED, FAILED) -> end = batch.updated_at
         (freeze). Apply the same naive->UTC guard. Defensive fallback: if updated_at is None,
         use datetime.now(UTC) so the function never crashes.
      3. else (non-terminal / running) -> end = datetime.now(UTC)  (keeps ticking).
    Define the terminal set as a module-level constant near the top, e.g.
    `_TERMINAL_STATUSES = frozenset({ScanStatus.COMPLETED, ScanStatus.FAILED})`. `batch.status`
    is a str; StrEnum membership comparison (`batch.status in _TERMINAL_STATUSES`) works because
    StrEnum members compare/hash as their str value. Do NOT place fenced code in this plan.

    Update the docstring: the existing "Incident 260608" paragraph (lines ~68-71) describes only
    the completed_at-set freeze. Supersede it: terminal rows with a NULL completed_at (legacy/
    pre-backfill rows — incident 260609) freeze at `updated_at`; running rows still track now.
    Keep the tz-naive->UTC safety note, now also covering updated_at.

    Add tests to `tests/test_routers/test_pipeline_scans.py` (mirror the existing unit tests at
    lines 202-269 that construct a ScanBatch and set created_at/completed_at directly):
      - terminal+NULL freeze: status=COMPLETED, completed_at=None, created_at=now-100s,
        updated_at=now-40s -> assert 58 <= elapsed <= 62 (frozen at updated_at, NOT ~100).
      - FAILED variant: same shape with status=FAILED -> frozen.
      - tz-naive updated_at: terminal+NULL with updated_at set tz-naive -> treated as UTC, frozen.
    Leave the existing `test_elapsed_seconds_tracks_now_when_completed_at_none` (RUNNING) and
    `test_elapsed_seconds_freezes_when_completed_at_set` tests passing unchanged — verify they
    still pass under the new branching.
  </action>
  <verify>
    <automated>cd /Users/Robert/Code/public/phaze-pr2-scan-elapsed && uv run pytest tests/test_routers/test_pipeline_scans.py -x -q && uv run ruff check src/phaze/routers/pipeline_scans.py && uv run mypy src/phaze/routers/pipeline_scans.py</automated>
  </verify>
  <done>elapsed_seconds freezes terminal+NULL rows at updated_at, keeps running rows ticking, keeps completed_at-set behavior; new + existing unit tests pass; ruff + mypy clean; docstring updated to supersede the 260608 note.</done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| DB row -> admin UI render | Terminal rows with NULL completed_at produce a misleading ever-climbing timer (integrity/availability of the displayed metric, not a security breach). |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-PR2-01 | Tampering/Integrity | elapsed_seconds display value | mitigate | Backfill (016) + writer fix (run_scan) + defensive read (elapsed_seconds freezes terminal+NULL at updated_at). |
| T-PR2-02 | Information disclosure | migration raw SQL | accept | UPDATE uses no untrusted input (static literals); no injection surface. |
| T-PR2-SC | Tampering | npm/pip/cargo installs | accept | No new dependencies added by this task. |
</threat_model>

<verification>
Run the full quality gate from the worktree before committing each task:

```
cd /Users/Robert/Code/public/phaze-pr2-scan-elapsed
uv run pytest tests/test_services/test_ingestion.py tests/test_routers/test_pipeline_scans.py tests/test_migrations/test_016_upgrade.py -q
uv run pytest --cov=phaze --cov-report=term-missing -q   # confirm >=85% overall
uv run ruff check . && uv run ruff format --check .
uv run mypy .
pre-commit run --all-files                                # frozen hooks; never --no-verify
```

Migration tests require the operator-provisioned `phaze_migrations_test` DB on localhost:5432
(same pre-condition as the existing tests/test_migrations suite).
</verification>

<success_criteria>
- Migration 016 is the single Alembic head; backfill touches only terminal rows with NULL completed_at.
- run_scan stamps completed_at on both COMPLETED and FAILED transitions; no new NULL terminal rows.
- elapsed_seconds freezes terminal+NULL rows at updated_at; running rows still tick; completed_at-set behavior unchanged.
- No other terminal-status writer exists (audited); watcher LIVE path is non-terminal and correctly untouched.
- New tests cover all three branches; overall coverage stays >=85%; ruff + mypy + pre-commit clean.
- Scope held: no stall-heartbeat column, reaper, UI indicator (PR4), or structlog (PR3).
</success_criteria>

<output>
Create `.planning/quick/260609-pr2-scan-completed-at-elapsed/SUMMARY.md` when done.
</output>
