---
phase: quick-260606-n7g
plan: 01
subsystem: deployment
tags: [docker-compose, ghcr, sidecars, agent]
requires: []
provides:
  - "audfprint/panako sidecars pulled from GHCR (image:) in docker-compose.agent.yml"
  - "regression guard test asserting all four agent services use GHCR images"
affects:
  - docker-compose.agent.yml
  - docs/deployment.md
  - tests/test_deployment/test_agent_compose.py
tech-stack:
  added: []
  patterns:
    - "All four agent images pull from GHCR via ${PHAZE_IMAGE_TAG:-latest}; sidecars keep a commented dev-only build: fallback"
key-files:
  created: []
  modified:
    - docker-compose.agent.yml
    - docs/deployment.md
    - tests/test_deployment/test_agent_compose.py
decisions:
  - "Sidecars switched from local build: to image: ghcr.io/simplicityguy/phaze/{audfprint,panako}:${PHAZE_IMAGE_TAG:-latest}, mirroring worker/watcher; original build: retained as commented local-dev-only fallback"
metrics:
  duration: ~10m
  completed: 2026-06-06
---

# Quick 260606-n7g: Switch audfprint/panako sidecars to GHCR images Summary

Switched the `audfprint` and `panako` fingerprint sidecars in `docker-compose.agent.yml` from local `build:` to pulling published GHCR images (`ghcr.io/simplicityguy/phaze/{audfprint,panako}:${PHAZE_IMAGE_TAG:-latest}`), so file-server hosts no longer need the full phaze source to run them; reconciled the stale "not on GHCR / built locally" claims in the compose header, `docs/deployment.md`, and the test docstring, and added a regression guard test.

## What changed

### Task 1 — `docker-compose.agent.yml` (commit 0f1d512)
- Both sidecars now use `image:` pulling the correct GHCR sub-path (`/audfprint`, `/panako`) pinned via `${PHAZE_IMAGE_TAG:-latest}`, matching the docker-publish.yml matrix `image_suffix` values.
- Original `build:` block retained directly beneath each `image:`, commented out and labeled `# Local-dev override — build from source instead of pulling GHCR:` (2-space indent preserved for a clean re-enable).
- Header comment block updated: all four agent images pull from GHCR; the D-16 image-tag-pinning note left intact.
- Preserved on both sidecars: the `${SCAN_PATH:?SCAN_PATH required}:/data/music:ro` fail-fast mount, the `audfprint_data`/`panako_data` named volumes, and `restart: unless-stopped`. `worker`, `watcher`, the top-level `volumes:`, and the services set are untouched.

### Task 2 — `docs/deployment.md` (commit 0b56e7d)
- Deployment Targets table: the agent-compose row now states all four services pull from GHCR via `PHAZE_IMAGE_TAG` (worker/watcher from the bare repo, sidecars from the `/audfprint` + `/panako` sub-paths).
- File-server services table: `audfprint`/`panako` rows now reference their GHCR images and drop the "Not on GHCR — built on the file-server host" claim.
- Step 4 prose clarified: the `git clone` on the file-server host is for the compose file + `.env` template (and optional dev source builds), not a requirement to build the sidecars.
- Build Pipeline and `docker-validate.yml` descriptions left intact (CI still builds + publishes + hadolints the sidecar Dockerfiles).
- Line-1 `<!-- generated-by: gsd-doc-writer -->` marker preserved.

### Task 3 — `tests/test_deployment/test_agent_compose.py` (commit 95cd630)
- Added `test_all_agent_services_pull_from_ghcr`: asserts each of the four services declares an `image:` matching the expected `ghcr.io/simplicityguy/phaze...` path and containing `PHAZE_IMAGE_TAG`. A service with only `build:` and no `image:` fails the guard (regression catch).
- Module docstring updated to five invariants; sidecars described as GHCR-pulled rather than build-context.
- All pre-existing invariant tests (services set, no-postgres, PHAZE_ROLE, fail-fast SCAN_PATH, publish-workflow tags) remain green.

## Verification

- `docker compose -f docker-compose.agent.yml --env-file <placeholders> config --quiet` → `PARSE_OK` (placeholder `.env` created/removed; `.env` is gitignored).
- `uv run pytest tests/test_deployment/test_agent_compose.py -q` → 6 passed.
- `uv run ruff check` on the test file → clean.
- Pre-commit hooks (yamllint strict, ruff, ruff-format, bandit, mypy) ran on every commit (no `--no-verify`); all passed. The project mypy hook runs `uv run mypy .` which excludes `tests/`, so the two pre-existing mypy notes on the test file (yaml stub import-untyped + `_load_agent_compose` no-any-return, present at HEAD) are out of scope and do not gate.
- Negative check confirmed the guard FAILS when a sidecar regresses to a bare `build:` with no `image:`.
- Protected invariants intact: services set unchanged `{worker, watcher, audfprint, panako}`; SCAN_PATH fail-fast mounts + named volumes + restart preserved; no DATABASE_URL/postgres/redis added; sidecar Dockerfiles and `docker-validate.yml` untouched.

## Deviations from Plan

None — plan executed exactly as written.

## Self-Check: PASSED

- docker-compose.agent.yml — FOUND (modified)
- docs/deployment.md — FOUND (modified)
- tests/test_deployment/test_agent_compose.py — FOUND (modified)
- Commit 0f1d512 (build) — FOUND
- Commit 0b56e7d (docs) — FOUND
- Commit 95cd630 (test) — FOUND
