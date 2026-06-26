---
task: pr3-structlog-observability
type: quick
branch: feat/structlog-observability
worktree: /Users/Robert/Code/public/phaze-pr3-structlog
scope: logging/observability only (NOT PR4 stall-heartbeat/reaper, NOT PR5 delete-scans)
files_modified:
  - pyproject.toml
  - uv.lock
  - src/phaze/logging_config.py            # NEW
  - src/phaze/config.py
  - src/phaze/main.py
  - src/phaze/tasks/agent_worker.py
  - src/phaze/tasks/controller.py
  - src/phaze/agent_watcher/__main__.py
  - src/phaze/scripts/download_models.py
  - src/phaze/cli/__init__.py
  - src/phaze/tasks/_shared/model_bootstrap.py
  - src/phaze/tasks/scan.py
  - src/phaze/services/ingestion.py
  - "src/phaze/tasks/*.py + tasks/_shared/*.py (mechanical getLogger swap)"
  - "src/phaze/services/*.py (mechanical getLogger swap)"
  - "src/phaze/routers/*.py + agent_watcher/*.py + database.py + cert_bootstrap.py (mechanical swap)"
  - tests/test_logging_config.py           # NEW
  - tests/test_logging_operational.py      # NEW
  - README.md
  - docs/configuration.md
  - docs/architecture.md
  - .env.example
  - .env.example.agent
  - src/phaze/agent_watcher/README.md

commit_style: many small atomic commits — ONE atomic commit per task below.
---

<objective>
Migrate the whole `phaze` project from bare-stdlib `logging.getLogger(__name__)` (34
call sites, no central config) to structlog with a single `configure_logging()` entry
point. Route stdlib + uvicorn + SAQ + foreign-lib logs through one consistent pipeline
(JSON in prod / console in dev), wire it into EVERY process entry point, and add real
operational logging — INFO that proves work is happening (downloads, scans, fingerprints,
all major operations) plus verbose DEBUG detail.

Purpose: production logs today show almost no operational signal — an operator cannot tell
whether a running scan is doing work. This makes the app observable.

Output: `src/phaze/logging_config.py`, settings knobs (`PHAZE_LOG_LEVEL` / `PHAZE_LOG_JSON`),
all entry points wired, all 34 call sites migrated, operational logs at the core operations,
tests, and updated docs.
</objective>

<context>
@/Users/Robert/Code/public/phaze-pr3-structlog/CLAUDE.md
@/Users/Robert/Code/public/phaze-pr3-structlog/src/phaze/config.py
@/Users/Robert/Code/public/phaze-pr3-structlog/src/phaze/main.py
@/Users/Robert/Code/public/phaze-pr3-structlog/src/phaze/tasks/agent_worker.py
@/Users/Robert/Code/public/phaze-pr3-structlog/src/phaze/tasks/controller.py
@/Users/Robert/Code/public/phaze-pr3-structlog/src/phaze/agent_watcher/__main__.py
@/Users/Robert/Code/public/phaze-pr3-structlog/src/phaze/tasks/scan.py
@/Users/Robert/Code/public/phaze-pr3-structlog/tests/test_task_split.py

INTERFACES (extracted — use directly, no codebase exploration needed):

config.py — role-split settings (Phase 26 D-14):
  - BaseSettings(PydanticBaseSettings): shared fields; model_config extra="ignore".
    Fields bind via validation_alias=AliasChoices("PHAZE_X","X","x").
  - ControlSettings(BaseSettings), AgentSettings(BaseSettings).
  - get_settings() -> BaseSettings (lru_cache; PHAZE_ROLE dispatch; AgentSettings()
    RAISES at construction if PHAZE_AGENT_API_URL/TOKEN/SCAN_ROOTS missing).
  - module-level `settings: ControlSettings = _build_default_settings()` (back-compat).
  Add the new log_* fields to BaseSettings so BOTH roles inherit them.

Entry points (each runs its own OS process — each MUST call configure_logging()):
  - main.py::lifespan                  (FastAPI/api) — call at top, BEFORE run_migrations().
  - tasks/agent_worker.py::startup     (SAQ agent worker process).
  - tasks/controller.py::startup       (SAQ control worker process).
  - agent_watcher/__main__.py::main    (asyncio) — has a local `_configure_logging()`
    called FIRST (before get_settings()); REPLACE its body to delegate to the central fn.
  - scripts/download_models.py::__main__   (CLI).
  - cli/__init__.py::main              (CLI `phaze agents add`).

IMPORT-BOUNDARY INVARIANT (tests/test_task_split.py subprocess gates — MUST stay green):
  These modules + their transitive imports MUST NOT import phaze.database,
  phaze.tasks.session, or sqlalchemy.ext.asyncio:
    phaze.tasks.agent_worker, phaze.agent_watcher, phaze.tasks._shared.agent_bootstrap,
    phaze.tasks._shared.model_bootstrap, phaze.cert_bootstrap (also no phaze.config).
  => logging_config.py imports ONLY stdlib logging/sys/os + structlog. NO phaze.* / DB.
</context>

<design>
## configure_logging() — exact shape (Task 2)

Keyword-only, env-fallback so it is decoupled from full settings construction:

    def configure_logging(*, level: str | None = None, json_logs: bool | None = None) -> None

Resolution (so the watcher can configure logging BEFORE settings validation):
  - level = level or os.environ.get("PHAZE_LOG_LEVEL") or "INFO"   (upper-cased)
  - json  = json_logs if json_logs is not None
            else _parse_bool(os.environ["PHAZE_LOG_JSON"]) if that env is set
            else (not sys.stdout.isatty())     # default: JSON when not a TTY

Shared processor chain (order matters):
  merge_contextvars, add_log_level, add_logger_name,
  PositionalArgumentsFormatter(),          # CRITICAL: keeps legacy logger.info("x %s", v) working
  TimeStamper(fmt="iso", utc=True), StackInfoRenderer(), format_exc_info

structlog.configure(
  processors=[*shared, ProcessorFormatter.wrap_for_formatter],
  logger_factory=structlog.stdlib.LoggerFactory(),
  wrapper_class=structlog.make_filtering_bound_logger(level_int),
  cache_logger_on_first_use=True,
)
renderer  = JSONRenderer() if json else dev.ConsoleRenderer()
formatter = ProcessorFormatter(foreign_pre_chain=shared,
            processors=[ProcessorFormatter.remove_processors_meta, renderer])

Root handler (idempotent — clear first so re-calling is safe):
  root = logging.getLogger(); root.handlers.clear()
  h = logging.StreamHandler(sys.stdout); h.setFormatter(formatter); root.addHandler(h)
  root.setLevel(level_int)

Tame noisy libs (WARNING unless level==DEBUG):
  for name in ("httpx","httpcore","asyncio"):
      logging.getLogger(name).setLevel(WARNING if level_int>DEBUG else level_int)

Route uvicorn through root:
  for name in ("uvicorn","uvicorn.error","uvicorn.access"):
      lg=logging.getLogger(name); lg.handlers.clear(); lg.propagate=True

## Settings knobs (Task 2 — on BaseSettings so both roles inherit)
  log_level: str = Field(default="INFO",
      validation_alias=AliasChoices("PHAZE_LOG_LEVEL","log_level"))
  log_json: bool | None = Field(default=None,
      validation_alias=AliasChoices("PHAZE_LOG_JSON","log_json"),
      description="True=JSON, False=console, None=auto (JSON when stdout is not a TTY).")

Entry points holding a settings instance pass it through:
  configure_logging(level=cfg.log_level, json_logs=cfg.log_json)
The watcher calls bare configure_logging() (env-driven) BEFORE get_settings() so a
pydantic ValidationError can still be logged. Both paths read the same env vars;
settings is the typed/documented source, env is the watcher's pre-settings fallback.

## Mechanical swap rule (Tasks 4a/4b/4c)
  import logging                            ->  import structlog
  logger = logging.getLogger(__name__)      ->  logger = structlog.get_logger(__name__)
Keep every call site working unchanged (PositionalArgumentsFormatter preserves %s).
Do NOT restructure call sites in mechanical tasks — structured key=value enrichment
happens ONLY in operational tasks (7/8). Leave logger.exception(...) / exc_info= as-is.
If a module also uses logging constants (e.g. logging.WARNING), keep `import logging`
AND add `import structlog`.
</design>

<tasks>

<task type="auto" n="1">
  <name>Task 1: Add structlog dependency + lock</name>
  <files>pyproject.toml, uv.lock</files>
  <action>Add "structlog>=25.4.0" to [project] dependencies in pyproject.toml,
    alphabetically (between "sse-starlette..." and "tenacity..."). Run `uv lock`. No
    plugin/extra. Confirm the litellm cap and other pins are untouched by the lock.</action>
  <verify>grep "structlog" pyproject.toml; then `uv sync --frozen` succeeds and `uv run python -c "import structlog"` works.</verify>
  <done>structlog resolves and imports; uv.lock updated; no unrelated dep churn.</done>
  <commit>build: add structlog dependency</commit>
</task>

<task type="auto" n="2" tdd="true">
  <name>Task 2: Central logging_config + settings knobs + unit tests</name>
  <files>src/phaze/logging_config.py, src/phaze/config.py, tests/test_logging_config.py</files>
  <behavior>
    - json_logs=True -> root emits valid JSON (one parseable object per line with
      "event","level","timestamp","logger").
    - json_logs=False -> console (non-JSON) output.
    - level="DEBUG" -> a DEBUG record from structlog.get_logger is emitted; level="INFO"
      suppresses the same DEBUG record.
    - Idempotent: calling twice leaves exactly ONE handler on the root logger.
    - Foreign stdlib log flows through: logging.getLogger("uvicorn.error").info(...) after
      configure_logging is rendered by the same pipeline (has timestamp/level).
    - Noisy libs: after level="INFO", logging.getLogger("httpx") effective level is
      WARNING; after level="DEBUG" it is DEBUG.
    - Env fallback: no args + PHAZE_LOG_LEVEL=DEBUG in env -> level resolves DEBUG.
  </behavior>
  <action>Create src/phaze/logging_config.py exactly per the design block:
    configure_logging(*, level=None, json_logs=None), the ProcessorFormatter bridge,
    PositionalArgumentsFormatter in the shared chain, idempotent root-handler reset,
    noisy-lib taming, uvicorn re-routing, private _parse_bool/_resolve_level/_resolve_json
    helpers. Import ONLY stdlib + structlog (no phaze.*, no DB). Full type hints, double
    quotes, line length 150. In config.py add log_level and log_json to BaseSettings per
    the design (AliasChoices convention). Write tests/test_logging_config.py covering every
    behavior bullet — use capsys/caplog, parse JSON lines; do NOT assert exact full log
    strings (test wiring + levels, not formatting cosmetics). Add a fixture that calls
    structlog.reset_defaults() + clears root handlers in teardown so tests do not leak
    config into each other.</action>
  <verify>`uv run pytest tests/test_logging_config.py -x -q` green; `uv run mypy src/phaze/logging_config.py` clean; `uv run ruff check src/phaze/logging_config.py src/phaze/config.py` clean.</verify>
  <done>logging_config.py exists, fully typed, all unit tests green; settings expose
    log_level/log_json; module imports no DB/phaze.*.</done>
  <commit>feat(logging): central structlog configure_logging + PHAZE_LOG_LEVEL/PHAZE_LOG_JSON knobs</commit>
</task>

<task type="auto" n="3">
  <name>Task 3: Wire configure_logging into every entry point</name>
  <files>src/phaze/main.py, src/phaze/tasks/agent_worker.py, src/phaze/tasks/controller.py, src/phaze/agent_watcher/__main__.py, src/phaze/scripts/download_models.py, src/phaze/cli/__init__.py</files>
  <action>Call configure_logging once per process, as early as possible:
    - main.py: very top of lifespan, BEFORE run_migrations(), call
      configure_logging(level=settings.log_level, json_logs=settings.log_json).
    - agent_worker.py::startup: after `cfg=get_settings()` + isinstance check, call
      configure_logging(level=cfg.log_level, json_logs=cfg.log_json) BEFORE its first logger.info.
    - controller.py::startup: after `cfg=get_settings()`, call configure_logging(...) BEFORE its first log.
    - agent_watcher/__main__.py: REPLACE the body of the local _configure_logging() so it
      delegates to configure_logging() (bare/env-driven — it runs BEFORE get_settings(); keep
      idempotent). Keep the _configure_logging() name + its main() call site so the Gap-7
      "watcher logs reach docker logs" guarantee holds. Remove the now-unused stdlib
      Formatter/StreamHandler code.
    - download_models.py: in `if __name__ == "__main__":`, call configure_logging() before download_to(target).
    - cli/__init__.py::main: call configure_logging() at the top of main() (before argparse).
      The minted token stays print()-only — NEVER logged.
    Import `from phaze.logging_config import configure_logging` where used. Do NOT add it to
    phaze.entrypoint or phaze.cert_bootstrap (they must stay config-free; the api gets logging
    from main.py lifespan).</action>
  <verify>`uv run pytest tests/test_task_split.py tests/test_main_lifespan.py tests/test_agent_watcher tests/test_cli tests/test_config_worker.py -q` green; `uv run ruff check src/phaze` clean.</verify>
  <done>All six entry points configure logging; import-boundary subprocess tests stay green;
    watcher still configures logging before settings load.</done>
  <commit>feat(logging): wire configure_logging into all process entry points</commit>
</task>

<task type="auto" n="4">
  <name>Task 4a: Mechanical getLogger swap — tasks/ + tasks/_shared/</name>
  <files>src/phaze/tasks/agent_worker.py, controller.py, scan.py, fingerprint.py, metadata_extraction.py, discogs.py, execution.py, heartbeat.py, _shared/agent_bootstrap.py, _shared/model_bootstrap.py, _shared/queue_defaults.py (only those that define a module logger)</files>
  <action>Apply the mechanical swap rule (design block) to every file under src/phaze/tasks
    that has `logger = logging.getLogger(__name__)`. Replace import (preserve isort: structlog
    is third-party, force-sort-within-sections) and the getLogger line. Do not touch other
    call sites. agent_worker/controller already got configure_logging in Task 3 — only swap
    their logger factory here.</action>
  <verify>`grep -rl "logging.getLogger" src/phaze/tasks` returns nothing; `uv run pytest tests/test_task_split.py tests/test_tasks -q` green; `uv run mypy src/phaze/tasks` clean; `uv run ruff check src/phaze/tasks` clean.</verify>
  <done>No `logging.getLogger` remains under tasks/; import-boundary gates green.</done>
  <commit>refactor(logging): swap tasks/ logger factories to structlog</commit>
</task>

<task type="auto" n="5">
  <name>Task 4b: Mechanical getLogger swap — services/</name>
  <files>src/phaze/services/agent_bootstrap.py, agent_client.py, agent_task_router.py, discogs_matcher.py, execution.py, fingerprint.py, ingestion.py, metadata.py, tag_writer.py, tracklist_scraper.py</files>
  <action>Apply the mechanical swap rule to every services/*.py with a module logger. No
    call-site restructuring. Note: services/fingerprint.py is in the agent import graph —
    keep it Postgres-free (structlog is fine).</action>
  <verify>`grep -rl "logging.getLogger" src/phaze/services` returns nothing; `uv run pytest tests/test_services tests/test_task_split.py -q` green; `uv run mypy src/phaze/services` clean; `uv run ruff check src/phaze/services` clean.</verify>
  <done>No `logging.getLogger` remains under services/.</done>
  <commit>refactor(logging): swap services/ logger factories to structlog</commit>
</task>

<task type="auto" n="6">
  <name>Task 4c: Mechanical getLogger swap — routers/ + watcher/ + misc</name>
  <files>src/phaze/routers/admin_agents.py, agent_files.py, cue.py, execution.py, pipeline_scans.py, src/phaze/agent_watcher/__main__.py, debouncer.py, observer.py, poster.py, src/phaze/database.py, src/phaze/cert_bootstrap.py, src/phaze/scripts/download_models.py</files>
  <action>Apply the mechanical swap rule to the remaining call sites. cert_bootstrap.py MUST
    NOT import phaze.config — structlog.get_logger is fine (no config dependency). database.py
    is control-only (not in the agent graph) so structlog there is unconstrained.</action>
  <verify>`grep -rl "logging.getLogger" src/phaze` returns nothing (all 34 migrated); `uv run pytest tests/test_task_split.py tests/test_routers tests/test_agent_watcher tests/test_database.py tests/test_cert_bootstrap.py tests/test_scripts -q` green; `uv run mypy src/phaze` clean; `uv run ruff check src/phaze` clean.</verify>
  <done>Zero `logging.getLogger` left in src/phaze; all boundary gates green.</done>
  <commit>refactor(logging): swap routers/watcher/misc logger factories to structlog</commit>
</task>

<task type="auto" n="7">
  <name>Task 7: Operational logging — model bootstrap + scan (core user ask)</name>
  <files>src/phaze/tasks/_shared/model_bootstrap.py, src/phaze/scripts/download_models.py, src/phaze/tasks/scan.py, src/phaze/services/ingestion.py</files>
  <action>Enrich the existing migrated loggers with structured events (event name + bound
    context). Convert noisy `%s` lines to key=value where it improves signal; keep behavior
    identical. Add:
    Model bootstrap (model_bootstrap.py + download_models.py):
      INFO "validating model weights" (count, dir); per repaired file INFO "downloading model"
      (file, reason=missing|size_mismatch, expected_bytes); INFO "model download complete"
      (file, bytes); INFO "models validated" (present_count, repaired_count); DEBUG "model ok"
      (file, size) per kept file. Thread a small counter through download_to/_ensure_present
      so present_count/repaired_count are real (e.g. return a small result tally or log per
      file inside _ensure_present). Do NOT change the validate-or-download decision logic.
    Scan (tasks/scan.py::scan_directory + services/ingestion.py::run_scan):
      INFO "scan started" (batch_id, path, agent); INFO "scan progress" (batch_id, processed,
      total) on each chunk flush; INFO "scan completed" (batch_id, files, duration_s); ERROR
      "scan failed" (batch_id, error). DEBUG "file discovered" (path, size, ext) per file.
      Use time.monotonic() for duration_s. agent context = identity.agent_id (scan_directory
      can read ctx["agent_identity"].agent_id if present, else omit).
    Keep the D-13 secret-preview invariant; never log tokens. Stay Postgres-free in scan.py /
    model_bootstrap.py (no new DB imports).</action>
  <verify>`uv run pytest tests/test_tasks tests/test_scripts tests/test_services tests/test_task_split.py -q` green; `uv run mypy src/phaze/tasks/scan.py src/phaze/tasks/_shared/model_bootstrap.py src/phaze/scripts/download_models.py src/phaze/services/ingestion.py` clean; `uv run ruff check` clean on those files.</verify>
  <done>Scans and model downloads emit INFO start/progress/complete + DEBUG per-file; a
    running scan is observable from INFO logs alone.</done>
  <commit>feat(logging): operational INFO/DEBUG for model bootstrap + scan</commit>
</task>

<task type="auto" n="8">
  <name>Task 8: Operational logging — fingerprint/metadata/execution/discogs/tracklist + heartbeat/router</name>
  <files>src/phaze/tasks/fingerprint.py, metadata_extraction.py, execution.py, discogs.py, tracklist.py, heartbeat.py, src/phaze/services/agent_task_router.py</files>
  <action>Add INFO start + finish events (event name + key counts/ids) and DEBUG detail to each
    task function: fingerprint_file, extract_file_metadata, execute_approved_batch,
    match_tracklist_to_discogs, search_tracklist/scrape_and_store_tracklist. Pattern:
    INFO "<task> started" (file_id/batch_id/...); INFO "<task> completed" (key result counts/ids);
    DEBUG intermediate detail. Keep heartbeat_tick at DEBUG only (high frequency — do NOT spam
    INFO). In agent_task_router (and any real job-enqueue path), emit INFO "task enqueued"
    (queue, function, file_id/batch_id) on enqueue of real jobs. Behavior unchanged; logging
    only.</action>
  <verify>`uv run pytest tests/test_tasks tests/test_services tests/test_task_split.py -q` green; `uv run mypy src/phaze/tasks src/phaze/services/agent_task_router.py` clean; `uv run ruff check` clean on those files.</verify>
  <done>Every major task type logs INFO start/finish with ids; heartbeat stays DEBUG; real
    enqueues log INFO.</done>
  <commit>feat(logging): operational INFO/DEBUG for fingerprint/metadata/execution/discogs/tracklist + enqueue</commit>
</task>

<task type="auto" n="9">
  <name>Task 9: Operational-emission tests (levels + foreign-log flow)</name>
  <files>tests/test_logging_operational.py</files>
  <action>Add a focused test module that asserts key operations emit at the expected LEVELS
    (not exact strings). Use structlog.testing.capture_logs as a context manager to capture
    event dicts, OR caplog with the configured pipeline. Cover at minimum: scan_directory
    emits a "scan started" and "scan completed" event at INFO and a per-file event at DEBUG;
    ensure_models_present emits "validating model weights" at INFO; heartbeat_tick emits at
    DEBUG (NOT INFO). Add one test that a foreign stdlib logger record flows through the
    configured root pipeline after configure_logging(). Reuse the reset fixture from Task 2
    (extract it to tests/conftest.py if cleaner). Keep total project coverage >=85%.</action>
  <verify>`uv run pytest tests/test_logging_operational.py -x -q` green; `uv run pytest --cov --cov-report=term-missing -q` reports >=85% total.</verify>
  <done>Operational events are asserted at correct levels; coverage gate holds.</done>
  <commit>test(logging): assert operational events emit at expected levels</commit>
</task>

<task type="auto" n="10">
  <name>Task 10: Docs — env vars, structlog setup, verbose DEBUG</name>
  <files>README.md, docs/configuration.md, docs/architecture.md, .env.example, .env.example.agent, src/phaze/agent_watcher/README.md</files>
  <action>Document the new observability:
    - docs/configuration.md: add a "Logging / observability (all roles)" section with a table
      for PHAZE_LOG_LEVEL (default INFO; DEBUG|INFO|WARNING|ERROR) and PHAZE_LOG_JSON (default
      auto = JSON when stdout is not a TTY; true|false). Match the existing table format.
    - docs/architecture.md: short subsection describing the central structlog ProcessorFormatter
      bridge (one pipeline for native + foreign/uvicorn/SAQ logs), where configure_logging is
      wired (all entry points), and how to get verbose output (PHAZE_LOG_LEVEL=DEBUG).
    - README.md: one line in the relevant config/usage area pointing at the two env vars + how
      to enable DEBUG. Keep README badges on one line; do not re-add removed badges.
    - .env.example and .env.example.agent: add PHAZE_LOG_LEVEL / PHAZE_LOG_JSON with commented
      defaults, in the style of the surrounding entries.
    - agent_watcher/README.md: note the watcher now logs through the central structlog config
      (replaces the old ad-hoc stdout handler) and respects PHAZE_LOG_LEVEL/PHAZE_LOG_JSON.</action>
  <verify>`pre-commit run --all-files` passes (yamllint/markdown/actionlint clean); grep confirms PHAZE_LOG_LEVEL + PHAZE_LOG_JSON appear in docs/configuration.md, .env.example, and .env.example.agent.</verify>
  <done>Operators can discover and use PHAZE_LOG_LEVEL/PHAZE_LOG_JSON from docs + env examples;
    architecture doc explains the pipeline.</done>
  <commit>docs: document structlog logging config + PHAZE_LOG_LEVEL/PHAZE_LOG_JSON</commit>
</task>

</tasks>

<verification>
Whole-task gate (run before opening the PR):
  - `grep -rc "logging.getLogger" src/phaze` => 0 (all 34 call sites migrated).
  - `uv run pytest -q` => all green, including tests/test_task_split.py (import-boundary
    subprocess gates) and the new logging tests.
  - `uv run pytest --cov --cov-report=term-missing` => total >= 85%.
  - `uv run mypy .` clean; `uv run ruff check .` + `uv run ruff format --check .` clean.
  - `pre-commit run --all-files` passes (frozen hooks; NEVER --no-verify).
  - Manual smoke: `PHAZE_LOG_JSON=true PHAZE_LOG_LEVEL=DEBUG uv run python -c "from phaze.logging_config import configure_logging; import structlog; configure_logging(); structlog.get_logger('smoke').info('hello', k=1)"`
    prints one JSON line with event/level/timestamp; switch PHAZE_LOG_JSON=false for console.
  - Import-boundary smoke (agent role, no DB reachable):
    `PHAZE_ROLE=agent PHAZE_AGENT_API_URL=http://x PHAZE_AGENT_TOKEN=phaze_agent_t PHAZE_AGENT_QUEUE=phaze-agent-x PHAZE_AGENT_SCAN_ROOTS=/tmp PHAZE_REDIS_URL=redis://localhost:6379/0 uv run python -c "import phaze.tasks.agent_worker, phaze.agent_watcher; import sys; assert 'sqlalchemy.ext.asyncio' not in sys.modules and 'phaze.database' not in sys.modules"`
</verification>

<success_criteria>
- structlog is a declared dependency; uv.lock updated.
- A single configure_logging() renders BOTH structlog-native and foreign stdlib/uvicorn/SAQ
  logs through one pipeline; JSON when not a TTY, console otherwise; respects PHAZE_LOG_LEVEL;
  idempotent.
- configure_logging() is called in all six process entry points.
- All 34 `logging.getLogger` call sites use structlog.get_logger; legacy %s calls still format.
- Downloads, scans, fingerprints, and every major operation emit INFO that proves work is
  happening; DEBUG adds per-file/intermediate detail; heartbeat stays at DEBUG.
- The Postgres-free import boundary (test_task_split.py) stays green.
- Coverage >= 85%; mypy/ruff/pre-commit all clean.
- Docs + .env examples document PHAZE_LOG_LEVEL / PHAZE_LOG_JSON and how to enable DEBUG.
</success_criteria>

<risks>
1. **Postgres-free import boundary (HIGHEST).** logging_config.py must import only stdlib +
   structlog. agent_worker/watcher/_shared/model_bootstrap/cert_bootstrap import it transitively;
   any phaze.database/sqlalchemy leak turns test_task_split.py red and breaks the agent's
   no-Postgres guarantee. cert_bootstrap additionally must NOT import phaze.config. Tasks 3/4/6
   re-run those subprocess gates explicitly.
2. **SAQ multi-process logging.** Each SAQ worker (agent_worker, controller) is its OWN OS
   process and does NOT inherit the api's logging config — configure_logging MUST run inside
   each worker's `startup` hook (Tasks 3). Without it, worker logs fall back to stdlib defaults
   (the current silent-scan problem persists in workers).
3. **Legacy %s interpolation.** structlog.get_logger does NOT %-format positional args unless
   PositionalArgumentsFormatter is in the shared chain. It is included by design — if dropped,
   dozens of existing `logger.info("text %s", x)` calls render the literal "%s". Mechanical
   swap correctness depends on this processor.
4. **Watcher chicken-and-egg.** agent_watcher logs a pydantic ValidationError BEFORE settings
   exist, so it must call configure_logging() bare (env-driven), not via cfg. Reading cfg there
   would crash on the very misconfig it is trying to report (Gap-5/Gap-7).
5. **Secret-preview invariant (D-13).** Token previews are first-12-chars + "..."; the CLI mints
   tokens via print() only. Operational enrichment (Tasks 7/8) must not bind full tokens/secrets
   into event context.
6. **Coverage gate.** New logging_config.py + operational branches add lines; Task 9 must cover
   enough to keep total >= 85% (codecov patch target 80%).
</risks>
