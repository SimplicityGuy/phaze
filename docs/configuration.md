<!-- generated-by: gsd-doc-writer -->
# Configuration

All configuration is via environment variables (or a `.env` file). See [`.env.example`](../.env.example) for the operator-facing defaults.

The canonical source of truth is [`src/phaze/config.py`](../src/phaze/config.py), a [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) hierarchy.

## How settings are loaded

Phaze splits settings into a shared `BaseSettings` class plus two role-specific subclasses, selected at process boot by the `PHAZE_ROLE` env var:

| `PHAZE_ROLE` | Settings class    | Role                                                                 |
|--------------|-------------------|---------------------------------------------------------------------|
| `control` (default) | `ControlSettings` | Application server: LLM proposals, Discogs matching, fileless tasks |
| `agent`      | `AgentSettings`   | File server: HTTP client to the app server, file-bound SAQ tasks    |

`get_settings()` (cached via `lru_cache`) is the single dispatch point. A module-level `settings = ControlSettings()` singleton is preserved for back-compat with existing `from phaze.config import settings` call sites; agent entry points call `get_settings()` / `AgentSettings()` directly.

**Env var binding:** most fields bind to the uppercased field name (e.g., `scan_path` ← `SCAN_PATH`). Several fields are bound to an explicit `PHAZE_*` alias via `validation_alias=AliasChoices(...)`, in which case the `PHAZE_*` form is the documented operator-facing name and the bare name still works for in-process / test convenience. Both forms are listed below where they differ.

## Core settings (all roles)

| Variable                          | Required | Default                                                  | Description                                                                 |
|-----------------------------------|----------|----------------------------------------------------------|-----------------------------------------------------------------------------|
| `PHAZE_ROLE`                      | No       | `control`                                                | Selects the settings subclass: `control` or `agent`.                        |
| `PHAZE_DATABASE_URL` (or `DATABASE_URL`) | No | `postgresql+asyncpg://phaze:phaze@postgres:5432/phaze`    | PostgreSQL connection string. Use `localhost` when running on the host instead of in Compose. |
| `PHAZE_REDIS_URL` (or `REDIS_URL`)| No       | `redis://redis:6379/0`                                    | Redis connection string (SAQ broker). In production agent mode, a password is required (see Per-environment overrides). |
| `DEBUG`                           | No       | `false`                                                  | Enable debug mode.                                                          |
| `API_HOST`                        | No       | `0.0.0.0`                                                | API server bind address.                                                    |
| `API_PORT`                        | No       | `8000`                                                   | API server port.                                                            |
| `SCAN_PATH`                       | No       | `/data/music`                                            | Music directory mounted for scanning.                                       |
| `MODELS_PATH`                     | No       | `/models` (config default; `.env.example` uses `./models`) | Essentia audio-analysis model directory. Run `just download-models` to populate. |
| `OUTPUT_PATH`                     | No       | `/data/output`                                           | Destination directory for executed file moves.                             |

## Worker / task queue settings (all roles)

| Variable                       | Required | Default | Description                                          |
|--------------------------------|----------|---------|------------------------------------------------------|
| `WORKER_MAX_JOBS`              | No       | `8`     | Concurrent SAQ jobs per worker.                      |
| `WORKER_JOB_TIMEOUT`          | No       | `600`   | Per-job timeout in seconds.                          |
| `WORKER_MAX_RETRIES`          | No       | `4`     | Max attempts per job (1 initial + 3 retries).        |
| `WORKER_PROCESS_POOL_SIZE`    | No       | `4`     | CPU-bound process pool size.                         |
| `WORKER_HEALTH_CHECK_INTERVAL`| No       | `60`    | SAQ health-check interval in seconds.                |
| `WORKER_KEEP_RESULT`          | No       | `3600`  | Seconds SAQ retains a finished job's result.         |

## Fingerprint service settings (all roles)

The fingerprint sidecars are validated to live on the agent's local Compose network only — `audfprint_url`/`panako_url` must resolve to `localhost`, `127.0.0.1`, `audfprint`, or `panako`. Cross-file-server fingerprint matching is not supported in v4.0.

| Variable        | Required | Default                 | Description                            |
|-----------------|----------|-------------------------|----------------------------------------|
| `AUDFPRINT_URL` | No       | `http://audfprint:8001` | Audfprint fingerprint service endpoint.|
| `PANAKO_URL`    | No       | `http://panako:8002`    | Panako fingerprint service endpoint.   |

## Internal agent API settings (all roles)

| Variable               | Required | Default          | Description                                                   |
|------------------------|----------|------------------|---------------------------------------------------------------|
| `AGENT_TOKEN_PREFIX`   | No       | `phaze_agent_`   | Required prefix for agent bearer tokens.                      |
| `AGENT_FILE_CHUNK_MAX` | No       | `1000`           | Max file records per chunk in the internal agent API.         |

## Bring-up settings (all roles)

| Variable                  | Required | Default | Description                                                                                   |
|---------------------------|----------|---------|-----------------------------------------------------------------------------------------------|
| `PHAZE_AUTO_MIGRATE`      | No       | `true`  | Run `alembic upgrade head` in the api lifespan startup. Set `false` in production to gate migrations behind a maintenance window. |
| `PHAZE_DEV_SEED_AGENT`    | No       | `false` | On a fresh `agents` table, seed a single dev-agent row so the watcher can authenticate on first start. Keep `false` in production. |
| `PHAZE_DEV_AGENT_TOKEN`   | No       | (random)| Optional fixed bearer for the dev-seeded agent. If unset, the api generates a random one and logs it at INFO. Format: `phaze_agent_<32 urlsafe-base64 bytes>`. |

## HTTPS / internal CA settings (Phase 29)

The application server generates a self-signed CA + leaf certificate pair into the certs directory on first startup (idempotent). The pre-uvicorn entrypoint ([`src/phaze/entrypoint.py`](../src/phaze/entrypoint.py)) reads three env vars directly (it must not load `phaze.config`):

| Variable             | Required | Default                       | Description                                                                 |
|----------------------|----------|-------------------------------|-----------------------------------------------------------------------------|
| `PHAZE_CERTS_DIR`    | No       | `/certs`                      | Directory the cert bootstrap writes to and uvicorn loads TLS material from (bind-mount target). |
| `PHAZE_API_HOST`     | No       | `localhost`                   | CN baked into the auto-generated leaf certificate.                          |
| `PHAZE_API_TLS_SANS` | No       | `localhost,127.0.0.1,api`     | Comma-separated SAN list for the leaf cert. Production should add the app server's LAN hostname / IP. |

`PHAZE_API_TLS_SANS` is also a `BaseSettings` field (`api_tls_sans`) so other parts of the app can read the same value.

## Control role settings (`PHAZE_ROLE=control`)

These fields exist only on `ControlSettings` (the application server).

### LLM / litellm settings

| Variable                  | Required | Default                      | Description                                       |
|---------------------------|----------|------------------------------|---------------------------------------------------|
| `LLM_MODEL`               | No       | `claude-sonnet-4-20250514`   | LLM model used for filename/path proposals.       |
| `ANTHROPIC_API_KEY`       | No*      | (none)                       | Anthropic API key (`SecretStr`). Required only if using an Anthropic model. |
| `OPENAI_API_KEY`          | No*      | (none)                       | OpenAI API key (`SecretStr`). Required only if using an OpenAI model. |
| `LLM_MAX_RPM`             | No       | `30`                         | Max LLM requests per minute.                      |
| `LLM_BATCH_SIZE`          | No       | `10`                         | Files per LLM batch call.                         |
| `LLM_MAX_COMPANION_CHARS` | No       | `3000`                       | Max characters of companion-file content sent per file. |

\* Neither key is required by the config schema, but at least one matching the selected `LLM_MODEL` provider is needed to generate proposals at runtime.

### Discogs settings

| Variable                    | Required | Default                       | Description                          |
|-----------------------------|----------|-------------------------------|--------------------------------------|
| `DISCOGSOGRAPHY_URL`        | No       | `http://discogsography:8000`  | Discogsography service endpoint.     |
| `DISCOGS_MATCH_CONCURRENCY` | No       | `5`                           | Concurrent Discogs match tasks.      |

## Agent role settings (`PHAZE_ROLE=agent`)

These fields exist only on `AgentSettings` (the file server). When `PHAZE_ROLE=agent`, a model validator fails fast at startup if any **required** field is missing.

### Required agent fields

| Variable                                      | Required | Default | Description                                                                 |
|-----------------------------------------------|----------|---------|-----------------------------------------------------------------------------|
| `PHAZE_AGENT_API_URL` (or `AGENT_API_URL`)    | **Yes**  | (empty) | Base URL of the application server (e.g., `http://api:8000` in Compose). In `production` mode this must be `https://`. |
| `PHAZE_AGENT_TOKEN` (or `AGENT_TOKEN`)        | **Yes**  | (empty) | Bearer token (`SecretStr`) issued at agent registration. Must match the stored hash in the `agents` table. Format: `phaze_agent_<32 urlsafe-base64 bytes>`. |
| `PHAZE_AGENT_SCAN_ROOTS` (or `SCAN_ROOTS`)    | **Yes**  | (empty) | Comma-separated list of absolute paths the agent may read/write, used for path-traversal containment (e.g., `/data/music,/data/concerts`). |
| `PHAZE_AGENT_QUEUE` (or `AGENT_QUEUE`)        | **Yes**  | (empty) | SAQ queue the agent worker consumes. By convention it MUST equal `phaze-agent-<PHAZE_AGENT_ID>`. There is **no queue column** on the `agents` table: both the control plane and the agent worker derive the queue name from the agent_id. At startup `phaze.tasks.agent_worker` resolves the agent_id from the token via `/whoami` and asserts `PHAZE_AGENT_QUEUE == f"phaze-agent-{agent_id}"`, exiting non-zero on mismatch. Use the exact value printed by `phaze agents add` (see [deployment.md](deployment.md) Step 3). |

### Optional agent fields

| Variable                                          | Required | Default              | Description                                                                 |
|---------------------------------------------------|----------|----------------------|-----------------------------------------------------------------------------|
| `PHAZE_AGENT_ENV` (or `AGENT_ENV`)                | No       | `dev`                | Deployment mode: `dev` or `production`. `production` enforces `https://` agent URL and a passworded Redis URL. |
| `PHAZE_AGENT_CA_FILE` (or `AGENT_CA_FILE`)        | No       | `/certs/phaze-ca.crt`| Path to the operator-distributed CA cert the agent's HTTP client uses to verify the app-server TLS endpoint. |
| `PHAZE_WATCHER_SETTLE_SECONDS` (or `WATCHER_SETTLE_SECONDS`) | No | `10` | Seconds a file's mtime must be stable before the watcher posts it.          |
| `PHAZE_WATCHER_MAX_PENDING_SECONDS` (or `WATCHER_MAX_PENDING_SECONDS`) | No | `3600` | Stuck-file cap; pending entries older than this are evicted without posting.|
| `PHAZE_WATCHER_SWEEP_INTERVAL_SECONDS` (or `WATCHER_SWEEP_INTERVAL_SECONDS`) | No | `2` | How often the watcher's sweep task checks for settled files.               |
| `PHAZE_WATCHER_POLLING_MODE` (or `WATCHER_POLLING_MODE`) | No | `false` | Use watchdog's `PollingObserver` instead of native inotify. Required for macOS Docker bind mounts where inotify events do not propagate. |
| `PHAZE_SCAN_CHUNK_SIZE` (or `SCAN_CHUNK_SIZE`)    | No       | `500`                | Number of file-upsert rows per chunk in `scan_directory`.                   |

## Docker Compose-only variables

These are consumed by the Compose stack (`docker-compose.yml`, `docker-compose.agent.yml`), not by `phaze.config`.

| Variable           | Required | Default       | Description                                                                 |
|--------------------|----------|---------------|-----------------------------------------------------------------------------|
| `POSTGRES_USER`    | No       | `phaze`       | PostgreSQL superuser for the `postgres` service.                            |
| `POSTGRES_PASSWORD`| No       | `phaze`       | PostgreSQL password for the `postgres` service.                            |
| `POSTGRES_DB`      | No       | `phaze`       | PostgreSQL database name created on first boot.                            |
| `REDIS_PASSWORD`   | **Yes**  | (none)        | Password for `redis-server --requirepass`. Compose fails at parse time if unset (`${REDIS_PASSWORD:?...}`). `.env.example` ships a `changeme` placeholder for dev. |
| `REDIS_BIND_IP`    | No       | `127.0.0.1`   | Host interface to bind Redis `:6379` on. Production overrides to a LAN IP so off-host agents can connect. |
| `UID`              | No       | `1000`        | Host user ID for volume permissions.                                       |
| `GID`              | No       | `1000`        | Host group ID for volume permissions.                                      |
| `CA_PATH`          | No       | `./certs`     | Host path bind-mounted read-only to `/certs` in agent containers (operator-distributed CA cert). |
| `PHAZE_IMAGE_TAG`  | No       | `latest`      | GHCR image tag pulled by `docker-compose.agent.yml` (e.g., `v4.0.0`).      |

## Config file format

Phaze has no JSON/YAML/TOML application config file. All runtime configuration flows through environment variables (loaded from a `.env` file via pydantic-settings, `env_file=".env"`). Unknown env vars are ignored (`extra="ignore"`).

A minimal `.env` for a single-host dev bring-up:

```bash
# Database + Redis (Docker service names)
DATABASE_URL=postgresql+asyncpg://phaze:phaze@postgres:5432/phaze
REDIS_URL=redis://redis:6379/0
REDIS_PASSWORD=changeme

# App
SCAN_PATH=/data/music
MODELS_PATH=./models

# Dev agent bring-up (so the watcher can authenticate on a fresh DB)
PHAZE_DEV_SEED_AGENT=true
PHAZE_AGENT_API_URL=http://api:8000
PHAZE_AGENT_TOKEN=phaze_agent_<token from `docker compose logs api`>
PHAZE_AGENT_SCAN_ROOTS=/data/music
```

## Required vs optional settings

Almost every field has a safe default so a fresh clone runs with `docker compose up`. The settings that cause a **fail-fast at startup** if missing or misconfigured:

- **Agent role (`PHAZE_ROLE=agent`)** — `PHAZE_AGENT_API_URL`, `PHAZE_AGENT_TOKEN`, and `PHAZE_AGENT_SCAN_ROOTS` are all required. The `_enforce_required_agent_fields` model validator raises `ValueError` at construction if any is empty.
- **Redis password (Compose)** — `REDIS_PASSWORD` must be set or `docker compose` aborts at parse time (`${REDIS_PASSWORD:?REDIS_PASSWORD required}`).
- **Fingerprint URLs** — `AUDFPRINT_URL` / `PANAKO_URL` are rejected unless their host is `localhost`, `127.0.0.1`, `audfprint`, or `panako`.

## Defaults

Defaults are defined in `src/phaze/config.py`. Highlights:

- `database_url` → `postgresql+asyncpg://phaze:phaze@postgres:5432/phaze`
- `redis_url` → `redis://redis:6379/0`
- `api_host` → `0.0.0.0`, `api_port` → `8000`
- `scan_path` → `/data/music`, `output_path` → `/data/output`, `models_path` → `/models`
- `worker_max_jobs` → `8`, `worker_job_timeout` → `600`, `worker_max_retries` → `4`
- `llm_model` → `claude-sonnet-4-20250514`, `llm_max_rpm` → `30`, `llm_batch_size` → `10`
- `agent_env` → `dev`, `agent_ca_file` → `/certs/phaze-ca.crt`
- `watcher_settle_seconds` → `10`, `watcher_sweep_interval_seconds` → `2`, `scan_chunk_size` → `500`

## Per-environment overrides

There are no `.env.development` / `.env.production` files; environment selection is explicit:

- **Host vs container connection strings** — `.env.example` defaults to the Docker service names `postgres` / `redis`. When running a service directly on the host with `uv run`, switch `DATABASE_URL` and `REDIS_URL` to `localhost` (or an SSH tunnel to the home server).
- **Agent dev vs production** — set `PHAZE_AGENT_ENV=production` on agents. This activates two guards:
  - `_enforce_https_in_production` — `agent_api_url` must start with `https://`, otherwise the bearer token travels in cleartext.
  - `_enforce_redis_password_in_production` — `redis_url` must contain a password, paired with the server-side `--requirepass` + LAN-bound port hardening. `dev` (default) permits passwordless Redis so a fresh clone works without extra ceremony.
- **Redis exposure** — keep `REDIS_BIND_IP=127.0.0.1` in dev; set it to the app server's LAN IP in production so agents on other hosts can reach Redis.
- **TLS SANs** — extend `PHAZE_API_TLS_SANS` with the app server's production LAN hostname / IP so agents can verify the TLS handshake.
- **Migrations** — set `PHAZE_AUTO_MIGRATE=false` in production to run Alembic migrations manually during a maintenance window.
- **Agent images** — `docker-compose.agent.yml` pulls `ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}`; pin `PHAZE_IMAGE_TAG` (e.g., `v4.0.0`) per deployment.
