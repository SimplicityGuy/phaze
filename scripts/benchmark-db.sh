#!/usr/bin/env bash
# Lifecycle for the reusable, synthetic-corpus benchmark database.
set -euo pipefail

docker_bin="${DOCKER_BIN:-docker}"

die() {
  echo "❌ $*" >&2
  exit 1
}

validate_port() {
  local port="$1"
  [[ "$port" =~ ^[0-9]+$ ]] || die "benchmark database port must be an integer"
  ((port >= 1 && port <= 65535)) || die "benchmark database port must be between 1 and 65535"
}

container_exists() {
  "$docker_bin" inspect "$1" >/dev/null 2>&1
}

verify_container() {
  local container="$1" expected_image="$2" expected_bind_ip="$3" expected_port="$4"
  local actual_image actual_bind_ip actual_port
  actual_image="$("$docker_bin" inspect -f '{{.Config.Image}}' "$container")"
  actual_bind_ip="$("$docker_bin" inspect -f '{{(index (index .HostConfig.PortBindings "5432/tcp") 0).HostIp}}' "$container")"
  actual_port="$("$docker_bin" inspect -f '{{(index (index .HostConfig.PortBindings "5432/tcp") 0).HostPort}}' "$container")"
  [[ "$actual_image" == "$expected_image" ]] || die "${container} uses image ${actual_image}; expected ${expected_image}"
  [[ "$actual_bind_ip" == "$expected_bind_ip" ]] || die "${container} binds ${actual_bind_ip}; expected ${expected_bind_ip}"
  [[ "$actual_port" == "$expected_port" ]] || die "${container} publishes port ${actual_port}; expected ${expected_port}"
}

wait_for_database() {
  local container="$1" database="$2"
  local _
  for _ in {1..30}; do
    if "$docker_bin" exec "$container" pg_isready -h 127.0.0.1 -U phaze -d "$database" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  "$docker_bin" logs "$container" >&2 || true
  die "${container} did not become ready within 30s"
}

start_database() {
  local container="$1" port="$2" database="$3" image="$4" shm_size="$5" bind_ip="$6"
  validate_port "$port"
  if container_exists "$container"; then
    # Verify before start. A stale same-name container must never be accepted or even started.
    verify_container "$container" "$image" "$bind_ip" "$port"
    if [[ "$("$docker_bin" inspect -f '{{.State.Running}}' "$container")" != "true" ]]; then
      "$docker_bin" start "$container" >/dev/null
    fi
  else
    local run_err
    run_err="$(mktemp)"
    if ! "$docker_bin" run -d --name "$container" \
      -e POSTGRES_USER=phaze -e POSTGRES_PASSWORD=phaze -e "POSTGRES_DB=${database}" \
      --shm-size "$shm_size" -p "${bind_ip}:${port}:5432" "$image" >/dev/null 2>"$run_err"; then
      if grep -q "is already in use" "$run_err"; then
        verify_container "$container" "$image" "$bind_ip" "$port"
      else
        sed 's/^/docker: /' "$run_err" >&2
        rm -f "$run_err"
        return 1
      fi
    fi
    rm -f "$run_err"
  fi
  wait_for_database "$container" "$database"
  echo "✅ ${container} ready on ${bind_ip}:${port} (${database})"
}

case "${1:-}" in
  up)
    [[ $# -eq 7 ]] || die "usage: $0 up CONTAINER PORT DATABASE IMAGE SHM_SIZE BIND_IP"
    start_database "$2" "$3" "$4" "$5" "$6" "$7"
    ;;
  down)
    [[ $# -eq 2 ]] || die "usage: $0 down CONTAINER"
    "$docker_bin" rm -f "$2" >/dev/null 2>&1 || true
    echo "🧹 Removed $2"
    ;;
  *)
    die "usage: $0 {up|down} ..."
    ;;
esac
