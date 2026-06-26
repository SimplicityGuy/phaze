---
phase: 51-deployment-config-docs
plan: 02
subsystem: infra
tags: [docker-compose, tailscale, arm64, oci-a1, compute-agent, saq, yaml, pytest]

# Dependency graph
requires:
  - phase: 47-official-arm64-essentia-agent-image
    provides: "native-arm64 GHCR image published with a -arm64 tag suffix (latest-arm64 + <version>-arm64)"
  - phase: 48-compute-agent-type
    provides: "kind=compute agent type; PHAZE_AGENT_KIND env relaxes the empty-scan-roots gate (config.py:470)"
  - phase: 50-push-pipeline
    provides: "rsync-over-Tailscale scratch handoff; cloud_scratch_dir / PHAZE_CLOUD_SCRATCH_DIR landing path"
provides:
  - "docker-compose.cloud-agent.yml — worker-only, host-Tailscale, no-media, arm64, named-scratch OCI A1 compute-agent stack"
  - "tests/test_deployment/test_cloud_agent_compose.py — 8 YAML-parse invariant assertions (no docker daemon)"
  - "justfile cloud-agent-up / cloud-agent-down recipes"
affects: [51-03, 51-04, cloud-burst-docs, oci-a1-runbook]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Worker-only compute-agent compose: strip media sidecars, drop SCAN_PATH/media bind, add network_mode host + -arm64 image + named scratch volume"
    - "YAML-parse compose invariant test (yaml.safe_load, raw ${VAR} tokens, no docker daemon) mirroring test_agent_compose.py"

key-files:
  created:
    - docker-compose.cloud-agent.yml
    - tests/test_deployment/test_cloud_agent_compose.py
  modified:
    - justfile

key-decisions:
  - "Scratch volume mount written as cloud_scratch:${PHAZE_CLOUD_SCRATCH_DIR:?...}:rw — named-volume source (left of first colon) with a fail-fast container mount path"
  - "network_mode: host (D-05) — no bridge network, no published port; the agent is a pure outbound tailnet consumer"
  - "Image pinned ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}-arm64 — the -arm64 suffix is mandatory (D-08, no multi-arch manifest)"

patterns-established:
  - "Compute-agent compose = agent.yml minus media sidecars/media bind, plus host networking, -arm64 image, named scratch"
  - "Named-volume invariant test: source segment (vol.split(':',1)[0]) has no leading / . or $ and is declared under top-level volumes:"

requirements-completed: [CLOUDDEPLOY-01]

# Metrics
duration: ~12min
completed: 2026-06-26
---

# Phase 51 Plan 02: Cloud-agent compose stack Summary

**Net-new `docker-compose.cloud-agent.yml` — a worker-only, host-Tailscale, no-media, `-arm64`, named-scratch OCI A1 compute-agent stack — with 8 YAML-parse invariant tests and justfile up/down recipes (CLOUDDEPLOY-01).**

## Performance

- **Duration:** ~12 min
- **Completed:** 2026-06-26
- **Tasks:** 2
- **Files modified:** 3 (2 created, 1 modified)

## Accomplishments
- Authored `docker-compose.cloud-agent.yml`: single `worker` service, `network_mode: host` (D-05), `PHAZE_ROLE=agent` + `PHAZE_AGENT_KIND=compute` (D-06), `-arm64` image suffix (D-08), named `cloud_scratch` volume with MODELS `:rw` / CA `:ro` (D-07), no media bind, no Postgres surface (DIST-04).
- Mirrored the agent.yml banner + "Invariants (asserted by tests/...)" comment block, adapted for the cloud stack.
- Added `tests/test_deployment/test_cloud_agent_compose.py`: 8 pure-`yaml.safe_load` assertions (single-worker, no-DB/DIST-04, role+kind, `-arm64` image, named scratch + top-level volume, no-media, MODELS rw / CA ro, host networking) — needs no docker daemon.
- Added `cloud-agent-up` / `cloud-agent-down` justfile recipes mirroring the `up-agent` convention.

## Task Commits

Each task was committed atomically:

1. **Task 1: Author docker-compose.cloud-agent.yml** - `26cc002` (feat)
2. **Task 2: Author the cloud-agent compose invariant test** - `510c455` (test)

## Files Created/Modified
- `docker-compose.cloud-agent.yml` - OCI A1 compute-agent compose stack (worker-only, host net, arm64, named scratch)
- `tests/test_deployment/test_cloud_agent_compose.py` - 8 YAML-parse invariant assertions
- `justfile` - cloud-agent-up / cloud-agent-down recipes (group dev)

## Decisions Made
- Scratch mount uses the named-volume form `cloud_scratch:${PHAZE_CLOUD_SCRATCH_DIR:?PHAZE_CLOUD_SCRATCH_DIR required}:rw` — the named volume `cloud_scratch` is the mount *source* and the operator-supplied `PHAZE_CLOUD_SCRATCH_DIR` is the in-container mount path (fail-fast if unset), matching the Phase 50 push-pipeline landing path.
- The named-scratch test parses the source as `vol.split(":", 1)[0]` and treats any segment not starting with `/`, `.`, or `$` as a named-volume ref (binds and `${VAR}` host paths are excluded), then asserts it is declared under top-level `volumes:`.

## Deviations from Plan

None - plan executed exactly as written. (The pre-commit `ruff-format` hook reflowed one long multi-line `assert` in the test file during the Task 2 commit; semantics unchanged.)

## Issues Encountered
- The worktree spawned at an ancestor commit (`f4d7017`) that predated the Phase 51 planning commits; ran the `<worktree_branch_check>` base-correction `git reset --hard 9aad3f5` to land on the plan's base before reading the plan. No content impact.

## User Setup Required
None - no external service configuration required for this plan. (Operator-facing OCI A1 / Tailscale provisioning is documented in a later Plan 51-04 runbook; this plan ships only the compose file, its test, and justfile recipes.)

## Next Phase Readiness
- The cloud-agent compose + invariant test are in place for the 51-03 config toggle and 51-04 docs/runbook plans to reference.
- `uv run pytest tests/test_deployment/ -q` green (23 passed); `pre-commit` (yamllint + ruff + mypy) clean on both new files.

## Self-Check: PASSED

- FOUND: docker-compose.cloud-agent.yml
- FOUND: tests/test_deployment/test_cloud_agent_compose.py
- FOUND: justfile cloud-agent-up / cloud-agent-down recipes
- FOUND: commit 26cc002 (Task 1, feat)
- FOUND: commit 510c455 (Task 2, test)

---
*Phase: 51-deployment-config-docs*
*Completed: 2026-06-26*
