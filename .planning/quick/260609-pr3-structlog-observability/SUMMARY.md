# PR3 — structlog Observability Migration — SUMMARY

**Task:** `260609-pr3-structlog-observability`
**Branch:** `feat/structlog-observability` (worktree `/Users/Robert/Code/public/phaze-pr3-structlog`)
**Scope:** logging/observability only (NOT PR4 stall-heartbeat/reaper, NOT PR5 delete-scans)
**Status:** All 10 tasks complete. Final gate green (except known Redis-only test errors — no Redis in sandbox).

One-liner: migrated the whole project from bare `logging.getLogger` (34 sites) to a single
central structlog `configure_logging()` wired into every process entry point, with real
operational INFO/DEBUG across all major operations, JSON-in-prod / console-in-dev rendering,
and a Postgres-free import boundary kept intact.

---

## Commits (in order)

| # | Hash | Message |
|---|--------|---------|
| 1 | `1455af4` | build: add structlog dependency |
| 2 | `b40d194` | feat(logging): central structlog configure_logging + PHAZE_LOG_LEVEL/PHAZE_LOG_JSON knobs |
| 3 | `e512390` | feat(logging): wire configure_logging into all process entry points |
| 4 | `9bebdb8` | refactor(logging): swap tasks/ logger factories to structlog |
| 5 | `ba83148` | refactor(logging): swap services/ logger factories to structlog |
| 6 | `8d3dc6a` | refactor(logging): swap routers/watcher/misc logger factories to structlog |
| 7 | `4d7e0a0` | feat(logging): operational INFO/DEBUG for model bootstrap + scan |
| 8 | `d9659fb` | feat(logging): operational INFO/DEBUG for fingerprint/metadata/execution/discogs/tracklist + enqueue |
| 9 | `6e1470b` | test(logging): assert operational events emit at expected levels |
| 10 | `776bbbf` | docs: document structlog logging config + PHAZE_LOG_LEVEL/PHAZE_LOG_JSON |

---

## What was built

- `src/phaze/logging_config.py` (NEW): `configure_logging(*, level=None, json_logs=None)`.
  Single ProcessorFormatter bridge that renders structlog-native AND foreign stdlib / uvicorn /
  SAQ records through one root handler. JSON when stdout is not a TTY, console otherwise. Shared
  chain includes PositionalArgumentsFormatter (legacy `%s` still works), merge_contextvars,
  add_log_level, add_logger_name, ISO UTC timestamp, exc rendering. Noisy libs
  (httpx/httpcore/asyncio) pinned to WARNING unless DEBUG. Idempotent root reset. Env-fallback
  resolution (PHAZE_LOG_LEVEL / PHAZE_LOG_JSON). Imports ONLY stdlib + structlog.
- Settings knobs on `BaseSettings`: `log_level` (default INFO) and `log_json` (default None =
  auto) via AliasChoices, inherited by both Control and Agent roles.
- All six entry points call configure_logging() once per process: main.py::lifespan (before
  migrations), agent_worker.startup, controller.startup, agent_watcher.main() (bare/env-driven,
  before get_settings()), download_models.__main__, cli.main.
- 34/34 logging.getLogger call sites migrated to structlog.get_logger. The gate
  `grep -rln "logging.getLogger" src/phaze` is empty (central module imports the stdlib accessor
  as `_stdlib_get_logger`).
- Operational logging: model bootstrap (validating model weights, per-file downloading model /
  model download complete / model ok, models validated with real present/repaired counts threaded
  through download_to), scans (scan started / scan progress / scan completed with duration_s via
  time.monotonic() / scan failed, per-file file discovered DEBUG), fingerprint / metadata /
  execution / discogs / tracklist start+finish events, per-agent task enqueued INFO. Heartbeat
  stays DEBUG.
- Tests: tests/test_logging_config.py (13 unit tests incl. reconfigure-level regression),
  tests/test_logging_operational.py (4 level/flow assertions via structlog.testing.capture_logs).
  Autouse conftest fixture routes structlog through the stdlib bridge per test so caplog keeps
  working and global logging state cannot leak between tests.
- Docs: docs/configuration.md, docs/architecture.md, README.md, .env.example, .env.example.agent,
  agent_watcher/README.md.

---

## Deviations from plan

### cache_logger_on_first_use=False (Rule 1 — fix latent bug; design block specified True)

With True, each module-level structlog.get_logger(__name__) proxy permanently freezes its bound
logger at the level active on its FIRST log call; a later configure_logging() with a different
level is silently ignored for that logger, and structlog.reset_defaults() does not clear the
per-proxy cache. This breaks the module's documented idempotent/reconfigure guarantee and caused
cross-test contamination. Set to False so reconfiguration takes effect for every logger
(production still configures once per process before any logging). Locked in by
test_reconfigure_changes_level_for_already_used_logger. Documented inline.

### Test capture mechanism updates (Rule 1 — caused by the migration)

configure_logging() resets root handlers (by design), removing caplog's handler when the
code-under-test itself calls it. Three watcher tests and two startup-banner tests were switched
from caplog to capsys (tests the real rendered pipeline end-to-end). The signal-handler test now
sets PHAZE_LOG_LEVEL=DEBUG explicitly. Model-bootstrap test mocks return the (present, repaired)
tally. No behavior assertions weakened.

### download_to return type (sanctioned by the plan)

download_to now returns tuple[int, int] (present_count, repaired_count) and _ensure_present
returns bool, to thread real counts into the models validated event — the plan explicitly offered
"return a small result tally". Existing tests only check side-effects, unaffected.

---

## Secret invariant (D-13)

No new log line binds or logs agent_token / bearer secrets. Token-preview banners (first-12 +
"...") preserved; CLI mints token via print() only. Operational enrichment binds only ids /
counts / paths / sizes.

---

## Verification gate (run from worktree)

1. grep -rln "logging.getLogger" src/phaze --include="*.py" -> EMPTY (all 34 migrated).
2. uv run pytest tests/test_task_split.py -> 6 passed (Postgres-free import boundary intact).
3. uv run pytest (full) -> 1415 passed, 7 failed, 39 errors. Every failure/error is a
   redis.exceptions.ConnectionError to localhost:6379 (no Redis in sandbox); 92 ConnectionError
   occurrences, zero non-Redis assertion failures. Suites: test_agent_tracklists,
   test_agent_task_router, test_agent_exec_batches, test_execution_dispatch (all Redis-backed).
4. uv run ruff check . + ruff format --check . + mypy . -> all clean (137 source files).
5. pre-commit run --all-files -> all hooks Passed (no --no-verify).
6. Operational smoke: JSON mode emits parseable objects with event/level/timestamp/logger, DEBUG
   detail present, %s interpolation working; console mode human-friendly and suppresses DEBUG at
   INFO. Agent import-boundary smoke: agent_worker + agent_watcher import with logging_config
   loaded and NO phaze.database / sqlalchemy.ext.asyncio in sys.modules.
7. structlog>=25.4.0 in pyproject.toml (alphabetized); uv.lock updated (structlog 26.1.0); no
   unrelated dep churn.

Coverage: TOTAL 5523 stmts, 209 missing, 96.22% (>= 85% gate).

---

## Self-Check: PASSED

- src/phaze/logging_config.py, tests/test_logging_config.py, tests/test_logging_operational.py exist.
- All 10 commits present on feat/structlog-observability.
- grep gate empty; pre-commit all-files green; coverage 96.22%.
