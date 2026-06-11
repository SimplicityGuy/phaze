<!-- GSD:RESEARCH phase=33 -->
# Phase 33: SAQ Monitoring UI (mounted in phaze-api) - Research

**Researched:** 2026-06-11
**Domain:** ASGI sub-app mounting (FastAPI/Starlette) + SAQ embedded web dashboard
**Confidence:** HIGH (every load-bearing claim verified against installed source `saq==0.26.4`, `starlette==1.2.1`, `fastapi==0.136.3`, plus an empirical mount-in-lifespan probe)

## Summary

The design is LOCKED and correct. All five research questions resolve cleanly, and the single load-bearing risk — whether `app.mount("/saq", ...)` *inside the lifespan startup* is actually served — is **VERIFIED true** both by reading installed Starlette source and by running an end-to-end `TestClient` probe (`/saq/` returned 200, `/health` unaffected). `saq_web` is a thin factory that reuses the exact `Queue` instances you pass it (it never opens its own pool), renders its dashboard shell with plain `str.format` over package-bundled static assets (no `jinja2`, no `aiohttp`, no `saq[web]` extra needed), and mounts cleanly under `/saq` with correctly-resolved asset URLs. The only genuinely sharp edge is that `saq_web` stores its queue registry in **module-level globals** (`saq.web.starlette.QUEUES`/`ROOT_PATH`) and **clears them on every call** — so it must be called exactly once per process, and tests must account for that shared state.

**Primary recommendation:** Add a tiny testable helper `phaze/web/saq_mount.py::build_saq_app(queues: list[Queue]) -> Starlette` that wraps `saq_web("/saq", queues)`. Mount it **inside the existing `lifespan` startup** (Approach 1) right after the controller queue + task_router are wired and after enumerating non-revoked agents from the DB — `app.mount("/saq", build_saq_app([controller_queue, *agent_queues]))`. Reuse `app.state.task_router.queue_for(agent_id)` for the agent queues so the same hook-applied instances are reused (no second pool). Guard the whole block behind a new `settings.enable_saq_ui` bool (default `True`). No new top-level dependency; no `saq[web]` extra.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Serve `/saq` dashboard HTML + static | API / Backend (FastAPI ASGI) | CDN/Static (bundled in saq pkg) | Mounted sub-app inside the existing API process; assets ship inside the `saq` wheel |
| Read queue/job state for dashboard | Database / Storage (Redis) | API | `saq_web` calls `Queue.info()` on the passed instances → Redis; no app logic |
| Decide which queues are visible | API (lifespan wiring) | Database (Postgres agent list) | Queue set is computed at startup from `app.state` + a non-revoked-agent query |
| Auth for `/saq` | Reverse proxy (out of process) | — | LOCKED: intentionally no app-layer auth |

## User Constraints (from CONTEXT.md)

### Locked Decisions
- **Mount mechanism:** `from saq.web.starlette import saq_web` → `app.mount("/saq", saq_web("/saq", queues=[...]))`. Mount target is `create_app()` in `src/phaze/main.py`. No standalone server, no new port, no auth middleware.
- **Queues to monitor:** the named **controller** queue (`app.state.controller_queue`, name `controller`) PLUS the per-agent queues (`AgentTaskRouter.queue_for(agent_id)`, name `phaze-agent-<id>`). Agent queues MUST be included — that is where `process_file` lives.
- **Reuse existing Queue instances — NO second pool.** Reuse the same `saq.Queue` instances created in the lifespan; do not construct a second connection pool for the dashboard.
- **Security posture:** intentionally unauthenticated at the app layer; only reachable behind the reverse proxy's internal-realm auth. The PR description MUST state this explicitly. No auth middleware added.
- **Dependencies:** SAQ is already a direct dependency. Confirm whether the starlette `saq_web` path renders without `aiohttp_jinja2` (it should) or whether a `saq[web]` extra is needed. If an extra is genuinely needed, add `saq[web]` pinned consistently — no NEW top-level package.
- **Dynamic agents (operator-acceptable):** enumerate non-revoked agents at **app startup**; agents registered after startup won't appear until the next `phaze-api` restart. Acceptable for single-user homelab. Document the limitation; do NOT build live hot-reload.

### Claude's Discretion
- Exact module placement of the mount helper (inline in `main.py` lifespan vs a small `phaze/web/saq_mount.py` helper). CONTEXT recommends a tiny helper for testability.
- Whether to guard the mount behind a settings flag (`enable_saq_ui`, default on). CONTEXT recommends a flag, default-enabled.
- How `saq_web`'s `root_path="/saq"` interacts with the reverse-proxy path prefix — verify links/assets resolve under `/saq`.

### Deferred Ideas (OUT OF SCOPE)
- App-layer auth for `/saq` (deferred to the reverse proxy).
- Live hot-reload of agent queues registered after `phaze-api` startup (restart picks them up).
- Standalone `saq --web` service / separate port.

## Phase Requirements

This phase predates a formal REQUIREMENTS.md mapping; the LOCKED CONTEXT decisions are the requirement set. Mapping for traceability:

| ID | Description | Research Support |
|----|-------------|------------------|
| SAQUI-01 | Mount SAQ dashboard at `/saq` in the existing API | Q1/Q3 — `app.mount` in lifespan VERIFIED served; root_path resolves correctly |
| SAQUI-02 | Monitor controller + per-agent queues with the existing instances (no second pool) | Q1 — `saq_web` reuses passed `Queue.info()` instances; never opens its own pool (`saq/web/starlette.py:100-101`) |
| SAQUI-03 | No new top-level dependency / no `saq[web]` extra | Q2 — render path uses `str.format` + bundled static; `saq[web]` = aiohttp path only |
| SAQUI-04 | No app-layer auth; `/health` + existing routers unaffected | Q3 — mounted sub-app's internal `/health` is namespaced under `/saq/health`; probe confirms app `/health` intact |
| SAQUI-05 | Toggle via `settings.enable_saq_ui` (default on) | Q5 — add bool to `BaseSettings` in `config.py`; lifespan reads `settings.enable_saq_ui` |

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| saq | 0.26.4 (already pinned `saq[redis]>=0.26.4`) | Embedded queue dashboard via `saq.web.starlette.saq_web` | Already the project task queue; ships an embeddable Starlette UI |
| starlette | 1.2.1 (transitive via FastAPI) | ASGI app `saq_web` returns + the `Mount`/router machinery | Already installed; FastAPI is built on it |
| fastapi | 0.136.3 (already direct) | Host app + `app.mount` | Existing app factory |

`[VERIFIED: installed dist]` versions read from `.venv/lib/python3.14/site-packages` and `uv run python` probe.

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| jinja2 | installed (app templates) | NOT used by `saq_web` | n/a — `saq_web` renders via `str.format`, not jinja |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Mount-in-lifespan (Approach 1) | Mount at `create_app()` with controller-only | Loses agent-queue coverage (agent list needs the DB, only up post-startup). Weaker — rejected per CONTEXT. |
| `saq_web` Starlette path | `saq.web.aiohttp` path | Requires `aiohttp` + `aiohttp_basicauth` (the `saq[web]` extra) and a second aiohttp server. Wrong tier — rejected. |
| `standalone saq --web` | — | Out of scope (new port/service). Rejected per CONTEXT. |

**Installation:** No change. `saq[redis]>=0.26.4` already in `pyproject.toml:13`. `saq[web]` is **NOT** required (see Q2).

**Version verification (run):**
```
uv run python -c "import saq,starlette,fastapi;print(saq.__version__,starlette.__version__,fastapi.__version__)"
# -> 0.26.4 1.2.1 0.136.3
```

## Package Legitimacy Audit

No new external packages are installed by this phase. The mount reuses already-vendored, already-pinned dependencies (`saq[redis]`, `starlette` via `fastapi`). slopcheck is therefore not applicable.

| Package | Registry | Age | Downloads | Source Repo | slopcheck | Disposition |
|---------|----------|-----|-----------|-------------|-----------|-------------|
| saq | PyPI | mature | high | github.com/tobymao/saq | n/a (already a dependency) | Already approved (`pyproject.toml:13`) |

**Packages removed due to slopcheck [SLOP] verdict:** none
**Packages flagged as suspicious [SUS]:** none

## Resolution of the Five Research Questions

### Q1 — [LOAD-BEARING] Queue-wiring timing → Approach 1 (mount-in-lifespan) is VERIFIED safe

**Decision: Approach 1.** Mount `/saq` *inside* the existing `lifespan` startup, after the controller queue + task_router are created and after enumerating non-revoked agents from the DB.

**Why it works (Starlette source, `starlette==1.2.1`):**
- `Starlette.__call__` builds the middleware stack lazily on the FIRST call, which is the ASGI **lifespan** scope: `if self.middleware_stack is None: self.middleware_stack = self.build_middleware_stack()` (`starlette/applications.py:86-90`).
- `build_middleware_stack()` wraps **`self.router` by object reference**, not a snapshot of routes: `app = self.router` (`starlette/applications.py:74`).
- The `Router` matches each request by iterating its **live** `self.routes` list (`starlette/routing.py:622`, also `:439`, `:501`); `self.routes` is a plain mutable `list` (`starlette/routing.py:578`).
- `app.mount(path, sub)` → `self.router.mount(...)` (`starlette/applications.py:92-93`) appends a `Mount` to that same live list.

So the sequence is: lifespan scope arrives → middleware stack is built wrapping the router *by reference* → our startup body runs (queues created, agents enumerated, `app.mount("/saq", ...)` appends to `router.routes`) → startup completes → the server begins serving HTTP → each HTTP request iterates the now-updated `router.routes` and finds `/saq`. The route is in place **before any HTTP request is served**.

**Empirical proof (ran in this session):** a FastAPI app that mounts a Starlette sub-app inside its lifespan, then `with TestClient(app)`:
```
/health -> 200 {'status': 'ok'}
/saq/   -> 200 'MOUNTED-IN-LIFESPAN-OK'
routes count after mount-in-lifespan: 6
```
`[VERIFIED: empirical TestClient probe + starlette source]`

**`saq_web` connection behavior — no second pool (VERIFIED):**
- `saq_web` only stores the passed instances and later calls `q.info()` on them: `for q in QUEUES.values(): await q.info()` (`saq/web/starlette.py:100-101`, `:67`, `:109`). It never constructs a `Queue`, never calls `connect()`, never builds a Redis client.
- `RedisQueue.info()` issues commands directly against `self.redis` (`saq/queue/redis.py:119-191`); `RedisQueue` wraps `aioredis.from_url(url)` whose connection pool connects lazily on first command (`saq/queue/redis.py:64-66`, `:79`). `Queue.connect()` is effectively a no-op for Redis (just records the loop, `saq/queue/base.py:213-214`).
- Therefore passing the **lifespan-created** instances (`app.state.controller_queue` and the cached `app.state.task_router.queue_for(agent_id)` queues) means the dashboard reads through the **same** pools. `[VERIFIED: saq source]`

**Building the agent-queue list at startup:** enumerate non-revoked agents with the existing query shape already used in `pipeline.py:186`:
```python
select(Agent).where(Agent.revoked_at.is_(None)).order_by(Agent.name)
```
For each agent id, call `app.state.task_router.queue_for(agent.id)` — this constructs+caches the `phaze-agent-<id>` `Queue` *with* the `apply_project_job_defaults` hook (`agent_task_router.py:79-101`) and returns the cached instance, so the dashboard and the enqueue path share one instance per agent. `revoked_at IS NULL` auto-excludes the permanently-revoked `legacy-application-server` (its `revoked_at == created_at`, per `enqueue_router.py:26-27`). `[VERIFIED: codebase]`

> Note: `enqueue_router.select_active_agent()` returns **one** agent (`LIMIT 1`, `enqueue_router.py:103-111`) — it is NOT the right helper here. Phase 33 needs **all** non-revoked agents; use the `pipeline.py:186` query shape (a tiny new query, or factor a `list_monitorable_agents(session)` helper). `[VERIFIED: codebase]`

### Q2 — Runtime deps: NO `aiohttp_jinja2`, NO `saq[web]`, NO new package

- `saq_web`'s page render is `Response(content=render(root_path=ROOT_PATH), ...)` (`saq/web/starlette.py:51-52`), and `render` is plain `str.format` over a hardcoded HTML `BODY` string with `html.escape` (`saq/web/common.py:8-28`). **No templating engine is imported or used.**
- Static assets ship inside the wheel: `saq/web/static/{app.js, pico.min.css.gz, snabbdom.js.gz}`, served by a `StaticFiles` sub-mount at `/static` (`saq/web/starlette.py:150`, `STATIC_PATH` = `saq/web/common.py:7`). `[VERIFIED: ls of installed pkg]`
- The full import chain of `saq.web.starlette` is: `os`, `starlette.*`, `saq.job`, `saq.queue`, `saq.types`, `saq.web.common` (`saq/web/starlette.py:7-21`). `saq.web.common` imports only `html`, `pathlib`, `saq.job` (`saq/web/common.py:1-5`). **Nothing pulls jinja2, aiohttp, or aiohttp_jinja2.**
- `saq[web]` extra = `aiohttp` + `aiohttp_basicauth` (`saq` METADATA `Provides-Extra: web`). Those belong to the **separate** `saq/web/aiohttp.py` path — irrelevant to the Starlette path. **Do not add `saq[web]`.**
- `starlette` is present transitively via `fastapi==0.136.3`. `jinja2` and `aiohttp` happen to be installed already (app templates / another dep) but are **not** on the `saq_web` import path. `[VERIFIED: uv run import probe]`

**Conclusion:** zero dependency changes. The existing `saq[redis]>=0.26.4` pin is sufficient at runtime.

### Q3 — Mount mechanics + root_path: correct, not doubled; `/health` and routers unaffected

- `app.mount("/saq", saq_web("/saq", ...))`: the outer mount strips the `/saq` prefix before delegating to the inner Starlette app, whose own routes are prefix-free (`/`, `/api/queues`, `/static`, `/health` — confirmed list from probe). The `root_path="/saq"` argument is used **only** to build absolute asset/link URLs in the rendered HTML (`{root_path}/static/...`, `saq/web/common.py:14,19,21`).
- Flow for `/saq/`: outer mount strips `/saq` → `/` → `views()` renders HTML whose links are `/saq/static/pico.min.css` → browser requests `/saq/static/pico.min.css` → outer mount strips `/saq` → `/static/pico.min.css` → `GZStaticFiles`. **Prefix is applied exactly once; not doubled.** The two `/saq` values (mount path and `root_path` arg) MUST be equal — they are. `[VERIFIED: source + render probe output showing `/saq/static/pico.min.css`]`
- **Reverse-proxy prefix:** if the proxy serves the dashboard at the same external path it forwards (`https://host/saq/...` → app `/saq/...`), no change needed. If the proxy ever exposes it under an *additional* external prefix (e.g. `/phaze/saq`), then `root_path` must equal the **externally-visible** prefix while the `app.mount` path stays `/saq`. For the homelab single-host case, `/saq` direct is correct. Document this coupling. `[VERIFIED: source reasoning]`
- **No collision with the app's `/health`:** the sub-app's internal `/health` lives at `/saq/health`, a different path from the FastAPI `health.router` at `/health` (`main.py:118`). Empirical probe confirmed `/health` still returns `200 {"status":"ok"}` after the mount. The mount only **appends** to `router.routes`, so every existing router (`main.py:118-155`) is untouched. `[VERIFIED: probe]`
- **OpenAPI:** a `Mount` is excluded from FastAPI's OpenAPI schema, so `/saq` will not appear in `/docs`. Harmless and expected.

### Q4 — Testing approach: testable `build_saq_app(queues)` helper + a lifespan integration test

The default `client` fixture (`tests/conftest.py:155-161`) constructs the app with `AsyncClient(transport=ASGITransport(app=app))` and **never enters the lifespan**, so `app.state.controller_queue`/`task_router` are absent AND the `/saq` mount (done in lifespan) is not present. This is the same limitation the Phase 30/34 work hit — those tests inject fakes via `tests/_queue_fakes.py::install_fake_queues(client)` / `wire_fakes(client)`. So Phase 33 needs two layers of test:

1. **Unit-test the helper directly** (no lifespan, no DB, no Redis). `build_saq_app([FakeQueue("controller"), FakeQueue("phaze-agent-nox")])`:
   - Assert it returns a `starlette.applications.Starlette` whose `routes` include `/`, `/api/queues`, `/static`, etc.
   - Mount it on a throwaway `FastAPI()` and hit `/saq/api/queues` with `TestClient`; assert `200` and that the JSON lists both queue names. A `FakeQueue` needs an `async def info(self, jobs=False, ...)` returning a minimal `QueueInfo`-shaped dict — extend the existing `tests/_queue_fakes.py::FakeQueue` (it currently has `enqueue`/`job` but not `info`).
   - Assert `/saq/` returns `200` and the body contains `/saq/static/` (root_path correctly baked).
   - **Gotcha to encode in the test:** `saq_web` mutates module-level `saq.web.starlette.QUEUES`/`ROOT_PATH` and **clears** them on each call (`saq/web/starlette.py:135`). Verified: a second `saq_web(...)` call dropped the first call's queues. Tests that build multiple apps must not assert on the global across builds, and the production code must call it exactly once. Consider a test that explicitly documents this clobber behavior so a future "mount twice" regression is caught.

2. **Lifespan integration test** — mirror `tests/test_main_lifespan.py` exactly (it already monkeypatches `Queue`, `AgentTaskRouter`, `redis_async`, `engine`, `run_migrations`, `ensure_dev_agent` and drives startup via `with TestClient(app):`, `test_main_lifespan.py:100-114`). Extend that pattern to:
   - Assert that after startup, `app.router.routes` contains a `Mount` with `path == "/saq"` (the route was added during lifespan).
   - With `settings.enable_saq_ui = True`, `GET /saq/` (or `/saq/health`) returns `200`; existing `GET /health` still `200`.
   - With `settings.enable_saq_ui = False` (monkeypatched), assert no `/saq` mount is present and `/saq/` returns `404`.
   - Assert the queue instances handed to the mount are the **same objects** as `app.state.controller_queue` and `app.state.task_router.queue_for(...)` (no second pool) — read `saq.web.starlette.QUEUES` values and `is`-compare, or have `build_saq_app` return the list it registered for direct assertion.

**Analogs to cite in the plan:** `tests/test_health.py:5-10` (route-200 shape), `tests/test_main_lifespan.py:100-124` (lifespan-driven `TestClient` + constructor monkeypatching), `tests/_queue_fakes.py:56-127` (FakeQueue / app.state injection).

**Wave-0 harness need:** add an `async def info(...)` method to `FakeQueue` in `tests/_queue_fakes.py` returning a minimal `QueueInfo`-shaped mapping (`{"name", "queued", "active", "scheduled", "jobs", "workers", ...}` — read the real shape from `saq/queue/redis.py:119` `info()`), so the helper unit test can exercise `/saq/api/queues` without Redis. No new test framework; pytest + httpx already in place.

### Q5 — Config flag: add `enable_saq_ui` to `BaseSettings`

Add to `phaze/config.py` `BaseSettings` (so both roles parse it; only the API process acts on it):
```python
enable_saq_ui: bool = Field(
    default=True,
    validation_alias=AliasChoices("PHAZE_ENABLE_SAQ_UI", "enable_saq_ui"),
    description="Mount the SAQ monitoring dashboard at /saq in the API (Phase 33).",
)
```
`main.py` already imports the module-level singleton `from phaze.config import settings` (`main.py:13`) and reads `settings.*` throughout the lifespan (e.g. `settings.redis_url`, `main.py:98`). The lifespan body simply wraps the mount block in `if settings.enable_saq_ui:`. Place the field near the other API/lifespan toggles (`auto_migrate`, `api_tls_sans`, `config.py:242-256`) for cohesion. `[VERIFIED: config.py + main.py]`

## Architecture Patterns

### System Architecture Diagram

```
                         phaze-api (single FastAPI ASGI process)
   browser /saq/* ──▶ reverse proxy (TLS + internal-realm auth) ──▶ app.__call__
                                                                       │
                          ┌────────────────────────────────────────────┤ (router.routes, live list)
                          │                                            │
                   existing routers                          Mount("/saq", saq_web app)
                   (/health, /pipeline, …)                          │ strips "/saq"
                          │                                  ┌───────┴────────────┐
                          ▼                                  ▼                    ▼
                     (unchanged)                       Route /, /api/queues   Mount /static
                                                            │                 (bundled assets)
                                                            ▼
                                              q.info() on PASSED instances
                                                  │            │
                                     app.state.controller_queue  task_router.queue_for(<id>)
                                                  └──────┬───────┘
                                                         ▼
                                                  same Redis pools
                                              (NO second connection pool)

   Wiring happens during lifespan startup:
   create queues ─▶ SELECT agents WHERE revoked_at IS NULL ─▶ queue_for(each) ─▶ app.mount("/saq", build_saq_app([...]))
```

### Recommended Project Structure
```
src/phaze/
├── web/
│   └── saq_mount.py     # build_saq_app(queues) -> Starlette  (new, tiny, testable)
├── main.py              # lifespan: if settings.enable_saq_ui: app.mount("/saq", build_saq_app([...]))
└── config.py            # + enable_saq_ui flag
```

### Pattern 1: Testable mount helper
**What:** isolate the one `saq_web` call behind a pure function so it can be unit-tested without booting the app.
**When to use:** always — keeps the lifespan body thin and gives a seam for the FakeQueue test.
```python
# src/phaze/web/saq_mount.py
from saq import Queue
from saq.web.starlette import saq_web
from starlette.applications import Starlette

def build_saq_app(queues: list[Queue]) -> Starlette:
    """Return the embeddable SAQ dashboard for `queues`, rooted at /saq.

    WARNING: saq_web stores its queue registry in module-level globals and
    CLEARS them on each call (saq/web/starlette.py:135). Call exactly once per
    process. The returned app reads queue state via the PASSED instances'
    .info() — it never opens a second Redis pool.
    """
    return saq_web("/saq", queues=queues)
```

### Pattern 2: Mount inside lifespan after queues + agents exist
```python
# inside main.py lifespan, after controller_queue/task_router/redis are wired:
if settings.enable_saq_ui:
    async with async_session() as s:
        agents = (await s.execute(
            select(Agent).where(Agent.revoked_at.is_(None)).order_by(Agent.name)
        )).scalars().all()
    agent_queues = [_app.state.task_router.queue_for(a.id) for a in agents]
    _app.mount("/saq", build_saq_app([_app.state.controller_queue, *agent_queues]))
```

### Anti-Patterns to Avoid
- **Calling `saq_web`/`build_saq_app` more than once per process:** the second call clears the first call's `QUEUES` global — the earlier mount silently stops resolving queues. Mount once.
- **Constructing fresh `Queue.from_url(...)` for the dashboard:** opens a second pool, violates the LOCKED no-second-pool decision. Always reuse `app.state.controller_queue` and `task_router.queue_for(id)`.
- **Mounting at `create_app()`:** `app.state` queues and the agent list don't exist yet → either crashes or loses agent-queue coverage. Mount in lifespan.
- **Adding `saq[web]` or `aiohttp_jinja2`:** not on the Starlette render path; pure bloat.
- **Adding any auth middleware to `/saq`:** explicitly LOCKED out — proxy owns auth.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Queue/job dashboard | Custom HTMX page hitting `Queue.info()` | `saq.web.starlette.saq_web` | Ships UI + JSON API + retry/abort actions, already vendored |
| Static asset serving for the dashboard | Your own StaticFiles mount | `saq_web`'s bundled `/static` | Assets ship in the wheel, gzip-aware (`GZStaticFiles`) |
| Reusing Redis connections | New pool per dashboard | Pass lifespan `Queue` instances | `saq_web` reads via the passed instances; one pool total |

**Key insight:** `saq_web` is already exactly "send the queue instances, get a mountable dashboard." The only engineering is *timing the mount* and *assembling the queue list* — both pure-Python, both covered above.

## Common Pitfalls

### Pitfall 1: Module-level global registry gets clobbered
**What goes wrong:** `saq.web.starlette.QUEUES`/`ROOT_PATH` are module globals; `saq_web` calls `QUEUES.clear()` then repopulates (`saq/web/starlette.py:23-24,135-138`). A second `saq_web` call (e.g. a re-mount, or two tests in one process) wipes the previous registration.
**Why it happens:** SAQ designed `saq_web` for a single embed per process.
**How to avoid:** call it exactly once in production. In tests, treat the global as process-shared state; don't assert across builds; the lifespan test should assert the *final* registration only.
**Warning signs:** `/saq/api/queues` returns fewer queues than expected, or an empty list after a code path mounts twice.

### Pitfall 2: Tests skip the lifespan, so `/saq` isn't mounted
**What goes wrong:** the default `client` fixture never enters the lifespan (`conftest.py:155-161`), so `/saq` is absent and a naive `await client.get("/saq/")` 404s.
**Why it happens:** ASGITransport doesn't run lifespan; app.state queues are injected manually by `_queue_fakes`.
**How to avoid:** unit-test `build_saq_app` directly; integration-test the mount via `with TestClient(app):` + constructor monkeypatching (the `test_main_lifespan.py` pattern).
**Warning signs:** a `/saq` test that only passes when run with a live Redis/DB.

### Pitfall 3: root_path / mount-path mismatch
**What goes wrong:** if `app.mount("/saq", saq_web("/foo", ...))` (paths differ), rendered asset URLs point at `/foo/static/...` which the `/saq` mount won't serve → broken CSS/JS.
**Why it happens:** `root_path` only feeds the HTML template; it must equal the externally-visible mount prefix.
**How to avoid:** keep both literals equal (`/saq`). Behind an extra proxy prefix, set `root_path` to the external prefix.
**Warning signs:** dashboard loads as unstyled HTML; 404s on `/saq/static/*`.

## Runtime State Inventory

Not a rename/refactor/migration phase — this is additive (a new mount + one config flag). No stored data, live service config, OS-registered state, secrets, or build artifacts are renamed or migrated. **None — verified: the change only appends a route during startup and reads existing Redis queues.**

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| saq (Starlette web) | the mount | ✓ | 0.26.4 | — |
| starlette | mount machinery | ✓ (via fastapi) | 1.2.1 | — |
| Redis | `Queue.info()` at request time | runtime (homelab) | 7+ | dashboard shows errors if Redis down; no fallback needed for tests (FakeQueue.info) |

**Missing dependencies with no fallback:** none.
**Missing dependencies with fallback:** none — all build/test paths use fakes; live Redis only needed at runtime.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio + httpx (already configured) |
| Config file | `pyproject.toml` (`[tool.pytest...]`) |
| Quick run command | `uv run pytest tests/test_web/test_saq_mount.py -x` |
| Full suite command | `uv run pytest` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| SAQUI-01 | `build_saq_app(queues)` returns a Starlette app with the dashboard routes | unit | `uv run pytest tests/test_web/test_saq_mount.py::test_build_saq_app_routes -x` | ❌ Wave 0 |
| SAQUI-01 | mounted `/saq/` returns 200 when wired | unit (throwaway FastAPI + TestClient) | `...::test_saq_root_served -x` | ❌ Wave 0 |
| SAQUI-02 | `/saq/api/queues` lists controller + agent queues via passed instances (no new pool) | unit (FakeQueue.info) | `...::test_api_queues_lists_passed_instances -x` | ❌ Wave 0 |
| SAQUI-02 | registered queues are the SAME objects as `app.state.controller_queue` / `queue_for(id)` | integration (lifespan) | `uv run pytest tests/test_main_lifespan.py::test_saq_mount_reuses_instances -x` | ❌ Wave 0 (extend existing file) |
| SAQUI-04 | app `/health` + existing routers unaffected after mount | integration (lifespan) | `...::test_health_intact_with_saq_mount -x` | ❌ Wave 0 |
| SAQUI-05 | `enable_saq_ui=False` → no `/saq` mount (404); default True → mounted | integration (lifespan, monkeypatch settings) | `...::test_enable_saq_ui_toggles_mount -x` | ❌ Wave 0 |
| (gotcha) | second `saq_web` call clobbers globals (regression guard) | unit | `...::test_saq_web_single_call_contract -x` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_web/test_saq_mount.py -x` (quick).
- **Per wave merge:** `uv run pytest tests/test_web tests/test_main_lifespan.py tests/test_health.py`.
- **Phase gate:** full `uv run pytest` green + `uv run ruff check .` + `uv run mypy .` + `pre-commit run --all-files` before `/gsd:verify-work`. Coverage ≥ 85% (CLAUDE.md).

### Wave 0 Gaps
- [ ] `tests/test_web/test_saq_mount.py` — covers SAQUI-01/02 + the single-call contract.
- [ ] `tests/test_web/__init__.py` — new test package dir.
- [ ] Extend `tests/_queue_fakes.py::FakeQueue` with `async def info(self, jobs=False, offset=0, limit=10)` returning a minimal `QueueInfo`-shaped mapping (read shape from `saq/queue/redis.py:119`).
- [ ] Extend `tests/test_main_lifespan.py` — mount-present / instance-reuse / health-intact / flag-toggle cases (reuse the existing monkeypatch scaffold at `:75-99`).
- [ ] No framework install needed (pytest/httpx present).

## Security Domain

`security_enforcement` is not configured for this milestone; this phase's posture is LOCKED (no app-layer auth — proxy owns it).

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no (by design) | Reverse proxy internal-realm auth (out of app scope) |
| V3 Session Management | no | none |
| V4 Access Control | no (by design) | Network/proxy boundary; private LAN |
| V5 Input Validation | minimal | `saq_web` retry/abort POST actions take queue/job path params validated by Starlette routing; no app-supplied input |
| V6 Cryptography | no | none |

### Known Threat Patterns for this mount
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Unauthenticated mutation (`/saq/.../retry`, `/.../abort` are POST that mutate jobs) | Tampering / Elevation | LOCKED: only reachable behind proxy auth on a private LAN. **The PR MUST state explicitly** that `/saq` includes job retry/abort mutation endpoints and is intentionally unauthenticated at the app layer (`saq/web/starlette.py:148-149`). If the proxy boundary ever weakens, this becomes the primary risk. |
| Info disclosure (job kwargs/results shown in UI) | Information disclosure | `job_dict` `repr()`s kwargs/result (`saq/web/common.py:31-37`); acceptable on a single-user internal tool. Note any secret-bearing task payloads. |

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | Live homelab reverse proxy actually fronts `/saq` at the same path it forwards (so `root_path="/saq"` is correct) | Q3 | LOW — if an extra external prefix exists, set `root_path` to it; cosmetic asset 404s only, easy fix |
| A2 | Monitoring all `revoked_at IS NULL` agents (incl. never-seen ones with empty queues) is the desired set | Q1 | LOW — empty queues render harmlessly; if undesired, add `last_seen_at IS NOT NULL` to the query |
| A3 | Redis being reachable at request time is acceptable (dashboard errors if Redis down) | Env | LOW — operational; the dashboard's own `/saq/health` 500s when no queue info, which is informative |

All other claims are `[VERIFIED]` against installed source or empirical probe.

## Open Questions

1. **Should the agent-queue list include never-seen agents?**
   - What we know: `revoked_at IS NULL` (pipeline.py:186 shape) is the simplest enumeration and auto-excludes legacy.
   - What's unclear: whether the operator wants only `last_seen_at IS NOT NULL` agents (active) vs every registered agent.
   - Recommendation: include all non-revoked (empty queues are harmless); flag as A2 for the planner/discuss to confirm.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `from saq.web import saq_web` (bare) | `from saq.web.starlette import saq_web` | saq ≥0.13-ish web split | Bare path no longer the entry; CONTEXT already pins the correct import |
| Run `saq --web` as a separate service | Embed `saq_web` as a mounted ASGI sub-app | saq starlette web path | One process, one pool, no extra port |

**Deprecated/outdated:** none relevant; `saq==0.26.4` is current per `pyproject.toml` pin.

## Sources

### Primary (HIGH confidence)
- Installed `saq/web/starlette.py` (`saq_web` factory, routes, `QUEUES`/`ROOT_PATH` globals, `q.info()` reuse) — lines 23-24, 51-52, 100-101, 115-153.
- Installed `saq/web/common.py` (`render` via `str.format`, `STATIC_PATH`, `job_dict`) — lines 7-37.
- Installed `saq/queue/redis.py` (`from_url` lazy pool, `info`, `disconnect`) — lines 64-191; `saq/queue/base.py` (`from_url`, `connect`) — lines 194-214.
- Installed `starlette/applications.py` (lazy middleware stack wrapping `self.router` by reference, `mount`) — lines 57-93; `starlette/routing.py` (live `self.routes` iteration) — lines 578, 622.
- Empirical `uv run python` probes (this session): import chain + render output + `QUEUES` clobber; FastAPI mount-in-lifespan `TestClient` end-to-end (200 on `/saq/`, `/health` intact).
- Codebase: `src/phaze/main.py` (lifespan + create_app), `src/phaze/config.py` (settings shape), `src/phaze/services/agent_task_router.py` (`queue_for` caching+hook), `src/phaze/services/enqueue_router.py` (single-agent select + revoked semantics), `src/phaze/routers/pipeline.py:186` (all-non-revoked agent query), `tests/conftest.py`, `tests/test_main_lifespan.py`, `tests/_queue_fakes.py`, `tests/test_health.py`.
- `saq` dist METADATA — `Provides-Extra: web` = `aiohttp` + `aiohttp_basicauth` (aiohttp path only).

### Secondary (MEDIUM confidence)
- None required — design is locked and all load-bearing claims were verified against installed source.

### Tertiary (LOW confidence)
- None.

## Metadata

**Confidence breakdown:**
- Mount-in-lifespan timing (Q1): HIGH — starlette source + empirical TestClient probe.
- No second pool (Q1/Q2): HIGH — `saq_web` reads via passed instances; verified in source.
- Runtime deps / no `saq[web]` (Q2): HIGH — full import chain inspected; render is `str.format`.
- root_path / mount mechanics (Q3): HIGH — source + render output + probe.
- Testing approach (Q4): HIGH — existing analogs in repo (`test_main_lifespan`, `_queue_fakes`).
- Config flag (Q5): HIGH — config.py + main.py patterns confirmed.

**Research date:** 2026-06-11
**Valid until:** 2026-07-11 (stable; pinned `saq==0.26.4` / `starlette==1.2.1` / `fastapi==0.136.3`). Re-verify the `QUEUES` global behavior and `saq_web` signature if `saq` is bumped.
