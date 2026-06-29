<!-- generated-by: gsd-doc-writer -->
# Phaze v4.0 Deployment Guide

Production deployment of Phaze v4.0 (Distributed Agents) runs as **two compose files on two (or more) hosts**:

- **Application server** (`docker-compose.yml`): API/UI, controller worker, Postgres, Redis. No music/model/output file mounts. HTTPS via an internal CA. Redis `requirepass` + LAN binding.
- **File servers** (`docker-compose.agent.yml`, one per host): agent worker, watcher, and the `audfprint` + `panako` fingerprint sidecars. Holds music/video files locally; reaches the app-server over HTTPS for every state change.

This guide walks through bringing up a fresh two-host deployment from a clean checkout, then covers the build pipeline, rollback, and monitoring.

## Deployment Targets

The repo ships three deployment compose files plus a dev override:

| File | Host | Services | Notes |
|------|------|----------|-------|
| `docker-compose.yml` | Application server | `api`, `worker` (control role), `postgres`, `redis` | Built locally from `Dockerfile`. No file mounts on `api`/`worker` except `./certs/` on `api` (DIST-01). |
| `docker-compose.agent.yml` | File server (one per host) | `worker` (agent role), `watcher`, `audfprint`, `panako` | All four services pull from GHCR via `PHAZE_IMAGE_TAG`: `worker`/`watcher` from `ghcr.io/simplicityguy/phaze`, `audfprint`/`panako` from the `/audfprint` + `/panako` sub-paths. Each sidecar keeps a commented dev-only `build:` fallback. |
| `docker-compose.cloud-agent.yml` | OCI A1 (cloud) | `worker` (agent role, `kind=compute`) | arm64 image, no media, named scratch. Cloud-burst compute agent over Tailscale. See [cloud-burst.md](cloud-burst.md). |
| `docker-compose.override.yml` | Application server (dev only) | overlays `api` + `worker` | Auto-merged by `docker compose` in dev. Mounts `./src` for live reload, runs `uvicorn --reload`, sets `PHAZE_DEBUG=true`. Do **not** rely on it in production (the override skips the cert-bootstrap entrypoint). |

### Application-server services (`docker-compose.yml`)

| Service | Image / build | Command | Ports | Role |
|---------|---------------|---------|-------|------|
| `api` | build `Dockerfile` | `uv run python -m phaze.entrypoint` | `${API_PORT:-8000}:8000` | FastAPI + admin UI behind TLS. Mounts `${CA_PATH:-./certs}:/certs:rw` for the cert bootstrap. |
| `worker` | build `Dockerfile` | `uv run saq phaze.tasks.controller.settings` | — | Control-role SAQ worker (`PHAZE_ROLE=control`). Fileless; no volume mounts. |
| `postgres` | `postgres:18-alpine` | — | `5432:5432` | Primary database. Data on the `pgdata` named volume mounted at `/var/lib/postgresql`. |
| `redis` | `redis:8-alpine` | `redis-server --requirepass ${REDIS_PASSWORD:?...}` | `${REDIS_BIND_IP:-127.0.0.1}:6379:6379` | Cache / rate-limit / counters (no longer the SAQ broker — Postgres is, via `PHAZE_QUEUE_URL`). `--requirepass` fails fast at compose-parse time if `REDIS_PASSWORD` is unset. |

`api` and `worker` are built from the same `Dockerfile` and differ only by their `command`: `api` runs the cert-bootstrap entrypoint then uvicorn; `worker` runs the controller SAQ worker with `PHAZE_ROLE=control`.

### File-server services (`docker-compose.agent.yml`)

| Service | Image / build | Command | Role |
|---------|---------------|---------|------|
| `worker` | `ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}` | `uv run saq phaze.tasks.agent_worker.settings` | Agent-role SAQ worker (`PHAZE_ROLE=agent`). |
| `watcher` | `ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}` | `uv run python -m phaze.agent_watcher` | Always-on directory watcher (`PHAZE_ROLE=agent`). |
| `audfprint` | `ghcr.io/simplicityguy/phaze/audfprint:${PHAZE_IMAGE_TAG:-latest}` | (image default) | Fingerprint sidecar. Pulls from GHCR. (Commented dev-only `build:` fallback in the compose file.) |
| `panako` | `ghcr.io/simplicityguy/phaze/panako:${PHAZE_IMAGE_TAG:-latest}` | (image default) | Fingerprint sidecar. Pulls from GHCR. (Commented dev-only `build:` fallback in the compose file.) |

All four services mount the music library read-only via `${SCAN_PATH:?SCAN_PATH required}:/data/music:ro`. There is **no `postgres` or `redis` service here** — agents reach the app-server's Redis (cache) and, as of the Phase-36 queue-backend migration, the app-server's **Postgres broker** directly over the LAN via `PHAZE_QUEUE_URL` (Postgres:5432). Application/file metadata is still reached only via the HTTP API — DIST-04 — and there is **no `DATABASE_URL`** on any agent service; the agent's only Postgres connection is the SAQ broker pool.

## Cloud-burst compute agent

`docker-compose.cloud-agent.yml` is a fourth, optional deployment target (v5.0): a **worker-only**
compute agent that runs on a free OCI Ampere A1 (arm64) over Tailscale and analyzes **long** audio
sets that would otherwise time out on a file server. It owns no media, mounts a named scratch
volume, and reaches lux only via the SAQ Postgres broker + the HTTP API (DIST-04). The feature
ships **off by default** (`PHAZE_CLOUD_TARGET=local`; set `a1` to enable this compute agent, or
`k8s` for the Kubernetes target). **Renamed in v6.0** — `PHAZE_CLOUD_TARGET` replaces the old
cloud-burst on/off boolean.

The full compose walkthrough, the homelab provisioning runbook (OCI A1 OpenTofu spec, Tailscale
grants ACL, and the least-privilege `phaze_broker` Postgres role), the deploy ordering, and the
smoke test live in **[cloud-burst.md](cloud-burst.md)** — they are intentionally kept out of this
guide. See also [configuration.md → Cloud-burst settings](configuration.md#cloud-burst-settings)
for the per-knob reference.

For the **Kubernetes (Kueue) burst** target (`PHAZE_CLOUD_TARGET=k8s`) — the cluster-admin
Kueue objects (ResourceFlavor / ClusterQueue / LocalQueue), the namespaced RBAC Role phaze's
ServiceAccount needs to submit/watch Jobs, the `_FILE`-mounted kubeconfig/SA-token + S3-credential
Secret, the S3 staging bucket + lifecycle rule, and the transport (Tailscale **or** WireGuard) to
the kube API and S3 endpoint — see the dedicated runbook in **[k8s-burst.md](k8s-burst.md)**.

### Revert / single-toggle (disable cloud offload)

`PHAZE_CLOUD_TARGET` is the **single master switch** for the whole cloud-offload feature, so
reverting is one env-var flip plus a restart — **no other change**:

1. Set `PHAZE_CLOUD_TARGET=local` on the application server (the `api` + `worker` services).
   `local` is also the shipped default, so simply **removing** the var has the same effect.
2. Restart the controller worker **and** the api (`docker compose up -d --force-recreate worker api`).
   `cloud_target` is read from the import-time settings singleton, so the flip takes effect only
   after a restart — like every other knob.

After the restart, **every** file — short and long alike — routes to the local file-server queue
exactly as it did before cloud burst existed: no file is held `AWAITING_CLOUD`, the staging cron
no-ops, and backfill-to-cloud is rejected. (Long files may then time out locally and fail cleanly
as `ANALYSIS_FAILED`.) In-flight work drains rather than aborting — files already `PUSHING`/`PUSHED`
finish on their current target.

You do **not** need to tear down the K8s/S3 (or A1) objects to revert: the kube API, the LocalQueue,
the S3 bucket, the mounted Secret, and the OCI A1 compute agent can all be left in place, inert. The
toggle alone fully disables the path; re-enabling later is the reverse flip (`a1` or `k8s`) plus a
restart, with no re-provisioning. To switch *between* cloud targets instead of fully reverting, set
`a1` (rsync → OCI A1; requires `compute_scratch_dir`) or `k8s` (S3 → Kueue; requires
`kube_api_url` / `kube_namespace` / `kube_local_queue` + `s3_bucket` / `s3_endpoint_url`, all
fail-fast at startup) — full cluster/bucket/secret setup for `k8s` is in **[k8s-burst.md](k8s-burst.md)**.

## Controller vs Agent roles

Phaze v4.0 selects its settings class at process boot from the `PHAZE_ROLE` env var (default `control`), via `phaze.config.get_settings()`:

- `PHAZE_ROLE=control` → `ControlSettings` (LLM proposal generation, Discogs matching, fileless tasks). Used by the app-server `api` + `worker`.
- `PHAZE_ROLE=agent` → `AgentSettings` (HTTP client to the app-server, file-bound tasks). Used by the file-server `worker` + `watcher`. The validators in `AgentSettings` raise at construction time if `PHAZE_AGENT_API_URL`, `PHAZE_AGENT_TOKEN`, or `PHAZE_AGENT_SCAN_ROOTS` is missing — agents fail fast with a clear error rather than emitting runtime 401s.

The `api` container does **not** start uvicorn directly. It runs `uv run python -m phaze.entrypoint`, which:

1. Runs `phaze.cert_bootstrap.ensure_certs_present(/certs, ...)` to generate (or no-op past) the internal CA + leaf cert **before** uvicorn binds.
2. `os.execvp`-replaces the process with `uvicorn phaze.main:app --ssl-keyfile /certs/phaze-server.key --ssl-certfile /certs/phaze-server.crt`, so signals and PID 1 propagate cleanly.

The entrypoint reads three env vars, all with safe defaults so a plain `docker compose up` works in dev: `PHAZE_CERTS_DIR` (default `/certs`), `PHAZE_API_HOST` (default `localhost`, baked into the leaf CN), and `PHAZE_API_TLS_SANS` (default `localhost,127.0.0.1,api`). The bootstrap is idempotent — restarts against a populated `/certs/` skip regeneration.

## Internal CA / mTLS bootstrap

`phaze.cert_bootstrap` generates a self-signed ECDSA P-256 CA (10-year validity) and a CA-signed leaf cert (2-year validity) into `/certs/` on the app-server's first start:

| File | Mode | Distribution |
|------|------|--------------|
| `phaze-ca.crt` | 0644 | **Public.** Copied to every file server; agents point `PHAZE_AGENT_CA_FILE` at it. |
| `phaze-ca.key` | 0600 | **Private CA signing key. Never leaves the app-server host.** |
| `phaze-server.crt` | 0644 | Leaf cert presented by uvicorn over TLS. |
| `phaze-server.key` | 0600 | Leaf private key. |

On actual generation (not the idempotent no-op path) a loud banner is emitted via **both** `print()` (interactive `docker compose up`) and `logger.warning()` (`docker compose logs api`). The banner references only the public CA path. Agents trust the app-server by validating its TLS chain against the operator-distributed `phaze-ca.crt`; the agent's outbound bearer token (`PHAZE_AGENT_TOKEN`) is what authenticates the agent to the app-server.

The env vars that gate this bootstrap on the agent side are `PHAZE_AGENT_CA_FILE` (default `/certs/phaze-ca.crt`), `PHAZE_AGENT_API_URL` (must be `https://` when `PHAZE_AGENT_ENV=production`), and `PHAZE_AGENT_TOKEN`. See [docs/configuration.md](configuration.md) for the full env-var reference.

## Prerequisites

- Docker Engine 20.10+ and `docker compose` v2.x on both hosts
- `just` installed on both hosts (or run `docker compose` directly)
- Both hosts on the same private LAN; no firewall blocking ports 6379 (Redis cache), 5432 (Postgres SAQ broker — agents reach it directly as of Phase 36), or 8000 (API) between them
- Postgres + Redis are NOT directly exposed to the public internet (LAN-only; the agent→Postgres:5432 edge is private-LAN scoped)
- On the app-server host: `./certs/` (auto-populated on first start), `.env`
- On each file-server host: `./certs/` (CA only, scp'd from app-server), `./models/` (auto-downloads on first agent start), `.env`

## Step 1 — Bring up the application server

On the **app-server host**:

```bash
git clone https://github.com/simplicityguy/phaze.git
cd phaze
cp .env.example .env
# Edit .env: set REDIS_PASSWORD to a strong unique value (>= 32 chars)
# Edit .env: set REDIS_BIND_IP to the app-server's private LAN IP (e.g., 192.168.1.10)
# Edit .env: set PHAZE_QUEUE_URL (raw libpq postgresql:// DSN) — the SAQ Postgres broker.
#            Defaults to the in-compose `postgres` service; treat as a secret (PHAZE_QUEUE_URL_FILE).
just up
```

`just up` runs `docker compose up -d`. On first start, the `api` container's entrypoint generates the internal CA + leaf cert into `./certs/`. Watch the logs:

```bash
docker compose logs -f api
```

You will see a multi-line banner:

```
==============================================================
GENERATED NEW PHAZE INTERNAL CA at /certs/phaze-ca.crt
COPY THIS FILE TO EVERY FILE SERVER and point each agent's
PHAZE_AGENT_CA_FILE env var at it. EXISTING AGENTS WILL FAIL
TO CONNECT UNTIL THEY HAVE THIS NEW CA.
==============================================================
```

After the banner, uvicorn binds port 8000 with TLS.

**Verify**: `curl --cacert ./certs/phaze-ca.crt https://localhost:8000/docs` returns the OpenAPI UI, and `curl --cacert ./certs/phaze-ca.crt https://localhost:8000/health` returns `{"status":"ok"}` once Postgres is reachable.

## Step 2 — Copy the CA cert to each file server

The CA private key (`./certs/phaze-ca.key`) **stays on the app-server host** (mode 0600). Only the public CA cert (`./certs/phaze-ca.crt`, mode 0644) is distributed to file-server hosts.

From the app-server host, for each file server:

```bash
scp ./certs/phaze-ca.crt operator@fileserver-east:/home/operator/phaze/certs/phaze-ca.crt
```

<!-- VERIFY: operator@fileserver-east:/home/operator/phaze and the file-server hostnames are deployment-specific examples; substitute your real operator account, hostnames, and paths. -->

Or use rsync, ansible, or any one-time file transfer mechanism. The operator-distributed CA is a public cert; non-secret.

## Step 3 — Register an agent and mint a token

On the **app-server host**, run the bundled `phaze agents add` management CLI (it
ships with the application image; run it inside the API container or any
environment that has the `phaze` package installed and `DATABASE_URL` configured):

```bash
phaze agents add \
    --id fileserver-east \
    --name "File Server East" \
    --scan-roots /data/music,/data/concerts
```

The CLI mints a strong bearer token, stores only its sha256 hash in the `agents`
table, and prints two things you need:

- **The cleartext token** — printed **exactly once**. **Save it now — it is NOT
  recoverable from the database** (only the hash is stored). This is the value
  you put in `PHAZE_AGENT_TOKEN` on the file-server side (Step 4).
- **The derived queue name** `phaze-agent-fileserver-east` — put this in
  `PHAZE_AGENT_QUEUE` on the file-server side (Step 4). The queue name is always
  `phaze-agent-<agent_id>`; the agent worker asserts this at startup and exits if
  it disagrees, so copy the value the CLI printed.

The `--id` must match `^[a-z0-9]+(-[a-z0-9]+)*$` (the `agents.id` charset
constraint); the CLI rejects an invalid id (or a non-absolute scan root) with a
non-zero exit **before** touching the database, and reports a friendly error if
the id already exists. `--name` defaults to the titleized id when omitted.

A sentinel `LIVE` ScanBatch row is auto-created the first time the agent posts a file.

<details>
<summary>Under the hood / SQL fallback (no CLI access)</summary>

`phaze agents add` is equivalent to inserting the row by hand. If you only have a
psql session, generate a token and insert its sha256 hash (the **full** wire
string, prefix included) yourself:

```bash
python -c "import secrets; print('phaze_agent_' + secrets.token_urlsafe(32))"
```

```sql
INSERT INTO agents (id, name, token_hash, scan_roots, created_at)
VALUES (
    'fileserver-east',
    'File Server East',
    -- token_hash is sha256() of the chosen plaintext token (prefix included)
    encode(sha256('phaze_agent_REPLACE_WITH_RANDOM_32_URLSAFE'::bytea), 'hex'),
    '["/data/music", "/data/concerts"]'::jsonb,
    now()
);
```

Same rules apply: save the plaintext token (only the hash is stored), and the
agent's queue name is `phaze-agent-fileserver-east`.

</details>

## Step 4 — Populate the file-server `.env`

On the **file-server host**, get the compose file and the `.env` template. All four agent images are pulled from GHCR, so the checkout is only needed for `docker-compose.agent.yml` + `.env.example.agent` (and optionally if you want to build a sidecar from source via the commented dev-only `build:` fallback) — not to build the sidecars for a normal deployment:

```bash
git clone https://github.com/simplicityguy/phaze.git
cd phaze
cp .env.example.agent .env
```

Edit `.env` to set the required variables. The agent stack uses `${VAR:?msg}` interpolation on `SCAN_PATH`, so docker compose fails fast at parse time if it is unset:

- `PHAZE_AGENT_API_URL=https://<app-server-lan-ip>:8000`
- `PHAZE_REDIS_URL=redis://default:<REDIS_PASSWORD>@<app-server-lan-ip>:6379/0` (cache only — no longer the SAQ broker)
- `PHAZE_QUEUE_URL=postgresql://<user>:<password>@<app-server-lan-ip>:5432/phaze` — the SAQ Postgres broker DSN (**raw libpq form**, NOT `postgresql+asyncpg://`). The agent now opens a psycopg3 pool to the app-server Postgres, so its host must be reachable on 5432 (new firewall edge relaxing D-25). Treat it as a secret — prefer `PHAZE_QUEUE_URL_FILE=/run/secrets/phaze_queue_url`.
- `PHAZE_AGENT_ID=fileserver-east`
- `PHAZE_AGENT_TOKEN=<the plaintext token from Step 3>`
- `PHAZE_AGENT_QUEUE=phaze-agent-fileserver-east` — by convention this MUST equal `phaze-agent-<agent_id>` (the value `phaze agents add` printed in Step 3). There is no queue column on the `agents` table; the worker derives the expected name from the token's agent_id and exits non-zero on mismatch.
- `PHAZE_AGENT_CA_FILE=/certs/phaze-ca.crt`
- `PHAZE_AGENT_ENV=production`
- `SCAN_PATH=/path/to/your/music/library`
- `MODELS_PATH=./models`
- `CA_PATH=./certs`
- `PHAZE_AGENT_SCAN_ROOTS=/data/music,/data/concerts`
- `PHAZE_IMAGE_TAG=v4.0.0` (or `latest` for first-time setup)

See [docs/configuration.md](configuration.md) for the complete env-var reference and defaults.

## Step 5 — Bring up the agent stack

On the **file-server host**:

```bash
just up-agent
```

`just up-agent` runs `docker compose -f docker-compose.agent.yml up -d`. On first start, the agent worker boots, calls `/api/internal/agent/whoami` to verify its token, then downloads ~150MB of essentia weights to `./models/` (2-5 minutes; logs an INFO line). The watcher comes up in parallel.

Watch the logs:

```bash
docker compose -f docker-compose.agent.yml logs -f worker
```

You should see:

- `phaze.tasks.agent_worker startup role=agent api=https://... auth_id_prefix=phaze_agent_a1b2... queue=phaze-agent-fileserver-east`
- `/models is empty; downloading essentia weights (~150MB, takes 2-5min on first start)...`
- `Models downloaded successfully to /models`
- `phaze.tasks.agent_worker startup complete agent_id=fileserver-east queue=phaze-agent-fileserver-east`

After ~5 minutes, the heartbeat — an asyncio background task in the agent worker (every 30s) — starts firing against `POST /api/internal/agent/heartbeat`.

> **Run both stacks on one host (dev convenience):** `just up-all` runs `docker compose -f docker-compose.yml -f docker-compose.agent.yml up -d`. This is for development only — production keeps the app-server and file-server stacks on separate hosts to preserve filesystem isolation (DIST-01).

## Step 6 — Verify on the admin page

From any browser on the LAN (or via SSH tunnel from your laptop):

```bash
# Trust the CA in your local browser, or use curl:
curl --cacert ./certs/phaze-ca.crt https://<app-server-lan-ip>:8000/admin/agents
```

The `/admin/agents` page renders an agent table and self-refreshes via an HTMX poll every 5 seconds. Each agent row shows a liveness status derived from `agents.last_seen_at` (`phaze.services.agent_liveness.classify`):

| Status | Condition |
|--------|-----------|
| **alive** | `now - last_seen_at < 90s` (3x the 30s heartbeat cadence) |
| **stale** | `90s <= now - last_seen_at < 300s` (one or more missed beats) |
| **dead** | `now - last_seen_at >= 300s` (~10 missed beats) |
| **never** | agent registered but has never sent a heartbeat (`last_seen_at IS NULL`) |
| **revoked** | agent has a `revoked_at` timestamp |

You should see the agent reach **alive** within ~60s of `just up-agent`.

If the row shows **never**: the agent worker has not completed startup yet. Check the worker logs.

If the row shows **stale** then **dead**: the worker is up but heartbeats are not reaching the app-server. Check the agent worker logs for `heartbeat failed: ...` WARNING lines, and verify the agent can reach `https://<app-server>:8000/api/internal/agent/heartbeat` with the correct CA cert and token. If a **busy** worker (high CPU, actively analyzing) still appears stale/dead, confirm the agent image carries the Phase 46 build — before it, the heartbeat ran as a SAQ cron job that competed for the `worker_max_jobs` dispatch slots and was starved by multi-hour `process_file` jobs; the fix runs the heartbeat as an in-process asyncio background task that cannot be starved.

## The watcher service

The `watcher` service (`src/phaze/agent_watcher/`, runnable via `python -m phaze.agent_watcher`) is an always-on asyncio process — **not** a SAQ worker. On startup it:

1. Loads `AgentSettings` via `get_settings()` (raises if `PHAZE_ROLE != agent`).
2. Calls `/api/internal/agent/whoami` with bounded retry to resolve the calling agent's identity and scan roots. A bad token short-circuits immediately (fail fast, no restart loop).
3. Schedules one `watchdog` Observer per scan root and posts each settled file to the app-server.

Tunables (see `AgentSettings` in `docs/configuration.md`): `PHAZE_WATCHER_SETTLE_SECONDS` (default 10), `PHAZE_WATCHER_SWEEP_INTERVAL_SECONDS` (default 2), `PHAZE_WATCHER_MAX_PENDING_SECONDS` (default 3600), and `PHAZE_WATCHER_POLLING_MODE` (default false — set true for macOS Docker bind mounts where inotify does not propagate).

## Build Pipeline

Images are built and published to the GitHub Container Registry (GHCR) by two reusable GitHub Actions workflows, both invoked from `.github/workflows/ci.yml` via `workflow_call`.

### `docker-validate.yml` (validation, runs on every PR/push)

Called from the CI `docker` job (after `quality`, only when non-markdown files change). It:

- Builds each Dockerfile (`Dockerfile`, `services/audfprint/Dockerfile.audfprint`, `services/panako/Dockerfile.panako`) via a matrix and lints them with **hadolint** (`failure-threshold: error`).
- Validates both compose files parse cleanly: `docker compose -f docker-compose.yml config --quiet` (with placeholder `REDIS_PASSWORD`/`REDIS_BIND_IP`) and `docker compose -f docker-compose.agent.yml --env-file .env.agent config --quiet` (with placeholder agent vars).

No images are pushed by this workflow — it is a gate.

### `docker-publish.yml` (build + push to GHCR)

Called from the CI `docker-publish` job, which runs only after `aggregate-results` passes and only when code changed. It:

- Builds the same three images in a matrix and pushes to GHCR. `push` is `true` for non-PR events.
- The `api` image publishes to the bare repo URL `ghcr.io/simplicityguy/phaze` (no sub-path) so `docker-compose.agent.yml`'s `worker` + `watcher` can pull it directly; the sidecars publish under `/audfprint` and `/panako` suffixes.
- **Authoritative image paths:** `ghcr.io/simplicityguy/phaze` is the authoritative api/worker/watcher image; `ghcr.io/simplicityguy/phaze/audfprint` and `ghcr.io/simplicityguy/phaze/panako` are the sidecar images. `ghcr.io/simplicityguy/phaze/api` is a **deprecated/orphaned** path from a pre-D-15 convention — it is no longer published and must NOT be pulled or referenced.
- Tag strategy (via `docker/metadata-action`): `latest` on the default branch, plus `{{version}}` and `{{major}}.{{minor}}` semver tags, `ref`-based tags (tag/branch/PR), and a dated schedule tag. Tagged releases therefore produce **both** `:latest` and `:v<version>`.
- Release tags MUST be 3-part semver (`vX.Y.Z`, e.g. `v4.0.0`) — `ci.yml` triggers the publish pipeline on `push` of a `v*.*.*` tag, and the `{{version}}` / `{{major}}.{{minor}}` image tags are only produced for a 3-part semver ref. A 2-part tag (`v4.0`) will not match the trigger and will not publish version-pinnable images.
- Builds with `provenance: true` and `sbom: true` for supply-chain attestation, on `linux/amd64`.

The single-stage `Dockerfile` (`FROM python:3.14-slim AS base`) installs deps with `uv sync --frozen --no-dev` in cached layers, copies `src/`, `alembic/`, and `alembic.ini`, runs as the non-root `phaze` user, and exposes port 8000. The `api` and `worker` containers share this image and diverge only by `command`.

You can also build/push manually with `just`: `just docker-build`, `just docker-validate` (hadolint), `just docker-compose-validate`, and `just image-push` (requires a `gh` token with `packages:write`).

## Environment Setup

The full environment-variable reference, including required-vs-optional status and defaults, lives in [docs/configuration.md](configuration.md). The two templates in the repo are `.env.example` (app-server) and `.env.example.agent` (file-server agent).

Production-critical variables:

| Variable | Host | Why it matters |
|----------|------|----------------|
| `REDIS_PASSWORD` | app-server | `redis-server --requirepass`; compose parse fails if unset. Use a unique high-entropy value (>= 32 chars). |
| `REDIS_BIND_IP` | app-server | Must be the app-server's private LAN IP so agents on other hosts can reach Redis. Never `0.0.0.0`, never a public IP. |
| `PHAZE_QUEUE_URL` | app-server + file-server | The SAQ Postgres broker DSN (**raw libpq** `postgresql://…`, NOT `+asyncpg`). On agents it points at the app-server Postgres LAN IP:5432 — open that firewall edge (relaxes D-25). Carries DB credentials; use the `_FILE` secret form. Keep the per-queue pool budget under Postgres `max_connections`. |
| `PHAZE_AGENT_ENV=production` | file-server | Activates the `AgentSettings` guards: refuses non-`https://` `agent_api_url` (CR-01) and passwordless `redis_url` (D-06). Note: there is no production credential guard on `PHAZE_QUEUE_URL` yet — protect it via the LAN-scoped firewall + a strong DB password. |
| `PHAZE_AGENT_TOKEN` | file-server | The plaintext bearer token; must match the `token_hash` row in `agents`. Generate via `secrets.token_urlsafe(32)`. |
| `PHAZE_AGENT_CA_FILE` | file-server | Path to the operator-distributed `phaze-ca.crt`; the agent's HTTP client verifies the app-server TLS chain against it. |
| `PHAZE_IMAGE_TAG` | file-server | Pin to a specific version (`v4.0.0`) in production rather than `latest`. |
| `SCAN_PATH` | file-server | The music-library root, bind-mounted read-only into all agent services. Compose parse fails if unset. |

### Secrets via files (Docker secrets)

Every secret-bearing variable accepts a `<VAR>_FILE` sibling that points at a file holding the value, so you can mount a Docker/Swarm secret (or a Kubernetes secret / SOPS-decrypted file) instead of inlining cleartext into the environment. Supported: `DATABASE_URL`, `REDIS_URL`, `PHAZE_QUEUE_URL` (the SAQ Postgres broker DSN — `queue_url`), `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` (each also via its `PHAZE_*` alias where one exists), and `PHAZE_AGENT_TOKEN` / `AGENT_TOKEN`. See [docs/configuration.md → Secrets via files](configuration.md#secrets-via-files-_file-convention) for the precedence and newline-stripping rules.

Example — mount the Anthropic key as a Docker secret on the app server and reference it by path (no `ANTHROPIC_API_KEY` in the environment):

```yaml
# docker-compose.yml (app server)
secrets:
  anthropic_api_key:
    file: ./secrets/anthropic_api_key   # contents: the raw sk-ant-... key

services:
  api:
    secrets: [anthropic_api_key]
    environment:
      ANTHROPIC_API_KEY_FILE: /run/secrets/anthropic_api_key
```

The same pattern works for the agent's bearer token on a file server — mount the secret and set `PHAZE_AGENT_TOKEN_FILE=/run/secrets/phaze_agent_token`. The file's trailing newline is stripped, so the hashed wire string still matches the `token_hash` row (a stray `\n` would otherwise cause a permanent 401). A `_FILE` path that is missing or unreadable fails fast at startup with a clear error.

## Rollback Procedure

There is no automated rollback in CI — rollback is a manual re-deploy of a previously published image tag.

**File servers (agent stack)** pull from GHCR, so rolling back is a tag swap:

```bash
# On the file-server host:
# 1. Edit .env: set PHAZE_IMAGE_TAG back to the last-known-good version, e.g.
#    PHAZE_IMAGE_TAG=v4.0.0
# 2. Re-pull and recreate the agent containers:
docker compose -f docker-compose.agent.yml pull
docker compose -f docker-compose.agent.yml up -d
```

Because `docker-publish.yml` tags both `:latest` and `:v<version>`, every release remains pullable by its version tag — keep `PHAZE_IMAGE_TAG` pinned in production so a rollback is just editing one line.

**Application server** is built locally from the checkout, so rolling back means checking out the previous git tag and rebuilding:

```bash
# On the app-server host:
git checkout v4.0.0          # the last-known-good release tag
just rebuild                 # docker compose up -d --build
```

To stop and restart cleanly without rebuilding: `just down` (`docker compose down`) then `just up`. The `pgdata` named volume and `./certs/` persist across `down`/`up`, so no data or cert state is lost.

> Do **not** `rm -rf ./certs/` as part of a rollback — that triggers a full CA regeneration and breaks every agent until the new `phaze-ca.crt` is re-distributed (see CA Rotation below).

## One-time cleanup after the Phase 46 heartbeat fix

Builds **before** Phase 46 registered the heartbeat as a SAQ `CronJob` with `unique=True`, which parks a deterministic row keyed `cron:heartbeat_tick` in the Postgres broker (`saq_jobs`). After you redeploy the new agent image, nothing re-schedules that row (the heartbeat now runs as an in-process asyncio background task), so it lingers as a stale parked entry. Run this one-time cleanup **after the new agent image is deployed**, against the queue database (`PHAZE_QUEUE_URL`):

```sql
DELETE FROM saq_jobs WHERE key = 'cron:heartbeat_tick';
```

It is harmless if the row is already absent (e.g. on a fresh broker or after a `saq_jobs` truncate). This mirrors the prior Redis orphaned-cron-purge runbook, adapted to the Postgres broker.

## Monitoring & Health

- **API health endpoint:** `GET /health` returns `{"status":"ok"}` and checks database connectivity (`SELECT 1`). It requires Postgres to be reachable. Use it as the app-server liveness probe: `curl --cacert ./certs/phaze-ca.crt https://<app-server>:8000/health`.
- **Agent heartbeat / liveness:** each agent worker runs an asyncio background task in the agent worker (every 30s — `phaze.tasks.heartbeat._heartbeat_loop`) that POSTs to `/api/internal/agent/heartbeat` with `{agent_version, worker_pid, queue_depth}`. It is launched in the worker `startup` hook and cancelled on `shutdown`, so it runs outside the SAQ job-dispatch pool and is never starved by long-running analysis jobs (Phase 46). The endpoint stamps `agents.last_seen_at` and persists the payload to the `agents.last_status` JSONB column. The `/admin/agents` page classifies each agent as alive/stale/dead/never/revoked from `last_seen_at` (thresholds: alive < 90s, dead >= 300s) and self-refreshes every 5s via HTMX.
- **Sidecar health:** the `audfprint` and `panako` fingerprint sidecars expose `/health`; `just audfprint-health` and `just panako-health` exec into the worker and curl them.
- **Worker health:** `just worker-health` runs the SAQ `--check` against the controller worker; `just worker-logs` follows its logs.
- **Logging:** services log to stdout/stderr (`docker compose logs -f <service>`). The cert-bootstrap banner additionally lands in `docker compose logs api` via `logger.warning()`. No external metrics/tracing exporter (Sentry, Datadog, OpenTelemetry) is configured in this repo. <!-- VERIFY: any external log aggregation, alerting, or metrics dashboard configured at the deployment level (outside the repo) is not represented here. -->

## Filesystem-Isolation Smoke (D-20)

To verify DIST-01 (the app-server has no way to read or write music files), exec into the api container and try to read a file:

```bash
docker compose exec api ls -la /data/music
# Expected: ls: cannot access '/data/music': No such file or directory
```

Or trust the structural test that runs in CI:

```bash
uv run pytest tests/test_deployment/ -v
```

The compose-parse tests assert that `docker-compose.yml` declares no `SCAN_PATH`, `MODELS_PATH`, or `OUTPUT_PATH` bind mounts on `api` or `worker` services — only `./certs/` is mounted on `api` (and that one is required for the cert bootstrap).

## CA Rotation (caution)

The CA + leaf cert generated in Step 1 is valid for 10 years (CA) / 2 years (leaf). If you ever need to rotate:

```bash
# On the app-server host:
rm -rf ./certs/                       # destructive — all current cert state is lost
docker compose restart api            # cert_bootstrap regenerates + prints the loud banner again
# Then repeat Step 2 (copy ./certs/phaze-ca.crt to every file server) and restart each agent.
```

Every file-server agent will fail to connect until you re-distribute the new `phaze-ca.crt`. The loud banner is the only safeguard — do not delete the certs directory casually.

## Pinning the agent image for production

For first-time setup, `PHAZE_IMAGE_TAG=latest` pulls the most recent tagged release from GHCR. For production, pin to a specific version:

```bash
# On the file-server host's .env:
PHAZE_IMAGE_TAG=v4.0.0
```

Then `just up-agent` pulls exactly that version. The `docker-publish.yml` workflow tags both `:latest` and `:v<version>` on tagged releases. The pin MUST be a 3-part `vX.Y.Z` value matching a published release tag (`ci.yml` only publishes on `push` of a `v*.*.*` tag).

## Pre-warming models (skip the first-start wait)

To avoid the 2-5 minute model download on first agent boot:

```bash
# On the file-server host BEFORE just up-agent:
just download-models
```

This runs `bash scripts/download-models.sh models`, populating `./models/` directly; the agent's auto-download check then no-ops.

## Production Checklist

Before shipping a file-server host to production:

- [ ] `REDIS_PASSWORD` set to a unique high-entropy value (>= 32 chars) — never the default
- [ ] `REDIS_BIND_IP` set to the app-server's private LAN IP (never `0.0.0.0`, never the public IP)
- [ ] `PHAZE_AGENT_ENV=production` — enables the redis-password-required and https-required guards in `AgentSettings`
- [ ] `PHAZE_AGENT_TOKEN` generated via `secrets.token_urlsafe(32)`, not a placeholder
- [ ] `phaze-ca.crt` distributed via secure channel (scp over SSH, not email/chat)
- [ ] `phaze-ca.key` NEVER copied off the app-server host
- [ ] `PHAZE_IMAGE_TAG` pinned to a specific version (`v4.0.0`), not `latest`
- [ ] `SCAN_PATH` points at the actual music library root (compose parse fails if unset)
- [ ] `docker-compose.override.yml` not present / not active on production hosts (it bypasses the cert-bootstrap entrypoint)
- [ ] Filesystem-isolation smoke confirmed (see above) — `docker compose exec api ls /data/music` returns "No such file or directory"
- [ ] `/admin/agents` page shows **alive** status within ~60s of `just up-agent`

## See also

- `.env.example` — app-server environment template
- `.env.example.agent` — file-server agent environment template
- `docker-compose.yml` — app-server compose
- `docker-compose.agent.yml` — file-server agent compose
- `docker-compose.cloud-agent.yml` — OCI A1 cloud compute-agent compose (cloud burst)
- `docker-compose.override.yml` — dev-only overlay (live reload)
- [docs/cloud-burst.md](cloud-burst.md) — cloud-burst compute-agent deploy, runbook, and toggle
- [docs/configuration.md](configuration.md) — full environment-variable reference
- [docs/architecture.md](architecture.md) — system architecture overview
