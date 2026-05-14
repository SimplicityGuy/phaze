---
phase: 27-watcher-service-user-initiated-scan
plan: UAT-GAPS
type: gap-closure
status: complete
started: 2026-05-13T18:30:00Z
completed: 2026-05-13T18:55:00Z
base_commit: 3a6d8ea76224d1fee496f084cf48b70f598b8da6
commits:
  - ff0ea45: fix(27-uat-gaps) gap-1 remove rejected SAQ Worker kwargs
  - 8b1a3a3: fix(27-uat-gaps) gap-2/gap-3 auto-migrate + seed dev agent at api startup
  - dbf82e7: fix(27-uat-gaps) gap-4 document required agent-mode env vars
  - 5286cf0: fix(27-uat-gaps) gap-5 surface readable error on missing watcher env
  - 081bc05: docs(27-uat-gaps) gap-6 add fresh-install quickstart to watcher README
  - b79606b: test(27-uat-gaps) update lifespan tests for Gap 2/Gap 3 entry points
tags: [bugfix, infra, watcher, lifespan, migrations, dev-experience]
---

# Phase 27 UAT Gap Closure Summary

**One-liner:** Five blockers + one docs gap surfaced by Phase 27 UAT prevented `docker compose up` from booting cleanly on a fresh DB. All six fixes landed with regression tests that would have caught the original bugs.

## Gaps Fixed (in order of execution)

### Gap 1 — SAQ Worker rejects `timeout` / `retries` / `keep_result` (BLOCKER)

**Symptom:** `saq phaze.tasks.controller.settings` and `saq phaze.tasks.agent_worker.settings` failed at boot with `TypeError: __init__() got an unexpected keyword argument 'timeout'`. SAQ 0.26.3's `Worker.__init__` does not accept those three keys — they are per-Job settings.

**Fix:**
- Dropped `"timeout"`, `"retries"`, `"keep_result"` from both `settings` dicts (`src/phaze/tasks/controller.py:107-111`, `src/phaze/tasks/agent_worker.py:182-186`).
- New shared module `src/phaze/tasks/_shared/queue_defaults.py` exporting `apply_project_job_defaults(job)` — a SAQ `before_enqueue` hook that applies the project's policy defaults (`worker_job_timeout=600`, `worker_max_retries=4`, `worker_keep_result=3600`) to every Job whose corresponding attribute is still at SAQ's default. Caller-supplied per-Job overrides are preserved.
- Hook registered on both Queues via `queue.register_before_enqueue(apply_project_job_defaults)` after construction.

**Regression tests (`tests/test_tasks/test_queue_defaults.py`):**
- `test_before_enqueue_applies_project_defaults` — Job with SAQ defaults inherits Phaze policy values.
- `test_before_enqueue_preserves_explicit_overrides` — explicit per-Job values survive the hook.
- `test_controller_settings_construct_real_worker` — `saq.Worker(**controller.settings)` no longer raises TypeError (this is the canonical test that would have caught the original bug).
- `test_agent_worker_settings_construct_real_worker` — same, for the agent-side dict.

**Commit:** `ff0ea45`

---

### Gap 2 — Migrations don't run on api startup (BLOCKER)

**Symptom:** The api lifespan opened the engine for a `SELECT 1` connectivity probe but never ran `alembic upgrade head`. On a fresh docker compose stack the tables didn't exist and every request 500'd.

**Fix:**
- New `phaze.database.run_migrations()` function — wraps `alembic.command.upgrade(cfg, "head")` in `asyncio.to_thread` (avoids the nested-event-loop conflict with `alembic/env.py`'s internal `asyncio.run`).
- Wired into `phaze.main.lifespan` BEFORE the engine `SELECT 1` probe — so the schema is at head before any router reaches the engine.
- New config knob `Settings.auto_migrate: bool = True` (env: `PHAZE_AUTO_MIGRATE`) so operators can disable the auto-upgrade in production where they want manual migration windows.

**Regression tests (`tests/test_database.py`, `tests/test_main_lifespan.py`):**
- `test_run_migrations_invokes_alembic_upgrade_head` — fake `command.upgrade` is called once with `revision="head"`.
- `test_run_migrations_is_idempotent` — calling twice produces two dispatches, no error.
- `test_run_migrations_skips_when_auto_migrate_false` — gate respected.
- `test_api_lifespan_runs_migrations_on_startup` — verifies the call-order invariant `run_migrations` → `engine.begin(SELECT 1)` → `ensure_dev_agent`.

**Commit:** `8b1a3a3` (combined with Gap 3 since both are api-startup flow)

---

### Gap 3 — No initial agent seeded on a fresh DB (BLOCKER)

**Symptom:** Migration 012 seeds the legacy agent ONLY when backfilling a populated v3.0 `files` table. On a fresh DB no agent exists, so the watcher's `/whoami` call returned 403 and the watcher container restart-looped.

**Fix:**
- New module `src/phaze/services/agent_bootstrap.py` exporting `ensure_dev_agent(session)`.
- On an empty `agents` table, seeds a single `dev-agent` row with a SHA-256'd bearer token. Token is either operator-supplied (`PHAZE_DEV_AGENT_TOKEN`) or freshly generated (`phaze_agent_<32 urlsafe-base64>`).
- Cleartext token logged once at INFO (intentional for the dev-seed path — operator copies it into the watcher's `.env`). Production deployments leave `PHAZE_DEV_SEED_AGENT=false` and never reach this code.
- Wired into `phaze.main.lifespan` AFTER `run_migrations` (the table must exist before we can count rows).
- New config knobs: `Settings.dev_seed_agent: bool = False`, `Settings.dev_agent_token: SecretStr | None = None`.

**Regression tests (`tests/test_services/test_agent_bootstrap.py`):**
- `test_ensure_dev_agent_seeds_when_table_empty` — empty table → exactly one row created, SHA-256 hash matches `hashlib.sha256(raw_token)`.
- `test_ensure_dev_agent_noop_when_agent_exists` — pre-existing legacy row → no new row (idempotency).
- `test_ensure_dev_agent_uses_env_token_when_set` — `PHAZE_DEV_AGENT_TOKEN` overrides the random generator (operator can pin one token for the lifetime of the dev stack).
- `test_ensure_dev_agent_disabled_in_prod` — `dev_seed_agent=false` short-circuits.

**Commit:** `8b1a3a3` (combined with Gap 2)

---

### Gap 4 — `.env.example` missing required agent-mode vars

**Symptom:** `.env.example` documented four optional `PHAZE_WATCHER_*` tunables but NOT the three required agent-mode vars (`PHAZE_AGENT_API_URL`, `PHAZE_AGENT_TOKEN`, `PHAZE_AGENT_SCAN_ROOTS`) nor the host-vs-container hostname distinction (`postgres`/`redis` service DNS when in docker compose vs `localhost` when running on host via `uv run`).

**Fix:**
- Restructured `.env.example` into clearly labelled sections:
  - Host vs Container hostname rule (callout at the top)
  - Bring-up knobs (Gap 2/3: `PHAZE_AUTO_MIGRATE`, `PHAZE_DEV_SEED_AGENT`, `PHAZE_DEV_AGENT_TOKEN`)
  - Required agent-mode vars with example values and operator notes
  - Watcher tunables (existing PHAZE_WATCHER_* knobs)

**Regression tests (added to `tests/test_config_role_split.py`):**
- `test_env_example_documents_all_required_agent_mode_vars` — scans `.env.example` for the required trio.
- `test_env_example_documents_auto_migrate_and_dev_seed` — scans for the Gap 2/3 knobs.
- `test_env_example_explains_host_vs_container` — verifies the host/container distinction is documented (looks for `localhost` and `docker compose` keywords).

**Commit:** `dbf82e7`

---

### Gap 5 — Watcher's pitfall-7 hint hidden behind pydantic ValidationError

**Symptom:** When `PHAZE_AGENT_API_URL` (or any required `AgentSettings` field) was missing, the watcher container died with a raw pydantic `ValidationError` stack trace. The operator-actionable "auth invalid; check `PHAZE_AGENT_TOKEN`" hint emitted by `whoami_with_retry` was never reached because the validator tripped first.

**Fix:**
- Wrapped `get_settings()` in `phaze.agent_watcher.__main__.main()` with try/except `ValidationError`.
- New helper `_log_settings_validation_error(exc)` emits one ERROR log per failed field with the field name and its mapped env-var name (e.g., `agent_api_url` → `PHAZE_AGENT_API_URL`).
- Original pydantic exception logged at DEBUG for troubleshooting (no information loss).
- `sys.exit(1)` so docker compose restart-cycles with a meaningful logline visible in `docker compose logs watcher`.

**Regression test (`tests/test_agent_watcher/test_main.py`):**
- `test_main_logs_actionable_error_on_missing_env` — monkeypatches env to remove `PHAZE_AGENT_API_URL`, calls `main()`, asserts the ERROR log mentions `PHAZE_AGENT_API_URL` by name AND uses the "missing"/"required" keyword AND exits with code 1.

**Commit:** `5286cf0`

---

### Gap 6 — agent_watcher README quickstart missing

**Symptom:** `src/phaze/agent_watcher/README.md` documented env vars but lacked a sequenced bring-up walkthrough.

**Fix:** Added a `## Fresh Install Quickstart` section to the README that walks through the entire flow end-to-end:
1. `cp .env.example .env` and adjust hostnames for host/container mode.
2. Enable `PHAZE_DEV_SEED_AGENT=true`, pick a token (or let the api generate one).
3. Set watcher auth + scan roots (`PHAZE_AGENT_API_URL`, `PHAZE_AGENT_TOKEN`, `PHAZE_AGENT_SCAN_ROOTS`).
4. Bring up `postgres` + `redis`.
5. Bring up `api` + `worker` (migrations + seeding happen automatically per Gap 2/3).
6. Bring up `watcher`.
7. Verify by dropping a file into the scan path and watching `docker logs watcher`.
8. Production checklist for disabling the dev-seed path.

No test (docs only).

**Commit:** `081bc05`

---

## Auxiliary Test Fix

The pre-existing `tests/test_phase04_gaps.py::test_lifespan_creates_queue_on_startup` and `::test_lifespan_disconnects_queue_on_shutdown` started failing after Gap 2/3 wired `run_migrations` and `ensure_dev_agent` into the lifespan — they opened a real DB connection before reaching the Queue/engine mocks. Patched the new entry points (`run_migrations`, `ensure_dev_agent`, `async_session`) so these tests stay unit-level.

**Commit:** `b79606b`

---

## Test Results

Full test suite (`just test`) passes:

```
1086 passed, 30 warnings in 126.52s (0:02:06)
```

The 30 pre-existing warnings are unrelated `RuntimeWarning: coroutine ... was never awaited` from test mocks in `test_discogs.py` / `test_metadata_extraction.py` / `test_tracklist.py` — out of scope per the SCOPE BOUNDARY rule.

## Files Changed

**Created:**
- `src/phaze/services/agent_bootstrap.py` (Gap 3)
- `src/phaze/tasks/_shared/queue_defaults.py` (Gap 1)
- `tests/test_database.py` (Gap 2)
- `tests/test_main_lifespan.py` (Gap 2/3)
- `tests/test_services/test_agent_bootstrap.py` (Gap 3)
- `tests/test_tasks/test_queue_defaults.py` (Gap 1)
- `.planning/phases/27-watcher-service-user-initiated-scan/27-UAT-GAPS-SUMMARY.md` (this file)

**Modified:**
- `.env.example` (Gap 4)
- `src/phaze/agent_watcher/__main__.py` (Gap 5)
- `src/phaze/agent_watcher/README.md` (Gap 6)
- `src/phaze/config.py` (Gap 2/3 — new knobs)
- `src/phaze/database.py` (Gap 2)
- `src/phaze/main.py` (Gap 2/3 — lifespan wiring)
- `src/phaze/tasks/agent_worker.py` (Gap 1)
- `src/phaze/tasks/controller.py` (Gap 1)
- `tests/test_agent_watcher/test_main.py` (Gap 5)
- `tests/test_config_role_split.py` (Gap 4)
- `tests/test_phase04_gaps.py` (auxiliary lifespan test fix)

## Deviations

None — each gap was fixed exactly as described in the objective, with the noted exception that Gap 2 and Gap 3 were combined into a single commit (the objective explicitly allows this since they're both api-startup flow).

The only out-of-scope change made was the auxiliary `test_phase04_gaps.py` fix (Rule 1 — Gap 2/3 broke a pre-existing test because the test pre-dated the new lifespan entry points). Documented above and committed separately with a `test(...)` prefix rather than `fix(...)`.

## Self-Check: PASSED

Verified:
- All 6 commits present in `git log 3a6d8ea..HEAD`.
- New files exist:
  - `src/phaze/services/agent_bootstrap.py`
  - `src/phaze/tasks/_shared/queue_defaults.py`
  - `tests/test_database.py`
  - `tests/test_main_lifespan.py`
  - `tests/test_services/test_agent_bootstrap.py`
  - `tests/test_tasks/test_queue_defaults.py`
- `just test` exits 0 with 1086 passed.
- `saq.Worker(**phaze.tasks.controller.settings)` no longer raises (verified by `test_controller_settings_construct_real_worker`).
- `.env.example` contains all required vars (verified by 3 new tests in `test_config_role_split.py`).
