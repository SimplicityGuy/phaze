#!/usr/bin/env bash
# Dedicated integration-test containers. The justfile supplies configuration; this script owns
# creation, final-server readiness, per-run environment, and ownership-aware cleanup.
#
#   run    one run on its own Postgres + Valkey pair, torn down on success, failure or interrupt
#   clean  reap stale runs THIS checkout owns; never an active run, never another worktree's
set -euo pipefail

docker_bin="${DOCKER_BIN:-docker}"
just_bin="${JUST_BIN:-just}"
pg_image="${PHAZE_INTEGRATION_POSTGRES_IMAGE:-postgres:18-alpine}"
pg_shm_size="${PHAZE_INTEGRATION_POSTGRES_SHM_SIZE:-256m}"
valkey_image="${PHAZE_INTEGRATION_VALKEY_IMAGE:-valkey/valkey:9-alpine}"
bind_ip="${PHAZE_INTEGRATION_BIND_IP:-127.0.0.1}"
fixed_pg_port="${PHAZE_INTEGRATION_DB_PORT:-0}"
fixed_valkey_port="${PHAZE_INTEGRATION_VALKEY_PORT:-0}"
pg_prefix="${PHAZE_INTEGRATION_DB_PREFIX:-phaze-integration-test-db}"
valkey_prefix="${PHAZE_INTEGRATION_VALKEY_PREFIX:-phaze-integration-test-valkey}"

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
  local db_container="${pg_prefix}-${run_id}" valkey_container="${valkey_prefix}-${run_id}"
  local found=0 container actual_run actual_owner owner_pid

  validate_run_id "$run_id"
  for container in "$db_container" "$valkey_container"; do
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
  "$docker_bin" rm -f "$db_container" "$valkey_container" >/dev/null 2>&1 || true
  echo "🧹 Removed owned stale integration run ${run_id}"
}

# phaze-aje5d: the no-argument replacement for `cleanup RUN_ID`. Every labelled integration run is
# listed; a run another worktree owns is named and skipped, and each run this checkout owns goes
# through cleanup_run, whose live-pid refusal still protects a run that is in flight. A refused run
# does not stop the sweep -- cleanup_run runs in a subshell so its `die` only ends that one run.
clean_stale() {
  local owner rows run_id run_owner removed=0 kept=0 foreign=0
  owner="$(owner_id)"
  rows="$("$docker_bin" ps -a --filter label=com.phaze.integration.run-id \
    --format '{{.Label "com.phaze.integration.run-id"}} {{.Label "com.phaze.integration.owner"}}' | sort -u)"
  while read -r run_id run_owner; do
    [[ -n "$run_id" ]] || continue
    if [[ "$run_owner" != "$owner" ]]; then
      foreign=$((foreign + 1))
      echo "↪️  Leaving integration run ${run_id}: another worktree owns it."
      continue
    fi
    if (cleanup_run "$run_id" "$owner"); then
      removed=$((removed + 1))
    else
      kept=$((kept + 1))
    fi
  done <<<"$rows"
  echo "🧹 Integration-test cleanup: removed ${removed} stale run(s); left ${kept} active and ${foreign} foreign run(s) alone."
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

wait_for_valkey() {
  local container="$1"
  echo "⏳ Waiting for Valkey to accept connections..."
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
  validate_port "$fixed_valkey_port" PHAZE_INTEGRATION_TEST_VALKEY_PORT

  # run_id and owner are GLOBALS on purpose (phaze-aje5d): the EXIT trap below expands them after
  # run_suite has returned, when a `local` is already gone. With `local`, a green suite ended in
  # "run_id: unbound variable" under `set -u` -- exit 1 and BOTH containers left running.
  run_id="${PHAZE_INTEGRATION_RUN_ID:-$$_${RANDOM}}"
  validate_run_id "$run_id"
  owner="$(owner_id)"
  local db_container valkey_container pg_port valkey_port
  db_container="${pg_prefix}-${run_id}"
  valkey_container="${valkey_prefix}-${run_id}"

  trap 'cleanup_run "$run_id" "$owner" "$$" >/dev/null 2>&1 || true' EXIT
  # phaze-aje5d: an interrupt must leave nothing behind either. bash defers a trapped signal until
  # a FOREGROUND child returns, so the suite runs in the background and is waited on; the handler
  # stops it and exits, which runs the EXIT trap above. TERM, not the received signal, is forwarded:
  # a non-interactive bash starts background jobs with SIGINT ignored.
  local suite_pid=""
  stop_suite() {
    if [[ -n "${suite_pid:-}" ]]; then
      kill -TERM "$suite_pid" 2>/dev/null || true
      wait "$suite_pid" 2>/dev/null || true
    fi
    exit "$1"
  }
  trap 'stop_suite 130' INT
  trap 'stop_suite 143' TERM
  trap 'stop_suite 129' HUP
  local labels=(
    --label "com.phaze.integration.run-id=${run_id}"
    --label "com.phaze.integration.owner=${owner}"
    --label "com.phaze.integration.pid=$$"
  )
  local pg_publish valkey_publish
  if [[ "$fixed_pg_port" == "0" ]]; then
    pg_publish="${bind_ip}::5432"
  else
    pg_publish="${bind_ip}:${fixed_pg_port}:5432"
  fi
  if [[ "$fixed_valkey_port" == "0" ]]; then
    valkey_publish="${bind_ip}::6379"
  else
    valkey_publish="${bind_ip}:${fixed_valkey_port}:6379"
  fi

  echo "🐘 Starting ${db_container} (${pg_image})..."
  "$docker_bin" run -d --name "$db_container" "${labels[@]}" \
    -e POSTGRES_USER=phaze -e POSTGRES_PASSWORD=phaze -e POSTGRES_DB=phaze_test \
    --shm-size "$pg_shm_size" -p "$pg_publish" "$pg_image" >/dev/null
  echo "🟥 Starting ${valkey_container} (${valkey_image})..."
  "$docker_bin" run -d --name "$valkey_container" "${labels[@]}" \
    -p "$valkey_publish" "$valkey_image" >/dev/null

  if [[ "$fixed_pg_port" == "0" ]]; then
    pg_port="$("$docker_bin" port "$db_container" 5432/tcp | head -n1 | sed -E 's/.*:([0-9]+)$/\1/')"
  else
    pg_port="$fixed_pg_port"
  fi
  if [[ "$fixed_valkey_port" == "0" ]]; then
    valkey_port="$("$docker_bin" port "$valkey_container" 6379/tcp | head -n1 | sed -E 's/.*:([0-9]+)$/\1/')"
  else
    valkey_port="$fixed_valkey_port"
  fi

  wait_for_postgres "$db_container"
  wait_for_valkey "$valkey_container"
  bash scripts/ensure-pg-database.sh "$db_container" phaze_migrations_test

  export TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:${pg_port}/phaze_test"
  export MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:${pg_port}/phaze_migrations_test"
  # DB 0 is safe here because every run owns a separate Valkey container and host port.
  export PHAZE_REDIS_URL="redis://localhost:${valkey_port}/0"
  echo "🧪 Dedicated run ${run_id}: Postgres ${pg_port}, Valkey ${valkey_port}; running the validation-grade coverage suite."
  "$just_bin" test-cov &
  suite_pid=$!
  wait "$suite_pid"
}

case "${1:-}" in
  run)
    run_suite
    ;;
  clean)
    [[ $# -eq 1 ]] || die "usage: $0 clean"
    clean_stale
    ;;
  *)
    die "usage: $0 {run|clean}"
    ;;
esac
