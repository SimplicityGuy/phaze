---
phase: quick-260606-n0y
plan: 01
subsystem: ci-deployment
tags: [ghcr, docker, ci, cleanup, tests]
requires:
  - .github/workflows/docker-publish.yml (published image_suffix matrix — source of truth)
provides:
  - cleanup-images.yml prunes the canonical published packages {phaze, phaze/audfprint, phaze/panako}
  - deployment.md authoritative-vs-deprecated GHCR image-path statement
  - publish/cleanup package-set parity guard test
affects:
  - .github/workflows/cleanup-images.yml
  - docs/deployment.md
  - tests/test_deployment/test_agent_compose.py
tech-stack:
  added: []
  patterns:
    - YAML-parse guard test deriving published set from docker-publish matrix and asserting parity with cleanup matrix
key-files:
  created: []
  modified:
    - .github/workflows/cleanup-images.yml
    - docs/deployment.md
    - tests/test_deployment/test_agent_compose.py
decisions:
  - "Cleanup prunes the bare-repo `phaze` package (api/worker/watcher image), not the orphan `phaze/api`"
  - "keep-n-tagged: 2 + older-than: 30 days retained — prunes only old/untagged versions, never released :v<version>/:latest"
metrics:
  duration: ~6m
  completed: 2026-06-06
---

# Quick 260606-n0y: Reconcile GHCR Image Paths Summary

Fixed cleanup-images.yml to prune the canonical published `phaze` package instead of the dead `phaze/api` orphan, documented the authoritative-vs-deprecated GHCR paths in deployment.md, and added a parity guard test so the publish and cleanup package lists can no longer silently drift.

## What Changed

### Task 1 — cleanup-images.yml points at published packages (commit aee0628)
In `jobs.cleanup.strategy.matrix.package`, replaced the orphan `- phaze/api` with the canonical bare-repo `- phaze`, keeping `- phaze/audfprint` and `- phaze/panako`. All `with:` config (`delete-partial-images`, `delete-untagged`, `keep-n-tagged: 2`, `older-than: 30 days`, action SHA pin `f092b48...` v1.2.1) is unchanged. The bug was that cleanup pruned the dead `phaze/api` path and never pruned the live bare `phaze` package, so its untagged/old versions accumulated forever.

### Task 2 — deployment.md authoritative path note (commit 56e56ec)
Added an "Authoritative image paths" bullet in the `### docker-publish.yml` subsection of `## Build Pipeline`: `ghcr.io/simplicityguy/phaze` is authoritative (api/worker/watcher); `/audfprint` and `/panako` are sidecars; `phaze/api` is deprecated/orphaned and must not be pulled or referenced. The line-1 `<!-- generated-by: gsd-doc-writer -->` marker is intact.

### Task 3 — publish/cleanup parity guard test (commit a993aea)
Added `CLEANUP_WORKFLOW_PATH` constant and `test_cleanup_package_list_matches_published_images` to `tests/test_deployment/test_agent_compose.py`. It derives the published set as `{("phaze" + entry["image_suffix"]).rstrip("/")}` over `docker-publish.yml`'s `jobs.build-and-push.strategy.matrix.include` and asserts it equals `cleanup-images.yml`'s `jobs.cleanup.strategy.matrix.package` set, with a symmetric-difference message naming whichever side diverged.

## Verification

- `uv run pytest tests/test_deployment/test_agent_compose.py -q` → 6 passed (including new parity guard).
- Drift check confirmed the guard genuinely fails on divergence: the old buggy set `{phaze/api, phaze/audfprint, phaze/panako}` differs from the published `{phaze, phaze/audfprint, phaze/panako}` by `{phaze, phaze/api}`.
- All frozen-SHA pre-commit hooks (actionlint, yamllint strict, check-jsonschema, ruff, mypy) passed on each commit. No `--no-verify` used.

## Deviations from Plan

None — plan executed exactly as written. The plan referenced the publish job as `jobs.build-and-push`, which was verified to match the actual job key before writing the test.

## Out-of-Scope Maintainer Follow-up (manual ops, NOT a repo change)

The stale `ghcr.io/simplicityguy/phaze/api` package still exists in GHCR and is not removable from the repo. The cleanup workflow can only prune versions of packages in its matrix; it cannot delete an entire orphaned package. A maintainer with package-admin auth must delete it once, via the GitHub package UI or:

```
gh api --method DELETE /user/packages/container/phaze%2Fapi
```

If the package is org-owned (it is, under SimplicityGuy), use the org-scoped variant instead:

```
gh api --method DELETE /orgs/SimplicityGuy/packages/container/phaze%2Fapi
```

This cannot be done from the repository and must be performed manually by the maintainer.

## Self-Check: PASSED

- FOUND: .github/workflows/cleanup-images.yml (matrix = {phaze, phaze/audfprint, phaze/panako})
- FOUND: docs/deployment.md (deprecated + phaze/api + gsd-doc-writer marker)
- FOUND: tests/test_deployment/test_agent_compose.py (test_cleanup_package_list_matches_published_images)
- FOUND commit aee0628 (Task 1)
- FOUND commit 56e56ec (Task 2)
- FOUND commit a993aea (Task 3)
