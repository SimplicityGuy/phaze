# phaze.agent_watcher

## Purpose

Always-on file watcher for the file-server agent role. Observes the agent's configured `scan_roots` with watchdog, debounces events by mtime-stability (default 10s settle period), and POSTs each settled file to the application server via the existing `/api/internal/agent/files` endpoint. Bound to the agent's sentinel LIVE `ScanBatch` (one per agent, seeded at registration time). NOT a SAQ worker -- entry point is `asyncio.run(main())`.

## Entry point

```
uv run python -m phaze.agent_watcher
```

The Dockerfile's same image runs both the SAQ agent worker (`uv run saq phaze.tasks.agent_worker.settings`) and the watcher; the compose service distinguishes by `command:`.

## Fresh Install Quickstart

The watcher requires a registered agent in the `agents` table. On a brand-new docker compose stack (empty database), use the dev-seeded agent path to get up and running:

1. `cp .env.example .env`
2. Open `.env`. The default DATABASE_URL and REDIS_URL use the docker-service hostnames (`postgres`, `redis`) — leave them as-is when running via `docker compose up`. If you instead run any service via `uv run` on the host, switch the hostnames to `localhost`.
3. Enable dev-agent seeding and pick a fixed token so you can paste it into the watcher config in one shot:
   ```
   PHAZE_DEV_SEED_AGENT=true
   PHAZE_DEV_AGENT_TOKEN=phaze_agent_<32 urlsafe-base64 bytes>
   ```
   If you leave `PHAZE_DEV_AGENT_TOKEN` empty, the api will generate a random one and log it at INFO — you can grab it from `docker compose logs api`.
4. Set the watcher's auth + scan roots in the same `.env`:
   ```
   PHAZE_AGENT_API_URL=http://api:8000
   PHAZE_AGENT_TOKEN=phaze_agent_<same token you set above>
   PHAZE_AGENT_SCAN_ROOTS=/data/music
   ```
5. Bring up the data plane:
   ```
   docker compose up -d postgres redis
   ```
6. Bring up the api + worker. The api lifespan runs `alembic upgrade head` and seeds the dev agent automatically:
   ```
   docker compose up -d api worker
   docker compose logs api  # look for: "seeded dev agent id=dev-agent ..."
   ```
7. Bring up the watcher (image is the same as `worker`, but the command is `python -m phaze.agent_watcher`):
   ```
   docker compose up -d watcher
   ```
8. Drop an MP3 into the scan path (mounted to `/data/music` inside the watcher container). After the 10s settle window, the watcher posts the file to the api:
   ```
   docker logs watcher --tail=20  # look for: POST /api/internal/agent/files -> 200
   ```
9. (Production checklist) Once the dev path is working, flip `PHAZE_DEV_SEED_AGENT=false`, provision real agents via the management CLI, and rotate `PHAZE_AGENT_TOKEN`.

## Required env vars

- `PHAZE_ROLE=agent` -- selects the agent settings module via `get_settings()`
- `PHAZE_AGENT_API_URL` -- base URL of the application server (e.g., `http://api:8000`)
- `PHAZE_AGENT_TOKEN` -- bearer token issued by the operator at agent registration (format: `phaze_agent_<32 urlsafe-base64>`)
- `PHAZE_AGENT_SCAN_ROOTS` -- comma-separated list of absolute paths to watch (read from `/whoami` if omitted, but recommended to set explicitly)

## Optional tunable env vars

- `PHAZE_WATCHER_SETTLE_SECONDS=10` -- seconds of mtime stability before posting (D-01)
- `PHAZE_WATCHER_MAX_PENDING_SECONDS=3600` -- stuck-file cap; entries older than this are evicted without posting (D-02)
- `PHAZE_WATCHER_SWEEP_INTERVAL_SECONDS=2` -- sweep task cadence
- `PHAZE_SCAN_CHUNK_SIZE=500` -- used by `scan_directory` task (not the watcher itself, but shared AgentSettings field)

## Import-boundary invariant

This module MUST NOT import `phaze.database`, `phaze.tasks.session`, `sqlalchemy.ext.asyncio`, or `phaze.tasks.agent_worker`. Enforced by `tests/test_task_split.py::test_agent_watcher_does_not_import_phaze_database`. The watcher reaches the database only via the HTTP boundary (DIST-04).

## Phase 29 migration note

Phase 27 lands the watcher in the root `docker-compose.yml` alongside `worker`, `audfprint`, and `panako`. Phase 29 will move all four to a new `docker-compose.agent.yml` and strip them from the root compose (which becomes application-server-only). The watcher module itself does not change.

## Operational notes

- Container restart count climbing in `docker compose ps`: usually transient API boot (~63s budget absorbed by `whoami_with_retry`). If persistent, check `docker compose logs watcher` for `AgentApiAuthError` (RESEARCH Pitfall 7 -- bad PHAZE_AGENT_TOKEN).
- Inotify fallback for NFS/FUSE: swap `Observer` for `watchdog.observers.polling.PollingObserver` in `__main__.py` (one-line change). Not a Phase 27 deliverable.
- Catch-up on startup is intentionally NOT performed (D-04). Operator runs a manual `/pipeline/` scan trigger after a watcher restart if they want to backfill files that landed during downtime.
