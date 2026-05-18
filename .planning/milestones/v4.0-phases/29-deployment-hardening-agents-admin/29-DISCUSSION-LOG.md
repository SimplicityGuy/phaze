# Phase 29: Deployment Hardening & Agents Admin - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-05-15
**Phase:** 29-deployment-hardening-agents-admin
**Areas discussed:** HTTPS termination + CA flow, Heartbeat caller placement, Agents admin page UX, Compose split + dev workflow, Redis hardening, Models setup

---

## HTTPS Termination + CA Flow

### Q1: Where should TLS be terminated for the application-server API?

| Option | Description | Selected |
|--------|-------------|----------|
| uvicorn-direct (Recommended) | `--ssl-keyfile` + `--ssl-certfile` in uvicorn command; no new service; private LAN single-user — no reverse-proxy needed | ✓ |
| Caddy reverse proxy | Adds `caddy` service on :443; easy reload, future routing flexibility | |
| nginx reverse proxy | Manual reload on cert rotation; most YAML/knobs | |

**User's choice:** uvicorn-direct

### Q2: How should the internal CA and server certificate be generated?

| Option | Description | Selected |
|--------|-------------|----------|
| `just init-tls` one-time script (Recommended) | openssl recipe → `./certs/`; operator runs once; outputs CA path | |
| Auto-generate in api entrypoint on first start | Container checks `/certs/ca.crt`; absent → generates; zero operator action | ✓ |
| Use mkcert / smallstep step-ca | External tool with more features; overkill for single-user | |

**User's choice:** Auto-generate in api entrypoint
**Notes:** User chose the zero-touch option over the recommended explicit script. Phase 29 mitigates the "volume nuked → silent regen breaks all agents" risk via a loud multi-line stdout/log banner when a new CA is generated (D-02).

### Q3: How does each file server obtain the CA cert to plant in its httpx trust store?

| Option | Description | Selected |
|--------|-------------|----------|
| Manual copy at setup, env var points at path (Recommended) | scp/rsync `phaze-ca.crt`; new `PHAZE_AGENT_CA_FILE` on AgentSettings | ✓ |
| Mount the same `./certs/` dir via NFS | Shared filesystem; fragile if file servers join later | |
| Bake into agent Docker image at build time | Couples image to deployment; full rebuild for new CA | |

**User's choice:** Manual copy + PHAZE_AGENT_CA_FILE env var

### Q4: How should we verify success-criterion #3 ("swapping the CA causes connection failure")?

| Option | Description | Selected |
|--------|-------------|----------|
| Integration test in pytest (Recommended) | `tests/test_services/test_agent_client_tls.py`; wrong-CA → `httpx.ConnectError` / SSL error | ✓ |
| Manual operator smoke test only | One-paragraph ops-doc | |
| Both | Integration test + ops-doc paragraph | |

**User's choice:** Integration test in pytest

---

## Heartbeat Caller Placement

### Q1: Which always-on agent process should emit the 30s heartbeat?

| Option | Description | Selected |
|--------|-------------|----------|
| agent_worker SAQ process (Recommended) | Trivially knows `worker_pid` + queue depth; if worker dies, heartbeats stop (right signal) | ✓ |
| agent_watcher process | Always-on + Postgres-free, but no queue; would need raw Redis LLEN + fake `worker_pid` | |
| Both (last-seen wins) | Double POST volume; confuses worker_pid semantics | |
| New dedicated `phaze.agent_heartbeat` process | Cleanest separation; adds a third compose service | |

**User's choice:** agent_worker SAQ process

### Q2: How should the heartbeat be scheduled inside the chosen process?

| Option | Description | Selected |
|--------|-------------|----------|
| SAQ cron job @ 30s (Recommended) | Register in `agent_worker.settings.cron_jobs`; reuses SAQ machinery | ✓ |
| asyncio background task launched at SAQ startup | More control; bypasses SAQ cron internals | |
| Threaded loop alongside SAQ | Survives event-loop stalls; mixes threading + asyncio | |

**User's choice:** SAQ cron @ 30s
**Notes:** Phase 29 D-08 explicitly allows the fallback `asyncio.create_task` sleep-loop if SAQ's cron doesn't support 6-field (sub-minute) cron strings.

### Q3: What should the worker do when a heartbeat POST fails after retries?

| Option | Description | Selected |
|--------|-------------|----------|
| Log WARNING and continue (Recommended) | Fire-and-forget; admin UI shows "stale" naturally | ✓ |
| Log + try with longer backoff | Adds state machine complexity | |
| Crash the worker (restart: unless-stopped retry) | Flapping API would bounce all agents | |

**User's choice:** Log WARNING + continue

### Q4: Where should `queue_depth` come from in the heartbeat payload?

| Option | Description | Selected |
|--------|-------------|----------|
| SAQ `Queue.info()` (Recommended) | Type-safe; no raw Redis access; survives SAQ internal changes | ✓ |
| Raw Redis `LLEN saq:phaze-agent-<id>` | Faster but couples to SAQ key layout | |
| Combined (pending + active) | More informative; doubles metric meaning | |

**User's choice:** SAQ `Queue.info()`

---

## Agents Admin Page UX

### Q1: Where should the Agents admin page live in the navigation?

| Option | Description | Selected |
|--------|-------------|----------|
| New `/admin/agents` top-level page (Recommended) | Sets up `admin/` namespace for future operational pages | ✓ |
| Extend `/pipeline/` dashboard with an Agents card | Reuses existing layout + 5s poll; mixed with pipeline ops | |
| Under `/settings/` (new section) | Heavier framing | |

**User's choice:** New `/admin/agents` top-level

### Q2: What `last_seen_at` thresholds define alive / stale / revoked status?

| Option | Description | Selected |
|--------|-------------|----------|
| alive < 90s, stale 90-300s, dead > 300s (Recommended) | 3 missed = stale; ~10 missed = dead; revoked_at overrides | ✓ |
| alive < 60s, stale 60-180s, dead > 180s | Tighter; more false-positives | |
| alive < 120s, stale 120-600s, dead > 600s | Looser; slower outage visibility | |

**User's choice:** alive < 90s / stale 90-300s / dead > 300s

### Q3: How should the admin page refresh without a manual reload?

| Option | Description | Selected |
|--------|-------------|----------|
| HTMX poll every 5s (Recommended) | Matches pipeline dashboard cadence; OOB swap | ✓ |
| HTMX poll every 2s | 2.5× controller load for marginal UX gain | |
| SSE stream | Reuses Phase 28 pattern; over-engineered for a 5-row table | |

**User's choice:** HTMX poll every 5s

### Q4: What columns should the agents table show, and in what default sort order?

| Option | Description | Selected |
|--------|-------------|----------|
| Name, status pill, queue depth, last-seen relative, scan_roots count, actions (Recommended) | Sort: revoked → status rank → last-seen desc; actions = future-revoke placeholder | ✓ |
| Minimal: name, status pill, last-seen relative | Cleanest; loses queue depth visibility | |
| Full debug: name, id, status, queue depth, worker_pid, agent_version, scan_roots, last-seen | Useful for troubleshooting; noisy day-to-day | |

**User's choice:** Recommended (name + status + queue depth + last-seen + scan_roots count + actions)

---

## Compose Split + Dev Workflow

### Q1: How should docker-compose.agent.yml relate to the root docker-compose.yml?

| Option | Description | Selected |
|--------|-------------|----------|
| Standalone, file-server-host only (Recommended) | Self-contained; `docker compose -f docker-compose.agent.yml up` | ✓ |
| Overlay merged with root compose | Single-host parity but confusing override semantics | |
| Both — standalone for prod, overlay for dev | Maintains two similar files | |

**User's choice:** Standalone

### Q2: What Docker image should docker-compose.agent.yml reference?

| Option | Description | Selected |
|--------|-------------|----------|
| Published GHCR image with version tag (Recommended) | `ghcr.io/simplicityguy/phaze:<tag>`; file server doesn't need source tree | ✓ |
| Build from local context | Requires file server to clone repo + build deps | |
| GHCR with build fallback | Adds YAML ambiguity for marginal benefit | |

**User's choice:** Published GHCR image
**Notes:** Phase 29 D-16 uses `${PHAZE_IMAGE_TAG:-latest}` default with explicit comment in `.env.example.agent` recommending version pins for production.

### Q3: What happens to the existing `watcher` and `agent-worker` services in the root docker-compose.yml?

| Option | Description | Selected |
|--------|-------------|----------|
| Remove from root; root becomes app-server-only (Recommended) | Sharp boundary; matches production topology | ✓ |
| Keep on root for single-host dev parity | Easier daily workflow; risks drift | |
| Keep with `profiles: [dev]` flag | Opt-in dev mode; less elegant than standalone | |

**User's choice:** Remove from root
**Notes:** Single-host dev convenience addressed via new `just up-all` recipe (Phase 29 D-18) that runs both compose files together.

### Q4: How should we verify success-criterion #1 ("the api container can't read music files")?

| Option | Description | Selected |
|--------|-------------|----------|
| CI integration test + ops-doc smoke (Recommended) | Pytest YAML-parse assertion + ops-doc two-host paragraph | ✓ |
| CI test only | Reproducible; doesn't validate operator's actual setup | |
| Manual ops-doc smoke only | Light-weight; not CI-enforced | |

**User's choice:** CI test + ops-doc smoke

---

## Redis Hardening

### Q1: How should Redis be exposed and authenticated for cross-host agent access?

| Option | Description | Selected |
|--------|-------------|----------|
| Bind to `${REDIS_BIND_IP}:6379` + `requirepass` from `PHAZE_REDIS_PASSWORD` (Recommended) | `${REDIS_BIND_IP:-127.0.0.1}:6379:6379`; redis-server `--requirepass`; agents get `redis://default:${REDIS_PASSWORD}@${REDIS_HOST}:6379` | ✓ |
| Bind to all interfaces, rely on `requirepass` alone | Rejected by AUTH-03 wording ("bound only to the private LAN interface") | |
| Docker network host mode | Loses Docker network isolation | |

**User's choice:** Bind to ${REDIS_BIND_IP}:6379 + requirepass

---

## Models Setup

### Q1: How should models setup work on a fresh file server?

| Option | Description | Selected |
|--------|-------------|----------|
| `just download-models` once + agent_worker startup check (Recommended) | Operator pre-warms; container refuses to start with empty `/models` | |
| `just download-models` once, no startup check | Trust the operator's README reading; failures surface mid-job | |
| Auto-download on first agent_worker start if `/models` is empty | Zero-touch first boot; ~150MB download blocks startup 2-5 min | ✓ |

**User's choice:** Auto-download on first start
**Notes:** User explicitly preferred zero-touch over the recommended option. Phase 29 D-21 mitigates the slow-first-boot tradeoff by (a) running auto-download AFTER whoami succeeds so bad-token failures fast-fail, (b) logging clear progress, (c) hard-failing the container on network error so `restart: unless-stopped` doesn't infinite-loop. Operators who want instant startup can still pre-warm with `just download-models`.

---

## Claude's Discretion

Areas the user left to Claude's judgment:

- GHCR image tag pinning format (`:latest` default with strong version-pin recommendation in `.env.example.agent`)
- `agent_version` source (`importlib.metadata.version("phaze")` reading pyproject)
- Single-host dev convenience recipe (`just up-all`)
- audfprint/panako sidecar localhost-only enforcement (out-of-scope for Phase 29 — addressed by Phase 28 D-12 structural test)
- Exact CA-regen warning banner copy (multi-line stdout + WARNING log)
- Cert library: `cryptography` (already transitive) vs `openssl` shell-out → `cryptography`
- Cert algorithm: RSA-3072 vs ECDSA P-256 → ECDSA P-256
- Relative time helper (`phaze.utils.humanize.relative_time`)
- Status pill Tailwind class names (match existing palette)
- Admin nav link position (far right, conventional for ops pages)
- 6-field SAQ cron support detection + asyncio fallback (D-08)
- `model_bootstrap` module location: `phaze.tasks._shared.model_bootstrap`
- `PHAZE_AGENT_CA_FILE` default: in-container `/certs/phaze-ca.crt`
- Display of never-heartbeated agents on admin page (show with "never" status)
- CI test pragmatism: YAML-parse over docker-compose-up for filesystem-isolation test

## Deferred Ideas

Surfaced during discussion, captured in CONTEXT.md `<deferred>` for future phases:

- Reverse-proxy fronting (Caddy/nginx/Traefik)
- mTLS for agent boundary (OPS-05)
- Agent self-registration UI (OPS-06)
- Prometheus metrics scrape (OPS-07)
- Per-agent revoke / rotate-token buttons
- Automated CA rotation (`just rotate-ca`)
- Agent-side TLS cert (mTLS prep)
- GHCR publishing for audfprint + panako sidecars
- Multi-agent token-rotation orchestration
- Hierarchical Redis ACLs (per-agent Redis user)
- Watcher-emitted liveness (separate `last_watcher_seen_at`)
- `/admin/agents/{id}` detail page
- Liveness-driven dispatch filter (exclude dead agents)
- Model-download progress bar in admin UI
- CA-expiry alerting
