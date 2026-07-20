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
# Dedicated ephemeral Postgres for the Phase-82 PERF-02 /pipeline/stats bench. A SEPARATE container
# (own port 5545) so an explicit `just test-db-down`/`test-db` recreate on the shared phaze-test-db
# (e.g. from a sibling session) can never wipe the ~200K seeded perf corpus mid-measurement.
perf_db_container := "phaze-perf-db"
perf_db_port := env_var_or_default("PHAZE_PERF_DB_PORT", "5545")
perf_db_name := "phaze_perf82"
perf_db_dsn := "postgresql://phaze:phaze@localhost:" + perf_db_port + "/" + perf_db_name
perf_db_sa_dsn := "postgresql+asyncpg://phaze:phaze@localhost:" + perf_db_port + "/" + perf_db_name
# Standalone Tailwind CSS binary version. Keep in sync with the Dockerfile
# css-build stage. NO Node — the standalone binary compiles assets/src/app.css.
tailwind_version := "v4.3.2"

[doc('Install all dependencies')]
[group('dev')]
install: tailwind
    uv sync

[doc('Start all services in Docker')]
[group('dev')]
up: tailwind
    docker compose up -d

[doc('Start file-server agent stack (standalone docker-compose.agent.yml)')]
[group('dev')]
up-agent:
    docker compose -f docker-compose.agent.yml up -d

[doc('Start the OCI A1 cloud compute-agent stack (standalone docker-compose.cloud-agent.yml)')]
[group('dev')]
cloud-agent-up:
    docker compose -f docker-compose.cloud-agent.yml up -d

[doc('Stop the OCI A1 cloud compute-agent stack')]
[group('dev')]
cloud-agent-down:
    docker compose -f docker-compose.cloud-agent.yml down

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
rebuild: tailwind
    docker compose up -d --build

[doc('Download the standalone Tailwind binary (NO Node) and rebuild app.css')]
[group('build')]
tailwind:
    @mkdir -p src/phaze/static/css bin
    @if [ ! -x ./bin/tailwindcss ]; then \
        echo "⬇️  Downloading standalone Tailwind binary ({{ tailwind_version }})..."; \
        OS=$(uname -s | tr '[:upper:]' '[:lower:]' | sed 's/darwin/macos/'); \
        ARCH=$(uname -m | sed 's/x86_64/x64/;s/aarch64/arm64/'); \
        curl -fsSL --retry 3 --retry-delay 5 -o ./bin/tailwindcss \
            "https://github.com/tailwindlabs/tailwindcss/releases/download/{{ tailwind_version }}/tailwindcss-${OS}-${ARCH}"; \
        chmod +x ./bin/tailwindcss; \
    fi
    ./bin/tailwindcss -i assets/src/app.css -o src/phaze/static/css/app.css --minify

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

# Non-blocking dead-code sweep (CLEAN-02). NOT a CI/pre-commit gate — framework-invoked
# code produces false-positives that need per-candidate human reachability judgment. A
# nonzero exit merely lists remaining candidates to hand-verify. vulture_whitelist.py is a
# hand-audited suppression list for framework false-positives (FastAPI handlers, Pydantic
# validators, transient ORM attrs, SAQ hooks, CLI entry points).
[doc('Run the vulture dead-code sweep over src/phaze (non-blocking; lists candidates to hand-verify)')]
[group('test')]
vulture:
    uv run vulture src/phaze vulture_whitelist.py --min-confidence 80 --ignore-decorators "@router.*,@app.*,@field_validator,@model_validator,@validator,@pytest.fixture"

# --cov-fail-under=0 is REQUIRED: a single bucket only exercises a fraction of
# phaze, so pytest-cov auto-enforcing pyproject's global fail_under gate against a
# bucket's PARTIAL coverage would fail every leg (exit 1) before the shard is uploaded,
# and the combine job (needs: [test]) would never run. The global gate is enforced
# once, on the COMBINED number, by `coverage-combine`.
[doc('Run a single test bucket, writing coverage data to .coverage.<bucket> (CI shard). XDIST="" keeps DB buckets serial; DB-free buckets pass XDIST="-n auto".')]
[group('test')]
test-bucket NAME XDIST="":
    COVERAGE_FILE=.coverage.{{NAME}} uv run pytest tests/{{NAME}} {{XDIST}} --cov=phaze --cov-report= --cov-fail-under=0 -q

[doc('Combine per-bucket .coverage.* shards into coverage.xml and enforce the gate')]
[group('test')]
coverage-combine:
    uv run coverage combine
    uv run coverage xml
    uv run coverage json
    uv run coverage report --fail-under=95
    uv run python scripts/coverage_floor.py

[doc('Classify changed files (newline-delimited on stdin) as code-changed=true|false for the CI doc-only skip gate (CI-04)')]
[group('test')]
detect-code-changes:
    @bash scripts/classify-changed-files.sh

[doc('Start ephemeral Postgres + Redis for integration tests (ports PHAZE_TEST_DB_PORT/PHAZE_TEST_REDIS_PORT, defaults 5433/6380)')]
[group('test')]
test-db:
    #!/usr/bin/env bash
    set -euo pipefail
    container="{{test_db_container}}"
    port="{{test_db_port}}"
    redis_container="{{test_redis_container}}"
    redis_port="{{test_redis_port}}"
    if [ "$(docker inspect -f '{{{{.State.Running}}' "$container" 2>/dev/null || echo false)" = "true" ]; then
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
    if [ "$(docker inspect -f '{{{{.State.Running}}' "$redis_container" 2>/dev/null || echo false)" = "true" ]; then
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

[doc('Create a correctly-named isolated test DB pair for one worktree, e.g. `just test-db-for laqf`')]
[group('test')]
test-db-for name:
    #!/usr/bin/env bash
    set -euo pipefail
    # Exists so nobody hand-rolls an isolated database name again. The natural instinct is to
    # SUFFIX the standard name (`phaze_test_<name>`); that shape is accepted by the guard in
    # `tests/db_guard.py`, but this recipe emits the canonical `phaze_<name>_test` pair and,
    # more importantly, prints the exact exports to use. Requires `just test-db` first.
    just test-db
    container="{{test_db_container}}"
    port="{{test_db_port}}"
    main_db="phaze_{{name}}_test"
    migrations_db="phaze_{{name}}_migrations_test"
    for db in "$main_db" "$migrations_db"; do
        if docker exec "$container" psql -U phaze -d phaze_test -tc \
            "SELECT 1 FROM pg_database WHERE datname = '${db}'" | grep -q 1; then
            echo "🐘 ${db} already exists"
        else
            docker exec "$container" psql -U phaze -d phaze_test \
                -c "CREATE DATABASE ${db} OWNER phaze;" >/dev/null
            echo "✅ created ${db}"
        fi
    done
    echo ""
    echo "Export these before running pytest in this worktree:"
    echo "  export TEST_DATABASE_URL=\"postgresql+asyncpg://phaze:phaze@localhost:${port}/${main_db}\""
    echo "  export MIGRATIONS_TEST_DATABASE_URL=\"postgresql+asyncpg://phaze:phaze@localhost:${port}/${migrations_db}\""

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

[doc('Run all quality checks (lint + typecheck + test); auto-provisions the ephemeral test-db when no TEST_DATABASE_URL override is already exported (e.g. a fresh worktree)')]
[group('lint')]
check: lint typecheck
    #!/usr/bin/env bash
    set -euo pipefail
    # A fresh worktree has no Postgres/Redis of its own -- `test` (bare `uv run
    # pytest`) then dies at fixture setup dialing the CI-matching localhost:5432
    # default (tests/conftest.py:45). `just integration-test` already solves this
    # for a one-shot run, but its `test-db-down` EXIT trap would tear down a
    # services container other concurrent worktrees/sessions are relying on if
    # `check` reused it. So: provision (idempotently, via the existing `test-db`
    # recipe) and export the matching env here, but never tear down -- leave that
    # to an explicit `just test-db-down`. If the caller already exported
    # TEST_DATABASE_URL (CI, another `just` recipe, a shared multi-worktree rig
    # with per-worktree database names), respect it verbatim and skip provisioning.
    if [ -z "${TEST_DATABASE_URL:-}" ]; then
        just test-db
        export TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:{{test_db_port}}/phaze_test"
        export MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:{{test_db_port}}/phaze_migrations_test"
        export PHAZE_REDIS_URL="redis://localhost:{{test_redis_port}}/0"
    fi
    just test

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
    # CI publishes the api image at the bare-repo URL (image_suffix="" for api,
    # Phase 29 D-15) — ghcr.io/<owner>/<repo>:<tag>, NOT a /api sub-path. Match it.
    IMAGE="${REGISTRY}/${OWNER}/${REPO}:{{TAG}}"
    # 1. Provision the essentia model weights locally (host ./models).
    echo "📥 Provisioning models into ./models ..."
    bash scripts/download-models.sh models
    # 2. Run the SHARED dump tool inside the x86 api image over the committed reference clip.
    #    This writes scripts/parity/golden-x86.json for offline inspection.
    #    NOTE: CI (plan 47-04) is the AUTHORITATIVE golden producer; this is the operator regen path.
    echo "🐳 Generating golden-x86.json via ${IMAGE} ..."
    # The image runs as a non-root user that cannot write into the host-owned
    # bind-mounted scripts/parity dir; write into a world-writable output dir and
    # copy the result out host-side (same fix as the parity-dump recipe).
    OUT_DIR=$(mktemp -d)
    chmod 777 "${OUT_DIR}"
    docker run --rm \
        -v "$(pwd)/scripts/parity:/parity:ro" \
        -v "$(pwd)/models:/models:ro" \
        -v "${OUT_DIR}:/out" \
        "${IMAGE}" \
        uv run python /parity/dump_analysis.py /parity/reference.wav /models --out /out/golden-x86.json
    cp "${OUT_DIR}/golden-x86.json" scripts/parity/golden-x86.json
    rm -rf "${OUT_DIR}"
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
    # The image runs as a NON-ROOT user that cannot write into the host-owned
    # bind-mounted scripts/ dir (PermissionError on /scripts/parity/<out>.json).
    # Mount scripts read-only and give the container a dedicated world-writable
    # output dir to write --out into, then copy the result to {{OUT}} host-side.
    OUT_DIR=$(mktemp -d)
    chmod 777 "${OUT_DIR}"
    echo "🐳 Dumping analyze_file from {{IMAGE}} (interp: {{INTERP}}) → {{OUT}} ..."
    docker run --rm \
        -v "$(pwd)/scripts:/scripts:ro" \
        -v "$(pwd)/{{MODELS}}:/models:ro" \
        -v "${OUT_DIR}:/out" \
        "{{IMAGE}}" \
        {{INTERP}} /scripts/parity/dump_analysis.py /scripts/parity/reference.wav /models --out "/out/${OUT_BASE}"
    cp "${OUT_DIR}/${OUT_BASE}" "{{OUT}}"
    rm -rf "${OUT_DIR}"
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

[doc('Run the state↔derived shadow-compare gate against the target DB (MIG-02). Exit nonzero on hard divergence.')]
[group('db')]
shadow-compare *ARGS:
    uv run python -m phaze.cli.shadow_compare {{ ARGS }}

[doc('Start a DEDICATED ephemeral Postgres for the PERF-02 bench (own port, never wiped by test-db recreates)')]
[group('db')]
perf-db-up:
    #!/usr/bin/env bash
    set -euo pipefail
    container="{{perf_db_container}}"
    port="{{perf_db_port}}"
    if [ "$(docker inspect -f '{{{{.State.Running}}' "$container" 2>/dev/null || echo false)" = "true" ]; then
        echo "🐘 ${container} already running on port ${port}"
    else
        docker rm -f "$container" >/dev/null 2>&1 || true
        echo "🐘 Starting ${container} (postgres:18-alpine) on host port ${port}..."
        docker run -d --name "$container" \
            -e POSTGRES_USER=phaze -e POSTGRES_PASSWORD=phaze -e POSTGRES_DB={{perf_db_name}} \
            -p "${port}:5432" postgres:18-alpine >/dev/null
    fi
    for _ in $(seq 1 30); do
        if docker exec "$container" pg_isready -U phaze -d {{perf_db_name}} >/dev/null 2>&1; then
            echo "✅ ${container} ready on localhost:${port} ({{perf_db_name}})"; exit 0
        fi
        sleep 1
    done
    echo "❌ ${container} did not become ready within 30s" >&2; exit 1

[doc('Stop and remove the dedicated PERF-02 bench Postgres')]
[group('db')]
perf-db-down:
    docker rm -f "{{perf_db_container}}" >/dev/null 2>&1 || true
    @echo "🧹 Removed {{perf_db_container}}"

[doc('Migrate the perf DB to HEAD (>=036) and seed the ~N synthetic corpus for the PERF-02 bench (Phase 82)')]
[group('db')]
perf-seed N='200000':
    PHAZE_DATABASE_URL="{{perf_db_sa_dsn}}" uv run alembic upgrade head
    uv run python scripts/seed_perf_corpus.py --n {{N}} --dsn "{{perf_db_dsn}}" --reseed

[doc('EXPLAIN ANALYZE the derived hot queries + time /pipeline/stats against the seeded perf DB (PERF-02, D-07)')]
[group('db')]
perf-explain ITER='20':
    uv run python scripts/perf_explain.py --dsn "{{perf_db_dsn}}" --iterations {{ITER}}

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
