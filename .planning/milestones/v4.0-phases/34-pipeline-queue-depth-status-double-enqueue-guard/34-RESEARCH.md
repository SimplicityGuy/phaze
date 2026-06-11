# Phase 34: Pipeline Queue-Depth Status & Double-Enqueue Guard - Research

**Researched:** 2026-06-10
**Domain:** SAQ (Redis) queue introspection + FastAPI/HTMX/Alpine dashboard surfacing
**Confidence:** HIGH (all APIs read from installed source + existing codebase; no external/training-data dependency)

## Summary

The locked design is sound and every primitive it relies on is verified against installed source. `saq==0.26.4` is installed and `Queue.count(kind)` for `kind ∈ {"queued","active"}` is a single cheap Redis op (`LLEN`) that provably excludes scheduled/cron jobs. The queue handles (`app.state.controller_queue`, `app.state.task_router`) and the non-revoked-agent predicate (`Agent.revoked_at.is_(None)`) all exist and are exercised by current code. The work is almost entirely additive plumbing plus mirroring the already-established `stats_bar.html` OOB + `$store.pipeline` x-init pattern.

Two findings change the implementation surface and the planner MUST account for them:

1. **The test `client` fixture does NOT run the lifespan** (`conftest.py:155-161` calls `create_app()` directly), so `app.state.controller_queue` and `app.state.task_router` are **absent** in every router test that does not call `wire_fakes()`/`install_fake_queues()`. `get_queue_activity` must therefore degrade to 0 on `AttributeError` (missing app.state attr) **as well as** Redis errors — otherwise the three existing un-wired tests (`test_dashboard_page`, `test_pipeline_stats_partial`, `test_dashboard_includes_settings_batch_size`) start 500-ing the moment the dashboard/stats endpoints call the new service.

2. **`stage_cards.html` contains only TWO buttons — "Run Analysis" and "Generate Proposals".** There are no Fingerprint or Extract-Metadata buttons in the dashboard UI (those tasks exist only as `/pipeline/fingerprint` and `/pipeline/extract-metadata` endpoints with no rendered trigger). The CONTEXT's "Analyze / Fingerprint / Extract-Metadata buttons" reference is partly aspirational. Realizable scope: gate **Run Analysis** with `agentBusy`, gate **Generate Proposals** with `controllerBusy`. See Assumptions Log A1.

**Primary recommendation:** Add `get_queue_activity(app_state, session)` to `services/pipeline.py` that sums `count("queued")+count("active")` across all `revoked_at IS NULL` agents' queues plus the controller queue, wrapping each read in a try/except that catches `(AttributeError, Exception)` → 0. Surface via both `dashboard()` and `pipeline_stats_partial()` contexts; mirror the `stats_bar.html` `oob_counts`-gated OOB + x-init store-write exactly for the new counts and a `processing_card.html`. Extend the `tests/_queue_fakes.py` doubles with an async `count(kind)` method.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Live queue-depth read | API / Backend (`services/pipeline.py`) | Database/Redis | Authoritative signal lives in Redis (SAQ); service owns the read + failure isolation |
| Non-revoked agent enumeration | API / Backend (DB query) | — | `Agent.revoked_at IS NULL` is a DB predicate already used by `dashboard()` |
| Count context injection | Frontend Server (SSR, `routers/pipeline.py`) | — | Both `dashboard()` (seed) and `pipeline_stats_partial()` (poll) contexts |
| Processing-card render | Frontend Server (Jinja partial) | Browser (HTMX OOB swap) | Server renders; HTMX swaps OOB by id on the 5s tick |
| Button disable / store state | Browser (Alpine `$store.pipeline`) | — | Client-side reactive `:disabled`; never re-render the button subtree |

## User Constraints (from CONTEXT.md)

### Locked Decisions
- **Data source:** `Queue.count(kind)` with `kind ∈ {"queued","active"}` (NOT `"incomplete"`). Reads live SAQ depth; excludes scheduled/cron jobs.
- **Queue handles:** `app.state.controller_queue` (named `controller`) and `app.state.task_router` (`AgentTaskRouter`, per-agent `queue_for(agent_id)`).
- **Sum across ALL non-revoked agents** (`Agent.revoked_at IS NULL`), not just the active one.
- **New service** `get_queue_activity(app_state, session)` in `services/pipeline.py` returning `agent_queued`, `agent_active`, `controller_queued`, `controller_active`, `agent_busy = agent_queued+agent_active`, `controller_busy = controller_queued+controller_active`. Must NOT raise — degrade to 0 on any unreachable queue.
- **Surface via the EXISTING 5s `/pipeline/stats` poll** — no new loop/SSE/websocket. Extend `pipeline_stats_partial()` context AND seed the same counts in the initial `dashboard()` full-page render.
- **Persistent "Processing" card** `partials/processing_card.html` in `dashboard.html` ABOVE the stats bar, OOB-swapped on each tick using the SAME `hx-swap-oob` + `oob_*`-flag gating as `stats_bar.html`. Shows progress bar + `"{queued} queued · {active} active"` when `agent_busy > 0`. **Progress denominator is DB-derived:** `percent = analyzed / (analyzed + agent_busy)` using `stats.analyzed` as `done`. Guard `analyzed + agent_busy == 0` → render empty. Second compact line for controller queue when `controller_busy > 0`. Card renders EMPTY when both busy values are 0.
- **Coarse button disable via Alpine `$store.pipeline`** using the SAME `x-init` store-write trick as `discovered`/`analyzed`. Analyze (and any agent-task trigger): `:disabled="loading || <ready>===0 || $store.pipeline.agentBusy > 0"`. Generate Proposals: `:disabled="loading || analyzed===0 || $store.pipeline.controllerBusy > 0"`. Coarse (all agent buttons share one serial queue) is intentional.
- **Store init:** `$store.pipeline` must define `agentBusy`/`controllerBusy` defaulting to 0; seed from the initial full-page render.

### Claude's Discretion
- Exact Tailwind classes / progress-bar markup (match existing partials' dark-mode-aware styling).
- Whether `get_queue_activity` takes `app.state + session` or narrower params — pick the cleanest testable signature.
- Helper to enumerate non-revoked agents (new vs reuse from `enqueue_router`).
- Number formatting (thousands separators) for large counts.

### Deferred Ideas (OUT OF SCOPE)
- Per-task-type queue counts / per-stage progress bars (would need self-maintained counters or full job-hash scans).
- SAQ's built-in monitoring dashboard (Phase 33).
- Reboot re-enqueue resilience (Phase 32). Any change to how jobs are enqueued/processed. Any new poll loop/websocket/SSE.

## Project Constraints (from CLAUDE.md)

- **Python 3.14, `uv` only** — every command prefixed `uv run` (`uv run pytest`, `uv run ruff check .`, `uv run mypy .`).
- **Ruff:** line length 150, double quotes, `target-version = py313`, rule sets incl. `ARG B C4 E F I PLC PTH RUF S SIM T20 TCH UP W`. `T201` (print) NOT allowed outside CLI/tests. New code is non-CLI → no `print`.
- **mypy strict** on `src/` (tests excluded): `disallow_untyped_defs`, `disallow_incomplete_defs`, `warn_return_any`, `no_implicit_optional`, etc. `services/pipeline.py` is under strict checking (only `^(tests/|prototype/|services/)` is excluded — NOTE: that `services/` is the top-level `./services/` deployment dir, NOT `src/phaze/services/`; the latter IS type-checked. Verify against `pyproject.toml` exclude regex when planning). All new functions need full type hints.
- **Coverage ≥ 85%** (Codecov patch target 80% / 5% threshold). New service + endpoint context + partial all need tests.
- **Pre-commit must pass** (frozen-SHA hooks; bandit, ruff, mypy local hook). Never `--no-verify`.
- **PR per phase** via worktree branch; commit frequently; keep service README current.

## Phase Requirements

> No `REQUIREMENTS.md` exists for this phase (the milestone-level `.planning/milestones/v4.0-REQUIREMENTS.md` predates phase 34; the phase was added 2026-06-10 from a live incident). Requirements below are derived from the LOCKED CONTEXT decisions and the STATE.md roadmap entry.

| ID (derived) | Description | Research Support |
|----|-------------|------------------|
| Q34-1 | Read live SAQ agent + controller queue depth without 500-ing the poll | `Queue.count` verified async + cheap; degrade-to-0 wrapping defined |
| Q34-2 | Sum agent depth across all non-revoked agents | `Agent.revoked_at.is_(None)` predicate (reuse `dashboard()` query) |
| Q34-3 | Seed counts on first load AND every 5s tick | extend both `dashboard()` and `pipeline_stats_partial()` contexts |
| Q34-4 | Persistent OOB "Processing" card with DB-derived progress + divide-by-zero guard | mirror `stats_bar.html` OOB gating; `analyzed/(analyzed+agent_busy)` |
| Q34-5 | Coarse button disable via `$store.pipeline.agentBusy`/`controllerBusy` | mirror existing `x-init` store-write; extend `base.html:91` store defaults |

## Standard Stack

All dependencies already installed — **no new packages**.

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| saq[redis] | 0.26.4 | Queue depth read via `Queue.count` | Already the project task queue; `count` is a first-class API `[VERIFIED: .venv/.../saq/queue/redis.py:179]` |
| FastAPI | (installed) | `/pipeline/stats` + `dashboard()` endpoints | Existing router `[VERIFIED: src/phaze/routers/pipeline.py]` |
| Jinja2 | (installed) | `processing_card.html` partial | Existing template engine via `Jinja2Templates` |
| SQLAlchemy + asyncpg | (installed) | non-revoked agent `select` | Existing `Agent` model query |

### Supporting (frontend, CDN — already loaded by `base.html`)
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| HTMX | 2.x (self-hosted/CDN) | `hx-swap-oob` of the processing card on the 5s tick | mirror `stats_bar.html` OOB block |
| Alpine.js | 3.x | `$store.pipeline.agentBusy`/`controllerBusy` reactive `:disabled` | mirror existing `x-init` store-write |
| Tailwind | 3.x | progress-bar styling (dark-mode-aware) | Claude's discretion; match existing partials |

**Installation:** none. `saq[redis]>=0.26.4` already pinned `[VERIFIED: pyproject.toml:13]`.

## Package Legitimacy Audit

No new external packages are installed by this phase. All libraries are pre-existing, pinned project dependencies. slopcheck not run (no install surface).

| Package | Registry | Disposition |
|---------|----------|-------------|
| saq[redis] | PyPI (already pinned 0.26.4) | Pre-existing — no change |

## SAQ `Queue.count` Semantics (Research Q1) — VERIFIED from installed source

`[VERIFIED: .venv/lib/python3.14/site-packages/saq/queue/redis.py:179-192]`

```python
async def count(self, kind: CountKind) -> int:
    if kind == "queued":
        return await self.redis.llen(self._queued)      # LLEN saq:<name>:queued
    if kind == "active":
        return await self.redis.llen(self._active)       # LLEN saq:<name>:active
    if kind == "incomplete":
        return await self.redis.zcard(self._incomplete)  # ZCARD saq:<name>:incomplete
    raise ValueError("Can't count unknown type {kind}")
```

- **`count` is `async`** — must be `await`ed. Returns `int`.
- **`count("queued")` = `LLEN` of the ready-to-run list only; `count("active")` = `LLEN` of the in-progress list only.** Both O(1).
- **`scheduled`/cron jobs are NOT in either count.** Proven by the sibling `info()` method `[VERIFIED: redis.py:175]`: `"scheduled": incomplete - queued - active`. So `incomplete = queued + active + scheduled` ⇒ `queued + active` provably excludes scheduled. The idle controller crons (`reap_stalled_scans`, `refresh_tracklists`) sit in the `incomplete`/`schedule` ZSET and will NOT inflate `queued`+`active`. **Do NOT use `count("incomplete")`** — it would include them (matches CONTEXT decision).
- **`CountKind` is `Literal["queued", "active", "incomplete"]`** `[VERIFIED: saq/types.py:137]` — there is no `"scheduled"` kind; passing one raises `ValueError`. Stick to the two literals.
- **Connection:** `count` calls `self.redis.<op>` — the queue's own already-connected async redis client (set at construction, `redis.py:79`). No per-call connect/disconnect; the pool is reused. `Queue.from_url` lazily connects on first op. Safe to call repeatedly.
- **Version gotcha:** the CONTEXT note "incomplete would also count scheduled in some versions" is conservative; in 0.26.4 `incomplete` ZCARD includes scheduled jobs (they live in the incomplete set until run). Using `queued`+`active` is version-robust. Confidence HIGH.

## Per-Agent Queue Enumeration (Research Q2) — VERIFIED

**Exact non-revoked predicate to reuse** — `dashboard()` already runs it `[VERIFIED: src/phaze/routers/pipeline.py:186]`:
```python
agents_stmt = select(Agent).where(Agent.revoked_at.is_(None)).order_by(Agent.name)
```
This is the predicate to mirror for summing queue depth. `Agent.revoked_at` is a nullable `DateTime` `[VERIFIED: src/phaze/models/agent.py:30]`. The permanently-revoked `legacy-application-server` (`revoked_at == created_at`) is excluded by `revoked_at IS NULL` `[CITED: enqueue_router.py:24-27]`.

**Do NOT reuse `select_active_agent`** for the sum: `[VERIFIED: enqueue_router.py:93-117]` it additionally filters `last_seen_at IS NOT NULL` and `ORDER BY last_seen_at DESC LIMIT 1` — returns exactly ONE agent and raises `NoActiveAgentError` when none qualifies. CONTEXT requires summing across **all** non-revoked agents (robust to a second agent), so use the `dashboard()`-style `revoked_at IS NULL` query and iterate.

**How `queue_for` builds/caches** `[VERIFIED: services/agent_task_router.py:68-101]`:
```python
def queue_for(self, agent_id: str) -> Queue:   # public accessor (Phase 30)
    return self._queue_for(agent_id)
def _queue_for(self, agent_id: str) -> Queue:
    if agent_id not in self._queues:
        queue = Queue.from_url(self._redis_url, name=f"phaze-agent-{agent_id}")
        queue.register_before_enqueue(apply_project_job_defaults)
        self._queues[agent_id] = queue
    return self._queues[agent_id]
```
- `queue_for(agent.id)` returns the cached `phaze-agent-<id>` `saq.Queue`; calling it during enumeration **lazily constructs** a Queue (and its redis client) for any not-yet-seen agent — cheap, cached, connection reused thereafter. Reading `count` on a queue for an agent with no jobs returns 0 (empty list).
- **Recommended `get_queue_activity` shape:** query `Agent.revoked_at IS NULL`, then for each agent `q = app_state.task_router.queue_for(agent.id)` and sum `await q.count("queued")` + `await q.count("active")`; add `app_state.controller_queue.count(...)`. (No new helper required — reusing the inline `revoked_at IS NULL` select is the cleanest. A thin helper `select_non_revoked_agents(session)` could be extracted but is optional per Claude's discretion.)
- **mypy note:** `app_state` is typed `Any` in `enqueue_router.resolve_queue_for_task` `[VERIFIED: enqueue_router.py:122]`. Use the same `Any` (or a narrow `Protocol`) for `get_queue_activity`'s `app_state` param so the missing-attr degrade path type-checks under strict mypy.

## Failure Isolation (Research Q3)

**There is NO existing prior art** for redis/queue error handling in the dashboard/stats path — `get_pipeline_stats` and `dashboard()`/`pipeline_stats_partial()` touch only Postgres today `[VERIFIED: services/pipeline.py:29-39, routers/pipeline.py:183-222]`. The only queue-error handling anywhere is `NoActiveAgentError` in the trigger endpoints `[VERIFIED: routers/pipeline.py:109,238]`, which is a different concern (no agent to enqueue to).

**Two failure modes must both degrade to 0:**
1. **Redis unreachable / timeout** while calling `count` → any `Exception` from the redis client.
2. **`app.state` missing the attribute** (`AttributeError`) — this is the common case in tests because the **lifespan does not run for the test `client`** `[VERIFIED: conftest.py:155-161]`. Without this guard, `test_dashboard_page`, `test_pipeline_stats_partial`, and `test_dashboard_includes_settings_batch_size` (none of which wire fakes) would `AttributeError`/500 once the endpoints call `get_queue_activity`.

**Recommended pattern** — isolate per-source so one dead queue does not zero the other, and the whole function never raises:
```python
async def get_queue_activity(app_state: Any, session: AsyncSession) -> dict[str, int]:
    agent_queued = agent_active = controller_queued = controller_active = 0
    try:
        for agent in <revoked_at IS NULL agents>:
            q = app_state.task_router.queue_for(agent.id)
            agent_queued += await q.count("queued")
            agent_active += await q.count("active")
    except Exception:          # AttributeError (no lifespan) OR redis error  # noqa: BLE001
        agent_queued = agent_active = 0
    try:
        controller_queued = await app_state.controller_queue.count("queued")
        controller_active = await app_state.controller_queue.count("active")
    except Exception:          # noqa: BLE001
        controller_queued = controller_active = 0
    agent_busy = agent_queued + agent_active
    controller_busy = controller_queued + controller_active
    return {"agent_queued": ..., "agent_active": ..., "controller_queued": ...,
            "controller_active": ..., "agent_busy": agent_busy, "controller_busy": controller_busy}
```
- Broad `except Exception` trips ruff `BLE001` (blind-except) — annotate the two lines `# noqa: BLE001` with a comment, or catch a tuple `(AttributeError, RedisError, OSError, ConnectionError)`. A blind catch is the safer operational choice (the whole point is "a Redis hiccup must never 500 the dashboard"); justify the `noqa` in a comment. Consider a `structlog` `logger.warning(...)` in the except so silent zeros are observable (no `print` — `T201`).
- Splitting agent vs controller try-blocks means a controller-queue outage still surfaces agent depth and vice-versa.

## Testing Approach (Research Q4)

### (a) Service that reads from queues
- **No existing pure-unit analog** — `test_services/test_agent_task_router.py` `[VERIFIED]` is `@pytest.mark.integration` and needs a **real Redis** (it explicitly notes "no `fakeredis` fallback because SAQ's Queue.from_url is not compatible with fakeredis at saq>=0.26"). Do NOT model `get_queue_activity` unit tests on it.
- **Recommended:** unit-test `get_queue_activity` with a fake `app_state` + fake queues that expose an **async `count(kind)`**. The existing `tests/_queue_fakes.py` `FakeQueue`/`FakeTaskRouter` do **NOT** have a `count` method `[VERIFIED: tests/_queue_fakes.py:56-111]` — **the planner must add an async `count(self, kind)` to `FakeQueue`** (configurable per-kind return, e.g. `self._counts = {"queued": n, "active": m}`) and let `FakeTaskRouter.queue_for` hand back agents' fakes. Seed agents with `seed_active_agent(session)` (or several) so the `revoked_at IS NULL` enumeration finds them.
- Cover: sum across 2 agents; scheduled-excluded (assert the service never calls `count("incomplete")` — or that a queue reporting incomplete>queued+active does not change the result); Redis-error degrade (fake `count` raises → result all-0); missing-`app.state`-attr degrade (pass a `SimpleNamespace()` lacking `task_router`/`controller_queue`).

### (b) `/pipeline/stats` router test
- **Exact analog:** `tests/test_routers/test_pipeline.py::test_pipeline_stats_partial` `[VERIFIED: test_pipeline.py:290-301]` — GETs `/pipeline/stats`, asserts 200 + HTML substrings. It does NOT wire fakes, proving the endpoint must survive absent `app.state` queues (the degrade path).
- **app.state faking:** `install_fake_queues(client)` / `wire_fakes(client)` `[VERIFIED: _queue_fakes.py:114-141]` set `app.state.controller_queue` + `app.state.task_router`. Use `install_fake_queues` to assert per-queue, `wire_fakes` for a merged capture. After adding `count` to the fakes, a wired test can assert the rendered counts/`agentBusy` reflect the fake depths.
- Background-task drain helper `_drain_background()` `[VERIFIED: test_pipeline.py:40-47]` is for enqueue tests; not needed for the read-only stats path.

### (c) Partial-rendering test
- **Exact pattern:** `tests/test_template_helpers/test_progress_partial.py` `[VERIFIED]` renders partials directly via `Jinja2Templates(directory=...).TemplateResponse(request=_fake_request(), name=..., context={...})` then `response.body.decode()` and asserts on HTML. `_fake_request()` (lines 26-44) builds a minimal Starlette `Request`. The `_render_scan_progress_card` helper (lines 313-342) is the closest model — a pipeline partial fed a `SimpleNamespace` batch + context. **Mirror this for `processing_card.html`**: render with `agent_busy`/`controller_busy`/`stats` permutations and assert:
  - busy>0 → progress bar markup + `"{queued} queued · {active} active"` text present.
  - both busy==0 → card renders empty (assert the visual block / bar class is absent).
  - `analyzed + agent_busy == 0` → no divide-by-zero, empty render (the guard).
  - percent math: e.g. `analyzed=30, agent_busy=10` → 75% width string present.
- `TEMPLATES_DIR` constant + the `Jinja2Templates` reuse give production-identical autoescape.

## HTMX OOB + Alpine `$store.pipeline` Pattern (Research Q5) — VERIFIED mechanics

**Reference:** `partials/stats_bar.html:27-48` `[VERIFIED]`. Mechanics to mirror exactly:

1. **`oob_counts` flag gates the OOB block.** The block is wrapped `{% if oob_counts %}...{% endif %}` (line 45). `pipeline_stats_partial()` sets `oob_counts=True` in context `[VERIFIED: routers/pipeline.py:221]`; the full-page `dashboard()` include omits it, so the OOB paragraphs are **skipped at initial load**. This avoids (a) `hx-swap-oob` rendering as stray visible text at load (htmx only honors it during a swap) and (b) duplicate-id DOM collision with the same ids already present in `stage_cards.html`. **The new processing card OOB block + new count OOB paragraphs must use the identical `{% if oob_counts %}` gate.**

2. **OOB swap targets by id.** Each poll-only element carries `id="..."` + `hx-swap-oob="true"`:
   ```html
   <p id="analyze-files-ready" hx-swap-oob="true"
      x-init="$store.pipeline.discovered = {{ stats.discovered }}" ...>{{ stats.discovered }} files ready</p>
   ```
   On each 5s `/pipeline/stats` response, htmx swaps the element with matching id wherever it lives in the DOM, **without** the poll touching the interactive button subtree (`#analyze-response` / `#proposals-response` are never swap targets — so a click's loading state and "Enqueued N files" message survive).

3. **`x-init` writes the store.** Each freshly-swapped node runs `x-init="$store.pipeline.<key> = <value>"` when Alpine initializes it, pushing the new value into the single-source-of-truth store that drives both the text and the `:disabled` bindings.

**Apply to phase 34:**
- **Store defaults** — extend `base.html:91` `[VERIFIED]` from `Alpine.store('pipeline', { discovered: 0, analyzed: 0 });` to add `agentBusy: 0, controllerBusy: 0` so `:disabled` never reads `undefined` before the first poll.
- **Processing card** — `processing_card.html` lives in `dashboard.html` above the `#pipeline-stats` div `[VERIFIED: dashboard.html:16-19]`. Give the card a stable `id`. The full-page include renders it WITHOUT oob; the `/pipeline/stats` partial response emits the same card wrapped with `hx-swap-oob="true"` + the `{% if oob_counts %}` gate so the 5s tick swaps it in place. Inside, `x-init="$store.pipeline.agentBusy = {{ agent_busy }}; $store.pipeline.controllerBusy = {{ controller_busy }}"` (or via dedicated OOB paragraphs mirroring the existing two) pushes busy counts into the store.
- **Button disable** — `stage_cards.html` `[VERIFIED]`: Run Analysis `:disabled="loading || $store.pipeline.discovered === 0"` → add `|| $store.pipeline.agentBusy > 0`. Generate Proposals `:disabled="loading || $store.pipeline.analyzed === 0"` → add `|| $store.pipeline.controllerBusy > 0`. The button subtree stays out of the swap target, preserving loading state (same invariant as today).
- **Progress bar** — `percent = analyzed / (analyzed + agent_busy)`; guard `analyzed + agent_busy == 0` → render empty (no bar, card hidden). Compute in the template or pass a pre-computed `percent` in context (cleaner + unit-testable; recommended).

## Architecture Patterns

### System data flow
```
              ┌─────────────────────────── Browser ───────────────────────────┐
              │  #pipeline-stats  --hx-get /pipeline/stats every 5s-->          │
              │  Alpine $store.pipeline { discovered, analyzed,                 │
              │                           agentBusy, controllerBusy }           │
              │     ▲ x-init store-writes        │ :disabled bindings           │
              └─────┼────────────────────────────┼──────────────────────────────┘
                    │ (HTMX OOB swap by id)       │
        ┌───────────┴───────────── FastAPI (routers/pipeline.py) ───────────────┐
        │ dashboard()  ── seed counts on first load (no oob flag)               │
        │ pipeline_stats_partial() ── oob_counts=True, swaps counts + card      │
        │            │ calls                                                     │
        │   get_pipeline_stats(session)   get_queue_activity(app_state, session)│
        └───────────┼────────────────────────────┼──────────────────────────────┘
                    │ Postgres (FileRecord)       │ try/except → 0 on failure
                    │                             ▼
                    │              ┌── revoked_at IS NULL agents ──┐
                    │              │  task_router.queue_for(id)    │  controller_queue
                    ▼              ▼   .count("queued"/"active")   ▼   .count(...)
              analyzed/discovered            Redis (SAQ per-agent + controller queues)
```

### Pattern: read-only service with hard failure isolation
**What:** A service that reads a flaky external system (Redis) inside a hot poll path returns sane defaults instead of propagating exceptions.
**When:** Any value surfaced on the 5s dashboard poll where staleness/zero is strictly preferable to a 500.
**Example:** the `get_queue_activity` try/except sketch above.

### Anti-Patterns to Avoid
- **Using `count("incomplete")`** — pulls in scheduled/cron jobs; inflates the busy signal. Use `queued`+`active`.
- **Reusing `select_active_agent` for the sum** — it returns one agent and raises when none is "recently seen"; would under-count and could 500 the poll. Use `revoked_at IS NULL` enumeration.
- **Letting the OOB block render at initial load** — duplicate ids + stray text. Gate with `oob_counts`.
- **Making the button subtree a swap target** — clobbers in-flight loading state. Drive disable purely through the store.
- **Bare `except` with no log** — silent zeros hide a real Redis outage; emit a `structlog` warning.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Queue depth | Custom `LLEN`/`ZCARD` via raw redis client | `Queue.count("queued"/"active")` | SAQ owns the key namespacing (`saq:<name>:queued`); raw keys drift |
| Per-task counters | Self-maintained enqueue/complete counters | Coarse `agent_busy`/`controller_busy` | CONTEXT-locked; per-task needs job-hash scans (deferred) |
| Agent enumeration | New bespoke query | reuse `Agent.revoked_at.is_(None)` select | already the canonical predicate (`dashboard()`, `enqueue_router`) |
| Partial OOB refresh | New poll loop / SSE | existing 5s `/pipeline/stats` + `hx-swap-oob` | CONTEXT-locked; pattern already proven in `stats_bar.html` |

## Common Pitfalls

### Pitfall 1: `get_queue_activity` 500s the existing un-wired tests
**What goes wrong:** Adding the call to `dashboard()`/`pipeline_stats_partial()` makes `test_dashboard_page` / `test_pipeline_stats_partial` / `test_dashboard_includes_settings_batch_size` raise `AttributeError` (no `app.state.task_router` because the test `client` skips the lifespan).
**Why:** `conftest.py:155-161` builds the app with `create_app()` and never enters the lifespan that sets `app.state.controller_queue`/`task_router`.
**Avoid:** the degrade path must catch `AttributeError` too, not only redis errors. Add a regression test that GETs `/pipeline/stats` with NO fakes wired and asserts 200 + counts all 0.
**Warning sign:** any traceback mentioning `'State' object has no attribute 'task_router'`.

### Pitfall 2: `count` not awaited
**What goes wrong:** `q.count("queued")` returns a coroutine; summing coroutines yields a `TypeError`/always-truthy garbage.
**Why:** `count` is `async`.
**Avoid:** `await` every `count`. mypy strict + `warn_return_any` will catch an un-awaited coroutine assigned to an `int`.

### Pitfall 3: divide-by-zero / backward-jumping progress
**What goes wrong:** `analyzed + agent_busy == 0` → ZeroDivisionError; or progress bar jumps backward on worker restart if `done` came from SAQ `complete`.
**Avoid:** guard the denominator → empty render; use DB `stats.analyzed` as `done` (CONTEXT-locked, survives restarts). Unit-test both the guard and the percent math.

### Pitfall 4: blind-except lint failure
**What goes wrong:** `except Exception:` trips ruff `BLE001`.
**Avoid:** either catch a specific tuple `(AttributeError, OSError, ConnectionError, RedisError)` or `# noqa: BLE001` with a justifying comment. Bandit may also flag `try/except/pass` (`S110`) — log a warning instead of silent `pass`.

### Pitfall 5: Fingerprint/Metadata buttons don't exist
**What goes wrong:** Planner writes tasks to add `agentBusy` to non-existent Fingerprint/Extract-Metadata buttons.
**Avoid:** only Run Analysis (agent) + Generate Proposals (controller) are rendered. If the operator wants the others gated, the buttons must be ADDED first — flag as out-of-scope or confirm. See Assumptions Log A1.

## Code Examples

### Counting queue depth (the verified primitive)
```python
# Source: VERIFIED saq/queue/redis.py:179 — both are async, O(1), exclude scheduled.
queued = await queue.count("queued")   # LLEN saq:<name>:queued
active = await queue.count("active")   # LLEN saq:<name>:active
busy = queued + active                 # excludes scheduled/cron jobs
```

### Non-revoked agent enumeration (verified predicate)
```python
# Source: VERIFIED src/phaze/routers/pipeline.py:186 (dashboard()) — reuse this predicate.
from phaze.models.agent import Agent
from sqlalchemy import select
agents = (await session.execute(
    select(Agent).where(Agent.revoked_at.is_(None))
)).scalars().all()
```

### Rendering a partial in a unit test (verified harness)
```python
# Source: VERIFIED tests/test_template_helpers/test_progress_partial.py:313-342
response = _templates.TemplateResponse(
    request=_fake_request(),
    name="pipeline/partials/processing_card.html",
    context={"agent_busy": 10, "controller_busy": 0, "stats": {"analyzed": 30}, "percent": 75},
)
html = response.body.decode()
assert "10 queued" not in html  # adjust to the locked "{queued} queued · {active} active" copy
```

## Runtime State Inventory

This is an additive feature (new read path + new template + store-field additions). No rename/migration.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | None — reads live Redis via SAQ; writes nothing. Verified by reading the service/router. | None |
| Live service config | None — no new queue, no config keys. `app.state` handles already wired in `main.py` lifespan. | None |
| OS-registered state | None | None |
| Secrets/env vars | None new — reuses `settings.redis_url` already consumed by `main.py`. | None |
| Build artifacts | None | None |
| Frontend store schema | `base.html:91` `Alpine.store('pipeline', {...})` gains `agentBusy`/`controllerBusy` keys (additive; old keys unchanged). | Add 2 default-0 keys |

## Validation Architecture

> `workflow.nyquist_validation` not explicitly false → section included.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (installed); `uv run pytest` |
| Config | `pyproject.toml` (`[tool.pytest.ini_options]`) + `tests/conftest.py` |
| Quick run | `uv run pytest tests/test_services/test_pipeline.py tests/test_routers/test_pipeline.py tests/test_template_helpers/test_progress_partial.py -x` |
| Full suite | `uv run pytest --cov --cov-report=term-missing` (≥85%) |

### Locked Behavior → Test Map
| Behavior | Test Type | File (new/extend) | Assertion |
|----------|-----------|-------------------|-----------|
| Queue count sums across N non-revoked agents | unit | extend `tests/test_services/test_pipeline.py` | seed 2 agents, fake `count` returns per-agent depths → `agent_queued`/`agent_active` are the sum |
| Scheduled jobs excluded | unit | same | service never calls `count("incomplete")`; a fake reporting large `incomplete` doesn't change `agent_busy` |
| Redis error degrades to 0 | unit | same | fake `count` raises → all-0 dict, no exception |
| Missing `app.state` attr degrades to 0 | unit + router | same + `test_pipeline.py` | `SimpleNamespace()` app_state → all-0; GET `/pipeline/stats` with no fakes → 200, counts 0 |
| Controller depth independent of agent depth | unit | same | controller queue raises but agents succeed → agent counts intact |
| First-load seeds counts | router | `test_pipeline.py` | GET `/pipeline/` (fakes wired) → rendered store-seed reflects depths |
| 5s poll seeds counts (oob) | router | `test_pipeline.py::test_pipeline_stats_partial` (extend) | GET `/pipeline/stats` → `hx-swap-oob` block + `agentBusy` x-init present |
| busy>0 disables buttons | partial | extend `test_progress_partial.py` (stage_cards render) | `$store.pipeline.agentBusy > 0` in Analyze `:disabled`; `controllerBusy > 0` in Proposals |
| Processing card renders when busy | partial | new in `test_progress_partial.py` | `agent_busy=10` → bar + "queued · active" text present |
| Card empty when idle | partial | same | `agent_busy==0 and controller_busy==0` → no bar markup |
| Progress denominator math | partial/unit | same | `analyzed=30, agent_busy=10` → 75% |
| Divide-by-zero guard | partial/unit | same | `analyzed+agent_busy==0` → empty render, no error |

### Sampling Rate
- **Per task commit:** quick run command above.
- **Per wave/PR:** `uv run pytest --cov --cov-report=term-missing` + `uv run ruff check . && uv run ruff format --check . && uv run mypy .` + `pre-commit run --all-files`.
- **Phase gate:** full suite green, ≥85% coverage, all pre-commit hooks pass.

### Wave 0 Gaps
- [ ] **Extend `tests/_queue_fakes.py::FakeQueue` with an async `count(self, kind)`** returning configurable per-kind values — required before any `get_queue_activity` test can run. (FakeTaskRouter already routes per-agent fakes.)
- [ ] `src/phaze/templates/pipeline/partials/processing_card.html` — new file; covered by new partial tests.
- [ ] Confirm whether a `percent` is computed in the router context or the template (recommend context → unit-testable).

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| saq[redis] | `Queue.count` | ✓ | 0.26.4 | — |
| Redis | runtime queue depth | runtime-only (homelab `phaze-redis`) | 7+ | Tests use `FakeQueue.count`; prod degrade-to-0 covers outages |
| pytest/pytest-asyncio | tests | ✓ | installed | — |

Redis is NOT needed for the unit/router tests (fakes + degrade path). The `@integration` agent_task_router tests already gate on a real Redis and skip cleanly when absent.

## Security Domain

> `security_enforcement` not configured false; this phase is read-only internal dashboard plumbing on a private network (single-user admin UI).

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V5 Input Validation | minimal | No user input — counts are server-derived ints rendered into Jinja (autoescaped). `count` returns `int`; no injection surface. |
| V6 Cryptography | no | — |
| Others | no | No auth/session/access-control change; no new endpoint (extends existing contexts). |

Note: counts rendered into HTML/JS via `x-init="$store.pipeline.agentBusy = {{ agent_busy }}"` are integers from `count()`/DB — not user-controlled — so the inline-JS interpolation carries no XSS risk. Keep them ints (no formatting that injects quotes into the JS expression; if adding thousands-separators for display text, do it in the visible text node, NOT in the `x-init` numeric assignment).

## State of the Art

| Old Approach | Current Approach | Impact |
|--------------|------------------|--------|
| Dashboard reads DB `FileState` only | + live SAQ queue depth | DB can't distinguish "nothing queued" from "everything queued"; queue depth is the only authoritative in-flight signal |
| `_queue_for` private | `queue_for` public accessor (Phase 30) | enumeration can fetch hook-applied per-agent queues without touching privates |

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | Only "Run Analysis" + "Generate Proposals" buttons exist in `stage_cards.html`; CONTEXT's "Fingerprint/Extract-Metadata buttons" are not currently rendered. `[VERIFIED: stage_cards.html — but the design intent is ASSUMED]` | Summary / Pitfall 5 | Planner may waste effort gating non-existent buttons, or under-deliver if operator expected those buttons added. **Confirm with operator before planning the disable scope.** |
| A2 | `src/phaze/services/` IS under strict mypy (only top-level `./services/` deploy dir is excluded by `exclude = "^(tests/|prototype/|services/)"`). `[ASSUMED — exclude regex is relative; verify against actual layout]` | Project Constraints | If `src/phaze/services` is excluded, type-hint strictness on the new function is relaxed (lower risk). Verify by checking whether the exclude matches `src/phaze/services/pipeline.py` (it likely does NOT, since the path doesn't start with `services/`). |
| A3 | The `processing_card.html` percent is best pre-computed in the router context for unit-testability. `[ASSUMED — Claude's discretion per CONTEXT]` | Validation / Wave 0 | Low — either location works; template-side math is harder to unit-test. |

## Open Questions (RESOLVED)

1. **Fingerprint/Metadata button gating** — RESOLVED (operator decision, 2026-06-10): this phase ADDS the two missing buttons → **four buttons total**, all gated. Run Analysis / Fingerprint / Extract-Metadata gated by `agentBusy`; Generate Proposals by `controllerBusy`. (Supersedes the original "scope to existing two" recommendation; the new buttons wire to the already-existing `/pipeline/fingerprint` + `/pipeline/extract-metadata` endpoints — no new enqueue paths.) See CONTEXT.md "Four buttons (two new)".
2. **`get_queue_activity` signature** — RESOLVED: `get_queue_activity(app_state: Any, session: AsyncSession)` (matches `enqueue_router` precedent; keeps the missing-attr degrade path simple). Locked in CONTEXT.md.

## Sources

### Primary (HIGH confidence — installed source / live codebase)
- `.venv/lib/python3.14/site-packages/saq/queue/redis.py:160-192` — `count` + `info` (scheduled = incomplete-queued-active)
- `.venv/lib/python3.14/site-packages/saq/queue/base.py:102-104` + `types.py:137` — `CountKind` literal, abstract `count`
- `src/phaze/routers/pipeline.py` — `dashboard()`, `pipeline_stats_partial()`, non-revoked agent select (L186)
- `src/phaze/services/pipeline.py` — `get_pipeline_stats` (target module)
- `src/phaze/services/enqueue_router.py` — `revoked_at IS NULL` predicate, `app_state: Any`, `select_active_agent`
- `src/phaze/services/agent_task_router.py` — `queue_for` lazy cache + before_enqueue hook
- `src/phaze/main.py:95-111` — lifespan wires `controller_queue` + `task_router`
- `src/phaze/templates/pipeline/{dashboard,partials/stats_bar,partials/stage_cards}.html` + `base.html:91` — OOB + store pattern
- `tests/_queue_fakes.py`, `tests/conftest.py:155-161`, `tests/test_routers/test_pipeline.py`, `tests/test_services/test_pipeline.py`, `tests/test_template_helpers/test_progress_partial.py` — test harness analogs
- `pyproject.toml:13` — `saq[redis]>=0.26.4`; `saq-0.26.4.dist-info/METADATA` — version

### Secondary / Tertiary
- None required — all claims verified against installed source or repo. No WebSearch used (closed-world phase).

## Metadata

**Confidence breakdown:**
- SAQ `count` semantics: HIGH — read directly from installed 0.26.4 source.
- Agent enumeration / queue plumbing: HIGH — verified against live codebase.
- Failure isolation requirement: HIGH — confirmed lifespan-skip in conftest + no existing handler.
- Testing approach: HIGH — concrete analog files cited; one gap (add `count` to fakes) flagged.
- Button-scope (Fingerprint/Metadata): MEDIUM — UI absence verified, operator intent assumed (A1).

**Research date:** 2026-06-10
**Valid until:** 2026-07-10 (stable; pinned saq version, internal code)
