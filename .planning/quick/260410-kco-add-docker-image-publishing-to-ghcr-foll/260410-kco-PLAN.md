---
phase: quick
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - .github/actions/docker-build-cache/action.yml
  - .github/workflows/docker-publish.yml
  - .github/workflows/ci.yml
  - .github/workflows/cleanup-images.yml
  - justfile
autonomous: true
requirements: []
must_haves:
  truths:
    - "Docker images are published to GHCR on push to main"
    - "Docker images are NOT pushed on pull requests (build-only)"
    - "All three images (api, audfprint, panako) are built and published"
    - "Docker layer caching speeds up subsequent builds"
    - "Cleanup workflow targets correct package names matching publish pattern"
  artifacts:
    - path: ".github/actions/docker-build-cache/action.yml"
      provides: "Composite action for Docker layer caching"
    - path: ".github/workflows/docker-publish.yml"
      provides: "Reusable workflow for building and pushing Docker images to GHCR"
    - path: ".github/workflows/ci.yml"
      provides: "Updated orchestrator with docker-publish job"
    - path: ".github/workflows/cleanup-images.yml"
      provides: "Updated cleanup with correct package name pattern"
  key_links:
    - from: ".github/workflows/ci.yml"
      to: ".github/workflows/docker-publish.yml"
      via: "workflow_call after aggregate-results"
    - from: ".github/workflows/docker-publish.yml"
      to: ".github/actions/docker-build-cache/action.yml"
      via: "composite action usage in build steps"
---

<objective>
Add Docker image publishing to GHCR following the discogsography pattern.

Purpose: Enable automated Docker image builds and pushes to GitHub Container Registry on every push to main, with build-only validation on PRs. This completes the CI/CD pipeline for phaze's three Docker images (api, audfprint, panako).

Output: Composite cache action, reusable docker-publish workflow, updated ci.yml orchestrator, corrected cleanup workflow, and justfile image-push recipe.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.github/workflows/ci.yml
@.github/workflows/docker-validate.yml
@.github/workflows/cleanup-images.yml
@justfile

<interfaces>
<!-- Existing docker-validate matrix structure (reuse for docker-publish): -->
```yaml
strategy:
  matrix:
    include:
      - name: api
        dockerfile: Dockerfile
        context: .
      - name: audfprint
        dockerfile: services/audfprint/Dockerfile.audfprint
        context: .
      - name: panako
        dockerfile: services/panako/Dockerfile.panako
        context: .
```

<!-- Discogsography docker-build-cache action interface: -->
```yaml
inputs:
  service-name: { required: true }
  dockerfile-path: { required: true }
  use-cache: { required: false, default: "true" }
outputs:
  cache-from: "Cache source for docker build"
  cache-to: "Cache destination for docker build"
  cache-hit: "Whether cache was hit"
```
</interfaces>
</context>

<tasks>

<task type="auto">
  <name>Task 1: Create docker-build-cache composite action and docker-publish reusable workflow</name>
  <files>.github/actions/docker-build-cache/action.yml, .github/workflows/docker-publish.yml</files>
  <action>
**Create `.github/actions/docker-build-cache/action.yml`** — copy the discogsography pattern exactly:
- Composite action with inputs: `service-name` (required), `dockerfile-path` (required), `use-cache` (optional, default "true")
- Outputs: `cache-from`, `cache-to`, `cache-hit`
- Step 1: Setup cache paths — if use-cache is true, set local buildx cache paths using `${{ runner.temp }}/.buildx-cache` and `.buildx-cache-new` (mode=max). Otherwise output empty strings.
- Step 2: Cache Docker layers — uses `actions/cache@v5` with key `${{ runner.os }}-buildx-${{ inputs.service-name }}-${{ hashFiles(inputs.dockerfile-path) }}-${{ hashFiles('**/uv.lock') }}` and restore-keys falling back progressively.

**Create `.github/workflows/docker-publish.yml`** — reusable workflow (`workflow_call`):
- `on: workflow_call:` with no inputs needed (it reads github context directly)
- `env:` block with `REGISTRY: ghcr.io` and `PYTHON_VERSION: "3.13"`
- `concurrency:` group `docker-publish-${{ github.workflow }}-${{ github.ref }}` with cancel-in-progress true
- `permissions:` contents read, packages write
- Single job `build-and-push` with `runs-on: ubuntu-latest`, `timeout-minutes: 30`
- Static strategy matrix (same as docker-validate.yml) with `use_cache: true` on each entry:
  ```yaml
  strategy:
    matrix:
      include:
        - name: api
          dockerfile: Dockerfile
          context: .
          use_cache: true
        - name: audfprint
          dockerfile: services/audfprint/Dockerfile.audfprint
          context: .
          use_cache: true
        - name: panako
          dockerfile: services/panako/Dockerfile.panako
          context: .
          use_cache: true
  ```

- Steps (following discogsography build-discogsography job, lines 201-329, but WITHOUT Anchore scan, setup-python-uv, Discord notification, or uv sync --frozen):
  1. Start timer (`date +%s` to output)
  2. Set lowercase image name: `echo "IMAGE_NAME=$(echo "${{ github.repository }}" | tr "[:upper:]" "[:lower:]")" >> "$GITHUB_ENV"`
  3. Checkout with `actions/checkout@v6` (no submodules needed for phaze)
  4. Free disk space (same pattern: remove dotnet, android, ghc, CodeQL, AGENT_TOOLSDIRECTORY, docker system prune)
  5. GHCR login with `docker/login-action@v4` — only when `github.event_name != 'pull_request'`. Registry `${{ env.REGISTRY }}`, username `${{ github.actor }}`, password `${{ secrets.GITHUB_TOKEN }}`
  6. Setup Docker build cache — `uses: ./.github/actions/docker-build-cache` with service-name, dockerfile-path from matrix, use-cache from matrix
  7. Extract metadata with `docker/metadata-action@v6`:
     ```yaml
     images: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}/${{ matrix.name }}
     tags: |
       type=raw,value=latest,enable={{is_default_branch}}
       type=ref,event=branch
       type=ref,event=pr
       type=schedule,pattern={{date 'YYYYMMDD'}}
     ```
  8. Setup Docker Buildx with `docker/setup-buildx-action@v4`, platforms `linux/amd64`, driver-opts with `image=moby/buildkit:latest` and `network=host`
  9. Build and push with `docker/build-push-action@v7`:
     - context: `${{ matrix.context }}`
     - file: `${{ matrix.dockerfile }}`
     - platforms: linux/amd64
     - push: `${{ github.event_name != 'pull_request' }}`
     - tags/labels from metadata step
     - provenance: true, sbom: true
     - cache-from/cache-to from docker-cache step
     - build-args: BUILDKIT_INLINE_CACHE=1, BUILDKIT_CACHE_MOUNT_NS=phaze, DOCKER_BUILDKIT=1, PYTHON_VERSION, BUILD_DATE (from head_commit.timestamp or repository.updated_at), BUILD_VERSION (from meta version), VCS_REF (github.sha)
  10. Move cache (temp fix for buildkit issue) — only if `matrix.use_cache`, move `.buildx-cache-new` to `.buildx-cache`
  11. Cleanup build artifacts (always) — buildx prune and system prune with 1h filter, show remaining disk
  12. Collect metrics (always) — calculate duration, notice with service name, duration, cache used, cache hit info

All step names must use emoji prefixes matching the discogsography pattern.
  </action>
  <verify>
    <automated>cd /Users/Robert/Code/public/phaze && yamllint -d relaxed .github/actions/docker-build-cache/action.yml .github/workflows/docker-publish.yml && actionlint .github/workflows/docker-publish.yml</automated>
  </verify>
  <done>
  - docker-build-cache composite action exists with cache setup and actions/cache@v5
  - docker-publish.yml reusable workflow builds all 3 images with GHCR push on non-PR events
  - Both files pass yamllint and actionlint validation
  </done>
</task>

<task type="auto">
  <name>Task 2: Update ci.yml orchestrator, cleanup-images.yml package names, and justfile</name>
  <files>.github/workflows/ci.yml, .github/workflows/cleanup-images.yml, justfile</files>
  <action>
**Update `.github/workflows/ci.yml`:**

1. Add `packages: write` to top-level permissions (alongside existing `contents: read` and `security-events: write`).

2. Add `docker-publish` job AFTER the `aggregate-results` job:
```yaml
  # ============================================================================
  # DOCKER PUBLISH - Build and push images to GHCR after all gates pass
  # ============================================================================
  docker-publish:
    needs: [detect-changes, aggregate-results]
    if: needs.detect-changes.outputs.code-changed == 'true'
    uses: ./.github/workflows/docker-publish.yml
    permissions:
      contents: read
      packages: write
```

**Update `.github/workflows/cleanup-images.yml`:**

Change the static matrix package names to match the publish naming pattern `phaze/<service>`:
```yaml
matrix:
  package:
    - phaze/api
    - phaze/audfprint
    - phaze/panako
```
This replaces the current `phaze`, `phaze/audfprint`, `phaze/panako` list. The `api` image was previously just `phaze` but now publishes as `phaze/api` for consistency.

**Update `justfile`:**

Add an `image-push` recipe in the `# === Docker ===` section, after the existing `docker-validate` recipe:
```just
# Push Docker images to GHCR (requires: gh auth token with packages:write)
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
```
  </action>
  <verify>
    <automated>cd /Users/Robert/Code/public/phaze && yamllint -d relaxed .github/workflows/ci.yml .github/workflows/cleanup-images.yml && actionlint .github/workflows/ci.yml && just --list | grep image-push</automated>
  </verify>
  <done>
  - ci.yml has packages:write permission and docker-publish job after aggregate-results
  - cleanup-images.yml uses phaze/api, phaze/audfprint, phaze/panako package names
  - justfile has image-push recipe for local Docker image publishing
  - All workflow files pass yamllint and actionlint
  </done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| CI -> GHCR | GitHub Actions pushes images using GITHUB_TOKEN |
| GHCR -> Users | Published images are pulled by consumers |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-quick-01 | Tampering | Docker images | mitigate | Enable provenance and SBOM attestation on build-push-action (provenance: true, sbom: true) |
| T-quick-02 | Information Disclosure | GHCR auth | mitigate | Only login to GHCR on non-PR events; PR builds are build-only, never push. Uses ephemeral GITHUB_TOKEN, not PAT. |
| T-quick-03 | Elevation of Privilege | packages:write | accept | Required for GHCR push. Scoped to workflow_call, only runs after aggregate-results passes all quality gates. |
</threat_model>

<verification>
1. All workflow YAML files pass `yamllint -d relaxed` and `actionlint`
2. docker-publish.yml is a valid reusable workflow (workflow_call trigger)
3. ci.yml correctly chains docker-publish after aggregate-results
4. cleanup-images.yml package names align with publish image names
5. justfile image-push recipe is listed and syntactically valid
</verification>

<success_criteria>
- Three new/modified workflow files and one composite action committed
- docker-publish.yml builds 3 images with GHCR push on non-PR, build-only on PR
- Docker layer caching via composite action with actions/cache@v5
- Cleanup workflow package names match publish pattern (phaze/api, phaze/audfprint, phaze/panako)
- justfile has image-push recipe for local publishing
- All files pass linting (yamllint, actionlint)
</success_criteria>

<output>
After completion, create `.planning/quick/260410-kco-add-docker-image-publishing-to-ghcr-foll/260410-kco-SUMMARY.md`
</output>
