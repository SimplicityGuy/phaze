# Phase 1: Infrastructure & Project Setup - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-03-28 (re-discussed)
**Phase:** 01-infrastructure-project-setup
**Areas discussed:** Docker architecture, Database schema strategy, CI/CD pipeline, Project conventions

---

## Docker Architecture

| Option | Description | Selected |
|--------|-------------|----------|
| Keep as-is | 4-service layout with health checks and dev overrides | ✓ |
| Add reverse proxy | Add nginx/traefik as 5th service for SSL/routing | |
| Simplify | Merge api+worker into one process | |

**User's choice:** Keep as-is
**Notes:** Working well for single-user home server tool.

| Option | Description | Selected |
|--------|-------------|----------|
| Named volumes for DB | PostgreSQL already uses named volume, back up separately | ✓ |
| Add backup service | Add pg_dump cron container | |
| You decide | Claude decides persistence approach | |

**User's choice:** Named volumes for DB
**Notes:** No automated backup needed for now.

---

## Database Schema Strategy

| Option | Description | Selected |
|--------|-------------|----------|
| Keep approach | Big initial schema + incremental per phase | ✓ |
| One migration per phase | Stricter separation, more migrations | |
| Single evolving migration | Squash periodically, simpler history | |

**User's choice:** Keep approach
**Notes:** 7 tables across 3 migrations working well.

| Option | Description | Selected |
|--------|-------------|----------|
| JSONB for extras is fine | Structured columns for known fields, JSONB for variable data | ✓ |
| More structured columns | Extract common JSONB fields into dedicated columns | |
| You decide | Claude decides based on query patterns | |

**User's choice:** JSONB for extras is fine
**Notes:** Standard PostgreSQL pattern confirmed.

---

## CI/CD Pipeline

| Option | Description | Selected |
|--------|-------------|----------|
| Keep structure | 3 reusable workflows, fix issues as they arise | ✓ |
| Add deployment step | Build/push Docker images or deploy to home server | |
| Stricter gates | Block merges on any check failure | |
| You decide | Claude manages CI evolution | |

**User's choice:** Keep structure

| Option | Description | Selected |
|--------|-------------|----------|
| Tests depend on quality | No point running tests if lint fails, saves CI minutes | ✓ |
| Run independently | Run tests even if quality fails | |
| You decide | Claude picks dependency structure | |

**User's choice:** Tests depend on quality

---

## Project Conventions

| Option | Description | Selected |
|--------|-------------|----------|
| Keep conventions | Package structure clean and well-organized | ✓ |
| Add strict layering rules | Enforce routers→services→models only | |
| Restructure by feature | Organize by feature instead of by layer | |

**User's choice:** Keep conventions

| Option | Description | Selected |
|--------|-------------|----------|
| Current grouping is fine | Groups by category, add commands to appropriate groups | ✓ |
| Group by phase/feature | Organize by feature area | |
| You decide | Claude organizes justfile commands | |

**User's choice:** Current grouping is fine

---

## Claude's Discretion

- Alembic configuration details
- Docker base image selection
- uvicorn configuration
- Redis configuration defaults

## Deferred Ideas

None — discussion stayed within phase scope.
