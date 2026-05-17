# Phase 29: Deployment Hardening & Agents Admin - Pattern Map

**Mapped:** 2026-05-16
**Files analyzed:** 24 new + 14 modified = 38 total
**Analogs found:** 32 / 38 (6 with no close analog — see §No Analog Found)

This map gives the planner concrete, line-numbered code excerpts the executor
can copy from. Every "Pattern Assignments" subsection lists the analog file
path + the specific behavior the new file inherits. The shared-patterns block
at the bottom covers cross-cutting concerns (Postgres-free import boundary,
HTMX self-replacing partial, smoke-app test fixture, AliasChoices env
mapping) that apply to multiple files.

---

## File Classification

### New files

| New File | Role | Data Flow | Closest Analog | Match Quality |
|----------|------|-----------|----------------|---------------|
| `src/phaze/cert_bootstrap.py` | bootstrap-module | file-I/O (write-once, idempotent) | `src/phaze/tasks/_shared/agent_bootstrap.py` | role-match (idiom) |
| `src/phaze/entrypoint.py` | entrypoint-shim | process-exec | `src/phaze/agent_watcher/__main__.py` | role-match (entry point shape) |
| `src/phaze/constants.py` (extend) | constants module | n/a | self (already exists) | exact (extend existing) |
| `src/phaze/routers/admin_agents.py` | controller (router) | request-response (read-only + HTMX partial) | `src/phaze/routers/pipeline_scans.py` | exact |
| `src/phaze/services/agent_liveness.py` | service (pure classifier) | transform | (none — new shape) | no analog |
| `src/phaze/services/model_bootstrap.py` (alt: `tasks/_shared/`) | service (bootstrap) | file-I/O + HTTP-download | `src/phaze/tasks/_shared/agent_bootstrap.py` | role-match (idiom) |
| `src/phaze/tasks/_shared/model_bootstrap.py` | shared bootstrap | file-I/O + HTTP-download | `src/phaze/tasks/_shared/agent_bootstrap.py` | exact (sibling) |
| `src/phaze/scripts/__init__.py` + `src/phaze/scripts/download_models.py` | helper module | HTTP-download (streaming) | `scripts/download-models.sh` (bash) | role-match (translation) |
| `src/phaze/tasks/heartbeat.py` | SAQ cron handler | request-response (POST + fire-and-forget) | `src/phaze/tasks/agent_worker.py` (startup hook ctx usage) | role-match |
| `src/phaze/utils/__init__.py` + `src/phaze/utils/humanize.py` | utility (pure function) | transform | (no existing utils dir) | no analog |
| `src/phaze/templates/admin/agents.html` | page shell | server-rendered | `src/phaze/templates/pipeline/dashboard.html` | exact |
| `src/phaze/templates/admin/partials/agents_table.html` | HTMX poll partial | request-response (polled) | `src/phaze/templates/pipeline/partials/recent_scans_table.html` + `pipeline/partials/scan_progress_card.html` | role-match (compose) |
| `src/phaze/templates/admin/partials/_status_pill.html` | nested include | server-rendered fragment | `src/phaze/templates/pipeline/partials/scan_status_pill.html` | exact |
| `docker-compose.agent.yml` | infra-config | deployment | `docker-compose.yml` (existing) | role-match (subset) |
| `.env.example.agent` | infra-config | deployment | `.env.example` (existing) | role-match (subset) |
| `docs/deployment.md` | docs | n/a | (no existing docs/*.md) | no analog |
| `tests/test_cert_bootstrap.py` | test | unit | `tests/test_agent_watcher/test_main.py` (similar bootstrap test) | role-match |
| `tests/test_services/test_agent_client_tls.py` | test | integration (real TLS) | `tests/test_services/test_agent_client.py` | role-match (extend) |
| `tests/test_services/test_model_bootstrap.py` | test | unit (httpx mock) | `tests/test_services/test_discogs_matcher.py` (httpx + tmp_path) | role-match |
| `tests/test_services/test_agent_liveness.py` | test | unit (pure) | `tests/test_services/test_dedup.py` (pure classifier tests) | role-match |
| `tests/test_tasks/test_heartbeat_cron.py` | test | unit (SAQ ctx + httpx) | `tests/test_tasks/test_execute_approved_batch_progress.py` | role-match |
| `tests/test_tasks/test_heartbeat_failure.py` | test | unit (error path) | same | role-match |
| `tests/test_routers/test_admin_agents.py` | test | smoke-app integration | `tests/test_routers/test_pipeline_scans.py` | exact |
| `tests/test_utils/test_humanize.py` | test | unit (pure) | `tests/test_services/test_dedup.py` | role-match |
| `tests/test_deployment/__init__.py` + `test_api_filesystem_isolation.py` + `test_agent_compose.py` | test | structural (YAML parse) | (no existing structural tests) | no analog |
| `tests/test_config/test_agent_settings_redis_password.py` | test | unit (pydantic validator) | `tests/test_config_role_split.py` | exact |

### Modified files

| Modified File | Role | Change Type | Closest Analog (for new region) | Match Quality |
|---------------|------|-------------|--------------------------------|---------------|
| `src/phaze/main.py` | app factory | add router include + cert bootstrap call | self (existing include_router pattern) | exact |
| `src/phaze/config.py` | settings | add 3 fields + 1 model_validator | self (AliasChoices pattern at lines 102-122, 153-172) | exact |
| `src/phaze/services/agent_client.py` | HTTP client | add `verify=` kwarg to `__init__` + `httpx.AsyncClient(...)` | self (existing __init__ at lines 118-131) | exact |
| `src/phaze/tasks/_shared/agent_bootstrap.py` | bootstrap | pass `verify=` to `PhazeAgentClient` ctor | self (lines 44-57) | exact |
| `src/phaze/tasks/agent_worker.py` | SAQ entry | add `CronJob` + import + replace models-check with `ensure_models_present` | `src/phaze/tasks/controller.py` (existing `cron_jobs` example at lines 116-118) | exact |
| `src/phaze/agent_watcher/__main__.py` | watcher entry | add `ensure_models_present` call after whoami | self (startup sequence at lines 18-31) | exact |
| `src/phaze/templates/base.html` | nav shell | add Agents nav link | self (lines 134-169 — 9 nav-link blocks to mirror byte-for-byte) | exact |
| `docker-compose.yml` | infra-config | strip mounts + delete services + add TLS/Redis hardening | self (existing redis at lines 118-126, api at 3-17) | exact |
| `.env.example` | infra-config | add 3 env vars | self (existing pattern) | exact |
| `justfile` | command runner | add 2 recipes (`up-agent`, `up-all`) | self (existing `up` recipe at lines 9-12) | exact |
| `pyproject.toml` | package manifest | add `cryptography` runtime dep | self (existing deps list at lines 11-31) | exact |
| `tests/test_task_split.py` | test | extend with `test_cert_bootstrap_stays_postgres_free` | self (lines 33-73 — existing import-boundary subprocess pattern) | exact |
| `PROJECT.md` | docs | append Deployment subsection | self (append-only) | n/a (free-form prose) |
| `scripts/update-project.sh` | helper script | add `admin_agents` router + `cert_bootstrap` module to service list | self | exact |

---

## Pattern Assignments

### `src/phaze/cert_bootstrap.py` (bootstrap-module, file-I/O)

**Analog:** `src/phaze/tasks/_shared/agent_bootstrap.py` — same "idempotent bootstrap module called once at startup" idiom. The cert_bootstrap module is NOT in `tasks/_shared/` because (a) it's used by the api process not by tasks, and (b) the import-boundary invariant must extend to it via a Postgres-free test (Phase 26 D-25 + Phase 29 D-22).

**Module docstring + import-boundary banner** (mirror `tasks/_shared/agent_bootstrap.py:1-21`):
```python
"""Pre-uvicorn cert bootstrap (Phase 29 D-02).

IMPORT-BOUNDARY INVARIANT (extends Phase 26 D-25 + Phase 27 D-22):
    This module MUST NOT import phaze.database, phaze.tasks.session, or
    sqlalchemy.ext.asyncio. Verified in CI by
    tests/test_task_split.py::test_cert_bootstrap_stays_postgres_free.

Public exports:
    - ensure_certs_present(certs_dir, cn, sans_csv): idempotent CA + leaf bootstrap
"""
```

**Logger pattern** (mirror lines 36-37):
```python
import logging
logger = logging.getLogger(__name__)
```

**Function signature shape** (mirror the `construct_agent_client(cfg) -> PhazeAgentClient` at line 44):
```python
def ensure_certs_present(certs_dir: Path, cn: str, sans_csv: str) -> None:
```

**Loud banner constant + `print()` + `logger.warning()` dual emit** (Phase 29 D-02 specifics line 349; mirror the `_auth_hint` pattern at lines 81-94 of agent_bootstrap.py for "constants near top, logger inside function"). Cert generation body uses `cryptography.x509.CertificateBuilder` per RESEARCH.md Pattern 1 (lines 252-393).

**Idempotency check** (mirror the "exists + parseable" pattern from RESEARCH.md lines 357-364): `Path.exists()` plus `x509.load_pem_x509_certificate(path.read_bytes())` inside try/except ValueError.

**File modes** (literal from RESEARCH.md lines 372-387): `0o644` for CA cert + leaf cert (public); `0o600` for both private keys. Use `path.chmod(0o600)` after `write_bytes()`.

**No phaze.database import.** This module must stay importable from `entrypoint.py` before any Postgres connection exists. Verified by extending `tests/test_task_split.py`.

---

### `src/phaze/entrypoint.py` (entrypoint-shim, process-exec)

**Analog:** `src/phaze/agent_watcher/__main__.py` — same "single-purpose Python entry point invoked via `python -m`" idiom.

**Module-level `__main__` guard pattern** (mirror `agent_watcher/__main__.py` overall shape):
```python
"""Pre-uvicorn entrypoint shim (Phase 29 D-02).

Runs `cert_bootstrap.ensure_certs_present(...)` before exec'ing uvicorn.
Invoked from docker-compose.yml `command:` as:
    uv run python -m phaze.entrypoint
"""
from __future__ import annotations
import os
import sys
from pathlib import Path
from phaze.cert_bootstrap import ensure_certs_present

def main() -> None:
    certs_dir = Path(os.environ.get("PHAZE_CERTS_DIR", "/certs"))
    cn = os.environ.get("PHAZE_API_HOST", "localhost")
    sans = os.environ.get("PHAZE_API_TLS_SANS", "localhost,127.0.0.1,api")
    ensure_certs_present(certs_dir, cn=cn, sans_csv=sans)
    os.execvp("uv", ["uv", "run", "uvicorn", "phaze.main:app",
                     "--host", "0.0.0.0", "--port", "8000",
                     "--ssl-keyfile", str(certs_dir / "phaze-server.key"),
                     "--ssl-certfile", str(certs_dir / "phaze-server.crt")])

if __name__ == "__main__":
    main()
```

**Why `os.execvp`:** replaces the python process with uvicorn so signals + PID-1 propagate cleanly (no double-fork in Docker). This is a cleaner pattern than `subprocess.run(...)` for an entrypoint shim.

**Import-boundary:** entrypoint.py imports only `phaze.cert_bootstrap` (Postgres-free per above). Test extension lives in `tests/test_task_split.py`.

---

### `src/phaze/routers/admin_agents.py` (controller, request-response + HTMX partial)

**Analog:** `src/phaze/routers/pipeline_scans.py` (Phase 27) — EXACT match: same dual-endpoint pattern (page route + HTMX partial route), same `Jinja2Templates` setup, same `Depends(get_session)` injection, same HX-Request handling.

**Imports pattern** (mirror lines 26-42):
```python
from datetime import UTC, datetime
import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.services.agent_liveness import classify, sort_key
```

**Templates setup pattern** (literal copy of lines 47-48):
```python
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
```

**Router instantiation pattern** (mirror line 50; new prefix + new tag):
```python
router = APIRouter(prefix="/admin/agents", tags=["admin"])
```

**Page route pattern** (mirror the `agent_roots_swap` shape at lines 82-104):
```python
@router.get("", response_class=HTMLResponse)
async def page(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    agents = await _load_agents(session)
    template = "admin/partials/agents_table.html" if _is_htmx(request) else "admin/agents.html"
    return templates.TemplateResponse(request=request, name=template, context={
        "request": request,
        "agents": agents,
        "current_page": "admin_agents",
        "refreshed_at_iso": datetime.now(UTC).isoformat(),
    })
```

**HX-Request header detection** (Phase 27 STATE.md pattern, used in `services/search.py`):
```python
def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"
```

**Transient ORM attribute pattern** (mirror Phase 27's `_agent_name` / `_elapsed_seconds` injection in `routers/pipeline.py` dashboard). Same pattern for `agent._status`:
```python
async def _load_agents(session: AsyncSession) -> list[Agent]:
    result = await session.execute(select(Agent))
    rows = list(result.scalars().all())
    now = datetime.now(UTC)
    for a in rows:
        a._status = classify(a, now)  # noqa: SLF001  -- mirror pipeline_scans naming
    rows.sort(key=lambda a: sort_key(a, now))
    return rows
```

**No auth dependency.** Pipeline router (Phase 27) is the precedent: operator pages are open on private LAN. Do NOT add `get_authenticated_agent`. See RESEARCH.md "Anti-Patterns to Avoid" #4.

**Dedicated `/_table` partial endpoint** (matches Phase 27's `/pipeline/scans/agent-roots`; mirror lines 82-104). The partial route returns the partial unconditionally — never the full page.

---

### `src/phaze/services/agent_liveness.py` (service, pure transform)

**No close analog.** Pure-function classifier; no service in `src/phaze/services/` currently has this shape. RESEARCH.md Pattern 6 (lines 696-726) provides the full target code. Style mirrors `phaze.services.dedup` (pure functions, no DB).

**Public surface:**
```python
"""Agent liveness classification (UI-SPEC §Status Pill Component).

Reads phaze.constants.AGENT_LIVENESS_* thresholds so tests + UI render share one
source of truth (Phase 29 D-12).
"""
from datetime import datetime
from typing import Literal

from phaze.constants import AGENT_LIVENESS_ALIVE_SECONDS, AGENT_LIVENESS_STALE_SECONDS
from phaze.models.agent import Agent

AgentStatus = Literal["alive", "stale", "dead", "revoked", "never"]
_STATUS_RANK = {"alive": 0, "stale": 1, "dead": 2, "revoked": 3, "never": 3}


def classify(agent: Agent, now: datetime) -> AgentStatus: ...
def sort_key(agent: Agent, now: datetime) -> tuple[int, int, float]: ...
```

**Pure-function convention:** every helper accepts `now: datetime` explicitly (no `datetime.now()` inside) so tests are time-deterministic without `freezegun`. Mirrors the `elapsed_seconds(batch)` shape at `pipeline_scans.py:53-79`.

---

### `src/phaze/utils/humanize.py` (utility, pure transform)

**No close analog.** `src/phaze/utils/` does not yet exist; this phase establishes it. Style mirrors `phaze.services.dedup` pure-function style.

**Public surface** (UI-SPEC LOCKED — RESEARCH.md lines 729-748):
```python
"""Relative-time formatter: '23s ago', '4m ago', '2h ago', '3d ago'.

UI-SPEC §Relative-Time Helper locks this signature. Pure Python, no deps.
"""
from datetime import UTC, datetime


def relative_time(dt: datetime | None, *, now: datetime | None = None) -> str: ...
```

**Behavioral rules (LOCKED, from UI-SPEC §Relative-Time Helper):**
| Delta | Output |
|-------|--------|
| `None` | `"never"` |
| `delta < 0` | `"just now"` |
| `0 ≤ d < 60` | `f"{int(d)}s ago"` |
| `60 ≤ d < 3600` | `f"{int(d/60)}m ago"` |
| `3600 ≤ d < 86400` | `f"{int(d/3600)}h ago"` |
| `d ≥ 86400` | `f"{int(d/86400)}d ago"` |

Note `int()` truncates toward zero — UI-SPEC explicit: `89.7s → "89s ago"`, NOT `"1m ago"`.

---

### `src/phaze/tasks/_shared/model_bootstrap.py` (shared bootstrap, file-I/O + HTTP)

**Analog:** `src/phaze/tasks/_shared/agent_bootstrap.py` — EXACT match (sibling module in the same package); both are postgres-free shared bootstraps consumed by `agent_worker.py` + `agent_watcher/__main__.py`.

**Module docstring pattern** (mirror lines 1-21, replacing payload-specific text):
```python
"""Auto-download essentia weights when /models is empty (Phase 29 D-21).

IMPORT-BOUNDARY (extends Phase 26 D-25 + Phase 27 D-22):
    Postgres-free. Imports: httpx + pathlib + hashlib only.
    Verified by tests/test_task_split.py::test_shared_bootstrap_stays_postgres_free
    (existing test already covers everything in tasks/_shared/).

Public exports:
    - ensure_models_present(models_dir): idempotent .pb-file check + download-on-empty
"""
```

**Function signature** (mirror `construct_agent_client(cfg) -> PhazeAgentClient` shape):
```python
def ensure_models_present(models_dir: Path) -> None: ...
```

**Body shape** (RESEARCH.md Pattern 8 lines 838-853): glob for `*.pb`, log status, call `download_to(models_dir)` from `phaze.scripts.download_models`, wrap exceptions in `RuntimeError` per D-21.

---

### `src/phaze/scripts/download_models.py` (helper module, HTTP-download)

**Analog:** `scripts/download-models.sh` (bash). Phase 29 translates the URL list + loop into Python so both bash and `model_bootstrap.py` drive the same logic.

**URL list extraction** (mirror lines 16-50 of `download-models.sh` — 33 classifier model paths + 1 genre model). Keep the same `CLASSIFIER_BASE` / `GENRE_BASE` constants.

**Download function shape** (RESEARCH.md lines 880-901):
```python
import httpx
from pathlib import Path

def _download_one(url: str, dest: Path) -> None:
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=60) as response:
        response.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                fh.write(chunk)
    tmp.rename(dest)  # atomic on POSIX

def download_to(target_dir: Path) -> None: ...
```

**Atomic write pattern** (`.part` suffix + rename) prevents half-downloaded `.pb` files from satisfying the idempotency check.

**Bash shim** (RESEARCH.md lines 914-918): rewrite `scripts/download-models.sh` to one line: `exec uv run python -m phaze.scripts.download_models "${1:-./models}"`.

---

### `src/phaze/tasks/heartbeat.py` (SAQ cron handler, request-response)

**Analog:** SAQ task handlers throughout `src/phaze/tasks/` (e.g., `tasks/fingerprint.py`, `tasks/proposal.py`). Signature is `async def name(ctx: dict[str, Any]) -> ReturnType`. Reads from `ctx` dict; raises on real errors; logs on fire-and-forget failures.

**Module docstring + imports** (mirror agent_worker.py:1-66 module header style + RESEARCH.md Pattern 5 lines 528-580):
```python
"""30-second cron handler that POSTs an agent heartbeat (Phase 29 D-07..D-10).

Reads from SAQ ctx (populated by phaze.tasks.agent_worker.startup):
    - ctx["api_client"]: PhazeAgentClient
    - ctx["agent_identity"]: AgentIdentity
    - ctx["worker"]: SAQ Worker (gives .queue for Queue.info())

Failure policy (D-09): catch AgentApiError, log WARNING, return. SAQ retries
on next tick. Mirrors Phase 28 D-16 fire-and-forget posture.
"""
from __future__ import annotations

import importlib.metadata
import logging
import os
from typing import Any

from phaze.schemas.agent_heartbeat import HeartbeatRequest
from phaze.services.agent_client import AgentApiError

logger = logging.getLogger(__name__)
```

**Handler body** (RESEARCH.md lines 553-580):
```python
async def heartbeat_tick(ctx: dict[str, Any]) -> None:
    client = ctx.get("api_client")
    identity = ctx.get("agent_identity")
    if client is None or identity is None:
        logger.warning("heartbeat_tick: ctx not initialized; skipping")
        return

    queue = ctx["worker"].queue  # SAQ Worker stashes Queue on .queue (Pitfall 8)
    try:
        info = await queue.info()
        queue_depth = int(info.get("queued", 0))
    except Exception:
        logger.warning("heartbeat_tick: queue.info() failed; defaulting to 0", exc_info=True)
        queue_depth = 0

    payload = HeartbeatRequest(
        agent_version=importlib.metadata.version("phaze"),
        worker_pid=os.getpid(),
        queue_depth=queue_depth,
    )
    try:
        await client.heartbeat(payload)
        logger.debug("heartbeat sent agent=%s queue_depth=%d", identity.agent_id, queue_depth)
    except AgentApiError as exc:
        logger.warning("heartbeat failed: %s", exc)
```

**Why `ctx["worker"].queue` not `ctx["queue"]`:** RESEARCH.md Pitfall 8 — SAQ pre-populates `ctx["worker"] = self` in `Worker.__init__`. The Queue is on `worker.queue`. Using `ctx["queue"]` would `KeyError` because `agent_worker.startup` does NOT add a `queue` key (unlike controller.startup which does at line 80).

---

### `src/phaze/templates/admin/agents.html` (page shell)

**Analog:** `src/phaze/templates/pipeline/dashboard.html` — EXACT match (page shell, `space-y-6` rhythm, skip link, current_page anchor).

**Full structure** (literal mirror of `dashboard.html` adapted for D-11):
```jinja
{% extends "base.html" %}
{% block title %}Agents - Phaze{% endblock %}
{% block skip_link %}
<a href="#agents-table-section" class="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:bg-blue-600 dark:bg-blue-700 focus:text-white focus:px-4 focus:py-2 focus:rounded focus:z-50">Skip to agents table</a>
{% endblock %}
{% block content %}
<div class="space-y-6">
    <h1 class="text-2xl font-semibold leading-tight">Agents</h1>
    <p class="text-sm text-gray-500 dark:text-gray-400">
        Live status of every registered file-server agent. Refreshes every 5 seconds.
    </p>
    {% include "admin/partials/agents_table.html" %}
</div>
{% endblock %}
```

**Sets `current_page = "admin_agents"`** via the router context dict so `base.html`'s nav-link active-state matches (UI-SPEC §Navigation Integration).

---

### `src/phaze/templates/admin/partials/agents_table.html` (HTMX self-replacing partial)

**Analog:** `src/phaze/templates/pipeline/partials/recent_scans_table.html` (table + empty state structure) PLUS `pipeline/partials/scan_progress_card.html` (HTMX self-replacing outer `<section>`). The agents partial composes both patterns.

**Outer self-replacing `<section>` shape** (UI-SPEC §Interaction Contract self-contained partial pattern + mirror of `scan_progress_card.html` lines 10-14):
```jinja
<section id="agents-table-section"
         hx-get="/admin/agents/_table"
         hx-trigger="every 5s"
         hx-swap="outerHTML"
         data-refreshed-at="{{ refreshed_at_iso }}"
         aria-labelledby="agents-table-heading"
         class="border border-gray-200 dark:border-phaze-border rounded-lg p-4">
    <h2 id="agents-table-heading" class="sr-only">Registered agents</h2>
    ...table or empty-state...
</section>
```

**KEY DIFFERENCE from `scan_progress_card.html`:** `scan_progress_card.html` OMITS `hx-trigger` on terminal states to halt polling (Pitfall 6 in that template). The agents partial NEVER halts — UI-SPEC LOCKS this. Always emit `hx-trigger="every 5s"` so polling continues.

**Empty state markup** (literal mirror of `recent_scans_table.html` lines 14-20):
```jinja
{% if not agents %}
<div class="text-center py-8">
    <p class="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-1">No agents registered yet</p>
    <p class="text-sm text-gray-500 dark:text-gray-400">
        Provision an agent token via psql, then run <code class="font-mono bg-gray-100 dark:bg-phaze-panel px-1 rounded">just up-agent</code> on the file server. Once the worker boots, it will appear here within a few seconds.
    </p>
</div>
{% else %}
```

**Table head pattern** (literal mirror of `recent_scans_table.html` lines 22-33):
```jinja
<div class="overflow-x-auto">
    <table class="w-full text-sm text-left">
        <caption class="sr-only">Registered agents</caption>
        <thead class="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase border-b border-gray-200 dark:border-phaze-border">
            <tr>
                <th scope="col" class="px-4 py-3">Agent</th>
                <th scope="col" class="px-4 py-3">Status</th>
                <th scope="col" class="px-4 py-3">Queue</th>
                <th scope="col" class="px-4 py-3">Last seen</th>
                <th scope="col" class="px-4 py-3">Scan roots</th>
                <th scope="col" class="px-4 py-3">Actions</th>
            </tr>
        </thead>
        <tbody class="divide-y divide-gray-100 dark:divide-phaze-border">
```

**Agent name + ID two-line cell** (literal mirror of `execution/partials/agents_table.html` lines 38-40):
```jinja
<td class="px-4 py-3">
    <span class="text-sm font-semibold text-gray-900 dark:text-gray-100 block" title="{{ agent.id }}">{{ agent.name }}</span>
    <span class="font-mono text-xs text-gray-500 dark:text-gray-400 block">{{ agent.id }}</span>
</td>
```

**Status pill cell** uses the nested include (see `_status_pill.html` section below):
```jinja
<td class="px-4 py-3">{% include "admin/partials/_status_pill.html" %}</td>
```

**Last-refreshed footer with Alpine.js** (UI-SPEC §Self-Contained Partial Pattern literal markup; uses `data-refreshed-at` attribute on outer section). This is a new pattern (no existing template uses Alpine.js + `data-*` like this for live countdown) — derive directly from UI-SPEC.

---

### `src/phaze/templates/admin/partials/_status_pill.html` (nested include)

**Analog:** `src/phaze/templates/pipeline/partials/scan_status_pill.html` — EXACT match (3→5 state expansion with same pill geometry).

**Pill geometry constant** (literal from scan_status_pill.html lines 6-10 — all pills in the project use this):
```
text-xs font-semibold px-2 py-0.5 rounded-full
```

**5-state branching** (UI-SPEC §Status Pill Component LOCKED — expand the `scan_status_pill.html` if/elif/elif into 5 branches matching the agent.\_status transient attribute):
```jinja
{# Phase 29: 5-state agent liveness pill. Mirrors scan_status_pill.html geometry.
   Expects `agent` in context (loop variable) with transient `_status` attribute
   set by routers/admin_agents._load_agents per UI-SPEC §Status Pill. #}
{% if agent._status == 'alive' %}
<span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-green-100 dark:bg-green-950 text-green-700 dark:text-green-400" aria-label="Status: alive">ALIVE</span>
{% elif agent._status == 'stale' %}
<span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-amber-100 dark:bg-amber-950 text-amber-700 dark:text-amber-400" aria-label="Status: stale">STALE</span>
{% elif agent._status == 'dead' %}
<span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-red-100 dark:bg-red-950 text-red-700 dark:text-red-400" aria-label="Status: dead">DEAD</span>
{% elif agent._status == 'revoked' %}
<span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300" aria-label="Status: revoked">REVOKED</span>
{% elif agent._status == 'never' %}
<span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300" aria-label="Status: never seen">NEVER</span>
{% endif %}
```

**Underscore-prefix naming** is the project convention for nested-include partials (not for HTMX-swap targets). UI-SPEC §Template Structure LOCKS this.

---

### `src/phaze/templates/base.html` (nav edit)

**Analog:** SELF (existing nav-link block at lines 134-169 — 9 nav-link `<a>` blocks already there). Phase 29 adds a 10th, byte-for-byte mirror of any existing block.

**Exact markup to insert between `Audit Log` link (lines 166-169) and the `<div class="ml-auto" x-data>` theme-toggle wrapper (line 173)** (UI-SPEC §Navigation Integration LOCKED):
```jinja
<a href="/admin/agents"
   {% if current_page == 'admin_agents' %}aria-current="page"{% endif %}
   class="text-sm font-semibold px-3 py-2 rounded-md transition-colors {% if current_page == 'admin_agents' %}text-blue-600 dark:text-blue-400 bg-blue-50 dark:bg-blue-950{% else %}text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-gray-50 dark:hover:bg-phaze-panel{% endif %}">
    Agents
</a>
```

**`aria-current="page"` is new** but UI-SPEC says planner may optionally retrofit the other 9 nav links to add it (one-line change × 9 links). UI-checker will flag inconsistency either way; retrofitting is the cleaner outcome.

---

### `src/phaze/services/agent_client.py` (MODIFY — add `verify=` kwarg)

**Analog:** SELF — existing `__init__` at lines 118-131. Phase 29 adds one kwarg + threads it through.

**Existing `__init__` shape** (lines 118-131):
```python
def __init__(
    self,
    base_url: str,
    token: str,
    *,
    timeout: float = 30.0,
    _client: httpx.AsyncClient | None = None,
) -> None:
    self.base_url = base_url
    self._client = _client or httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
```

**Phase 29 modification** (RESEARCH.md Pattern 3 lines 463-481):
```python
def __init__(
    self,
    base_url: str,
    token: str,
    *,
    timeout: float = 30.0,
    verify: ssl.SSLContext | str | bool = True,    # NEW
    _client: httpx.AsyncClient | None = None,
) -> None:
    self.base_url = base_url
    self._client = _client or httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
        verify=verify,         # NEW
    )
```

**Default = `True`** preserves existing respx-based tests (Pitfall 10 RESEARCH.md). Need to add `import ssl` at the top.

---

### `src/phaze/tasks/_shared/agent_bootstrap.py` (MODIFY — pass `verify=`)

**Analog:** SELF — existing `construct_agent_client` at lines 44-57.

**Existing body:**
```python
def construct_agent_client(cfg: AgentSettings) -> PhazeAgentClient:
    return PhazeAgentClient(
        base_url=cfg.agent_api_url,
        token=cfg.agent_token.get_secret_value(),
        timeout=30.0,
    )
```

**Phase 29 modification** (RESEARCH.md Pattern 3 lines 449-461; CA-file pre-check is fail-fast per D-03):
```python
def construct_agent_client(cfg: AgentSettings) -> PhazeAgentClient:
    ca_path = Path(cfg.agent_ca_file)
    if not ca_path.exists() or ca_path.stat().st_size == 0:
        msg = f"CA file empty or unreadable: {cfg.agent_ca_file}"
        raise RuntimeError(msg)
    return PhazeAgentClient(
        base_url=cfg.agent_api_url,
        token=cfg.agent_token.get_secret_value(),
        timeout=30.0,
        verify=cfg.agent_ca_file,
    )
```

Need to add `from pathlib import Path` at the top.

---

### `src/phaze/tasks/agent_worker.py` (MODIFY — add cron + replace models check)

**Analog:** SELF + `src/phaze/tasks/controller.py` (which already registers a `cron_jobs` entry at lines 116-118).

**Existing cron pattern from controller.py:116-118** (literal copy shape):
```python
from saq import CronJob, Queue

settings = {
    ...
    "cron_jobs": [
        CronJob(refresh_tracklists, cron="0 3 1 * *"),  # type: ignore[type-var]
    ],
    ...
}
```

**Phase 29 modification to `agent_worker.py:179` settings dict** (RESEARCH.md Pattern 5 lines 583-597 + Critical Discovery #2 trailing-seconds cron):
```python
from saq import CronJob, Queue   # already imports Queue; ADD CronJob

from phaze.tasks.heartbeat import heartbeat_tick   # NEW import

settings = {
    "queue": queue,
    "functions": [
        process_file,
        extract_file_metadata,
        fingerprint_file,
        scan_live_set,
        scan_directory,
        execute_approved_batch,
        heartbeat_tick,          # ADD — register as function so SAQ can dispatch it
    ],
    "cron_jobs": [
        CronJob(heartbeat_tick, cron="* * * * * */30", unique=True, timeout=10),   # type: ignore[type-var]
    ],
    "concurrency": get_settings().worker_max_jobs,
    "startup": startup,
    "shutdown": shutdown,
}
```

**Replace existing models check at lines 88-97** with `ensure_models_present` call (RESEARCH.md Pattern 8 lines 904-910):
```python
# OLD (delete):
#   models_dir = Path(cfg.models_path)
#   if not models_dir.is_dir():
#       msg = f"Models directory not found: ..."
#       raise RuntimeError(msg)
#   pb_files = list(models_dir.glob("*.pb"))
#   if not pb_files:
#       msg = f"No .pb model files in ..."
#       raise RuntimeError(msg)
#   logger.info("Found %d model files in %s", len(pb_files), cfg.models_path)
#
# NEW (replace):
from phaze.tasks._shared.model_bootstrap import ensure_models_present
ensure_models_present(Path(cfg.models_path))
```

**Order in startup:** `whoami_with_retry()` (Step 3) → `ensure_models_present()` (NEW; replaces Step 1's old logic) → fingerprint orchestrator (Step 5) → process pool (Step 6). RESEARCH.md `<specifics>` line 906: "auth fails fast before downloading 150MB".

**Critical:** keep `agent_worker.py` as a single .py file. Do NOT convert to a package (Pitfall 9 RESEARCH.md).

---

### `src/phaze/agent_watcher/__main__.py` (MODIFY — add models check after whoami)

**Analog:** SELF — existing startup sequence at lines 18-31 (the docstring outlines it).

**Modification:** after `whoami_with_retry(client)` succeeds (around line 50), call `ensure_models_present(Path(cfg.models_path))`. Same pattern as agent_worker — fail-fast on bad auth before downloading.

```python
from phaze.tasks._shared.model_bootstrap import ensure_models_present   # NEW import

# inside main() after whoami:
identity = await whoami_with_retry(client)
ensure_models_present(Path(cfg.models_path))   # NEW
```

---

### `src/phaze/config.py` (MODIFY — add 3 fields + validator)

**Analog:** SELF — existing `AliasChoices` pattern in `BaseSettings` (lines 102-122) and `AgentSettings` (lines 153-172).

**Existing AliasChoices pattern from lines 102-106:**
```python
auto_migrate: bool = Field(
    default=True,
    validation_alias=AliasChoices("PHAZE_AUTO_MIGRATE", "auto_migrate"),
    description="Run `alembic upgrade head` in the api lifespan startup.",
)
```

**Phase 29 NEW fields** (all use the same Field + AliasChoices template):

1. On `BaseSettings` (D-02 — applies to api control role):
```python
api_tls_sans: str = Field(
    default="localhost,127.0.0.1,api",
    validation_alias=AliasChoices("PHAZE_API_TLS_SANS", "api_tls_sans"),
    description="Comma-separated SAN list for the auto-generated leaf cert (Phase 29 D-02).",
)
```

2. On `AgentSettings` (D-03 — agent role):
```python
agent_ca_file: str = Field(
    default="/certs/phaze-ca.crt",
    validation_alias=AliasChoices("PHAZE_AGENT_CA_FILE", "agent_ca_file"),
    description="Path to the operator-distributed CA cert for verifying the app-server TLS endpoint (Phase 29 D-03).",
)

agent_env: Literal["dev", "production"] = Field(
    default="dev",
    validation_alias=AliasChoices("PHAZE_AGENT_ENV", "agent_env"),
    description="Deployment mode. Production refuses passwordless Redis URLs (Phase 29 D-06).",
)
```

**model_validator pattern** (mirror existing `_enforce_localhost_only` `@field_validator` at lines 64-90 — same shape for the new password-required check; use `model_validator(mode="after")` since we need access to both `redis_url` and `agent_env`):
```python
@model_validator(mode="after")
def _enforce_redis_password_in_production(self) -> "AgentSettings":
    """D-06: production refuses passwordless redis_url."""
    if self.agent_env == "production":
        parsed = urlparse(self.redis_url)
        if not parsed.password:
            msg = "agent_env=production requires a password in redis_url (Phase 29 D-06)"
            raise ValueError(msg)
    return self
```

`model_validator` is already imported at line 16. `Literal` needs to be added: `from typing import Annotated, Literal`.

---

### `docker-compose.yml` (REWRITE)

**Analog:** SELF — the existing root compose plus the agent.yml shape from RESEARCH.md Pattern 7 (lines 766-809) for what to remove.

**Concrete diff target:**

1. **api service** (lines 3-17): strip `volumes:` block; replace `command:` with `uv run python -m phaze.entrypoint`; add `volumes: ["${CA_PATH:-./certs}:/certs:rw"]`.

2. **worker service (control)** (lines 28-45): strip `volumes:` entries for SCAN_PATH/MODELS_PATH/OUTPUT_PATH; remove `MODELS_PATH=/models` from `environment:`.

3. **DELETE `watcher` service block** (lines 50-64).

4. **DELETE `agent-worker` service block** (lines 72-96).

5. **DELETE `audfprint` + `panako` services** (lines 128-154) — move to agent.yml only.

6. **redis service** (lines 118-126): rewrite per RESEARCH.md Pattern 4 lines 502-516:
```yaml
redis:
  image: redis:8-alpine
  command:
    - "redis-server"
    - "--requirepass"
    - "${REDIS_PASSWORD:?REDIS_PASSWORD required}"
  ports:
    - "${REDIS_BIND_IP:-127.0.0.1}:6379:6379"
  healthcheck:
    test: ["CMD", "redis-cli", "--no-auth-warning", "-a", "${REDIS_PASSWORD}", "ping"]
    interval: 5s
    timeout: 5s
    retries: 5
```

**End state:** root compose has services `{api, worker, postgres, redis}` only. Verified by `tests/test_deployment/test_api_filesystem_isolation.py::test_no_watcher_or_agent_worker_in_root_compose`.

---

### `docker-compose.agent.yml` (NEW)

**Analog:** existing `docker-compose.yml` services `watcher`, `agent-worker`, `audfprint`, `panako` (about to be deleted from root) — Phase 29 reconstructs them in the new file with GHCR image references instead of `build:`.

**Full file shape:** RESEARCH.md Pattern 7 lines 766-809 (literal YAML). Key differences from root compose:
- No postgres, no redis (agents don't reach them locally)
- `worker` + `watcher` services use `image: ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}` (NOT `build:`)
- `audfprint` + `panako` keep `build:` (sidecars not yet published to GHCR per D-15)
- Volumes scoped to file-server paths only (`SCAN_PATH`, `MODELS_PATH`, `CA_PATH`)
- `${...:?required}` fail-fast syntax on SCAN_PATH

---

### `.env.example` (MODIFY) and `.env.example.agent` (NEW)

**Analog:** existing `.env.example` for both (the agent version is a file-server subset).

**Phase 29 ADDITIONS to root `.env.example`** (D-23; insert after line 27 API_PORT block):
```bash
# =====================================================================
# Phase 29: Redis hardening (D-05)
# =====================================================================
# Required password for redis-server --requirepass. Fresh dev clones can
# use the placeholder; production MUST set a strong unique value.
REDIS_PASSWORD=changeme
# Interface to bind redis :6379 on. Dev = loopback. Production = LAN IP
# (e.g., 192.168.1.10) so agents on other hosts can reach it.
REDIS_BIND_IP=127.0.0.1

# =====================================================================
# Phase 29: HTTPS via internal CA (D-02)
# =====================================================================
# Comma-separated SAN list for the auto-generated leaf cert. Defaults
# include `api` (docker compose service-name DNS) for single-host dev.
# Production should add the app-server's LAN hostname / IP.
PHAZE_API_TLS_SANS=localhost,127.0.0.1,api
```

**`.env.example.agent`** is a NEW file with only the file-server-relevant variables. Use D-23 spec:
```bash
# Phaze file-server agent .env template (Phase 29 D-23)
# Copy to .env on the file-server host. Required variables fail-fast on `docker compose up`.

# Image tag (pin to a version for production)
PHAZE_IMAGE_TAG=latest

# Application server URL — must be HTTPS (Phase 29 D-01)
PHAZE_AGENT_API_URL=https://<app-server-ip>:8000
PHAZE_REDIS_URL=redis://default:<REDIS_PASSWORD>@<app-server-ip>:6379/0

# Agent identity (provisioned via psql on app-server)
PHAZE_AGENT_ID=fileserver-east
PHAZE_AGENT_TOKEN=phaze_agent_<32urlsafe>
PHAZE_AGENT_QUEUE=phaze-agent-fileserver-east

# Operator-copied CA cert (scp from app-server ./certs/phaze-ca.crt)
PHAZE_AGENT_CA_FILE=/certs/phaze-ca.crt

# Production refuses passwordless Redis URLs (Phase 29 D-06)
PHAZE_AGENT_ENV=production

# File-server local paths
SCAN_PATH=/data/music
MODELS_PATH=./models
CA_PATH=./certs

# Scan roots (comma-separated absolute paths)
PHAZE_AGENT_SCAN_ROOTS=/data/music,/data/concerts
```

---

### `justfile` (MODIFY — add 2 recipes)

**Analog:** SELF — existing `up` recipe at lines 9-12.

**Existing `up` recipe stays as-is** (`docker compose up -d`).

**Add `up-agent` + `up-all`** (RESEARCH.md lines 1108-1123):
```just
[doc('Start file-server agent stack (standalone docker-compose.agent.yml)')]
[group('dev')]
up-agent:
    docker compose -f docker-compose.agent.yml up -d

[doc('Start both stacks on one host (developer convenience)')]
[group('dev')]
up-all:
    docker compose -f docker-compose.yml -f docker-compose.agent.yml up -d
```

Insert these immediately after the existing `up` recipe.

---

### `pyproject.toml` (MODIFY — add cryptography dep)

**Analog:** SELF — existing deps list at lines 11-31. Maintain alphabetical order.

**Modification:** insert `cryptography>=46.0.0,<49` between `beautifulsoup4` and `essentia-tensorflow` (alphabetic order):
```toml
dependencies = [
    "alembic>=1.18.4",
    "saq[redis]>=0.26.3",
    "asyncpg>=0.31.0",
    "beautifulsoup4>=4.14.3",
    "cryptography>=46.0.0,<49",     # NEW (Phase 29 D-02)
    "essentia-tensorflow>=2.1b6.dev1389; sys_platform != 'linux' or platform_machine == 'x86_64'",
    ...
]
```

**Why version pin:** RESEARCH.md Critical Discovery #1 confirmed cryptography is NOT a transitive dep. 46.0.0 is the minimum that ships abi3 wheels for Python 3.13 across all targets; `<49` accepts security patches in 46-48 but locks out unforeseen major-version API changes.

Run `uv lock && uv sync` after the edit; commit the updated `uv.lock`.

---

### `tests/test_task_split.py` (MODIFY — extend with cert_bootstrap case)

**Analog:** SELF — existing subprocess test at lines 33-73 (`test_agent_worker_does_not_import_phaze_database`).

**Existing subprocess pattern** is the template (lines 45-73): set env vars, `import phaze.tasks.agent_worker`, check `sys.modules` for forbidden names, exit 0/1, parent asserts.

**Phase 29 ADDITION** — mirror the existing test with `import phaze.cert_bootstrap` instead:
```python
def test_cert_bootstrap_stays_postgres_free() -> None:
    """Phase 29 D-22: phaze.cert_bootstrap must not transitively import phaze.database."""
    script = textwrap.dedent("""
        import sys
        import phaze.cert_bootstrap  # noqa: F401
        forbidden = ("phaze.database", "phaze.tasks.session", "sqlalchemy.ext.asyncio")
        present = [m for m in forbidden if m in sys.modules]
        if present:
            for m in present:
                mod = sys.modules[m]
                sys.stderr.write(f"BANNED MODULE IMPORTED: {m} (file={getattr(mod, '__file__', '?')})\\n")
            sys.exit(1)
        sys.exit(0)
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, f"cert_bootstrap contaminated sys.modules:\nstdout={result.stdout}\nstderr={result.stderr}"
```

**No env vars required** for cert_bootstrap (unlike agent_worker which needs PHAZE_AGENT_QUEUE etc.) — cert_bootstrap is a pure file-I/O module that doesn't call `get_settings()`.

---

### `tests/test_routers/test_admin_agents.py` (NEW)

**Analog:** `tests/test_routers/test_pipeline_scans.py` — EXACT match (smoke-app fixture + agent seeding + HTMX header tests).

**Smoke-app fixture pattern** (mirror lines 46-78):
```python
def _make_smoke_app(session: AsyncSession) -> FastAPI:
    app = FastAPI(title="admin-agents-smoke", version="test")
    app.include_router(admin_agents.router)
    app.dependency_overrides[get_session] = lambda: session
    return app

@pytest_asyncio.fixture
async def smoke(session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    # Seed test agents in all 5 states: alive, stale, dead, revoked, never
    now = datetime.now(UTC)
    session.add_all([
        Agent(id="alive-agent", name="Alive", scan_roots=["/data"], last_seen_at=now),
        Agent(id="stale-agent", name="Stale", scan_roots=["/data"], last_seen_at=now - timedelta(seconds=120)),
        Agent(id="dead-agent", name="Dead", scan_roots=["/data"], last_seen_at=now - timedelta(seconds=600)),
        Agent(id="revoked-agent", name="Revoked", scan_roots=["/data"], last_seen_at=now, revoked_at=now),
        Agent(id="never-agent", name="Never", scan_roots=["/data"]),
    ])
    await session.commit()

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
```

**HX-Request header test pattern** (Phase 27 pipeline_scans tests cover this — assert that with `HX-Request: true` the response body does NOT contain `<html>` or `<nav>`):
```python
async def test_htmx_request_returns_partial_only(smoke: AsyncClient) -> None:
    response = await smoke.get("/admin/agents", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "<html" not in response.text
    assert "agents-table-section" in response.text
```

---

### `tests/test_cert_bootstrap.py` (NEW)

**Analog:** existing tests like `tests/test_services/test_agent_bootstrap.py` for the "bootstrap module unit test" idiom. The cert content tests use `tmp_path` + `cryptography.x509.load_pem_x509_certificate` for parse-back verification.

**Test surface (D-22 LOCKED):**
1. **First call generates** — `ensure_certs_present(tmp_path, ...)`, assert all 4 files exist (`phaze-ca.crt`, `phaze-ca.key`, `phaze-server.crt`, `phaze-server.key`).
2. **Second call is no-op** — mtime on all 4 files stays the same after second call.
3. **Banner output sanity** — capture stdout, assert it contains "GENERATED NEW PHAZE INTERNAL CA" AND does NOT contain `BEGIN` or `PRIVATE KEY` (Pitfall 4 RESEARCH.md).
4. **File modes** — `phaze-ca.crt` mode is `0o644`; `phaze-ca.key` and `phaze-server.key` are `0o600`.
5. **SAN list parses** — leaf cert contains the SAN entries supplied via `sans_csv`.
6. **Default SAN parses 3 entries** — `_parse_san_entries("localhost,127.0.0.1,api")` yields 3 GeneralName objects (Pitfall 6 RESEARCH.md).

**Use pytest's `capsys` fixture** for the banner stdout test.

---

### `tests/test_services/test_agent_client_tls.py` (NEW — integration test)

**Analog:** `tests/test_services/test_agent_client.py` (existing respx-based tests) — same module structure but uses real TLS via in-process uvicorn instead of respx.

**Two-CA cert generation** (RESEARCH.md lines 491-497): Call `cert_bootstrap.ensure_certs_present(tmp_path1, ...)` and `cert_bootstrap.ensure_certs_present(tmp_path2, ...)` to get two distinct CA bundles. Start a real `uvicorn`/`hypercorn` smoke server with `tmp_path1`'s leaf cert, point `PhazeAgentClient(..., verify=tmp_path2 / "phaze-ca.crt")` at it, assert `httpx.ConnectError` (cert chain mismatch).

**Assertion:**
```python
with pytest.raises(httpx.ConnectError):
    await client.whoami()
```

Catch at `httpx.ConnectError` not `ssl.SSLCertVerificationError` — httpx wraps raw SSL errors (RESEARCH.md Pattern 3 line 484).

---

### `tests/test_deployment/test_api_filesystem_isolation.py` + `test_agent_compose.py` (NEW)

**No close analog.** No existing tests parse compose YAML files; this is a new structural pattern. RESEARCH.md "Code Examples" (lines 1029-1104) provides the full target code.

**Structural-parse style:**
```python
from pathlib import Path
import yaml

BANNED_MOUNT_TARGETS = ("/data/music", "/models", "/data/output")

def test_api_service_has_no_file_mounts() -> None:
    compose_path = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    data = yaml.safe_load(compose_path.read_text())
    api_volumes = data["services"]["api"].get("volumes", []) or []
    ...
```

**PyYAML is transitive** (RESEARCH.md confirmed via `uv pip show pyyaml`) — no new dev dep.

**Create `tests/test_deployment/__init__.py`** as an empty file so pytest discovers the directory.

---

### `tests/test_config/test_agent_settings_redis_password.py` (NEW)

**Analog:** `tests/test_config_role_split.py` — EXACT match (pydantic validator tests via monkeypatch env vars).

**Test surface:**
1. `agent_env=production` + `redis_url` without password → `pydantic.ValidationError`.
2. `agent_env=production` + `redis_url` with password → success.
3. `agent_env=dev` + passwordless redis_url → success (dev allows it).

**Use `monkeypatch.setenv` + `get_settings.cache_clear()`** to test different env combinations cleanly.

---

### `tests/test_tasks/test_heartbeat_cron.py` + `test_heartbeat_failure.py` (NEW)

**Analog:** `tests/test_tasks/test_execute_approved_batch_progress.py` — similar (mock PhazeAgentClient + assert call args).

**Cron handler test surface:**
1. **Successful heartbeat** — mock `ctx["api_client"].heartbeat` to succeed; mock `ctx["worker"].queue.info` to return `{"queued": 5, ...}`; call `await heartbeat_tick(ctx)`; assert `client.heartbeat.await_args` has `HeartbeatRequest(agent_version="0.1.0", worker_pid>0, queue_depth=5)`.
2. **Missing ctx keys** — call with empty ctx; assert WARNING logged, no exception.
3. **queue.info failure** — mock `queue.info` to raise; assert `queue_depth=0` defaulted.
4. **agent_version source** — assert `importlib.metadata.version("phaze")` is what populates the payload.

**Failure-mode test:** mock `client.heartbeat` to raise `AgentApiServerError`; assert WARNING logged via `caplog`; assert no exception escapes.

---

### `tests/test_services/test_agent_liveness.py` + `test_model_bootstrap.py` (NEW)

**Analogs:** existing pure-service tests like `tests/test_services/test_dedup.py` (no DB, no I/O). For `test_model_bootstrap.py`, mirror `tests/test_services/test_discogs_matcher.py` for the httpx-mocking pattern (or use `respx` against `httpx.stream`).

**`test_agent_liveness.py` surface:**
1. `classify(agent_alive, now)` → `"alive"` (5 states × matrix of inputs).
2. `sort_key(...)` produces expected ordering: revoked last → status_rank ascending → last_seen descending.

**`test_model_bootstrap.py` surface:**
1. Empty `tmp_path` → calls `download_to`, files appear.
2. Populated `tmp_path` (touch a `.pb` file) → no-op, doesn't call `download_to`.
3. Network failure (`download_to` raises) → `RuntimeError` propagates.

Use `monkeypatch` to replace `phaze.scripts.download_models.download_to` with a mock for case 3.

---

### `tests/test_utils/test_humanize.py` (NEW)

**Analog:** `tests/test_services/test_dedup.py` — pure-function tests, parametrized.

**Test surface** (UI-SPEC §Relative-Time Helper §Test surface lines 260-262 LOCKED):
- `None` → `"never"`
- Negative delta → `"just now"`
- 0, 23, 59 → `"0s ago"`, `"23s ago"`, `"59s ago"`
- 60, 90, 3599 → `"1m ago"`, `"1m ago"`, `"59m ago"`
- 3600, 86399 → `"1h ago"`, `"23h ago"`
- 86400 → `"1d ago"`
- Boundary: 89.7s → `"89s ago"` (int truncates, NOT rounds to `"1m"`)

Create `tests/test_utils/__init__.py` as empty file.

---

## Shared Patterns

### Postgres-Free Import Boundary (Phase 26 D-25 invariant)

**Source pattern:** `tests/test_task_split.py:33-73` + `phaze.tasks._shared.agent_bootstrap` module docstring (lines 1-21).

**Applies to (Phase 29):** `phaze.cert_bootstrap`, `phaze.entrypoint`, `phaze.tasks._shared.model_bootstrap`.

**The invariant:**
- The module's docstring MUST include an "IMPORT-BOUNDARY INVARIANT" banner with the forbidden module list: `phaze.database`, `phaze.tasks.session`, `sqlalchemy.ext.asyncio`.
- Concrete imports allowed: stdlib + `cryptography` + `httpx` + `pathlib` + project schemas/services that themselves don't pull Postgres.
- Verification: subprocess test in `tests/test_task_split.py` that imports the module fresh in a child python, then asserts none of the forbidden modules are in `sys.modules`.

**Why:** the agent role + api entrypoint must be importable on hosts that have no Postgres reachability. Drag-in violations crash the worker on Day 1 (Pitfall 3 + RESEARCH §Critical Discovery #3).

---

### HTMX Self-Replacing Poll Partial (UI-SPEC §Self-Contained Partial Pattern)

**Source pattern:** `src/phaze/templates/pipeline/partials/scan_progress_card.html:10-14` + UI-SPEC LOCKED markup.

**Applies to:** `templates/admin/partials/agents_table.html`.

**The pattern:**
```jinja
<section id="..."
         hx-get="..."
         hx-trigger="every 5s"
         hx-swap="outerHTML"
         class="...">
    ... content ...
</section>
```

`hx-swap="outerHTML"` replaces the entire `<section>`. Because the new section arrives with its own `hx-trigger` attribute, polling continues indefinitely.

**KEY DELTA from `scan_progress_card.html`:** the agents partial NEVER halts (UI-SPEC LOCKED). The pipeline-scan partial OMITS `hx-trigger` on terminal states to halt; agent monitor polls forever.

---

### Smoke-App Test Fixture

**Source pattern:** `tests/test_routers/test_pipeline_scans.py:46-78`.

**Applies to:** `tests/test_routers/test_admin_agents.py`.

**The pattern:**
1. Build a FastAPI app with only the router under test (and any other operator-facing routers needed for include-rendering).
2. Override `get_session` dependency with the test session.
3. Install `AsyncMock()` at `app.state.<resource>` for any lifespan-wired resource the router references (task_router, queue, redis).
4. Wrap with `httpx.AsyncClient(transport=ASGITransport(app=app), ...)` in a pytest-asyncio fixture.

**For admin_agents:** no `app.state.task_router` mock needed (admin page is read-only). Just override `get_session`.

---

### AliasChoices Env Mapping

**Source pattern:** `src/phaze/config.py:102-122` (BaseSettings auto_migrate / dev_seed_agent) + lines 153-172 (AgentSettings agent_api_url / agent_token).

**Applies to all 3 new Phase 29 config fields:** `api_tls_sans`, `agent_ca_file`, `agent_env`.

**The pattern:**
```python
field_name: type = Field(
    default=<sensible default>,
    validation_alias=AliasChoices("PHAZE_<UPPER_SNAKE>", "field_name"),
    description="...",
)
```

Both the documented env-var name (`PHAZE_API_TLS_SANS`) AND the bare attribute name (`api_tls_sans`) are accepted — the latter is for pytest monkeypatch convenience.

---

### Pydantic `extra="forbid"` on Schemas

**Source pattern:** `src/phaze/schemas/agent_heartbeat.py:6-16`.

**Applies to:** No NEW schemas in Phase 29 (heartbeat schema is reused unchanged). Pattern documented here because the cron handler MUST construct `HeartbeatRequest(agent_version=..., worker_pid=..., queue_depth=...)` with EXACTLY those three fields — `extra="forbid"` rejects anything else.

---

### Fire-and-Forget Logging Posture (Phase 28 D-16)

**Source pattern:** `src/phaze/tasks/_shared/agent_bootstrap.py:90-99` (`AgentApiError` caught, `logger.warning(...)`, continue) + Phase 28 D-16 `execute_approved_batch._execute_one` failure swallow.

**Applies to:** `phaze.tasks.heartbeat.heartbeat_tick`.

**The pattern:**
```python
try:
    await client.heartbeat(payload)
    logger.debug("...")
except AgentApiError as exc:
    logger.warning("heartbeat failed: %s", exc)
    # NO re-raise — SAQ retries on next cron tick
```

Catch `AgentApiError` (the base class) — NOT bare `Exception`. Coding bugs must still bubble up.

---

### Empty State Block (UI-SPEC LOCKED markup)

**Source pattern:** `src/phaze/templates/pipeline/partials/recent_scans_table.html:14-20`.

**Applies to:** `templates/admin/partials/agents_table.html` empty branch.

**The pattern:**
```jinja
<div class="text-center py-8">
    <p class="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-1">{heading}</p>
    <p class="text-sm text-gray-500 dark:text-gray-400">{body}</p>
</div>
```

---

### Table Row Hover + Cell Padding (Project-Wide Convention)

**Source pattern:** `recent_scans_table.html:36` + `execution/partials/agents_table.html:37`.

**Applies to:** `templates/admin/partials/agents_table.html`.

**The pattern:**
- Row: `class="hover:bg-gray-50 dark:hover:bg-phaze-panel"`
- Cell: `class="px-4 py-3 ..."`
- Header: `class="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase border-b border-gray-200 dark:border-phaze-border"` then `<th scope="col" class="px-4 py-3">...</th>`
- Divider: `<tbody class="divide-y divide-gray-100 dark:divide-phaze-border">`

The `py-3` (12px) is the project-wide locked exception (UI-SPEC §Spacing Exceptions).

---

### Pill Geometry (Project-Wide Convention)

**Source pattern:** every pill in the project uses literal class string `text-xs font-semibold px-2 py-0.5 rounded-full bg-<HUE>-100 dark:bg-<HUE>-950 text-<HUE>-700 dark:text-<HUE>-400` (or `bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300` for neutral).

**Applies to:** `templates/admin/partials/_status_pill.html`.

The `py-0.5` (2px) is the project-wide locked exception (UI-SPEC §Spacing Exceptions). Deviating breaks visual consistency with every other pill in the app.

---

## No Analog Found

Files with no close match in the existing codebase. Planner should derive directly from RESEARCH.md and UI-SPEC.md.

| File | Role | Reason |
|------|------|--------|
| `src/phaze/services/agent_liveness.py` | service (pure classifier) | No existing service is a "pure classifier without DB access". `phaze.services.dedup` is the nearest stylistic match. RESEARCH.md Pattern 6 lines 696-726 supplies full target code. |
| `src/phaze/utils/humanize.py` | utility | `src/phaze/utils/` does not exist yet. Phase 29 establishes the package. RESEARCH.md Pattern 6 lines 729-748 supplies full code. Tests in new `tests/test_utils/`. |
| `docs/deployment.md` | docs | No existing `docs/*.md` ops walkthrough. Free-form prose per D-23 spec — no code analog needed. Outline given in CONTEXT.md `<decisions>` D-23. |
| `tests/test_deployment/test_*.py` | structural-parse test | No existing tests parse compose YAML. RESEARCH.md "Code Examples" lines 1029-1104 supplies full code. New test directory + `__init__.py` per CONTEXT.md `<specifics>`. |
| `.env.example.agent` | infra-config (subset) | Subset of existing `.env.example` but tailored to file-server context. D-23 + RESEARCH.md specify the exact variable list. |
| `src/phaze/entrypoint.py` | process exec shim | `agent_watcher/__main__.py` is the closest by entry-point shape, but the body uses `os.execvp` (not asyncio) to replace itself with uvicorn. Pattern documented inline above. |

---

## Metadata

**Analog search scope:**
- `src/phaze/routers/` — 24 files scanned (chose `pipeline_scans.py` as primary analog)
- `src/phaze/services/` — 24 files scanned (chose `agent_bootstrap.py` as primary bootstrap analog)
- `src/phaze/tasks/` — 13 files scanned (chose `agent_worker.py` + `controller.py` for SAQ patterns)
- `src/phaze/tasks/_shared/` — 2 existing modules (`agent_bootstrap.py`, `queue_defaults.py`)
- `src/phaze/templates/` — 9 subdirs scanned (chose `pipeline/dashboard.html`, `pipeline/partials/recent_scans_table.html`, `pipeline/partials/scan_status_pill.html`, `pipeline/partials/scan_progress_card.html`, `execution/partials/agents_table.html` as primary template analogs)
- `tests/test_routers/` — 27 files scanned (chose `test_pipeline_scans.py` as primary smoke-app analog)
- `tests/test_services/` — 24 files scanned
- `tests/test_tasks/` — 9 files scanned

**Files scanned:** ~140 source files, ~100 test files, 6 config/infra files.

**Pattern extraction date:** 2026-05-16.

---

*Phase: 29-deployment-hardening-agents-admin*
*Mapped by: gsd-pattern-mapper*
