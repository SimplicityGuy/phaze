<!-- generated-by: gsd-doc-writer -->
# 🚀 Quick Start

Get Phaze running locally and walk a music file through the full pipeline — scan,
fingerprint, analyze, propose, review, execute. This is the fuller companion to the
short Setup block in the [README](../README.md).

Every command below is a real `just` recipe (see `just --list`) or a verified shell
command. Configuration details live in [Configuration](configuration.md).

## 📋 Prerequisites

| Tool | Version | Purpose | Install |
| ---- | ------- | ------- | ------- |
| **Docker + Docker Compose** | Compose v2 | Runs the Postgres, Redis, API, and worker containers | https://docs.docker.com/get-docker/ |
| **uv** | latest | Python package manager (replaces `pip`) | https://docs.astral.sh/uv/ |
| **just** | latest | Command runner for all project recipes | https://just.systems/ |
| **Python** | `>=3.14,<3.15` | Application runtime (managed by `uv`) | https://www.python.org/ |

The `requires-python = ">=3.14,<3.15"` constraint is enforced by `pyproject.toml`.
`uv sync` provisions a matching interpreter if one is not already on your machine.

> **macOS note:** essentia-tensorflow only ships Linux x86_64 wheels. On Apple Silicon
> or Intel macOS the package is skipped by a platform marker, so local audio analysis runs
> inside the Linux Docker containers rather than on the host. Develop on macOS, run analysis
> in Docker.

## 🛠️ Installation

Run these steps from a terminal. Each `just` recipe is defined in the `justfile`.

1. **Clone the repository.**

   ```bash
   git clone https://github.com/SimplicityGuy/phaze.git
   cd phaze
   ```

2. **Install Python dependencies.**

   ```bash
   uv sync          # equivalent: just install
   ```

3. **Create your environment file.**

   ```bash
   cp .env.example .env
   ```

   The defaults in `.env.example` work for single-host local development out of the box
   (the `DATABASE_URL` and `REDIS_URL` already point at the `postgres` and `redis`
   Docker service names). Before going further, review:

   - `SCAN_PATH` — the music directory mounted into the containers for scanning
     (default `/data/music`).
   - `MODELS_PATH` — host directory for essentia models (default `./models`,
     populated in the next step).
   - `REDIS_PASSWORD` — placeholder `changeme` is fine for dev; **set a strong value
     for any networked deployment.**

   See [Configuration](configuration.md) for every variable, its default, and whether
   it is required.

4. **Download the essentia audio-analysis models.**

   ```bash
   just download-models     # runs scripts/download-models.sh -> models/
   ```

   This is required before the analyze stage can run. Skipping it causes the analysis
   step to fail (see [Common setup issues](#-common-setup-issues)).

5. **Start the core services.**

   ```bash
   just up                  # docker compose up -d
   ```

   This launches four containers: `api` (FastAPI), `worker` (SAQ), `postgres`, and `redis`.

6. **Apply database migrations.**

   ```bash
   just db-upgrade          # uv run alembic upgrade head
   ```

   > The `api` container also runs migrations on startup by default
   > (`PHAZE_AUTO_MIGRATE=true`), so the schema is normally already at head after
   > `just up`. Running `just db-upgrade` is a safe, idempotent confirmation —
   > and the explicit command you use when auto-migrate is disabled.

## ✅ First Run / Verify

Confirm the API is healthy:

```bash
curl http://localhost:8000/health
```

Expected response (the endpoint checks the database with a `SELECT 1` before answering):

```json
{"status": "ok"}
```

If you get a connection error, the containers may still be starting — check
`just docker-ps` and `just logs`.

### 🌐 Service URLs

| Service | URL / Address | Stack | Notes |
| ------- | ------------- | ----- | ----- |
| 🖥️ **Web UI / API** | http://localhost:8000 | core (`just up`) | FastAPI app + HTMX admin UI |
| 🐘 **PostgreSQL** | `localhost:5432` | core (`just up`) | user/password `phaze` / `phaze` |
| 🔴 **Redis** | `localhost:6379` | core (`just up`) | bound to `127.0.0.1` in dev; password from `REDIS_PASSWORD` |
| 🎯 **audfprint** | `audfprint:8001` (internal) | agent (`just up-agent`) | landmark fingerprint sidecar |
| 🎼 **panako** | `panako:8002` (internal) | agent (`just up-agent`) | tempo-robust fingerprint sidecar |

> **About the fingerprint sidecars:** `audfprint` (8001) and `panako` (8002) live in
> `docker-compose.agent.yml`, not the core stack. They are reachable on the internal
> Docker network by service name (`http://audfprint:8001`, `http://panako:8002`) and do
> not publish host ports. To run them on the same host for development, use
> `just up-agent` (agent stack only) or `just up-all` (both stacks). Check their health
> with `just audfprint-health` and `just panako-health`.

## 🔄 Your First Workflow

A file moves through these states:
`DISCOVERED → METADATA_EXTRACTED → FINGERPRINTED → ANALYZED → PROPOSAL_GENERATED →
APPROVED → EXECUTED`. You drive each stage from either a `just` recipe (curl wrapper) or
the pipeline dashboard in the Web UI.

1. **Open the pipeline dashboard.**

   Visit http://localhost:8000/pipeline/ — it shows per-stage counts and buttons to
   advance files through the pipeline.

2. **Start a scan** (file discovery against `SCAN_PATH`):

   ```bash
   just scan                # POST /api/v1/scan -> {"batch_id": "...", "message": "Scan started"}
   ```

   The scan runs in the background. Check its progress with the returned batch ID:

   ```bash
   just scan-status <BATCH_ID>     # GET /api/v1/scan/<BATCH_ID>
   ```

3. **Run the pipeline stages.** From the dashboard, advance the discovered files through:

   - **Extract metadata** (mutagen) — `POST /pipeline/extract-metadata`
   - **Fingerprint** (audfprint + panako) — `POST /pipeline/fingerprint`
     (or `just fingerprint`; track progress with `just fingerprint-progress`)
   - **Analyze** (essentia: BPM, key, mood, style) — `POST /pipeline/analyze`
   - **Generate proposals** (LLM rename/path suggestions) — `POST /pipeline/proposals`

   Each button enqueues SAQ jobs handled by the `worker` container. Follow them with
   `just worker-logs`.

4. **Review proposals in the Web UI.**

   Visit http://localhost:8000/proposals/ to see each AI-generated rename/move. Approve
   or reject individually, or use bulk actions. Nothing is moved on disk at this point —
   approval only marks a proposal as ready to execute.

   Duplicate groups surface separately at http://localhost:8000/duplicates/, and concert
   tracklist matches at http://localhost:8000/tracklists/.

5. **Execute the approved batch.**

   Approved proposals are committed to disk through the safe copy-verify-delete protocol
   from the execution view (`POST /execution/start`), with live progress at
   `GET /execution/progress/{batch_id}`. The audit trail of every operation is at
   http://localhost:8000/audit/.

## 🩹 Common Setup Issues

- **Analysis fails with missing essentia models.**
  The analyze stage needs the pre-trained TensorFlow models. If they were never
  downloaded (or `MODELS_PATH` points at an empty directory), run:

  ```bash
  just download-models
  ```

  Confirm files exist under the directory named by `MODELS_PATH` (default `./models`).

- **API returns 500s about missing tables / relations.**
  The schema has not been migrated. Apply migrations and confirm the current revision:

  ```bash
  just db-upgrade      # uv run alembic upgrade head
  just db-current      # uv run alembic current
  ```

- **`just up` fails with a port already in use (8000, 5432, or 6379).**
  Another process is bound to one of the published ports. Stop the conflicting service,
  or change the mapping — `API_PORT` and `REDIS_BIND_IP` are configurable in `.env`.
  Inspect what is running with `just docker-ps`.

- **Fingerprint health checks fail.**
  `audfprint`/`panako` live in the agent stack, not the core stack. If
  `just audfprint-health` or `just panako-health` errors, start the sidecars with
  `just up-agent` (or `just up-all`) first.

## ➡️ Next Steps

- [Architecture Overview](architecture.md) — services, data flow, and the approval pipeline.
- [Configuration](configuration.md) — every environment variable, default, and required setting.
- [Database Schema & Migrations](database.md) — PostgreSQL schema and Alembic workflow.
- [API Reference](api.md) — REST and HTMX endpoints for scan, pipeline, proposals, and execution.
- [Deployment Guide](deployment.md) — distributed two-host (control + agent) production setup.
- [Project Structure](project-structure.md) — codebase layout and module organization.
