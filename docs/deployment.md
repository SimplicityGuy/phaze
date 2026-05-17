# Phaze v4.0 Deployment Guide

Production deployment of Phaze v4.0 (Distributed Agents) runs as **two compose files on two hosts**:

- **Application server** (`docker-compose.yml`): API, UI, Postgres, Redis, fileless controller worker. No file mounts. HTTPS via internal CA. Redis `requirepass` + LAN binding.
- **File servers** (`docker-compose.agent.yml`, one per host): agent worker, watcher, audfprint, panako. Holds music/video files locally; reaches the app-server over HTTPS for every state change.

This guide walks through bringing up a fresh two-host deployment from a clean checkout.

## Prerequisites

- Docker Engine 20.10+ and `docker compose` v2.x on both hosts
- `just` installed on both hosts (or run `docker compose` directly)
- Both hosts on the same private LAN; no firewall blocking ports 6379 (Redis) or 8000 (API) between them
- Postgres + Redis are NOT directly exposed to the public internet
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
just up
```

On first start, the `api` container generates an internal CA + leaf cert into `./certs/`. Watch the logs:

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

**Verify**: `curl --cacert ./certs/phaze-ca.crt https://localhost:8000/docs` returns the OpenAPI UI.

## Step 2 — Copy the CA cert to each file server

The CA private key (`./certs/phaze-ca.key`) **stays on the app-server host** (mode 0600). Only the public CA cert (`./certs/phaze-ca.crt`, mode 0644) is distributed to file-server hosts.

From the app-server host, for each file server:

```bash
scp ./certs/phaze-ca.crt operator@fileserver-east:/home/operator/phaze/certs/phaze-ca.crt
```

Or use rsync, ansible, or any one-time file transfer mechanism. The operator-distributed CA is a public cert; non-secret.

## Step 3 — Register an agent and mint a token

On the **app-server host**, in a psql session (or via your preferred SQL client):

```sql
INSERT INTO agents (id, name, token_hash, scan_roots, created_at)
VALUES (
    'fileserver-east',
    'File Server East',
    -- token_hash is sha256() of the chosen plaintext token
    encode(sha256('phaze_agent_REPLACE_WITH_RANDOM_32_URLSAFE'::bytea), 'hex'),
    '["/data/music", "/data/concerts"]'::jsonb,
    now()
);
```

The plaintext token (the part you sha256) is what you put in `.env` on the file-server side. **Save it now — it is not recoverable from the database** (only the hash is stored).

Generate a strong token before running the INSERT:

```bash
python -c "import secrets; print('phaze_agent_' + secrets.token_urlsafe(32))"
```

A sentinel `LIVE` ScanBatch row is auto-created the first time the agent posts a file (Phase 24 + Phase 27 invariant).

## Step 4 — Populate the file-server `.env`

On the **file-server host**:

```bash
git clone https://github.com/simplicityguy/phaze.git
cd phaze
cp .env.example.agent .env
```

Edit `.env` to set the required variables. The agent stack uses `${VAR:?msg}` interpolation on `SCAN_PATH`, so docker compose fails fast at parse time if it is unset:

- `PHAZE_AGENT_API_URL=https://<app-server-lan-ip>:8000`
- `PHAZE_REDIS_URL=redis://default:<REDIS_PASSWORD>@<app-server-lan-ip>:6379/0`
- `PHAZE_AGENT_ID=fileserver-east`
- `PHAZE_AGENT_TOKEN=<the plaintext token from Step 3>`
- `PHAZE_AGENT_QUEUE=phaze-agent-fileserver-east`
- `PHAZE_AGENT_CA_FILE=/certs/phaze-ca.crt`
- `PHAZE_AGENT_ENV=production`
- `SCAN_PATH=/path/to/your/music/library`
- `MODELS_PATH=./models`
- `CA_PATH=./certs`
- `PHAZE_AGENT_SCAN_ROOTS=/data/music,/data/concerts`
- `PHAZE_IMAGE_TAG=v4.0.0` (or `latest` for first-time setup)

## Step 5 — Bring up the agent stack

On the **file-server host**:

```bash
just up-agent
```

On first start, the agent worker boots, calls `/whoami` to verify its token, then downloads ~150MB of essentia weights to `./models/` (2-5 minutes; logs an INFO line). The watcher comes up in parallel.

Watch the logs:

```bash
docker compose -f docker-compose.agent.yml logs -f worker
```

You should see:

- `phaze.tasks.agent_worker startup role=agent api=https://... auth_id_prefix=phaze_agent_a1b2... queue=phaze-agent-fileserver-east`
- `/models is empty; downloading essentia weights (~150MB, takes 2-5min on first start)...`
- `Models downloaded successfully to /models`
- `phaze.tasks.agent_worker startup complete agent_id=fileserver-east queue=phaze-agent-fileserver-east`

After ~5 minutes, the heartbeat cron starts firing every 30s against `POST /api/internal/agent/heartbeat`.

## Step 6 — Verify on the admin page

From any browser on the LAN (or via SSH tunnel from your laptop):

```bash
# Trust the CA in your local browser, or use curl:
curl --cacert ./certs/phaze-ca.crt https://<app-server-lan-ip>:8000/admin/agents
```

You should see the agent row with status **ALIVE** (green pill) within 60s of `just up-agent`.

If the row shows **NEVER**: the agent worker has not completed startup yet. Check the worker logs.

If the row shows **DEAD** after 5 minutes: the worker is up but heartbeats are not reaching the app-server. Check the agent worker logs for `heartbeat failed: ...` WARNING lines, and verify the agent can reach `https://<app-server>:8000/api/internal/agent/heartbeat` with the correct CA cert and token.

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

Then `just up-agent` pulls exactly that version. The `docker-publish.yml` workflow tags both `:latest` and `:v<version>` on tagged releases.

## Pre-warming models (skip the first-start wait)

To avoid the 2-5 minute model download on first agent boot:

```bash
# On the file-server host BEFORE just up-agent:
just download-models
```

This populates `./models/` directly; the agent's auto-download check then no-ops.

## Production Checklist

Before shipping a file-server host to production:

- [ ] `REDIS_PASSWORD` set to a unique high-entropy value (>= 32 chars) — never the default
- [ ] `REDIS_BIND_IP` set to the app-server's private LAN IP (never `0.0.0.0`, never the public IP)
- [ ] `PHAZE_AGENT_ENV=production` — enables the redis-password-required guard in `AgentSettings`
- [ ] `PHAZE_AGENT_TOKEN` generated via `secrets.token_urlsafe(32)`, not a placeholder
- [ ] `phaze-ca.crt` distributed via secure channel (scp over SSH, not email/chat)
- [ ] `phaze-ca.key` NEVER copied off the app-server host
- [ ] `PHAZE_IMAGE_TAG` pinned to a specific version (`v4.0.0`), not `latest`
- [ ] `SCAN_PATH` points at the actual music library root (compose parse fails if unset)
- [ ] Filesystem-isolation smoke confirmed (see above) — `docker compose exec api ls /data/music` returns "No such file or directory"
- [ ] `/admin/agents` page shows ALIVE status within 60s of `just up-agent`

## See also

- `.env.example` — app-server environment template
- `.env.example.agent` — file-server agent environment template
- `docker-compose.yml` — app-server compose
- `docker-compose.agent.yml` — file-server agent compose
- `.planning/PROJECT.md` — v4.0 architecture overview
- `.planning/ROADMAP.md` — phase history
