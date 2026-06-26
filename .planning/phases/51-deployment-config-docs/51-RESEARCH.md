# Phase 51: Deployment, config & docs - Research

**Researched:** 2026-06-26
**Domain:** Deployment (Docker Compose), pydantic-settings config surface, feature-flag wiring, OCI/Tailscale/Postgres provisioning specs (homelab change-prompt), operator docs
**Confidence:** HIGH (codebase + SAQ source verified; PG-role behavior empirically proven against live Postgres 18; Tailscale/OCI specs CITED from current vendor docs)

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** New setting `cloud_burst_enabled: bool`, default `False`, alias `PHAZE_CLOUD_BURST_ENABLED` (mirrors `enable_saq_ui` at `config.py:292`). Off-by-default; the just-built Phase 49/50 cloud machinery becomes OFF on the next deploy until the operator flips it — intended.
- **D-02:** OFF = pure pre-Phase-49 behavior — ALL files (incl. long ≥ `cloud_route_threshold_sec`) route to the fileserver/local queue. Suspends Phase 49's "never analyze a long file locally" invariant ONLY while OFF. Long files may time out locally (bounded ~4h timeout + `retries=1`, fails cleanly as `ANALYSIS_FAILED`).
- **D-03:** Toggle gates EVERY cloud entry point: (a) the duration-routing decision in `_route_discovered_by_duration` / `trigger_analysis` (`routers/pipeline.py`); (b) the Phase 50 staging/top-up cron; (c) the Phase 49 `release_awaiting_cloud` cron. One toggle ⇒ zero cloud activity anywhere.
- **D-04:** In-flight cloud work drains; OFF only stops NEW cloud work. `PUSHING`/`PUSHED` files finish; no mid-transfer/mid-analysis abort, no scratch reclaim.
- **D-05:** Host-installed `tailscaled` on the OCI A1 (apt + `tailscale up`, owned by homelab OpenTofu/runbook), NOT a sidecar container. Compose uses the host's Tailscale connectivity. No `TS_AUTHKEY` secret, no `network_mode` sidecar wiring.
- **D-06:** Worker-only compose: only the agent SAQ worker (`PHAZE_ROLE=agent`, `kind=compute`). NO watcher, NO fingerprint sidecars, NO media mount (DIST-04). Mirrors `docker-compose.agent.yml` minus media-bound services.
- **D-07:** Scratch = a named docker volume mounted at `cloud_scratch_dir` (not a host bind mount). `MODELS_PATH` mounted `rw`; CA cert mounted `ro`.
- **D-08:** Image line: `image: ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}-arm64`. The Phase 47 image is a SEPARATE `-arm64` tag (NOT multi-arch); the `-arm64` suffix is mandatory. Production pins `PHAZE_IMAGE_TAG=v5.0.0` → `v5.0.0-arm64`.
- **D-09:** OCI A1 infra = OpenTofu IaC in the homelab repo (NOT the phaze repo — workspace boundary). Phase 51 delivers a ready-to-paste homelab change prompt (Phase 36 "Step D" precedent) specifying the OpenTofu OCI A1 module: Always-Free A1 Ampere, arm64 Ubuntu 24.04, boot volume, SSH key, networking/security-list.
- **D-10:** Tailscale ACL + least-privilege Postgres queue-broker role are applied in homelab, spec'd by phaze. The change prompt carries the EXACT ACL JSON (A1→`lux:{5432,6379,8000}` + `nox→A1:22`) and the EXACT PG role SQL. phaze stays source-of-truth spec; live infra lives in homelab.
- **D-11:** Least-privilege PG role = full SQL in the spec/runbook. Compute agent connects via `PHAZE_QUEUE_URL` for the `saq_jobs` table ONLY (NOT the app ORM — DIST-04). Document `CREATE ROLE` + minimal `GRANT`s. Grant-timing: pick the safer of (a) CREATE-on-first-boot vs (b) pre-create-table.
- **D-12:** Full cloud-burst config table in `docs/configuration.md` (knob, env var, default, `_FILE`-secret?), plus master-toggle semantics. Source descriptions from the `Field(...)` descriptions in `config.py`.
- **D-13:** ONE new `docs/cloud-burst.md` (compose/deploy walkthrough + runbook + homelab-OpenTofu reference + ACL JSON + PG role SQL copies + smoke test); config subsection stays in `configuration.md`; pointer from `deployment.md`; index entry in `docs/README.md`.

### Claude's Discretion
- PG-role grant-timing approach (CREATE-on-first-boot vs pre-create table) — pick the safer of D-11's two options. **[RESOLVED by empirical test — see "Least-Privilege Postgres Role" below: both options still require `CREATE ON SCHEMA`; recommended synthesis = pre-create tables AND grant CREATE.]**
- Exact compose env-var list / `.env` layout for the cloud-agent file, within worker-only + no-media + named-scratch + `-arm64` constraints.
- Whether the master toggle is read once at startup vs per-request/per-cron-tick (prefer per-tick so flipping doesn't require a restart, unless that complicates the routing seam). **[See "Master Toggle Wiring" — per-tick-no-restart is NOT achievable with the module-level `settings` singleton; recommend startup-read = restart required, consistent with every other knob.]**
- Exact wording/structure of the homelab change prompt, within the D-09/D-10 spec content.
- Whether a deployed-compute-agent smoke-test step is a doc checklist vs a scripted check.

### Deferred Ideas (OUT OF SCOPE)
- Cost/throughput-aware routing beyond the fixed duration threshold — CLOUDROUTE-05.
- Dynamic multi-compute-agent target discovery via heartbeat — static single-A1 config is sufficient.
- Hard-stop + reclaim of in-flight cloud work on toggle-OFF — drain-only chosen (D-04).
- Tailscale sidecar container packaging — host-installed `tailscaled` chosen (D-05).
- Terraform/OCI-CLI alternatives for provisioning — superseded by the homelab OpenTofu directive (D-09).
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| CLOUDDEPLOY-01 | Cloud-agent compose brings up the compute agent: Tailscale connectivity, no media mount, scratch volume, arm64 image. | "Compose Pattern" section: new `docker-compose.cloud-agent.yml`, worker-only, `-arm64` image, named scratch volume, host-Tailscale via `network_mode: host`, mirror+extend `test_agent_compose.py`. |
| CLOUDDEPLOY-02 | All cloud-burst params configurable via pydantic-settings with `_FILE`-secret support: threshold, max in-flight, agent concurrency, scratch dir, push SSH target, cloud queue name, master toggle. | "Config Knobs Audit" — full enumeration + 2 FLAGGED criterion-named knobs (`agent concurrency`=`WORKER_MAX_JOBS` exists; `cloud queue name`=`PHAZE_AGENT_QUEUE` is a raw `os.environ` read, NOT a pydantic field). |
| CLOUDDEPLOY-03 | Runbook: OCI Always-Free A1 provisioning + Tailscale ACL scoping A1→`lux:{5432,6379,8000}`+`nox→A1:22` + least-privilege Postgres broker role. | "OCI A1 OpenTofu Spec", "Tailscale Grants ACL" (copy-paste block), "Least-Privilege Postgres Role" (empirically-verified SQL). |
| CLOUDDEPLOY-04 | Single config toggle disables the whole feature, reverting to all-local with no other change. | "Master Toggle Wiring" — exact short-circuit sites; one-line gate in `_route_discovered_by_duration`; cron no-op; backfill gate flag. |
</phase_requirements>

## Project Constraints (from CLAUDE.md)

- **Python 3.14 exclusively**; **`uv` only** — never bare `pip`/`python`/`pytest`/`mypy`; always `uv run …`.
- **ruff** (line length 150, target py313), **mypy** strict (excludes `tests/`, `services/`), **pre-commit** with frozen SHAs — all hooks pass before commit.
- **85% min coverage**, Codecov with service flags. **`just` is the command runner**; GitHub Actions delegate to `just`.
- **One PR per feature/phase**, own git worktree, never push to main, never `--no-verify`.
- **READMEs/docs up to date alongside code** (this phase IS largely docs — D-12/D-13 satisfy this directly).
- pyproject section order `[build-system]`→`[project]`→`[project.scripts]`→`[tool.*]`→`[dependency-groups]`, alphabetically sorted deps.
- **YAML/compose:** new compose file must pass the existing `yamllint` strict, `check-jsonschema` (GitHub workflows), and `actionlint` hooks where applicable.
- **No new pip dependency is needed for this phase** (toggle + compose + docs only). OpenTofu/OCI provider live in the homelab repo, not phaze — so phaze adds zero supply-chain surface here.

## Summary

Phase 51 is a **deployment + config + docs** phase with exactly **one net-new code path**: the `cloud_burst_enabled` master toggle (D-01). Everything else is (a) a new worker-only compose file mirroring `docker-compose.agent.yml`, (b) documentation of already-shipped Phase 49/50 knobs, and (c) a ready-to-paste homelab change prompt that specifies OCI A1 OpenTofu, a Tailscale grants ACL, and a least-privilege Postgres broker role. No new pip packages, no DB migration (the toggle is config-only), no new SAQ task.

The genuinely unfamiliar surface — Tailscale ACL JSON, OCI A1 OpenTofu, and the Postgres broker role — has been grounded against current vendor docs and, crucially, **empirically verified against the live test Postgres**. The single most load-bearing finding: **SAQ's `init_db()` runs `CREATE TABLE IF NOT EXISTS saq_versions` unconditionally on every `queue.connect()`, and PostgreSQL checks `CREATE ON SCHEMA` privilege *before* the `IF NOT EXISTS` existence short-circuit** — so a broker role that lacks `CREATE ON SCHEMA public` cannot connect *even when all SAQ tables already exist*. This invalidates the naive "pre-create the tables and grant only table DML" reading of D-11's option (b). The safe least-privilege role is: pre-create tables as the full role (so the broker never owns/migrates them) **AND** grant `CREATE ON SCHEMA public` (required for init_db's version probe) **AND** table-DML + sequence-USAGE, with **zero** grants on any app-ORM table (that is the DIST-04 boundary that actually matters).

**Primary recommendation:** Add `cloud_burst_enabled` to `ControlSettings` (mirror `enable_saq_ui`); gate it with a single condition inside `_route_discovered_by_duration` (`is_long = settings.cloud_burst_enabled and duration is not None and duration >= threshold`), a top-of-function no-op in `stage_cloud_window`, and an explicit early-return in `trigger_backfill_cloud`. Author `docker-compose.cloud-agent.yml` (worker-only, `network_mode: host`, `-arm64`, named scratch) with a parallel YAML-parse test. Document everything in `docs/cloud-burst.md` + the config table in `docs/configuration.md`, and emit the homelab change prompt with the verified PG SQL, the Tailscale grants block, and the OCI A1 OpenTofu spec (specced at the **current** June-2026 Always-Free limit: **2 OCPU / 12 GB**, not 4/24).

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Master toggle decision (route long→local vs hold for cloud) | Control plane (`routers/pipeline.py`) | — | The control plane owns ALL routing decisions; `cloud_burst_enabled` lives on `ControlSettings`. |
| Cloud staging cron gate | Control plane (`tasks/release_awaiting_cloud.py::stage_cloud_window`) | — | Single "stay one ahead" driver runs on the controller worker only. |
| Backfill gate | Control plane (`routers/pipeline.py::trigger_backfill_cloud`) | — | Backfill is a control-side HTTP trigger that funnels through the same router seam. |
| Compute-agent runtime (drain queue, analyze, push-receive, cleanup) | Compute agent (OCI A1, `agent_worker.py`, `kind=compute`) | — | Phase 48/50 already built this; Phase 51 only packages/deploys it. |
| SAQ queue broker (`saq_jobs`) | Database (lux Postgres) | Control + Agent (psycopg3 clients) | Phase 36 made the broker Postgres; the compute agent connects via `PHAZE_QUEUE_URL` for `saq_jobs` only. |
| Cache / rate-limit / counters (Redis) | Database (lux Redis) | Control + Agent | Redis is cache-plane only post-Phase-36. |
| Network access control (A1↔lux, nox→A1) | Tailscale tailnet (homelab-applied) | — | Host-installed `tailscaled` (D-05); ACL spec authored by phaze, applied in homelab. |
| OCI A1 instance + boot volume + VCN/security-list | OCI (homelab OpenTofu IaC) | — | Workspace boundary (D-09): infra authored in homelab, specced by phaze. |
| Least-privilege broker DB role | Database (lux Postgres, homelab-applied) | — | Role SQL authored by phaze (D-10/D-11), applied in homelab. |

## Standard Stack

This phase introduces **no new pip packages**. The relevant components already exist in the repo or live in the homelab repo. Versions verified in the running environment / current vendor docs:

### Core (already present — verified)
| Component | Version | Purpose | Provenance |
|-----------|---------|---------|------------|
| SAQ | 0.26.4 | Postgres queue broker; the compute agent's `saq_jobs` consumer | `[VERIFIED: uv run python -c "import saq; print(saq.__version__)"]` |
| psycopg3 + psycopg_pool | (SAQ `[postgres]` extra) | The compute agent's libpq pool to `PHAZE_QUEUE_URL` | `[VERIFIED: saq/queue/postgres.py imports psycopg]` |
| pydantic-settings | (existing) | `cloud_burst_enabled` field + `_FILE` secret machinery | `[VERIFIED: config.py]` |
| Docker Compose | 2.x | `docker-compose.cloud-agent.yml` | `[CITED: project constraint]` |

### Supporting (infra — homelab repo, NOT phaze)
| Component | Purpose | When to Use | Provenance |
|-----------|---------|-------------|------------|
| OpenTofu + `oracle/oci` provider | OCI A1 instance, VCN/subnet/security-list, boot volume, SSH key | Homelab change prompt (D-09) | `[CITED: registry.terraform.io/providers/oracle/oci]` |
| Tailscale tailnet policy (grants) | ACL scoping A1↔lux + nox→A1 | Homelab change prompt (D-10) | `[CITED: tailscale.com/docs/reference/syntax/grants]` |
| host `tailscaled` (apt) on Ubuntu 24.04 arm64 | A1 tailnet connectivity (D-05) | Runbook | `[ASSUMED — standard Tailscale install; verify at provision time]` |
| `rsync` + `ssh` on the A1 | Receive pushed files into scratch (Phase 50 `push_file` target) | Runbook — must be present on the A1 host/image | `[VERIFIED: src/phaze/tasks/push.py uses rsync over ssh]` |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `network_mode: host` for the compute container | bridge networking + tailnet IP literals in env | host networking gives MagicDNS + tailnet routing for free on a single-purpose VM; bridge requires hard-coding lux's `100.x` tailnet IP and loses MagicDNS resolution inside the container. Host mode is simplest here (only one service, no port conflicts). |
| Grant `CREATE ON SCHEMA public` to the broker role | Dedicated `saq` schema + `search_path` on both roles | the dedicated-schema approach is genuinely tighter (broker gets zero rights in `public`) but requires moving the *live* control-plane `saq_jobs` table and changing the full role's `search_path` — a riskier homelab change. Recommended as documented optional hardening, not the default. |
| `docker-compose.cloud-agent.yml` (own file) | Profiles/overrides on `docker-compose.agent.yml` | a standalone file matches the existing `docker-compose.agent.yml` precedent and keeps the worker-only invariants testable in isolation. |

**Installation:** None. (`uv sync` unchanged; no dependency edits.)

## Package Legitimacy Audit

> **N/A — this phase installs no external packages into the phaze repo.** The toggle is a config field, the compose file references an already-published GHCR image, and the OpenTofu/OCI-provider/Tailscale tooling lives in the **homelab** repo (separate workspace, D-09). No `uv add`, no new `[dependency-groups]` entry. slopcheck not run because there is nothing to check. If the planner later decides to add a Python smoke-test helper that pulls a new dependency, run the Package Legitimacy Gate at that point.

## Master Toggle Wiring

The single net-new code path (CLOUDDEPLOY-04). All factual claims below are `[VERIFIED]` against the current source.

### The setting (mirror `enable_saq_ui`)
Add to `ControlSettings` (it owns routing + staging; `get_settings()` returns `ControlSettings` under `PHAZE_ROLE=control`):

```python
# Phase 51 D-01: master cloud-burst kill switch. Default False — a fresh deploy is all-local
# until the operator provisions the A1 + push config and explicitly opts in. When False, EVERY
# cloud entry point no-ops: long files route local (suspending Phase 49's "never local" invariant,
# D-02), the staging cron stages nothing, and backfill-to-cloud is rejected. In-flight PUSHING/
# PUSHED work drains (D-04). Mirrors enable_saq_ui (config.py:292): plain bool, PHAZE_* alias.
cloud_burst_enabled: bool = Field(
    default=False,
    validation_alias=AliasChoices("PHAZE_CLOUD_BURST_ENABLED", "cloud_burst_enabled"),
    description="Master switch for the cloud-burst feature. False (default) reverts to all-local analysis (Phase 51, CLOUDDEPLOY-04).",
)
```

### The three exact short-circuit sites
`routers/pipeline.py` reads the module-level `from phaze.config import settings` singleton (`ControlSettings`). `stage_cloud_window` reads `get_settings()`. Both resolve `cloud_burst_enabled`.

1. **Routing seam — `_route_discovered_by_duration` (`routers/pipeline.py:307-317`)** `[VERIFIED]`
   The `is_long` flag drives the only cloud branch. One condition makes OFF → all-local:
   ```python
   # current (Phase 50):
   is_long = duration is not None and duration >= threshold_sec
   # Phase 51 (D-02): OFF -> nothing is "long" -> every file falls to the local branch
   is_long = settings.cloud_burst_enabled and duration is not None and duration >= threshold_sec
   ```
   This is the cleanest seam: with OFF, no file ever sets `FileState.AWAITING_CLOUD`; short and long alike append to `local_files`. Covers BOTH `trigger_analysis` and `trigger_analysis_ui` (both call this function). `threshold_sec`/`settings` are already in scope. **Note:** the function signature takes `threshold_sec` but reads `settings` at module level — the gate can read `settings.cloud_burst_enabled` directly (consistent with how the caller already passes `settings.cloud_route_threshold_sec`). Consider passing `cloud_enabled: bool` as a parameter for unit-testability/parity with the threshold param (Claude's discretion).

2. **Staging cron — `stage_cloud_window` (`tasks/release_awaiting_cloud.py:109`)** `[VERIFIED]`
   Add a top-of-function gate BEFORE the advisory lock / agent selection:
   ```python
   cfg = get_settings()
   if not cfg.cloud_burst_enabled:  # type: ignore[attr-defined]
       return {"staged": 0, "skipped": 0}
   max_in_flight = cfg.cloud_max_in_flight  # type: ignore[attr-defined]
   ```
   Clean no-op (NOT a raise — matches the existing "no compute agent" no-op contract, T-50-cron-raise). Held `AWAITING_CLOUD` files stay put. **Note:** with OFF, the routing seam stops *producing* `AWAITING_CLOUD` files, but any held from before the flip stay held until a re-flip — acceptable per D-04 (OFF stops NEW cloud work; it does not forcibly drain the held set).

3. **Backfill — `trigger_backfill_cloud` (`routers/pipeline.py:639-710`)** `[VERIFIED — see Open Question 1]`
   Backfill resets `ANALYSIS_FAILED` long files to `DISCOVERED` then routes them via `_route_discovered_by_duration`. With the gate in site #1, those files would route **local** when OFF and re-time-out — the opposite of the operator's intent. Recommend an **explicit early-return** when OFF:
   ```python
   if not settings.cloud_burst_enabled:
       return templates.TemplateResponse(..., context={"request": request, "count": 0, "disabled": True})
   ```
   so a disabled feature does not silently reset 144 files to DISCOVERED and re-route them local. (`release_awaiting_cloud` as a *separate* cron no longer exists — Phase 50 replaced it with `stage_cloud_window` in the same file; D-03's clause (c) is satisfied by gating site #2.)

### Startup-read vs per-tick (discretion D resolution)
The module-level `settings = _build_default_settings()` singleton (`config.py:741`) is constructed once at import; pydantic-settings reads env at construction. `settings.cloud_burst_enabled` therefore returns a value fixed at process start. **Per-tick-without-restart is NOT achievable** without re-constructing settings or reading `os.environ` directly each call — a departure from EVERY other knob (all require a restart to change). **Recommendation: startup-read; flipping the toggle requires a control-plane (controller worker + api) restart.** Document this explicitly. In-flight `PUSHING`/`PUSHED` drains across the restart because state is durable in Postgres (D-04). This is simplest, consistent, and matches the "complicates the seam" escape clause in the discretion text.

## Compose Pattern

### Recommended file: `docker-compose.cloud-agent.yml`
(name matches the roadmap-evolution log in STATE.md). Worker-only, mirroring `docker-compose.agent.yml`'s structure minus media-bound services.

```yaml
# docker-compose.cloud-agent.yml — OCI A1 compute-agent compose (Phase 51, CLOUDDEPLOY-01).
# Worker-only: agent SAQ worker, kind=compute, NO watcher, NO fingerprint sidecars, NO media mount.
# Host-installed tailscaled (D-05) provides connectivity to lux; network_mode: host gives the
# container MagicDNS + tailnet routing for free (single-purpose VM, no port conflicts).
services:
  worker:
    image: ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}-arm64   # D-08: -arm64 suffix MANDATORY
    command: uv run saq phaze.tasks.agent_worker.settings
    network_mode: host                       # reach lux via host tailnet + MagicDNS
    env_file: .env
    environment:
      - PHAZE_ROLE=agent
      - PHAZE_AGENT_KIND=compute             # relaxes the empty-scan-roots gate (config.py:470)
    volumes:
      - cloud_scratch:${PHAZE_CLOUD_SCRATCH_DIR:?PHAZE_CLOUD_SCRATCH_DIR required}:rw   # D-07 named volume
      - "${MODELS_PATH:-./models}:/models:rw"   # D-07 auto-download
      - "${CA_PATH:-./certs}:/certs:ro"         # D-07 operator CA cert, ro
    restart: unless-stopped
volumes:
  cloud_scratch:
```

**Invariants the new file MUST satisfy (and a parallel `tests/test_deployment/test_cloud_agent_compose.py` MUST assert — mirror `test_agent_compose.py`):**
- `services` is exactly `{worker}` (no watcher/audfprint/panako).
- NO `DATABASE_URL` / `POSTGRES_*` env on any service (DIST-04 — verified pattern in `test_agent_compose.py:69`). The agent reaches Postgres ONLY via `PHAZE_QUEUE_URL` (saq_jobs broker) + the HTTP API.
- NO `SCAN_PATH` / media bind mount anywhere (the compute agent owns no media).
- Image is `ghcr.io/simplicityguy/phaze:…` and **ends with `-arm64`** (new assertion vs the agent test).
- `PHAZE_ROLE=agent` and `PHAZE_AGENT_KIND=compute` on `worker`.
- MODELS mount is `rw`, CA mount is `ro`, scratch is a **named volume** (not a host bind).

### `.env` for the compute agent (no-media, production)
The compute agent's required + cloud-burst env (sourced from `config.py` `AgentSettings`):
```bash
PHAZE_QUEUE_URL=postgresql://phaze_broker:<pw>@lux:5432/phaze   # libpq form; broker role (NOT phaze)
PHAZE_REDIS_URL=redis://:<redis_pw>@lux:6379/0                  # production mode REQUIRES a password
PHAZE_AGENT_API_URL=https://lux:8000                           # production mode REQUIRES https://
PHAZE_AGENT_ENV=production
PHAZE_AGENT_KIND=compute
PHAZE_AGENT_QUEUE=phaze-agent-<compute_agent_id>               # raw env, read at SAQ import time
PHAZE_AGENT_TOKEN_FILE=/run/secrets/agent_token                # _FILE secret
PHAZE_CLOUD_SCRATCH_DIR=/scratch                               # MUST match control's PHAZE_COMPUTE_SCRATCH_DIR
WORKER_MAX_JOBS=1                                              # RAM-bound single analysis on 12GB A1 (see audit)
MODELS_PATH=/models
PHAZE_AGENT_CA_FILE=/certs/phaze-ca.crt
# NO DATABASE_URL (DIST-04). NO SCAN_PATH / PHAZE_AGENT_SCAN_ROOTS (kind=compute relaxes it).
```
Two production guards in `AgentSettings` (`config.py:648-680`) WILL fire if violated: `_enforce_https_in_production` (https agent_api_url) and `_enforce_redis_password_in_production` (passworded redis_url). The compose/runbook must satisfy both.

## Architecture Patterns

### System Architecture Diagram (deploy view)
```
                         Tailscale tailnet (default-deny grants ACL)
   ┌─────────────────────────────────────────────────────────────────────────┐
   │                                                                           │
   │   nox (file server)                              OCI A1 (compute agent)   │
   │   ┌──────────────────┐   rsync over SSH (push)   ┌─────────────────────┐  │
   │   │ docker-compose    │  ── nox → A1:22 ───────▶ │ host tailscaled      │  │
   │   │  .agent.yml       │                          │ docker-compose       │  │
   │   │  (worker+watcher  │                          │  .cloud-agent.yml    │  │
   │   │   +fprint+media)  │                          │  worker (kind=compute│  │
   │   └──────────────────┘                          │   no media, scratch  │  │
   │            │                                      │   volume, -arm64 img)│  │
   │            │ HTTP API + saq_jobs + cache          └──────────┬──────────┘  │
   │            ▼                                                  │             │
   │   lux (application server)  ◀── A1 → lux:{5432,6379,8000} ────┘             │
   │   ┌───────────────────────────────────────────────────────┐               │
   │   │ api(:8000)   Postgres(:5432: app ORM + saq_jobs broker) │               │
   │   │ controller worker (stage_cloud_window cron)   Redis(:6379 cache) │      │
   │   │ broker role 'phaze_broker' → saq_jobs ONLY (least-priv)│               │
   │   └───────────────────────────────────────────────────────┘               │
   └───────────────────────────────────────────────────────────────────────────┘

   cloud_burst_enabled=False  ⇒  control plane routes long files LOCAL; staging cron no-ops;
                                  backfill rejected. A1 idle. (CLOUDDEPLOY-04)
```

### Pattern 1: bool kill-switch field (mirror `enable_saq_ui`)
Plain `bool` `Field` with `PHAZE_*` `AliasChoices`, default chosen for the safe state. See `config.py:292` (verified). The toggle gate is a cheap attribute read at each decision point.

### Pattern 2: cron gate as a clean no-op
Every cloud cron returns its normal `{"staged": 0, "skipped": 0}` shape on the disabled path — never raises (matches T-50-cron-raise discipline in `release_awaiting_cloud.py:20`).

### Pattern 3: standalone testable compose file
Mirror `docker-compose.agent.yml` + `test_agent_compose.py` (pure `yaml.safe_load`, no docker daemon). The test asserts source-file invariants against raw `${VAR}` tokens (no interpolation).

### Anti-Patterns to Avoid
- **Adding `DATABASE_URL` to the cloud compose** — breaks DIST-04. The agent reaches Postgres only via `PHAZE_QUEUE_URL` (saq_jobs) + the HTTP API.
- **Multi-arch assumption** — there is NO multi-arch manifest; the `-arm64` suffix is mandatory (D-08, `docs/arm64-agent-image.md:189-194`).
- **Granting the broker role app-ORM table privileges** — it must reach `saq_jobs`/`saq_stats`/`saq_versions` ONLY.
- **Gating backfill via the routing seam alone** — would silently re-route 144 files to local and re-time-them-out (see Open Question 1).
- **Reintroducing any object-storage assumption** — v5.0 chose rsync-over-Tailscale, NO buckets.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Secret file mounting for the compute agent | Custom env-file reader | Existing `_FILE` machinery (`SECRET_FILE_FIELDS`, `_resolve_secret_files`) | Already supports `agent_token`, `queue_url`, `redis_url`, `push_ssh_key`, `push_known_hosts` — the compose just mounts `*_FILE` paths. |
| SAQ table creation on the broker DB | Manual DDL replicating saq schema, or an Alembic step | Let the **full** `phaze` role (control plane) run SAQ's own `init_db()` first | SAQ owns the `saq_jobs`/`saq_stats`/`saq_versions` migration set; Alembic must never touch saq_jobs (Phase 36 principle). |
| Compute-agent network restriction | iptables on the A1 | Tailscale grants ACL (default-deny) | The tailnet is the trust boundary; grants give exactly `A1→lux:{5432,6379,8000}` + `nox→A1:22` and nothing else. |
| OCI instance provisioning | Console click-path in phaze docs | Homelab OpenTofu module (D-09) | IaC is reproducible + lives in the correct workspace; phaze emits the spec only. |

**Key insight:** This phase is mostly *wiring + specification*, not construction. The strongest failure mode is mis-specifying the broker DB role or the ACL — both verified below.

## Least-Privilege Postgres Role (CLOUDDEPLOY-03, D-11)

### Empirical finding (decisive) `[VERIFIED: live Postgres 18 test, 2026-06-26]`
SAQ's `init_db()` (`saq/queue/postgres.py:153`) runs, on **every** `queue.connect()`, in order:
1. `pg_try_advisory_lock(saq_lock_keyspace=0, 0)` — early-returns ONLY if another session holds the lock (never the steady-state case).
2. **`CREATE TABLE IF NOT EXISTS saq_versions (version INT)` — UNCONDITIONAL.**
3. `SELECT version FROM saq_versions` → if `== target_version (3)` → return (no further DDL).

I tested a least-privilege role (`GRANT USAGE` on schema, table-DML on the three saq tables, sequence USAGE) against a Postgres where the SAQ tables were **pre-created by the full role**:

| Test | Result |
|------|--------|
| `CREATE TABLE IF NOT EXISTS saq_versions` (table EXISTS), as broker w/o `CREATE ON SCHEMA` | **`ERROR: permission denied for schema public`** — PG checks schema-CREATE *before* the IF-NOT-EXISTS short-circuit |
| `SELECT version FROM saq_versions` | OK (returned 3) |
| `INSERT INTO saq_jobs (...)` exercising the `lock_key SERIAL` | OK (lock_key=1) with `GRANT USAGE, SELECT ON SEQUENCE saq_jobs_lock_key_seq` |
| `CREATE TABLE evil_table` | ERROR permission denied (confirms no schema CREATE) |
| `pg_try_advisory_lock(0,0)` | OK (no grant needed) |
| `LISTEN "saq:…"; NOTIFY "saq:…"` | OK (no grant needed) |

**Conclusion:** D-11 option (b) "pre-create tables and grant table-scoped privileges only" **does NOT work** — the broker cannot even complete `init_db()` without `CREATE ON SCHEMA public`, regardless of whether the tables exist. Both of D-11's options therefore require `CREATE ON SCHEMA`. The **safe synthesis** is: pre-create the tables with the full role (so the broker never *owns* or *migrates* them — its init_db short-circuits at the version check), **AND** grant the broker `CREATE ON SCHEMA public` (for the unavoidable `saq_versions` probe), table-DML on the three SAQ tables, and sequence USAGE — with **zero** grants on any app-ORM table. The security boundary that matters (DIST-04: no read access to `files`, `metadata`, `agents`, etc.) is fully preserved because a freshly-created role has no privileges on the `phaze`-owned domain tables and we grant none.

### Recommended SQL (for the homelab change prompt + `docs/cloud-burst.md`)
```sql
-- Run as the phaze owner / a superuser on the lux Postgres, in the phaze database.
-- The control plane (full 'phaze' role) must boot FIRST so SAQ has already created
-- saq_jobs/saq_stats/saq_versions and migrated them to the current version.

CREATE ROLE phaze_broker LOGIN PASSWORD '<strong-unique-password>';   -- use a secret/_FILE in deploy

-- USAGE to resolve objects; CREATE is REQUIRED by SAQ init_db's unconditional
-- `CREATE TABLE IF NOT EXISTS saq_versions` (PG checks schema-CREATE before IF-NOT-EXISTS).
GRANT USAGE, CREATE ON SCHEMA public TO phaze_broker;

-- Queue table DML (the broker dequeues, sweeps, writes stats).
GRANT SELECT, INSERT, UPDATE, DELETE ON saq_jobs, saq_stats, saq_versions TO phaze_broker;

-- saq_jobs.lock_key is SERIAL -> an INSERT assigns from this sequence.
GRANT USAGE, SELECT ON SEQUENCE saq_jobs_lock_key_seq TO phaze_broker;

-- DIST-04: grant NOTHING on the app ORM tables. Verify the role is blind to app data:
--   SET ROLE phaze_broker; SELECT * FROM files LIMIT 1;  -- must ERROR: permission denied
```

**Postgres-version note** `[VERIFIED on PG18; CITED for general PG15+]`: On PostgreSQL **15+**, the `public` schema no longer grants `CREATE` to `PUBLIC` by default, so the explicit `GRANT … CREATE ON SCHEMA public` is genuinely required and meaningful. On PG **<15**, `PUBLIC` already had `CREATE` on `public`, so the grant is redundant there (and the role could already create tables). Confirm the lux Postgres major version in the runbook.

**Optional stronger hardening (document, don't default):** dedicated `saq` schema — `CREATE SCHEMA saq; ALTER TABLE saq_jobs/saq_stats/saq_versions SET SCHEMA saq;` then `ALTER ROLE phaze SET search_path = saq, public;` and `ALTER ROLE phaze_broker SET search_path = saq;` with `GRANT USAGE, CREATE ON SCHEMA saq` (and NO rights in `public`). This gives the broker zero ability to touch `public` at all, but it relocates the **live** control-plane queue table and changes the full role's search_path — a riskier homelab change. Defer unless the operator wants strict public-schema lockdown.

## Tailscale Grants ACL (CLOUDDEPLOY-03, D-10)

`[CITED: tailscale.com/docs/reference/syntax/grants, /docs/reference/migrate-acls-grants]` — Tailscale's current recommended form is **`grants`** (generally available, easier than the legacy `acls`/`ports` form; the two can coexist). Grants are **default-deny**: once any policy exists, the A1 (tagged) gets ONLY what is explicitly granted.

### Copy-paste tailnet policy block (the spec phaze carries)
```jsonc
{
  // The A1 must come up tagged 'tag:cloud-agent' (tailscale up --advertise-tags=tag:cloud-agent,
  // or auth-key with the tag). 'hosts' map lux/nox to their tailnet IPs (or use tags if preferred).
  "tagOwners": {
    "tag:cloud-agent": ["autogroup:admin"]
  },
  "hosts": {
    "lux": "100.x.x.x",   // application server tailnet IP (Postgres queue + Redis cache + HTTP API)
    "nox": "100.y.y.y"    // file server tailnet IP (push initiator)
  },
  "grants": [
    // A1 compute agent -> lux: saq_jobs broker (5432), Redis cache (6379), app HTTP API (8000).
    {
      "src": ["tag:cloud-agent"],
      "dst": ["lux"],
      "ip": ["tcp:5432", "tcp:6379", "tcp:8000"]
    },
    // nox file server -> A1: SSH for the rsync-over-SSH push (Phase 50 push_file target).
    {
      "src": ["nox"],
      "dst": ["tag:cloud-agent"],
      "ip": ["tcp:22"]
    }
  ]
}
```
Notes: with default-deny, the A1 cannot reach anything else on the tailnet, and nothing else can reach the A1 except `nox:22`. `5432` is required because the SAQ broker is Postgres (Phase 36); the A1 still has NO `DATABASE_URL` (DIST-04 holds — it touches only `saq_jobs`). If `port 22` collides with Tailscale-SSH policy, use a normal `sshd` (plain `tcp:22` grant is sufficient; Tailscale SSH `ssh` rules are a separate construct and NOT needed here).

## OCI Always-Free A1 OpenTofu Spec (CLOUDDEPLOY-03, D-09)

`[CITED: registry.terraform.io/providers/oracle/oci; OCI free-tier docs]`. phaze authors the **spec** only; the homelab repo authors the `.tf`.

### ⚠️ Capacity/limit gotcha (load-bearing) `[CITED, MEDIUM confidence — verify at provision time]`
Multiple current sources report that **as of June 2026 the OCI Always-Free Ampere A1 limit was reduced to 2 OCPU / 12 GB total** (previously 4 OCPU / 24 GB). This **matches the project's own assumption** (auto-memory: "RAM-bound on the 12 GB Always-Free shape"; CLOUDSCALE-01 deferred). **Spec the A1 at 2 OCPU / 12 GB**, not 4/24, and set `WORKER_MAX_JOBS=1` on the compute agent (single concurrent analysis, RAM-bound). Always-Free A1 capacity is also region-constrained ("Out of Capacity" is common) — the runbook should note retrying across availability domains/regions or using a small provisioning retry loop.

### OpenTofu module spec (what the homelab `.tf` must declare)
```hcl
# Provider: oracle/oci. Resources the homelab module must create:
resource "oci_core_instance" "phaze_compute_a1" {
  availability_domain = <pick an AD with A1 capacity>
  compartment_id      = var.compartment_id
  display_name        = "phaze-cloud-agent"
  shape               = "VM.Standard.A1.Flex"        # Always-Free eligible (Ampere arm64)
  shape_config {
    ocpus         = 2                                 # current Always-Free limit (June 2026)
    memory_in_gbs = 12
  }
  source_details {
    source_type = "image"
    source_id   = <Canonical Ubuntu 24.04 Minimal aarch64 image OCID for the region>
  }
  create_vnic_details {
    subnet_id        = oci_core_subnet.phaze_public.id
    assign_public_ip = true                           # for outbound + tailscale bootstrap
  }
  metadata = { ssh_authorized_keys = var.ssh_public_key }
}
# Plus: oci_core_vcn, oci_core_subnet, oci_core_internet_gateway, oci_core_route_table,
#       and a security list / NSG. Boot volume defaults are Always-Free within the 200GB total.
```
**Security-list / NSG note:** Tailscale provides the actual A1↔lux / nox→A1 access control (the grants ACL above), so the OCI security list only needs to allow **outbound** (for `tailscaled` to reach the tailnet relays/DERP) and may keep inbound tightly closed except what the operator needs for first-boot SSH bootstrap (or bootstrap entirely over Tailscale once `tailscaled` is up). Document: install `tailscaled` + `rsync` in cloud-init / the runbook so the A1 can receive pushes and reach lux.

### Deploy ordering (Phase 36 "Step D" precedent — datum@nox / datum@lux)
1. **homelab:** OpenTofu apply → A1 up (Ubuntu 24.04 arm64, 2 OCPU/12 GB); cloud-init installs `tailscaled` + `rsync`; `tailscale up --advertise-tags=tag:cloud-agent`.
2. **homelab:** apply the tailnet grants ACL (A1↔lux + nox→A1).
3. **homelab (lux Postgres):** run the `phaze_broker` role SQL (control plane already booted → SAQ tables exist).
4. **phaze release:** ship v5.0.x → GHCR publishes `…:v5.0.0-arm64`.
5. **A1:** populate `.env` (broker `PHAZE_QUEUE_URL`, agent token, scratch dir, `WORKER_MAX_JOBS=1`); `docker compose -f docker-compose.cloud-agent.yml up -d`.
6. **lux control plane:** set `PHAZE_CLOUD_BURST_ENABLED=true`; restart the controller worker + api (toggle is startup-read).
7. **smoke test:** see Validation Architecture.

## Config Knobs Audit (CLOUDDEPLOY-02, D-12)

Full enumeration of cloud-burst knobs. Descriptions sourced from the `Field(...)` text in `config.py` `[all VERIFIED against config.py]`.

| Knob | Env var (alias) | Class | Default | Bounds | `_FILE`? | Status |
|------|-----------------|-------|---------|--------|----------|--------|
| **master toggle** | `PHAZE_CLOUD_BURST_ENABLED` | ControlSettings | `False` | — | no | **NEW (D-01)** |
| threshold | `PHAZE_CLOUD_ROUTE_THRESHOLD_SEC` | ControlSettings | `5400` | gt0, lt86400 | no | exists (Phase 49) |
| max in-flight | `PHAZE_CLOUD_MAX_IN_FLIGHT` | ControlSettings | `2` | gt0, lt100 | no | exists (Phase 50) |
| push max attempts | `PHAZE_PUSH_MAX_ATTEMPTS` | ControlSettings | `3` | gt0, lt20 | no | exists (Phase 50) |
| compute scratch dir (control mirror) | `PHAZE_COMPUTE_SCRATCH_DIR` | ControlSettings | `None` | — | no | exists (Phase 50) |
| cloud scratch dir (agent) | `PHAZE_CLOUD_SCRATCH_DIR` | AgentSettings | `None` | — | no | exists (Phase 50) — MUST match `PHAZE_COMPUTE_SCRATCH_DIR` |
| push SSH host | `PHAZE_PUSH_SSH_HOST` | AgentSettings | `None` | — | no | exists (Phase 50) |
| push SSH user | `PHAZE_PUSH_SSH_USER` | AgentSettings | `None` | — | no | exists (Phase 50) |
| push timeout | `PHAZE_PUSH_TIMEOUT_SEC` | AgentSettings | `600` | gt0, lt86400 | no | exists (Phase 50) |
| push connect timeout | `PHAZE_PUSH_CONNECT_TIMEOUT_SEC` | AgentSettings | `30` | gt0, lt3600 | no | exists (Phase 50) |
| **push SSH key** | `PHAZE_PUSH_SSH_KEY` (`…_FILE`) | AgentSettings | `None` | — | **YES** (whitespace-PRESERVED) | exists (Phase 50) |
| **push known_hosts** | `PHAZE_PUSH_KNOWN_HOSTS` (`…_FILE`) | AgentSettings | `None` | — | **YES** (whitespace-PRESERVED) | exists (Phase 50) |
| agent token | `PHAZE_AGENT_TOKEN` (`…_FILE`) | AgentSettings | required | — | **YES** | exists |
| queue/broker DSN | `PHAZE_QUEUE_URL` (`…_FILE`) | all | libpq default | — | **YES** | exists (Phase 36) |
| redis cache DSN | `PHAZE_REDIS_URL` (`…_FILE`) | all | default | — | **YES** | exists |

### ⚠️ Two criterion-named knobs that need a planner decision
Success criterion #2 names "**agent concurrency**" and "**cloud queue name**". Cross-check:

1. **"agent concurrency" → `WORKER_MAX_JOBS`** `[VERIFIED]` — `worker_max_jobs: int = 8` (`config.py:233`, `BaseSettings`). It has **no explicit `PHAZE_*` alias**, but pydantic-settings binds the bare uppercased `WORKER_MAX_JOBS` (documented at `configuration.md:70`), and the agent/controller workers read it as their SAQ `concurrency`. **It IS configurable via pydantic-settings — no new field needed.** Recommendation: document `WORKER_MAX_JOBS` as the compute-agent concurrency knob and set it to **`1`** in the cloud compose (single RAM-bound analysis on the 12 GB A1; CLOUDSCALE-01 deferred). Optional polish: add a `PHAZE_WORKER_MAX_JOBS` alias for naming consistency (cosmetic, not required).

2. **"cloud queue name" → `PHAZE_AGENT_QUEUE`** `[VERIFIED — GAP]` — the per-agent queue name (`phaze-agent-<agent_id>`) is read via **`os.environ.get("PHAZE_AGENT_QUEUE")` at SAQ module-import time** in `agent_worker.py:250`, NOT as a pydantic-settings field. It is a **required** operator-set env var, but it is technically not "configurable via pydantic-settings" as criterion #2's wording implies. **Structural reason:** SAQ requires the Queue object at module import, before `get_settings()` constructs (Phase 26 D-16) — that is *why* it is a raw env read. **Planner decision needed (flagged ASSUMED):** either (a) accept and **document** `PHAZE_AGENT_QUEUE` as the cloud queue name knob, noting it is an operator env var read at import time (recommended — moving it into the settings class fights the import-time requirement), or (b) add a settings field that shadows it (awkward; duplicates the import-time read). Recommend (a); confirm in discuss-phase.

**Net new config work for this phase = exactly ONE field (`cloud_burst_enabled`).** Everything else is documentation. The two criterion-named items above are already operator-configurable; the only open item is whether the planner treats `PHAZE_AGENT_QUEUE`'s non-pydantic status as a doc note (recommended) or new code.

## Runtime State Inventory

> Included because this is a deployment/config phase whose off-by-default toggle and broker role change live behavior on the next redeploy.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | **`AWAITING_CLOUD` / `PUSHING` / `PUSHED` FileRecords** already exist in the live DB from Phase 49/50 deploys. On the v5.0 redeploy, `cloud_burst_enabled` defaults False ⇒ the staging cron stops topping the window; in-flight `PUSHING`/`PUSHED` drains (D-04); NEW long files route local. Held `AWAITING_CLOUD` rows from before the flip stay held until the operator turns cloud on (acceptable per D-04). | Doc note in `cloud-burst.md`: "off-by-default means the just-built cloud feature ships dormant; flip `PHAZE_CLOUD_BURST_ENABLED=true` after provisioning. Pre-existing AWAITING_CLOUD rows release once enabled." |
| Live service config | **Tailscale tailnet ACL** (homelab-applied), **OCI A1 instance + VCN/security-list** (OpenTofu, homelab), **`phaze_broker` Postgres role** (lux, homelab). None of these live in the phaze repo (D-09/D-10 workspace boundary). | Emit the homelab change prompt carrying the exact ACL JSON + PG SQL + OpenTofu spec; homelab agent applies. |
| OS-registered state | **host `tailscaled`** on the A1 (apt + `tailscale up --advertise-tags=tag:cloud-agent`); **`rsync`/`ssh`** must be present on the A1 to receive pushes. | Runbook step (cloud-init or manual). The A1 host, not the container, runs tailscaled (D-05). |
| Secrets / env vars | NEW: `phaze_broker` DB password (in the A1's `PHAZE_QUEUE_URL` / `_FILE`); the A1's agent token (`PHAZE_AGENT_TOKEN_FILE`); the compute agent's SSH host key must be in nox's `PHAZE_PUSH_KNOWN_HOSTS` (Phase 50 strict known_hosts). `PHAZE_CLOUD_BURST_ENABLED` is a new (non-secret) env on the lux control plane. | Document all in `cloud-burst.md`; nox's `push_known_hosts` must be re-provisioned with the A1's host key after the A1 is up. |
| Build artifacts | The `-arm64` image tag (`ghcr.io/simplicityguy/phaze:v5.0.0-arm64`) is published by the Phase 47 CI `build-arm64` job (already shipped). The cloud compose pins it. | No phaze build change; just pin `PHAZE_IMAGE_TAG` in the A1 `.env`. |

**The canonical question — what runtime state still holds the old behavior after every repo file is updated?** The control plane's *behavior* changes only when the operator (1) provisions the A1 + broker role + ACL and (2) sets `PHAZE_CLOUD_BURST_ENABLED=true` and restarts the control plane. Until then, the redeploy is all-local (intended, D-01).

## Common Pitfalls

### Pitfall 1: broker role can't connect despite pre-created tables
**What goes wrong:** Following D-11 option (b) literally (pre-create + table-DML only) → the compute agent's `queue.connect()` fails with `permission denied for schema public`.
**Why:** SAQ `init_db()` runs `CREATE TABLE IF NOT EXISTS saq_versions` unconditionally; PG checks schema-CREATE before the existence short-circuit (empirically verified).
**Avoid:** grant `CREATE ON SCHEMA public` to the broker role (synthesis SQL above).
**Warning signs:** compute agent container crash-loops at startup with a psycopg permission error.

### Pitfall 2: backfill re-times-out files when cloud is OFF
**What goes wrong:** gating only the routing seam → `trigger_backfill_cloud` resets 144 `ANALYSIS_FAILED` files to `DISCOVERED` and routes them LOCAL, where they time out again.
**Avoid:** explicit early-return in `trigger_backfill_cloud` when `cloud_burst_enabled` is False (Open Question 1).

### Pitfall 3: forgetting the `-arm64` suffix
**What goes wrong:** `image: …/phaze:${PHAZE_IMAGE_TAG:-latest}` (no suffix) pulls the x86 image → won't run on the A1 (or pulls a non-existent multi-arch manifest).
**Avoid:** mandatory `-arm64` suffix (D-08); assert it in `test_cloud_agent_compose.py`.

### Pitfall 4: production guards fire on the compute agent
**What goes wrong:** `PHAZE_AGENT_ENV=production` with an `http://` API URL or passwordless Redis → `AgentSettings` raises at startup.
**Avoid:** `PHAZE_AGENT_API_URL=https://lux:8000` and a passworded `PHAZE_REDIS_URL` (`config.py:648-680`).

### Pitfall 5: scratch dir skew
**What goes wrong:** `PHAZE_CLOUD_SCRATCH_DIR` (A1) ≠ `PHAZE_COMPUTE_SCRATCH_DIR` (lux control) → the control plane builds a `process_file` scratch_path the A1 never wrote to → sha256/transfer failure.
**Avoid:** document them as a matched pair; both must equal the named-volume mount path (e.g. `/scratch`).

### Pitfall 6: toggle flip without restart
**What goes wrong:** operator sets `PHAZE_CLOUD_BURST_ENABLED=true` but the running controller still reads the import-time singleton (False) → nothing happens.
**Avoid:** document that flipping the toggle requires a control-plane restart (startup-read).

## Code Examples

### Master toggle field (verified pattern)
```python
# Source: src/phaze/config.py:292 (enable_saq_ui) — VERIFIED
cloud_burst_enabled: bool = Field(
    default=False,
    validation_alias=AliasChoices("PHAZE_CLOUD_BURST_ENABLED", "cloud_burst_enabled"),
    description="Master switch for cloud-burst; False reverts to all-local analysis (CLOUDDEPLOY-04).",
)
```

### Routing-seam gate (one line)
```python
# Source: src/phaze/routers/pipeline.py:308 — VERIFIED current line
is_long = settings.cloud_burst_enabled and duration is not None and duration >= threshold_sec
```

### Cron gate
```python
# Source: src/phaze/tasks/release_awaiting_cloud.py:121 — VERIFIED insertion point
cfg = get_settings()
if not cfg.cloud_burst_enabled:           # type: ignore[attr-defined]
    return {"staged": 0, "skipped": 0}
max_in_flight = cfg.cloud_max_in_flight   # type: ignore[attr-defined]
```

### Broker role SQL — see "Least-Privilege Postgres Role" above (empirically verified).
### Tailscale grants block — see "Tailscale Grants ACL" above.
### OCI A1 OpenTofu spec — see "OCI Always-Free A1 OpenTofu Spec" above.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Tailscale `acls` + `ports` (legacy) | **`grants`** (`src`/`dst`/`ip`) | GA, recommended as of 2026 | Use the grants form for the ACL spec; simpler, default-deny. |
| OCI Always-Free A1 = 4 OCPU / 24 GB | **2 OCPU / 12 GB total** | reported June 2026 | Spec the A1 at 2/12; `WORKER_MAX_JOBS=1`. Verify current limit at provision time. |
| Phase 49 `release_awaiting_cloud` drain cron | **`stage_cloud_window`** (bounded top-up) in the same file | Phase 50 | D-03 clause (c) is satisfied by gating `stage_cloud_window`; there is no separate release cron to gate. |

**Deprecated/outdated:**
- Object-storage / presigned-URL staging — superseded by rsync-over-Tailscale (v5.0).
- Multi-arch single-tag image — deferred (CLOUDIMG-04); the `-arm64` suffix tag is current.

## Environment Availability

| Dependency | Required By | Available (dev host) | Version | Fallback |
|------------|------------|----------------------|---------|----------|
| Docker + compose | new compose file (parse/test) | ✓ (colima, arm64) | running | — |
| Postgres (test) | PG-role verification | ✓ | 18-alpine (port 5433) | — |
| `uv` | all commands | ✓ (project constraint) | — | — |
| OpenTofu / `oci` provider | homelab `.tf` | ✗ (homelab repo, not phaze) | — | spec-only; homelab applies |
| `tailscale`/`tailscaled` | A1 connectivity | ✗ (A1 host) | — | runbook install step |
| `rsync`/`ssh` on the A1 | Phase 50 push target | ✗ (A1 host) | — | runbook/cloud-init install |

**Missing with no fallback (blocking the LIVE deploy, not the phaze PR):** the OCI A1 + Tailscale ACL + broker role are all homelab-applied; the phaze PR (toggle + compose + docs + tests) does not need them to merge. The live cloud-burst smoke test is verified-docs / deferred-to-manual until the A1 exists (consistent with the 29-HUMAN-UAT precedent in STATE.md Blockers).

## Validation Architecture

> `workflow.nyquist_validation` not disabled → section included. Focus: master-toggle behavior, compose invariants, config round-trips that ARE automatable. Live A1 smoke is manual/deferred.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (existing) |
| Config | `pyproject.toml` `[tool.pytest...]` (existing) |
| Quick run | `uv run pytest tests/test_deployment/ -x` |
| Full suite | `uv run pytest --cov --cov-report=term-missing` (≥85%) |

### Phase Requirements → Test Map
| Req | Behavior | Test Type | Automated Command | Exists? |
|-----|----------|-----------|-------------------|---------|
| CLOUDDEPLOY-04 | OFF ⇒ long file routes local (no `AWAITING_CLOUD`) | unit | `uv run pytest tests/test_pipeline/test_route_by_duration.py -k cloud_burst_disabled` | ❌ Wave 0 |
| CLOUDDEPLOY-04 | ON ⇒ long file held `AWAITING_CLOUD` (regression of existing) | unit | existing routing test, parametrized on the toggle | ⚠️ extend |
| CLOUDDEPLOY-04 | OFF ⇒ `stage_cloud_window` no-ops | unit | `uv run pytest tests/test_tasks/test_stage_cloud_window.py -k disabled` | ❌ Wave 0 |
| CLOUDDEPLOY-04 | OFF ⇒ `trigger_backfill_cloud` early-return (no DISCOVERED reset) | unit | `tests/test_pipeline/...::test_backfill_disabled` | ❌ Wave 0 |
| CLOUDDEPLOY-01 | cloud compose: worker-only, no media, no DATABASE_URL, named scratch, `-arm64` image | unit (YAML parse) | `uv run pytest tests/test_deployment/test_cloud_agent_compose.py` | ❌ Wave 0 (mirror `test_agent_compose.py`) |
| CLOUDDEPLOY-02 | `cloud_burst_enabled` parses from `PHAZE_CLOUD_BURST_ENABLED`; default False | unit | `tests/test_config/...::test_cloud_burst_enabled_alias_and_default` | ❌ Wave 0 |
| CLOUDDEPLOY-02 | `_FILE` round-trip for a cloud secret (e.g. `PHAZE_QUEUE_URL_FILE`) | unit | existing `_resolve_secret_files` test pattern; add a cloud-knob case | ⚠️ extend |
| CLOUDDEPLOY-03 | broker role least-privilege (can DML saq_jobs, cannot read app tables) | integration (real PG) | `uv run pytest tests/integration/test_broker_role.py` (needs `just test-db`) | ❌ Wave 0 (optional — high value, proves the SQL) |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_deployment/ tests/test_config/ -x`
- **Per wave merge:** full suite `uv run pytest --cov` (≥85%).
- **Phase gate:** full suite green + `pre-commit run --all-files` before `/gsd:verify-work`.

### Wave 0 Gaps
- [ ] `tests/test_deployment/test_cloud_agent_compose.py` — covers CLOUDDEPLOY-01 (mirror `test_agent_compose.py`, add `-arm64` + no-media + named-scratch + single-service assertions).
- [ ] toggle unit tests (routing OFF/ON, cron no-op, backfill early-return) — CLOUDDEPLOY-04.
- [ ] config test for `cloud_burst_enabled` alias/default — CLOUDDEPLOY-02.
- [ ] (optional, high-value) `tests/integration/test_broker_role.py` — proves the least-privilege SQL against `just test-db` (CLOUDDEPLOY-03). Marks the only automatable slice of the infra spec.
- Live A1 end-to-end smoke (push→analyze→cleanup on the real A1) is **manual/deferred** (no A1 hardware in CI; 29-HUMAN-UAT precedent).

## Security Domain

> `security_enforcement` not disabled → section included.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | yes | Agent bearer token (`_FILE` secret), hashed in `agents`; SSH key-based push auth (Phase 50). |
| V4 Access Control | yes | DIST-04: compute agent reaches Postgres ONLY for `saq_jobs` (least-priv `phaze_broker` role); Tailscale default-deny grants; no app-ORM grants. |
| V5 Input Validation | partial | New compose file passes yamllint/check-jsonschema; toggle is a typed bool. |
| V6 Cryptography | yes | SSH key + known_hosts whitespace-preserved (`SECRET_FILE_PRESERVE_WHITESPACE`); TLS to lux:8000 (production https guard); never log secrets (D-13 discipline). |
| V9 Communications | yes | All A1↔lux traffic over Tailscale (WireGuard); strict known_hosts on push; https + passworded Redis enforced in production. |

### Known Threat Patterns for this deploy
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Compute agent reads app data (FileRecord/metadata) | Information disclosure | least-priv `phaze_broker` role with ZERO app-table grants (verify `SELECT * FROM files` ERRORs); DIST-04. |
| Over-broad DB role (broker can create/alter app tables) | Elevation of privilege | grant only `CREATE ON SCHEMA` (unavoidable for init_db) + saq-table DML; no ownership of app tables; optional dedicated `saq` schema for stricter lockdown. |
| A1 reachable from the wider tailnet / internet | Spoofing/Tampering | Tailscale default-deny grants (only `nox:22` inbound, only `lux:{5432,6379,8000}` outbound); OCI security list outbound-mostly. |
| Half-written / corrupt pushed file analyzed | Tampering | rsync `--partial-dir` + atomic rename + app-level sha256 verify (Phase 50, unchanged). |
| Toggle left ON insecurely by default | — | default `False` (D-01) — feature ships dormant; opt-in only after provisioning. |

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | OCI Always-Free A1 is currently 2 OCPU / 12 GB (reduced June 2026) | OCI spec | If still 4/24, the A1 could run higher `WORKER_MAX_JOBS`; spec is conservative either way. Verify at provision time. |
| A2 | "cloud queue name" (criterion #2) = `PHAZE_AGENT_QUEUE`, and documenting the existing import-time env var satisfies CLOUDDEPLOY-02 (vs adding a pydantic field) | Config audit | If the planner/operator requires it as a true pydantic-settings field, that is NEW code (awkward vs the SAQ import-time requirement). **Confirm in discuss-phase.** |
| A3 | "agent concurrency" (criterion #2) = `WORKER_MAX_JOBS` and is sufficient as-is | Config audit | Low — it is already env-configurable; only a `PHAZE_*` alias is missing (cosmetic). |
| A4 | `network_mode: host` is the right way to give the container host-Tailscale connectivity | Compose | If host networking is undesired, fall back to bridge + tailnet-IP env literals (loses MagicDNS). |
| A5 | host `tailscaled` install + `rsync`/`ssh` presence on the A1 is standard and runbook-able | Env availability | Low — standard Ubuntu packages; documented in runbook. |
| A6 | lux Postgres is v15+ (so `CREATE ON SCHEMA` grant is meaningful) | PG role | If <15, the grant is redundant (role already had CREATE); confirm major version in runbook. |
| A7 | The compute agent's `queue.connect()` runs SAQ `init_db()` (the broker role hits the CREATE-on-schema requirement) | PG role | VERIFIED in code (`connect()`→`init_db()`); low risk. The empirical PG behavior is VERIFIED, not assumed. |

## Open Questions

1. **Backfill behavior when cloud is OFF.**
   - Known: `trigger_backfill_cloud` resets `ANALYSIS_FAILED` long files to `DISCOVERED` and routes via the (now-gated) seam → they'd go local and re-time-out.
   - Unclear: should backfill no-op (recommended) or be allowed to proceed local?
   - Recommendation: explicit early-return when OFF (Pitfall 2). Confirm with operator in discuss.

2. **`PHAZE_AGENT_QUEUE` as criterion-named "cloud queue name."**
   - Known: it is a raw import-time env read, not a pydantic field (structural).
   - Recommendation: document it as the knob (A2). Confirm whether that satisfies CLOUDDEPLOY-02 or a settings field is required.

3. **Held `AWAITING_CLOUD` rows across a toggle flip.**
   - Known: D-04 = OFF stops NEW cloud work; in-flight drains. Pre-existing `AWAITING_CLOUD` rows are neither in-flight nor newly produced.
   - Recommendation: leave them held; they release when cloud is re-enabled (the staging cron resumes). Document this so the "Awaiting cloud" card isn't mistaken for a bug while OFF.

## Sources

### Primary (HIGH confidence)
- `src/phaze/config.py` — `enable_saq_ui` pattern (L292), `ControlSettings`/`AgentSettings` cloud knobs, `_FILE` secret machinery, production guards. `[VERIFIED]`
- `src/phaze/routers/pipeline.py` — `_route_discovered_by_duration` (L254-335), `trigger_analysis`/`_ui`, `trigger_backfill_cloud` (L639). `[VERIFIED]`
- `src/phaze/tasks/release_awaiting_cloud.py` — `stage_cloud_window` (the single cloud cron; replaced the Phase 49 release cron). `[VERIFIED]`
- `src/phaze/tasks/controller.py` — cron registration (`stage_cloud_window` `*/5`), startup. `[VERIFIED]`
- `src/phaze/tasks/agent_worker.py` — `PHAZE_AGENT_QUEUE` import-time read (L250), compute scratch janitor, queue construction. `[VERIFIED]`
- `src/phaze/tasks/push.py` — rsync-over-SSH, strict known_hosts, `_FILE` secrets. `[VERIFIED]`
- `.venv/.../saq/queue/postgres.py` + `postgres_migrations.py` — `init_db()` unconditional `CREATE TABLE IF NOT EXISTS saq_versions`, `saq_jobs`/`saq_stats`/`saq_versions` + `saq_jobs_lock_key_seq`. `[VERIFIED — SAQ 0.26.4]`
- **Live Postgres 18 test (2026-06-26)** — broker role `permission denied for schema public` on `CREATE TABLE IF NOT EXISTS` of an EXISTING table without `CREATE ON SCHEMA`; INSERT works with sequence USAGE; advisory lock + LISTEN/NOTIFY need no grant. `[VERIFIED — empirical]`
- `docker-compose.agent.yml` + `tests/test_deployment/test_agent_compose.py` — compose template + invariant test pattern. `[VERIFIED]`
- `docs/configuration.md`, `docs/deployment.md`, `docs/README.md`, `docs/arm64-agent-image.md:189-194` — doc conventions + `-arm64` tag scheme. `[VERIFIED]`

### Secondary (MEDIUM confidence — current vendor docs)
- Tailscale grants syntax/migration/examples — `[CITED]` https://tailscale.com/docs/reference/syntax/grants , /docs/reference/migrate-acls-grants , /docs/reference/examples/grants
- OCI A1 OpenTofu + free-tier limit (2 OCPU/12 GB as of June 2026) — `[CITED]` registry.terraform.io/providers/oracle/oci ; oneuptime.com OCI+OpenTofu guide ; OCI free-tier breakdowns

### Tertiary (LOW confidence — verify at provision time)
- Exact current OCI Always-Free A1 OCPU/RAM limit and regional capacity (vendor changes these without notice).

## Metadata

**Confidence breakdown:**
- Master toggle wiring: HIGH — exact lines verified; one-line gate; startup-read precedent confirmed.
- Compose pattern: HIGH — mirrors a verified, tested existing file; `-arm64` scheme documented.
- PG broker role: HIGH — empirically proven against live Postgres 18; SAQ source confirms the unconditional DDL.
- Config audit: HIGH — every knob cross-checked against `config.py`; 2 criterion gaps flagged.
- Tailscale ACL: MEDIUM — current grants syntax cited; tailnet IPs/tags must be filled by homelab.
- OCI A1 OpenTofu: MEDIUM — provider resources cited; free-tier limit is vendor-volatile (flagged A1).

**Research date:** 2026-06-26
**Valid until:** ~2026-07-26 for codebase claims (stable); ~2026-07-10 for OCI free-tier limits / Tailscale syntax (vendor-volatile — re-verify at provision time).
</content>
</invoke>
