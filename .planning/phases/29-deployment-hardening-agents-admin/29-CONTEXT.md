# Phase 29: Deployment Hardening & Agents Admin - Context

**Gathered:** 2026-05-15
**Status:** Ready for planning

<domain>
## Phase Boundary

Phase 29 finishes v4.0 by making the two-host deployment **real**: the application server holds no music/video filesystem mounts, all agent → application-server traffic is HTTPS with an internal CA, Redis is `requirepass` + LAN-bound, each file server runs `docker-compose.agent.yml` referencing a published GHCR image, and an operator can see at a glance which agents are alive on a new `/admin/agents` page.

Concretely, this phase delivers:

1. **Application-server compose hardening** — Strip `SCAN_PATH` / `MODELS_PATH` / `OUTPUT_PATH` volume mounts from `api` and `worker` (control) services in `docker-compose.yml`. The application-server stack ends up with `api`, `worker` (control role), `postgres`, `redis` only — no file-mount surface area. The existing `watcher` and `agent-worker` services move OUT of the root compose and into the new `docker-compose.agent.yml`.

2. **HTTPS via internal CA** — `uvicorn` terminates TLS directly (`--ssl-keyfile` + `--ssl-certfile`); no reverse-proxy service is added. The api container's entrypoint auto-generates a CA + leaf cert into a `./certs/` bind-mount on first start, logs a loud `=== GENERATED NEW PHAZE CA: copy ./certs/phaze-ca.crt to all file servers ===` banner so the operator knows distribution is needed. A new `PHAZE_AGENT_CA_FILE` env var on `AgentSettings` points each agent's `httpx.AsyncClient(verify=...)` at the operator-copied CA file. An integration test asserts a wrong-CA client → `httpx.ConnectError` / SSL error (success criterion #3 enforcement).

3. **Redis hardening** — Redis service starts with `--requirepass ${REDIS_PASSWORD}` and the compose `ports` mapping becomes `${REDIS_BIND_IP:-127.0.0.1}:6379:6379` so dev binds loopback and production sets the private LAN IP via `.env`. Agents receive `redis://default:${REDIS_PASSWORD}@${REDIS_HOST}:6379` and SAQ + heartbeat use the same URL. The watcher/worker bootstrap (D-22 here, mirrors Phase 27 D-16) refuses to start if the password is missing in production env.

4. **`docker-compose.agent.yml` standalone file** — File-server-host-only YAML declaring `worker` (agent role), `watcher`, `audfprint`, `panako`. Image references published GHCR (`ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}`); no `build:` block on the agent side — file server hosts don't need the source tree. Env shape: `PHAZE_API_URL` (HTTPS), `PHAZE_REDIS_URL`, `PHAZE_AGENT_TOKEN`, `PHAZE_AGENT_ID`, `PHAZE_AGENT_CA_FILE`, `PHAZE_AGENT_SCAN_ROOTS`, plus the watcher knobs from Phase 27 D-03.

5. **Models setup on each file server** — `just download-models` recipe exists today and writes to `./models`. Phase 29 adds an **auto-download-on-empty** check inside `phaze.tasks._shared.agent_bootstrap.ensure_models_present()`: if `/models` is empty (no `.pb` files), the agent_worker / watcher startup invokes the same model-fetch logic (extracted from `scripts/download-models.sh` into a Python helper) and blocks startup until weights land. On network failure the container exits non-zero → `restart: unless-stopped` retries. Application-server image neither downloads nor mounts models.

6. **Heartbeat caller wiring** — The existing `POST /api/internal/agent/heartbeat` endpoint (Phase 25) is unchanged. Phase 29 registers a 30-second SAQ cron entry inside `phaze.tasks.agent_worker.settings.cron_jobs` that calls `PhazeAgentClient.heartbeat(HeartbeatRequest(agent_version=..., worker_pid=os.getpid(), queue_depth=...))`. `agent_version` reads from `phaze.__version__` (or pyproject metadata). `queue_depth` reads from SAQ's `Queue.info()` for `phaze-agent-<agent_id>`. On HTTP failure after tenacity retries, log WARNING and continue — heartbeat is fire-and-forget.

7. **Agents admin page** — New `/admin/agents` route (sets up `admin/` as the home for future operational pages). HTMX poll every 5s; OOB-swap the table body. Columns: name, status pill (alive < 90s green / stale 90-300s amber / dead > 300s red / revoked grey), queue depth, last-seen relative (e.g., "23s ago"), scan_roots count, `actions` column slot (no buttons in Phase 29; placeholder for future revoke/rotate). Sort: revoked last, then by status (alive → stale → dead), then last-seen desc. Reads from `agents.last_seen_at` + `last_status` JSONB (already populated by the heartbeat endpoint).

Phase 29 does **NOT** introduce: mTLS (OPS-05 deferred), agent self-registration UI (OPS-06 deferred), Prometheus scrape (OPS-07 deferred), token rotation UI, scheduled re-execution, public-internet support, the scan_live_set artist/title regression fix (still deferred from Phase 26-11), nor any new Alembic migration (the `agents` table from Phase 24 + `Agent.last_status` from Phase 25 are sufficient).

</domain>

<decisions>
## Implementation Decisions

### HTTPS Termination & Internal CA (D-01..D-04)

- **D-01:** **`uvicorn`-direct TLS termination.** No reverse-proxy service added. The api command in `docker-compose.yml` becomes `uv run uvicorn phaze.main:app --host 0.0.0.0 --port 8000 --ssl-keyfile /certs/phaze-server.key --ssl-certfile /certs/phaze-server.crt`. Private LAN + single-user scale doesn't justify adding Caddy/nginx; future routing flexibility (HTTP/3, gzip, etc.) can be revisited if operational needs change.
- **D-02:** **Auto-generate CA + leaf cert in the api entrypoint on first start.** A new helper module `phaze.cert_bootstrap` (Postgres-free; importable from a thin entrypoint shim or from `phaze.main` startup) checks `/certs/phaze-ca.crt` and `/certs/phaze-server.crt`; if absent, generates a 10-year self-signed CA + a 2-year leaf cert (CN = `${PHAZE_API_HOST:-localhost}`, SAN list configurable via `PHAZE_API_TLS_SANS` env, default `localhost,127.0.0.1` plus the api container hostname). Files are written 0600 root:root into `/certs/` (bind-mounted from `./certs/`). The entrypoint logs a loud multi-line banner:
  ```
  ==============================================================
  GENERATED NEW PHAZE INTERNAL CA at /certs/phaze-ca.crt
  COPY THIS FILE TO EVERY FILE SERVER and point each agent's
  PHAZE_AGENT_CA_FILE env var at it. EXISTING AGENTS WILL FAIL
  TO CONNECT UNTIL THEY HAVE THIS NEW CA.
  ==============================================================
  ```
  If files already exist, the bootstrap is a no-op (idempotent). Operator can force-regenerate by `rm -rf ./certs/` and restarting api.
- **D-03:** **CA distribution via manual copy + `PHAZE_AGENT_CA_FILE` env var.** Operator scp/rsync's `./certs/phaze-ca.crt` from the application-server host to each file server during one-time setup. `AgentSettings` gains a new field `agent_ca_file: str = "/certs/phaze-ca.crt"` ← `PHAZE_AGENT_CA_FILE`. `PhazeAgentClient.__init__` passes it through as `httpx.AsyncClient(verify=settings.agent_ca_file, ...)`. A pre-existing-but-empty CA file results in an explicit `RuntimeError("CA file empty or unreadable: <path>")` at startup so misconfig surfaces fast.
- **D-04:** **CA swap test = pytest integration test.** New file `tests/test_services/test_agent_client_tls.py` constructs a `PhazeAgentClient(verify=<wrong-ca>)` against a respx-mocked HTTPS server that presents the real-CA cert, asserts that `client.whoami()` raises `httpx.ConnectError` (or `ssl.SSLCertVerificationError` depending on the wrapping). Plus a one-paragraph ops-doc step in `docs/deployment.md` (or the file-server README) describing the manual two-host swap smoke. CI + manual belt-and-braces.

### Redis Hardening (D-05, D-06)

- **D-05:** **Redis `requirepass` + LAN port binding.** `docker-compose.yml` redis service:
  ```yaml
  redis:
    image: redis:8-alpine
    command: ["redis-server", "--requirepass", "${REDIS_PASSWORD:?REDIS_PASSWORD required}"]
    ports:
      - "${REDIS_BIND_IP:-127.0.0.1}:6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "-a", "${REDIS_PASSWORD}", "ping"]
  ```
  The `:?REDIS_PASSWORD required` syntax fails fast at `docker compose up` if the password is unset. `${REDIS_BIND_IP:-127.0.0.1}` keeps dev on loopback; production sets it to the application server's private LAN IP in `.env`. `.env.example` documents both variables with empty defaults + comments explaining the production values.
- **D-06:** **Agent Redis URL shape.** Agents connect with `redis://default:${REDIS_PASSWORD}@${REDIS_HOST}:6379/${REDIS_DB:-0}`. `AgentSettings` already exposes `redis_url`; Phase 29 documents the production URL shape in `.env.example.agent` and the agent bootstrap rejects URLs without a password component when `PHAZE_ENV != "dev"` (new field `agent_env: Literal["dev","production"] = "dev"`; production refuses passwordless Redis URLs).

### Heartbeat Caller (D-07..D-10)

- **D-07:** **agent_worker SAQ process owns the heartbeat.** Watcher does NOT emit heartbeats (it has no queue + reporting `queue_depth=0` would race with worker's real numbers). If the worker dies and only the watcher is up, heartbeats stop → admin page shows "stale" → operator sees the right signal (file execution path is down).
- **D-08:** **SAQ cron job @ 30s.** Register a cron entry in `phaze.tasks.agent_worker.settings.cron_jobs`:
  ```python
  cron_jobs = [
      CronJob(heartbeat_tick, cron="*/30 * * * * *"),  # 6-field cron with seconds; SAQ supports it
  ]
  ```
  If SAQ's cron parser doesn't accept 6-field cron strings, fall back to an `asyncio.create_task` sleep-loop spawned in the agent_worker startup hook (D-discretion: planner picks based on SAQ version capability and notes it in the plan; the contract — "heartbeat fires every 30s while worker is alive" — is what matters). The cron handler reads `app.state.api_client` (already wired Phase 26 D-20) and `app.state.task_router.queue_for(agent_id)` (Phase 26 D-19) to construct the payload.
- **D-09:** **Heartbeat-fail policy: log WARNING + continue.** The `heartbeat_tick` body catches `AgentApiError` (any subclass), logs `logger.warning("heartbeat failed: %s", exc)`, and returns. SAQ retries the cron on the next tick. The application server sees `last_seen_at` stop advancing and the admin UI surfaces "stale" naturally. Mirrors Phase 28 D-16 fire-and-forget posture.
- **D-10:** **`queue_depth` from SAQ `Queue.info()`.** Use `queue.info()` (or the SAQ-version-equivalent method that returns pending-count) for `phaze-agent-<agent_id>`. Type-safe; no raw Redis access; survives SAQ internal key-layout changes. `agent_version` reads from `phaze.__version__` (sourced from `pyproject.toml` via `importlib.metadata`). `worker_pid` is `os.getpid()` inside the SAQ worker process.

### Agents Admin Page (D-11..D-14)

- **D-11:** **Route `/admin/agents`** in a new `src/phaze/routers/admin_agents.py` (sets up the `admin/` namespace for future operational pages). The router registers `GET /admin/agents` (page render) and `GET /admin/agents/_table` (HTMX partial swap target). Page extends the existing nav template (`templates/base.html` or equivalent); add a top-nav link "Agents" alongside Pipeline / Proposals / Duplicates.
- **D-12:** **Status thresholds.** Compute status from `agents.revoked_at`, `agents.last_seen_at`, and `now()`:
  - `revoked_at IS NOT NULL` → **revoked** (grey pill)
  - `last_seen_at IS NULL` → **never-seen** (grey pill, same styling as revoked) — agent registered but never heartbeated
  - `now() - last_seen_at < 90s` → **alive** (green)
  - `90s <= now() - last_seen_at < 300s` → **stale** (amber)
  - `now() - last_seen_at >= 300s` → **dead** (red)
  Thresholds are constants in `phaze.constants.AGENT_LIVENESS_*` so the test suite + UI render share one source of truth.
- **D-13:** **HTMX poll every 5s.** Matches Phase 27 D-08's pipeline dashboard cadence. Templates: `templates/admin/agents.html` (page shell) + `templates/admin/partials/agents_table.html` (poll target). Table row uses `hx-get="/admin/agents/_table"`, `hx-trigger="every 5s"`, `hx-swap="outerHTML"`. Terminal halt is not applicable — the table polls indefinitely while the page is open.
- **D-14:** **Columns + sort.** Display order: `name`, `status` (pill), `queue_depth` (from `last_status.queue_depth`), `last_seen` (relative, e.g., "23s ago"), `scan_roots_count` (`len(agent.scan_roots)`), `actions` (empty `<td>` placeholder column). Sort key: `(revoked_at IS NOT NULL, status_rank, -last_seen_at)` where `status_rank = {alive: 0, stale: 1, dead: 2, revoked: 3, never: 3}`. Agent name is the visible identity (no ID column; ID surfaces in a `title=...` tooltip for debugging).

### Compose Split & Image Strategy (D-15..D-18)

- **D-15:** **Standalone `docker-compose.agent.yml`** in repo root. Self-contained — operator runs `docker compose -f docker-compose.agent.yml up -d` on a file server, no other compose file needed. Declared services:
  ```yaml
  services:
    worker:        # agent role
      image: ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}
      command: uv run saq phaze.tasks.agent_worker.settings
      env_file: .env
      environment:
        - PHAZE_ROLE=agent
      volumes:
        - "${SCAN_PATH:?SCAN_PATH required}:/data/music:ro"
        - "${MODELS_PATH:-./models}:/models:rw"  # rw for auto-download (D-21)
        - "${CA_PATH:-./certs}:/certs:ro"
      restart: unless-stopped
    watcher:
      image: ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}
      command: uv run python -m phaze.agent_watcher
      env_file: .env
      environment:
        - PHAZE_ROLE=agent
      volumes:
        - "${SCAN_PATH:?SCAN_PATH required}:/data/music:ro"
        - "${CA_PATH:-./certs}:/certs:ro"
      restart: unless-stopped
    audfprint:
      # build kept — sidecars are file-server-local and don't ship via GHCR yet
      build:
        context: .
        dockerfile: services/audfprint/Dockerfile.audfprint
      volumes:
        - "${SCAN_PATH:?}:/data/music:ro"
        - audfprint_data:/data/fprint
      restart: unless-stopped
    panako:
      build:
        context: .
        dockerfile: services/panako/Dockerfile.panako
      volumes:
        - "${SCAN_PATH:?}:/data/music:ro"
        - panako_data:/data/fprint
      restart: unless-stopped
  volumes:
    audfprint_data:
    panako_data:
  ```
  The fingerprint sidecars stay on `build:` because they aren't published to GHCR (yet); file server still needs the `services/` subtree. A future quick-task could publish them to GHCR too.
- **D-16:** **Image tag pinning.** `${PHAZE_IMAGE_TAG:-latest}` with `latest` default so first-time operators get the most recent image without explicit setup, but the .env.example.agent comments strongly recommend pinning to a specific version tag (`PHAZE_IMAGE_TAG=v4.0.0`) for production rollouts. The existing docker-publish.yml workflow tags both `:latest` and `:v<version>`.
- **D-17:** **Root `docker-compose.yml` becomes app-server-only.** After Phase 29, root services = `api`, `worker` (control role), `postgres`, `redis`. Strip `SCAN_PATH` / `MODELS_PATH` / `OUTPUT_PATH` volume mounts from `api` and `worker`. Remove the `watcher` and `agent-worker` service blocks entirely (they live only in `docker-compose.agent.yml` now). The Phase 26-13 doc-sweep comment about "moves to docker-compose.agent.yml in Phase 29" resolves here.
- **D-18:** **Single-host dev convenience: `just up-all` recipe.** For developers running both roles on one machine, a new `just up-all` recipe runs `docker compose -f docker-compose.yml -f docker-compose.agent.yml up`. Operators on file-server hosts use `just up-agent` (just `docker compose -f docker-compose.agent.yml up`). Operators on the application server use `just up`. All three recipes land in `justfile`.

### Filesystem-Isolation Verification (D-19, D-20)

- **D-19:** **CI integration test for "api can't read music files".** New test `tests/test_deployment/test_api_filesystem_isolation.py`:
  1. Spins up `api` via `docker compose -f docker-compose.yml up -d api postgres redis` (or uses a respx-fronted in-process FastAPI app + asserts no volume mount declarations contain `SCAN_PATH` / `MODELS_PATH` / `OUTPUT_PATH` in the YAML — depends on planner choice; the YAML-parse variant is lighter and doesn't require Docker in CI).
  2. Asserts `/data/music`, `/models`, `/data/output` don't exist inside the api container (or aren't declared in any api service's volumes).
  3. As a stronger guard, asserts `phaze.tasks.controller.settings.functions` contains no file-bound task names (Phase 26 D-25 import-boundary test already covers part of this; D-19 extends it with a "no file mounts" YAML check).
  Planner picks the lightest implementation that gives a real signal.
- **D-20:** **Ops-doc smoke for two-host verification.** A paragraph in the new deployment doc (`docs/deployment.md` or extending an existing README) walks the operator through: (a) bring up app-server stack, (b) exec into `api` container, (c) confirm `/data/music` is empty/absent, (d) attempt `cat /data/music/anything.mp3` — expect "No such file or directory". Plain English, ~5 lines.

### Models Setup (D-21)

- **D-21:** **Auto-download on empty `/models` at agent_worker / watcher startup.** A new shared helper `phaze.tasks._shared.model_bootstrap.ensure_models_present(models_dir: Path) -> None`:
  1. Counts `.pb` files under `models_dir`.
  2. If zero, logs `INFO: /models is empty; downloading essentia weights (~150MB, this takes 2-5 min on first start)...`.
  3. Invokes the existing model-fetch logic — extracted from `scripts/download-models.sh` into a Python helper `phaze.scripts.download_models.download_to(target_dir)` so it's callable from both the bash script and the bootstrap. Idempotent (skips files that already exist with the right SHA-256).
  4. On network failure: raises `RuntimeError("Model download failed: <exc>")` → container exits non-zero → `restart: unless-stopped` retries (operator can intervene).
  5. On success: logs `INFO: Models present (%d weight files at /models)` and returns.

  `agent_worker.startup` calls `ensure_models_present(Path("/models"))` after whoami succeeds, before the SAQ worker starts pulling. `agent_watcher.__main__` calls it on startup too (watcher doesn't strictly need the weights but ensuring presence at watcher start gives faster failure feedback than waiting for the first analysis job). The volume mount is rw on the file-server side (D-15) so the container can write the weights; ro on the app-server side (irrelevant — app-server doesn't mount /models anymore).

### Test Surface (D-22)

- **D-22:** **New tests added in Phase 29:**
  - `tests/test_services/test_agent_client_tls.py` — D-04: wrong-CA → ConnectError; correct-CA → success.
  - `tests/test_cert_bootstrap.py` — D-02: generates CA + leaf on first call, no-op on second call, idempotent.
  - `tests/test_deployment/test_api_filesystem_isolation.py` — D-19: app-server compose declares no music/model/output mounts on api or controller worker.
  - `tests/test_deployment/test_agent_compose.py` — D-15..D-17: parses `docker-compose.agent.yml`, asserts services list is exactly `worker, watcher, audfprint, panako`, asserts no agent service has a Postgres connection env var, asserts the worker service has `PHAZE_ROLE=agent`.
  - `tests/test_tasks/test_heartbeat_cron.py` — D-07..D-10: register the cron job, fake-clock-tick 30s, assert one `client.heartbeat()` call with `worker_pid > 0`, `queue_depth >= 0`, `agent_version == phaze.__version__`. Mock SAQ Queue.info().
  - `tests/test_tasks/test_heartbeat_failure.py` — D-09: simulate `AgentApiServerError` from client.heartbeat(); assert WARNING logged, no exception escapes, next tick fires normally.
  - `tests/test_routers/test_admin_agents.py` — D-11..D-14: page render with 0/1/many agents, status-pill computation across all 5 states (alive/stale/dead/revoked/never), HTMX partial returns correctly when `HX-Request: true` header is set, sort order matches D-14.
  - `tests/test_services/test_model_bootstrap.py` — D-21: empty dir → downloads, populated dir → no-op, network failure → RuntimeError propagates.
  - `tests/test_config/test_agent_settings_redis_password.py` — D-06: passwordless redis_url + `agent_env=production` raises ValidationError; passwordless + `agent_env=dev` is allowed.
  - `tests/test_task_split.py` (existing, Phase 26 D-25) — extend with assertion that `phaze.cert_bootstrap` is Postgres-free (api entrypoint must remain import-light).

### Doc & Config Sweep (D-23)

- **D-23:** **Doc + .env touch at end of Phase 29** (single commit alongside the code):
  - `.env.example` — add `REDIS_PASSWORD`, `REDIS_BIND_IP`, `PHAZE_API_TLS_SANS` (with empty/loopback defaults + production-tuning comments).
  - `.env.example.agent` — NEW. File-server-host env template: `PHAZE_API_URL=https://<app-server-ip>:8000`, `PHAZE_REDIS_URL=redis://default:<password>@<app-server-ip>:6379/0`, `PHAZE_AGENT_ID`, `PHAZE_AGENT_TOKEN`, `PHAZE_AGENT_CA_FILE=/certs/phaze-ca.crt`, `PHAZE_AGENT_SCAN_ROOTS`, `PHAZE_IMAGE_TAG`, `SCAN_PATH`, `MODELS_PATH`, `CA_PATH`.
  - `docs/deployment.md` — NEW. Two-host setup walkthrough: (1) bring up app-server, (2) copy `./certs/phaze-ca.crt` to file server, (3) register agent + get token (psql snippet), (4) populate `.env.agent`, (5) run `just up-agent`, (6) verify on `/admin/agents` page.
  - `PROJECT.md` — append a "Deployment" subsection under Constraints noting that v4.0 production runs as two compose files (one per host), with HTTPS + Redis hardening as locked invariants.
  - `CLAUDE.md` — no change (deployment artifacts only; no new dev workflow knobs).
  - Per-service READMEs — `src/phaze/agent_watcher/README.md` (Phase 27 D-24 sets this up) gets a heartbeat note ("watcher does NOT emit heartbeats — that's the agent_worker"). `src/phaze/tasks/agent_worker/README.md` (if exists, otherwise skip) gets a section on the new cron heartbeat.
  - `scripts/update-project.sh` — touch if it lists services; add admin_agents router + cert_bootstrap module to the list per the memory rule on keeping it current.
  - `justfile` — add `just up`, `just up-agent`, `just up-all` recipes (D-18).

### Claude's Discretion

- Exact YAML field ordering in `docker-compose.agent.yml` (style only).
- Whether the cert bootstrap uses Python `cryptography` library or shells out to `openssl` — `cryptography` is already a transitive dep (FastAPI/Starlette), prefer it for type safety and Windows compatibility (not that we run on Windows, but it's cleaner).
- Exact relative-time formatting on the agents page (`"23s ago"`, `"4m ago"`, `"2h ago"`) — pick a small helper or use `humanize` if it's already pulled in.
- Status-pill colors (CSS class names) — match the existing project palette in `templates/base.html`. `bg-green-100 text-green-800` etc., Tailwind utility classes already in use.
- Whether the admin nav link goes between Pipeline and Proposals or to the far right. Far right is conventional for ops-oriented pages.
- 6-field cron support detection in SAQ — planner checks SAQ version and falls back to `asyncio.create_task` sleep-loop if 6-field cron isn't supported (D-08 explicitly allows this).
- Exact `phaze.cert_bootstrap` API shape (module-level function vs class). Pure-function recommended.
- Whether the CA + leaf cert generation uses RSA-3072 or P-256 ECDSA. ECDSA is faster + smaller; RSA-3072 is more compatible. Pick ECDSA P-256 unless something breaks.
- Where the "loud CA-generated banner" gets printed — stdout + the api startup log line. Both.
- Whether the agents page's "actions" column is empty or contains a `<button disabled title="coming in Phase 30">Revoke</button>` placeholder. Empty is cleaner; planner picks.
- Naming convention for the model-fetch helper module (`phaze.scripts.download_models` vs `phaze.tasks._shared.model_bootstrap`). The latter aligns with the existing `_shared` package; pick it.
- Whether `PHAZE_AGENT_CA_FILE` defaults to `/certs/phaze-ca.crt` (the in-container path) or is required. Default the in-container path; production sets it explicitly via .env.example.agent if a different mount point is used.
- Whether agents that have never registered display in the admin table or are hidden. Display — the row shows "never" status so the operator knows a token was minted but never used.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project & Milestone Context
- `.planning/PROJECT.md` — v4.0 milestone scope; "Per-agent bearer tokens with `agent_id` derived from token on the application server, never from request body, private LAN, self-signed HTTPS, Redis `requirepass` + LAN-bound interface" target-features line.
- `.planning/REQUIREMENTS.md` §"Topology & Boundary" — DIST-01 (no SCAN_PATH/MODELS_PATH on application server).
- `.planning/REQUIREMENTS.md` §"Authentication & Security" — AUTH-02 (HTTPS + internal CA), AUTH-03 (Redis requirepass + LAN binding).
- `.planning/REQUIREMENTS.md` §"Deployment & Operations" — OPS-02 (`docker-compose.agent.yml`), OPS-03 (per-file-server `just download-models`), OPS-04 (heartbeat + Agents admin page).
- `.planning/REQUIREMENTS.md` §"Future Requirements → Operational Polish" — OPS-05 (mTLS deferred), OPS-06 (agent self-registration UI deferred), OPS-07 (Prometheus deferred). Phase 29 explicitly does NOT touch these.
- `.planning/ROADMAP.md` §"Phase 29: Deployment Hardening & Agents Admin" — 6 success criteria.
- `.planning/STATE.md` §"Accumulated Context → Decisions" — locked v4.0 + Phase 24..28 invariants. Especially: Phase 27 D-19 ("Phase 29 will move watcher + worker to docker-compose.agent.yml"), Phase 26 D-04 ("agent worker moves to docker-compose.agent.yml in Phase 29"), Phase 28 dispatch decisions (no Phase 29 change to execution dispatch).

### Direct Predecessors (MUST read in full)
- `.planning/phases/24-schema-foundation-agent-registry/24-CONTEXT.md` (if present) — D-01 (agent.id kebab-case slug), D-04 (`agents.last_seen_at`, `agents.revoked_at` columns), D-09/D-11 (LIVE sentinel).
- `.planning/phases/25-internal-agent-http-api-bearer-auth/25-CONTEXT.md` (if present) — D-05 (auth dep `get_authenticated_agent`), D-17/D-19 (heartbeat endpoint contract — body shape `{agent_version, worker_pid, queue_depth}` `extra="forbid"`, returns 204), Phase 25 Plan 01 (`Agent.last_status` JSONB column added in migration 014).
- `.planning/phases/26-task-code-reorg-http-backed-agent-worker/26-CONTEXT.md` — D-03/D-25 (import-boundary invariant — Phase 29 extends it for `phaze.cert_bootstrap`), D-09..D-13 (PhazeAgentClient + tenacity retry policy — heartbeat caller reuses), D-14 (AgentSettings — Phase 29 adds `agent_ca_file`, `agent_env`), D-18/D-19 (`phaze-agent-<id>` queue naming + `AgentTaskRouter` — heartbeat reads queue depth via the router-owned Queue handle).
- `.planning/phases/27-watcher-service-user-initiated-scan/27-CONTEXT.md` — D-08 (HTMX poll-partial halt pattern — agents page reuses but never halts), D-15..D-17 (`phaze.agent_watcher` package + bootstrap), D-19 (root compose watcher block — Phase 29 deletes it), D-20 (cross-tenant guard placement).
- `.planning/phases/28-distributed-execution-dispatch/28-CONTEXT.md` — D-02 (Application server owns Redis hash writes exclusively — Phase 29 inherits this for the admin page's data reads), D-16 (fire-and-forget POST posture — Phase 29 heartbeat mirrors).

### Existing Code to Read Before Modifying

#### TLS termination + cert bootstrap (new surface)
- `src/phaze/main.py` — `create_app()`. Phase 29 adds an entrypoint shim (or directly inside main) that calls `phaze.cert_bootstrap.ensure_certs_present(Path("/certs"))` before uvicorn binds. Must remain Postgres-free until called from the right startup hook (Phase 26 D-03 invariant).
- `src/phaze/config.py` — `BaseSettings` + `ControlSettings` + `AgentSettings`. Phase 29 adds `agent_ca_file`, `agent_env`, `api_tls_sans` to the respective subclasses. The pydantic-settings `AliasChoices` pattern (Phase 26-01) is the template for `PHAZE_AGENT_CA_FILE` env mapping.
- `docker-compose.yml` — root file; Phase 29 strips volume mounts from api + worker, deletes watcher + agent-worker blocks, adds Redis requirepass + bind-IP, adds api TLS flags.
- `Dockerfile` — verify no `/data/music`, `/models`, `/data/output` ENV defaults that would mask the missing mounts at runtime. If present, scrub or move to compose env.

#### Redis hardening
- `src/phaze/config.py` — `redis_url` field. Phase 29 adds the production-validation hook (`agent_env=production` requires password).
- `src/phaze/tasks/agent_worker/startup.py` (or wherever Phase 26 D-16 lives) — bootstrap reads `AgentSettings`; Phase 29 extends with the password-required guard.
- `docker-compose.yml` redis block (lines ~118-126 today) — full rewrite per D-05.

#### Heartbeat caller (existing endpoint)
- `src/phaze/routers/agent_heartbeat.py` — UNCHANGED. Endpoint already exists from Phase 25; Phase 29 only adds the caller.
- `src/phaze/schemas/agent_heartbeat.py` — `HeartbeatRequest(agent_version, worker_pid, queue_depth)` `extra="forbid"`. UNCHANGED.
- `src/phaze/services/agent_client.py:340-345` — `PhazeAgentClient.heartbeat(payload: HeartbeatRequest) -> None` method exists. UNCHANGED.
- `src/phaze/tasks/agent_worker/settings.py` (Phase 26 D-10) — Phase 29 adds a `cron_jobs` entry pointing at the new `heartbeat_tick` task function.
- `src/phaze/tasks/agent_worker/__init__.py` or a new sibling module — Phase 29 lands `heartbeat_tick(ctx) -> None` here. Reads `ctx["api_client"]` + `ctx["agent_identity"]` + queue-depth via `ctx["task_router"].queue_for(agent_id).info()`.

#### Agents admin page (new surface)
- `src/phaze/main.py` — register the new `admin_agents` router.
- `src/phaze/templates/base.html` (or whichever shell template provides nav) — add Agents nav link.
- `src/phaze/templates/pipeline/dashboard.html` — Phase 27 D-08 HTMX poll pattern; Phase 29 mirrors for `templates/admin/partials/agents_table.html`.
- `src/phaze/templates/tracklists/partials/scan_progress.html` — existing HTMX poll partial; reference for cadence + structure.
- `src/phaze/models/agent.py` — `Agent.last_seen_at`, `Agent.last_status` JSONB, `Agent.revoked_at`, `Agent.scan_roots`. Read-only from the admin router.
- `src/phaze/routers/pipeline_scans.py` (Phase 27) — pattern for combining a router + HTMX partial + agent-dropdown rendering.

#### Models setup
- `scripts/download-models.sh` — existing 11-classifier × 3-variants fetch script. Phase 29 extracts the URL list + SHA-256 manifest into a Python helper so both bash and Python can drive the download.
- `src/phaze/services/analysis.py` (or wherever essentia model loading lives) — confirm the path lookup (`/models/...`) matches what the bootstrap writes.
- `justfile` — `download-models` recipe (existing); Phase 29 adds `up`, `up-agent`, `up-all` recipes per D-18.

#### Compose split
- `docker-compose.yml` — full audit + rewrite per D-15/D-17.
- `docker-compose.override.yml` — UNCHANGED (dev overlay for `--reload` + source mount).
- `services/audfprint/Dockerfile.audfprint` — UNCHANGED. Build context required by D-15.
- `services/panako/Dockerfile.panako` — UNCHANGED. Build context required by D-15.
- `.github/workflows/docker-publish.yml` (referenced by memory `[Discogsography reference]`) — publishes `ghcr.io/simplicityguy/phaze:latest` and version tags; Phase 29 docs reference this image URL.

### Configuration & Wiring
- `src/phaze/config.py` — new fields per D-03 (`agent_ca_file`), D-06 (`agent_env`), D-02 (`api_tls_sans`).
- `docker-compose.yml` (root) — rewrite per D-05, D-17.
- `docker-compose.agent.yml` (NEW) — per D-15.
- `.env.example` — additions per D-23.
- `.env.example.agent` (NEW) — file-server-host template per D-23.
- `pyproject.toml` — no new runtime deps (cryptography is already transitive; `httpx` already supports `verify=`).
- `justfile` — add `up`, `up-agent`, `up-all` recipes (D-18) and keep `download-models` recipe in sync with the extracted Python helper (D-21).
- `Dockerfile` — verify no MODELS/SCAN-related ENV defaults; if present, audit and remove per D-17.
- `CLAUDE.md` — unchanged; project conventions hold.

### Tests
- `tests/test_task_split.py` — Phase 26 D-25 import-boundary; Phase 29 adds the cert_bootstrap import-cleanliness case (D-22).
- `tests/test_routers/test_agent_heartbeat.py` (if it exists from Phase 25) — heartbeat-endpoint contract; Phase 29 does NOT modify, only references.
- All new tests under D-22.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`PhazeAgentClient.heartbeat()`** (`services/agent_client.py:340-345`) — Phase 25; method already exists. Phase 29 calls it from the cron handler.
- **`HeartbeatRequest`** (`schemas/agent_heartbeat.py`) — Phase 25; `extra="forbid"` with `{agent_version, worker_pid, queue_depth}`. Phase 29 caller MUST populate all three.
- **`POST /api/internal/agent/heartbeat`** (`routers/agent_heartbeat.py`) — Phase 25; updates `agents.last_seen_at` + `agents.last_status`. UNCHANGED in Phase 29.
- **`Agent.last_status` JSONB column** (`models/agent.py`, migration 014 from Phase 25 Plan 01) — admin page reads `queue_depth`, `worker_pid`, `agent_version` from here.
- **`AgentTaskRouter.queue_for(agent_id)`** (Phase 26 D-19; `services/agent_task_router.py`) — gives the SAQ Queue handle. Phase 29's heartbeat caller uses `queue_for(agent_id).info()` for queue depth (D-10).
- **`scripts/download-models.sh`** — existing model-fetch logic. Phase 29 extracts the URL list + SHA manifest into a Python helper callable from both bash and the agent bootstrap.
- **`just download-models` recipe** (`justfile`) — Phase 29 keeps it (operator can still pre-warm models manually) and adds the in-container auto-download fallback (D-21).
- **`templates/pipeline/dashboard.html` HTMX poll pattern** (Phase 27 D-08) — mirrored for the agents page (D-13).
- **`get_authenticated_agent`** (`routers/agent_auth.py`) — Phase 25 AUTH-01; admin page is operator-facing, not agent-facing — does NOT use this dep. The admin router uses whatever pattern other operator pages use (Pipeline router; no auth dep in Phase 27).
- **`phaze.tasks._shared.agent_bootstrap`** (Phase 27 D-17) — existing shared bootstrap. Phase 29 adds `phaze.tasks._shared.model_bootstrap` as a sibling module (D-21).
- **`tenacity` retry funnel** (Phase 26 D-11) — heartbeat caller wraps `client.heartbeat()` in the same retry decorator; 4xx-no-retry, 5xx-with-retry.

### Established Patterns
- **One router file per resource** — Phase 29 adds `routers/admin_agents.py`.
- **`APIRouter(prefix="/admin/agents", tags=["admin"])`** — new prefix; first "admin" router. Sets up the `tags=["admin"]` convention for future admin pages.
- **HTMX `hx-trigger="every 5s"` + `hx-swap="outerHTML"`** — Phase 27 D-08 / D-22 pattern; reused.
- **HTMX partial detection via `HX-Request` header** — STATE.md "Search UI: HTMX partial detection via truthy HX-Request header check" — reused.
- **Pydantic `extra="forbid"`** — every new schema; pattern unchanged.
- **`AliasChoices` per-field env mapping** — Phase 26-01; `PHAZE_AGENT_CA_FILE`, `PHAZE_AGENT_ENV`, `PHAZE_API_TLS_SANS`.
- **SAQ cron registration** — Phase 26 D-09 / D-10 settings modules; Phase 29 extends `agent_worker.settings.cron_jobs`.
- **Fire-and-forget POST posture** — Phase 28 D-16; heartbeat mirrors.
- **`restart: unless-stopped` as the only liveness mechanism for always-on agent processes** — Phase 27 D-19; preserved.

### Integration Points
- **0 new Alembic migrations** — `agents` table + `last_status` JSONB suffice.
- **0 new pip dependencies** — `cryptography` is already transitive via FastAPI/Starlette; `httpx.verify=` is built-in.
- **1 new internal-agent endpoint?** — NO. The heartbeat endpoint already exists from Phase 25; Phase 29 only adds the caller.
- **1 new admin-UI router** — `routers/admin_agents.py` with `GET /admin/agents` (page) + `GET /admin/agents/_table` (HTMX partial).
- **1 new bootstrap module** — `phaze.cert_bootstrap` (api-side CA + leaf generation).
- **1 new shared bootstrap module** — `phaze.tasks._shared.model_bootstrap` (agent-side models check).
- **1 new SAQ cron handler** — `heartbeat_tick` in `tasks/agent_worker/`.
- **1 new compose file** — `docker-compose.agent.yml`.
- **1 root compose rewrite** — strip mounts, drop watcher + agent-worker, add TLS + Redis hardening.
- **3 new AgentSettings fields** — `agent_ca_file`, `agent_env`, `api_tls_sans` (and root-level `redis_password`, `redis_bind_ip` are .env-only, not Settings).
- **2 new env example files** — `.env.example` additions + new `.env.example.agent`.
- **1 new deployment doc** — `docs/deployment.md`.
- **3 new justfile recipes** — `up`, `up-agent`, `up-all`.
- **~9 new test modules** (D-22).
- **2 new templates** — `templates/admin/agents.html` (page) + `templates/admin/partials/agents_table.html` (partial).
- **1 base-template nav edit** — Agents link.

### Constraints to Plan Around
- **Heartbeat endpoint contract is locked** — `{agent_version, worker_pid, queue_depth}` `extra="forbid"`. The caller MUST populate all three (D-10).
- **`cert_bootstrap` must run BEFORE uvicorn binds the TLS port** — chicken-and-egg: uvicorn needs the cert files to exist at command-line time. Bootstrap runs in a Docker entrypoint shim or as a pre-uvicorn startup module that exits if certs are absent and re-generates them. Planner picks the lightest implementation.
- **`cert_bootstrap` must NOT import `phaze.database`** — Phase 26 D-03 / D-25 import-boundary invariant. Test extended (D-22).
- **Heartbeat caller cannot block the SAQ event loop** — 30s tick must `await` the HTTP POST, not block. tenacity wraps cleanly.
- **`/admin/agents` is operator-facing** — no `get_authenticated_agent` dep; uses whatever operator-pages convention is in place (none currently — Pipeline router is open). Planner notes if a future phase needs operator auth, this router gets retrofitted.
- **`docker-compose.agent.yml` has no Postgres service** — agents must not reach Postgres. Confirmed by D-22 test.
- **CA regeneration is destructive** — operator who `rm -rf certs/` and restarts api unknowingly breaks every agent until they re-distribute the CA. The loud banner (D-02) is the only safeguard. Acceptable for personal-collection scale.
- **GHCR `:latest` default in docker-compose.agent.yml** — pragmatic for first-time setup but production operators should pin (D-16 comment in .env.example.agent).
- **Models auto-download blocks startup** — first agent-worker boot on a fresh file server takes 2-5 minutes (~150MB). Documented in deployment.md; operator can pre-warm with `just download-models` for instant startup.
- **Watcher does NOT emit heartbeats** (D-07) — if the worker is down but the watcher is up, the agent looks "stale" in the admin UI. That's the desired signal.
- **Cross-host time skew** — heartbeat liveness math uses the application server's `now()` against `last_seen_at` (server-stamped). No clock skew concern from the file server side.

</code_context>

<specifics>
## Specific Ideas

- **CA banner copy** — multi-line stdout printout per D-02, surfaced from `phaze.cert_bootstrap` via plain `print()` AND `logger.warning()` so it lands in both `docker compose logs api` and `docker compose up` foreground output.
- **Heartbeat cron handler** — small async function that wraps `client.heartbeat(HeartbeatRequest(...))` in a try/except (catches `AgentApiError` only — not bare `Exception`, so coding bugs still bubble up). One log line per success (DEBUG) and per failure (WARNING).
- **Admin page status pill colors** — match the existing Tailwind palette: `bg-green-100 text-green-800` (alive), `bg-amber-100 text-amber-800` (stale), `bg-red-100 text-red-800` (dead), `bg-gray-100 text-gray-800` (revoked / never). The class names come from existing templates so style consistency is automatic.
- **Relative time formatting** — single helper `phaze.utils.humanize.relative_time(dt: datetime) -> str` that returns "23s ago", "4m ago", "2h ago", "3d ago". Pure Python, no extra dep. Used by the agents table.
- **`docker-compose.agent.yml` env-var failure mode** — `${SCAN_PATH:?SCAN_PATH required}` and `${REDIS_PASSWORD:?REDIS_PASSWORD required}` (compose-native fail-fast). Operator gets a clear "var unset" message on first `docker compose up`.
- **CI test pragmatism** — D-19's filesystem-isolation test should prefer the YAML-parse variant over a real Docker-compose-up to keep CI fast. Parse `docker-compose.yml`, find services `api` + `worker`, assert no volume entries contain `/data/music`, `/models`, `/data/output`. Real Docker exec is overkill for a structural assertion.
- **Cron 6-field detection** — `from saq import CronJob; CronJob.__init__` signature inspection or version check. If sub-minute cron isn't supported, the fallback is `asyncio.create_task(_heartbeat_loop(api_client, queue, interval=30))` registered in the SAQ `startup` hook — 5 lines and avoids the cron-syntax dependency entirely. Planner picks based on actual SAQ behavior.
- **`PHAZE_AGENT_ID` is already part of agent bootstrap** (Phase 26 / 27) — Phase 29's `.env.example.agent` documents it but no new code reads it for the first time.
- **The agents page lists ALL agents** including never-seen-heartbeated ones — Phase 24 D-09's sentinel ScanBatch is created at registration time, so a freshly minted agent appears in the table immediately with status="never" until its worker boots and sends the first heartbeat.
- **Model auto-download bootstrap is best invoked AFTER whoami** — if the agent's token is wrong or app server is unreachable, fail fast on auth before spending 5 minutes downloading 150MB of weights. Order: `whoami_with_retry()` → `ensure_models_present()` → SAQ worker start.
- **Single-host dev with TLS** — local dev can use the auto-generated CA against `localhost`/`127.0.0.1` SANs (D-02 default). The agent containers running on the same docker network point at `https://api:8000` and the cert SAN list includes `api` (the service-name DNS resolves inside the docker network). Planner ensures the default `PHAZE_API_TLS_SANS` includes `localhost,127.0.0.1,api`.
- **CA file permissions** — `0644` on the public CA cert (it's distributed; not secret); `0600` on the leaf private key. Bootstrap sets both explicitly.
- **`tests/test_deployment/`** is a NEW test directory. Add `__init__.py` if pytest config requires it.
- **`tests/test_config/test_agent_settings_redis_password.py`** uses the production-mode validator from D-06; this is the only Phase 29 config-validation test.

</specifics>

<deferred>
## Deferred Ideas

- **Reverse-proxy fronting (Caddy / nginx / Traefik)** — Phase 29 chose uvicorn-direct TLS. A future deployment phase can swap to a reverse proxy if HTTP/3, gzip, multi-host routing, or auto-cert-rotation becomes a real need.
- **mTLS for agent boundary** — REQUIREMENTS.md OPS-05; explicitly deferred. Bearer token over TLS is sufficient for v4.0 private LAN.
- **Agent self-registration UI** — REQUIREMENTS.md OPS-06; operator pre-seeds tokens via psql. Future phase can add `/admin/agents/new` form + token-generation flow.
- **Prometheus metrics scrape endpoint** — REQUIREMENTS.md OPS-07.
- **Per-agent revoke / rotate-token buttons** — D-14 leaves the `actions` column as a placeholder. Future phase adds the buttons + the audit-logged endpoint.
- **Automated CA rotation** — Phase 29 generates a 10-year CA. Future ops phase can add a `just rotate-ca` recipe + an in-page "Rotate CA" admin button.
- **Agent-side TLS cert (mTLS prep)** — Currently agents authenticate with bearer tokens, not client certs. The cert infrastructure built in Phase 29 is server-only.
- **scan_live_set artist/title resolution rewrite** — STATE.md Phase 26-11 / Phase 27 deferred; Phase 29 does NOT pick it up.
- **GHCR publishing for audfprint + panako sidecars** — Phase 29 keeps `build:` for those services in `docker-compose.agent.yml`. Future quick-task can extend the docker-publish workflow to publish them too.
- **Multi-agent token-rotation orchestration** — rotating all agent tokens at once (e.g., compliance event) would require an admin tool. Manual psql is fine for now.
- **Hierarchical Redis ACLs (per-agent Redis user)** — Phase 29 uses a single shared password. Future hardening phase can split per-agent ACLs.
- **Watcher heartbeat (separate liveness for the watcher process)** — Phase 29 routes liveness through the worker only. A future phase could add a `last_watcher_seen_at` column + a separate watcher-emitted heartbeat if the operator needs per-process liveness.
- **`/admin/agents/{id}` detail page** — Phase 29 ships only the index page. Drill-down to per-agent recent jobs / heartbeats / queue-depth chart is a future enhancement.
- **Liveness-driven dispatch filter** — Phase 28's dispatch filter excludes `revoked_at` agents. Future enhancement: also exclude `status=dead` agents so a permanently-offline agent doesn't accept work. Today the dispatch queues to a dead agent's Redis queue and the jobs sit until the agent comes back.
- **`docker-compose.dev.yml` overlay** — Phase 29 leaves `docker-compose.override.yml` unchanged for `--reload` dev. A future quick-task could clean up dev compose ergonomics.
- **Model-download progress bar in the admin UI** — Phase 29 auto-download logs to stdout only. UI surfacing is a polish item.
- **CA-expiry alerting** — Phase 29 generates a 10-year CA. No alerting if it expires. Future ops phase.
- **Reverse-proxy-based 401 -> login redirect** — for the operator UI. Phase 29 leaves the operator UI open (single-user home server). Future phase if multi-user.

</deferred>

---

*Phase: 29-deployment-hardening-agents-admin*
*Context gathered: 2026-05-15*
