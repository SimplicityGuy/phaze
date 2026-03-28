# Phaze - Music alignment tool
# Run `just` to see all available commands

# === Dev ===

# Install all dependencies
install:
    uv sync

# Start all services in Docker
up:
    docker compose up -d

# Stop all services
down:
    docker compose down

# View logs for all services (follow mode)
logs:
    docker compose logs -f

# Rebuild and restart services
rebuild:
    docker compose up -d --build

# === Test ===

# Run all tests
test:
    uv run pytest tests/ -x -q

# Run tests with coverage report
test-cov:
    uv run pytest --cov=phaze --cov-report=term-missing

# Run tests with coverage XML output (for CI)
test-ci:
    uv run pytest --cov=phaze --cov-report=xml --cov-report=term-missing

# Run a specific test file
test-file FILE:
    uv run pytest {{FILE}} -x -v

# === Lint/Format ===

# Run ruff linter
lint:
    uv run ruff check .

# Run ruff linter with auto-fix
lint-fix:
    uv run ruff check . --fix

# Format code with ruff
fmt:
    uv run ruff format .

# Run mypy type checker
typecheck:
    uv run mypy .

# Run all pre-commit hooks
pre-commit:
    pre-commit run --all-files

# Run all quality checks (lint + typecheck + test)
check: lint typecheck test

# === Security ===

# Run pip-audit for dependency vulnerability scanning
pip-audit:
    uvx pip-audit -r <(uv pip freeze)

# Run bandit for Python SAST
security:
    uv run bandit -r src/ -x tests -s B608

# Run all security checks
security-all: pip-audit security

# === Docker ===

# Build Docker image
docker-build:
    docker compose build

# Shell into the API container
docker-shell:
    docker compose exec api bash

# View running containers
docker-ps:
    docker compose ps

# === Database/Migrations ===

# Run Alembic migrations
db-upgrade:
    uv run alembic upgrade head

# Create a new Alembic migration
db-revision MESSAGE:
    uv run alembic revision --autogenerate -m "{{MESSAGE}}"

# Show current migration status
db-current:
    uv run alembic current

# Downgrade one migration
db-downgrade:
    uv run alembic downgrade -1

# Show migration history
db-history:
    uv run alembic history

# === Maintenance ===

# Update pre-commit hooks (with frozen SHAs)
update-hooks:
    pre-commit autoupdate --freeze

# Lock and upgrade all dependencies
lock-upgrade:
    uv lock --upgrade

# Sync after lock upgrade
sync:
    uv sync
