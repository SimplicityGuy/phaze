---
phase: quick-260608-mbc
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - Dockerfile
  - src/phaze/tasks/scan.py
  - tests/test_tasks/test_scan_directory.py
  - src/phaze/models/scan_batch.py
  - alembic/versions/015_add_completed_at_to_scan_batches.py
  - src/phaze/routers/agent_scan_batches.py
  - src/phaze/routers/pipeline_scans.py
  - tests/test_routers/test_agent_scan_batches.py
  - tests/test_routers/test_pipeline_scans.py
autonomous: true
requirements: [SCAN-INCIDENT-260608]
must_haves:
  truths:
    - "The published image runs as uid 1000 / gid 1000 so it can read media owned by uid 1000 (mode 700/770)."
    - "A scan whose root (or every subdir) is unreadable reports status=failed with a permission-pointing message instead of completed/0 files."
    - "A partial-access scan that still found files reports completed and logs a single skipped-dirs warning."
    - "A ScanBatch's elapsed timer freezes once the batch reaches a terminal (completed/failed) state."
  artifacts:
    - path: "Dockerfile"
      provides: "uid/gid 1000 phaze user"
      contains: "useradd -m -u 1000 -g 1000 phaze"
    - path: "src/phaze/tasks/scan.py"
      provides: "os.walk onerror handler + zero-access failed PATCH"
      contains: "onerror"
    - path: "alembic/versions/015_add_completed_at_to_scan_batches.py"
      provides: "scan_batches.completed_at nullable column migration"
      contains: "completed_at"
    - path: "src/phaze/models/scan_batch.py"
      provides: "ScanBatch.completed_at mapped column"
      contains: "completed_at"
  key_links:
    - from: "src/phaze/routers/agent_scan_batches.py"
      to: "ScanBatch.completed_at"
      via: "terminal-transition stamp in PATCH handler"
      pattern: "completed_at"
    - from: "src/phaze/routers/pipeline_scans.py"
      to: "ScanBatch.completed_at"
      via: "elapsed_seconds freeze branch"
      pattern: "completed_at"
---

<objective>
Fix three issues surfaced by a single real incident: an agent scan reported status=completed with 0 files because the agent container ran as uid 999 while the media was owned by uid 1000 (mode 700/770) — unreadable — and os.walk silently swallowed the PermissionError.

Three independent fixes, one per task, each landing as one atomic commit on the already-checked-out branch `fix/scan-zero-files-incident` (single PR):
1. Dockerfile: pin the container user to uid 1000 / gid 1000 (eliminates the root cause).
2. scan_directory: surface zero-access scans as `failed` with a permission-pointing message (makes the failure mode visible instead of silent).
3. ScanBatch.completed_at: stamp a terminal timestamp so the elapsed timer stops (UI correctness for terminal batches).

Purpose: Make the exact incident impossible to hide again and stop the runaway elapsed clock on finished scans.
Output: Updated Dockerfile, scan.py, scan_batch.py model, migration 015, two routers, and extended tests — all green under the project quality gate.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/STATE.md
@./CLAUDE.md

# Fix 2 — scan walk
@src/phaze/tasks/scan.py
@tests/test_tasks/test_scan_directory.py
@src/phaze/schemas/agent_scan_batches.py

# Fix 3 — completed_at
@src/phaze/models/scan_batch.py
@src/phaze/models/base.py
@src/phaze/routers/agent_scan_batches.py
@src/phaze/routers/pipeline_scans.py
@alembic/versions/014_add_last_status_to_agents.py

# Fix 1 — Dockerfile
@Dockerfile

<interfaces>
<!-- Contracts the executor needs; extracted from the codebase. No exploration required. -->

ScanBatchPatch (src/phaze/schemas/agent_scan_batches.py) — extra="forbid":
  total_files: int | None = None
  processed_files: int | None = None
  status: Literal["running", "completed", "failed"] | None = None
  error_message: str | None = None

ScanStatus (src/phaze/models/scan_batch.py) is a StrEnum: RUNNING, COMPLETED, FAILED, LIVE.

TimestampMixin (src/phaze/models/base.py) column style:
  created_at: Mapped[datetime] = mapped_column(server_default=func.now())
  updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
  NOTE: these materialize as tz-aware (TIMESTAMP WITH TIME ZONE) at runtime — see the
  long docstring in pipeline_scans.elapsed_seconds. New completed_at MUST be tz-aware too.

elapsed_seconds(batch) (src/phaze/routers/pipeline_scans.py:53) currently:
  created_at = batch.created_at; if tz-naive -> assume UTC; return int((now(UTC) - created_at).total_seconds())

agent_scan_batches.patch_scan_batch handler (src/phaze/routers/agent_scan_batches.py):
  - Applies mutations via `for field, value in set_fields.items(): setattr(batch, field, value)` then commit.
  - `set_fields = body.model_dump(exclude_unset=True)`; `cur = ScanStatus(batch.status)`.
  - Idempotent same-state no-op returns BEFORE any write (must NOT bump completed_at).

scan_directory return shapes already in use:
  {"status": "completed", "files_posted": N}
  {"status": "failed", "files_posted": N, "reason": "scan_path_not_a_directory" | "controller_5xx"}

Migration head: 014 (down_revision="013"). New migration 015 down_revision="014".
Migration test convention: tests/test_migrations/ uses a `migrated_engine` fixture and
information_schema queries (see test_013_upgrade.py). conftest requires a live
phaze_migrations_test DB on localhost:5432.
</interfaces>
</context>

<tasks>

<task type="auto">
  <name>Task 1: Pin container user to uid 1000 / gid 1000 (Dockerfile)</name>
  <files>Dockerfile</files>
  <action>
Replace the `RUN useradd -m -r phaze` line (~line 24) so the image canonically runs as uid 1000, gid 1000. Use: `RUN groupadd -g 1000 phaze && useradd -m -u 1000 -g 1000 phaze`. Keep the following `USER phaze` line unchanged. Base image is python:3.14-slim — uid/gid 1000 is free on a clean slim image. Do NOT add a separate gid line if groupadd already creates it. This is the root-cause fix: the agent container previously got auto-assigned system uid 999 (because of `-r`), which could not read media owned by uid 1000 with mode 700/770.

No test currently asserts uid 999 (verified via grep — only Dockerfile lines 24-25 reference the user). Do not add a Docker build to CI for this. Ensure the Dockerfile still parses cleanly and any configured pre-commit hooks (hadolint/shellcheck if present) pass.
  </action>
  <verify>
    <automated>grep -q "useradd -m -u 1000 -g 1000 phaze" Dockerfile && ! grep -q "useradd -m -r phaze" Dockerfile && echo OK</automated>
  </verify>
  <done>Dockerfile creates the phaze user as uid 1000 / gid 1000; the `-r` system-uid form is gone; `USER phaze` retained; pre-commit (if it lints Dockerfiles) passes. Commit as one atomic commit.</done>
</task>

<task type="auto" tdd="true">
  <name>Task 2: Surface zero-access scans as failed (scan_directory onerror)</name>
  <files>src/phaze/tasks/scan.py, tests/test_tasks/test_scan_directory.py</files>
  <behavior>
    - Walk root raises PermissionError (onerror invoked) AND total==0 -> terminal PATCH ScanBatchPatch(status="failed", error_message=<names scan_path + count of dir read errors + first error + points at container-UID/ownership cause>); return {"status": "failed", "files_posted": 0, "reason": "walk_permission_errors"}.
    - Partial access: some subdirs error via onerror but >=1 file found -> still terminal PATCH status="completed"; a single warning logged summarizing N dirs skipped; return {"status": "completed", "files_posted": N}.
    - Existing behavior unchanged: chunking at scan_chunk_size, per-chunk processed_files PATCH, NFC normalization, per-file OSError skip+continue, AgentApiServerError abort path, is_dir() short-circuit, followlinks=False, no agent_id/id stamping.
  </behavior>
  <action>
In `scan_directory` (src/phaze/tasks/scan.py): the `os.walk(scan_root, followlinks=False)` at ~line 164 has no `onerror` handler, so a PermissionError raised while reading the root (or a subdir) is silently swallowed and the task returns status=completed/total_files=0 — indistinguishable from a genuinely empty directory. This is the exact failure mode that hid the incident.

Add a local list `walk_errors: list[OSError] = []` before the walk. Define a nested `def _on_walk_error(exc: OSError) -> None:` callback that appends `exc` to `walk_errors` AND logs `logger.warning("scan_directory: cannot read directory during walk: %s", exc)`. Pass `onerror=_on_walk_error` to `os.walk`. (os.walk passes the OSError instance to onerror; its `.filename` attribute carries the path.)

After the walk loop completes (inside the existing `try`, before/at the terminal PATCH):
- If `total == 0 and walk_errors`: PATCH `ScanBatchPatch(status="failed", error_message=...)` where the message NAMES `payload.scan_path`, the count `len(walk_errors)`, and the first error, and points at the likely UID/ownership cause. Example message: f"Scanned 0 files but hit {len(walk_errors)} directory read error(s) (first: {walk_errors[0]}). The agent container user likely cannot read {payload.scan_path} — check file ownership/permissions vs the container UID." Then `return {"status": "failed", "files_posted": 0, "reason": "walk_permission_errors"}`.
- Else (normal terminal path): if `walk_errors` is non-empty (partial access with files found), log a SINGLE `logger.warning("scan_directory: completed with partial access — %d director(ies) skipped (first: %s)", len(walk_errors), walk_errors[0])` before the existing terminal `completed` PATCH. Keep the existing completed PATCH + `return {"status": "completed", "files_posted": total}` exactly as-is.

Preserve all existing behavior: chunking, NFC normalization, per-file OSError skip, AgentApiServerError handling (the outer except), and the is_dir() short-circuit. Do not change the per-file try/except. Type-hint the callback (`-> None`) for mypy-strict.

Tests (tests/test_tasks/test_scan_directory.py) — mirror the existing monkeypatch-on-os.walk / scan_module style:
  (a) test_scan_directory_root_unreadable_fails: monkeypatch `scan_module.os.walk` with a generator that calls the passed `onerror` with a `PermissionError("[Errno 13] Permission denied: '<scan_path>'")` (set `.filename`) and yields nothing. Assert result == {"status": "failed", "files_posted": 0, "reason": "walk_permission_errors"}; assert the terminal PATCH carried status="failed" and error_message contains the scan_path and "ownership" or "permission"; assert upsert_files was never awaited.
  (b) test_scan_directory_partial_access_still_completes: monkeypatch os.walk to first invoke onerror for one subdir (PermissionError) then yield one real dir containing a single .mp3 (use tmp_path with a real file so hashing succeeds, or stub compute_sha256 like the existing skip test). Assert result["status"] == "completed", files_posted == 1, and that a warning containing "partial access" was logged (use caplog at WARNING on logger "phaze.tasks.scan").
  </action>
  <verify>
    <automated>uv run pytest tests/test_tasks/test_scan_directory.py -x -q</automated>
  </verify>
  <done>Both new tests pass alongside all existing scan_directory tests. A fully-unreadable scan returns reason=walk_permission_errors with a permission-pointing failed PATCH; a partial-access scan still completes and logs one warning. No existing behavior regressed. mypy/ruff clean. Commit as one atomic commit.</done>
</task>

<task type="auto" tdd="true">
  <name>Task 3: ScanBatch.completed_at so elapsed timer freezes</name>
  <files>src/phaze/models/scan_batch.py, alembic/versions/015_add_completed_at_to_scan_batches.py, src/phaze/routers/agent_scan_batches.py, src/phaze/routers/pipeline_scans.py, tests/test_routers/test_agent_scan_batches.py, tests/test_routers/test_pipeline_scans.py</files>
  <behavior>
    - Terminal PATCH (status -> "completed" or "failed") sets batch.completed_at = datetime.now(UTC) in the same commit.
    - Idempotent same-state PATCH does NOT set/bump completed_at (it returns before any write today — keep that).
    - A PATCH that does not transition into a terminal state (e.g. processed_files-only while RUNNING) leaves completed_at NULL.
    - completed_at is never set for LIVE or RUNNING target states.
    - elapsed_seconds(batch): when completed_at is set, return int((completed_at - created_at)); else int((now(UTC) - created_at)). Keep tz-naive-as-UTC safety for BOTH timestamps.
  </behavior>
  <action>
1. Model (src/phaze/models/scan_batch.py): add `completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)`. Import `datetime` from `datetime` and `DateTime` from sqlalchemy. Use `DateTime(timezone=True)` so the column is tz-aware to match the runtime behavior of TimestampMixin's columns (see elapsed_seconds docstring — created_at materializes tz-aware). Place the column after `error_message`.

2. Migration (alembic/versions/015_add_completed_at_to_scan_batches.py): NEW file. revision = "015"; down_revision = "014" (read 014_add_last_status_to_agents.py to confirm its revision id is "014" — it is; do not assume blindly, the file is in context). upgrade(): `op.add_column("scan_batches", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))`. downgrade(): `op.drop_column("scan_batches", "completed_at")`. Match the docstring/header style of 014 (Revision ID / Revises / Create Date, typed revision identifiers, `from alembic import op`, `import sqlalchemy as sa`).

3. Controller handler (src/phaze/routers/agent_scan_batches.py): in `patch_scan_batch`, after the transition guards and right before/within the mutation-apply block (the `for field, value in set_fields.items(): setattr(...)` loop, before `await session.commit()`), detect a terminal transition: if `body.status is not None and ScanStatus(body.status) in {ScanStatus.COMPLETED, ScanStatus.FAILED}` and `batch.completed_at is None`, set `batch.completed_at = datetime.now(UTC)`. Import `from datetime import UTC, datetime`. Do NOT set it for LIVE (already rejected) or RUNNING. The idempotent same-state no-op path returns earlier (line ~94) and must remain untouched so a same-state PATCH never stamps completed_at. Guarding on `batch.completed_at is None` keeps it idempotent across repeated terminal PATCHes (first terminal transition wins).

4. elapsed_seconds (src/phaze/routers/pipeline_scans.py:53): compute the end bound as `completed_at` when set, else `datetime.now(UTC)`. Apply the SAME existing tz-naive->UTC safety to `completed_at` as is applied to `created_at`. Keep `created_at` handling and the existing docstring rationale intact (extend the docstring with one line noting the completed_at freeze). Return `int((end - created_at).total_seconds())`.

Tests:
  - tests/test_routers/test_agent_scan_batches.py: add (i) terminal PATCH RUNNING->COMPLETED sets completed_at non-null (assert via re-fetch / response or a SELECT); (ii) RUNNING->FAILED sets completed_at; (iii) processed_files-only PATCH while RUNNING leaves completed_at NULL; (iv) idempotent same-state PATCH (status="running" on a RUNNING batch with only status set) does NOT set completed_at. Reuse the existing `_seed_batch` / smoke-app fixtures in that file.
  - tests/test_routers/test_pipeline_scans.py: add a unit test that elapsed_seconds freezes once completed_at is set — construct a ScanBatch with created_at = now-100s and completed_at = now-40s, assert elapsed is ~60 (60..62), independent of wall clock; and that with completed_at None it tracks now-created_at (mirror existing test_elapsed_seconds_handles_tz_aware_created_at style at line ~96). Also add a tz-naive completed_at case asserting it is treated as UTC.
  - Migration round-trip: tests/test_migrations covers upgrade-to-head + downgrade (see test_013_upgrade.py + test_downgrade.py). Add a check (in the existing migration test style, gated on the live phaze_migrations_test DB via the `migrated_engine` fixture) that `scan_batches.completed_at` exists and is nullable after head upgrade. If the migration suite is environment-gated/skipped in CI, keep the assertion lightweight and consistent with the existing convention — do NOT introduce a new DB dependency pattern.
  </action>
  <verify>
    <automated>uv run pytest tests/test_routers/test_agent_scan_batches.py tests/test_routers/test_pipeline_scans.py -x -q && uv run python -c "import ast,sys; ast.parse(open('alembic/versions/015_add_completed_at_to_scan_batches.py').read())"</automated>
  </verify>
  <done>ScanBatch has a nullable tz-aware completed_at column; migration 015 (down_revision 014) adds/drops it with a working downgrade; terminal PATCHes stamp completed_at once (idempotent, never on same-state/RUNNING/LIVE); elapsed_seconds freezes once completed_at is set with tz-naive safety on both bounds; new and existing router tests pass. Commit as one atomic commit.</done>
</task>

</tasks>

<verification>
Run the full project quality gate (must all pass, never use --no-verify):

```
uv run ruff check . && uv run ruff format . && uv run mypy . && uv run pytest --cov --cov-report=term-missing
```

- Coverage stays >= 85%.
- pre-commit frozen hooks pass.
- All three commits land on the existing branch `fix/scan-zero-files-incident` (do NOT create new branches). One commit per task = three atomic commits feeding a single PR.
</verification>

<success_criteria>
- Dockerfile runs as uid 1000 / gid 1000 (`-r` form gone).
- A fully-unreadable scan returns reason="walk_permission_errors" with a permission-pointing failed PATCH that names the scan_path; a partial-access scan completes and logs one warning; all prior scan_directory behavior intact.
- scan_batches.completed_at exists (nullable, tz-aware), migration 015 has a working up/down; terminal PATCHes stamp it once; elapsed_seconds freezes on terminal batches.
- ruff + mypy clean, pytest green, coverage >= 85%.
</success_criteria>

<output>
Create `.planning/quick/260608-mbc-fix-three-scan-incident-issues-in-one-pr/260608-mbc-SUMMARY.md` when done.
</output>
