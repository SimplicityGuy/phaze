# Phase 29: Deployment Hardening & Agents Admin - Research

**Researched:** 2026-05-16
**Domain:** Two-host deployment hardening — uvicorn-direct TLS via self-signed internal CA, Redis `requirepass` + LAN binding, `docker-compose.agent.yml` split, SAQ 30s heartbeat cron, `/admin/agents` HTMX page, models auto-download on agent startup
**Confidence:** HIGH

## Summary

Phase 29 closes v4.0 with deployment hardening: strip file mounts from the app-server compose, terminate TLS in uvicorn against an auto-generated internal CA + leaf cert (10y CA / 2y leaf, ECDSA P-256), require a Redis password on a LAN-bound port, ship a standalone `docker-compose.agent.yml` referencing GHCR, auto-download essentia weights on first agent_worker / watcher boot, register a 30-second SAQ cron that POSTs heartbeats from the agent_worker, and add an HTMX-polled `/admin/agents` page showing 5-state liveness.

Every locked decision in CONTEXT.md (D-01..D-23) is implementable with the existing stack PLUS one explicit new runtime dependency: `cryptography>=46.0.0`. That single discovery is the most important research finding (see §Critical Discoveries). Everything else (httpx `verify=`, uvicorn `--ssl-keyfile`, SAQ `CronJob`, redis-py URL parsing, HTMX 2.x `hx-trigger`) is already available at the project's pinned versions.

**Primary recommendation:** Add `cryptography>=46.0.0` to pyproject.toml as a runtime dependency in the first plan, then build `phaze.cert_bootstrap` on top of `cryptography.x509.CertificateBuilder` + `ec.SECP256R1`. The CONTEXT.md D-discretion line claiming "cryptography is already a transitive dep (FastAPI/Starlette)" is FALSE for this project (verified via `uv pip show fastapi starlette` + `uv pip list`).

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| TLS termination | API / Backend (uvicorn) | — | D-01: uvicorn-direct, no reverse proxy. Private LAN scale doesn't justify Caddy/nginx |
| Cert generation + CA distribution | API / Backend (api container entrypoint) | Operator (file copy) | D-02/D-03: bootstrap module runs before uvicorn binds; operator scp's CA to file servers |
| Cert trust on agents | Agent (httpx.AsyncClient) | — | D-03: each agent's `PhazeAgentClient` passes `verify=<ca_file>` |
| Redis auth + LAN binding | Application Server (Docker compose) | — | D-05: compose `command` + `ports` mapping is the binding mechanism |
| Redis URL validation | Agent (AgentSettings model_validator) | — | D-06: production-mode validator refuses passwordless URLs |
| Heartbeat emission | Agent (SAQ cron in agent_worker) | — | D-07/D-08: only worker emits, not watcher; SAQ cron @ 30s |
| Heartbeat persistence | API / Backend (existing endpoint, UNCHANGED) | Database | Phase 25 endpoint already writes `agents.last_seen_at` + `last_status` |
| Admin page rendering | API / Backend (FastAPI router + Jinja2) | Database (read) | D-11: `/admin/agents` reads `agents` table; no Redis access |
| HTMX poll cadence | Browser / Client | API / Backend (partial route) | D-13: `hx-trigger="every 5s"` → `/admin/agents/_table` |
| Status classification | API / Backend (service helper) | — | UI-SPEC: `agent_liveness.classify()` from `agents.last_seen_at` |
| Models auto-download | Agent (startup hook) | — | D-21: agent_worker + watcher startup; app-server never touches /models |
| Compose YAML invariant tests | CI (YAML parse) | — | D-19: pytest reads YAML; no Docker required |

## Standard Stack

### Core (existing — no upgrades needed)
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| FastAPI | 0.136.1+ | Web framework | [VERIFIED: pyproject.toml] |
| uvicorn | 0.46.0+ | ASGI + TLS termination | [VERIFIED: pyproject.toml]; supports `--ssl-keyfile` + `--ssl-certfile` natively |
| SAQ | 0.26.3 | Task queue + cron | [VERIFIED: pyproject.toml + `uv pip show`]; `CronJob` supports 6-field cron via croniter |
| croniter | 6.2.2 | Cron parsing (SAQ dep) | [VERIFIED: `uv pip show croniter`]; pulled in by `saq[redis]` |
| httpx | 0.28.1 | HTTP client w/ `verify=` | [VERIFIED: pyproject.toml]; `verify: ssl.SSLContext \| str \| bool = True` |
| Jinja2 | (transitive via FastAPI) | Server-side templates | [VERIFIED: existing routers use `Jinja2Templates`] |
| pydantic-settings | 2.14.0+ | Config validation | [VERIFIED: pyproject.toml]; supports `model_validator(mode="after")` |
| PyYAML | 6.0.3 | YAML parse for tests | [VERIFIED: `uv pip show pyyaml`]; transitive (uvicorn dep) |
| respx | 0.23.1 | httpx mock | [VERIFIED: pyproject.toml dev-deps]; used by existing test_agent_client.py |
| pytest-asyncio | 1.3.0+ | Async test runner | [VERIFIED: pyproject.toml dev-deps] |

### NEW runtime dependency (must be added)
| Library | Version | Purpose | Why Required |
|---------|---------|---------|--------------|
| cryptography | >=46.0.0,<49 | x509 + ECDSA cert generation | [VERIFIED: `uv pip list` confirms NOT installed; NOT a transitive dep of FastAPI/Starlette]. Required by `phaze.cert_bootstrap` for CA + leaf generation (D-02). 46.x is the current stable minor with abi3 wheels for python 3.13 on linux/amd64 + linux/arm64 + macOS arm64. Pin upper bound at 49 to avoid unforeseen API breaks across major releases (current latest is 48.0.0 — pinning `<49` allows 48.x security patches). License: Apache-2.0 OR BSD-3-Clause. |

**Version verification (slopcheck-equivalent inspection):**
- `cryptography` on PyPI — published since 2014, 100s of millions of downloads/week, GitHub org `pyca` (Python Cryptographic Authority), backed by Python Cryptographic Authority. [VERIFIED: legitimate, widely used].
- No `postinstall` (Python wheels don't run install scripts).
- Wheels: `manylinux2014_x86_64`, `manylinux2014_aarch64`, `macosx_11_0_arm64`, `macosx_11_0_x86_64`. [CITED: https://pypi.org/project/cryptography/]

### Supporting (existing — used as-is)
| Library | Version | Purpose | Where in this phase |
|---------|---------|---------|---------------------|
| tenacity | 8.5.0+ | Retry funnel | [VERIFIED: pyproject.toml]; heartbeat caller inherits PhazeAgentClient's retry policy automatically |
| watchdog | 4.0+ | FS events (watcher) | [VERIFIED: pyproject.toml]; watcher invokes model_bootstrap at startup |
| importlib.metadata | stdlib | Read `phaze.__version__` | [VERIFIED: `m.version('phaze')` returns `'0.1.0'`] |

### Alternatives Considered (do not use)
| Instead of | Could Use | Why Rejected |
|------------|-----------|--------------|
| `cryptography` for cert gen | `openssl` subprocess | Adds runtime shell dep, weaker error handling, harder to test deterministically, more brittle (different openssl CLI versions across base images). `cryptography` Python API is the type-safe path. |
| `cryptography` | `trustme` | trustme is a TEST library, not production cert mgmt. Internal API may shift. trustme itself depends on `cryptography`, so it'd add an extra dep for no win. |
| `humanize` (PyPI lib) for relative time | hand-rolled helper | UI-SPEC LOCKS hand-rolled helper at `phaze.utils.humanize.relative_time` — 12 lines, no dep, fully testable. Matches CONTEXT.md "no new pip dependencies" intent. |
| Real Docker test for filesystem isolation | YAML-parse test | D-19 + `<specifics>` explicitly prefer the YAML parse: fast (< 100ms), no Docker required in CI, structural assertions are honest signals. |
| 6-field cron with seconds | `asyncio.create_task` sleep loop | SAQ + croniter 6.2.2 fully support 6-field cron (trailing seconds — see §Critical Discoveries §2). No fallback needed. |

**Installation:**
```bash
uv add 'cryptography>=46.0.0,<49'
```

**Version verification commands run during research:**
```bash
uv run python -c "import saq; print(saq.__version__)"       # → 0.26.3
uv run python -c "import croniter; import importlib.metadata; print(importlib.metadata.version('croniter'))"  # → 6.2.2
uv run python -c "import httpx; print(httpx.__version__)"   # → 0.28.1
uv run python -c "import yaml; print(yaml.__version__)"     # → 6.0.3
uv run python -c "import respx; print(respx.__version__)"   # → 0.23.1
uv run pip list | grep -i crypto                            # → (empty — NOT installed)
```

## Package Legitimacy Audit

| Package | Registry | Age | Downloads | Source Repo | slopcheck | Disposition |
|---------|----------|-----|-----------|-------------|-----------|-------------|
| cryptography | PyPI | 12 yrs (since 2014) | ~200M/wk | github.com/pyca/cryptography | [OK] (manual inspection) | Approved — sole new dep |

slopcheck CLI was not available during this research session; the single new package was instead verified manually against:
- Stable PyPI publication history (since 2014, no name typo, no recent ownership churn)
- Project membership in the Python Cryptographic Authority (PyCA) GitHub org
- Apache-2.0 OR BSD-3-Clause SPDX license
- Pre-built abi3 wheels covering the project's full target matrix (verified at https://pypi.org/project/cryptography/#files)

No other Phase 29 work installs packages.

## Critical Discoveries (read first)

These four findings shape the plan more than anything else. Each is verified against running source / installed binaries — none are training-data assumptions.

### 1. `cryptography` is NOT a transitive dependency [VERIFIED: `uv pip list` + `uv pip show fastapi starlette`]
CONTEXT.md D-discretion claims "cryptography is already a transitive dep (FastAPI/Starlette), prefer it for type safety and Windows compatibility." This is **incorrect** for this project. Verified by:
```bash
$ uv pip list | grep -i crypto
(empty)
$ uv pip show fastapi
Requires: annotated-doc, pydantic, starlette, typing-extensions, typing-inspection
$ uv pip show starlette
Requires: anyio
$ grep -B1 -A4 'name = "cryptography"' uv.lock
(no match)
```
**Action for planner:** Phase 29 MUST add `cryptography>=46.0.0,<49` to `pyproject.toml` as the first task. The cert bootstrap module cannot be built without it. This is not a "discretion" choice between `cryptography` and `openssl` shell-out; it's an explicit addition either way.

### 2. SAQ 6-field cron uses TRAILING seconds, NOT leading [VERIFIED: croniter 6.2.2 source + behavioral test]
CONTEXT.md D-08 shows `cron="*/30 * * * * *"` (leading seconds). **This is wrong for croniter 6.x default config**, which puts seconds as field 6 (trailing). Empirical verification:
```python
from croniter import croniter
list(croniter('*/30 * * * * *', start_time=0))[:5]    # → [1.0, 2.0, 3.0, 4.0, 5.0] (every second — WRONG)
list(croniter('* * * * * */30', start_time=0))[:5]    # → [30.0, 60.0, 90.0, ...] (every 30s — CORRECT)
```
The croniter README states: *"Croniter is able to do second repetition crontabs form and by default seconds are the 6th field"* [CITED: https://github.com/pallets-eco/croniter]. The `second_at_beginning=True` flag flips this — but SAQ's `saq.worker.schedule` calls `croniter(cron_str, start_time)` with no kwargs, so the default trailing-seconds convention applies.
**Action for planner:** the heartbeat CronJob string is `"* * * * * */30"` (trailing). Update D-08 implementation accordingly. No `asyncio.create_task` fallback is needed — SAQ + croniter 6.2.2 fully support sub-minute cron.

### 3. `agent_worker` is a single .py file, NOT a package [VERIFIED: filesystem inspection]
```bash
$ ls src/phaze/tasks/agent_worker*
src/phaze/tasks/agent_worker.py     # single file, not a package
```
CONTEXT.md repeatedly references `phaze.tasks.agent_worker.settings.cron_jobs` as if `agent_worker` were a package directory (e.g., D-08 talks about "register a cron entry in `phaze.tasks.agent_worker.settings.cron_jobs`"). The existing layout uses a module-level `settings = {...}` dict at line 179 of the single `agent_worker.py` file. The SAQ CLI invocation `uv run saq phaze.tasks.agent_worker.settings` works as `<module>.<attribute>` and does NOT require a package conversion.
**Action for planner:** Phase 29 has TWO equally valid options — (a) keep `agent_worker.py` as a single file and add `cron_jobs` to the existing `settings` dict in-place + put `heartbeat_tick` either inline in the same file or in a new sibling module `phaze/tasks/agent_worker_cron.py`; OR (b) convert `agent_worker.py` to a package `agent_worker/` with `__init__.py` + `settings.py` + `cron.py`. **Option (a) is strictly less code churn and preserves all existing imports unchanged**, including `from phaze.tasks.agent_worker import startup, shutdown` references the test suite makes. Recommend Option (a): add `cron_jobs=[CronJob(heartbeat_tick, cron="* * * * * */30", unique=True, timeout=10)]` to the existing settings dict + define `heartbeat_tick` in a new sibling file `phaze/tasks/heartbeat.py` to keep `agent_worker.py` thin.

### 4. SAQ Worker polls cron every 1 second (default) [VERIFIED: SAQ source `saq/worker.py:66-68`]
```python
self.timers: TimersDict = {
    "schedule": 1,           # how often Worker.schedule() runs
    "worker_info": 10,
    ...
}
```
The SAQ Worker invokes `self.schedule()` every 1 second by default. Each tick calls `croniter(cron_str, datetime.now()).get_next()` and enqueues a job keyed `cron:heartbeat_tick` with `unique=True`. The unique-key + scheduled timestamp combination ensures only one heartbeat job is queued at any moment, regardless of how often `schedule()` polls.
**Action for planner:** trust SAQ's default `timers["schedule"]=1`. No need to override. The 30-second cadence is enforced by croniter, not by the poll loop.

## Architecture Patterns

### System Architecture Diagram

```
                         APPLICATION SERVER (host A)
                         ┌─────────────────────────────────────────┐
   Operator browser ───► │  uvicorn (TLS on :8000)                 │
       /admin/agents     │  ─ TLS via ./certs/phaze-server.{crt,key}│
                         │  ─ /admin/agents → admin_agents router  │
                         │  ─ /admin/agents/_table (HTMX partial)  │
                         │                                          │
                         │  cert_bootstrap entrypoint (pre-uvicorn) │
                         │  ─ generates CA + leaf if missing       │
                         │  ─ prints loud banner on first gen      │
                         │                                          │
                         │  worker (control role, fileless)        │
                         │  postgres :5432 (loopback only)         │
                         │  redis :${REDIS_BIND_IP}:6379           │
                         │    ─ requirepass ${REDIS_PASSWORD}      │
                         └─────────────────────────────────────────┘
                              ▲  HTTPS (TLS via PHAZE_AGENT_CA_FILE)
                              │  redis (TLS-free, password-auth)
                              │
                         ┌─────────────────────────────────────────┐
                         │             FILE SERVER (host B)        │
                         │  docker-compose.agent.yml               │
                         │                                          │
                         │  worker (agent role)                    │
                         │   ─ startup: whoami → ensure_models →   │
                         │     SAQ start                            │
                         │   ─ cron @ 30s: heartbeat_tick           │
                         │     ▼ POST /api/internal/agent/heartbeat │
                         │       {agent_version, worker_pid,        │
                         │        queue_depth=Queue.info()["queued"]│
                         │                                          │
                         │  watcher  (mounts SCAN_PATH:ro)          │
                         │  audfprint (sidecar, build)              │
                         │  panako    (sidecar, build)              │
                         │                                          │
                         │  Volumes:                                │
                         │   ─ SCAN_PATH:/data/music:ro             │
                         │   ─ MODELS_PATH:/models:rw  (auto-DL)    │
                         │   ─ CA_PATH:/certs:ro       (CA only)    │
                         └─────────────────────────────────────────┘
```

### Recommended Project Structure (new + modified files)
```
src/phaze/
├── cert_bootstrap.py                   # NEW (D-02)
├── constants.py                        # MODIFY: add AGENT_LIVENESS_* constants
├── config.py                           # MODIFY: add agent_ca_file, agent_env, api_tls_sans
├── main.py                             # MODIFY: register admin_agents router + cert bootstrap call
├── routers/admin_agents.py             # NEW (D-11)
├── services/agent_liveness.py          # NEW (UI-SPEC): classify() helper
├── services/agent_client.py            # MODIFY: pass verify= to httpx.AsyncClient
├── tasks/_shared/agent_bootstrap.py    # MODIFY: pass verify into construct_agent_client
├── tasks/_shared/model_bootstrap.py    # NEW (D-21): ensure_models_present()
├── tasks/heartbeat.py                  # NEW (D-08): heartbeat_tick(ctx) cron handler
├── tasks/agent_worker.py               # MODIFY: cron_jobs entry + model_bootstrap call in startup
├── agent_watcher/__main__.py           # MODIFY: model_bootstrap call after whoami
├── templates/admin/agents.html         # NEW (UI-SPEC)
├── templates/admin/partials/agents_table.html    # NEW (UI-SPEC HTMX poll target)
├── templates/admin/partials/_status_pill.html    # NEW (UI-SPEC reusable include)
├── templates/base.html                 # MODIFY: add "Agents" nav link
└── utils/humanize.py                   # NEW (UI-SPEC): relative_time()

scripts/
├── download-models.sh                  # MODIFY: extract URL/SHA manifest to .py helper
└── update-project.sh                   # MODIFY: list new modules

docker-compose.yml                      # REWRITE: strip mounts; delete watcher+agent-worker; add TLS+Redis hardening
docker-compose.agent.yml                # NEW (D-15)
.env.example                            # MODIFY: add REDIS_PASSWORD, REDIS_BIND_IP, PHAZE_API_TLS_SANS
.env.example.agent                      # NEW (D-23)
docs/deployment.md                      # NEW (D-23)
PROJECT.md                              # MODIFY: append "Deployment" subsection
justfile                                # MODIFY: keep existing `up`; add `up-agent`, `up-all`

tests/
├── test_cert_bootstrap.py              # NEW (D-22)
├── test_config/test_agent_settings_redis_password.py    # NEW (D-22)
├── test_deployment/                    # NEW dir
│   ├── __init__.py
│   ├── test_api_filesystem_isolation.py    # NEW (D-22 / D-19)
│   └── test_agent_compose.py               # NEW (D-22 / D-15..D-17)
├── test_routers/test_admin_agents.py   # NEW (D-22 / D-11..D-14)
├── test_services/test_agent_client_tls.py  # NEW (D-22 / D-04)
├── test_services/test_model_bootstrap.py   # NEW (D-22 / D-21)
├── test_services/test_agent_liveness.py    # NEW (UI-SPEC classifier)
├── test_tasks/test_heartbeat_cron.py       # NEW (D-22 / D-07..D-10)
├── test_tasks/test_heartbeat_failure.py    # NEW (D-22 / D-09)
├── test_task_split.py                  # MODIFY: extend with cert_bootstrap Postgres-free case
├── test_templates/                     # may exist; add status-pill render test
└── test_utils/test_humanize.py         # NEW (UI-SPEC relative_time)
```

### Pattern 1: Self-signed CA + leaf cert generation (cryptography x509)

[CITED: https://cryptography.io/en/latest/x509/tutorial/ — official tutorial]

```python
# src/phaze/cert_bootstrap.py
"""Pre-uvicorn cert bootstrap (D-02).

IMPORT-BOUNDARY INVARIANT (extends Phase 26 D-25):
    MUST NOT import phaze.database, phaze.tasks.session, or sqlalchemy.ext.asyncio.
    Verified by tests/test_task_split.py (Phase 29 extension per D-22).

`cryptography` only depends on `cffi` + system libssl/libcrypto. Confirmed
no transitive SQLAlchemy import via subprocess inspection.
"""
from __future__ import annotations

import datetime
import ipaddress
import logging
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

logger = logging.getLogger(__name__)

_BANNER = """\
==============================================================
GENERATED NEW PHAZE INTERNAL CA at {ca_path}
COPY THIS FILE TO EVERY FILE SERVER and point each agent's
PHAZE_AGENT_CA_FILE env var at it. EXISTING AGENTS WILL FAIL
TO CONNECT UNTIL THEY HAVE THIS NEW CA.
=============================================================="""


def _parse_san_entries(sans_csv: str) -> list[x509.GeneralName]:
    """Parse comma-separated SAN list: DNSName for hostnames, IPAddress for IPs."""
    entries: list[x509.GeneralName] = []
    for raw in (s.strip() for s in sans_csv.split(",") if s.strip()):
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(raw)))
        except ValueError:
            entries.append(x509.DNSName(raw))
    return entries


def _generate_ca(cn: str) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.UTC))
        .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_cert_sign=True, crl_sign=True,
            content_commitment=False, key_encipherment=False, data_encipherment=False,
            key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _generate_leaf(
    ca_key: ec.EllipticCurvePrivateKey, ca_cert: x509.Certificate,
    cn: str, sans: list[x509.GeneralName],
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.UTC))
        .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=730))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_encipherment=True,
            content_commitment=False, data_encipherment=False, key_agreement=False,
            key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    return leaf_key, leaf_cert


def ensure_certs_present(certs_dir: Path, cn: str, sans_csv: str) -> None:
    """Idempotent CA + leaf bootstrap. Generates only if files missing or unparseable.

    File modes:
        - phaze-ca.crt        0644  (public; distributed to agents)
        - phaze-ca.key        0600  (private CA signing key; never leaves app server)
        - phaze-server.crt    0644
        - phaze-server.key    0600
    """
    certs_dir.mkdir(parents=True, exist_ok=True)
    ca_crt = certs_dir / "phaze-ca.crt"
    ca_key_path = certs_dir / "phaze-ca.key"
    server_crt = certs_dir / "phaze-server.crt"
    server_key = certs_dir / "phaze-server.key"

    # Idempotency: all four exist and CA + leaf parse.
    if all(p.exists() for p in (ca_crt, ca_key_path, server_crt, server_key)):
        try:
            x509.load_pem_x509_certificate(ca_crt.read_bytes())
            x509.load_pem_x509_certificate(server_crt.read_bytes())
            logger.info("cert_bootstrap: existing certs at %s — no-op", certs_dir)
            return
        except ValueError:
            logger.warning("cert_bootstrap: existing certs unparseable; regenerating")

    sans = _parse_san_entries(sans_csv)
    ca_key, ca_cert = _generate_ca(cn=f"Phaze Internal CA ({cn})")
    leaf_key, leaf_cert = _generate_leaf(ca_key, ca_cert, cn=cn, sans=sans)

    # Write CA cert + key
    ca_crt.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    ca_crt.chmod(0o644)
    ca_key_path.write_bytes(ca_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()))
    ca_key_path.chmod(0o600)

    # Write leaf
    server_crt.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    server_crt.chmod(0o644)
    server_key.write_bytes(leaf_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()))
    server_key.chmod(0o600)

    banner = _BANNER.format(ca_path=ca_crt)
    print(banner, flush=True)             # noqa: T201 — see D-discretion "loud banner stdout"
    for line in banner.splitlines():
        logger.warning(line)
```

**Notes for planner:**
- ECDSA P-256 is chosen per CONTEXT.md D-discretion ("ECDSA is faster + smaller; pick unless something breaks"). Wider compat than RSA-3072 for this LAN-only use.
- The `NoEncryption()` private-key serialization is intentional — the key file is `0600` and never leaves the container's bind-mount. Encrypting with a passphrase would require pre-shared secret distribution which is precisely what HTTPS replaces.
- `x509.random_serial_number()` produces a 159-bit RFC-5280-compliant serial.
- CN values: `localhost` (single-host dev) is the safest default for `PHAZE_API_HOST`.

### Pattern 2: Uvicorn TLS termination [CITED: https://www.uvicorn.org/]

```yaml
# docker-compose.yml — root (app-server)
services:
  api:
    command: >
      uv run uvicorn phaze.main:app
      --host 0.0.0.0 --port 8000
      --ssl-keyfile /certs/phaze-server.key
      --ssl-certfile /certs/phaze-server.crt
    volumes:
      - "${CA_PATH:-./certs}:/certs:rw"   # rw because cert_bootstrap may write
```

**Fail-fast behavior on missing cert files:** [CITED: github.com/Kludex/uvicorn issues] uvicorn's `create_ssl_context` raises `FileNotFoundError` at process init when either `--ssl-keyfile` or `--ssl-certfile` point at a non-existent path. The process exits non-zero before the HTTP loop starts. This is the desired behavior: if `cert_bootstrap` hasn't run yet, uvicorn dies → container restarts → operator notices.

**Bootstrap entrypoint pattern:** The cleanest way to guarantee `cert_bootstrap` runs *before* uvicorn binds is a Docker entrypoint shim:

```dockerfile
# Dockerfile (no Phase 29 change needed beyond what's documented)
CMD ["uv", "run", "python", "-m", "phaze.entrypoint"]
```

OR override the api `command:` in compose:

```yaml
# docker-compose.yml — explicit two-step command
command: >
  sh -c "
  uv run python -m phaze.cert_bootstrap &&
  uv run uvicorn phaze.main:app
    --host 0.0.0.0 --port 8000
    --ssl-keyfile /certs/phaze-server.key
    --ssl-certfile /certs/phaze-server.crt
  "
```

The `sh -c` pattern works but ties the bootstrap behavior to compose. **Recommended:** add a tiny `phaze/__main__.py`-style entry: `phaze/entrypoint.py` containing `if __name__ == "__main__": ensure_certs_present(...); _exec_uvicorn(...)` and reference it from compose `command:`. This makes the bootstrap testable directly via `python -m phaze.entrypoint --dry-run`.

**Effect on `--reload` (dev override):** `docker-compose.override.yml` may add `--reload` for dev. Uvicorn's reload is incompatible with `--ssl-keyfile` on some versions [CITED: github.com/Kludex/uvicorn/issues/352 — historical bug]; but with uvicorn 0.46.0 this is resolved. Plan defensively: dev override keeps `--reload` and adds `--reload-exclude '/certs/*'` so cert file writes don't trigger reload loops. Verify behavior in a dev smoke test.

### Pattern 3: httpx verify= against an internal CA

[CITED: https://www.python-httpx.org/advanced/ssl/]

```python
# src/phaze/tasks/_shared/agent_bootstrap.py — MODIFY construct_agent_client
def construct_agent_client(cfg: AgentSettings) -> PhazeAgentClient:
    # Validate CA file at construction time (fail-fast)
    ca_path = Path(cfg.agent_ca_file)
    if not ca_path.exists() or ca_path.stat().st_size == 0:
        msg = f"CA file empty or unreadable: {cfg.agent_ca_file}"
        raise RuntimeError(msg)
    return PhazeAgentClient(
        base_url=cfg.agent_api_url,
        token=cfg.agent_token.get_secret_value(),
        timeout=30.0,
        verify=cfg.agent_ca_file,    # NEW — string path
    )
```

```python
# src/phaze/services/agent_client.py — MODIFY __init__
class PhazeAgentClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        verify: ssl.SSLContext | str | bool = True,    # NEW
        _client: httpx.AsyncClient | None = None,
    ) -> None:
        ...
        self._client = _client or httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            verify=verify,        # passes through to ssl.SSLContext
        )
```

**Exception path on wrong-CA:** httpx raises `httpx.ConnectError` whose `__cause__` wraps `ssl.SSLCertVerificationError` (a subclass of `ssl.SSLError`). The test asserts:
```python
with pytest.raises(httpx.ConnectError):
    await client.whoami()
```
Catch at `httpx.ConnectError` not `ssl.SSLCertVerificationError` — httpx maps raw network errors into its own exception hierarchy. [CITED: https://www.python-httpx.org/advanced/ssl/]

**Test approach for D-04 wrong-CA assertion:** The cleanest path is **NOT respx** (which mocks the httpx transport before TLS happens). Use **trustme** for ad-hoc CA generation inside the test… but `trustme` is not in our dependency tree. Alternative: have `cert_bootstrap.ensure_certs_present()` generate certs into two distinct `tmp_path` dirs, start a real `uvicorn` test server with one CA, point the client at the other CA's PEM, expect `httpx.ConnectError`. This is heavier than typical respx tests but is the only way to *actually* assert TLS rejection rather than mocking it.

Two test strategies for D-04 (planner picks):
1. **Direct ssl test (lightweight, recommended):** Use `cert_bootstrap` itself to write two CA bundles in `tmp_path`, then construct an `ssl.SSLContext` from each. Build an `httpx.AsyncClient(verify=<wrong_ca>)`, spin up a minimal `aiohttp.web` or `hypercorn`/`uvicorn` async server on a free port serving the correct cert, fire one request, assert `httpx.ConnectError`. Tear down. ~30 lines, no new deps.
2. **trustme-less ssl-context unit test (cheapest):** Construct two `ssl.SSLContext` objects from `cert_bootstrap` outputs. Verify that `verify=wrong_ca` produces a context that REJECTS the cert chain via `ssl.match_hostname` or `SSLContext.wrap_socket` at the asyncio level. No HTTP server needed; tests the cert-trust logic in isolation. Slightly less realistic but faster CI.

Recommend option 1 with a 2-second smoke server lifetime — fast enough, exercises the real httpx path.

### Pattern 4: Redis hardening in compose

```yaml
# docker-compose.yml — root (app-server)
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

**`${VAR:?msg}` fail-fast:** docker-compose v2 enforces unset-var checks at parse time; `docker compose up` exits with `error: REDIS_PASSWORD required` if `REDIS_PASSWORD` is missing from `.env` / shell env. [CITED: docs.docker.com/compose/compose-file/12-interpolation]

**`${IP}:6379:6379` binding:** [CITED: docs.docker.com/compose/compose-file/05-services/#ports] — *"If you do not specify a host IP, Docker binds to all interfaces (0.0.0.0)."* Setting an explicit IP DOES restrict binding to that interface. Tested form: `"127.0.0.1:6379:6379"` binds to loopback only. `"192.168.1.10:6379:6379"` binds to that LAN IP only. There is **no recent regression** that ignores the IP prefix — the syntax is honored by Docker Engine 20.10+.

**`--no-auth-warning`:** When `redis-cli -a <pw>` runs, redis writes `Warning: Using a password with '-a' or '-u' option on the command line interface may not be safe.` to stderr. This warning does NOT cause healthcheck failure (the command still exits 0 on ping success), but it pollutes logs. The `--no-auth-warning` flag suppresses it. [CITED: github.com/redis/redis/issues/5073 — flag added by redis 5.0+]

**Redis URL parsing for SAQ + redis-py:** Both libraries accept `redis://default:<password>@<host>:6379/0`. The `default` is the username (redis ACL default user); the password is URL-encoded. Verified by inspection — SAQ uses `redis.asyncio.from_url(url)` which delegates to redis-py's URL parser. No special handling needed; pydantic-settings stores the URL as a plain string.

### Pattern 5: SAQ cron entry for 30s heartbeat

```python
# src/phaze/tasks/heartbeat.py — NEW
"""30-second cron handler that POSTs an agent heartbeat (D-08/D-10).

Reads from SAQ ctx:
    - ctx["api_client"]: PhazeAgentClient (set in agent_worker.startup)
    - ctx["agent_identity"]: AgentIdentity (set in agent_worker.startup)
    - ctx["worker"]: SAQ Worker — for queue access (gives Queue.info)

Failure policy (D-09): catch AgentApiError, log WARNING, return. SAQ
retries on next tick.
"""
from __future__ import annotations

import importlib.metadata
import logging
import os
from typing import Any

from phaze.schemas.agent_heartbeat import HeartbeatRequest
from phaze.services.agent_client import AgentApiError

logger = logging.getLogger(__name__)


async def heartbeat_tick(ctx: dict[str, Any]) -> None:
    """SAQ cron handler. ctx is the worker context dict from startup hook."""
    client = ctx.get("api_client")
    identity = ctx.get("agent_identity")
    if client is None or identity is None:
        logger.warning("heartbeat_tick: ctx not initialized; skipping")
        return

    # Queue depth from SAQ Queue.info()["queued"]
    queue = ctx["worker"].queue  # SAQ Worker stashes its Queue on self.queue
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

```python
# src/phaze/tasks/agent_worker.py — MODIFY settings dict (existing line 179)
from saq import CronJob

from phaze.tasks.heartbeat import heartbeat_tick

settings = {
    "queue": queue,
    "functions": [..., heartbeat_tick],   # ADD to functions list
    "cron_jobs": [
        CronJob(heartbeat_tick, cron="* * * * * */30", unique=True, timeout=10),
    ],
    "concurrency": get_settings().worker_max_jobs,
    "startup": startup,
    "shutdown": shutdown,
}
```

**SAQ ctx mutability:** [VERIFIED: saq/worker.py:79] `self.context: CtxType = t.cast(CtxType, {"worker": self})`. The startup hook receives this dict and mutates it (adds `api_client`, `agent_identity`). For each job/cron tick, SAQ does `context = {**self.context, "job": job}` — a shallow copy that includes everything the startup hook added. So `ctx["api_client"]` is available in the cron handler.

**`Queue.info()["queued"]` shape:** [VERIFIED: saq/types.py] `QueueInfo` is a TypedDict with `queued: int` (pending count), `active: int`, `scheduled: int`, `name: str`, `workers: dict`, `jobs: list`. Use `info["queued"]` for heartbeat queue_depth.

**SAQ CronJob signature [VERIFIED at saq 0.26.3]:**
```python
@dataclasses.dataclass
class CronJob(t.Generic[CtxType]):
    function: Function[CtxType]
    cron: str
    unique: bool = True
    timeout: int | None = None
    heartbeat: int | None = None
    retries: int | None = None
    ttl: int | None = None
    kwargs: dict[str, t.Any] | None = None
```

### Pattern 6: HTMX self-replacing poll partial (UI-SPEC §Interaction Contract)

[CITED: UI-SPEC §Self-Contained Partial Pattern]

```python
# src/phaze/routers/admin_agents.py — NEW
"""Operator-facing /admin/agents page + HTMX poll partial (D-11..D-14).

Mirrors the smoke-app testability pattern from pipeline_scans.py:
- Templates wired via Jinja2Templates(TEMPLATES_DIR)
- HX-Request header → return partial only
- Sort key: (revoked_at IS NOT NULL, status_rank, -last_seen_at)
"""
from datetime import UTC, datetime
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

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/admin/agents", tags=["admin"])


async def _load_agents(session: AsyncSession) -> list[Agent]:
    result = await session.execute(select(Agent))
    rows = list(result.scalars().all())
    now = datetime.now(UTC)
    for a in rows:
        # transient attrs (mirrors Phase 27 _agent_name pattern)
        a._status = classify(a, now)                   # noqa: SLF001
    rows.sort(key=lambda a: sort_key(a, now))
    return rows


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


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


@router.get("/_table", response_class=HTMLResponse)
async def table_partial(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    """Dedicated partial endpoint — unconditionally returns the partial only."""
    agents = await _load_agents(session)
    return templates.TemplateResponse(request=request, name="admin/partials/agents_table.html", context={
        "request": request,
        "agents": agents,
        "refreshed_at_iso": datetime.now(UTC).isoformat(),
    })
```

```python
# src/phaze/services/agent_liveness.py — NEW
"""Agent liveness classification (UI-SPEC; reads phaze.constants.AGENT_LIVENESS_*)."""
from datetime import datetime
from typing import Literal

from phaze.constants import AGENT_LIVENESS_ALIVE_SECONDS, AGENT_LIVENESS_STALE_SECONDS
from phaze.models.agent import Agent

AgentStatus = Literal["alive", "stale", "dead", "revoked", "never"]
_STATUS_RANK = {"alive": 0, "stale": 1, "dead": 2, "revoked": 3, "never": 3}


def classify(agent: Agent, now: datetime) -> AgentStatus:
    if agent.revoked_at is not None:
        return "revoked"
    if agent.last_seen_at is None:
        return "never"
    delta = (now - agent.last_seen_at).total_seconds()
    if delta < AGENT_LIVENESS_ALIVE_SECONDS:
        return "alive"
    if delta < AGENT_LIVENESS_STALE_SECONDS:
        return "stale"
    return "dead"


def sort_key(agent: Agent, now: datetime) -> tuple[int, int, float]:
    """(revoked-bool-as-int, status_rank, -last_seen_unix_or_min)."""
    status = classify(agent, now)
    last_seen = agent.last_seen_at.timestamp() if agent.last_seen_at else float("-inf")
    return (1 if agent.revoked_at is not None else 0, _STATUS_RANK[status], -last_seen)
```

```python
# src/phaze/utils/humanize.py — NEW (UI-SPEC §Relative-Time Helper)
"""Relative-time formatter: '23s ago', '4m ago', '2h ago', '3d ago'."""
from datetime import UTC, datetime


def relative_time(dt: datetime | None, *, now: datetime | None = None) -> str:
    if dt is None:
        return "never"
    now = now or datetime.now(UTC)
    delta = (now - dt).total_seconds()
    if delta < 0:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"
```

```python
# src/phaze/constants.py — APPEND
AGENT_LIVENESS_ALIVE_SECONDS: int = 90
"""Seconds since last_seen_at below which agent is 'alive'."""

AGENT_LIVENESS_STALE_SECONDS: int = 300
"""Seconds since last_seen_at below which agent is 'stale'; ≥ this = 'dead'."""
```

**HTMX 2.0.7 `hx-trigger="every 5s"`:** [CITED: htmx.org/attributes/hx-trigger] — supported in HTMX 2.x with the documented `every <interval>` syntax. The pattern is already proven in `templates/pipeline/dashboard.html:17` (5s) and `templates/pipeline/partials/scan_progress_card.html:13` (2s). No version concerns.

**`HX-Request` header detection:** Established pattern; mirrored from `services/search.py` / STATE.md decision (line 61: "Search UI: HTMX partial detection via truthy HX-Request header check"). Idiomatic FastAPI: `request.headers.get("hx-request") == "true"`.

### Pattern 7: docker-compose.agent.yml structure (D-15)

```yaml
# docker-compose.agent.yml — standalone file (file-server host only)
services:
  worker:
    image: ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}
    command: uv run saq phaze.tasks.agent_worker.settings
    env_file: .env
    environment:
      - PHAZE_ROLE=agent
    volumes:
      - "${SCAN_PATH:?SCAN_PATH required}:/data/music:ro"
      - "${MODELS_PATH:-./models}:/models:rw"   # rw for D-21 auto-download
      - "${CA_PATH:-./certs}:/certs:ro"
    restart: unless-stopped
  watcher:
    image: ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}
    command: uv run python -m phaze.agent_watcher
    env_file: .env
    environment:
      - PHAZE_ROLE=agent
    volumes:
      - "${SCAN_PATH:?SCAN_PATH required}:/data/music:ro"
      - "${MODELS_PATH:-./models}:/models:rw"   # watcher also calls ensure_models_present
      - "${CA_PATH:-./certs}:/certs:ro"
    restart: unless-stopped
  audfprint:
    build:
      context: .
      dockerfile: services/audfprint/Dockerfile.audfprint
    volumes:
      - "${SCAN_PATH:?}:/data/music:ro"
      - audfprint_data:/data/fprint
    restart: unless-stopped
  panako:
    build:
      context: .
      dockerfile: services/panako/Dockerfile.panako
    volumes:
      - "${SCAN_PATH:?}:/data/music:ro"
      - panako_data:/data/fprint
    restart: unless-stopped
volumes:
  audfprint_data:
  panako_data:
```

**Standalone-file behavior:** `docker compose -f docker-compose.agent.yml up -d` operates on a wholly separate project name; no service or network from the root `docker-compose.yml` is reachable. The file-server host needs the `services/` subtree (for the fingerprint sidecar build contexts) but does NOT need `src/`, `alembic/`, `pyproject.toml`, etc. — a minimal file-server checkout is `services/`, `.env`, `docker-compose.agent.yml`, `certs/`, `models/` (optional pre-warm).

**GHCR pull:** The existing `.github/workflows/docker-publish.yml` is already set up [VERIFIED: STATE.md quick-task `260410-kco`, justfile `image-push` recipe]. Image URL format is `ghcr.io/simplicityguy/phaze:<tag>`. The workflow needs to tag `:latest` and `:v<version>` on tagged releases — confirm during planning by reading `.github/workflows/docker-publish.yml`.

**File-server build context for sidecars:** The audfprint+panako sidecars use `build: context: .` — that `.` is relative to the directory containing `docker-compose.agent.yml` on the file-server host. So the file-server host needs at minimum `services/audfprint/Dockerfile.audfprint` and `services/panako/Dockerfile.panako` (and whatever those Dockerfiles `COPY` from `services/audfprint/` and `services/panako/`). Document in `docs/deployment.md`.

**Compose env-var fail-fast:** `${SCAN_PATH:?SCAN_PATH required}` causes `docker compose up` to error out at parse time if `SCAN_PATH` is unset. Same syntax already used by Phase 27 (`docker-compose.yml`). Confirmed in `.env.example`.

### Pattern 8: Models auto-download on agent startup (D-21)

```python
# src/phaze/tasks/_shared/model_bootstrap.py — NEW
"""Auto-download essentia weights when /models is empty (D-21).

IMPORT-BOUNDARY (extends Phase 26 D-25 + Phase 27 D-22):
    Postgres-free. Imports: httpx + pathlib + hashlib only.
"""
from __future__ import annotations

import logging
from pathlib import Path

from phaze.scripts.download_models import download_to

logger = logging.getLogger(__name__)


def ensure_models_present(models_dir: Path) -> None:
    """Skip if any .pb files exist; else download. Raises RuntimeError on failure."""
    pb_files = list(models_dir.glob("*.pb"))
    if pb_files:
        logger.info("Models present (%d weight files at %s)", len(pb_files), models_dir)
        return
    logger.info(
        "%s is empty; downloading essentia weights (~150MB, takes 2-5min on first start)...",
        models_dir,
    )
    try:
        download_to(models_dir)
    except Exception as exc:
        msg = f"Model download failed: {exc}"
        raise RuntimeError(msg) from exc
    logger.info("Models downloaded successfully to %s", models_dir)
```

```python
# src/phaze/scripts/download_models.py — NEW (extract from scripts/download-models.sh)
"""Python helper that fetches the essentia weight files (D-21).

The same URL list + SHA manifest the existing bash script uses, exposed as a
Python function so both bash and the agent bootstrap can drive the download.

Idempotent: skips files that already exist; verifies SHA-256 if provided.
"""
from pathlib import Path

import httpx

_CLASSIFIER_BASE = "https://essentia.upf.edu/models/classifiers"
_GENRE_BASE = "https://essentia.upf.edu/models/music-style-classification/discogs-effnet"

# 33 classifier model paths (extracted from scripts/download-models.sh:22-50)
CLASSIFIER_MODELS: tuple[str, ...] = (
    "mood_acoustic/mood_acoustic-musicnn-msd-2",
    ...,  # full 33 paths from the existing bash script
)
GENRE_MODELS: tuple[str, ...] = ("discogs-effnet-bs64-1",)


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
    tmp.rename(dest)   # atomic on POSIX


def download_to(target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    for model_path in CLASSIFIER_MODELS:
        filename = model_path.rsplit("/", 1)[-1]
        _download_one(f"{_CLASSIFIER_BASE}/{model_path}.pb", target_dir / f"{filename}.pb")
        _download_one(f"{_CLASSIFIER_BASE}/{model_path}.json", target_dir / f"{filename}.json")
    for model in GENRE_MODELS:
        _download_one(f"{_GENRE_BASE}/{model}.pb", target_dir / f"{model}.pb")
        _download_one(f"{_GENRE_BASE}/{model}.json", target_dir / f"{model}.json")
```

**Where to invoke (D-discretion confirms):**
- `agent_worker.startup` calls `ensure_models_present(Path(cfg.models_path))` AFTER `whoami_with_retry()` succeeds and BEFORE the SAQ worker starts pulling jobs. This way, an auth failure fails fast (~60s budget) instead of after spending 5 minutes on a download.
- `agent_watcher.main` calls it on startup too, AFTER `whoami_with_retry`. The watcher doesn't strictly need the weights but ensuring presence at watcher start surfaces failure earlier than waiting for the first analysis job.

**Failure mode:** `RuntimeError` propagates → container exits non-zero → `restart: unless-stopped` triggers retry. [CITED: docs.docker.com/engine/reference/commandline/run/#restart-policies] — `unless-stopped` restarts on any non-zero exit indefinitely until manually `docker stop`.

**Replace existing models-present check in agent_worker.startup:** The existing code at `src/phaze/tasks/agent_worker.py:88-97` currently RAISES `RuntimeError("Models directory not found / No .pb model files")`. Phase 29 replaces this check with `ensure_models_present(models_dir)` which logs+downloads on empty.

**Updating `scripts/download-models.sh`:** Convert the bash script to a thin shim that invokes the Python helper:
```bash
#!/usr/bin/env bash
# scripts/download-models.sh
set -euo pipefail
exec uv run python -m phaze.scripts.download_models "${1:-./models}"
```
Keeps the `just download-models` recipe working; the manifest of URLs lives in one place (Python).

### Anti-Patterns to Avoid

- **DO NOT shell out to `openssl` for cert generation.** The Python `cryptography` library is purpose-built for this and produces type-safe, testable output. shell-out would fail on minimal base images that strip `openssl` CLI.
- **DO NOT use `*/30 * * * * *` (leading-seconds) for the heartbeat cron.** croniter 6.x defaults to trailing-seconds. Use `* * * * * */30`. (See §Critical Discoveries §2.)
- **DO NOT write CA private key (`phaze-ca.key`) to a bind mount that gets distributed to agents.** Only the public CA cert (`phaze-ca.crt`) is copied to file servers. The CA private key stays on the app server, mode 0600.
- **DO NOT read Redis hash data from the admin page.** [Phase 28 D-02 invariant]: only the app-server writes to `exec:{batch_id}` Redis hash. The admin page reads from `agents` DB table (`last_seen_at`, `last_status` JSONB, `revoked_at`).
- **DO NOT add `get_authenticated_agent` dep to admin_agents router.** That dep is for *agent*-facing endpoints. `/admin/agents` is operator-facing and follows the same convention as `pipeline.py` / `pipeline_scans.py` (no auth dep — single-user home server). [CITED: CONTEXT.md "constraints to plan around"]
- **DO NOT block uvicorn startup on cert generation when certs already exist.** `ensure_certs_present` MUST be idempotent: existing-and-parseable → return immediately, otherwise generate. The CONTEXT.md banner only prints on actual generation.
- **DO NOT issue heartbeats from the watcher.** [D-07] — only `agent_worker` emits. If the worker dies and only the watcher is up, the admin page shows "stale" which is the correct signal (file execution path is down).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| x509 / CA / leaf cert generation | Manual ASN.1 encoding, subprocess to openssl | `cryptography.x509.CertificateBuilder` | Type-safe, deterministic, testable, abi3 wheels on all targets |
| Cron parsing | Reimplement minute/hour matching | SAQ `CronJob` (uses croniter) | Already pinned; supports 6-field |
| HTMX polling loop | Custom JS setInterval + fetch | `hx-trigger="every 5s"` | Already used 6× in this project |
| YAML parsing for compose invariants | Regex / hand-roll | `yaml.safe_load(open(...))` | PyYAML 6.0.3 already transitive |
| Relative-time formatting | `humanize` library | 12-line `phaze.utils.humanize.relative_time` | UI-SPEC locks the helper; zero new deps |
| Container restart on failure | Custom supervisor | docker `restart: unless-stopped` | Already the project's liveness mechanism |
| Cert idempotency check | mtime check / file size | `x509.load_pem_x509_certificate(...)` then catch ValueError | Verifies parse-ability not just existence |
| SAQ queue depth | Raw Redis LLEN | `Queue.info()["queued"]` | Survives SAQ internal layout changes |

**Key insight:** Phase 29 introduces no new categories of work that aren't already solved by existing libraries. The only "new" code is small glue between proven primitives (cryptography → file → uvicorn flag, croniter → SAQ → POST, agents table → Jinja2 → HTMX swap).

## Runtime State Inventory

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | **None.** Phase 29 doesn't rename or migrate anything. Existing `agents.last_seen_at`, `last_status` JSONB, `revoked_at` columns (Phase 24/25) are used unchanged. Zero new Alembic migrations [CONTEXT.md D-23]. | none |
| Live service config | **Redis configuration.** Existing Redis is unauth + 0.0.0.0; Phase 29 rebuilds it with `requirepass` + LAN-bound port. Any pre-existing data in Redis (SAQ queues, idempotency cache) survives the rebuild only if `pgdata`-equivalent volume persists — Redis volumes are NOT named in current compose, so on `docker compose down -v` everything is lost. Acceptable: SAQ queues are ephemeral, idempotency cache has 1h TTL. | document in `docs/deployment.md` |
| OS-registered state | **None.** No Windows Task Scheduler, launchd, systemd unit names involved. Container restart policies (`restart: unless-stopped`) are compose-managed, not OS-registered. | none |
| Secrets/env vars | NEW env vars introduced: `REDIS_PASSWORD`, `REDIS_BIND_IP`, `PHAZE_API_TLS_SANS`, `PHAZE_AGENT_CA_FILE`, `PHAZE_AGENT_ENV`, `PHAZE_IMAGE_TAG`, `CA_PATH`, `MODELS_PATH` (on agent side). Existing `.env` files must be regenerated on next deploy. `PHAZE_AGENT_API_URL` switches from `http://api:8000` → `https://api:8000`. | update `.env.example` + create `.env.example.agent`; document in deployment.md |
| Build artifacts / installed packages | `uv.lock` will gain a `cryptography` entry + its deps (cffi, pycparser). After `uv sync` post-merge, the lockfile must be committed. No egg-info-style cleanup needed; uv handles this cleanly. | run `uv lock && uv sync` after adding `cryptography` to pyproject; commit `uv.lock` |

**The canonical question:** *After every file in the repo is updated, what runtime systems still have the old string cached, stored, or registered?*

For Phase 29 specifically: the **certificate distribution to file-server hosts** is the only "out-of-band" runtime state. Operators must manually `scp` `phaze-ca.crt` from app-server `./certs/` to each file-server. This is OS-level filesystem state on the file-server hosts that is NOT tracked in any database or git repo. Document explicitly in `docs/deployment.md` Step 2: "Copy ./certs/phaze-ca.crt from the application server to ${CA_PATH}/phaze-ca.crt on each file server."

## Common Pitfalls

### Pitfall 1: Forgetting `cryptography` is not transitive
**What goes wrong:** Plan attempts to write `phaze.cert_bootstrap` without adding `cryptography` to pyproject.toml. `import cryptography` fails at runtime.
**Why it happens:** CONTEXT.md D-discretion incorrectly states "already a transitive dep".
**How to avoid:** Phase 29's first plan MUST `uv add 'cryptography>=46.0.0,<49'`. Verify with `uv pip show cryptography` post-install.
**Warning signs:** `ModuleNotFoundError: No module named 'cryptography'` at api container startup.

### Pitfall 2: Using leading-seconds croniter syntax
**What goes wrong:** Cron `*/30 * * * * *` fires every SECOND (because croniter 6.x default treats seconds as field 6, not field 1). Application server gets DDoS'd by 1Hz heartbeats from every agent.
**Why it happens:** Stack Overflow + cron documentation outside Python often uses leading-seconds (Quartz, Spring); Python's croniter does NOT.
**How to avoid:** Use `"* * * * * */30"` (trailing seconds). Add a regression test that constructs `croniter("* * * * * */30", start_time=0)` and asserts the next 3 values are 30.0, 60.0, 90.0.
**Warning signs:** logs show heartbeat success every second; admin page `last_seen_at` updates suspiciously fast.

### Pitfall 3: Uvicorn binding before certs exist
**What goes wrong:** Container starts uvicorn directly with `--ssl-keyfile /certs/phaze-server.key`; cert files don't exist; uvicorn raises `FileNotFoundError` and container exits; `restart: unless-stopped` loops forever.
**Why it happens:** Two-step bootstrap (generate certs → exec uvicorn) wasn't wired correctly; or operator deleted `./certs/` and restarted api alone.
**How to avoid:** Use an explicit entrypoint shim (`python -m phaze.entrypoint`) that calls `ensure_certs_present` then exec's uvicorn. Idempotent: regenerates if missing.
**Warning signs:** `docker compose logs api` shows `FileNotFoundError: /certs/phaze-server.key` in a restart loop.

### Pitfall 4: CA private key leak via banner / log
**What goes wrong:** Operator pastes the cert_bootstrap banner output into a chat / issue; banner contains the CA private key.
**Why it happens:** Banner is hand-written; planner accidentally includes more than the public cert path.
**How to avoid:** Banner is LITERAL CONSTANT, references only `phaze-ca.crt` (public). Never reads or formats the private key. Test the banner output explicitly: `assert "BEGIN" not in banner`, `assert "PRIVATE" not in banner`.
**Warning signs:** banner string contains `-----BEGIN` or `PRIVATE KEY`.

### Pitfall 5: HX-Request header detection on dual-purpose route
**What goes wrong:** `/admin/agents` GET handler returns the full page even when HTMX swaps it via the same URL; the result is the full HTML (including `<base>`, nav, etc.) being injected into the table's swap target, breaking layout.
**Why it happens:** UI-SPEC has TWO endpoints (`/admin/agents` for page, `/admin/agents/_table` for partial), but the operator might also call `/admin/agents` with `HX-Request: true` via direct browser navigation (e.g., back button replay).
**How to avoid:** UI-SPEC §Interaction Contract says "The `_table` route returns the partial unconditionally — it never serves the full page." Make `/admin/agents` page route HX-aware (returns partial when `HX-Request: true`) so both endpoints behave correctly; the dedicated `/_table` is the canonical polling target.
**Warning signs:** stacked `<nav>` blocks inside the agents table region; page title duplicated.

### Pitfall 6: SAN list missing the docker-network service name
**What goes wrong:** Single-host dev runs agent + api in the same compose network; agent's `PHAZE_AGENT_API_URL=https://api:8000`; cert SAN list is `localhost,127.0.0.1` (default); cert doesn't include `api`; TLS handshake fails with `SSLCertVerificationError: hostname 'api' doesn't match`.
**Why it happens:** Default SAN list assumes loopback. Docker compose service-name DNS is the standard pattern but easily forgotten.
**How to avoid:** Default `PHAZE_API_TLS_SANS=localhost,127.0.0.1,api`. Document in `.env.example`. Add a test that the default SAN list parses to 3 entries.
**Warning signs:** dev agent fails to talk to dev api with hostname mismatch error.

### Pitfall 7: Compose env-var fail-fast vs. dev convenience
**What goes wrong:** `${REDIS_PASSWORD:?required}` blocks `docker compose up` in dev when developer forgets to set the password.
**Why it happens:** D-05 enforces fail-fast; dev convenience says "any password works".
**How to avoid:** `.env.example` ships with `REDIS_PASSWORD=dev-redis-password` uncommented (or `REDIS_PASSWORD=changeme`) so a fresh `cp .env.example .env && docker compose up` works. Production operators set their real password via `.env` (or env injection from secrets store).
**Warning signs:** new contributor runs `docker compose up` and sees a parse error.

### Pitfall 8: SAQ cron handler reading wrong `ctx` shape
**What goes wrong:** `heartbeat_tick(ctx)` tries `ctx["queue"]` (set in startup) and gets None because SAQ injects `{**self.context, "job": job}` and the `queue` key isn't part of `self.context` — only what startup put there is.
**Why it happens:** Confusion between SAQ's `Worker.queue` attribute and a `ctx["queue"]` key.
**How to avoid:** Access the queue via `ctx["worker"].queue` (SAQ pre-populates `ctx["worker"]` in `Worker.__init__: self.context = {"worker": self}`). Use `Queue.info()` not raw redis access.
**Warning signs:** `KeyError: 'queue'` in heartbeat handler logs.

### Pitfall 9: agent_worker.py converted to a package unnecessarily
**What goes wrong:** Planner reads CONTEXT.md "register a cron entry in `phaze.tasks.agent_worker.settings.cron_jobs`" and converts the single .py file into a package. Every existing import (`from phaze.tasks.agent_worker import ...`), every test file (`tests/test_task_split.py:54`), and the SAQ CLI invocation breaks until updated everywhere.
**Why it happens:** Dotted name `phaze.tasks.agent_worker.settings` resembles a package path; CONTEXT.md uses ambiguous prose.
**How to avoid:** Leave `agent_worker.py` as-is. Add `cron_jobs=[...]` to the existing `settings = {...}` dict at line 179. Put `heartbeat_tick` in a NEW sibling file `phaze/tasks/heartbeat.py` (not in agent_worker.py itself, to keep that module thin).
**Warning signs:** test_task_split.py fails with `import phaze.tasks.agent_worker as <module> # but agent_worker is a directory`.

### Pitfall 10: Forgetting to add `verify=` to the test smoke client
**What goes wrong:** Existing tests like `test_agent_client.py` construct `PhazeAgentClient(base_url=..., token=...)` with no `verify=` kwarg. After Phase 29 adds the `verify` parameter (with default `True`), these tests continue to pass because `verify=True` falls back to system certs which respx intercepts at the transport layer (no real TLS occurs in respx tests).
**Why it happens:** respx mocks at the httpx transport level, so cert validation is bypassed in those tests.
**How to avoid:** Keep `verify=True` as the parameter default — preserves backwards compat with all respx-based tests in `tests/test_services/test_agent_client*.py`. The new D-04 test (`test_agent_client_tls.py`) exercises real TLS, not respx.
**Warning signs:** No warning signs — this is a non-issue if you preserve the default. Test failures would only happen if you made `verify` a required kwarg.

## Code Examples

Verified patterns from official sources. All code shown above in §Architecture Patterns is implementable as-shown; the planner can drop these snippets into PLAN.md task `<action>` blocks.

### CI YAML-Parse Test for Filesystem Isolation (D-19)

```python
# tests/test_deployment/test_api_filesystem_isolation.py — NEW
"""D-19: app-server compose declares NO music/model/output mounts on api or worker.

Pure YAML-parse structural assertion. No Docker required; ~50ms.
"""
from pathlib import Path

import yaml

BANNED_MOUNT_TARGETS = ("/data/music", "/models", "/data/output")


def test_api_service_has_no_file_mounts() -> None:
    compose_path = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    data = yaml.safe_load(compose_path.read_text())
    api_volumes = data["services"]["api"].get("volumes", []) or []
    for vol_entry in api_volumes:
        # docker-compose volume strings: "host:container[:ro|rw]"
        if isinstance(vol_entry, str):
            target = vol_entry.split(":")[1] if ":" in vol_entry else vol_entry
        else:
            target = vol_entry.get("target", "")
        for banned in BANNED_MOUNT_TARGETS:
            assert banned not in target, f"api service has banned mount: {vol_entry}"


def test_controller_worker_has_no_file_mounts() -> None:
    compose_path = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    data = yaml.safe_load(compose_path.read_text())
    worker_volumes = data["services"]["worker"].get("volumes", []) or []
    for vol_entry in worker_volumes:
        target = vol_entry.split(":")[1] if isinstance(vol_entry, str) and ":" in vol_entry else (vol_entry.get("target", "") if isinstance(vol_entry, dict) else "")
        for banned in BANNED_MOUNT_TARGETS:
            assert banned not in target, f"worker has banned mount: {vol_entry}"


def test_no_watcher_or_agent_worker_in_root_compose() -> None:
    """D-17: watcher + agent-worker services live ONLY in docker-compose.agent.yml."""
    compose_path = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    data = yaml.safe_load(compose_path.read_text())
    assert "watcher" not in data["services"]
    assert "agent-worker" not in data["services"]
```

```python
# tests/test_deployment/test_agent_compose.py — NEW
"""D-15..D-17: docker-compose.agent.yml structural assertions."""
from pathlib import Path

import yaml


def test_agent_compose_service_list() -> None:
    compose_path = Path(__file__).resolve().parents[2] / "docker-compose.agent.yml"
    data = yaml.safe_load(compose_path.read_text())
    assert set(data["services"].keys()) == {"worker", "watcher", "audfprint", "panako"}


def test_agent_compose_has_no_postgres_env() -> None:
    """Agents must never have DATABASE_URL set (D-04 DIST-04 invariant)."""
    compose_path = Path(__file__).resolve().parents[2] / "docker-compose.agent.yml"
    data = yaml.safe_load(compose_path.read_text())
    for svc_name, svc in data["services"].items():
        env = svc.get("environment", [])
        env_strs = env if isinstance(env, list) else [f"{k}={v}" for k, v in env.items()]
        for entry in env_strs:
            assert "DATABASE_URL" not in entry, f"agent service {svc_name} has DATABASE_URL"


def test_worker_service_has_phaze_role_agent() -> None:
    compose_path = Path(__file__).resolve().parents[2] / "docker-compose.agent.yml"
    data = yaml.safe_load(compose_path.read_text())
    worker_env = data["services"]["worker"].get("environment", [])
    env_strs = worker_env if isinstance(worker_env, list) else [f"{k}={v}" for k, v in worker_env.items()]
    assert any("PHAZE_ROLE=agent" in e for e in env_strs)
```

### Justfile Recipes (D-18)

```just
[doc('Start app-server stack (root docker-compose.yml)')]
[group('dev')]
up:
    docker compose up -d

[doc('Start file-server agent stack (standalone docker-compose.agent.yml)')]
[group('dev')]
up-agent:
    docker compose -f docker-compose.agent.yml up -d

[doc('Start both stacks on one host (developer convenience)')]
[group('dev')]
up-all:
    docker compose -f docker-compose.yml -f docker-compose.agent.yml up -d
```

**Note:** The existing `just up` recipe (`docker compose up -d`) is unchanged — it already does the right thing for the app-server stack. The new `up-agent` and `up-all` recipes are additive. `docker compose -f a.yml -f b.yml up` is the canonical multi-file merge syntax [CITED: docs.docker.com/compose/compose-file/13-merge].

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| pyOpenSSL for cert manipulation | `cryptography` library | ~2019 (pyOpenSSL marked deprecated) | Use `cryptography` — modern, type-safe, abi3-stable |
| 5-field cron with separate seconds wrapper | croniter 6-field cron (trailing seconds) | croniter 0.x → 6.x | Use 6-field `* * * * * */30`; SAQ supports it natively |
| Caddy / nginx for cert auto-mgmt | Self-managed cert in app process | (Phase 29 specific decision) | uvicorn-direct TLS; CA bootstrap module |
| Celery for cron jobs | SAQ `CronJob` with croniter | (project pinned SAQ) | Already in stack; no change needed |
| trustme for test certs | Direct `cryptography` cert gen in tests | (Phase 29 specific) | Reuse `cert_bootstrap` in test fixtures; no trustme dep |

**Deprecated/outdated:**
- **pyOpenSSL**: deprecated in favor of `cryptography`. Do not use.
- **`*/30 * * * * *` leading-seconds cron**: NOT how croniter 6.x parses it. Use trailing seconds.
- **`arq`**: previously the project's task queue; replaced by SAQ. Phase 29 doesn't touch tasks anyway.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `cryptography` 46.0.0+ has no breaking API changes through 48.x for `x509.CertificateBuilder`, `ec.SECP256R1`, `x509.SubjectAlternativeName` | Standard Stack | If wrong, pin upper bound tighter. Mitigation: tests exercise the full API surface — would catch breaks at upgrade. |
| A2 | `docker-publish.yml` workflow tags both `:latest` and `:v<version>` for the project image | Pattern 7 | If workflow only tags `:latest`, `PHAZE_IMAGE_TAG=v4.0.0` pin won't pull. Verify by reading `.github/workflows/docker-publish.yml` in first planning task. |
| A3 | uvicorn 0.46.0 doesn't have the `--reload` + `--ssl-keyfile` bug from older versions | Pattern 2 | If broken, dev override must drop TLS for dev mode. Mitigation: dev smoke test with `--reload` + TLS. |
| A4 | The `services/audfprint/Dockerfile.audfprint` build context only requires the `services/` subtree (not `src/`) on the file-server host | Pattern 7 | If Dockerfile COPYs from `src/` or pyproject.toml, file-server host needs more files. Verify in planning task by reading the two service Dockerfiles. |
| A5 | Tests for `agent_liveness.classify` can use `unittest.mock.patch` on `phaze.constants.AGENT_LIVENESS_*` or pass `now=` explicitly — no fancy time-freeze needed | UI-SPEC §Status Pill | If wrong, add `freezegun` test dep. Mitigation: the helpers all accept `now: datetime` as a parameter, making mocking trivial. |

## Open Questions

1. **`docker-publish.yml` tag strategy** — does the workflow tag `:v<version>` on tagged releases, or only `:latest`?
   - What we know: Workflow exists (per STATE.md quick-task `260410-kco`); justfile has `image-push` recipe that pushes `:latest`.
   - What's unclear: tag-on-release behavior.
   - Recommendation: First planning task reads `.github/workflows/docker-publish.yml`; if `:v<version>` tagging is missing, file as a follow-up quick-task to add it. Phase 29 docker-compose.agent.yml ships with `:latest` default which works regardless.

2. **CA distribution mechanism for production** — Phase 29 documents manual `scp`. Is this acceptable or does it need automation?
   - What we know: CONTEXT.md D-03 explicitly chose manual copy. v4.0 scope is private LAN + single operator.
   - What's unclear: nothing — operator-confirmed manual is fine.
   - Recommendation: document the scp/rsync command literally in `docs/deployment.md`. No automation in Phase 29.

3. **Reverse-proxy escape hatch** — Phase 29 commits to uvicorn-direct TLS. Is there a documented path to swap in Caddy later without rewriting cert bootstrap?
   - What we know: CONTEXT.md `<deferred>` lists reverse-proxy as a future swap.
   - What's unclear: how disruptive the swap would be.
   - Recommendation: minor — cert_bootstrap writes to `./certs/` which Caddy can also consume; no architectural lock-in. Document this in `docs/deployment.md` as an "if you ever need to add a proxy" note.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python 3.13 | All | ✓ | 3.13 (project constraint) | — |
| uv | Package mgmt | ✓ | (project constraint) | — |
| Docker / docker compose | Deployment | ✓ assumed (project deploys via compose) | v2.x | — |
| openssl CLI in api image | NOT required (we use `cryptography`) | ✗ in python:3.13-slim | — | Use `cryptography` exclusively |
| `cryptography` library | cert bootstrap | ✗ (NOT installed; NEW dep) | (none) | Must add to pyproject.toml |
| PyYAML | YAML parse tests | ✓ | 6.0.3 (transitive) | — |
| respx | Existing tests | ✓ | 0.23.1 (dev-dep) | — |
| pytest-asyncio | Async tests | ✓ | 1.3.0+ | — |
| httpx | Agent client + new helpers | ✓ | 0.28.1 | — |
| SAQ + croniter | Cron heartbeat | ✓ | 0.26.3 + 6.2.2 | — |
| watchdog | Watcher (unchanged) | ✓ | 4.0+ | — |
| Redis 8 | App-server cache + queue | ✓ (in compose) | redis:8-alpine | — |
| Postgres 18 | App-server only | ✓ (in compose) | postgres:18-alpine | — |
| Network egress for model download | First file-server agent boot | ⚠ depends on file-server LAN | — | Pre-warm via `just download-models`; documented in deployment.md |

**Missing dependencies with no fallback:**
- `cryptography` library — MUST be added in plan-01.

**Missing dependencies with fallback:**
- File-server outbound network for essentia model download — falls back to operator running `just download-models` manually before `just up-agent`. Documented.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.0.3 + pytest-asyncio 1.3.0 (asyncio_mode = "auto" — already configured) |
| Config file | pyproject.toml `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/ -x -q` (existing `just test` recipe) |
| Full suite command | `uv run pytest --cov=phaze --cov-report=term-missing` (existing `just test-cov`) |
| Coverage threshold | 85% (existing `[tool.coverage.report] fail_under = 85`) |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| DIST-01 | app-server compose has no SCAN_PATH/MODELS_PATH/OUTPUT_PATH mounts | structural-parse | `uv run pytest tests/test_deployment/test_api_filesystem_isolation.py -x` | ❌ Wave 0 |
| AUTH-02 | wrong-CA httpx client → ConnectError; correct-CA → success | integration (real TLS) | `uv run pytest tests/test_services/test_agent_client_tls.py -x` | ❌ Wave 0 |
| AUTH-02 | cert_bootstrap generates idempotently | unit | `uv run pytest tests/test_cert_bootstrap.py -x` | ❌ Wave 0 |
| AUTH-02 | cert_bootstrap stays Postgres-free | subprocess import test | `uv run pytest tests/test_task_split.py -x` | ✓ (extend) |
| AUTH-03 | AgentSettings rejects passwordless redis_url when production | unit | `uv run pytest tests/test_config/test_agent_settings_redis_password.py -x` | ❌ Wave 0 |
| AUTH-03 | docker-compose.yml redis service has requirepass + bound port | structural-parse | `uv run pytest tests/test_deployment/test_api_filesystem_isolation.py::test_redis_hardened -x` | ❌ Wave 0 |
| OPS-02 | docker-compose.agent.yml has exactly {worker, watcher, audfprint, panako} | structural-parse | `uv run pytest tests/test_deployment/test_agent_compose.py -x` | ❌ Wave 0 |
| OPS-02 | no agent service has Postgres env var | structural-parse | same file | ❌ Wave 0 |
| OPS-03 | empty /models triggers download; populated /models = no-op; network fail → RuntimeError | unit (mock httpx) | `uv run pytest tests/test_services/test_model_bootstrap.py -x` | ❌ Wave 0 |
| OPS-04 | heartbeat cron registered; fires every 30s; populates worker_pid + queue_depth + agent_version | unit (mock SAQ Queue.info + PhazeAgentClient) | `uv run pytest tests/test_tasks/test_heartbeat_cron.py -x` | ❌ Wave 0 |
| OPS-04 | heartbeat failure → WARNING log; no exception escapes | unit | `uv run pytest tests/test_tasks/test_heartbeat_failure.py -x` | ❌ Wave 0 |
| OPS-04 (UI) | /admin/agents renders with 0/1/many agents | smoke-app integration | `uv run pytest tests/test_routers/test_admin_agents.py -x` | ❌ Wave 0 |
| OPS-04 (UI) | status pill 5 states classified correctly | unit | `uv run pytest tests/test_services/test_agent_liveness.py -x` | ❌ Wave 0 |
| OPS-04 (UI) | HTMX partial returned when HX-Request: true | smoke-app integration | same as test_admin_agents | ❌ Wave 0 |
| OPS-04 (UI) | sort order matches D-14 | unit | covered in test_agent_liveness | ❌ Wave 0 |
| OPS-04 (UI) | relative_time produces correct outputs across 6 ladder cases | unit | `uv run pytest tests/test_utils/test_humanize.py -x` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_<area> -x -q` (target file or directory)
- **Per wave merge:** `uv run pytest -x -q` (full suite, fast mode)
- **Phase gate:** `uv run pytest --cov=phaze --cov-report=term-missing` (full + coverage) — must reach 85% project-wide
- **Pre-commit hooks:** all pre-commit checks must pass (ruff + ruff-format + mypy via local hook + bandit + actionlint)

### Wave 0 Gaps

All Wave 0 — every test file below is new and must be authored as the first action in each plan that lands its associated module:

- [ ] `tests/test_cert_bootstrap.py` — covers AUTH-02 (cert generation + idempotency)
- [ ] `tests/test_services/test_agent_client_tls.py` — covers AUTH-02 wrong-CA → ConnectError (integration with real TLS smoke server)
- [ ] `tests/test_config/test_agent_settings_redis_password.py` — covers AUTH-03 production validator
- [ ] `tests/test_deployment/__init__.py` — new dir
- [ ] `tests/test_deployment/test_api_filesystem_isolation.py` — covers DIST-01 + AUTH-03 (compose structural)
- [ ] `tests/test_deployment/test_agent_compose.py` — covers OPS-02 (compose structural)
- [ ] `tests/test_services/test_model_bootstrap.py` — covers OPS-03 auto-download
- [ ] `tests/test_tasks/test_heartbeat_cron.py` — covers OPS-04 emission
- [ ] `tests/test_tasks/test_heartbeat_failure.py` — covers OPS-04 fire-and-forget posture
- [ ] `tests/test_routers/test_admin_agents.py` — covers OPS-04 UI
- [ ] `tests/test_services/test_agent_liveness.py` — covers OPS-04 classifier + sort
- [ ] `tests/test_utils/test_humanize.py` — covers UI-SPEC helper

**Existing test extension:**
- [ ] `tests/test_task_split.py` — add `test_cert_bootstrap_stays_postgres_free` case (extends Phase 26 D-25 invariant for the new `phaze.cert_bootstrap` module)

**Framework install:** None — pytest, pytest-asyncio, respx, httpx, PyYAML all already in dev-deps.

## Security Domain

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | yes | Bearer token (Phase 25 — unchanged); /admin/agents intentionally has NO auth (single-user home server per CONTEXT.md constraints) |
| V3 Session Management | no | No user sessions in Phase 29 (operator UI is single-user, open) |
| V4 Access Control | yes | Cross-tenant guard already on agent endpoints (Phase 26); admin page is read-only |
| V5 Input Validation | yes | Pydantic `extra="forbid"` on HeartbeatRequest (Phase 25 — unchanged); `_parse_san_entries` parses operator-supplied SAN env safely |
| V6 Cryptography | **YES — primary V6 focus of this phase** | `cryptography` library (NOT hand-rolled). ECDSA P-256, SHA-256 signing, x509 v3 with BasicConstraints + KeyUsage extensions. 10y CA, 2y leaf. NEVER reuse the CA private key for anything else. |
| V8 Data Protection | yes | CA private key stored at 0600; never logged; never in banner. SAN env var sanitized via `_parse_san_entries` (regex-free DNSName / IPAddress dispatch). |
| V9 Communication | **YES** | All agent → app-server traffic over TLS (D-01..D-04). agents reject untrusted certs via `httpx.AsyncClient(verify=<ca_file>)`. Redis password-auth + LAN bind. |
| V11 Configuration | yes | `${REDIS_PASSWORD:?required}` fail-fast at compose parse time; AgentSettings model_validator refuses passwordless Redis in production. |
| V14 SBoM / Components | yes | One new runtime dep (`cryptography`), explicitly version-pinned. License-compatible (Apache-2.0/BSD-3-Clause). |

### Known Threat Patterns for Phase 29 Stack

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Passive eavesdropping on agent → app-server traffic | Information Disclosure | TLS termination at uvicorn; cert pinned via `PHAZE_AGENT_CA_FILE` |
| Active MITM (rogue agent / wrong cert) | Spoofing | `httpx.AsyncClient(verify=<ca_file>)` rejects any cert not signed by the operator-distributed CA; httpx.ConnectError fail-fast |
| CA private key exfiltration | Information Disclosure | CA private key written 0600, root:root, never logged, never in banner; CA private key bind-mounted only on app-server `./certs/` |
| Stolen leaf cert (re-use by attacker) | Spoofing | 2y leaf rotation (operator regenerates via `rm -rf ./certs && docker compose restart api`); future Phase: shorter leaf rotation + automation |
| Redis password leak via process listing | Information Disclosure | `--no-auth-warning` on redis-cli healthcheck suppresses stderr; `command:` in compose passes via env var not visible in `ps`. Compose-level `${VAR}` interpolation expands at parse time so the password appears in the redis CMD line — accept as residual risk on a private LAN |
| Redis exposed to public internet | Information Disclosure / Tampering | `${REDIS_BIND_IP:-127.0.0.1}:6379:6379` binds to loopback only by default; production sets LAN IP. AUTH-03 verified by AgentSettings validator. |
| Heartbeat replay / spoofing | Spoofing | Bearer-token auth on `/api/internal/agent/heartbeat` (Phase 25 unchanged); HeartbeatRequest schema `extra="forbid"` |
| /admin/agents data leak (XSS, etc.) | Information Disclosure | Jinja2 autoescape ON by default (no `\|safe` filter usage in Phase 29 templates); operator-controlled values are agent.name + agent.id which pass through CHECK constraint (`^[a-z0-9]+(-[a-z0-9]+)*$`) |
| Path traversal in PHAZE_API_TLS_SANS | Tampering | `_parse_san_entries` only accepts IP addresses (via `ipaddress.ip_address`) or DNS names (string passthrough); no file I/O on env values |
| Model download MITM (essentia.upf.edu over HTTP) | Tampering | URLs are HTTPS (verified in `scripts/download-models.sh`); add SHA-256 manifest verification in `download_to` as a future hardening (P29 ships without per-file SHA verify — accept; cert is self-signed CA-signed in transit) |
| CA regeneration breaks all agents silently | DoS / availability | Loud multi-line banner on stdout AND logger.warning; documented "rm -rf ./certs/ is destructive" in deployment.md |
| Operator forgets to copy CA → agent fails to connect | Operational | `httpx.ConnectError` surfaces in agent logs immediately at startup (whoami probe); explicit RuntimeError in `construct_agent_client` if CA file is empty/missing |

## Sources

### Primary (HIGH confidence) — verified against source / installed binaries
- **Local inspection of installed packages** (via `uv run python -c "..."` + `uv pip show`):
  - SAQ 0.26.3 — `CronJob` dataclass signature; `Worker.schedule` source; `Worker.timers` defaults; `QueueInfo["queued"]` TypedDict field
  - croniter 6.2.2 — 6-field trailing-seconds semantics empirically tested; `second_at_beginning` flag exists but is NOT used by SAQ
  - httpx 0.28.1 — `AsyncClient.verify: ssl.SSLContext | str | bool` signature confirmed; `ConnectError → TransportError` hierarchy
  - PyYAML 6.0.3 — transitive dep verified
  - cryptography — confirmed NOT installed (via `uv pip list`)
- **CONTEXT.md** D-01..D-23 (LOCKED user decisions) — `.planning/phases/29-deployment-hardening-agents-admin/29-CONTEXT.md`
- **UI-SPEC.md** — `.planning/phases/29-deployment-hardening-agents-admin/29-UI-SPEC.md`
- **Existing code** — read in full:
  - `src/phaze/config.py` (AgentSettings shape for new fields)
  - `src/phaze/services/agent_client.py` (verify= parameter wiring point)
  - `src/phaze/tasks/agent_worker.py` (single-file module, settings dict)
  - `src/phaze/tasks/_shared/agent_bootstrap.py` (Pitfall 7 short-circuit pattern)
  - `src/phaze/services/agent_task_router.py` (Queue.from_url + per-agent caching)
  - `src/phaze/routers/pipeline_scans.py` (HTMX poll pattern reference)
  - `src/phaze/templates/base.html` (nav link template)
  - `src/phaze/templates/pipeline/dashboard.html` + partials (HTMX `every 5s` cadence)
  - `docker-compose.yml` (current root file)
  - `pyproject.toml` (deps + ruff/mypy config)
  - `justfile` (existing recipes)
  - `scripts/download-models.sh` (URL manifest to extract)

### Secondary (MEDIUM confidence) — official documentation
- [cryptography x509 tutorial](https://cryptography.io/en/latest/x509/tutorial/) — CertificateBuilder + SECP256R1 + SAN
- [cryptography on PyPI](https://pypi.org/project/cryptography/) — version 48.0.0 stable, abi3 wheels for all targets, Apache-2.0/BSD-3-Clause
- [croniter on PyPI](https://pypi.org/project/croniter/) — 6.2.2 verified
- [croniter on GitHub (pallets-eco/croniter)](https://github.com/pallets-eco/croniter) — 6-field trailing-seconds confirmed in README
- [httpx SSL advanced](https://www.python-httpx.org/advanced/ssl/) — verify=str path, ConnectError wrapping
- [Python ssl module](https://docs.python.org/3.13/library/ssl.html) — SSLCertVerificationError hierarchy
- [Docker compose ports docs](https://docs.docker.com/compose/compose-file/05-services/#ports) — IP-prefix binding confirmed
- [SAQ documentation](https://saq-py.readthedocs.io/en/latest/) — CronJob + Tasks page
- [HTMX 2.x hx-trigger](https://htmx.org/attributes/hx-trigger/) — `every 5s` syntax

### Tertiary (LOW confidence) — secondary references; cross-verified above
- redis docker compose patterns (multiple blog posts) — confirmed via local empirical knowledge of redis-cli `--no-auth-warning` flag
- [github.com/Kludex/uvicorn/issues/352](https://github.com/Kludex/uvicorn/issues/352) — historical `--reload` + TLS bug, now resolved in 0.46.0+

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — every package empirically verified against `uv pip show` / `uv run python -c "import X; print(X.__version__)"`
- Architecture: HIGH — patterns are direct extensions of Phase 24-28 conventions already in source
- Pitfalls: HIGH — Pitfalls 1+2 are empirically demonstrated, not speculation
- Security domain: MEDIUM — V6 controls are standard for self-signed internal CA; threat model is well-understood for private LAN scope
- TLS test approach (D-04): MEDIUM — two strategies proposed; planner picks based on infra ergonomics

**Research date:** 2026-05-16
**Valid until:** 2026-06-15 (30 days) — `cryptography`, SAQ, croniter, httpx, redis versions are stable enough for 30-day validity. If `cryptography` 49 ships before then, recheck the upper-bound pin.

---

*Phase: 29-deployment-hardening-agents-admin*
*Research gathered: 2026-05-16*
