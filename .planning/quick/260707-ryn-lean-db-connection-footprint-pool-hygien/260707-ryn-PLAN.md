---
phase: quick-260707-ryn
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/phaze/config.py
  - src/phaze/database.py
  - src/phaze/tasks/controller.py
  - src/phaze/services/agent_task_router.py
  - tests/shared/core/test_config_worker.py
  - tests/shared/core/test_database.py
  - tests/shared/tasks/test_controller_startup_banner.py
  - tests/agents/services/test_agent_task_router.py
  - tests/agents/tasks/test_agent_worker_lanes.py
autonomous: true
requirements: [POOL-HYGIENE]
must_haves:
  truths:
    - "The api engine (phaze.database.engine) is built with pool_size=5, max_overflow=5, pool_timeout=10, pool_recycle=1800, pool_pre_ping=True — all sourced from config."
    - "The control worker task_engine is built with the same five pool kwargs (pool_size=5, max_overflow=5, pool_timeout=10, pool_recycle=1800, pool_pre_ping=True) sourced from config."
    - "The per-(agent,lane) dispatch queues built by AgentTaskRouter._queue_for use min_size=0/max_size=2 sourced from config."
    - "The agent worker's own lane queue is unchanged: min_size=1/max_size=4 (MUST stay 1/4)."
    - "All seven pool/dispatch knobs are PHAZE_-prefixed, env-configurable, and live on a shared base settings class reachable by both the api engine and the controller."
  artifacts:
    - path: "src/phaze/config.py"
      provides: "Seven env-configurable pool/dispatch knobs on BaseSettings"
      contains: "db_pool_size"
    - path: "src/phaze/database.py"
      provides: "api engine with pool hygiene sourced from settings"
      contains: "pool_pre_ping"
    - path: "src/phaze/tasks/controller.py"
      provides: "control worker task_engine with pool hygiene sourced from cfg"
      contains: "pool_recycle"
    - path: "src/phaze/services/agent_task_router.py"
      provides: "dispatch queue sizing sourced from config"
      contains: "dispatch_queue_min_size"
  key_links:
    - from: "src/phaze/database.py"
      to: "phaze.config.settings"
      via: "create_async_engine kwargs read from settings.db_*"
      pattern: "settings\\.db_pool_size"
    - from: "src/phaze/services/agent_task_router.py"
      to: "phaze.config.get_settings"
      via: "min_size/max_size read from config dispatch knobs"
      pattern: "dispatch_queue_(min|max)_size"
status: complete
---

<objective>
Lean phaze's PgBouncer server-connection footprint so the shared (phaze,phaze) session-mode
pool (cap ~55) stops deadlocking under normal multi-worker load (which hangs /health behind the
exhausted pool). In session mode every app→pooler connection pins one server connection for its
whole lifetime, so the fix is to reduce how many server connections phaze holds open AND to add
liveness hygiene (pre_ping / recycle / bounded acquire timeout) so stale/idle connections free
their server slot instead of pinning it.

Homelab is raising the pooler cap to ~80 in parallel; these app-side reductions give HEADROOM,
not a hard fit. Every value is sourced from a PHAZE_-prefixed config knob so an operator can
re-tune without a code change.

Purpose: Stop the session-pool exhaustion deadlock; keep the numbers operator-tunable.
Output: Reduced + hygienic SQLAlchemy engine pools (api + control worker), reduced per-lane
dispatch-queue psycopg3 pools, and seven env knobs on the shared BaseSettings class.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/STATE.md
@./CLAUDE.md

<interfaces>
<!-- Sourced from the codebase. Use these directly — no exploration needed. -->

phaze/config.py — settings layout:
- `class BaseSettings(PydanticBaseSettings)` holds fields BOTH roles read (worker_max_jobs,
  scan_stall_seconds, lane_*_concurrency, etc.). This is the SHARED BASE — put the new knobs here.
- `class ControlSettings(BaseSettings)` and `class AgentSettings(BaseSettings)` inherit it.
- `settings: ControlSettings = _build_default_settings()` is the module-level singleton
  (`from phaze.config import settings`). database.py reads THIS.
- `get_settings()` returns the role instance (ControlSettings when PHAZE_ROLE=control).
  controller.py and agent_task_router construction read THIS.
- Existing knob pattern to MIRROR exactly (Field + validation_alias=AliasChoices):
    scan_stall_seconds: int = Field(default=86400,
        validation_alias=AliasChoices("PHAZE_SCAN_STALL_SECONDS", "SCAN_STALL_SECONDS", "scan_stall_seconds"),
        description="...")

phaze/database.py (module-level, built at import):
    engine = create_async_engine(str(settings.database_url), echo=settings.debug, pool_size=5, max_overflow=10)

phaze/tasks/controller.py (inside async startup(ctx), cfg = get_settings()):
    task_engine = create_async_engine(str(cfg.database_url), echo=cfg.debug, pool_size=10, max_overflow=5)

phaze/services/agent_task_router.py `_queue_for` (~line 135) builds each dispatch queue:
    queue = build_pipeline_queue(queue_name, self._queue_url, cache_redis_url=self._cache_redis_url,
        min_size=1, max_size=4, ledger_sessionmaker=self._ledger_sessionmaker)
  build_pipeline_queue signature: (name, url, *, cache_redis_url, min_size=1, max_size=4, ledger_sessionmaker=None) -> PostgresQueue
  The returned queue exposes `queue.pool.min_size` / `queue.pool.max_size` (psycopg3 AsyncConnectionPool).

phaze/tasks/agent_worker.py line 354 (the agent's OWN queue — MUST STAY 1/4, do not touch):
    queue = build_pipeline_queue(_queue_name, _settings_obj.queue_url, cache_redis_url=_settings_obj.redis_url, min_size=1, max_size=4)
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Add seven PHAZE_-prefixed pool/dispatch knobs to BaseSettings</name>
  <files>src/phaze/config.py, tests/shared/core/test_config_worker.py</files>
  <behavior>
    - `Settings()` (== ControlSettings) exposes defaults: db_pool_size=5, db_max_overflow=5,
      db_pool_timeout=10, db_pool_recycle=1800, db_pool_pre_ping=True, dispatch_queue_min_size=0,
      dispatch_queue_max_size=2.
    - Each knob reads its documented PHAZE_ env alias (monkeypatch.setenv then construct Settings()
      returns the overridden value; e.g. PHAZE_DB_POOL_SIZE=9 -> db_pool_size==9,
      PHAZE_DISPATCH_QUEUE_MAX_SIZE=6 -> dispatch_queue_max_size==6, PHAZE_DB_POOL_PRE_PING=false ->
      db_pool_pre_ping is False).
    - Because they live on BaseSettings, AgentSettings inherits them too (no separate assertion
      required, but the dispatch knobs must be reachable from get_settings() under either role).
  </behavior>
  <action>
    Add seven fields to `BaseSettings` (the shared base — NOT ControlSettings/AgentSettings, so
    both the api engine via the module-level `settings` and the controller via `get_settings()`
    reach them). Place them in a clearly-commented block after the existing worker/lane knobs.
    Mirror the existing `scan_stall_seconds` Field+AliasChoices pattern exactly (validation_alias
    with the PHAZE_ form, the bare field name, and — matching neighbors — no third UPPER alias is
    required; two choices `PHAZE_<NAME>` + `<name>` is sufficient and consistent with the lane knobs).

    Fields and defaults (types matter for mypy strict):
      - db_pool_size: int = 5            (PHAZE_DB_POOL_SIZE)
      - db_max_overflow: int = 5         (PHAZE_DB_MAX_OVERFLOW)
      - db_pool_timeout: int = 10        (PHAZE_DB_POOL_TIMEOUT)
      - db_pool_recycle: int = 1800      (PHAZE_DB_POOL_RECYCLE)
      - db_pool_pre_ping: bool = True    (PHAZE_DB_POOL_PRE_PING)
      - dispatch_queue_min_size: int = 0 (PHAZE_DISPATCH_QUEUE_MIN_SIZE)
      - dispatch_queue_max_size: int = 2 (PHAZE_DISPATCH_QUEUE_MAX_SIZE)

    Comment the block referencing the incident: PgBouncer SESSION mode pins one server connection
    per client connection for its whole lifetime; the shared (phaze,phaze) pool (cap ~55) deadlocks
    under normal multi-worker load and /health hangs behind the exhausted pool. These reduced +
    hygienic defaults cut phaze's server-connection footprint; homelab raises the pooler cap to
    ~80 in parallel, so this is HEADROOM, not a hard fit. Note db_pool_pre_ping validates a
    connection before checkout (drops dead server conns) and db_pool_recycle=1800 recycles a conn
    after 30 min so idle server slots are freed rather than pinned indefinitely.

    Add tests to test_config_worker.py mirroring the existing `test_lane_concurrency_defaults` /
    `test_lane_knobs_read_env_aliases` structure: one defaults test asserting all seven defaults,
    one env-alias test using monkeypatch.setenv for each PHAZE_ alias (include the bool
    "false"->False case for PHAZE_DB_POOL_PRE_PING).
  </action>
  <verify>
    <automated>uv run pytest tests/shared/core/test_config_worker.py -x -q</automated>
  </verify>
  <done>Seven knobs on BaseSettings with PHAZE_ aliases + the given defaults; new config tests pass.</done>
</task>

<task type="auto" tdd="true">
  <name>Task 2: Wire pool hygiene into the api engine + control worker task_engine</name>
  <files>src/phaze/database.py, src/phaze/tasks/controller.py, tests/shared/core/test_database.py, tests/shared/tasks/test_controller_startup_banner.py</files>
  <behavior>
    - phaze.database.engine (module-level) is created with pool_size=settings.db_pool_size (5),
      max_overflow=settings.db_max_overflow (5), pool_timeout=settings.db_pool_timeout (10),
      pool_recycle=settings.db_pool_recycle (1800), pool_pre_ping=settings.db_pool_pre_ping (True).
      Assert on the live engine's pool: engine.pool.size()==5, engine.pool._max_overflow==5,
      engine.pool._timeout==10, engine.pool._recycle==1800, engine.pool._pre_ping is True.
    - controller.startup builds task_engine via create_async_engine with the SAME five kwargs read
      from cfg (pool_size=5, max_overflow=5, pool_timeout=10, pool_recycle=1800, pool_pre_ping=True).
      Assert by capturing the create_async_engine call kwargs (monkeypatch pattern already used in
      test_controller_startup_banner.py).
  </behavior>
  <action>
    database.py: change the module-level `create_async_engine(...)` call to source ALL pool kwargs
    from the imported `settings` singleton — pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow (was hardcoded 10 -> now the config default 5),
    pool_timeout=settings.db_pool_timeout, pool_recycle=settings.db_pool_recycle,
    pool_pre_ping=settings.db_pool_pre_ping. Keep `from phaze.config import settings` exactly as-is
    (the module-level singleton is ControlSettings, which inherits the BaseSettings knobs) — do NOT
    switch this module to get_settings(); match the existing import pattern. Add a comment tying the
    three new hygiene kwargs to the PgBouncer session-mode 55-cap exhaustion incident and the
    parallel homelab cap raise to ~80 (headroom, not a hard fit).

    controller.py: in `startup(ctx)`, change the `task_engine = create_async_engine(...)` call
    (currently pool_size=10, max_overflow=5) to source all five kwargs from `cfg` (get_settings()):
    pool_size=cfg.db_pool_size (10 -> 5), max_overflow=cfg.db_max_overflow (5), plus the three
    hygiene kwargs pool_timeout=cfg.db_pool_timeout, pool_recycle=cfg.db_pool_recycle,
    pool_pre_ping=cfg.db_pool_pre_ping. Same incident comment.

    Tests:
    - test_database.py: add a test importing `phaze.database as db` and asserting the five live
      pool attributes above. (SQLAlchemy 2.0 AsyncAdaptedQueuePool exposes size(), _max_overflow,
      _timeout, _recycle, _pre_ping — confirm the attribute names hold at implementation time and
      assert on them; do not reload the module.)
    - test_controller_startup_banner.py: add a test that mirrors the existing banner test's stubs
      (monkeypatch create_async_engine / async_sessionmaker / DiscogsographyClient /
      load_prompt_template / ProposalService / get_settings), but replace the create_async_engine
      stub with a capturing fake that records its kwargs, set concrete db_* ints/bool on fake_cfg
      (5/5/10/1800/True), await controller.startup(ctx), and assert the recorded kwargs equal those
      config values. NOTE: startup() also constructs redis + reads log_level/log_json — copy the
      fake_cfg field set from the existing banner test so startup runs to the engine-build point.
  </action>
  <verify>
    <automated>uv run pytest tests/shared/core/test_database.py tests/shared/tasks/test_controller_startup_banner.py -x -q</automated>
  </verify>
  <done>Both engines source all pool kwargs from config; api engine defaults are 5/5/10/1800/True; controller captured kwargs match config; tests pass.</done>
</task>

<task type="auto" tdd="true">
  <name>Task 3: Source dispatch-queue sizing from config; regression-guard the agent worker queue</name>
  <files>src/phaze/services/agent_task_router.py, tests/agents/services/test_agent_task_router.py, tests/agents/tasks/test_agent_worker_lanes.py</files>
  <behavior>
    - AgentTaskRouter._queue_for builds each per-(agent,lane) dispatch queue with
      min_size=dispatch_queue_min_size (0) and max_size=dispatch_queue_max_size (2), sourced from
      config. A constructed queue reports queue.pool.min_size==0 and queue.pool.max_size==2
      (construction is open=False — no live connection needed).
    - REGRESSION: the agent worker's OWN lane queue (agent_worker.py line 354) MUST STAY 1/4:
      queue.pool.min_size==1 and queue.pool.max_size==4 (unchanged by this plan).
  </behavior>
  <action>
    agent_task_router.py: add `from phaze.config import get_settings` (safe — config does not import
    agent_task_router, so no cycle). In `_queue_for`, read the two dispatch knobs from get_settings()
    and pass them to build_pipeline_queue as min_size / max_size in place of the hardcoded 1 / 4.
    (get_settings() is lru_cached — a dict lookup per call; reading inside _queue_for keeps it lazy
    and covers all callers: queue_for, all_lane_queues, legacy_base_queue.) Update the existing 1/4
    comment block: dispatch queues are control-side PRODUCER pools that only enqueue (no long-lived
    consumer), so min_size=0 keeps zero idle server connections pinned and max_size=2 caps the burst
    — part of the PgBouncer session-mode 55-cap footprint reduction (homelab raising the cap to ~80
    in parallel = headroom, not a hard fit). Keep the RESEARCH Pitfall 4 "under Postgres
    max_connections" note.

    Do NOT touch agent_worker.py (its own queue stays 1/4) and do NOT touch the controller_queue.

    Tests:
    - test_agent_task_router.py: add a NON-integration unit test (no @pytest.mark.integration — it
      only constructs a queue, open=False, no socket). Construct
      `AgentTaskRouter(queue_url="postgresql://u:p@h:5432/d", cache_redis_url="redis://c:6379/0")`,
      call `_queue_for("a", "meta")`, and assert queue.pool.min_size==0 and queue.pool.max_size==2.
      (Mirror the pool-attribute assertion style from tests/analyze/core/test_queue_factory.py.)
    - test_agent_worker_lanes.py: add a regression test that reloads the worker (use the existing
      `_reload_worker` helper, e.g. lane="analyze" or all-mode) and asserts
      `worker.queue.pool.min_size == 1` and `worker.queue.pool.max_size == 4` — proving the agent's
      own queue was NOT swept into the dispatch reduction.
  </action>
  <verify>
    <automated>uv run pytest tests/agents/services/test_agent_task_router.py tests/agents/tasks/test_agent_worker_lanes.py -x -q -m "not integration"</automated>
  </verify>
  <done>Dispatch queues build 0/2 from config; agent worker queue still 1/4; both tests pass.</done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| app → PgBouncer (session mode) | Every client conn pins one server conn for its lifetime; the shared 55-cap pool is the contended resource this change protects. |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-ryn-01 | Denial of Service | SQLAlchemy engine pools + dispatch queue pools | mitigate | Reduce pool_size/max_overflow + dispatch max_size, add pool_pre_ping (drop dead server conns) + pool_recycle=1800 (free idle server slots) + pool_timeout=10 (bounded acquire, fail fast instead of hanging /health). This is the core fix for the session-pool exhaustion deadlock. |
| T-ryn-02 | Tampering | config knobs (operator env) | accept | New knobs are ints/bool with sane defaults; an out-of-range operator value only mis-sizes this process's own pool (no cross-tenant impact, single-user app). No bounds validators added (matches the un-bounded worker_max_jobs / lane_*_concurrency neighbors). |
| T-ryn-SC | Tampering | dependency installs | accept | No new packages — pure config + call-site edits. Package Legitimacy Gate N/A. |
</threat_model>

<verification>
- `uv run ruff check .` and `uv run ruff format --check .` clean.
- `uv run mypy .` clean (strict; the new knobs are typed, get_settings() import adds no Any).
- `uv run pytest tests/shared/core/test_config_worker.py tests/shared/core/test_database.py tests/shared/tasks/test_controller_startup_banner.py tests/agents/services/test_agent_task_router.py tests/agents/tasks/test_agent_worker_lanes.py -q -m "not integration"` passes.
- Grep confirms sourcing (not hardcoding): `rg "settings\.db_pool_size|cfg\.db_pool_size|dispatch_queue_(min|max)_size" src/` returns the api engine, controller task_engine, and router lines.
- Grep confirms the agent worker was NOT changed: `rg -n "min_size=1, max_size=4" src/phaze/tasks/agent_worker.py` still matches.
- pre-commit: all hooks pass, no `--no-verify`.
</verification>

<success_criteria>
- api engine + control worker task_engine both build with pool_size=5, max_overflow=5,
  pool_timeout=10, pool_recycle=1800, pool_pre_ping=True — every value from config.
- Per-(agent,lane) dispatch queues build with min_size=0/max_size=2 from config.
- Agent worker's own queue and the controller_queue are unchanged (1/4 and 2/8 respectively).
- Seven PHAZE_-prefixed knobs on BaseSettings, env-overridable, reachable by both the api engine
  (module-level `settings`) and the controller (`get_settings()`).
- Full quality gate (ruff + mypy strict + targeted pytest + pre-commit) green.
</success_criteria>

<output>
Create `.planning/quick/260707-ryn-lean-db-connection-footprint-pool-hygien/260707-ryn-SUMMARY.md` when done.
</output>
