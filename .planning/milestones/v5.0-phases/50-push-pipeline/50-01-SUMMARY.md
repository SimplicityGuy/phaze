---
phase: 50-push-pipeline
plan: 01
subsystem: api
tags: [pydantic, config, saq, file-state, ssh-secrets, contracts]

# Dependency graph
requires:
  - phase: 49-cloud-routing
    provides: FileState.AWAITING_CLOUD held-state + cloud_route_threshold_sec + kind-scoped agent selection
provides:
  - FileState.PUSHING / FileState.PUSHED code-only StrEnum members (no migration, D-08)
  - ProcessFilePayload.expected_sha256 + scratch_path optional fields (D-11)
  - PushFilePayload (file_id/original_path/file_type/agent_id, extra=forbid)
  - agent_push.py push-callback schemas (PushedResponse, PushMismatchRequest, PushMismatchResponse)
  - ControlSettings.cloud_max_in_flight (≤N window, D-03), push_max_attempts (D-12), compute_scratch_dir
  - AgentSettings push/SSH/scratch knobs + push_ssh_key/push_known_hosts _FILE secrets (D-05/D-07)
affects: [50-02, 50-03, 50-04, 50-05, push-task, staging-cron, push-callbacks, compute-scratch]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Interface-first contract layer: states + payloads + config landed before behavior plans consume them"
    - "_FILE secret resolution extended to SSH credentials via SECRET_FILE_FIELDS (no new resolution code)"

key-files:
  created:
    - src/phaze/schemas/agent_push.py
    - tests/test_schemas/test_agent_push.py
    - tests/test_config/test_push_config.py
  modified:
    - src/phaze/models/file.py
    - src/phaze/schemas/agent_tasks.py
    - src/phaze/config.py
    - tests/test_models/test_core_models.py
    - tests/test_schemas/test_agent_tasks.py

key-decisions:
  - "PUSHING/PUSHED are code-only StrEnum members over the existing String(30) state column — no Alembic migration (ANALYSIS_FAILED/AWAITING_CLOUD precedent, D-08)"
  - "scratch_path is not None is itself the compute-read/ephemeral signal — no separate boolean flag (D-11)"
  - "file_id flows on the URL path, never in the push-callback request body (AUTH-01); request bodies carry only optional bounded diagnostics"
  - "push_ssh_key/push_known_hosts added to AgentSettings.SECRET_FILE_FIELDS so the existing _resolve_secret_files validator auto-resolves <VAR>_FILE siblings with zero new code (D-05/D-07)"
  - "All numeric knobs bounded with Field(gt=, lt=) so an out-of-range value fails fast at startup (cloud_route_threshold_sec precedent, T-50-config-oob)"

patterns-established:
  - "Push-callback schemas are ORM-free so they stay import-safe across the Postgres-free agent boundary"
  - "ControlSettings.compute_scratch_dir documents the must-match invariant with AgentSettings.cloud_scratch_dir (T-50-scratch-skew)"

requirements-completed: [CLOUDPIPE-01, CLOUDPIPE-02, CLOUDPIPE-03]

# Metrics
duration: 5min
completed: 2026-06-26
---

# Phase 50 Plan 01: Push Pipeline Contracts Summary

**Additive contract layer for the cloud push pipeline: two new FileState members, ProcessFilePayload scratch/integrity fields, PushFilePayload + push-callback schemas, and the ≤N-window / attempt-cap / SSH-scratch config knobs with _FILE secrets — zero behavior change to local-file analysis, no migration.**

## Performance

- **Duration:** ~5 min
- **Started:** 2026-06-26T03:27:45Z
- **Completed:** 2026-06-26T03:33:47Z
- **Tasks:** 3
- **Files modified:** 8 (3 created, 5 modified)

## Accomplishments
- `FileState.PUSHING` / `FileState.PUSHED` code-only members fit the existing `String(30)` column — no Alembic migration added.
- `ProcessFilePayload` gained optional `expected_sha256` + `scratch_path`; the five-field local producer stays byte-identical under `extra="forbid"`.
- New `PushFilePayload` and ORM-free `agent_push.py` callback schemas (`PushedResponse`, `PushMismatchRequest`, `PushMismatchResponse`) ready for the 50-03 client and 50-05 router.
- `ControlSettings` carries the load-bearing `cloud_max_in_flight` (≤N window), `push_max_attempts`, and `compute_scratch_dir`; `AgentSettings` carries the push/SSH/scratch knobs plus two `_FILE`-mounted SSH secrets auto-resolved via `SECRET_FILE_FIELDS`.

## Task Commits

Each task was committed atomically:

1. **Task 1: PUSHING/PUSHED states + payload fields (TDD)** — `ffc6663` (test, RED), `483bbbd` (feat, GREEN)
2. **Task 2: push-callback schemas (agent_push.py)** — `c8e07d7` (feat)
3. **Task 3: window/attempt + push/SSH/scratch config knobs with _FILE secrets** — `2a8e83a` (feat)

## Files Created/Modified
- `src/phaze/models/file.py` — added `PUSHING`/`PUSHED` FileState members (code-only, no migration).
- `src/phaze/schemas/agent_tasks.py` — added `ProcessFilePayload.expected_sha256`/`scratch_path`; new `PushFilePayload`.
- `src/phaze/schemas/agent_push.py` (new) — push-callback request/response schemas, ORM-free, `extra="forbid"`.
- `src/phaze/config.py` — `cloud_max_in_flight`/`push_max_attempts`/`compute_scratch_dir` on ControlSettings; `push_ssh_host`/`push_ssh_user`/`cloud_scratch_dir`/`push_timeout_sec`/`push_connect_timeout_sec` + `push_ssh_key`/`push_known_hosts` secrets on AgentSettings.
- `tests/test_models/test_core_models.py` — PUSHING/PUSHED state assertions.
- `tests/test_schemas/test_agent_tasks.py` — ProcessFilePayload scratch-field + PushFilePayload coverage.
- `tests/test_schemas/test_agent_push.py` (new) — push-callback schema coverage.
- `tests/test_config/test_push_config.py` (new) — config knob defaults/aliases/bounds + `_FILE` secret resolution.

## Decisions Made
See `key-decisions` frontmatter. All decisions follow established in-repo precedents (D-03/D-05/D-07/D-08/D-11/D-12) cited in 50-PATTERNS.md; no novel architecture introduced.

## Deviations from Plan

None - plan executed exactly as written.

(One minor inline adjustment: the `agent_push.py` module docstring originally named `phaze.database`/`phaze.models`/`sqlalchemy` literally, which tripped the Task-2 `grep -L` ORM-free acceptance check; reworded to "no database / model / ORM-engine imports" so the textual guard passes. The actual imports were always clean — uuid, typing, pydantic only. Not a behavior change.)

## Issues Encountered
- `tests/test_models/test_core_models.py::test_tables_created_in_database` errors in this worktree because no PostgreSQL is listening on `localhost:5432`. This is a pre-existing environment dependency unrelated to this plan (my changes touch only the StrEnum and pydantic layers); all non-DB model tests pass. Out of scope per the scope-boundary rule.

## User Setup Required
None for Phase 50 Plan 01 — the SSH push target (`PHAZE_PUSH_SSH_KEY_FILE`, `PHAZE_PUSH_KNOWN_HOSTS_FILE`) is operator-provisioned in Phase 51; this plan only declares the config fields with safe `None` defaults.

## Next Phase Readiness
- All downstream Phase 50 plans (50-02 routing seam, 50-03 push client/task, 50-04 compute scratch read/verify, 50-05 push callbacks + staging cron) can now import these contracts directly instead of exploring the codebase.
- `ruff check .`, `mypy .` (166 files), and the full config/schema test suites are green. No migration was added (D-08).

## Self-Check: PASSED

All created/modified source files exist on disk and all four task commits (ffc6663, 483bbbd, c8e07d7, 2a8e83a) are present in git history.

---
*Phase: 50-push-pipeline*
*Completed: 2026-06-26*
