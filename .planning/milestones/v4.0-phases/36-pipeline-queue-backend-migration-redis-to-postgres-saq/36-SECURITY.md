---
phase: 36
slug: pipeline-queue-backend-migration-redis-to-postgres-saq
status: verified
threats_open: 0
asvs_level: 2
created: 2026-06-13
---

# Phase 36 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.
> Register authored at plan time (all 4 PLANs carried `<threat_model>` blocks); auditor ran in verify-mitigations mode.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| operator config → process env | `PHAZE_QUEUE_URL` is operator-supplied and carries DB credentials | DB DSN (secret) |
| package registry → build | swapping the SAQ extra resolves new transitive packages (psycopg, psycopg-pool) | dependency artifacts |
| cache plane ↔ broker plane | Redis (cache/rate-limit/counters) stays isolated from the now-Postgres broker | queue/cache handles |
| API/control process → Postgres | each PostgresQueue opens its own psycopg3 pool — fan-out vs `max_connections` | DB connections |
| file-server agents → control-host Postgres | new network edge (Postgres:5432) created by the broker move | broker traffic + creds |
| agent module import → dependency graph | import-boundary test enforces agents never pull the SQLAlchemy async engine | code boundary |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-36-02 | Information Disclosure | `queue_url` DSN (config.py) | mitigate | `queue_url` ∈ `SECRET_FILE_FIELDS` (config.py:79, `_FILE`/SOPS like `database_url`); full DSN never logged (controller.py:63 / agent_worker.py:92-97 log backend+name only); docs/.env.example present `_FILE` convention | closed |
| T-36-05 | Tampering (input validation) | `PHAZE_QUEUE_URL` value | mitigate | `_strip_sqlalchemy_driver` before-validator normalizes `+asyncpg`/`+psycopg` → libpq DSN at load (config.py:170-185) | closed |
| T-36-01 | Denial of Service (silent failure) | cache readers borrowing `queue.redis` | mitigate | proposal.py:68 reads `ctx["redis"]`; counter hooks read `getattr(job.queue, "cache_redis", None)` (deterministic_key.py:110,131); factory attaches `cache_redis` (queue_factory.py:70); grep confirms no live `queue.redis` reads | closed |
| T-36-04 | Denial of Service (connection exhaustion) | per-queue psycopg3 pools | mitigate | Conservative sizing — controller 2/8 (main.py:103, controller.py:158), agent 1/4 (agent_worker.py:204), per-agent 1/4 (agent_task_router.py:94-99) | closed |
| T-36-06 | Tampering (regression) | default-queue producer guard | mitigate | Factory uses named `PostgresQueue.from_url(url, name=name, ...)` (queue_factory.py:63); `tests/test_no_default_queue_producers.py` green | closed |
| T-36-07 | Tampering (regression escape) | `/saq` monitor over PostgresQueue | mitigate | `tests/test_web/test_saq_mount.py:93,106,120,123` asserts render over `PostgresQueue.info()` | closed |
| T-36-08 | Elevation of Privilege (boundary erosion) | agent_worker import graph | mitigate | `tests/test_task_split.py:68,79-80` forbids `sqlalchemy.ext.asyncio`, requires `saq.queue.postgres` | closed |
| T-36-03 | Elevation / Spoofing | agent→Postgres:5432 network edge | mitigate | 36-HOMELAB-CHANGE-PROMPT.md:99-120 firewall agents→Postgres + least-priv own-schema-CREATE-only role; mirrored in docs/deployment.md:76-77,204,318-319 | closed |
| T-36-10 | Tampering (first-boot DDL under shared schema) | `saq_jobs` auto-create | mitigate | 36-HOMELAB-CHANGE-PROMPT.md:89-104 advisory-lock-guarded idempotent `init_db()` `CREATE TABLE IF NOT EXISTS` + own-schema CREATE-only role | closed |
| T-36-SC | Tampering (supply chain) | `saq[postgres]` / psycopg install | accept | 36-RESEARCH.md:69-73 Package Legitimacy Audit marks all packages `[VERIFIED]`; pinned in uv.lock; pyproject.toml:36,38 | closed |
| T-36-09 | Denial of Service (offline test break) | `tests/integration/` marker gating | accept | Auto-marker excludes `tests/integration/` from `pytest -m 'not integration'` (tests/conftest.py:126); real-PG runs gated to `just integration-test` | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-36-1 | T-36-SC | All new packages (`saq[postgres]`, `psycopg`, `psycopg-pool`, `redis`) pre-approved in 36-RESEARCH Package Legitimacy Audit and pinned via uv.lock; official SAQ extra, no typosquat/slop. | operator | 2026-06-13 |
| AR-36-2 | T-36-09 | Integration tests are intentionally excluded from offline `pytest -m 'not integration'` to keep CI green without a live broker; real-PG coverage runs via `just integration-test`. | operator | 2026-06-13 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-06-13 | 11 | 11 | 0 | gsd-security-auditor (verify-mitigations mode) |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-06-13
