---
phase: 29-deployment-hardening-agents-admin
verified: 2026-05-17T00:09:50Z
status: human_needed
score: 6/6 must-haves verified
overrides_applied: 0
human_verification:
  - test: "Real two-host smoke: bring up app-server with `just up`, copy CA, register agent, run `just up-agent` on file server, check /admin/agents shows ALIVE within 60s"
    expected: "Agent row appears on /admin/agents with green ALIVE pill within 60 seconds of `just up-agent`; agent logs show cert banner, model download, heartbeat every 30s; REDIS_BIND_IP set to app-server LAN IP prevents file-server direct Redis access from outside"
    why_human: "Real two-host hardware not available during verification; docs-only review was accepted by the operator (resume signal `verified-docs-only` in 29-08-SUMMARY.md). The ROADMAP phase goal explicitly includes 'A real two-host deployment runs end-to-end' — this is a deferred UAT item, not a blocking gap, per the operator's own verification decision."
---

# Phase 29: Deployment Hardening + Agents Admin Verification Report

**Phase Goal:** A real two-host deployment runs end-to-end with the application server holding no file mounts, HTTPS + Redis hardening in place, and an admin can see at a glance which agents are alive and healthy.
**Verified:** 2026-05-17T00:09:50Z
**Status:** human_needed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Application server compose has no file mounts on api or worker (DIST-01) | VERIFIED | `docker-compose.yml` services = `{api, postgres, redis, worker}` only; api volumes = `['${CA_PATH:-./certs}:/certs:rw']`; worker has no volumes key; 4 passing structural-parse tests in `tests/test_deployment/test_api_filesystem_isolation.py` |
| 2 | HTTPS termination via internal CA: cert-bootstrap generates CA + leaf on api startup; entrypoint shim wires uvicorn with TLS flags (AUTH-02) | VERIFIED | `src/phaze/cert_bootstrap.py` (240 lines) exports `ensure_certs_present`; `src/phaze/entrypoint.py` calls it then `os.execvp("uv", [..., "--ssl-keyfile", ..., "--ssl-certfile", ...])`; `docker-compose.yml` api command is `uv run python -m phaze.entrypoint`; 7 cert_bootstrap tests + 4 TLS integration tests pass (92 total phase-29 tests green) |
| 3 | Redis hardened: requirepass enforced, LAN-bound port, no 0.0.0.0 exposure (AUTH-03) | VERIFIED | redis command = `['redis-server', '--requirepass', '${REDIS_PASSWORD:?REDIS_PASSWORD required}']`; ports = `['${REDIS_BIND_IP:-127.0.0.1}:6379:6379']`; healthcheck = `redis-cli --no-auth-warning -a ${REDIS_PASSWORD} ping`; `AgentSettings._enforce_redis_password_in_production` validator refuses passwordless URLs in production; 4 config tests pass |
| 4 | `docker-compose.agent.yml` exists with exactly 4 services (worker, watcher, audfprint, panako); no Postgres/Redis service; file-server agents reach app-server Redis by URL (OPS-02) | VERIFIED | `docker-compose.agent.yml` confirmed; `sorted(services) = ['audfprint', 'panako', 'watcher', 'worker']`; worker image = `ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}`; 5 structural-parse tests pass including WARNING-3 SCAN_PATH failfast + WARNING-4 docker-publish tag check |
| 5 | Model auto-download: agent worker downloads essentia weights on empty /models; watcher does NOT download (OPS-03, WARNING-7) | VERIFIED | `src/phaze/scripts/download_models.py` (CLASSIFIER_MODELS=33, GENRE_MODELS=1); `src/phaze/tasks/_shared/model_bootstrap.py::ensure_models_present` called in `agent_worker.startup` after `whoami_with_retry` (line 101 of agent_worker.py); watcher `__main__` has documentation comment — no call; 6 model_bootstrap tests pass; `test_model_bootstrap_stays_postgres_free` subprocess test passes |
| 6 | Admin can see agent liveness at a glance: `/admin/agents` page with 5-state classification, 5s HTMX polling, failure-tolerant footer (OPS-04) | VERIFIED | `src/phaze/routers/admin_agents.py` registered in `main.py`; 5-state `classify()` + `sort_key()` in `agent_liveness.py` (20 tests pass); `relative_time()` in `utils/humanize.py` (31 tests pass); BLOCKER-2 HTMX error listener + `phaze:agents:lastError` localStorage + `role=alert` red footer confirmed in templates by grep gates; heartbeat cron `CronJob(heartbeat_tick, cron="* * * * * */30", ...)` registered in `agent_worker.py::settings`; 5 heartbeat tests pass |

**Score:** 6/6 truths verified

### Notes on human_needed status

The phase goal states "A **real two-host deployment** runs end-to-end." The operator executed Task 2 of Plan 08 with the `verified-docs-only` resume signal, accepting that the actual two-host smoke requires file-server hardware that was not available at time of verification. Per the instructions provided with this verification request, the real-deployment smoke is listed as a v4.0 outstanding UAT item rather than a phase-blocking gap. All automated verifications pass. Status is `human_needed` because the phase goal's "real" qualifier cannot be confirmed programmatically.

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/cert_bootstrap.py` | CA + leaf cert generation, Postgres-free | VERIFIED | 240 lines; exports `ensure_certs_present`; IMPORT-BOUNDARY INVARIANT docstring; 7 tests pass |
| `src/phaze/entrypoint.py` | Pre-uvicorn shim, reads env vars, calls ensure_certs_present, execvp uvicorn with TLS | VERIFIED | Exists; `main()` is public; docker-compose.yml api command confirmed |
| `src/phaze/config.py` | `api_tls_sans` on BaseSettings + `agent_ca_file` on AgentSettings + `agent_env` + `_enforce_redis_password_in_production` | VERIFIED | All 4 fields/validators confirmed by grep |
| `src/phaze/services/agent_client.py` | `verify=` kwarg on `PhazeAgentClient.__init__` (default True) | VERIFIED | `verify: ssl.SSLContext \| str \| bool = True` confirmed at line 125; threaded to httpx.AsyncClient |
| `src/phaze/tasks/_shared/agent_bootstrap.py` | `construct_agent_client` raises RuntimeError on missing/empty CA; passes `verify=cfg.agent_ca_file` | VERIFIED | Lines 60-68 confirmed |
| `docker-compose.yml` | App-server only: {api, worker, postgres, redis}; api command = entrypoint shim; Redis hardened; no file mounts | VERIFIED | Structural parse confirmed via YAML load + 4 passing pytest assertions |
| `docker-compose.agent.yml` | File-server: {worker, watcher, audfprint, panako}; GHCR image; SCAN_PATH failfast; no DATABASE_URL | VERIFIED | Structural parse confirmed; 5 passing tests |
| `.env.example` | REDIS_PASSWORD, REDIS_BIND_IP, PHAZE_API_TLS_SANS documented | VERIFIED | Confirmed in 29-03-SUMMARY.md + file content |
| `.env.example.agent` | All PHAZE_AGENT_* vars documented | VERIFIED | File exists with all vars; 75 lines |
| `src/phaze/scripts/download_models.py` | CLASSIFIER_MODELS=33, GENRE_MODELS=1, download_to(), atomic .part rename | VERIFIED | Runtime check: len=33, len=1; 6 tests pass |
| `src/phaze/tasks/_shared/model_bootstrap.py` | ensure_models_present(), Postgres-free | VERIFIED | Exists; subprocess import-boundary test passes |
| `src/phaze/tasks/agent_worker.py` | CronJob(heartbeat_tick, "* * * * * */30") + heartbeat_tick in functions list | VERIFIED | Lines 185-191 confirmed by grep |
| `src/phaze/tasks/heartbeat.py` | heartbeat_tick(ctx), fire-and-forget, queue_depth from ctx["worker"].queue | VERIFIED | 78 lines; 5 heartbeat tests pass |
| `src/phaze/constants.py` | AGENT_LIVENESS_ALIVE_SECONDS=90, AGENT_LIVENESS_STALE_SECONDS=300 | VERIFIED | Lines 52, 61 confirmed |
| `src/phaze/services/agent_liveness.py` | classify(), sort_key(), AgentStatus (5 states) | VERIFIED | 20 tests pass covering all 5 states |
| `src/phaze/utils/humanize.py` | relative_time() with truncation not rounding | VERIFIED | 31 tests pass including "89s ago" truncation case |
| `src/phaze/routers/admin_agents.py` | GET /admin/agents + GET /admin/agents/_table; no auth dep | VERIFIED | 132 lines; registered in main.py; no `get_authenticated_agent` |
| `src/phaze/templates/admin/agents.html` | BLOCKER-2: htmx:responseError + htmx:sendError + htmx:afterSwap listeners | VERIFIED | All 6 grep gates pass (listeners at lines 40-42, localStorage calls at lines 32, 37) |
| `src/phaze/templates/admin/partials/agents_table.html` | hx-get, hx-trigger="every 5s", hx-swap="outerHTML"; failure footer with role=alert | VERIFIED | hx-trigger at line 16; localStorage.getItem at line 87; role="alert" at line 85 |
| `src/phaze/templates/admin/partials/_status_pill.html` | 5-state pill with locked Tailwind classes | VERIFIED | All 5 states (alive/stale/dead/revoked/never) with aria-label confirmed |
| `src/phaze/templates/base.html` | Agents nav link between Audit Log and theme toggle, uses current_page == 'admin_agents' | VERIFIED | Link at lines 177-182; uses 'admin_agents' short slug matching WARNING-1 |
| `src/phaze/main.py` | include_router(admin_agents.router) | VERIFIED | Lines 16, 136 confirmed |
| `justfile` | up-agent + up-all recipes | VERIFIED | Lines 14-22 confirmed; `docker compose -f docker-compose.agent.yml up -d` |
| `docs/deployment.md` | 6-step walkthrough, D-20 filesystem smoke, CA rotation, 90+ lines | VERIFIED | 230 lines; 6 numbered steps confirmed; Filesystem-Isolation Smoke section present; CA Rotation section present |
| `.planning/PROJECT.md` | Deployment subsection with docker-compose.agent.yml reference | VERIFIED | Lines 137-145 confirmed |
| `tests/test_cert_bootstrap.py` | 7 test cases including WARNING-8 banner-via-logger | VERIFIED | 7 tests pass |
| `tests/test_services/test_agent_client_tls.py` | wrong-CA ConnectError + correct-CA success + 2 RuntimeError cases | VERIFIED | 4 tests pass |
| `tests/test_config/test_agent_settings_redis_password.py` | 4 tests covering prod/dev matrix | VERIFIED | 4 tests pass |
| `tests/test_deployment/test_api_filesystem_isolation.py` | 4 structural-parse tests (D-19) | VERIFIED | 4 tests pass |
| `tests/test_deployment/test_agent_compose.py` | 5 tests: service list, no-DB-env, PHAZE_ROLE, SCAN_PATH failfast, docker-publish tags | VERIFIED | 5 tests pass |
| `tests/test_tasks/test_heartbeat_cron.py` | 4 happy-path tests | VERIFIED | 4 tests pass |
| `tests/test_tasks/test_heartbeat_failure.py` | 1 AgentApiError WARNING-and-continue test | VERIFIED | 1 test passes |
| `tests/test_services/test_model_bootstrap.py` | 3 model-bootstrap tests | VERIFIED | 6 tests pass (3 original + 3 added) |
| `tests/test_task_split.py` | test_cert_bootstrap_stays_postgres_free + test_model_bootstrap_stays_postgres_free | VERIFIED | Both pass; 6 total subprocess tests pass |
| `tests/test_services/test_agent_liveness.py` | 5-state classify matrix + sort_key tests | VERIFIED | 20 tests pass |
| `tests/test_utils/test_humanize.py` | Parametrized 14+ boundary cases | VERIFIED | 31 tests pass |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `src/phaze/entrypoint.py` | `src/phaze/cert_bootstrap.py::ensure_certs_present` | `from phaze.cert_bootstrap import ensure_certs_present` | VERIFIED | Confirmed in 29-01-SUMMARY.md; entrypoint calls ensure_certs_present before execvp |
| `docker-compose.yml::api.command` | `src/phaze/entrypoint.py::main` | `uv run python -m phaze.entrypoint` | VERIFIED | `api.command = "uv run python -m phaze.entrypoint"` confirmed by YAML parse |
| `docker-compose.yml::redis.command` | REDIS_PASSWORD env var | `--requirepass ${REDIS_PASSWORD:?...}` | VERIFIED | `requirepass` and `REDIS_PASSWORD` both present in command list |
| `docker-compose.yml::api.volumes` | `./certs/` bind mount | `${CA_PATH:-./certs}:/certs:rw` | VERIFIED | Single volume entry confirmed |
| `src/phaze/tasks/_shared/agent_bootstrap.py::construct_agent_client` | `src/phaze/services/agent_client.py::PhazeAgentClient` | `verify=cfg.agent_ca_file` | VERIFIED | Line 68 of agent_bootstrap.py |
| `src/phaze/services/agent_client.py::PhazeAgentClient.__init__` | `httpx.AsyncClient` | `verify=verify` | VERIFIED | Line 147 of agent_client.py |
| `src/phaze/tasks/agent_worker.py::settings.cron_jobs` | `src/phaze/tasks/heartbeat.py::heartbeat_tick` | `CronJob(heartbeat_tick, cron="* * * * * */30", ...)` | VERIFIED | Line 191 of agent_worker.py |
| `src/phaze/tasks/agent_worker.py::startup` | `src/phaze/tasks/_shared/model_bootstrap.py::ensure_models_present` | `ensure_models_present(Path(cfg.models_path))` after whoami | VERIFIED | Line 101; imports at line 57; order confirmed (after whoami at line 95) |
| `src/phaze/routers/admin_agents.py` | `src/phaze/services/agent_liveness.py::classify` | `agent._status = classify(a, now)` transient injection | VERIFIED | Confirmed by admin_agents.py structure |
| `src/phaze/main.py` | `src/phaze/routers/admin_agents.py::router` | `app.include_router(admin_agents.router)` | VERIFIED | Lines 16, 136 confirmed |
| `src/phaze/templates/admin/partials/agents_table.html` | `GET /admin/agents/_table` | `hx-get="/admin/agents/_table"` | VERIFIED | Line 15 confirmed |
| `docker-compose.agent.yml::worker.image` | `ghcr.io/simplicityguy/phaze` GHCR published image | `image: ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}` | VERIFIED | docker-publish.yml extended to emit :latest + :v<version> tags; test_docker_publish_workflow_tags_both_latest_and_version passes |
| `justfile::up-agent` | `docker-compose.agent.yml` | `docker compose -f docker-compose.agent.yml up -d` | VERIFIED | Line 17 of justfile |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|--------------|--------|-------------------|--------|
| `src/phaze/routers/admin_agents.py` | `agents` list | `SELECT FROM agents` via `get_session` dependency | Yes — real asyncpg DB query per `_load_agents(session)` | FLOWING |
| `src/phaze/tasks/heartbeat.py::heartbeat_tick` | `queue_depth` | `ctx["worker"].queue.info()["queued"]` | Yes — live SAQ queue introspection; falls back to 0 on exception | FLOWING |
| `src/phaze/tasks/heartbeat.py::heartbeat_tick` | `agent_version` | `importlib.metadata.version("phaze")` | Yes — reads installed package version | FLOWING |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| cert_bootstrap generates valid parseable certs | `uv run pytest tests/test_cert_bootstrap.py -q` | 7 passed | PASS |
| Wrong CA rejects TLS (D-04) | `uv run pytest tests/test_services/test_agent_client_tls.py::test_wrong_ca_raises_connect_error -q` | 1 passed | PASS |
| Production Redis refuses passwordless URL | `uv run pytest tests/test_config/test_agent_settings_redis_password.py -q` | 4 passed | PASS |
| docker-compose.yml structural invariants | `uv run pytest tests/test_deployment/test_api_filesystem_isolation.py -q` | 4 passed | PASS |
| docker-compose.agent.yml structural invariants + publish tags | `uv run pytest tests/test_deployment/test_agent_compose.py -q` | 5 passed | PASS |
| Heartbeat cron fires at 30s trailing-seconds form | `uv run pytest tests/test_tasks/test_heartbeat_cron.py -q` | 4 passed | PASS |
| Postgres-free import boundaries | `uv run pytest tests/test_task_split.py -q` | 6 passed | PASS |
| Agent liveness 5-state classifier | `uv run pytest tests/test_services/test_agent_liveness.py -q` | 20 passed | PASS |
| All phase-29 automated tests | `uv run pytest` (92 phase-29 tests) | 92 passed | PASS |
| CLASSIFIER_MODELS count | `uv run python -c "from phaze.scripts.download_models import CLASSIFIER_MODELS; assert len(CLASSIFIER_MODELS)==33"` | 33 confirmed | PASS |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| DIST-01 | 29-03 | App-server has no SCAN_PATH/MODELS_PATH/OUTPUT_PATH mounts | SATISFIED | docker-compose.yml services {api,worker,postgres,redis}; api volumes = /certs:rw only; worker has no volumes; 4 structural tests pass |
| AUTH-02 | 29-01 | All agent→app-server traffic uses HTTPS via self-signed internal CA | SATISFIED | cert_bootstrap.py generates CA+leaf; entrypoint.py wires uvicorn TLS; agent_client verify= kwarg; wrong-CA ConnectError test passes |
| AUTH-03 | 29-02 + 29-03 | Redis requirepass + LAN binding; agents use password-bearing URL | SATISFIED | Server-side: compose redis hardened with requirepass+IP-bind; client-side: AgentSettings production validator refuses passwordless URLs |
| OPS-02 | 29-04 | docker-compose.agent.yml with {worker,watcher,audfprint,panako} | SATISFIED | File exists with exactly those 4 services; no Postgres; GHCR images; 5 structural tests pass |
| OPS-03 | 29-05 | File servers download models; app-server has no model mounts | SATISFIED | model_bootstrap.py auto-downloads on empty /models; agent_worker calls after whoami; watcher does NOT call (WARNING-7); app-server compose has no MODELS_PATH mount |
| OPS-04 | 29-06 + 29-07 | Agents post heartbeat every 30s; /admin/agents shows status | SATISFIED | heartbeat_tick SAQ cron "* * * * * */30"; /admin/agents with 5-state classify, HTMX polling, BLOCKER-2 failure footer; all tests pass |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| — | — | No TBD/FIXME/XXX debt markers found in phase-29 modified source files | — | — |
| `src/phaze/tasks/heartbeat.py` | line with broad except | `except Exception:` on queue.info() failure | Info | Intentional per D-09 defensive design; `# noqa: BLE001` if ruff flags; documented in heartbeat.py docstring; not a gap |

### Human Verification Required

#### 1. Real Two-Host Deployment Smoke

**Test:** Follow `docs/deployment.md` end-to-end on a real (or single-host simulated) two-host setup: bring up app-server with `just up`, copy CA cert to file-server, register agent via psql, populate `.env` on file-server, run `just up-agent`, wait for model download and first heartbeat.

**Expected:** Within 60 seconds of `just up-agent`, the `/admin/agents` page on the app-server shows the agent row with a green ALIVE pill. Worker logs show the GENERATED NEW PHAZE INTERNAL CA banner; model download INFO line; heartbeat sent log at DEBUG every 30s. Redis is reachable only from the LAN IP set in REDIS_BIND_IP. `docker compose exec api ls /data/music` returns "No such file or directory" on the app-server.

**Why human:** Real file-server hardware was not available at time of phase execution. The operator accepted the `verified-docs-only` resume signal (Plan 08 Task 2) deferring this to a v4.0 outstanding UAT item. This is not a gap in the implementation — all code, compose files, documentation, and automated tests are in place. The outstanding item is the physical hardware smoke test to confirm the documented sequence works end-to-end on real hosts.

---

### Gaps Summary

No blocking gaps. All 6 must-have truths are VERIFIED by codebase evidence. The `human_needed` status reflects a single UAT item (real two-host smoke) deferred by the operator's own decision with the `verified-docs-only` resume signal. The phase is feature-complete; the real-deployment smoke is the remaining v4.0 outstanding verification.

**Total tests added by Phase 29:** 92 phase-specific tests pass (7 cert_bootstrap + 4 TLS + 4 config + 4 deployment-isolation + 5 agent-compose + 5 heartbeat + 6 model_bootstrap + 6 task_split + 20 liveness + 31 humanize).

---

_Verified: 2026-05-17T00:09:50Z_
_Verifier: Claude (gsd-verifier)_
