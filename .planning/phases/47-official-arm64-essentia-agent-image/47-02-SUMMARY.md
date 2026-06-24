---
phase: 47-official-arm64-essentia-agent-image
plan: 02
subsystem: ci
tags: [ci, github-actions, docker, arm64, aarch64, ghcr, hadolint, metadata-action, just, import-smoke]

# Dependency graph
requires:
  - phase: 47-01
    provides: Dockerfile.agent-arm64 (the recipe this CI job builds + import-smokes)
provides:
  - build-arm64 job (native ubuntu-24.04-arm, load + import-smoke, NO push) that warms the scope=arm64 gha cache
  - build-arm64 job outputs.tags + outputs.labels (resolved -arm64 tags AND OCI labels) for the 47-04 parity-gated push
  - hadolint gate for Dockerfile.agent-arm64 in docker-validate.yml
  - just image-build-arm64 / image-push-arm64 operator recipes
  - tag-strategy regression test for the -arm64 latest+version tags
affects: [47-04 (parity-guard gated push consumes build-arm64 outputs.tags/labels + replays the warmed cache), 51 (cloud-agent compose pins <version>-arm64)]

# Tech tracking
tech-stack:
  added: [ubuntu-24.04-arm native arm64 runner, docker/metadata-action flavor suffix=-arm64,onlatest=true, type=gha,scope=arm64 cache]
  patterns: [native-arm64 build-load-smoke job (no QEMU, no push), resolve-and-expose tags+labels for a downstream gated push, lint-only matrix entry for an expensive-to-build Dockerfile]

key-files:
  created: []
  modified:
    - .github/workflows/docker-publish.yml
    - .github/workflows/docker-validate.yml
    - justfile
    - tests/test_deployment/test_agent_compose.py

key-decisions:
  - "Tag mechanism: flavor: suffix=-arm64,onlatest=true over the base tag set (NOT explicit -arm64 raw/semver tags) — verified against docker/metadata-action docs (Assumption A5): suffix applies to ALL generated tags and onlatest=true extends it to the latest tag, cleanly yielding latest-arm64 + <version>-arm64"
  - "build-arm64 does NOT push (push: false, load: true) — the registry push is the 47-04 parity-gated step; this job only resolves+exposes outputs.tags + outputs.labels and warms type=gha,scope=arm64"
  - "Import-smoke invokes the system interpreter directly (python3 -c, NOT uv run) to match the image CMD python3 -m saq — the agent image installs --system on 3.13 and uv run would re-validate requires-python >=3.14"

requirements-completed: [CLOUDIMG-01, CLOUDIMG-02]

# Metrics
duration: 12min
completed: 2026-06-24
---

# Phase 47 Plan 02: Official arm64 essentia agent image Summary

**Wired CI to build + load + import-smoke `Dockerfile.agent-arm64` on a native `ubuntu-24.04-arm` runner (no QEMU) on the same release triggers as x86, resolving matching `-arm64` tags AND OCI labels as job outputs for the 47-04 parity-gated push, with a hadolint gate, operator `just` recipes, and a tag-strategy regression test.**

## Performance

- **Duration:** ~12 min
- **Completed:** 2026-06-24
- **Tasks:** 3
- **Files modified:** 4

## Accomplishments
- Added a `build-arm64` job to `.github/workflows/docker-publish.yml`: `runs-on: ubuntu-24.04-arm` (native aarch64, free for public repos, no QEMU), `timeout-minutes: 60` (the essentia C++ compile is ~324s cold), least-priv `permissions: { contents: read, packages: write }`.
- The job builds `file: Dockerfile.agent-arm64` at `platforms: linux/arm64` with `load: true` + `push: false` (no push — the registry push is the 47-04 parity-gated step), `build-args: TF_VERSION=2.20.0`, warming the shared `cache-from/to: type=gha,scope=arm64,mode=max` so 47-04 replays the layers instead of recompiling essentia.
- Resolves the `-arm64` tags via `flavor: suffix=-arm64,onlatest=true` and exposes BOTH `outputs.tags` AND `outputs.labels` (gave the metadata-action step `id: meta`) so the 47-04 gated push pushes exactly these `-arm64` tags WITH the same OCI source/revision/version labels the x86 image carries.
- Added an import-smoke step against the loaded image via direct `python3 -c "import phaze.tasks.agent_worker; import essentia.standard"` (the v4.1.0-class boot guard, CLOUDIMG-01) before the image is eligible for the gated push.
- Extended the `docker-validate.yml` hadolint matrix with an `agent-arm64` entry (lint-only) and guarded the x86 "Test Docker build" step with `if: matrix.name != 'agent-arm64'` (the api import-smoke is already scoped to `matrix.name == 'api'`).
- Added `just image-build-arm64` / `image-push-arm64` recipes (bash shebang, `set -e`, OWNER/REPO derived from `git remote`, lowercased, `--build-arg TF_VERSION=2.20.0`, `-arm64` tag) as the operator fallback to the CI path.
- Added `test_docker_publish_arm64_job_tags_latest_and_version` to `tests/test_deployment/test_agent_compose.py` — a YAML-parse regression guard for the `-arm64` latest+version tag strategy.

## Tag mechanism used
**`flavor: suffix=-arm64,onlatest=true`** (NOT explicit `-arm64` raw/semver tags). Verified against the docker/metadata-action docs (Assumption A5): the `flavor` suffix applies to ALL generated tags, and `onlatest=true` additionally applies it to the `latest` tag — so `type=raw,value=latest` → `latest-arm64` and `type=semver,pattern={{version}}` → `<version>-arm64`. Both `outputs.tags` and `outputs.labels` are exposed for the 47-04 gated push (confirmed via `yaml.safe_load`: `outputs: ['tags', 'labels']`).

## Task Commits

Each task was committed atomically:

1. **Task 1: Add the build-arm64 job (native arm64, load + import-smoke, no push, -arm64 tags + labels outputs)** - `8c7f99f` (feat)
2. **Task 2: Extend the hadolint matrix + add operator image-build/push-arm64 just recipes** - `9f08747` (feat)
3. **Task 3: Tag-strategy regression test for the -arm64 build job** - `0929c57` (test)

## Files Created/Modified
- `.github/workflows/docker-publish.yml` - new `build-arm64` job (native arm64, load + import-smoke, no push; exposes `outputs.tags` + `outputs.labels`).
- `.github/workflows/docker-validate.yml` - `agent-arm64` hadolint matrix entry (lint-only); x86 build step guarded off for it.
- `justfile` - `image-build-arm64` + `image-push-arm64` operator recipes.
- `tests/test_deployment/test_agent_compose.py` - `test_docker_publish_arm64_job_tags_latest_and_version` + two helpers (`_extract_build_arm64_metadata_step`, `_flavor_lines`).

## Decisions Made
- **Tag mechanism = flavor suffix:** chose `flavor: suffix=-arm64,onlatest=true` over explicit raw/semver `-arm64` tags — single source of truth, applies uniformly to every tag pattern, and matches the documented metadata-action behavior.
- **No push from build-arm64:** `push: false` + `load: true`; the registry push (with `provenance`/`sbom`/`labels`) is the 47-04 parity-gated step. The `load` (docker) exporter cannot emit attestations, so this job only resolves+exposes them. A parity-divergent image therefore can never reach GHCR ahead of the guard (T-47-08).
- **Direct interpreter import-smoke:** `python3 -c` (not `uv run`) to match the image CMD `python3 -m saq` — the agent image installs `--system` on 3.13 and `uv run` would re-validate `requires-python >=3.14` and miss those packages.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Added `type=ref,event=branch` + `type=ref,event=pr` tag patterns to the build-arm64 metadata-action**
- **Found during:** Task 1 (trigger analysis)
- **Issue:** The plan's three specified tag types (`type=raw,value=latest,enable={{is_default_branch}}`, `type=semver,pattern={{version}}`, `type=ref,event=tag`) ALL resolve to empty on a `pull_request` event (latest is gated to the default branch; semver/ref-tag require a tag push). docker-publish runs on PRs (gated only on `code-changed == 'true'`), so on a PR the job would build with `load: true` and an EMPTY tag set, and the import-smoke `docker run "<first-tag>"` would fail with no image to run.
- **Fix:** Added `type=ref,event=branch` + `type=ref,event=pr` (and `type=semver,pattern={{major}}.{{minor}}`) to the metadata-action tags, mirroring the x86 `build-and-push` job, so a non-empty loadable tag always exists on every trigger. With `flavor: suffix=-arm64`, these resolve to `<branch>-arm64` / `pr-<n>-arm64` / `<major>.<minor>-arm64`. None of them resolve on a release-tag push, so the gated push outputs remain `<version>-arm64` (+ `v<version>-arm64` via `type=ref,event=tag`).
- **Files modified:** `.github/workflows/docker-publish.yml`
- **Verification:** yamllint + actionlint clean; the tag-strategy test still asserts the latest+version `-arm64` pair; the existing `test_docker_publish_workflow_tags_both_latest_and_version` (x86) still passes.
- **Committed in:** `8c7f99f`

---

**Total deviations:** 1 auto-fixed (1 blocking — empty-tags-on-PR would break `load`+import-smoke)
**Impact on plan:** Strictly additive tag patterns that guarantee a loadable tag on every CI trigger; the `-arm64` latest+version contract and the gated-push outputs are unchanged.

## Issues Encountered
- yamllint flagged the import-smoke `python3 -c` line (>150 chars) twice; shortened the success print message to satisfy the 150-char limit (cosmetic, no behavior change). Folded into `8c7f99f` before the commit landed.
- The native arm64 build + import-smoke were NOT executed locally (this is x86 macOS / no native arm64 CI runner here, and a QEMU compile is forbidden). The job is validated by YAML-parse + actionlint + hadolint-config assertions; the real native build runs in CI on `ubuntu-24.04-arm`, and real-audio numeric parity (proving fix #4) is owned by plan 47-04.

## Next Phase Readiness
- `build-arm64` exposes `outputs.tags` + `outputs.labels` and warms `type=gha,scope=arm64` — ready for plan 47-04 to add the real-audio numeric parity guard and the gated registry push that consumes these outputs and replays the cache.
- Open follow-ups carried forward: (1) 47-04 owns the gated push (`provenance`/`sbom`/`labels: ${{ needs.build-arm64.outputs.labels }}`) and the real-audio parity comparison incl. fix #4 validation; (2) Phase 51 cloud-agent compose pins `<version>-arm64`.

## Self-Check: PASSED
- FOUND: `.github/workflows/docker-publish.yml` (build-arm64 job, runs-on ubuntu-24.04-arm, outputs tags+labels)
- FOUND: `.github/workflows/docker-validate.yml` (agent-arm64 hadolint matrix entry)
- FOUND: `justfile` (image-build-arm64 + image-push-arm64)
- FOUND: `tests/test_deployment/test_agent_compose.py` (test_docker_publish_arm64_job_tags_latest_and_version — green)
- FOUND commits: `8c7f99f`, `9f08747`, `0929c57`

---
*Phase: 47-official-arm64-essentia-agent-image*
*Completed: 2026-06-24*
