# Phase 1: Infrastructure & Project Setup - Context

**Gathered:** 2026-03-27
**Updated:** 2026-03-28
**Status:** Ready for planning

<domain>
## Phase Boundary

Stand up the foundational development environment: Docker Compose with PostgreSQL, Redis, FastAPI skeleton, Alembic migrations, and the full initial database schema. All subsequent phases build code on top of this foundation.

</domain>

<decisions>
## Implementation Decisions

### Docker Architecture
- **D-01:** 4-service Docker Compose layout: api, worker, postgres, redis. Each with health checks. Dev override file adds volume mounts and hot reload.
- **D-02:** Named volumes for PostgreSQL data persistence. No backup service needed for now — single-user home server.
- **D-03:** Scan path as read-only bind mount for safety.

### Database Schema
- **D-04:** Big-bang initial migration for known tables (files, metadata, analysis, proposals, execution_log), incremental migrations for new tables added per phase. This "big bang + incremental" approach is confirmed working well across 7 tables in 3 migrations.
- **D-05:** PostgreSQL 16+ with JSONB columns for flexible metadata (raw_tags, features, context_used). Structured columns for known fields, JSONB for variable/extensible data.

### CI/CD Pipeline
- **D-06:** 3 reusable workflows via workflow_call: code-quality (pre-commit), tests (pytest + Codecov), security (pip-audit, bandit, Semgrep, TruffleHog).
- **D-07:** Tests depend on quality passing first — no point running tests if code doesn't lint. Security runs independently.

### Project Conventions
- **D-08:** `src/phaze/` layout with layer-based subdirectories: routers/, services/, models/, tasks/, schemas/. Not feature-based.
- **D-09:** pydantic-settings with `.env` file for configuration. `SecretStr` for API keys.
- **D-10:** justfile grouped by category (Dev, Test, Lint/Format, Docker, Database/Migrations, Worker). New commands added to appropriate groups as features grow.

### Claude's Discretion
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
- None at phase start — greenfield project.

### Established Patterns
- CLAUDE.md defines: ruff config (line length 150, double quotes), mypy strict mode, pre-commit hooks with frozen SHAs, 85% test coverage minimum
- pyproject.toml section ordering specified in CLAUDE.md
- essentia module needs mypy override (`ignore_missing_imports = true`) due to CI/local environment differences

### Integration Points
- This phase creates the foundation all other phases connect to
- Database schema must accommodate all v1 requirements (ING, ANL, AIP, APR, EXE categories)

</code_context>

<specifics>
## Specific Ideas

No specific requirements — user confirmed all recommended approaches. Infrastructure decisions deferred to Claude's discretion where noted, indicating trust in conventional Python/Docker patterns.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 01-infrastructure-project-setup*
*Context gathered: 2026-03-27*
*Context updated: 2026-03-28*
