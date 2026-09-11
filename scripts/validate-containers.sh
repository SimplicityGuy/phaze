#!/usr/bin/env bash
# One local/CI implementation for every shipped Compose shape and Dockerfile lint target.
set -euo pipefail

docker_bin="${DOCKER_BIN:-docker}"
hadolint_bin="${HADOLINT_BIN:-hadolint}"
hadolint_version="v2.15.1"
repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd -P)"

die() {
  echo "❌ $*" >&2
  exit 1
}

restore_env_file() {
  if [[ -n "${saved_env:-}" && -e "$saved_env" ]]; then
    cp "$saved_env" "$repo_root/.env"
  elif [[ "${created_env:-0}" == "1" ]]; then
    rm -f "$repo_root/.env"
  fi
  [[ -n "${scratch:-}" ]] && rm -rf "$scratch"
}

prepare_env_file() {
  scratch="$(mktemp -d)"
  if [[ -e "$repo_root/.env" ]]; then
    saved_env="$scratch/env.backup"
    cp "$repo_root/.env" "$saved_env"
  else
    created_env=1
  fi
  trap restore_env_file EXIT
  printf '%s\n' \
    'REDIS_PASSWORD=container-validate-placeholder' \
    'REDIS_BIND_IP=127.0.0.1' \
    'POSTGRES_PASSWORD=container-validate-placeholder' \
    'POSTGRES_BIND_IP=127.0.0.1' \
    'SCAN_PATH=/tmp/phaze-container-validate-scan' \
    'MODELS_PATH=/tmp/phaze-container-validate-models' \
    'PHAZE_API_URL=https://app-server.example:8000' \
    'PHAZE_REDIS_URL=redis://default:container-validate@app-server.example:6379/0' \
    'PHAZE_AGENT_TOKEN=phaze_agent_container-validate' \
    'PHAZE_AGENT_ID=container-validate-agent' \
    'PHAZE_CLOUD_SCRATCH_DIR=/tmp/phaze-container-validate-scratch' \
    >"$repo_root/.env"
}

validate_compose() {
  prepare_env_file
  cd "$repo_root"
  local -a common=(--env-file "$repo_root/.env" config --quiet)

  echo "🔍 Compose: application-server base"
  "$docker_bin" compose -f docker-compose.yml "${common[@]}"
  echo "🔍 Compose: live-reload development overlay"
  "$docker_bin" compose -f docker-compose.yml -f docker-compose.dev.yml "${common[@]}"
  echo "🔍 Compose: file-server agent"
  "$docker_bin" compose -f docker-compose.agent.yml "${common[@]}"
  echo "🔍 Compose: cloud compute agent"
  "$docker_bin" compose -f docker-compose.cloud-agent.yml "${common[@]}"
  echo "🔍 Compose: standalone telemetry example"
  "$docker_bin" compose -f docker-compose.telemetry.example.yml "${common[@]}"
  echo "✅ All shipped Compose shapes are valid"
}

validate_dockerfiles() {
  local reported_version
  reported_version="$("$hadolint_bin" --version 2>&1)" || die "hadolint is not installed"
  [[ "$reported_version" == *"${hadolint_version#v}"* ]] || die "hadolint ${hadolint_version} is required; got: ${reported_version}"
  cd "$repo_root"
  local dockerfile
  for dockerfile in Dockerfile Dockerfile.agent-arm64 Dockerfile.job; do
    echo "🔍 Dockerfile: ${dockerfile}"
    "$hadolint_bin" "$dockerfile"
  done
  echo "✅ All shipped Dockerfiles passed hadolint ${hadolint_version}"
}

case "${1:-}" in
  compose)
    validate_compose
    ;;
  dockerfiles)
    validate_dockerfiles
    ;;
  all)
    validate_compose
    validate_dockerfiles
    ;;
  *)
    die "usage: $0 {compose|dockerfiles|all}"
    ;;
esac
