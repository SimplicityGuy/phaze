---
phase: 29
slug: deployment-hardening-agents-admin
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-05-16
---

# Phase 29 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Derived from `29-RESEARCH.md` §"Validation Architecture".

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.3 + pytest-asyncio 1.3.0 (`asyncio_mode = "auto"` already configured) |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/<area> -x -q` (per-area run via `just test`) |
| **Full suite command** | `uv run pytest --cov=phaze --cov-report=term-missing` (`just test-cov`) |
| **Coverage threshold** | 85% project-wide (`[tool.coverage.report] fail_under = 85`) |
| **Estimated runtime** | ~60–90 seconds full suite (current baseline) |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/<targeted-file-or-dir> -x -q`
- **After every plan wave:** Run `uv run pytest -x -q` (full suite, fast mode)
- **Before `/gsd:verify-work`:** Full suite must be green AND coverage ≥85%
- **Pre-commit hooks:** ruff + ruff-format + mypy local hook + bandit + actionlint MUST pass
- **Max feedback latency:** ~5 seconds per-file; ~90 seconds full suite

---

## Per-Task Verification Map

> Populated post-planning. Each row corresponds to one Wave 0 test or a follow-on
> task that asserts the listed secure behavior. Test files marked ❌ are authored
> as the FIRST task in the owning plan (Wave 0 within the plan, before any
> production code lands).

| Req ID | Behavior | Test Type | Automated Command | File Exists |
|--------|----------|-----------|-------------------|-------------|
| DIST-01 | app-server compose declares no `SCAN_PATH`/`MODELS_PATH`/`OUTPUT_PATH` mounts on `api` or `worker` (controller) | structural-parse | `uv run pytest tests/test_deployment/test_api_filesystem_isolation.py -x` | ❌ Wave 0 |
| AUTH-02 | wrong-CA `httpx` client → `httpx.ConnectError`; correct-CA → success | integration (real TLS smoke server) | `uv run pytest tests/test_services/test_agent_client_tls.py -x` | ❌ Wave 0 |
| AUTH-02 | `phaze.cert_bootstrap.ensure_certs_present()` generates CA + leaf on first call; no-op on second call | unit | `uv run pytest tests/test_cert_bootstrap.py -x` | ❌ Wave 0 |
| AUTH-02 | `phaze.cert_bootstrap` stays Postgres-free (no `phaze.database` / `sqlalchemy` imports) | subprocess import test | `uv run pytest tests/test_task_split.py -x` | ✓ extend |
| AUTH-03 | `AgentSettings` rejects passwordless `redis_url` when `agent_env == "production"` | unit | `uv run pytest tests/test_config/test_agent_settings_redis_password.py -x` | ❌ Wave 0 |
| AUTH-03 | `docker-compose.yml` redis service runs `--requirepass` and binds `${REDIS_BIND_IP:-127.0.0.1}:6379:6379` | structural-parse | `uv run pytest tests/test_deployment/test_api_filesystem_isolation.py::test_redis_hardened -x` | ❌ Wave 0 |
| OPS-02 | `docker-compose.agent.yml` declares EXACTLY `{worker, watcher, audfprint, panako}` | structural-parse | `uv run pytest tests/test_deployment/test_agent_compose.py -x` | ❌ Wave 0 |
| OPS-02 | no agent service references Postgres connection env | structural-parse | `uv run pytest tests/test_deployment/test_agent_compose.py -x` | ❌ Wave 0 |
| OPS-02 | agent `worker` service has `PHAZE_ROLE=agent` | structural-parse | `uv run pytest tests/test_deployment/test_agent_compose.py -x` | ❌ Wave 0 |
| OPS-03 | empty `/models` triggers download; populated `/models` is a no-op; network failure raises `RuntimeError` | unit (mock httpx download) | `uv run pytest tests/test_services/test_model_bootstrap.py -x` | ❌ Wave 0 |
| OPS-04 | SAQ cron registered with trailing-seconds spec `* * * * * */30`; emits one heartbeat per tick with `worker_pid > 0`, `queue_depth >= 0`, `agent_version == phaze.__version__` | unit (mock `SAQ Queue.info()` + `PhazeAgentClient`) | `uv run pytest tests/test_tasks/test_heartbeat_cron.py -x` | ❌ Wave 0 |
| OPS-04 | heartbeat-call failure (`AgentApiError` subclass) → `WARNING` log; no exception escapes; next tick fires normally | unit | `uv run pytest tests/test_tasks/test_heartbeat_failure.py -x` | ❌ Wave 0 |
| OPS-04 | `GET /admin/agents` renders for 0 / 1 / many agents | smoke-app integration (TestClient) | `uv run pytest tests/test_routers/test_admin_agents.py -x` | ❌ Wave 0 |
| OPS-04 | status pill classifier returns the right state for alive / stale / dead / revoked / never-seen | unit | `uv run pytest tests/test_services/test_agent_liveness.py -x` | ❌ Wave 0 |
| OPS-04 | HTMX partial (`/admin/agents/_table`) returns when `HX-Request: true` header present | smoke-app integration | `uv run pytest tests/test_routers/test_admin_agents.py::test_htmx_partial -x` | ❌ Wave 0 |
| OPS-04 | agent sort order matches D-14: `(revoked_at IS NOT NULL, status_rank, -last_seen_at)` | unit | covered in `tests/test_services/test_agent_liveness.py` | ❌ Wave 0 |
| OPS-04 (UI) | `phaze.utils.humanize.relative_time(dt)` produces correct outputs across the 6-step ladder (just-now / Ns / Nm / Nh / Nd / never) | unit | `uv run pytest tests/test_utils/test_humanize.py -x` | ❌ Wave 0 |

---

## Wave 0 Requirements

All Wave 0. Every test file below is new and must be authored as the FIRST task
in the owning plan (Red-first; Wave 0 within each plan, before any production
code from that plan lands):

- [ ] `tests/test_cert_bootstrap.py` — AUTH-02 cert generation + idempotency
- [ ] `tests/test_services/test_agent_client_tls.py` — AUTH-02 wrong-CA → `ConnectError`
- [ ] `tests/test_config/test_agent_settings_redis_password.py` — AUTH-03 production validator
- [ ] `tests/test_deployment/__init__.py` — new test package marker
- [ ] `tests/test_deployment/test_api_filesystem_isolation.py` — DIST-01 + AUTH-03 (compose structural)
- [ ] `tests/test_deployment/test_agent_compose.py` — OPS-02 (compose structural)
- [ ] `tests/test_services/test_model_bootstrap.py` — OPS-03 auto-download
- [ ] `tests/test_tasks/test_heartbeat_cron.py` — OPS-04 emission cadence
- [ ] `tests/test_tasks/test_heartbeat_failure.py` — OPS-04 fire-and-forget
- [ ] `tests/test_routers/test_admin_agents.py` — OPS-04 router rendering + HTMX partial
- [ ] `tests/test_services/test_agent_liveness.py` — OPS-04 classifier + sort
- [ ] `tests/test_utils/test_humanize.py` — UI-SPEC `relative_time` helper

**Existing test extension:**
- [ ] `tests/test_task_split.py` — append `test_cert_bootstrap_stays_postgres_free` (extends Phase 26 D-25 import-boundary invariant for the new `phaze.cert_bootstrap` module)

**Framework install:** None required. pytest, pytest-asyncio, respx, httpx, PyYAML already in dev-deps. A new explicit `cryptography>=46.0.0,<49` dependency is added to runtime deps (NOT dev-deps) — see RESEARCH.md Critical Discovery #1.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Two-host CA copy + agent connects over HTTPS end-to-end | AUTH-02 | Requires a real second host with `docker compose -f docker-compose.agent.yml up -d`. CI cannot simulate two hosts cheaply. | `docs/deployment.md` walkthrough: (1) bring up app-server stack; (2) `scp ./certs/phaze-ca.crt operator@file-server:/etc/phaze/certs/`; (3) populate `.env.agent`; (4) `just up-agent`; (5) confirm agent registers and heartbeats land on `/admin/agents` within 90s. |
| Operator confirms `api` container cannot read music files | DIST-01 | Structural YAML test asserts the *intent*; the live-container check confirms the *result*. Plain English smoke documented for the operator. | `docs/deployment.md` 5-line section: (a) `docker compose -f docker-compose.yml up -d`; (b) `docker compose exec api ls /data/music` → expect `No such file or directory`; (c) `docker compose exec api cat /data/music/anything.mp3` → expect failure. |
| `just download-models` on a fresh file server populates `/models` | OPS-03 | Network egress + ~150MB disk I/O on a fresh host. Documented but not run in CI. | Run `just download-models` on a fresh file server; verify `.pb` files materialize under `./models` and SHA-256 checksums match the manifest in `phaze.scripts.download_models`. |
| `restart: unless-stopped` retries after model download failure | OPS-03 | Requires inducing real network failure on a live container; structural test asserts the env-var configuration only. | Unplug file server network → `docker compose -f docker-compose.agent.yml up worker` → observe non-zero exit + restart loop; restore network → observe successful download and stable run. |
| HTMX 5s poll refreshes the agents table without manual reload | OPS-04 | Visual confirmation in a browser. The router-level test asserts the HX-Request header path; doesn't render the page in a browser. | Browse to `http://localhost:8000/admin/agents`; observe the table re-render every ~5s without F5; stop the agent worker on a file server, watch the row transition from green→amber→red within 5 minutes. |

---

## Validation Sign-Off

- [ ] All tasks have an `<automated>` verify command OR a documented Wave 0 dependency
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers every test file marked ❌ in the per-task verification map
- [ ] No watch-mode flags anywhere (`pytest -f`, `--watch` etc. forbidden)
- [ ] Feedback latency < 90s for full suite, < 5s for targeted file run
- [ ] `nyquist_compliant: true` set in frontmatter when all checks pass

**Approval:** pending
