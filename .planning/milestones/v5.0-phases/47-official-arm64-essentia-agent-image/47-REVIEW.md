---
phase: 47-official-arm64-essentia-agent-image
reviewed: 2026-06-24T00:00:00Z
depth: standard
files_reviewed: 10
files_reviewed_list:
  - Dockerfile.agent-arm64
  - .github/workflows/docker-publish.yml
  - .github/workflows/docker-validate.yml
  - justfile
  - pyproject.toml
  - scripts/parity/compare_analysis.py
  - scripts/parity/dump_analysis.py
  - scripts/parity/generate_reference.py
  - tests/test_deployment/test_agent_compose.py
  - tests/test_parity/test_compare_analysis.py
findings:
  critical: 1
  warning: 2
  info: 1
  total: 4
status: issues_found
---

# Phase 47: Code Review Report

**Reviewed:** 2026-06-24
**Depth:** standard
**Files Reviewed:** 10
**Status:** issues_found

## Summary

Reviewed the arm64 essentia agent image implementation: `Dockerfile.agent-arm64`, CI workflow additions (`docker-publish.yml`, `docker-validate.yml`), justfile operator recipes, `pyproject.toml` per-file-ignore delta, the three parity toolkit scripts (`compare_analysis.py`, `dump_analysis.py`, `generate_reference.py`), and their tests.

The Dockerfile is well-structured — all four spike fixes are present, essentia is pinned to a hardcoded SHA, the 3.13 reconciliation mechanism is sound, and the CMD correctly targets the system interpreter. The parity comparator is solid: the anti-silent-pass invariant (None vs number = fail) is correctly implemented and unit-tested, `_flatten_scores` handles the recursive schema without hard-coding keys, and the `_EXACT_FIELDS` / epsilon split is correct.

One critical defect was found: the `parity-golden-regen` justfile recipe constructs a non-existent GHCR image URL, making the operator golden-regen path non-functional. Two warnings concern the CI workflow: `build-arm64` carries `packages: write` permission it never uses, and the test helper that guards the x86 tag strategy finds the first metadata-action step across all jobs rather than specifically the `build-and-push` job.

---

## Critical Issues

### CR-01: `parity-golden-regen` pulls a non-existent image URL — operator golden regen is broken

**File:** `justfile:305`
**Issue:** The `parity-golden-regen` recipe constructs the x86 api image URL as:
```bash
IMAGE="${REGISTRY}/${OWNER}/${REPO}/api:{{TAG}}"
# → ghcr.io/simplicityguy/phaze/api:latest
```
But per Phase 29 D-15, the api image is published at the **bare-repo URL** (`image_suffix = ""` in the `build-and-push` matrix):
```
ghcr.io/simplicityguy/phaze:latest   ← correct
ghcr.io/simplicityguy/phaze/api:latest  ← does not exist
```
`test_all_agent_services_pull_from_ghcr` (line 127) asserts worker/watcher pull from `ghcr.io/simplicityguy/phaze` (no sub-path), confirming the bare-repo canonical form. `test_cleanup_package_list_matches_published_images` also derives the api package as `"phaze"` (not `"phaze/api"`) from `image_suffix = ""`.

The CI path (`parity-golden-x86` job) is unaffected — it correctly resolves the tag from `metadata-action` with `images: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}` (bare-repo). Only the operator recipe is broken.

Result: `docker pull ghcr.io/simplicityguy/phaze/api:latest` fails with a manifest-not-found error. An operator running `just parity-golden-regen` cannot regenerate the golden.

**Fix:**
```bash
# justfile parity-golden-regen recipe, line 305 — remove the /api sub-path:
IMAGE="${REGISTRY}/${OWNER}/${REPO}:{{TAG}}"
# → ghcr.io/simplicityguy/phaze:latest  (matches the bare-repo published location)
```

---

## Warnings

### WR-01: `build-arm64` job carries `packages: write` permission but never pushes

**File:** `.github/workflows/docker-publish.yml:200-203`
**Issue:** The `build-arm64` job is documented as doing only `load: true, push: false` — the registry push is intentionally deferred to `parity-guard`. Yet the job declares `packages: write`, and the GHCR login step (lines 223-228) is present, gated on non-PR. Neither is needed: `docker/build-push-action` with `load: true` builds locally and does not contact GHCR; GHA cache (`type=gha`) uses the Actions cache API, not GHCR credentials.

The `packages: write` scope on the GITHUB_TOKEN is a least-privilege violation. If any frozen-SHA action in this job were compromised, it would carry registry write access without the parity gate that `parity-guard` enforces before the push. This undermines the T-47-08 containment model.

**Fix:**
```yaml
# docker-publish.yml  build-arm64 job
permissions:
  contents: read
  # packages: write removed — this job never pushes; login step also removed
  # packages: write lives only on parity-guard (the job that actually pushes)
```
Remove the GHCR login step from `build-arm64` entirely (lines 222-228). The `parity-guard` job already has `packages: write` and its own login, so the gated push is unaffected.

---

### WR-02: `_extract_api_metadata_action_step` matches the first metadata-action across all jobs, not specifically `build-and-push`

**File:** `tests/test_deployment/test_agent_compose.py:151-156`
**Issue:** The helper iterates `workflow_data["jobs"].values()` and returns the **first** `docker/metadata-action` step found across all jobs (line 154: `return step`). Phase 47 added two more jobs with metadata-action steps (`build-arm64` at line 232 and `parity-golden-x86` at line 334). YAML insertion order is preserved by `yaml.safe_load`, and `build-and-push` is currently first, so the test works today.

If jobs are ever reordered — or if someone glances at the helper name "extract API metadata action step" and assumes it specifically validates the x86 `build-and-push` job — the function silently returns a different job's step without failing or warning. `test_docker_publish_workflow_tags_both_latest_and_version` (which uses this helper) would then validate the wrong tag strategy.

**Fix:**
```python
def _extract_api_metadata_action_step(workflow_data: dict[str, Any]) -> dict[str, Any] | None:
    """Locate the docker/metadata-action step in the build-and-push job."""
    job = (workflow_data.get("jobs") or {}).get("build-and-push")
    if not isinstance(job, dict):
        return None
    for step in job.get("steps", []) or []:
        uses = (step.get("uses") or "").lower()
        if "docker/metadata-action" in uses:
            return step  # type: ignore[no-any-return]
    return None
```
Targeting `build-and-push` explicitly makes the helper robust to job reordering and matches the function's name and its callers' intent.

---

## Info

### IN-01: `uv:0.11.23` in `Dockerfile.agent-arm64` is pinned by tag, not digest

**File:** `Dockerfile.agent-arm64:132`
**Issue:** The uv binary is copied via:
```dockerfile
COPY --from=ghcr.io/astral-sh/uv:0.11.23 /uv /uvx /bin/
```
A version tag on GHCR can theoretically be mutated (unlike immutable digests). The Dockerfile pins all other supply-chain inputs by SHA or exact version (essentia SHA, `tensorflow==2.20.0`); this is the only image reference that does not use a digest.

**Fix:**
```dockerfile
# Resolve once: docker pull ghcr.io/astral-sh/uv:0.11.23 --platform linux/arm64 && docker inspect ...
COPY --from=ghcr.io/astral-sh/uv:0.11.23@sha256:<digest> /uv /uvx /bin/
```
Alternatively, document this as an accepted residual risk in the threat model (the uv copy is resolver-only, not a runtime attack surface in the final image since uv is not in the PATH after the build layers that use it).

---

_Reviewed: 2026-06-24_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
