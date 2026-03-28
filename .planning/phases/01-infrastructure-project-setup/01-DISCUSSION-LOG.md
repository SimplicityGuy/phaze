# Phase 1: Infrastructure & Project Setup - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-03-27
**Phase:** 01-infrastructure-project-setup
**Areas discussed:** Dev workflow (user-selected), Project structure, Initial schema, Config & secrets (Claude's discretion)

---

## Gray Area Selection

| Option | Description | Selected |
|--------|-------------|----------|
| Project structure | Python package layout (src/, flat, etc.) | |
| Initial schema | What tables in the first migration? | |
| Config & secrets | Env vars, .env files, API key management | |
| Dev workflow | Run app locally vs everything in Docker? | |

**User's choice:** "You tell me what clarification you need" — user deferred most decisions to Claude, indicating these are standard infrastructure choices where conventions apply.

---

## Dev Workflow

| Option | Description | Selected |
|--------|-------------|----------|
| Local app + Docker services | Run FastAPI/workers locally, Docker for Postgres/Redis | |
| Everything in Docker | All containers including the app | ✓ |
| You decide | Claude picks | |

**User's choice:** Everything in Docker
**Notes:** Volume mounts + hot reload to maintain fast iteration despite full-Docker setup.

---

## Claude's Discretion

- Project structure: `src/` layout with `src/phaze/` package
- Initial schema: Full schema up front (all tables known from requirements)
- Configuration: pydantic-settings with `.env`, `SecretStr` for API keys
- Docker Compose profiles for dev vs prod

## Deferred Ideas

None.
