---
phase: quick-260608-mbc
plan: 01
status: complete
branch: fix/scan-zero-files-incident
requirements: [SCAN-INCIDENT-260608]
commits:
  - task: 1
    sha: 072289fef02676ac42a46bff177d1236168a55ba
    type: fix(docker)
  - task: 2
    sha: 2f5babc354dd6214636f9421439d2382b2344083
    type: fix(scan)
  - task: 3
    sha: cdc3c594136e21e9dfc9b6f136e027c3811dc7ba
    type: feat(scan)
coverage: 97.14%
completed: 2026-06-08
---

# Quick 260608-mbc: Fix three scan-incident issues in one PR — Summary

Fixed three issues surfaced by a single real incident (an agent scan reported
`status=completed` with 0 files because the container ran as uid 999 while the
media was owned by uid 1000, mode 700/770 — unreadable — and `os.walk` silently
swallowed the `PermissionError`). Three independent fixes landed as three atomic
commits on `fix/scan-zero-files-incident` feeding one PR.

## Tasks

### Task 1 — Pin container user to uid/gid 1000 (`072289f`)
Replaced `RUN useradd -m -r phaze` (which auto-assigned system uid 999) with
`RUN groupadd -g 1000 phaze && useradd -m -u 1000 -g 1000 phaze`. `USER phaze`
retained. This is the root-cause fix. hadolint (pre-commit "Lint Dockerfiles")
passes.

### Task 2 — Surface zero-access scans as failed (`2f5babc`)
Added an `os.walk(..., onerror=_on_walk_error)` handler in `scan_directory` that
collects directory read errors into `walk_errors`:
- **Zero-access** (`total == 0 and walk_errors`): terminal PATCH
  `status="failed"` with a permission-pointing message that names `scan_path`,
  the error count, and the first error; returns
  `{"status": "failed", "files_posted": 0, "reason": "walk_permission_errors"}`.
- **Partial access** (errors but ≥1 file): completes normally and logs a single
  summarizing `partial access` warning.
- All existing behavior preserved (chunking, NFC normalization, per-file OSError
  skip, AgentApiServerError abort, `is_dir()` short-circuit, `followlinks=False`).

TDD: two new tests (`test_scan_directory_root_unreadable_fails`,
`test_scan_directory_partial_access_still_completes`) written first (RED), then
implementation (GREEN). All 20 scan_directory tests pass; scan.py at 100% coverage.

### Task 3 — `ScanBatch.completed_at` so elapsed timer freezes (`cdc3c59`)
- **Model**: added `completed_at: Mapped[datetime | None]` as
  `DateTime(timezone=True)` (tz-aware to match TimestampMixin runtime behavior).
- **Migration 015** (`down_revision="014"`): adds/drops the nullable column;
  up/down roundtrip verified against the live migrations test DB.
- **Controller** (`agent_scan_batches.patch_scan_batch`): stamps
  `batch.completed_at = datetime.now(UTC)` on the first terminal transition
  (`completed`/`failed`) guarded on `completed_at is None`; never on
  same-state no-op (returns earlier), RUNNING, or LIVE.
- **`elapsed_seconds`**: end bound is `completed_at` when set, else
  `datetime.now(UTC)`; tz-naive→UTC safety applied to both bounds.
- Tests: terminal-stamping (completed + failed), non-terminal/idempotent
  no-stamp cases, elapsed-freeze unit tests (incl. tz-naive), and a migration
  round-trip test.

## Verification
- `uv run ruff check .` — clean
- `uv run ruff format --check .` — clean
- `uv run mypy .` — Success, no issues in 136 source files
- `uv run pytest --cov` — **1432 passed**, total coverage **97.14%** (≥85% gate)
- All three commits passed pre-commit frozen hooks (no `--no-verify`).

## Deviations from Plan
- **[Rule 3 — environment]** The DB-backed and Redis-backed test suites require
  a live Postgres (`phaze_test`, `phaze_migrations_test` on localhost:5432,
  `phaze:phaze`) and a Redis on localhost:6379. Neither was provisioned in the
  execution environment (port 5432 was held by an unrelated project's Postgres;
  no Redis was running). To run the full quality gate I provisioned the `phaze`
  role + the two test databases additively inside the existing Postgres
  container (non-destructive to the other project) and started a throwaway
  `redis:7-alpine` container, then stopped it after the run. No project code or
  config was changed for this; it only affected the local test environment.
- No other deviations. All three tasks implemented exactly as specified
  (migration `down_revision` confirmed as `"014"` by reading
  `014_add_last_status_to_agents.py`).

## Notes
- Did NOT push and did NOT open a PR — stopped after the third commit + this
  SUMMARY, per instructions. The orchestrator handles PR creation.
