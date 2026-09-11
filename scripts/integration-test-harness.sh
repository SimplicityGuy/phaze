#!/usr/bin/env bash
# Dedicated integration-test containers. The justfile supplies configuration; this script owns
# creation, final-server readiness, per-run environment, and ownership-aware cleanup.
set -euo pipefail

docker_bin="${DOCKER_BIN:-docker}"
just_bin="${JUST_BIN:-just}"
pg_image="${PHAZE_INTEGRATION_POSTGRES_IMAGE:-postgres:18-alpine}"
pg_shm_size="${PHAZE_INTEGRATION_POSTGRES_SHM_SIZE:-256m}"
bind_ip="${PHAZE_INTEGRATION_BIND_IP:-127.0.0.1}"
fixed_pg_port="${PHAZE_INTEGRATION_DB_PORT:-0}"
fixed_redis_port="${PHAZE_INTEGRATION_REDIS_PORT:-0}"
pg_prefix="${PHAZE_INTEGRATION_DB_PREFIX:-phaze-integration-test-db}"
redis_prefix="${PHAZE_INTEGRATION_REDIS_PREFIX:-phaze-integration-test-redis}"

die() {
  echo "❌ $*" >&2
  exit 1
}

validate_port() {
  local value="$1" name="$2"
  [[ "$value" =~ ^[0-9]+$ ]] || die "${name} must be 0 or an integer TCP port"
  ((value >= 0 && value <= 65535)) || die "${name} must be between 0 and 65535"
}

validate_run_id() {
  [[ "$1" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$ ]] || die "integration run ID must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}"
}

owner_id() {
  local root
  root="$(git rev-parse --show-toplevel 2>/dev/null || pwd -P)"
  printf '%s' "$root" | cksum | awk '{print $1}'
}

container_label() {
  "$docker_bin" inspect -f "{{index .Config.Labels \"$2\"}}" "$1" 2>/dev/null || true
}

container_exists() {
  "$docker_bin" inspect "$1" >/dev/null 2>&1
}

cleanup_run() {
  local run_id="$1" expected_owner="$2" allowed_pid="${3:-}"
  local db_container="${pg_prefix}-${run_id}" redis_container="${redis_prefix}-${run_id}"
  local found=0 container actual_run actual_owner owner_pid

  validate_run_id "$run_id"
  for container in "$db_container" "$redis_container"; do
    container_exists "$container" || continue
    found=1
    actual_run="$(container_label "$container" com.phaze.integration.run-id)"
    actual_owner="$(container_label "$container" com.phaze.integration.owner)"
    owner_pid="$(container_label "$container" com.phaze.integration.pid)"
    [[ "$actual_run" == "$run_id" ]] || die "${container} is not a labelled Phaze integration container for run ${run_id}"
    [[ "$actual_owner" == "$expected_owner" ]] || die "run ${run_id} belongs to another worktree; refusing cross-seat cleanup"
    if [[ -n "$owner_pid" && "$owner_pid" =~ ^[0-9]+$ && "$owner_pid" != "$allowed_pid" ]] && kill -0 "$owner_pid" 2>/dev/null; then
      die "run ${run_id} is still active under pid ${owner_pid}; refusing cleanup"
    fi
  done
  ((found == 1)) || die "no dedicated integration containers found for run ${run_id}"
  "$docker_bin" rm -f "$db_container" "$redis_container" >/dev/null 2>&1 || true
  echo "🧹 Removed owned stale integration run ${run_id}"
}

wait_for_postgres() {
  local container="$1"
  echo "⏳ Waiting for the final Postgres TCP server..."
  for _ in $(seq 1 30); do
    # The entrypoint's temporary init server is socket-only. The explicit host keeps readiness
    # false until the final TCP server is accepting connections and phaze_test exists.
    if "$docker_bin" exec "$container" pg_isready -h 127.0.0.1 -U phaze -d phaze_test >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  "$docker_bin" logs "$container" >&2 || true
  die "${container} did not become ready within 30s"
}

wait_for_redis() {
  local container="$1"
  echo "⏳ Waiting for Redis to accept connections..."
  for _ in $(seq 1 30); do
    if "$docker_bin" exec "$container" redis-cli ping >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  "$docker_bin" logs "$container" >&2 || true
  die "${container} did not become ready within 30s"
}

run_suite() {
  validate_port "$fixed_pg_port" PHAZE_INTEGRATION_TEST_DB_PORT
  validate_port "$fixed_redis_port" PHAZE_INTEGRATION_TEST_REDIS_PORT

  local run_id="${PHAZE_INTEGRATION_RUN_ID:-$$_${RANDOM}}"
  validate_run_id "$run_id"
  local owner db_container redis_container pg_port redis_port
  owner="$(owner_id)"
  db_container="${pg_prefix}-${run_id}"
  redis_container="${redis_prefix}-${run_id}"

  trap 'cleanup_run "$run_id" "$owner" "$$" >/dev/null 2>&1 || true' EXIT
  local labels=(
    --label "com.phaze.integration.run-id=${run_id}"
    --label "com.phaze.integration.owner=${owner}"
    --label "com.phaze.integration.pid=$$"
  )
  local pg_publish redis_publish
  if [[ "$fixed_pg_port" == "0" ]]; then
    pg_publish="${bind_ip}::5432"
  else
    pg_publish="${bind_ip}:${fixed_pg_port}:5432"
  fi
  if [[ "$fixed_redis_port" == "0" ]]; then
    redis_publish="${bind_ip}::6379"
  else
    redis_publish="${bind_ip}:${fixed_redis_port}:6379"
  fi

  echo "🐘 Starting ${db_container} (${pg_image})..."
  "$docker_bin" run -d --name "$db_container" "${labels[@]}" \
    -e POSTGRES_USER=phaze -e POSTGRES_PASSWORD=phaze -e POSTGRES_DB=phaze_test \
    --shm-size "$pg_shm_size" -p "$pg_publish" "$pg_image" >/dev/null
  echo "🟥 Starting ${redis_container} (redis:7-alpine)..."
  "$docker_bin" run -d --name "$redis_container" "${labels[@]}" \
    -p "$redis_publish" redis:7-alpine >/dev/null

  if [[ "$fixed_pg_port" == "0" ]]; then
    pg_port="$("$docker_bin" port "$db_container" 5432/tcp | head -n1 | sed -E 's/.*:([0-9]+)$/\1/')"
  else
    pg_port="$fixed_pg_port"
  fi
  if [[ "$fixed_redis_port" == "0" ]]; then
    redis_port="$("$docker_bin" port "$redis_container" 6379/tcp | head -n1 | sed -E 's/.*:([0-9]+)$/\1/')"
  else
    redis_port="$fixed_redis_port"
  fi

  wait_for_postgres "$db_container"
  wait_for_redis "$redis_container"
  bash scripts/ensure-pg-database.sh "$db_container" phaze_migrations_test

  export TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:${pg_port}/phaze_test"
  export MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:${pg_port}/phaze_migrations_test"
  # DB 0 is safe here because every run owns a separate Redis container and host port.
  export PHAZE_REDIS_URL="redis://localhost:${redis_port}/0"
  echo "🧪 Dedicated run ${run_id}: Postgres ${pg_port}, Redis ${redis_port}; running the validation-grade coverage suite."
  "$just_bin" test-cov
}

case "${1:-}" in
  run)
    run_suite
    ;;
  cleanup)
    [[ $# -eq 2 ]] || die "usage: $0 cleanup RUN_ID"
    cleanup_run "$2" "$(owner_id)"
    ;;
  *)
    die "usage: $0 {run|cleanup RUN_ID}"
    ;;
esac
