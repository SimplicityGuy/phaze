---
phase: 33-saq-monitoring-ui-mounted-in-phaze-api
verified: 2026-06-11T16:29:31Z
status: human_needed
score: 6/6
overrides_applied: 0
human_verification:
  - test: "Open https://<host>/saq (via the reverse proxy) and confirm the SAQ dashboard loads listing both the 'controller' queue and at least one 'phaze-agent-<id>' queue."
    expected: "Dashboard renders with queue name, job counts, and worker status. CSS/JS assets resolve correctly under the /saq prefix (no 404 for /saq/static/*)."
    why_human: "The reverse-proxy TLS + internal-realm auth boundary and the live Redis/SAQ state cannot be exercised in unit/integration tests. Asset URL resolution under the proxy prefix is only provable in a real deployment."
  - test: "With the dashboard open, navigate to a queue and verify live job counts update without a page reload (SAQ dashboard polls via its own JS)."
    expected: "Queue depth cards show live counts from the actual Redis-backed queues, not zeroed-out test-double values."
    why_human: "Real-time behavior requires a live Redis + SAQ worker and cannot be simulated by the FakeQueue doubles used in tests."
  - test: "From the pipeline dashboard page, click the 'Queue Monitor' link in the header."
    expected: "The SAQ dashboard opens in a new browser tab; the pipeline page remains open in the original tab."
    why_human: "target=_blank / rel=noopener browser behavior and new-tab opening require a real browser; the test only asserts href= appears in rendered HTML."
---

# Phase 33: SAQ Monitoring UI Mounted in phaze-api — Verification Report

**Phase Goal:** Expose SAQ's built-in monitoring web UI by mounting it into the existing `phaze-api` FastAPI ASGI app at the `/saq` subpath — NOT the standalone `saq --web` server, NOT a new bound port, NO app-layer auth. Reuse the lifespan-created SAQ queue instances (controller queue + per-agent queues) — no second Redis connection pool. Additionally: a link from the main pipeline dashboard page to the mounted /saq UI.

**Verified:** 2026-06-11T16:29:31Z
**Status:** human_needed (all 6 automated must-haves VERIFIED; 3 items require live deployment testing)
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | `/saq` is mounted into the phaze-api app in the lifespan, not a standalone server and not a new bound port | VERIFIED | `src/phaze/main.py:141` — `_app.mount("/saq", build_saq_app([...]))` inside the `@asynccontextmanager async def lifespan` function, before `yield`. No subprocess, no new port, no `saq --web`. |
| 2 | The mount reuses the lifespan-created controller queue + per-agent queue instances with no second Redis pool | VERIFIED | `src/phaze/main.py:140-141` — `agent_queues = [_app.state.task_router.queue_for(agent.id) for agent in agents]` + `build_saq_app([_app.state.controller_queue, *agent_queues])`. `src/phaze/web/saq_mount.py` — no `Queue.from_url`, no Redis construction anywhere in the function body (AST-walked by `test_api_queues_reuses_passed_instances_no_pool`). |
| 3 | `settings.enable_saq_ui` exists and defaults to True; when False, no /saq route is registered and /health still works | VERIFIED | `src/phaze/config.py:252-256` — `enable_saq_ui: bool = Field(default=True, validation_alias=AliasChoices("PHAZE_ENABLE_SAQ_UI", "enable_saq_ui"), ...)`. `src/phaze/main.py:136` — `if settings.enable_saq_ui:` guards the mount. `test_saq_disabled_flag_skips_mount` asserts no `/saq` route, `/saq/` → 404, `/health` → 200 when flag is False. |
| 4 | No app-layer auth middleware was added | VERIFIED | No `add_middleware`, `HTTPBearer`, `BasicAuth`, or auth `Depends` wired to `/saq` anywhere in `src/phaze/main.py` or `src/phaze/web/`. Comment at `main.py:134-135` explicitly states "No auth middleware... the reverse proxy's internal-realm auth on the private LAN is the sole access control (LOCKED, threat T-33-03)." |
| 5 | The pipeline dashboard renders a link (`href="/saq"`) to the SAQ UI in a new tab with `rel="noopener"` | VERIFIED | `src/phaze/templates/pipeline/dashboard.html:10` — `<a href="/saq" target="_blank" rel="noopener" class="...">Queue Monitor ↗</a>`. `test_dashboard_links_to_saq_ui` asserts `href="/saq"` appears in `GET /pipeline/` response. |
| 6 | All phase-relevant tests pass; ruff and mypy are clean | VERIFIED | 54/54 targeted tests pass (`tests/test_web/`, `tests/test_main_lifespan.py`, `tests/test_health.py`, `tests/test_queue_fakes.py`, `tests/test_routers/test_pipeline.py`, `tests/test_phase04_gaps.py`). `uv run ruff check .` → All checks passed. `uv run mypy .` → Success, no issues in 145 source files. |

**Score:** 6/6 automated truths verified

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/web/__init__.py` | phaze.web package marker | VERIFIED | Present; one-line module docstring. |
| `src/phaze/web/saq_mount.py` | `build_saq_app(queues: list[Queue]) -> Starlette` factory | VERIFIED | 45 lines; exports `build_saq_app`; body is `return saq_web(_MOUNT_PATH, queues=queues)`; docstring warns of once-per-process clobber and no-second-pool contract; `Queue`/`Starlette` under `TYPE_CHECKING`, `saq_web` is the only runtime import. |
| `src/phaze/config.py` | `enable_saq_ui: bool = Field(default=True, ...)` | VERIFIED | Line 252-256; placed adjacent to `auto_migrate`; alias `PHAZE_ENABLE_SAQ_UI` wired. |
| `src/phaze/main.py` | Lifespan mount of `build_saq_app` at `/saq`, gated by `settings.enable_saq_ui` | VERIFIED | Lines 110-141; single mount call; `select`/`Agent`/`build_saq_app` imports added; comment block documents design decisions. |
| `src/phaze/templates/pipeline/dashboard.html` | `<a href="/saq" ...>Queue Monitor ↗</a>` link | VERIFIED | Line 10; flex row wrapping `<h1>` + trailing anchor with `target="_blank" rel="noopener"` and Tailwind nav-link classes. |
| `tests/_queue_fakes.py` | `FakeQueue.info()` returning a QueueInfo-shaped dict | VERIFIED | Line 126-145; `async def info(self, jobs=False, offset=0, limit=10) -> dict[str, Any]`; returns `{"workers": {}, "name": self.name, "queued", "active", "scheduled", "jobs": []}`; constructor kwargs `queued`/`active`/`scheduled` default 0. |
| `tests/test_queue_fakes.py` | Regression tests pinning the QueueInfo shape | VERIFIED | 3 async tests: shape + name echo, constructor count flow-through, `info(jobs=True)` shape. |
| `tests/test_web/__init__.py` | Package marker for test_web | VERIFIED | Present. |
| `tests/test_web/test_saq_mount.py` | 4 unit tests for `build_saq_app` | VERIFIED | `test_build_saq_app_routes_and_root_renders`, `test_api_queues_reuses_passed_instances_no_pool`, `test_enable_saq_ui_flag_defaults_true`, `test_saq_web_single_call_contract`; all pass. |
| `tests/test_main_lifespan.py` | 3 SAQ integration tests + migration compat fix | VERIFIED | `test_saq_mount_served_in_lifespan`, `test_saq_queues_assembled_and_reused`, `test_saq_disabled_flag_skips_mount` + updated `_fake_async_session` with AsyncMock; all 4 lifespan tests pass. |
| `tests/test_routers/test_pipeline.py` | `test_dashboard_links_to_saq_ui` | VERIFIED | Line 93-102; asserts `href="/saq"` in `GET /pipeline/` response. |

---

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `src/phaze/web/saq_mount.py::build_saq_app` | `saq.web.starlette.saq_web` | `saq_web("/saq", queues=queues)` | VERIFIED | `_MOUNT_PATH = "/saq"` at line 24; call at line 44 `return saq_web(_MOUNT_PATH, queues=queues)`. |
| `src/phaze/main.py lifespan` | `phaze.web.saq_mount.build_saq_app` | `_app.mount("/saq", build_saq_app([controller_queue, *agent_queues]))` | VERIFIED | `main.py:47` import, `main.py:141` call site. |
| `src/phaze/main.py lifespan` | `phaze.models.agent.Agent` | `select(Agent).where(Agent.revoked_at.is_(None))` | VERIFIED | `main.py:16` import, `main.py:138` query. |
| `src/phaze/config.py::Settings.enable_saq_ui` | `phaze.main lifespan` | `if settings.enable_saq_ui:` guard | VERIFIED | `main.py:136`. |
| `src/phaze/templates/pipeline/dashboard.html` | `/saq` mount | `<a href="/saq" target="_blank" rel="noopener">` | VERIFIED | `dashboard.html:10`. |

---

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|--------------------|--------|
| `saq_mount.py::build_saq_app` | `queues` (passed instances) | Caller provides lifespan-created `Queue` instances | Yes — real SAQ `Queue.info()` called in production; `FakeQueue.info()` in tests | FLOWING |
| `dashboard.html` | Static anchor href | Template literal `/saq` | N/A — static link, not dynamic data | N/A (static) |

---

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| `build_saq_app([])` returns a Starlette app | `uv run pytest tests/test_web/test_saq_mount.py::test_build_saq_app_routes_and_root_renders -q` | 1 passed | PASS |
| `/saq/api/queues` lists passed queues | `uv run pytest tests/test_web/test_saq_mount.py::test_api_queues_reuses_passed_instances_no_pool -q` | 1 passed | PASS |
| Lifespan mounts /saq, serves 200, /health intact | `uv run pytest tests/test_main_lifespan.py::test_saq_mount_served_in_lifespan -q` | 1 passed | PASS |
| Flag False → no /saq, /saq/ 404, /health 200 | `uv run pytest tests/test_main_lifespan.py::test_saq_disabled_flag_skips_mount -q` | 1 passed | PASS |
| Pipeline dashboard contains href="/saq" | `uv run pytest tests/test_routers/test_pipeline.py::test_dashboard_links_to_saq_ui -q` | 1 passed | PASS |
| `settings.enable_saq_ui` defaults True | `uv run pytest tests/test_web/test_saq_mount.py::test_enable_saq_ui_flag_defaults_true -q` | 1 passed | PASS |
| Full targeted suite (54 tests) | `uv run pytest tests/test_web/ tests/test_main_lifespan.py tests/test_health.py tests/test_queue_fakes.py tests/test_routers/test_pipeline.py tests/test_phase04_gaps.py -q` | 54 passed | PASS |
| `uv run ruff check .` | ruff lint | All checks passed | PASS |
| `uv run mypy .` | mypy type check | Success, no issues in 145 source files | PASS |

---

### Probe Execution

Step 7c: SKIPPED — no `scripts/*/tests/probe-*.sh` discovered for this phase.

---

### Requirements Coverage

| Requirement | Source Plan | Description (from ROADMAP) | Status | Evidence |
|-------------|------------|---------------------------|--------|----------|
| SAQUI-01 | 33-00, 33-01, 33-02 | Mount SAQ web UI at /saq in phaze-api ASGI app | SATISFIED | `_app.mount("/saq", build_saq_app(...))` in lifespan (`main.py:141`) |
| SAQUI-02 | 33-00, 33-01, 33-02 | Reuse lifespan queue instances, no second Redis pool | SATISFIED | `_app.state.controller_queue` + `task_router.queue_for(agent.id)` passed directly; no `Queue.from_url` in `saq_mount.py` |
| SAQUI-03 | 33-01 | `build_saq_app(queues)` helper wraps `saq_web("/saq", queues)` | SATISFIED | `saq_mount.py:44` |
| SAQUI-04 | 33-02 | `enable_saq_ui` flag gates the mount | SATISFIED | `config.py:252-256`, `main.py:136` |
| SAQUI-05 | 33-01, 33-02 | No auth middleware; no new bound port; no standalone server | SATISFIED | Grep confirms no middleware; no port binding beyond existing 8000; no subprocess |
| SAQUI-06 | 33-03 | Pipeline dashboard link to /saq | SATISFIED | `dashboard.html:10`, `test_pipeline.py:93-102` |

---

### Anti-Patterns Found

No TBD, FIXME, or XXX markers found in any phase-33-modified file. No stub implementations, placeholder components, or empty handlers detected.

---

### Human Verification Required

The following items require live deployment testing and cannot be verified programmatically:

#### 1. SAQ Dashboard Renders Correctly Behind the Reverse Proxy

**Test:** After deploying this phase, open `https://<host>/saq` through the reverse proxy (internal network auth boundary).
**Expected:** The SAQ monitoring dashboard loads. The queue list shows `controller` and one or more `phaze-agent-<id>` queues with live Redis-backed counts. CSS/JS assets resolve under `/saq/static/` with no 404s.
**Why human:** The reverse proxy's TLS termination + internal-realm auth and live Redis/SAQ worker state cannot be exercised in the unit/integration test environment. Asset URL resolution under the proxy prefix requires a real proxy + deployment.

#### 2. Live Queue Counts Update in the Dashboard

**Test:** With at least one SAQ worker running, open `/saq` and observe the queue depth counters.
**Expected:** Queue cards show non-zero `queued` / `active` counts that reflect actual job state in Redis (not zeroed-out test-double values). The SAQ dashboard's built-in polling refreshes the counts.
**Why human:** Real-time behavior requires live Redis + SAQ workers. The FakeQueue doubles used in all tests always return `queued=0, active=0`.

#### 3. "Queue Monitor" Link Opens SAQ Dashboard in New Tab

**Test:** Open the pipeline dashboard (`/pipeline/`). Click the "Queue Monitor ↗" link in the page header.
**Expected:** The SAQ dashboard opens in a **new** browser tab (`target="_blank"`). The pipeline dashboard remains open in the original tab. `rel="noopener"` prevents the new tab from accessing `window.opener`.
**Why human:** `target="_blank"` browser tab behavior and `rel="noopener"` cannot be verified by server-side HTML assertions; requires a real browser.

---

### Gaps Summary

None. All 6 automated must-haves are VERIFIED. The 3 human verification items are environmental deployment checks, not implementation gaps.

---

_Verified: 2026-06-11T16:29:31Z_
_Verifier: Claude (gsd-verifier)_
