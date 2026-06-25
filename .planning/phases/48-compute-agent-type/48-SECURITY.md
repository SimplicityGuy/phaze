---
phase: 48
slug: compute-agent-type
status: verified
threats_open: 0
asvs_level: 1
created: 2026-06-25
---

# Phase 48 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.
> Register authored at plan time (all 3 plans carried `<threat_model>` blocks); audit verified
> each declared mitigation exists in the implementation — no new-threat scan performed.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| operator CLI → `agents` row | `phaze agents add` mints a bearer token and inserts agent identity (kind + scan_roots) into Postgres. | Bearer token (secret), agent kind, scan roots |
| environment (`PHAZE_AGENT_*`) → agent worker process | Deploy config configures the worker; an empty/wrong kind must fail safe at startup. | Agent kind, API URL, bearer token |
| application code → Postgres (`agents` table) | The kind value is written by the CLI and read by the admin page; the DB is the authoritative store of agent identity. | `agents.kind` enum value |
| compute agent worker → control plane | The compute agent reaches ONLY the SAQ Postgres broker + cache Redis + HTTP API; it must NOT reach app ORM tables or the media filesystem. | SAQ jobs, analysis results (HTTP PUT) |
| `agents.kind` column → admin template render | The page renders column data (`kind`) into HTML; must fail safe on unexpected values. | `kind` string → badge label/class |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-48-01-A | Tampering | `agents.kind` DB column | mitigate | `CheckConstraint("kind IN ('fileserver','compute')", name="kind_enum")` → `ck_agents_kind_enum` (agent.py:39-42); migration 024 `create_check_constraint` (024:45); reject test asserts `kind='bogus'` raises (test_024.py:107-113) | closed |
| T-48-02-A | Tampering | `--kind` CLI flag + `AgentSettings.kind` | mitigate | argparse `choices=("fileserver","compute")` (cli/__init__.py:117-123); config `kind: Literal["fileserver","compute"]` (config.py:400-404) — outer two layers of the 3-layer enum defense | closed |
| T-48-02-B | Information Disclosure | minted bearer token at registration | mitigate | token is `print()`-only, never logged — D-13 invariant (cli/__init__.py:175); negative assertion token absent from `caplog.text` (test_agents_add.py:171-172) | closed |
| T-48-02-C | Elevation of Privilege | relaxed scan-roots gate for compute kind | mitigate | `_enforce_required_agent_fields` relaxes ONLY `scan_roots` and ONLY when `kind != "compute"` (config.py:526); `agent_api_url`/`agent_token` stay unconditional for all kinds (config.py:518,520) | closed |
| T-48-03-A | Elevation of Privilege | `phaze.tasks.agent_worker` (module the compute agent runs) | mitigate | subprocess test asserts `phaze.database`/`phaze.tasks.session`/`sqlalchemy.ext.asyncio` absent from `sys.modules` while `saq.queue.postgres` present (test_task_split.py:83-96); docstring names the CLOUDAGENT-02 invariant (:33-49) | closed |
| T-48-03-B | Tampering | `_kind_badge.html` render | mitigate | defensive `{% if compute %}…{% else %}` neutral fallback — out-of-enum never blanks/injects (_kind_badge.html:11-15); only static literals interpolated (Jinja2 autoescaping); single include covers full-page + `/_table` poll paths | closed |
| T-48-SC | Tampering (supply chain) | dependency install | accept | NO new packages added this phase — `git diff main...HEAD` shows `pyproject.toml`/`uv.lock` untouched; all 3 SUMMARYs report `tech-stack.added: []`. No supply-chain surface. | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-48-SC | T-48-SC | Phase installs no third-party packages (only already-pinned SQLAlchemy/Alembic/argparse/pydantic-settings/Jinja2). No new supply-chain surface to mitigate. | Robert Wlodarczyk | 2026-06-25 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-06-25 | 7 | 7 | 0 | gsd-security-auditor (opus) |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-06-25
