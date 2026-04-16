# Phaze - Music alignment tool
# Run `just` to see all available commands

# Install all dependencies
[group('dev')]
install:
    uv sync

# Start all services in Docker
[group('dev')]
up:
    docker compose up -d

# Stop all services
[group('dev')]
down:
    docker compose down

# View logs for all services (follow mode)
[group('dev')]
logs:
    docker compose logs -f

# Rebuild and restart services
[group('dev')]
rebuild:
    docker compose up -d --build

# Run all tests
[group('test')]
test:
    uv run pytest tests/ -x -q

# Run tests with coverage report
[group('test')]
test-cov:
    uv run pytest --cov=phaze --cov-report=term-missing

# Run tests with coverage XML output (for CI)
[group('test')]
test-ci:
    uv run pytest --cov=phaze --cov-report=xml --cov-report=term-missing

# Run a specific test file
[group('test')]
test-file FILE:
    uv run pytest {{FILE}} -x -v

# Run ruff linter
[group('lint')]
lint:
    uv run ruff check .

# Run ruff linter with auto-fix
[group('lint')]
lint-fix:
    uv run ruff check . --fix

# Format code with ruff
[group('lint')]
fmt:
    uv run ruff format .

# Run mypy type checker
[group('lint')]
typecheck:
    uv run mypy .

# Run all pre-commit hooks
[group('lint')]
pre-commit:
    uv run pre-commit run --all-files

# Run all quality checks (lint + typecheck + test)
[group('lint')]
check: lint typecheck test

# Trigger a file scan (requires running services)
[group('scan')]
scan:
    curl -s -X POST http://localhost:8000/api/v1/scan | python -m json.tool

# Check scan status by batch ID
[group('scan')]
scan-status BATCH_ID:
    curl -s http://localhost:8000/api/v1/scan/{{BATCH_ID}} | python -m json.tool

# Run pip-audit for dependency vulnerability scanning
[group('security')]
pip-audit:
    #!/usr/bin/env bash
    set -e
    IGNORE_ARGS=""
    if [[ -f .pip-audit-ignores ]]; then
        while IFS= read -r line; do
            vuln_id=$(echo "$line" | sed 's/#.*//' | tr -d '[:space:]')
            [[ -z "$vuln_id" ]] && continue
            IGNORE_ARGS="$IGNORE_ARGS --ignore-vuln $vuln_id"
        done < .pip-audit-ignores
    fi
    # shellcheck disable=SC2086
    uv run pip-audit --desc --skip-editable $IGNORE_ARGS

# Run bandit for Python SAST
[group('security')]
security:
    uv run bandit -r src/ -x tests -s B608

# Run all security checks
[group('security')]
security-all: pip-audit security

# View worker logs (follow mode)
[group('worker')]
worker-logs:
    docker compose logs -f worker

# Restart worker service
[group('worker')]
worker-restart:
    docker compose restart worker

# Check SAQ worker health
[group('worker')]
worker-health:
    docker compose exec worker uv run saq phaze.tasks.worker.settings --check

# Build Docker images
[group('docker')]
docker-build:
    docker compose build

# Validate Dockerfiles with hadolint
[group('docker')]
docker-validate:
    #!/usr/bin/env bash
    set -e
    for df in Dockerfile services/audfprint/Dockerfile.audfprint services/panako/Dockerfile.panako; do
        echo "🔍 Validating ${df}..."
        docker run --rm -i hadolint/hadolint < "${df}"
        echo "✅ ${df} passed"
    done

# Push Docker images to GHCR (requires: gh auth token with packages:write)
[group('docker')]
image-push:
    #!/usr/bin/env bash
    set -e
    REGISTRY="ghcr.io"
    OWNER=$(echo "$(git remote get-url origin)" | sed 's|.*github.com[:/]||;s|/.*||' | tr '[:upper:]' '[:lower:]')
    REPO=$(basename -s .git "$(git remote get-url origin)" | tr '[:upper:]' '[:lower:]')
    TAG="latest"
    declare -A IMAGES=(
        ["api"]="Dockerfile"
        ["audfprint"]="services/audfprint/Dockerfile.audfprint"
        ["panako"]="services/panako/Dockerfile.panako"
    )
    for SERVICE in "${!IMAGES[@]}"; do
        IMAGE="${REGISTRY}/${OWNER}/${REPO}/${SERVICE}:${TAG}"
        echo "🐳 Building and pushing ${IMAGE}..."
        docker build -f "${IMAGES[$SERVICE]}" -t "${IMAGE}" .
        docker push "${IMAGE}"
        echo "✅ ${SERVICE} pushed"
    done

# Validate docker-compose.yml syntax
[group('docker')]
docker-compose-validate:
    docker compose config --quiet && echo "✅ docker-compose.yml is valid"

# Shell into the API container
[group('docker')]
docker-shell:
    docker compose exec api bash

# View running containers
[group('docker')]
docker-ps:
    docker compose ps

# Run Alembic migrations
[group('db')]
db-upgrade:
    uv run alembic upgrade head

# Create a new Alembic migration
[group('db')]
db-revision MESSAGE:
    uv run alembic revision --autogenerate -m "{{MESSAGE}}"

# Show current migration status
[group('db')]
db-current:
    uv run alembic current

# Downgrade one migration
[group('db')]
db-downgrade:
    uv run alembic downgrade -1

# Show migration history
[group('db')]
db-history:
    uv run alembic history

# Download essentia ML models for audio analysis
[group('models')]
download-models:
    bash scripts/download-models.sh models

# Trigger fingerprint processing for all eligible files
[group('fingerprint')]
fingerprint:
    curl -s -X POST http://localhost:8000/api/v1/fingerprint | python -m json.tool

# Check fingerprint progress
[group('fingerprint')]
fingerprint-progress:
    curl -s http://localhost:8000/api/v1/fingerprint/progress | python -m json.tool

# Check audfprint container health
[group('fingerprint')]
audfprint-health:
    docker compose exec worker curl -sf http://audfprint:8001/health | python -m json.tool

# Check panako container health
[group('fingerprint')]
panako-health:
    docker compose exec worker curl -sf http://panako:8002/health | python -m json.tool

# Update pre-commit hooks (with frozen SHAs)
[group('maintenance')]
update-hooks:
    uv run pre-commit autoupdate --freeze

# Lock and upgrade all dependencies
[group('maintenance')]
lock-upgrade:
    uv lock --upgrade

# Sync after lock upgrade
[group('maintenance')]
sync:
    uv sync
