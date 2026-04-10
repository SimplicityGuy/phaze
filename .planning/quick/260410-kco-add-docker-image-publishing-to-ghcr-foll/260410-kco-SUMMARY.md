---
phase: quick
plan: 01
subsystem: ci-cd
tags: [docker, ghcr, ci, github-actions]
dependency_graph:
  requires: []
  provides: [docker-publish-workflow, ghcr-image-publishing, docker-build-cache-action]
  affects: [ci.yml, cleanup-images.yml, justfile]
tech_stack:
  added: []
  patterns: [composite-action-caching, reusable-workflow-call, matrix-strategy-docker]
key_files:
  created:
    - .github/actions/docker-build-cache/action.yml
    - .github/workflows/docker-publish.yml
  modified:
    - .github/workflows/ci.yml
    - .github/workflows/cleanup-images.yml
    - justfile
decisions:
  - "Followed discogsography composite action pattern exactly for docker-build-cache"
  - "Used env vars for all github context in run blocks (security best practice)"
  - "Renamed phaze package to phaze/api in cleanup workflow for consistency with publish naming"
metrics:
  duration: ~4m
  completed: "2026-04-10"
  tasks_completed: 2
  tasks_total: 2
---

# Quick Task 260410-kco: Add Docker Image Publishing to GHCR Summary

Docker image publishing to GHCR following the discogsography pattern, with composite cache action, reusable workflow, CI orchestration, and corrected cleanup targets.

## Tasks Completed

### Task 1: Create docker-build-cache composite action and docker-publish reusable workflow
- **Commit:** `5e62ef8`
- **Files created:** `.github/actions/docker-build-cache/action.yml`, `.github/workflows/docker-publish.yml`
- Created composite action with inputs (service-name, dockerfile-path, use-cache) and outputs (cache-from, cache-to, cache-hit)
- Uses `actions/cache@v5` with progressive restore-key fallback based on Dockerfile hash and uv.lock hash
- Reusable workflow builds 3 images (api, audfprint, panako) with matrix strategy
- Push to GHCR on non-PR events only; build-only on PRs (T-quick-02 mitigation)
- Provenance and SBOM attestation enabled (T-quick-01 mitigation)
- All step names use emoji prefixes matching discogsography pattern
- Disk space cleanup, buildx cache rotation, and metrics collection included

### Task 2: Update ci.yml, cleanup-images.yml, and justfile
- **Commit:** `3f91f93`
- **Files modified:** `.github/workflows/ci.yml`, `.github/workflows/cleanup-images.yml`, `justfile`
- Added `packages: write` permission to ci.yml top-level permissions
- Added `docker-publish` job after `aggregate-results` with proper needs chain
- Updated cleanup-images.yml matrix from `phaze` to `phaze/api` for consistency
- Added `image-push` justfile recipe for local Docker image building and pushing

## Deviations from Plan

None - plan executed exactly as written.

## Verification Results

- All workflow YAML files pass `yamllint -d relaxed` (warnings only for line length)
- All workflow files pass `yamllint` strict mode via pre-commit hooks
- `actionlint` passes clean on both `docker-publish.yml` and `ci.yml`
- `just --list` shows `image-push` recipe
- Pre-commit hooks pass on both commits

## Threat Mitigations Applied

| Threat ID | Mitigation |
|-----------|------------|
| T-quick-01 | `provenance: true` and `sbom: true` on build-push-action |
| T-quick-02 | GHCR login gated on `github.event_name != 'pull_request'`; uses ephemeral GITHUB_TOKEN |
| T-quick-03 | `packages:write` scoped to workflow_call, only runs after aggregate-results passes |

## Self-Check: PASSED

- [x] `.github/actions/docker-build-cache/action.yml` exists
- [x] `.github/workflows/docker-publish.yml` exists
- [x] `.github/workflows/ci.yml` updated with docker-publish job
- [x] `.github/workflows/cleanup-images.yml` updated with phaze/api
- [x] `justfile` updated with image-push recipe
- [x] Commit `5e62ef8` exists
- [x] Commit `3f91f93` exists
