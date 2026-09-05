<!-- generated-by: gsd-doc-writer -->
# 🚀 Quick Start

Get Phaze running locally and walk a music file through the full pipeline — scan,
metadata, analyze, propose, review, execute. This is the fuller companion to the
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

> **macOS note:** essentia-tensorflow `>=2.1b6.dev1438` ships native wheels for macOS
> (both Apple Silicon `arm64` and Intel `x86_64`), in addition to Linux `x86_64`. The
> `pyproject.toml` platform marker only skips the dependency on **Linux non-x86_64**
> (e.g. `linux/arm64`, which has no wheel yet) — on macOS it installs and runs natively via
> `uv sync`, so local audio analysis works on the host, not just inside Docker.

## 🛠️ Installation

Run these steps from a terminal. Each `just` recipe is defined in the `justfile`.

1. **Clone the repository.**

   ```bash
   git clone https://github.com/SimplicityGuy/phaze.git
   cd phaze
   ```

2. **Install Python dependencies.**

   ```bash
   uv sync          # just install does this PLUS builds the Tailwind app.css
   ```

   > `just install` runs the `tailwind` recipe first (compiling `assets/src/app.css` →
   > `src/phaze/static/css/app.css` with the standalone Tailwind binary, no Node) and *then*
   > `uv sync`. Bare `uv sync` installs Python deps only and **skips the CSS build**, so prefer
   > `just install` (or run `just tailwind` once) if you are serving the Web UI locally.

3. **Create your environment file.**

   ```bash
   cp .env.example .env
   ```

   The defaults in `.env.example` work for single-host local development out of the box
   (the `DATABASE_URL` and `REDIS_URL` already point at the `postgres` and `redis`
   Docker service names; `PHAZE_QUEUE_URL` defaults to the Postgres DSN, since the SAQ task
   broker has run on Postgres — not Redis — since Phase 36). Before going further, review:

   - `SCAN_PATH` — the music directory mounted into the containers for scanning
     (default `/data/music`).
   - `MODELS_PATH` — host directory for essentia models (default `./models`,
     populated in the next step).
   - `REDIS_PASSWORD` — placeholder `changeme` is fine for dev; **set a strong value
     for any networked deployment.**

   See [Configuration](configuration.md) for every variable, its default, and whether
   it is required.

4. **Provision the essentia audio-analysis models.**

   phaze never downloads models on its own (phaze-ynv6w): the worker validates the directory
   `MODELS_PATH` names at boot and exits naming the missing files otherwise. If you already
   hold the consolidated model directory, point `MODELS_PATH` in `.env` at it. Otherwise
   provision the default bind source once:

   ```bash
   just download-models models     # runs scripts/download-models.sh -> ./models/
   ```

   This is required before the analyze stage can run. Skipping it causes the analysis
   step to fail (see [Common setup issues](#-common-setup-issues)).

5. **Start the core services.**

   ```bash
   just up                  # rebuilds app.css (tailwind), then docker compose up -d
   ```

   `just up` depends on the `tailwind` recipe, so it recompiles `app.css` before starting the
   stack — you always serve fresh CSS. This launches four containers: `api` (FastAPI),
   `worker` (SAQ), `postgres`, and `redis`.

6. **Apply database migrations.**

   ```bash
   just db-upgrade          # uv run alembic upgrade head
   ```

   > The `api` container also runs migrations on startup by default
   > (`PHAZE_AUTO_MIGRATE=true`), so the schema is normally already at head after
   > `just up`. Running `just db-upgrade` is a safe, idempotent confirmation —
   > and the explicit command you use when auto-migrate is disabled.
   >
   > **Host vs container:** `just db-upgrade` runs `uv run alembic` on the **host**, but the
   > shipped `.env` points `DATABASE_URL` at the Docker service DNS name
   > (`…@postgres:5432/phaze`), which does not resolve outside the compose network — the
   > command fails there. Before running it from the host, swap `DATABASE_URL` (and
   > `PHAZE_QUEUE_URL` / `REDIS_URL` alongside it) to their `localhost` forms, as documented
   > at the top of `.env.example`. If auto-migrate is left on, you can skip this step entirely.

## ✅ First Run / Verify

Confirm the API is healthy:

```bash
curl --cacert ./certs/phaze-ca.crt https://localhost:8000/health
```

The container entrypoint bootstraps a self-signed internal CA and execs `uvicorn` with
`--ssl-keyfile`/`--ssl-certfile`, so the server speaks **HTTPS** — hence `--cacert`. Plain
`http://localhost:8000` applies only under `just up-dev`, whose dev overlay skips the
cert-bootstrap entrypoint.

Expected response (the endpoint checks the database with a `SELECT 1` before answering):

```json
{"status": "ok"}
```

If you get a connection error, the containers may still be starting — check
`just docker-ps` and `just logs`.

### 🌐 Service URLs

| Service | URL / Address | Stack | Notes |
| ------- | ------------- | ----- | ----- |
| 🖥️ **Web UI / API** | https://localhost:8000 | core (`just up`) | FastAPI app + HTMX admin UI. HTTPS with a self-signed internal CA — browsers warn until you trust `certs/phaze-ca.crt`; curl needs `--cacert ./certs/phaze-ca.crt`. Plain HTTP only under `just up-dev`. |
| 🐘 **PostgreSQL** | `${POSTGRES_BIND_IP:-127.0.0.1}:5432` | core (`just up`) | user `POSTGRES_USER` (default `phaze`); `POSTGRES_PASSWORD` is **required** — compose fails to parse without it |
| 🔴 **Redis** | `${REDIS_BIND_IP:-127.0.0.1}:6379` | core (`just up`) | bound to `127.0.0.1` in dev; override `REDIS_BIND_IP` to a LAN IP so off-host agents can connect. Password from `REDIS_PASSWORD` (**required** — compose fails to parse without it) |

## 🔄 Your First Workflow

A file advances through six pipeline stages. There is no stored `files.state` column —
each stage's status (`not_started` / `in_flight` / `done` / `skipped` / `failed`) is derived
on read from that stage's own output table (see [Database → Derived per-stage
status](database.md#derived-per-stage-status)). The numbered steps below map 1:1 onto the
stages:

```mermaid
flowchart LR
    D[discovered] --> M[metadata]
    M --> A[analyze]
    A --> P[propose]
    P --> R[review]
    R --> E[apply]
```

You drive each stage from the responsive console (`/` plus `/s/<stage>` workspaces; ⌘K to
search or jump) or the documented HTTP endpoints.

1. **Open the console.**

   Visit https://localhost:8000/ — the console opens on the actionable **Summary**, which
   reports collection metrics, recent activity, operational problems, and the recommended
   next action. The left rail groups Overview, Pipeline, Review, and Operations workspaces;
   clicking a destination swaps the workspace in place. Press **⌘K** at any time for the
   command palette (search files/tracklists/artists or jump to a workspace).

2. **Start a scan** (file discovery, dispatched to a registered agent):

   The **Discover** stage of the DAG rail (`/s/discover`) is the primary way to trigger a
   scan: pick an agent + scan root (and optional subpath) and submit the form. Scans are
   agent-scoped — file discovery is dispatched to a registered agent's `scan_directory`
   job, not run in-process by the api server.

   ```bash
   # Equivalent HTMX form POST against the api host (agent_id must be a registered,
   # non-revoked agent id; scan_root must be one of that agent's configured scan_roots):
   API_HOST=https://localhost:8000
   curl -s --cacert ./certs/phaze-ca.crt -X POST "$API_HOST/pipeline/scans" \
     -d "agent_id=dev-agent" -d "scan_root=/data/music"
   ```

   The scan runs in the background as a `ScanBatch`. Check its progress with the returned
   batch ID (see the Recent Scans panel on the Discover workspace, or poll directly):

   ```bash
   curl -s --cacert ./certs/phaze-ca.crt "$API_HOST/pipeline/scans/<BATCH_ID>"   # HTMX progress-card fragment
   ```

3. **Run the pipeline stages.** From the DAG rail, open each stage workspace (`/s/<stage>`) and advance the discovered files through:

   - **Extract metadata** (mutagen) — `POST /pipeline/extract-metadata`
   - **Analyze** (essentia: BPM, key, mood, style) — `POST /pipeline/analyze`
   - **Generate proposals** (LLM rename/path suggestions) — `POST /pipeline/proposals`

   Each button enqueues SAQ jobs handled by the `worker` container. Follow them with
   `just worker-logs`.

4. **Review proposals in the Web UI.**

   **Propose changes** generates and inspects suggestions; it is not an authorization surface.
   Open **Changes Review** to see each filename + destination decision as an atomic before →
   after diff. Approve, edit, or skip individual rows, or bulk-act only on the reviewed
   selection. The former corpus-wide "approve all high-confidence" action was removed. Nothing
   is moved on disk at this point — approval only marks a proposal as ready to execute.

   Duplicate groups surface under **Duplicates**, and concert tracklist matches under
   **Tracklists**.

5. **Execute the approved batch.**

   Approved proposals are committed to disk through the safe copy-verify-delete protocol
   from **Execute approved** after reviewing its preflight manifest (`POST /execution/start`), with live progress at
   `GET /execution/progress/{batch_id}`. The audit trail of every operation is at
   the **Audit log** workspace (`/s/audit`; legacy `/audit/` redirects there).

## 🧪 Running the tests

The suite needs a real Postgres and Redis. Bring the harness up, then run it:

```bash
just test-db        # shared test harness: Postgres on 5433, Redis on 6380
just check          # lint + typecheck + full suite (auto-provisions if nothing is exported)
```

**One database, one pytest process.** `tests/conftest.py` creates the schema at session start and
drops it at session teardown, so two pytest processes on the same database destroy each other —
whichever finishes first pulls the schema out from under the other, and the loser fails with
hundreds of `relation "…" does not exist` errors that all pass on isolated re-run.

Since phaze-ieqg that is prevented rather than merely documented: `pytest_sessionstart` takes a
Postgres advisory lock on the resolved database and holds it for the whole run. A second process is
refused **before collection** with `SharedTestDatabaseError`, naming the holder's pid. That error
comes from `tests/db_guard.py`; it is a harness precondition, not a bug in the suite.

The trap is ordinary: re-running a subset "just to check something" in a second terminal while the
full suite is still going is enough to trigger it.

**Working in more than one worktree at a time?** Isolate each seat — never share Postgres *or*
Redis:

```bash
just test-db-for <name>
```

It normalizes `<name>` into `<derived>` (hyphens become underscores, plus a short hash of the
original `<name>` so e.g. `my-seat` and `my_seat` can't collide onto one shared seat), creates
`phaze_<derived>_test` + `phaze_<derived>_migrations_test`, and allocates a dedicated Redis
logical database, then prints three exports. **Copy the exports it prints** rather than
constructing the DSN from `<name>` yourself — they agree only when `<name>` has no hyphens.
Export **all three**:

```bash
export TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_<derived>_test"
export MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_<derived>_migrations_test"
export PHAZE_REDIS_URL="redis://localhost:6380/<index>"
```

Exporting only the first is the common mistake: Redis stays shared and one seat's fixture sweep
deletes another's live keys mid-test. Two worktrees with their own `test-db-for` databases are the
supported way to run concurrently.

**When the worktree is finished, hand its Redis index back** — the logical-DB space is finite (64
by default) and shared by every seat on the machine:

```bash
just test-db-release <name>   # frees this seat's index; stops and clears nothing
just test-db-seats            # who holds what, and why each seat is considered in use
just test-db-reclaim          # dry run: which seats a sweep would free (--apply to do it)
```

If allocation ever refuses because the space is full, `just test-db-reclaim` is the answer.
**Never `just test-db-down`** — it removes the containers every other seat is using and destroys
their databases mid-run (phaze-ieqg, phaze-68wky).

Every run prints its resolved target in the pytest header — check it before trusting a green run.
`exclusive` means this process holds the lock; `unlocked` means it does not, and the run is
unprotected. See [CLAUDE.md](../CLAUDE.md) for the full rules.

## 🩹 Common Setup Issues

- **The worker exits with `essentia models are not provisioned at /models`.**
  The analyze stage needs the pre-trained TensorFlow models, and phaze never downloads
  them (phaze-ynv6w). The message lists every missing or wrong-size file. Point
  `MODELS_PATH` at the consolidated model directory, or provision the default one:

  ```bash
  just download-models models
  ```

  Confirm the 68 `.pb`/`.json` files exist under the directory named by `MODELS_PATH`
  (default `./models`).

- **API returns 500s about missing tables / relations.**
  The schema has not been migrated. Apply migrations and confirm the current revision:

  ```bash
  just db-upgrade      # uv run alembic upgrade head
  just db-current      # uv run alembic current
  ```

- **`just up` fails with a port already in use (8000, 5432, or 6379).**
  Another process is bound to one of the published ports. Stop the conflicting service,
  or change the mapping — `API_PORT`, `POSTGRES_BIND_IP` (the 5432 publish, default
  `127.0.0.1`), and `REDIS_BIND_IP` are configurable in `.env`.
  Inspect what is running with `just docker-ps`.

## ➡️ Next Steps

- [Architecture Overview](architecture.md) — services, data flow, and the approval pipeline.
- [Configuration](configuration.md) — every environment variable, default, and required setting.
- [Database Schema & Migrations](database.md) — PostgreSQL schema and Alembic workflow.
- [API Reference](api.md) — REST and HTMX endpoints for scan, pipeline, proposals, and execution.
- [Deployment Guide](deployment.md) — distributed two-host (control + agent) production setup.
- [Project Structure](project-structure.md) — codebase layout and module organization.
