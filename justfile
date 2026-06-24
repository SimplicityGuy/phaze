# Phaze - Music alignment tool
# Run `just` to see all available commands

# Host port for the ephemeral integration-test Postgres (5433 avoids the dev DB on 5432)
test_db_port := env_var_or_default("PHAZE_TEST_DB_PORT", "5433")
# Fixed container name for the ephemeral integration-test Postgres
test_db_container := "phaze-test-db"
# Host port for the ephemeral integration-test Redis (6380 avoids a dev Redis on 6379)
test_redis_port := env_var_or_default("PHAZE_TEST_REDIS_PORT", "6380")
# Fixed container name for the ephemeral integration-test Redis
test_redis_container := "phaze-test-redis"

[doc('Install all dependencies')]
[group('dev')]
install:
    uv sync

[doc('Start all services in Docker')]
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

[doc('Stop all services')]
[group('dev')]
down:
    docker compose down

[doc('View logs for all services (follow mode)')]
[group('dev')]
logs:
    docker compose logs -f

[doc('Rebuild and restart services')]
[group('dev')]
rebuild:
    docker compose up -d --build

[doc('Run all tests')]
[group('test')]
test:
    uv run pytest tests/ -x -q

[doc('Run tests with coverage report')]
[group('test')]
test-cov:
    uv run pytest --cov=phaze --cov-report=term-missing

[doc('Run tests with coverage XML output (for CI)')]
[group('test')]
test-ci:
    uv run pytest --cov=phaze --cov-report=xml --cov-report=term-missing

[doc('Run a specific test file')]
[group('test')]
test-file FILE:
    uv run pytest {{FILE}} -x -v

[doc('Start ephemeral Postgres + Redis for integration tests (ports PHAZE_TEST_DB_PORT/PHAZE_TEST_REDIS_PORT, defaults 5433/6380)')]
[group('test')]
test-db:
    #!/usr/bin/env bash
    set -euo pipefail
    container="{{test_db_container}}"
    port="{{test_db_port}}"
    redis_container="{{test_redis_container}}"
    redis_port="{{test_redis_port}}"
    if [ "$(docker inspect -f '{{{{.State.Running}}}}' "$container" 2>/dev/null || echo false)" = "true" ]; then
        echo "🐘 ${container} already running on port ${port}"
    else
        docker rm -f "$container" >/dev/null 2>&1 || true
        echo "🐘 Starting ${container} (postgres:18-alpine) on host port ${port}..."
        docker run -d --name "$container" \
            -e POSTGRES_USER=phaze \
            -e POSTGRES_PASSWORD=phaze \
            -e POSTGRES_DB=phaze_test \
            -p "${port}:5432" \
            postgres:18-alpine >/dev/null
    fi
    if [ "$(docker inspect -f '{{{{.State.Running}}}}' "$redis_container" 2>/dev/null || echo false)" = "true" ]; then
        echo "🟥 ${redis_container} already running on port ${redis_port}"
    else
        docker rm -f "$redis_container" >/dev/null 2>&1 || true
        echo "🟥 Starting ${redis_container} (redis:7-alpine) on host port ${redis_port}..."
        docker run -d --name "$redis_container" \
            -p "${redis_port}:6379" \
            redis:7-alpine >/dev/null
    fi
    echo "⏳ Waiting for Postgres to accept connections..."
    for _ in $(seq 1 30); do
        if docker exec "$container" pg_isready -U phaze -d phaze_test >/dev/null 2>&1; then
            db_ready=1
            break
        fi
        sleep 1
    done
    if [ "${db_ready:-0}" != "1" ]; then
        echo "❌ ${container} did not become ready within 30s" >&2
        docker logs "$container" >&2 || true
        exit 1
    fi
    echo "⏳ Waiting for Redis to accept connections..."
    for _ in $(seq 1 30); do
        if docker exec "$redis_container" redis-cli ping >/dev/null 2>&1; then
            redis_ready=1
            break
        fi
        sleep 1
    done
    if [ "${redis_ready:-0}" != "1" ]; then
        echo "❌ ${redis_container} did not become ready within 30s" >&2
        docker logs "$redis_container" >&2 || true
        exit 1
    fi
    docker exec "$container" psql -U phaze -d phaze_test -tc \
        "SELECT 1 FROM pg_database WHERE datname = 'phaze_migrations_test'" \
        | grep -q 1 \
        || docker exec "$container" psql -U phaze -d phaze_test \
            -c "CREATE DATABASE phaze_migrations_test OWNER phaze;" >/dev/null
    echo "✅ ${container} ready on localhost:${port} (phaze_test + phaze_migrations_test)"
    echo "✅ ${redis_container} ready on localhost:${redis_port}"

[doc('Stop and remove the ephemeral integration-test Postgres + Redis')]
[group('test')]
test-db-down:
    #!/usr/bin/env bash
    set -euo pipefail
    docker rm -f "{{test_db_container}}" >/dev/null 2>&1 || true
    docker rm -f "{{test_redis_container}}" >/dev/null 2>&1 || true
    echo "🧹 Removed {{test_db_container}} + {{test_redis_container}}"

[doc('Run the full suite against self-contained ephemeral Postgres + Redis (auto teardown)')]
[group('test')]
integration-test:
    #!/usr/bin/env bash
    set -euo pipefail
    just test-db
    trap 'just test-db-down' EXIT
    export TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:{{test_db_port}}/phaze_test"
    export MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:{{test_db_port}}/phaze_migrations_test"
    export PHAZE_REDIS_URL="redis://localhost:{{test_redis_port}}/0"
    uv run pytest tests/ -q

[doc('Run ruff linter')]
[group('lint')]
lint:
    uv run ruff check .

[doc('Run ruff linter with auto-fix')]
[group('lint')]
lint-fix:
    uv run ruff check . --fix

[doc('Format code with ruff')]
[group('lint')]
fmt:
    uv run ruff format .

[doc('Run mypy type checker')]
[group('lint')]
typecheck:
    uv run mypy .

[doc('Run all pre-commit hooks')]
[group('lint')]
pre-commit:
    uv run pre-commit run --all-files

[doc('Run all quality checks (lint + typecheck + test)')]
[group('lint')]
check: lint typecheck test

[doc('Trigger a file scan (requires running services)')]
[group('scan')]
scan:
    curl -s -X POST http://localhost:8000/api/v1/scan | python -m json.tool

[doc('Check scan status by batch ID')]
[group('scan')]
scan-status BATCH_ID:
    curl -s http://localhost:8000/api/v1/scan/{{BATCH_ID}} | python -m json.tool

[doc('Run pip-audit for dependency vulnerability scanning')]
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

[doc('Run bandit for Python SAST')]
[group('security')]
security:
    uv run bandit -r src/ -x tests -s B608

[doc('Run all security checks')]
[group('security')]
security-all: pip-audit security

[doc('View worker logs (follow mode)')]
[group('worker')]
worker-logs:
    docker compose logs -f worker

[doc('Restart worker service')]
[group('worker')]
worker-restart:
    docker compose restart worker

[doc('Check SAQ worker health')]
[group('worker')]
worker-health:
    docker compose exec worker uv run saq phaze.tasks.worker.settings --check

[doc('Build Docker images')]
[group('docker')]
docker-build:
    docker compose build

[doc('Validate Dockerfiles with hadolint')]
[group('docker')]
docker-validate:
    #!/usr/bin/env bash
    set -e
    for df in Dockerfile services/audfprint/Dockerfile.audfprint services/panako/Dockerfile.panako; do
        echo "🔍 Validating ${df}..."
        docker run --rm -i hadolint/hadolint < "${df}"
        echo "✅ ${df} passed"
    done

[doc('Push Docker images to GHCR (requires: gh auth token with packages:write)')]
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

[doc('Build the arm64 essentia agent image locally (operator fallback to the CI build-arm64 job)')]
[group('docker')]
image-build-arm64 TAG="latest":
    #!/usr/bin/env bash
    set -e
    REGISTRY="ghcr.io"
    OWNER=$(echo "$(git remote get-url origin)" | sed 's|.*github.com[:/]||;s|/.*||' | tr '[:upper:]' '[:lower:]')
    REPO=$(basename -s .git "$(git remote get-url origin)" | tr '[:upper:]' '[:lower:]')
    IMAGE="${REGISTRY}/${OWNER}/${REPO}:{{TAG}}-arm64"
    echo "🐳 Building ${IMAGE} (Dockerfile.agent-arm64, native arm64 essentia)..."
    docker build --build-arg TF_VERSION=2.20.0 -f Dockerfile.agent-arm64 -t "${IMAGE}" .
    echo "✅ built ${IMAGE}"

[doc('Build + push the arm64 essentia agent image to GHCR (operator fallback; CI push is parity-gated in 47-04)')]
[group('docker')]
image-push-arm64 TAG="latest":
    #!/usr/bin/env bash
    set -e
    REGISTRY="ghcr.io"
    OWNER=$(echo "$(git remote get-url origin)" | sed 's|.*github.com[:/]||;s|/.*||' | tr '[:upper:]' '[:lower:]')
    REPO=$(basename -s .git "$(git remote get-url origin)" | tr '[:upper:]' '[:lower:]')
    IMAGE="${REGISTRY}/${OWNER}/${REPO}:{{TAG}}-arm64"
    echo "🐳 Building and pushing ${IMAGE}..."
    docker build --build-arg TF_VERSION=2.20.0 -f Dockerfile.agent-arm64 -t "${IMAGE}" .
    docker push "${IMAGE}"
    echo "✅ ${IMAGE} pushed"

[doc('Regenerate the x86 parity golden JSON from the reference clip (operator path; CI in plan 47-04 is authoritative)')]
[group('docker')]
parity-golden-regen TAG="latest":
    #!/usr/bin/env bash
    set -e
    REGISTRY="ghcr.io"
    OWNER=$(echo "$(git remote get-url origin)" | sed 's|.*github.com[:/]||;s|/.*||' | tr '[:upper:]' '[:lower:]')
    REPO=$(basename -s .git "$(git remote get-url origin)" | tr '[:upper:]' '[:lower:]')
    IMAGE="${REGISTRY}/${OWNER}/${REPO}/api:{{TAG}}"
    # 1. Provision the essentia model weights locally (host ./models).
    echo "📥 Provisioning models into ./models ..."
    bash scripts/download-models.sh models
    # 2. Run the SHARED dump tool inside the x86 api image over the committed reference clip.
    #    This writes scripts/parity/golden-x86.json for offline inspection.
    #    NOTE: CI (plan 47-04) is the AUTHORITATIVE golden producer; this is the operator regen path.
    echo "🐳 Generating golden-x86.json via ${IMAGE} ..."
    docker run --rm \
        -v "$(pwd)/scripts/parity:/parity" \
        -v "$(pwd)/models:/models:ro" \
        "${IMAGE}" \
        uv run python /parity/dump_analysis.py /parity/reference.wav /models --out /parity/golden-x86.json
    echo "✅ wrote scripts/parity/golden-x86.json"

[doc('Run the shared analyze_file dump inside an image; INTERP picks "uv run python" (x86 uv image) vs python3 (arm64 --system 3.13 agent image)')]
[group('docker')]
parity-dump IMAGE MODELS="./models" OUT="scripts/parity/actual.json" INTERP="uv run python":
    #!/usr/bin/env bash
    set -e
    # The SHARED dump path BOTH CI parity jobs delegate to (workflows delegate to
    # just — MEMORY). INTERP selects the in-image interpreter: the x86 api image
    # runs the uv-managed venv (default "uv run python"); the arm64 agent image
    # installs --system on 3.13 and MUST run python3 directly (uv run would
    # re-validate requires-python >=3.14 and miss the --system packages).
    OUT_BASE=$(basename "{{OUT}}")
    echo "🐳 Dumping analyze_file from {{IMAGE}} (interp: {{INTERP}}) → {{OUT}} ..."
    docker run --rm \
        -v "$(pwd)/scripts:/scripts" \
        -v "$(pwd)/{{MODELS}}:/models:ro" \
        "{{IMAGE}}" \
        {{INTERP}} /scripts/parity/dump_analysis.py /scripts/parity/reference.wav /models --out "/scripts/parity/${OUT_BASE}"
    PRODUCED="scripts/parity/${OUT_BASE}"
    if [ "${PRODUCED}" != "{{OUT}}" ]; then
        cp "${PRODUCED}" "{{OUT}}"
    fi
    echo "✅ wrote {{OUT}}"

[doc('Run the arm64↔x86 numeric parity check locally (operator mirror of the CI parity-guard)')]
[group('docker')]
parity-check TAG="latest":
    #!/usr/bin/env bash
    set -e
    REGISTRY="ghcr.io"
    OWNER=$(echo "$(git remote get-url origin)" | sed 's|.*github.com[:/]||;s|/.*||' | tr '[:upper:]' '[:lower:]')
    REPO=$(basename -s .git "$(git remote get-url origin)" | tr '[:upper:]' '[:lower:]')
    IMAGE="${REGISTRY}/${OWNER}/${REPO}:{{TAG}}-arm64"
    # 1. Provision the essentia model weights locally (host ./models).
    echo "📥 Provisioning models into ./models ..."
    bash scripts/download-models.sh models
    # 2. Dump the arm64 actual via the shared recipe — direct python3 for the agent image.
    just parity-dump "${IMAGE}" ./models scripts/parity/actual.json python3
    # 3. Compare against the committed/CI golden (non-zero exit on any parity break).
    echo "🔬 Comparing scripts/parity/actual.json against scripts/parity/golden-x86.json ..."
    uv run python scripts/parity/compare_analysis.py scripts/parity/golden-x86.json scripts/parity/actual.json

[doc('Validate docker-compose.yml syntax')]
[group('docker')]
docker-compose-validate:
    docker compose config --quiet && echo "✅ docker-compose.yml is valid"

[doc('Shell into the API container')]
[group('docker')]
docker-shell:
    docker compose exec api bash

[doc('View running containers')]
[group('docker')]
docker-ps:
    docker compose ps

[doc('Run Alembic migrations')]
[group('db')]
db-upgrade:
    uv run alembic upgrade head

[doc('Create a new Alembic migration')]
[group('db')]
db-revision MESSAGE:
    uv run alembic revision --autogenerate -m "{{MESSAGE}}"

[doc('Show current migration status')]
[group('db')]
db-current:
    uv run alembic current

[doc('Downgrade one migration')]
[group('db')]
db-downgrade:
    uv run alembic downgrade -1

[doc('Show migration history')]
[group('db')]
db-history:
    uv run alembic history

[doc('Download essentia ML models for audio analysis')]
[group('models')]
download-models:
    bash scripts/download-models.sh models

[doc('Trigger fingerprint processing for all eligible files')]
[group('fingerprint')]
fingerprint:
    curl -s -X POST http://localhost:8000/api/v1/fingerprint | python -m json.tool

[doc('Check fingerprint progress')]
[group('fingerprint')]
fingerprint-progress:
    curl -s http://localhost:8000/api/v1/fingerprint/progress | python -m json.tool

[doc('Check audfprint container health')]
[group('fingerprint')]
audfprint-health:
    docker compose exec worker curl -sf http://audfprint:8001/health | python -m json.tool

[doc('Check panako container health')]
[group('fingerprint')]
panako-health:
    docker compose exec worker curl -sf http://panako:8002/health | python -m json.tool

[doc('Update pre-commit hooks (with frozen SHAs)')]
[group('maintenance')]
update-hooks:
    uv run pre-commit autoupdate --freeze

[doc('Lock and upgrade all dependencies')]
[group('maintenance')]
lock-upgrade:
    uv lock --upgrade

[doc('Sync after lock upgrade')]
[group('maintenance')]
sync:
    uv sync
