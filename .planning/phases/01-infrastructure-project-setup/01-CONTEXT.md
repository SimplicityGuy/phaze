# Phase 1: Infrastructure & Project Setup - Context

**Gathered:** 2026-03-27
**Status:** Ready for planning

<domain>
## Phase Boundary

Stand up the foundational development environment: Docker Compose with PostgreSQL, Redis, FastAPI skeleton, Alembic migrations, and the full initial database schema. All subsequent phases build code on top of this foundation.

</domain>

<decisions>
## Implementation Decisions

### Project Structure
- **D-01:** Use `src/` layout with `src/phaze/` as the main package. Modern Python convention, works well with uv, Docker, and editable installs.
- **D-02:** Separate router/service/worker layers within the package (async monolith pattern per research).

### Database Schema
- **D-03:** Full schema in the initial migration — all tables (files, metadata, analysis, proposals, execution_log, audit) since they're known from requirements. Easier to develop against than incremental migrations.
- **D-04:** PostgreSQL 16+ as specified in research. Use JSONB columns for flexible metadata storage.

### Configuration
- **D-05:** Use pydantic-settings with `.env` file for configuration. `SecretStr` for API keys and sensitive values.
- **D-06:** Docker Compose profiles for dev vs prod differentiation.

### Development Workflow
- **D-07:** Everything runs in Docker — FastAPI, workers, PostgreSQL, Redis all in Docker Compose. Volume mounts for source code + uvicorn `--reload` for hot reload during development.

### Claude's Discretion
- Project file layout within `src/phaze/` (routers, services, models, workers subdirectories)
- Alembic configuration details (async template, naming conventions)
- Docker base image selection (python:3.13-slim or similar)
- uvicorn configuration (port, workers, reload settings)
- Redis configuration defaults

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project Configuration
- `CLAUDE.md` — Development setup, code quality rules, pre-commit hooks, CI patterns
- `.planning/PROJECT.md` — Project vision, constraints, key decisions
- `.planning/REQUIREMENTS.md` — v1 requirements with REQ-IDs

### Research
- `.planning/research/STACK.md` — Technology recommendations with versions (FastAPI, SQLAlchemy, asyncpg, arq, etc.)
- `.planning/research/ARCHITECTURE.md` — System structure, component boundaries, data flows
- `.planning/research/PITFALLS.md` — Domain pitfalls (Docker permissions, Unicode normalization, bulk insert performance)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- None — greenfield project. Only CLAUDE.md, LICENSE, and README.md exist.

### Established Patterns
- CLAUDE.md defines: ruff config (line length 150, double quotes), mypy strict mode, pre-commit hooks with frozen SHAs, 85% test coverage minimum
- pyproject.toml section ordering specified in CLAUDE.md

### Integration Points
- This phase creates the foundation all other phases connect to
- Database schema must accommodate all v1 requirements (ING, ANL, AIP, APR, EXE categories)

</code_context>

<specifics>
## Specific Ideas

No specific requirements — open to standard approaches. User deferred most infrastructure decisions to Claude's discretion, indicating trust in conventional Python/Docker patterns.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 01-infrastructure-project-setup*
*Context gathered: 2026-03-27*
