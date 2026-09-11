#!/usr/bin/env bash
# Shared local image naming, authentication, build, push, and model resolution.
set -euo pipefail

docker_bin="${DOCKER_BIN:-docker}"
gh_bin="${GH_BIN:-gh}"
registry="ghcr.io"

die() {
  echo "❌ $*" >&2
  exit 1
}

repository_name() {
  local remote owner repo
  remote="$(git remote get-url origin)" || die "git remote 'origin' is required"
  owner="$(printf '%s\n' "$remote" | sed 's|.*github.com[:/]||;s|/.*||' | tr '[:upper:]' '[:lower:]')"
  repo="$(basename -s .git "$remote" | tr '[:upper:]' '[:lower:]')"
  [[ -n "$owner" && -n "$repo" ]] || die "could not derive the GHCR repository from origin: $remote"
  printf '%s/%s\n' "$owner" "$repo"
}

image_ref() {
  local tag="$1"
  local variant="${2:-api}"
  local suffix=""
  case "$variant" in
    api) ;;
    arm64) suffix="-arm64" ;;
    *) die "unknown image variant: $variant" ;;
  esac
  printf '%s/%s:%s%s\n' "$registry" "$(repository_name)" "$tag" "$suffix"
}

registry_login() {
  local username
  username="$($gh_bin api user --jq .login)" || die "GitHub CLI authentication is required"
  "$gh_bin" auth token | "$docker_bin" login "$registry" --username "$username" --password-stdin
}

models_dir() {
  local requested="${MODELS_PATH:-}"
  if [[ -n "$requested" ]]; then
    [[ -d "$requested" ]] || die "MODELS_PATH is not a directory: $requested"
    (cd "$requested" && pwd -P)
    return
  fi
  local fallback
  fallback="$(pwd -P)/models"
  if [[ ! -d "$fallback" ]]; then
    echo "📥 Provisioning models into ./models (set MODELS_PATH to reuse an existing set) ..." >&2
    bash scripts/download-models.sh "$fallback"
  fi
  printf '%s\n' "$fallback"
}

build_image() {
  local variant="$1"
  local tag="$2"
  local image
  image="$(image_ref "$tag" "$variant")"
  case "$variant" in
    api)
      "$docker_bin" build -f Dockerfile -t "$image" .
      ;;
    arm64)
      DOCKER_BUILDKIT=1 "$docker_bin" build --build-arg TF_VERSION=2.20.0 -f Dockerfile.agent-arm64 -t "$image" .
      ;;
    *) die "unknown image variant: $variant" ;;
  esac
  printf '%s\n' "$image"
}

push_image() {
  local variant="$1"
  local tag="$2"
  local image
  registry_login
  image="$(build_image "$variant" "$tag")"
  "$docker_bin" push "$image"
  printf '%s\n' "$image"
}

case "${1:-}" in
  ref)
    [[ $# -ge 2 && $# -le 3 ]] || die "usage: $0 ref TAG [api|arm64]"
    image_ref "$2" "${3:-api}"
    ;;
  models-dir)
    [[ $# -eq 1 ]] || die "usage: $0 models-dir"
    models_dir
    ;;
  build)
    [[ $# -eq 3 ]] || die "usage: $0 build {api|arm64} TAG"
    build_image "$2" "$3"
    ;;
  push)
    [[ $# -eq 3 ]] || die "usage: $0 push {api|arm64} TAG"
    push_image "$2" "$3"
    ;;
  *)
    die "usage: $0 {ref|models-dir|build|push} ..."
    ;;
esac
