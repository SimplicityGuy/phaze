---
phase: 36-pipeline-queue-backend-migration-redis-to-postgres-saq
plan: 01
subsystem: infra
tags: [saq, postgres, psycopg3, redis, queue, pydantic-settings, config]

# Dependency graph
requires:
  - phase: 27-uat-fixes
    provides: apply_project_job_defaults before-enqueue hook (job-policy defaults)
  - phase: 35-pipeline-counters
    provides: apply_deterministic_key before-enqueue hook (anti-drift key + enqueued counter)
provides:
  - "saq[postgres] dependency extra installed (psycopg[pool]>=3.2.0); redis pinned as an explicit first-class dep"
  - "PHAZE_QUEUE_URL config field: driver-normalized libpq DSN, secret-backed via SECRET_FILE_FIELDS"
  - "build_pipeline_queue factory: the single PostgresQueue construction seam (both hooks + decoupled cache_redis handle)"
affects: [36-02, pipeline-queue-construction-sites, worker-startup]

# Tech tracking
tech-stack:
  added: ["psycopg 3.3.4 (via saq[postgres])", "psycopg-pool", "redis (now explicit, not transitive)"]
  patterns:
    - "Single construction seam: one factory owns PostgresQueue construction + hook registration + pool sizing"
    - "Driver-strip before-validator normalizes SQLAlchemy dialect DSN to raw libpq for psycopg3"
    - "Decoupled cache_redis handle attached to the queue object so backend-agnostic counter hooks read getattr(job.queue, 'cache_redis', None)"

key-files:
  created:
    - src/phaze/tasks/_shared/queue_factory.py
    - tests/test_queue_factory.py
  modified:
    - pyproject.toml
    - uv.lock
    - src/phaze/config.py

key-decisions:
  - "queue_url defaults to libpq postgresql:// form (NOT +asyncpg) — psycopg3's AsyncConnectionPool cannot parse the dialect suffix"
  - "Dropped saq[redis] removes transitive redis; pinned redis>=4.2,<8.0 explicitly because redis.asyncio is imported directly across the codebase"
  - "No production credential guard on queue_url this phase (RESEARCH Open Q1, deferred to homelab discussion)"
  - "cache_redis is the LOCKED mechanism (RESEARCH Open Q3): nothing reads queue.redis; the broker is Postgres now"

patterns-established:
  - "build_pipeline_queue(name, url, *, cache_redis_url, min_size=1, max_size=4) -> PostgresQueue is the sole queue construction API"
  - "Dynamic queue attribute (cache_redis) assigned with a targeted type: ignore[attr-defined]; read via getattr in hooks"

requirements-completed: [REQ-36-1, REQ-36-5]

# Metrics
duration: 18min
completed: 2026-06-12
---

# Phase 36 Plan 01: Queue Backend Migration Foundation Summary

**Swapped the SAQ extra to `saq[postgres]` (psycopg3 + pool), added the secret-backed `PHAZE_QUEUE_URL` libpq DSN with a driver-strip validator, and built `build_pipeline_queue` — the single PostgresQueue construction seam that registers both before-enqueue hooks and a decoupled `cache_redis` handle.**

## Performance

- **Duration:** ~18 min
- **Completed:** 2026-06-12
- **Tasks:** 3 (Task 3 was TDD: RED → GREEN)
- **Files modified:** 5 (2 created, 3 modified)

## Accomplishments
- `saq[postgres]>=0.26.4` resolves `psycopg 3.3.4` + `psycopg-pool`; `PostgresQueue` imports cleanly
- `redis>=4.2,<8.0` is now an explicit dependency (no longer transitive via `saq[redis]`) — `redis.asyncio` direct imports stay sound
- `PHAZE_QUEUE_URL` setting: defaults to a libpq `postgresql://` DSN, normalizes `+asyncpg`/`+psycopg` dialect forms, and is a member of `SECRET_FILE_FIELDS` (inherited by both roles)
- `build_pipeline_queue` factory returns a `PostgresQueue` with both `apply_project_job_defaults` and `apply_deterministic_key` registered, plus a `cache_redis` handle, opening NO connection at construction — proven by a 4-test unit suite

## Task Commits

Each task was committed atomically:

1. **Task 1: Swap SAQ extra to postgres, pin redis explicit** - `e2adfec` (chore)
2. **Task 2: Add PHAZE_QUEUE_URL with driver-strip + secret handling** - `f837322` (feat)
3. **Task 3 (TDD RED): failing unit proof for factory** - `f269aa1` (test)
4. **Task 3 (TDD GREEN): implement build_pipeline_queue factory** - `b4c1b2a` (feat)

## Files Created/Modified
- `pyproject.toml` - Replaced `saq[redis]` with `saq[postgres]>=0.26.4`; added explicit `redis>=4.2,<8.0` (both alphabetically placed)
- `uv.lock` - Resolved psycopg 3.3.4 + psycopg-pool
- `src/phaze/config.py` - Added `queue_url` Field, `_strip_sqlalchemy_driver` before-validator, and `queue_url` membership in `SECRET_FILE_FIELDS`
- `src/phaze/tasks/_shared/queue_factory.py` - The single PostgresQueue construction seam (`build_pipeline_queue`)
- `tests/test_queue_factory.py` - 4 construction-time contract tests (type, both hooks, cache_redis, no-connection)

## Decisions Made
- **libpq default DSN** for `queue_url` (`postgresql://`, never `+asyncpg`): psycopg3's `AsyncConnectionPool` rejects the SQLAlchemy dialect suffix. The before-validator lets operators still paste a `+asyncpg`/`+psycopg` DSN.
- **Explicit `redis` dependency**: dropping `saq[redis]` removes the transitive `redis`, but `redis.asyncio` is imported directly (cache handle, pipeline counters), so it must be first-class.
- **`cache_redis` mechanism** (RESEARCH Open Q3, LOCKED): the factory attaches the Redis client to the queue object; the backend-agnostic `before_enqueue`/`after_process` counter hooks read it via `getattr(job.queue, "cache_redis", None)`. Nothing reads `queue.redis`.

## Deviations from Plan

None - plan executed exactly as written. Two minor mechanical adjustments within plan intent:
- Fixed the pre-existing alphabetical mis-ordering of the SAQ dependency line when relocating it (CLAUDE.md mandates alphabetical sort) — `redis` and `saq[postgres]` now sit in their correct alphabetical positions.
- Added a targeted `# type: ignore[attr-defined]` on the dynamic `q.cache_redis = ...` assignment (SAQ's `PostgresQueue` does not declare the attribute); required for the plan's "mypy passes" acceptance criterion.

## Issues Encountered
- The TDD RED commit was first aborted by the `ruff-format` pre-commit hook reformatting one long line; re-staged the formatter's output and committed cleanly (no `--no-verify`, per project policy).

## Threat Surface
- T-36-02 (Information Disclosure): mitigated — `queue_url` added to `SECRET_FILE_FIELDS` (`<VAR>_FILE`/SOPS), never logged in full.
- T-36-05 (Tampering/input-validation): mitigated — `_strip_sqlalchemy_driver` normalizes to a libpq DSN at load.
- No new security surface beyond the plan's threat register.

## Known Stubs
None — the factory is fully wired; `cache_redis` and both hooks are live. Pool opening and call-site adoption are intentionally deferred to Plan 02 (documented in the module docstring).

## Verification
- `uv run pytest tests/test_queue_factory.py -x` → 4 passed
- `uv run python -c "from saq.queue.postgres import PostgresQueue"` → succeeds
- `uv run ruff check .` → clean; `uv run mypy src/phaze` → no issues (130 files)
- Regression: `tests/test_deterministic_key.py` + `tests/test_web/test_saq_mount.py` → 26 passed (both queue backends still import)

## Next Phase Readiness
- The one seam (factory + config) is in place. Plan 02 adopts `build_pipeline_queue` at all four construction sites and wires pool open/close in role startup.
- Blockers: none.

---
*Phase: 36-pipeline-queue-backend-migration-redis-to-postgres-saq*
*Completed: 2026-06-12*
